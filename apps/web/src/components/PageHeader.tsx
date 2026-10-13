import { Group, Stack, Text, Title } from '@mantine/core';
import type { ReactNode } from 'react';

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <Group justify="space-between" align="flex-start" mb="md" wrap="wrap">
      <Stack gap={2}>
        <Title order={2} fz={{ base: 22, sm: 26 }}>{title}</Title>
        {subtitle ? <Text c="dimmed" size="sm">{subtitle}</Text> : null}
      </Stack>
      {actions ? <Group gap="xs" wrap="wrap">{actions}</Group> : null}
    </Group>
  );
}
