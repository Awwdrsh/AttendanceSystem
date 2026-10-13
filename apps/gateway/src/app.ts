import express, { type Express } from 'express';
import { pinoHttp } from 'pino-http';
import type { Logger } from 'pino';
import { buildOptions } from './adms/options.js';
import { directionOf, parseAttlog, parseDeviceCmdResults, verifyModeOf } from './adms/parse.js';
import { buildPunch } from './punch/event.js';
import type { Signer } from './punch/signer.js';
import type { Store } from './store/types.js';

export interface GatewayDeps {
  store: Store;
  signer: Signer;
  logger?: Logger;
  serverVersion?: string;
}

/**
 * ZKTeco ADMS/PUSH endpoints (plan §7.3, Appendix B). The device is the HTTP client:
 *   GET  /iclock/cdata?SN=..&options=all   handshake -> options block (approved devices only)
 *   POST /iclock/cdata?SN=..&table=ATTLOG  attendance records -> "OK: n"
 *   POST /iclock/cdata?SN=..&table=OPERLOG operation log -> "OK: 1"
 *   GET  /iclock/getrequest?SN=..          command poll / heartbeat -> "C:id:CMD" lines or "OK"
 *   POST /iclock/devicecmd                 command results
 *   POST /iclock/fdata                     templates / photos (acknowledged; encrypted storage is a later piece)
 */
export function createGatewayApp({ store, signer, logger, serverVersion = 'hajiri-gw/0.0.1' }: GatewayDeps): Express {
  const app = express();
  app.disable('x-powered-by');
  if (logger) app.use(pinoHttp({ logger }));
  // Devices send raw tab-separated text with odd or missing content types; accept everything as text (1 MB cap).
  app.use('/iclock', express.text({ type: () => true, limit: '1mb' }));

  const text = (body: string) => ({ 'Content-Type': 'text/plain; charset=utf-8', body });

  app.get('/health', (_req, res) => { res.json({ ok: true, signer: signer.ephemeral ? 'ephemeral' : 'configured' }); });
  app.get('/public-key', (_req, res) => { res.type('text/plain').send(signer.publicKeyPem); });

  app.get('/iclock/cdata', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    if (!sn) return res.status(400).type('text/plain').send('ERROR: SN required');
    const pushVersion = req.query.pushver ? String(req.query.pushver) : undefined;
    let device = await store.getDevice(sn);
    if (!device) {
      device = await store.registerPending(sn, { vendor: 'zkteco', protocol: 'adms', ...(pushVersion ? { pushVersion } : {}) });
      logger?.warn({ sn }, 'unknown device registered as pending');
    }
    await store.touch(sn, { lastSeenAt: new Date(), lastHandshakeAt: new Date(), ...(pushVersion ? { pushVersion } : {}) });
    if (device.status !== 'active') {
      // Not approved: acknowledge so the device keeps polling, but send no options and accept no data.
      const t = text('OK');
      return res.set('Content-Type', t['Content-Type']).send(t.body);
    }
    const t = text(buildOptions(sn, { attlogStamp: device.attlogStamp, oplogStamp: device.oplogStamp, serverVersion }));
    return res.set('Content-Type', t['Content-Type']).send(t.body);
  });

  app.post('/iclock/cdata', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    const table = String(req.query.table ?? '').toUpperCase();
    const device = sn ? await store.getDevice(sn) : null;
    if (!device || device.status !== 'active') {
      logger?.warn({ sn, table }, 'data from unapproved device rejected');
      return res.status(403).type('text/plain').send('ERROR: device not approved');
    }
    await store.touch(sn, { lastSeenAt: new Date() });
    const body = typeof req.body === 'string' ? req.body : '';

    if (table === 'ATTLOG') {
      const { records, malformed } = parseAttlog(body);
      let inserted = 0, duplicate = 0, near = 0, unmapped = 0;
      for (const r of records) {
        const verifyMode = verifyModeOf(r.verifyCode);
        const base = {
          deviceId: device.id, deviceSn: sn, vendor: device.vendor, deviceUserPin: r.pin, employeeId: null as string | null,
          deviceLocalTime: r.deviceLocalTime, direction: directionOf(r.statusCode), verifyMode, source: device.vendor,
          raw: { line: r.line, statusCode: r.statusCode, verifyCode: r.verifyCode, workcode: r.workcode },
        };
        const punch = buildPunch(base, device.tzOffsetMin, signer);
        punch.employeeId = await store.resolveEmployee(device.id, r.pin, punch.localDate);
        if (!punch.employeeId) unmapped++;
        const outcome = await store.insertPunch(punch);
        if (outcome === 'inserted') inserted++; else if (outcome === 'duplicate') duplicate++; else near++;
      }
      const stamp = req.query.Stamp ? String(req.query.Stamp) : undefined;
      if (stamp) await store.touch(sn, { attlogStamp: stamp });
      logger?.info({ sn, records: records.length, inserted, duplicate, near, unmapped, malformed: malformed.length }, 'ATTLOG');
      if (malformed.length) logger?.warn({ sn, malformed }, 'malformed ATTLOG lines skipped');
      return res.type('text/plain').send(`OK: ${records.length}`);
    }

    if (table === 'OPERLOG') {
      const stamp = req.query.OpStamp ? String(req.query.OpStamp) : undefined;
      if (stamp) await store.touch(sn, { oplogStamp: stamp });
      logger?.info({ sn, bytes: body.length }, 'OPERLOG received (archive to object store: TODO)');
      return res.type('text/plain').send('OK: 1');
    }

    logger?.info({ sn, table, bytes: body.length }, 'unhandled cdata table acknowledged');
    return res.type('text/plain').send('OK: 1');
  });

  app.get('/iclock/getrequest', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    const device = sn ? await store.getDevice(sn) : null;
    if (!device) return res.status(404).type('text/plain').send('ERROR: unknown device');
    await store.touch(sn, { lastSeenAt: new Date() });
    if (device.status !== 'active') return res.type('text/plain').send('OK');
    const cmds = await store.popQueuedCommands(device.id);
    if (cmds.length === 0) return res.type('text/plain').send('OK');
    return res.type('text/plain').send(cmds.map((c) => `C:${c.id}:${c.command}`).join('\n') + '\n');
  });

  app.post('/iclock/devicecmd', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    if (sn) await store.touch(sn, { lastSeenAt: new Date() });
    for (const r of parseDeviceCmdResults(typeof req.body === 'string' ? req.body : '')) {
      await store.ackCommand(r.id, r.returnCode);
      logger?.info({ sn, ...r }, 'device command result');
    }
    res.type('text/plain').send('OK');
  });

  app.post('/iclock/fdata', async (req, res) => {
    const sn = String(req.query.SN ?? '').trim();
    // Biometric templates must only ever be stored envelope-encrypted (plan §9.1 decision 5). Until that store exists,
    // acknowledge and discard; never log the body.
    logger?.info({ sn, bytes: typeof req.body === 'string' ? req.body.length : 0 }, 'fdata acknowledged and discarded');
    res.type('text/plain').send('OK');
  });

  return app;
}
