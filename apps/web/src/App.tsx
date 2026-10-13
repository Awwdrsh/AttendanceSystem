import { lazy } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { RequireRole } from './auth/RequireRole';
import { useAuth } from './auth/AuthProvider';

const LoginPage = lazy(() => import('./pages/LoginPage'));
const ChangePasswordPage = lazy(() => import('./pages/ChangePasswordPage'));
const HomePage = lazy(() => import('./pages/employee/HomePage'));
const MyAttendancePage = lazy(() => import('./pages/employee/MyAttendancePage'));
const LeavePage = lazy(() => import('./pages/employee/LeavePage'));
const OvertimePage = lazy(() => import('./pages/employee/OvertimePage'));
const PayslipsPage = lazy(() => import('./pages/employee/PayslipsPage'));
const PayslipDetailPage = lazy(() => import('./pages/employee/PayslipDetailPage'));
const MyDataPage = lazy(() => import('./pages/employee/MyDataPage'));
const NotificationsPage = lazy(() => import('./pages/employee/NotificationsPage'));
const ProfilePage = lazy(() => import('./pages/employee/ProfilePage'));
const ApprovalsPage = lazy(() => import('./pages/manager/ApprovalsPage'));
const TeamTodayPage = lazy(() => import('./pages/manager/TeamTodayPage'));
const TeamCalendarPage = lazy(() => import('./pages/manager/TeamCalendarPage'));
const FixPunchPage = lazy(() => import('./pages/manager/FixPunchPage'));
const RosterPage = lazy(() => import('./pages/manager/RosterPage'));
const SetupPage = lazy(() => import('./pages/hr/SetupPage'));
const DashboardPage = lazy(() => import('./pages/hr/DashboardPage'));
const EmployeesPage = lazy(() => import('./pages/hr/EmployeesPage'));
const EmployeeDetailPage = lazy(() => import('./pages/hr/EmployeeDetailPage'));
const DevicesPage = lazy(() => import('./pages/hr/DevicesPage'));
const ShiftsPage = lazy(() => import('./pages/hr/ShiftsPage'));
const HolidaysPage = lazy(() => import('./pages/hr/HolidaysPage'));
const LocationsPage = lazy(() => import('./pages/hr/LocationsPage'));
const LeaveTypesPage = lazy(() => import('./pages/hr/LeaveTypesPage'));
const LeaveAdjustPage = lazy(() => import('./pages/hr/LeaveAdjustPage'));
const ExceptionsPage = lazy(() => import('./pages/hr/ExceptionsPage'));
const RequestsPage = lazy(() => import('./pages/hr/RequestsPage'));
const OtSlipsPage = lazy(() => import('./pages/hr/OtSlipsPage'));
const SettingsPage = lazy(() => import('./pages/hr/SettingsPage'));
const ReportsPage = lazy(() => import('./pages/hr/ReportsPage'));
const UsersPage = lazy(() => import('./pages/hr/UsersPage'));
const PayrollHubPage = lazy(() => import('./pages/payroll/PayrollHubPage'));
const RunWizardPage = lazy(() => import('./pages/payroll/RunWizardPage'));
const StructuresPage = lazy(() => import('./pages/payroll/StructuresPage'));
const AssignmentsPage = lazy(() => import('./pages/payroll/AssignmentsPage'));
const DeclarationsPage = lazy(() => import('./pages/payroll/DeclarationsPage'));
const LoansPage = lazy(() => import('./pages/payroll/LoansPage'));
const RulesPage = lazy(() => import('./pages/payroll/RulesPage'));

/** Home depends on who you are: employees land on Today; everyone else on the dashboard they can see. */
function Home() {
  const { user, hasRole } = useAuth();
  if (user?.employeeId) return <HomePage />;
  if (hasRole('hr', 'finance')) return <Navigate to="/hr/dashboard" replace />;
  if (hasRole('manager')) return <Navigate to="/manager/approvals" replace />;
  return <HomePage />;
}

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
          <Route path="/me/overtime" element={<OvertimePage />} />
          <Route path="/me/payslips" element={<PayslipsPage />} />
          <Route path="/me/payslips/:id" element={<PayslipDetailPage />} />
          <Route path="/me/data" element={<MyDataPage />} />
          <Route path="/me/notifications" element={<NotificationsPage />} />
          <Route path="/me/profile" element={<ProfilePage />} />
          <Route element={<RequireRole roles={['manager', 'hr']} />}>
            <Route path="/manager/approvals" element={<ApprovalsPage />} />
            <Route path="/manager/team" element={<TeamTodayPage />} />
            <Route path="/manager/calendar" element={<TeamCalendarPage />} />
            <Route path="/manager/fix-punch" element={<FixPunchPage />} />
            <Route path="/manager/roster" element={<RosterPage />} />
          </Route>
          <Route element={<RequireRole roles={['hr']} />}>
            <Route path="/hr/setup" element={<SetupPage />} />
            <Route path="/hr/dashboard" element={<DashboardPage />} />
            <Route path="/hr/employees" element={<EmployeesPage />} />
            <Route path="/hr/employees/:id" element={<EmployeeDetailPage />} />
            <Route path="/hr/devices" element={<DevicesPage />} />
            <Route path="/hr/shifts" element={<ShiftsPage />} />
            <Route path="/hr/holidays" element={<HolidaysPage />} />
            <Route path="/hr/locations" element={<LocationsPage />} />
            <Route path="/hr/leave-types" element={<LeaveTypesPage />} />
            <Route path="/hr/leave-adjust" element={<LeaveAdjustPage />} />
            <Route path="/hr/exceptions" element={<ExceptionsPage />} />
            <Route path="/hr/requests" element={<RequestsPage />} />
            <Route path="/hr/ot-slips" element={<OtSlipsPage />} />
            <Route path="/hr/settings" element={<SettingsPage />} />
            <Route path="/hr/reports" element={<ReportsPage />} />
            <Route path="/hr/users" element={<UsersPage />} />
          </Route>
          <Route element={<RequireRole roles={['finance', 'hr']} />}>
            <Route path="/payroll" element={<PayrollHubPage />} />
            <Route path="/payroll/runs/:id" element={<RunWizardPage />} />
            <Route path="/payroll/structures" element={<StructuresPage />} />
            <Route path="/payroll/assignments" element={<AssignmentsPage />} />
            <Route path="/payroll/declarations" element={<DeclarationsPage />} />
            <Route path="/payroll/loans" element={<LoansPage />} />
            <Route path="/payroll/rules" element={<RulesPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}
