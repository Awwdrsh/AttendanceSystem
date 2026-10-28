import { Button, Checkbox, Group, Modal, Stack, Text } from '@mantine/core';
import { useEffect, useState } from 'react';
import { relations } from '../../api/relations';
import { useAuth } from '../../auth/AuthProvider';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';

/** Notices HR marked "show at sign-in": one at a time, once per person, or until they confirm reading it. */
export function PopupNotices() {
  const { user } = useAuth();
  const [queue, setQueue] = useState([]);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!user) return;
    let alive = true;
    relations
      .popups()
      .then((r) => {
        if (alive) setQueue(r.popups);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [user?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const p = queue[0];
  if (!p) return null;
  const dismiss = async () => {
    setBusy(true);
    try {
      await relations.dismissPopup(p.id, p.requiresAck);
      setQueue(queue.slice(1));
      setAck(false);
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      opened
      onClose={() => {
        if (!p.requiresAck) void dismiss();
      }}
      withCloseButton={!p.requiresAck}
      closeOnClickOutside={!p.requiresAck}
      closeOnEscape={!p.requiresAck}
      title={<Text fw={600}>{p.title}</Text>}
      centered
      size="lg"
    >
      <Stack>
        <Text size="xs" c="dimmed">
          {p.authorName}, {relativeTime(p.createdAt)}
        </Text>
        <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
          {p.body}
        </Text>
        {p.requiresAck ? (
          <Checkbox label="I have read and understood this notice" checked={ack} onChange={(e) => setAck(e.currentTarget.checked)} />
        ) : null}
        <Group justify="flex-end">
          <Button loading={busy} disabled={p.requiresAck && !ack} onClick={dismiss}>
            {p.requiresAck ? 'Confirm' : 'Got it'}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
