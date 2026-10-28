import { Alert, Grid, Group, Loader, SegmentedControl, Select, Stack, Text } from '@mantine/core';
import { IconChartHistogram, IconInfoCircle } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { insights as api } from '../../api/insights';
import { ChartCard } from '../../components/charts/ChartCard';
import { ColumnChart, Donut, HBars, LineChart } from '../../components/charts/Charts';
import { useVizColors } from '../../components/charts/palette';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { fmtDate, fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { adToBs } from '@/lib/bs-calendar';

function rangeOf(months) {
  const now = adToBs(todayNpt());
  let y = now.year;
  let m = now.month;
  const to = `${y}-${String(m).padStart(2, '0')}`;
  for (let i = 1; i < months; i++) {
    m -= 1;
    if (m === 0) {
      m = 12;
      y -= 1;
    }
  }
  return { from: `${y}-${String(m).padStart(2, '0')}`, to };
}

const unitText = (v, unit) => {
  if (v === null) return '—';
  if (unit === 'NPR') return fmtMoney(v, { decimals: 0 });
  if (unit === '%') return `${v}%`;
  if (unit === 'h') return `${v} h`;
  if (unit === 'days') return `${v} d`;
  return v.toLocaleString('en-US');
};

function kpiStats(d) {
  const prevLabel = d.months.length > 1 ? d.months[d.months.length - 2].label.split(' ')[0] : null;
  return d.kpis.map((k) => {
    const delta = k.delta;
    const tone =
      delta === null || delta === 0 || k.goodWhen === 'neutral' ? undefined : (k.goodWhen === 'down') === delta < 0 ? 'ok' : 'warn';
    const vs = k.vs ?? prevLabel;
    const hint =
      delta === null || !vs
        ? undefined
        : `${delta > 0 ? '+' : delta < 0 ? '−' : '±'}${k.unit === '%' ? `${Math.abs(delta)} pts` : unitText(Math.abs(delta), k.unit === 'NPR' ? 'NPR' : k.unit === 'h' ? 'h' : '')} vs ${vs}`;
    return { key: k.key, label: k.label, value: unitText(k.value, k.unit), ...(hint ? { hint } : {}), ...(tone ? { tone } : {}) };
  });
}

/** Workforce insights for HR: trends by Bikram Sambat month, filtered by branch and department, every chart downloadable. */
export default function InsightsPage() {
  const c = useVizColors();
  const [range, setRange] = useState('12');
  const [branchId, setBranchId] = useState(null);
  const [orgUnitId, setOrgUnitId] = useState(null);
  const filters = useQuery(() => api.filters(), []);
  const q = useMemo(() => ({ ...rangeOf(Number(range)), branchId, orgUnitId }), [range, branchId, orgUnitId]);
  const data = useQuery(() => api.get(q), [q]);
  const d = data.data;
  const dl = (ds) => () => {
    api.exportCsv(ds, q).catch((e) => toast.err(e, 'Download failed'));
  };
  const labels = d?.months.map((m) => m.short) ?? [];
  const money = (n) => fmtMoney(n, { decimals: 0 });
  const noCost = d ? d.cost.every((x) => !x.locked) : true;

  return (
    <>
      <PageHeader
        title="Insights"
        subtitle="How the workforce is changing, month by month. Numbers come from employee records, attendance, leave and locked pay runs."
        actions={
          <>
            <Select
              size="sm"
              w={170}
              placeholder="All branches"
              clearable
              value={branchId}
              onChange={setBranchId}
              aria-label="Branch"
              data={(filters.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name }))}
            />
            <Select
              size="sm"
              w={190}
              placeholder="All departments"
              clearable
              searchable
              value={orgUnitId}
              onChange={setOrgUnitId}
              aria-label="Department"
              data={(filters.data?.orgUnits ?? []).map((o) => ({ value: o.id, label: o.name }))}
            />
            <SegmentedControl
              size="sm"
              value={range}
              onChange={(v) => setRange(v)}
              data={[
                { value: '6', label: '6 months' },
                { value: '12', label: '12 months' },
                { value: '24', label: '24 months' },
              ]}
            />
          </>
        }
      />
      {data.error ? (
        <Alert color="red" mb="lg">
          {data.error}
        </Alert>
      ) : null}
      {!d ? (
        data.loading ? (
          <Group justify="center" py={80}>
            <Loader size="sm" />
          </Group>
        ) : (
          <EmptyState
            icon={<IconChartHistogram size={20} stroke={1.6} />}
            title="No insights yet"
            hint="Add employees and run attendance to see trends here."
          />
        )
      ) : (
        <Stack gap="lg" style={{ opacity: data.loading ? 0.6 : 1, transition: 'opacity .15s' }}>
          <Text size="xs" c="dimmed" mt={-12}>
            {d.months[0].label} to {d.months[d.months.length - 1].label} · people counts as of {fmtDate(d.asOf)} · changes compare the last
            month with the one before
          </Text>
          <StatStrip stats={kpiStats(d)} />

          <Grid gap="lg">
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="Headcount"
                subtitle="Active employees at the end of each month"
                onDownload={dl('headcount')}
                table={{
                  columns: ['Month', 'Headcount', 'Joined', 'Left'],
                  rows: d.headcount.map((h) => [h.label, h.headcount, h.joins, h.exits]),
                }}
              >
                <ColumnChart
                  ariaLabel="Headcount by month"
                  labels={labels}
                  series={[{ key: 'hc', label: 'Headcount', values: d.headcount.map((h) => h.headcount) }]}
                />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="Joiners and leavers"
                subtitle={`${d.headcount.reduce((s, h) => s + h.joins, 0)} joined, ${d.attrition.exits} left in this period`}
                onDownload={dl('headcount')}
                legend={[
                  { label: 'Joined', color: c.series[0] },
                  { label: 'Left', color: c.series[2] },
                ]}
                table={{ columns: ['Month', 'Joined', 'Left', 'Net'], rows: d.headcount.map((h) => [h.label, h.joins, h.exits, h.net]) }}
              >
                <ColumnChart
                  ariaLabel="Joiners and leavers by month"
                  labels={labels}
                  series={[
                    { key: 'j', label: 'Joined', values: d.headcount.map((h) => h.joins), color: c.series[0] },
                    { key: 'x', label: 'Left', values: d.headcount.map((h) => h.exits), color: c.series[2] },
                  ]}
                />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="Attrition"
                onDownload={dl('headcount')}
                subtitle={`${d.attrition.monthlyAvgPct}% a month on average, ${d.attrition.annualisedPct}% a year${d.attrition.split ? ` · ${d.attrition.split.voluntary} resigned, ${d.attrition.split.involuntary} let go` : ''}`}
                table={{
                  columns: ['Month', 'Attrition %', 'Left', ...(d.attrition.split ? ['Voluntary', 'Involuntary'] : [])],
                  rows: d.headcount.map((h) => [
                    h.label,
                    h.attritionPct,
                    h.exits,
                    ...(d.attrition.split ? [h.voluntary, h.involuntary] : []),
                  ]),
                }}
              >
                <LineChart
                  ariaLabel="Monthly attrition rate"
                  labels={labels}
                  suffix="%"
                  series={[{ key: 'a', label: 'Attrition', values: d.headcount.map((h) => h.attritionPct) }]}
                />
                {!d.exitReasonsAvailable ? (
                  <Text size="xs" c="dimmed" mt={6}>
                    Record exit reasons to see voluntary and involuntary leavers separately.
                  </Text>
                ) : null}
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="People cost"
                subtitle="Gross pay plus employer SSF, PF and gratuity, from locked pay runs"
                onDownload={dl('cost')}
                legend={[
                  { label: 'Gross pay', color: c.series[0] },
                  { label: 'Employer contributions', color: c.series[1] },
                ]}
                table={{
                  columns: ['Month', 'Gross', 'Employer', 'Total', 'People paid'],
                  rows: d.cost.map((x) => [x.label, x.gross, x.employerContributions, x.totalCost, x.people]),
                }}
                empty={
                  noCost ? (
                    <EmptyState
                      compact
                      title="No locked pay runs in this period"
                      hint="Costs appear once a pay run is approved and locked."
                    />
                  ) : undefined
                }
              >
                <ColumnChart
                  ariaLabel="People cost by month"
                  labels={labels}
                  stacked
                  money
                  format={money}
                  series={[
                    { key: 'g', label: 'Gross pay', values: d.cost.map((x) => x.gross), color: c.series[0] },
                    { key: 'e', label: 'Employer contributions', values: d.cost.map((x) => x.employerContributions), color: c.series[1] },
                  ]}
                />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="Absenteeism and late arrivals"
                subtitle="Share of working days absent, and of days worked that started late"
                onDownload={dl('attendance')}
                legend={[
                  { label: 'Absent', color: c.series[2] },
                  { label: 'Late', color: c.series[3] },
                ]}
                table={{
                  columns: ['Month', 'Absent %', 'Late %', 'Absent days', 'Late days'],
                  rows: d.attendance.map((a) => [a.label, a.absenteeismPct, a.latePct, a.absentDays, a.lateDays]),
                }}
              >
                <LineChart
                  ariaLabel="Absenteeism and late arrival rates"
                  labels={labels}
                  suffix="%"
                  series={[
                    { key: 'abs', label: 'Absent', values: d.attendance.map((a) => a.absenteeismPct), color: c.series[2] },
                    { key: 'late', label: 'Late', values: d.attendance.map((a) => a.latePct), color: c.series[3] },
                  ]}
                />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="Average working day"
                subtitle="Hours worked on days with time recorded"
                onDownload={dl('attendance')}
                table={{
                  columns: ['Month', 'Average hours', 'Days worked'],
                  rows: d.attendance.map((a) => [a.label, a.avgWorkedHours, a.presentDays]),
                }}
              >
                <LineChart
                  ariaLabel="Average hours worked per day"
                  labels={labels}
                  suffix=" h"
                  series={[{ key: 'h', label: 'Average hours', values: d.attendance.map((a) => a.avgWorkedHours) }]}
                />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="Overtime"
                subtitle="Hours detected by attendance, and hours approved for pay"
                onDownload={dl('overtime')}
                legend={[
                  { label: 'Detected', color: c.series[1] },
                  { label: 'Approved', color: c.series[0] },
                ]}
                table={{
                  columns: ['Month', 'Detected (h)', 'Approved (h)'],
                  rows: d.overtime.map((o) => [o.label, o.detectedHours, o.approvedHours]),
                }}
              >
                <ColumnChart
                  ariaLabel="Overtime hours by month"
                  labels={labels}
                  format={(n) => `${n} h`}
                  series={[
                    { key: 'd', label: 'Detected', values: d.overtime.map((o) => o.detectedHours), color: c.series[1] },
                    { key: 'a', label: 'Approved', values: d.overtime.map((o) => o.approvedHours), color: c.series[0] },
                  ]}
                />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, md: 6 }}>
              <ChartCard
                title="Leave taken by type"
                subtitle={`Days taken in this period · ${d.leaveMonthly.reduce((s, m) => s + m.days, 0)} days in total`}
                onDownload={dl('leave')}
                table={{ columns: ['Leave type', 'Days', 'Per employee'], rows: d.leave.map((l) => [l.type, l.days, l.perEmployee]) }}
                empty={d.leave.length ? undefined : <EmptyState compact title="No leave taken in this period" />}
              >
                <HBars data={d.leave.map((l) => ({ label: l.type, value: l.days }))} format={(n) => `${n} d`} />
              </ChartCard>
            </Grid.Col>
          </Grid>

          <Text fw={600} mt="sm">
            Who works here
          </Text>
          <Grid gap="lg">
            <Grid.Col span={{ base: 12, sm: 6, lg: 4 }}>
              <ChartCard
                title="Gender"
                onDownload={dl('gender')}
                table={{ columns: ['Gender', 'People'], rows: d.gender.map((g) => [g.label, g.count]) }}
              >
                <Donut ariaLabel="Employees by gender" centerLabel="people" data={d.gender} size={140} />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, sm: 6, lg: 4 }}>
              <ChartCard
                title="Tenure"
                subtitle="Years with the company"
                onDownload={dl('tenure')}
                table={{ columns: ['Tenure', 'People'], rows: d.tenure.map((t) => [t.label, t.count]) }}
              >
                <HBars data={d.tenure.map((t) => ({ label: t.label, value: t.count }))} />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, sm: 6, lg: 4 }}>
              <ChartCard
                title="Age"
                onDownload={dl('age')}
                table={{ columns: ['Age', 'People'], rows: d.age.map((a) => [a.label, a.count]) }}
              >
                <HBars data={d.age.map((a) => ({ label: a.label, value: a.count }))} />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, sm: 6, lg: 4 }}>
              <ChartCard
                title="Departments"
                onDownload={dl('departments')}
                table={{ columns: ['Department', 'People'], rows: d.departments.map((x) => [x.label, x.count]) }}
              >
                <HBars data={d.departments.map((x) => ({ label: x.label, value: x.count }))} />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, sm: 6, lg: 4 }}>
              <ChartCard
                title="Branches"
                onDownload={dl('branches')}
                table={{ columns: ['Branch', 'People'], rows: d.branches.map((x) => [x.label, x.count]) }}
              >
                <HBars data={d.branches.map((x) => ({ label: x.label, value: x.count }))} />
              </ChartCard>
            </Grid.Col>
            <Grid.Col span={{ base: 12, sm: 6, lg: 4 }}>
              <ChartCard
                title="People cost by department"
                subtitle="Whole period, locked pay runs"
                onDownload={dl('costByDepartment')}
                table={{ columns: ['Department', 'Total cost'], rows: d.costByDepartment.map((x) => [x.label, x.totalCost]) }}
                empty={d.costByDepartment.length ? undefined : <EmptyState compact title="No locked pay runs in this period" />}
              >
                <HBars data={d.costByDepartment.map((x) => ({ label: x.label, value: x.totalCost }))} format={money} />
              </ChartCard>
            </Grid.Col>
          </Grid>
          <Group gap={6} c="dimmed">
            <IconInfoCircle size={14} stroke={1.6} />
            <Text size="xs">Branch and department filters use where each person sits today. Months follow the Bikram Sambat calendar.</Text>
          </Group>
        </Stack>
      )}
    </>
  );
}
