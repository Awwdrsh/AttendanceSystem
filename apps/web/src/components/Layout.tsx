import { ActionIcon, AppShell, Avatar, Badge, Burger, Group, Indicator, Menu, NavLink, ScrollArea, SegmentedControl, Text, Tooltip, useMantineColorScheme } from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { Suspense, useEffect, useState } from 'react';
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Center, Loader } from '@mantine/core';
import { useAuth } from '../auth/AuthProvider';
import { useI18n, type Key } from '../i18n';
import { notifications as notifApi, settings as settingsApi } from '../api';
import type { Role } from '../api/types';
import { fmtNum } from '../lib/format';

interface NavItem { to: string; label: Key; roles?: Role[]; employeeOnly?: boolean }
interface NavGroup { title: Key; items: NavItem[]; roles?: Role[] }

const NAV: NavGroup[] = [
  { title: 'home', items: [
    { to: '/', label: 'today', employeeOnly: true }, { to: '/me/attendance', label: 'myAttendance', employeeOnly: true }, { to: '/me/leave', label: 'leave', employeeOnly: true },
    { to: '/me/overtime', label: 'overtime', employeeOnly: true }, { to: '/me/payslips', label: 'payslips', employeeOnly: true }, { to: '/me/data', label: 'myData', employeeOnly: true },
    { to: '/me/notifications', label: 'notifications' }, { to: '/me/profile', label: 'profile' },
  ] },
  { title: 'team', roles: ['manager', 'hr'], items: [
    { to: '/manager/approvals', label: 'approvals' }, { to: '/manager/team', label: 'teamToday' }, { to: '/manager/calendar', label: 'teamCalendar' },
    { to: '/manager/fix-punch', label: 'fixPunch' }, { to: '/manager/roster', label: 'roster' },
  ] },
  { title: 'employees', roles: ['hr'], items: [
    { to: '/hr/dashboard', label: 'dashboard' }, { to: '/hr/setup', label: 'setup' }, { to: '/hr/employees', label: 'employees' }, { to: '/hr/devices', label: 'devices' },
    { to: '/hr/exceptions', label: 'exceptions' }, { to: '/hr/requests', label: 'requests' }, { to: '/hr/ot-slips', label: 'otSlips' }, { to: '/hr/shifts', label: 'shifts' },
    { to: '/hr/holidays', label: 'holidays' }, { to: '/hr/locations', label: 'locations' }, { to: '/hr/leave-types', label: 'leaveTypes' }, { to: '/hr/leave-adjust', label: 'leaveAdjust' },
    { to: '/hr/settings', label: 'rules' }, { to: '/hr/reports', label: 'reports' }, { to: '/hr/users', label: 'users' },
  ] },
  { title: 'payroll', roles: ['finance', 'hr'], items: [
    { to: '/payroll', label: 'runPayroll' }, { to: '/payroll/structures', label: 'structures' }, { to: '/payroll/assignments', label: 'assignments' },
    { to: '/payroll/declarations', label: 'declarations' }, { to: '/payroll/loans', label: 'loans' }, { to: '/payroll/rules', label: 'statutoryRules' },
  ] },
];

