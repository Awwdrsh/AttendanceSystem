import { Anchor, Avatar, Button, Grid, Group, Paper, Skeleton, Stack, Text, ThemeIcon } from '@mantine/core';
import { IconAlertTriangle, IconArrowUpRight, IconCash, IconCircleCheck, IconClockHour4, IconFingerprint, IconInbox, IconUsersGroup } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { attendance, dashboard as dashApi, shifts as shiftsApi } from '../../api';
import { ArrivalsRibbon } from '../../components/ArrivalsRibbon';
import { StatStrip, TrendBars } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { liveStatus, StatusBadge } from '../../components/StatusBadge';
import { useAuth } from '../../auth/AuthProvider';
import { bsMonthLabel, fmtDateLong, fmtMoney, fmtTime, greeting, minutesToHm, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const ATTENTION: Record<string, { to: string; icon: typeof IconInbox; action: string }> = {
  device_offline: { to: '/settings/devices', icon: IconFingerprint, action: 'Check device' },
  unmapped_punches: { to: '/settings/devices', icon: IconFingerprint, action: 'Map PINs' },
  ot_unapproved: { to: '/attendance/overtime', icon: IconClockHour4, action: 'Review' },
  leave_overdue: { to: '/approvals', icon: IconInbox, action: 'Open approvals' },
};

export default function DashboardPage() {
  const { user } = useAuth();
  const today = todayNpt();
  const q = useQuery(() => dashApi.executive(), []);
  const team = useQuery(() => attendance.today(today), [today]);
  const sh = useQuery(() => shiftsApi.list(), []);
  const d = q.data;
  const tile = (k: string) => d?.tiles.find((t) => t.key === k)?.value ?? 0;
  const rows = team.data?.rows ?? [];
  const arrivals = rows.map((r) => ({ id: r.employee_id, name: r.name_en, at: r.first_in, late: (r.late_min ?? 0) > 0 }));
  const shift = sh.data?.shifts[0];
  const recent = [...rows].filter((r) => r.first_in).sort((a, b) => Date.parse(b.first_in!) - Date.parse(a.first_in!)).slice(0, 6);
  const notIn = rows.filter((r) => !r.first_in && r.status !== 'WeeklyOff' && r.status !== 'Holiday' && r.status !== 'OnLeave').length;
  const firstName = (user?.name ?? '').split(/\s+/)[0];

  return (
    <Stack gap="lg">
      <PageHeader title={`${greeting()}${firstName ? `, ${firstName}` : ''}`} subtitle={fmtDateLong(today)}
        actions={<>
          <Button variant="default" component={Link} to="/reports">Reports</Button>
          <Button component={Link} to="/attendance" leftSection={<IconUsersGroup size={16} stroke={1.8} />}>See who's in</Button>
        </>} />

      {d ? (
        <StatStrip stats={[
          { key: 'present', label: 'In today', value: tile('present'), hint: `of ${tile('headcount')} active employees` },
          { key: 'late', label: 'Late', value: tile('late'), hint: 'after shift grace', ...(Number(tile('late')) > 0 ? { tone: 'warn' as const } : {}) },
          { key: 'absent', label: 'Absent', value: tile('absent'), hint: `${notIn} not clocked in yet` },
          { key: 'pending', label: 'Pending approvals', value: tile('pendingApprovals'), hint: 'leave, corrections, overtime' },
          { key: 'exceptions', label: 'Open exceptions', value: tile('exceptions'), hint: 'this payroll month' },
        ]} />
      ) : <Skeleton h={98} radius="lg" />}

      <Paper withBorder p="lg">
        <Group justify="space-between" mb="md" align="flex-start">
          <div>
            <Text fw={600} size="sm">Arrivals today</Text>
            <Text size="xs" c="dimmed">Each mark is one person's first clock-in.</Text>
          </div>
          <Anchor component={Link} to="/attendance" size="sm">Open list</Anchor>
        </Group>
        {team.loading && !team.data ? <Skeleton h={90} /> : <ArrivalsRibbon arrivals={arrivals} shiftStart={shift?.start?.slice(0, 5) ?? '10:00'} grace={shift?.grace_in_min ?? 15} />}
      </Paper>

      <Grid gap="lg">
        <Grid.Col span={{ base: 12, md: 8 }}>
          {d ? <TrendBars label="Attendance rate"  points={d.trend.filter((p) => p.date !== today).map((p) => ({ date: p.date, value: p.presentPct }))} /> : <Skeleton h={232} radius="lg" />}
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 4 }}>
          <Paper withBorder p="lg" h="100%">
            <Text fw={600} size="sm" mb="md">Needs attention</Text>
            {d && d.needsAttention.length === 0 ? (
              <Group gap="sm" wrap="nowrap"><ThemeIcon variant="light" color="brand" radius="xl"><IconCircleCheck size={16} /></ThemeIcon><Text size="sm" c="dimmed">All clear. Nothing is waiting on you.</Text></Group>
            ) : null}
            <Stack gap="sm">
              {(d?.needsAttention ?? []).map((n, i) => {
                const a = ATTENTION[n.kind] ?? { to: '/overview', icon: IconAlertTriangle, action: 'Open' };
                const Ico = a.icon;
                return (
                  <Group key={i} gap="sm" wrap="nowrap" align="flex-start">
                    <ThemeIcon variant="light" color="orange" radius="md" size={30}><Ico size={16} stroke={1.7} /></ThemeIcon>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Text size="sm" lh={1.35}>{n.label}</Text>
                      <Anchor component={Link} to={a.to} size="xs">{a.action}</Anchor>
                    </div>
                  </Group>
                );
              })}
            </Stack>
          </Paper>
        </Grid.Col>
      </Grid>

      <Grid gap="lg">
        <Grid.Col span={{ base: 12, md: 8 }}>
          <Paper withBorder p="lg" h="100%">
            <Group justify="space-between" mb="sm">
              <Text fw={600} size="sm">Latest clock-ins</Text>
              <Text size="xs" c="dimmed">{rows.filter((r) => r.first_in).length} so far today</Text>
            </Group>
            {recent.length === 0 ? <Text size="sm" c="dimmed">No one has clocked in yet today.</Text> : (
              <Stack gap={0}>
                {recent.map((r) => (
                  <Group key={r.employee_id} justify="space-between" py={8} wrap="nowrap" style={{ borderTop: '1px solid var(--hj-line-soft)' }}>
                    <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
                      <Avatar size={28} radius="xl" color="brand" variant="light">{r.name_en.split(' ').map((p) => p[0]).slice(0, 2).join('')}</Avatar>
                      <Anchor component={Link} to={`/employees/${r.employee_id}`} c="inherit" size="sm" fw={500} truncate>{r.name_en}</Anchor>
                    </Group>
                    <Group gap="md" wrap="nowrap">
                      <StatusBadge status={liveStatus(r.status, { date: today, today, lastOut: r.last_out, firstIn: r.first_in }) ?? 'NotComputed'} late={r.late_min} remote={r.remote} />
                      <Text size="sm" className="num" w={44} ta="right">{fmtTime(r.first_in)}</Text>
                    </Group>
                  </Group>
                ))}
              </Stack>
            )}
          </Paper>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 4 }}>
          <Paper withBorder p="lg" h="100%">
            <Group justify="space-between" mb="md">
              <Text fw={600} size="sm">Payroll</Text>
              <ThemeIcon variant="light" color="gray" radius="md" size={28}><IconCash size={16} stroke={1.7} /></ThemeIcon>
            </Group>
            {d?.lastPayrollRun ? (
              <Stack gap={4}>
                <Text size="sm" c="dimmed">{bsMonthLabel(d.lastPayrollRun.bs_year, d.lastPayrollRun.bs_month)} run</Text>
                <Text fz={24} fw={600} className="num" style={{ letterSpacing: '-0.02em' }}>{fmtMoney(d.lastPayrollRun.net)}</Text>
                <Group gap="xs"><StatusBadge status={d.lastPayrollRun.status} /><Text size="xs" c="dimmed">{d.lastPayrollRun.headcount} payslips</Text></Group>
              </Stack>
            ) : (
              <Stack gap="xs">
                <Text size="sm" c="dimmed">No pay run yet. Start one when the month's attendance is final.</Text>
                <Anchor component={Link} to="/payroll" size="sm" style={{ display: 'inline-flex', alignItems: 'center', gap: 2 }}>Go to pay runs <IconArrowUpRight size={14} /></Anchor>
              </Stack>
            )}
            <Text size="xs" c="dimmed" mt="lg">Overtime detected this month</Text>
            <Text fw={600} className="num">{d ? minutesToHm(d.otMinutesThisMonth) : '—'}</Text>
          </Paper>
        </Grid.Col>
      </Grid>
    </Stack>
  );
}
