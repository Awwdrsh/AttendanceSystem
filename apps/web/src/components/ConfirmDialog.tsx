import { Button, Group, Modal, Stack, Text } from '@mantine/core';
import { useState } from 'react';

/** Confirmation for destructive actions. The message should spell out exactly what will change. */
export function ConfirmDialog({ opened, title, message, confirmLabel = 'Confirm', color = 'red', onConfirm, onClose }: {
  opened: boolean; title: string; message: string; confirmLabel?: string; color?: string; onConfirm: () => Promise<void> | void; onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={busy ? () => undefined : onClose} title={<Text fw={600}>{title}</Text>} centered size="md">
      <Stack gap="lg">
        <Text size="sm">{message}</Text>
        <Group justify="flex-end" gap="xs">
          <Button variant="default" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button color={color} loading={busy} onClick={async () => { setBusy(true); try { await onConfirm(); onClose(); } finally { setBusy(false); } }}>{confirmLabel}</Button>
        </Group>
      </Stack>
    </Modal>
  );
}
