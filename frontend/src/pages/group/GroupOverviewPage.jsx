import { Badge, Button, Group, Paper, SimpleGrid, Stack, Switch, Text, ThemeIcon } from '@mantine/core';
import {
  IconAlertTriangle,
  IconCashOff,
  IconDeviceDesktopOff,
  IconFileAlert,
  IconListCheck,
  IconRefresh,
  IconUserCheck,
} from '@tabler/icons-react';
import { useState } from 'react';
import { group as groupApi } from '../../api/group';
import { useAuth } from '../../auth/AuthProvider';
import { useCompanies } from '../../auth/CompaniesProvider';
import { ChartCard } from '../../components/charts/ChartCard';
import { LineChart } from '../../components/charts/Charts';
import { useVizColors } from '../../components/charts/palette';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { SwitchLink } from '../../components/group/SwitchLink';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { attentionByCompany } from '../../lib/group';
import { bsMonthLabel, fmtDate, fmtMoney, minutesToHm } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = {
  devices_offline: IconDeviceDesktopOff,
  payroll_not_run: IconCashOff,
  documents_expiring: IconFileAlert,
  probation_due: IconUserCheck,
  approvals: IconListCheck,
};
const TONE_COLOR = { bad: 'red', warn: 'orange', info: 'blue', ok: 'teal' };
const whole = (v) => fmtMoney(v, { decimals: 0 });

function stats(d) {
  const t = d.totals;
  const out = [
    { key: 'companies', label: 'Companies', value: d.companies.length },
    { key: 'people', label: 'People', value: t.headcount, hint: `+${t.joiners} joined, ${t.leavers} left this month` },
    { key: 'in', label: 'In today', value: t.present, hint: `${t.late} late, ${t.absent} absent, ${t.onLeave} on leave` },
    {
      key: 'approvals',
      label: 'Waiting for approval',
      value: t.pendingApprovals,
      tone: t.pendingApprovals ? 'warn' : 'ok',
    },
    { key: 'exceptions', label: 'Open exceptions', value: t.exceptions, tone: t.exceptions ? 'warn' : 'ok' },
  ];
  if (t.payrollCost !== null && t.payrollCost !== undefined)
    out.push({
      key: 'cost',
      label: 'Latest payroll cost',
      value: whole(t.payrollCost),
      hint: `Latest locked run of each company${t.payHidden ? ' you may see' : ''}`,
    });
  else out.push({ key: 'ot', label: 'Overtime this month', value: minutesToHm(t.otMinutes), hint: `${t.otAwaiting} awaiting review` });
  return out;
}

function AttritionChart({ d }) {
  const c = useVizColors();
  const months = d.attrition ?? [];
  const labels = months.map((m) => m.short);
  const series = [{ key: 'group', label: 'Group', values: months.map((m) => m.attritionPct), color: c.series[0] }];
  if (d.companies.length > 1)
    d.companies.forEach((co, i) =>
      series.push({
        key: co.code,
        label: co.name,
        color: c.series[(i + 1) % c.series.length],
        values: months.map((m) => m.byCompany?.[co.id] ?? null),
      }),
    );
  const empty = !months.some((m) => m.start || m.end);
  return (
    <ChartCard
      title="Attrition, last 12 months"
      subtitle="Leavers as a share of average headcount, per BS month"
      legend={series.map((s) => ({ label: s.label, color: s.color }))}
      table={{ columns: ['Month', 'Leavers', 'Attrition %'], rows: months.map((m) => [m.label, m.exits, m.attritionPct]) }}
      empty={empty ? <EmptyState compact title="No headcount history yet" /> : undefined}
    >
      <LineChart labels={labels} series={series} suffix="%" ariaLabel="Attrition by month" />
    </ChartCard>
  );
}

function NeedsAttention({ items }) {
  const groups = attentionByCompany(items);
  if (!groups.length)
    return (
      <EmptyState
        compact
        title="Nothing needs attention"
        hint="No offline devices, missed payroll, expiring documents or waiting approvals."
      />
    );
  return (
    <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
      {groups.map((g) => (
        <Paper withBorder p="md" key={g.companyId}>
          <Group justify="space-between" mb="xs">
            <Text fw={600} size="sm">
              {g.companyName}
            </Text>
            {g.active ? (
              <Badge size="xs" variant="light">
                You are here
              </Badge>
            ) : (
              <Text size="xs" c="dimmed">
                {g.companyCode}
              </Text>
            )}
          </Group>
          <Stack gap={8}>
            {g.items.map((i) => {
              const Ico = ICON[i.kind] ?? IconAlertTriangle;
              return (
                <Group key={i.kind} justify="space-between" wrap="nowrap" gap="sm">
                  <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
                    <ThemeIcon size={26} radius="md" variant="light" color={TONE_COLOR[i.tone] ?? 'gray'}>
                      <Ico size={15} stroke={1.7} />
                    </ThemeIcon>
                    <Text size="sm">{i.label}</Text>
                  </Group>
                  <SwitchLink companyId={i.companyId} to={i.next} button>
                    Open
                  </SwitchLink>
                </Group>
              );
            })}
          </Stack>
        </Paper>
      ))}
    </SimpleGrid>
  );
}

