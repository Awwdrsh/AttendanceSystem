import { ActionIcon, Button, Group, Paper, ScrollArea, Select, Stack, Table, Text, Tooltip } from '@mantine/core';
import { IconChevronLeft, IconChevronRight, IconCopy, IconDeviceFloppy, IconSend } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { shifts as shiftApi } from '../../api';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { addDays, fmtAd, fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const dow = (d: string) => new Date(`${d}T00:00:00Z`).getUTCDay();

/** Week grid: employees × 7 days, each cell a shift select; copy last week; publish (plan §5.3). */
export default function RosterPage() {
  const today = todayNpt();
  const [weekStart, setWeekStart] = useState(addDays(today, -dow(today)));
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)), [weekStart]);
  const shifts = useQuery(() => shiftApi.list(), []);
  const roster = useQuery(() => shiftApi.roster(weekStart, days[6]!), [weekStart]);
  const [pending, setPending] = useState<Map<string, string | null>>(new Map());
  const [confirm, setConfirm] = useState<'copy' | 'publish' | null>(null);
  const [busy, setBusy] = useState(false);
  const cur = new Map((roster.data?.roster ?? []).map((r) => [`${r.employee_id}|${r.local_date}`, r.shift_id]));
  const shiftOpts = [{ value: '__off', label: 'Off' }, ...(shifts.data?.shifts ?? []).map((s) => ({ value: s.id, label: `${s.start.slice(0, 5)}–${s.end.slice(0, 5)}`, title: s.name_en }))];
  const cellOpts = [{ value: '__clear', label: 'Assigned shift' }, ...shiftOpts];
  const valueFor = (emp: string, d: string, def: string | null) => { const k = `${emp}|${d}`; if (pending.has(k)) return pending.get(k) ?? '__off'; if (cur.has(k)) return cur.get(k) ?? '__off'; return def ?? '__default'; };
  const save = async () => {
    setBusy(true);
    try { const rows = [...pending.entries()].map(([k, v]) => { const [employeeId, localDate] = k.split('|') as [string, string]; return { employeeId, localDate, shiftId: v }; }); await shiftApi.saveRoster(rows); toast.ok(`${rows.length} roster ${rows.length === 1 ? 'entry' : 'entries'} saved. Attendance will be recalculated.`); setPending(new Map()); await roster.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const employees = roster.data?.employees ?? [];
  const stickyCell = { position: 'sticky' as const, left: 0, background: 'var(--hj-surface)', zIndex: 2, borderRight: '1px solid var(--hj-line-soft)' };

  return (
    <Stack gap="md">
      <PageHeader title="Roster" subtitle={`Week of ${fmtAd(weekStart)} to ${fmtAd(days[6]!)}. Leave a cell blank to use the employee's assigned shift.`}
        actions={<>
          <Group gap={4} wrap="nowrap">
            <Tooltip label="Previous week"><ActionIcon variant="default" size={36} aria-label="Previous week" onClick={() => setWeekStart(addDays(weekStart, -7))}><IconChevronLeft size={16} stroke={1.6} /></ActionIcon></Tooltip>
            <Button variant="default" onClick={() => setWeekStart(addDays(today, -dow(today)))}>This week</Button>
            <Tooltip label="Next week"><ActionIcon variant="default" size={36} aria-label="Next week" onClick={() => setWeekStart(addDays(weekStart, 7))}><IconChevronRight size={16} stroke={1.6} /></ActionIcon></Tooltip>
          </Group>
          <Button variant="default" leftSection={<IconCopy size={16} stroke={1.6} />} onClick={() => setConfirm('copy')}>Copy last week</Button>
          <Button variant="default" leftSection={<IconSend size={16} stroke={1.6} />} onClick={() => setConfirm('publish')}>Publish</Button>
          <Button leftSection={<IconDeviceFloppy size={16} stroke={1.6} />} disabled={pending.size === 0} loading={busy} onClick={save}>{pending.size ? `Save ${pending.size} ${pending.size === 1 ? 'change' : 'changes'}` : 'Save changes'}</Button>
        </>} />

      {roster.error ? <EmptyState title="Could not load the roster" hint={`${roster.error}. Refresh the page to try again.`} />
        : !roster.loading && employees.length === 0 ? <EmptyState title="No one to roster yet" hint="Employees who report to you will appear here once they are added." />
        : (
          <Paper withBorder style={{ overflow: 'hidden' }}>
            <ScrollArea type="auto">
              <Table miw={1040} stickyHeader highlightOnHover={false} verticalSpacing={8} horizontalSpacing="sm">
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th style={{ ...stickyCell, zIndex: 3, minWidth: 180 }}>Employee</Table.Th>
                    {days.map((d) => {
                      const isToday = d === today;
                      return (
                        <Table.Th key={d} style={{ minWidth: 112 }}>
                          <Text size="xs" fw={isToday ? 600 : 500} c={isToday ? 'brand' : 'dimmed'}>{WEEKDAYS[dow(d)]} {fmtDateShort(d)}{isToday ? ', today' : ''}</Text>
                        </Table.Th>
                      );
                    })}
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {employees.map((e) => (
                    <Table.Tr key={e.id}>
                      <Table.Td style={stickyCell}><Text size="sm" fw={500}>{e.name_en}</Text><Text size="xs" c="dimmed">{e.emp_code}</Text></Table.Td>
                      {days.map((d) => {
                        const k = `${e.id}|${d}`;
                        const v = valueFor(e.id, d, e.default_shift_id);
                        const isDefault = v === '__default';
                        const weeklyOff = e.weekly_off_days.includes(dow(d));
                        const changed = pending.has(k);
                        return (
                          <Table.Td key={d} style={{ background: changed ? 'var(--mantine-color-brand-light)' : weeklyOff ? 'var(--hj-canvas)' : undefined }}>
                            <Select size="xs" variant={isDefault ? 'unstyled' : 'filled'} value={isDefault ? null : v} placeholder={weeklyOff ? 'Weekly off' : 'Assigned shift'} data={cellOpts}
                              rightSection={<span />} rightSectionWidth={4} comboboxProps={{ width: 200, position: 'bottom-start' }} allowDeselect={false}
                              aria-label={`${e.name_en}, ${WEEKDAYS[dow(d)]} ${fmtDateShort(d)}`}
                              styles={{ input: { paddingInline: 8, fontVariantNumeric: 'tabular-nums', ...(isDefault ? { color: 'var(--hj-muted)' } : { fontWeight: 500 }) } }}
                              onChange={(nv) => { const n = new Map(pending); if (nv === '__clear' || nv === null) n.delete(k); else n.set(k, nv === '__off' ? null : nv); setPending(n); }} />
                          </Table.Td>
                        );
                      })}
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </ScrollArea>
          </Paper>
        )}
      <Text size="xs" c="dimmed">Blank cells use the employee's assigned shift. "Off" means no shift that day. Highlighted cells are unsaved changes. Days already locked by payroll cannot be changed.</Text>

      <ConfirmDialog opened={confirm === 'copy'} onClose={() => setConfirm(null)} title="Copy last week" color="brand" confirmLabel="Copy roster" message={`Every roster entry from the previous week is copied onto ${fmtAd(weekStart)} to ${fmtAd(days[6]!)}. Existing entries for this week are overwritten.`}
        onConfirm={async () => { try { const r = await shiftApi.copyWeek(weekStart); toast.ok(`${r.copied} entries copied.`); await roster.reload(); } catch (e) { toast.err(e); } }} />
      <ConfirmDialog opened={confirm === 'publish'} onClose={() => setConfirm(null)} title="Publish roster" color="brand" confirmLabel="Publish and notify" message="Every rostered employee gets a notification with their shifts for this week."
        onConfirm={async () => { try { const r = await shiftApi.publish(weekStart, days[6]!); toast.ok(`${r.notified} employees notified.`); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
