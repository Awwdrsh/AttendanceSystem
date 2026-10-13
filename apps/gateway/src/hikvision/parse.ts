/**
 * Hikvision ISAPI "HTTP listening host" events (plan Appendix C). The terminal POSTs JSON (or multipart with an
 * event_log JSON part plus an optional Picture part). Field names come from community captures, not official docs
 * (NDA): verify on the purchased unit. Attendance filter: majorEventType 5 with subEventType 75 (face), 113
 * (fingerprint), 38 (card).
 */
export interface HikEvent {
  kind: 'attendance' | 'heartbeat' | 'other';
  serialNo: string | null;
  deviceLocalTime: string | null;      // 'YYYY-MM-DD HH:MM:SS'
  tzOffsetMin: number | null;          // from the dateTime offset, e.g. +05:45
  pin: string | null;
  verifyMode: string;
  direction: 'in' | 'out' | 'unknown';
  major: number | null;
  minor: number | null;
  raw: Record<string, unknown>;
}

const ATTENDANCE_MINOR: Record<number, string> = { 75: 'face', 113: 'fingerprint', 38: 'card', 1: 'card' };

const DIRECTION: Record<string, 'in' | 'out'> = {
  checkin: 'in', checkout: 'out', breakout: 'out', breakin: 'in', overtimein: 'in', overtimeout: 'out', overtimein_: 'in',
};

/** Parse '2026-09-21T10:17:03+05:45' into local time text and the offset in minutes. */
export function parseIsoWithOffset(s: string): { local: string; offsetMin: number | null } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/.exec(s.trim());
  if (!m) return null;
  if (!isRealDateTime(Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6]))) return null;
  let offsetMin: number | null = null;
  const z = m[7];
  if (z === 'Z') offsetMin = 0;
  else if (z) { const sign = z.startsWith('-') ? -1 : 1; const hh = Number(z.slice(1, 3)); const mm = Number(z.slice(-2)); offsetMin = sign * (hh * 60 + mm); }
  return { local: `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`, offsetMin };
}

/** Calendar-valid components (no 2026-13-40 roll-over). */
export function isRealDateTime(y: number, mo: number, d: number, h: number, mi: number, sec: number): boolean {
  if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || sec > 59) return false;
  const t = new Date(Date.UTC(y, mo - 1, d, h, mi, sec));
  return t.getUTCFullYear() === y && t.getUTCMonth() === mo - 1 && t.getUTCDate() === d;
}

type J = Record<string, unknown>;
const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : typeof v === 'number' ? String(v) : null);
const num = (v: unknown): number | null => (typeof v === 'number' ? v : typeof v === 'string' && /^\d+$/.test(v) ? Number(v) : null);

export function parseHikEvent(json: unknown): HikEvent {
  const root = (json && typeof json === 'object' ? json : {}) as J;
  const ace = (root.AccessControllerEvent && typeof root.AccessControllerEvent === 'object' ? root.AccessControllerEvent : {}) as J;
  const eventType = str(root.eventType)?.toLowerCase() ?? '';
  const major = num(ace.majorEventType);
  const minor = num(ace.subEventType);
  const serialNo = str(root.serialNo) ?? str(ace.serialNo) ?? str(ace.deviceID) ?? str(root.deviceID) ?? null;
  const dt = str(root.dateTime) ?? str(ace.dateTime);
  const parsed = dt ? parseIsoWithOffset(dt) : null;
  const base = {
    serialNo, deviceLocalTime: parsed?.local ?? null, tzOffsetMin: parsed?.offsetMin ?? null, major, minor,
    raw: { eventType: root.eventType, major, minor, employeeNoString: ace.employeeNoString, cardNo: ace.cardNo, currentVerifyMode: ace.currentVerifyMode, attendanceStatus: ace.attendanceStatus, deviceName: ace.deviceName, ipAddress: root.ipAddress },
  };
  if (eventType === 'heartbeat' || (eventType === 'accesscontrollerevent' && major === 5 && minor === 0) || (major === null && minor === null && !ace.employeeNoString)) {
    return { ...base, kind: 'heartbeat', pin: null, verifyMode: 'none', direction: 'unknown' };
  }
  const pin = str(ace.employeeNoString) ?? str(ace.employeeNo) ?? null;
  const verify = major === 5 && minor !== null ? ATTENDANCE_MINOR[minor] : undefined;
  if (!verify || !pin || !parsed) return { ...base, kind: 'other', pin, verifyMode: verify ?? 'none', direction: 'unknown' };
  const att = str(ace.attendanceStatus)?.toLowerCase().replace(/[^a-z]/g, '') ?? '';
  return { ...base, kind: 'attendance', pin, verifyMode: verify, direction: DIRECTION[att] ?? 'unknown' };
}