/** The active company's own choice (its admins): hide its pay figures from executives in group views. */
function PayPolicy({ onChange }) {
  const { active } = useCompanies();
  const q = useQuery(() => groupApi.payPolicy(), []);
  const [busy, setBusy] = useState(false);
  if (!q.data) return null;
  return (
    <Paper withBorder p="md">
      <Switch
        label={`Hide the pay figures of ${active?.name ?? 'this company'} from executives`}
        description="Group executives then see its people and attendance but not its payroll cost. HR and payroll admins still see it."
        checked={q.data.hidePayFromExecutives}
        disabled={busy}
        onChange={async (e) => {
          const hide = e.currentTarget.checked;
          setBusy(true);
          try {
            q.setData(await groupApi.setPayPolicy(hide));
            toast.ok(hide ? 'Executives no longer see pay figures of this company.' : 'Executives see pay figures of this company.');
            onChange();
          } catch (ex) {
            toast.err(ex);
          } finally {
            setBusy(false);
          }
        }}
      />
    </Paper>
  );
}

export default function GroupOverviewPage() {
  const { hasRole } = useAuth();
  const [refresh, setRefresh] = useState(0);
  const q = useQuery(() => groupApi.dashboard(refresh > 0), [refresh]);
  const d = q.data;
  return (
    <Stack gap="lg">
      <PageHeader
        title="Group overview"
        subtitle={
          d
            ? `Every company of the group you can see, ${fmtDate(d.date)}. Figures refresh every minute.`
            : 'Every company of the group you can see, today.'
        }
        actions={
          <Button
            variant="default"
            size="sm"
            leftSection={<IconRefresh size={16} stroke={1.6} />}
            loading={q.loading && !!d}
            onClick={() => setRefresh((n) => n + 1)}
          >
            Refresh
          </Button>
        }
      />
      {q.error ? <EmptyState title="Could not load the group overview" hint={q.error} /> : null}
      {d ? (
        <>
          <StatStrip stats={stats(d)} />
          <div>
            <SectionTitle hint="Each opens in that company: the switcher moves you there first.">Needs attention</SectionTitle>
            <NeedsAttention items={d.needsAttention} />
          </div>
          <div>
            <SectionTitle hint={`Today, and ${d.month.label} so far`}>By company</SectionTitle>
            <DataTable
              rows={d.companies}
              rowKey={(c) => c.id}
              columns={[
                {
                  key: 'name',
                  header: 'Company',
                  sticky: true,
                  render: (c) => (
                    <div style={{ whiteSpace: 'nowrap' }}>
                      <Group gap={6} wrap="nowrap">
                        <Text size="sm" fw={500}>
                          {c.name}
                        </Text>
                        {c.active ? (
                          <Badge size="xs" variant="light" style={{ flex: 'none' }}>
                            Active
                          </Badge>
                        ) : null}
                      </Group>
                      <Text size="xs" c="dimmed">
                        {c.code}
                      </Text>
                    </div>
                  ),
                },
                { key: 'headcount', header: 'People', align: 'right' },
                { key: 'moves', header: 'Joined / left', align: 'right', render: (c) => `+${c.joiners} / ${c.leavers}` },
                { key: 'present', header: 'In today', align: 'right' },
                { key: 'late', header: 'Late', align: 'right' },
                { key: 'absent', header: 'Absent', align: 'right' },
                { key: 'onLeave', header: 'On leave', align: 'right' },
                { key: 'pendingApprovals', header: 'Approvals', align: 'right' },
                { key: 'exceptions', header: 'Exceptions', align: 'right' },
                { key: 'ot', header: 'Overtime', align: 'right', render: (c) => minutesToHm(c.otMinutes) },
                {
                  key: 'payroll',
                  header: 'Latest payroll',
                  render: (c) =>
                    !c.paySeen ? (
                      <Text size="xs" c="dimmed">
                        Hidden by the company
                      </Text>
                    ) : c.payroll ? (
                      <Group gap={6} wrap="nowrap" style={{ whiteSpace: 'nowrap' }}>
                        <div style={{ flex: 'none' }}>
                          <StatusBadge status={c.payroll.status} />
                        </div>
                        <Text size="xs" c="dimmed" style={{ whiteSpace: 'nowrap' }}>
                          {bsMonthLabel(c.payroll.bsYear, c.payroll.bsMonth)} · {whole(c.payroll.cost)}
                        </Text>
                      </Group>
                    ) : (
                      <Text size="xs" c="dimmed">
                        No locked run yet
                      </Text>
                    ),
                },
              ]}
            />
          </div>
          <AttritionChart d={d} />
          {hasRole('admin') ? <PayPolicy onChange={() => setRefresh((n) => n + 1)} /> : null}
        </>
      ) : null}
    </Stack>
  );
}
