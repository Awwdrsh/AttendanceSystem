import { Badge, Button, Group, Modal, Paper, SegmentedControl, Select, Stack, Switch, Text, Textarea } from '@mantine/core';
import { IconMessageCircle, IconSend } from '@tabler/icons-react';
import { useState } from 'react';
import { useAuth } from '../../auth/AuthProvider';
import { performance as perfApi, type Feedback } from '../../api/performance';
import { EmptyState } from '../../components/EmptyState';
import { SectionTitle } from '../../components/PageHeader';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ColleaguePicker, fmtDateTime, ICON } from './parts';

type Box = 'received' | 'given' | 'requests';

/** Continuous feedback: give it any time, ask colleagues for it, answer what you were asked. */
export default function FeedbackPage() {
  const { user } = useAuth();
  const [box, setBox] = useState<Box>('received');
  const q = useQuery(() => perfApi.feedback(box), [box]);
  const [give, setGive] = useState(false); const [ask, setAsk] = useState(false);
  const [answer, setAnswer] = useState<Feedback | null>(null);
  const rows = q.data?.feedback ?? [];
  const me = user?.employeeId;

  return (
    <Stack gap="lg">
      <SectionTitle hint="Feedback goes to the person and their manager. Mark it private to share it with their manager only."
        actions={<Group gap="xs" wrap="wrap">
          <SegmentedControl size="xs" value={box} onChange={(v) => setBox(v as Box)} data={[{ value: 'received', label: 'Received' }, { value: 'given', label: 'Given' }, { value: 'requests', label: 'Requests' }]} />
          <Button size="xs" variant="default" onClick={() => setAsk(true)}>Ask for feedback</Button>
          <Button size="xs" leftSection={<IconSend {...ICON} />} onClick={() => setGive(true)}>Give feedback</Button>
        </Group>}>Feedback</SectionTitle>
      {!q.loading && rows.length === 0 ? (
        <EmptyState icon={<IconMessageCircle size={20} stroke={1.6} />} title={box === 'received' ? 'No feedback yet' : box === 'given' ? 'You have not given feedback yet' : 'No open requests'}
          hint="Ask two or three colleagues you worked with recently what went well and what to improve." action={{ label: 'Ask for feedback', onClick: () => setAsk(true) }} />
      ) : (
        <Stack gap="sm">
          {rows.map((f) => {
            const toAnswer = box === 'requests' && f.status === 'requested' && f.giver_employee_id === me;
            return (
              <Paper key={f.id} withBorder radius="lg" p="md">
                <Group justify="space-between" wrap="nowrap" align="flex-start">
                  <div style={{ minWidth: 0 }}>
                    <Text size="sm" fw={600}>
                      {box === 'received' ? `From ${f.giver_name}` : box === 'given' ? `To ${f.subject_name}` : toAnswer ? `${f.requester_name ?? f.subject_name} asked you${f.subject_employee_id !== f.requester_employee_id ? ` about ${f.subject_name}` : ''}` : `Asked ${f.giver_name}`}
                    </Text>
                    <Text size="xs" c="dimmed">{fmtDateTime(f.given_at ?? f.created_at)}</Text>
                  </div>
                  <Group gap={4}>
                    {f.private_to_manager ? <Badge size="sm" variant="light" color="gray">Manager only</Badge> : null}
                    {box === 'requests' ? <Badge size="sm" variant="light" color={f.status === 'given' ? 'teal' : f.status === 'declined' ? 'gray' : 'yellow'}>{f.status === 'requested' ? 'Waiting' : f.status}</Badge> : null}
                  </Group>
                </Group>
                {f.prompt ? <Text size="sm" c="dimmed" mt={6}>“{f.prompt}”</Text> : null}
                {f.body ? <Text size="sm" mt={6} style={{ whiteSpace: 'pre-wrap' }}>{f.body}</Text> : f.status === 'given' && box === 'requests' ? <Text size="xs" c="dimmed" mt={6}>Shared with your manager only.</Text> : null}
                {toAnswer ? (
                  <Group justify="flex-end" mt="xs" gap="xs">
                    <Button size="xs" variant="subtle" color="gray" onClick={async () => { try { await perfApi.declineFeedback(f.id); toast.ok('Declined.'); await q.reload(); } catch (e) { toast.err(e); } }}>Decline</Button>
                    <Button size="xs" onClick={() => setAnswer(f)}>Answer</Button>
                  </Group>
                ) : null}
              </Paper>
            );
          })}
        </Stack>
      )}
      <GiveModal opened={give || !!answer} request={answer} onClose={() => { setGive(false); setAnswer(null); }} onDone={() => void q.reload()} me={me ?? null} />
      <AskModal opened={ask} onClose={() => setAsk(false)} onDone={() => { setBox('requests'); void q.reload(); }} me={me ?? null} />
    </Stack>
  );
}

function GiveModal({ opened, request, onClose, onDone, me }: { opened: boolean; request: Feedback | null; onClose: () => void; onDone: () => void; me: string | null }) {
  const colleagues = useQuery(() => perfApi.colleagues(), [opened], { enabled: opened && !request });
  const [to, setTo] = useState<string | null>(null); const [body, setBody] = useState(''); const [priv, setPriv] = useState(false);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true);
    try {
      if (request) await perfApi.answerFeedback(request.id, { body, privateToManager: priv });
      else await perfApi.giveFeedback({ toEmployeeId: to, body, privateToManager: priv });
      toast.ok('Feedback sent.'); setBody(''); setTo(null); setPriv(false); onDone(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={request ? `Feedback about ${request.subject_name}` : 'Give feedback'}>
      <Stack gap="sm">
        {request?.prompt ? <Text size="sm" c="dimmed">“{request.prompt}”</Text> : null}
        {!request ? <Select label="To" searchable value={to} onChange={setTo} data={(colleagues.data?.colleagues ?? []).filter((c) => c.id !== me).map((c) => ({ value: c.id, label: `${c.name_en}${c.designation ? `, ${c.designation}` : ''}` }))} /> : null}
        <Textarea label="Feedback" placeholder="Be specific: what happened, the effect, and what to keep or change" autosize minRows={4} value={body} onChange={(e) => setBody(e.currentTarget.value)} />
        <Switch label="Share with their manager only" checked={priv} onChange={(e) => setPriv(e.currentTarget.checked)} />
        <Group justify="flex-end"><Button onClick={send} loading={busy} disabled={!body.trim() || (!request && !to)}>Send</Button></Group>
      </Stack>
    </Modal>
  );
}

function AskModal({ opened, onClose, onDone, me }: { opened: boolean; onClose: () => void; onDone: () => void; me: string | null }) {
  const [from, setFrom] = useState<string[]>([]); const [prompt, setPrompt] = useState(''); const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true);
    try { await perfApi.requestFeedback({ fromEmployeeIds: from, ...(prompt.trim() ? { prompt } : {}) }); toast.ok('Request sent.'); setFrom([]); setPrompt(''); onDone(); onClose(); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={opened} onClose={onClose} title="Ask for feedback">
      <Stack gap="sm">
        <ColleaguePicker label="Ask" value={from} onChange={setFrom} exclude={me ? [me] : []} />
        <Textarea label="Question (optional)" placeholder="e.g. How did I handle the client handover?" autosize minRows={2} value={prompt} onChange={(e) => setPrompt(e.currentTarget.value)} />
        <Group justify="flex-end"><Button onClick={send} loading={busy} disabled={!from.length}>Send request</Button></Group>
      </Stack>
    </Modal>
  );
}
