import { ActionIcon, Badge, Box, Button, Drawer, Group, NumberInput, Paper, SegmentedControl, Select, Stack, Switch, Text, Textarea, TextInput, Tooltip } from '@mantine/core';
import { IconBuilding, IconPencil, IconPlus, IconSitemap, IconTrash } from '@tabler/icons-react';
import { useState, type ReactNode } from 'react';
import { people, type StructBranch, type StructDesignation, type StructUnit } from '../../api/people';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;
type Kind = 'unit' | 'branch' | 'designation';
type Draft = Record<string, string | number | boolean | null | undefined> & { id?: string };

/** Departments and teams as a tree, each with its head and headcount. */
function UnitTree({ units, onEdit, onAdd, onDelete }: { units: StructUnit[]; onEdit: (u: StructUnit) => void; onAdd: (parent: StructUnit) => void; onDelete: (u: StructUnit) => void }) {
  const ids = new Set(units.map((u) => u.id));
  const kids = (p: string | null) => units.filter((u) => (p ? u.parent_id === p : !u.parent_id || !ids.has(u.parent_id)));
  const total = (u: StructUnit): number => u.headcount + kids(u.id).reduce((s, k) => s + total(k), 0);
  const row = (u: StructUnit, depth: number): ReactNode => (
    <Box key={u.id}>
      <Group justify="space-between" wrap="nowrap" py={8} pl={depth * 24} pr="xs" style={{ borderBottom: '1px solid var(--hj-line-soft)' }}>
        <Group gap="sm" wrap="nowrap" style={{ minWidth: 0 }}>
          {u.kind === 'department' ? <IconBuilding size={18} stroke={1.6} color="var(--hj-muted)" /> : <IconSitemap size={18} stroke={1.6} color="var(--hj-muted)" />}
          <div style={{ minWidth: 0 }}>
            <Group gap={6} wrap="nowrap">
              <Text size="sm" fw={600} truncate>{u.name_en}</Text>
              <Text size="xs" c="dimmed" ff="monospace">{u.code}</Text>
            </Group>
            <Text size="xs" c="dimmed" truncate>{u.head_name ? `Head: ${u.head_name}` : 'No head set'}</Text>
          </div>
        </Group>
        <Group gap={4} wrap="nowrap">
          <Tooltip label="People in this unit and its sub-units"><Badge variant="light" color="gray" size="sm">{total(u)} people</Badge></Tooltip>
          <Tooltip label="Add a team under this unit"><ActionIcon variant="subtle" color="gray" onClick={() => onAdd(u)} aria-label={`Add a team under ${u.name_en}`}><IconPlus {...ICON} /></ActionIcon></Tooltip>
          <Tooltip label="Edit"><ActionIcon variant="subtle" color="gray" onClick={() => onEdit(u)} aria-label={`Edit ${u.name_en}`}><IconPencil {...ICON} /></ActionIcon></Tooltip>
          <Tooltip label="Delete"><ActionIcon variant="subtle" color="red" onClick={() => onDelete(u)} aria-label={`Delete ${u.name_en}`}><IconTrash {...ICON} /></ActionIcon></Tooltip>
        </Group>
      </Group>
      {kids(u.id).map((k) => row(k, depth + 1))}
    </Box>
  );
  return <Paper withBorder px="sm">{kids(null).map((u) => row(u, 0))}</Paper>;
}

