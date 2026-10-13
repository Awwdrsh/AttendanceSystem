import { Button, Group, Modal, NumberInput, Select, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { shifts as shiftApi } from '../../api';
import type { Shift } from '../../api/types';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { BsDatePicker } from '../../components/BsDatePicker';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const blank = { code: '', nameEn: '', nameNe: '', start: '10:00', end: '18:00', graceInMin: 15, graceOutMin: 0, earlyInWindowMin: 60, lateOutWindowMin: 120, breakMinutes: 0, halfDayThresholdMin: 240, absentThresholdMin: 120, pairingMode: 'alternating', roundingMin: 0, roundingDirection: 'nearest' };

export default function ShiftsPage() {
  const { t } = useI18n();
  const q = useQuery(() => shiftApi.list(), []);
  const [edit, setEdit] = useState<(typeof blank & { id?: string }) | null>(null);
  const [del, setDel] = useState<Shift | null>(null);
  const [assign, setAssign] = useState<Shift | null>(null); const [assignEmp, setAssignEmp] = useState<string | null>(null); const [assignFrom, setAssignFrom] = useState<string | null>(todayNpt());
  const [busy, setBusy] = useState(false);
  const n = (k: keyof typeof blank) => ({ value: edit?.[k] as number, onChange: (v: number | string) => setEdit((e) => e && ({ ...e, [k]: Number(v) })), min: 0 });
  const save = async () => {
    if (!edit) return; setBusy(true);
    try { const { id, ...body } = edit; if (id) await shiftApi.update(id, body); else await shiftApi.create(body); toast.ok('Shift saved.'); setEdit(null); await q.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('shifts')} subtitle="Shift rules follow the attendance engine: grace, early-in/late-out windows, half-day and absent thresholds (plan §10.2)." actions={<Button onClick={() => setEdit({ ...blank })}>{t('create')}</Button>} />
      <DataTable<Shift> rows={q.data?.shifts ?? []} rowKey={(s) => s.id} loading={q.loading} error={q.error} emptyTitle="No shifts yet." emptyHint="Create your first shift. Most companies start with General 10:00–18:00." onRowClick={(s) => setEdit({ id: s.id, code: s.code, nameEn: s.name_en, nameNe: s.name_ne ?? '', start: s.start.slice(0, 5), end: s.end.slice(0, 5), graceInMin: s.grace_in_min, graceOutMin: s.grace_out_min, earlyInWindowMin: s.early_in_window_min, lateOutWindowMin: s.late_out_window_min, breakMinutes: s.break_minutes, halfDayThresholdMin: s.half_day_threshold_min, absentThresholdMin: s.absent_threshold_min, pairingMode: s.pairing_mode, roundingMin: s.rounding_min, roundingDirection: s.rounding_direction })}
        columns={[
          { key: 'code', header: t('code'), render: (s) => <><Text size="sm" fw={500}>{s.code}</Text><Text size="xs" c="dimmed">{s.name_en}</Text></> },
          { key: 'time', header: 'Hours', render: (s) => `${s.start.slice(0, 5)}–${s.end.slice(0, 5)}${s.crosses_midnight ? ' (+1 day)' : ''}` },
          { key: 'grace', header: 'Grace', render: (s) => `${s.grace_in_min} / ${s.grace_out_min} min` },
          { key: 'thr', header: 'Half / absent', render: (s) => `${s.half_day_threshold_min} / ${s.absent_threshold_min} min` },
          { key: 'n', header: 'Employees', render: (s) => s.assigned_employees, align: 'right' },
          { key: 'a', header: '', render: (s) => <Group gap={4}><Button size="xs" variant="light" onClick={(e) => { e.stopPropagation(); setAssign(s); }}>Assign</Button><Button size="xs" variant="subtle" color="red" onClick={(e) => { e.stopPropagation(); setDel(s); }}>{t('delete')}</Button></Group> },
        ]} />
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? `${t('edit')} ${edit.code}` : 'New shift'} size="lg" centered>
        {edit ? <Stack>
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label={t('code')} value={edit.code} onChange={(e) => setEdit({ ...edit, code: e.currentTarget.value.toUpperCase() })} required />
            <TextInput label="Name" value={edit.nameEn} onChange={(e) => setEdit({ ...edit, nameEn: e.currentTarget.value })} required />
            <TextInput label="Start (HH:MM)" value={edit.start} onChange={(e) => setEdit({ ...edit, start: e.currentTarget.value })} />
            <TextInput label="End (HH:MM)" description="End earlier than start = night shift crossing midnight" value={edit.end} onChange={(e) => setEdit({ ...edit, end: e.currentTarget.value })} />
            <NumberInput label="Grace in (min)" description="Mark Late if IN is after start + grace" {...n('graceInMin')} />
            <NumberInput label="Grace out (min)" {...n('graceOutMin')} />
            <NumberInput label="Early-in window (min)" {...n('earlyInWindowMin')} />
            <NumberInput label="Late-out window (min)" {...n('lateOutWindowMin')} />
            <NumberInput label="Fixed break (min)" description="30 min after 5 h (Sec 28(2)); 0 = measured from punches" {...n('breakMinutes')} />
            <NumberInput label="Half-day if worked under (min)" {...n('halfDayThresholdMin')} />
            <NumberInput label="Absent if worked under (min)" {...n('absentThresholdMin')} />
            <Select label="Pairing" value={edit.pairingMode} onChange={(v) => v && setEdit({ ...edit, pairingMode: v })} allowDeselect={false} data={[{ value: 'alternating', label: 'Alternating IN/OUT' }, { value: 'strict_log_type', label: 'Use device direction' }, { value: 'first_last', label: 'First IN to last OUT' }]} />
            <NumberInput label="Round worked minutes to" {...n('roundingMin')} />
            <Select label="Rounding direction" value={edit.roundingDirection} onChange={(v) => v && setEdit({ ...edit, roundingDirection: v })} allowDeselect={false} data={['nearest', 'down', 'up']} />
          </SimpleGrid>
          <Group justify="flex-end"><Button loading={busy} onClick={save}>{t('save')}</Button></Group>
        </Stack> : null}
      </Modal>
      <Modal opened={!!assign} onClose={() => setAssign(null)} title={`Assign ${assign?.code ?? ''}`} centered>
        <Stack><EmployeePicker value={assignEmp} onChange={setAssignEmp} required /><BsDatePicker label="From" value={assignFrom} onChange={setAssignFrom} required />
          <Group justify="flex-end"><Button disabled={!assignEmp || !assignFrom} onClick={async () => { if (!assign || !assignEmp || !assignFrom) return; try { await shiftApi.assign(assignEmp, assign.id, assignFrom); toast.ok('Shift assigned.'); setAssign(null); await q.reload(); } catch (e) { toast.err(e); } }}>{t('save')}</Button></Group></Stack>
      </Modal>
      <ConfirmDialog opened={!!del} onClose={() => setDel(null)} title={`Delete shift ${del?.code ?? ''}?`} confirmLabel={t('delete')} message={Number(del?.assigned_employees ?? 0) > 0 ? `${del?.assigned_employees} employee(s) are assigned — reassign them first; deletion will be refused.` : 'This shift is not assigned to anyone. Historical attendance keeps its data.'}
        onConfirm={async () => { if (!del) return; try { await shiftApi.remove(del.id); toast.ok('Shift deleted.'); await q.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
