import { Badge, Button, Group, Modal, Paper, SegmentedControl, Stack, Switch, Text, Textarea } from '@mantine/core';
import { IconBulb } from '@tabler/icons-react';
import { useState } from 'react';
import { engage } from '../../api/engage';
import { categoryLabel } from '../../components/engage/Panels';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS_COLOR = { new: 'brand', reviewed: 'blue', answered: 'teal', archived: 'gray' };
const STATUS_LABEL = { new: 'New', reviewed: 'Reviewed', answered: 'Answered', archived: 'Archived' };

/** HR inbox for the anonymous suggestion box: read, reply, and choose whether the answer is shared with everyone. */
export default function SuggestionsPage() {
  const [status, setStatus] = useState('open');
  const q = useQuery(() => engage.suggestions(status === 'open' ? {} : { status }), [status]);
  const [replying, setReplying] = useState(null);
  const [reply, setReply] = useState('');
  const [share, setShare] = useState(true);
  const [busy, setBusy] = useState(false);
  const list = (q.data?.suggestions ?? []).filter((s) => status !== 'open' || s.status === 'new' || s.status === 'reviewed');
  const update = async (s, b, ok) => {
    try {
      await engage.updateSuggestion(s.id, b);
      toast.ok(ok);
      await q.reload();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <>
      <PageHeader
        title="Suggestions"
        subtitle="Anonymous ideas and concerns from employees. Nothing about the sender is stored, only the day it arrived."
        actions={
          <SegmentedControl
            size="xs"
            value={status}
            onChange={(v) => setStatus(v)}
            data={[
              { value: 'open', label: 'To handle' },
              { value: 'answered', label: 'Answered' },
              { value: 'archived', label: 'Archived' },
            ]}
          />
        }
      />
      {!q.loading && list.length === 0 ? (
        <EmptyState
          icon={<IconBulb size={20} stroke={1.6} />}
          title={status === 'open' ? 'No suggestions waiting' : 'Nothing here'}
          hint="Employees send suggestions from the Engage page."
        />
      ) : (
        <Stack gap="sm">
          {list.map((s) => (
            <Paper key={s.id} withBorder p="md" radius="lg">
              <Group justify="space-between" mb={6} wrap="nowrap">
                <Group gap={6}>
                  <Badge variant="light" color={STATUS_COLOR[s.status ?? 'new']}>
                    {STATUS_LABEL[s.status ?? 'new']}
                  </Badge>
                  <Badge variant="outline" color="gray">
                    {categoryLabel(s.category)}
                  </Badge>
                  {s.isPublic ? (
                    <Badge variant="outline" color="teal">
                      Shared
                    </Badge>
                  ) : null}
                </Group>
                <Text size="xs" c="dimmed">
                  {fmtDate(s.receivedOn)}
                </Text>
              </Group>
              <Text size="sm" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                {s.body}
              </Text>
              {s.reply ? (
                <Paper mt="sm" p="sm" radius="md" bg="var(--hj-canvas)">
                  <Text size="xs" c="dimmed" mb={2}>
                    HR reply
                  </Text>
                  <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                    {s.reply}
                  </Text>
                </Paper>
              ) : null}
              <Group gap="xs" mt="sm">
                <Button
                  size="xs"
                  onClick={() => {
                    setReplying(s);
                    setReply(s.reply ?? '');
                    setShare(s.isPublic ?? true);
                  }}
                >
                  {s.reply ? 'Edit reply' : 'Reply'}
                </Button>
                {s.status === 'new' ? (
                  <Button size="xs" variant="default" onClick={() => void update(s, { status: 'reviewed' }, 'Marked as reviewed.')}>
                    Mark reviewed
                  </Button>
                ) : null}
                {s.status !== 'archived' ? (
                  <Button size="xs" variant="subtle" color="gray" onClick={() => void update(s, { status: 'archived' }, 'Archived.')}>
                    Archive
                  </Button>
                ) : null}
                {s.reply ? (
                  <Button
                    size="xs"
                    variant="subtle"
                    onClick={() => void update(s, { isPublic: !s.isPublic }, s.isPublic ? 'No longer shared.' : 'Shared with everyone.')}
                  >
                    {s.isPublic ? 'Stop sharing' : 'Share with everyone'}
                  </Button>
                ) : null}
              </Group>
            </Paper>
          ))}
        </Stack>
      )}
      <Modal opened={!!replying} onClose={() => setReplying(null)} title="Reply to suggestion" centered size="lg">
        <Stack>
          <Text size="sm" c="dimmed" style={{ whiteSpace: 'pre-wrap' }}>
            “{replying?.body}”
          </Text>
          <Textarea
            label="Your reply"
            autosize
            minRows={4}
            maxLength={3000}
            value={reply}
            onChange={(e) => setReply(e.currentTarget.value)}
          />
          <Switch
            label="Share the suggestion and reply with everyone"
            description="Shown on the Engage page under the suggestion box."
            checked={share}
            onChange={(e) => setShare(e.currentTarget.checked)}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setReplying(null)}>
              Cancel
            </Button>
            <Button
              loading={busy}
              disabled={!reply.trim()}
              onClick={async () => {
                if (!replying) return;
                setBusy(true);
                try {
                  await engage.updateSuggestion(replying.id, { reply: reply.trim(), isPublic: share });
                  toast.ok('Reply saved.');
                  setReplying(null);
                  await q.reload();
                } catch (e) {
                  toast.err(e);
                } finally {
                  setBusy(false);
                }
              }}
            >
              Save reply
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
