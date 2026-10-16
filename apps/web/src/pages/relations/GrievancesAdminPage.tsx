import { Button, Drawer, Group, MultiSelect, Paper, SegmentedControl, Stack, Text } from '@mantine/core';
import { useEffect, useState } from 'react';
import { auth as authApi } from '../../api';
import { relations } from '../../api/relations';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { GrievanceThread } from './GrievanceThread';
import { GrievanceRow } from './MyGrievancesPage';

/** HR: the grievance committee and every grievance it hears (HR's own grievances and cases they are excluded from never show here). */
export default function GrievancesAdminPage() {
  const [filter, setFilter] = useState<'open' | 'all'>('open');
  const q = useQuery(() => relations.grievances('handle'), []);
  const committee = useQuery(() => relations.committee(), []);
  const users = useQuery(() => authApi.users(), []);
  const [members, setMembers] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  useEffect(() => { setMembers((committee.data?.members ?? []).map((m) => m.user_id)); }, [committee.data]);
  const all = q.data?.grievances ?? [];
  const list = filter === 'open' ? all.filter((g) => !['closed', 'withdrawn'].includes(g.status)) : all;
  const changed = JSON.stringify([...members].sort()) !== JSON.stringify((committee.data?.members ?? []).map((m) => m.user_id).sort());
  return (
    <Stack gap="lg">
      <PageHeader title="Grievances" subtitle="Grievances are heard by a committee you name here, never by the employee's manager. Contents are encrypted and every view is recorded." />
      <div>
        <SectionTitle hint="Committee members see and decide grievances. HR hears appeals.">Grievance committee</SectionTitle>
        <Paper withBorder p="sm">
          <Group align="flex-end" wrap="wrap">
            <MultiSelect style={{ flex: 1, minWidth: 260 }} searchable placeholder="Add people" value={members} onChange={setMembers}
              data={(users.data?.users ?? []).filter((u) => u.status === 'active').map((u) => ({ value: u.id, label: `${u.name} (${u.email})` }))} />
            <Button loading={busy} disabled={!changed} onClick={async () => {
              setBusy(true);
              try { await relations.setCommittee(members); toast.ok('Committee saved.'); await committee.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); }
            }}>Save</Button>
          </Group>
          {!committee.data?.members.length ? <Text size="xs" c="orange" mt="xs">No committee yet: only HR will be told about new grievances.</Text> : null}
        </Paper>
      </div>
      <div>
        <SectionTitle actions={<SegmentedControl size="xs" value={filter} onChange={(v) => setFilter(v as 'open')} data={[{ value: 'open', label: 'Open' }, { value: 'all', label: 'All' }]} />}>Grievances</SectionTitle>
        {q.error ? <EmptyState title="Could not load grievances" hint={q.error} /> : null}
        {!q.loading && !q.error && !list.length ? <EmptyState title={filter === 'open' ? 'No open grievances' : 'No grievances yet'} hint="Employees raise grievances from Help desk, Grievances." /> : null}
        {list.length ? <Paper withBorder p={4}><Stack gap={0}>{list.map((g) => <GrievanceRow key={g.id} g={g} showName onOpen={() => setOpenId(g.id)} />)}</Stack></Paper> : null}
      </div>
      <Drawer opened={!!openId} onClose={() => setOpenId(null)} position="right" size="lg" title={<Text fw={600}>Grievance</Text>}>
        {openId ? <GrievanceThread id={openId} onChanged={() => { void q.reload(); }} /> : null}
      </Drawer>
    </Stack>
  );
}
