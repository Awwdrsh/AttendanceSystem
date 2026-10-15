import { Box, Button, Group, Paper, Stack, Text, ThemeIcon } from '@mantine/core';
import { IconAlertTriangle, IconBeach, IconCalendarEvent, IconCalendarStats, IconClockHour4, IconClockPlus, IconDeviceDesktop, IconDownload, IconFingerprint, IconMapPin, IconCoffee, IconUsers } from '@tabler/icons-react';
import { useState, type ReactNode } from 'react';
import { reports as repApi } from '../../api';
import { download } from '../../api/client';
import { BsMonthPicker, DateField } from '../../components/BsDatePicker';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { addDays, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';

interface ReportRow { key: string; icon: ReactNode; title: string; desc: string; inputs?: ReactNode; onExport: () => void }

const ico = (I: typeof IconDownload) => <I size={18} stroke={1.6} />;

export default function ReportsPage() {
  const bs = safeAdToBs(todayNpt());
  const [ym, setYm] = useState({ year: bs?.year ?? 2083, month: bs?.month ?? 1 });
  const [date, setDate] = useState<string | null>(todayNpt());
  const [from, setFrom] = useState<string | null>(addDays(todayNpt(), -30)); const [to, setTo] = useState<string | null>(todayNpt());
  const [emp, setEmp] = useState<string | null>(null);
  const dl = (name: string, q: Record<string, string | number | null | undefined>, file: string) => {
    if (Object.values(q).some((v) => v === null || v === undefined || v === '')) { toast.info('Pick the dates first, then export.'); return; }
    return repApi.csv(name, q, file).catch((e) => toast.err(e, 'Export failed'));
  };
  const range = (
    <Group gap="xs" wrap="wrap">
      <DateField value={from} onChange={setFrom} w={170} placeholder="From" {...(to ? { maxDate: to } : {})} />
      <Text size="sm" c="dimmed">to</Text>
      <DateField value={to} onChange={setTo} w={170} placeholder="To" {...(from ? { minDate: from } : {})} />
    </Group>
  );
  const rows: ReportRow[] = [
    { key: 'register', icon: ico(IconCalendarStats), title: 'Monthly attendance register', desc: 'One row per employee per day with arrival, departure, hours, overtime, leave and breaks. Ready for a labour inspection.', inputs: <BsMonthPicker value={ym} onChange={setYm} />, onExport: () => dl('register', { bsYear: ym.year, bsMonth: ym.month }, `register-${ym.year}-${ym.month}.csv`) },
    { key: 'daily', icon: ico(IconCalendarEvent), title: 'Daily attendance', desc: 'Status, clock-in and clock-out, late minutes and overtime for every active employee on one day.', inputs: <DateField value={date} onChange={setDate} w={170} />, onExport: () => dl('daily', { date }, `daily-${date}.csv`) },
    { key: 'ot', icon: ico(IconClockPlus), title: 'Overtime register', desc: 'Detected and approved overtime hours by day type and rate.', inputs: range, onExport: () => dl('ot', { from, to }, `ot-${from}_${to}.csv`) },
    { key: 'late', icon: ico(IconClockHour4), title: 'Late arrivals and early exits', desc: 'Count and minutes per employee across the date range.', inputs: range, onExport: () => dl('late-early', { from, to }, `late-early-${from}_${to}.csv`) },
    { key: 'exceptions', icon: ico(IconAlertTriangle), title: 'Exceptions summary', desc: 'Missing punches, absences, late arrivals and overtime flags per day.', inputs: range, onExport: () => dl('exceptions-summary', { from, to }, `exceptions-${from}_${to}.csv`) },
    { key: 'breaks', icon: ico(IconCoffee), title: 'Breaks', desc: 'Every break with its type, start, end, minutes and whether it ran over the limit (up to 92 days).', inputs: range,
      onExport: () => (from && to ? download(`/field/breaks?from=${from}&to=${to}&format=csv`, `breaks-${from}_${to}.csv`).catch((e) => toast.err(e, 'Export failed')) : toast.info('Pick the dates first, then export.')) },
    { key: 'visits', icon: ico(IconMapPin), title: 'Field visits', desc: 'Client and site visits with place, purpose, arrival, departure, outcome and location (up to 92 days).', inputs: range,
      onExport: () => (from && to ? download(`/field/visits?from=${from}&to=${to}&format=csv`, `field-visits-${from}_${to}.csv`).catch((e) => toast.err(e, 'Export failed')) : toast.info('Pick the dates first, then export.')) },
    { key: 'leave', icon: ico(IconBeach), title: 'Leave balances and liability', desc: 'Balance per leave type, with encashment liability at current basic pay.', onExport: () => dl('leave-balances', {}, 'leave-balances.csv') },
    { key: 'headcount', icon: ico(IconUsers), title: 'Headcount', desc: 'By branch, unit, employment type and gender.', onExport: () => dl('headcount', {}, 'headcount.csv') },
    { key: 'devices', icon: ico(IconDeviceDesktop), title: 'Device health', desc: 'Last seen, firmware, punches today and offline status for each terminal.', onExport: () => dl('devices', {}, 'devices.csv') },
    { key: 'punches', icon: ico(IconFingerprint), title: 'Punch audit for one employee', desc: 'Every raw punch with its device, source and any corrections that replaced it.',
      inputs: <Stack gap="xs"><EmployeePicker value={emp} onChange={setEmp} label="" aria-label="Employee" w={{ base: '100%', sm: 360 }} />{range}</Stack>,
      onExport: () => { if (!emp) { toast.info('Pick an employee first, then export.'); return; } void dl('punch-audit', { employeeId: emp, from, to }, `punches-${emp}.csv`); } },
  ];
  return (
    <Stack gap="md">
      <PageHeader title="Reports" subtitle="Each report downloads as a CSV file that opens directly in Excel." />
      <Paper withBorder>
        {rows.map((r, i) => (
            <Box key={r.key} px="lg" py="md" style={i > 0 ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
              <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
                <Group align="flex-start" gap="md" wrap="nowrap" style={{ flex: '1 1 420px', minWidth: 0 }}>
                  <ThemeIcon variant="light" color="gray" size={36} radius="md">{r.icon}</ThemeIcon>
                  <Stack gap={8} style={{ minWidth: 0, flex: 1 }}>
                    <div>
                      <Text fw={500} size="sm">{r.title}</Text>
                      <Text size="sm" c="dimmed">{r.desc}</Text>
                    </div>
                    {r.inputs ?? null}
                  </Stack>
                </Group>
                <Button variant="default" leftSection={<IconDownload size={16} stroke={1.6} />} onClick={r.onExport}>Export CSV</Button>
              </Group>
            </Box>
        ))}
      </Paper>
    </Stack>
  );
}
