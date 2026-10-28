import { lazy } from 'react';
import { useAuth } from '../auth/AuthProvider';
import { Hub } from '../components/Hub';
import { AREA } from '../lib/access';

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
const PayrollReportsPage = lazy(() => import('./payroll/compliance/PayrollReportsPage'));
const OptimumCitPage = lazy(() => import('./payroll/compliance/OptimumCitPage'));
const OpeningBalancesPage = lazy(() => import('./payroll/compliance/OpeningBalancesPage'));
const HeldPayPage = lazy(() => import('./payroll/compliance/HeldPayPage'));
const SettingsPage = lazy(() => import('./hr/SettingsPage'));
const ShiftsPage = lazy(() => import('./hr/ShiftsPage'));
const HolidaysPage = lazy(() => import('./hr/HolidaysPage'));
const DevicesPage = lazy(() => import('./hr/DevicesPage'));
const UsersPage = lazy(() => import('./hr/UsersPage'));
const SetupPage = lazy(() => import('./hr/SetupPage'));
const ApprovalFlowsPage = lazy(() => import('./hr/ApprovalFlowsPage'));
const ProfilePage = lazy(() => import('./employee/ProfilePage'));
const MyDataPage = lazy(() => import('./employee/MyDataPage'));
const MyTicketsPage = lazy(() => import('./helpdesk/MyTicketsPage'));
const MyGrievancesPage = lazy(() => import('./relations/MyGrievancesPage'));
const MyConductPage = lazy(() => import('./relations/MyConductPage'));
const MyDevicesPage = lazy(() => import('./relations/MyDevicesPage'));
const GrievancesAdminPage = lazy(() => import('./relations/GrievancesAdminPage'));
const DisciplinaryPage = lazy(() => import('./relations/DisciplinaryPage'));
const MilestonesPage = lazy(() => import('./relations/MilestonesPage'));
const TempAssignmentsPage = lazy(() => import('./relations/TempAssignmentsPage'));
const KyePage = lazy(() => import('./relations/KyePage'));
const RegisteredDevicesPage = lazy(() => import('./relations/RegisteredDevicesPage'));
const MyAttendancePage = lazy(() => import('./employee/MyAttendancePage'));
const PayslipsPage = lazy(() => import('./employee/PayslipsPage'));
const EmployeesPage = lazy(() => import('./hr/EmployeesPage'));
const SecurityPage = lazy(() => import('./employee/SecurityPage'));
const MyTaxPage = lazy(() => import('./employee/MyTaxPage'));
const MyTimesheetPage = lazy(() => import('./time/MyTimesheetPage'));
const TimesheetsReviewPage = lazy(() => import('./time/TimesheetsReviewPage'));
const ProjectsPage = lazy(() => import('./time/ProjectsPage'));
const FieldVisitsPage = lazy(() => import('./field/FieldVisitsPage'));
const MyFieldVisitsPage = lazy(() => import('./field/MyFieldVisitsPage'));
const RostersPage = lazy(() => import('./field/RostersPage'));
const MyRosterPage = lazy(() => import('./field/MyRosterPage'));
const MyKioskPage = lazy(() => import('./field/MyKioskPage'));
const KiosksPage = lazy(() => import('./field/KiosksPage'));
const LocationsPage = lazy(() => import('./field/LocationsPage'));
const PeopleDirectoryPage = lazy(() => import('./people/PeopleDirectoryPage'));
const OrgChartPage = lazy(() => import('./people/OrgChartPage'));
const OrgStructurePage = lazy(() => import('./people/OrgStructurePage'));
const ChangeRequestsPage = lazy(() => import('./people/ChangeRequestsPage'));
const MyDocumentsPage = lazy(() => import('./documents/MyDocumentsPage'));
const DocumentsAdminPage = lazy(() => import('./documents/DocumentsAdminPage'));
const OnboardingPage = lazy(() => import('./lifecycle/OnboardingPage'));
const ExitsPage = lazy(() => import('./lifecycle/ExitsPage'));
const MyClaimsPage = lazy(() => import('./expenses/MyClaimsPage'));
const MyTravelPage = lazy(() => import('./expenses/MyTravelPage'));
const ClaimsReviewPage = lazy(() => import('./expenses/ClaimsReviewPage'));
const TravelReviewPage = lazy(() => import('./expenses/TravelReviewPage'));
const ExpensePoliciesPage = lazy(() => import('./expenses/ExpensePoliciesPage'));
const MyGoalsPage = lazy(() => import('./performance/MyGoalsPage'));
const MyReviewsPage = lazy(() => import('./performance/MyReviewsPage'));
const OneOnOnesPage = lazy(() => import('./performance/OneOnOnesPage'));
const MyLearningPage = lazy(() => import('./performance/MyLearningPage'));
const FeedbackPage = lazy(() => import('./performance/FeedbackPage'));
const TeamGoalsPage = lazy(() => import('./performance/TeamGoalsPage'));
const ReviewCyclesPage = lazy(() => import('./performance/ReviewCyclesPage'));
const LearningAdminPage = lazy(() => import('./performance/LearningAdminPage'));
const JobsPage = lazy(() => import('./hiring/JobsPage'));
const PipelinePage = lazy(() => import('./hiring/PipelinePage'));
const InterviewsPage = lazy(() => import('./hiring/InterviewsPage'));
const RequisitionsPage = lazy(() => import('./hiring/RequisitionsPage'));
const EngageFeedPage = lazy(() => import('./engage/EngageFeedPage'));
const SurveysPage = lazy(() => import('./engage/SurveysPage'));
const AttendanceIssuesPage = lazy(() => import('./attendance-tools/AttendanceIssuesPage'));
const LatePenaltiesPage = lazy(() => import('./attendance-tools/LatePenaltiesPage'));
const AttendanceUploadPage = lazy(() => import('./attendance-tools/AttendanceUploadPage'));
const SuggestionsPage = lazy(() => import('./engage/SuggestionsPage'));
const FeaturesPage = lazy(() => import('./hr/FeaturesPage'));
const ReportsPage = lazy(() => import('./hr/ReportsPage'));
const ReportBuilderPage = lazy(() => import('./hr/ReportBuilderPage'));

