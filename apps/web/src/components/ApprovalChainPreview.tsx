import { Group, Paper, Skeleton, Stack, Text, ThemeIcon } from '@mantine/core';
import { IconAlertTriangle, IconArrowRight, IconRoute } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { flows, type Preview } from '../api/flows';

/**
 * "Who will approve this": the approval chain for a request type, resolved for the employee (and amount, for money
 * requests) so people know where their request goes before they send it. Silent when it cannot be worked out.
 */
export function ApprovalChainPreview({ objectType, employeeId, amount }: { objectType: string; employeeId?: string | null; amount?: number | null }) {
  const [data, setData] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  // Debounce amount changes so typing a figure does not fire a request per keystroke.
  useEffect(() => {
    let alive = true;
    setLoading(true);
    const t = setTimeout(() => {
      flows.preview({ objectType, ...(employeeId ? { employeeId } : {}), ...(amount != null && amount > 0 ? { amount } : {}) })
        .then((r) => { if (alive) setData(r); })
        .catch(() => { if (alive) setData(null); })
        .finally(() => { if (alive) setLoading(false); });
    }, 300);
    return () => { alive = false; clearTimeout(t); };
  }, [objectType, employeeId, amount]);

  if (loading && !data) return <Skeleton h={44} radius="md" />;
  if (!data || !data.steps.length) return null;
  return (
    <Paper withBorder radius="md" p="sm" bg="var(--mantine-color-default-hover)">
      <Group gap="xs" wrap="nowrap" align="flex-start">
        <ThemeIcon variant="light" size="sm" radius="xl"><IconRoute size={14} stroke={1.6} /></ThemeIcon>
        <Stack gap={4} style={{ minWidth: 0 }}>
          <Text size="xs" c="dimmed" fw={600}>Approved by</Text>
          <Group gap={6} wrap="wrap">
            {data.steps.map((s, i) => (
              <Group key={i} gap={6} wrap="nowrap">
                {i > 0 ? <IconArrowRight size={12} stroke={1.6} color="var(--mantine-color-dimmed)" /> : null}
                <Text size="sm">
                  {s.approvers.length === 0 ? s.label : s.approvers.length <= 2 ? s.approvers.map((a) => a.name).join(' or ') : `${s.label} (${s.approvers.length} people)`}
                </Text>
              </Group>
            ))}
          </Group>
          {data.steps.flatMap((s) => s.notes).length ? (
            <Group gap={4} wrap="nowrap">
              <IconAlertTriangle size={12} stroke={1.6} color="var(--mantine-color-orange-6)" />
              <Text size="xs" c="dimmed">{[...new Set(data.steps.flatMap((s) => s.notes))].join('. ')}.</Text>
            </Group>
          ) : null}
        </Stack>
      </Group>
    </Paper>
  );
}
