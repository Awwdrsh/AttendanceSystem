/** Pure helpers for groups of companies: the company switcher and the Group hub. Unit-tested in group.test.js. */
import { AREA } from './access';

/** The role guards of App.jsx by address prefix, to know whether a page is open in the company switched to. */
const ROUTE_ROLES = [
  ['/attendance', ['manager', ...AREA.attendance]],
  ['/leave', ['manager', ...AREA.attendance]],
  ['/expenses', ['manager', 'hr']],
  ['/performance', ['manager', 'hr']],
  ['/hiring', ['manager', ...AREA.hiring]],
  ['/overview', AREA.insights],
  ['/insights', AREA.insights],
  ['/employees', ['hr']],
  ['/helpdesk', ['hr']],
  ['/relations', ['hr']],
  ['/reports', ['hr']],
  ['/settings', ['hr']],
  ['/payroll', AREA.payroll],
];

/** Whether a person with these roles (in the new company) may open this address; admin may open everything. */
export function canOpen(path, roles) {
  const r = roles ?? [];
  if (r.includes('admin')) return true;
  const p = path.split('?')[0];
  const rule = ROUTE_ROLES.find(([prefix]) => p === prefix || p.startsWith(`${prefix}/`));
  return !rule || rule[1].some((x) => r.includes(x));
}

/** Where to land after a switch: the same page (or its hub root, see pathAfterSwitch) when the person's roles in the
 * new company open it, else home. */
export function landingAfterSwitch(pathname, search, roles) {
  const p = pathAfterSwitch(pathname, search);
  return canOpen(p, roles) ? p : '/';
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const UUID_ANYWHERE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/** A path segment that names one record (a UUID or a long number), not a page. */
export function isRecordSegment(seg) {
  return UUID.test(seg) || /^\d{4,}$/.test(seg);
}

/**
 * Where to go after switching company. Records belong to exactly one company, so an address that names a record
 * (/employees/<id>, /payroll/runs/<id>, /tasks/<board>) cannot exist in the other company: go back to the page
 * above it (the hub root). A query string that carries an id is dropped for the same reason.
 */
export function pathAfterSwitch(pathname, search = '') {
  const segs = (pathname || '/').split('/').filter(Boolean);
  const i = segs.findIndex(isRecordSegment);
  if (i >= 0) return `/${segs.slice(0, i).join('/')}`;
  const q = search && search !== '?' && !UUID_ANYWHERE.test(search) ? search : '';
  return `/${segs.join('/')}${q}`;
}

/** Only a same-site path is followed after a switch (a needs-attention link's `next`). */
export function safeNext(next) {
  if (typeof next !== 'string' || !next.startsWith('/') || next.startsWith('//') || next.includes('\\')) return '/';
  return next;
}

/** What the Group hub offers, from GET /me/companies. */
export function groupAccess(me) {
  if (!me) return { hub: false, view: false, admin: false, transfer: false, startGroup: false, multi: false };
  const view = !!me.canViewGroup;
  const admin = !!me.canAdministerGroup;
  const transfer = !!me.canTransfer;
  const startGroup = !!me.canStartGroup;
  return {
    hub: view || admin || transfer || startGroup,
    view,
    admin,
    transfer,
    startGroup,
    multi: (me.companies?.length ?? 0) > 1,
  };
}

/** The active company of GET /me/companies (null while loading). */
export function activeCompany(me) {
  return me?.companies?.find((c) => c.active) ?? null;
}

/** The first page of the Group hub this person may open. */
export function groupHome(access) {
  if (access.view) return '/group';
  if (access.admin || access.startGroup) return '/group/companies';
  if (access.transfer) return '/group/transfers';
  return '/';
}

/** Needs-attention items grouped by company, companies in the order they first appear. */
export function attentionByCompany(items) {
  const out = [];
  const byId = new Map();
  for (const i of items ?? []) {
    let g = byId.get(i.companyId);
    if (!g) {
      g = { companyId: i.companyId, companyName: i.companyName, companyCode: i.companyCode, active: !!i.active, items: [] };
      byId.set(i.companyId, g);
      out.push(g);
    }
    g.items.push(i);
  }
  return out;
}

export const POLICY_ACTION = {
  create: { label: 'Create', tone: 'ok' },
  overwrite: { label: 'Overwrite', tone: 'warn' },
  keep: { label: 'Kept as is', tone: 'info' },
  same: { label: 'Already the same', tone: 'idle' },
  skip: { label: 'Not copied', tone: 'bad' },
};

/** "3 to create, 1 to overwrite" for a policy preview's totals (only the non-zero counts). */
export function policyTotalsLabel(totals) {
  if (!totals) return '';
  const parts = [];
  if (totals.create) parts.push(`${totals.create} to create`);
  if (totals.overwrite) parts.push(`${totals.overwrite} to overwrite`);
  if (totals.keep) parts.push(`${totals.keep} kept as they are`);
  if (totals.same) parts.push(`${totals.same} already the same`);
  if (totals.skip) parts.push(`${totals.skip} not copied`);
  return parts.length ? parts.join(', ') : 'Nothing to copy';
}

/** Whether applying a preview would change anything. */
export function previewChanges(totals) {
  return !!totals && (totals.create ?? 0) + (totals.overwrite ?? 0) > 0;
}

export const GROUP_ROLE_LABEL = {
  group_admin: 'Group admin',
  group_hr: 'Group HR',
  group_executive: 'Group executive',
};

export const GROUP_ROLE_HELP = {
  group_admin: 'Admin in every company of the group: adds companies, gives access, copies policies',
  group_hr: 'HR in every company of the group, and transfers people between them',
  group_executive: 'Read-only group dashboard, directory and reports',
};

/** Rows of the access list grouped by person (one person can work in several companies). */
export function accessByPerson(rows) {
  const out = [];
  const byId = new Map();
  for (const r of rows ?? []) {
    let p = byId.get(r.userId);
    if (!p) {
      p = { userId: r.userId, name: r.name, email: r.email, companies: [] };
      byId.set(r.userId, p);
      out.push(p);
    }
    p.companies.push(r);
  }
  return out;
}

/** A company code as the server accepts it: 2 to 20 letters, digits, - or _, starting with a letter or digit. */
export function validCompanyCode(code) {
  return /^[A-Za-z0-9][A-Za-z0-9_-]{1,19}$/.test(code ?? '');
}

/** The body of a company PATCH: only changed fields, blanks as null (the server clears them). */
export function companyChanges(before, after) {
  const out = {};
  for (const k of ['nameEn', 'nameNe', 'pan', 'address', 'logoDataUrl']) {
    const a = after[k] === '' || after[k] === undefined ? null : after[k];
    const b = before?.[k] === '' || before?.[k] === undefined ? null : before?.[k];
    if (a !== b) out[k] = a;
  }
  if ('nameEn' in out && !out.nameEn) delete out.nameEn;
  return out;
}

/** Report filters as query parameters for /group/reports/<kind>. */
export function reportQuery(f) {
  const q = { period: f.period };
  if (f.companyId) q.companyId = f.companyId;
  if (f.period === 'fy') q.fy = f.fy;
  else {
    q.bsYear = f.bsYear;
    q.bsMonth = f.bsMonth;
  }
  return q;
}
