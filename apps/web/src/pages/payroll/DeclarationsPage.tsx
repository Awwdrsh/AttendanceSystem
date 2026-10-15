import { Anchor, Button, Group, NumberInput, Paper, Select, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconCheck, IconSend, IconUserSearch, IconX } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { payroll as payApi } from '../../api';
import { taxSavings } from '../../api/security';
import { useAuth } from '../../auth/AuthProvider';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Self-service tax declaration with HR verification. */
export default function DeclarationsPage() {
  const { user, hasRole } = useAuth();
  const bs = safeAdToBs(todayNpt());
  const currentFy = bs ? (bs.month >= 4 ? bs.year : bs.year - 1) : 2083;
  const [fy, setFy] = useState<number | string>(currentFy);
  const [employeeId, setEmployeeId] = useState<string | null>(user?.employeeId ?? null);
  const q = useQuery(() => payApi.declaration(Number(fy), employeeId ?? undefined), [fy, employeeId], { enabled: !!employeeId });
  const [f, setF] = useState({ lifePremium: 0, healthPremium: 0, citMonthly: 0, medicalCost: 0, otherRetirementAnnual: 0 });
  // A request waiting for HR is shown instead of the verified figures, so HR checks exactly what was asked for.
  const pending = q.data?.pending ?? null;
  useEffect(() => { const d = q.data?.pending ?? q.data?.declaration; if (d) setF({ lifePremium: Number(d.life_premium), healthPremium: Number(d.health_premium), citMonthly: Number(d.cit_monthly), medicalCost: Number(d.medical_cost), otherRetirementAnnual: Number(d.other_retirement_annual) }); else setF({ lifePremium: 0, healthPremium: 0, citMonthly: 0, medicalCost: 0, otherRetirementAnnual: 0 }); }, [q.data]);
  const [busy, setBusy] = useState(false);
  const isVerifier = hasRole('hr');
  const status = pending ? 'submitted' : q.data?.declaration?.status;
  const canVerify = isVerifier && !!employeeId && status === 'submitted' && employeeId !== user?.employeeId;
  const forSomeoneElse = isVerifier && employeeId !== user?.employeeId;
  const fyOptions = [currentFy - 2, currentFy - 1, currentFy, currentFy + 1].map((y) => ({ value: String(y), label: `FY ${y}/${String(y + 1).slice(-2)}` }));
  const n = (k: keyof typeof f, label: string, hint: string) => <NumberInput label={label} description={hint} value={f[k]} onChange={(v) => setF({ ...f, [k]: Number(v) || 0 })} min={0} thousandSeparator="," leftSection={<Text size="xs" c="dimmed">NPR</Text>} leftSectionWidth={42} />;
  const submit = async () => {
    setBusy(true);
    try { await payApi.saveDeclaration({ ...(employeeId !== user?.employeeId ? { employeeId } : {}), fiscalYearStart: Number(fy), lifePremium: f.lifePremium.toFixed(2), healthPremium: f.healthPremium.toFixed(2), citMonthly: f.citMonthly.toFixed(2), medicalCost: f.medicalCost.toFixed(2), otherRetirementAnnual: f.otherRetirementAnnual.toFixed(2), submit: true }); toast.ok(forSomeoneElse ? 'Declaration saved and verified. Payroll uses it from the next run.' : 'Declaration sent to HR for verification.'); await q.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const verify = async (result: 'verified' | 'rejected') => {
    if (!employeeId) return;
    try { await payApi.verifyDeclaration(employeeId, Number(fy), result); if (result === 'verified') toast.ok('Declaration verified.'); await q.reload(); } catch (e) { toast.err(e); }
  };
  return (
    <Stack gap="lg">
      <PageHeader title="Tax declarations" subtitle="Insurance premiums and retirement contributions you declare reduce taxable income, within the legal limits. Keep the receipts; HR verifies them."
        actions={<>
          {isVerifier ? <EmployeePicker value={employeeId} onChange={setEmployeeId} label="" /> : null}
          <Select aria-label="Fiscal year" value={String(fy)} onChange={(v) => v && setFy(Number(v))} allowDeselect={false} data={fyOptions} w={140} />
        </>} />
      {!employeeId ? (
        <EmptyState icon={<IconUserSearch size={20} stroke={1.6} />} title="Pick an employee" hint="Choose someone above to view, edit or verify their declaration." />
      ) : (
        <Paper withBorder p="lg">
          <SectionTitle hint="Annual amounts unless marked monthly." actions={status ? <StatusBadge status={status} /> : <Text size="sm" c="dimmed">Not submitted</Text>}>Deductions</SectionTitle>
          <SimpleGrid cols={{ base: 1, sm: 2 }} mt="sm">
            {n('lifePremium', 'Life insurance premium', 'Deductible up to NPR 40,000 a year')}
            {n('healthPremium', 'Health insurance premium', 'Deductible up to NPR 20,000 a year')}
            {n('citMonthly', 'CIT contribution (monthly)', 'Counts toward the retirement limit: the lower of one third of income or NPR 500,000')}
            {n('otherRetirementAnnual', 'Other approved retirement fund', 'Shares the CIT retirement limit')}
            {n('medicalCost', 'Approved medical costs', '15% tax credit, up to NPR 1,500')}
          </SimpleGrid>
          {pending ? (
            <Stack gap={4} mt="md">
              <Text size="sm" fw={600}>Waiting for verification{pending.note ? `: “${pending.note}”` : ''}</Text>
              {q.data?.declaration ? <Text size="xs" c="dimmed">Payroll keeps using the verified figures until this request is verified.</Text> : <Text size="xs" c="dimmed">Nothing is deducted until this request is verified.</Text>}
              {(pending.proof_file_ids ?? []).length ? (
                <Group gap="xs">{(pending.proof_file_ids ?? []).map((fid, i) => <Anchor key={fid} size="sm" href={taxSavings.fileUrl(pending.id, fid)} target="_blank" rel="noopener">Proof {i + 1}</Anchor>)}</Group>
              ) : <Text size="xs" c="orange">No proof files attached.</Text>}
            </Stack>
          ) : null}
          <Group justify="flex-end" mt="lg" gap="xs">
            {canVerify ? <>
              <Button variant="subtle" color="red" leftSection={<IconX size={16} stroke={1.6} />} onClick={() => verify('rejected')}>Reject</Button>
              <Button leftSection={<IconCheck size={16} stroke={1.6} />} onClick={() => verify('verified')}>Verify declaration</Button>
            </> : null}
            <Button variant={canVerify ? 'default' : 'filled'} leftSection={<IconSend size={16} stroke={1.6} />} loading={busy} onClick={submit}>{forSomeoneElse ? 'Save as verified' : 'Submit declaration'}</Button>
          </Group>
        </Paper>
      )}
    </Stack>
  );
}
