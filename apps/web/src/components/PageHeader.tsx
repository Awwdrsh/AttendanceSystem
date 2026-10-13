import { Anchor, Group, Stack, Text, Title } from '@mantine/core';
import { IconChevronLeft } from '@tabler/icons-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { useEmbedded } from './embedded';

/** Page title, one line of context, and the page's actions on the right. */
export function PageHeader({ title, subtitle, actions, back }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; back?: { to: string; label: string } }) {
  const embedded = useEmbedded();
  if (embedded) {
    // Inside a hub tab: the hub already shows the title; keep the tab's description and actions on one row.
    if (!subtitle && !actions) return null;
    return (
      <Group justify="space-between" align="center" wrap="wrap" gap="md" mb="lg">
        {subtitle ? <Text c="dimmed" size="sm" maw={680}>{subtitle}</Text> : <span />}
        {actions ? <Group gap="xs" wrap="wrap">{actions}</Group> : null}
      </Group>
    );
  }
  return (
    <Stack gap={10} mb={24}>
      {back ? (
        <Anchor component={Link} to={back.to} size="sm" c="dimmed" underline="never" style={{ display: 'inline-flex', alignItems: 'center', gap: 2, width: 'fit-content' }}>
          <IconChevronLeft size={15} stroke={1.8} />{back.label}
        </Anchor>
      ) : null}
      <Group justify="space-between" align="flex-end" wrap="wrap" gap="md">
        <Stack gap={4} style={{ minWidth: 0 }}>
          <Title order={1} fz={{ base: 22, sm: 26 }} fw={600} style={{ letterSpacing: '-0.02em' }}>{title}</Title>
          {subtitle ? <Text c="dimmed" size="sm" maw={680}>{subtitle}</Text> : null}
        </Stack>
        {actions ? <Group gap="xs" wrap="wrap">{actions}</Group> : null}
      </Group>
    </Stack>
  );
}

/** Section heading inside a page. */
export function SectionTitle({ children, actions, hint }: { children: ReactNode; actions?: ReactNode; hint?: ReactNode }) {
  return (
    <Group justify="space-between" align="flex-end" mb="sm" mt="xs">
      <div>
        <Text fw={600}>{children}</Text>
        {hint ? <Text size="sm" c="dimmed">{hint}</Text> : null}
      </div>
      {actions}
    </Group>
  );
}
