import { Button, Group, Paper, SimpleGrid, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { reports as repApi } from '../../api';
import { BsDatePicker, BsMonthPicker } from '../../components/BsDatePicker';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { addDays, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';

/** Each report with a BS picker and a CSV download (plan §5.10). */
export default function ReportsPage() {
  const { t } = useI18n();
  const bs = safeAdToBs(todayNpt());
  const [ym, setYm] = useState({ year: bs?.year ?? 2083, month: bs?.month ?? 1 });
  const [date, setDate] = useState<string | null>(todayNpt());
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -30)); const [to, setTo] = useState<string | null>(todayNpt());
  const [emp, setEmp] = useState<string | null>(null);
  const dl = (name: string, q: Record<string, string | number | null | undefined>, file: string) => {
    if (Object.values(q).some((v) => v === null || v === undefined || v === '')) { toast.info('Pick the date(s) first.'); return; }
    return repApi.csv(name, q, file).catch((e) => toast.err(e, 'Download failed'));
  };
  const card = (title: string, desc: string, controls: React.ReactNode, onDownload: () => void) => (
    <Paper withBorder p="md" radius="md"><Stack gap="xs"><Text fw={600}>{title}</Text><Text size="xs" c="dimmed">{desc}</Text>{controls}<Group justify="flex-end"><Button size="xs" onClick={onDownload}>{t('download')} {t('csv')}</Button></Group></Stack></Paper>
  );
  return (
    <Stack>
      <PageHeader title={t('reports')} subtitle="CSV files open in Excel with Nepali text intact (UTF-8 with BOM)." />
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="sm">
        {card('Monthly attendance register', 'Inspector-ready: one row per employee per BS calendar day with arrival, departure, hours, OT, leave and breaks (Labour Act records).', <BsMonthPicker value={ym} onChange={setYm} />, () => dl('register', { bsYear: ym.year, bsMonth: ym.month }, `register-${ym.year}-${ym.month}.csv`))}
        {card('Daily attendance', 'Status, in/out, late and OT for every active employee on one day.', <BsDatePicker value={date} onChange={setDate} w={260} />, () => dl('daily', { date }, `daily-${date}.csv`))}
        {card('Overtime register', 'Detected vs approved hours by day type and rate.', <Group><BsDatePicker value={from} onChange={setFrom} w={220} /><BsDatePicker value={to} onChange={setTo} w={220} /></Group>, () => dl('ot', { from, to }, `ot-${from}_${to}.csv`))}
        {card('Late / early-out', 'Counts and minutes per employee for a range.', <Group><BsDatePicker value={from} onChange={setFrom} w={220} /><BsDatePicker value={to} onChange={setTo} w={220} /></Group>, () => dl('late-early', { from, to }, `late-early-${from}_${to}.csv`))}
        {card('Exceptions summary', 'Incomplete, absent, late and OT flags per day.', <Group><BsDatePicker value={from} onChange={setFrom} w={220} /><BsDatePicker value={to} onChange={setTo} w={220} /></Group>, () => dl('exceptions-summary', { from, to }, `exceptions-${from}_${to}.csv`))}
        {card('Leave balances & liability', 'Balances per type with encashment liability at current basic.', null, () => dl('leave-balances', {}, 'leave-balances.csv'))}
        {card('Headcount', 'By branch, unit, employment type and gender.', null, () => dl('headcount', {}, 'headcount.csv'))}
        {card('Device health', 'Last seen, firmware, punches today, offline flag.', null, () => dl('devices', {}, 'devices.csv'))}
        {card('Punch audit (one employee)', 'Every raw punch with device, source and superseding corrections.', <Group><EmployeePicker value={emp} onChange={setEmp} label="" /><BsDatePicker value={from} onChange={setFrom} w={220} /><BsDatePicker value={to} onChange={setTo} w={220} /></Group>, () => emp ? dl('punch-audit', { employeeId: emp, from, to }, `punches-${emp}.csv`) : toast.info('Pick an employee first.'))}
      </SimpleGrid>
    </Stack>
  );
}
