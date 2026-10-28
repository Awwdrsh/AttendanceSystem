import { lazy } from 'react';
import { Navigate, Route, Routes, useParams } from 'react-router-dom';
import { Layout } from './components/Layout';
import { RequireRole } from './auth/RequireRole';
import { AREA } from './lib/access';
import { useAuth } from './auth/AuthProvider';
import { FeatureRoute, FeaturesProvider } from './lib/features';
import {
  AttendanceHub,
  EMPLOYEES_HUB_PATHS,
  EmployeesHub,
  EngageHub,
  ExpensesHub,
  GrowthHub,
  HiringHub,
  LeaveHub,
  MyAttendanceHub,
  MyClaimsHub,
  MyPayHub,
  PayrollHub,
  PeopleHub,
  HelpHub,
  RelationsHub,
  PerformanceHub,
  ProfileHub,
  ReportsHub,
  SettingsHub,
  TimesheetHub,
} from './pages/hubs';

const LoginPage = lazy(() => import('./pages/LoginPage'));
const ChangePasswordPage = lazy(() => import('./pages/ChangePasswordPage'));
const TwoStepSetupPage = lazy(() => import('./pages/employee/TwoStepSetupPage'));
const HomePage = lazy(() => import('./pages/employee/HomePage'));
const LeavePage = lazy(() => import('./pages/employee/LeavePage'));
const PayslipDetailPage = lazy(() => import('./pages/employee/PayslipDetailPage'));
const NotificationsPage = lazy(() => import('./pages/employee/NotificationsPage'));
const ApprovalsPage = lazy(() => import('./pages/manager/ApprovalsPage'));
const DashboardPage = lazy(() => import('./pages/hr/DashboardPage'));
const EmployeeDetailPage = lazy(() => import('./pages/hr/EmployeeDetailPage'));

const RunWizardPage = lazy(() => import('./pages/payroll/RunWizardPage'));
const HelpdeskPage = lazy(() => import('./pages/helpdesk/HelpdeskPage'));
const InsightsPage = lazy(() => import('./pages/insights/InsightsPage'));
const CareersPage = lazy(() => import('./pages/hiring/CareersPage'));
const KioskPage = lazy(() => import('./pages/field/KioskPage'));
const TasksPage = lazy(() => import('./pages/tasks/TasksPage'));

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
const LEGACY = [
  ['/hr/dashboard', '/overview'],
  ['/hr/employees', '/employees'],
  ['/manager/approvals', '/approvals'],
  ['/manager/team', '/attendance'],
  ['/hr/exceptions', '/attendance/exceptions'],
  ['/hr/ot-slips', '/attendance/overtime'],
  ['/manager/fix-punch', '/attendance/fix'],
  ['/manager/calendar', '/leave'],
  ['/hr/requests', '/leave/history'],
  ['/hr/leave-adjust', '/leave/balances'],
  ['/hr/leave-types', '/leave/policies'],
  ['/hr/reports', '/reports'],
  ['/hr/settings', '/settings'],
  ['/hr/shifts', '/settings/shifts'],
  ['/hr/holidays', '/settings/holidays'],
  ['/hr/devices', '/settings/devices'],
  ['/hr/users', '/settings/users'],
  ['/hr/setup', '/settings/setup'],
  ['/me/data', '/me/profile/privacy'],
];

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/careers/*" element={<CareersPage />} />
      <Route path="/kiosk" element={<KioskPage />} />
      <Route element={<RequireRole />}>
        <Route path="/change-password" element={<ChangePasswordPage />} />
        <Route path="/setup-two-step" element={<TwoStepSetupPage />} />
        <Route
          element={
            <FeaturesProvider>
              <Layout />
            </FeaturesProvider>
          }
        >
          <Route path="/" element={<Home />} />
          <Route path="/me/attendance/*" element={<MyAttendanceHub />} />
          <Route path="/me/timesheet" element={<TimesheetHub />} />
          <Route path="/me/claims/*" element={<MyClaimsHub />} />
          <Route path="/me/growth/*" element={<GrowthHub />} />
          <Route
            path="/me/help/*"
            element={
              <FeatureRoute feature="helpdesk">
                <HelpHub />
              </FeatureRoute>
            }
          />
          <Route
            path="/tasks"
            element={
              <FeatureRoute feature="tasks">
                <TasksPage />
              </FeatureRoute>
            }
          />
          <Route
            path="/tasks/:boardId"
            element={
              <FeatureRoute feature="tasks">
                <TasksPage />
              </FeatureRoute>
            }
          />
          <Route path="/people/*" element={<PeopleHub />} />
          <Route path="/engage/*" element={<EngageHub />} />
          <Route path="/me/leave" element={<LeavePage />} />
          <Route path="/me/payslips" element={<MyPayHub />} />
          <Route path="/me/payslips/tax" element={<MyPayHub />} />
          <Route path="/me/payslips/:id" element={<PayslipDetailPage />} />
          <Route path="/me/notifications" element={<NotificationsPage />} />
          <Route path="/me/profile/*" element={<ProfileHub />} />
          {/* Anyone can be asked to approve (a named person or a group member in a flow); the inbox shows only their items. */}
          <Route path="/approvals" element={<ApprovalsPage />} />
          <Route element={<RequireRole roles={['manager', ...AREA.attendance]} />}>
            <Route path="/attendance/*" element={<AttendanceHub />} />
            <Route path="/leave/*" element={<LeaveHub />} />
          </Route>
          <Route element={<RequireRole roles={['manager', 'hr']} />}>
            <Route path="/expenses/*" element={<ExpensesHub />} />
            <Route path="/performance/*" element={<PerformanceHub />} />
          </Route>
          <Route element={<RequireRole roles={['manager', ...AREA.hiring]} />}>
            <Route path="/hiring/*" element={<HiringHub />} />
          </Route>
          <Route element={<RequireRole roles={AREA.insights} />}>
            <Route path="/overview" element={<DashboardPage />} />
            <Route path="/insights" element={<InsightsPage />} />
          </Route>
          <Route element={<RequireRole roles={['hr']} />}>
            {EMPLOYEES_HUB_PATHS.map((p) => (
              <Route key={p} path={p} element={<EmployeesHub />} />
            ))}
            <Route
              path="/helpdesk"
              element={
                <FeatureRoute feature="helpdesk">
                  <HelpdeskPage />
                </FeatureRoute>
              }
            />
            <Route path="/relations/*" element={<RelationsHub />} />
            <Route path="/employees/:id" element={<EmployeeDetailPage />} />
            <Route path="/reports/*" element={<ReportsHub />} />
            <Route path="/settings/*" element={<SettingsHub />} />
          </Route>
          <Route element={<RequireRole roles={AREA.payroll} />}>
            <Route path="/payroll/runs/:id" element={<RunWizardPage />} />
            <Route path="/payroll/*" element={<PayrollHub />} />
          </Route>
          <Route path="/hr/employees/:id" element={<EmployeeRedirect />} />
          {LEGACY.map(([from, to]) => (
            <Route key={from} path={from} element={<Navigate to={to} replace />} />
          ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}
