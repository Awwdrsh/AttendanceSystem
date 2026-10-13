import { ActionIcon, AppShell, Avatar, Badge, Box, Burger, Center, Group, Indicator, Loader, Menu, NavLink, ScrollArea, Stack, Text, Tooltip, UnstyledButton, useMantineColorScheme } from '@mantine/core';
import { useDisclosure, useHotkeys, useMediaQuery } from '@mantine/hooks';
import { Spotlight, spotlight, type SpotlightActionData } from '@mantine/spotlight';
import {
  IconAdjustmentsHorizontal, IconBell, IconBuildingBank, IconCalendarEvent, IconCalendarStats, IconCalendarTime, IconChecklist,
  IconChevronDown, IconClockEdit, IconClockHour4, IconCoin, IconDeviceDesktopAnalytics, IconFileInvoice, IconFingerprint, IconGavel, IconHome,
  IconLayoutDashboard, IconLogout, IconMapPin, IconMoon, IconReceipt2, IconReportAnalytics, IconSearch, IconSelector, IconShieldLock, IconSun,
  IconUserCircle, IconUserShield, IconUsers, IconUsersGroup, IconAlertTriangle, IconInbox, IconListCheck, IconBeach, IconCash, IconFileCertificate,
  IconAdjustments, IconHourglass, type Icon,
} from '@tabler/icons-react';
import { Suspense, useEffect, useMemo, useState } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import { notifications as notifApi, settings as settingsApi } from '../api';
import type { Role } from '../api/types';

interface NavItem { to: string; label: string; icon: Icon; roles?: Role[]; employeeOnly?: boolean; keywords?: string[] }
interface NavGroup { id: string; title: string; items: NavItem[]; roles?: Role[]; collapsible?: boolean }

const NAV: NavGroup[] = [
  { id: 'me', title: 'You', items: [
    { to: '/', label: 'Home', icon: IconHome, keywords: ['today', 'clock in', 'punch'] },
    { to: '/me/attendance', label: 'My attendance', icon: IconCalendarStats, employeeOnly: true },
    { to: '/me/leave', label: 'Time off', icon: IconBeach, employeeOnly: true, keywords: ['leave', 'holiday', 'vacation'] },
    { to: '/me/overtime', label: 'Overtime', icon: IconHourglass, employeeOnly: true },
    { to: '/me/payslips', label: 'Payslips', icon: IconReceipt2, employeeOnly: true, keywords: ['salary', 'pay'] },
  ] },
  { id: 'team', title: 'Team', roles: ['manager', 'hr'], items: [
    { to: '/manager/approvals', label: 'Approvals', icon: IconListCheck, keywords: ['approve', 'requests'] },
    { to: '/manager/team', label: "Who's in", icon: IconUsersGroup, keywords: ['team today', 'present'] },
    { to: '/manager/calendar', label: 'Team calendar', icon: IconCalendarEvent },
    { to: '/manager/roster', label: 'Roster', icon: IconCalendarTime, keywords: ['schedule', 'shifts'] },
    { to: '/manager/fix-punch', label: 'Fix a punch', icon: IconClockEdit, keywords: ['correct', 'missing'] },
  ] },
  { id: 'people', title: 'People', roles: ['hr'], items: [
    { to: '/hr/dashboard', label: 'Overview', icon: IconLayoutDashboard, keywords: ['dashboard'] },
    { to: '/hr/employees', label: 'Employees', icon: IconUsers, keywords: ['staff', 'people'] },
    { to: '/hr/requests', label: 'Requests', icon: IconInbox },
    { to: '/hr/exceptions', label: 'Exceptions', icon: IconAlertTriangle, keywords: ['missing punch', 'absent'] },
    { to: '/hr/ot-slips', label: 'Overtime review', icon: IconClockHour4, keywords: ['ot slips'] },
    { to: '/hr/reports', label: 'Reports', icon: IconReportAnalytics, keywords: ['export', 'csv'] },
  ] },
  { id: 'payroll', title: 'Payroll', roles: ['finance', 'hr'], items: [
    { to: '/payroll', label: 'Pay runs', icon: IconCash, keywords: ['run payroll', 'salary'] },
    { to: '/payroll/structures', label: 'Salary structures', icon: IconCoin },
    { to: '/payroll/assignments', label: 'Salary assignments', icon: IconFileInvoice },
    { to: '/payroll/declarations', label: 'Tax declarations', icon: IconFileCertificate },
    { to: '/payroll/loans', label: 'Loans & advances', icon: IconBuildingBank },
    { to: '/payroll/rules', label: 'Statutory rules', icon: IconGavel, keywords: ['tax', 'ssf'] },
  ] },
  { id: 'settings', title: 'Workspace', roles: ['hr'], collapsible: true, items: [
    { to: '/hr/setup', label: 'Setup checklist', icon: IconChecklist },
    { to: '/hr/devices', label: 'Devices', icon: IconFingerprint, keywords: ['terminal', 'biometric'] },
    { to: '/hr/shifts', label: 'Shifts', icon: IconClockHour4 },
    { to: '/hr/holidays', label: 'Holidays', icon: IconCalendarEvent },
    { to: '/hr/locations', label: 'Locations', icon: IconMapPin, keywords: ['geofence'] },
    { to: '/hr/leave-types', label: 'Leave policies', icon: IconAdjustmentsHorizontal, keywords: ['leave types'] },
    { to: '/hr/leave-adjust', label: 'Leave adjustments', icon: IconAdjustments },
    { to: '/hr/settings', label: 'Attendance rules', icon: IconDeviceDesktopAnalytics, keywords: ['settings', 'grace', 'late'] },
    { to: '/hr/users', label: 'Users & roles', icon: IconUserShield, keywords: ['permissions', 'access'] },
  ] },
];

