import { ActionIcon, Badge, Button, Group, Modal, Paper, Select, Skeleton, Stack, Switch, Text, TextInput, Tooltip } from '@mantine/core';
import { IconChevronLeft, IconChevronRight, IconDownload, IconPlus, IconTrash } from '@tabler/icons-react';
import { BS_MONTH_NAMES_EN } from '@hajiri/bs-calendar';
import { useState } from 'react';
import { shifts as shiftApi } from '../../api';
import type { Holiday } from '../../api/types';
import { DateField } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { fmtGregorian, safeAdToBs, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;
const WEEKDAY = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

export default function HolidaysPage() {
  const q = useQuery(() => shiftApi.holidays(), []);
  const today = todayNpt();
  const [year, setYear] = useState(() => safeAdToBs(today)?.year ?? 2083);
  const inYear = (q.data?.holidays ?? []).filter((h) => safeAdToBs(h.local_date)?.year === year).sort((a, b) => a.local_date.localeCompare(b.local_date));
  const byMonth = new Map<number, Holiday[]>();
  for (const h of inYear) { const m = safeAdToBs(h.local_date)!.month; byMonth.set(m, [...(byMonth.get(m) ?? []), h]); }
  const months = [...byMonth.entries()].sort((a, b) => a[0] - b[0]);
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
      <PageHeader title="Holidays" subtitle="Pick 13 paid public holidays (14 for women) from the government list (Sec 41). Red dates are observed by everyone; outlined ones are optional or for a specific community."
        actions={<>
          <Button variant="default" leftSection={<IconDownload {...ICON} />} loading={importing} onClick={importDefaults}>Import public holidays</Button>
          <Button leftSection={<IconPlus {...ICON} />} onClick={() => setEdit({ localDate: todayNpt(), nameEn: '', nameNe: '', genderScope: 'all', optional: false })}>Add holiday</Button>
        </>} />
      <Group justify="space-between" align="center">
        <Group gap={4} wrap="nowrap">
          <ActionIcon variant="default" size={34} aria-label="Previous year" onClick={() => setYear(year - 1)}><IconChevronLeft {...ICON} /></ActionIcon>
          <Text fw={700} fz={20} w={92} ta="center" className="num" style={{ letterSpacing: '-0.02em' }}>{year}</Text>
          <ActionIcon variant="default" size={34} aria-label="Next year" onClick={() => setYear(year + 1)}><IconChevronRight {...ICON} /></ActionIcon>
        </Group>
        <Text className="mono" fz={11} c="dimmed" style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}>
          {inYear.length} {inYear.length === 1 ? 'holiday' : 'holidays'}, {inYear.filter((h) => !h.optional).length} observed by everyone
        </Text>
      </Group>
      {q.error ? <EmptyState title="Could not load holidays" hint={q.error} /> : q.loading && !q.data ? <Skeleton h={240} radius="lg" /> : inYear.length === 0 ? (
        <EmptyState title={`No holidays for ${year} yet`} hint={year === 2083 ? 'Import the government list for 2083, then remove the days your company does not observe.' : 'Add the days your company observes this year.'}
          {...(year === 2083 ? { action: { label: 'Import public holidays', onClick: importDefaults } } : {})} />
      ) : (
        <Stack gap="md">
          {months.map(([m, list]) => (
            <Paper key={m} withBorder style={{ overflow: 'hidden' }}>
              <Group justify="space-between" px="md" py={10} style={{ borderBottom: '1px solid var(--hj-line)', background: 'var(--hj-raised)' }}>
                <Text fw={700} size="sm">{BS_MONTH_NAMES_EN[m - 1]} {year}</Text>
                <Text className="mono" fz={10.5} c="dimmed" style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}>{list.length} {list.length === 1 ? 'day' : 'days'}</Text>
              </Group>
              {list.map((h, i) => {
                const b = safeAdToBs(h.local_date)!;
                const past = h.local_date < today;
                return (
                  <Group key={h.id} wrap="nowrap" gap="md" px="md" py={10} onClick={() => setEdit({ id: h.id, localDate: h.local_date, nameEn: h.name_en, nameNe: h.name_ne ?? '', genderScope: h.gender_scope, optional: h.optional })}
                    style={{ cursor: 'pointer', borderTop: i ? '1px solid var(--hj-line-soft)' : undefined, opacity: past ? 0.6 : 1 }}>
                    <Stack gap={0} align="center" w={52} py={4} style={{ borderRadius: 6, border: '1px solid var(--hj-line)', background: h.optional ? 'transparent' : 'rgba(220, 38, 38, 0.06)', flex: 'none' }}>
                      <Text fz={20} fw={800} lh={1.1} className="num" c={h.optional ? 'inherit' : 'var(--hj-bad)'}>{b.day}</Text>
                      <Text className="mono" fz={9.5} c="dimmed" style={{ letterSpacing: '0.06em', textTransform: 'uppercase' }}>{WEEKDAY[new Date(`${h.local_date}T00:00:00Z`).getUTCDay()]}</Text>
                    </Stack>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <Text size="sm" fw={600} truncate>{h.name_en}</Text>
                      <Text size="xs" c="dimmed">{fmtGregorian(h.local_date)}</Text>
                    </div>
                    <Group gap={6} wrap="nowrap">
                      {h.gender_scope === 'female' ? <Badge variant="light" color="grape" size="sm">Women only</Badge> : null}
                      {h.optional ? <Badge variant="outline" color="gray" size="sm">Optional</Badge> : null}
                      <Tooltip label="Delete holiday">
                        <ActionIcon variant="subtle" color="gray" aria-label={`Delete ${h.name_en}`} onClick={(e) => { e.stopPropagation(); setDel(h); }}><IconTrash {...ICON} /></ActionIcon>
                      </Tooltip>
                    </Group>
                  </Group>
                );
              })}
            </Paper>
          ))}
        </Stack>
      )}
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
