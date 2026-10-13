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

const TIME_RE = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;

export function parseAttlog(body: string): AttlogParseResult {
  const records: AttlogRecord[] = [];
  const malformed: string[] = [];
  for (const rawLine of body.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) continue;
    const cols = line.split('\t');
    const pin = cols[0]?.trim() ?? '';
    const time = cols[1]?.trim() ?? '';
    const statusCode = Number(cols[2]);
    const verifyCode = Number(cols[3]);
    if (!pin || !TIME_RE.test(time) || !Number.isInteger(statusCode) || !Number.isInteger(verifyCode)) {
      malformed.push(line);
      continue;
    }
    records.push({ pin, deviceLocalTime: time, statusCode, verifyCode, workcode: cols[4]?.trim() || null, line });
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
