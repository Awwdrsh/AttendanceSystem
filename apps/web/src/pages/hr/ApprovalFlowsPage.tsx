import {
  Accordion, ActionIcon, Alert, Badge, Button, Drawer, Group, MultiSelect, NumberInput, Paper, SegmentedControl, Select, SimpleGrid, Stack, Switch, Table, Text,
  TextInput, Tooltip,
} from '@mantine/core';
import { IconArrowDown, IconArrowUp, IconCheck, IconPlus, IconRoute, IconTrash, IconUsersGroup } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { auth as authApi, employees as empApi } from '../../api';
import { flows, toStepInput, type ApprovalFlow, type ApproverKind, type FlowConditions, type FlowStep, type FlowType, type StepInput, type StepRole, type UserGroup, type VerifyProblem } from '../../api/flows';
import { people as peopleApi } from '../../api/people';
import { ApprovalChainPreview } from '../../components/ApprovalChainPreview';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { fmtMoney } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;

const KIND_OPTIONS: Array<{ value: ApproverKind; label: string }> = [
  { value: 'manager', label: 'Line manager' },
  { value: 'manager2', label: "Manager's manager" },
  { value: 'department_head', label: 'Department head' },
  { value: 'branch_head', label: 'Branch head' },
  { value: 'role', label: 'Everyone with a role' },
  { value: 'user', label: 'A named person' },
  { value: 'group', label: 'Anyone in a group' },
];
const ROLE_OPTIONS: Array<{ value: StepRole; label: string }> = [
  { value: 'hr', label: 'HR' }, { value: 'payroll_admin', label: 'Payroll admin' }, { value: 'attendance_admin', label: 'Attendance admin' },
  { value: 'recruiter', label: 'Recruiter' }, { value: 'admin', label: 'Admin' },
];
const EMPTY_CONDITIONS: FlowConditions = { employeeIds: [], groupIds: [], designationIds: [], orgUnitIds: [], branchIds: [] };

interface Lookups {
  users: Array<{ value: string; label: string }>;
  groups: Array<{ value: string; label: string }>;
  employees: Array<{ value: string; label: string }>;
  designations: Array<{ value: string; label: string }>;
  units: Array<{ value: string; label: string }>;
  branches: Array<{ value: string; label: string }>;
}

function stepText(s: FlowStep, lk: Lookups): string {
  const kind = s.kind ?? (s.role === 'manager' ? 'manager' : 'role');
  let t = KIND_OPTIONS.find((k) => k.value === kind)?.label ?? kind;
  if (kind === 'role') t = ROLE_OPTIONS.find((r) => r.value === s.role)?.label ?? s.role;
  if (kind === 'user') t = lk.users.find((u) => u.value === s.userId)?.label ?? 'A named person';
  if (kind === 'group') t = `Group: ${lk.groups.find((g) => g.value === s.groupId)?.label ?? '?'}`;
  return s.minAmount !== undefined ? `${t} (from Rs ${fmtMoney(s.minAmount, { decimals: 0 })})` : t;
}

function conditionText(c: Partial<FlowConditions>, lk: Lookups): string {
  const parts: string[] = [];
  const names = (ids: string[] | undefined, list: Array<{ value: string; label: string }>) => (ids ?? []).map((id) => list.find((x) => x.value === id)?.label ?? '?');
  if (c.employeeIds?.length) parts.push(names(c.employeeIds, lk.employees).join(', '));
  if (c.groupIds?.length) parts.push(`group ${names(c.groupIds, lk.groups).join(', ')}`);
  if (c.designationIds?.length) parts.push(names(c.designationIds, lk.designations).join(', '));
  if (c.orgUnitIds?.length) parts.push(`in ${names(c.orgUnitIds, lk.units).join(', ')}`);
  if (c.branchIds?.length) parts.push(`at ${names(c.branchIds, lk.branches).join(', ')}`);
  return parts.length ? parts.join(' · ') : 'Everyone';
}

