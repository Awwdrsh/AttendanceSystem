import { ActionIcon, Badge, Button, Drawer, Group, MultiSelect, NumberInput, Paper, Progress, SegmentedControl, Select, SimpleGrid, Stack, Switch, Table, Text, Textarea, TextInput } from '@mantine/core';
import { IconArrowLeft, IconPlus, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { useAuth } from '../../auth/AuthProvider';
import { performance as perfApi, type Competency, type Cycle, type CycleReviewRow, type Phase } from '../../api/performance';
import { DateField } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ICON, PHASE_LABEL, PhaseBadge, ratingLabel } from './parts';
import { ReviewForm } from './ReviewForm';

const NEXT: Partial<Record<Phase, string>> = {
  self_review: 'Open manager reviews', manager_review: 'Next phase', peer_review: 'Start calibration', calibration: 'Release results', released: 'Close cycle',
};

/** Review cycles: HR runs them phase by phase; managers review their team in each. HR also keeps the competency framework. */
export default function ReviewCyclesPage() {
  const { hasRole } = useAuth();
  const hr = hasRole('hr');
  const [view, setView] = useState<'cycles' | 'competencies'>('cycles');
  const [cycleId, setCycleId] = useState<string | null>(null);
  if (cycleId) return <CycleDetail id={cycleId} hr={hr} onBack={() => setCycleId(null)} />;
  return (
    <Stack gap="lg">
      {hr ? <SegmentedControl value={view} onChange={(v) => setView(v as typeof view)} data={[{ value: 'cycles', label: 'Review cycles' }, { value: 'competencies', label: 'Competencies' }]} style={{ alignSelf: 'flex-start' }} /> : null}
      {view === 'cycles' ? <CycleList hr={hr} onOpen={setCycleId} /> : <Competencies />}
    </Stack>
  );
}

function CycleList({ hr, onOpen }: { hr: boolean; onOpen: (id: string) => void }) {
  const q = useQuery(() => perfApi.cycles(), []);
  const [edit, setEdit] = useState<Cycle | 'new' | null>(null);
  return (
    <Stack gap="md">
      <SectionTitle hint={hr ? 'Each cycle moves through self review, manager review, optional peer review, calibration and release.' : 'Cycles where you review your team.'}
        actions={hr ? <Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => setEdit('new')}>New cycle</Button> : undefined}>Review cycles</SectionTitle>
      <DataTable<Cycle> rows={q.data?.cycles ?? []} rowKey={(c) => c.id} loading={q.loading} error={q.error} onRowClick={(c) => onOpen(c.id)}
        emptyTitle="No review cycles yet" emptyHint={hr ? 'Create an annual or half-yearly cycle, then launch it.' : 'HR has not started a cycle for your team.'}
        columns={[
          { key: 'name', header: 'Cycle', render: (c) => <div><Text size="sm" fw={500}>{c.name}</Text><Text size="xs" c="dimmed">{fmtDateShort(c.period_start)} to {fmtDateShort(c.period_end)}</Text></div> },
          { key: 'phase', header: 'Phase', render: (c) => <PhaseBadge phase={c.status} /> },
          { key: 'progress', header: 'Self / manager done', render: (c) => <Text size="sm">{c.participants ? `${c.self_done ?? 0} / ${c.manager_done ?? 0} of ${c.participants}` : '—'}</Text> },
          { key: 'weights', header: 'Goals / competencies', render: (c) => <Text size="sm">{Number(c.goals_weight)}% / {Number(c.competency_weight)}%</Text> },
          ...(hr ? [{ key: 'edit', header: '', render: (c: Cycle) => (c.status !== 'closed' ? <Button size="compact-xs" variant="subtle" onClick={(e) => { e.stopPropagation(); setEdit(c); }}>Edit</Button> : null) }] : []),
        ]} />
      <CycleDrawer cycle={edit} onClose={() => setEdit(null)} onSaved={() => void q.reload()} />
    </Stack>
  );
}

