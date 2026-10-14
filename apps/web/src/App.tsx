import { lazy } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { Layout } from './components/Layout';
import { RequireRole } from './auth/RequireRole';
import { useAuth } from './auth/AuthProvider';
import { AttendanceHub, LeaveHub, PayrollHub, ProfileHub, SettingsHub } from './pages/hubs';

const LoginPage = lazy(() => import('./pages/LoginPage'));
const ChangePasswordPage = lazy(() => import('./pages/ChangePasswordPage'));
const HomePage = lazy(() => import('./pages/employee/HomePage'));
const MyAttendancePage = lazy(() => import('./pages/employee/MyAttendancePage'));
const LeavePage = lazy(() => import('./pages/employee/LeavePage'));
const PayslipsPage = lazy(() => import('./pages/employee/PayslipsPage'));
const PayslipDetailPage = lazy(() => import('./pages/employee/PayslipDetailPage'));
const NotificationsPage = lazy(() => import('./pages/employee/NotificationsPage'));
const ApprovalsPage = lazy(() => import('./pages/manager/ApprovalsPage'));
const DashboardPage = lazy(() => import('./pages/hr/DashboardPage'));
const EmployeesPage = lazy(() => import('./pages/hr/EmployeesPage'));
const EmployeeDetailPage = lazy(() => import('./pages/hr/EmployeeDetailPage'));
const ReportsPage = lazy(() => import('./pages/hr/ReportsPage'));
const RunWizardPage = lazy(() => import('./pages/payroll/RunWizardPage'));

/** Everyone lands where their work is: employees on their own day, HR on the overview, managers on approvals. */
function Home() {
  const { user, hasRole } = useAuth();
  if (user?.employeeId) return <HomePage />;
  if (hasRole('hr')) return <Navigate to="/overview" replace />;
  if (hasRole('manager')) return <Navigate to="/approvals" replace />;
  return <HomePage />;
}

function EmployeeRedirect() {
  const { id } = useParams();
  return <Navigate to={`/employees/${id ?? ''}`} replace />;
}

/** Addresses from the earlier layout keep working. */
const LEGACY: Array<[string, string]> = [
  ['/hr/dashboard', '/overview'], ['/hr/employees', '/employees'], ['/manager/approvals', '/approvals'], ['/manager/team', '/attendance'],
  ['/hr/exceptions', '/attendance/exceptions'], ['/hr/ot-slips', '/attendance/overtime'], ['/manager/fix-punch', '/attendance/fix'],
  ['/manager/calendar', '/leave'], ['/hr/requests', '/leave/history'], ['/hr/leave-adjust', '/leave/balances'], ['/hr/leave-types', '/leave/policies'],
  ['/hr/reports', '/reports'], ['/hr/settings', '/settings'], ['/hr/shifts', '/settings/shifts'], ['/hr/holidays', '/settings/holidays'],
  ['/hr/devices', '/settings/devices'], ['/hr/users', '/settings/users'], ['/hr/setup', '/settings/setup'], ['/me/data', '/me/profile/privacy'],
];

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireRole />}>
        <Route path="/change-password" element={<ChangePasswordPage />} />
        <Route element={<Layout />}>
          <Route path="/" element={<Home />} />
          <Route path="/me/attendance" element={<MyAttendancePage />} />
          <Route path="/me/leave" element={<LeavePage />} />
          <Route path="/me/payslips" element={<PayslipsPage />} />
          <Route path="/me/payslips/:id" element={<PayslipDetailPage />} />
          <Route path="/me/notifications" element={<NotificationsPage />} />
          <Route path="/me/profile/*" element={<ProfileHub />} />
          <Route element={<RequireRole roles={['manager', 'hr']} />}>
            <Route path="/approvals" element={<ApprovalsPage />} />
            <Route path="/attendance/*" element={<AttendanceHub />} />
            <Route path="/leave/*" element={<LeaveHub />} />
          </Route>
          <Route element={<RequireRole roles={['hr']} />}>
            <Route path="/overview" element={<DashboardPage />} />
            <Route path="/employees" element={<EmployeesPage />} />
            <Route path="/employees/:id" element={<EmployeeDetailPage />} />
            <Route path="/reports" element={<ReportsPage />} />
            <Route path="/settings/*" element={<SettingsHub />} />
          </Route>
          <Route element={<RequireRole roles={['hr']} />}>
            <Route path="/payroll/runs/:id" element={<RunWizardPage />} />
            <Route path="/payroll/*" element={<PayrollHub />} />
          </Route>
          <Route path="/hr/employees/:id" element={<EmployeeRedirect />} />
          {LEGACY.map(([from, to]) => <Route key={from} path={from} element={<Navigate to={to} replace />} />)}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}
