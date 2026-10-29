const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Splits the address after /careers into a company code and a job id. The server's share links are /careers/<code>
 *  and /careers/<code>/<jobId>; links inside this page are /careers/<jobId>?company=<code>. */
export function careersPath(rest, query) {
  const parts = (rest ?? '').split('/').filter(Boolean);
  if (parts.length >= 2) return { company: parts[0], jobId: parts[1] };
  if (parts.length === 1 && !UUID_RE.test(parts[0])) return { company: parts[0], jobId: '' };
  return { company: query, jobId: parts[0] ?? '' };
}
