import { Group, Text } from '@mantine/core';
import { IconCloudOff, IconCloudUpload } from '@tabler/icons-react';
import { useEffect, useRef } from 'react';
import { useAuth } from '../auth/AuthProvider';
import { flushOfflinePunches, useOnline, useQueuedPunches } from '../lib/offlinePunch';
import { toast } from '../lib/toast';

/** A slim bar while the device is offline, and the sender for clock-ins saved while offline. */
export function OfflineBanner() {
  const online = useOnline();
  const { user } = useAuth();
  const queued = useQueuedPunches(user?.id);
  const sending = useRef(false);

  useEffect(() => {
    if (!online || !user?.id || !queued.length || sending.current) return;
    sending.current = true;
    flushOfflinePunches(user.id)
      .then(({ sent, refused }) => {
        if (sent)
          toast.ok(
            `${sent} clock-${sent === 1 ? 'in' : 'ins'} made while offline ${sent === 1 ? 'was' : 'were'} sent to your manager as ${sent === 1 ? 'a correction' : 'corrections'}.`,
            'Back online',
          );
        for (const r of refused) toast.err(new Error(r.message), 'An offline clock-in was not accepted');
      })
      .finally(() => {
        sending.current = false;
      });
  }, [online, user?.id, queued.length]);

  if (online && !queued.length) return null;
  return (
    <Group
      gap={8}
      wrap="nowrap"
      px="md"
      py={6}
      role="status"
      style={{
        background: online ? 'var(--hj-hydro-soft)' : 'var(--mantine-color-orange-light)',
        borderBottom: '1px solid var(--hj-line)',
      }}
    >
      {online ? <IconCloudUpload size={16} stroke={1.6} /> : <IconCloudOff size={16} stroke={1.6} />}
      <Text size="sm">
        {online
          ? 'Sending clock-ins saved while you were offline…'
          : `You are offline. Pages you opened still show; nothing new can be saved.${queued.length ? ` ${queued.length} clock-${queued.length === 1 ? 'in is' : 'ins are'} saved on this device and will be sent when you are back online.` : ''}`}
      </Text>
    </Group>
  );
}
