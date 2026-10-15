import { download, get, patch, post, put, del, type Query } from './client';

export interface TimePolicy {
  enabled: boolean; requireProject: boolean; roundingStep: 0 | 5 | 6 | 10 | 15; roundingMode: 'nearest' | 'up' | 'down';
  fridayReminder: boolean; autoStopHours: number;
}

export interface TimeClient { id: string; name: string; notes: string | null; archived: boolean; active_projects?: number }

export interface TimeProject {
  id: string; code: string; name: string; color: string; billable: boolean; bill_rate: string | null; budget_hours: string | null;
  all_members: boolean; status: 'active' | 'archived'; client_id: string | null; client_name: string | null; tracked_min: number;
  members: Array<{ id: string; name: string; empCode: string }>;
}

export interface TimeActivity { id: string; name: string; project_id: string | null; project_name: string | null; archived: boolean }

export interface TimeEntry {
  id: string; employee_id: string; local_date: string; started_at: string | null; ended_at: string | null; duration_min: number | null;
  project_id: string | null; project_name: string | null; project_code: string | null; project_color: string | null; client_name: string | null;
  activity_id: string | null; activity_name: string | null; note: string | null; billable: boolean; source: 'timer' | 'manual'; auto_stopped: boolean;
}

export type TimesheetStatus = 'submitted' | 'approved' | 'rejected' | 'cancelled';

export interface TimesheetRow {
  id: string; week_start: string; status: TimesheetStatus; total_min: number; note: string | null; submitted_at: string; decided_at: string | null;
  decision_comment: string | null; workflow_instance_id: string | null; decided_by_name: string | null;
}

export interface WeekDay {
  date: string; entries: TimeEntry[]; trackedMin: number; roundedMin: number; billableMin: number;
  attendanceMin: number | null; attendanceStatus: string | null; diffMin: number | null; flag: 'over' | 'under' | 'no_attendance' | null;
}

export interface WeekView {
  weekStart: string; weekEnd: string; days: WeekDay[]; running: boolean;
  totals: { trackedMin: number; roundedMin: number; billableMin: number; attendanceMin: number; flaggedDays: number };
  timesheet: TimesheetRow | null; history: TimesheetRow[]; rounding: { step: number; mode: string };
}

export interface TimesheetListRow extends TimesheetRow {
  employee_id: string; emp_code: string; name_en: string; attendance_min: number; billable_min: number; diff_min: number; workflow_state: string | null;
}

export interface TeamWeekRow { employee_id: string; emp_code: string; name_en: string; tracked_min: number; attendance_min: number; timesheet_id: string | null; status: TimesheetStatus | null }

export interface SummaryRow {
  key: string; label: string; code: string | null; entries: number; tracked_hours: number; hours: number; billable_hours: number; non_billable_hours: number;
  billable_amount: number; budget_hours: number | null; budget_used_pct: number | null;
}

export interface EntryInput {
  date: string; start?: string; end?: string; durationMin?: number; projectId?: string | null; activityId?: string | null; note?: string | null; billable?: boolean;
}

export interface ProjectInput {
  code: string; name: string; clientId?: string | null; color?: string; billable?: boolean; billRate?: number | null; budgetHours?: number | null;
  allMembers?: boolean; memberIds?: string[]; status?: 'active' | 'archived';
}

export type GroupBy = 'project' | 'client' | 'employee' | 'activity' | 'day';

