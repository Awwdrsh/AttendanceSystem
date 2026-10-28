import { Anchor, Badge, Button, Group, Paper, Rating, Select, Stack, Switch, Text, Textarea } from '@mantine/core';
import { IconLock, IconPaperclip } from '@tabler/icons-react';
import { useState } from 'react';
import { helpdesk, PRIORITY_COLOR, PRIORITY_LABEL, STATUS_COLOR, STATUS_LABEL } from '../../api/helpdesk';
import { FileUpload } from '../../components/FileUpload';
import { fmtDateShort, fmtTime, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 14, stroke: 1.6 };

export function TicketBadges({ status, priority, confidential, overdue }) {
  return (
    <Group gap={6} wrap="wrap">
      <Badge variant="light" color={STATUS_COLOR[status]}>
        {STATUS_LABEL[status]}
      </Badge>
      <Badge variant="outline" color={PRIORITY_COLOR[priority]}>
        {PRIORITY_LABEL[priority]}
      </Badge>
      {confidential ? (
        <Badge variant="light" color="grape" leftSection={<IconLock size={11} />}>
          Confidential
        </Badge>
      ) : null}
      {overdue ? (
        <Badge variant="filled" color="red">
          Overdue
        </Badge>
      ) : null}
    </Group>
  );
}