const ACCOUNT: NavItem[] = [
  { to: '/me/notifications', label: 'Notifications', icon: IconBell },
  { to: '/me/profile', label: 'Profile', icon: IconUserCircle },
  { to: '/me/data', label: 'Privacy & my data', icon: IconShieldLock, employeeOnly: true },
];

function isActive(path: string, to: string): boolean {
  if (to === '/') return path === '/';
  if (to === '/payroll') return path === '/payroll' || path.startsWith('/payroll/runs');
  return path === to || path.startsWith(`${to}/`);
}

export function Layout() {
  const [opened, { toggle, close }] = useDisclosure();
  const mobile = useMediaQuery('(max-width: 48em)', false);
  const { user, logout, hasRole } = useAuth();
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const loc = useLocation();
  const nav = useNavigate();
  const [unread, setUnread] = useState(0);
  const [company, setCompany] = useState<string | null>(null);
  const [workspaceOpen, setWorkspaceOpen] = useState(() => localStorage.getItem('hajiri.nav.workspace') === 'open');

  useEffect(() => { close(); }, [loc.pathname, close]);
  useEffect(() => {
    let alive = true;
    const load = async () => { try { const r = await notifApi.list({ limit: 1 }); if (alive) setUnread(r.unread); } catch { /* ignore */ } };
    void load();
    const id = setInterval(load, 60_000);
    settingsApi.company().then((c) => { if (alive) setCompany(c.company.nameEn); }).catch(() => undefined);
    return () => { alive = false; clearInterval(id); };
  }, [loc.pathname]);

  const visible = (i: NavItem) => (!i.roles || hasRole(...i.roles)) && (!i.employeeOnly || !!user?.employeeId);
  const groups = NAV.filter((g) => !g.roles || hasRole(...g.roles)).map((g) => ({ ...g, items: g.items.filter(visible) })).filter((g) => g.items.length > 0);
  const account = ACCOUNT.filter(visible);
  const workspaceHasActive = groups.find((g) => g.id === 'settings')?.items.some((i) => isActive(loc.pathname, i.to)) ?? false;

  const actions: SpotlightActionData[] = useMemo(() => [...groups.flatMap((g) => g.items.map((i) => ({ ...i, group: g.title }))), ...account.map((i) => ({ ...i, group: 'Account' }))].map((i) => {
    const Ico = i.icon;
    return { id: i.to, label: i.label, description: i.group, keywords: i.keywords ?? [], onClick: () => nav(i.to), leftSection: <Ico size={18} stroke={1.6} /> };
  }), [groups.length, account.length, user?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useHotkeys([['mod+K', () => spotlight.open()]]);

  const initials = (user?.name ?? '?').split(/\s+/).map((p) => p[0]).slice(0, 2).join('').toUpperCase();

  const link = (i: NavItem) => {
    const Ico = i.icon;
    return (
      <NavLink key={i.to} component={Link} to={i.to} className="hj-nav-link" label={i.label} active={isActive(loc.pathname, i.to)}
        leftSection={<Ico size={17} stroke={1.6} />}
        rightSection={i.to === '/me/notifications' && unread > 0 ? <Badge size="xs" circle variant="filled">{unread > 99 ? '99+' : unread}</Badge> : null} />
    );
  };

  return (
    <AppShell header={{ height: 52, collapsed: !mobile }} navbar={{ width: 252, breakpoint: 'sm', collapsed: { mobile: !opened } }} padding={0}>
      <AppShell.Header withBorder>
        <Group h="100%" px="sm" justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} size="sm" aria-label="Open menu" />
            <Text fw={600}>{company ?? 'Hajiri'}</Text>
          </Group>
          <ActionIcon variant="subtle" color="gray" onClick={() => spotlight.open()} aria-label="Search"><IconSearch size={18} /></ActionIcon>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar className="hj-nav" p={10}>
        <AppShell.Section>
          <Group justify="space-between" wrap="nowrap" px={4} pt={2} pb={10}>
            <Group gap={10} wrap="nowrap" style={{ minWidth: 0 }}>
              <Center w={28} h={28} style={{ borderRadius: 7, background: 'var(--mantine-color-brand-6)', color: 'white', flex: 'none' }}><IconClockHour4 size={17} stroke={2} /></Center>
              <div style={{ minWidth: 0 }}>
                <Text fw={600} size="sm" truncate lh={1.2}>{company ?? 'Hajiri'}</Text>
                <Text size="xs" c="dimmed" lh={1.2}>Hajiri</Text>
              </div>
            </Group>
            <Tooltip label="Notifications">
              <Indicator disabled={unread === 0} size={8} offset={6} color="red">
                <ActionIcon variant="subtle" color="gray" component={Link} to="/me/notifications" aria-label="Notifications"><IconBell size={18} stroke={1.6} /></ActionIcon>
              </Indicator>
            </Tooltip>
          </Group>
          <UnstyledButton className="hj-search" onClick={() => spotlight.open()} aria-label="Search pages">
            <IconSearch size={15} stroke={1.8} /> Search or jump to
            <span className="hj-kbd">⌘K</span>
          </UnstyledButton>
        </AppShell.Section>

        <AppShell.Section grow component={ScrollArea} type="scroll" mt={6} mx={-4} px={4}>
          {groups.map((g) => {
            const collapsed = g.collapsible && !workspaceOpen && !workspaceHasActive;
            return (
              <Box key={g.id}>
                {g.collapsible ? (
                  <UnstyledButton className="hj-nav-section" style={{ display: 'flex', alignItems: 'center', gap: 4, width: '100%' }}
                    onClick={() => { const n = !workspaceOpen; setWorkspaceOpen(n); localStorage.setItem('hajiri.nav.workspace', n ? 'open' : 'closed'); }} aria-expanded={!collapsed}>
                    {g.title}<IconChevronDown size={13} style={{ transform: collapsed ? 'rotate(-90deg)' : undefined, transition: 'transform 120ms' }} />
                  </UnstyledButton>
                ) : <div className="hj-nav-section">{g.title}</div>}
                {collapsed ? null : <Stack gap={1}>{g.items.map(link)}</Stack>}
              </Box>
            );
          })}
        </AppShell.Section>

        <AppShell.Section pt={8} style={{ borderTop: '1px solid var(--hj-line)' }}>
          <Menu position="top-start" width={232} shadow="md" offset={6}>
            <Menu.Target>
              <UnstyledButton w="100%" p={6} style={{ borderRadius: 8 }} aria-label="Account menu">
                <Group gap={10} wrap="nowrap">
                  <Avatar size={30} radius="xl" color="brand" variant="light">{initials}</Avatar>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <Text size="sm" fw={500} truncate lh={1.25}>{user?.name}</Text>
                    <Text size="xs" c="dimmed" truncate lh={1.25}>{user?.email}</Text>
                  </div>
                  <IconSelector size={15} color="var(--hj-muted)" />
                </Group>
              </UnstyledButton>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Signed in as {user?.roles.join(', ')}</Menu.Label>
              {account.map((i) => { const Ico = i.icon; return <Menu.Item key={i.to} component={Link} to={i.to} leftSection={<Ico size={16} stroke={1.6} />}>{i.label}</Menu.Item>; })}
              <Menu.Item leftSection={colorScheme === 'dark' ? <IconSun size={16} stroke={1.6} /> : <IconMoon size={16} stroke={1.6} />}
                onClick={() => setColorScheme(colorScheme === 'dark' ? 'light' : 'dark')}>{colorScheme === 'dark' ? 'Light mode' : 'Dark mode'}</Menu.Item>
              <Menu.Divider />
              <Menu.Item color="red" leftSection={<IconLogout size={16} stroke={1.6} />} onClick={async () => { await logout(); nav('/login'); }}>Sign out</Menu.Item>
            </Menu.Dropdown>
          </Menu>
        </AppShell.Section>
      </AppShell.Navbar>

      <AppShell.Main className="hj-main">
        <Suspense fallback={<Center h="60vh"><Loader size="sm" /></Center>}>
          <div className="hj-page"><Outlet /></div>
        </Suspense>
      </AppShell.Main>

      <Spotlight actions={actions} nothingFound="No matching page" highlightQuery shortcut={null}
        searchProps={{ leftSection: <IconSearch size={18} stroke={1.6} />, placeholder: 'Search pages and actions' }} limit={12} />
    </AppShell>
  );
}