/** Ordered approver steps: who approves at each level, with an optional amount threshold for money requests. */
function StepsEditor({ steps, onChange, money, lk }: { steps: StepInput[]; onChange: (s: StepInput[]) => void; money: boolean; lk: Lookups }) {
  const set = (i: number, patch: Partial<StepInput>) => onChange(steps.map((s, j) => (j === i ? { ...s, ...patch } : s)));
  const move = (i: number, d: -1 | 1) => { const n = [...steps]; const [x] = n.splice(i, 1); n.splice(i + d, 0, x!); onChange(n); };
  return (
    <Stack gap="xs">
      {steps.map((s, i) => (
        <Paper key={i} withBorder radius="md" p="xs">
          <Group gap="xs" align="flex-end" wrap="wrap">
            <Badge variant="light" radius="sm" mb={6}>Step {i + 1}</Badge>
            <Select label="Approved by" w={200} data={KIND_OPTIONS} value={s.kind} allowDeselect={false}
              onChange={(v) => { const kind = (v ?? 'manager') as ApproverKind; onChange(steps.map((x, j) => (j === i ? { kind, ...(x.minAmount !== undefined ? { minAmount: x.minAmount } : {}), ...(kind === 'role' ? { role: 'hr' as StepRole } : {}) } : x))); }} />
            {s.kind === 'role' ? <Select label="Role" w={170} data={ROLE_OPTIONS} value={s.role ?? 'hr'} allowDeselect={false} onChange={(v) => set(i, { role: (v ?? 'hr') as StepRole })} /> : null}
            {s.kind === 'user' ? <Select label="Person" w={220} searchable data={lk.users} value={s.userId ?? null} onChange={(v) => set(i, { ...(v ? { userId: v } : {}) })} /> : null}
            {s.kind === 'group' ? <Select label="Group" w={200} data={lk.groups} value={s.groupId ?? null} onChange={(v) => set(i, { ...(v ? { groupId: v } : {}) })} placeholder={lk.groups.length ? 'Choose a group' : 'Create a group first'} /> : null}
            {money ? (
              <NumberInput label="Only from (Rs)" w={150} min={0} thousandSeparator="," placeholder="Any amount" value={s.minAmount ?? ''}
                onChange={(v) => { const n = typeof v === 'number' ? v : Number(v); onChange(steps.map((x, j) => { if (j !== i) return x; const { minAmount: _m, ...rest } = x; return v === '' || Number.isNaN(n) ? rest : { ...rest, minAmount: n }; })); }} />
            ) : null}
            <Group gap={2} ml="auto" mb={4}>
              <ActionIcon variant="subtle" color="gray" aria-label="Move up" disabled={i === 0} onClick={() => move(i, -1)}><IconArrowUp {...ICON} /></ActionIcon>
              <ActionIcon variant="subtle" color="gray" aria-label="Move down" disabled={i === steps.length - 1} onClick={() => move(i, 1)}><IconArrowDown {...ICON} /></ActionIcon>
              <ActionIcon variant="subtle" color="red" aria-label="Remove step" disabled={steps.length === 1} onClick={() => onChange(steps.filter((_, j) => j !== i))}><IconTrash {...ICON} /></ActionIcon>
            </Group>
          </Group>
        </Paper>
      ))}
      {steps.length < 6 ? <Button variant="subtle" size="xs" leftSection={<IconPlus {...ICON} />} style={{ alignSelf: 'flex-start' }} onClick={() => onChange([...steps, { kind: 'role', role: 'hr' }])}>Add a step</Button> : null}
    </Stack>
  );
}