/** Minimal multipart/form-data splitter (no dependencies): returns name -> body buffer for each part. */
export function parseMultipart(body: Buffer, contentType: string): Map<string, { headers: string; data: Buffer }> {
  const parts = new Map<string, { headers: string; data: Buffer }>();
  const bm = /boundary="?([^";]+)"?/i.exec(contentType);
  if (!bm) return parts;
  const boundary = Buffer.from(`--${bm[1]}`);
  let pos = body.indexOf(boundary);
  while (pos !== -1) {
    const start = pos + boundary.length;
    if (body.subarray(start, start + 2).toString() === '--') break;           // closing boundary
    const next = body.indexOf(boundary, start);
    const segment = body.subarray(start, next === -1 ? body.length : next);
    // Header/body separator may be CRLF CRLF (spec) or LF LF (some firmware).
    let headerEnd = segment.indexOf('\r\n\r\n'); let sepLen = 4;
    if (headerEnd === -1) { headerEnd = segment.indexOf('\n\n'); sepLen = 2; }
    if (headerEnd !== -1) {
      const headers = segment.subarray(0, headerEnd).toString('utf8');
      let data = segment.subarray(headerEnd + sepLen);
      if (data.subarray(data.length - 2).toString() === '\r\n') data = data.subarray(0, data.length - 2);
      else if (data.subarray(data.length - 1).toString() === '\n') data = data.subarray(0, data.length - 1);
      const nm = /name="([^"]+)"/i.exec(headers);
      if (nm) parts.set(nm[1]!, { headers, data });
    }
    if (next === -1) break;
    pos = next;
  }
  return parts;
}

/** Extract the event JSON from a JSON or multipart request. Returns null when nothing parseable is present. */
export function extractEventJson(body: Buffer, contentType: string): unknown | null {
  if (/multipart\//i.test(contentType)) {
    const parts = parseMultipart(body, contentType);
    for (const [name, part] of parts) {
      if (/picture|image/i.test(name) || /image\//i.test(part.headers)) continue;   // photos are discarded (plan §8.9 opt-in)
      const text = part.data.toString('utf8').trim();
      if (text.startsWith('<')) return xmlEventToJson(text);
      try { return JSON.parse(text); } catch { /* try next part */ }
    }
    return null;
  }
  const text = body.toString('utf8').trim();
  if (!text) return null;
  if (text.startsWith('<')) return xmlEventToJson(text);
  try { return JSON.parse(text); } catch { return null; }
}

const XML_FIELDS = ['ipAddress', 'dateTime', 'eventType', 'serialNo', 'deviceID'] as const;
const XML_ACE_FIELDS = ['deviceName', 'majorEventType', 'subEventType', 'employeeNoString', 'employeeNo', 'cardNo', 'serialNo', 'deviceID', 'currentVerifyMode', 'attendanceStatus'] as const;

function xmlText(xml: string, tag: string): string | null {
  const m = new RegExp(`<${tag}(?:\\s[^>]*)?>([^<]*)</${tag}>`, 'i').exec(xml);
  return m ? m[1]!.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&apos;/g, "'").trim() : null;
}

/**
 * Older Hikvision firmware posts <EventNotificationAlert> XML (Appendix C). Only the fields the attendance filter
 * needs are extracted, into the same shape as the JSON payload, so one parser serves both formats.
 */
export function xmlEventToJson(xml: string): Record<string, unknown> | null {
  if (!/<EventNotificationAlert[\s>]/i.test(xml)) return null;
  const out: Record<string, unknown> = { fromXml: true };
  for (const f of XML_FIELDS) { const v = xmlText(xml, f); if (v !== null) out[f] = v; }
  const aceM = /<AccessControllerEvent[\s>]([\s\S]*?)<\/AccessControllerEvent>/i.exec(xml);
  if (aceM) {
    const ace: Record<string, unknown> = {};
    for (const f of XML_ACE_FIELDS) { const v = xmlText(aceM[1]!, f); if (v !== null) ace[f] = v; }
    out.AccessControllerEvent = ace;
  }
  return out;
}
