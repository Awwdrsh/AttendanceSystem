import { PayrollAttendanceCheck } from '../attendance-tools/PayrollAttendanceCheck';
import { Alert, Badge, Button, Group, Modal, Paper, ScrollArea, Select, SimpleGrid, Stack, Stepper, Table, Text, TextInput, Textarea } from '@mantine/core';
import { IconAlertCircle, IconAlertTriangle, IconCalculator, IconCheck, IconChevronLeft, IconCircleCheck, IconDownload, IconInfoCircle, IconLock, IconPlus, IconSend, IconShieldCheck } from '@tabler/icons-react';
import { useEffect, useState, type ReactNode } from 'react';
import { useParams } from 'react-router-dom';
import { payroll as payApi } from '../../api';
import type { PayslipSummary, Preflight } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { bsMonthLabel, fmtAd, fmtMoney, fmtTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { SalarySheet } from './SalarySheet';

const ORDER = ['draft', 'preflight_failed', 'computed', 'reviewed', 'approved', 'locked', 'disbursed', 'filed'];
const stepOf = (s: string) => ({ draft: 0, preflight_failed: 0, computed: 2, reviewed: 3, approved: 3, locked: 4, disbursed: 4, filed: 4 }[s] ?? 0);
const RUN_TYPE_LABEL: Record<string, string> = { regular: 'Regular run', supplementary: 'Supplementary run', arrears: 'Arrears run', off_cycle: 'Off-cycle run', final_settlement: 'Final settlement', bonus: 'Bonus run' };
const ICON = { size: 16, stroke: 1.6 } as const;
const stamp = (iso: string) => `${fmtAd(iso)} at ${fmtTime(iso)}`;
const sum = (rows: PayslipSummary[], k: 'gross' | 'tds' | 'net') => rows.reduce((a, r) => a + Number(r[k] ?? 0), 0);

type Issue = Preflight['blockers'][number];

/** Compact list of pre-flight findings, one line each with an icon. */
function IssueList({ items, kind }: { items: Issue[]; kind: 'blocker' | 'warning' }) {
  const bad = kind === 'blocker';
  const Icon = bad ? IconAlertCircle : IconAlertTriangle;
  return (
    <Paper withBorder p="md">
      <Group gap={8} mb="xs">
        <Text fw={600} size="sm">{bad ? 'Blockers' : 'Warnings'}</Text>
        <Text size="sm" c="dimmed" className="num">{items.length}</Text>
      </Group>
      {items.length === 0 ? (
        <Group gap={8} wrap="nowrap"><IconCircleCheck {...ICON} color="var(--hj-ok)" /><Text size="sm" c="dimmed">{bad ? 'None. You can calculate.' : 'None.'}</Text></Group>
      ) : (
        <Stack gap={10}>
          {items.map((b) => (
            <Group key={b.code} gap={8} wrap="nowrap" align="flex-start">
              <Icon {...ICON} color={bad ? 'var(--hj-bad)' : 'var(--hj-warn)'} style={{ flex: 'none', marginTop: 2 }} />
              <div style={{ minWidth: 0 }}>
                <Text size="sm" fw={500}>{b.message}</Text>
                {b.count !== undefined || b.employees?.length ? (
                  <Text size="xs" c="dimmed">
                    {b.count !== undefined ? `${b.count} affected` : ''}
                    {b.count !== undefined && b.employees?.length ? ': ' : ''}
                    {b.employees?.length ? `${b.employees.slice(0, 5).join(', ')}${b.employees.length > 5 ? ` and ${b.employees.length - 5} more` : ''}` : ''}
                  </Text>
                ) : null}
              </div>
            </Group>
          ))}
        </Stack>
      )}
    </Paper>
  );
}

function StepNav({ onBack, children }: { onBack?: () => void; children?: ReactNode }) {
  return (
    <Group justify="space-between" mt="xs">
      {onBack ? <Button variant="subtle" color="gray" leftSection={<IconChevronLeft {...ICON} />} onClick={onBack}>Back</Button> : <span />}
      <Group gap="xs" wrap="wrap">{children}</Group>
    </Group>
  );
}

export default function RunWizardPage() {
  const { id = '' } = useParams();
  const { hasRole } = useAuth();
  const q = useQuery(() => payApi.run(id), [id]);
  const inputs = useQuery(() => payApi.inputs(id), [id]);
  const [step, setStep] = useState(0);
  const [pf, setPf] = useState<Preflight | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [addInput, setAddInput] = useState(false); const [inEmp, setInEmp] = useState<string | null>(null); const [inCode, setInCode] = useState('ALLOWANCE'); const [inAmt, setInAmt] = useState(''); const [inNote, setInNote] = useState('');
  const [notes, setNotes] = useState('');
  const [override, setOverride] = useState(false); const [overrideReason, setOverrideReason] = useState('');
  const comps = useQuery(() => payApi.components(), []);
  const run = q.data?.run; const period = q.data?.period;
  useEffect(() => { if (run) { setStep(stepOf(run.status)); if (run.preflight) setPf(run.preflight); } }, [run?.status]);
  if (q.error) return <EmptyState title="Pay run not found" hint={`${q.error} Go back to Pay runs and open it from the list.`} />;
  if (!run || !period) return null;
  const locked = !!run.locked_at;
  const act = async (key: string, fn: () => Promise<unknown>, okMsg: string) => { setBusy(key); try { await fn(); toast.ok(okMsg); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(null); } };
  const totals = run.totals ?? {};
  const varianceRows = (run.variance?.byEmployee ?? []);
  const slips = q.data?.payslips ?? [];
  const canCompute = ['draft', 'preflight_failed', 'computed', 'reviewed'].includes(run.status);
  const headcount = totals.payslips ?? String(slips.length);
  const totalGross = totals.gross ?? sum(slips, 'gross');
  const totalTds = totals.tds ?? sum(slips, 'tds');
  const totalNet = totals.net ?? sum(slips, 'net');
  const summary = [
    { key: 'n', label: 'Headcount', value: headcount },
    { key: 'gross', label: 'Gross pay', value: fmtMoney(totalGross) },
    { key: 'tds', label: 'TDS', value: fmtMoney(totalTds) },
    { key: 'net', label: 'Net pay', value: fmtMoney(totalNet) },
  ];
  const reviewStatuses = ['reviewed', 'approved', 'locked', 'disbursed', 'filed'];
  const published = slips.length > 0 && slips.every((x) => x.published_at);

  return (
    <Stack gap="lg">
      <PageHeader back={{ to: '/payroll', label: 'Pay runs' }}
        title={bsMonthLabel(period.bs_year, period.bs_month)}
        subtitle={`${RUN_TYPE_LABEL[run.run_type] ?? run.run_type.replace(/_/g, ' ')}, ${fmtAd(period.start_ad)} to ${fmtAd(period.end_ad)} (${period.days} days)`}
        actions={<StatusBadge status={run.status} />} />

      <Stepper active={step} onStepClick={setStep} allowNextStepsSelect={ORDER.indexOf(run.status) >= 2} size="sm" iconSize={30}>
        <Stepper.Step label="Pre-flight" description={pf ? (pf.ok ? 'Clear' : `${pf.blockers.length} ${pf.blockers.length === 1 ? 'blocker' : 'blockers'}`) : 'Not run'} />
        <Stepper.Step label="Inputs" description={`${inputs.data?.inputs.length ?? 0} items`} />
        <Stepper.Step label="Calculate & review" description={run.computed_at ? `${slips.length} payslips` : undefined} />
        <Stepper.Step label="Approve & lock" description={locked ? 'Locked' : run.approved_at ? 'Approved' : undefined} />
        <Stepper.Step label="Salary sheet" description={published ? 'Payslips published' : locked ? 'Final' : run.computed_at ? 'Draft' : undefined} />
      </Stepper>

      {step === 0 ? <Stack>
        <SectionTitle hint="Checks attendance lock, unresolved exceptions, missing bank, PAN and SSF details, joiners and leavers, and unapproved overtime."
          actions={<Group gap="xs">
            {hasRole('hr') && !period.attendance_locked_at
              ? <Button variant="default" leftSection={<IconLock {...ICON} />} loading={busy === 'lock'} onClick={() => act('lock', () => payApi.lockAttendance(id), 'Attendance locked for the period.')}>Lock attendance</Button>
              : <Group gap={6}><IconLock {...ICON} color="var(--hj-ok)" /><Text size="sm" c="dimmed">Attendance locked</Text></Group>}
            <Button variant={pf?.ok ? 'default' : 'filled'} leftSection={<IconShieldCheck {...ICON} />} loading={busy === 'pf'} onClick={() => act('pf', async () => setPf(await payApi.preflight(id)), 'Pre-flight checks complete.')}>{pf ? 'Run checks again' : 'Run checks'}</Button>
          </Group>}>Pre-flight checks</SectionTitle>
        {pf ? <SimpleGrid cols={{ base: 1, md: 2 }}>
          <IssueList items={pf.blockers} kind="blocker" />
          <IssueList items={pf.warnings} kind="warning" />
        </SimpleGrid> : <EmptyState compact icon={<IconShieldCheck size={20} stroke={1.6} />} title="Checks not run yet" hint="Run the checks to see anything that would stop this run from calculating correctly." />}
        {run.run_type === 'regular' ? <PayrollAttendanceCheck bsYear={period.bs_year} bsMonth={period.bs_month} /> : null}
        <StepNav>
          {pf && !pf.ok && hasRole('hr') && canCompute ? <Button variant="default" color="orange" leftSection={<IconAlertTriangle {...ICON} />} onClick={() => setOverride(true)}>Calculate anyway</Button> : null}
          {pf?.ok ? <Button onClick={() => setStep(1)}>Continue to inputs</Button> : <Button variant="default" disabled>Continue to inputs</Button>}
        </StepNav>
      </Stack> : null}

      {step === 1 ? <Stack>
        <SectionTitle hint="Arrears, allowances, advances and deductions for this period only. Approved overtime is picked up automatically."
          actions={<Button variant="default" leftSection={<IconPlus {...ICON} />} disabled={locked} onClick={() => setAddInput(true)}>Add input</Button>}>One-time inputs</SectionTitle>
        <DataTable rows={inputs.data?.inputs ?? []} rowKey={(i) => i.id} loading={inputs.loading} emptyTitle="No one-time inputs" emptyHint="Nothing extra this month. Add an earning or deduction if someone needs one, or continue."
          columns={[
            { key: 'emp_code', header: 'Employee code', render: (i) => <Text size="sm" fw={500}>{i.emp_code}</Text> },
            { key: 'component_code', header: 'Component' },
            { key: 'amount', header: 'Amount', align: 'right', render: (i) => <span className="num">{fmtMoney(i.amount)}</span> },
            { key: 'note', header: 'Note', render: (i) => <Text size="sm" c="dimmed">{i.note ?? ''}</Text> },
          ]} />
        <StepNav onBack={() => setStep(0)}><Button onClick={() => setStep(2)}>Continue to review</Button></StepNav>
      </Stack> : null}

      {step === 2 ? <Stack>
        <SectionTitle hint={run.computed_at ? `Last calculated ${stamp(run.computed_at)}. Flagged rows are highlighted; click a row to open the payslip.` : 'Calculate payslips for everyone in this run, then check them before approval.'}
          actions={<Group gap="xs">
            <Button variant={run.computed_at ? 'default' : 'filled'} leftSection={<IconCalculator {...ICON} />} disabled={!canCompute} loading={busy === 'compute'} onClick={() => act('compute', () => payApi.compute(id), 'Payslips calculated.')}>{run.computed_at ? 'Recalculate' : 'Calculate payslips'}</Button>
            {run.status === 'computed' ? <Button leftSection={<IconCheck {...ICON} />} loading={busy === 'review'} onClick={() => act('review', () => payApi.review(id, notes || undefined), 'Marked as reviewed.')}>Mark reviewed</Button> : null}
          </Group>}>Calculate and review</SectionTitle>
        {run.computed_at ? <StatStrip stats={[
          { key: 'n', label: 'Payslips', value: headcount },
          { key: 'gross', label: 'Gross pay', value: fmtMoney(totalGross) },
          { key: 'er', label: 'Employer SSF/PF', value: fmtMoney(totals.employerStatutory ?? totals.employer_statutory) },
          { key: 'tds', label: 'TDS', value: fmtMoney(totalTds) },
          { key: 'net', label: 'Net pay', value: fmtMoney(totalNet) },
        ]} /> : null}
        {run.variance ? <Group gap={6}><IconInfoCircle {...ICON} color="var(--hj-muted)" /><Text size="sm" c="dimmed">{run.variance.flagged} flagged {run.variance.flagged === 1 ? 'row' : 'rows'} shown first. Previous run net: <span className="num">{fmtMoney(run.variance.previousNet)}</span></Text></Group> : null}
        {slips.length === 0 ? <EmptyState compact icon={<IconCalculator size={20} stroke={1.6} />} title="Not calculated yet" hint="Payslips appear here after you calculate." /> : (
          <Paper withBorder style={{ overflow: 'hidden' }}>
            <ScrollArea.Autosize mah="70vh" type="auto">
              <Table stickyHeader miw={820}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Employee</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>Paid days</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>Unpaid days</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>Gross</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>TDS</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>Net</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>vs last run</Table.Th>
                    <Table.Th>Flags</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {slips.map((s) => {
                    const flagged = (s.flags?.length ?? 0) > 0;
                    const v = varianceRows.find((x) => x.employeeId === s.employee_id);
                    let delta: ReactNode;
                    if (!v || v.previousNet === null) delta = <Text size="xs" c="dimmed">New</Text>;
                    else {
                      const d = Number(v.net) - Number(v.previousNet);
                      const p = Number(v.previousNet) ? Math.round((d / Number(v.previousNet)) * 100) : 0;
                      delta = <Text size="xs" className="num" c={Math.abs(p) > 10 ? 'orange' : 'dimmed'}>{d >= 0 ? '+' : '−'}{Math.abs(p)}%</Text>;
                    }
                    return (
                      <Table.Tr key={s.id} onClick={() => window.open(`/me/payslips/${s.id}`, '_blank')} style={{ cursor: 'pointer', background: flagged ? 'var(--mantine-color-yellow-light)' : undefined }}>
                        <Table.Td><Text size="sm" fw={500}>{s.name_en}</Text><Text size="xs" c="dimmed">{s.emp_code}</Text></Table.Td>
                        <Table.Td ta="right" className="num">{s.paid_days}</Table.Td>
                        <Table.Td ta="right" className="num">{s.lop_days}</Table.Td>
                        <Table.Td ta="right" className="num">{fmtMoney(s.gross, { currency: false })}</Table.Td>
                        <Table.Td ta="right" className="num">{fmtMoney(s.tds, { currency: false })}</Table.Td>
                        <Table.Td ta="right" className="num" fw={600}>{fmtMoney(s.net, { currency: false })}</Table.Td>
                        <Table.Td ta="right">{delta}</Table.Td>
                        <Table.Td><Group gap={4}>{(s.flags ?? []).map((f) => <Badge key={f} size="sm" variant="light" color="orange">{f}</Badge>)}</Group></Table.Td>
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
                <Table.Tfoot style={{ borderTop: '1px solid var(--hj-line)' }}>
                  <Table.Tr>
                    <Table.Th>Total ({headcount})</Table.Th>
                    <Table.Th />
                    <Table.Th />
                    <Table.Th ta="right" className="num">{fmtMoney(totalGross, { currency: false })}</Table.Th>
                    <Table.Th ta="right" className="num">{fmtMoney(totalTds, { currency: false })}</Table.Th>
                    <Table.Th ta="right" className="num">{fmtMoney(totalNet, { currency: false })}</Table.Th>
                    <Table.Th />
                    <Table.Th />
                  </Table.Tr>
                </Table.Tfoot>
              </Table>
            </ScrollArea.Autosize>
          </Paper>
        )}
        <Textarea label="Reviewer notes" description="Saved with the review." value={notes} onChange={(e) => setNotes(e.currentTarget.value)} autosize minRows={2} disabled={locked} />
        <StepNav onBack={() => setStep(1)}>
          {run.computed_at ? <Button variant="default" onClick={() => setStep(4)}>Preview salary sheet</Button> : null}
          <Button variant={reviewStatuses.includes(run.status) ? 'filled' : 'default'} disabled={!reviewStatuses.includes(run.status)} onClick={() => setStep(3)}>Continue to approval</Button>
        </StepNav>
      </Stack> : null}

      {step === 3 ? <Stack>
        <SectionTitle hint="HR signs off the totals, then locks the run. Locking freezes the payslips and makes attendance for the period read-only; later corrections go in a supplementary run.">Approve and lock</SectionTitle>
        <StatStrip stats={summary} />
        {run.approved_at ? <Alert variant="light" color="brand" icon={<IconCircleCheck {...ICON} />} title="Approved">Signed {stamp(run.approved_at)}. Signature <Text span ff="monospace" size="xs">{run.approval_signature?.slice(0, 20) ?? ''}</Text></Alert> : null}
        {locked ? <Alert variant="light" color="brand" icon={<IconLock {...ICON} />} title="Locked">Locked {stamp(run.locked_at!)}. The salary sheet is final.</Alert> : null}
        {!run.approved_at && run.status !== 'reviewed' ? <Alert variant="light" color="gray" icon={<IconInfoCircle {...ICON} />}>Calculate the run and mark it reviewed before approving.</Alert> : null}
        <StepNav onBack={() => setStep(2)}>
          {run.status === 'reviewed' && hasRole('hr') ? <Button leftSection={<IconCheck {...ICON} />} loading={busy === 'approve'} onClick={() => act('approve', () => payApi.approve(id), 'Run approved.')}>Approve run</Button> : null}
          {run.status === 'approved' ? <Button leftSection={<IconLock {...ICON} />} loading={busy === 'lockrun'} onClick={() => act('lockrun', () => payApi.lock(id), 'Run locked. Attendance for the period is now read-only.')}>Lock run</Button> : null}
          {locked ? <Button onClick={() => setStep(4)}>Continue to salary sheet</Button> : null}
        </StepNav>
      </Stack> : null}

      {step === 4 ? <Stack>
        <SalarySheet runId={id} version={`${run.status}:${run.computed_at ?? ''}`} />
        <Paper withBorder p="md">
          <Group justify="space-between" wrap="wrap" gap="sm">
            <div>
              <Text fw={600} size="sm">Payslips and SSF</Text>
              <Text size="sm" c="dimmed">{published ? 'Payslips are published. Employees can see them under Payslips.' : locked ? 'Publish to let each employee see their own payslip. The SSF sheet lists contributions to deposit within 25 days of month end.' : 'Available once the run is locked.'}</Text>
            </div>
            <Group gap="xs">
              <Button variant="default" leftSection={<IconDownload {...ICON} />} disabled={!locked} onClick={() => payApi.ssfSheet(id).catch((e) => toast.err(e))}>SSF sheet</Button>
              <Button leftSection={<IconSend {...ICON} />} disabled={!locked || published} loading={busy === 'pub'} onClick={() => act('pub', () => payApi.publish(id), 'Payslips published and employees notified.')}>{published ? 'Published' : 'Publish payslips'}</Button>
            </Group>
          </Group>
        </Paper>
        <StepNav onBack={() => setStep(3)} />
      </Stack> : null}

      <Modal opened={override} onClose={() => setOverride(false)} title="Calculate despite blockers" centered>
        <Stack>
          <Text size="sm" c="dimmed">Pre-flight found blockers. Calculating anyway is recorded in the audit log with your reason. Fix the underlying data before approval.</Text>
          <Textarea label="Reason" description="At least 5 characters." value={overrideReason} onChange={(e) => setOverrideReason(e.currentTarget.value)} autosize minRows={2} required />
          <Group justify="flex-end"><Button variant="default" onClick={() => setOverride(false)}>Cancel</Button><Button color="orange" disabled={overrideReason.trim().length < 5} loading={busy === 'compute'} onClick={() => act('compute', async () => { await payApi.compute(id, overrideReason.trim()); setOverride(false); setStep(2); }, 'Payslips calculated with an override.')}>Calculate anyway</Button></Group>
        </Stack>
      </Modal>
      <Modal opened={addInput} onClose={() => setAddInput(false)} title="Add one-time input" centered>
        <Stack>
          <EmployeePicker value={inEmp} onChange={setInEmp} required />
          <Select label="Component" value={inCode} onChange={(v) => v && setInCode(v)} data={(comps.data?.components ?? []).filter((c) => c.active && c.type !== 'statistical').map((c) => ({ value: c.code, label: `${c.code}, ${c.name_en} (${c.type})` }))} searchable />
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label="Amount (NPR)" description="Up to 2 decimals." value={inAmt} onChange={(e) => setInAmt(e.currentTarget.value)} placeholder="0.00" required />
            <TextInput label="Note" value={inNote} onChange={(e) => setInNote(e.currentTarget.value)} />
          </SimpleGrid>
          <Group justify="flex-end"><Button variant="default" onClick={() => setAddInput(false)}>Cancel</Button><Button disabled={!inEmp || !/^\d+(\.\d{1,2})?$/.test(inAmt)} onClick={async () => { try { await payApi.addInputs(id, [{ employeeId: inEmp!, componentCode: inCode, amount: inAmt, ...(inNote ? { note: inNote } : {}) }]); setAddInput(false); setInAmt(''); await inputs.reload(); } catch (e) { toast.err(e); } }}>Add input</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
