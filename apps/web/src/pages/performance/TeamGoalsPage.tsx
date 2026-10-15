import { ActionIcon, Badge, Button, Drawer, Group, NumberInput, Paper, SegmentedControl, Select, SimpleGrid, Stack, Switch, Text, Textarea, TextInput } from '@mantine/core';
import { IconNotebook, IconPlus, IconTargetArrow, IconTrash } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { useAuth } from '../../auth/AuthProvider';
import { performance as perfApi, type Goal, type Kpi, type Measure } from '../../api/performance';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { GoalCard, GoalDetail, GoalDrawer, GoalProgress, ICON } from './parts';

type View = 'team' | 'company' | 'kpis' | 'journal' | 'feedback';

/** Managers follow their team's goals and keep a journal; HR also sets company goals and the KPI library. */
export default function TeamGoalsPage() {
  const { hasRole, user } = useAuth();
  const hr = hasRole('hr');
  const [view, setView] = useState<View>('team');
  const colleagues = useQuery(() => perfApi.colleagues(), []);
  const team = useMemo(() => (colleagues.data?.colleagues ?? []).filter((c) => (hr ? c.id !== user?.employeeId : c.manager_id === user?.employeeId)), [colleagues.data, hr, user?.employeeId]);
  const views = [
    { value: 'team', label: 'Team goals' }, { value: 'company', label: 'Company & department' }, { value: 'journal', label: 'Journal' }, { value: 'feedback', label: 'Feedback' },
    ...(hr ? [{ value: 'kpis', label: 'KPI library' }] : []),
  ];
  return (
    <Stack gap="lg">
      <SegmentedControl value={view} onChange={(v) => setView(v as View)} data={views} style={{ alignSelf: 'flex-start' }} />
      {view === 'team' ? <TeamGoals team={team} hr={hr} /> : null}
      {view === 'company' ? <CompanyGoals hr={hr} /> : null}
      {view === 'kpis' && hr ? <KpiLibrary /> : null}
      {view === 'journal' ? <Journal team={team} /> : null}
      {view === 'feedback' ? <TeamFeedback team={team} /> : null}
    </Stack>
  );
}

