import {
  ActionIcon,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Collapse,
  Drawer,
  Group,
  Paper,
  Progress,
  Select,
  Stack,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { IconMessageCircle, IconPlayerSkipForward, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ASSIGNEE_LABEL, lifecycle } from '../../api/lifecycle';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { fmtAd, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 };

export function dueTone(t) {
  if (t.status !== 'open' || !t.due_date) return null;
  const today = todayNpt();
  if (t.due_date < today) return { color: 'red', label: `Overdue, was due ${fmtAd(t.due_date)}` };
  if (t.due_date === today) return { color: 'orange', label: 'Due today' };
  return { color: 'gray', label: `Due ${fmtAd(t.due_date)}` };
}

function Comments({ task }) {
  const q = useQuery(() => lifecycle.comments(task.id), [task.id]);
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true);
    try {
      await lifecycle.addComment(task.id, body.trim());
      setBody('');
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap={6} mt={6} pl={30}>
      {(q.data?.comments ?? []).map((c) => (
        <Text key={c.id} size="xs">
          <Text span fw={600}>
            {c.author_name}
          </Text>{' '}
          <Text span c="dimmed">
            {relativeTime(c.created_at)}
          </Text>
          <br />
          {c.body}
        </Text>
      ))}
      <Group gap="xs" wrap="nowrap" align="flex-end">
        <Textarea
          size="xs"
          placeholder="Add a comment"
          value={body}
          onChange={(e) => setBody(e.currentTarget.value)}
          autosize
          minRows={1}
          style={{ flex: 1 }}
        />
        <Button size="xs" variant="default" loading={busy} disabled={!body.trim()} onClick={send}>
          Send
        </Button>
      </Group>
    </Stack>
  );
}

