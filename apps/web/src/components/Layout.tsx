import { ActionIcon, AppShell, Avatar, Badge, Box, Burger, Center, Group, Indicator, Loader, Menu, NavLink, ScrollArea, Stack, Text, Tooltip, UnstyledButton, useMantineColorScheme } from '@mantine/core';
import { useDisclosure, useHotkeys, useMediaQuery } from '@mantine/hooks';
import { Spotlight, spotlight, type SpotlightActionData } from '@mantine/spotlight';
import {
  IconAdjustmentsHorizontal, IconBell, IconBuildingBank, IconCalendarEvent, IconCalendarStats, IconChecklist,
  IconClockEdit, IconClockHour4, IconCoin, IconFileInvoice, IconFingerprint, IconGavel, IconHome,
  IconLayoutDashboard, IconLogout, IconMoon, IconReceipt2, IconReportAnalytics, IconSearch, IconSelector, IconSun,
  IconUserCircle, IconUserShield, IconUsers, IconAlertTriangle, IconListCheck, IconBeach, IconCash, IconFileCertificate,
  IconAdjustments, IconSettings, type Icon,
} from '@tabler/icons-react';
import { Suspense, useEffect, useMemo, useState } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../auth/AuthProvider';
import { notifications as notifApi, workflow as workflowApi } from '../api';
import { Brand } from './Brand';
import type { Role } from '../api/types';

interface NavItem { to: string; label: string; icon: Icon; roles?: Role[]; employeeOnly?: boolean; keywords?: string[]; match?: string[]; badge?: 'approvals' }
interface NavGroup { id: string; title: string; items: NavItem[]; roles?: Role[] }

const NAV: NavGroup[] = [
  { id: 'me', title: 'My work', items: [
    { to: '/', label: 'Home', icon: IconHome, employeeOnly: true, keywords: ['today', 'clock in', 'clock out', 'punch'] },
    { to: '/me/attendance', label: 'My attendance', icon: IconCalendarStats, employeeOnly: true },
    { to: '/me/leave', label: 'Time off', icon: IconBeach, employeeOnly: true, keywords: ['leave', 'holiday', 'vacation', 'apply'] },
    { to: '/me/payslips', label: 'Payslips', icon: IconReceipt2, employeeOnly: true, keywords: ['salary', 'pay'] },
  ] },
  { id: 'manage', title: 'Manage', roles: ['manager', 'hr'], items: [
    { to: '/overview', label: 'Overview', icon: IconLayoutDashboard, roles: ['hr'], keywords: ['dashboard'] },
    { to: '/approvals', label: 'Approvals', icon: IconListCheck, roles: ['manager', 'hr'], keywords: ['approve', 'requests', 'inbox'], badge: 'approvals' },
    { to: '/employees', label: 'Employees', icon: IconUsers, roles: ['hr'], keywords: ['staff', 'people', 'add employee'] },
    { to: '/attendance', label: 'Attendance', icon: IconClockHour4, roles: ['manager', 'hr'], keywords: ["who's in", 'today', 'exceptions', 'overtime', 'fix punch'] },
    { to: '/leave', label: 'Leave', icon: IconCalendarEvent, roles: ['manager', 'hr'], keywords: ['calendar', 'balances', 'policies', 'time off'] },
    { to: '/payroll', label: 'Payroll', icon: IconCash, roles: ['hr'], keywords: ['pay run', 'salary', 'tax', 'loans', 'ssf'] },
    { to: '/reports', label: 'Reports', icon: IconReportAnalytics, roles: ['hr'], keywords: ['export', 'csv', 'register'] },
  ] },
];

const SETTINGS: NavItem = { to: '/settings', label: 'Settings', icon: IconSettings, roles: ['hr'], keywords: ['shifts', 'holidays', 'devices', 'users', 'rules', 'setup'] };

/** Extra jump targets for the command palette: tabs inside pages. */
const JUMPS: Array<{ to: string; label: string; group: string; roles?: Role[]; icon: Icon }> = [
  { to: '/attendance/exceptions', label: 'Attendance exceptions', group: 'Attendance', roles: ['hr'], icon: IconAlertTriangle },
  { to: '/attendance/overtime', label: 'Overtime review', group: 'Attendance', roles: ['hr'], icon: IconClockHour4 },
  { to: '/attendance/fix', label: 'Fix a punch', group: 'Attendance', roles: ['manager', 'hr'], icon: IconClockEdit },
  { to: '/leave/history', label: 'Request history', group: 'Leave', roles: ['hr'], icon: IconListCheck },
  { to: '/leave/balances', label: 'Leave balances', group: 'Leave', roles: ['hr'], icon: IconAdjustments },
  { to: '/leave/policies', label: 'Leave policies', group: 'Leave', roles: ['hr'], icon: IconAdjustmentsHorizontal },
  { to: '/payroll/structures', label: 'Salary structures', group: 'Payroll', roles: ['hr'], icon: IconCoin },
  { to: '/payroll/assignments', label: 'Employee salaries', group: 'Payroll', roles: ['hr'], icon: IconFileInvoice },
  { to: '/payroll/declarations', label: 'Tax declarations', group: 'Payroll', roles: ['hr'], icon: IconFileCertificate },
  { to: '/payroll/loans', label: 'Loans & advances', group: 'Payroll', roles: ['hr'], icon: IconBuildingBank },
  { to: '/payroll/rules', label: 'Tax & SSF rules', group: 'Payroll', roles: ['hr'], icon: IconGavel },
  { to: '/settings/shifts', label: 'Shifts', group: 'Settings', roles: ['hr'], icon: IconClockHour4 },
  { to: '/settings/holidays', label: 'Holidays', group: 'Settings', roles: ['hr'], icon: IconCalendarEvent },
  { to: '/settings/devices', label: 'Devices', group: 'Settings', roles: ['hr'], icon: IconFingerprint },
  { to: '/settings/users', label: 'Users & roles', group: 'Settings', roles: ['hr'], icon: IconUserShield },
  { to: '/settings/setup', label: 'Setup checklist', group: 'Settings', roles: ['hr'], icon: IconChecklist },
];

