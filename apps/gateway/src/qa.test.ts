import { mkdtempSync, readdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { beforeEach, describe, expect, it } from 'vitest';
import request from 'supertest';
import { parseAttlog, isValidDeviceTime } from './adms/parse.js';
import { createGatewayApp } from './app.js';
import { RawArchive, safeSerial } from './archive.js';
import { extractEventJson, parseMultipart, xmlEventToJson, parseIsoWithOffset } from './hikvision/parse.js';
import { deviceTimeToUtc } from './punch/event.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';
import { assertAllowedCommand, COMMAND_ACK_TIMEOUT_MS, type Store } from './store/types.js';

const signer = Signer.ephemeral();
let store: MemoryStore; let deviceId: string;
beforeEach(() => {
  store = new MemoryStore();
  deviceId = store.addDevice({ serialNo: 'DEMO-01' }).id;
  store.mappings.set(`${deviceId}:1`, 'emp-1');
});
const attlog = (app: ReturnType<typeof createGatewayApp>, sn: string, body: string) =>
  request(app).post(`/iclock/cdata?SN=${encodeURIComponent(sn)}&table=ATTLOG`).set('Content-Type', 'text/plain').send(body);

describe('serial numbers and archive paths', () => {
  it('sanitises serials so they can never leave the archive directory', () => {
    expect(safeSerial('..')).toBe('unknown');
    expect(safeSerial('../../etc')).toBe('______etc');
    expect(safeSerial('A/B\\C.D')).toBe('A_B_C_D');
    expect(safeSerial('x'.repeat(100))).toHaveLength(64);
  });
  it('archive never writes outside its directory and caps files per day', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'hajiri-raw-'));
    const a = new RawArchive(dir);
    const f = a.append('../../escape', 'cdata', 'x');
    expect(f && path.resolve(f).startsWith(path.resolve(dir) + path.sep)).toBe(true);
    expect(existsSync(path.join(dir, '..', 'escape'))).toBe(false);
    expect(readdirSync(dir)).toEqual(['______escape']);
  });
  it('rejects over-long or hostile serials at the edge', async () => {
    const app = createGatewayApp({ store, signer });
    expect((await request(app).get(`/iclock/cdata?SN=${'A'.repeat(65)}&options=all`)).status).toBe(400);
    expect((await request(app).get(`/iclock/cdata?SN=${encodeURIComponent('../x')}&options=all`)).status).toBe(400);
    expect((await request(app).get(`/iclock/getrequest?SN=${encodeURIComponent('a\nb')}`)).status).toBe(404);
    expect(store.devices.size).toBe(1);
  });
});

describe('command safety', () => {
  it('refuses multi-line, zero-width-prefixed and case-varied SHELL, and CONTROL DEVICE unless enabled', () => {
    expect(() => assertAllowedCommand('DATA UPDATE USERINFO PIN=1\nSHELL reboot')).toThrow(/single line/);
    expect(() => assertAllowedCommand('CHECK\r\nSHELL ls')).toThrow(/single line/);
    expect(() => assertAllowedCommand('​SHELL ls')).toThrow(/SHELL/);
    expect(() => assertAllowedCommand('\u0000shell ls')).toThrow(/SHELL/);
    expect(() => assertAllowedCommand('  Shell')).toThrow(/SHELL/);
    expect(() => assertAllowedCommand('CONTROL DEVICE 010101FF05')).toThrow(/dangerous/);
    expect(() => assertAllowedCommand('CONTROL DEVICE 010101FF05', true)).not.toThrow();
    expect(() => assertAllowedCommand('')).toThrow(/empty/);
    expect(() => assertAllowedCommand('DATA UPDATE USERINFO PIN=1\tName=Sita\tPri=0')).not.toThrow();
  });
  it('re-sends an unacknowledged command once after the ack timeout, then fails it', async () => {
    const id = await store.enqueueCommand(deviceId, 'CHECK');
    const t0 = new Date('2026-09-21T04:00:00Z');
    expect(await store.popQueuedCommands(deviceId, t0)).toEqual([{ id, command: 'CHECK' }]);
    expect(await store.popQueuedCommands(deviceId, new Date(t0.getTime() + 60_000))).toEqual([]);
    const t1 = new Date(t0.getTime() + COMMAND_ACK_TIMEOUT_MS + 1000);
    expect(await store.popQueuedCommands(deviceId, t1)).toEqual([{ id, command: 'CHECK' }]);
    const t2 = new Date(t1.getTime() + COMMAND_ACK_TIMEOUT_MS + 1000);
    expect(await store.popQueuedCommands(deviceId, t2)).toEqual([]);
    expect(store.commands[0]).toMatchObject({ status: 'failed', attempts: 2 });
    // a late ack for a failed command is ignored
    await store.ackCommand(id, 0);
    expect(store.commands[0]!.status).toBe('failed');
  });
});

