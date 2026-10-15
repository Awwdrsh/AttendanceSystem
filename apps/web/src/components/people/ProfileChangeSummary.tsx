import { Stack, Text } from '@mantine/core';

/** Inbox card line for a profile change: section, each field from → to, the employee's note. */
export function ProfileChangeSummary({ summary }: { summary: Record<string, unknown> }) {
  const changes = (summary.changes as Array<{ label: string; from: string | null; to: string | null }> | undefined) ?? [];
  return (
    <Stack gap={2} component="span" style={{ display: 'flex' }}>
      <Text span size="sm">{String(summary.sectionLabel ?? 'Profile')}{summary.byHr ? ' (HR edit, second approval)' : ''}{summary.hasProof ? ', proof attached' : ''}</Text>
      {changes.slice(0, 4).map((c) => (
        <Text span key={c.label} size="xs" c="dimmed">{c.label}: {c.from ?? '(empty)'} → <Text span inherit c="var(--mantine-color-text)">{c.to ?? '(cleared)'}</Text></Text>
      ))}
      {changes.length > 4 ? <Text span size="xs" c="dimmed">and {changes.length - 4} more</Text> : null}
      {summary.reason ? <Text span size="xs" c="dimmed">“{String(summary.reason)}”</Text> : null}
    </Stack>
  );
}
