import { randomBytes, timingSafeEqual } from 'node:crypto';
import type { FieldCipher } from '../../crypto.js';

/**
 * Signed tokens for kiosks, keyed by the field-encryption key (FieldCipher.blindIndex is an HMAC with that key), so
 * nothing extra has to be configured and nothing can be forged without the server's key.
 */
const sign = (cipher: FieldCipher, s: string) => cipher.blindIndex(s).toLowerCase();
const same = (a: string, b: string) => a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));

// ---------------------------------------------------------------- QR badge (rotating, 30 s windows)
export const QR_WINDOW_MS = 30_000;
export const qrWindow = (now = Date.now()) => Math.floor(now / QR_WINDOW_MS);

export function qrToken(cipher: FieldCipher, employeeId: string, version: number, window = qrWindow()): string {
  return `HQ1.${employeeId}.${window}.${sign(cipher, `kqr|${employeeId}|${window}|${version}`)}`;
}

/** Parse a scanned badge: the employee and window it claims (the signature is checked once the version is known). */
export function parseQr(token: string): { employeeId: string; window: number; sig: string } | null {
  const m = /^HQ1\.([0-9a-f-]{36})\.(\d{1,12})\.([0-9a-f]{32})$/.exec(token.trim());
  if (!m) return null;
  return { employeeId: m[1]!, window: Number(m[2]), sig: m[3]! };
}

/** Valid for the current window and one either side (clock drift, a slow scan). */
export function verifyQr(cipher: FieldCipher, p: { employeeId: string; window: number; sig: string }, version: number, now = Date.now()): boolean {
  if (Math.abs(p.window - qrWindow(now)) > 1) return false;
  return same(p.sig, sign(cipher, `kqr|${p.employeeId}|${p.window}|${version}`));
}

// ---------------------------------------------------------------- identify → punch ticket (2 minutes, one kiosk)
export const TICKET_TTL_MS = 2 * 60_000;

export function issueTicket(cipher: FieldCipher, kioskId: string, employeeId: string, now = Date.now(), viaQr = false): string {
  const exp = now + TICKET_TTL_MS;
  // The first nonce character records how the person identified (signed with the rest): Q = QR badge, P = PIN.
  const nonce = (viaQr ? 'Q' : 'P') + randomBytes(9).toString('base64url').slice(1);
  return `${kioskId}.${employeeId}.${exp}.${nonce}.${sign(cipher, `kt|${kioskId}|${employeeId}|${exp}|${nonce}`)}`;
}

export function verifyTicket(cipher: FieldCipher, ticket: string, kioskId: string, now = Date.now()): { employeeId: string; nonce: string; viaQr: boolean } | null {
  const m = /^([0-9a-f-]{36})\.([0-9a-f-]{36})\.(\d{13})\.([A-Za-z0-9_-]{12})\.([0-9a-f]{32})$/.exec(ticket);
  if (!m || m[1] !== kioskId) return null;
  const exp = Number(m[3]);
  if (exp < now) return null;
  if (!same(m[5]!, sign(cipher, `kt|${m[1]}|${m[2]}|${exp}|${m[4]}`))) return null;
  return { employeeId: m[2]!, nonce: m[4]!, viaQr: m[4]!.startsWith('Q') };
}

// ---------------------------------------------------------------- pairing codes and PINs
const CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'; // no 0/O, 1/I

export function pairingCode(): string {
  const b = randomBytes(8);
  let s = '';
  for (let i = 0; i < 8; i++) s += CODE_ALPHABET[b[i]! % CODE_ALPHABET.length];
  return `${s.slice(0, 4)}-${s.slice(4)}`;
}
export const normalisePairingCode = (c: string) => c.toUpperCase().replace(/[^A-Z0-9]/g, '');

/** PINs anyone would guess first are refused. */
export function weakPin(pin: string): boolean {
  if (/^(\d)\1+$/.test(pin)) return true;
  const digits = pin.split('').map(Number);
  const up = digits.every((d, i) => i === 0 || d === (digits[i - 1]! + 1) % 10);
  const down = digits.every((d, i) => i === 0 || d === (digits[i - 1]! + 9) % 10);
  return up || down || ['1212', '2580', '0852', '1122', '112233', '121212', '123123'].includes(pin);
}

// ---------------------------------------------------------------- in-memory rate limiting (per process)
const hits = new Map<string, number[]>();

/** True if `key` has made fewer than `limit` calls in the last `windowMs` (and records this one). */
export function allow(key: string, limit: number, windowMs: number, now = Date.now()): boolean {
  const list = (hits.get(key) ?? []).filter((t) => t > now - windowMs);
  if (list.length >= limit) { hits.set(key, list); return false; }
  list.push(now);
  hits.set(key, list);
  if (hits.size > 10_000) for (const [k, v] of hits) if (!v.some((t) => t > now - windowMs)) hits.delete(k);
  return true;
}
export function resetRateLimits(): void { hits.clear(); }
