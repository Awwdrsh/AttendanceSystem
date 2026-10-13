import { Box, Button, Group, Loader, Paper, Progress, Stack, Text, ThemeIcon } from '@mantine/core';
import { IconCheck, IconChevronRight } from '@tabler/icons-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { devices as devApi, employees as empApi, leave as leaveApi, payroll as payApi, settings as settingsApi, shifts as shiftApi } from '../../api';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

interface Step { title: string; done: boolean; detail: string; go: string; cta: string; auto?: { label: string; run: () => Promise<void> } | undefined }

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** Guided setup: each step shows its state, a link to the page that completes it, and a one-click default where one exists. */
export default function SetupPage() {
  const [running, setRunning] = useState<string | null>(null);
  const q = useQuery(async () => {
    const [company, branches, holidays, shifts, types, comps, emps, devs] = await Promise.all([
      settingsApi.company(), settingsApi.branches(), shiftApi.holidays(), shiftApi.list(), leaveApi.types(), payApi.components().catch(() => ({ components: [] })), empApi.list({ status: 'active' }), devApi.list(),
    ]);
    return { company, branches: branches.branches.length, holidays: holidays.holidays.length, shifts: shifts.shifts.length, types: types.types.length, comps: comps.components.length, emps: emps.employees.length, devs: devs.devices.filter((d) => !['mobile', 'kiosk', 'manual'].includes(d.vendor)).length, pending: devs.devices.filter((d) => d.status === 'pending').length };
  }, []);
  const d = q.data;
  const steps: Step[] = d ? [
    { title: 'Company profile', done: !!d.company.company.nameEn, detail: d.company.company.nameEn || 'Add your company name, PAN and address.', go: '/hr/settings', cta: 'Open settings' },
    { title: 'Branches', done: d.branches > 0, detail: d.branches > 0 ? plural(d.branches, 'branch', 'branches') : 'Add at least one branch or office.', go: '/hr/settings', cta: 'Open settings' },
    { title: 'Holiday calendar', done: d.holidays > 0, detail: d.holidays > 0 ? plural(d.holidays, 'holiday', 'holidays') : 'Load the public holidays for this year.', go: '/hr/holidays', cta: 'Open holidays',
      auto: { label: 'Add public holidays', run: async () => { const r = await shiftApi.importHolidays(2083); toast.ok(`${r.inserted} holidays added.`); } } },
    { title: 'Shifts', done: d.shifts > 0, detail: d.shifts > 0 ? plural(d.shifts, 'shift', 'shifts') : 'Define working hours and grace periods.', go: '/hr/shifts', cta: 'Open shifts',
      auto: d.shifts === 0 ? { label: 'Create 10:00 to 18:00 shift', run: async () => { await shiftApi.create({ code: 'GEN', nameEn: 'General 10:00–18:00', start: '10:00', end: '18:00', graceInMin: 15, graceOutMin: 0, earlyInWindowMin: 60, lateOutWindowMin: 120, breakMinutes: 0, halfDayThresholdMin: 240, absentThresholdMin: 120, pairingMode: 'alternating', roundingMin: 0, roundingDirection: 'nearest' }); toast.ok('General shift created.'); } } : undefined },
    { title: 'Leave policies', done: d.types > 0, detail: d.types > 0 ? `${plural(d.types, 'leave type', 'leave types')}, statutory defaults included` : 'Review the statutory leave types.', go: '/hr/leave-types', cta: 'Open leave types' },
    { title: 'Pay components and statutory rules', done: d.comps > 0, detail: d.comps > 0 ? plural(d.comps, 'component', 'components') : 'Set up basic pay, allowances and deductions.', go: '/payroll/structures', cta: 'Open salary structures' },
    { title: 'Employees', done: d.emps > 0, detail: d.emps > 0 ? `${d.emps} active` : 'Add people one by one or import a CSV.', go: '/hr/employees', cta: 'Open employees' },
    { title: 'Attendance terminals', done: d.devs > 0 && d.pending === 0, detail: d.devs > 0 ? `${plural(d.devs, 'terminal', 'terminals')}, ${d.pending} waiting for approval` : 'Connect your biometric terminals.', go: '/hr/devices', cta: 'Open devices' },
    { title: 'Pilot, then go live', done: false, detail: 'Run one department for two pay cycles alongside your current method before switching everyone over.', go: '/hr/dashboard', cta: 'Open overview' },
  ] : [];
  const doneCount = steps.filter((s) => s.done).length;
  const nextIdx = steps.findIndex((s) => !s.done);
  return (
    <Stack gap="md">
      <PageHeader title="Setup checklist" subtitle="Work from top to bottom. Sensible Nepal defaults are already loaded, and you can come back to any step later." />
      {q.error ? <EmptyState title="Could not load setup status" hint={q.error} action={{ label: 'Try again', onClick: () => { void q.reload(); } }} /> : null}
      {!d && !q.error ? <Group justify="center" py="xl"><Loader size="sm" /></Group> : null}
      {d ? (
        <Paper withBorder>
          <Group justify="space-between" px="lg" py="md" gap="md" wrap="wrap" style={{ borderBottom: '1px solid var(--hj-line)' }}>
            <Text size="sm" fw={500} className="num">{doneCount} of {steps.length} steps done</Text>
            <Progress value={(doneCount / steps.length) * 100} w={{ base: '100%', sm: 220 }} size="sm" color="teal" aria-label="Setup progress" />
          </Group>
          {steps.map((s, i) => {
            const isNext = i === nextIdx;
            return (
              <Box key={s.title} px="lg" py="md" style={{ borderTop: i > 0 ? '1px solid var(--hj-line-soft)' : undefined }}>
                <Group justify="space-between" wrap="wrap" gap="md">
                  <Group gap="md" wrap="nowrap" align="flex-start" style={{ flex: '1 1 360px', minWidth: 0 }}>
                    {s.done
                      ? <ThemeIcon radius="xl" size={26} color="teal" variant="light" aria-label="Done"><IconCheck size={15} stroke={2} /></ThemeIcon>
                      : <ThemeIcon radius="xl" size={26} color={isNext ? 'brand' : 'gray'} variant={isNext ? 'filled' : 'light'}><Text size="xs" fw={600} className="num">{i + 1}</Text></ThemeIcon>}
                    <div style={{ minWidth: 0 }}>
                      <Text fw={500} size="sm" c={s.done ? 'dimmed' : 'inherit'}>{s.title}</Text>
                      <Text size="sm" c="dimmed">{s.detail}</Text>
                    </div>
                  </Group>
                  <Group gap="xs">
                    {s.auto && !s.done ? (
                      <Button size="xs" variant={isNext ? 'filled' : 'default'} loading={running === s.title}
                        onClick={async () => { setRunning(s.title); try { await s.auto!.run(); await q.reload(); } catch (e) { toast.err(e); } finally { setRunning(null); } }}>{s.auto.label}</Button>
                    ) : null}
                    <Button component={Link} to={s.go} size="xs" variant="subtle" rightSection={<IconChevronRight size={14} stroke={1.6} />}>{s.cta}</Button>
                  </Group>
                </Group>
              </Box>
            );
          })}
        </Paper>
      ) : null}
    </Stack>
  );
}
