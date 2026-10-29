import { Button, Paper, SegmentedControl, Select, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconDownload } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { group as groupApi } from '../../api/group';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { ReportTable } from '../../components/ReportTable';
import { reportQuery } from '../../lib/group';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function GroupReportsPage() {
  const cat = useQuery(() => groupApi.reportCatalog(), []);
  const [f, setF] = useState(null);
  useEffect(() => {
    if (cat.data && !f)
      setF({
        kind: cat.data.reports[0]?.key ?? 'headcount',
        companyId: null,
        period: 'month',
        bsYear: cat.data.current.bsYear,
        bsMonth: cat.data.current.bsMonth,
        fy: cat.data.current.fy,
      });
  }, [cat.data, f]);
  const key = f ? JSON.stringify(f) : '';
  const r = useQuery(() => (f ? groupApi.report(f.kind, reportQuery(f)) : Promise.resolve(null)), [key], { enabled: !!f });
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v }));

  if (cat.error) return <EmptyState title="Could not load group reports" hint={cat.error} />;
  if (!cat.data || !f) return null;
  const c = cat.data;
  const desc = c.reports.find((x) => x.key === f.kind)?.description;
  const years = [];
  for (let y = c.current.bsYear; y >= c.current.bsYear - 5; y -= 1) years.push({ value: String(y), label: String(y) });

  return (
    <Stack gap="lg">
      <PageHeader
        title="Group reports"
        subtitle="Consolidated reports across the companies of the group, with company as the first column and a group total."
        actions={
          <Button
            variant="default"
            size="sm"
            leftSection={<IconDownload size={16} stroke={1.6} />}
            onClick={() => groupApi.reportCsv(f.kind, reportQuery(f)).catch((e) => toast.err(e, 'Download failed'))}
          >
            Download CSV
          </Button>
        }
      />
      <Paper withBorder p="md">
        <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} spacing="sm">
          <Select
            label="Report"
            data={c.reports.map((x) => ({ value: x.key, label: x.title }))}
            value={f.kind}
            onChange={(v) => v && set('kind')(v)}
            allowDeselect={false}
          />
          <Select
            label="Company"
            placeholder="All companies"
            clearable
            data={c.companies.map((x) => ({ value: x.id, label: x.name }))}
            value={f.companyId}
            onChange={set('companyId')}
          />
          <div>
            <Text size="sm" fw={500} mb={4}>
              Period
            </Text>
            <SegmentedControl
              fullWidth
              value={f.period}
              onChange={set('period')}
              data={[
                { value: 'month', label: 'BS month' },
                { value: 'fy', label: 'Fiscal year' },
              ]}
            />
          </div>
          {f.period === 'fy' ? (
            <Select
              label="Fiscal year"
              data={c.fiscalYears.map((y) => ({ value: String(y.value), label: y.label }))}
              value={String(f.fy)}
              onChange={(v) => v && set('fy')(Number(v))}
              allowDeselect={false}
            />
          ) : (
            <SimpleGrid cols={2} spacing="xs">
              <Select
                label="Month"
                data={c.months.map((m) => ({ value: String(m.value), label: m.label }))}
                value={String(f.bsMonth)}
                onChange={(v) => v && set('bsMonth')(Number(v))}
                allowDeselect={false}
              />
              <Select label="Year" data={years} value={String(f.bsYear)} onChange={(v) => v && set('bsYear')(Number(v))} allowDeselect={false} />
            </SimpleGrid>
          )}
        </SimpleGrid>
        {desc ? (
          <Text size="xs" c="dimmed" mt="sm">
            {desc}
          </Text>
        ) : null}
      </Paper>
      <div>
        <SectionTitle hint={r.data?.period}>{r.data?.title ?? 'Report'}</SectionTitle>
        {r.error ? (
          <EmptyState title="Could not run the report" hint={r.error} />
        ) : (
          <ReportTable columns={r.data?.columns ?? []} rows={r.data?.rows ?? []} loading={r.loading} emptyTitle="Nothing to report for this period." />
        )}
      </div>
    </Stack>
  );
}
