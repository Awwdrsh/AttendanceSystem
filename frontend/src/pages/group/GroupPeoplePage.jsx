import { Button, Group, Pagination, Paper, Select, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { IconDownload, IconSearch } from '@tabler/icons-react';
import { useState } from 'react';
import { group as groupApi } from '../../api/group';
import { useCompanies } from '../../auth/CompaniesProvider';
import { HBars } from '../../components/charts/Charts';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { SwitchLink } from '../../components/group/SwitchLink';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const PAGE_SIZE = 50;
const STATUS = [
  { value: 'active', label: 'Active' },
  { value: 'exited', label: 'Exited' },
  { value: 'all', label: 'Everyone' },
];
const GENDER = [
  { value: 'male', label: 'Male' },
  { value: 'female', label: 'Female' },
  { value: 'other', label: 'Other' },
];
const EMPLOYMENT = [
  { value: 'regular', label: 'Regular' },
  { value: 'work_based', label: 'Work based' },
  { value: 'time_based', label: 'Time based' },
  { value: 'casual', label: 'Casual' },
  { value: 'part_time', label: 'Part time' },
];
const SORT = [
  { value: 'name', label: 'Name' },
  { value: 'company', label: 'Company' },
  { value: 'code', label: 'Code' },
  { value: '-joined', label: 'Newest joiners' },
  { value: 'joined', label: 'Longest serving' },
];

export default function GroupPeoplePage() {
  const { me } = useCompanies();
  const [f, setF] = useState({ companyId: null, status: 'active', gender: null, employmentType: null, sort: 'name' });
  const [branch, setBranch] = useState('');
  const [department, setDepartment] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [qText] = useDebouncedValue(search, 300);
  const [branchQ] = useDebouncedValue(branch, 400);
  const [deptQ] = useDebouncedValue(department, 400);

  const params = {
    companyId: f.companyId,
    status: f.status,
    gender: f.gender,
    employmentType: f.employmentType,
    branch: branchQ.trim(),
    department: deptQ.trim(),
    q: qText.trim(),
    sort: f.sort,
  };
  const key = JSON.stringify(params);
  const q = useQuery(() => groupApi.people({ ...params, page, pageSize: PAGE_SIZE }), [key, page]);
  const set = (k) => (v) => {
    setF((x) => ({ ...x, [k]: v }));
    setPage(1);
  };
  const isHrIn = (companyId) => {
    const c = me?.companies?.find((x) => x.id === companyId);
    return !!c && (c.roles.includes('admin') || c.roles.includes('hr'));
  };
  const d = q.data;
  const pages = d ? Math.max(1, Math.ceil(d.total / d.pageSize)) : 1;
  const csv = (fn) => fn(params).catch((e) => toast.err(e, 'Download failed'));

  return (
    <Stack gap="lg">
      <PageHeader
        title="Group people"
        subtitle="Everyone in the companies of the group you can see, with headcount by company, branch, department, gender and employment type."
        actions={
          <>
            <Button variant="default" size="sm" leftSection={<IconDownload size={16} stroke={1.6} />} onClick={() => csv(groupApi.peopleCsv)}>
              People CSV
            </Button>
            <Button variant="default" size="sm" leftSection={<IconDownload size={16} stroke={1.6} />} onClick={() => csv(groupApi.breakdownsCsv)}>
              Breakdowns CSV
            </Button>
          </>
        }
      />
      <Paper withBorder p="md">
        <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} spacing="sm">
          <TextInput
            label="Search"
            placeholder="Name, code or email"
            leftSection={<IconSearch size={15} stroke={1.6} />}
            value={search}
            onChange={(e) => {
              setSearch(e.currentTarget.value);
              setPage(1);
            }}
          />
          <Select
            label="Company"
            placeholder="All companies"
            clearable
            data={(d?.companies ?? []).map((c) => ({ value: c.id, label: c.name }))}
            value={f.companyId}
            onChange={set('companyId')}
          />
          <Select label="Status" data={STATUS} value={f.status} onChange={(v) => set('status')(v ?? 'active')} allowDeselect={false} />
          <Select label="Sort by" data={SORT} value={f.sort} onChange={(v) => set('sort')(v ?? 'name')} allowDeselect={false} />
          <Select label="Gender" placeholder="Any gender" clearable data={GENDER} value={f.gender} onChange={set('gender')} />
          <Select
            label="Employment type"
            placeholder="Any employment type"
            clearable
            data={EMPLOYMENT}
            value={f.employmentType}
            onChange={set('employmentType')}
          />
          <TextInput
            label="Branch"
            placeholder="Exact branch name"
            value={branch}
            onChange={(e) => {
              setBranch(e.currentTarget.value);
              setPage(1);
            }}
          />
          <TextInput
            label="Department"
            placeholder="Exact department name"
            value={department}
            onChange={(e) => {
              setDepartment(e.currentTarget.value);
              setPage(1);
            }}
          />
        </SimpleGrid>
      </Paper>

      {d ? (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md">
          {d.breakdowns.map((b) => (
            <Paper withBorder p="lg" key={b.key}>
              <Group justify="space-between" mb="sm">
                <Text fw={600} size="sm">
                  By {b.title.toLowerCase()}
                </Text>
                <Text size="xs" c="dimmed">
                  {d.total.toLocaleString('en-US')} people
                </Text>
              </Group>
              {b.totals.length ? <HBars data={b.totals} max={8} /> : <Text size="sm" c="dimmed">No one matches.</Text>}
            </Paper>
          ))}
        </SimpleGrid>
      ) : null}

      <div>
        <SectionTitle hint={d ? `${d.total.toLocaleString('en-US')} people match` : undefined}>Directory</SectionTitle>
        {q.error ? (
          <EmptyState title="Could not load the directory" hint={q.error} />
        ) : (
          <DataTable
            rows={d?.items ?? []}
            rowKey={(p) => p.id}
            loading={q.loading}
            emptyTitle="No people match these filters."
            columns={[
              { key: 'empCode', header: 'Code', width: 100, render: (p) => <span className="num">{p.empCode}</span> },
              {
                key: 'name',
                header: 'Name',
                render: (p) =>
                  isHrIn(p.companyId) ? (
                    <SwitchLink companyId={p.companyId} to={`/employees/${p.id}`}>
                      {p.name}
                    </SwitchLink>
                  ) : (
                    <Text size="sm">{p.name}</Text>
                  ),
              },
              { key: 'companyName', header: 'Company' },
              { key: 'designation', header: 'Designation', render: (p) => p.designation ?? '—' },
              { key: 'department', header: 'Department', render: (p) => p.department ?? '—' },
              { key: 'branch', header: 'Branch', render: (p) => p.branch ?? '—' },
              { key: 'employmentType', header: 'Type', render: (p) => p.employmentTypeLabel || '—' },
              { key: 'joinDate', header: 'Joined', render: (p) => (p.joinDate ? fmtDate(p.joinDate) : '—') },
              { key: 'status', header: 'Status', render: (p) => <StatusBadge status={p.status} /> },
            ]}
          />
        )}
        {pages > 1 ? (
          <Group justify="center" mt="md">
            <Pagination total={pages} value={page} onChange={setPage} size="sm" />
          </Group>
        ) : null}
      </div>
    </Stack>
  );
}
