import { Button, Group, Modal, Stack, Text } from '@mantine/core';
import { useState } from 'react';

/** Destructive confirmation with the affected counts spelled out (plan §13.8). */
export function ConfirmDialog({ opened, title, message, confirmLabel = 'Confirm', color = 'red', onConfirm, onClose }: {
  opened: boolean; title: string; message: string; confirmLabel?: string; color?: string; onConfirm: () => Promise<void> | void; onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={onClose} title={title} centered>
      <Stack>
        <Text size="sm">{message}</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button color={color} loading={busy} onClick={async () => { setBusy(true); try { await onConfirm(); onClose(); } finally { setBusy(false); } }}>{confirmLabel}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
