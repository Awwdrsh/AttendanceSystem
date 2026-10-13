import { Alert, Button, Group, Input, Paper, SegmentedControl, SimpleGrid, Stack, Textarea, TextInput } from '@mantine/core';
import { IconClock, IconInfoCircle } from '@tabler/icons-react';
import { useState } from 'react';
import { punch as punchApi, regularisation as regApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { statusLabel } from '../../components/StatusBadge';
import { todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';

const TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Fix a punch on behalf of a worker. HR entries are applied immediately (audited); managers raise a request. */
export default function FixPunchPage() {
  const { hasRole } = useAuth();
  const [employeeId, setEmployeeId] = useState<string | null>(null);
  const [date, setDate] = useState<string | null>(todayNpt());
  const [time, setTime] = useState('18:00'); const [direction, setDirection] = useState<'in' | 'out'>('out'); const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const isHr = hasRole('hr');
  const timeOk = TIME_RE.test(time);
  const submit = async () => {
    if (!employeeId || !date) return; setBusy(true);
    try {
      if (isHr) { const r = await punchApi.manual({ employeeId, localDate: date, time, direction, reason }); toast.ok(r.duplicate ? 'That punch already existed.' : `Punch saved. The day is now ${r.day?.status ? statusLabel(r.day.status).toLowerCase() : 'recalculated'}.`); }
      else { await regApi.create({ employeeId, localDate: date, kind: 'missed_punch', punchTime: `${date}T${time}:00+05:45`, punchDirection: direction, reason }); toast.ok('Correction request sent for approval.'); }
      setReason('');
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack gap={0} maw={560}>
      <PageHeader title="Fix a punch"
        subtitle={isHr ? 'Add a missed clock-in or clock-out. HR entries apply immediately and are written to the audit log.' : 'Add a missed clock-in or clock-out. It is sent as an attendance correction and approved through the normal chain.'} />
      <Paper withBorder p="lg">
        <Stack gap="md">
          <EmployeePicker value={employeeId} onChange={setEmployeeId} required />
          <DateField label="Date" value={date} onChange={setDate} maxDate={todayNpt()} required />
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label="Time" description="24-hour clock, Nepal time" value={time} onChange={(e) => setTime(e.currentTarget.value)} placeholder="HH:MM"
              leftSection={<IconClock size={16} stroke={1.6} />} {...(time && !timeOk ? { error: 'Use HH:MM, for example 18:05' } : {})} />
            <Input.Wrapper label="Punch type" description="Which punch was missed">
              <SegmentedControl fullWidth mt={4} value={direction} onChange={(v) => setDirection(v as 'in' | 'out')} data={[{ value: 'in', label: 'Clock-in' }, { value: 'out', label: 'Clock-out' }]} />
            </Input.Wrapper>
          </SimpleGrid>
          <Textarea label="Reason" description="The employee sees this next to the entry." required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} placeholder="For example: forgot to clock out after the evening shift" />
          <Alert variant="light" color="gray" icon={<IconInfoCircle size={18} stroke={1.6} />}>The employee can see this entry and your reason in their own punch log.</Alert>
          <Group justify="flex-end">
            <Button loading={busy} disabled={!employeeId || !date || reason.trim().length < 3 || !timeOk} onClick={submit}>{isHr ? 'Save punch' : 'Send for approval'}</Button>
          </Group>
        </Stack>
      </Paper>
    </Stack>
  );
}
