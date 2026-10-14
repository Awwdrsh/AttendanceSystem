import express, { type Express, type NextFunction, type Request, type Response } from 'express';
import { pinoHttp } from 'pino-http';
import type { Logger } from 'pino';
import { createHash, timingSafeEqual } from 'node:crypto';
import { buildOptions } from './adms/options.js';
import { directionOf, driftSeconds, parseAttlog, parseDeviceCmdResults, verifyModeOf } from './adms/parse.js';
import { parseTemplateUpload } from './adms/templates.js';
import { RawArchive, safeSerial } from './archive.js';
import { aadHash, templateAad, type TemplateCipher } from './crypto.js';
import { extractEventJson, parseHikEvent } from './hikvision/parse.js';
import { buildPunch, type NormalizedPunch } from './punch/event.js';
import type { Signer } from './punch/signer.js';
import type { DeviceRecord, Store } from './store/types.js';

export interface GatewayDeps {
  store: Store;
  signer: Signer;
  logger?: Logger;
  serverVersion?: string;
  /** Template encryption; when absent, templates are discarded (never stored in plaintext). */
  cipher?: TemplateCipher | null;
  archive?: RawArchive;
  maxClockSkewMin?: number;
}

const TEMPLATE_TABLES = new Set(['FINGERTMP', 'FACE', 'BIODATA', 'FP', 'ATTPHOTO', 'USERPIC', 'BIOPHOTO']);

/**
 * Device-facing HTTP endpoints. The device is always the HTTP client (plan §7.3).
 *   ZKTeco ADMS/PUSH (Appendix B):
 *     GET  /iclock/cdata?SN=..&options=all    handshake -> options block (approved devices only)
 *     POST /iclock/cdata?SN=..&table=ATTLOG   attendance records -> "OK: n"
 *     POST /iclock/cdata?SN=..&table=OPERLOG  operation log incl. FP/FACE/BIODATA template lines -> "OK: 1"
 *     GET  /iclock/getrequest?SN=..           command poll / heartbeat -> "C:id:CMD" lines or "OK"
 *     POST /iclock/devicecmd                  command results
 *     POST /iclock/fdata?SN=..&table=..       templates / photos
 *   Hikvision ISAPI listening host (Appendix C):
 *     POST /hikvision/event?sn=..             JSON or multipart AccessControllerEvent / heartbeat
 */
/**
 * Days a punch can change: its own date and, for a morning punch (before 12:00 Nepal time), the previous date, because a
 * night shift or an overtime punch-out after midnight belongs to the day the shift started.
 */
export function affectedDates(punchedAt: Date, localDate: string): string[] {
  const nptHour = new Date(punchedAt.getTime() + 345 * 60_000).getUTCHours();
  if (nptHour >= 12) return [localDate];
  return [new Date(Date.parse(`${localDate}T00:00:00Z`) - 86_400_000).toISOString().slice(0, 10), localDate];
}

