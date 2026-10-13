import express, { type Express, type NextFunction, type Request, type Response } from 'express';
import { pinoHttp } from 'pino-http';
import type { Logger } from 'pino';
import { buildOptions } from './adms/options.js';
import { directionOf, driftSeconds, parseAttlog, parseDeviceCmdResults, verifyModeOf } from './adms/parse.js';
import { parseTemplateUpload } from './adms/templates.js';
import { RawArchive } from './archive.js';
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
export function createGatewayApp(deps: GatewayDeps): Express {
  const { store, signer, logger, serverVersion = 'hajiri-gw/0.1.0' } = deps;
  const cipher = deps.cipher ?? null;
  const archive = deps.archive ?? new RawArchive(null);
  const maxSkewMs = (deps.maxClockSkewMin ?? 10) * 60_000;

  const app = express();
  app.disable('x-powered-by');
  if (logger) app.use(pinoHttp({ logger, autoLogging: { ignore: (req) => (req.url ?? '').startsWith('/iclock/getrequest') || req.url === '/health' } }));
  // Devices send raw tab-separated text with odd or missing content types; accept everything as text.
  app.use('/iclock/fdata', express.text({ type: () => true, limit: '10mb' }));
  app.use('/iclock', express.text({ type: () => true, limit: '2mb' }));
  app.use('/hikvision', express.raw({ type: () => true, limit: '10mb' }));

  const plain = (res: Response, body: string, status = 200) => res.status(status).type('text/plain').send(body);

  app.get('/health', (_req, res) => { res.json({ ok: true, signer: signer.ephemeral ? 'ephemeral' : 'configured', templates: cipher ? 'encrypted' : 'discarded' }); });
  app.get('/public-key', (_req, res) => { res.type('text/plain').send(signer.publicKeyPem); });

  /** Shared ingest for every vendor: normalise, sign, dedupe, store, queue recompute. */
  async function ingest(device: DeviceRecord, base: Omit<NormalizedPunch, 'punchedAt' | 'localDate' | 'idempotencyKey' | 'gatewaySignature'>, tzOffsetMin: number, counters: Counters) {
    const punch = buildPunch(base, tzOffsetMin, signer);
    const skewMs = punch.punchedAt.getTime() - Date.now();
    if (skewMs > maxSkewMs) {
      punch.raw = { ...punch.raw, clockSkew: true, clockSkewMs: skewMs };
      counters.skewed++;
    }
    punch.employeeId = await store.resolveEmployee(device.id, base.deviceUserPin, punch.localDate);
    if (!punch.employeeId) counters.unmapped++;
    const outcome = await store.insertPunch(punch);
    if (outcome === 'inserted') {
      counters.inserted++;
      if (punch.employeeId) await store.enqueueRecompute(punch.employeeId, punch.localDate, `punch:${device.vendor}`);
    } else if (outcome === 'duplicate') counters.duplicate++;
    else counters.near++;
    return punch;
  }

  /** Encrypt and store template uploads; discard photos; never log bodies (plan §9.1 decision 5). */
  async function storeTemplates(device: DeviceRecord, table: string, body: string) {
    const parsed = parseTemplateUpload(table, body);
    let stored = 0, discarded = 0;
    for (const t of parsed.templates) {
      if (!cipher || !t.valid) { discarded++; continue; }
      const employeeId = await store.resolveEmployee(device.id, t.pin, new Date().toISOString().slice(0, 10));
      const aad = templateAad(employeeId ?? `pin:${device.serialNo}:${t.pin}`, t.modality);
      await store.insertTemplate({
        deviceId: device.id, deviceUserPin: t.pin, employeeId, modality: t.modality, vendorFormat: t.vendorFormat,
        ciphertext: cipher.encrypt(t.templateB64, aad), keyVersion: cipher.keyVersion, aadHash: aadHash(aad),
      });
      stored++;
    }
    if (parsed.templates.length && !cipher) logger?.warn({ sn: device.serialNo, count: parsed.templates.length }, 'templates discarded: DATA_ENCRYPTION_KEY not configured');
    logger?.info({ sn: device.serialNo, table, stored, discarded, photos: parsed.photos, users: parsed.users, unknown: parsed.unknown }, 'template upload');
    return { stored, discarded, ...parsed };
  }

  // ------------------------------------------------------------------ ZKTeco ADMS
  app.get('/iclock/cdata', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    if (!sn) return plain(res, 'ERROR: SN required', 400);
    const pushVersion = req.query.pushver ? String(req.query.pushver) : undefined;
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
    const sn = String(req.query.SN ?? '').trim();
    const table = String(req.query.table ?? '').toUpperCase();
    const body = typeof req.body === 'string' ? req.body : '';
    const device = sn ? await store.getDevice(sn) : null;
    if (!device || device.status !== 'active') {
      logger?.warn({ sn, table }, 'data from unapproved device rejected');
      return plain(res, 'ERROR: device not approved', 403);
    }
    archive.append(sn, `cdata:${table || 'none'}`, body);
    await store.touch(sn, { lastSeenAt: new Date() });

    if (table === 'ATTLOG') {
      const { records, malformed } = parseAttlog(body);
      const c = newCounters();
      let newest = '';
      for (const r of records) {
        await ingest(device, {
          deviceId: device.id, deviceSn: sn, vendor: device.vendor, deviceUserPin: r.pin, employeeId: null,
          deviceLocalTime: r.deviceLocalTime, direction: directionOf(r.statusCode), verifyMode: verifyModeOf(r.verifyCode), source: device.vendor,
          raw: { line: r.line, statusCode: r.statusCode, verifyCode: r.verifyCode, workcode: r.workcode },
        }, device.tzOffsetMin, c);
        if (r.deviceLocalTime > newest) newest = r.deviceLocalTime;
      }
      const stamp = req.query.Stamp ? String(req.query.Stamp) : undefined;
      if (stamp) await store.touch(sn, { attlogStamp: stamp });
      if (newest && c.inserted > 0) {
        // Only fresh (non-buffered) records say anything about the clock; buffered replays are old by definition.
        const drift = driftSeconds(newest, device.tzOffsetMin);
        if (drift > 0 || drift > -600) await store.touch(sn, { clockDriftSec: drift });
        if (Math.abs(drift) > 60 && drift > -600) logger?.warn({ sn, driftSec: drift }, 'device clock drift above 60 s');
      }
      logger?.info({ sn, records: records.length, ...c, malformed: malformed.length }, 'ATTLOG');
      if (malformed.length) logger?.warn({ sn, malformed: malformed.slice(0, 5) }, 'malformed ATTLOG lines skipped');
      return plain(res, `OK: ${records.length}`);
    }

    if (table === 'OPERLOG') {
      const stamp = req.query.OpStamp ? String(req.query.OpStamp) : undefined;
      if (stamp) await store.touch(sn, { oplogStamp: stamp });
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
    const sn = String(req.query.SN ?? '').trim();
    const device = sn ? await store.getDevice(sn) : null;
    if (!device) return plain(res, 'ERROR: unknown device', 404);
    await store.touch(sn, { lastSeenAt: new Date() });
    if (device.status !== 'active') return plain(res, 'OK');
    const cmds = await store.popQueuedCommands(device.id);
    if (cmds.length === 0) return plain(res, 'OK');
    return plain(res, cmds.map((c) => `C:${c.id}:${c.command}`).join('\n') + '\n');
  });

  app.post('/iclock/devicecmd', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    const body = typeof req.body === 'string' ? req.body : '';
    if (sn) { archive.append(sn, 'devicecmd', body); await store.touch(sn, { lastSeenAt: new Date() }); }
    for (const r of parseDeviceCmdResults(body)) {
      await store.ackCommand(r.id, r.returnCode);
      logger?.info({ sn, ...r }, 'device command result');
    }
    return plain(res, 'OK');
  });

  app.post('/iclock/fdata', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    const table = String(req.query.table ?? '').toUpperCase();
    const body = typeof req.body === 'string' ? req.body : '';
    const device = sn ? await store.getDevice(sn) : null;
    if (!device || device.status !== 'active') return plain(res, 'ERROR: device not approved', 403);
    archive.append(sn, `fdata:${table || 'none'}`, body);
    await store.touch(sn, { lastSeenAt: new Date() });
    await storeTemplates(device, table, body);
    return plain(res, 'OK');
  });

  // ------------------------------------------------------------------ Hikvision ISAPI listening host
  app.post('/hikvision/event', async (req, res) => {
    const body: Buffer = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
    const contentType = req.get('content-type') ?? '';
    const json = extractEventJson(body, contentType);
    const ev = parseHikEvent(json && typeof json === 'object' && !('xml' in (json as object)) ? json : {});
    const sn = String(req.query.sn ?? '').trim() || ev.serialNo || '';
    if (!sn) return plain(res, 'ERROR: serial number required (use ?sn=)', 400);
    archive.append(sn, 'hikvision', body);
    let device = await store.getDevice(sn);
    if (!device) {
      device = await store.registerPending(sn, { vendor: 'hikvision', protocol: 'isapi' });
      if (!device) { logger?.warn({ sn }, 'pending device cap reached; registration refused'); return plain(res, 'ERROR: too many pending devices', 403); }
      logger?.warn({ sn }, 'unknown hikvision device registered as pending');
    }
    await store.touch(sn, { lastSeenAt: new Date() });
    if (device.status !== 'active') return plain(res, 'ERROR: device not approved', 403);
    if (json && typeof json === 'object' && 'xml' in (json as object)) { logger?.warn({ sn }, 'XML event archived but not parsed (configure JSON format)'); return plain(res, 'OK'); }
    if (ev.kind === 'heartbeat') return plain(res, 'OK');
    if (ev.kind !== 'attendance' || !ev.pin || !ev.deviceLocalTime) { logger?.debug({ sn, major: ev.major, minor: ev.minor }, 'non-attendance event ignored'); return plain(res, 'OK'); }
    const c = newCounters();
    await ingest(device, {
      deviceId: device.id, deviceSn: sn, vendor: device.vendor, deviceUserPin: ev.pin, employeeId: null,
      deviceLocalTime: ev.deviceLocalTime, direction: ev.direction, verifyMode: ev.verifyMode, source: device.vendor, raw: ev.raw,
    }, ev.tzOffsetMin ?? device.tzOffsetMin, c);
    if (c.inserted > 0) {
      const drift = driftSeconds(ev.deviceLocalTime, ev.tzOffsetMin ?? device.tzOffsetMin);
      await store.touch(sn, { clockDriftSec: drift });
      if (Math.abs(drift) > 60) logger?.warn({ sn, driftSec: drift }, 'device clock drift above 60 s');
    }
    logger?.info({ sn, ...c }, 'hikvision event');
    return plain(res, 'OK');
  });

  // Devices must get plain text, never an HTML error page.
  app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
    const e = err as { type?: string; message?: string };
    if (e.type === 'entity.too.large') return plain(res, 'ERROR: body too large', 413);
    logger?.error({ err, path: req.path }, 'gateway error');
    return plain(res, 'ERROR', 500);
  });

  return app;
}

interface Counters { inserted: number; duplicate: number; near: number; unmapped: number; skewed: number }
const newCounters = (): Counters => ({ inserted: 0, duplicate: 0, near: 0, unmapped: 0, skewed: 0 });