const ACCOUNT: NavItem[] = [
  { to: '/me/notifications', label: 'Notifications', icon: IconBell },
  { to: '/me/profile', label: 'Profile & privacy', icon: IconUserCircle },
];

function isActive(path: string, to: string): boolean {
  if (to === '/') return path === '/';
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
  const [pendingApprovals, setPendingApprovals] = useState(0);

  useEffect(() => { close(); }, [loc.pathname, close]);
  useEffect(() => {
    let alive = true;
    const load = async () => { try { const r = await notifApi.list({ limit: 1 }); if (alive) setUnread(r.unread); } catch { /* ignore */ } };
    void load();
    const id = setInterval(load, 60_000);
    if (hasRole('manager', 'hr')) workflowApi.inbox().then((r) => { if (alive) setPendingApprovals(r.items.length); }).catch(() => undefined);
    return () => { alive = false; clearInterval(id); };
  }, [loc.pathname]);

  const visible = (i: NavItem) => (!i.roles || hasRole(...i.roles)) && (!i.employeeOnly || !!user?.employeeId);
  const groups = NAV.filter((g) => !g.roles || hasRole(...g.roles)).map((g) => ({ ...g, items: g.items.filter(visible) })).filter((g) => g.items.length > 0);
  const account = ACCOUNT.filter(visible);
  const settings = visible(SETTINGS) ? SETTINGS : null;
  const jumps = JUMPS.filter((j) => !j.roles || hasRole(...j.roles));

  const actions: SpotlightActionData[] = useMemo(() => [
    ...groups.flatMap((g) => g.items.map((i) => ({ ...i, group: g.title }))),
    ...(settings ? [{ ...settings, group: 'Settings' }] : []),
    ...jumps.map((j) => ({ ...j, keywords: [] as string[] })),
    ...account.map((i) => ({ ...i, group: 'Account' })),
  ].map((i) => {
    const Ico = i.icon;
    return { id: i.to, label: i.label, description: i.group, keywords: i.keywords ?? [], onClick: () => nav(i.to), leftSection: <Ico size={18} stroke={1.6} /> };
  }), [groups.length, account.length, jumps.length, user?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useHotkeys([['mod+K', () => spotlight.open()]]);

  const initials = (user?.name ?? '?').split(/\s+/).map((p) => p[0]).slice(0, 2).join('').toUpperCase();

  const link = (i: NavItem) => {
    const Ico = i.icon;
    return (
      <NavLink key={i.to} component={Link} to={i.to} className="hj-nav-link" label={i.label} active={isActive(loc.pathname, i.to)}
        leftSection={<Ico size={17} stroke={1.6} />}
        rightSection={i.badge === 'approvals' && pendingApprovals > 0 ? <Badge size="sm" variant="light" radius="xl">{pendingApprovals > 99 ? '99+' : pendingApprovals}</Badge> : null} />
    );
  };

  return (
    <AppShell header={{ height: 52, collapsed: !mobile }} navbar={{ width: 252, breakpoint: 'sm', collapsed: { mobile: !opened } }} padding={0}>
      <AppShell.Header withBorder>
        <Group h="100%" px="sm" justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} size="sm" aria-label="Open menu" />
            <Brand height={18} />
          </Group>
          <ActionIcon variant="subtle" color="gray" onClick={() => spotlight.open()} aria-label="Search"><IconSearch size={18} /></ActionIcon>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar className="hj-nav" p={10}>
        <AppShell.Section>
          <Group justify="space-between" wrap="nowrap" px={4} pt={2} pb={10}>
            <Link to="/" aria-label="Home" style={{ display: 'inline-flex', minWidth: 0, textDecoration: 'none' }}><Brand height={20} /></Link>
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
          {groups.map((g) => (
            <Box key={g.id}>
              {groups.length > 1 ? <div className="hj-nav-section">{g.title}</div> : <Box h={8} />}
              <Stack gap={1}>{g.items.map(link)}</Stack>
            </Box>
          ))}
        </AppShell.Section>

        {settings ? <AppShell.Section pb={6}>{link(settings)}</AppShell.Section> : null}
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

