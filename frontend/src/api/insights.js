import { download, get } from './client';

export const insights = {
  get: (q) => get('/insights', q),
  filters: () => get('/insights/filters'),
  exportCsv: (dataset, q) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
    const s = p.toString();
    return download(`/insights/export/${dataset}${s ? `?${s}` : ''}`, `insights-${dataset}.csv`);
  },
};
