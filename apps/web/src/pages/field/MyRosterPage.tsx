import { Badge, Button, Group, Modal, Paper, SegmentedControl, Select, Stack, Text, Textarea } from '@mantine/core';
import { IconArrowsExchange } from '@tabler/icons-react';
import { useState } from 'react';
import { useAuth } from '../../auth/AuthProvider';
import { field as fieldApi, type RosterDay, type SwapRow } from '../../api/field';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateLong, fmtDateShort, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const SWAP_STATUS: Record<SwapRow['status'], { label: string; color: string }> = {
  awaiting_colleague: { label: 'Waiting for colleague', color: 'yellow' }, pending: { label: 'Waiting for manager', color: 'blue' },
  approved: { label: 'Approved', color: 'teal' }, rejected: { label: 'Not approved', color: 'red' }, declined: { label: 'Declined', color: 'gray' }, cancelled: { label: 'Cancelled', color: 'gray' },
};
const shiftText = (code: string | null, name: string | null) => (code ? name || code : 'Off');

function dayLine(d: RosterDay): { text: string; muted: boolean } {
  if (d.shift) return { text: `${d.shift.name_en || d.shift.code}, ${d.shift.start} to ${d.shift.end}`, muted: false };
  if (d.source === 'weekly_off') return { text: 'Weekly off', muted: true };
  if (d.source === 'roster_off') return { text: 'Off (rostered)', muted: true };
  return { text: 'No shift', muted: true };
}

