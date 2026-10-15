import { api, del, get, post, put, type Query } from './client';

export type BreakKind = 'lunch' | 'tea' | 'prayer' | 'personal';
export type GeofenceMode = 'off' | 'flag' | 'block';
export interface BreakRule { enabled: boolean; paid: boolean; maxMin: number }
export interface FieldPolicy { geofenceMode: GeofenceMode; breaks: Record<BreakKind, BreakRule>; fieldPhotoRequired: boolean; fieldVisitClockIn: boolean }

export interface WorkBreak { id: string; kind: BreakKind; paid: boolean; max_min: number; started_at: string; ended_at: string | null; minutes: number | null; over_limit: boolean }
export interface BreakRow extends WorkBreak { employee_id: string; emp_code: string; name_en: string; local_date: string; open: boolean }
export interface MyVisit { id: string; place: string; purpose: string | null; started_at: string; ended_at: string | null; outcome: string | null; has_photo: boolean }
export interface VisitRow extends MyVisit {
  employee_id: string; emp_code: string; name_en: string; designation: string | null; local_date: string; minutes: number; opened_day: boolean;
  start_lat: number | null; start_lng: number | null; start_accuracy_m: number | null; end_lat: number | null; end_lng: number | null; end_accuracy_m: number | null;
}
export interface FieldMe {
  date: string; clockedIn: boolean; fieldWork: boolean; fieldPhotoRequired: boolean;
  breakTypes: Array<{ kind: BreakKind; paid: boolean; maxMin: number }>; breaks: WorkBreak[]; openBreak: WorkBreak | null;
  visits: MyVisit[]; openVisit: MyVisit | null; kioskPin: { set: boolean; lockedUntil: string | null };
}
export interface FieldLocation { id: string; name: string; address: string | null; branch_id: string | null; branch_name: string | null; lat: number; lng: number; radius_m: number; active: boolean; employee_ids: string[] }
export interface StaffRow { id: string; emp_code: string; name_en: string; designation: string | null; field_work: boolean; work_from_home: boolean; branch_name: string | null; location_ids: string[]; kiosk_pin_set: boolean; kiosk_pin_locked: boolean }
export interface KioskRow {
  id: string; name: string; branch_id: string | null; branch_name: string | null; location_id: string | null; location_name: string | null; selfie_required: boolean;
  status: 'pairing' | 'active' | 'revoked'; paired_at: string | null; last_seen_at: string | null; created_at: string; revoked_at: string | null; locked: boolean;
  pairing_open: boolean; pairing_expires_at: string | null; punches_today: number;
}
export interface ShiftBrief { id: string; code: string; name_en: string; start: string; end: string }
export interface RosterDay { date: string; weekday: number; source: 'roster' | 'default' | 'weekly_off' | 'roster_off' | 'none'; shift: ShiftBrief | null; holidays: string[] }
export interface SwapRow {
  id: string; local_date: string; status: 'awaiting_colleague' | 'pending' | 'approved' | 'rejected' | 'declined' | 'cancelled'; reason: string | null; colleague_note: string | null;
  created_at: string; decided_at: string | null; requester_id: string; requester_name: string; requester_code: string; colleague_id: string; colleague_name: string; colleague_code: string;
  requester_shift_code: string | null; requester_shift_name: string | null; colleague_shift_code: string | null; colleague_shift_name: string | null;
}
export interface SwapColleague { id: string; emp_code: string; name_en: string; designation: string | null; shift: ShiftBrief | null; off: boolean; sameAsMine: boolean }

const geo = (g?: { lat?: number; lng?: number; accuracyM?: number }) => (g && g.lat !== undefined ? { lat: g.lat, lng: g.lng, accuracyM: g.accuracyM } : {});

