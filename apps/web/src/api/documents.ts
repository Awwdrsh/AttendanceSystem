import { del, get, patch, post, put, type Query } from './client';

export interface DocCategory { id: string; code: string; name: string; employee_can_upload: boolean; expiry_required: boolean; visible_to_employee: boolean; sort_order: number; active: boolean }

export interface EmployeeDoc {
  id: string; employee_id: string; category_id: string; category_name: string; title: string; file_id: string; version: number; replaces_id: string | null;
  is_current: boolean; status: 'pending' | 'verified' | 'rejected'; reject_reason: string | null; expiry_date: string | null; visible_to_employee: boolean;
  notes: string | null; uploaded_by: string | null; uploaded_by_name: string | null; verified_at: string | null; created_at: string; filename: string; mime: string; bytes: number;
  emp_code?: string; name_en?: string; days_left?: number;
}

export interface LetterTemplate { id: string; code: string; name: string; subject: string; body?: string; requestable?: boolean; visible_to_employee?: boolean; is_builtin?: boolean; active?: boolean; updated_at?: string }
export interface Placeholder { key: string; label: string }
export interface LetterSettings { signatoryName: string | null; signatoryTitle: string | null; refPrefix: string }

export interface IssuedLetter {
  id: string; employee_id: string; emp_code: string; name_en: string; template_code: string; ref_no: string; subject: string; visible_to_employee: boolean;
  request_id: string | null; issued_at: string; voided_at: string | null; void_reason: string | null; issued_by_name: string | null;
}

export interface LetterRequest {
  id: string; employee_id: string; emp_code: string; name_en: string; template_id: string; template_name: string; template_code: string; purpose: string;
  addressed_to: string | null; status: 'pending' | 'approved' | 'rejected' | 'issued' | 'cancelled'; decision_reason: string | null; issued_letter_id: string | null;
  created_at: string; decided_at: string | null;
}

export interface Policy {
  id: string; title: string; description: string | null; requires_ack: boolean; published_at: string; filename: string; my_ack_at: string | null; in_audience: boolean;
  audience?: 'all' | 'branch' | 'department'; audience_ids?: string[]; archived_at?: string | null; acks?: number; audience_size?: number;
}
export interface PolicyAck { employee_id: string; emp_code: string; name_en: string; acknowledged_at: string | null; last_reminded_at: string | null }

export const documents = {
  categories: () => get<{ categories: DocCategory[] }>('/documents/categories'),
  createCategory: (b: { code: string; name: string; employeeCanUpload: boolean; expiryRequired: boolean; visibleToEmployee: boolean }) => post<{ id: string }>('/documents/categories', b),
  updateCategory: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/documents/categories/${id}`, b),

  mine: () => get<{ documents: EmployeeDoc[] }>('/documents/mine'),
  forEmployee: (employeeId: string, history = false) => get<{ documents: EmployeeDoc[] }>(`/documents/employee/${employeeId}`, history ? { history: 1 } : {}),
  queue: (days = 30) => get<{ pending: EmployeeDoc[]; expiring: EmployeeDoc[] }>('/documents/queue', { days }),
  add: (employeeId: string, b: { categoryId: string; title: string; fileId: string; expiryDate?: string | null; visibleToEmployee?: boolean; notes?: string | null; replacesId?: string | null }) =>
    post<{ id: string }>(`/documents/employee/${employeeId}`, b),
  verify: (id: string, decision: 'verify' | 'reject', reason?: string) => post<{ ok: boolean }>(`/documents/${id}/verify`, reason ? { decision, reason } : { decision }),
  update: (id: string, b: { title?: string; expiryDate?: string | null; visibleToEmployee?: boolean; notes?: string | null }) => patch<{ ok: boolean }>(`/documents/${id}`, b),
  remove: (id: string) => del<{ ok: boolean }>(`/documents/${id}`),
  fileUrl: (id: string, download = false) => `/api/v1/documents/${id}/file${download ? '?download=1' : ''}`,

  placeholders: () => get<{ placeholders: Placeholder[] }>('/documents/placeholders'),
  letterSettings: () => get<{ settings: LetterSettings }>('/documents/letter-settings'),
  saveLetterSettings: (b: LetterSettings) => put<{ settings: LetterSettings }>('/documents/letter-settings', b),
  templates: () => get<{ templates: LetterTemplate[] }>('/documents/templates'),
  createTemplate: (b: { code: string; name: string; subject: string; body: string; requestable: boolean; visibleToEmployee: boolean }) => post<{ id: string }>('/documents/templates', b),
  updateTemplate: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/documents/templates/${id}`, b),
  preview: (b: { employeeId: string; templateId?: string; subject?: string; body?: string; purpose?: string | null; addressedTo?: string | null }) =>
    post<{ html: string; missing: string[]; unknown: string[] }>('/documents/templates/preview', b),

  issue: (b: { templateId: string; employeeId: string; visibleToEmployee?: boolean; purpose?: string | null; addressedTo?: string | null; requestId?: string; allowMissing?: boolean }) =>
    post<{ id: string; refNo: string }>('/documents/letters', b),
  letters: (q?: Query) => get<{ letters: IssuedLetter[] }>('/documents/letters', q),
  letterUrl: (id: string) => `/api/v1/documents/letters/${id}/html`,
  voidLetter: (id: string, reason: string) => post<{ ok: boolean }>(`/documents/letters/${id}/void`, { reason }),

  requestLetter: (b: { templateId: string; purpose: string; addressedTo?: string | null }) => post<{ id: string }>('/documents/letter-requests', b),
  requests: (q?: Query) => get<{ requests: LetterRequest[] }>('/documents/letter-requests', q),
  cancelRequest: (id: string) => post<{ ok: boolean }>(`/documents/letter-requests/${id}/cancel`),

  policies: (archived = false) => get<{ policies: Policy[] }>('/documents/policies', archived ? { archived: 1 } : {}),
  publishPolicy: (b: { title: string; description?: string | null; fileId: string; audience: 'all' | 'branch' | 'department'; audienceIds: string[]; requiresAck: boolean }) => post<{ id: string }>('/documents/policies', b),
  updatePolicy: (id: string, b: { title?: string; description?: string | null; requiresAck?: boolean; archived?: boolean }) => patch<{ ok: boolean }>(`/documents/policies/${id}`, b),
  policyUrl: (id: string) => `/api/v1/documents/policies/${id}/file`,
  acknowledge: (id: string) => post<{ ok: boolean }>(`/documents/policies/${id}/acknowledge`),
  policyAcks: (id: string) => get<{ employees: PolicyAck[] }>(`/documents/policies/${id}/acks`),
  remindPolicy: (id: string) => post<{ sent: number }>(`/documents/policies/${id}/remind`),
};

/** Date part of a timestamp in Nepal time, for showing as a BS date. */
export function nptDateOf(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const ms = Date.parse(ts);
  return Number.isNaN(ms) ? null : new Date(ms + 345 * 60_000).toISOString().slice(0, 10);
}

export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