function TeamGoals({ team, hr }: { team: Array<{ id: string; name_en: string; designation: string | null; department: string | null; manager_id: string | null; emp_code: string }>; hr: boolean }) {
  const q = useQuery(() => perfApi.goals({ scope: hr ? 'all' : 'team' }), [hr]);
  const [create, setCreate] = useState(false); const [openId, setOpenId] = useState<string | null>(null);
  const [person, setPerson] = useState<string | null>(null);
  const goals = (q.data?.goals ?? []).filter((g) => g.status !== 'cancelled' && (!person || g.employee_id === person));
  const byPerson = useMemo(() => {
    const m = new Map<string, Goal[]>();
    for (const g of goals) m.set(g.employee_name ?? '', [...(m.get(g.employee_name ?? '') ?? []), g]);
    return [...m.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [goals]);
  const withoutGoals = team.filter((t) => !(q.data?.goals ?? []).some((g) => g.employee_id === t.id && g.status === 'active'));
  return (
    <Stack gap="md">
      <SectionTitle hint={hr ? 'Every individual goal in the company.' : 'Goals of the people who report to you, directly or through their managers.'}
        actions={<Group gap="xs">
          <Select size="xs" w={220} placeholder="Everyone" clearable searchable value={person} onChange={setPerson} data={team.map((t) => ({ value: t.id, label: t.name_en }))} />
          <Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => setCreate(true)}>Set a goal</Button>
        </Group>}>Team goals</SectionTitle>
      {withoutGoals.length && !person && !q.loading ? <Text size="sm" c="orange.7">No active goals: {withoutGoals.slice(0, 8).map((t) => t.name_en).join(', ')}{withoutGoals.length > 8 ? ` and ${withoutGoals.length - 8} more` : ''}</Text> : null}
      {!q.loading && goals.length === 0 ? <EmptyState icon={<IconTargetArrow size={20} stroke={1.6} />} title="No team goals yet" hint="Agree two to five goals with each person." action={{ label: 'Set a goal', onClick: () => setCreate(true) }} /> : byPerson.map(([name, gs]) => (
        <Stack key={name} gap="xs">
          <Group gap="xs"><Text fw={600} size="sm">{name}</Text><Text size="xs" c="dimmed">{gs.length} goal{gs.length === 1 ? '' : 's'} · avg {Math.round(gs.reduce((s, g) => s + Number(g.progress), 0) / gs.length)}%</Text></Group>
          <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>{gs.map((g) => <GoalCard key={g.id} g={g} onOpen={(x) => setOpenId(x.id)} />)}</SimpleGrid>
        </Stack>
      ))}
      <GoalDrawer opened={create} onClose={() => setCreate(false)} onSaved={() => void q.reload()} employees={team} defaultEmployeeId={person} />
      <GoalDetail goalId={openId} onClose={() => setOpenId(null)} onChanged={() => void q.reload()} />
    </Stack>
  );
}

function CompanyGoals({ hr }: { hr: boolean }) {
  const q = useQuery(() => perfApi.goals({ scope: 'company' }), []);
  const [create, setCreate] = useState(false); const [openId, setOpenId] = useState<string | null>(null);
  const goals = q.data?.goals ?? [];
  return (
    <Stack gap="md">
      <SectionTitle hint="Company and department goals everyone can see and align their own goals to."
        actions={hr ? <Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => setCreate(true)}>New goal</Button> : undefined}>Company & department goals</SectionTitle>
      <DataTable<Goal> rows={goals} rowKey={(g) => g.id} loading={q.loading} error={q.error} onRowClick={(g) => setOpenId(g.id)}
        emptyTitle="No company goals yet" emptyHint={hr ? 'Start with three to five company goals for the year.' : 'HR publishes these.'}
        columns={[
          { key: 'title', header: 'Goal', render: (g) => <div><Text size="sm" fw={500}>{g.title}</Text><Text size="xs" c="dimmed">{g.scope === 'company' ? 'Company' : g.org_unit_name}</Text></div> },
          { key: 'due', header: 'Due', render: (g) => <Text size="sm">{fmtDateShort(g.due_date)}</Text> },
          { key: 'progress', header: 'Progress', width: 200, render: (g) => <GoalProgress value={g.progress} /> },
          { key: 'status', header: 'Status', render: (g) => <Badge variant="light" color={g.status === 'completed' ? 'brand' : 'gray'}>{g.status}</Badge> },
        ]} />
      <GoalDrawer opened={create} onClose={() => setCreate(false)} onSaved={() => void q.reload()} allowCompany />
      <GoalDetail goalId={openId} onClose={() => setOpenId(null)} onChanged={() => void q.reload()} />
    </Stack>
  );
}

const MEASURES = [{ value: 'number', label: 'Number' }, { value: 'percent', label: 'Percent' }, { value: 'currency', label: 'Amount (NPR)' }, { value: 'boolean', label: 'Done / not done' }];

