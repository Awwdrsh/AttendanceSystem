/**
 * Terminal simulator (plan §15.3). ZKTeco ADMS (default) or Hikvision ISAPI (--vendor hikvision).
 *   pnpm --filter @hajiri/gateway simulate -- --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"
 *   pnpm --filter @hajiri/gateway simulate -- --vendor hikvision --sn HIK-01 --pin 1 --time "2026-09-21 10:17:03"
 * Repeat --pin/--time pairs to send several records; --poll N polls getrequest N times (ADMS only);
 * --template sends a fake fingerprint template line in an OPERLOG upload (ADMS only).
 */
const args = process.argv.slice(2);
const opt = (name: string, def?: string) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] ?? def : def; };
const all = (name: string) => args.flatMap((a, i) => (a === `--${name}` ? [args[i + 1] ?? ''] : []));

const url = opt('url', 'http://localhost:8080')!;
const sn = opt('sn', 'DEMO-01')!;
const vendor = opt('vendor', 'zkteco')!;
const pins = all('pin');
const times = all('time');
const polls = Number(opt('poll', '1'));
const verify = opt('verify', '1')!;
const status = opt('status', '0')!;

const log = (label: string, r: Response, body: string) => console.log(`${label} -> ${r.status}\n${body.trimEnd()}\n`);

if (vendor === 'hikvision') {
  const hb = await fetch(`${url}/hikvision/event?sn=${encodeURIComponent(sn)}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ipAddress: '10.0.0.5', dateTime: new Date().toISOString(), eventType: 'heartBeat' }),
  });
  log('heartbeat', hb, await hb.text());
  for (const [i, pin] of pins.entries()) {
    const t = (times[i] ?? times[0] ?? '2026-09-21 10:00:00').replace(' ', 'T') + '+05:45';
    const ev = { ipAddress: '10.0.0.5', dateTime: t, eventType: 'AccessControllerEvent', eventState: 'active',
      AccessControllerEvent: { deviceName: 'Front door', majorEventType: 5, subEventType: 75, employeeNoString: pin, serialNo: sn, currentVerifyMode: 'face', attendanceStatus: 'checkIn' } };
    const r = await fetch(`${url}/hikvision/event?sn=${encodeURIComponent(sn)}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(ev) });
    log(`event pin ${pin}`, r, await r.text());
  }
} else {
  const hs = await fetch(`${url}/iclock/cdata?SN=${encodeURIComponent(sn)}&options=all&pushver=2.4.1&language=69`);
  log('handshake', hs, await hs.text());

  if (pins.length) {
    const lines = pins.map((pin, i) => `${pin}\t${times[i] ?? times[0] ?? '2026-09-21 10:00:00'}\t${status}\t${verify}\t0`).join('\n') + '\n';
    const up = await fetch(`${url}/iclock/cdata?SN=${encodeURIComponent(sn)}&table=ATTLOG&Stamp=${Date.now()}`, {
      method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: lines,
    });
    log(`ATTLOG (${pins.length} record(s))`, up, await up.text());
  }

  if (args.includes('--template')) {
    const fake = Buffer.from(`simulated-template-${Date.now()}`).toString('base64');
    const up = await fetch(`${url}/iclock/cdata?SN=${encodeURIComponent(sn)}&table=OPERLOG&OpStamp=${Date.now()}`, {
      method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: `FP PIN=${pins[0] ?? '1'}\tFID=6\tSize=${fake.length}\tValid=1\tTMP=${fake}\n`,
    });
    log('OPERLOG template', up, await up.text());
  }

  for (let i = 0; i < polls; i++) {
    const gr = await fetch(`${url}/iclock/getrequest?SN=${encodeURIComponent(sn)}`);
    const body = await gr.text();
    log('getrequest', gr, body);
    for (const line of body.split('\n')) {
      const m = /^C:(\d+):(.*)$/.exec(line);
      if (m) {
        const dc = await fetch(`${url}/iclock/devicecmd?SN=${encodeURIComponent(sn)}`, {
          method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: `ID=${m[1]}&Return=0&CMD=${encodeURIComponent(m[2]!.split(' ')[0]!)}`,
        });
        log(`devicecmd ack ${m[1]}`, dc, await dc.text());
      }
    }
  }
}
