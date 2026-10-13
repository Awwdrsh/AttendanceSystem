/** Typed fetch wrapper: JSON in/out, cookie session, normalised errors, 401 → login. */
export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly details?: unknown, readonly issues?: Array<{ path: string; message: string }>, readonly suggestion?: unknown) {
    super(message);
  }
}

const listeners = new Set<() => void>();
/** AuthProvider subscribes to be told when the server says the session is gone. */
export function onUnauthorized(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export type Query = Record<string, string | number | boolean | null | undefined>;

export function qs(params?: Query): string {
  if (!params) return '';
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : '';
}

export async function api<T>(method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, body?: unknown, opts: { raw?: boolean; headers?: Record<string, string> } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, {
      method,
      credentials: 'include',
      headers: { ...(body !== undefined && !(body instanceof FormData) && typeof body !== 'string' ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers ?? {}) },
      body: body === undefined ? undefined : typeof body === 'string' || body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, 'No connection. Please check your network and try again.');
  }
  if (res.status === 401 && !path.startsWith('/auth/login')) {
    for (const l of listeners) l();
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
    const d = (data ?? {}) as { error?: string; details?: unknown; issues?: Array<{ path: string; message: string }>; suggestion?: unknown };
    const msg = d.issues?.length ? d.issues.map((i) => `${i.path ? `${i.path}: ` : ''}${i.message}`).join('; ') : d.error ?? `Request failed (${res.status})`;
    throw new ApiError(res.status, msg, d.details, d.issues, d.suggestion);
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
  if (!res.ok) throw new ApiError(res.status, (await res.text()) || 'Download failed');
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
