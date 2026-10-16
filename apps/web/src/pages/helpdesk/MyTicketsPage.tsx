import { Alert, Anchor, Badge, Button, Divider, Drawer, Group, Modal, Paper, SegmentedControl, Select, Stack, Text, Textarea, TextInput, UnstyledButton } from '@mantine/core';
import { IconDoorExit, IconLock, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { UploadedFile } from '../../api/files';
import { helpdesk, type Priority, type Ticket } from '../../api/helpdesk';
import { lifecycle, RESIGN_REASONS } from '../../api/lifecycle';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { FileUpload } from '../../components/FileUpload';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { addDays, fmtAd, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { exitStatusBadge, InterviewForm } from '../lifecycle/ExitsPage';
import { TicketBadges, TicketThread } from './TicketThread';

const ICON = { size: 16, stroke: 1.6 } as const;

function NewTicket({ opened, onClose, onCreated }: { opened: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const cats = useQuery(() => helpdesk.categories(), []);
  const [categoryId, setCategoryId] = useState<string | null>(null);
  const [subject, setSubject] = useState(''); const [body, setBody] = useState(''); const [priority, setPriority] = useState<Priority>('normal');
  const [files, setFiles] = useState<UploadedFile[]>([]); const [busy, setBusy] = useState(false);
  const cat = cats.data?.categories.find((c) => c.id === categoryId);
  const submit = async () => {
    if (!categoryId) return;
    setBusy(true);
    try {
      const r = await helpdesk.create({ categoryId, subject: subject.trim(), body: body.trim(), priority, fileIds: files.map((f) => f.id) });
      toast.ok(`Ticket #${r.number} raised. We will get back to you.`);
      setSubject(''); setBody(''); setFiles([]); setCategoryId(null); setPriority('normal');
      onCreated(r.id);
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={opened} onClose={onClose} title="Ask HR" centered size="lg">
      <Stack>
        <Select label="What is it about?" placeholder="Choose a topic" value={categoryId} onChange={setCategoryId} allowDeselect={false}
          data={(cats.data?.categories ?? []).filter((c) => c.active).map((c) => ({ value: c.id, label: c.name }))} />
        {cat?.description ? <Text size="xs" c="dimmed" mt={-8}>{cat.description}</Text> : null}
        {cat?.confidential ? <Alert variant="light" color="grape" icon={<IconLock {...ICON} />}>Only HR can see this ticket. Your manager is never shown it.</Alert> : null}
        <TextInput label="Subject" value={subject} onChange={(e) => setSubject(e.currentTarget.value)} maxLength={160} required />
        <Textarea label="Details" value={body} onChange={(e) => setBody(e.currentTarget.value)} autosize minRows={4} maxLength={5000} required />
        <Group align="flex-end" justify="space-between" wrap="wrap">
          <Stack gap={4}>
            <Text size="sm" fw={500}>How urgent?</Text>
            <SegmentedControl value={priority} onChange={(v) => setPriority(v as Priority)} data={[{ value: 'low', label: 'Low' }, { value: 'normal', label: 'Normal' }, { value: 'high', label: 'High' }, { value: 'urgent', label: 'Urgent' }]} />
          </Stack>
          <Group gap="xs">
            {files.map((f) => <Badge key={f.id} variant="light" color="gray">{f.filename}</Badge>)}
            {files.length < 5 ? <FileUpload purpose="helpdesk_attachment" value={null} label="Attach" onChange={(f) => f && setFiles([...files, f])} /> : null}
          </Group>
        </Group>
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button><Button loading={busy} disabled={!categoryId || subject.trim().length < 3 || !body.trim()} onClick={submit}>Send to HR</Button></Group>
      </Stack>
    </Modal>
  );
}

/** Resign, follow the notice and clearance, fill in the exit interview, and see the company assets you hold. */
function Leaving() {
  const q = useQuery(() => lifecycle.myExits(), []);
  const assets = useQuery(() => lifecycle.myAssets(), []);
  const [form, setForm] = useState<{ lwd: string | null; reasonCategory: string | null; reason: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const policy = q.data?.policy;
  const current = q.data?.exits.find((x) => ['pending', 'accepted'].includes(x.status));
  const today = todayNpt();
  const shortfall = form?.lwd && policy ? Math.max(0, policy.noticeDays - Math.round((Date.parse(form.lwd) - Date.parse(today)) / 86_400_000)) : 0;
  const resign = async () => {
    if (!form?.lwd) return;
    setBusy(true);
    try { await lifecycle.resign({ proposedLastWorkingDay: form.lwd, reasonCategory: form.reasonCategory, reason: form.reason.trim() || null }); toast.ok('Your resignation was sent to your manager.'); setForm(null); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const withdraw = async () => { if (!current) return; try { await lifecycle.withdraw(current.id); toast.ok('Resignation withdrawn.'); await q.reload(); } catch (e) { toast.err(e); } };
  return (
    <Stack gap="md">
      {(assets.data?.assets ?? []).length ? (
        <div>
          <SectionTitle hint="Return these to HR before your last day.">Company assets with you</SectionTitle>
          <Paper withBorder p="sm"><Stack gap={6}>{assets.data!.assets.map((a) => <Group key={a.id} justify="space-between"><Text size="sm">{a.name} <Text span size="xs" c="dimmed" ff="monospace">{a.tag}</Text></Text><Text size="xs" c="dimmed">since {fmtAd(a.assigned_on)}</Text></Group>)}</Stack></Paper>
        </div>
      ) : null}
      <div>
        <SectionTitle hint="Resignations go to your manager and then HR.">Leaving the company</SectionTitle>
        {current ? (
          <Paper withBorder p="md">
            <Stack gap="sm">
              <Group justify="space-between">{exitStatusBadge(current.status)}{current.status === 'pending' ? <Button size="xs" variant="subtle" color="gray" onClick={withdraw}>Withdraw resignation</Button> : null}</Group>
              <Text size="sm">Last working day: <b>{fmtAd(current.accepted_lwd ?? current.proposed_lwd)}</b></Text>
              {current.status === 'accepted' && current.tasks_total ? <Text size="sm" c="dimmed">Clearance: {current.tasks_done} of {current.tasks_total} done. Your own tasks are on Home.</Text> : null}
              {current.status === 'accepted' && policy?.selfExitInterview && !current.interview_done ? (
                <>
                  <Divider label="Exit interview (only HR reads this)" labelPosition="left" />
                  <InterviewForm exitId={current.id} onDone={() => { void q.reload(); }} />
                </>
              ) : null}
              {current.interview_done ? <Text size="sm" c="dimmed">Thank you for completing the exit interview.</Text> : null}
            </Stack>
          </Paper>
        ) : form ? (
          <Paper withBorder p="md">
            <Stack>
              <DateField label="Proposed last working day" value={form.lwd} onChange={(v) => setForm({ ...form, lwd: v })} minDate={today} required />
              {form.lwd && shortfall ? <Text size="sm" c="orange">That is {shortfall} {shortfall === 1 ? 'day' : 'days'} short of the {policy?.noticeDays}-day notice. Your manager and HR will see this.</Text> : null}
              <Select label="Main reason (optional)" clearable value={form.reasonCategory} onChange={(v) => setForm({ ...form, reasonCategory: v })} data={RESIGN_REASONS} />
              <Textarea label="Anything you want to add (optional)" value={form.reason} onChange={(e) => setForm({ ...form, reason: e.currentTarget.value })} autosize minRows={2} />
              <Group justify="flex-end"><Button variant="default" onClick={() => setForm(null)}>Cancel</Button><Button color="red" loading={busy} disabled={!form.lwd} onClick={resign}>Submit resignation</Button></Group>
            </Stack>
          </Paper>
        ) : (
          <Button variant="default" leftSection={<IconDoorExit {...ICON} />} onClick={() => setForm({ lwd: policy ? addDays(today, policy.noticeDays) : null, reasonCategory: null, reason: '' })}>Resign</Button>
        )}
      </div>
    </Stack>
  );
}

export default function MyTicketsPage() {
  const { user } = useAuth();
  const q = useQuery(() => helpdesk.mine(), []);
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const tickets = q.data?.tickets ?? [];
  const row = (t: Ticket) => (
    <UnstyledButton key={t.id} onClick={() => setOpenId(t.id)} p="sm" style={{ borderRadius: 8, display: 'block', width: '100%' }} className="hj-row-hover">
      <Group justify="space-between" wrap="nowrap" gap="sm">
        <div style={{ minWidth: 0 }}>
          <Text size="sm" fw={500} truncate>{t.subject}</Text>
          <Text size="xs" c="dimmed">#{t.number}, {t.category_name}, updated {relativeTime(t.updated_at)}</Text>
        </div>
        <TicketBadges status={t.status} priority={t.priority} />
      </Group>
    </UnstyledButton>
  );
  return (
    <Stack gap="lg">
      <PageHeader title="Help desk" subtitle="Ask HR about pay, leave, documents or anything else, and follow the answer here."
        actions={<Button leftSection={<IconPlus {...ICON} />} onClick={() => setCreating(true)}>Ask HR</Button>} />
      <Alert variant="light" color="grape" icon={<IconLock {...ICON} />}>
        A formal complaint about how you are treated at work? <Anchor component={Link} to="/me/help/grievances" size="sm">Raise a grievance</Anchor>: a committee hears it, never your manager.
      </Alert>
      {q.error ? <EmptyState title="Could not load your tickets" hint={q.error} /> : null}
      {!q.loading && !q.error && !tickets.length ? <EmptyState title="No questions yet" hint="Raise a ticket and HR will answer here. You get a notification when they reply." action={{ label: 'Ask HR', onClick: () => setCreating(true) }} /> : null}
      {tickets.length ? <Paper withBorder p={4}><Stack gap={0}>{tickets.map(row)}</Stack></Paper> : null}
      {user?.employeeId ? <Leaving /> : null}
      <NewTicket opened={creating} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); void q.reload(); setOpenId(id); }} />
      <Drawer opened={!!openId} onClose={() => setOpenId(null)} position="right" size="lg" title={<Text fw={600}>Ticket</Text>}>
        {openId ? <TicketThread ticketId={openId} onChanged={() => { void q.reload(); }} /> : null}
      </Drawer>
    </Stack>
  );
}
