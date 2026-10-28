/** Typed fetch wrapper: JSON in/out, cookie session, normalised errors, 401 → login, 403 password-change → change page. */
export class ApiError extends Error {
  constructor(status, message, details, issues, suggestion) {
    super(message);
    this.status = status;
    this.details = details;
    this.issues = issues;
    this.suggestion = suggestion;
  }
}

const unauthorizedListeners = new Set();
const passwordListeners = new Set();
/** AuthProvider subscribes to be told when the server says the session is gone. */
export function onUnauthorized(fn) {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}
/** Fired when any API call answers 403 "password change required" (account flagged after the session started). */
export function onPasswordChangeRequired(fn) {
  passwordListeners.add(fn);
  return () => passwordListeners.delete(fn);
}

export function qs(params) {
  if (!params) return '';
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : '';
}

export const PASSWORD_CHANGE_MESSAGE = 'password change required';
export const TWO_FACTOR_SETUP_MESSAGE = 'two-step sign-in setup required';
const twoFactorListeners = new Set();
/** Fired when the company requires two-step sign-in for this account and it is not set up yet. */
export function onTwoFactorSetupRequired(fn) {
  twoFactorListeners.add(fn);
  return () => twoFactorListeners.delete(fn);
}

/** Turn an error response body into an ApiError (pure; unit-tested). */
export function normalizeError(status, data) {
  const d = data && typeof data === 'object' ? data : {};
  const msg = d.issues?.length
    ? d.issues.map((i) => `${i.path ? `${i.path}: ` : ''}${i.message}`).join('; ')
    : (d.error ?? (typeof data === 'string' && data.trim() ? data.trim() : `Request failed (${status})`));
  // Some endpoints answer 4xx with a structured body and no `error` key (e.g. CSV import row errors); keep the body.
  const details = d.details !== undefined ? d.details : data;
  return new ApiError(status, msg, details, d.issues, d.suggestion);
}

export async function api(method, path, body, opts = {}) {
  const init = { method, credentials: 'include', headers: { ...(opts.headers ?? {}) } };
  if (body !== undefined) {
    if (typeof body === 'string' || body instanceof FormData) init.body = body;
    else {
      init.body = JSON.stringify(body);
      init.headers = { 'Content-Type': 'application/json', ...(opts.headers ?? {}) };
    }
  }
  let res;
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
    return await res.text();
  }
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const err = normalizeError(res.status, data);
    if (res.status === 403 && err.message === PASSWORD_CHANGE_MESSAGE) for (const l of passwordListeners) l();
    if (res.status === 403 && err.message === TWO_FACTOR_SETUP_MESSAGE) for (const l of twoFactorListeners) l();
    throw err;
  }
  return data;
}

export const get = (path, params) => api('GET', `${path}${qs(params)}`);
export const post = (path, body) => api('POST', path, body ?? {});
export const put = (path, body) => api('PUT', path, body ?? {});
export const patch = (path, body) => api('PATCH', path, body ?? {});
export const del = (path, body) => api('DELETE', path, body);

/** Download a CSV/text endpoint through the session and save it with a filename. */
export async function download(path, filename) {
  const res = await fetch(`/api/v1${path}`, { credentials: 'include' });
  if (res.status === 401) {
    for (const l of unauthorizedListeners) l();
    throw new ApiError(401, 'Your session has ended. Please sign in again.');
  }
  if (!res.ok) {
    const text = await res.text();
    let data = text;
    try {
      data = JSON.parse(text);
    } catch {
      /* plain text */
    }
    throw normalizeError(res.status, data);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function errorMessage(e) {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return 'Something went wrong. Please try again.';
}
