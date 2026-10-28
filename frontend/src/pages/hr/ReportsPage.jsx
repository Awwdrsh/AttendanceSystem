import { Badge, Box, Button, Chip, Group, Modal, NumberInput, Paper, Select, Stack, Text, TextInput, ThemeIcon } from '@mantine/core';
import {
  IconAddressBook,
  IconAlertTriangle,
  IconBeach,
  IconBook,
  IconCake,
  IconCalendarEvent,
  IconCalendarStats,
  IconCertificate,
  IconChartBar,
  IconClockHour4,
  IconClockPlus,
  IconCoffee,
  IconDeviceDesktop,
  IconDownload,
  IconEye,
  IconFingerprint,
  IconHeartbeat,
  IconHome,
  IconHourglass,
  IconListCheck,
  IconMapPin,
  IconReceiptRefund,
  IconSchool,
  IconSearch,
  IconUserPlus,
  IconUsers,
  IconUsersGroup,
  IconCash,
  IconArrowRight,
} from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { reports as repApi } from '../../api';
import { catalog } from '../../api/catalog';
import { download, get } from '../../api/client';
import { BsMonthPicker, DateField } from '../../components/BsDatePicker';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { ReportTable } from '../../components/ReportTable';
import { useFeatures } from '../../lib/features';
import { addDays, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';

const CATEGORIES = ['Attendance', 'Leave', 'Payroll', 'People', 'Time', 'Expenses', 'Hiring', 'Learning'];

/** What a viewable report asks for before it runs. */
const ico = (I) => <I size={18} stroke={1.6} />;
const people = (name) => `/reports/people/${name}`;

export default function ReportsPage() {
  const { on } = useFeatures();
  const bs = safeAdToBs(todayNpt());
  const [ym, setYm] = useState({ year: bs?.year ?? 2083, month: bs?.month ?? 1 });
  const [date, setDate] = useState(todayNpt());
  const [from, setFrom] = useState(addDays(todayNpt(), -30));
  const [to, setTo] = useState(todayNpt());
  const [emp, setEmp] = useState(null);
  const [q, setQ] = useState('');
  const [cat, setCat] = useState('all');
  const [open, setOpen] = useState(null);

  const dl = (name, params, file) => {
    if (Object.values(params).some((v) => v === null || v === undefined || v === '')) {
      toast.info('Pick the dates first, then export.');
      return;
    }
    return repApi.csv(name, params, file).catch((e) => toast.err(e, 'Export failed'));
  };
  const range = (
    <Group gap="xs" wrap="wrap">
      <DateField value={from} onChange={setFrom} w={170} placeholder="From" {...(to ? { maxDate: to } : {})} />
      <Text size="sm" c="dimmed">
        to
      </Text>
      <DateField value={to} onChange={setTo} w={170} placeholder="To" {...(from ? { minDate: from } : {})} />
    </Group>
  );
  const fieldCsv = (path, file) => () =>
    from && to
      ? download(`${path}?from=${from}&to=${to}&format=csv`, `${file}-${from}_${to}.csv`).catch((e) => toast.err(e, 'Export failed'))
      : toast.info('Pick the dates first, then export.');

  const entries = [
    // Attendance
    {
      key: 'register',
      category: 'Attendance',
      icon: ico(IconCalendarStats),
      title: 'Monthly attendance register',
      desc: 'One row per employee per day with arrival, departure, hours, overtime, leave and breaks. Ready for a labour inspection.',
      csv: {
        inputs: <BsMonthPicker value={ym} onChange={setYm} />,
        onExport: () => void dl('register', { bsYear: ym.year, bsMonth: ym.month }, `register-${ym.year}-${ym.month}.csv`),
      },
    },
    {
      key: 'daily',
      category: 'Attendance',
      icon: ico(IconCalendarEvent),
      title: 'Daily attendance',
      desc: 'Status, clock-in and clock-out, late minutes and overtime for every active employee on one day.',
      csv: {
        inputs: <DateField value={date} onChange={setDate} w={170} />,
        onExport: () => void dl('daily', { date }, `daily-${date}.csv`),
      },
    },
    {
      key: 'ot',
      category: 'Attendance',
      icon: ico(IconClockPlus),
      title: 'Overtime register',
      desc: 'Detected and approved overtime hours by day type and rate.',
      csv: { inputs: range, onExport: () => void dl('ot', { from, to }, `ot-${from}_${to}.csv`) },
    },
    {
      key: 'late',
      category: 'Attendance',
      icon: ico(IconClockHour4),
      title: 'Late arrivals and early exits',
      desc: 'Count and minutes per employee across the date range.',
      csv: { inputs: range, onExport: () => void dl('late-early', { from, to }, `late-early-${from}_${to}.csv`) },
    },
    {
      key: 'exceptions',
      category: 'Attendance',
      icon: ico(IconAlertTriangle),
      title: 'Exceptions summary',
      desc: 'Missing punches, absences, late arrivals and overtime flags per day.',
      csv: { inputs: range, onExport: () => void dl('exceptions-summary', { from, to }, `exceptions-${from}_${to}.csv`) },
    },
    {
      key: 'breaks',
      category: 'Attendance',
      icon: ico(IconCoffee),
      title: 'Breaks',
      feature: 'field',
      desc: 'Every break with its type, start, end, minutes and whether it ran over the limit (up to 92 days).',
      csv: { inputs: range, onExport: fieldCsv('/field/breaks', 'breaks') },
    },
    {
      key: 'visits',
      category: 'Attendance',
      icon: ico(IconMapPin),
      title: 'Field visits',
      feature: 'field',
      desc: 'Client and site visits with place, purpose, arrival, departure, outcome and location (up to 92 days).',
      csv: { inputs: range, onExport: fieldCsv('/field/visits', 'field-visits') },
    },
    {
      key: 'devices',
      category: 'Attendance',
      icon: ico(IconDeviceDesktop),
      title: 'Device health',
      desc: 'Last seen, firmware, punches today and offline status for each terminal.',
      csv: { onExport: () => void dl('devices', {}, 'devices.csv') },
    },
    {
      key: 'punches',
      category: 'Attendance',
      icon: ico(IconFingerprint),
      title: 'Punch audit for one employee',
      desc: 'Every raw punch with its device, source and any corrections that replaced it.',
      csv: {
        inputs: (
          <Stack gap="xs">
            <EmployeePicker value={emp} onChange={setEmp} label="" aria-label="Employee" w={{ base: '100%', sm: 360 }} />
            {range}
          </Stack>
        ),
        onExport: () => {
          if (!emp) {
            toast.info('Pick an employee first, then export.');
            return;
          }
          void dl('punch-audit', { employeeId: emp, from, to }, `punches-${emp}.csv`);
        },
      },
    },
    // Leave
    {
      key: 'leave',
      category: 'Leave',
      icon: ico(IconBeach),
      title: 'Leave balances and liability',
      desc: 'Balance per leave type, with encashment liability at current basic pay.',
      csv: { onExport: () => void dl('leave-balances', {}, 'leave-balances.csv') },
    },
    // Payroll
    {
      key: 'salary-sheet',
      category: 'Payroll',
      icon: ico(IconCash),
      title: 'Salary sheet and SSF sheet',
      desc: 'Every pay run has its salary sheet (on screen, Excel and print) and its SSF contribution sheet.',
      link: { to: '/payroll', label: 'Open pay runs' },
    },
    // People
    {
      key: 'headcount-on-date',
      category: 'People',
      icon: ico(IconUsers),
      title: 'Headcount on a date',
      desc: 'People employed on any date, by branch, department, employment type and gender.',
      table: {
        path: people('headcount-on-date'),
        params: [{ kind: 'date', key: 'date', label: 'On' }, { kind: 'placement' }],
        file: 'headcount',
      },
    },
    {
      key: 'statistics',
      category: 'People',
      icon: ico(IconChartBar),
      title: 'Workforce statistics',
      desc: 'Age, length of service, gender, marital status and where people come from.',
      table: { path: people('statistics'), params: [{ kind: 'placement', status: true }], file: 'people-statistics' },
    },
    {
      key: 'joiners-leavers',
      category: 'People',
      icon: ico(IconUserPlus),
      title: 'Joiners and leavers',
      desc: 'Everyone who joined or left in a period, with their department and the reason for leaving.',
      table: { path: people('joiners-leavers'), params: [{ kind: 'range' }, { kind: 'placement' }], file: 'joiners-leavers' },
    },
    {
      key: 'birthdays',
      category: 'People',
      icon: ico(IconCake),
      title: 'Birthdays and work anniversaries',
      desc: 'Who has a birthday or a work anniversary in the coming days.',
      table: {
        path: people('birthdays-anniversaries'),
        params: [
          { kind: 'date', key: 'from', label: 'From' },
          { kind: 'days', key: 'days', label: 'Days ahead', max: 92, def: 30 },
          { kind: 'placement' },
        ],
        file: 'birthdays-anniversaries',
      },
    },
    {
      key: 'nominees',
      category: 'People',
      icon: ico(IconUsersGroup),
      title: 'Nominees',
      desc: 'Nominees for SSF, gratuity and insurance, with their share.',
      table: { path: people('nominees'), params: [{ kind: 'placement', status: true }], file: 'nominees' },
    },
    {
      key: 'emergency',
      category: 'People',
      icon: ico(IconHeartbeat),
      title: 'Emergency contacts',
      desc: 'Who to call for each employee, with blood group.',
      table: { path: people('emergency-contacts'), params: [{ kind: 'placement', status: true }], file: 'emergency-contacts' },
    },
    {
      key: 'addresses',
      category: 'People',
      icon: ico(IconHome),
      title: 'Addresses',
      desc: 'Permanent and temporary addresses with province and district.',
      table: { path: people('addresses'), params: [{ kind: 'placement', status: true }], file: 'addresses' },
    },
    {
      key: 'education',
      category: 'People',
      icon: ico(IconSchool),
      title: 'Education',
      desc: 'Every qualification on file: level, degree, institution and year.',
      table: { path: people('education'), params: [{ kind: 'placement', status: true }], file: 'education' },
    },
    {
      key: 'licences',
      category: 'People',
      icon: ico(IconCertificate),
      title: 'Licence and document register',
      desc: 'Passports, driving licences, citizenship and documents with an expiry date. Numbers are never shown.',
      table: {
        path: people('licence-register'),
        params: [
          { kind: 'days', key: 'withinDays', label: 'Expiring within (days)', max: 3650, def: 90 },
          { kind: 'placement', status: true },
        ],
        file: 'licence-register',
      },
    },
    {
      key: 'completeness',
      category: 'People',
      icon: ico(IconListCheck),
      title: 'Record completeness',
      desc: 'How complete each employee record is and what is missing, least complete first.',
      table: { path: people('record-completeness'), params: [{ kind: 'placement', status: true }], file: 'record-completeness' },
    },
    {
      key: 'directory',
      category: 'People',
      icon: ico(IconAddressBook),
      title: 'Your own report',
      desc: 'Pick the columns, filters and sort you need from employees, education, family, previous jobs, career events or leave balances.',
      link: { to: '/reports/builder', label: 'Build a report' },
    },
    // Time
    {
      key: 'time',
      category: 'Time',
      icon: ico(IconHourglass),
      title: 'Hours by project, client, person or activity',
      feature: 'time',
      desc: 'Tracked hours, billable hours and amount, and budget used.',
      table: {
        path: '/time/reports/summary',
        params: [
          { kind: 'range' },
          {
            kind: 'select',
            key: 'groupBy',
            label: 'Group by',
            def: 'project',
            options: [
              { value: 'project', label: 'Project' },
              { value: 'client', label: 'Client' },
              { value: 'employee', label: 'Person' },
              { value: 'activity', label: 'Activity' },
              { value: 'day', label: 'Day' },
            ],
          },
        ],
        file: 'time',
      },
    },
    // Expenses
    {
      key: 'spend',
      category: 'Expenses',
      icon: ico(IconReceiptRefund),
      title: 'Expense spend',
      feature: 'expenses',
      desc: 'Approved claim amounts by category, department, month or person.',
      table: {
        path: '/expenses/reports/spend',
        params: [
          { kind: 'range' },
          {
            kind: 'select',
            key: 'groupBy',
            label: 'Group by',
            def: 'category',
            options: [
              { value: 'category', label: 'Category' },
              { value: 'department', label: 'Department' },
              { value: 'month', label: 'Month' },
              { value: 'employee', label: 'Person' },
            ],
          },
        ],
        file: 'expense-spend',
      },
    },
    {
      key: 'advances',
      category: 'Expenses',
      icon: ico(IconReceiptRefund),
      title: 'Outstanding travel advances',
      feature: 'expenses',
      desc: 'Advances paid for trips that are not settled yet.',
      table: { path: '/expenses/reports/advances', params: [], file: 'travel-advances' },
    },
    {
      key: 'pending-pay',
      category: 'Expenses',
      icon: ico(IconReceiptRefund),
      title: 'Claims waiting for payment',
      feature: 'expenses',
      desc: 'Approved claims and settlements not yet paid through payroll.',
      table: { path: '/expenses/reports/pending-payment', params: [], file: 'expenses-pending-payment' },
    },
    // Hiring and learning
    {
      key: 'hiring',
      category: 'Hiring',
      icon: ico(IconUserPlus),
      title: 'Hiring funnel and time to hire',
      feature: 'hiring',
      desc: 'Applications reaching each stage per job, time to hire and where candidates come from.',
      link: { to: '/hiring', label: 'Open hiring' },
    },
    {
      key: 'training-cost',
      category: 'Learning',
      icon: ico(IconBook),
      title: 'Training cost and certificates',
      feature: 'learning',
      desc: 'Training spend by course and department, completions and certificates about to expire.',
      link: { to: '/performance/learning', label: 'Open learning' },
    },
  ];

  const visible = entries.filter((e) => on(e.feature));
  const needle = q.trim().toLowerCase();
  const shown = visible.filter(
    (e) => (cat === 'all' || e.category === cat) && (!needle || `${e.title} ${e.desc} ${e.category}`.toLowerCase().includes(needle)),
  );
  const counts = useMemo(
    () => Object.fromEntries(CATEGORIES.map((c) => [c, visible.filter((e) => e.category === c).length])),
    [visible.length],
  ); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Stack gap="md">
      <PageHeader
        title="Reports"
        subtitle="Every report opens on screen or downloads as a CSV file for Excel. Dates are in Bikram Sambat."
      />
      <Group gap="sm" wrap="wrap" align="center">
        <TextInput
          leftSection={<IconSearch size={16} stroke={1.6} />}
          placeholder="Search reports"
          value={q}
          onChange={(e) => setQ(e.currentTarget.value)}
          w={{ base: '100%', sm: 280 }}
          aria-label="Search reports"
        />
        <Chip.Group value={cat} onChange={(v) => setCat(v)}>
          <Group gap={6} wrap="wrap">
            <Chip value="all" size="sm" variant="light">
              All
            </Chip>
            {CATEGORIES.filter((c) => counts[c]).map((c) => (
              <Chip key={c} value={c} size="sm" variant="light">
                {c}
              </Chip>
            ))}
          </Group>
        </Chip.Group>
      </Group>
      {shown.length === 0 ? (
        <Text c="dimmed" size="sm" py="lg" ta="center">
          No report matches “{q}”.
        </Text>
      ) : null}
      {CATEGORIES.filter((c) => shown.some((e) => e.category === c)).map((c) => (
        <Paper withBorder key={c}>
          <Box px="lg" py="sm" style={{ borderBottom: '1px solid var(--hj-line-soft)' }}>
            <Text fw={600} size="sm">
              {c}
            </Text>
          </Box>
          {shown
            .filter((e) => e.category === c)
            .map((r, i) => (
              <Box key={r.key} px="lg" py="md" style={i > 0 ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
                <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
                  <Group align="flex-start" gap="md" wrap="nowrap" style={{ flex: '1 1 420px', minWidth: 0 }}>
                    <ThemeIcon variant="light" color="gray" size={36} radius="md">
                      {r.icon}
                    </ThemeIcon>
                    <Stack gap={8} style={{ minWidth: 0, flex: 1 }}>
                      <div>
                        <Text fw={500} size="sm">
                          {r.title}
                        </Text>
                        <Text size="sm" c="dimmed">
                          {r.desc}
                        </Text>
                      </div>
                      {r.csv?.inputs ?? null}
                    </Stack>
                  </Group>
                  {r.csv ? (
                    <Button variant="default" leftSection={<IconDownload size={16} stroke={1.6} />} onClick={r.csv.onExport}>
                      Export CSV
                    </Button>
                  ) : null}
                  {r.table ? (
                    <Button variant="default" leftSection={<IconEye size={16} stroke={1.6} />} onClick={() => setOpen(r)}>
                      View
                    </Button>
                  ) : null}
                  {r.link ? (
                    <Button variant="subtle" component={Link} to={r.link.to} rightSection={<IconArrowRight size={16} stroke={1.6} />}>
                      {r.link.label}
                    </Button>
                  ) : null}
                </Group>
              </Box>
            ))}
        </Paper>
      ))}
      {open?.table ? <TableReportModal title={open.title} spec={open.table} onClose={() => setOpen(null)} /> : null}
    </Stack>
  );
}

/** A report on screen: its own inputs, branch/department/status filters, the table and a CSV download of the same view. */
function TableReportModal({ title, spec, onClose }) {
  const today = todayNpt();
  const init = {};
  for (const p of spec.params) {
    if (p.kind === 'date') init[p.key] = today;
    if (p.kind === 'range') {
      init.from = addDays(today, -30);
      init.to = today;
    }
    if (p.kind === 'days') init[p.key] = String(p.def);
    if (p.kind === 'select') init[p.key] = p.def;
    if (p.kind === 'placement' && p.status) init.status = 'active';
  }
  const [vals, setVals] = useState(init);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [lookups, setLookups] = useState({ branches: [], units: [] });
  const set = (k, v) =>
    setVals((s) => {
      const n = { ...s };
      if (v) n[k] = v;
      else delete n[k];
      return n;
    });
  const placement = spec.params.find((p) => p.kind === 'placement');

  useEffect(() => {
    if (!placement) return;
    get('/people/structure')
      .then((r) =>
        setLookups({
          branches: r.branches.map((b) => ({ value: b.id, label: b.name_en })),
          units: r.units.map((u) => ({ value: u.id, label: u.name_en })),
        }),
      )
      .catch(() => undefined);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const key = JSON.stringify(vals);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    catalog
      .table(spec.path, vals)
      .then((r) => {
        if (alive) setData(r);
      })
      .catch((e) => {
        if (alive) {
          setData(null);
          toast.err(e, 'Could not run the report');
        }
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Modal opened onClose={onClose} title={<Text fw={600}>{title}</Text>} size="min(1200px, 96vw)">
      <Stack gap="md">
        <Group gap="sm" wrap="wrap" align="flex-end">
          {spec.params.map((p) => {
            switch (p.kind) {
              case 'date':
                return <DateField key={p.key} label={p.label} value={vals[p.key] ?? null} onChange={(v) => set(p.key, v)} w={170} />;
              case 'range':
                return (
                  <Group key="range" gap="xs" align="flex-end" wrap="wrap">
                    <DateField
                      label="From"
                      value={vals.from ?? null}
                      onChange={(v) => set('from', v)}
                      w={170}
                      {...(vals.to ? { maxDate: vals.to } : {})}
                    />
                    <DateField
                      label="To"
                      value={vals.to ?? null}
                      onChange={(v) => set('to', v)}
                      w={170}
                      {...(vals.from ? { minDate: vals.from } : {})}
                    />
                  </Group>
                );
              case 'days':
                return (
                  <NumberInput
                    key={p.key}
                    label={p.label}
                    value={Number(vals[p.key] ?? p.def)}
                    min={1}
                    max={p.max}
                    w={170}
                    onChange={(v) => set(p.key, v === '' ? String(p.def) : String(Math.min(p.max, Math.max(1, Number(v)))))}
                  />
                );
              case 'select':
                return (
                  <Select
                    key={p.key}
                    label={p.label}
                    data={p.options}
                    value={vals[p.key] ?? p.def}
                    onChange={(v) => set(p.key, v ?? p.def)}
                    w={170}
                    allowDeselect={false}
                  />
                );
              case 'placement':
                return (
                  <Group key="placement" gap="sm" align="flex-end" wrap="wrap">
                    <Select
                      label="Branch"
                      placeholder="All branches"
                      data={lookups.branches}
                      value={vals.branchId ?? null}
                      onChange={(v) => set('branchId', v)}
                      clearable
                      searchable
                      w={190}
                    />
                    <Select
                      label="Department"
                      placeholder="All departments"
                      data={lookups.units}
                      value={vals.orgUnitId ?? null}
                      onChange={(v) => set('orgUnitId', v)}
                      clearable
                      searchable
                      w={210}
                    />
                    {p.status ? (
                      <Select
                        label="Employees"
                        data={[
                          { value: 'active', label: 'Current' },
                          { value: 'exited', label: 'Left' },
                          { value: 'all', label: 'Everyone' },
                        ]}
                        value={vals.status ?? 'active'}
                        onChange={(v) => set('status', v ?? 'active')}
                        allowDeselect={false}
                        w={140}
                      />
                    ) : null}
                  </Group>
                );
            }
            return null;
          })}
          <Box style={{ flex: 1 }} />
          <Button
            variant="default"
            leftSection={<IconDownload size={16} stroke={1.6} />}
            onClick={() => catalog.tableCsv(spec.path, vals, `${spec.file}.csv`).catch((e) => toast.err(e, 'Export failed'))}
          >
            Export CSV
          </Button>
        </Group>
        {data ? (
          <Group gap="xs">
            <Badge variant="light" color="gray">
              {data.rows.length} {data.rows.length === 1 ? 'row' : 'rows'}
            </Badge>
          </Group>
        ) : null}
        <ReportTable columns={data?.columns ?? []} rows={data?.rows ?? []} loading={loading && !data} />
      </Stack>
    </Modal>
  );
}