function KpiLibrary() {
  const q = useQuery(() => perfApi.kpis(), []);
  const [edit, setEdit] = useState<(Partial<Kpi> & { defaultTarget?: number }) | null>(null); const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!edit) return; setBusy(true);
    try {
      const body = { designation: edit.designation || null, title: edit.title, description: edit.description || null, measure: edit.measure ?? 'number', defaultTarget: edit.defaultTarget ?? 100, unit: edit.unit || null, active: edit.active ?? true };
      if (edit.id) await perfApi.updateKpi(edit.id, body); else await perfApi.createKpi(body);
      toast.ok('KPI saved.'); setEdit(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack gap="md">
      <SectionTitle hint="Standard measures per designation. Employees and managers turn them into goals with one click."
        actions={<Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => setEdit({ measure: 'number', defaultTarget: 100, active: true })}>New KPI</Button>}>KPI library</SectionTitle>
      <DataTable<Kpi> rows={q.data?.kpis ?? []} rowKey={(k) => k.id} loading={q.loading} error={q.error} onRowClick={(k) => setEdit({ ...k, defaultTarget: Number(k.default_target) })}
        emptyTitle="No KPIs yet" emptyHint="Add measures such as 'Tickets resolved within SLA' for support staff."
        columns={[
          { key: 'title', header: 'KPI', render: (k) => <div><Text size="sm" fw={500}>{k.title}</Text>{k.description ? <Text size="xs" c="dimmed" lineClamp={1}>{k.description}</Text> : null}</div> },
          { key: 'designation', header: 'Designation', render: (k) => <Text size="sm">{k.designation ?? 'Everyone'}</Text> },
          { key: 'target', header: 'Default target', align: 'right', render: (k) => <Text size="sm">{k.measure === 'boolean' ? 'Done' : `${Number(k.default_target).toLocaleString('en-US')}${k.measure === 'percent' ? '%' : k.unit ? ` ${k.unit}` : ''}`}</Text> },
          { key: 'active', header: '', render: (k) => (k.active ? null : <Badge variant="light" color="gray">Inactive</Badge>) },
        ]} />
      <Drawer opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? 'Edit KPI' : 'New KPI'} position="right">
        {edit ? (
          <Stack gap="sm">
            <TextInput label="KPI" required value={edit.title ?? ''} onChange={(e) => { const v = e.currentTarget.value; setEdit((x) => x && { ...x, title: v }); }} />
            <Textarea label="How it is measured" autosize minRows={2} value={edit.description ?? ''} onChange={(e) => { const v = e.currentTarget.value; setEdit((x) => x && { ...x, description: v }); }} />
            <TextInput label="Designation" description="Leave empty for everyone" value={edit.designation ?? ''} onChange={(e) => { const v = e.currentTarget.value; setEdit((x) => x && { ...x, designation: v }); }} />
            <SimpleGrid cols={2}>
              <Select label="Measure" value={edit.measure ?? 'number'} onChange={(v) => setEdit((x) => x && { ...x, measure: (v ?? 'number') as Measure })} data={MEASURES} allowDeselect={false} />
              <NumberInput label="Default target" value={edit.defaultTarget ?? 100} onChange={(v) => setEdit((x) => x && { ...x, defaultTarget: Number(v) || 0 })} />
            </SimpleGrid>
            <TextInput label="Unit" placeholder="e.g. tickets" value={edit.unit ?? ''} onChange={(e) => { const v = e.currentTarget.value; setEdit((x) => x && { ...x, unit: v }); }} />
            <Switch label="Active" checked={edit.active ?? true} onChange={(e) => { const v = e.currentTarget.checked; setEdit((x) => x && { ...x, active: v }); }} />
            <Group justify="flex-end"><Button onClick={save} loading={busy} disabled={!edit.title?.trim()}>Save</Button></Group>
          </Stack>
        ) : null}
      </Drawer>
    </Stack>
  );
}

const TAG = [{ value: 'positive', label: 'Went well' }, { value: 'improvement', label: 'To improve' }, { value: 'neutral', label: 'Note' }];
const TAG_COLOR: Record<string, string> = { positive: 'teal', improvement: 'orange', neutral: 'gray' };