export const time = {
  settings: () => get<{ policy: TimePolicy }>('/time/settings'),
  saveSettings: (p: TimePolicy) => put<{ policy: TimePolicy }>('/time/settings', p),

  clients: (all = false) => get<{ clients: TimeClient[] }>('/time/clients', all ? { all: 1 } : undefined),
  createClient: (b: { name: string; notes?: string | null }) => post<{ client: TimeClient }>('/time/clients', b),
  updateClient: (id: string, b: { name?: string; notes?: string | null; archived?: boolean }) => patch<{ client: TimeClient }>(`/time/clients/${id}`, b),

  projects: (all = false) => get<{ projects: TimeProject[] }>('/time/projects', all ? { all: 1 } : undefined),
  createProject: (b: ProjectInput) => post<{ id: string }>('/time/projects', b),
  updateProject: (id: string, b: Partial<ProjectInput>) => patch<{ ok: boolean }>(`/time/projects/${id}`, b),

  activities: (q?: { projectId?: string; all?: 1 }) => get<{ activities: TimeActivity[] }>('/time/activities', q),
  createActivity: (b: { name: string; projectId?: string | null }) => post<{ activity: TimeActivity }>('/time/activities', b),
  updateActivity: (id: string, b: { name?: string; archived?: boolean }) => patch<{ activity: TimeActivity }>(`/time/activities/${id}`, b),

  timer: () => get<{ running: TimeEntry | null; last: TimeEntry | null; todayMin: number; serverTime: string }>('/time/timer'),
  start: (b: { projectId?: string | null; activityId?: string | null; note?: string | null; billable?: boolean }) => post<{ running: TimeEntry }>('/time/timer/start', b),
  resume: () => post<{ running: TimeEntry }>('/time/timer/resume'),
  stop: (note?: string | null) => post<{ entry: TimeEntry }>('/time/timer/stop', note === undefined ? {} : { note }),

  entries: (q: { from: string; to: string; employeeId?: string }) => get<{ entries: TimeEntry[] }>('/time/entries', q),
  addEntry: (b: EntryInput) => post<{ entry: TimeEntry }>('/time/entries', b),
  updateEntry: (id: string, b: Partial<EntryInput>) => patch<{ entry: TimeEntry }>(`/time/entries/${id}`, b),
  deleteEntry: (id: string) => del<{ ok: boolean }>(`/time/entries/${id}`),

  week: (q: { weekStart?: string; employeeId?: string }) => get<WeekView>('/time/week', q),
  submit: (weekStart: string, note?: string | null) => post<{ id: string }>('/time/timesheets', { weekStart, ...(note ? { note } : {}) }),
  withdraw: (id: string) => post<{ ok: boolean }>(`/time/timesheets/${id}/withdraw`),
  timesheets: (q?: Query) => get<{ timesheets: TimesheetListRow[] }>('/time/timesheets', q),
  timesheet: (id: string) => get<WeekView & { employee: { id: string; empCode: string; name: string }; submission: TimesheetRow }>(`/time/timesheets/${id}`),
  teamWeek: (weekStart: string) => get<{ weekStart: string; weekEnd: string; people: TeamWeekRow[] }>('/time/team-week', { weekStart }),

  summary: (q: { from: string; to: string; groupBy: GroupBy; projectId?: string; employeeId?: string; clientId?: string }) =>
    get<{ rows: SummaryRow[]; totals: { hours: number; billableHours: number; billableAmount: number }; rounding: { step: number; mode: string } }>('/time/reports/summary', q),
  downloadSummary: (q: { from: string; to: string; groupBy: GroupBy }) =>
    download(`/time/reports/summary?from=${q.from}&to=${q.to}&groupBy=${q.groupBy}&format=csv`, `time-by-${q.groupBy}-${q.from}-to-${q.to}.csv`),
  downloadEntries: (q: { from: string; to: string }) => download(`/time/reports/entries?from=${q.from}&to=${q.to}&format=csv`, `time-entries-${q.from}-to-${q.to}.csv`),
};

/** Sunday of the week containing an ISO date (weeks run Sunday to Saturday). */
export function weekStartOf(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return new Date(d.getTime() - d.getUTCDay() * 86_400_000).toISOString().slice(0, 10);
}

/** "3:05" style hours and minutes for timesheet grids. */
export function hhmmOf(min: number | null | undefined): string {
  if (!min) return '0:00';
  return `${Math.floor(min / 60)}:${String(min % 60).padStart(2, '0')}`;
}
