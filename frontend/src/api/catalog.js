import { get, post, put, del, download } from './client';

/** Settings → Features: the modules a company can switch off. */
export const features = {
  get: () => get('/settings/features'),
  save: (enabled) => put('/settings/features', { enabled }),
};

/** Turn `{ a: 1, b: undefined }` into a query string the API helpers accept. */
const clean = (q) => Object.fromEntries(Object.entries(q).filter(([, v]) => v !== undefined && v !== null && v !== ''));

export const catalog = {
  people: (name, q = {}) => get(`/reports/people/${name}`, clean(q)),
  peopleCsv: (name, q, filename) => {
    const p = new URLSearchParams(Object.entries(clean({ ...q, format: 'csv' })).map(([k, v]) => [k, String(v)]));
    return download(`/reports/people/${name}?${p.toString()}`, filename);
  },
  /** Any report endpoint that answers `{ columns, rows }` as JSON and CSV with `?format=csv`. */
  table: (path, q = {}) => get(path, clean(q)),
  tableCsv: (path, q, filename) => {
    const p = new URLSearchParams(Object.entries(clean({ ...q, format: 'csv' })).map(([k, v]) => [k, String(v)]));
    return download(`${path}?${p.toString()}`, filename);
  },
  datasets: () => get('/reports/adhoc/datasets'),
  run: (def) => post('/reports/adhoc/run', def),
  runCsv: async (def, filename) => {
    const res = await fetch('/api/v1/reports/adhoc/run?format=csv', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(def),
    });
    if (!res.ok) {
      let msg = `Export failed (${res.status})`;
      try {
        const b = await res.json();
        msg = b.issues?.map((i) => i.message).join('; ') ?? b.error ?? msg;
      } catch {
        /* not JSON */
      }
      throw new Error(msg);
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  saved: () => get('/reports/adhoc/saved'),
  save: (b) => post('/reports/adhoc/saved', b),
  update: (id, b) => put(`/reports/adhoc/saved/${id}`, b),
  remove: (id) => del(`/reports/adhoc/saved/${id}`),
};
