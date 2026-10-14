import { beforeEach, describe, expect, it } from 'vitest';
import request from 'supertest';
import { createGatewayApp } from './app.js';
import { parseHikEvent, parseIsoWithOffset, parseMultipart } from './hikvision/parse.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';

const signer = Signer.ephemeral();
let store: MemoryStore; let app: ReturnType<typeof createGatewayApp>; let deviceId: string;

beforeEach(() => {
  store = new MemoryStore();
  deviceId = store.addDevice({ serialNo: 'HIK-01', vendor: 'hikvision', protocol: 'isapi' }).id;
  store.mappings.set(`${deviceId}:1`, 'emp-1');
  app = createGatewayApp({ store, signer });
});

const event = (over: Record<string, unknown> = {}, ace: Record<string, unknown> = {}) => ({
  ipAddress: '10.0.0.5', dateTime: '2026-09-21T10:17:03+05:45', eventType: 'AccessControllerEvent', eventState: 'active',
  AccessControllerEvent: { deviceName: 'Front door', majorEventType: 5, subEventType: 75, employeeNoString: '1', serialNo: 'HIK-01', currentVerifyMode: 'face', attendanceStatus: 'checkIn', ...ace },
  ...over,
});

describe('Hikvision ISAPI listening host', () => {
  it('ingests a JSON face event with the +05:45 offset and queues recompute', async () => {
    const r = await request(app).post('/hikvision/event?sn=HIK-01').send(event());
    expect(r.status).toBe(200);
    expect(store.punches).toHaveLength(1);
    expect(store.punches[0]).toMatchObject({ vendor: 'hikvision', deviceUserPin: '1', employeeId: 'emp-1', verifyMode: 'face', direction: 'in', localDate: '2026-09-21', deviceLocalTime: '2026-09-21 10:17:03' });
    expect(store.punches[0]!.punchedAt.toISOString()).toBe('2026-09-21T04:32:03.000Z');
    // A morning punch also re-evaluates the previous day (a night shift's punch-out belongs to the day it started).
    expect(store.recomputeQueue).toEqual([{ employeeId: 'emp-1', localDate: '2026-09-20', reason: 'punch:hikvision' }, { employeeId: 'emp-1', localDate: '2026-09-21', reason: 'punch:hikvision' }]);
  });

  it('dedupes resent events and does not dedupe across devices', async () => {
    await request(app).post('/hikvision/event?sn=HIK-01').send(event());
    await request(app).post('/hikvision/event?sn=HIK-01').send(event());
    await request(app).post('/hikvision/event?sn=HIK-01').send(event({ dateTime: '2026-09-21T10:17:06+05:45' }));
    expect(store.punches).toHaveLength(1);
    store.addDevice({ serialNo: 'HIK-02', vendor: 'hikvision', protocol: 'isapi' });
    await request(app).post('/hikvision/event?sn=HIK-02').send(event());
    expect(store.punches).toHaveLength(2);
  });

  it('parses multipart with an event_log part and a picture part (picture discarded)', async () => {
    const boundary = 'xx-boundary-7d';
    const body = Buffer.concat([
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="event_log"\r\nContent-Type: application/json\r\n\r\n${JSON.stringify(event({}, { subEventType: 113 }))}\r\n`),
      Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="Picture"; filename="1.jpg"\r\nContent-Type: image/jpeg\r\n\r\n`),
      Buffer.from([0xff, 0xd8, 0xff, 0x00, 0x0d, 0x0a, 0x01]),
      Buffer.from(`\r\n--${boundary}--\r\n`),
    ]);
    const r = await request(app).post('/hikvision/event?sn=HIK-01').set('Content-Type', `multipart/form-data; boundary=${boundary}`).send(body);
    expect(r.status).toBe(200);
    expect(store.punches).toHaveLength(1);
    expect(store.punches[0]!.verifyMode).toBe('fingerprint');
  });

  it('heartbeat only touches last_seen; non-attendance events are ignored; XML is acknowledged', async () => {
    await request(app).post('/hikvision/event?sn=HIK-01').send({ ipAddress: '10.0.0.5', dateTime: '2026-09-21T10:00:00+05:45', eventType: 'heartBeat' });
    expect(store.devices.get('HIK-01')!.lastSeenAt).toBeInstanceOf(Date);
    await request(app).post('/hikvision/event?sn=HIK-01').send(event({}, { subEventType: 76 }));   // face auth failed
    await request(app).post('/hikvision/event?sn=HIK-01').set('Content-Type', 'application/xml').send('<EventNotificationAlert><eventType>x</eventType></EventNotificationAlert>');
    expect(store.punches).toHaveLength(0);
  });

  it('unknown serial becomes pending (vendor hikvision) and its events are rejected; serial from body when no ?sn', async () => {
    const r = await request(app).post('/hikvision/event').send(event({}, { serialNo: 'HIK-NEW' }));
    expect(r.status).toBe(403);
    expect(store.devices.get('HIK-NEW')).toMatchObject({ vendor: 'hikvision', protocol: 'isapi', status: 'pending' });
    expect((await request(app).post('/hikvision/event').send({ eventType: 'heartBeat' })).status).toBe(400);
  });

  it('parser helpers', () => {
    expect(parseIsoWithOffset('2026-09-21T10:17:03+05:45')).toEqual({ local: '2026-09-21 10:17:03', offsetMin: 345 });
    expect(parseIsoWithOffset('2026-09-21T04:32:03Z')).toEqual({ local: '2026-09-21 04:32:03', offsetMin: 0 });
    expect(parseIsoWithOffset('2026-09-21 10:17:03')).toEqual({ local: '2026-09-21 10:17:03', offsetMin: null });
    expect(parseHikEvent(event({}, { subEventType: 38, attendanceStatus: 'checkOut' }))).toMatchObject({ kind: 'attendance', verifyMode: 'card', direction: 'out' });
    expect(parseHikEvent({ eventType: 'heartBeat' }).kind).toBe('heartbeat');
    const parts = parseMultipart(Buffer.from('--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nhello\r\n--b--\r\n'), 'multipart/form-data; boundary=b');
    expect(parts.get('a')!.data.toString()).toBe('hello');
  });
});
