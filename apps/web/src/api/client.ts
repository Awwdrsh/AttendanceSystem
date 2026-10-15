/** Typed fetch wrapper: JSON in/out, cookie session, normalised errors, 401 → login, 403 password-change → change page. */
export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly details?: unknown, readonly issues?: Array<{ path: string; message: string }>, readonly suggestion?: unknown) {
    super(message);
  }
}

const unauthorizedListeners = new Set<() => void>();
const passwordListeners = new Set<() => void>();
/** AuthProvider subscribes to be told when the server says the session is gone. */
export function onUnauthorized(fn: () => void): () => void {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}
/** Fired when any API call answers 403 "password change required" (account flagged after the session started). */
export function onPasswordChangeRequired(fn: () => void): () => void {
  passwordListeners.add(fn);
  return () => passwordListeners.delete(fn);
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export function qs(params?: Query): string {
  if (!params) return '';
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : '';
}

export const PASSWORD_CHANGE_MESSAGE = 'password change required';
export const TWO_FACTOR_SETUP_MESSAGE = 'two-step sign-in setup required';
const twoFactorListeners = new Set<() => void>();
/** Fired when the company requires two-step sign-in for this account and it is not set up yet. */
export function onTwoFactorSetupRequired(fn: () => void): () => void {
  twoFactorListeners.add(fn);
  return () => twoFactorListeners.delete(fn);
}

/** Turn an error response body into an ApiError (pure; unit-tested). */
export function normalizeError(status: number, data: unknown): ApiError {
  const d = (data && typeof data === 'object' ? data : {}) as { error?: string; details?: unknown; issues?: Array<{ path: string; message: string }>; suggestion?: unknown };
  const msg = d.issues?.length ? d.issues.map((i) => `${i.path ? `${i.path}: ` : ''}${i.message}`).join('; ')
    : d.error ?? (typeof data === 'string' && data.trim() ? data.trim() : `Request failed (${status})`);
  // Some endpoints answer 4xx with a structured body and no `error` key (e.g. CSV import row errors); keep the body.
  const details = d.details !== undefined ? d.details : data;
  return new ApiError(status, msg, details, d.issues, d.suggestion);
}

export async function api<T>(method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, body?: unknown, opts: { raw?: boolean; headers?: Record<string, string> } = {}): Promise<T> {
  const init: RequestInit = { method, credentials: 'include', headers: { ...(opts.headers ?? {}) } };
  if (body !== undefined) {
    if (typeof body === 'string' || body instanceof FormData) init.body = body;
    else { init.body = JSON.stringify(body); init.headers = { 'Content-Type': 'application/json', ...(opts.headers ?? {}) }; }
  }
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, init);
  } catch {
    throw new ApiError(0, 'No connection. Please check your network and try again.');
  }
  if (res.status === 401 && !path.startsWith('/auth/login')) {
    for (const l of unauthorizedListeners) l();
    throw new ApiError(401, 'Your session has ended. Please sign in again.');
  }
  if (opts.raw) {
    if (!res.ok) throw new ApiError(res.status, await res.text());
    return (await res.text()) as unknown as T;
  }
  const text = await res.text();
  let data: unknown = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const err = normalizeError(res.status, data);
    if (res.status === 403 && err.message === PASSWORD_CHANGE_MESSAGE) for (const l of passwordListeners) l();
    if (res.status === 403 && err.message === TWO_FACTOR_SETUP_MESSAGE) for (const l of twoFactorListeners) l();
    throw err;
  }
  return data as T;
}

export const get = <T>(path: string, params?: Query) => api<T>('GET', `${path}${qs(params)}`);
export const post = <T>(path: string, body?: unknown) => api<T>('POST', path, body ?? {});
export const put = <T>(path: string, body?: unknown) => api<T>('PUT', path, body ?? {});
export const patch = <T>(path: string, body?: unknown) => api<T>('PATCH', path, body ?? {});
export const del = <T>(path: string, body?: unknown) => api<T>('DELETE', path, body);

/** Download a CSV/text endpoint through the session and save it with a filename. */
export async function download(path: string, filename: string): Promise<void> {
  const res = await fetch(`/api/v1${path}`, { credentials: 'include' });
  if (res.status === 401) { for (const l of unauthorizedListeners) l(); throw new ApiError(401, 'Your session has ended. Please sign in again.'); }
  if (!res.ok) {
    const text = await res.text();
    let data: unknown = text; try { data = JSON.parse(text); } catch { /* plain text */ }
    throw normalizeError(res.status, data);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return 'Something went wrong. Please try again.';
}
