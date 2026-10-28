import { api, del, get, post, put } from './client';

const geo = (g) => (g && g.lat !== undefined ? { lat: g.lat, lng: g.lng, accuracyM: g.accuracyM } : {});

export const field = {
  policy: () => get('/field/policy'),
  savePolicy: (p) => put('/field/policy', p),
  me: () => get('/field/me'),
  startBreak: (kind) => post('/field/breaks/start', { kind }),
  endBreak: () => post('/field/breaks/end'),
  breaks: (q) => get('/field/breaks', q),
  startVisit: (b) =>
    post('/field/visits/start', {
      place: b.place,
      ...(b.purpose ? { purpose: b.purpose } : {}),
      ...(b.photo ? { photo: b.photo } : {}),
      ...geo(b),
    }),
  endVisit: (id, b) => post(`/field/visits/${id}/end`, { ...(b.outcome ? { outcome: b.outcome } : {}), ...geo(b) }),
  visits: (q) => get('/field/visits', q),
  visitPhotoUrl: (id) => `/api/v1/field/visits/${id}/photo`,
  locations: () => get('/field/locations'),
  createLocation: (b) => post('/field/locations', b),
  updateLocation: (id, b) => put(`/field/locations/${id}`, b),
  deleteLocation: (id) => del(`/field/locations/${id}`),
  staff: () => get('/field/staff'),
  updateStaff: (id, b) => put(`/field/staff/${id}`, b),
  resetKioskPin: (employeeId) => post(`/field/staff/${employeeId}/kiosk-reset`),
  kiosks: () => get('/field/kiosks'),
  createKiosk: (b) => post('/field/kiosks', b),
  updateKiosk: (id, b) => put(`/field/kiosks/${id}`, b),
  pairingCode: (id) => post(`/field/kiosks/${id}/pairing-code`),
  revokeKiosk: (id) => post(`/field/kiosks/${id}/revoke`),
  unlockKiosk: (id) => post(`/field/kiosks/${id}/unlock`),
  setKioskPin: (pin) => put('/field/kiosk-pin', { pin }),
  kioskQr: () => get('/field/kiosk-qr'),
  myRoster: (from, to) => get('/field/my-roster', { from, to }),
  swapColleagues: (date) => get('/field/swap-colleagues', { date }),
  swaps: (q) => get('/field/swaps', q),
  requestSwap: (b) => post('/field/swaps', b),
  acceptSwap: (id, note) => post(`/field/swaps/${id}/accept`, note ? { note } : {}),
  declineSwap: (id, note) => post(`/field/swaps/${id}/decline`, note ? { note } : {}),
  cancelSwap: (id) => post(`/field/swaps/${id}/cancel`),
};

// ---------------------------------------------------------------- kiosk tablet (no session; the kiosk key travels in a header)
const KIOSK_KEY = 'hajiri.kioskKey';
export const kioskKey = {
  get: () => {
    try {
      return localStorage.getItem(KIOSK_KEY);
    } catch {
      return null;
    }
  },
  set: (k) => localStorage.setItem(KIOSK_KEY, k),
  clear: () => localStorage.removeItem(KIOSK_KEY),
};
const kh = () => ({ headers: { 'X-Kiosk-Key': kioskKey.get() ?? '' } });

export const kiosk = {
  pair: (code) => post('/kiosk/pair', { code }),
  session: () => api('GET', '/kiosk/session', undefined, kh()),
  identify: (b) => api('POST', '/kiosk/identify', b, kh()),
  punch: (ticket, selfie) => api('POST', '/kiosk/punch', { ticket, ...(selfie ? { selfie } : {}) }, kh()),
};

/** "https://www.openstreetmap.org/?mlat=..&mlon=..#map=17/../.." for a point. */
export function osmLink(lat, lng, zoom = 17) {
  return `https://www.openstreetmap.org/?mlat=${lat.toFixed(6)}&mlon=${lng.toFixed(6)}#map=${zoom}/${lat.toFixed(6)}/${lng.toFixed(6)}`;
}

export const BREAK_LABEL = { lunch: 'Lunch', tea: 'Tea', prayer: 'Prayer', personal: 'Personal' };

/** Current position, or nothing if the browser refuses or times out. */
export async function currentPosition(timeout = 8000) {
  if (!('geolocation' in navigator)) return {};
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, accuracyM: Math.round(p.coords.accuracy) }),
      () => resolve({}),
      { enableHighAccuracy: true, timeout, maximumAge: 30_000 },
    );
  });
}
