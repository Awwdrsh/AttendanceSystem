import { Alert, Avatar, Button, Code, Drawer, Group, List, Modal, SegmentedControl, Stack, Text, Textarea, TextInput } from '@mantine/core';
import { IconCheck, IconPlus, IconSearch, IconUpload } from '@tabler/icons-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { employees as empApi } from '../../api';
import type { Employee } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { EmployeeForm } from '../../components/EmployeeForm';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { fmtAd } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

type ImportResult = { imported: number; valid?: number; errors: Array<{ row: number; message: string }> };

export default function EmployeesPage() {
  const nav = useNavigate();
  const [search, setSearch] = useState(''); const [status, setStatus] = useState<'active' | 'exited'>('active');
  const q = useQuery(() => empApi.list({ search, status }), [search, status]);
  const [create, setCreate] = useState(false); const [busy, setBusy] = useState(false);
  const [imp, setImp] = useState(false); const [csv, setCsv] = useState(''); const [impResult, setImpResult] = useState<ImportResult | null>(null); const [impBusy, setImpBusy] = useState(false);
  const runImport = async (dryRun: boolean) => {
    setImpBusy(true);
    try { const r = await empApi.importCsv(csv, dryRun); setImpResult(r); if (!dryRun) { toast.ok(`${r.imported} employees imported.`); await q.reload(); } }
    catch (e) {
      const err = e as { details?: { errors?: Array<{ row: number; message: string }>; valid?: number }; message: string };
      if (Array.isArray(err.details?.errors)) setImpResult({ imported: 0, ...(err.details.valid !== undefined ? { valid: err.details.valid } : {}), errors: err.details.errors });
      else setImpResult({ imported: 0, errors: [{ row: 0, message: err.message }] });
    }
    finally { setImpBusy(false); }
  };
  const closeImport = () => { setImp(false); setImpResult(null); };
  const rows = q.data?.employees ?? [];
  const filtered = search.trim() !== '';
  const errCount = impResult?.errors.length ?? 0;
  return (
    <Stack gap="md">
      <PageHeader title="Employees" subtitle="Everyone on the payroll, with their code, role and status."
        actions={<>
          <Button variant="default" leftSection={<IconUpload size={16} stroke={1.6} />} onClick={() => setImp(true)}>Import CSV</Button>
          <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setCreate(true)}>Add employee</Button>
        </>} />
      <Group gap="sm" wrap="wrap">
        <TextInput placeholder="Search by name or code" leftSection={<IconSearch size={16} stroke={1.6} />} value={search} onChange={(e) => setSearch(e.currentTarget.value)} w={{ base: '100%', sm: 300 }} aria-label="Search employees" />
        <SegmentedControl value={status} onChange={(v) => setStatus(v as 'active' | 'exited')} data={[{ value: 'active', label: 'Active' }, { value: 'exited', label: 'Exited' }]} />
        {q.data ? <Text size="sm" c="dimmed" ml="auto" className="num">{rows.length} {rows.length === 1 ? 'person' : 'people'}</Text> : null}
      </Group>
      <DataTable<Employee> rows={rows} rowKey={(r) => r.id} loading={q.loading} error={q.error}
        emptyTitle={filtered ? 'No employees match your search' : status === 'exited' ? 'No exited employees' : 'No employees yet'}
        emptyHint={filtered ? 'Try a different name or code.' : status === 'exited' ? 'People appear here once an exit date is set on their profile.' : 'Add your first employee, or import a CSV with emp_code and name_en columns.'}
        onRowClick={(r) => nav(`/employees/${r.id}`)}
        columns={[
          { key: 'name', header: 'Name', render: (r) => (
            <Group gap="sm" wrap="nowrap">
              <Avatar name={r.name_en} size={32} radius="xl" color="brand" variant="light" />
              <div style={{ minWidth: 0 }}>
                <Text size="sm" fw={500} truncate>{r.name_en}</Text>
                {r.designation || r.email ? <Text size="xs" c="dimmed" truncate>{r.designation ?? r.email}</Text> : null}
              </div>
            </Group>
          ) },
          { key: 'code', header: 'Code', render: (r) => <Text size="sm" c="dimmed" className="num">{r.emp_code}</Text> },
          { key: 'join', header: 'Joined', render: (r) => <span className="num">{fmtAd(r.join_date)}</span> },
          { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
        ]} />
      <Drawer opened={create} onClose={() => setCreate(false)} title="Add employee" position="right" size="lg">
        <EmployeeForm busy={busy} submitLabel="Add employee" onSubmit={async (draft) => { setBusy(true); try { const r = await empApi.create(draft); toast.ok('Employee added.'); setCreate(false); nav(`/employees/${r.id}`); } catch (e) { toast.err(e); } finally { setBusy(false); } }} />
      </Drawer>
      <Modal opened={imp} onClose={closeImport} title="Import employees from CSV" size="lg" centered>
        <Stack gap="sm">
          <Text size="sm">Paste a CSV with a header row. Required columns: <Code>emp_code</Code> and <Code>name_en</Code>.</Text>
          <Text size="xs" c="dimmed">Optional columns: gender, dob, marital_status, join_date, designation, grade, phone, email, regime, ssf_id, pan, bank_code, bank_branch, bank_account, ot_eligible. Dates use YYYY-MM-DD.</Text>
          <Textarea value={csv} onChange={(e) => { setCsv(e.currentTarget.value); setImpResult(null); }} autosize minRows={6} maxRows={14} placeholder={'emp_code,name_en,join_date,designation,work_from_home\nE010,Hari Lal,2026-01-01,Accountant,yes'} ff="monospace" fz="xs" aria-label="CSV contents" />
          {impResult ? (
            errCount ? (
              <Alert color="red" variant="light" title={`${errCount} ${errCount === 1 ? 'row needs' : 'rows need'} fixing before you can import`}>
                <List size="xs" spacing={2}>{impResult.errors.slice(0, 20).map((e, i) => <List.Item key={i}>{e.row ? `Row ${e.row}: ` : ''}{e.message}</List.Item>)}</List>
                {errCount > 20 ? <Text size="xs" c="dimmed" mt={4}>and {errCount - 20} more</Text> : null}
              </Alert>
            ) : (
              <Alert color="brand" variant="light" icon={<IconCheck size={16} stroke={1.6} />} title={`${impResult.valid ?? impResult.imported} ${(impResult.valid ?? impResult.imported) === 1 ? 'row' : 'rows'} valid`} />
            )
          ) : null}
          <Group justify="space-between" wrap="wrap">
            <Text size="xs" c="dimmed">Check the file first. Nothing is saved until you import.</Text>
            <Group gap="xs">
              <Button variant="default" loading={impBusy} disabled={!csv.trim()} onClick={() => runImport(true)}>Check file</Button>
              <Button leftSection={<IconUpload size={16} stroke={1.6} />} loading={impBusy} disabled={!csv.trim() || errCount > 0 || !impResult} onClick={() => runImport(false)}>Import</Button>
            </Group>
          </Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