/** HR maintains the company's departments, teams, branches and designations. */
export default function OrgStructurePage() {
  const q = useQuery(() => people.structure(), []);
  const policy = useQuery(() => people.policy(), []);
  const [edit, setEdit] = useState<{ kind: Kind; draft: Draft } | null>(null);
  const [del, setDel] = useState<{ kind: Kind; id: string; name: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const units = q.data?.units ?? [];
  const set = (k: string, v: Draft[string]) => setEdit((e) => e && ({ ...e, draft: { ...e.draft, [k]: v } }));

  const openUnit = (u?: StructUnit, parent?: StructUnit) => setEdit({ kind: 'unit', draft: u
    ? { id: u.id, kind: u.kind, code: u.code, nameEn: u.name_en, parentId: u.parent_id, branchId: u.branch_id, headEmployeeId: u.head_employee_id, description: u.description ?? '' }
    : { kind: parent ? 'team' : 'department', code: '', nameEn: '', parentId: parent?.id ?? null, branchId: parent?.branch_id ?? null, headEmployeeId: null, description: '' } });
  const openBranch = (b?: StructBranch) => setEdit({ kind: 'branch', draft: b
    ? { id: b.id, code: b.code, nameEn: b.name_en, address: b.address ?? '', phone: b.phone ?? '', headEmployeeId: b.head_employee_id }
    : { code: '', nameEn: '', address: '', phone: '', headEmployeeId: null } });
  const openDesignation = (d?: StructDesignation) => setEdit({ kind: 'designation', draft: d
    ? { id: d.id, code: d.code, nameEn: d.name_en, grade: d.grade ?? '', jobLevel: d.job_level, orgUnitId: d.org_unit_id, description: d.description ?? '', active: d.active }
    : { code: '', nameEn: '', grade: '', jobLevel: null, orgUnitId: null, description: '', active: true } });

  const save = async () => {
    if (!edit) return;
    setBusy(true);
    try {
      const { id, ...rest } = edit.draft;
      // Empty optional text becomes null so it clears; required text is sent as typed.
      const body = Object.fromEntries(Object.entries(rest).map(([k, v]) => [k, v === '' && !['code', 'nameEn'].includes(k) ? null : v]));
      if (edit.kind === 'unit') { if (id) await people.updateUnit(id, body); else await people.createUnit(body); }
      if (edit.kind === 'branch') { if (id) await people.updateBranch(id, body); else await people.createBranch(body); }
      if (edit.kind === 'designation') { if (id) await people.updateDesignation(id, body); else await people.createDesignation(body); }
      toast.ok('Saved.'); setEdit(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const remove = async () => {
    if (!del) return;
    try {
      if (del.kind === 'unit') await people.deleteUnit(del.id);
      if (del.kind === 'branch') await people.deleteBranch(del.id);
      if (del.kind === 'designation') await people.deleteDesignation(del.id);
      toast.ok(`${del.name} deleted.`); await q.reload();
    } catch (e) { toast.err(e, 'Not deleted'); }
  };
  const unitOptions = (exclude?: string) => units.filter((u) => u.id !== exclude).map((u) => ({ value: u.id, label: u.name_en }));
  const d = edit?.draft ?? {};
  const str = (k: string) => (d[k] as string | null | undefined) ?? '';

  return (
    <Stack gap="xl">
      <PageHeader title="Org structure" subtitle="Departments, teams, branches and designations. They drive approvals, reports and the org chart." />

      <div>
        <SectionTitle hint="Departments hold teams. Each unit can have a head." actions={<Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => openUnit()}>New department</Button>}>Departments and teams</SectionTitle>
        {q.loading && !q.data ? null : units.length
          ? <UnitTree units={units} onEdit={(u) => openUnit(u)} onAdd={(p) => openUnit(undefined, p)} onDelete={(u) => setDel({ kind: 'unit', id: u.id, name: u.name_en })} />
          : <EmptyState icon={<IconBuilding size={20} stroke={1.6} />} title="No departments yet" hint="Start with your main departments, then add teams under them." action={{ label: 'Add a department', onClick: () => openUnit() }} />}
      </div>

      <div>
        <SectionTitle hint="Job titles with their grade and level. Picking one on a promotion sets both." actions={<Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => openDesignation()}>New designation</Button>}>Designations</SectionTitle>
        <DataTable<StructDesignation> rows={q.data?.designations ?? []} rowKey={(x) => x.id} loading={q.loading} error={q.error} onRowClick={(x) => openDesignation(x)}
          emptyTitle="No designations yet" emptyHint="Add the job titles you use, such as Software Engineer or Accountant."
          columns={[
            { key: 'n', header: 'Designation', render: (x) => <div><Text size="sm" fw={500}>{x.name_en}{!x.active ? <Badge ml={6} size="xs" color="gray" variant="light">Inactive</Badge> : null}</Text><Text size="xs" c="dimmed" ff="monospace">{x.code}</Text></div> },
            { key: 'g', header: 'Grade', render: (x) => x.grade ?? '—' },
            { key: 'l', header: 'Level', align: 'right', render: (x) => <span className="num">{x.job_level ?? '—'}</span> },
            { key: 'u', header: 'Department', render: (x) => units.find((u) => u.id === x.org_unit_id)?.name_en ?? 'Any' },
            { key: 'h', header: 'People', align: 'right', render: (x) => <span className="num">{x.headcount}</span> },
            { key: 'x', header: '', align: 'right', render: (x) => <ActionIcon variant="subtle" color="red" onClick={(e) => { e.stopPropagation(); setDel({ kind: 'designation', id: x.id, name: x.name_en }); }} aria-label={`Delete ${x.name_en}`}><IconTrash {...ICON} /></ActionIcon> },
          ]} />
      </div>

      <div>
        <SectionTitle hint="Offices and sites. Holidays, devices and reports can be set per branch." actions={<Button size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => openBranch()}>New branch</Button>}>Branches</SectionTitle>
        <DataTable<StructBranch> rows={q.data?.branches ?? []} rowKey={(x) => x.id} loading={q.loading} error={q.error} onRowClick={(x) => openBranch(x)}
          emptyTitle="No branches yet" emptyHint="Add your head office first."
          columns={[
            { key: 'n', header: 'Branch', render: (x) => <div><Text size="sm" fw={500}>{x.name_en}</Text><Text size="xs" c="dimmed" ff="monospace">{x.code}</Text></div> },
            { key: 'a', header: 'Address', render: (x) => x.address ?? '—' },
            { key: 'hd', header: 'Head', render: (x) => x.head_name ?? '—' },
            { key: 'h', header: 'People', align: 'right', render: (x) => <span className="num">{x.headcount}</span> },
            { key: 'x', header: '', align: 'right', render: (x) => <ActionIcon variant="subtle" color="red" onClick={(e) => { e.stopPropagation(); setDel({ kind: 'branch', id: x.id, name: x.name_en }); }} aria-label={`Delete ${x.name_en}`}><IconTrash {...ICON} /></ActionIcon> },
          ]} />
      </div>

      <Paper withBorder p="lg">
        <Switch checked={!!policy.data?.policy.makerChecker} disabled={!policy.data}
          label="Second HR approval for bank and ID changes"
          description="When on, a change to someone's bank account, PAN or ID numbers made by one HR person waits until a different HR person approves it."
          onChange={async (e) => {
            const on = e.currentTarget.checked;
            try { await people.setPolicy({ makerChecker: on }); toast.ok(on ? 'Second approval turned on.' : 'Second approval turned off.'); await policy.reload(); } catch (err) { toast.err(err); }
          }} />
      </Paper>

      <Drawer opened={!!edit} onClose={() => setEdit(null)} position="right" size="md"
        title={edit ? `${edit.draft.id ? 'Edit' : 'New'} ${edit.kind === 'unit' ? (d.kind === 'team' ? 'team' : 'department') : edit.kind}` : ''}>
        {edit ? (
          <Stack gap="md">
            {edit.kind === 'unit' ? <SegmentedControl value={str('kind') || 'department'} onChange={(v) => set('kind', v)} data={[{ value: 'department', label: 'Department' }, { value: 'team', label: 'Team' }]} /> : null}
            <TextInput label="Name" required value={str('nameEn')} onChange={(e) => set('nameEn', e.currentTarget.value)} />
            <TextInput label="Code" required description="Short and unique, e.g. ENG or KTM" value={str('code')} onChange={(e) => set('code', e.currentTarget.value.toUpperCase())} maxLength={20} />
            {edit.kind === 'unit' ? (
              <>
                <Select label="Part of" placeholder="Top level" clearable searchable value={(d.parentId as string | null) ?? null} onChange={(v) => set('parentId', v)} data={unitOptions(edit.draft.id)} />
                <Select label="Branch" placeholder="All branches" clearable value={(d.branchId as string | null) ?? null} onChange={(v) => set('branchId', v)}
                  data={(q.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en }))} />
              </>
            ) : null}
            {edit.kind === 'designation' ? (
              <>
                <Group grow>
                  <TextInput label="Grade" placeholder="e.g. G5" value={str('grade')} onChange={(e) => set('grade', e.currentTarget.value)} />
                  <NumberInput label="Job level" min={1} max={20} value={(d.jobLevel as number | null) ?? ''} onChange={(v) => set('jobLevel', v === '' ? null : Number(v))} />
                </Group>
                <Select label="Department" placeholder="Any department" clearable searchable value={(d.orgUnitId as string | null) ?? null} onChange={(v) => set('orgUnitId', v)} data={unitOptions()} />
                <Switch label="Active" description="Inactive designations stay on the people who hold them but are not offered for new changes." checked={d.active !== false} onChange={(e) => set('active', e.currentTarget.checked)} />
              </>
            ) : null}
            {edit.kind === 'branch' ? (
              <>
                <Textarea label="Address" autosize minRows={2} value={str('address')} onChange={(e) => set('address', e.currentTarget.value)} />
                <TextInput label="Phone" value={str('phone')} onChange={(e) => set('phone', e.currentTarget.value)} />
              </>
            ) : null}
            {edit.kind !== 'designation'
              ? <EmployeePicker label="Head" value={(d.headEmployeeId as string | null) ?? null} onChange={(v) => set('headEmployeeId', v)} />
              : null}
            {edit.kind !== 'branch' ? <Textarea label="Description" autosize minRows={2} value={str('description')} onChange={(e) => set('description', e.currentTarget.value)} /> : null}
            <Group justify="flex-end" mt="sm">
              <Button variant="default" onClick={() => setEdit(null)}>Cancel</Button>
              <Button onClick={save} loading={busy} disabled={!str('nameEn').trim() || !str('code').trim()}>Save</Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>
      <ConfirmDialog opened={!!del} onClose={() => setDel(null)} title={`Delete ${del?.name ?? ''}?`} confirmLabel="Delete"
        message="This cannot be undone. Anything still assigned to it must be moved first." onConfirm={remove} />
    </Stack>
  );
}
