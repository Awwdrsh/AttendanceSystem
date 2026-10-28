import { download, get, patch, post, put, del } from './client';

export const time = {
  settings: () => get('/time/settings'),
  saveSettings: (p) => put('/time/settings', p),

  clients: (all = false) => get('/time/clients', all ? { all: 1 } : undefined),
  createClient: (b) => post('/time/clients', b),
  updateClient: (id, b) => patch(`/time/clients/${id}`, b),

  projects: (all = false) => get('/time/projects', all ? { all: 1 } : undefined),
  createProject: (b) => post('/time/projects', b),
  updateProject: (id, b) => patch(`/time/projects/${id}`, b),

  activities: (q) => get('/time/activities', q),
  createActivity: (b) => post('/time/activities', b),
  updateActivity: (id, b) => patch(`/time/activities/${id}`, b),

  timer: () => get('/time/timer'),
  start: (b) => post('/time/timer/start', b),
  resume: () => post('/time/timer/resume'),
  stop: (note) => post('/time/timer/stop', note === undefined ? {} : { note }),

  entries: (q) => get('/time/entries', q),
  addEntry: (b) => post('/time/entries', b),
  updateEntry: (id, b) => patch(`/time/entries/${id}`, b),
  deleteEntry: (id) => del(`/time/entries/${id}`),

  week: (q) => get('/time/week', q),
  submit: (weekStart, note) => post('/time/timesheets', { weekStart, ...(note ? { note } : {}) }),
  withdraw: (id) => post(`/time/timesheets/${id}/withdraw`),
  timesheets: (q) => get('/time/timesheets', q),
  timesheet: (id) => get(`/time/timesheets/${id}`),
  teamWeek: (weekStart) => get('/time/team-week', { weekStart }),

  summary: (q) => get('/time/reports/summary', q),
  downloadSummary: (q) =>
    download(
      `/time/reports/summary?from=${q.from}&to=${q.to}&groupBy=${q.groupBy}&format=csv`,
      `time-by-${q.groupBy}-${q.from}-to-${q.to}.csv`,
    ),
  downloadEntries: (q) => download(`/time/reports/entries?from=${q.from}&to=${q.to}&format=csv`, `time-entries-${q.from}-to-${q.to}.csv`),
};

/** Sunday of the week containing an ISO date (weeks run Sunday to Saturday). */
export function weekStartOf(iso) {
  const d = new Date(`${iso}T00:00:00Z`);
  return new Date(d.getTime() - d.getUTCDay() * 86_400_000).toISOString().slice(0, 10);
}

/** "3:05" style hours and minutes for timesheet grids. */
export function hhmmOf(min) {
  if (!min) return '0:00';
  return `${Math.floor(min / 60)}:${String(min % 60).padStart(2, '0')}`;
}
