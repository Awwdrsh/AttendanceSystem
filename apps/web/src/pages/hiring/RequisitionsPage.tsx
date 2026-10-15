import { Button, Drawer, Group, NumberInput, SegmentedControl, Select, SimpleGrid, Stack, Text, Textarea, TextInput } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { useAuth } from '../../auth/AuthProvider';
import { hiring, type Requisition } from '../../api/hiring';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { fmtDateShort, fmtMoney } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { JobForm } from './JobForm';
import { ICON, RequisitionStatusBadge, useLookups } from './shared';

const BLANK = { title: '', designation: '', orgUnitId: null as string | null, branchId: null as string | null, headcount: 1, salaryMin: '' as number | '', salaryMax: '' as number | '', kind: 'new', replacingEmployeeId: null as string | null, justification: '' };

function RequestForm({ opened, onClose, onSaved }: { opened: boolean; onClose: () => void; onSaved: () => void }) {
  const lookups = useLookups();
  const [f, setF] = useState(BLANK); const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      await hiring.createRequisition({
        title: f.title, designation: f.designation || null, orgUnitId: f.orgUnitId, branchId: f.branchId, headcount: f.headcount,
        salaryMin: f.salaryMin === '' ? null : String(f.salaryMin), salaryMax: f.salaryMax === '' ? null : String(f.salaryMax), kind: f.kind,
        replacingEmployeeId: f.kind === 'replacement' ? f.replacingEmployeeId : null, justification: f.justification,
      });
      toast.ok('Request sent to HR for approval.'); setF(BLANK); onSaved(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="lg" title={<Text fw={600}>Request to hire</Text>}>
      <Stack gap="md">
        <SegmentedControl value={f.kind} onChange={(v) => setF({ ...f, kind: v })} data={[{ value: 'new', label: 'New position' }, { value: 'replacement', label: 'Replacement' }]} />
        {f.kind === 'replacement' ? <EmployeePicker label="Replacing" status="all" required value={f.replacingEmployeeId} onChange={(v) => setF({ ...f, replacingEmployeeId: v })} /> : null}
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <TextInput label="Role" required placeholder="Backend engineer" value={f.title} onChange={(e) => setF({ ...f, title: e.currentTarget.value })} maxLength={120} />
          <TextInput label="Designation" value={f.designation} onChange={(e) => setF({ ...f, designation: e.currentTarget.value })} maxLength={120} />
          <Select label="Department" clearable searchable value={f.orgUnitId} onChange={(v) => setF({ ...f, orgUnitId: v })} data={lookups.orgUnits.map((u) => ({ value: u.id, label: u.name_en }))} />
          <Select label="Branch" clearable value={f.branchId} onChange={(v) => setF({ ...f, branchId: v })} data={lookups.branches.map((b) => ({ value: b.id, label: b.name_en }))} />
          <NumberInput label="How many people" min={1} max={500} value={f.headcount} onChange={(v) => setF({ ...f, headcount: Number(v) || 1 })} />
          <div />
          <NumberInput label="Budget from (NPR a month)" min={0} thousandSeparator="," value={f.salaryMin} onChange={(v) => setF({ ...f, salaryMin: v === '' ? '' : Number(v) })} />
          <NumberInput label="Budget to (NPR a month)" min={0} thousandSeparator="," value={f.salaryMax} onChange={(v) => setF({ ...f, salaryMax: v === '' ? '' : Number(v) })} />
        </SimpleGrid>
        <Textarea label="Why is this needed?" required description="At least 10 characters. HR sees this when deciding." autosize minRows={3} maxLength={4000} value={f.justification} onChange={(e) => setF({ ...f, justification: e.currentTarget.value })} />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button onClick={save} loading={busy} disabled={f.title.trim().length < 2 || f.justification.trim().length < 10 || (f.kind === 'replacement' && !f.replacingEmployeeId)}>Send request</Button>
        </Group>
      </Stack>
    </Drawer>
  );
}

/** Managers ask for headcount; HR approves in Approvals and turns approved requests into job openings. */
export default function RequisitionsPage() {
  const { hasRole, user } = useAuth();
  const hr = hasRole('hr');
  const q = useQuery(() => hiring.requisitions(), []);
  const [creating, setCreating] = useState(false);
  const [toJob, setToJob] = useState<Requisition | null>(null);
  const cancel = async (r: Requisition) => {
    try { await hiring.cancelRequisition(r.id); toast.ok('Request cancelled.'); await q.reload(); } catch (e) { toast.err(e); }
  };
  const budget = (r: Requisition) => (r.salary_min || r.salary_max ? `NPR ${[r.salary_min, r.salary_max].filter(Boolean).map((x) => fmtMoney(x, { decimals: 0 })).join(' – ')}` : '—');
  return (
    <Stack gap="lg">
      <PageHeader title="Requisitions" subtitle="Ask for a new or replacement hire. HR approves it, then opens a job for it."
        actions={<Button leftSection={<IconPlus {...ICON} />} onClick={() => setCreating(true)}>Request to hire</Button>} />
      <DataTable<Requisition> rows={q.data?.requisitions ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error}
        emptyTitle="No hiring requests" emptyHint="Raise a request when your team needs another person."
        columns={[
          { key: 'role', header: 'Role', render: (r) => <div><Text size="sm" fw={500}>{r.headcount} × {r.title}</Text><Text size="xs" c="dimmed">{[r.department, r.branch, r.kind === 'replacement' ? `Replacing ${r.replacing_name ?? ''}` : 'New position'].filter(Boolean).join(' · ')}</Text></div> },
          { key: 'budget', header: 'Budget', render: (r) => <Text size="sm">{budget(r)}</Text> },
          { key: 'by', header: 'Raised by', render: (r) => <div><Text size="sm">{r.raised_by_name ?? '—'}</Text><Text size="xs" c="dimmed">{fmtDateShort(r.created_at.slice(0, 10))}</Text></div> },
          { key: 'status', header: 'Status', render: (r) => <div><RequisitionStatusBadge status={r.status} />{r.status === 'approved' || r.status === 'filled' ? <Text size="xs" c="dimmed" mt={2}>{r.filled} of {r.headcount} hired · {r.jobs} job{r.jobs === 1 ? '' : 's'}</Text> : null}{r.decision_reason ? <Text size="xs" c="dimmed" mt={2}>{r.decision_reason}</Text> : null}</div> },
          {
            key: 'actions', header: '', align: 'right', render: (r) => (
              <Group gap="xs" justify="flex-end" wrap="nowrap">
                {hr && r.status === 'approved' ? <Button size="xs" variant="light" onClick={() => setToJob(r)}>Create job</Button> : null}
                {(hr || r.raised_by === user?.id) && (r.status === 'pending' || r.status === 'approved') ? <Button size="xs" variant="subtle" color="red" onClick={() => cancel(r)}>Cancel</Button> : null}
              </Group>
            ),
          },
        ]} />
      <RequestForm opened={creating} onClose={() => setCreating(false)} onSaved={() => void q.reload()} />
      <JobForm opened={!!toJob} requisition={toJob} onClose={() => setToJob(null)} onSaved={() => { setToJob(null); void q.reload(); toast.info('Find the new job under Jobs and publish it when ready.'); }} />
    </Stack>
  );
}