export function createGatewayApp(deps: GatewayDeps): Express {
  const { store, signer, logger, serverVersion = 'hajiri-gw/0.1.0' } = deps;
  const cipher = deps.cipher ?? null;
  const archive = deps.archive ?? new RawArchive(null);
  const maxSkewMs = (deps.maxClockSkewMin ?? 10) * 60_000;

  const app = express();
  app.disable('x-powered-by');
  // Request logs carry method and path only: the Hikvision device key travels in the query string or x-device-key header.
  if (logger) app.use(pinoHttp({ logger, autoLogging: { ignore: (req) => (req.url ?? '').startsWith('/iclock/getrequest') || req.url === '/health' },
    serializers: { req: (req: { method?: string; url?: string }) => ({ method: req.method, url: redactUrl(req.url ?? '') }), res: (res: { statusCode?: number }) => ({ statusCode: res.statusCode }) } }));
  // Devices send raw tab-separated text with odd or missing content types; accept everything as text.
  app.use('/iclock/fdata', express.text({ type: () => true, limit: '10mb' }));
  app.use('/iclock', express.text({ type: () => true, limit: '2mb' }));
  app.use('/hikvision', express.raw({ type: () => true, limit: '10mb' }));

  const plain = (res: Response, body: string, status = 200) => res.status(status).type('text/plain').send(body);
  /** ADMS cursors are echoed back in the handshake options block, so only plain digits are accepted (no line injection). */
  const stampOf = (v: unknown): string | undefined => (typeof v === 'string' && /^\d{1,20}$/.test(v) ? v : undefined);
  /** Firmware version strings are stored and shown to admins: printable and short. */
  const versionOf = (v: unknown): string | undefined => (typeof v === 'string' && /^[\w.() -]{1,32}$/.test(v) ? v : undefined);

  /** Serial numbers: printable, bounded, no path or log tricks. Returns null when unacceptable. */
  const serialOf = (v: unknown): string | null => {
    const sn = String(v ?? '').trim();
    return sn && sn.length <= 64 && /^[A-Za-z0-9_.:-]+$/.test(sn) && safeSerial(sn) !== 'unknown' ? sn : null;
  };

  /**
   * Per-device shared secret (plan §12.5). ADMS terminals cannot send one, so it is enforced only for other protocols
   * (Hikvision carries it in the listening-host URL: ?sn=..&key=..). Compared constant-time against sha256(key).
   */
  const commKeyOk = (device: DeviceRecord, req: Request): boolean => {
    if (!device.commKeyHash || device.protocol === 'adms') return true;
    const presented = String(req.query.key ?? req.get('x-device-key') ?? '');
    if (!presented) return false;
    const a = Buffer.from(createHash('sha256').update(presented, 'utf8').digest('hex'));
    const b = Buffer.from(device.commKeyHash.toLowerCase());
    return a.length === b.length && timingSafeEqual(a, b);
  };

  app.get('/health', (_req, res) => { res.json({ ok: true, signer: signer.ephemeral ? 'ephemeral' : 'configured', templates: cipher ? 'encrypted' : 'discarded' }); });
  app.get('/public-key', (_req, res) => { res.type('text/plain').send(signer.publicKeyPem); });

  /** Shared ingest for every vendor: normalise, sign, dedupe, store, queue recompute. */
  async function ingest(device: DeviceRecord, base: Omit<NormalizedPunch, 'punchedAt' | 'localDate' | 'idempotencyKey' | 'gatewaySignature'>, tzOffsetMin: number, counters: Counters) {
    let punch: NormalizedPunch;
    try { punch = buildPunch(base, tzOffsetMin, signer); } catch { counters.malformed++; return null; }
    const skewMs = punch.punchedAt.getTime() - Date.now();
    // A reset clock (factory 2000-01-01) or one days ahead cannot be a real punch; storing it would put attendance on the
    // wrong day. Rejected outright (the raw archive keeps the line); smaller future skews are stored but flagged.
    if (punch.punchedAt.getTime() < MIN_PUNCH_MS || skewMs > MAX_FUTURE_MS) { counters.rejected++; return null; }
    if (skewMs > maxSkewMs) {
      punch.raw = { ...punch.raw, clockSkew: true, clockSkewMs: skewMs };
      counters.skewed++;
    }
    punch.employeeId = await store.resolveEmployee(device.id, base.deviceUserPin, punch.localDate);
    if (!punch.employeeId) counters.unmapped++;
    const outcome = await store.insertPunch(punch);
    if (outcome === 'inserted') {
      counters.inserted++;
      if (punch.employeeId) for (const d of affectedDates(punch.punchedAt, punch.localDate)) await store.enqueueRecompute(punch.employeeId, d, `punch:${device.vendor}`);
    } else if (outcome === 'duplicate') counters.duplicate++;
    else counters.near++;
    return punch;
  }

  /** Encrypt and store template uploads; discard photos; never log bodies (plan §9.1 decision 5). */
  async function storeTemplates(device: DeviceRecord, table: string, body: string) {
    const parsed = parseTemplateUpload(table, body);
    let stored = 0, discarded = 0, noConsent = 0;
    for (const t of parsed.templates) {
      if (!cipher || !t.valid) { discarded++; continue; }
      // A template is kept only for a mapped employee with an active consent for that modality; after a withdrawal
      // (or before consent is given) the device's upload is dropped. The raw archive is unaffected.
      const employeeId = await store.resolveEmployee(device.id, t.pin, new Date().toISOString().slice(0, 10));
      const consentId = employeeId ? await store.activeConsentId(employeeId, t.modality) : null;
      if (!employeeId || !consentId) { discarded++; noConsent++; continue; }
      const aad = templateAad(employeeId, t.modality);
      await store.insertTemplate({
        deviceId: device.id, deviceUserPin: t.pin, employeeId, modality: t.modality, vendorFormat: t.vendorFormat,
        ciphertext: cipher.encrypt(t.templateB64, aad), keyVersion: cipher.keyVersion, aadHash: aadHash(aad), consentId,
      });
      stored++;
    }
    if (noConsent) logger?.warn({ sn: device.serialNo, count: noConsent }, 'templates discarded: no active biometric consent for the mapped employee');
    if (parsed.templates.length && !cipher) logger?.warn({ sn: device.serialNo, count: parsed.templates.length }, 'templates discarded: DATA_ENCRYPTION_KEY not configured');
    logger?.info({ sn: device.serialNo, table, stored, discarded, photos: parsed.photos, users: parsed.users, unknown: parsed.unknown }, 'template upload');
    return { stored, discarded, ...parsed };
  }

  // ------------------------------------------------------------------ ZKTeco ADMS
  app.get('/iclock/cdata', async (req, res) => {
    const sn = serialOf(req.query.SN);
    if (!sn) return plain(res, 'ERROR: SN required', 400);
    const pushVersion = versionOf(req.query.pushver);
    let device = await store.getDevice(sn);
    if (!device) {
      device = await store.registerPending(sn, { vendor: 'zkteco', protocol: 'adms', ...(pushVersion ? { pushVersion } : {}) });
      if (!device) { logger?.warn({ sn }, 'pending device cap reached; registration refused'); return plain(res, 'ERROR: too many pending devices', 403); }
      logger?.warn({ sn }, 'unknown device registered as pending');
    }
    await store.touch(sn, { lastSeenAt: new Date(), lastHandshakeAt: new Date(), ...(pushVersion ? { pushVersion } : {}) });
    // Not approved: acknowledge so the device keeps polling, but send no options and accept no data.
    if (device.status !== 'active') return plain(res, 'OK');
    return plain(res, buildOptions(sn, { attlogStamp: device.attlogStamp, oplogStamp: device.oplogStamp, serverVersion }));
  });

  app.post('/iclock/cdata', async (req, res) => {
    const sn = serialOf(req.query.SN);
    const table = String(req.query.table ?? '').toUpperCase().slice(0, 32);
    const body = typeof req.body === 'string' ? req.body : '';
    const device = sn ? await store.getDevice(sn) : null;
    if (!device || device.status !== 'active') {
      logger?.warn({ sn, table }, 'data from unapproved device rejected');
      return plain(res, 'ERROR: device not approved', 403);
    }
    archive.append(sn!, `cdata:${table || 'none'}`, body);
    await store.touch(sn!, { lastSeenAt: new Date() });

    if (table === 'ATTLOG') {
      const { records, malformed } = parseAttlog(body);
      const c = newCounters();
      let newest = '';
      for (const r of records) {
        await ingest(device, {
          deviceId: device.id, deviceSn: sn!, vendor: device.vendor, deviceUserPin: r.pin, employeeId: null,
          deviceLocalTime: r.deviceLocalTime, direction: directionOf(r.statusCode), verifyMode: verifyModeOf(r.verifyCode), source: device.vendor,
          raw: { line: r.line, statusCode: r.statusCode, verifyCode: r.verifyCode, workcode: r.workcode },
        }, device.tzOffsetMin, c);
        if (r.deviceLocalTime > newest) newest = r.deviceLocalTime;
      }
      const stamp = stampOf(req.query.Stamp);
      if (stamp) await store.touch(sn!, { attlogStamp: stamp });
      if (newest && c.inserted > 0) {
        // Only fresh (non-buffered) records say anything about the clock; buffered replays are old by definition.
        const drift = driftSeconds(newest, device.tzOffsetMin);
        if (drift > -600) await store.touch(sn!, { clockDriftSec: drift });
        if (Math.abs(drift) > 60 && drift > -600) logger?.warn({ sn, driftSec: drift }, 'device clock drift above 60 s');
      }
      logger?.info({ sn, records: records.length, ...c, malformed: malformed.length + c.malformed }, 'ATTLOG');
      if (malformed.length) logger?.warn({ sn, malformed: malformed.slice(0, 5) }, 'malformed ATTLOG lines skipped');
      return plain(res, `OK: ${records.length}`);
    }

    if (table === 'OPERLOG') {
      const stamp = stampOf(req.query.OpStamp);
      if (stamp) await store.touch(sn!, { oplogStamp: stamp });
      await storeTemplates(device, table, body);
      return plain(res, 'OK: 1');
    }

    if (TEMPLATE_TABLES.has(table)) {
      await storeTemplates(device, table, body);
      return plain(res, 'OK: 1');
    }

    logger?.info({ sn, table, bytes: body.length }, 'unhandled cdata table acknowledged');
    return plain(res, 'OK: 1');
  });

  app.get('/iclock/getrequest', async (req, res) => {
    const sn = serialOf(req.query.SN);
    const device = sn ? await store.getDevice(sn) : null;
    if (!device) return plain(res, 'ERROR: unknown device', 404);
    await store.touch(sn!, { lastSeenAt: new Date() });
    if (device.status !== 'active') return plain(res, 'OK');
    const cmds = await store.popQueuedCommands(device.id);
    if (cmds.length === 0) return plain(res, 'OK');
    return plain(res, cmds.map((c) => `C:${c.id}:${c.command}`).join('\n') + '\n');
  });

  app.post('/iclock/devicecmd', async (req, res) => {
    const sn = serialOf(req.query.SN);
    const body = typeof req.body === 'string' ? req.body : '';
    // Results are accepted only from an approved device and only for that device's own commands; otherwise anyone on
    // the network could mark another terminal's commands as done or failed.
    const device = sn ? await store.getDevice(sn) : null;
    if (!device || device.status !== 'active') return plain(res, 'ERROR: device not approved', 403);
    archive.append(sn!, 'devicecmd', body);
    await store.touch(sn!, { lastSeenAt: new Date() });
    for (const r of parseDeviceCmdResults(body)) {
      await store.ackCommand(device.id, r.id, r.returnCode);
      logger?.info({ sn, ...r }, 'device command result');
    }
    return plain(res, 'OK');
  });

  app.post('/iclock/fdata', async (req, res) => {
    const sn = serialOf(req.query.SN);
    const table = String(req.query.table ?? '').toUpperCase().slice(0, 32);
    const body = typeof req.body === 'string' ? req.body : '';
    const device = sn ? await store.getDevice(sn) : null;
    if (!device || device.status !== 'active') return plain(res, 'ERROR: device not approved', 403);
    archive.append(sn!, `fdata:${table || 'none'}`, body);
    await store.touch(sn!, { lastSeenAt: new Date() });
    await storeTemplates(device, table, body);
    return plain(res, 'OK');
  });

  // ------------------------------------------------------------------ Hikvision ISAPI listening host
  app.post('/hikvision/event', async (req, res) => {
    const body: Buffer = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
    const contentType = req.get('content-type') ?? '';
    const json = extractEventJson(body, contentType);
    const ev = parseHikEvent(json ?? {});
    const sn = serialOf(req.query.sn) ?? (ev.serialNo ? serialOf(ev.serialNo) : null);
    if (!sn) return plain(res, 'ERROR: serial number required (use ?sn=)', 400);
    let device = await store.getDevice(sn);
    if (!device) {
      device = await store.registerPending(sn, { vendor: 'hikvision', protocol: 'isapi' });
      if (!device) { logger?.warn({ sn }, 'pending device cap reached; registration refused'); return plain(res, 'ERROR: too many pending devices', 403); }
      logger?.warn({ sn }, 'unknown hikvision device registered as pending');
    }
    if (device.status !== 'active') { await store.touch(sn, { lastSeenAt: new Date() }); return plain(res, 'ERROR: device not approved', 403); }
    if (!commKeyOk(device, req)) { logger?.warn({ sn }, 'device key missing or wrong'); return plain(res, 'ERROR: device key rejected', 403); }
    // Archived and marked seen only once authenticated (unknown or unauthenticated senders could otherwise fill the disk).
    archive.append(sn, 'hikvision', body);
    await store.touch(sn, { lastSeenAt: new Date() });
    if (ev.kind === 'heartbeat') return plain(res, 'OK');
    if (ev.kind !== 'attendance' || !ev.pin || !ev.deviceLocalTime) { logger?.debug({ sn, major: ev.major, minor: ev.minor, xml: ev.raw.fromXml === true }, 'non-attendance event ignored'); return plain(res, 'OK'); }
    const c = newCounters();
    await ingest(device, {
      deviceId: device.id, deviceSn: sn, vendor: device.vendor, deviceUserPin: ev.pin, employeeId: null,
      deviceLocalTime: ev.deviceLocalTime, direction: ev.direction, verifyMode: ev.verifyMode, source: device.vendor, raw: ev.raw,
    }, ev.tzOffsetMin ?? device.tzOffsetMin, c);
    if (c.inserted > 0) {
      const drift = driftSeconds(ev.deviceLocalTime, ev.tzOffsetMin ?? device.tzOffsetMin);
      if (drift > -600) await store.touch(sn, { clockDriftSec: drift });
      if (Math.abs(drift) > 60 && drift > -600) logger?.warn({ sn, driftSec: drift }, 'device clock drift above 60 s');
    }
    logger?.info({ sn, ...c }, 'hikvision event');
    return plain(res, 'OK');
  });

  // Devices must get plain text, never an HTML error page.
  app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
    const e = err as { type?: string; message?: string; code?: string };
    if (e.type === 'entity.too.large') return plain(res, 'ERROR: body too large', 413);
    if (isStorageUnavailable(e)) { logger?.error({ code: e.code, path: req.path }, 'storage unavailable'); return plain(res, 'ERROR: storage unavailable, retry later', 503); }
    logger?.error({ err, path: req.path }, 'gateway error');
    return plain(res, 'ERROR', 500);
  });

  return app;
}