export function ReportsHub() {
  return (
    <Hub
      title="Reports"
      tabs={[
        { path: '/reports', label: 'Catalogue', element: <ReportsPage /> },
        { path: '/reports/builder', label: 'Build a report', element: <ReportBuilderPage /> },
      ]}
    />
  );
}

export function AttendanceHub() {
  return (
    <Hub
      title="Attendance"
      tabs={[
        { path: '/attendance', label: 'Today', element: <TeamTodayPage /> },
        { path: '/attendance/from-home', label: 'Work from home', element: <RemoteCheckinsPage /> },
        { path: '/attendance/exceptions', label: 'Exceptions', element: <ExceptionsPage />, roles: AREA.attendance },
        { path: '/attendance/overtime', label: 'Overtime', element: <OtSlipsPage />, roles: AREA.attendance },
        { path: '/attendance/timesheets', label: 'Timesheets', element: <TimesheetsReviewPage />, feature: 'time' },
        { path: '/attendance/rosters', label: 'Rosters', element: <RostersPage /> },
        { path: '/attendance/visits', label: 'Field visits', element: <FieldVisitsPage />, feature: 'field' },
        { path: '/attendance/projects', label: 'Projects', element: <ProjectsPage />, roles: AREA.attendance, feature: 'time' },
        { path: '/attendance/kiosks', label: 'Kiosks', element: <KiosksPage />, roles: AREA.attendance, feature: 'field' },
        { path: '/attendance/issues', label: 'Issues', element: <AttendanceIssuesPage /> },
        { path: '/attendance/penalties', label: 'Late penalties', element: <LatePenaltiesPage />, roles: AREA.attendance },
        { path: '/attendance/upload', label: 'Upload', element: <AttendanceUploadPage />, roles: AREA.attendance },
        { path: '/attendance/fix', label: 'Fix a punch', element: <FixPunchPage /> },
      ]}
    />
  );
}

export function LeaveHub() {
  return (
    <Hub
      title="Leave"
      tabs={[
        { path: '/leave', label: 'Calendar', element: <TeamCalendarPage /> },
        { path: '/leave/balances', label: 'Balances', element: <LeaveAdjustPage />, roles: AREA.attendance },
        { path: '/leave/policies', label: 'Policies', element: <LeaveTypesPage />, roles: AREA.attendance },
        { path: '/leave/history', label: 'Request history', element: <RequestsPage />, roles: AREA.attendance },
      ]}
    />
  );
}

