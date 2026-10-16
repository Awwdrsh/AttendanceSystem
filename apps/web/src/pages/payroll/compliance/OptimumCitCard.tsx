import { Alert, Button, Group, Paper, Text } from '@mantine/core';
import { IconBulb } from '@tabler/icons-react';
import { compliance } from '../../../api/compliance';
import { fmtMoney } from '../../../lib/format';
import { useQuery } from '../../../lib/useQuery';

const money = (v: string | undefined) => fmtMoney(v, { decimals: 0 });

/** Employee view: the monthly CIT that would bring this year's tax lowest, with a button to put it in the declaration form. */
export function OptimumCitCard({ onUse }: { onUse?: (monthly: number) => void }) {
  const q = useQuery(() => compliance.optimumMine(), []);
  const o = q.data;
  if (!o || !o.available) return null;
  const saving = Number(o.annualSaving ?? 0);
  if (saving <= 0) {
    return (
      <Alert variant="light" color="teal" icon={<IconBulb size={18} stroke={1.6} />} title="Your CIT is already working for you">
        {o.reason === 'already_optimal'
          ? `Your current contribution of ${money(o.currentMonthly)} a month already gets the lowest tax the rules allow this year.`
          : 'More CIT would not lower your tax this year: your retirement savings already reach the limit, or your remaining income is not taxed.'}
      </Alert>
    );
  }
  return (
    <Paper withBorder p="md" style={{ borderColor: 'var(--mantine-color-brand-4)' }}>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
        <div style={{ maxWidth: 620 }}>
          <Group gap={8} mb={4}><IconBulb size={18} stroke={1.6} color="var(--mantine-color-brand-6)" /><Text fw={600}>Save {money(o.annualSaving)} in tax this year</Text></Group>
          <Text size="sm" c="dimmed">
            Contributing <b className="num">{money(o.optimumMonthly)}</b> a month to the Citizen Investment Trust for the {o.remainingMonths} month{o.remainingMonths === 1 ? '' : 's'} left
            (now {money(o.currentMonthly)}) brings your tax for the year from {money(o.currentAnnualTax)} to {money(o.optimumAnnualTax)}.
            Retirement savings reduce taxable income up to {money(o.deductionLimit)} a year (one third of income or NPR 5,00,000, whichever is lower). The money stays yours in the CIT account.
          </Text>
        </div>
        {onUse ? <Button variant="light" onClick={() => onUse(Number(o.optimumMonthly))}>Use this amount</Button> : null}
      </Group>
    </Paper>
  );
}
