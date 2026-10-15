import { Badge, Button, ColorInput, Group, Modal, MultiSelect, NumberInput, Progress, SegmentedControl, Select, Stack, Switch, Text, TextInput } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { employees as empApi } from '../../api';
import { time as timeApi, type ProjectInput, type TimeActivity, type TimeClient, type TimeProject } from '../../api/time';
import type { Employee } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { SectionTitle } from '../../components/PageHeader';
import { fmtMoney } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ProjectDot } from './parts';

const BRAND = '#12773a';
const SWATCHES = [BRAND, '#0b7285', '#1971c2', '#6741d9', '#c2255c', '#e8590c', '#f08c00', '#495057'];

function ProjectModal({ project, opened, onClose, clients, onSaved }: { project: TimeProject | null; opened: boolean; onClose: () => void; clients: TimeClient[]; onSaved: () => void }) {
  const [f, setF] = useState<ProjectInput>({ code: '', name: '' });
  const [people, setPeople] = useState<Employee[]>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (opened) empApi.list({ status: 'active' }).then((r) => setPeople(r.employees)).catch(() => setPeople([])); }, [opened]);
  useEffect(() => {
    if (!opened) return;
    setF(project ? {
      code: project.code, name: project.name, clientId: project.client_id, color: project.color, billable: project.billable,
      billRate: project.bill_rate === null ? null : Number(project.bill_rate), budgetHours: project.budget_hours === null ? null : Number(project.budget_hours),
      allMembers: project.all_members, memberIds: project.members.map((m) => m.id), status: project.status,
    } : { code: '', name: '', clientId: null, color: BRAND, billable: true, billRate: null, budgetHours: null, allMembers: true, memberIds: [], status: 'active' });
  }, [opened, project]);
  const set = <K extends keyof ProjectInput>(k: K, v: ProjectInput[K]) => setF((x) => ({ ...x, [k]: v }));
  const save = async () => {
    setBusy(true);
    try {
      const body = { ...f, memberIds: f.allMembers ? [] : f.memberIds ?? [] };
      if (project) await timeApi.updateProject(project.id, body); else await timeApi.createProject(body);
      toast.ok(project ? 'Project saved' : 'Project created'); onSaved(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={project ? `Edit ${project.name}` : 'New project'} centered radius="lg" size="lg">
      <Stack gap="sm">
        <Group grow align="flex-start">
          <TextInput label="Name" required value={f.name} onChange={(e) => set('name', e.currentTarget.value)} maxLength={120} />
          <TextInput label="Code" required description="Short reference, e.g. HYDRO-1" value={f.code} onChange={(e) => set('code', e.currentTarget.value.toUpperCase())} maxLength={20} />
        </Group>
        <Group grow align="flex-start">
          <Select label="Client" placeholder="Internal (no client)" clearable data={clients.map((c) => ({ value: c.id, label: c.name }))} value={f.clientId ?? null} onChange={(v) => set('clientId', v)} />
          <ColorInput label="Colour" value={f.color ?? BRAND} onChange={(v) => set('color', v)} swatches={SWATCHES} format="hex" withPicker={false} />
        </Group>
        <Switch label="Billable by default" description="New time on this project is marked billable unless the person changes it." checked={f.billable ?? true} onChange={(e) => set('billable', e.currentTarget.checked)} />
        <Group grow align="flex-start">
          <NumberInput label="Bill rate" description="Per hour" prefix="Rs " min={0} thousandSeparator="," value={f.billRate ?? ''} onChange={(v) => set('billRate', v === '' ? null : Number(v))} />
          <NumberInput label="Budget" description="Total hours" suffix=" h" min={1} value={f.budgetHours ?? ''} onChange={(v) => set('budgetHours', v === '' ? null : Number(v))} />
        </Group>
        <Switch label="Everyone can log time to this project" checked={f.allMembers ?? true} onChange={(e) => set('allMembers', e.currentTarget.checked)} />
        {!f.allMembers ? (
          <MultiSelect label="Members" placeholder="Pick people" searchable data={people.map((p) => ({ value: p.id, label: `${p.name_en} (${p.emp_code})` }))}
            value={f.memberIds ?? []} onChange={(v) => set('memberIds', v)} nothingFoundMessage="No one matches" />
        ) : null}
        {project ? <Switch label="Archived" description="Archived projects stay in reports but no one can log new time to them." checked={f.status === 'archived'} onChange={(e) => set('status', e.currentTarget.checked ? 'archived' : 'active')} /> : null}
        <Group justify="flex-end" mt="xs">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} onClick={save} disabled={!f.name.trim() || !f.code.trim()}>{project ? 'Save' : 'Create project'}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function Projects({ clients }: { clients: TimeClient[] }) {
  const q = useQuery(() => timeApi.projects(true), []);
  const [edit, setEdit] = useState<TimeProject | null | 'new'>(null);
  return (
    <Stack gap="md">
      <SectionTitle hint="What people can log time to. Budgets show how much of the planned hours are used." actions={<Button size="sm" leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setEdit('new')}>New project</Button>}>Projects</SectionTitle>
      <DataTable<TimeProject> rows={q.data?.projects ?? []} rowKey={(p) => p.id} loading={q.loading} error={q.error} onRowClick={(p) => setEdit(p)}
        emptyTitle="No projects yet" emptyHint="Create a project so people can log their time against it."
        columns={[
          { key: 'name', header: 'Project', render: (p) => <Group gap={8} wrap="nowrap"><ProjectDot color={p.color} /><div><Text size="sm" fw={500}>{p.name}</Text><Text size="xs" c="dimmed" ff="monospace">{p.code}</Text></div></Group> },
          { key: 'client', header: 'Client', render: (p) => <Text size="sm" c={p.client_name ? 'inherit' : 'dimmed'}>{p.client_name ?? 'Internal'}</Text> },
          { key: 'rate', header: 'Billing', render: (p) => p.billable ? <Text size="sm">{p.bill_rate ? `${fmtMoney(p.bill_rate)}/h` : 'Billable'}</Text> : <Text size="sm" c="dimmed">Non-billable</Text> },
          { key: 'logged', header: 'Logged', align: 'right', render: (p) => <Text size="sm" ff="monospace">{(p.tracked_min / 60).toFixed(1)} h</Text> },
          { key: 'budget', header: 'Budget', width: 160, render: (p) => {
            if (!p.budget_hours) return <Text size="sm" c="dimmed">—</Text>;
            const pct = Math.round((p.tracked_min / 60 / Number(p.budget_hours)) * 100);
            return <Stack gap={2}><Progress value={Math.min(pct, 100)} color={pct >= 100 ? 'red' : pct >= 80 ? 'orange' : 'brand'} size="sm" /><Text size="xs" c="dimmed">{pct}% of {Number(p.budget_hours)} h</Text></Stack>;
          } },
          { key: 'members', header: 'Who', render: (p) => <Text size="sm" c="dimmed">{p.all_members ? 'Everyone' : `${p.members.length} ${p.members.length === 1 ? 'person' : 'people'}`}</Text> },
          { key: 'status', header: 'Status', render: (p) => <Badge variant="light" color={p.status === 'active' ? 'teal' : 'gray'}>{p.status === 'active' ? 'Active' : 'Archived'}</Badge> },
        ]} />
      <ProjectModal opened={edit !== null} project={edit === 'new' ? null : edit} onClose={() => setEdit(null)} clients={clients} onSaved={() => void q.reload()} />
    </Stack>
  );
}

function Clients({ q }: { q: ReturnType<typeof useQuery<{ clients: TimeClient[] }>> }) {
  const [name, setName] = useState(''); const [busy, setBusy] = useState(false);
  const add = async () => {
    setBusy(true);
    try { await timeApi.createClient({ name: name.trim() }); setName(''); toast.ok('Client added'); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const toggle = async (c: TimeClient) => {
    try { await timeApi.updateClient(c.id, { archived: !c.archived }); await q.reload(); } catch (e) { toast.err(e); }
  };
  return (
    <Stack gap="md">
      <SectionTitle hint="Who the work is for. Group projects by client in reports.">Clients</SectionTitle>
      <Group align="flex-end" gap="xs">
        <TextInput label="New client" placeholder="Client name" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={120} w={280} />
        <Button onClick={add} loading={busy} disabled={!name.trim()}>Add</Button>
      </Group>
      <DataTable<TimeClient> rows={q.data?.clients ?? []} rowKey={(c) => c.id} loading={q.loading} error={q.error}
        emptyTitle="No clients yet" emptyHint="Add the organisations you do project work for."
        columns={[
          { key: 'name', header: 'Client', render: (c) => <Text size="sm" fw={500} c={c.archived ? 'dimmed' : 'inherit'}>{c.name}</Text> },
          { key: 'projects', header: 'Active projects', align: 'right', render: (c) => <Text size="sm">{c.active_projects ?? 0}</Text> },
          { key: 'actions', header: '', align: 'right', render: (c) => <Button size="compact-xs" variant="subtle" color="gray" onClick={() => toggle(c)}>{c.archived ? 'Restore' : 'Archive'}</Button> },
        ]} />
    </Stack>
  );
}

function Activities({ projects }: { projects: TimeProject[] }) {
  const q = useQuery(() => timeApi.activities({ all: 1 }), []);
  const [name, setName] = useState(''); const [projectId, setProjectId] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const add = async () => {
    setBusy(true);
    try { await timeApi.createActivity({ name: name.trim(), projectId }); setName(''); toast.ok('Activity added'); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const toggle = async (a: TimeActivity) => {
    try { await timeApi.updateActivity(a.id, { archived: !a.archived }); await q.reload(); } catch (e) { toast.err(e); }
  };
  return (
    <Stack gap="md">
      <SectionTitle hint="The kind of work, such as design, meetings or field work. Company-wide activities can be used on any project.">Activities</SectionTitle>
      <Group align="flex-end" gap="xs" wrap="wrap">
        <TextInput label="New activity" placeholder="e.g. Field work" value={name} onChange={(e) => setName(e.currentTarget.value)} maxLength={80} w={240} />
        <Select label="For" placeholder="Every project" clearable data={projects.filter((p) => p.status === 'active').map((p) => ({ value: p.id, label: p.name }))} value={projectId} onChange={setProjectId} w={240} />
        <Button onClick={add} loading={busy} disabled={!name.trim()}>Add</Button>
      </Group>
      <DataTable<TimeActivity> rows={q.data?.activities ?? []} rowKey={(a) => a.id} loading={q.loading} error={q.error}
        emptyTitle="No activities yet" emptyHint="Activities are optional. Add them if you want to see time by type of work."
        columns={[
          { key: 'name', header: 'Activity', render: (a) => <Text size="sm" fw={500} c={a.archived ? 'dimmed' : 'inherit'}>{a.name}</Text> },
          { key: 'project', header: 'Project', render: (a) => <Text size="sm" c="dimmed">{a.project_name ?? 'Every project'}</Text> },
          { key: 'actions', header: '', align: 'right', render: (a) => <Button size="compact-xs" variant="subtle" color="gray" onClick={() => toggle(a)}>{a.archived ? 'Restore' : 'Archive'}</Button> },
        ]} />
    </Stack>
  );
}

/** HR manages clients, projects and activities for time tracking. */
export default function ProjectsPage() {
  const [view, setView] = useState<'projects' | 'clients' | 'activities'>('projects');
  const clients = useQuery(() => timeApi.clients(true), []);
  const projects = useQuery(() => timeApi.projects(true), [view]);
  return (
    <Stack gap="lg">
      <Group justify="flex-end">
        <SegmentedControl size="xs" value={view} onChange={(v) => setView(v as typeof view)}
          data={[{ value: 'projects', label: 'Projects' }, { value: 'clients', label: 'Clients' }, { value: 'activities', label: 'Activities' }]} />
      </Group>
      {view === 'projects' ? <Projects clients={(clients.data?.clients ?? []).filter((c) => !c.archived)} /> : null}
      {view === 'clients' ? <Clients q={clients} /> : null}
      {view === 'activities' ? <Activities projects={projects.data?.projects ?? []} /> : null}
    </Stack>
  );
}
