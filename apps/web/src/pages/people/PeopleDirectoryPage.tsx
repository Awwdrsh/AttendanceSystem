import { Badge, Group, Paper, Select, SimpleGrid, Skeleton, Stack, Text, TextInput, UnstyledButton } from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { IconSearch, IconUsers } from '@tabler/icons-react';
import { useState } from 'react';
import { people, type DirectoryPerson } from '../../api/people';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { PageHeader } from '../../components/PageHeader';
import { PersonAvatar } from '../../components/people/PersonAvatar';
import { PersonCardModal } from '../../components/people/PersonCardModal';
import { useQuery } from '../../lib/useQuery';

function PersonTile({ p, onOpen }: { p: DirectoryPerson; onOpen: () => void }) {
  return (
    <UnstyledButton onClick={onOpen} aria-label={`Open ${p.name_en}`}>
      <Paper withBorder p="md" h="100%">
        <Group gap="md" wrap="nowrap" align="flex-start">
          <PersonAvatar id={p.id} name={p.name_en} hasAvatar={p.has_avatar} size={44} />
          <div style={{ minWidth: 0 }}>
            <Text fw={600} size="sm" truncate>{p.name_en}</Text>
            <Text size="xs" c="dimmed" truncate>{p.designation ?? '—'}</Text>
            <Text size="xs" c="dimmed" truncate>{[p.unit_name, p.branch_name].filter(Boolean).join(' · ') || ' '}</Text>
            {p.temp_assignment ? <Badge size="xs" variant="light" color={p.temp_assignment.kind === 'acting' ? 'violet' : 'cyan'} mt={4}>{p.temp_assignment.kind === 'acting' ? 'Acting' : 'Deputed to'} {p.temp_assignment.label ?? ''}</Badge> : null}
            {p.work_email ? <Text size="xs" truncate mt={4}>{p.work_email}</Text> : null}
          </div>
        </Group>
      </Paper>
    </UnstyledButton>
  );
}

/** Everyone in the company, with work contact details only. */
export default function PeopleDirectoryPage() {
  const [search, setSearch] = useState('');
  const [debounced] = useDebouncedValue(search, 250);
  const [unit, setUnit] = useState<string | null>(null);
  const [branch, setBranch] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const structure = useQuery(() => people.structure(), []);
  const summary = useQuery(() => people.summary(), []);
  const list = useQuery(() => people.directory({ ...(debounced.trim() ? { search: debounced.trim() } : {}), ...(unit ? { orgUnitId: unit } : {}), ...(branch ? { branchId: branch } : {}) }), [debounced, unit, branch]);
  const s = summary.data;
  const filtered = !!(debounced.trim() || unit || branch);
  return (
    <Stack gap="lg">
      <PageHeader title="People" subtitle="Find a colleague, see who they report to and how to reach them at work." />
      {s ? (
        <StatStrip stats={[
          { key: 'a', label: 'People', value: s.active },
          { key: 'd', label: 'Departments and teams', value: structure.data?.units.length ?? 0 },
          { key: 'b', label: 'Branches', value: structure.data?.branches.length ?? 0 },
          { key: 'j', label: 'Joined in the last 30 days', value: s.joinedLast30Days },
        ]} />
      ) : <Skeleton h={88} />}
      <Group gap="sm" wrap="wrap">
        <TextInput placeholder="Search by name, role, team or skill" leftSection={<IconSearch size={16} stroke={1.6} />} value={search}
          onChange={(e) => setSearch(e.currentTarget.value)} style={{ flex: '1 1 260px' }} aria-label="Search people" />
        <Select placeholder="All departments" clearable searchable value={unit} onChange={setUnit} w={220} aria-label="Department"
          data={(structure.data?.units ?? []).map((u) => ({ value: u.id, label: u.name_en }))} />
        <Select placeholder="All branches" clearable value={branch} onChange={setBranch} w={180} aria-label="Branch"
          data={(structure.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en }))} />
      </Group>
      {list.loading && !list.data ? (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 3, xl: 4 }}>{Array.from({ length: 8 }, (_, i) => <Skeleton key={i} h={92} radius="md" />)}</SimpleGrid>
      ) : list.error ? <EmptyState title="We could not load the directory" hint={list.error} action={{ label: 'Try again', onClick: () => void list.reload() }} />
        : !list.data?.people.length ? (
          <EmptyState icon={<IconUsers size={20} stroke={1.6} />} title={filtered ? 'Nobody matches that search' : 'No one here yet'}
            hint={filtered ? 'Try a shorter name, or clear the filters.' : 'People appear here once HR adds employees.'}
            {...(filtered ? { action: { label: 'Clear filters', onClick: () => { setSearch(''); setUnit(null); setBranch(null); } } } : {})} />
        ) : (
          <>
            <Text size="xs" c="dimmed">{list.data.people.length} {list.data.people.length === 1 ? 'person' : 'people'}</Text>
            <SimpleGrid cols={{ base: 1, sm: 2, lg: 3, xl: 4 }} spacing="sm">
              {list.data.people.map((p) => <PersonTile key={p.id} p={p} onOpen={() => setOpen(p.id)} />)}
            </SimpleGrid>
          </>
        )}
      <PersonCardModal id={open} onClose={() => setOpen(null)} />
    </Stack>
  );
}
