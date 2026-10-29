import { describe, expect, it } from 'vitest';
import {
  accessByPerson,
  activeCompany,
  attentionByCompany,
  canOpen,
  landingAfterSwitch,
  companyChanges,
  groupAccess,
  groupHome,
  isRecordSegment,
  pathAfterSwitch,
  policyTotalsLabel,
  previewChanges,
  reportQuery,
  safeNext,
  validCompanyCode,
} from './group';

const ID = '01934a2b-7c3d-7e8f-9a0b-1c2d3e4f5a6b';

describe('pathAfterSwitch', () => {
  it('keeps a page address', () => {
    expect(pathAfterSwitch('/employees')).toBe('/employees');
    expect(pathAfterSwitch('/attendance/exceptions', '?status=open')).toBe('/attendance/exceptions?status=open');
    expect(pathAfterSwitch('/')).toBe('/');
    expect(pathAfterSwitch('')).toBe('/');
  });
  it('goes to the hub root when the address names a record of the old company', () => {
    expect(pathAfterSwitch(`/employees/${ID}`)).toBe('/employees');
    expect(pathAfterSwitch(`/payroll/runs/${ID}`, '?step=2')).toBe('/payroll/runs');
    expect(pathAfterSwitch(`/tasks/${ID}`)).toBe('/tasks');
    expect(pathAfterSwitch(`/me/payslips/${ID}`)).toBe('/me/payslips');
    expect(pathAfterSwitch(`/${ID}`)).toBe('/');
  });
  it('drops a query string that carries an id', () => {
    expect(pathAfterSwitch('/attendance', `?employeeId=${ID}`)).toBe('/attendance');
  });
  it('treats long numbers as ids but not short words', () => {
    expect(isRecordSegment('123456')).toBe(true);
    expect(isRecordSegment('2083')).toBe(true);
    expect(isRecordSegment('tax')).toBe(false);
    expect(isRecordSegment('12')).toBe(false);
  });
});

describe('safeNext', () => {
  it('follows only same-site paths', () => {
    expect(safeNext('/approvals')).toBe('/approvals');
    expect(safeNext('/employees/onboarding?tab=probation')).toBe('/employees/onboarding?tab=probation');
    expect(safeNext('//evil.test/x')).toBe('/');
    expect(safeNext('https://evil.test')).toBe('/');
    expect(safeNext('/\\evil.test')).toBe('/');
    expect(safeNext(undefined)).toBe('/');
  });
});

describe('groupAccess', () => {
  it('is closed without data', () => {
    expect(groupAccess(null).hub).toBe(false);
  });
  it('opens the hub for viewers, admins, transferers and standalone admins', () => {
    const viewer = groupAccess({ canViewGroup: true, companies: [{}, {}] });
    expect(viewer).toMatchObject({ hub: true, view: true, admin: false, multi: true });
    expect(groupHome(viewer)).toBe('/group');
    const starter = groupAccess({ canStartGroup: true, companies: [{}] });
    expect(starter).toMatchObject({ hub: true, view: false, multi: false });
    expect(groupHome(starter)).toBe('/group/companies');
    expect(groupHome(groupAccess({ canTransfer: true }))).toBe('/group/transfers');
    expect(groupAccess({ companies: [{}, {}] }).hub).toBe(false);
  });
  it('finds the active company', () => {
    expect(activeCompany({ companies: [{ id: 'a' }, { id: 'b', active: true }] }).id).toBe('b');
    expect(activeCompany(null)).toBe(null);
  });
});

describe('grouping', () => {
  it('groups needs-attention items by company in order', () => {
    const g = attentionByCompany([
      { companyId: 'b', companyName: 'Beta', kind: 'x' },
      { companyId: 'a', companyName: 'Alpha', kind: 'y', active: true },
      { companyId: 'b', companyName: 'Beta', kind: 'z' },
    ]);
    expect(g.map((x) => x.companyId)).toEqual(['b', 'a']);
    expect(g[0].items.map((i) => i.kind)).toEqual(['x', 'z']);
    expect(g[1].active).toBe(true);
    expect(attentionByCompany(undefined)).toEqual([]);
  });
  it('groups access rows by person', () => {
    const p = accessByPerson([
      { userId: 'u1', name: 'Asha', companyId: 'a' },
      { userId: 'u2', name: 'Bimal', companyId: 'b' },
      { userId: 'u1', name: 'Asha', companyId: 'b' },
    ]);
    expect(p).toHaveLength(2);
    expect(p[0].companies.map((c) => c.companyId)).toEqual(['a', 'b']);
  });
});

describe('policies', () => {
  it('summarises preview totals', () => {
    expect(policyTotalsLabel({ create: 3, overwrite: 1, keep: 0, same: 2, skip: 0 })).toBe(
      '3 to create, 1 to overwrite, 2 already the same',
    );
    expect(policyTotalsLabel({ create: 0, overwrite: 0, keep: 0, same: 0, skip: 0 })).toBe('Nothing to copy');
    expect(previewChanges({ create: 0, overwrite: 1 })).toBe(true);
    expect(previewChanges({ create: 0, overwrite: 0, keep: 4 })).toBe(false);
    expect(previewChanges(null)).toBe(false);
  });
});

describe('companies', () => {
  it('checks codes like the server', () => {
    expect(validCompanyCode('BETA')).toBe(true);
    expect(validCompanyCode('b-2_x')).toBe(true);
    expect(validCompanyCode('B')).toBe(false);
    expect(validCompanyCode('-AB')).toBe(false);
    expect(validCompanyCode('A'.repeat(21))).toBe(false);
  });
  it('sends only changed fields, blanks as null', () => {
    const before = { nameEn: 'Beta', nameNe: null, pan: '123456789', address: 'Pokhara', logoDataUrl: null };
    expect(companyChanges(before, { ...before, address: '' })).toEqual({ address: null });
    expect(companyChanges(before, { ...before, nameEn: 'Beta Foods', pan: '987654321' })).toEqual({
      nameEn: 'Beta Foods',
      pan: '987654321',
    });
    expect(companyChanges(before, { ...before, nameEn: '' })).toEqual({});
    expect(companyChanges(before, before)).toEqual({});
  });
});

describe('reportQuery', () => {
  it('sends the month or the fiscal year', () => {
    expect(reportQuery({ period: 'month', bsYear: 2082, bsMonth: 7, fy: 2082, companyId: null })).toEqual({
      period: 'month',
      bsYear: 2082,
      bsMonth: 7,
    });
    expect(reportQuery({ period: 'fy', bsYear: 2082, bsMonth: 7, fy: 2081, companyId: 'c1' })).toEqual({
      period: 'fy',
      fy: 2081,
      companyId: 'c1',
    });
  });
});

describe('landingAfterSwitch', () => {
  it('keeps a page the new roles open', () => {
    expect(landingAfterSwitch('/payroll', '', ['payroll_admin'])).toBe('/payroll');
    expect(landingAfterSwitch('/attendance/exceptions', '', ['manager'])).toBe('/attendance/exceptions');
    expect(landingAfterSwitch('/settings', '', ['admin'])).toBe('/settings');
    expect(landingAfterSwitch('/people', '', ['employee'])).toBe('/people');
  });
  it('goes home when the page is closed in the new company', () => {
    expect(landingAfterSwitch('/payroll', '', ['manager', 'employee'])).toBe('/');
    expect(landingAfterSwitch(`/employees/${ID}`, '', ['employee'])).toBe('/');
    expect(canOpen('/employeesx', ['employee'])).toBe(true);
    expect(canOpen('/overview?x=1', ['executive'])).toBe(true);
  });
});
