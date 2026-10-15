import { lazy } from 'react';
import { useAuth } from '../auth/AuthProvider';
import { Hub } from '../components/Hub';

const TeamTodayPage = lazy(() => import('./manager/TeamTodayPage'));
const ExceptionsPage = lazy(() => import('./hr/ExceptionsPage'));
const OtSlipsPage = lazy(() => import('./hr/OtSlipsPage'));
const FixPunchPage = lazy(() => import('./manager/FixPunchPage'));
const RemoteCheckinsPage = lazy(() => import('./manager/RemoteCheckinsPage'));
const RequestsPage = lazy(() => import('./hr/RequestsPage'));
const TeamCalendarPage = lazy(() => import('./manager/TeamCalendarPage'));
const LeaveAdjustPage = lazy(() => import('./hr/LeaveAdjustPage'));
const LeaveTypesPage = lazy(() => import('./hr/LeaveTypesPage'));
const PayrollHubPage = lazy(() => import('./payroll/PayrollHubPage'));
const StructuresPage = lazy(() => import('./payroll/StructuresPage'));
const AssignmentsPage = lazy(() => import('./payroll/AssignmentsPage'));
const DeclarationsPage = lazy(() => import('./payroll/DeclarationsPage'));
const LoansPage = lazy(() => import('./payroll/LoansPage'));
const RulesPage = lazy(() => import('./payroll/RulesPage'));
const SettingsPage = lazy(() => import('./hr/SettingsPage'));
const ShiftsPage = lazy(() => import('./hr/ShiftsPage'));
const HolidaysPage = lazy(() => import('./hr/HolidaysPage'));
const DevicesPage = lazy(() => import('./hr/DevicesPage'));
const UsersPage = lazy(() => import('./hr/UsersPage'));
const SetupPage = lazy(() => import('./hr/SetupPage'));
const ProfilePage = lazy(() => import('./employee/ProfilePage'));
const MyDataPage = lazy(() => import('./employee/MyDataPage'));

export function AttendanceHub() {
  return <Hub title="Attendance" tabs={[
    { path: '/attendance', label: 'Today', element: <TeamTodayPage /> },
    { path: '/attendance/from-home', label: 'Work from home', element: <RemoteCheckinsPage /> },
    { path: '/attendance/exceptions', label: 'Exceptions', element: <ExceptionsPage />, roles: ['hr'] },
    { path: '/attendance/overtime', label: 'Overtime', element: <OtSlipsPage />, roles: ['hr'] },
    { path: '/attendance/fix', label: 'Fix a punch', element: <FixPunchPage /> },
  ]} />;
}

export function LeaveHub() {
  return <Hub title="Leave" tabs={[
    { path: '/leave', label: 'Calendar', element: <TeamCalendarPage /> },
    { path: '/leave/balances', label: 'Balances', element: <LeaveAdjustPage />, roles: ['hr'] },
    { path: '/leave/policies', label: 'Policies', element: <LeaveTypesPage />, roles: ['hr'] },
    { path: '/leave/history', label: 'Request history', element: <RequestsPage />, roles: ['hr'] },
  ]} />;
}

export function PayrollHub() {
  return <Hub title="Payroll" tabs={[
    { path: '/payroll', label: 'Pay runs', element: <PayrollHubPage /> },
    { path: '/payroll/structures', label: 'Salary structures', element: <StructuresPage /> },
    { path: '/payroll/assignments', label: 'Employee salaries', element: <AssignmentsPage /> },
    { path: '/payroll/declarations', label: 'Tax declarations', element: <DeclarationsPage /> },
    { path: '/payroll/loans', label: 'Loans & advances', element: <LoansPage /> },
    { path: '/payroll/rules', label: 'Tax & SSF rules', element: <RulesPage /> },
  ]} />;
}

export function SettingsHub() {
  return <Hub title="Settings" tabs={[
    { path: '/settings', label: 'Company & rules', element: <SettingsPage /> },
    { path: '/settings/shifts', label: 'Shifts', element: <ShiftsPage /> },
    { path: '/settings/holidays', label: 'Holidays', element: <HolidaysPage /> },
    { path: '/settings/devices', label: 'Devices', element: <DevicesPage /> },
    { path: '/settings/users', label: 'Users & roles', element: <UsersPage /> },
    { path: '/settings/setup', label: 'Setup checklist', element: <SetupPage /> },
  ]} />;
}

export function ProfileHub() {
  const { user } = useAuth();
  return <Hub title="Profile" tabs={[
    { path: '/me/profile', label: 'Profile', element: <ProfilePage /> },
    { path: '/me/profile/privacy', label: 'Privacy & my data', element: <MyDataPage />, show: !!user?.employeeId },
  ]} />;
}
