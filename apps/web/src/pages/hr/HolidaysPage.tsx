import { Button, Group, Modal, Select, Stack, Switch, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { shifts as shiftApi } from '../../api';
import type { Holiday } from '../../api/types';
import { BsDatePicker } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { dualDate, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function HolidaysPage() {
  const { t, lang, numerals } = useI18n();
  const q = useQuery(() => shiftApi.holidays(), []);
  const [edit, setEdit] = useState<{ id?: string; localDate: string | null; nameEn: string; nameNe: string; genderScope: 'all' | 'female'; optional: boolean } | null>(null);
  const [del, setDel] = useState<Holiday | null>(null); const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!edit?.localDate) return; setBusy(true);
    try { const { id, ...b } = edit; if (id) await shiftApi.updateHoliday(id, b); else await shiftApi.createHoliday(b); toast.ok('Holiday saved; affected days queued for recompute.'); setEdit(null); await q.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('holidays')} subtitle={lang === 'ne' ? 'रोजगारदाताले १३ (महिलाका लागि १४) तलबी बिदा छान्छ (दफा ४१)।' : 'The employer picks 13 paid days (14 for women) from the public list (Sec 41). Teej and Jitiya are women-only.'}
        actions={<Group gap="xs"><Button variant="light" onClick={async () => { try { const r = await shiftApi.importHolidays(2083); toast.ok(`${r.inserted} of ${r.total} default BS 2083 holidays added.`); await q.reload(); } catch (e) { toast.err(e); } }}>Import default 2083</Button><Button onClick={() => setEdit({ localDate: todayNpt(), nameEn: '', nameNe: '', genderScope: 'all', optional: false })}>{t('create')}</Button></Group>} />
      <DataTable<Holiday> rows={q.data?.holidays ?? []} rowKey={(h) => h.id} loading={q.loading} error={q.error} emptyTitle="No holidays yet." emptyHint="Import the default BS 2083 list, then adjust." onRowClick={(h) => setEdit({ id: h.id, localDate: h.local_date, nameEn: h.name_en, nameNe: h.name_ne ?? '', genderScope: h.gender_scope, optional: h.optional })}
        columns={[
          { key: 'date', header: t('date'), render: (h) => <><Text size="sm" fw={500}>{dualDate(h.local_date, lang, numerals).bsLong}</Text><Text size="xs" c="dimmed">{dualDate(h.local_date).ad}</Text></> },
          { key: 'name', header: t('name'), render: (h) => `${h.name_en}${h.name_ne ? ` / ${h.name_ne}` : ''}` },
          { key: 'scope', header: 'Who', render: (h) => h.gender_scope === 'female' ? 'Women only' : 'Everyone' },
          { key: 'opt', header: 'Optional', render: (h) => h.optional ? 'yes' : '' },
          { key: 'a', header: '', render: (h) => <Button size="xs" variant="subtle" color="red" onClick={(e) => { e.stopPropagation(); setDel(h); }}>{t('delete')}</Button> },
        ]} />
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? t('edit') : 'New holiday'} centered>
        {edit ? <Stack>
          <BsDatePicker label={t('date')} value={edit.localDate} onChange={(d) => setEdit({ ...edit, localDate: d })} required />
          <TextInput label="Name (English)" value={edit.nameEn} onChange={(e) => setEdit({ ...edit, nameEn: e.currentTarget.value })} required />
          <TextInput label="नाम (नेपाली)" value={edit.nameNe} onChange={(e) => setEdit({ ...edit, nameNe: e.currentTarget.value })} />
          <Select label="Applies to" value={edit.genderScope} onChange={(v) => v && setEdit({ ...edit, genderScope: v as 'all' | 'female' })} allowDeselect={false} data={[{ value: 'all', label: 'Everyone' }, { value: 'female', label: 'Women only (Teej, Jitiya)' }]} />
          <Switch label="Optional / restricted holiday" checked={edit.optional} onChange={(e) => setEdit({ ...edit, optional: e.currentTarget.checked })} />
          <Group justify="flex-end"><Button loading={busy} disabled={!edit.localDate || !edit.nameEn} onClick={save}>{t('save')}</Button></Group>
        </Stack> : null}
      </Modal>
      <ConfirmDialog opened={!!del} onClose={() => setDel(null)} title={`Delete ${del?.name_en ?? ''}?`} confirmLabel={t('delete')} message="Attendance for that date is recomputed for every active employee." onConfirm={async () => { if (!del) return; try { await shiftApi.deleteHoliday(del.id); await q.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
