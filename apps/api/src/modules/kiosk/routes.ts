import { Router, type NextFunction, type Request, type Response } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { randomToken, sha256Hex, verifyPassword } from '../../crypto.js';
import { BadRequestError, ConflictError, HttpError, UnauthorizedError } from '../../errors.js';
import { parsePhoto, PHOTO_MAX_BYTES } from '../punch/photo.js';
import { nptLocalTime } from '../punch/devices.js';
import { closeOpenBreak, insertAppPunch, nextDirection } from '../field/punches.js';
import { allow, issueTicket, normalisePairingCode, parseQr, verifyQr, verifyTicket } from './tokens.js';

export const KIOSK_HEADER = 'x-kiosk-key';
export const PIN_MAX_FAILS = 5;
export const PIN_LOCK_MIN = 15;
export const KIOSK_MAX_FAILS = 20;
export const KIOSK_LOCK_MIN = 5;

interface KioskRow { id: string; company_id: string; name: string; branch_id: string | null; location_id: string | null; selfie_required: boolean; locked_until: Date | null; last_seen_at: Date | null }
type KioskRequest = Request & { kiosk?: KioskRow };

class TooManyError extends HttpError { constructor(msg: string) { super(429, msg); } }

/**
 * Shared clock-in tablets (mounted before session auth). A tablet pairs once with a one-time code from HR and then
 * sends its kiosk key in the X-Kiosk-Key header. Employees identify with their code and PIN (or a rotating QR badge),
 * get a two-minute ticket, take a selfie and punch at server time.
 */