export function Layout() {
  const [opened, { toggle, close }] = useDisclosure();
  const { user, logout, hasRole } = useAuth();
  const { lang, setLang, numerals, setNumerals, t, both } = useI18n();
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  const loc = useLocation();
  const nav = useNavigate();
  const [unread, setUnread] = useState(0);
  const [company, setCompany] = useState<{ name: string; bs: string | null; ad: string } | null>(null);

  useEffect(() => { close(); }, [loc.pathname, close]);
  useEffect(() => {
    let alive = true;
    const load = async () => { try { const r = await notifApi.list({ limit: 1 }); if (alive) setUnread(r.unread); } catch { /* ignore */ } };
    void load();
    const id = setInterval(load, 60_000);
    settingsApi.company().then((c) => { if (alive) setCompany({ name: lang === 'ne' && c.company.nameNe ? c.company.nameNe : c.company.nameEn, bs: c.today.bs, ad: c.today.ad }); }).catch(() => undefined);
    return () => { alive = false; clearInterval(id); };
  }, [lang, loc.pathname]);

  const groups = NAV.filter((g) => !g.roles || hasRole(...g.roles)).map((g) => ({ ...g, items: g.items.filter((i) => (!i.roles || hasRole(...i.roles)) && (!i.employeeOnly || !!user?.employeeId || i.to === '/')) }));

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 240, breakpoint: 'sm', collapsed: { mobile: !opened } }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between" wrap="nowrap">
          <Group gap="sm" wrap="nowrap">
            <Burger opened={opened} onClick={toggle} hiddenFrom="sm" size="sm" aria-label="Menu" />
            <Text fw={700} component={Link} to="/" c="inherit" td="none">{t('appName')}</Text>
            {company ? <Text size="sm" c="dimmed" visibleFrom="xs" truncate maw={220}>{company.name}</Text> : null}
          </Group>
          <Group gap="xs" wrap="nowrap">
            {company ? <Text size="xs" c="dimmed" visibleFrom="md">{company.bs ? fmtNum(company.bs, numerals) : ''} · {company.ad}</Text> : null}
            <SegmentedControl size="xs" value={lang} onChange={(v) => setLang(v as 'en' | 'ne')} data={[{ value: 'ne', label: 'ने' }, { value: 'en', label: 'EN' }]} aria-label={t('language')} />
            <Tooltip label={t('notifications')}>
              <Indicator label={unread > 99 ? '99+' : unread} size={16} disabled={unread === 0} color="red">
                <ActionIcon variant="subtle" component={Link} to="/me/notifications" aria-label={t('notifications')} size="lg">🔔</ActionIcon>
              </Indicator>
            </Tooltip>
            <Menu shadow="md" width={240} position="bottom-end">
              <Menu.Target>
                <ActionIcon variant="subtle" size="lg" aria-label="Account menu"><Avatar size="sm" radius="xl" color="teal">{(user?.name ?? '?').slice(0, 1).toUpperCase()}</Avatar></ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Label>{user?.name}<br /><Text span size="xs" c="dimmed">{user?.email}</Text></Menu.Label>
                <Menu.Label>{user?.roles.map((r) => <Badge key={r} size="xs" variant="light" mr={4}>{r}</Badge>)}</Menu.Label>
                <Menu.Divider />
                <Menu.Item onClick={() => setNumerals(numerals === 'latin' ? 'devanagari' : 'latin')}>{t('numerals')}: {numerals === 'latin' ? '0-9' : '०-९'}</Menu.Item>
                <Menu.Item onClick={() => setColorScheme(colorScheme === 'dark' ? 'light' : 'dark')}>{t('theme')}: {colorScheme === 'dark' ? 'dark' : 'light'}</Menu.Item>
                <Menu.Item component={Link} to="/me/profile">{t('profile')}</Menu.Item>
                <Menu.Divider />
                <Menu.Item color="red" onClick={async () => { await logout(); nav('/login'); }}>{t('logout')}</Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        <ScrollArea type="auto">
          {groups.map((g) => (
            <div key={g.title} style={{ marginBottom: 8 }}>
              <Text size="xs" c="dimmed" fw={600} px="sm" pt="xs" pb={4} tt="uppercase">{both(g.title)}</Text>
              {g.items.map((i) => <NavLink key={i.to} component={Link} to={i.to} label={both(i.label)} active={i.to === '/' ? loc.pathname === '/' : loc.pathname.startsWith(i.to)} />)}
            </div>
          ))}
        </ScrollArea>
      </AppShell.Navbar>
      <AppShell.Main>
        <Suspense fallback={<Center h="50vh"><Loader /></Center>}>
          <Outlet />
        </Suspense>
      </AppShell.Main>
    </AppShell>
  );
}