/** pg connection/admin-shutdown errors: the terminal keeps the record buffered and retries (plan §7.7). */
function isStorageUnavailable(e: { code?: string; message?: string }): boolean {
  const codes = new Set(['ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT', 'ENOTFOUND', 'EAI_AGAIN', '57P01', '57P02', '57P03', '53300', '08000', '08003', '08006', '08001']);
  return (!!e.code && codes.has(e.code)) || /terminat|connection|timeout/i.test(e.message ?? '') && !/invalid/i.test(e.message ?? '');
}

interface Counters { inserted: number; duplicate: number; near: number; unmapped: number; skewed: number; malformed: number; rejected: number }
const newCounters = (): Counters => ({ inserted: 0, duplicate: 0, near: 0, unmapped: 0, skewed: 0, malformed: 0, rejected: 0 });

/** Punches before 2020 or more than a day in the future are refused (device clock reset or badly wrong). */
const MIN_PUNCH_MS = Date.UTC(2020, 0, 1);
const MAX_FUTURE_MS = 24 * 3600_000;

/** Drop secrets from a logged URL: the Hikvision listening-host URL may carry ?key=. */
export function redactUrl(url: string): string {
  return url.replace(/([?&](?:key|token|password|pwd)=)[^&]*/gi, '$1[redacted]');
}
