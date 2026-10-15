import { ActionIcon, Badge, Button, Group, NativeSelect, Paper, ScrollArea, Stack, Table, Text, Tooltip } from '@mantine/core';
import { IconChevronLeft, IconChevronRight, IconCopy, IconSend } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { shifts as shiftApi } from '../../api';
import { field as fieldApi } from '../../api/field';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const WD = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const weekday = (d: string) => new Date(`${d}T00:00:00Z`).getUTCDay();
const sundayOf = (d: string) => addDays(d, -weekday(d));
const SWAP_LABEL: Record<string, string> = { awaiting_colleague: 'Waiting for colleague', pending: 'Waiting for approval', approved: 'Approved', rejected: 'Not approved', declined: 'Declined', cancelled: 'Cancelled' };

/**
 * Weekly roster: pick a shift (or off) per person per day. Blank cells follow the person's normal shift and weekly off.
 * Managers roster their own team, HR everyone. Publishing tells everyone on the roster.
 */
export default function RostersPage() {
  const [weekStart, setWeekStart] = useState(sundayOf(todayNpt()));
  const weekEnd = addDays(weekStart, 6);
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)), [weekStart]);
  const shifts = useQuery(() => shiftApi.list(), []);
  const roster = useQuery(() => shiftApi.roster(weekStart, weekEnd), [weekStart]);
  const swaps = useQuery(() => fieldApi.swaps({ scope: 'team', from: weekStart, to: addDays(weekStart, 34) }), [weekStart]);
  const [saving, setSaving] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<'copy' | 'publish' | null>(null);
  const shiftList = shifts.data?.shifts ?? [];
  const byId = new Map(shiftList.map((s) => [s.id, s]));
  const cells = new Map((roster.data?.roster ?? []).map((r) => [`${r.employee_id}|${r.local_date.slice(0, 10)}`, r.shift_id]));

  const setCell = async (employeeId: string, date: string, value: string) => {
    const key = `${employeeId}|${date}`;
    setSaving(key);
    try {
      if (value === '') await shiftApi.clearRoster(employeeId, date);
      else await shiftApi.saveRoster([{ employeeId, localDate: date, shiftId: value === 'off' ? null : value }]);
      await roster.reload();
    } catch (e) { toast.err(e); } finally { setSaving(null); }
  };

  const employees = roster.data?.employees ?? [];
  return (
    <Stack gap="lg">
      <SectionTitle hint="Blank cells follow each person's normal shift. Changes save straight away; publish to let everyone know."
        actions={<Group gap="xs" wrap="wrap">
          <Group gap={4}>
            <ActionIcon variant="default" aria-label="Previous week" onClick={() => setWeekStart(addDays(weekStart, -7))}><IconChevronLeft size={16} /></ActionIcon>
            <Text size="sm" fw={500} miw={170} ta="center">{fmtDateShort(weekStart)} to {fmtDateShort(weekEnd)}</Text>
            <ActionIcon variant="default" aria-label="Next week" onClick={() => setWeekStart(addDays(weekStart, 7))}><IconChevronRight size={16} /></ActionIcon>
          </Group>
          <Button size="xs" variant="default" leftSection={<IconCopy size={14} />} onClick={() => setConfirm('copy')}>Copy last week</Button>
          <Button size="xs" leftSection={<IconSend size={14} />} onClick={() => setConfirm('publish')}>Publish</Button>
        </Group>}>Roster</SectionTitle>

      {!roster.loading && employees.length === 0 ? <EmptyState title="No one to roster" hint="People who report to you appear here. HR sees everyone." /> : (
        <Paper withBorder>
          <ScrollArea>
            <Table miw={900} verticalSpacing={6} horizontalSpacing="sm" striped={false}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th style={{ position: 'sticky', left: 0, background: 'var(--mantine-color-body)', zIndex: 1 }}>Name</Table.Th>
                  {days.map((d) => <Table.Th key={d} style={{ background: d === todayNpt() ? 'var(--hj-hydro-soft)' : undefined }}><Text size="xs" c="dimmed">{WD[weekday(d)]}</Text><Text size="sm" fw={500}>{fmtDateShort(d)}</Text></Table.Th>)}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {employees.map((e) => {
                  const def = e.default_shift_id ? byId.get(e.default_shift_id) : undefined;
                  return (
                    <Table.Tr key={e.id}>
                      <Table.Td style={{ position: 'sticky', left: 0, background: 'var(--mantine-color-body)', zIndex: 1 }}>
                        <Text size="sm" fw={500}>{e.name_en}</Text><Text size="xs" c="dimmed">{e.emp_code}</Text>
                      </Table.Td>
                      {days.map((d) => {
                        const key = `${e.id}|${d}`;
                        const has = cells.has(key);
                        const v = has ? (cells.get(key) ?? 'off') : '';
                        const normal = e.weekly_off_days.includes(weekday(d)) ? 'Weekly off' : def ? `Normal (${def.code})` : 'Normal';
                        return (
                          <Table.Td key={d}>
                            <NativeSelect size="xs" value={v} disabled={saving === key} aria-label={`${e.name_en} on ${d}`}
                              styles={{ input: { fontWeight: has ? 600 : 400, color: v === 'off' ? 'var(--hj-muted)' : undefined } }}
                              onChange={(ev) => void setCell(e.id, d, ev.currentTarget.value)}
                              data={[{ value: '', label: normal }, { value: 'off', label: 'Off' }, ...shiftList.map((s) => ({ value: s.id, label: `${s.code} ${s.start.slice(0, 5)}–${s.end.slice(0, 5)}` }))]} />
                          </Table.Td>
                        );
                      })}
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </Paper>
      )}

      <div>
        <SectionTitle hint="Swaps your team asked for. Approve them in Approvals.">Shift swaps</SectionTitle>
        {(swaps.data?.swaps ?? []).length === 0 ? <EmptyState compact title="No shift swaps" hint="When two people agree to swap a day it appears here and in Approvals." /> : (
          <Paper withBorder>
            {(swaps.data?.swaps ?? []).map((s, i) => (
              <Group key={s.id} justify="space-between" px="md" py={10} wrap="wrap" style={i ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
                <div>
                  <Text size="sm">{fmtDateShort(s.local_date)}: {s.requester_name} ⇄ {s.colleague_name}</Text>
                  <Text size="xs" c="dimmed">{s.requester_shift_code ?? 'Off'} ⇄ {s.colleague_shift_code ?? 'Off'}{s.reason ? ` · “${s.reason}”` : ''}</Text>
                </div>
                <Tooltip label={s.decided_at ? `Decided ${fmtDateShort(s.decided_at.slice(0, 10))}` : 'Open'}><Badge variant="light" color={s.status === 'approved' ? 'teal' : s.status === 'pending' ? 'blue' : 'gray'}>{SWAP_LABEL[s.status]}</Badge></Tooltip>
              </Group>
            ))}
          </Paper>
        )}
      </div>

      <ConfirmDialog opened={confirm === 'copy'} title="Copy last week" color="brand" confirmLabel="Copy"
        message={`Copy the roster of ${fmtDateShort(addDays(weekStart, -7))} to ${fmtDateShort(addDays(weekStart, -1))} onto this week? Cells already set this week are overwritten.`}
        onClose={() => setConfirm(null)} onConfirm={async () => {
          try { const r = await shiftApi.copyWeek(weekStart); toast.ok(`${r.copied} ${r.copied === 1 ? 'cell' : 'cells'} copied.`); setConfirm(null); await roster.reload(); } catch (e) { toast.err(e); }
        }} />
      <ConfirmDialog opened={confirm === 'publish'} title="Publish roster" color="brand" confirmLabel="Publish"
        message={`Tell everyone on this roster about their shifts from ${fmtDateShort(weekStart)} to ${fmtDateShort(weekEnd)}?`}
        onClose={() => setConfirm(null)} onConfirm={async () => {
          try { const r = await shiftApi.publish(weekStart, weekEnd); toast.ok(`Roster published. ${r.notified} ${r.notified === 1 ? 'person was' : 'people were'} notified.`); setConfirm(null); } catch (e) { toast.err(e); }
        }} />
    </Stack>
  );
}
