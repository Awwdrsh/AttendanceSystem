import { ActionIcon, Badge, Button, Checkbox, Drawer, Group, Modal, Paper, SegmentedControl, Select, SimpleGrid, Stack, Text, Textarea, TextInput } from '@mantine/core';
import { IconMessages, IconPlus, IconTrash } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { performance as perfApi, type Meeting } from '../../api/performance';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { fmtDateTime, ICON, nptInstant, splitNpt } from './parts';

const CADENCE = [{ value: 'none', label: 'One-off' }, { value: 'weekly', label: 'Weekly' }, { value: 'biweekly', label: 'Every 2 weeks' }, { value: 'monthly', label: 'Monthly' }];

/** 1-on-1s between a person and their manager: shared agenda, action items, and private notes for each side. */
export default function OneOnOnesPage() {
  const q = useQuery(() => perfApi.meetings(), []);
  const [create, setCreate] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const [show, setShow] = useState<'upcoming' | 'past'>('upcoming');
  const meetings = (q.data?.meetings ?? []).filter((m) => (show === 'upcoming' ? m.status === 'scheduled' : m.status !== 'scheduled'));
  const people = [...(q.data?.manager ? [{ ...q.data.manager, rel: 'Your manager' }] : []), ...(q.data?.reports ?? []).map((r) => ({ ...r, rel: 'Reports to you' }))];

  return (
    <Stack gap="lg">
      <SectionTitle hint="Regular conversations with your manager or your team. Only the two of you see the meeting; private notes are yours alone."
        actions={<Group gap="xs">
          <SegmentedControl size="xs" value={show} onChange={(v) => setShow(v as typeof show)} data={[{ value: 'upcoming', label: 'Upcoming' }, { value: 'past', label: 'Past' }]} />
          <Button size="xs" leftSection={<IconPlus {...ICON} />} disabled={!people.length} onClick={() => setCreate(true)}>Schedule</Button>
        </Group>}>1-on-1s</SectionTitle>
      {!q.loading && meetings.length === 0 ? (
        <EmptyState icon={<IconMessages size={20} stroke={1.6} />} title={show === 'upcoming' ? 'No 1-on-1s scheduled' : 'No past 1-on-1s'}
          hint={people.length ? 'Schedule a recurring 1-on-1 to keep goals, feedback and growth on track.' : 'You need a manager or direct reports in the system to schedule 1-on-1s.'}
          {...(people.length && show === 'upcoming' ? { action: { label: 'Schedule a 1-on-1', onClick: () => setCreate(true) } } : {})} />
      ) : (
        <Stack gap="sm">
          {meetings.map((m: Meeting) => (
            <Paper key={m.id} withBorder radius="lg" p="md" style={{ cursor: 'pointer' }} onClick={() => setOpen(m.id)}>
              <Group justify="space-between" wrap="wrap" gap="xs">
                <div>
                  <Text fw={600}>{m.side === 'manager' ? m.employee_name : m.manager_name}</Text>
                  <Text size="xs" c="dimmed">{fmtDateTime(m.scheduled_at)}{m.cadence !== 'none' ? ` · ${CADENCE.find((c) => c.value === m.cadence)?.label}` : ''}</Text>
                </div>
                <Group gap="xs">
                  {m.agenda_items ? <Badge variant="light" color="gray">{m.agenda_items} agenda</Badge> : null}
                  {m.open_actions ? <Badge variant="light" color="orange">{m.open_actions} open actions</Badge> : null}
                  {m.status !== 'scheduled' ? <Badge variant="light" color={m.status === 'done' ? 'brand' : 'gray'}>{m.status}</Badge> : null}
                </Group>
              </Group>
            </Paper>
          ))}
        </Stack>
      )}
      <ScheduleModal opened={create} onClose={() => setCreate(false)} people={people} onSaved={(id) => { void q.reload(); setOpen(id); }} />
      <MeetingDrawer id={open} onClose={() => setOpen(null)} onChanged={() => void q.reload()} onOpen={setOpen} />
    </Stack>
  );
}

