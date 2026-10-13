import { ActionIcon, Button, Group, Modal, Select, Stack, Switch, Text, TextInput, Tooltip } from '@mantine/core';
import { IconDownload, IconPlus, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { shifts as shiftApi } from '../../api';
import type { Holiday } from '../../api/types';
import { DateField } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { fmtDateLong, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;

export default function HolidaysPage() {
  const q = useQuery(() => shiftApi.holidays(), []);
  const [edit, setEdit] = useState<{ id?: string; localDate: string | null; nameEn: string; nameNe: string; genderScope: 'all' | 'female'; optional: boolean } | null>(null);
  const [del, setDel] = useState<Holiday | null>(null); const [busy, setBusy] = useState(false); const [importing, setImporting] = useState(false);
  const save = async () => {
    if (!edit?.localDate) return; setBusy(true);
    try { const { id, ...b } = edit; if (id) await shiftApi.updateHoliday(id, b); else await shiftApi.createHoliday(b); toast.ok('Holiday saved. Affected days are being recalculated.'); setEdit(null); await q.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const importDefaults = async () => {
    setImporting(true);
    try { const r = await shiftApi.importHolidays(2083); toast.ok(`${r.inserted} of ${r.total} public holidays added.`); await q.reload(); }
    catch (e) { toast.err(e); } finally { setImporting(false); }
  };
  return (
    <Stack gap="lg">
      <PageHeader title="Holidays" subtitle="Pick 13 paid public holidays (14 for women) from the government list (Sec 41). Teej and Jitiya apply to women only."
        actions={<>
          <Button variant="default" leftSection={<IconDownload {...ICON} />} loading={importing} onClick={importDefaults}>Import public holidays</Button>
          <Button leftSection={<IconPlus {...ICON} />} onClick={() => setEdit({ localDate: todayNpt(), nameEn: '', nameNe: '', genderScope: 'all', optional: false })}>Add holiday</Button>
        </>} />
      <DataTable<Holiday> rows={q.data?.holidays ?? []} rowKey={(h) => h.id} loading={q.loading} error={q.error}
        emptyTitle="No holidays yet" emptyHint="Import this year's public holiday list, then remove the days your company does not observe."
        onRowClick={(h) => setEdit({ id: h.id, localDate: h.local_date, nameEn: h.name_en, nameNe: h.name_ne ?? '', genderScope: h.gender_scope, optional: h.optional })}
        columns={[
          { key: 'date', header: 'Date', width: 180, render: (h) => <Text size="sm" className="num">{fmtDateLong(h.local_date)}</Text> },
          { key: 'name', header: 'Holiday', render: (h) => <><Text size="sm" fw={500}>{h.name_en}</Text>{h.optional ? <Text size="xs" c="dimmed">Optional</Text> : null}</> },
          { key: 'scope', header: 'Applies to', render: (h) => <Text size="sm" c={h.gender_scope === 'female' ? 'inherit' : 'dimmed'}>{h.gender_scope === 'female' ? 'Women only' : 'Everyone'}</Text> },
          {
            key: 'a', header: '', align: 'right', width: 56, render: (h) => (
              <Tooltip label="Delete holiday">
                <ActionIcon variant="subtle" color="gray" aria-label={`Delete ${h.name_en}`} onClick={(e) => { e.stopPropagation(); setDel(h); }}><IconTrash {...ICON} /></ActionIcon>
              </Tooltip>
            ),
          },
        ]} />
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.id ? 'Edit holiday' : 'New holiday'} centered>
        {edit ? (
          <Stack>
            <DateField label="Date" value={edit.localDate} onChange={(d) => setEdit({ ...edit, localDate: d })} required />
            <TextInput label="Name" value={edit.nameEn} onChange={(e) => setEdit({ ...edit, nameEn: e.currentTarget.value })} required />
            <Select label="Applies to" value={edit.genderScope} onChange={(v) => v && setEdit({ ...edit, genderScope: v as 'all' | 'female' })} allowDeselect={false} data={[{ value: 'all', label: 'Everyone' }, { value: 'female', label: 'Women only (Teej, Jitiya)' }]} />
            <Switch label="Optional holiday" description="Employees choose whether to take it" checked={edit.optional} onChange={(e) => setEdit({ ...edit, optional: e.currentTarget.checked })} />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setEdit(null)}>Cancel</Button>
              <Button loading={busy} disabled={!edit.localDate || !edit.nameEn} onClick={save}>Save holiday</Button>
            </Group>
          </Stack>
        ) : null}
      </Modal>
      <ConfirmDialog opened={!!del} onClose={() => setDel(null)} title={`Delete ${del?.name_en ?? 'holiday'}?`} confirmLabel="Delete holiday" message="Attendance for that date is recalculated for every active employee." onConfirm={async () => { if (!del) return; try { await shiftApi.deleteHoliday(del.id); await q.reload(); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
