import { Alert, Button, Code, Drawer, Group, Modal, SegmentedControl, Stack, Text, Textarea, TextInput } from '@mantine/core';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { employees as empApi } from '../../api';
import type { Employee } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { EmployeeForm } from '../../components/EmployeeForm';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function EmployeesPage() {
  const { t } = useI18n();
  const nav = useNavigate();
  const [search, setSearch] = useState(''); const [status, setStatus] = useState<'active' | 'exited'>('active');
  const q = useQuery(() => empApi.list({ search, status }), [search, status]);
  const [create, setCreate] = useState(false); const [busy, setBusy] = useState(false);
  const [imp, setImp] = useState(false); const [csv, setCsv] = useState(''); const [impResult, setImpResult] = useState<{ imported: number; valid?: number; errors: Array<{ row: number; message: string }> } | null>(null); const [impBusy, setImpBusy] = useState(false);
  const runImport = async (dryRun: boolean) => {
    setImpBusy(true);
    try { const r = await empApi.importCsv(csv, dryRun); setImpResult(r); if (!dryRun) { toast.ok(`${r.imported} employees imported.`); await q.reload(); } }
    catch (e) { const err = e as { details?: unknown; message: string; issues?: unknown }; setImpResult({ imported: 0, errors: [{ row: 0, message: err.message }] }); }
    finally { setImpBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('employees')} actions={<Group gap="xs"><Button variant="light" onClick={() => setImp(true)}>{t('importCsv')}</Button><Button onClick={() => setCreate(true)}>{t('create')}</Button></Group>} />
      <Group><TextInput placeholder={t('search')} value={search} onChange={(e) => setSearch(e.currentTarget.value)} w={260} /><SegmentedControl value={status} onChange={(v) => setStatus(v as 'active' | 'exited')} data={[{ value: 'active', label: 'Active' }, { value: 'exited', label: 'Exited' }]} /></Group>
      <DataTable<Employee> rows={q.data?.employees ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error} emptyTitle="No employees yet." emptyHint="Create one, or import a CSV with emp_code and name_en columns." onRowClick={(r) => nav(`/hr/employees/${r.id}`)}
        columns={[
          { key: 'code', header: t('code'), render: (r) => r.emp_code, sticky: true },
          { key: 'name', header: t('name'), render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text>{r.name_ne ? <Text size="xs" c="dimmed">{r.name_ne}</Text> : null}</> },
          { key: 'designation', header: 'Designation', render: (r) => r.designation ?? '—' },
          { key: 'regime', header: 'Regime', render: (r) => r.regime.toUpperCase() },
          { key: 'join', header: 'Joined', render: (r) => r.join_date ?? '—' },
          { key: 'status', header: t('status'), render: (r) => <StatusBadge status={r.status} /> },
        ]} />
      <Drawer opened={create} onClose={() => setCreate(false)} title="New employee" position="right" size="xl">
        <EmployeeForm busy={busy} submitLabel={t('create')} onSubmit={async (draft) => { setBusy(true); try { const r = await empApi.create(draft); toast.ok('Employee created.'); setCreate(false); nav(`/hr/employees/${r.id}`); } catch (e) { toast.err(e); } finally { setBusy(false); } }} />
      </Drawer>
      <Modal opened={imp} onClose={() => { setImp(false); setImpResult(null); }} title={t('importCsv')} size="lg" centered>
        <Stack>
          <Text size="sm">Paste CSV with a header row. Required: <Code>emp_code,name_en</Code>. Optional: name_ne, gender, dob, marital_status, join_date, designation, grade, phone, email, regime, ssf_id, pan, bank_code, bank_branch, bank_account, ot_eligible.</Text>
          <Textarea value={csv} onChange={(e) => setCsv(e.currentTarget.value)} autosize minRows={6} placeholder={'emp_code,name_en,name_ne,join_date\nE010,Hari Lal,हरि लाल,2026-01-01'} ff="monospace" />
          {impResult ? <Alert color={impResult.errors.length ? 'red' : 'teal'} variant="light" title={impResult.errors.length ? `${impResult.errors.length} problem(s)` : `${impResult.valid ?? impResult.imported} row(s) valid`}>
            <Stack gap={2}>{impResult.errors.slice(0, 20).map((e, i) => <Text key={i} size="xs">{e.row ? `Row ${e.row}: ` : ''}{e.message}</Text>)}</Stack>
          </Alert> : null}
          <Group justify="flex-end"><Button variant="default" loading={impBusy} onClick={() => runImport(true)}>{t('dryRun')}</Button><Button loading={impBusy} disabled={!csv.trim() || (impResult?.errors.length ?? 0) > 0 || !impResult} onClick={() => runImport(false)}>Import</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
