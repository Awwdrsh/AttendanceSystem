import { Button, Paper, Stack, Text, Title } from '@mantine/core';
import type { ReactNode } from 'react';

/** Empty states are constructive and name the next action (plan §13.9). */
export function EmptyState({ title, hint, action, icon }: { title: string; hint?: string; action?: { label: string; onClick: () => void }; icon?: ReactNode }) {
  return (
    <Paper withBorder p="xl" radius="md">
      <Stack align="center" gap="xs" ta="center">
        {icon ? <div style={{ fontSize: 32, lineHeight: 1 }}>{icon}</div> : null}
        <Title order={4}>{title}</Title>
        {hint ? <Text c="dimmed" size="sm" maw={480}>{hint}</Text> : null}
        {action ? <Button mt="sm" onClick={action.onClick}>{action.label}</Button> : null}
      </Stack>
    </Paper>
  );
}
