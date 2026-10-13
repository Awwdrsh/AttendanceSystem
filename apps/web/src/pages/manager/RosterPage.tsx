import { Button, Group, Paper, ScrollArea, Select, Stack, Table, Text } from '@mantine/core';
import { useMemo, useState } from 'react';
import { shifts as shiftApi } from '../../api';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { addDays, dualDate, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Week grid: employees × 7 days, each cell a shift select; copy last week; publish (plan §5.3). */
export default function RosterPage() {
  const { t, lang, numerals } = useI18n();
  const [weekStart, setWeekStart] = useState(addDays(todayNpt(), -new Date(todayNpt()).getUTCDay()));
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)), [weekStart]);
  const shifts = useQuery(() => shiftApi.list(), []);
  const roster = useQuery(() => shiftApi.roster(weekStart, days[6]!), [weekStart]);
  const [pending, setPending] = useState<Map<string, string | null>>(new Map());
  const [confirm, setConfirm] = useState<'copy' | 'publish' | null>(null);
  const [busy, setBusy] = useState(false);
  const cur = new Map((roster.data?.roster ?? []).map((r) => [`${r.employee_id}|${r.local_date}`, r.shift_id]));
  const shiftOpts = [{ value: '__off', label: lang === 'ne' ? 'बिदा' : 'Off' }, ...(shifts.data?.shifts ?? []).map((s) => ({ value: s.id, label: `${s.code} ${s.start.slice(0, 5)}–${s.end.slice(0, 5)}` }))];
  const valueFor = (emp: string, d: string, def: string | null) => { const k = `${emp}|${d}`; if (pending.has(k)) return pending.get(k) ?? '__off'; if (cur.has(k)) return cur.get(k) ?? '__off'; return def ?? '__default'; };
  const save = async () => {
    setBusy(true);
    try { const rows = [...pending.entries()].map(([k, v]) => { const [employeeId, localDate] = k.split('|') as [string, string]; return { employeeId, localDate, shiftId: v }; }); await shiftApi.saveRoster(rows); toast.ok(`${rows.length} roster entries saved; attendance will be recomputed.`); setPending(new Map()); await roster.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('roster')} subtitle={`${dualDate(weekStart, lang, numerals).bsLong} → ${dualDate(days[6]!, lang, numerals).bsLong}`}
        actions={<Group gap="xs"><Button variant="default" onClick={() => setWeekStart(addDays(weekStart, -7))}>‹</Button><Button variant="default" onClick={() => setWeekStart(addDays(weekStart, 7))}>›</Button><Button variant="light" onClick={() => setConfirm('copy')}>Copy last week</Button><Button variant="light" onClick={() => setConfirm('publish')}>Publish</Button><Button disabled={pending.size === 0} loading={busy} onClick={save}>{t('save')} ({pending.size})</Button></Group>} />
      <Paper withBorder radius="md">
        <ScrollArea type="auto">
          <Table miw={900} stickyHeader>
            <Table.Thead><Table.Tr><Table.Th style={{ position: 'sticky', left: 0, background: 'var(--mantine-color-body)', zIndex: 2 }}>{t('employees')}</Table.Th>{days.map((d) => <Table.Th key={d}><Text size="xs">{dualDate(d, lang, numerals).bs.slice(5)}</Text><Text size="xs" c={new Date(`${d}T00:00:00Z`).getUTCDay() === 6 ? 'red' : 'dimmed'}>{['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'][new Date(`${d}T00:00:00Z`).getUTCDay()]} {d.slice(5)}</Text></Table.Th>)}</Table.Tr></Table.Thead>
            <Table.Tbody>
              {(roster.data?.employees ?? []).map((e) => (
                <Table.Tr key={e.id}>
                  <Table.Td style={{ position: 'sticky', left: 0, background: 'var(--mantine-color-body)' }}><Text size="sm" fw={500}>{e.name_en}</Text><Text size="xs" c="dimmed">{e.emp_code}</Text></Table.Td>
                  {days.map((d) => {
                    const v = valueFor(e.id, d, e.default_shift_id);
                    const isDefault = v === '__default';
                    const weeklyOff = e.weekly_off_days.includes(new Date(`${d}T00:00:00Z`).getUTCDay());
                    return <Table.Td key={d} style={{ background: weeklyOff ? 'var(--mantine-color-gray-light)' : undefined }}>
                      <Select size="xs" value={isDefault ? null : v} placeholder={weeklyOff ? 'weekly off' : 'default'} data={shiftOpts} clearable
                        onChange={(nv) => { const n = new Map(pending); n.set(`${e.id}|${d}`, nv === '__off' ? null : nv); if (nv === null) n.delete(`${e.id}|${d}`); setPending(n); }} />
                    </Table.Td>;
                  })}
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </ScrollArea>
      </Paper>
      <Text size="xs" c="dimmed">{lang === 'ne' ? 'खाली = कर्मचारीको सामान्य सिफ्ट। "बिदा" = त्यो दिन सिफ्ट छैन।' : 'Blank = the employee\'s assigned shift. "Off" = no shift that day. Locked payroll days cannot be changed.'}</Text>
      <ConfirmDialog opened={confirm === 'copy'} onClose={() => setConfirm(null)} title="Copy last week" color="teal" confirmLabel="Copy" message={`Copy every roster entry from the previous week onto ${weekStart}..${days[6]}. Existing entries for this week are overwritten.`}
        onConfirm={async () => { try { const r = await shiftApi.copyWeek(weekStart); toast.ok(`${r.copied} entries copied.`); await roster.reload(); } catch (e) { toast.err(e); } }} />
      <ConfirmDialog opened={confirm === 'publish'} onClose={() => setConfirm(null)} title="Publish roster" color="teal" confirmLabel="Publish" message="Every rostered employee gets a notification with their shifts for this week."
        onConfirm={async () => { try { const r = await shiftApi.publish(weekStart, days[6]!); toast.ok(`${r.notified} employees notified.`); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
