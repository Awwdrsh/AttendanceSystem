import { Alert, Button, Group, Paper, Select, Stack, Textarea, TextInput } from '@mantine/core';
import { useState } from 'react';
import { punch as punchApi, regularisation as regApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { BsDatePicker } from '../../components/BsDatePicker';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';

/** Fix a punch on behalf of a worker. HR entries are applied immediately (audited); managers raise a request. */
export default function FixPunchPage() {
  const { t, lang } = useI18n();
  const { hasRole } = useAuth();
  const [employeeId, setEmployeeId] = useState<string | null>(null);
  const [date, setDate] = useState<string | null>(todayNpt());
  const [time, setTime] = useState('18:00'); const [direction, setDirection] = useState<'in' | 'out'>('out'); const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const isHr = hasRole('hr');
  const submit = async () => {
    if (!employeeId || !date) return; setBusy(true);
    try {
      if (isHr) { const r = await punchApi.manual({ employeeId, localDate: date, time, direction, reason }); toast.ok(r.duplicate ? 'That punch already existed.' : `Punch saved; day is now ${r.day?.status ?? 'recomputed'}.`); }
      else { await regApi.create({ employeeId, localDate: date, kind: 'missed_punch', punchTime: `${date}T${time}:00+05:45`, punchDirection: direction, reason }); toast.ok('Correction request raised for approval.'); }
      setReason('');
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('fixPunch')} subtitle={isHr ? (lang === 'ne' ? 'मानव संसाधनका प्रविष्टि तुरुन्त लागू हुन्छन् र लग हुन्छन्।' : 'HR entries apply immediately and are written to the audit log.') : (lang === 'ne' ? 'अनुरोध स्वीकृतिका लागि पठाइन्छ।' : 'Your entry is raised as a correction request and approved through the normal chain.')} />
      <Paper withBorder p="md" radius="md" maw={560}>
        <Stack>
          <EmployeePicker value={employeeId} onChange={setEmployeeId} required />
          <BsDatePicker label={t('date')} value={date} onChange={setDate} maxDate={todayNpt()} required />
          <Group grow>
            <TextInput label="Time (24 h)" value={time} onChange={(e) => setTime(e.currentTarget.value)} placeholder="HH:MM" />
            <Select label="Direction" value={direction} onChange={(v) => v && setDirection(v as 'in' | 'out')} allowDeselect={false} data={[{ value: 'in', label: 'IN' }, { value: 'out', label: 'OUT' }]} />
          </Group>
          <Textarea label={t('reason')} required value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
          <Alert variant="light" color="gray">{lang === 'ne' ? 'कर्मचारीले आफ्नो हाजिरी लगमा यो प्रविष्टि देख्छन्।' : 'The employee can see this entry in their own punch log, with the reason.'}</Alert>
          <Group justify="flex-end"><Button h={48} loading={busy} disabled={!employeeId || !date || reason.trim().length < 3 || !/^([01]\d|2[0-3]):[0-5]\d$/.test(time)} onClick={submit}>{t('save')}</Button></Group>
        </Stack>
      </Paper>
    </Stack>
  );
}
