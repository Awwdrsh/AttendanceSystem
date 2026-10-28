import { Badge, Button, Group, SegmentedControl, Stack, Text, Tooltip } from '@mantine/core';
import { IconBell } from '@tabler/icons-react';
import { adToBs, bsMonthBounds } from '@/lib/bs-calendar';
import { useState } from 'react';
import { attendanceTools } from '../../api/attendanceTools';
import { BsMonthPicker } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, bsMonthLabel, fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const list = (dates) => dates.map(fmtDateShort).join(', ');
const dayCount = (n) => `${n} ${n === 1 ? 'day' : 'days'}`;

/**
 * Attendance issues before payroll: missing punches, absences nobody has asked to cover, and leave and time requests still
 * waiting. One click reminds the employee or the approver (at most once a day each). Managers see their team, HR everyone.
 */
export default function AttendanceIssuesPage() {
  const now = adToBs(todayNpt());
  const [mode, setMode] = useState('month');
  const [month, setMonth] = useState({ year: now.year, month: now.month });
  const bounds = bsMonthBounds(month.year, month.month);
  const from = mode === 'month' ? bounds.startAd : addDays(todayNpt(), -13);
  const to = mode === 'month' ? bounds.endAd : todayNpt();
  const q = useQuery(() => attendanceTools.issues(from, to), [from, to]);
  const [busy, setBusy] = useState(null);
  const rows = q.data?.rows ?? [];
  const s = q.data?.summary;

  const nudge = async (key, fn) => {
    setBusy(key);
    try {
      const r = await fn();
      if (r.already) toast.info('Already reminded today.');
      else toast.ok(r.sent === 1 ? 'Reminder sent.' : `${r.sent} reminders sent.`);
    } catch (e) {
      toast.err(e, 'Reminder not sent');
    } finally {
      setBusy(null);
    }
  };
  const pending = (p) => (
    <Group key={p.instanceId} gap={6} wrap="nowrap">
      <Badge size="sm" variant="light" color={p.type === 'leave' ? 'blue' : 'orange'}>
        {p.label}
      </Badge>
      <Text size="xs" c="dimmed">
        {p.from === p.to ? fmtDateShort(p.from) : `${fmtDateShort(p.from)} to ${fmtDateShort(p.to)}`}, waiting {dayCount(p.ageDays)} on{' '}
        {p.waitingOn.map((w) => w.name).join(', ') || 'nobody'}
      </Text>
      <Tooltip label="Remind the approver">
        <Button
          size="compact-xs"
          variant="subtle"
          loading={busy === p.instanceId}
          onClick={() => void nudge(p.instanceId, () => attendanceTools.nudgeApprover(p.instanceId))}
        >
          <IconBell size={13} stroke={1.6} />
        </Button>
      </Tooltip>
    </Group>
  );

  return (
    <Stack gap="lg">
      <SectionTitle
        hint="Fix these before payroll: a missing punch is paid as worked, an absence with no leave or time request is unpaid, and waiting requests do not count until approved."
        actions={
          <Group gap="xs" align="flex-end">
            <SegmentedControl
              size="xs"
              value={mode}
              onChange={(v) => setMode(v)}
              data={[
                { value: 'month', label: 'Month' },
                { value: 'recent', label: 'Last 14 days' },
              ]}
            />
            {mode === 'month' ? <BsMonthPicker value={month} onChange={setMonth} /> : null}
          </Group>
        }
      >
        Attendance issues{mode === 'month' ? `, ${bsMonthLabel(month.year, month.month)}` : ''}
      </SectionTitle>
      <StatStrip
        stats={[
          { key: 'people', label: 'People with issues', value: s?.employees ?? 0 },
          { key: 'missing', label: 'Missing punches', value: s?.missing ?? 0, ...(s?.missing ? { tone: 'warn' } : {}) },
          { key: 'absent', label: 'Unexplained absences', value: s?.absent ?? 0, ...(s?.absent ? { tone: 'bad' } : {}) },
          {
            key: 'pending',
            label: 'Requests waiting',
            value: (s?.pendingLeave ?? 0) + (s?.pendingTime ?? 0),
            hint: s?.oldestPendingDays ? `oldest ${s.oldestPendingDays} days` : undefined,
          },
        ]}
      />
      <DataTable
        rows={rows}
        rowKey={(r) => r.employeeId}
        loading={q.loading}
        error={q.error}
        emptyTitle="No attendance issues"
        emptyHint="Everyone's punches, absences and requests for this period are in order."
        columns={[
          {
            key: 'emp',
            header: 'Name',
            sticky: true,
            render: (r) => (
              <div>
                <Text size="sm" fw={500}>
                  {r.name}
                </Text>
                <Text size="xs" c="dimmed">
                  {r.empCode}
                </Text>
              </div>
            ),
          },
          {
            key: 'missing',
            header: 'Missing punch',
            render: (r) =>
              r.missing.length ? (
                <Tooltip multiline w={260} label={list(r.missing)}>
                  <Badge variant="light" color="yellow" size="sm">
                    {dayCount(r.missing.length)}
                  </Badge>
                </Tooltip>
              ) : (
                <Text size="sm" c="dimmed">
                  —
                </Text>
              ),
          },
          {
            key: 'absent',
            header: 'Absent, no request',
            render: (r) =>
              r.absent.length ? (
                <Tooltip multiline w={260} label={list(r.absent)}>
                  <Badge variant="light" color="red" size="sm">
                    {dayCount(r.absent.length)}
                  </Badge>
                </Tooltip>
              ) : (
                <Text size="sm" c="dimmed">
                  —
                </Text>
              ),
          },
          {
            key: 'pending',
            header: 'Waiting for approval',
            render: (r) =>
              r.pending.length ? (
                <Stack gap={4}>{r.pending.map(pending)}</Stack>
              ) : (
                <Text size="sm" c="dimmed">
                  —
                </Text>
              ),
          },
          {
            key: 'act',
            header: '',
            align: 'right',
            render: (r) =>
              r.missing.length || r.absent.length ? (
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<IconBell size={14} stroke={1.6} />}
                  loading={busy === r.employeeId}
                  onClick={() => void nudge(r.employeeId, () => attendanceTools.nudgeEmployee(r.employeeId, from, to))}
                >
                  Remind
                </Button>
              ) : null,
          },
        ]}
      />
    </Stack>
  );
}
