import { Alert, Button, Group, NumberInput, Paper, SimpleGrid, Stack, Text } from '@mantine/core';
import { useEffect, useState } from 'react';
import { payroll as payApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Self-service tax declaration (plan §5.9) with HR/finance verification. */
export default function DeclarationsPage() {
  const { t } = useI18n();
  const { user, hasRole } = useAuth();
  const bs = safeAdToBs(todayNpt());
  const [fy, setFy] = useState<number | string>(bs ? (bs.month >= 4 ? bs.year : bs.year - 1) : 2083);
  const [employeeId, setEmployeeId] = useState<string | null>(user?.employeeId ?? null);
  const q = useQuery(() => payApi.declaration(Number(fy), employeeId ?? undefined), [fy, employeeId], { enabled: !!employeeId });
  const [f, setF] = useState({ lifePremium: 0, healthPremium: 0, citMonthly: 0, medicalCost: 0, otherRetirementAnnual: 0 });
  useEffect(() => { const d = q.data?.declaration; if (d) setF({ lifePremium: Number(d.life_premium), healthPremium: Number(d.health_premium), citMonthly: Number(d.cit_monthly), medicalCost: Number(d.medical_cost), otherRetirementAnnual: Number(d.other_retirement_annual) }); else setF({ lifePremium: 0, healthPremium: 0, citMonthly: 0, medicalCost: 0, otherRetirementAnnual: 0 }); }, [q.data]);
  const [busy, setBusy] = useState(false);
  const n = (k: keyof typeof f, label: string, hint: string) => <NumberInput label={label} description={hint} value={f[k]} onChange={(v) => setF({ ...f, [k]: Number(v) || 0 })} min={0} thousandSeparator="," />;
  return (
    <Stack>
      <PageHeader title={t('declarations')} subtitle="Declared premiums and contributions reduce taxable income within the statutory caps. Keep the receipts; HR verifies them." actions={<Group gap="xs">{hasRole('hr', 'finance') ? <EmployeePicker value={employeeId} onChange={setEmployeeId} label="" /> : null}<NumberInput value={fy} onChange={setFy} w={110} /></Group>} />
      {q.data?.declaration ? <Group><Text size="sm">Status:</Text><StatusBadge status={q.data.declaration.status} /></Group> : null}
      <Paper withBorder p="md" radius="md">
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          {n('lifePremium', 'Life insurance premium (annual)', 'Deductible up to NPR 40,000')}
          {n('healthPremium', 'Health insurance premium (annual)', 'Deductible up to NPR 20,000')}
          {n('citMonthly', 'CIT contribution (monthly)', 'Counts toward the retirement cap: min(1/3 of income, NPR 500,000)')}
          {n('otherRetirementAnnual', 'Other approved retirement fund (annual)', 'Same cap as CIT')}
          {n('medicalCost', 'Approved medical cost (annual)', '15% credit, max NPR 1,500')}
        </SimpleGrid>
        <Group justify="flex-end" mt="md">
          <Button loading={busy} disabled={!employeeId} onClick={async () => { setBusy(true); try { await payApi.saveDeclaration({ ...(employeeId !== user?.employeeId ? { employeeId } : {}), fiscalYearStart: Number(fy), lifePremium: f.lifePremium.toFixed(2), healthPremium: f.healthPremium.toFixed(2), citMonthly: f.citMonthly.toFixed(2), medicalCost: f.medicalCost.toFixed(2), otherRetirementAnnual: f.otherRetirementAnnual.toFixed(2), submit: true }); toast.ok('Declaration submitted.'); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('submit')}</Button>
          {hasRole('hr', 'finance') && employeeId && q.data?.declaration?.status === 'submitted' ? <><Button variant="light" color="teal" onClick={async () => { try { await payApi.verifyDeclaration(employeeId, Number(fy), 'verified'); toast.ok('Verified.'); await q.reload(); } catch (e) { toast.err(e); } }}>Verify</Button><Button variant="light" color="red" onClick={async () => { try { await payApi.verifyDeclaration(employeeId, Number(fy), 'rejected'); await q.reload(); } catch (e) { toast.err(e); } }}>{t('reject')}</Button></> : null}
        </Group>
      </Paper>
      {!employeeId ? <Alert variant="light" color="gray">Pick an employee to view or verify a declaration.</Alert> : null}
    </Stack>
  );
}