export function kioskRoutes({ db, cipher, pepper }: AppDeps): Router {
  const r = Router();

  r.post('/pair', async (req, res) => {
    if (!allow(`pair|${req.ip}`, 10, 10 * 60_000)) throw new TooManyError('Too many pairing attempts. Wait a few minutes and try again.');
    const b = z.object({ code: z.string().min(4).max(20) }).parse(req.body);
    const hash = sha256Hex(`kiosk-pair|${normalisePairingCode(b.code)}`);
    const k = await db.query<{ id: string; company_id: string; name: string }>(
      `SELECT id, company_id, name FROM kiosk WHERE pairing_code_hash = $1 AND pairing_expires_at > now() AND status <> 'revoked'`, [hash]);
    const kiosk = k.rows[0];
    if (!kiosk) throw new BadRequestError('That pairing code is wrong or has expired. Ask HR for a new one.');
    const key = randomToken(32);
    await db.query(
      `UPDATE kiosk SET key_hash = $2, status = 'active', pairing_code_hash = NULL, pairing_expires_at = NULL, paired_at = now(), last_seen_at = now(),
              failed_attempts = 0, locked_until = NULL WHERE id = $1`, [kiosk.id, sha256Hex(`kiosk-key|${key}`)]);
    await audit(db, { action: 'kiosk_paired', table: 'kiosk', rowId: kiosk.id, after: { ip: req.ip ?? null } });
    res.json({ key, kiosk: { id: kiosk.id, name: kiosk.name } });
  });

  // Everything below needs a paired kiosk.
  r.use(async (req: KioskRequest, _res: Response, next: NextFunction) => {
    const key = req.get(KIOSK_HEADER);
    if (!key || key.length < 20 || key.length > 100) return next(new UnauthorizedError('This tablet is not paired. Enter a pairing code from HR.'));
    const k = await db.query<KioskRow>(
      `SELECT id, company_id, name, branch_id, location_id, selfie_required, locked_until, last_seen_at FROM kiosk WHERE key_hash = $1 AND status = 'active'`,
      [sha256Hex(`kiosk-key|${key}`)]);
    if (!k.rows[0]) return next(new UnauthorizedError('This tablet is no longer paired. Ask HR for a new pairing code.'));
    req.kiosk = k.rows[0];
    if (!k.rows[0].last_seen_at || Date.now() - k.rows[0].last_seen_at.getTime() > 60_000) await db.query('UPDATE kiosk SET last_seen_at = now() WHERE id = $1', [k.rows[0].id]);
    return next();
  });

  r.get('/session', async (req: KioskRequest, res) => {
    const k = req.kiosk!;
    const c = await db.query<{ name_en: string; value: { nameEn?: string } | null }>(
      `SELECT c.name_en, s.value FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'company.profile' WHERE c.id = $1`, [k.company_id]);
    res.json({
      kiosk: { id: k.id, name: k.name, selfieRequired: k.selfie_required, lockedUntil: k.locked_until && k.locked_until > new Date() ? k.locked_until : null },
      company: { name: c.rows[0]?.value?.nameEn ?? c.rows[0]?.name_en ?? '' },
      serverTime: new Date().toISOString(),
    });
  });

  const kioskLocked = (k: KioskRow) => !!k.locked_until && k.locked_until > new Date();

  /** A wrong code or PIN counts against the kiosk too; many failures lock the tablet for a few minutes. */
  const kioskFailure = async (k: KioskRow) => {
    const u = await db.query<{ failed_attempts: number }>('UPDATE kiosk SET failed_attempts = failed_attempts + 1 WHERE id = $1 RETURNING failed_attempts', [k.id]);
    if ((u.rows[0]?.failed_attempts ?? 0) >= KIOSK_MAX_FAILS) {
      await db.query(`UPDATE kiosk SET failed_attempts = 0, locked_until = now() + make_interval(mins => $2) WHERE id = $1`, [k.id, KIOSK_LOCK_MIN]);
      await audit(db, { action: 'kiosk_locked', table: 'kiosk', rowId: k.id });
    }
  };

  r.post('/identify', async (req: KioskRequest, res) => {
    const k = req.kiosk!;
    if (kioskLocked(k)) throw new TooManyError('This kiosk is paused after too many wrong attempts. Try again in a few minutes.');
    if (!allow(`identify|${k.id}`, 30, 60_000)) throw new TooManyError('Too many attempts on this kiosk. Wait a minute.');
    const b = z.union([
      z.object({ empCode: z.string().trim().min(1).max(32), pin: z.string().regex(/^\d{4,6}$/, 'PIN is 4 to 6 digits') }),
      z.object({ qr: z.string().min(10).max(200) }),
    ]).parse(req.body);

    let employeeId: string;
    if ('qr' in b) {
      const p = parseQr(b.qr);
      const cred = p ? await db.query<{ qr_version: number; company_id: string }>('SELECT qr_version, company_id FROM kiosk_credential WHERE employee_id = $1', [p.employeeId]) : null;
      const version = cred?.rows[0] && cred.rows[0].company_id === k.company_id ? cred.rows[0].qr_version : 1;
      if (!p || !verifyQr(cipher, p, version)) { await kioskFailure(k); throw new BadRequestError('This QR code is not valid or has expired. Show the code in the app again.'); }
      const used = await db.query('INSERT INTO kiosk_qr_use (employee_id, time_window, kiosk_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING', [p.employeeId, p.window, k.id]);
      if (!used.rowCount) throw new BadRequestError('This QR code was already used. Wait for the next code in the app.');
      employeeId = p.employeeId;
    } else {
      const e = await db.query<{ id: string; pin_hash: string | null; locked_until: Date | null }>(
        `SELECT e.id, c.pin_hash, c.locked_until FROM employee e LEFT JOIN kiosk_credential c ON c.employee_id = e.id
          WHERE e.company_id = $1 AND upper(e.emp_code) = upper($2) AND e.status = 'active'`, [k.company_id, b.empCode]);
      const row = e.rows[0];
      if (row?.locked_until && row.locked_until > new Date()) {
        const mins = Math.max(1, Math.ceil((row.locked_until.getTime() - Date.now()) / 60_000));
        throw new TooManyError(`Your kiosk PIN is locked after too many wrong tries. Try again in ${mins} min or ask HR to reset it.`);
      }
      if (!row || !row.pin_hash) {
        await kioskFailure(k);
        // Same answer whether the code is unknown or no PIN is set, so codes cannot be probed.
        throw new BadRequestError('Employee code or PIN is wrong. Set your kiosk PIN in the app under My attendance.');
      }
      if (!(await verifyPassword(b.pin, row.pin_hash, pepper))) {
        await kioskFailure(k);
        const u = await db.query<{ failed_count: number }>(
          'UPDATE kiosk_credential SET failed_count = failed_count + 1 WHERE employee_id = $1 RETURNING failed_count', [row.id]);
        const n = u.rows[0]?.failed_count ?? 0;
        if (n >= PIN_MAX_FAILS) {
          await db.query(`UPDATE kiosk_credential SET failed_count = 0, locked_until = now() + make_interval(mins => $2) WHERE employee_id = $1`, [row.id, PIN_LOCK_MIN]);
          await audit(db, { action: 'kiosk_pin_locked', table: 'kiosk_credential', rowId: row.id, after: { kioskId: k.id } });
          throw new TooManyError(`Too many wrong PINs. Your kiosk PIN is locked for ${PIN_LOCK_MIN} min.`);
        }
        throw new BadRequestError(`Employee code or PIN is wrong. ${PIN_MAX_FAILS - n} ${PIN_MAX_FAILS - n === 1 ? 'try' : 'tries'} left.`);
      }
      await db.query('UPDATE kiosk_credential SET failed_count = 0, locked_until = NULL WHERE employee_id = $1', [row.id]);
      employeeId = row.id;
    }

    const emp = await db.query<{ id: string; emp_code: string; name_en: string; designation: string | null }>(
      `SELECT id, emp_code, name_en, designation FROM employee WHERE id = $1 AND company_id = $2 AND status = 'active'`, [employeeId, k.company_id]);
    if (!emp.rows[0]) { await kioskFailure(k); throw new BadRequestError('This QR code is not valid here.'); }
    await db.query('UPDATE kiosk SET failed_attempts = 0 WHERE id = $1', [k.id]);
    const direction = await nextDirection(db, employeeId);
    const openBreak = await db.query<{ kind: string; started_at: Date }>('SELECT kind, started_at FROM work_break WHERE employee_id = $1 AND ended_at IS NULL', [employeeId]);
    res.json({
      ticket: issueTicket(cipher, k.id, employeeId),
      employee: { name: emp.rows[0].name_en, empCode: emp.rows[0].emp_code, designation: emp.rows[0].designation },
      nextDirection: direction, selfieRequired: k.selfie_required, openBreak: openBreak.rows[0] ?? null,
    });
  });

  r.post('/punch', async (req: KioskRequest, res) => {
    const k = req.kiosk!;
    if (!allow(`punch|${k.id}`, 60, 60_000)) throw new TooManyError('Too many punches on this kiosk. Wait a minute.');
    const b = z.object({ ticket: z.string().min(20).max(200), selfie: z.string().max(Math.ceil(PHOTO_MAX_BYTES * 4 / 3) + 64).optional() }).parse(req.body);
    const t = verifyTicket(cipher, b.ticket, k.id);
    if (!t) throw new BadRequestError('This sign-in has expired. Enter your code and PIN again.');
    const photo = b.selfie ? parsePhoto(b.selfie) : null;
    if (k.selfie_required && !photo) throw new BadRequestError('Take a quick photo to punch at this kiosk.', { code: 'photo_required' });
    const emp = await db.query<{ emp_code: string; name_en: string }>(`SELECT emp_code, name_en FROM employee WHERE id = $1 AND company_id = $2 AND status = 'active'`, [t.employeeId, k.company_id]);
    if (!emp.rows[0]) throw new ConflictError('This employee can no longer punch here.');
    const direction = await nextDirection(db, t.employeeId);
    const p = await insertAppPunch(db, cipher, {
      companyId: k.company_id, employeeId: t.employeeId, empCode: emp.rows[0].emp_code, direction, kind: 'kiosk', idempotency: t.nonce, photo,
      raw: { kioskId: k.id, kioskName: k.name, locationId: k.location_id, remote: false, flags: ['Kiosk'], ip: req.ip ?? null },
    });
    if (!p.duplicate && direction === 'out') await closeOpenBreak(db, t.employeeId, p.punchedAt);
    const time = nptLocalTime(p.punchedAt.getTime()).slice(11, 16);
    const first = emp.rows[0].name_en.split(/\s+/)[0];
    res.status(p.duplicate ? 200 : 201).json({
      duplicate: p.duplicate, direction, punchedAt: p.punchedAt, name: emp.rows[0].name_en,
      message: p.duplicate ? `Already recorded at ${time}.` : `${direction === 'in' ? 'Clocked in' : 'Clocked out'} at ${time}. ${direction === 'in' ? 'Have a good day' : 'See you tomorrow'}, ${first}!`,
    });
  });

  return r;
}
