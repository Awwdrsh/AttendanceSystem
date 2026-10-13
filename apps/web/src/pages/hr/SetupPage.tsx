import { Badge, Button, Group, Paper, Stack, Text } from '@mantine/core';
import { useNavigate } from 'react-router-dom';
import { devices as devApi, employees as empApi, leave as leaveApi, payroll as payApi, settings as settingsApi, shifts as shiftApi } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Guided setup checklist (plan §13.8): each step shows its state and a "do it with defaults" action where one exists. */
export default function SetupPage() {
  const { t } = useI18n();
  const nav = useNavigate();
  const q = useQuery(async () => {
    const [company, branches, holidays, shifts, types, comps, emps, devs] = await Promise.all([
      settingsApi.company(), settingsApi.branches(), shiftApi.holidays(), shiftApi.list(), leaveApi.types(), payApi.components().catch(() => ({ components: [] })), empApi.list({ status: 'active' }), devApi.list(),
    ]);
    return { company, branches: branches.branches.length, holidays: holidays.holidays.length, shifts: shifts.shifts.length, types: types.types.length, comps: comps.components.length, emps: emps.employees.length, devs: devs.devices.filter((d) => !['mobile', 'kiosk', 'manual'].includes(d.vendor)).length, pending: devs.devices.filter((d) => d.status === 'pending').length };
  }, []);
  const d = q.data;
  const steps = d ? [
    { title: 'Company profile', done: !!d.company.company.nameEn, detail: d.company.company.nameEn, go: '/hr/settings' },
    { title: 'Branches', done: d.branches > 0, detail: `${d.branches} branch(es)`, go: '/hr/settings' },
    { title: 'Holiday calendar', done: d.holidays > 0, detail: `${d.holidays} holidays`, go: '/hr/holidays', auto: async () => { const r = await shiftApi.importHolidays(2083); toast.ok(`${r.inserted} holidays added.`); } },
    { title: 'Shifts', done: d.shifts > 0, detail: `${d.shifts} shift(s)`, go: '/hr/shifts', auto: d.shifts === 0 ? async () => { await shiftApi.create({ code: 'GEN', nameEn: 'General 10:00–18:00', start: '10:00', end: '18:00', graceInMin: 15, graceOutMin: 0, earlyInWindowMin: 60, lateOutWindowMin: 120, breakMinutes: 0, halfDayThresholdMin: 240, absentThresholdMin: 120, pairingMode: 'alternating', roundingMin: 0, roundingDirection: 'nearest' }); toast.ok('General shift created.'); } : undefined },
    { title: 'Leave policies', done: d.types > 0, detail: `${d.types} leave types (statutory defaults seeded)`, go: '/hr/leave-types' },
    { title: 'Pay components & statutory rules', done: d.comps > 0, detail: `${d.comps} components`, go: '/payroll/structures' },
    { title: 'Import employees', done: d.emps > 0, detail: `${d.emps} active`, go: '/hr/employees' },
    { title: 'Terminals', done: d.devs > 0 && d.pending === 0, detail: `${d.devs} terminal(s), ${d.pending} pending`, go: '/hr/devices' },
    { title: 'Pilot department → go live', done: false, detail: 'Run one department for two pay cycles in parallel with your current method (plan §13.15).', go: '/hr/dashboard' },
  ] : [];
  return (
    <Stack>
      <PageHeader title={t('setup')} subtitle="Work top to bottom. Sensible Nepal defaults are already loaded; each step can be revisited later." />
      {steps.map((s, i) => (
        <Paper key={s.title} withBorder p="md" radius="md">
          <Group justify="space-between" wrap="wrap">
            <Group gap="sm"><Badge circle size="lg" color={s.done ? 'teal' : 'gray'} variant={s.done ? 'filled' : 'light'}>{s.done ? '✓' : i + 1}</Badge><div><Text fw={600}>{s.title}</Text><Text size="xs" c="dimmed">{s.detail}</Text></div></Group>
            <Group gap="xs">{s.auto && !s.done ? <Button size="xs" variant="light" onClick={async () => { try { await s.auto!(); await q.reload(); } catch (e) { toast.err(e); } }}>Do it with defaults</Button> : null}<Button size="xs" variant="default" onClick={() => nav(s.go)}>Open</Button></Group>
          </Group>
        </Paper>
      ))}
    </Stack>
  );
}