function DefaultChain({ t, lk, onSaved }: { t: FlowType; lk: Lookups; onSaved: () => void }) {
  const [steps, setSteps] = useState<StepInput[]>(t.defaultSteps.map(toStepInput));
  const [busy, setBusy] = useState(false);
  const dirty = JSON.stringify(steps) !== JSON.stringify(t.defaultSteps.map(toStepInput));
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>Everyone else</Text>
      <StepsEditor steps={steps} onChange={setSteps} money={t.money} lk={lk} />
      {dirty ? (
        <Group gap="xs">
          <Button size="xs" loading={busy} leftSection={<IconCheck {...ICON} />} onClick={async () => { setBusy(true); try { await flows.setDefault(t.objectType, steps); toast.ok(`${t.label}: approval chain saved.`); onSaved(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Save</Button>
          <Button size="xs" variant="default" onClick={() => setSteps(t.defaultSteps.map(toStepInput))}>Undo</Button>
        </Group>
      ) : null}
    </Stack>
  );
}

function FlowDrawer({ type, flow, lk, onClose, onSaved }: { type: FlowType; flow: ApprovalFlow | null; lk: Lookups; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(flow?.name ?? '');
  const [cond, setCond] = useState<FlowConditions>({ ...EMPTY_CONDITIONS, ...(flow?.conditions ?? {}) });
  const [steps, setSteps] = useState<StepInput[]>(flow ? flow.steps.map(toStepInput) : [{ kind: 'manager' }]);
  const [priority, setPriority] = useState<number>(flow?.priority ?? 0);
  const [busy, setBusy] = useState(false);
  const any = Object.values(cond).some((v) => v.length);
  const save = async () => {
    setBusy(true);
    try {
      if (flow) await flows.update(flow.id, { name, conditions: cond, steps, priority });
      else await flows.create({ objectType: type.objectType, name, conditions: cond, steps, priority });
      toast.ok('Flow saved.'); onSaved(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const ms = (label: string, key: keyof FlowConditions, data: Array<{ value: string; label: string }>, placeholder: string) => (
    <MultiSelect label={label} data={data} value={cond[key]} searchable clearable placeholder={cond[key].length ? undefined : placeholder} onChange={(v) => setCond({ ...cond, [key]: v })} />
  );
  return (
    <Drawer opened onClose={onClose} position="right" size="lg" title={`${flow ? 'Edit' : 'New'} flow: ${type.label}`}>
      <Stack gap="md">
        <TextInput label="Name" placeholder="e.g. Factory staff" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={100} required />
        <Text size="sm" fw={600}>Applies to requests from</Text>
        <Text size="xs" c="dimmed" mt={-10}>Leave a box empty to match anyone. When several flows match, the most specific wins: a named person, then a group, designation, department and branch.</Text>
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          {ms('Departments (and below)', 'orgUnitIds', lk.units, 'Any department')}
          {ms('Branches', 'branchIds', lk.branches, 'Any branch')}
          {ms('Designations', 'designationIds', lk.designations, 'Any designation')}
          {ms('User groups', 'groupIds', lk.groups, 'Any group')}
        </SimpleGrid>
        {ms('Specific employees', 'employeeIds', lk.employees, 'Nobody in particular')}
        {!any ? <Alert color="yellow" variant="light">With no conditions this flow replaces the default chain for everyone.</Alert> : null}
        <Text size="sm" fw={600}>Approval steps</Text>
        <StepsEditor steps={steps} onChange={setSteps} money={type.money} lk={lk} />
        <NumberInput label="Priority" description="Breaks ties between equally specific flows (higher wins)." w={200} min={-100} max={100} value={priority} onChange={(v) => setPriority(Number(v) || 0)} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!name.trim()} onClick={save}>Save flow</Button>
        </Group>
      </Stack>
    </Drawer>
  );
}

function GroupDrawer({ group, lk, onClose, onSaved }: { group: UserGroup | null; lk: Lookups; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(group?.name ?? '');
  const [description, setDescription] = useState(group?.description ?? '');
  const [members, setMembers] = useState<string[]>(group?.members.map((m) => m.id) ?? []);
  const [busy, setBusy] = useState(false);
  return (
    <Drawer opened onClose={onClose} position="right" size="md" title={group ? `Edit group: ${group.name}` : 'New user group'}>
      <Stack gap="md">
        <Text size="sm" c="dimmed">A group is a named set of people. Use it as an approval step (anyone in the group can approve) or to aim a flow at its members.</Text>
        <TextInput label="Name" placeholder="e.g. Plant supervisors" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={80} required />
        <TextInput label="Description" value={description} onChange={(e) => setDescription(e.currentTarget.value)} maxLength={300} />
        <MultiSelect label="Members" searchable data={lk.users} value={members} onChange={setMembers} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!name.trim()} onClick={async () => {
            setBusy(true);
            try {
              const b = { name: name.trim(), description: description.trim() || null, userIds: members };
              if (group) await flows.updateGroup(group.id, b); else await flows.createGroup(b);
              toast.ok('Group saved.'); onSaved(); onClose();
            } catch (e) { toast.err(e); } finally { setBusy(false); }
          }}>Save group</Button>
        </Group>
      </Stack>
    </Drawer>
  );
}

function CheckPanel({ types, lk }: { types: FlowType[]; lk: Lookups }) {
  const [type, setType] = useState<string | null>(null);
  const [result, setResult] = useState<{ checked: number; problems: VerifyProblem[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [pEmp, setPEmp] = useState<string | null>(null);
  const [pType, setPType] = useState<string>('leave_request');
  const [pAmount, setPAmount] = useState<number | ''>('');
  const pMoney = types.find((t) => t.objectType === pType)?.money ?? false;
  return (
    <Stack gap="lg">
      <Paper withBorder radius="md" p="md">
        <Stack gap="sm">
          <Text fw={600}>Check every employee's chains</Text>
          <Text size="sm" c="dimmed">Finds people whose request would reach nobody (no manager or head set, an empty group), so HR would get it by default. Run it after reorganising teams.</Text>
          <Group align="flex-end">
            <Select label="Request type" w={240} clearable placeholder="All request types" data={types.map((t) => ({ value: t.objectType, label: t.label }))} value={type} onChange={setType} />
            <Button loading={busy} leftSection={<IconRoute {...ICON} />} onClick={async () => { setBusy(true); try { setResult(await flows.verify(type ?? undefined)); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Check now</Button>
          </Group>
          {result ? (result.problems.length === 0
            ? <Alert color="teal" variant="light" icon={<IconCheck {...ICON} />}>All {result.checked} chains reach someone.</Alert>
            : (
              <Table.ScrollContainer minWidth={760}>
                <Table striped verticalSpacing="xs">
                  <Table.Thead><Table.Tr><Table.Th>Employee</Table.Th><Table.Th>Request</Table.Th><Table.Th>Step</Table.Th><Table.Th>Problem</Table.Th><Table.Th>Fix</Table.Th></Table.Tr></Table.Thead>
                  <Table.Tbody>
                    {result.problems.slice(0, 500).map((p, i) => (
                      <Table.Tr key={i}>
                        <Table.Td><Text size="sm">{p.name}</Text><Text size="xs" c="dimmed">{p.empCode}</Text></Table.Td>
                        <Table.Td><Text size="sm">{p.typeLabel}</Text>{p.flow ? <Text size="xs" c="dimmed">{p.flow}</Text> : null}</Table.Td>
                        <Table.Td><Text size="sm">{p.step}. {p.stepLabel}</Text></Table.Td>
                        <Table.Td><Text size="sm">{p.problem}</Text></Table.Td>
                        <Table.Td><Text size="sm" c="dimmed">{p.suggestion}</Text></Table.Td>
                      </Table.Tr>
                    ))}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            )) : null}
        </Stack>
      </Paper>
      <Paper withBorder radius="md" p="md">
        <Stack gap="sm">
          <Text fw={600}>Who approves for someone</Text>
          <Group align="flex-end" wrap="wrap">
            <Select label="Employee" w={260} searchable data={lk.employees} value={pEmp} onChange={setPEmp} placeholder="Choose an employee" />
            <Select label="Request type" w={220} data={types.map((t) => ({ value: t.objectType, label: t.label }))} value={pType} onChange={(v) => setPType(v ?? 'leave_request')} allowDeselect={false} />
            {pMoney ? <NumberInput label="Amount (Rs)" w={160} min={0} thousandSeparator="," value={pAmount} onChange={(v) => setPAmount(typeof v === 'number' ? v : '')} /> : null}
          </Group>
          {pEmp ? <ApprovalChainPreview objectType={pType} employeeId={pEmp} amount={pMoney && pAmount !== '' ? pAmount : null} /> : <Text size="sm" c="dimmed">Pick an employee to see their chain.</Text>}
        </Stack>
      </Paper>
    </Stack>
  );
}

/**
 * Settings → Approval flows. By default every request follows the reporting line; HR sets the default chain per
 * request type, adds flows for parts of the company, keeps user groups, and checks that every chain reaches someone.
 */
export default function ApprovalFlowsPage() {
  const cfg = useQuery(() => flows.config(), []);
  const users = useQuery(() => authApi.users(), []);
  const emps = useQuery(() => empApi.list({ status: 'active' }), []);
  const structure = useQuery(() => peopleApi.structure(), []);
  const [tab, setTab] = useState('flows');
  const [editFlow, setEditFlow] = useState<{ type: FlowType; flow: ApprovalFlow | null } | null>(null);
  const [editGroup, setEditGroup] = useState<UserGroup | null | 'new'>(null);

  const lk: Lookups = useMemo(() => ({
    users: (users.data?.users ?? []).filter((u) => u.status === 'active').map((u) => ({ value: u.id, label: u.name })),
    groups: (cfg.data?.groups ?? []).map((g) => ({ value: g.id, label: g.name })),
    employees: (emps.data?.employees ?? []).map((e) => ({ value: e.id, label: `${e.name_en} (${e.emp_code})` })),
    designations: (structure.data?.designations ?? []).map((d) => ({ value: d.id, label: d.name_en })),
    units: (structure.data?.units ?? []).map((u) => ({ value: u.id, label: u.name_en })),
    branches: (structure.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en })),
  }), [users.data, cfg.data, emps.data, structure.data]);

  const types = cfg.data?.types ?? [];
  return (
    <Stack gap="md">
      <PageHeader title="Approval flows" subtitle="Requests go to the line manager unless you set otherwise. Set a chain per request type, add flows for parts of the company, and check that every chain reaches someone." />
      <SegmentedControl value={tab} onChange={setTab} style={{ alignSelf: 'flex-start' }}
        data={[{ value: 'flows', label: 'Flows' }, { value: 'groups', label: `User groups (${cfg.data?.groups.length ?? 0})` }, { value: 'check', label: 'Check & preview' }]} />
      {cfg.error ? <EmptyState title="Could not load approval flows" hint={String(cfg.error)} /> : null}

      {tab === 'flows' ? (
        <Accordion variant="separated" radius="md" multiple>
          {types.map((t) => (
            <Accordion.Item key={t.objectType} value={t.objectType}>
              <Accordion.Control>
                <Group justify="space-between" wrap="nowrap" pr="sm">
                  <Text fw={600}>{t.label}</Text>
                  <Text size="sm" c="dimmed" truncate>{t.defaultSteps.map((s) => stepText(s, lk)).join(' → ')}{t.flows.length ? ` · ${t.flows.length} more flow${t.flows.length === 1 ? '' : 's'}` : ''}</Text>
                </Group>
              </Accordion.Control>
              <Accordion.Panel>
                <Stack gap="md">
                  {t.flows.map((f) => (
                    <Paper key={f.id} withBorder radius="md" p="sm">
                      <Group justify="space-between" wrap="wrap" gap="xs">
                        <Stack gap={2} style={{ minWidth: 0 }}>
                          <Group gap="xs"><Text fw={600} size="sm">{f.name}</Text>{!f.active ? <Badge color="gray" variant="light">Off</Badge> : null}{f.priority ? <Badge variant="outline" color="gray">Priority {f.priority}</Badge> : null}</Group>
                          <Text size="xs" c="dimmed">For {conditionText(f.conditions, lk)}</Text>
                          <Text size="sm">{f.steps.map((s) => stepText(s, lk)).join(' → ')}</Text>
                        </Stack>
                        <Group gap="xs">
                          <Switch size="sm" label="On" checked={f.active} onChange={async (e) => { try { await flows.update(f.id, { active: e.currentTarget.checked }); await cfg.reload(); } catch (err) { toast.err(err); } }} />
                          <Button size="xs" variant="default" onClick={() => setEditFlow({ type: t, flow: f })}>Edit</Button>
                          <Tooltip label="Delete flow"><ActionIcon variant="subtle" color="red" aria-label="Delete flow" onClick={async () => { try { await flows.remove(f.id); toast.ok('Flow deleted.'); await cfg.reload(); } catch (e) { toast.err(e); } }}><IconTrash {...ICON} /></ActionIcon></Tooltip>
                        </Group>
                      </Group>
                    </Paper>
                  ))}
                  <DefaultChain key={JSON.stringify(t.defaultSteps)} t={t} lk={lk} onSaved={() => void cfg.reload()} />
                  <Button variant="light" size="xs" leftSection={<IconPlus {...ICON} />} style={{ alignSelf: 'flex-start' }} onClick={() => setEditFlow({ type: t, flow: null })}>Add a flow for part of the company</Button>
                </Stack>
              </Accordion.Panel>
            </Accordion.Item>
          ))}
        </Accordion>
      ) : null}

      {tab === 'groups' ? (
        <Stack gap="sm">
          <Button leftSection={<IconUsersGroup {...ICON} />} style={{ alignSelf: 'flex-start' }} onClick={() => setEditGroup('new')}>New group</Button>
          {(cfg.data?.groups ?? []).length === 0
            ? <EmptyState title="No user groups yet" hint="Groups let several people share an approval step, e.g. all plant supervisors." action={{ label: 'New group', onClick: () => setEditGroup('new') }} />
            : (cfg.data?.groups ?? []).map((g) => (
              <Paper key={g.id} withBorder radius="md" p="sm">
                <Group justify="space-between" wrap="wrap">
                  <Stack gap={2}>
                    <Text fw={600} size="sm">{g.name}</Text>
                    {g.description ? <Text size="xs" c="dimmed">{g.description}</Text> : null}
                    <Text size="sm">{g.members.length ? g.members.map((m) => m.name).join(', ') : <Text span c="dimmed" size="sm">No members</Text>}</Text>
                  </Stack>
                  <Group gap="xs">
                    <Button size="xs" variant="default" onClick={() => setEditGroup(g)}>Edit</Button>
                    <Tooltip label="Delete group"><ActionIcon variant="subtle" color="red" aria-label="Delete group" onClick={async () => { try { await flows.removeGroup(g.id); toast.ok('Group deleted.'); await cfg.reload(); } catch (e) { toast.err(e); } }}><IconTrash {...ICON} /></ActionIcon></Tooltip>
                  </Group>
                </Group>
              </Paper>
            ))}
        </Stack>
      ) : null}

      {tab === 'check' ? <CheckPanel types={types} lk={lk} /> : null}

      {editFlow ? <FlowDrawer type={editFlow.type} flow={editFlow.flow} lk={lk} onClose={() => setEditFlow(null)} onSaved={() => void cfg.reload()} /> : null}
      {editGroup ? <GroupDrawer group={editGroup === 'new' ? null : editGroup} lk={lk} onClose={() => setEditGroup(null)} onSaved={() => void cfg.reload()} /> : null}
    </Stack>
  );
}