function CycleDrawer({ cycle, onClose, onSaved }: { cycle: Cycle | 'new' | null; onClose: () => void; onSaved: () => void }) {
  const existing = cycle && cycle !== 'new' ? cycle : null;
  const draft = !existing || existing.status === 'draft';
  const depts = useQuery(() => perfApi.departments(), [!!cycle], { enabled: !!cycle });
  const [f, setF] = useState<Record<string, unknown>>({});
  const [key, setKey] = useState<string | null>(null);
  const k = existing?.id ?? (cycle === 'new' ? 'new' : null);
  if (k !== key) {
    setKey(k);
    setF(existing ? {
      name: existing.name, periodStart: existing.period_start, periodEnd: existing.period_end, goalsWeight: Number(existing.goals_weight), competencyWeight: Number(existing.competency_weight),
      peerEnabled: existing.peer_enabled, peerMin: existing.peer_min, selfDue: existing.self_due, managerDue: existing.manager_due, peerDue: existing.peer_due, calibrationDue: existing.calibration_due,
      orgUnitIds: existing.filter?.orgUnitIds ?? [], joinedBefore: existing.filter?.joinedBefore ?? null, ratingLabels: existing.rating_labels,
    } : { name: '', periodStart: null, periodEnd: null, goalsWeight: 60, competencyWeight: 40, peerEnabled: false, peerMin: 3, orgUnitIds: [], joinedBefore: null,
      ratingLabels: ['Needs improvement', 'Partly meets', 'Meets expectations', 'Exceeds expectations', 'Outstanding'] });
  }
  const set = (n: string, v: unknown) => setF((x) => ({ ...x, [n]: v }));
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const deadlines = { name: f.name, selfDue: f.selfDue ?? null, managerDue: f.managerDue ?? null, peerDue: f.peerDue ?? null, calibrationDue: f.calibrationDue ?? null, peerMin: f.peerMin };
      const body = draft ? {
        ...deadlines, periodStart: f.periodStart, periodEnd: f.periodEnd, goalsWeight: f.goalsWeight, competencyWeight: f.competencyWeight, peerEnabled: f.peerEnabled, ratingLabels: f.ratingLabels,
        filter: { ...((f.orgUnitIds as string[]).length ? { orgUnitIds: f.orgUnitIds } : {}), ...(f.joinedBefore ? { joinedBefore: f.joinedBefore } : {}) },
      } : deadlines;
      if (existing) await perfApi.updateCycle(existing.id, body); else await perfApi.createCycle(body);
      toast.ok('Cycle saved.'); onSaved(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const del = async () => { if (!existing) return; try { await perfApi.deleteCycle(existing.id); toast.ok('Deleted.'); onSaved(); onClose(); } catch (e) { toast.err(e); } };
  const labels = (f.ratingLabels as string[] | undefined) ?? [];
  return (
    <Drawer opened={!!cycle} onClose={onClose} position="right" size="lg" title={existing ? 'Edit cycle' : 'New review cycle'}>
      <Stack gap="sm">
        <TextInput label="Name" required placeholder="Annual review 2083/84" value={String(f.name ?? '')} onChange={(e) => set('name', e.currentTarget.value)} />
        <SimpleGrid cols={2}>
          <DateField label="Period from" required disabled={!draft} value={(f.periodStart as string) ?? null} onChange={(v) => set('periodStart', v)} />
          <DateField label="Period to" required disabled={!draft} value={(f.periodEnd as string) ?? null} onChange={(v) => set('periodEnd', v)} />
          <NumberInput label="Goals weight (%)" disabled={!draft} min={0} max={100} value={Number(f.goalsWeight)} onChange={(v) => { set('goalsWeight', Number(v) || 0); set('competencyWeight', 100 - (Number(v) || 0)); }} />
          <NumberInput label="Competencies weight (%)" disabled value={Number(f.competencyWeight)} />
        </SimpleGrid>
        <Switch label="Include peer (360) feedback" disabled={!draft} checked={!!f.peerEnabled} onChange={(e) => set('peerEnabled', e.currentTarget.checked)} />
        {f.peerEnabled ? <NumberInput label="Minimum responses before an employee sees peer feedback" min={1} max={10} value={Number(f.peerMin)} onChange={(v) => set('peerMin', Number(v) || 3)} /> : null}
        <Text fw={600} size="sm" mt="xs">Deadlines</Text>
        <SimpleGrid cols={2}>
          <DateField label="Self review" value={(f.selfDue as string) ?? null} onChange={(v) => set('selfDue', v)} />
          <DateField label="Manager review" value={(f.managerDue as string) ?? null} onChange={(v) => set('managerDue', v)} />
          {f.peerEnabled ? <DateField label="Peer review" value={(f.peerDue as string) ?? null} onChange={(v) => set('peerDue', v)} /> : null}
          <DateField label="Calibration" value={(f.calibrationDue as string) ?? null} onChange={(v) => set('calibrationDue', v)} />
        </SimpleGrid>
        {draft ? <>
          <Text fw={600} size="sm" mt="xs">Who takes part</Text>
          <MultiSelect label="Departments" placeholder="All departments" value={(f.orgUnitIds as string[]) ?? []} onChange={(v) => set('orgUnitIds', v)} data={(depts.data?.departments ?? []).map((d) => ({ value: d.id, label: d.name_en }))} />
          <DateField label="Joined on or before" description="Leave empty to include everyone active" value={(f.joinedBefore as string) ?? null} onChange={(v) => set('joinedBefore', v)} maxDate={todayNpt()} />
          <Text fw={600} size="sm" mt="xs">Rating scale</Text>
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            {labels.map((l, i) => <TextInput key={i} label={`${i + 1}`} value={l} onChange={(e) => { const v = e.currentTarget.value; set('ratingLabels', labels.map((x, j) => (j === i ? v : x))); }} />)}
          </SimpleGrid>
        </> : <Text size="xs" c="dimmed">After launch only the name, deadlines and the peer minimum can change.</Text>}
        <Group justify="space-between" mt="sm">
          {existing?.status === 'draft' ? <Button variant="subtle" color="red" onClick={del}>Delete</Button> : <span />}
          <Button onClick={save} loading={busy} disabled={!String(f.name ?? '').trim() || !f.periodStart || !f.periodEnd}>Save</Button>
        </Group>
      </Stack>
    </Drawer>
  );
}

function CycleDetail({ id, hr, onBack }: { id: string; hr: boolean; onBack: () => void }) {
  const q = useQuery(() => perfApi.cycle(id), [id]);
  const cal = useQuery(() => perfApi.calibration(id), [id], { enabled: hr && q.data?.cycle.status === 'calibration' });
  const [open, setOpen] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<'launch' | 'advance' | null>(null);
  const [filter, setFilter] = useState<'all' | 'self' | 'manager'>('all');
  const c = q.data?.cycle; const rows = q.data?.reviews ?? [];
  const shown = rows.filter((r) => (filter === 'self' ? !r.self_submitted_at : filter === 'manager' ? !r.manager_submitted_at : true));
  const run = async () => {
    if (!c || !confirm) return;
    try {
      if (confirm === 'launch') { const r = await perfApi.launchCycle(c.id); toast.ok(`Launched for ${r.participants} people.`); }
      else { const r = await perfApi.advanceCycle(c.id); toast.ok(`Moved to ${PHASE_LABEL[r.status]}.`); }
      setConfirm(null); await q.reload();
    } catch (e) { toast.err(e); setConfirm(null); }
  };
  const selfDone = rows.filter((r) => r.self_submitted_at).length; const mgrDone = rows.filter((r) => r.manager_submitted_at).length;
  return (
    <Stack gap="lg">
      <Group justify="space-between" wrap="wrap">
        <Group gap="xs">
          <ActionIcon variant="subtle" onClick={onBack} aria-label="Back to cycles"><IconArrowLeft {...ICON} /></ActionIcon>
          <div><Group gap="xs"><Text fw={600} size="lg">{c?.name ?? ''}</Text>{c ? <PhaseBadge phase={c.status} /> : null}</Group>
            {c ? <Text size="xs" c="dimmed">{fmtDateShort(c.period_start)} to {fmtDateShort(c.period_end)}</Text> : null}</div>
        </Group>
        {hr && c ? (c.status === 'draft' ? <Button onClick={() => setConfirm('launch')}>Launch cycle</Button> : NEXT[c.status] ? <Button onClick={() => setConfirm('advance')}>{c.status === 'manager_review' && !c.peer_enabled ? 'Start calibration' : c.status === 'manager_review' ? 'Open peer review' : NEXT[c.status]}</Button> : null) : null}
      </Group>
      {c && c.status !== 'draft' ? (
        <StatStrip stats={[
          { key: 'n', label: 'People', value: rows.length },
          { key: 's', label: 'Self reviews done', value: `${selfDone} / ${rows.length}` },
          { key: 'm', label: 'Manager reviews done', value: `${mgrDone} / ${rows.length}`, ...(mgrDone < rows.length && ['calibration', 'released'].includes(c.status) ? { tone: 'warn' as const } : {}) },
          { key: 'a', label: 'Acknowledged', value: rows.filter((r) => r.acknowledged_at).length },
        ]} />
      ) : null}
      {c?.status === 'draft' ? <Text size="sm" c="dimmed">Launching creates a review for every active employee in scope, with their goals for the period and the competencies of their role, and tells them their self review is open.</Text> : null}
      {hr && cal.data ? <Distribution overall={cal.data.distribution.overall} byDepartment={cal.data.distribution.byDepartment} labels={c?.rating_labels ?? []} /> : null}
      {c && c.status !== 'draft' ? (
        <>
          <SegmentedControl size="xs" value={filter} onChange={(v) => setFilter(v as typeof filter)} style={{ alignSelf: 'flex-start' }}
            data={[{ value: 'all', label: 'Everyone' }, { value: 'self', label: 'Self review pending' }, { value: 'manager', label: 'Manager review pending' }]} />
          <DataTable<CycleReviewRow> rows={shown} rowKey={(r) => r.id} loading={q.loading} error={q.error} onRowClick={(r) => setOpen(r.id)} emptyTitle="Nobody here"
            columns={[
              { key: 'name', header: 'Employee', render: (r) => <div><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{[r.designation, r.department].filter(Boolean).join(' · ')}</Text></div> },
              { key: 'mgr', header: 'Manager', render: (r) => <Text size="sm">{r.manager_name ?? <Text span c="orange.7" size="sm">None (HR reviews)</Text>}</Text> },
              { key: 'self', header: 'Self', render: (r) => <Badge size="sm" variant="light" color={r.self_submitted_at ? 'teal' : 'gray'}>{r.self_submitted_at ? 'Done' : 'Pending'}</Badge> },
              { key: 'm', header: 'Manager', render: (r) => <Badge size="sm" variant="light" color={r.manager_submitted_at ? 'teal' : 'gray'}>{r.manager_submitted_at ? 'Done' : 'Pending'}</Badge> },
              ...(c.peer_enabled ? [{ key: 'p', header: 'Peers', render: (r: CycleReviewRow) => <Text size="sm">{r.peers_done}/{r.peers_total}{r.peers_pending ? <Text span c="orange.7" size="xs"> · {r.peers_pending} to approve</Text> : null}</Text> }] : []),
              { key: 'score', header: 'Rating', render: (r) => <Text size="sm">{r.final_rating ? ratingLabel(r.final_rating, c.rating_labels) : r.manager_score ? ratingLabel(r.manager_score, c.rating_labels) : '—'}</Text> },
            ]} />
        </>
      ) : null}
      <ReviewForm reviewId={open} onClose={() => setOpen(null)} onChanged={() => { void q.reload(); if (cal.data) void cal.reload(); }} />
      <ConfirmDialog opened={!!confirm} color="brand" title={confirm === 'launch' ? 'Launch this cycle?' : `Move to ${c ? PHASE_LABEL[nextPhase(c)] : ''}?`} confirmLabel={confirm === 'launch' ? 'Launch' : 'Continue'}
        message={confirm === 'launch' ? 'Everyone in scope is notified and the setup is locked.' : c?.status === 'calibration' ? 'Employees will see their final ratings and manager comments. Training needs become enrolments.' : 'Everyone involved in the next phase is notified.'}
        onConfirm={run} onClose={() => setConfirm(null)} />
    </Stack>
  );
}

function nextPhase(c: Cycle): Phase {
  const order: Phase[] = ['draft', 'self_review', 'manager_review', 'peer_review', 'calibration', 'released', 'closed'];
  let i = order.indexOf(c.status) + 1;
  if (order[i] === 'peer_review' && !c.peer_enabled) i++;
  return order[Math.min(i, order.length - 1)]!;
}

/** Rating distribution overall and by department, for calibration. */
function Distribution({ overall, byDepartment, labels }: { overall: number[]; byDepartment: Record<string, number[]>; labels: string[] }) {
  const total = overall.reduce((a, b) => a + b, 0) || 1;
  const COLORS = ['red', 'orange', 'gray', 'teal', 'brand'];
  return (
    <Paper withBorder radius="lg" p="md">
      <Stack gap="sm">
        <Text fw={600}>Rating distribution</Text>
        <Progress.Root size={22} radius="md">
          {overall.map((n, i) => (n ? <Progress.Section key={i} value={(n / total) * 100} color={COLORS[i]!}><Progress.Label>{i + 1}: {n}</Progress.Label></Progress.Section> : null))}
        </Progress.Root>
        <Group gap="md" wrap="wrap">{labels.map((l, i) => <Text key={i} size="xs" c="dimmed">{i + 1} = {l}</Text>)}</Group>
        <Table striped={false} highlightOnHover={false} verticalSpacing={4}>
          <Table.Thead><Table.Tr><Table.Th>Department</Table.Th>{[1, 2, 3, 4, 5].map((n) => <Table.Th key={n} ta="right">{n}</Table.Th>)}<Table.Th ta="right">Average</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {Object.entries(byDepartment).map(([d, xs]) => {
              const cnt = xs.reduce((a, b) => a + b, 0);
              const avg = cnt ? xs.reduce((s, n, i) => s + n * (i + 1), 0) / cnt : 0;
              return <Table.Tr key={d}><Table.Td>{d}</Table.Td>{xs.map((n, i) => <Table.Td key={i} ta="right">{n || ''}</Table.Td>)}<Table.Td ta="right">{avg.toFixed(2)}</Table.Td></Table.Tr>;
            })}
          </Table.Tbody>
        </Table>
        <Text size="xs" c="dimmed">Open a review to set its final rating. Every change needs a reason and is recorded.</Text>
      </Stack>
    </Paper>
  );
}

const KIND = [{ value: 'knowledge', label: 'Knowledge' }, { value: 'skill', label: 'Skill' }, { value: 'attitude', label: 'Attitude' }];

function Competencies() {
  const q = useQuery(() => perfApi.competencies({ all: '1' }), []);
  const [edit, setEdit] = useState<Partial<Competency> | null>(null); const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!edit) return; setBusy(true);
    try {
      const body = { kind: edit.kind ?? 'skill', name: edit.name, description: edit.description || null, indicators: (edit.indicators ?? []).filter((i) => i.text.trim()), designations: edit.designations ?? [], grades: edit.grades ?? [], active: edit.active ?? true };
      if (edit.id) await perfApi.updateCompetency(edit.id, body); else await perfApi.createCompetency(body);
      toast.ok('Competency saved.'); setEdit(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const indicator = (level: number) => edit?.indicators?.find((i) => i.level === level)?.text ?? '';
  const setIndicator = (level: number, text: string) => setEdit((x) => x && { ...x, indicators: [...(x.indicators ?? []).filter((i) => i.level !== level), { level, text }].sort((a, b) => a.level - b.level) });
  return (
    <Stack gap="md">
      <SectionTitle hint="Knowledge, skills and attitudes expected in each role. Each review rates the competencies that apply to the person's designation and grade."
        actions={<Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => setEdit({ kind: 'skill', indicators: [], designations: [], grades: [], active: true })}>New competency</Button>}>Competency framework</SectionTitle>
      <DataTable<Competency> rows={q.data?.competencies ?? []} rowKey={(c) => c.id} loading={q.loading} error={q.error} onRowClick={(c) => setEdit(c)}
        emptyTitle="No competencies yet" emptyHint="Start with five or six that matter across the company, such as Communication and Ownership."
        columns={[
          { key: 'name', header: 'Competency', render: (c) => <div><Text size="sm" fw={500}>{c.name}</Text>{c.description ? <Text size="xs" c="dimmed" lineClamp={1}>{c.description}</Text> : null}</div> },
          { key: 'kind', header: 'Type', render: (c) => <Badge variant="light" color={c.kind === 'knowledge' ? 'blue' : c.kind === 'skill' ? 'grape' : 'teal'}>{c.kind}</Badge> },
          { key: 'applies', header: 'Applies to', render: (c) => <Text size="sm">{[c.designations.length ? c.designations.join(', ') : null, c.grades.length ? `Grades ${c.grades.join(', ')}` : null].filter(Boolean).join(' · ') || 'Everyone'}</Text> },
          { key: 'active', header: '', render: (c) => (c.active ? null : <Badge variant="light" color="gray">Inactive</Badge>) },
        ]} />
      <Drawer opened={!!edit} onClose={() => setEdit(null)} position="right" size="lg" title={edit?.id ? 'Edit competency' : 'New competency'}>
        {edit ? (
          <Stack gap="sm">
            <SimpleGrid cols={2}>
              <TextInput label="Name" required value={edit.name ?? ''} onChange={(e) => { const v = e.currentTarget.value; setEdit((x) => x && { ...x, name: v }); }} />
              <Select label="Type" value={edit.kind ?? 'skill'} onChange={(v) => setEdit((x) => x && { ...x, kind: (v ?? 'skill') as Competency['kind'] })} data={KIND} allowDeselect={false} />
            </SimpleGrid>
            <Textarea label="Description" autosize minRows={2} value={edit.description ?? ''} onChange={(e) => { const v = e.currentTarget.value; setEdit((x) => x && { ...x, description: v }); }} />
            <Text fw={600} size="sm">What each level looks like</Text>
            {[1, 2, 3, 4, 5].map((l) => <TextInput key={l} label={`Level ${l}`} value={indicator(l)} onChange={(e) => setIndicator(l, e.currentTarget.value)} />)}
            <TagsField label="Designations" description="Empty means every designation" value={edit.designations ?? []} onChange={(v) => setEdit((x) => x && { ...x, designations: v })} />
            <TagsField label="Grades" description="Empty means every grade" value={edit.grades ?? []} onChange={(v) => setEdit((x) => x && { ...x, grades: v })} />
            <Switch label="Active" checked={edit.active ?? true} onChange={(e) => { const v = e.currentTarget.checked; setEdit((x) => x && { ...x, active: v }); }} />
            <Group justify="flex-end"><Button onClick={save} loading={busy} disabled={!edit.name?.trim()}>Save</Button></Group>
          </Stack>
        ) : null}
      </Drawer>
    </Stack>
  );
}

/** Free-text list entry: type and press Enter. */
function TagsField({ label, description, value, onChange }: { label: string; description?: string; value: string[]; onChange: (v: string[]) => void }) {
  const [text, setText] = useState('');
  const add = () => { const t = text.trim(); if (t && !value.includes(t)) onChange([...value, t]); setText(''); };
  return (
    <Stack gap={4}>
      <TextInput label={label} description={description} placeholder="Type and press Enter" value={text} onChange={(e) => setText(e.currentTarget.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} onBlur={add} />
      {value.length ? <Group gap={4}>{value.map((v) => <Badge key={v} variant="light" rightSection={<ActionIcon size="xs" variant="transparent" aria-label={`Remove ${v}`} onClick={() => onChange(value.filter((x) => x !== v))}><IconTrash size={10} /></ActionIcon>}>{v}</Badge>)}</Group> : null}
    </Stack>
  );
}
