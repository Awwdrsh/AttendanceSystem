import { ActionIcon, Alert, Badge, Button, Drawer, Group, SegmentedControl, Select, Stack, Text, Textarea } from '@mantine/core';
import { IconChevronLeft, IconChevronRight, IconDownload } from '@tabler/icons-react';
import { useState } from 'react';
import { workflow as wfApi } from '../../api';
import { time as timeApi, hhmmOf, weekStartOf, type GroupBy, type SummaryRow, type TeamWeekRow, type TimesheetListRow } from '../../api/time';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateShort, fmtMoney, minutesToHm, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { TimesheetBadge, WeekDays } from './parts';

const weekLabel = (ws: string) => `${fmtDateShort(ws)} to ${fmtDateShort(addDays(ws, 6))}`;

/** Logged minus attended, coloured when it is more than an hour off. */
function Diff({ min }: { min: number }) {
  const off = Math.abs(min) > 60;
  return <Text size="sm" ff="monospace" c={off ? 'orange.7' : 'dimmed'}>{min > 0 ? '+' : min < 0 ? '−' : ''}{hhmmOf(Math.abs(min))}</Text>;
}

/** One submitted week with its days, and approve or return when it is waiting. */
function ReviewDrawer({ id, onClose, onDecided }: { id: string | null; onClose: () => void; onDecided: () => void }) {
  const q = useQuery(() => (id ? timeApi.timesheet(id) : Promise.resolve(null)), [id]);
  const [reason, setReason] = useState(''); const [busy, setBusy] = useState<'approve' | 'reject' | null>(null);
  const d = q.data;
  const sub = d?.submission;
  const decide = async (kind: 'approve' | 'reject') => {
    if (!sub?.workflow_instance_id) return;
    if (kind === 'reject' && !reason.trim()) { toast.info('Say what needs fixing so they can correct it.'); return; }
    setBusy(kind);
    try {
      if (kind === 'approve') await wfApi.approve(sub.workflow_instance_id, reason.trim() ? { reason: reason.trim() } : undefined);
      else await wfApi.reject(sub.workflow_instance_id, reason.trim());
      toast.ok(kind === 'approve' ? 'Timesheet approved' : 'Timesheet returned');
      setReason(''); onDecided(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(null); }
  };
  return (
    <Drawer opened={!!id} onClose={onClose} position="right" size="lg" title={d ? `${d.employee.name} · ${weekLabel(d.weekStart)}` : 'Timesheet'}>
      {d && sub ? (
        <Stack gap="md">
          <Group gap="xs"><TimesheetBadge status={sub.status} /><Text size="sm" c="dimmed">Sent {relativeTime(sub.submitted_at)}</Text></Group>
          <StatStrip stats={[
            { key: 'logged', label: 'Logged', value: minutesToHm(d.totals.roundedMin) },
            { key: 'attended', label: 'Attended', value: minutesToHm(d.totals.attendanceMin) },
            { key: 'billable', label: 'Billable', value: minutesToHm(d.totals.billableMin) },
            { key: 'flags', label: 'Days to check', value: d.totals.flaggedDays, ...(d.totals.flaggedDays ? { tone: 'warn' as const } : {}) },
          ]} />
          {sub.note ? <Alert variant="light" color="gray" title="Note">{sub.note}</Alert> : null}
          {sub.decision_comment ? <Alert variant="light" color={sub.status === 'rejected' ? 'red' : 'teal'} title={`${sub.decided_by_name ?? 'Reviewer'}`}>{sub.decision_comment}</Alert> : null}
          <WeekDays days={d.days} />
          {sub.status === 'submitted' && sub.workflow_instance_id ? (
            <Stack gap="xs">
              <Textarea label="Comment" description="Required when you return the week." value={reason} onChange={(e) => setReason(e.currentTarget.value)} maxLength={500} autosize minRows={2} />
              <Group justify="flex-end">
                <Button variant="default" color="red" loading={busy === 'reject'} onClick={() => decide('reject')}>Return</Button>
                <Button loading={busy === 'approve'} onClick={() => decide('approve')}>Approve</Button>
              </Group>
            </Stack>
          ) : null}
        </Stack>
      ) : <Text c="dimmed" size="sm">{q.error ?? 'Loading…'}</Text>}
    </Drawer>
  );
}

function ToApprove({ onOpen, version }: { onOpen: (id: string) => void; version: number }) {
  const q = useQuery(() => timeApi.timesheets({ status: 'submitted' }), [version]);
  return (
    <DataTable<TimesheetListRow> rows={q.data?.timesheets ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error} onRowClick={(r) => onOpen(r.id)}
      emptyTitle="Nothing waiting" emptyHint="Submitted timesheets from your team appear here for approval."
      columns={[
        { key: 'name', header: 'Employee', render: (r) => <div><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></div> },
        { key: 'week', header: 'Week', render: (r) => <Text size="sm">{weekLabel(r.week_start)}</Text> },
        { key: 'logged', header: 'Logged', align: 'right', render: (r) => <Text size="sm" ff="monospace">{hhmmOf(r.total_min)}</Text> },
        { key: 'att', header: 'Attended', align: 'right', render: (r) => <Text size="sm" ff="monospace" c="dimmed">{hhmmOf(r.attendance_min)}</Text> },
        { key: 'diff', header: 'Difference', align: 'right', render: (r) => <Diff min={r.diff_min} /> },
        { key: 'sent', header: 'Sent', render: (r) => <Text size="sm" c="dimmed">{relativeTime(r.submitted_at)}</Text> },
      ]} />
  );
}

function TeamWeek({ onOpen }: { onOpen: (id: string) => void }) {
  const thisWeek = weekStartOf(todayNpt());
  const [week, setWeek] = useState(thisWeek);
  const q = useQuery(() => timeApi.teamWeek(week), [week]);
  const people = q.data?.people ?? [];
  const submitted = people.filter((p) => p.status === 'submitted' || p.status === 'approved').length;
  return (
    <Stack gap="md">
      <SectionTitle hint={weekLabel(week)} actions={<Group gap="xs">
        <ActionIcon variant="default" size="lg" aria-label="Previous week" onClick={() => setWeek(addDays(week, -7))}><IconChevronLeft size={16} /></ActionIcon>
        <Button variant="default" size="sm" disabled={week === thisWeek} onClick={() => setWeek(thisWeek)}>This week</Button>
        <ActionIcon variant="default" size="lg" aria-label="Next week" disabled={week >= thisWeek} onClick={() => setWeek(addDays(week, 7))}><IconChevronRight size={16} /></ActionIcon>
      </Group>}>Who has submitted</SectionTitle>
      <StatStrip stats={[
        { key: 'people', label: 'People', value: people.length },
        { key: 'sub', label: 'Submitted', value: submitted },
        { key: 'missing', label: 'Not submitted', value: people.length - submitted, ...(people.length - submitted > 0 ? { tone: 'warn' as const } : {}) },
        { key: 'hours', label: 'Logged', value: minutesToHm(people.reduce((s, p) => s + p.tracked_min, 0)) },
      ]} />
      <DataTable<TeamWeekRow> rows={people} rowKey={(r) => r.employee_id} loading={q.loading} error={q.error}
        onRowClick={(r) => { if (r.timesheet_id) onOpen(r.timesheet_id); }}
        emptyTitle="No one to show" emptyHint="People who report to you appear here."
        columns={[
          { key: 'name', header: 'Employee', render: (r) => <div><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></div> },
          { key: 'logged', header: 'Logged', align: 'right', render: (r) => <Text size="sm" ff="monospace">{hhmmOf(r.tracked_min)}</Text> },
          { key: 'att', header: 'Attended', align: 'right', render: (r) => <Text size="sm" ff="monospace" c="dimmed">{hhmmOf(r.attendance_min)}</Text> },
          { key: 'diff', header: 'Difference', align: 'right', render: (r) => <Diff min={r.tracked_min - r.attendance_min} /> },
          { key: 'status', header: 'Timesheet', render: (r) => <TimesheetBadge status={r.status} /> },
        ]} />
    </Stack>
  );
}

function History({ onOpen, version }: { onOpen: (id: string) => void; version: number }) {
  const [status, setStatus] = useState<string | null>(null);
  const q = useQuery(() => timeApi.timesheets(status ? { status } : undefined), [status, version]);
  return (
    <Stack gap="md">
      <Group justify="flex-end">
        <Select size="xs" w={200} placeholder="Every status" clearable value={status} onChange={setStatus}
          data={[{ value: 'submitted', label: 'Waiting for approval' }, { value: 'approved', label: 'Approved' }, { value: 'rejected', label: 'Returned' }, { value: 'cancelled', label: 'Withdrawn' }]} />
      </Group>
      <DataTable<TimesheetListRow> rows={q.data?.timesheets ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error} onRowClick={(r) => onOpen(r.id)}
        emptyTitle="No timesheets yet" emptyHint="Timesheets appear here once people submit their weeks."
        columns={[
          { key: 'name', header: 'Employee', render: (r) => <Text size="sm" fw={500}>{r.name_en}</Text> },
          { key: 'week', header: 'Week', render: (r) => <Text size="sm">{weekLabel(r.week_start)}</Text> },
          { key: 'logged', header: 'Logged', align: 'right', render: (r) => <Text size="sm" ff="monospace">{hhmmOf(r.total_min)}</Text> },
          { key: 'diff', header: 'vs attended', align: 'right', render: (r) => <Diff min={r.diff_min} /> },
          { key: 'status', header: 'Status', render: (r) => <TimesheetBadge status={r.status} /> },
          { key: 'by', header: 'Decided by', render: (r) => <Text size="sm" c="dimmed">{r.decided_by_name ?? '—'}</Text> },
        ]} />
    </Stack>
  );
}

/** Hours by project, client, person, activity or day, with billable value and budget use; downloadable. */
export function HoursReport() {
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -29));
  const [to, setTo] = useState<string | null>(todayNpt());
  const [groupBy, setGroupBy] = useState<GroupBy>('project');
  const f = from ?? todayNpt(); const t = to ?? todayNpt();
  const q = useQuery(() => timeApi.summary({ from: f, to: t, groupBy }), [f, t, groupBy]);
  const rows = q.data?.rows ?? [];
  const label = groupBy === 'day' ? (r: SummaryRow) => fmtDateShort(r.label) : (r: SummaryRow) => r.label;
  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end" wrap="wrap" gap="sm">
        <Group gap="sm" align="flex-end" wrap="wrap">
          <DateField label="From" value={from} onChange={setFrom} maxDate={to ?? todayNpt()} w={180} />
          <DateField label="To" value={to} onChange={setTo} maxDate={todayNpt()} w={180} />
          <SegmentedControl value={groupBy} onChange={(v) => setGroupBy(v as GroupBy)}
            data={[{ value: 'project', label: 'Project' }, { value: 'client', label: 'Client' }, { value: 'employee', label: 'Person' }, { value: 'activity', label: 'Activity' }, { value: 'day', label: 'Day' }]} />
        </Group>
        <Group gap="xs">
          <Button variant="default" size="sm" leftSection={<IconDownload size={16} stroke={1.6} />} onClick={() => timeApi.downloadSummary({ from: f, to: t, groupBy }).catch((e) => toast.err(e, 'Download failed'))}>Summary CSV</Button>
          <Button variant="default" size="sm" leftSection={<IconDownload size={16} stroke={1.6} />} onClick={() => timeApi.downloadEntries({ from: f, to: t }).catch((e) => toast.err(e, 'Download failed'))}>All entries CSV</Button>
        </Group>
      </Group>
      <StatStrip stats={[
        { key: 'hours', label: 'Hours', value: q.data ? q.data.totals.hours.toFixed(2) : '—' },
        { key: 'billable', label: 'Billable hours', value: q.data ? q.data.totals.billableHours.toFixed(2) : '—' },
        { key: 'amount', label: 'Billable amount', value: q.data ? fmtMoney(q.data.totals.billableAmount) : '—' },
      ]} />
      {q.data?.rounding.step ? <Text size="xs" c="dimmed">Each entry is rounded {q.data.rounding.mode} to {q.data.rounding.step} minutes.</Text> : null}
      <DataTable<SummaryRow> rows={rows} rowKey={(r) => r.key} loading={q.loading} error={q.error}
        emptyTitle="No time in this range" emptyHint="Pick a longer range or check that people are logging time."
        columns={[
          { key: 'label', header: groupBy === 'employee' ? 'Person' : groupBy[0]!.toUpperCase() + groupBy.slice(1), render: (r) => <Group gap={6} wrap="nowrap"><Text size="sm" fw={500}>{label(r)}</Text>{r.code ? <Text size="xs" c="dimmed" ff="monospace">{r.code}</Text> : null}</Group> },
          { key: 'hours', header: 'Hours', align: 'right', render: (r) => <Text size="sm" ff="monospace">{r.hours.toFixed(2)}</Text> },
          { key: 'billable', header: 'Billable', align: 'right', render: (r) => <Text size="sm" ff="monospace">{r.billable_hours.toFixed(2)}</Text> },
          { key: 'amount', header: 'Amount', align: 'right', render: (r) => <Text size="sm">{r.billable_amount ? fmtMoney(r.billable_amount) : '—'}</Text> },
          ...(groupBy === 'project' ? [{ key: 'budget', header: 'Budget used', align: 'right' as const, render: (r: SummaryRow) => r.budget_used_pct === null ? <Text size="sm" c="dimmed">—</Text>
            : <Badge variant="light" color={r.budget_used_pct >= 100 ? 'red' : r.budget_used_pct >= 80 ? 'orange' : 'teal'}>{r.budget_used_pct}% of {r.budget_hours} h</Badge> }] : []),
        ]} />
    </Stack>
  );
}

/** Managers review their team's timesheets; HR sees everyone. */
export default function TimesheetsReviewPage() {
  const { hasRole } = useAuth();
  const [view, setView] = useState<'approve' | 'week' | 'history' | 'report'>('approve');
  const [open, setOpen] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  return (
    <Stack gap="lg">
      <SectionTitle hint={hasRole('hr') ? 'Everyone\'s project time, compared with their attendance.' : 'Your team\'s project time, compared with their attendance.'}
        actions={<SegmentedControl size="xs" value={view} onChange={(v) => setView(v as typeof view)}
          data={[{ value: 'approve', label: 'To approve' }, { value: 'week', label: 'This week' }, { value: 'history', label: 'History' }, { value: 'report', label: 'Hours report' }]} />}>
        Timesheets
      </SectionTitle>
      {view === 'approve' ? <ToApprove onOpen={setOpen} version={version} /> : null}
      {view === 'week' ? <TeamWeek onOpen={setOpen} /> : null}
      {view === 'history' ? <History onOpen={setOpen} version={version} /> : null}
      {view === 'report' ? <HoursReport /> : null}
      <ReviewDrawer id={open} onClose={() => setOpen(null)} onDecided={() => setVersion((v) => v + 1)} />
    </Stack>
  );
}
