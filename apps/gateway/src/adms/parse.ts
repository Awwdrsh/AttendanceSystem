/**
 * ZKTeco ADMS/PUSH ATTLOG parsing (plan Appendix B).
 * Body: one record per line, tab-separated: PIN \t YYYY-MM-DD HH:MM:SS \t status \t verify [\t workcode ...]
 * Status and verify code tables vary by firmware; unknown codes are kept as 'code:N' and must be verified on
 * purchased devices (plan §7.3, Appendix B "verify on purchased firmware").
 */
export interface AttlogRecord {
  pin: string;
  deviceLocalTime: string;
  statusCode: number;
  verifyCode: number;
  workcode: string | null;
  line: string;
}

export interface AttlogParseResult {
  records: AttlogRecord[];
  malformed: string[];
}

const TIME_RE = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/;

/** 'YYYY-MM-DD HH:MM:SS' that is a real calendar time (rejects 2026-13-40 24:61:00 instead of rolling over). */
export function isValidDeviceTime(s: string): boolean {
  const m = TIME_RE.exec(s);
  if (!m) return false;
  const [y, mo, d, h, mi, sec] = m.slice(1).map(Number) as [number, number, number, number, number, number];
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || sec > 59) return false;
  const t = new Date(Date.UTC(y, mo - 1, d, h, mi, sec));
  return t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d;
}
const INT_RE = /^-?\d{1,6}$/;

export function parseAttlog(body: string): AttlogParseResult {
  const records: AttlogRecord[] = [];
  const malformed: string[] = [];
  for (const rawLine of body.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const cols = line.split('\t');
    const pin = cols[0]?.trim() ?? '';
    const time = cols[1]?.trim() ?? '';
    const status = cols[2]?.trim() ?? '';
    const verify = cols[3]?.trim() ?? '';
    if (!pin || pin.length > 24 || !isValidDeviceTime(time) || !INT_RE.test(status) || !INT_RE.test(verify)) {
      malformed.push(line);
      continue;
    }
    records.push({ pin, deviceLocalTime: time, statusCode: Number(status), verifyCode: Number(verify), workcode: cols[4]?.trim() || null, line });
  }
  return { records, malformed };
}

/** ADMS status: 0 check-in, 1 check-out, 2 break-out, 3 break-in, 4 OT-in, 5 OT-out. */
export function directionOf(statusCode: number): 'in' | 'out' | 'unknown' {
  switch (statusCode) {
    case 0: case 3: case 4: return 'in';
    case 1: case 2: case 5: return 'out';
    default: return 'unknown';
  }
}

const VERIFY: Record<number, string> = {
  0: 'password', 1: 'fingerprint', 2: 'card', 3: 'password', 4: 'card', 15: 'face', 25: 'palm',
};

export function verifyModeOf(verifyCode: number): string {
  return VERIFY[verifyCode] ?? `code:${verifyCode}`;
}

/** Body of POST /iclock/devicecmd: one or more lines of ID=..&Return=..&CMD=.. */
export function parseDeviceCmdResults(body: string): Array<{ id: number; returnCode: number; cmd: string }> {
  const out: Array<{ id: number; returnCode: number; cmd: string }> = [];
  for (const line of body.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const p = new URLSearchParams(line.trim());
    const id = Number(p.get('ID'));
    const returnCode = Number(p.get('Return'));
    if (!Number.isInteger(id) || !Number.isInteger(returnCode)) continue;
    out.push({ id, returnCode, cmd: p.get('CMD') ?? '' });
  }
  return out;
}

/** 'YYYY-MM-DD HH:MM:SS' device time minus server time, in seconds (positive = device ahead). */
export function driftSeconds(deviceLocalTime: string, tzOffsetMin: number, now = Date.now()): number {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(deviceLocalTime);
  if (!m) return 0;
  const asUtc = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6])) - tzOffsetMin * 60_000;
  return Math.round((asUtc - now) / 1000);
}
