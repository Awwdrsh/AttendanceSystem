import {
  ActionIcon,
  Badge,
  Box,
  Button,
  Checkbox,
  Group,
  Menu,
  Modal,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconDots,
  IconPencil,
  IconPlayerPlay,
  IconPlayerStop,
  IconRotateClockwise,
  IconTrash,
} from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { time as timeApi, hhmmOf } from '../../api/time';
import { DateField } from '../../components/BsDatePicker';
import { fmtDateLong, fmtTime, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS = {
  submitted: { label: 'Waiting for approval', color: 'yellow' },
  approved: { label: 'Approved', color: 'teal' },
  rejected: { label: 'Returned', color: 'red' },
  cancelled: { label: 'Withdrawn', color: 'gray' },
};

export function TimesheetBadge({ status }) {
  if (!status)
    return (
      <Badge variant="light" color="gray">
        Not submitted
      </Badge>
    );
  const s = STATUS[status];
  return (
    <Badge variant="light" color={s.color}>
      {s.label}
    </Badge>
  );
}

export function ProjectDot({ color }) {
  return (
    <span
      aria-hidden
      style={{ width: 8, height: 8, borderRadius: 99, background: color ?? 'var(--hj-muted)', display: 'inline-block', flex: 'none' }}
    />
  );
}

/** Project and activity pickers that only offer what the person may log to. */
export function useWorkOptions() {
  const projects = useQuery(() => timeApi.projects(), []);
  const activities = useQuery(() => timeApi.activities(), []);
  return { projects: projects.data?.projects ?? [], activities: activities.data?.activities ?? [] };
}

function projectData(projects) {
  return projects.map((p) => ({ value: p.id, label: `${p.name} (${p.code})${p.client_name ? ` · ${p.client_name}` : ''}` }));
}
function activityData(activities, projectId) {
  return activities.filter((a) => !a.project_id || a.project_id === projectId).map((a) => ({ value: a.id, label: a.name }));
}

function elapsed(fromIso, now) {
  const s = Math.max(0, Math.floor((now - Date.parse(fromIso)) / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

/** Start/stop timer on the server clock, with the project, activity and a note; resume picks up the last entry. */
export function TimerCard({ compact, onChange }) {
  const t = useQuery(() => timeApi.timer(), []);
  const settings = useQuery(() => timeApi.settings(), []);
  const { projects, activities } = useWorkOptions();
  const [projectId, setProjectId] = useState(null);
  const [activityId, setActivityId] = useState(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());
  const running = t.data?.running ?? null;
  // Offset between the server clock and this device so the counter matches what will be recorded.
  const skew = useMemo(() => (t.data ? Date.parse(t.data.serverTime) - Date.now() : 0), [t.data]);
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [running]);
  useEffect(() => {
    if (running) {
      setProjectId(running.project_id);
      setActivityId(running.activity_id);
      setNote(running.note ?? '');
    }
  }, [running?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (settings.data && !settings.data.policy.enabled) return null;
  const requireProject = settings.data?.policy.requireProject ?? false;

  const act = async (fn, ok) => {
    setBusy(true);
    try {
      await fn();
      toast.ok(ok);
      await t.reload();
      onChange?.();
    } catch (e) {
      toast.err(e, 'Timer');
    } finally {
      setBusy(false);
    }
  };
  const start = () => act(() => timeApi.start({ projectId, activityId, note: note.trim() || null }), 'Timer started');
  const stop = () => act(() => timeApi.stop(note.trim() || null), 'Time saved');
  const resume = () => act(() => timeApi.resume(), 'Timer resumed');

  return (
    <Paper withBorder p={compact ? 'md' : 'lg'}>
      <Group justify="space-between" align="center" wrap="wrap" gap="md">
        <Stack gap={2} style={{ minWidth: 0, flex: 1 }}>
          <Text size="sm" c="dimmed">
            {running ? `Tracking since ${fmtTime(running.started_at)}` : `Today: ${minutesToHm(t.data?.todayMin ?? 0)} logged`}
          </Text>
          <Text ff="monospace" fz={compact ? 26 : 32} fw={600} style={{ letterSpacing: '-0.02em', lineHeight: 1.1 }}>
            {running?.started_at ? elapsed(running.started_at, now + skew) : '0:00:00'}
          </Text>
          {running ? (
            <Group gap={6} wrap="nowrap">
              <ProjectDot color={running.project_color} />
              <Text size="sm" truncate>
                {running.project_name ?? 'No project'}
                {running.activity_name ? ` · ${running.activity_name}` : ''}
                {running.note ? ` · ${running.note}` : ''}
              </Text>
            </Group>
          ) : null}
        </Stack>
        <Group gap="xs">
          {running ? (
            <Button color="red" leftSection={<IconPlayerStop size={16} stroke={1.8} />} loading={busy} onClick={stop}>
              Stop
            </Button>
          ) : (
            <>
              {t.data?.last ? (
                <Tooltip label={`Resume ${t.data.last.project_name ?? 'last entry'}`}>
                  <Button variant="default" leftSection={<IconRotateClockwise size={16} stroke={1.6} />} loading={busy} onClick={resume}>
                    Resume
                  </Button>
                </Tooltip>
              ) : null}
              <Button
                leftSection={<IconPlayerPlay size={16} stroke={1.8} />}
                loading={busy}
                onClick={start}
                disabled={requireProject && !projectId}
              >
                Start
              </Button>
            </>
          )}
        </Group>
      </Group>
      {!running && !compact ? (
        <Group mt="md" gap="sm" grow wrap="wrap" align="flex-end">
          <Select
            label="Project"
            placeholder={requireProject ? 'Pick a project' : 'No project'}
            searchable
            clearable={!requireProject}
            data={projectData(projects)}
            value={projectId}
            onChange={(v) => {
              setProjectId(v);
              setActivityId(null);
            }}
            nothingFoundMessage="No project matches"
            miw={200}
          />
          <Select
            label="Activity"
            placeholder="Optional"
            clearable
            data={activityData(activities, projectId)}
            value={activityId}
            onChange={setActivityId}
            miw={160}
          />
          <TextInput
            label="What are you working on?"
            placeholder="Optional note"
            value={note}
            onChange={(e) => setNote(e.currentTarget.value)}
            maxLength={500}
            miw={200}
          />
        </Group>
      ) : null}
      {running && !compact ? (
        <TextInput
          mt="md"
          label="Note"
          placeholder="Add a note before you stop"
          value={note}
          onChange={(e) => setNote(e.currentTarget.value)}
          maxLength={500}
        />
      ) : null}
    </Paper>
  );
}

const hm = (iso) => (iso ? fmtTime(iso) : '');

/** Add or edit one manual entry: start and end times, or just a duration. */
export function EntryModal({ opened, onClose, entry, defaultDate, onSaved }) {
  const { projects, activities } = useWorkOptions();
  const [mode, setMode] = useState('range');
  const [date, setDate] = useState(defaultDate);
  const [start, setStart] = useState('09:00');
  const [end, setEnd] = useState('10:00');
  const [duration, setDuration] = useState(60);
  const [projectId, setProjectId] = useState(null);
  const [activityId, setActivityId] = useState(null);
  const [note, setNote] = useState('');
  const [billable, setBillable] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!opened) return;
    if (entry) {
      setMode(entry.started_at ? 'range' : 'duration');
      setDate(entry.local_date);
      setStart(hm(entry.started_at) || '09:00');
      setEnd(hm(entry.ended_at) || '10:00');
      setDuration(entry.duration_min ?? 60);
      setProjectId(entry.project_id);
      setActivityId(entry.activity_id);
      setNote(entry.note ?? '');
      setBillable(entry.billable);
    } else {
      setMode('range');
      setDate(defaultDate);
      setStart('09:00');
      setEnd('10:00');
      setDuration(60);
      setProjectId(null);
      setActivityId(null);
      setNote('');
      setBillable(null);
    }
  }, [opened, entry, defaultDate]);

  const project = projects.find((p) => p.id === projectId);
  const running = !!entry && entry.duration_min === null;
  const save = async () => {
    if (!date) return;
    const body = { date, projectId, activityId, note: note.trim() || null, ...(billable === null ? {} : { billable }) };
    if (!running) {
      if (mode === 'range') {
        body.start = start;
        body.end = end;
      } else body.durationMin = Number(duration);
    }
    setBusy(true);
    try {
      if (entry) await timeApi.updateEntry(entry.id, running ? { projectId, activityId, note: body.note ?? null } : body);
      else await timeApi.addEntry(body);
      toast.ok(entry ? 'Entry updated' : 'Time added');
      onSaved();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal opened={opened} onClose={onClose} title={entry ? 'Edit time' : 'Add time'} centered radius="lg">
      <Stack gap="sm">
        {!running ? (
          <>
            <DateField label="Day" value={date} onChange={setDate} maxDate={todayNpt()} required />
            <SegmentedControl
              value={mode}
              onChange={(v) => setMode(v)}
              data={[
                { value: 'range', label: 'Start and end' },
                { value: 'duration', label: 'Duration' },
              ]}
            />
            {mode === 'range' ? (
              <Group grow>
                <TextInput type="time" label="Start" value={start} onChange={(e) => setStart(e.currentTarget.value)} required />
                <TextInput type="time" label="End" value={end} onChange={(e) => setEnd(e.currentTarget.value)} required />
              </Group>
            ) : (
              <NumberInput
                label="Duration"
                suffix=" min"
                min={1}
                max={1440}
                value={duration}
                onChange={setDuration}
                description={typeof duration === 'number' ? minutesToHm(duration) : undefined}
              />
            )}
          </>
        ) : (
          <Text size="sm" c="dimmed">
            The timer is still running. You can change what it is for; stop it to change its times.
          </Text>
        )}
        <Select
          label="Project"
          placeholder="No project"
          searchable
          clearable
          data={projectData(projects)}
          value={projectId}
          onChange={(v) => {
            setProjectId(v);
            setActivityId(null);
            setBillable(null);
          }}
          nothingFoundMessage="No project matches"
        />
        <Select
          label="Activity"
          placeholder="Optional"
          clearable
          data={activityData(activities, projectId)}
          value={activityId}
          onChange={setActivityId}
        />
        <TextInput label="Note" value={note} onChange={(e) => setNote(e.currentTarget.value)} maxLength={500} />
        <Checkbox
          label="Billable"
          checked={billable ?? project?.billable ?? false}
          onChange={(e) => setBillable(e.currentTarget.checked)}
        />
        <Group justify="flex-end" mt="xs">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!date}>
            {entry ? 'Save' : 'Add time'}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

const FLAG = {
  over: 'More time logged than attended',
  under: 'Less time logged than attended',
  no_attendance: 'Time logged on a day with no attendance',
};

/** A week of entries, day by day, with the attendance comparison. Editing actions appear only when handlers are given. */
export function WeekDays({ days, onEdit, onDelete, onAdd }) {
  const today = todayNpt();
  return (
    <Paper withBorder radius="lg" style={{ overflow: 'hidden' }}>
      {days.map((d, i) => (
        <Box
          key={d.date}
          px="md"
          py="sm"
          style={{ borderTop: i ? '1px solid var(--hj-line-soft)' : undefined, opacity: d.date > today ? 0.55 : 1 }}
        >
          <Group justify="space-between" wrap="nowrap" gap="sm">
            <Group gap="xs" wrap="nowrap" style={{ minWidth: 0 }}>
              <Text size="sm" fw={600} truncate>
                {fmtDateLong(d.date)}
              </Text>
              {d.flag ? (
                <Tooltip label={`${FLAG[d.flag]}${d.attendanceMin !== null ? ` (attended ${minutesToHm(d.attendanceMin)})` : ''}`}>
                  <IconAlertTriangle size={15} stroke={1.7} color="var(--hj-warn)" aria-label={FLAG[d.flag]} />
                </Tooltip>
              ) : null}
            </Group>
            <Group gap="md" wrap="nowrap">
              <Text size="xs" c="dimmed" visibleFrom="xs">
                Attended {d.attendanceMin === null ? '—' : hhmmOf(d.attendanceMin)}
              </Text>
              <Text size="sm" ff="monospace" fw={600}>
                {hhmmOf(d.roundedMin)}
              </Text>
              {onAdd && d.date <= today ? (
                <Button size="compact-xs" variant="subtle" onClick={() => onAdd(d.date)}>
                  Add
                </Button>
              ) : null}
            </Group>
          </Group>
          {d.entries.length ? (
            <Stack gap={4} mt={6}>
              {d.entries.map((e) => (
                <Group key={e.id} justify="space-between" wrap="nowrap" gap="xs">
                  <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
                    <ProjectDot color={e.project_color} />
                    <Text size="sm" truncate>
                      {e.project_name ?? 'No project'}
                      {e.activity_name ? (
                        <Text span c="dimmed">
                          {' '}
                          · {e.activity_name}
                        </Text>
                      ) : null}
                      {e.note ? (
                        <Text span c="dimmed">
                          {' '}
                          · {e.note}
                        </Text>
                      ) : null}
                    </Text>
                    {e.billable ? (
                      <Badge size="xs" variant="light" color="brand">
                        Billable
                      </Badge>
                    ) : null}
                    {e.auto_stopped ? (
                      <Badge size="xs" variant="light" color="orange">
                        Auto-stopped
                      </Badge>
                    ) : null}
                  </Group>
                  <Group gap={6} wrap="nowrap">
                    <Text size="xs" c="dimmed" visibleFrom="sm">
                      {e.started_at ? `${fmtTime(e.started_at)}–${e.ended_at ? fmtTime(e.ended_at) : 'now'}` : 'Duration'}
                    </Text>
                    <Text size="sm" ff="monospace">
                      {e.duration_min === null ? 'running' : hhmmOf(e.duration_min)}
                    </Text>
                    {onEdit || onDelete ? (
                      <Menu position="bottom-end" withinPortal>
                        <Menu.Target>
                          <ActionIcon variant="subtle" color="gray" size="sm" aria-label="Entry actions">
                            <IconDots size={14} />
                          </ActionIcon>
                        </Menu.Target>
                        <Menu.Dropdown>
                          {onEdit ? (
                            <Menu.Item leftSection={<IconPencil size={14} />} onClick={() => onEdit(e)}>
                              Edit
                            </Menu.Item>
                          ) : null}
                          {onDelete && e.duration_min !== null ? (
                            <Menu.Item color="red" leftSection={<IconTrash size={14} />} onClick={() => onDelete(e)}>
                              Delete
                            </Menu.Item>
                          ) : null}
                        </Menu.Dropdown>
                      </Menu>
                    ) : null}
                  </Group>
                </Group>
              ))}
            </Stack>
          ) : null}
        </Box>
      ))}
    </Paper>
  );
}
