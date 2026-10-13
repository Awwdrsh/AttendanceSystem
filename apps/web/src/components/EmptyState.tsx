import { Button, Stack, Text, ThemeIcon } from '@mantine/core';
import { IconInbox } from '@tabler/icons-react';
import type { ReactNode } from 'react';

/** Empty states say what is missing and offer the next step. */
export function EmptyState({ title, hint, action, icon, compact }: { title: string; hint?: string; action?: { label: string; onClick: () => void }; icon?: ReactNode; compact?: boolean }) {
  return (
    <Stack align="center" gap={6} ta="center" py={compact ? 'lg' : 48} px="md" style={{ border: '1px dashed var(--hj-line)', borderRadius: 12 }}>
      <ThemeIcon variant="light" color="gray" size={40} radius="md">{icon ?? <IconInbox size={20} stroke={1.6} />}</ThemeIcon>
      <Text fw={600} mt={4}>{title}</Text>
      {hint ? <Text c="dimmed" size="sm" maw={440}>{hint}</Text> : null}
      {action ? <Button mt="xs" size="sm" onClick={action.onClick}>{action.label}</Button> : null}
    </Stack>
  );
}