/** The employee's roster for the coming weeks, and shift swaps with colleagues. */
export default function MyRosterPage() {
  const { user } = useAuth();
  const [weeks, setWeeks] = useState('2');
  const from = todayNpt(); const to = addDays(from, Number(weeks) * 7 - 1);
  const roster = useQuery(() => fieldApi.myRoster(from, to), [from, to]);
  const swaps = useQuery(() => fieldApi.swaps(), []);
  const [open, setOpen] = useState(false);
  const [date, setDate] = useState<string | null>(null);
  const [colleague, setColleague] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const options = useQuery(() => fieldApi.swapColleagues(date!), [date], { enabled: !!date && open });
  const me = user?.employeeId;
  const list = swaps.data?.swaps ?? [];
  const incoming = list.filter((s) => s.colleague_id === me && s.status === 'awaiting_colleague');
  const others = list.filter((s) => !incoming.includes(s));

  const act = async (fn: () => Promise<unknown>, msg: string) => { try { await fn(); toast.ok(msg); await Promise.all([swaps.reload(), roster.reload()]); } catch (e) { toast.err(e); } };
  const send = async () => {
    if (!date || !colleague) return;
    setBusy(true);
    try { await fieldApi.requestSwap({ colleagueId: colleague, localDate: date, ...(reason.trim() ? { reason: reason.trim() } : {}) }); toast.ok('Swap request sent to your colleague.'); setOpen(false); setColleague(null); setReason(''); await swaps.reload(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };

  return (
    <Stack gap="lg">
      <SectionTitle hint="Your shifts for the coming days. Swap a shift with a colleague; your manager approves it."
        actions={<Group gap="xs">
          <SegmentedControl size="xs" value={weeks} onChange={setWeeks} data={[{ value: '1', label: '1 week' }, { value: '2', label: '2 weeks' }, { value: '4', label: '4 weeks' }]} />
          <Button size="xs" leftSection={<IconArrowsExchange size={14} />} onClick={() => { setDate(addDays(todayNpt(), 1)); setOpen(true); }}>Swap a shift</Button>
        </Group>}>My roster</SectionTitle>

      {incoming.length ? (
        <Paper withBorder p="md">
          <Text fw={600} mb="xs">Asked of you</Text>
          <Stack gap="sm">
            {incoming.map((s) => (
              <Group key={s.id} justify="space-between" wrap="wrap" gap="sm">
                <div>
                  <Text size="sm"><b>{s.requester_name}</b> wants to swap on {fmtDateLong(s.local_date)}</Text>
                  <Text size="xs" c="dimmed">You would work {shiftText(s.requester_shift_code, s.requester_shift_name)}; they would work {shiftText(s.colleague_shift_code, s.colleague_shift_name)}.{s.reason ? ` “${s.reason}”` : ''}</Text>
                </div>
                <Group gap="xs">
                  <Button size="xs" variant="default" onClick={() => act(() => fieldApi.declineSwap(s.id), 'Declined.')}>Decline</Button>
                  <Button size="xs" onClick={() => act(() => fieldApi.acceptSwap(s.id), 'Accepted. It now goes to the manager for approval.')}>Accept</Button>
                </Group>
              </Group>
            ))}
          </Stack>
        </Paper>
      ) : null}

      <Paper withBorder>
        {(roster.data?.days ?? []).map((d, i) => {
          const l = dayLine(d);
          return (
            <Group key={d.date} justify="space-between" px="md" py={10} wrap="nowrap" style={i ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
              <Text size="sm" fw={d.date === todayNpt() ? 600 : 400}>{fmtDateLong(d.date)}{d.date === todayNpt() ? ' (today)' : ''}</Text>
              <Group gap={6} wrap="nowrap">
                {d.holidays.length ? <Badge variant="light" color="grape">{d.holidays[0]}</Badge> : null}
                {d.source === 'roster' ? <Badge size="xs" variant="outline" color="gray">Rostered</Badge> : null}
                <Text size="sm" {...(l.muted ? { c: 'dimmed' } : {})} className="num">{l.text}</Text>
              </Group>
            </Group>
          );
        })}
      </Paper>

      <div>
        <SectionTitle>Swap requests</SectionTitle>
        {others.length === 0 ? <EmptyState compact title="No swap requests" hint="Use Swap a shift to ask a colleague to trade a day with you." /> : (
          <Paper withBorder>
            {others.map((s, i) => {
              const mine = s.requester_id === me;
              return (
                <Group key={s.id} justify="space-between" px="md" py={10} wrap="wrap" style={i ? { borderTop: '1px solid var(--hj-line-soft)' } : undefined}>
                  <div>
                    <Text size="sm">{fmtDateShort(s.local_date)}: {mine ? `with ${s.colleague_name}` : `${s.requester_name} with you`}</Text>
                    <Text size="xs" c="dimmed">{shiftText(s.requester_shift_code, s.requester_shift_name)} ⇄ {shiftText(s.colleague_shift_code, s.colleague_shift_name)}{s.colleague_note ? ` · “${s.colleague_note}”` : ''}</Text>
                  </div>
                  <Group gap="xs">
                    <Badge variant="light" color={SWAP_STATUS[s.status].color}>{SWAP_STATUS[s.status].label}</Badge>
                    {mine && ['awaiting_colleague', 'pending'].includes(s.status) ? <Button size="compact-xs" variant="subtle" color="gray" onClick={() => act(() => fieldApi.cancelSwap(s.id), 'Request cancelled.')}>Cancel</Button> : null}
                  </Group>
                </Group>
              );
            })}
          </Paper>
        )}
      </div>

      <Modal opened={open} onClose={() => setOpen(false)} title="Swap a shift" centered>
        <Stack>
          <DateField label="Day" value={date} onChange={(d) => { setDate(d); setColleague(null); }} minDate={todayNpt()} maxDate={addDays(todayNpt(), 90)} required />
          {options.data ? <Text size="sm" c="dimmed">You are on {options.data.mine.shift ? `${options.data.mine.shift.name_en || options.data.mine.shift.code}, ${options.data.mine.shift.start} to ${options.data.mine.shift.end}` : 'an off day'}.</Text> : null}
          <Select label="Swap with" placeholder={options.loading ? 'Loading colleagues…' : 'Choose a colleague'} searchable value={colleague} onChange={setColleague}
            data={(options.data?.colleagues ?? []).filter((c) => !c.sameAsMine).map((c) => ({ value: c.id, label: `${c.name_en} (${c.shift ? `${c.shift.name_en || c.shift.code} ${c.shift.start}–${c.shift.end}` : 'off'})` }))}
            nothingFoundMessage="No colleague on a different shift that day" />
          <Textarea label="Reason" placeholder="Optional" value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} maxLength={500} />
          <Group justify="flex-end"><Button variant="default" onClick={() => setOpen(false)}>Cancel</Button><Button loading={busy} disabled={!date || !colleague} onClick={send}>Send request</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
