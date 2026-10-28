import { ActionIcon, Alert, Button, Group, Modal, Stack, Text, Textarea } from '@mantine/core';
import { IconChevronLeft, IconChevronRight, IconHourglass, IconPlus, IconSend } from '@tabler/icons-react';
import { useState } from 'react';
import { time as timeApi, weekStartOf } from '../../api/time';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateShort, minutesToHm, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { EntryModal, TimerCard, TimesheetBadge, WeekDays } from './parts';

/** The employee's own week: a timer, entries day by day, and submitting the week for approval. */
export default function MyTimesheetPage() {
  const thisWeek = weekStartOf(todayNpt());
  const [week, setWeek] = useState(thisWeek);
  const q = useQuery(() => timeApi.week({ weekStart: week }), [week]);
  const settings = useQuery(() => timeApi.settings(), []);
  const [editing, setEditing] = useState(null);
  const [removing, setRemoving] = useState(null);
  const [submitOpen, setSubmitOpen] = useState(false);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  const w = q.data;
  const ts = w?.timesheet ?? null;
  const locked = ts?.status === 'submitted' || ts?.status === 'approved';
  const hasTime = (w?.totals.trackedMin ?? 0) > 0;
  const reload = () => void q.reload();
  const addDate = week === thisWeek ? todayNpt() : week;

  if (settings.data && !settings.data.policy.enabled) {
    return (
      <EmptyState
        icon={<IconHourglass size={20} stroke={1.6} />}
        title="Time tracking is turned off"
        hint="Your company does not track project time. Ask HR if you think this is wrong."
      />
    );
  }

  const submit = async () => {
    setBusy(true);
    try {
      await timeApi.submit(week, note.trim() || null);
      toast.ok('Timesheet sent for approval');
      setSubmitOpen(false);
      setNote('');
      reload();
    } catch (e) {
      toast.err(e, 'Not submitted');
    } finally {
      setBusy(false);
    }
  };
  const withdraw = async () => {
    if (!ts) return;
    setBusy(true);
    try {
      await timeApi.withdraw(ts.id);
      toast.ok('Timesheet withdrawn. You can edit the week again.');
      reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack gap="lg">
      <TimerCard onChange={reload} />

      <SectionTitle
        hint={
          w
            ? `${fmtDateShort(w.weekStart)} to ${fmtDateShort(w.weekEnd)}${w.rounding.step ? ` · rounded ${w.rounding.mode} to ${w.rounding.step} min` : ''}`
            : undefined
        }
        actions={
          <Group gap="xs" wrap="wrap">
            <ActionIcon variant="default" size="lg" aria-label="Previous week" onClick={() => setWeek(addDays(week, -7))}>
              <IconChevronLeft size={16} />
            </ActionIcon>
            <Button variant="default" size="sm" disabled={week === thisWeek} onClick={() => setWeek(thisWeek)}>
              This week
            </Button>
            <ActionIcon
              variant="default"
              size="lg"
              aria-label="Next week"
              disabled={week >= thisWeek}
              onClick={() => setWeek(addDays(week, 7))}
            >
              <IconChevronRight size={16} />
            </ActionIcon>
            {!locked ? (
              <Button
                variant="default"
                size="sm"
                leftSection={<IconPlus size={16} stroke={1.6} />}
                onClick={() => setEditing({ entry: null, date: addDate })}
              >
                Add time
              </Button>
            ) : null}
          </Group>
        }
      >
        <Group gap="xs" component="span">
          Week <TimesheetBadge status={ts?.status} />
        </Group>
      </SectionTitle>

      {ts?.status === 'rejected' ? (
        <Alert color="red" variant="light" title={`Returned by ${ts.decided_by_name ?? 'your manager'}`}>
          {ts.decision_comment ?? 'Fix the week and submit it again.'}
        </Alert>
      ) : null}
      {ts?.status === 'submitted' ? (
        <Alert color="yellow" variant="light" title="Waiting for approval">
          Sent {relativeTime(ts.submitted_at)}. The week is locked while your manager reviews it. Withdraw it if you need to change
          something.
        </Alert>
      ) : null}
      {ts?.status === 'approved' ? (
        <Alert color="teal" variant="light" title="Approved">
          Approved by {ts.decided_by_name ?? 'your manager'}
          {ts.decision_comment ? `: ${ts.decision_comment}` : ''}. This week is locked.
        </Alert>
      ) : null}

      <StatStrip
        stats={[
          { key: 'tracked', label: 'Logged', value: w ? minutesToHm(w.totals.roundedMin) : '—' },
          { key: 'billable', label: 'Billable', value: w ? minutesToHm(w.totals.billableMin) : '—' },
          { key: 'attended', label: 'Attended', value: w ? minutesToHm(w.totals.attendanceMin) : '—', hint: 'From your clock-ins' },
          {
            key: 'flags',
            label: 'Days to check',
            value: w?.totals.flaggedDays ?? '—',
            ...(w && w.totals.flaggedDays > 0 ? { tone: 'warn' } : {}),
          },
        ]}
      />

      {w && !hasTime && !w.running ? (
        <EmptyState
          icon={<IconHourglass size={20} stroke={1.6} />}
          title="No time logged this week"
          hint="Start the timer while you work, or add time you have already spent."
          {...(locked ? {} : { action: { label: 'Add time', onClick: () => setEditing({ entry: null, date: addDate }) } })}
        />
      ) : w ? (
        <WeekDays
          days={w.days}
          {...(locked
            ? {}
            : {
                onEdit: (e) => setEditing({ entry: e, date: e.local_date }),
                onDelete: setRemoving,
                onAdd: (date) => setEditing({ entry: null, date }),
              })}
        />
      ) : null}

      {w && hasTime ? (
        <Group justify="flex-end">
          {ts?.status === 'submitted' ? (
            <Button variant="default" loading={busy} onClick={withdraw}>
              Withdraw
            </Button>
          ) : null}
          {!locked ? (
            <Button leftSection={<IconSend size={16} stroke={1.6} />} disabled={w.running} onClick={() => setSubmitOpen(true)}>
              {ts?.status === 'rejected' ? 'Submit again' : 'Submit week'}
            </Button>
          ) : null}
        </Group>
      ) : null}
      {w?.running && !locked ? (
        <Text size="xs" c="dimmed" ta="right" mt={-12}>
          Stop the timer before you submit this week.
        </Text>
      ) : null}

      <EntryModal
        opened={!!editing}
        onClose={() => setEditing(null)}
        entry={editing?.entry ?? null}
        defaultDate={editing?.date ?? todayNpt()}
        onSaved={reload}
      />
      <ConfirmDialog
        opened={!!removing}
        title="Delete this time?"
        message={
          removing
            ? `${minutesToHm(removing.duration_min)} on ${fmtDateShort(removing.local_date)}${removing.project_name ? ` for ${removing.project_name}` : ''} will be removed.`
            : ''
        }
        confirmLabel="Delete"
        onClose={() => setRemoving(null)}
        onConfirm={async () => {
          if (!removing) return;
          try {
            await timeApi.deleteEntry(removing.id);
            toast.ok('Entry deleted');
            reload();
          } catch (e) {
            toast.err(e);
          }
        }}
      />
      <Modal opened={submitOpen} onClose={() => setSubmitOpen(false)} title="Submit this week?" centered radius="lg">
        <Stack gap="sm">
          <Text size="sm">
            {w
              ? `${minutesToHm(w.totals.roundedMin)} for ${fmtDateShort(w.weekStart)} to ${fmtDateShort(w.weekEnd)} goes to your manager. The week is locked until they decide.`
              : ''}
          </Text>
          {w && w.totals.flaggedDays > 0 ? (
            <Alert color="yellow" variant="light">
              {w.totals.flaggedDays} {w.totals.flaggedDays === 1 ? 'day differs' : 'days differ'} from your attendance. Add a note so your
              manager knows why.
            </Alert>
          ) : null}
          <Textarea
            label="Note for your manager"
            value={note}
            onChange={(e) => setNote(e.currentTarget.value)}
            maxLength={500}
            autosize
            minRows={2}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setSubmitOpen(false)}>
              Cancel
            </Button>
            <Button loading={busy} onClick={submit}>
              Submit
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