describe('time validation', () => {
  it('rejects calendar-impossible device times instead of rolling them over', () => {
    expect(isValidDeviceTime('2026-13-40 10:00:00')).toBe(false);
    expect(isValidDeviceTime('2026-02-29 10:00:00')).toBe(false);
    expect(isValidDeviceTime('2028-02-29 10:00:00')).toBe(true);
    expect(isValidDeviceTime('2026-09-21 24:00:00')).toBe(false);
    expect(parseAttlog('1\t2026-13-40 10:00:00\t0\t1\n').malformed).toHaveLength(1);
    expect(() => deviceTimeToUtc('2026-02-30 10:00:00', 345)).toThrow(RangeError);
    expect(parseIsoWithOffset('2026-02-30T10:00:00+05:45')).toBeNull();
  });
  it('accepts very old buffered punches (replay after an outage) without skew flags', async () => {
    const app = createGatewayApp({ store, signer, maxClockSkewMin: 10 });
    const r = await attlog(app, 'DEMO-01', '1\t2024-01-05 10:00:00\t0\t1\n');
    expect(r.text).toBe('OK: 1');
    expect(store.punches[0]!.raw.clockSkew).toBeUndefined();
    expect(store.devices.get('DEMO-01')!.clockDriftSec).toBeUndefined();   // old replays never update drift
  });
  it('honours the per-device timezone offset', async () => {
    const d = store.addDevice({ serialNo: 'UTC-01', tzOffsetMin: 0 });
    store.mappings.set(`${d.id}:1`, 'emp-1');
    const app = createGatewayApp({ store, signer });
    await attlog(app, 'UTC-01', '1\t2026-09-21 10:17:03\t0\t1\n');
    expect(store.punches[0]!.punchedAt.toISOString()).toBe('2026-09-21T10:17:03.000Z');
  });
});

