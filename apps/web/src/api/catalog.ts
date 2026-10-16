import { get, post, put, del, download, type Query } from './client';

/** Settings → Features: the modules a company can switch off. */
export type FeatureKey = 'time' | 'field' | 'expenses' | 'performance' | 'learning' | 'hiring' | 'lifecycle' | 'documents' | 'helpdesk' | 'tasks'
  | 'kudos' | 'surveys' | 'suggestions';

export interface FeatureInfo { key: FeatureKey; label: string; description: string; group: string; enabled: boolean }
export interface FeaturesResponse { features: FeatureInfo[]; enabled: Record<FeatureKey, boolean> }

export const features = {
  get: () => get<FeaturesResponse>('/settings/features'),
  save: (enabled: Partial<Record<FeatureKey, boolean>>) => put<FeaturesResponse>('/settings/features', { enabled }),
};

export interface TableColumn { key: string; header: string; type?: 'text' | 'number' | 'date' | 'bool' }
export interface TableResult { columns: TableColumn[]; rows: Array<Record<string, unknown>> }

export type PeopleReport = 'headcount-on-date' | 'statistics' | 'joiners-leavers' | 'birthdays-anniversaries' | 'nominees' | 'emergency-contacts'
  | 'addresses' | 'education' | 'licence-register' | 'record-completeness';

export type AdhocOp = 'eq' | 'neq' | 'contains' | 'starts' | 'gt' | 'gte' | 'lt' | 'lte' | 'empty' | 'not_empty';
export interface AdhocColumn { key: string; label: string; type: 'text' | 'number' | 'date' | 'bool' }
export interface AdhocDataset { key: string; label: string; description: string; columns: AdhocColumn[]; defaultColumns: string[] }
export interface AdhocFilter { column: string; op: AdhocOp; value?: string | number | boolean | null }
export interface AdhocDefinition { dataset: string; columns: string[]; filters: AdhocFilter[]; sort?: { column: string; dir: 'asc' | 'desc' } | null; includeExited: boolean }
export interface AdhocResult extends TableResult { total: number; limited: boolean }
export interface SavedReport { id: string; name: string; description: string | null; shared: boolean; definition: AdhocDefinition; mine: boolean; owner_name?: string; updated_at: string }

/** Turn `{ a: 1, b: undefined }` into a query string the API helpers accept. */
const clean = (q: Query) => Object.fromEntries(Object.entries(q).filter(([, v]) => v !== undefined && v !== null && v !== '')) as Query;

export const catalog = {
  people: (name: PeopleReport, q: Query = {}) => get<TableResult>(`/reports/people/${name}`, clean(q)),
  peopleCsv: (name: PeopleReport, q: Query, filename: string) => {
    const p = new URLSearchParams(Object.entries(clean({ ...q, format: 'csv' })).map(([k, v]) => [k, String(v)]));
    return download(`/reports/people/${name}?${p.toString()}`, filename);
  },
  /** Any report endpoint that answers `{ columns, rows }` as JSON and CSV with `?format=csv`. */
  table: (path: string, q: Query = {}) => get<TableResult>(path, clean(q)),
  tableCsv: (path: string, q: Query, filename: string) => {
    const p = new URLSearchParams(Object.entries(clean({ ...q, format: 'csv' })).map(([k, v]) => [k, String(v)]));
    return download(`${path}?${p.toString()}`, filename);
  },
  datasets: () => get<{ datasets: AdhocDataset[]; operators: AdhocOp[] }>('/reports/adhoc/datasets'),
  run: (def: AdhocDefinition) => post<AdhocResult>('/reports/adhoc/run', def),
  runCsv: async (def: AdhocDefinition, filename: string) => {
    const res = await fetch('/api/v1/reports/adhoc/run?format=csv', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(def) });
    if (!res.ok) {
      let msg = `Export failed (${res.status})`;
      try { const b = await res.json() as { error?: string; issues?: Array<{ message: string }> }; msg = b.issues?.map((i) => i.message).join('; ') ?? b.error ?? msg; } catch { /* not JSON */ }
      throw new Error(msg);
    }
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement('a'); a.href = url; a.download = filename; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
  saved: () => get<{ reports: SavedReport[] }>('/reports/adhoc/saved'),
  save: (b: { name: string; description?: string | null; shared: boolean; definition: AdhocDefinition }) => post<{ report: SavedReport }>('/reports/adhoc/saved', b),
  update: (id: string, b: { name: string; description?: string | null; shared: boolean; definition: AdhocDefinition }) => put<{ report: SavedReport }>(`/reports/adhoc/saved/${id}`, b),
  remove: (id: string) => del<{ ok: boolean }>(`/reports/adhoc/saved/${id}`),
};
