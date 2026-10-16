import { Alert, Anchor, Badge, Button, Group, NumberInput, Paper, Select, SimpleGrid, Stack, Text, Textarea } from '@mantine/core';
import { IconInfoCircle, IconSend } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import type { UploadedFile } from '../../api/files';
import { taxSavings, type DeclarationValues, type TaxSavingsRequest } from '../../api/security';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { FileUpload } from '../../components/FileUpload';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { bsMonthLabel, fmtAd, fmtMoney, NPT_OFFSET_MIN, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { OptimumCitCard } from '../payroll/compliance/OptimumCitCard';

type Form = Record<keyof DeclarationValues, number>;
const EMPTY: Form = { lifePremium: 0, healthPremium: 0, citMonthly: 0, medicalCost: 0, otherRetirementAnnual: 0, donation: 0 };
const toValues = (f: Form): DeclarationValues => ({
  lifePremium: f.lifePremium.toFixed(2), healthPremium: f.healthPremium.toFixed(2), citMonthly: f.citMonthly.toFixed(2),
  medicalCost: f.medicalCost.toFixed(2), otherRetirementAnnual: f.otherRetirementAnnual.toFixed(2), donation: f.donation.toFixed(2),
});
const fromRow = (d: { life_premium: string; health_premium: string; cit_monthly: string; medical_cost: string; other_retirement_annual: string; donation?: string }): Form => ({
  lifePremium: Number(d.life_premium), healthPremium: Number(d.health_premium), citMonthly: Number(d.cit_monthly),
  medicalCost: Number(d.medical_cost), otherRetirementAnnual: Number(d.other_retirement_annual), donation: Number(d.donation ?? 0),
});
const day = (iso: string) => fmtAd(new Date(Date.parse(iso) + NPT_OFFSET_MIN * 60_000).toISOString().slice(0, 10));
const money = (v: string | undefined) => fmtMoney(v, { decimals: 0 });

/** Tax savings: declare premiums and contributions with receipts, see what they do to your tax, and send them to HR. */
export default function MyTaxPage() {
  const bs = safeAdToBs(todayNpt());
  const currentFy = bs ? (bs.month >= 4 ? bs.year : bs.year - 1) : 2083;
  const [fy, setFy] = useState(currentFy);
  const q = useQuery(() => taxSavings.get(fy), [fy]);
  const [f, setF] = useState<Form>(EMPTY);
  const [note, setNote] = useState('');
  const [proofs, setProofs] = useState<UploadedFile[]>([]);
  const [busy, setBusy] = useState(false);
  const [estimate, setEstimate] = useState<Awaited<ReturnType<typeof taxSavings.estimate>> | null>(null);

  useEffect(() => {
    const d = q.data?.pending ?? q.data?.declaration;
    setF(d ? fromRow(d) : EMPTY);
  }, [q.data]);

  // What-if estimate, recalculated shortly after the figures stop changing.
  const key = JSON.stringify(f);
  useEffect(() => {
    if (fy !== currentFy) { setEstimate(null); return; }
    const t = setTimeout(() => { taxSavings.estimate({ fiscalYearStart: fy, proposed: toValues(f) }).then(setEstimate).catch(() => setEstimate(null)); }, 400);
    return () => clearTimeout(t);
  }, [key, fy]); // eslint-disable-line react-hooks/exhaustive-deps

  const pending = q.data?.pending ?? null;
  const verified = q.data?.declaration?.status === 'verified' ? q.data.declaration : null;
  const changed = useMemo(() => {
    const base = pending ?? verified;
    return JSON.stringify(base ? fromRow(base) : EMPTY) !== key;
  }, [pending, verified, key]);

  const submit = async () => {
    setBusy(true);
    try {
      await taxSavings.submit({ ...toValues(f), fiscalYearStart: fy, ...(note.trim() ? { note: note.trim() } : {}), proofFileIds: proofs.map((p) => p.id) });
      toast.ok('Sent to HR. Your tax changes once they verify it.', 'Tax savings submitted');
      setNote(''); setProofs([]); await q.reload();
    } catch (e) { toast.err(e, 'Not sent'); } finally { setBusy(false); }
  };
  const cancel = async (r: TaxSavingsRequest) => {
    try { await taxSavings.cancel(r.id); toast.ok('Request withdrawn.'); await q.reload(); } catch (e) { toast.err(e); }
  };

  const n = (k: keyof Form, label: string, hint: string) => (
    <NumberInput label={label} description={hint} value={f[k]} onChange={(v) => setF({ ...f, [k]: Math.max(0, Number(v) || 0) })} min={0} thousandSeparator=","
      leftSection={<Text size="xs" c="dimmed">NPR</Text>} leftSectionWidth={42} />
  );
  const fyOptions = [currentFy - 1, currentFy, currentFy + 1].map((y) => ({ value: String(y), label: `FY ${y}/${String(y + 1).slice(-2)}` }));
  const cur = estimate?.current; const prop = estimate?.proposed;

  return (
    <Stack gap="lg">
      <PageHeader title="Tax savings" subtitle="Insurance premiums, retirement contributions and medical costs reduce your income tax within legal limits. Declare them with the receipts; HR verifies, then your monthly tax is recalculated."
        actions={<Select aria-label="Fiscal year" value={String(fy)} onChange={(v) => v && setFy(Number(v))} allowDeselect={false} data={fyOptions} w={140} />} />

      {fy === currentFy ? (
        estimate && !estimate.available ? (
          <Alert variant="light" color="gray" icon={<IconInfoCircle size={18} />}>{estimate.reason}</Alert>
        ) : cur ? (
          <StatStrip stats={[
            { key: 'taxable', label: 'Projected taxable income', value: money(cur.projectedAnnualTaxable), hint: `For FY ${fy}/${String(fy + 1).slice(-2)}` },
            { key: 'annual', label: 'Tax for the year', value: money(cur.annualTax), hint: 'With what HR has verified' },
            { key: 'month', label: 'Tax this month', value: money(cur.tdsThisMonth), hint: estimate?.bsYear ? bsMonthLabel(estimate.bsYear, estimate.bsMonth!) : undefined },
            ...(prop && changed ? [{ key: 'after', label: 'With your new figures', value: money(prop.tdsThisMonth), hint: Number(estimate?.annualSaving) > 0 ? `a month; saves ${money(estimate?.annualSaving)} this year` : 'a month', ...(Number(estimate?.annualSaving) > 0 ? { tone: 'ok' as const } : {}) }] : []),
          ]} />
        ) : null
      ) : <Text size="sm" c="dimmed">Tax estimates are shown for the current fiscal year.</Text>}
      {fy === currentFy ? <OptimumCitCard onUse={(monthly) => setF((x) => ({ ...x, citMonthly: monthly }))} /> : null}

      <Paper withBorder p="lg">
        <SectionTitle hint="Annual amounts unless marked monthly." actions={pending ? <Badge variant="light" color="orange">Waiting for HR</Badge> : verified ? <Badge variant="light" color="teal">Verified</Badge> : <Text size="sm" c="dimmed">Nothing declared</Text>}>
          Your declaration
        </SectionTitle>
        <SimpleGrid cols={{ base: 1, sm: 2 }} mt="sm">
          {n('lifePremium', 'Life insurance premium', 'Deductible up to NPR 40,000 a year')}
          {n('healthPremium', 'Health insurance premium', 'Deductible up to NPR 20,000 a year')}
          {n('citMonthly', 'Extra CIT contribution (monthly)', 'Counts toward the retirement limit with SSF/PF')}
          {n('otherRetirementAnnual', 'Other approved retirement fund (yearly)', 'Shares the retirement limit')}
          {n('medicalCost', 'Medical costs (yearly)', '15% tax credit, up to NPR 1,500')}
          {n('donation', 'Donations (yearly)', 'To organisations approved by the tax office; up to NPR 1,00,000 or 5% of taxable income')}
        </SimpleGrid>
        {prop && changed ? (
          <Text size="sm" mt="md">
            Deductions with these figures: insurance {money(prop.insuranceDeduction)}, retirement {money(prop.retirementDeduction)}{Number(prop.medicalCredit) > 0 ? `, medical credit ${money(prop.medicalCredit)}` : ''}. Tax for the year {money(prop.annualTax)} instead of {money(cur?.annualTax)}.
          </Text>
        ) : null}
        <Stack gap="xs" mt="md">
          <Text size="sm" fw={500}>Receipts</Text>
          <Text size="xs" c="dimmed">Premium receipts, CIT statements or hospital bills (PDF or photo, up to 4 MB each). Only you and HR can open them.</Text>
          {proofs.map((p) => <FileUpload key={p.id} purpose="tax_proof" value={p} onChange={(v) => { if (!v) setProofs(proofs.filter((x) => x.id !== p.id)); }} />)}
          {proofs.length < 10 ? <FileUpload purpose="tax_proof" value={null} onChange={(v) => v && setProofs([...proofs, v])} label={proofs.length ? 'Add another receipt' : 'Attach a receipt'} /> : null}
        </Stack>
        <Textarea label="Note for HR" mt="md" value={note} onChange={(e) => setNote(e.currentTarget.value)} autosize minRows={2} maxLength={1000} placeholder="Optional, for example the policy number" />
        <Group justify="space-between" mt="lg" wrap="wrap">
          <Text size="xs" c="dimmed">{pending ? 'Sending again replaces the request HR has not checked yet.' : 'Nothing changes on your payslip until HR verifies it.'}</Text>
          <Button leftSection={<IconSend size={16} stroke={1.6} />} loading={busy} disabled={!changed && !proofs.length} onClick={submit}>Send to HR</Button>
        </Group>
      </Paper>

      <div>
        <SectionTitle>History</SectionTitle>
        {q.data && !q.data.history.length ? <EmptyState compact title="No declarations for this year" hint="Fill in the figures above and attach the receipts." /> : (
          <DataTable<TaxSavingsRequest> rows={q.data?.history ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error}
            columns={[
              { key: 'when', header: 'Sent', render: (r) => <Text size="sm">{day(r.created_at)}</Text> },
              { key: 'what', header: 'Declared', render: (r) => <Text size="sm">{[['Life', r.life_premium], ['Health', r.health_premium], ['CIT/month', r.cit_monthly], ['Retirement', r.other_retirement_annual], ['Medical', r.medical_cost]].filter(([, v]) => Number(v) > 0).map(([k, v]) => `${k} ${fmtMoney(v, { decimals: 0, currency: false })}`).join(', ') || 'Nothing'}</Text> },
              { key: 'proofs', header: 'Receipts', render: (r) => r.proofs?.length ? <Group gap={6}>{r.proofs.map((p) => <Anchor key={p.id} size="sm" href={taxSavings.fileUrl(r.id, p.id)} target="_blank" rel="noopener">{p.filename}</Anchor>)}</Group> : <Text size="sm" c="dimmed">None</Text> },
              { key: 'status', header: 'Status', render: (r) => <Stack gap={0}><StatusBadge status={r.status} />{r.decision_note && r.status === 'rejected' ? <Text size="xs" c="dimmed">{r.decision_note}</Text> : null}</Stack> },
              { key: 'a', header: '', align: 'right', render: (r) => r.status === 'pending' ? <Button size="xs" variant="subtle" color="red" onClick={() => void cancel(r)}>Withdraw</Button> : null },
            ]} />
        )}
      </div>
    </Stack>
  );
}
