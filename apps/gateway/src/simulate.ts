/**
 * ZKTeco ADMS device simulator (plan §15.3). Performs the handshake, pushes ATTLOG lines and polls for commands.
 *   pnpm --filter @hajiri/gateway simulate -- --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"
 * Repeat --pin/--time pairs to send several records; --poll N polls getrequest N times.
 */
const args = process.argv.slice(2);
const opt = (name: string, def?: string) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] ?? def : def; };
const all = (name: string) => args.flatMap((a, i) => (a === `--${name}` ? [args[i + 1] ?? ''] : []));

const url = opt('url', 'http://localhost:8080')!;
const sn = opt('sn', 'DEMO-01')!;
const pins = all('pin');
const times = all('time');
const polls = Number(opt('poll', '1'));
const verify = opt('verify', '1')!;
const status = opt('status', '0')!;

const log = (label: string, r: Response, body: string) => console.log(`${label} -> ${r.status}\n${body.trimEnd()}\n`);

const hs = await fetch(`${url}/iclock/cdata?SN=${encodeURIComponent(sn)}&options=all&pushver=2.4.1&language=69`);
log('handshake', hs, await hs.text());

if (pins.length) {
  const lines = pins.map((pin, i) => `${pin}\t${times[i] ?? times[0] ?? '2026-09-21 10:00:00'}\t${status}\t${verify}\t0`).join('\n') + '\n';
  const up = await fetch(`${url}/iclock/cdata?SN=${encodeURIComponent(sn)}&table=ATTLOG&Stamp=${Date.now()}`, {
    method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: lines,
  });
  log(`ATTLOG (${pins.length} record(s))`, up, await up.text());
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