function ScheduleModal({ opened, onClose, people, onSaved }: { opened: boolean; onClose: () => void; people: Array<{ id: string; name_en: string; rel: string }>; onSaved: (id: string) => void }) {
  const [withId, setWithId] = useState<string | null>(null);
  const [date, setDate] = useState<string | null>(addDays(todayNpt(), 1)); const [time, setTime] = useState('11:00');
  const [cadence, setCadence] = useState<string | null>('biweekly'); const [agenda, setAgenda] = useState('');
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (!withId || !date) return; setBusy(true);
    try {
      const r = await perfApi.createMeeting({ withEmployeeId: withId, scheduledAt: nptInstant(date, time), cadence, agenda: agenda.split('\n').map((s) => s.trim()).filter(Boolean) });
      toast.ok('1-on-1 scheduled.'); setAgenda(''); onClose(); onSaved(r.id);
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={opened} onClose={onClose} title="Schedule a 1-on-1">
      <Stack gap="sm">
        <Select label="With" required value={withId} onChange={setWithId} data={people.map((p) => ({ value: p.id, label: `${p.name_en} (${p.rel})` }))} />
        <SimpleGrid cols={2}>
          <DateField label="Date" value={date} onChange={setDate} />
          <TextInput label="Time" type="time" value={time} onChange={(e) => setTime(e.currentTarget.value)} />
        </SimpleGrid>
        <Select label="Repeats" value={cadence} onChange={setCadence} data={CADENCE} allowDeselect={false} />
        <Textarea label="Agenda" description="One topic per line" autosize minRows={3} value={agenda} onChange={(e) => setAgenda(e.currentTarget.value)} />
        <Group justify="flex-end"><Button onClick={save} loading={busy} disabled={!withId || !date || !time}>Schedule</Button></Group>
      </Stack>
    </Modal>
  );
}

