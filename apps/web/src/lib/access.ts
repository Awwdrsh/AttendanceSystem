import type { Role } from '../api/types';

/**
 * Who works in each HR area, mirroring MODULE_ROLES on the server: hr and admin everywhere, the specialist roles only
 * in their own area. Menus, route guards and tabs read from here.
 */
export const AREA = {
  payroll: ['hr', 'payroll_admin'],
  attendance: ['hr', 'attendance_admin'],
  hiring: ['hr', 'recruiter'],
  insights: ['hr', 'executive'],
} satisfies Record<string, Role[]>;

/** Everyone with some management view: line managers and heads, HR and the specialist roles. */
export const STAFF_ROLES: Role[] = ['manager', 'hr', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive'];

export const ROLE_LABEL: Record<Role, string> = {
  admin: 'Admin', hr: 'HR', manager: 'Manager', employee: 'Employee',
  payroll_admin: 'Payroll admin', attendance_admin: 'Attendance admin', recruiter: 'Recruiter', executive: 'Executive',
};

export const ROLE_HELP: Record<Role, string> = {
  admin: 'Everything, including users, roles and statutory rules',
  hr: 'All HR work across the company',
  manager: 'Their team: approvals, attendance and leave (department and branch heads get this automatically)',
  employee: 'Self-service: attendance, leave, payslips, claims',
  payroll_admin: 'Payroll, salary structures, loans and tax only',
  attendance_admin: 'Attendance, leave, shifts, rosters, field work and time tracking only',
  recruiter: 'Hiring: jobs, candidates, interviews and offers (not adding employees)',
  executive: 'Read-only overview, insights and dashboards',
};