describe('storage outage', () => {
  it('returns 503 plain text when the store is unreachable so the terminal retries', async () => {
    const down: Store = { ...store, getDevice: async () => { throw Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:5433'), { code: 'ECONNREFUSED' }); } } as unknown as Store;
    const app = createGatewayApp({ store: down, signer });
    const r = await attlog(app, 'DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\n');
    expect(r.status).toBe(503);
    expect(r.text).toMatch(/^ERROR/);
    expect(r.headers['content-type']).toMatch(/text\/plain/);
  });
});

describe('Hikvision: shared key, XML, LF multipart', () => {
  const hikEvent = { ipAddress: '10.0.0.5', dateTime: '2026-09-21T10:17:03+05:45', eventType: 'AccessControllerEvent',
    AccessControllerEvent: { majorEventType: 5, subEventType: 75, employeeNoString: '1', attendanceStatus: 'checkIn' } };
  const XML = `<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
<ipAddress>10.0.0.5</ipAddress><portNo>80</portNo><protocol>HTTP</protocol><channelID>1</channelID>
<dateTime>2026-09-21T10:17:03+05:45</dateTime><activePostCount>1</activePostCount>
<eventType>AccessControllerEvent</eventType><eventState>active</eventState>
<AccessControllerEvent><deviceName>Front &amp; Back</deviceName><majorEventType>5</majorEventType><subEventType>113</subEventType>
<employeeNoString>1</employeeNoString><serialNo>HIK-01</serialNo><currentVerifyMode>fingerPrint</currentVerifyMode><attendanceStatus>checkOut</attendanceStatus></AccessControllerEvent>
</EventNotificationAlert>`;

  it('requires the per-device key when one is configured, constant-time compared', async () => {
    const d = store.addDevice({ serialNo: 'HIK-01', vendor: 'hikvision', protocol: 'isapi', commKeyHash: createHash('sha256').update('s3cret').digest('hex') });
    store.mappings.set(`${d.id}:1`, 'emp-1');
    const app = createGatewayApp({ store, signer });
    expect((await request(app).post('/hikvision/event?sn=HIK-01').send(hikEvent)).status).toBe(403);
    expect((await request(app).post('/hikvision/event?sn=HIK-01&key=wrong').send(hikEvent)).status).toBe(403);
    expect(store.punches).toHaveLength(0);
    expect((await request(app).post('/hikvision/event?sn=HIK-01&key=s3cret').send(hikEvent)).status).toBe(200);
    expect((await request(app).post('/hikvision/event?sn=HIK-01').set('x-device-key', 's3cret').send({ ...hikEvent, dateTime: '2026-09-21T11:00:00+05:45' })).status).toBe(200);
    expect(store.punches).toHaveLength(2);
  });
  it('ADMS devices are not blocked by a configured key (the protocol cannot carry one)', async () => {
    store.devices.get('DEMO-01')!.commKeyHash = 'abc';
    const app = createGatewayApp({ store, signer });
    expect((await attlog(app, 'DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\n')).status).toBe(200);
  });
  it('parses XML events from older firmware into the same shape and ingests them', async () => {
    const j = xmlEventToJson(XML)!;
    expect(j).toMatchObject({ fromXml: true, dateTime: '2026-09-21T10:17:03+05:45', eventType: 'AccessControllerEvent' });
    expect(j.AccessControllerEvent).toMatchObject({ majorEventType: '5', subEventType: '113', employeeNoString: '1', deviceName: 'Front & Back', attendanceStatus: 'checkOut' });
    const d = store.addDevice({ serialNo: 'HIK-01', vendor: 'hikvision', protocol: 'isapi' });
    store.mappings.set(`${d.id}:1`, 'emp-1');
    const app = createGatewayApp({ store, signer });
    const r = await request(app).post('/hikvision/event?sn=HIK-01').set('Content-Type', 'application/xml').send(XML);
    expect(r.status).toBe(200);
    expect(store.punches).toHaveLength(1);
    expect(store.punches[0]).toMatchObject({ verifyMode: 'fingerprint', direction: 'out', employeeId: 'emp-1', deviceLocalTime: '2026-09-21 10:17:03' });
    expect(xmlEventToJson('<other/>')).toBeNull();
  });
  it('multipart with LF-only separators and a picture part is handled with bounded memory', async () => {
    const d = store.addDevice({ serialNo: 'HIK-01', vendor: 'hikvision', protocol: 'isapi' });
    store.mappings.set(`${d.id}:1`, 'emp-1');
    const app = createGatewayApp({ store, signer });
    const b = 'xyz';
    const body = Buffer.concat([
      Buffer.from(`--${b}\nContent-Disposition: form-data; name="event_log"\nContent-Type: application/json\n\n${JSON.stringify(hikEvent)}\n`),
      Buffer.from(`--${b}\nContent-Disposition: form-data; name="Picture"; filename="p.jpg"\nContent-Type: image/jpeg\n\n`), Buffer.alloc(200_000, 0xff),
      Buffer.from(`\n--${b}--\n`),
    ]);
    const parts = parseMultipart(body, `multipart/form-data; boundary=${b}`);
    expect([...parts.keys()]).toEqual(['event_log', 'Picture']);
    const r = await request(app).post('/hikvision/event?sn=HIK-01').set('Content-Type', `multipart/form-data; boundary=${b}`).send(body);
    expect(r.status).toBe(200);
    expect(store.punches).toHaveLength(1);
    expect(extractEventJson(Buffer.from('   '), 'application/json')).toBeNull();
  });
  it('unknown serials are not archived (disk-fill protection) and events with impossible dates are counted malformed, not 500', async () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'hajiri-raw-'));
    const app = createGatewayApp({ store, signer, archive: new RawArchive(dir) });
    await request(app).post('/hikvision/event?sn=NEW-77').send(hikEvent);
    expect(existsSync(path.join(dir, 'NEW-77'))).toBe(false);
    const d = store.addDevice({ serialNo: 'HIK-01', vendor: 'hikvision', protocol: 'isapi' });
    store.mappings.set(`${d.id}:1`, 'emp-1');
    const r = await request(app).post('/hikvision/event?sn=HIK-01').send({ ...hikEvent, dateTime: '2026-02-30T10:00:00+05:45' });
    expect(r.status).toBe(200);
    expect(store.punches).toHaveLength(0);
    expect(existsSync(path.join(dir, 'HIK-01'))).toBe(true);
  });
});