function Journal({ team }: { team: Array<{ id: string; name_en: string }> }) {
  const { user } = useAuth();
  const [person, setPerson] = useState<string | null>(null);
  const q = useQuery(() => (person ? perfApi.journal(person) : Promise.resolve({ entries: [] })), [person]);
  const [date, setDate] = useState<string | null>(todayNpt()); const [tag, setTag] = useState('neutral'); const [note, setNote] = useState(''); const [busy, setBusy] = useState(false);
  const add = async () => {
    if (!person || !date) return; setBusy(true);
    try { await perfApi.addJournal({ employeeId: person, noteDate: date, tag, note }); setNote(''); toast.ok('Note saved.'); await q.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack gap="md">
      <SectionTitle hint="Private, dated notes about what happened during the year, so reviews rest on evidence and not memory. Only you and HR see your notes."
        actions={<Select size="xs" w={240} placeholder="Choose a person" searchable value={person} onChange={setPerson} data={team.map((t) => ({ value: t.id, label: t.name_en }))} />}>Manager's journal</SectionTitle>
      {!person ? <EmptyState icon={<IconNotebook size={20} stroke={1.6} />} title="Choose someone from your team" hint="Their journal opens here." /> : (
        <>
          <Paper withBorder radius="lg" p="md">
            <Stack gap="xs">
              <Group gap="xs" wrap="wrap" align="flex-end">
                <DateField label="Date" value={date} onChange={setDate} maxDate={todayNpt()} w={190} />
                <SegmentedControl size="xs" value={tag} onChange={setTag} data={TAG} />
              </Group>
              <Textarea placeholder="What happened, and what was the impact?" autosize minRows={2} value={note} onChange={(e) => setNote(e.currentTarget.value)} />
              <Group justify="flex-end"><Button size="sm" onClick={add} loading={busy} disabled={!note.trim() || !date}>Add note</Button></Group>
            </Stack>
          </Paper>
          {(q.data?.entries ?? []).length === 0 ? <Text size="sm" c="dimmed">No notes yet.</Text> : (q.data?.entries ?? []).map((j) => (
            <Paper key={j.id} withBorder radius="md" p="sm">
              <Group justify="space-between" wrap="nowrap">
                <Group gap="xs"><Badge size="sm" variant="light" color={TAG_COLOR[j.tag] ?? 'gray'}>{TAG.find((t) => t.value === j.tag)?.label}</Badge><Text size="xs" c="dimmed">{fmtDateShort(j.note_date)} · {j.author_name}</Text></Group>
                {j.author_id === user?.id ? <ActionIcon variant="subtle" color="gray" aria-label="Delete note" onClick={async () => { try { await perfApi.deleteJournal(j.id); await q.reload(); } catch (e) { toast.err(e); } }}><IconTrash size={14} /></ActionIcon> : null}
              </Group>
              <Text size="sm" mt={4} style={{ whiteSpace: 'pre-wrap' }}>{j.note}</Text>
            </Paper>
          ))}
        </>
      )}
    </Stack>
  );
}

function TeamFeedback({ team }: { team: Array<{ id: string; name_en: string }> }) {
  const [person, setPerson] = useState<string | null>(null);
  const q = useQuery(() => (person ? perfApi.feedback('about', person) : Promise.resolve({ feedback: [] })), [person]);
  return (
    <Stack gap="md">
      <SectionTitle hint="All feedback colleagues gave a person, including notes shared with you only."
        actions={<Select size="xs" w={240} placeholder="Choose a person" searchable value={person} onChange={setPerson} data={team.map((t) => ({ value: t.id, label: t.name_en }))} />}>Feedback about your team</SectionTitle>
      {!person ? <EmptyState title="Choose someone from your team" /> : (q.data?.feedback ?? []).length === 0 ? <Text size="sm" c="dimmed">No feedback yet.</Text> : (q.data?.feedback ?? []).map((f) => (
        <Paper key={f.id} withBorder radius="md" p="sm">
          <Group gap="xs"><Text size="sm" fw={500}>From {f.giver_name}</Text>{f.private_to_manager ? <Badge size="sm" variant="light" color="gray">Manager only</Badge> : null}<Text size="xs" c="dimmed">{fmtDateShort(f.given_at?.slice(0, 10))}</Text></Group>
          <Text size="sm" mt={4} style={{ whiteSpace: 'pre-wrap' }}>{f.body}</Text>
        </Paper>
      ))}
    </Stack>
  );
}
