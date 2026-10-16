import { ActionIcon, Button, Checkbox, Group, Modal, NumberInput, Select, SimpleGrid, Stack, Text, Textarea, TextInput, Tooltip } from '@mantine/core';
import { IconPencil, IconPlus, IconTrash, IconUpload } from '@tabler/icons-react';
import { useState } from 'react';
import { compliance, type OpeningBalance, type OpeningBody } from '../../../api/compliance';
import { ApiError } from '../../../api/client';
import { ConfirmDialog } from '../../../components/ConfirmDialog';
import { DataTable } from '../../../components/DataTable';
import { EmployeePicker } from '../../../components/EmployeePicker';
import { PageHeader } from '../../../components/PageHeader';
import { fmtMoney, safeAdToBs, todayNpt } from '../../../lib/format';
import { toast } from '../../../lib/toast';
import { useQuery } from '../../../lib/useQuery';

const SOURCE: Record<OpeningBalance['source'], string> = { previous_employer: 'Previous employer', go_live: 'Before Hajiri', other: 'Other' };
const empty = (fy: number): OpeningBody => ({ employeeId: '', fiscalYearStart: fy, gross: '0', taxable: '0', tdsPaid: '0', retirementEmployee: '0', retirementEmployer: '0', festivalPaid: false, months: 0, source: 'previous_employer', note: null });
const TEMPLATE = 'emp_code,fiscal_year_start,gross,taxable,tds_paid,retirement_employee,retirement_employer,festival_paid,months,source,note\n';