export function PayrollHub() {
  return (
    <Hub
      title="Payroll"
      tabs={[
        { path: '/payroll', label: 'Pay runs', element: <PayrollHubPage /> },
        { path: '/payroll/structures', label: 'Salary structures', element: <StructuresPage /> },
        { path: '/payroll/assignments', label: 'Employee salaries', element: <AssignmentsPage /> },
        { path: '/payroll/declarations', label: 'Tax declarations', element: <DeclarationsPage /> },
        { path: '/payroll/loans', label: 'Loans & advances', element: <LoansPage /> },
        { path: '/payroll/reports', label: 'Reports', element: <PayrollReportsPage /> },
        { path: '/payroll/optimum-cit', label: 'Optimum CIT', element: <OptimumCitPage /> },
        { path: '/payroll/held-pay', label: 'Held pay', element: <HeldPayPage /> },
        { path: '/payroll/opening-balances', label: 'Opening balances', element: <OpeningBalancesPage /> },
        { path: '/payroll/rules', label: 'Tax & SSF rules', element: <RulesPage /> },
      ]}
    />
  );
}

export function SettingsHub() {
  return (
    <Hub
      title="Settings"
      tabs={[
        { path: '/settings', label: 'Company & rules', element: <SettingsPage /> },
        { path: '/settings/shifts', label: 'Shifts', element: <ShiftsPage /> },
        { path: '/settings/holidays', label: 'Holidays', element: <HolidaysPage /> },
        { path: '/settings/devices', label: 'Devices', element: <DevicesPage /> },
        { path: '/settings/locations', label: 'Locations', element: <LocationsPage /> },
        { path: '/settings/users', label: 'Users & roles', element: <UsersPage /> },
        { path: '/settings/approvals', label: 'Approval flows', element: <ApprovalFlowsPage /> },
        { path: '/settings/features', label: 'Features', element: <FeaturesPage /> },
        { path: '/settings/setup', label: 'Setup checklist', element: <SetupPage /> },
      ]}
    />
  );
}

export function ProfileHub() {
  const { user } = useAuth();
  return (
    <Hub
      title="Profile"
      tabs={[
        { path: '/me/profile', label: 'Profile', element: <ProfilePage /> },
        {
          path: '/me/profile/documents',
          label: 'Documents & letters',
          element: <MyDocumentsPage />,
          show: !!user?.employeeId,
          feature: 'documents',
        },
        { path: '/me/profile/security', label: 'Sign-in security', element: <SecurityPage /> },
        { path: '/me/profile/conduct', label: 'Conduct', element: <MyConductPage />, show: !!user?.employeeId },
        { path: '/me/profile/privacy', label: 'Privacy & my data', element: <MyDataPage />, show: !!user?.employeeId },
      ]}
    />
  );
}

export function MyAttendanceHub() {
  return (
    <Hub
      title="My attendance"
      tabs={[
        { path: '/me/attendance', label: 'Attendance', element: <MyAttendancePage /> },
        { path: '/me/attendance/roster', label: 'My roster', element: <MyRosterPage /> },
        { path: '/me/attendance/visits', label: 'Visits & breaks', element: <MyFieldVisitsPage />, feature: 'field' },
        { path: '/me/attendance/kiosk', label: 'Kiosk badge', element: <MyKioskPage />, feature: 'field' },
        { path: '/me/attendance/devices', label: 'Clock-in devices', element: <MyDevicesPage /> },
      ]}
    />
  );
}

export function MyPayHub() {
  return (
    <Hub
      title="Payslips"
      tabs={[
        { path: '/me/payslips', label: 'Payslips', element: <PayslipsPage /> },
        { path: '/me/payslips/tax', label: 'Tax savings', element: <MyTaxPage /> },
      ]}
    />
  );
}

export function MyClaimsHub() {
  return (
    <Hub
      title="Claims & travel"
      tabs={[
        { path: '/me/claims', label: 'Expense claims', element: <MyClaimsPage />, feature: 'expenses' },
        { path: '/me/claims/travel', label: 'Travel', element: <MyTravelPage />, feature: 'expenses' },
      ]}
    />
  );
}

export function GrowthHub() {
  return (
    <Hub
      title="Growth"
      tabs={[
        { path: '/me/growth', label: 'Goals', element: <MyGoalsPage />, feature: 'performance' },
        { path: '/me/growth/reviews', label: 'Reviews', element: <MyReviewsPage />, feature: 'performance' },
        { path: '/me/growth/one-on-ones', label: '1-on-1s', element: <OneOnOnesPage />, feature: 'performance' },
        { path: '/me/growth/feedback', label: 'Feedback', element: <FeedbackPage />, feature: 'performance' },
        { path: '/me/growth/learning', label: 'Learning', element: <MyLearningPage />, feature: 'learning' },
      ]}
    />
  );
}