function MeetingDrawer({ id, onClose, onChanged, onOpen }: { id: string | null; onClose: () => void; onChanged: () => void; onOpen: (id: string) => void }) {
  const q = useQuery(() => (id ? perfApi.meeting(id) : Promise.resolve(null)), [id]);
  const d = q.data;
  const [shared, setShared] = useState(''); const [priv, setPriv] = useState('');
  const [item, setItem] = useState(''); const [kind, setKind] = useState<'agenda' | 'action'>('agenda'); const [owner, setOwner] = useState<string | null>(null);
  const [date, setDate] = useState<string | null>(null); const [time, setTime] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!d) return;
    setShared(d.meeting.shared_notes ?? ''); setPriv(d.meeting.private_notes ?? '');
    const s = splitNpt(d.meeting.scheduled_at); setDate(s.date); setTime(s.time);
  }, [d]);
  const reload = async () => { await q.reload(); onChanged(); };
  const run = async (fn: () => Promise<unknown>, msg?: string) => { setBusy(true); try { await fn(); if (msg) toast.ok(msg); await reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } };
  if (!d) return <Drawer opened={!!id} onClose={onClose} position="right" size="lg" title="1-on-1"><Text size="sm" c="dimmed">{q.loading ? 'Loading…' : q.error ?? ''}</Text></Drawer>;
  const m = d.meeting;
  const other = m.side === 'manager' ? m.employee_name : m.manager_name;
  const agenda = d.items.filter((i) => i.kind === 'agenda'); const actions = d.items.filter((i) => i.kind === 'action');
  const ownerOptions = [{ value: m.manager_employee_id, label: m.manager_name }, { value: m.employee_id, label: m.employee_name }];
  return (
    <Drawer opened={!!id} onClose={onClose} position="right" size="lg" title={`1-on-1 with ${other}`}>
      <Stack gap="md">
        <Group justify="space-between" wrap="wrap">
          <Text size="sm" c="dimmed">{fmtDateTime(m.scheduled_at)}</Text>
          {m.status === 'scheduled' ? (
            <Group gap="xs">
              <Button size="xs" variant="subtle" color="gray" onClick={() => run(() => perfApi.updateMeeting(m.id, { status: 'cancelled' }), 'Cancelled.')}>Cancel</Button>
              <Button size="xs" loading={busy} onClick={() => run(async () => {
                const r = await perfApi.updateMeeting(m.id, { status: 'done', sharedNotes: shared || null, privateNotes: priv || null });
                if (r.nextId) { toast.ok('Done. The next one is scheduled.'); onOpen(r.nextId); }
              }, m.cadence === 'none' ? 'Marked done.' : undefined)}>Mark done</Button>
            </Group>
          ) : <Badge variant="light" color={m.status === 'done' ? 'brand' : 'gray'}>{m.status}</Badge>}
        </Group>
        {m.status === 'scheduled' ? (
          <Group gap="xs" align="flex-end" wrap="wrap">
            <DateField label="Reschedule" value={date} onChange={setDate} w={190} />
            <TextInput type="time" value={time} onChange={(e) => setTime(e.currentTarget.value)} w={120} />
            <Button size="sm" variant="default" disabled={!date || !time} onClick={() => run(() => perfApi.updateMeeting(m.id, { scheduledAt: nptInstant(date!, time) }), 'Rescheduled.')}>Save</Button>
          </Group>
        ) : null}
        {d.openActions.length ? (
          <Paper withBorder radius="md" p="sm">
            <Text size="sm" fw={600} mb={4}>Open actions from earlier 1-on-1s</Text>
            {d.openActions.map((a) => <Text key={a.id} size="sm">• {a.text}{a.owner_name ? ` (${a.owner_name})` : ''}</Text>)}
          </Paper>
        ) : null}
        {(['agenda', 'action'] as const).map((k) => (
          <Stack key={k} gap={4}>
            <Text fw={600} size="sm">{k === 'agenda' ? 'Agenda' : 'Action items'}</Text>
            {(k === 'agenda' ? agenda : actions).length === 0 ? <Text size="sm" c="dimmed">{k === 'agenda' ? 'Add topics either of you wants to cover.' : 'Nothing agreed yet.'}</Text> : null}
            {(k === 'agenda' ? agenda : actions).map((i) => (
              <Group key={i.id} justify="space-between" wrap="nowrap" gap="xs">
                <Checkbox label={`${i.text}${i.owner_name ? ` · ${i.owner_name}` : ''}`} checked={i.done} onChange={(e) => { const done = e.currentTarget.checked; void run(() => perfApi.updateMeetingItem(i.id, { done })); }} />
                <ActionIcon variant="subtle" color="gray" aria-label="Remove" onClick={() => run(() => perfApi.deleteMeetingItem(i.id))}><IconTrash size={14} /></ActionIcon>
              </Group>
            ))}
          </Stack>
        ))}
        <Group gap="xs" align="flex-end" wrap="wrap">
          <SegmentedControl size="xs" value={kind} onChange={(v) => setKind(v as typeof kind)} data={[{ value: 'agenda', label: 'Topic' }, { value: 'action', label: 'Action' }]} />
          <TextInput style={{ flex: 1, minWidth: 180 }} placeholder={kind === 'agenda' ? 'Add a topic' : 'Add an action item'} value={item} onChange={(e) => setItem(e.currentTarget.value)} />
          {kind === 'action' ? <Select w={170} placeholder="Owner" clearable value={owner} onChange={setOwner} data={ownerOptions} /> : null}
          <Button variant="default" disabled={!item.trim()} onClick={() => run(async () => { await perfApi.addMeetingItem(m.id, { kind, text: item, ownerEmployeeId: kind === 'action' ? owner : null }); setItem(''); })}>Add</Button>
        </Group>
        <Textarea label="Shared notes" description={`Visible to you and ${other}`} autosize minRows={3} value={shared} onChange={(e) => setShared(e.currentTarget.value)} />
        <Textarea label="Private notes" description="Only you can see these" autosize minRows={2} value={priv} onChange={(e) => setPriv(e.currentTarget.value)} />
        <Group justify="flex-end"><Button loading={busy} onClick={() => run(() => perfApi.updateMeeting(m.id, { sharedNotes: shared || null, privateNotes: priv || null }), 'Notes saved.')}>Save notes</Button></Group>
      </Stack>
    </Drawer>
  );
}
