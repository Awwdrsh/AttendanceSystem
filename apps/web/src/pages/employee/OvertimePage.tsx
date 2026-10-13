import { Button, Drawer, Group, NumberInput, Stack, Tabs, Text, Textarea } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { ot as otApi } from '../../api';
import type { OtRequest, OtSlip } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { fmtAd, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const dayType = (s: string) => ({ working: 'working day', workday: 'working day', weekly_off: 'weekly off', weeklyoff: 'weekly off', holiday: 'holiday' } as Record<string, string>)[s.toLowerCase()] ?? s.replace(/_/g, ' ').toLowerCase();

export default function OvertimePage() {
  const { user } = useAuth();
  const requests = useQuery(() => otApi.requests({ scope: 'own' }), []);
  const slips = useQuery(() => otApi.slips(), []);
  const [open, setOpen] = useState(false);
  const [date, setDate] = useState<string | null>(todayNpt()); const [minutes, setMinutes] = useState<number | string>(60); const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!date) return; setBusy(true);
    try { await otApi.request({ localDate: date, requestedMin: Number(minutes), reason }); toast.ok('Overtime request sent to your manager.'); setOpen(false); await requests.reload(); }
    catch (e) { toast.err(e, 'Request not sent'); } finally { setBusy(false); }
  };
  if (!user?.employeeId) return null;
  return (
    <Stack gap="lg">
      <PageHeader title="Overtime" subtitle="Only approved overtime is paid. Limits are 4 hours a day and 24 hours a week (Labour Act, section 30)."
        actions={<Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setOpen(true)}>Request pre-approval</Button>} />

      <Tabs defaultValue="requests">
        <Tabs.List mb="md">
          <Tabs.Tab value="requests">My requests</Tabs.Tab>
          <Tabs.Tab value="detected">Recorded overtime</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="requests">
          <DataTable<OtRequest> rows={requests.data?.requests ?? []} rowKey={(r) => r.id} loading={requests.loading} error={requests.error}
            emptyTitle="No overtime requests yet" emptyHint="Planning to stay late? Request pre-approval so the extra time is paid."
            columns={[
              { key: 'date', header: 'Date', render: (r) => <Text size="sm" fw={500}>{fmtAd(r.local_date)}</Text> },
              { key: 'req', header: 'Requested', align: 'right', render: (r) => <span className="num">{minutesToHm(r.requested_min)}</span> },
              { key: 'apr', header: 'Approved', align: 'right', render: (r) => <span className="num">{r.approved_min === null ? '—' : minutesToHm(r.approved_min)}</span> },
              { key: 'reason', header: 'Reason', render: (r) => <Text size="sm" c="dimmed" lineClamp={1} maw={260}>{r.reason || '—'}</Text> },
              { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
              { key: 'a', header: '', align: 'right', render: (r) => r.status === 'pending' ? <Button size="xs" variant="subtle" color="red" onClick={async () => { try { await otApi.cancelRequest(r.id); await requests.reload(); } catch (e) { toast.err(e, 'Could not cancel'); } }}>Cancel request</Button> : null },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="detected">
          <DataTable<OtSlip> rows={slips.data?.slips ?? []} rowKey={(r) => r.id} loading={slips.loading} error={slips.error}
            emptyTitle="No overtime recorded yet" emptyHint="Time you work beyond your shift shows here once it is calculated."
            columns={[
              { key: 'date', header: 'Date', render: (r) => <Text size="sm" fw={500}>{fmtAd(r.local_date)}</Text> },
              { key: 'det', header: 'Worked extra', align: 'right', render: (r) => <span className="num">{minutesToHm(r.detected_min)}</span> },
              { key: 'apr', header: 'Approved', align: 'right', render: (r) => <span className="num">{r.approved_min === null ? '—' : minutesToHm(r.approved_min)}</span> },
              { key: 'rate', header: 'Rate', align: 'right', render: (r) => <><span className="num">{r.multiplier}×</span> <Text span size="sm" c="dimmed">{dayType(r.day_type)}</Text></> },
              { key: 'status', header: 'Status', render: (r) => <StatusBadge status={r.status} /> },
            ]} />
        </Tabs.Panel>
      </Tabs>

      <Drawer opened={open} onClose={() => setOpen(false)} position="right" size="md" title={<Text fw={600}>Request overtime pre-approval</Text>}>
        <Stack>
          <DateField label="Date" value={date} onChange={setDate} required />
          <NumberInput label="Extra time (minutes)" description="Between 15 and 240 minutes, in steps of 15." value={minutes} onChange={setMinutes} min={15} max={240} step={15} />
          <Textarea label="Reason" description="What needs the extra time? Your manager sees this." value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
          <Group justify="flex-end"><Button loading={busy} disabled={!date} onClick={submit}>Send request</Button></Group>
        </Stack>
      </Drawer>
    </Stack>
  );
}