/** One checklist item: tick it off if it is yours, see who owns it and when it is due, discuss it. */
export function TaskRow({ task, onChanged, canSkip }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const due = dueTone(task);
  const set = async (status) => {
    setBusy(true);
    try {
      const r = await lifecycle.updateTask(task.id, { status });
      if (r.caseCompleted) toast.ok('Checklist complete.');
      onChanged();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const owner = task.assignee_type === 'user' ? (task.assignee_name ?? 'Named person') : ASSIGNEE_LABEL[task.assignee_type];
  return (
    <div>
      <Group gap="sm" wrap="nowrap" align="flex-start">
        <Checkbox
          mt={2}
          checked={task.status !== 'open'}
          disabled={busy || task.can_complete === false}
          onChange={(e) => set(e.currentTarget.checked ? 'done' : 'open')}
          aria-label={`Mark "${task.title}" done`}
        />
        <Stack gap={2} style={{ flex: 1, minWidth: 0 }}>
          <Text size="sm" fw={500} {...(task.status !== 'open' ? { td: 'line-through', c: 'dimmed' } : {})}>
            {task.link && task.status === 'open' ? (
              <Anchor component={Link} to={task.link} inherit>
                {task.title}
              </Anchor>
            ) : (
              task.title
            )}
          </Text>
          {task.description ? (
            <Text size="xs" c="dimmed">
              {task.description}
            </Text>
          ) : null}
          <Group gap={6} wrap="wrap">
            <Badge size="xs" variant="light" color="gray">
              {task.department ? `${task.department}, ${owner}` : owner}
            </Badge>
            {due ? (
              <Text size="xs" c={due.color === 'gray' ? 'dimmed' : due.color}>
                {due.label}
              </Text>
            ) : null}
            {task.status === 'done' ? (
              <Text size="xs" c="dimmed">
                Done{task.done_by_name ? ` by ${task.done_by_name}` : ''}
              </Text>
            ) : null}
            {task.status === 'skipped' ? (
              <Text size="xs" c="dimmed">
                Skipped
              </Text>
            ) : null}
            {task.note ? (
              <Text size="xs" c="dimmed">
                “{task.note}”
              </Text>
            ) : null}
          </Group>
        </Stack>
        <Group gap={2} wrap="nowrap">
          {canSkip && task.status === 'open' ? (
            <Tooltip label="Skip (not needed)">
              <ActionIcon variant="subtle" color="gray" onClick={() => set('skipped')} loading={busy} aria-label="Skip task">
                <IconPlayerSkipForward {...ICON} />
              </ActionIcon>
            </Tooltip>
          ) : null}
          <Tooltip label="Comments">
            <ActionIcon variant="subtle" color="gray" onClick={() => setOpen((o) => !o)} aria-label="Comments">
              <IconMessageCircle {...ICON} />
              {task.comments ? (
                <Text size="10px" ml={2}>
                  {task.comments}
                </Text>
              ) : null}
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>
      <Collapse expanded={open}>{open ? <Comments task={task} /> : null}</Collapse>
    </div>
  );
}

/** A checklist (onboarding or exit clearance) in a side drawer. */
export function CaseDrawer({ caseId, onClose, onChanged }) {
  const { hasRole } = useAuth();
  const hr = hasRole('hr');
  const q = useQuery(() => (caseId ? lifecycle.case(caseId) : Promise.resolve(null)), [caseId]);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState({ title: '', assigneeType: 'hr', dueDate: null });
  const d = q.data;
  const tasks = d?.tasks ?? [];
  const done = tasks.filter((t) => t.status !== 'open').length;
  const changed = async () => {
    await q.reload();
    onChanged?.();
  };
  const add = async () => {
    if (!caseId) return;
    try {
      await lifecycle.addTask(caseId, { title: draft.title.trim(), assigneeType: draft.assigneeType, dueDate: draft.dueDate });
      setDraft({ title: '', assigneeType: 'hr', dueDate: null });
      setAdding(false);
      await changed();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <Drawer
      opened={!!caseId}
      onClose={onClose}
      position="right"
      size="lg"
      title={
        d ? (
          <div>
            <Text fw={600}>
              {d.case.kind === 'onboarding' ? 'Onboarding' : 'Exit clearance'}: {d.employee.name_en}
            </Text>
            <Text size="xs" c="dimmed">
              {d.case.kind === 'onboarding' ? `Joins ${fmtAd(d.case.anchor_date)}` : `Last working day ${fmtAd(d.case.anchor_date)}`}
            </Text>
          </div>
        ) : (
          'Checklist'
        )
      }
    >
      {d ? (
        <Stack gap="md">
          <div>
            <Group justify="space-between" mb={4}>
              <Text size="sm" c="dimmed">
                {done} of {tasks.length} done
              </Text>
              <Badge variant="light" color={d.case.status === 'active' ? 'blue' : d.case.status === 'completed' ? 'teal' : 'gray'}>
                {d.case.status === 'active' ? 'In progress' : d.case.status === 'completed' ? 'Complete' : 'Cancelled'}
              </Badge>
            </Group>
            <Progress value={tasks.length ? (done / tasks.length) * 100 : 0} size="sm" radius="xl" />
          </div>
          {tasks.length ? (
            <Paper withBorder p="md">
              <Stack gap="md">
                {tasks.map((t) => (
                  <TaskRow key={t.id} task={t} onChanged={changed} canSkip={hr && d.case.status === 'active'} />
                ))}
              </Stack>
            </Paper>
          ) : (
            <EmptyState compact title="No tasks in this checklist" {...(hr ? { hint: 'Add the first task below.' } : {})} />
          )}
          {hr && d.case.status === 'active' ? (
            adding ? (
              <Paper withBorder p="md">
                <Stack gap="sm">
                  <TextInput
                    label="Task"
                    value={draft.title}
                    onChange={(e) => setDraft({ ...draft, title: e.currentTarget.value })}
                    data-autofocus
                  />
                  <Group grow align="flex-end">
                    <Select
                      label="Who does it"
                      value={draft.assigneeType}
                      allowDeselect={false}
                      onChange={(v) => v && setDraft({ ...draft, assigneeType: v })}
                      data={['hr', 'manager', 'employee'].map((k) => ({ value: k, label: ASSIGNEE_LABEL[k] }))}
                    />
                    <DateField label="Due" value={draft.dueDate} onChange={(v) => setDraft({ ...draft, dueDate: v })} />
                  </Group>
                  <Group justify="flex-end">
                    <Button variant="default" onClick={() => setAdding(false)}>
                      Cancel
                    </Button>
                    <Button disabled={!draft.title.trim()} onClick={add}>
                      Add task
                    </Button>
                  </Group>
                </Stack>
              </Paper>
            ) : (
              <Button
                variant="subtle"
                leftSection={<IconPlus {...ICON} />}
                onClick={() => setAdding(true)}
                style={{ alignSelf: 'flex-start' }}
              >
                Add a task
              </Button>
            )
          ) : null}
        </Stack>
      ) : null}
    </Drawer>
  );
}