/** HR: income and tax a person had this fiscal year before Hajiri paid them, so the yearly tax is projected correctly. */
export default function OpeningBalancesPage() {
  const bs = safeAdToBs(todayNpt());
  const curFy = bs ? (bs.month >= 4 ? bs.year : bs.year - 1) : 2083;
  const [fy, setFy] = useState(curFy);
  const q = useQuery(() => compliance.openings(fy), [fy]);
  const [form, setForm] = useState<OpeningBody | null>(null);
  const [del, setDel] = useState<OpeningBalance | null>(null);
  const [csvOpen, setCsvOpen] = useState(false); const [csv, setCsv] = useState(''); const [csvErrors, setCsvErrors] = useState<Array<{ row: number; message: string }>>([]);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (!form) return;
    setBusy(true);
    try { await compliance.saveOpening(form); toast.ok('Opening balance saved. It counts from the next pay run.'); setForm(null); await q.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const doImport = async () => {
    setBusy(true); setCsvErrors([]);
    try { const r = await compliance.importOpenings(csv); toast.ok(`${r.imported} opening balance${r.imported === 1 ? '' : 's'} imported.`); setCsvOpen(false); setCsv(''); await q.reload(); }
    catch (e) {
      const d = e instanceof ApiError ? (e.details as { errors?: Array<{ row: number; message: string }> } | undefined) : undefined;
      if (d?.errors?.length) setCsvErrors(d.errors); else toast.err(e);
    } finally { setBusy(false); }
  };
  const num = (k: keyof OpeningBody, label: string, hint?: string) => (
    <NumberInput label={label} description={hint} value={Number(form?.[k] ?? 0)} min={0} thousandSeparator="," decimalScale={2}
      onChange={(v) => form && setForm({ ...form, [k]: String(Number(v) || 0) })} leftSection={<Text size="xs" c="dimmed">NPR</Text>} leftSectionWidth={42} />
  );
  const fyOptions = [curFy - 1, curFy, curFy + 1].map((y) => ({ value: String(y), label: `FY ${y}/${String(y + 1).slice(-2)}` }));

  return (
    <Stack gap="lg">
      <PageHeader title="Opening balances" subtitle="Pay and tax from earlier in the fiscal year that Hajiri did not pay: a previous employer for someone who joined mid-year, or months paid by the old system before go-live. They are added to year-to-date so each person's yearly tax is right."
        actions={<>
          <Select aria-label="Fiscal year" value={String(fy)} onChange={(v) => v && setFy(Number(v))} data={fyOptions} allowDeselect={false} w={140} />
          <Button variant="default" leftSection={<IconUpload size={16} stroke={1.6} />} onClick={() => { setCsv(TEMPLATE); setCsvErrors([]); setCsvOpen(true); }}>Import CSV</Button>
          <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setForm(empty(fy))}>Add</Button>
        </>} />
      <DataTable<OpeningBalance> rows={q.data?.balances ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error}
        emptyTitle="No opening balances" emptyHint="Most people need none. Add one for a person who joined mid-year with income from a previous employer, or when moving from another payroll system."
        columns={[
          { key: 'emp', header: 'Employee', render: (r) => <><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code} · {SOURCE[r.source]}</Text></> },
          { key: 'gross', header: 'Gross', align: 'right', render: (r) => <span className="num">{fmtMoney(r.gross)}</span> },
          { key: 'taxable', header: 'Taxable', align: 'right', render: (r) => <span className="num">{fmtMoney(r.taxable)}</span> },
          { key: 'tds', header: 'Tax paid', align: 'right', render: (r) => <span className="num">{fmtMoney(r.tds_paid)}</span> },
          { key: 'ret', header: 'Retirement (employee / employer)', align: 'right', render: (r) => <span className="num">{fmtMoney(r.retirement_employee, { decimals: 0 })} / {fmtMoney(r.retirement_employer, { decimals: 0 })}</span> },
          { key: 'fest', header: 'Festival paid', render: (r) => r.festival_paid ? 'Yes' : 'No' },
          { key: 'act', header: '', align: 'right', render: (r) => <Group gap={4} justify="flex-end" wrap="nowrap">
            <Tooltip label="Edit"><ActionIcon variant="subtle" color="gray" aria-label="Edit" onClick={() => setForm({ employeeId: r.employee_id, fiscalYearStart: r.fiscal_year_start, gross: r.gross, taxable: r.taxable, tdsPaid: r.tds_paid, retirementEmployee: r.retirement_employee, retirementEmployer: r.retirement_employer, festivalPaid: r.festival_paid, months: r.months, source: r.source, note: r.note })}><IconPencil size={16} stroke={1.6} /></ActionIcon></Tooltip>
            <Tooltip label="Delete"><ActionIcon variant="subtle" color="red" aria-label="Delete" onClick={() => setDel(r)}><IconTrash size={16} stroke={1.6} /></ActionIcon></Tooltip>
          </Group> },
        ]} />

      <Modal opened={!!form} onClose={() => setForm(null)} title="Opening balance" size="lg" centered>
        {form ? <Stack>
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <EmployeePicker value={form.employeeId || null} onChange={(id) => setForm({ ...form, employeeId: id ?? '' })} required status="all" />
            <Select label="Source" value={form.source} onChange={(v) => v && setForm({ ...form, source: v as OpeningBody['source'] })} allowDeselect={false}
              data={Object.entries(SOURCE).map(([value, label]) => ({ value, label }))} />
            {num('gross', 'Gross pay', 'Everything paid in the year before Hajiri')}
            {num('taxable', 'Taxable pay', 'Including the employer\'s SSF/PF share if it was taxed')}
            {num('tdsPaid', 'Tax deducted (TDS and SST)')}
            <NumberInput label="Months" description="Months of pay included" value={form.months} min={0} max={12} onChange={(v) => setForm({ ...form, months: Number(v) || 0 })} />
            {num('retirementEmployee', 'Retirement, employee', 'SSF/PF employee share and CIT')}
            {num('retirementEmployer', 'Retirement, employer', 'SSF/PF employer share and gratuity')}
          </SimpleGrid>
          <Checkbox label="Festival allowance (Dashain) already paid this year" checked={form.festivalPaid} onChange={(e) => setForm({ ...form, festivalPaid: e.currentTarget.checked })} />
          <TextInput label="Note" value={form.note ?? ''} onChange={(e) => setForm({ ...form, note: e.currentTarget.value || null })} placeholder="Optional, e.g. previous employer's name" />
          <Group justify="flex-end"><Button variant="default" onClick={() => setForm(null)}>Cancel</Button><Button loading={busy} disabled={!form.employeeId} onClick={save}>Save</Button></Group>
        </Stack> : null}
      </Modal>

      <Modal opened={csvOpen} onClose={() => setCsvOpen(false)} title="Import opening balances" size="xl" centered>
        <Stack>
          <Text size="sm" c="dimmed">One row per employee and fiscal year, using the employee code. Amounts in NPR; festival_paid is yes or no; source is previous_employer, go_live or other. Nothing is saved if any row has an error.</Text>
          <Textarea value={csv} onChange={(e) => setCsv(e.currentTarget.value)} autosize minRows={8} maxRows={18} styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)', fontSize: 12 } }} />
          {csvErrors.length ? <Stack gap={2}>{csvErrors.slice(0, 12).map((x, i) => <Text key={i} size="xs" c="red">Row {x.row}: {x.message}</Text>)}{csvErrors.length > 12 ? <Text size="xs" c="red">and {csvErrors.length - 12} more</Text> : null}</Stack> : null}
          <Group justify="flex-end"><Button variant="default" onClick={() => setCsvOpen(false)}>Cancel</Button><Button loading={busy} disabled={csv.trim().split('\n').length < 2} onClick={doImport}>Import</Button></Group>
        </Stack>
      </Modal>
      <ConfirmDialog opened={!!del} onClose={() => setDel(null)} title="Delete opening balance?" confirmLabel="Delete"
        message={`${del?.name_en ?? ''}'s opening balance for FY ${fy} will no longer count in their tax. Pay runs already locked are not changed.`}
        onConfirm={async () => { if (!del) return; try { await compliance.deleteOpening(del.id); toast.ok('Deleted.'); setDel(null); await q.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
