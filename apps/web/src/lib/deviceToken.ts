/** This browser's clock-in device token: random, kept in local storage, sent with clock-in requests only. */
const KEY = 'hajiri.deviceToken';

export function existingDeviceToken(): string | null {
  try { return localStorage.getItem(KEY); } catch { return null; }
}

export function deviceToken(): string {
  const have = existingDeviceToken();
  if (have) return have;
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  const token = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  try { localStorage.setItem(KEY, token); } catch { /* private mode: the token lasts for this page only */ }
  return token;
}

/** Header for clock-in requests, when this browser has been registered. */
export function deviceHeaders(): Record<string, string> {
  const t = existingDeviceToken();
  return t ? { 'X-Device-Token': t } : {};
}
