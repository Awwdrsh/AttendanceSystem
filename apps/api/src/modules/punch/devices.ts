import type { Queryable } from '../../db/pool.js';

export type PseudoKind = 'MOBILE' | 'WEB' | 'MANUAL';
const KIND_META: Record<PseudoKind, { vendor: string; protocol: string; name: string }> = {
  MOBILE: { vendor: 'mobile', protocol: 'app', name: 'Mobile app punches' },
  WEB: { vendor: 'kiosk', protocol: 'app', name: 'Web punches' },
  MANUAL: { vendor: 'manual', protocol: 'manual', name: 'Manual entries (HR, regularisation, auto clock-out)' },
};

/** Per-company pseudo device for non-terminal punches, created on first use (serial MOBILE-<code>, WEB-<code>, MANUAL-<code>). */
export async function ensurePseudoDevice(db: Queryable, companyId: string, kind: PseudoKind): Promise<{ id: string; serialNo: string; vendor: string }> {
  const c = await db.query<{ code: string }>('SELECT code FROM company WHERE id = $1', [companyId]);
  if (!c.rows[0]) throw new Error('company not found');
  const serial = `${kind}-${c.rows[0].code}`;
  const meta = KIND_META[kind];
  await db.query(
    `INSERT INTO device (company_id, serial_no, vendor, protocol, name, status) VALUES ($1, $2, $3, $4, $5, 'active') ON CONFLICT (serial_no) DO NOTHING`,
    [companyId, serial, meta.vendor, meta.protocol, meta.name]);
  const d = await db.query<{ id: string; vendor: string }>('SELECT id, vendor FROM device WHERE serial_no = $1', [serial]);
  return { id: d.rows[0]!.id, serialNo: serial, vendor: d.rows[0]!.vendor };
}

/** Local NPT 'YYYY-MM-DD HH:MM:SS' for the device_local_time column. */
export function nptLocalTime(ms: number): string {
  return new Date(ms + 345 * 60_000).toISOString().slice(0, 19).replace('T', ' ');
}
