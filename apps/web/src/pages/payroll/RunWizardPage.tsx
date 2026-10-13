import { Alert, Badge, Button, Group, Modal, Paper, Select, SimpleGrid, Stack, Stepper, Text, TextInput, Textarea } from '@mantine/core';
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import type { PayslipSummary, Preflight } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { bsMonthLabel, fmtMoney } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ORDER = ['draft', 'preflight_failed', 'computed', 'reviewed', 'approved', 'locked', 'disbursed', 'filed'];
const stepOf = (s: string) => ({ draft: 0, preflight_failed: 0, computed: 2, reviewed: 3, approved: 3, locked: 4, disbursed: 4, filed: 4 }[s] ?? 0);

export default function RunWizardPage() {
  const { id = '' } = useParams();
  const { t, lang, numerals } = useI18n();
  const { user, hasRole } = useAuth();
  const q = useQuery(() => payApi.run(id), [id]);
  const inputs = useQuery(() => payApi.inputs(id), [id]);
  const [step, setStep] = useState(0);
  const [pf, setPf] = useState<Preflight | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [addInput, setAddInput] = useState(false); const [inEmp, setInEmp] = useState<string | null>(null); const [inCode, setInCode] = useState('ALLOWANCE'); const [inAmt, setInAmt] = useState(''); const [inNote, setInNote] = useState('');
  const [notes, setNotes] = useState(''); const [bank, setBank] = useState('GENERIC');
  const [override, setOverride] = useState(false); const [overrideReason, setOverrideReason] = useState('');
  const comps = useQuery(() => payApi.components(), []);
  const run = q.data?.run; const period = q.data?.period;
  useEffect(() => { if (run) { setStep(stepOf(run.status)); if (run.preflight) setPf(run.preflight); } }, [run?.status]);
  if (q.error) return <EmptyState title="Run not found" hint={q.error} />;
  if (!run || !period) return null;
  const locked = !!run.locked_at;
  const act = async (key: string, fn: () => Promise<unknown>, okMsg: string) => { setBusy(key); try { await fn(); toast.ok(okMsg); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };
  const isMaker = run.maker_id === user?.id;
  const totals = run.totals ?? {};
  const varianceRows = (run.variance?.byEmployee ?? []);
  const slips = q.data?.payslips ?? [];
  const canCompute = ['draft', 'preflight_failed', 'computed', 'reviewed'].includes(run.status);

  return (
    <Stack>
      <PageHeader title={`${bsMonthLabel(period.bs_year, period.bs_month, lang, numerals)} · ${run.run_type}`} subtitle={`${period.start_ad} → ${period.end_ad} (${period.days} days)`} actions={<Group gap="xs"><StatusBadge status={run.status} /><Button component={Link} to="/payroll" variant="default" size="xs">All runs</Button></Group>} />
      <Stepper active={step} onStepClick={setStep} allowNextStepsSelect={ORDER.indexOf(run.status) >= 2} size="sm">
        <Stepper.Step label={t('preflight')} description={pf ? (pf.ok ? 'clear' : `${pf.blockers.length} blockers`) : undefined} />
        <Stepper.Step label={t('inputs')} description={`${inputs.data?.inputs.length ?? 0} items`} />
        <Stepper.Step label={t('review')} description={run.computed_at ? `${slips.length} payslips` : undefined} />
        <Stepper.Step label={t('approve')} description={run.approved_at ? 'signed' : undefined} />
        <Stepper.Step label={t('disburse')} description={run.filed_at ? 'filed' : run.disbursed_at ? 'paid' : locked ? 'locked' : undefined} />
      </Stepper>

      {step === 0 ? <Stack>
        <Group><Button loading={busy === 'pf'} onClick={() => act('pf', async () => setPf(await payApi.preflight(id)), 'Pre-flight complete.')}>Run pre-flight</Button>
          {hasRole('hr') && !period.attendance_locked_at ? <Button variant="light" loading={busy === 'lock'} onClick={() => act('lock', () => payApi.lockAttendance(id), 'Attendance locked for the period.')}>{t('lockAttendance')}</Button> : <Badge variant="light" color="teal">attendance locked</Badge>}</Group>
        {pf ? <SimpleGrid cols={{ base: 1, md: 2 }}>
          <Paper withBorder p="md" radius="md"><Text fw={600} c="red">{t('blockers')} ({pf.blockers.length})</Text>{pf.blockers.length === 0 ? <Text size="sm" c="dimmed">None. You can calculate.</Text> : pf.blockers.map((b) => <Alert key={b.code} color="red" variant="light" mt="xs" title={b.message}>{b.count !== undefined ? `${b.count} affected` : ''}{b.employees?.length ? ` · ${b.employees.slice(0, 5).join(', ')}${b.employees.length > 5 ? '…' : ''}` : ''}</Alert>)}</Paper>
          <Paper withBorder p="md" radius="md"><Text fw={600} c="orange">{t('warnings')} ({pf.warnings.length})</Text>{pf.warnings.length === 0 ? <Text size="sm" c="dimmed">None.</Text> : pf.warnings.map((w) => <Alert key={w.code} color="orange" variant="light" mt="xs" title={w.message}>{w.count !== undefined ? `${w.count} affected` : ''}{w.employees?.length ? ` · ${w.employees.slice(0, 5).join(', ')}` : ''}</Alert>)}</Paper>
        </SimpleGrid> : <Text size="sm" c="dimmed">Pre-flight checks attendance lock, unresolved exceptions, missing bank/PAN/SSF details, joiners and leavers, and unapproved overtime.</Text>}
        <Group justify="flex-end">{pf && !pf.ok && hasRole('hr') && canCompute ? <Button variant="light" color="orange" onClick={() => setOverride(true)}>Calculate anyway (HR override)</Button> : null}<Button disabled={!pf || !pf.ok} onClick={() => setStep(1)}>Next: {t('inputs')}</Button></Group>
      </Stack> : null}

      {step === 1 ? <Stack>
        <Group><Button disabled={locked} onClick={() => setAddInput(true)}>Add one-time earning / deduction</Button></Group>
        <DataTable rows={inputs.data?.inputs ?? []} rowKey={(i) => i.id} loading={inputs.loading} emptyTitle="No one-time inputs." emptyHint="Arrears, allowances, advances and deductions for this period go here. Approved OT slips are picked up automatically."
          columns={[{ key: 'emp_code', header: t('code') }, { key: 'component_code', header: 'Component' }, { key: 'amount', header: 'Amount', render: (i) => fmtMoney(i.amount, { numerals }), align: 'right' }, { key: 'note', header: 'Note', render: (i) => i.note ?? '' }]} />
        <Group justify="space-between"><Button variant="default" onClick={() => setStep(0)}>Back</Button><Button onClick={() => setStep(2)}>Next: {t('review')}</Button></Group>
      </Stack> : null}

      {step === 2 ? <Stack>
        <Group><Button disabled={!canCompute} loading={busy === 'compute'} onClick={() => act('compute', () => payApi.compute(id), 'Payslips calculated.')}>{run.computed_at ? 'Recalculate' : t('compute')}</Button>
          {run.status === 'computed' ? <Button variant="light" loading={busy === 'review'} onClick={() => act('review', () => payApi.review(id, notes || undefined), 'Marked reviewed.')}>{t('markReviewed')}</Button> : null}</Group>
        {run.computed_at ? <SimpleGrid cols={{ base: 2, md: 5 }} spacing="sm">{[['Payslips', String(totals.payslips ?? slips.length)], [t('grossPay'), fmtMoney(totals.gross, { numerals })], ['Employer SSF/PF', fmtMoney(totals.employerStatutory ?? totals.employer_statutory, { numerals })], ['TDS', fmtMoney(totals.tds, { numerals })], [t('netPay'), fmtMoney(totals.net, { numerals })]].map(([k, v]) => <Paper key={k} withBorder p="sm" radius="md"><Text size="xs" c="dimmed">{k}</Text><Text fw={700}>{v}</Text></Paper>)}</SimpleGrid> : null}
        {run.variance ? <Text size="sm" c="dimmed">{run.variance.flagged} flagged row(s) sort first · previous run net {fmtMoney(run.variance.previousNet, { numerals })}</Text> : null}
        <DataTable<PayslipSummary> rows={slips} rowKey={(s) => s.id} loading={q.loading} emptyTitle="Not calculated yet." highlight={(s) => (s.flags?.length ?? 0) > 0} onRowClick={(s) => window.open(`/me/payslips/${s.id}`, '_blank')}
          columns={[
            { key: 'emp', header: t('name'), render: (s) => <><Text size="sm" fw={500}>{s.name_en}</Text><Text size="xs" c="dimmed">{s.emp_code}</Text></> },
            { key: 'days', header: 'Paid / LOP', render: (s) => `${s.paid_days} / ${s.lop_days}` },
            { key: 'gross', header: t('grossPay'), render: (s) => fmtMoney(s.gross, { numerals, currency: false }), align: 'right' },
            { key: 'tds', header: 'TDS', render: (s) => fmtMoney(s.tds, { numerals, currency: false }), align: 'right' },
            { key: 'net', header: t('netPay'), render: (s) => <strong>{fmtMoney(s.net, { numerals, currency: false })}</strong>, align: 'right' },
            { key: 'prev', header: 'Δ vs last', render: (s) => { const v = varianceRows.find((x) => x.employeeId === s.employee_id); if (!v || v.previousNet === null) return <Badge size="xs" variant="light" color="violet">new</Badge>; const d = Number(v.net) - Number(v.previousNet); const pct = Number(v.previousNet) ? Math.round((d / Number(v.previousNet)) * 100) : 0; return <Text size="xs" c={Math.abs(pct) > 10 ? 'orange' : 'dimmed'}>{d >= 0 ? '+' : ''}{pct}%</Text>; }, align: 'right' },
            { key: 'flags', header: t('flags'), render: (s) => <Group gap={2}>{(s.flags ?? []).map((f) => <Badge key={f} size="xs" variant="outline" color="orange">{f}</Badge>)}</Group> },
          ]} />
        <Textarea label="Reviewer notes" value={notes} onChange={(e) => setNotes(e.currentTarget.value)} autosize minRows={2} disabled={locked} />
        <Group justify="space-between"><Button variant="default" onClick={() => setStep(1)}>Back</Button><Button disabled={!['reviewed', 'approved', 'locked', 'disbursed', 'filed'].includes(run.status)} onClick={() => setStep(3)}>Next: {t('approve')}</Button></Group>
      </Stack> : null}

      {step === 3 ? <Stack>
        <SimpleGrid cols={{ base: 2, md: 4 }} spacing="sm">{[['Headcount', String(totals.payslips ?? slips.length)], [t('grossPay'), fmtMoney(totals.gross, { numerals })], ['TDS', fmtMoney(totals.tds, { numerals })], [t('netPay'), fmtMoney(totals.net, { numerals })]].map(([k, v]) => <Paper key={k} withBorder p="sm" radius="md"><Text size="xs" c="dimmed">{k}</Text><Text fw={700}>{v}</Text></Paper>)}</SimpleGrid>
        {isMaker ? <Alert variant="light" color="orange">{t('checkerSameAsMaker')}</Alert> : null}
        {!hasRole('finance') ? <Alert variant="light" color="gray">Approval requires the finance role.</Alert> : null}
        {run.approved_at ? <Alert variant="light" color="teal" title="Approved">Signed {new Date(run.approved_at).toLocaleString()} · signature {run.approval_signature?.slice(0, 20) ?? ''}…</Alert> : null}
        <Group justify="space-between"><Button variant="default" onClick={() => setStep(2)}>Back</Button>
          <Group>{run.status === 'reviewed' ? <Button h={48} disabled={isMaker || !hasRole('finance')} loading={busy === 'approve'} onClick={() => act('approve', async () => { const r = await payApi.approve(id); if (r.pendingSecondApproval) toast.info('Above the two-approver threshold: a second finance approver must also approve before the run can be locked.', 'First approval recorded'); }, 'Approval recorded.')}>{t('approve')}</Button> : null}
            {run.status === 'approved' ? <Button h={48} color="teal" loading={busy === 'lockrun'} onClick={() => act('lockrun', () => payApi.lock(id), 'Run locked. Attendance for the period is now immutable.')}>{t('lockRun')}</Button> : null}
            {locked ? <Button onClick={() => setStep(4)}>Next: {t('disburse')}</Button> : null}</Group></Group>
      </Stack> : null}

      {step === 4 ? <Stack>
        {!locked ? <Alert variant="light" color="gray">Lock the run first; files are generated from locked runs only.</Alert> : null}
        <Paper withBorder p="md" radius="md"><Text fw={600} mb="xs">{t('bankFile')}</Text>
          <Group><Select value={bank} onChange={(v) => v && setBank(v)} allowDeselect={false} data={['NMB', 'NABIL', 'NIC_ASIA', 'EVEREST', 'PRABHU', 'GENERIC']} w={160} /><Button disabled={!locked || !hasRole('finance')} loading={busy === 'bank'} onClick={() => act('bank', async () => { const r = await payApi.bankFile(id, bank); if (r.warnings.length) toast.info(r.warnings.slice(0, 3).join('; '), 'Validation notes'); }, 'Bank file generated.')}>Generate</Button></Group>
          <Stack gap={4} mt="sm">{(q.data?.bankFiles ?? []).map((f) => <Group key={f.id} justify="space-between"><Text size="sm">{f.filename} · {f.bank_code} · {new Date(f.created_at).toLocaleString()} <Text span size="xs" c="dimmed" ff="monospace">{f.checksum.slice(0, 12)}</Text></Text><Button size="xs" variant="light" onClick={() => payApi.downloadBankFile(id, f.id, f.filename).catch((e) => toast.err(e))}>{t('download')}</Button></Group>)}</Stack>
        </Paper>
        <Paper withBorder p="md" radius="md"><Text fw={600} mb="xs">Statutory outputs</Text>
          <Group><Button variant="light" disabled={!locked} onClick={() => payApi.ssfSheet(id).catch((e) => toast.err(e))}>SSF sheet</Button><Button variant="light" disabled={!locked || !hasRole('finance')} onClick={() => payApi.etds(id).catch((e) => toast.err(e))}>ETDS annex</Button><Button variant="light" disabled={!locked} onClick={() => payApi.journal(id).catch((e) => toast.err(e))}>Journal CSV</Button></Group>
          <Text size="xs" c="dimmed" mt="xs">SSF deposit within 25 days of month end; ETDS by the 25th of the following month (plan §16.5).</Text>
        </Paper>
        <Group justify="space-between"><Button variant="default" onClick={() => setStep(3)}>Back</Button>
          <Group>{run.status === 'locked' && hasRole('finance') ? <Button loading={busy === 'disb'} onClick={() => act('disb', () => payApi.disburse(id), 'Marked as paid.')}>Mark disbursed</Button> : null}
            {locked ? <Button variant="light" loading={busy === 'pub'} onClick={() => act('pub', () => payApi.publish(id), 'Payslips published; employees notified.')}>{t('publish')}</Button> : null}
            {run.status === 'disbursed' && hasRole('finance') ? <Button color="teal" loading={busy === 'file'} onClick={() => act('file', () => payApi.file(id), 'Run filed.')}>Mark filed</Button> : null}</Group></Group>
      </Stack> : null}

      <Modal opened={override} onClose={() => setOverride(false)} title="Calculate despite blockers" centered>
        <Stack>
          <Alert variant="light" color="orange">Pre-flight found blockers. Calculating anyway is recorded in the audit log with your reason; fix the underlying data before approval.</Alert>
          <Textarea label="Reason (at least 5 characters)" value={overrideReason} onChange={(e) => setOverrideReason(e.currentTarget.value)} autosize minRows={2} required />
          <Group justify="flex-end"><Button variant="default" onClick={() => setOverride(false)}>Back</Button><Button color="orange" disabled={overrideReason.trim().length < 5} loading={busy === 'compute'} onClick={() => act('compute', async () => { await payApi.compute(id, overrideReason.trim()); setOverride(false); setStep(2); }, 'Payslips calculated with an override.')}>Calculate anyway</Button></Group>
        </Stack>
      </Modal>
      <Modal opened={addInput} onClose={() => setAddInput(false)} title="One-time input" centered>
        <Stack>
          <EmployeePicker value={inEmp} onChange={setInEmp} required />
          <Select label="Component" value={inCode} onChange={(v) => v && setInCode(v)} data={(comps.data?.components ?? []).filter((c) => c.active && c.type !== 'statistical').map((c) => ({ value: c.code, label: `${c.code} · ${c.name_en} (${c.type})` }))} searchable />
          <TextInput label="Amount (NPR)" value={inAmt} onChange={(e) => setInAmt(e.currentTarget.value)} placeholder="0.00" required />
          <TextInput label="Note" value={inNote} onChange={(e) => setInNote(e.currentTarget.value)} />
          <Group justify="flex-end"><Button disabled={!inEmp || !/^\d+(\.\d{1,2})?$/.test(inAmt)} onClick={async () => { try { await payApi.addInputs(id, [{ employeeId: inEmp!, componentCode: inCode, amount: inAmt, ...(inNote ? { note: inNote } : {}) }]); setAddInput(false); setInAmt(''); await inputs.reload(); } catch (e) { toast.err(e); } }}>Add</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
