import { Alert, Badge, Button, Group, Paper, SegmentedControl, Stack, Switch, Text } from '@mantine/core';
import { useState } from 'react';
import { relations, type DeviceRow } from '../../api/relations';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS: Record<DeviceRow['status'], { label: string; color: string }> = {
  pending: { label: 'Waiting', color: 'orange' }, approved: { label: 'Approved', color: 'teal' }, rejected: { label: 'Not approved', color: 'red' }, revoked: { label: 'Removed', color: 'gray' },
};

/** HR: approve the phones and computers employees may clock in from, and switch the rule on or off. */
export default function RegisteredDevicesPage() {
  const policy = useQuery(() => relations.policy(), []);
  const [status, setStatus] = useState<'pending' | 'all'>('pending');
  const q = useQuery(() => relations.devices(status), [status]);
  const [busy, setBusy] = useState(false);
  const on = !!policy.data?.policy.registeredDevicesOnly;
  const act = async (d: DeviceRow, action: 'approve' | 'reject' | 'revoke') => {
    try { await relations.decideDevice(d.id, action); toast.ok(action === 'approve' ? 'Approved.' : action === 'reject' ? 'Not approved.' : 'Removed.'); await q.reload(); } catch (e) { toast.err(e); }
  };
  return (
    <Stack gap="lg">
      <PageHeader title="Clock-in devices" subtitle="Stop clock-ins from someone else's phone: when this is on, the website and app accept clock-ins only from devices you approved for that person. Device terminals and kiosks are not affected." />
      <Paper withBorder p="md">
        <Group justify="space-between" wrap="nowrap">
          <div><Text fw={600}>Clock in only from registered devices</Text><Text size="sm" c="dimmed">Employees register their phone under My attendance, Clock-in devices. Up to three each.</Text></div>
          <Switch checked={on} disabled={!policy.data || busy} onChange={async (e) => {
            const next = e.currentTarget.checked;
            setBusy(true);
            try { await relations.savePolicy({ ...policy.data!.policy, registeredDevicesOnly: next }); toast.ok(next ? 'Clock-in now needs a registered device.' : 'Any device can clock in again.'); await policy.reload(); }
            catch (err) { toast.err(err); } finally { setBusy(false); }
          }} />
        </Group>
        {on ? <Alert mt="sm" variant="light" color="orange">People without an approved device cannot clock in from the website until you approve one.</Alert> : null}
      </Paper>
      <Group justify="flex-end"><SegmentedControl size="xs" value={status} onChange={(v) => setStatus(v as 'all')} data={[{ value: 'pending', label: 'Waiting' }, { value: 'all', label: 'All' }]} /></Group>
      <DataTable<DeviceRow> rows={q.data?.devices ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error}
        emptyTitle={status === 'pending' ? 'Nothing to approve' : 'No devices registered'}
        columns={[
          { key: 'e', header: 'Employee', render: (r) => <><Text size="sm">{r.employee_name}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></> },
          { key: 'd', header: 'Device', render: (r) => <><Text size="sm">{r.label}</Text><Text size="xs" c="dimmed" lineClamp={1} maw={260}>{r.user_agent ?? ''}</Text></> },
          { key: 'w', header: 'Registered', render: (r) => relativeTime(r.requested_at) },
          { key: 'u', header: 'Last clock-in', render: (r) => r.last_used_at ? relativeTime(r.last_used_at) : '' },
          { key: 's', header: 'Status', render: (r) => <Badge variant="light" color={STATUS[r.status].color}>{STATUS[r.status].label}</Badge> },
          { key: 'a', header: '', align: 'right', render: (r) => r.status === 'pending' ? (
            <Group gap={4} justify="flex-end" wrap="nowrap"><Button size="compact-xs" onClick={() => act(r, 'approve')}>Approve</Button><Button size="compact-xs" variant="default" onClick={() => act(r, 'reject')}>Reject</Button></Group>
          ) : r.status === 'approved' ? <Button size="compact-xs" variant="subtle" color="red" onClick={() => act(r, 'revoke')}>Remove</Button> : null },
        ]} />
    </Stack>
  );
}