export const field = {
  policy: () => get<{ policy: FieldPolicy }>('/field/policy'),
  savePolicy: (p: FieldPolicy) => put<{ policy: FieldPolicy }>('/field/policy', p),
  me: () => get<FieldMe>('/field/me'),
  startBreak: (kind: BreakKind) => post<{ break: WorkBreak }>('/field/breaks/start', { kind }),
  endBreak: () => post<{ break: WorkBreak }>('/field/breaks/end'),
  breaks: (q: Query) => get<{ breaks: BreakRow[] }>('/field/breaks', q),
  startVisit: (b: { place: string; purpose?: string; photo?: string; lat?: number; lng?: number; accuracyM?: number }) =>
    post<{ visit: MyVisit; clockedIn: boolean; message: string }>('/field/visits/start', { place: b.place, ...(b.purpose ? { purpose: b.purpose } : {}), ...(b.photo ? { photo: b.photo } : {}), ...geo(b) }),
  endVisit: (id: string, b: { outcome?: string; lat?: number; lng?: number; accuracyM?: number }) => post<{ visit: MyVisit }>(`/field/visits/${id}/end`, { ...(b.outcome ? { outcome: b.outcome } : {}), ...geo(b) }),
  visits: (q: Query) => get<{ visits: VisitRow[] }>('/field/visits', q),
  visitPhotoUrl: (id: string) => `/api/v1/field/visits/${id}/photo`,
  locations: () => get<{ locations: FieldLocation[] }>('/field/locations'),
  createLocation: (b: Record<string, unknown>) => post<{ id: string }>('/field/locations', b),
  updateLocation: (id: string, b: Record<string, unknown>) => put<{ ok: boolean }>(`/field/locations/${id}`, b),
  deleteLocation: (id: string) => del<{ ok: boolean }>(`/field/locations/${id}`),
  staff: () => get<{ staff: StaffRow[] }>('/field/staff'),
  updateStaff: (id: string, b: { fieldWork?: boolean; locationIds?: string[] }) => put<{ ok: boolean }>(`/field/staff/${id}`, b),
  resetKioskPin: (employeeId: string) => post<{ ok: boolean }>(`/field/staff/${employeeId}/kiosk-reset`),
  kiosks: () => get<{ kiosks: KioskRow[] }>('/field/kiosks'),
  createKiosk: (b: Record<string, unknown>) => post<{ id: string; pairingCode: string; expiresAt: string }>('/field/kiosks', b),
  updateKiosk: (id: string, b: Record<string, unknown>) => put<{ ok: boolean }>(`/field/kiosks/${id}`, b),
  pairingCode: (id: string) => post<{ pairingCode: string; expiresAt: string }>(`/field/kiosks/${id}/pairing-code`),
  revokeKiosk: (id: string) => post<{ ok: boolean }>(`/field/kiosks/${id}/revoke`),
  unlockKiosk: (id: string) => post<{ ok: boolean }>(`/field/kiosks/${id}/unlock`),
  setKioskPin: (pin: string) => put<{ ok: boolean }>('/field/kiosk-pin', { pin }),
  kioskQr: () => get<{ token: string; refreshInMs: number; empCode: string }>('/field/kiosk-qr'),
  myRoster: (from: string, to: string) => get<{ days: RosterDay[] }>('/field/my-roster', { from, to }),
  swapColleagues: (date: string) => get<{ mine: { shift: ShiftBrief | null; off: boolean }; colleagues: SwapColleague[] }>('/field/swap-colleagues', { date }),
  swaps: (q?: Query) => get<{ swaps: SwapRow[] }>('/field/swaps', q),
  requestSwap: (b: { colleagueId: string; localDate: string; reason?: string }) => post<{ id: string }>('/field/swaps', b),
  acceptSwap: (id: string, note?: string) => post<{ status: string }>(`/field/swaps/${id}/accept`, note ? { note } : {}),
  declineSwap: (id: string, note?: string) => post<{ status: string }>(`/field/swaps/${id}/decline`, note ? { note } : {}),
  cancelSwap: (id: string) => post<{ status: string }>(`/field/swaps/${id}/cancel`),
};

// ---------------------------------------------------------------- kiosk tablet (no session; the kiosk key travels in a header)
const KIOSK_KEY = 'hajiri.kioskKey';
export const kioskKey = {
  get: () => { try { return localStorage.getItem(KIOSK_KEY); } catch { return null; } },
  set: (k: string) => localStorage.setItem(KIOSK_KEY, k),
  clear: () => localStorage.removeItem(KIOSK_KEY),
};
const kh = () => ({ headers: { 'X-Kiosk-Key': kioskKey.get() ?? '' } });

export interface KioskSession { kiosk: { id: string; name: string; selfieRequired: boolean; lockedUntil: string | null }; company: { name: string }; serverTime: string }
export interface KioskIdentity { ticket: string; employee: { name: string; empCode: string; designation: string | null }; nextDirection: 'in' | 'out'; selfieRequired: boolean; openBreak: { kind: string; started_at: string } | null }
export interface KioskPunchResult { duplicate: boolean; direction: 'in' | 'out'; punchedAt: string; name: string; message: string }

export const kiosk = {
  pair: (code: string) => post<{ key: string; kiosk: { id: string; name: string } }>('/kiosk/pair', { code }),
  session: () => api<KioskSession>('GET', '/kiosk/session', undefined, kh()),
  identify: (b: { empCode: string; pin: string } | { qr: string }) => api<KioskIdentity>('POST', '/kiosk/identify', b, kh()),
  punch: (ticket: string, selfie?: string) => api<KioskPunchResult>('POST', '/kiosk/punch', { ticket, ...(selfie ? { selfie } : {}) }, kh()),
};

/** "https://www.openstreetmap.org/?mlat=..&mlon=..#map=17/../.." for a point. */
export function osmLink(lat: number, lng: number, zoom = 17): string {
  return `https://www.openstreetmap.org/?mlat=${lat.toFixed(6)}&mlon=${lng.toFixed(6)}#map=${zoom}/${lat.toFixed(6)}/${lng.toFixed(6)}`;
}

export const BREAK_LABEL: Record<BreakKind, string> = { lunch: 'Lunch', tea: 'Tea', prayer: 'Prayer', personal: 'Personal' };

/** Current position, or nothing if the browser refuses or times out. */
export async function currentPosition(timeout = 8000): Promise<{ lat?: number; lng?: number; accuracyM?: number }> {
  if (!('geolocation' in navigator)) return {};
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracyM: Math.round(p.coords.accuracy) }),
      () => resolve({}), { enableHighAccuracy: true, timeout, maximumAge: 30_000 });
  });
}