/** A ticket's conversation: messages, attachments, the reply box, and for the team, status, owner and internal notes. */
export function TicketThread({ ticketId, agents, onChanged }) {
  const q = useQuery(() => helpdesk.ticket(ticketId), [ticketId]);
  const [body, setBody] = useState('');
  const [internal, setInternal] = useState(false);
  const [files, setFiles] = useState([]);
  const [pending, setPending] = useState(null);
  const [busy, setBusy] = useState(false);
  const [rating, setRating] = useState(0);
  const [ratingNote, setRatingNote] = useState('');
  const d = q.data;
  const changed = async () => {
    await q.reload();
    onChanged?.();
  };
  const act = async (fn, ok) => {
    setBusy(true);
    try {
      await fn();
      toast.ok(ok);
      await changed();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  if (!d) return null;
  const t = d.ticket;
  const agent = d.role === 'agent';
  const fileName = new Map(d.files.map((f) => [f.id, f.filename]));
  const send = () =>
    act(
      async () => {
        await helpdesk.reply(t.id, { body: body.trim(), internal: agent && internal, fileIds: files.map((f) => f.id) });
        setBody('');
        setFiles([]);
        setInternal(false);
      },
      internal ? 'Note added.' : 'Message sent.',
    );
  const closed = t.status === 'closed';
  return (
    <Stack gap="md">
      <div>
        <Text size="xs" c="dimmed">
          #{t.number}, {t.category_name}, raised {fmtDateShort(t.created_at)} by {t.requester_name}
        </Text>
        <Text fw={600} size="lg">
          {t.subject}
        </Text>
        <Group mt={6} gap="sm" wrap="wrap">
          <TicketBadges status={t.status} priority={t.priority} confidential={t.confidential} overdue={!!t.overdue} />
          {!['resolved', 'closed'].includes(t.status) ? (
            <Text size="xs" c={t.overdue ? 'red' : 'dimmed'}>
              Due {fmtDateShort(t.sla_due_at)} at {fmtTime(t.sla_due_at)}
            </Text>
          ) : null}
        </Group>
      </div>
      {agent ? (
        <Group gap="sm" wrap="wrap" align="flex-end">
          <Select
            label="Status"
            w={170}
            value={t.status}
            allowDeselect={false}
            onChange={(v) => v && v !== t.status && act(() => helpdesk.update(t.id, { status: v }), 'Status changed.')}
            data={Object.keys(STATUS_LABEL).map((k) => ({ value: k, label: k === 'waiting' ? 'Waiting on requester' : STATUS_LABEL[k] }))}
          />
          <Select
            label="Priority"
            w={130}
            value={t.priority}
            allowDeselect={false}
            onChange={(v) => v && v !== t.priority && act(() => helpdesk.update(t.id, { priority: v }), 'Priority changed.')}
            data={Object.keys(PRIORITY_LABEL).map((k) => ({ value: k, label: PRIORITY_LABEL[k] }))}
          />
          {agents ? (
            <Select
              label="Assigned to"
              w={220}
              searchable
              clearable
              placeholder="Unassigned"
              value={t.assignee_user_id}
              onChange={(v) =>
                v !== t.assignee_user_id && act(() => helpdesk.update(t.id, { assigneeUserId: v }), v ? 'Assigned.' : 'Unassigned.')
              }
              data={agents
                .filter((a) => (!t.confidential || a.is_hr) && a.id !== t.requester_user_id)
                .map((a) => ({ value: a.id, label: a.name }))}
            />
          ) : (
            <Text size="sm" c="dimmed">
              Assigned to {t.assignee_name ?? 'nobody yet'}
            </Text>
          )}
        </Group>
      ) : null}

      <Stack gap="sm">
        {d.messages.map((m) => (
          <Paper
            key={m.id}
            withBorder
            p="sm"
            {...(m.internal ? { bg: 'var(--mantine-color-yellow-light)' } : m.mine ? { bg: 'var(--mantine-color-brand-light)' } : {})}
          >
            <Group justify="space-between" gap="xs" mb={4}>
              <Text size="sm" fw={600}>
                {m.author_name}
                {m.internal ? (
                  <Text span size="xs" c="dimmed" fw={400}>
                    {' '}
                    Internal note
                  </Text>
                ) : null}
              </Text>
              <Text size="xs" c="dimmed" title={`${fmtDateShort(m.created_at)} ${fmtTime(m.created_at)}`}>
                {relativeTime(m.created_at)}
              </Text>
            </Group>
            <Text size="sm" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
              {m.body}
            </Text>
            {m.file_ids.length ? (
              <Group gap="sm" mt={6}>
                {m.file_ids.map((f) => (
                  <Anchor key={f} href={helpdesk.fileUrl(t.id, f)} target="_blank" rel="noopener" size="xs">
                    <IconPaperclip {...ICON} /> {fileName.get(f) ?? 'Attachment'}
                  </Anchor>
                ))}
              </Group>
            ) : null}
          </Paper>
        ))}
      </Stack>

      {!closed ? (
        <Paper withBorder p="sm">
          <Stack gap="xs">
            <Textarea
              placeholder={t.status === 'resolved' && !agent ? 'Still need help? Write here to reopen the ticket.' : 'Write a reply'}
              value={body}
              onChange={(e) => setBody(e.currentTarget.value)}
              autosize
              minRows={2}
              maxLength={5000}
            />
            <Group justify="space-between" wrap="wrap" gap="xs">
              <Group gap="xs" wrap="wrap">
                {files.map((f) => (
                  <Badge
                    key={f.id}
                    variant="light"
                    color="gray"
                    rightSection={
                      <Anchor size="xs" c="dimmed" onClick={() => setFiles(files.filter((x) => x.id !== f.id))}>
                        ×
                      </Anchor>
                    }
                  >
                    {f.filename}
                  </Badge>
                ))}
                {files.length < 5 ? (
                  <FileUpload
                    purpose="helpdesk_attachment"
                    value={pending}
                    label="Attach"
                    onChange={(f) => {
                      if (f) setFiles([...files, f]);
                      setPending(null);
                    }}
                  />
                ) : null}
                {agent ? (
                  <Switch size="sm" label="Internal note" checked={internal} onChange={(e) => setInternal(e.currentTarget.checked)} />
                ) : null}
              </Group>
              <Button loading={busy} disabled={!body.trim()} onClick={send}>
                {internal ? 'Add note' : 'Send'}
              </Button>
            </Group>
          </Stack>
        </Paper>
      ) : (
        <Text size="sm" c="dimmed">
          This ticket is closed. Raise a new ticket if you need more help.
        </Text>
      )}

      {!agent && ['resolved', 'closed'].includes(t.status) && t.rating === null ? (
        <Paper withBorder p="sm">
          <Text size="sm" fw={500} mb={6}>
            How did we do?
          </Text>
          <Group gap="sm" align="flex-end" wrap="wrap">
            <Rating value={rating} onChange={setRating} size="lg" />
            <Textarea
              placeholder="Anything to add? (optional)"
              value={ratingNote}
              onChange={(e) => setRatingNote(e.currentTarget.value)}
              autosize
              minRows={1}
              style={{ flex: 1, minWidth: 200 }}
            />
            <Button
              variant="default"
              disabled={!rating}
              onClick={() => act(() => helpdesk.rate(t.id, rating, ratingNote.trim() || undefined), 'Thanks for the feedback.')}
            >
              Rate
            </Button>
          </Group>
        </Paper>
      ) : null}
      {t.rating !== null ? (
        <Text size="sm" c="dimmed">
          Rated {t.rating} of 5{t.rating_comment ? `: “${t.rating_comment}”` : ''}
        </Text>
      ) : null}
      <Group gap="xs">
        {d.canReopen ? (
          <Button variant="default" size="xs" onClick={() => act(() => helpdesk.reopen(t.id), 'Ticket reopened.')}>
            Reopen
          </Button>
        ) : null}
        {!closed && !agent ? (
          <Button variant="subtle" size="xs" color="gray" onClick={() => act(() => helpdesk.close(t.id), 'Ticket closed.')}>
            Close ticket
          </Button>
        ) : null}
      </Group>
    </Stack>
  );
}
