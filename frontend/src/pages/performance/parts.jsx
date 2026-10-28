import {
  ActionIcon,
  Badge,
  Button,
  Divider,
  Drawer,
  Group,
  MultiSelect,
  NumberInput,
  Paper,
  Progress,
  Select,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  Tooltip,
} from '@mantine/core';
import { IconPlus, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { performance as perfApi } from '../../api/performance';
import { DateField } from '../../components/BsDatePicker';
import { fmtDateShort, fmtMoney, fmtTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export const ICON = { size: 16, stroke: 1.6 };

export const PHASE_LABEL = {
  draft: 'Draft',
  self_review: 'Self review',
  manager_review: 'Manager review',
  peer_review: 'Peer review',
  calibration: 'Calibration',
  released: 'Released',
  closed: 'Closed',
};
export const PHASE_COLOR = {
  draft: 'gray',
  self_review: 'blue',
  manager_review: 'grape',
  peer_review: 'cyan',
  calibration: 'orange',
  released: 'brand',
  closed: 'gray',
};

export function PhaseBadge({ phase }) {
  return (
    <Badge variant="light" color={PHASE_COLOR[phase]}>
      {PHASE_LABEL[phase]}
    </Badge>
  );
}

/** "28 Ashwin, 10:30" in Nepal time. */
export function fmtDateTime(iso) {
  if (!iso) return '—';
  const npt = new Date(Date.parse(iso) + 345 * 60_000).toISOString().slice(0, 10);
  return `${fmtDateShort(npt)}, ${fmtTime(iso)}`;
}

/** A Nepal-time date and HH:MM as an ISO instant with the +05:45 offset. */
export function nptInstant(date, time) {
  return `${date}T${time.length === 5 ? `${time}:00` : time}+05:45`;
}

/** The Nepal-time date and HH:MM of an instant, for editing. */
export function splitNpt(iso) {
  const s = new Date(Date.parse(iso) + 345 * 60_000).toISOString();
  return { date: s.slice(0, 10), time: s.slice(11, 16) };
}

export function ratingLabel(n, labels) {
  if (n === null || n === undefined || n === '') return 'Not rated';
  const v = Number(n);
  const label = labels[Math.min(5, Math.max(1, Math.round(v))) - 1] ?? '';
  return `${Number.isInteger(v) ? v : v.toFixed(2)} · ${label}`;
}

/** 1–5 rating picker with the cycle's labels as tooltips. */
export function RatingPicker({ value, onChange, labels, disabled }) {
  return (
    <Group gap={4} wrap="nowrap">
      {[1, 2, 3, 4, 5].map((n) => (
        <Tooltip key={n} label={labels[n - 1]} withArrow>
          <Button
            size="compact-sm"
            variant={value === n ? 'filled' : 'default'}
            onClick={() => onChange(n)}
            disabled={!!disabled}
            aria-label={`${n}: ${labels[n - 1]}`}
            miw={34}
          >
            {n}
          </Button>
        </Tooltip>
      ))}
    </Group>
  );
}

export function GoalProgress({ value, size = 'sm' }) {
  const v = Math.round(Number(value));
  const color = v >= 100 ? 'brand' : v >= 60 ? 'teal' : v >= 30 ? 'yellow' : 'orange';
  return (
    <Group gap="xs" wrap="nowrap">
      <Progress value={v} color={color} size={size} radius="xl" style={{ flex: 1, minWidth: 60 }} aria-label={`${v}% done`} />
      <Text size="xs" c="dimmed" w={36} ta="right" ff="monospace">
        {v}%
      </Text>
    </Group>
  );
}

export function krValue(k) {
  if (k.measure === 'boolean') return Number(k.current_value) >= 1 ? 'Done' : 'Not done';
  const f = (x) => (k.measure === 'currency' ? fmtMoney(String(x)) : Number(x).toLocaleString('en-US'));
  const unit = k.measure === 'percent' ? '%' : k.unit ? ` ${k.unit}` : '';
  return `${f(Number(k.current_value))}${unit} of ${f(Number(k.target_value))}${unit}`;
}

const CONFIDENCE = {
  1: { label: 'Off track', color: 'red' },
  2: { label: 'At risk', color: 'yellow' },
  3: { label: 'On track', color: 'teal' },
};
export function ConfidenceBadge({ value }) {
  if (!value) return null;
  const c = CONFIDENCE[value];
  return (
    <Badge size="sm" variant="light" color={c.color}>
      {c.label}
    </Badge>
  );
}

/** One goal as a card: title, owner, due date, progress and its key results. */
export function GoalCard({ g, onOpen, showOwner }) {
  return (
    <Paper withBorder radius="lg" p="md" style={{ cursor: 'pointer' }} onClick={() => onOpen(g)}>
      <Stack gap={8}>
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <div style={{ minWidth: 0 }}>
            <Text fw={600} size="sm" lineClamp={2}>
              {g.title}
            </Text>
            <Text size="xs" c="dimmed">
              {[
                showOwner && g.employee_name ? g.employee_name : null,
                g.scope === 'department' ? g.org_unit_name : g.scope === 'company' ? 'Company goal' : null,
                g.due_date ? `Due ${fmtDateShort(g.due_date)}` : null,
                Number(g.weight) > 0 ? `Weight ${Number(g.weight)}%` : null,
              ]
                .filter(Boolean)
                .join(' · ') || 'No due date'}
            </Text>
          </div>
          <Group gap={4} wrap="nowrap">
            {g.status !== 'active' ? (
              <Badge size="sm" variant="light" color={g.status === 'completed' ? 'brand' : 'gray'}>
                {g.status}
              </Badge>
            ) : null}
            <ConfidenceBadge value={g.last_checkin?.confidence} />
          </Group>
        </Group>
        <GoalProgress value={g.progress} />
        {g.key_results.length ? (
          <Stack gap={2}>
            {g.key_results.slice(0, 4).map((k) => (
              <Group key={k.id} justify="space-between" wrap="nowrap" gap="xs">
                <Text size="xs" truncate>
                  {k.title}
                </Text>
                <Text size="xs" c="dimmed" style={{ flex: 'none' }}>
                  {krValue(k)}
                </Text>
              </Group>
            ))}
          </Stack>
        ) : null}
        {g.parent_title ? (
          <Text size="xs" c="dimmed">
            Supports: {g.parent_title}
          </Text>
        ) : null}
      </Stack>
    </Paper>
  );
}

const MEASURES = [
  { value: 'number', label: 'Number' },
  { value: 'percent', label: 'Percent' },
  { value: 'currency', label: 'Amount (NPR)' },
  { value: 'boolean', label: 'Done / not done' },
];

/** Create a goal: who it is for (self, a report, a department or the company), key results, alignment. */
export function GoalDrawer({ opened, onClose, onSaved, employees, allowCompany, defaultEmployeeId }) {
  const [scope, setScope] = useState('individual');
  const [employeeId, setEmployeeId] = useState(defaultEmployeeId ?? null);
  const [orgUnitId, setOrgUnitId] = useState(null);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [weight, setWeight] = useState('');
  const [dueDate, setDueDate] = useState(null);
  const [parentId, setParentId] = useState(null);
  const [krs, setKrs] = useState([]);
  const [busy, setBusy] = useState(false);
  const parents = useQuery(() => perfApi.goals({ scope: 'company' }), [opened], { enabled: opened });
  const depts = useQuery(() => perfApi.departments(), [opened], { enabled: opened && !!allowCompany });
  const reset = () => {
    setTitle('');
    setDescription('');
    setWeight('');
    setDueDate(null);
    setParentId(null);
    setKrs([]);
    setScope('individual');
  };
  const save = async () => {
    setBusy(true);
    try {
      await perfApi.createGoal({
        scope,
        title,
        description: description || null,
        weight: weight === '' ? 0 : weight,
        dueDate,
        parentId,
        ...(scope === 'individual' && employeeId ? { employeeId } : {}),
        ...(scope === 'department' ? { orgUnitId } : {}),
        keyResults: krs.filter((k) => k.title.trim()).map((k) => ({ ...k, unit: k.unit || null })),
      });
      toast.ok('Goal saved.');
      reset();
      onSaved();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const setKr = (i, patch) => setKrs((xs) => xs.map((k, j) => (j === i ? { ...k, ...patch } : k)));
  return (
    <Drawer opened={opened} onClose={onClose} title="New goal" position="right" size="lg">
      <Stack gap="sm">
        {allowCompany ? (
          <SegmentedControl
            value={scope}
            onChange={(v) => setScope(v)}
            data={[
              { value: 'individual', label: 'Person' },
              { value: 'department', label: 'Department' },
              { value: 'company', label: 'Company' },
            ]}
          />
        ) : null}
        {scope === 'individual' && employees ? (
          <Select
            label="For"
            placeholder="Myself"
            clearable
            searchable
            value={employeeId}
            onChange={setEmployeeId}
            data={employees.map((e) => ({ value: e.id, label: `${e.name_en}${e.designation ? `, ${e.designation}` : ''}` }))}
          />
        ) : null}
        {scope === 'department' ? (
          <Select
            label="Department"
            required
            value={orgUnitId}
            onChange={setOrgUnitId}
            data={(depts.data?.departments ?? []).map((d) => ({ value: d.id, label: d.name_en }))}
          />
        ) : null}
        <TextInput
          label="Goal"
          required
          placeholder="e.g. Cut month-end close to 3 days"
          value={title}
          onChange={(e) => setTitle(e.currentTarget.value)}
        />
        <Textarea label="Why it matters" autosize minRows={2} value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
        <SimpleGrid cols={{ base: 1, xs: 2 }}>
          <DateField label="Due date" value={dueDate} onChange={setDueDate} />
          <NumberInput
            label="Weight in review (%)"
            min={0}
            max={100}
            value={weight}
            onChange={(v) => setWeight(v === '' ? '' : Number(v))}
          />
        </SimpleGrid>
        <Select
          label="Supports (optional)"
          clearable
          searchable
          value={parentId}
          onChange={setParentId}
          data={(parents.data?.goals ?? []).map((g) => ({
            value: g.id,
            label: `${g.scope === 'company' ? 'Company' : (g.org_unit_name ?? 'Department')}: ${g.title}`,
          }))}
        />
        <Divider label="Key results" labelPosition="left" />
        {krs.map((k, i) => (
          <Paper key={i} withBorder p="sm" radius="md">
            <Stack gap={6}>
              <Group gap="xs" wrap="nowrap" align="flex-end">
                <TextInput
                  style={{ flex: 1 }}
                  label="Key result"
                  placeholder="e.g. Close in 3 working days"
                  value={k.title}
                  onChange={(e) => setKr(i, { title: e.currentTarget.value })}
                />
                <ActionIcon
                  variant="subtle"
                  color="gray"
                  onClick={() => setKrs((xs) => xs.filter((_, j) => j !== i))}
                  aria-label="Remove key result"
                  mb={4}
                >
                  <IconTrash {...ICON} />
                </ActionIcon>
              </Group>
              <SimpleGrid cols={{ base: 2, sm: 4 }}>
                <Select
                  label="Measure"
                  value={k.measure}
                  onChange={(v) => setKr(i, { measure: v ?? 'number' })}
                  data={MEASURES}
                  allowDeselect={false}
                />
                {k.measure !== 'boolean' ? (
                  <>
                    <NumberInput label="Start" value={k.startValue} onChange={(v) => setKr(i, { startValue: Number(v) || 0 })} />
                    <NumberInput label="Target" value={k.targetValue} onChange={(v) => setKr(i, { targetValue: Number(v) || 0 })} />
                    {k.measure === 'number' ? (
                      <TextInput
                        label="Unit"
                        placeholder="days"
                        value={k.unit}
                        onChange={(e) => setKr(i, { unit: e.currentTarget.value })}
                      />
                    ) : null}
                  </>
                ) : null}
              </SimpleGrid>
            </Stack>
          </Paper>
        ))}
        <Button
          variant="default"
          leftSection={<IconPlus {...ICON} />}
          onClick={() => setKrs((xs) => [...xs, { title: '', measure: 'number', startValue: 0, targetValue: 100, unit: '' }])}
        >
          Add key result
        </Button>
        <Text size="xs" c="dimmed">
          Without key results, progress is updated by hand at each check-in.
        </Text>
        <Group justify="flex-end" mt="sm">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!title.trim() || (scope === 'department' && !orgUnitId)}>
            Save goal
          </Button>
        </Group>
      </Stack>
    </Drawer>
  );
}

/** Goal detail: key results with a check-in form, history, and the edit actions the viewer is allowed. */
export function GoalDetail({ goalId, onClose, onChanged }) {
  const q = useQuery(() => (goalId ? perfApi.goal(goalId) : Promise.resolve(null)), [goalId]);
  const [krId, setKrId] = useState(null);
  const [value, setValue] = useState('');
  const [progress, setProgress] = useState('');
  const [confidence, setConfidence] = useState('3');
  const [comment, setComment] = useState('');
  const [busy, setBusy] = useState(false);
  const d = q.data;
  const g = d?.goal;
  const reload = async () => {
    await q.reload();
    onChanged();
  };
  const checkin = async () => {
    if (!g) return;
    setBusy(true);
    try {
      const kr = g.key_results.find((k) => k.id === krId);
      await perfApi.checkin(g.id, {
        ...(krId ? { keyResultId: krId } : {}),
        ...(krId && value !== '' ? { value: kr?.measure === 'boolean' ? (value ? 1 : 0) : value } : {}),
        ...(!g.key_results.length && progress !== '' ? { progress } : {}),
        confidence: Number(confidence),
        ...(comment.trim() ? { comment } : {}),
      });
      toast.ok('Check-in saved.');
      setValue('');
      setProgress('');
      setComment('');
      await reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const setStatus = async (status) => {
    if (!g) return;
    try {
      await perfApi.updateGoal(g.id, { status });
      toast.ok(status === 'completed' ? 'Marked complete.' : 'Reopened.');
      await reload();
    } catch (e) {
      toast.err(e);
    }
  };
  const cancel = async () => {
    if (!g) return;
    try {
      await perfApi.cancelGoal(g.id);
      toast.ok('Goal cancelled.');
      onChanged();
      onClose();
    } catch (e) {
      toast.err(e);
    }
  };
  const selectedKr = g?.key_results.find((k) => k.id === krId);
  return (
    <Drawer opened={!!goalId} onClose={onClose} title={g?.title ?? 'Goal'} position="right" size="lg">
      {!g ? (
        <Text c="dimmed" size="sm">
          {q.loading ? 'Loading…' : (q.error ?? '')}
        </Text>
      ) : (
        <Stack gap="md">
          <Group gap="xs">
            <Badge variant="light">
              {g.scope === 'individual' ? g.employee_name : g.scope === 'company' ? 'Company' : g.org_unit_name}
            </Badge>
            {g.due_date ? (
              <Badge variant="light" color="gray">
                Due {fmtDateShort(g.due_date)}
              </Badge>
            ) : null}
            {Number(g.weight) > 0 ? (
              <Badge variant="light" color="gray">
                Weight {Number(g.weight)}%
              </Badge>
            ) : null}
            {g.status !== 'active' ? (
              <Badge variant="light" color={g.status === 'completed' ? 'brand' : 'gray'}>
                {g.status}
              </Badge>
            ) : null}
          </Group>
          {g.description ? <Text size="sm">{g.description}</Text> : null}
          {g.parent_title ? (
            <Text size="sm" c="dimmed">
              Supports: {g.parent_title}
            </Text>
          ) : null}
          <GoalProgress value={g.progress} size="md" />
          {g.key_results.length ? (
            <Stack gap={6}>
              {g.key_results.map((k) => (
                <Paper key={k.id} withBorder radius="md" p="xs">
                  <Group justify="space-between" wrap="nowrap">
                    <Text size="sm">{k.title}</Text>
                    <Text size="xs" c="dimmed" style={{ flex: 'none' }}>
                      {krValue(k)}
                    </Text>
                  </Group>
                </Paper>
              ))}
            </Stack>
          ) : null}
          {d.children.length ? (
            <Stack gap={4}>
              <Text size="sm" fw={600}>
                Goals supporting this
              </Text>
              {d.children.map((c) => (
                <Group key={c.id} justify="space-between">
                  <Text size="sm">{c.title}</Text>
                  <Text size="xs" c="dimmed">
                    {Math.round(Number(c.progress))}%
                  </Text>
                </Group>
              ))}
            </Stack>
          ) : null}
          {d.canEdit && g.status !== 'cancelled' ? (
            <Paper withBorder radius="md" p="sm">
              <Stack gap="xs">
                <Text fw={600} size="sm">
                  Check in
                </Text>
                {g.key_results.length ? (
                  <SimpleGrid cols={{ base: 1, xs: 2 }}>
                    <Select
                      label="Key result"
                      value={krId}
                      onChange={setKrId}
                      clearable
                      data={g.key_results.map((k) => ({ value: k.id, label: k.title }))}
                    />
                    {selectedKr?.measure === 'boolean' ? (
                      <Switch mt={28} label="Done" checked={value === 1} onChange={(e) => setValue(e.currentTarget.checked ? 1 : 0)} />
                    ) : (
                      <NumberInput label="New value" disabled={!krId} value={value} onChange={(v) => setValue(v === '' ? '' : Number(v))} />
                    )}
                  </SimpleGrid>
                ) : (
                  <NumberInput
                    label="Progress (%)"
                    min={0}
                    max={100}
                    value={progress}
                    onChange={(v) => setProgress(v === '' ? '' : Number(v))}
                  />
                )}
                <SegmentedControl
                  size="xs"
                  value={confidence}
                  onChange={setConfidence}
                  data={[
                    { value: '3', label: 'On track' },
                    { value: '2', label: 'At risk' },
                    { value: '1', label: 'Off track' },
                  ]}
                />
                <Textarea
                  placeholder="What changed? Any blockers?"
                  autosize
                  minRows={2}
                  value={comment}
                  onChange={(e) => setComment(e.currentTarget.value)}
                />
                <Group justify="space-between">
                  <Group gap="xs">
                    {g.status === 'active' ? (
                      <Button size="xs" variant="subtle" onClick={() => setStatus('completed')}>
                        Mark complete
                      </Button>
                    ) : (
                      <Button size="xs" variant="subtle" onClick={() => setStatus('active')}>
                        Reopen
                      </Button>
                    )}
                    <Button size="xs" variant="subtle" color="red" onClick={cancel}>
                      Cancel goal
                    </Button>
                  </Group>
                  <Button size="sm" onClick={checkin} loading={busy}>
                    Save check-in
                  </Button>
                </Group>
              </Stack>
            </Paper>
          ) : null}
          <Stack gap={6}>
            <Text fw={600} size="sm">
              History
            </Text>
            {d.checkins.length === 0 ? (
              <Text size="sm" c="dimmed">
                No check-ins yet.
              </Text>
            ) : (
              d.checkins.map((c) => (
                <Paper key={c.id} withBorder radius="md" p="xs">
                  <Group justify="space-between" wrap="nowrap" gap="xs">
                    <Text size="xs" c="dimmed">
                      {c.author_name ?? 'Someone'} · {fmtDateTime(c.created_at)}
                    </Text>
                    <Group gap={4}>
                      <ConfidenceBadge value={c.confidence} />
                      {c.progress !== null ? (
                        <Badge size="sm" variant="light" color="gray">
                          {Math.round(Number(c.progress))}%
                        </Badge>
                      ) : null}
                    </Group>
                  </Group>
                  {c.key_result_title ? (
                    <Text size="xs">
                      {c.key_result_title}
                      {c.value !== null ? `: ${Number(c.value).toLocaleString('en-US')}` : ''}
                    </Text>
                  ) : null}
                  {c.comment ? (
                    <Text size="sm" mt={2}>
                      {c.comment}
                    </Text>
                  ) : null}
                </Paper>
              ))
            )}
          </Stack>
        </Stack>
      )}
    </Drawer>
  );
}

/** Pick colleagues (feedback givers, peer reviewers). */
export function ColleaguePicker({ value, onChange, exclude = [], label = 'Colleagues', max = 10, data }) {
  const q = useQuery(() => (data ? Promise.resolve({ colleagues: data }) : perfApi.colleagues()), [data]);
  return (
    <MultiSelect
      label={label}
      searchable
      value={value}
      onChange={onChange}
      maxValues={max}
      data={(q.data?.colleagues ?? [])
        .filter((c) => !exclude.includes(c.id))
        .map((c) => ({ value: c.id, label: `${c.name_en}${c.designation ? `, ${c.designation}` : ''}` }))}
    />
  );
}

export function Section({ title, hint, actions, children }) {
  return (
    <Stack gap="sm">
      <Group justify="space-between" align="flex-end" wrap="wrap" gap="xs">
        <div>
          <Text fw={600}>{title}</Text>
          {hint ? (
            <Text size="sm" c="dimmed">
              {hint}
            </Text>
          ) : null}
        </div>
        {actions}
      </Group>
      {children}
    </Stack>
  );
}