export function TimesheetHub() {
  return <Hub title="Timesheet" tabs={[{ path: '/me/timesheet', label: 'Timesheet', element: <MyTimesheetPage />, feature: 'time' }]} />;
}

export function PeopleHub() {
  return (
    <Hub
      title="People"
      tabs={[
        { path: '/people', label: 'Directory', element: <PeopleDirectoryPage /> },
        { path: '/people/org-chart', label: 'Org chart', element: <OrgChartPage /> },
      ]}
    />
  );
}

export function EngageHub() {
  return (
    <Hub
      title="Engage"
      tabs={[
        { path: '/engage', label: 'Feed', element: <EngageFeedPage /> },
        { path: '/engage/surveys', label: 'Surveys & polls', element: <SurveysPage />, feature: 'surveys' },
        { path: '/engage/suggestions', label: 'Suggestions', element: <SuggestionsPage />, roles: ['hr'], feature: 'suggestions' },
      ]}
    />
  );
}

/** Tab paths of the Employees hub; App registers each one explicitly so they win over /employees/:id. */
export const EMPLOYEES_HUB_PATHS = [
  '/employees',
  '/employees/structure',
  '/employees/onboarding',
  '/employees/exits',
  '/employees/documents',
  '/employees/requests',
];

export function EmployeesHub() {
  return (
    <Hub
      title="Employees"
      tabs={[
        { path: '/employees', label: 'Directory', element: <EmployeesPage /> },
        { path: '/employees/structure', label: 'Org structure', element: <OrgStructurePage /> },
        { path: '/employees/onboarding', label: 'Onboarding', element: <OnboardingPage />, feature: 'lifecycle' },
        { path: '/employees/exits', label: 'Exits', element: <ExitsPage />, feature: 'lifecycle' },
        { path: '/employees/documents', label: 'Documents & letters', element: <DocumentsAdminPage />, feature: 'documents' },
        { path: '/employees/requests', label: 'Change requests', element: <ChangeRequestsPage /> },
      ]}
    />
  );
}

export function ExpensesHub() {
  return (
    <Hub
      title="Expenses & travel"
      tabs={[
        { path: '/expenses', label: 'Claims', element: <ClaimsReviewPage />, feature: 'expenses' },
        { path: '/expenses/travel', label: 'Travel', element: <TravelReviewPage />, feature: 'expenses' },
        { path: '/expenses/policies', label: 'Policies', element: <ExpensePoliciesPage />, roles: ['hr'], feature: 'expenses' },
      ]}
    />
  );
}

export function PerformanceHub() {
  return (
    <Hub
      title="Performance"
      tabs={[
        { path: '/performance', label: 'Goals', element: <TeamGoalsPage />, feature: 'performance' },
        { path: '/performance/reviews', label: 'Review cycles', element: <ReviewCyclesPage />, feature: 'performance' },
        { path: '/performance/learning', label: 'Learning', element: <LearningAdminPage />, feature: 'learning' },
      ]}
    />
  );
}

export function HiringHub() {
  return (
    <Hub
      title="Hiring"
      tabs={[
        { path: '/hiring', label: 'Jobs', element: <JobsPage />, feature: 'hiring' },
        { path: '/hiring/pipeline', label: 'Candidates', element: <PipelinePage />, feature: 'hiring' },
        { path: '/hiring/interviews', label: 'Interviews', element: <InterviewsPage />, feature: 'hiring' },
        { path: '/hiring/requisitions', label: 'Requisitions', element: <RequisitionsPage />, feature: 'hiring' },
      ]}
    />
  );
}

export function HelpHub() {
  return (
    <Hub
      title="Help desk"
      tabs={[
        { path: '/me/help', label: 'Questions', element: <MyTicketsPage /> },
        { path: '/me/help/grievances', label: 'Grievances', element: <MyGrievancesPage /> },
      ]}
    />
  );
}

export function RelationsHub() {
  return (
    <Hub
      title="Employee relations"
      tabs={[
        { path: '/relations', label: 'Grievances', element: <GrievancesAdminPage /> },
        { path: '/relations/disciplinary', label: 'Disciplinary', element: <DisciplinaryPage /> },
        { path: '/relations/milestones', label: 'Milestones', element: <MilestonesPage /> },
        { path: '/relations/assignments', label: 'Acting & deputation', element: <TempAssignmentsPage /> },
        { path: '/relations/kye', label: 'Know your employee', element: <KyePage /> },
        { path: '/relations/devices', label: 'Clock-in devices', element: <RegisteredDevicesPage /> },
      ]}
    />
  );
}
