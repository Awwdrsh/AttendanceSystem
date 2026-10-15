import { Badge, Button, Group, Paper, Stack, Text } from '@mantine/core';
import { people } from '../../api/people';
import { fmtAd, NPT_OFFSET_MIN } from '../../lib/format';
import { toast } from '../../lib/toast';
import type { QueryState } from '../../lib/useQuery';
import { ChangeList } from './ChangeList';

const STATUS: Record<string, { label: string; color: string }> = {
  pending: { label: 'Waiting for HR', color: 'yellow' }, approved: { label: 'Approved', color: 'green' }, rejected: { label: 'Not approved', color: 'red' }, cancelled: { label: 'Cancelled', color: 'gray' },
};
const nptDate = (iso: string) => new Date(Date.parse(iso) + NPT_OFFSET_MIN * 60_000).toISOString().slice(0, 10);

/** The employee's own profile change requests and what happened to them. */
export function MyChangeRequests({ q, onChanged }: { q: QueryState<Awaited<ReturnType<typeof people.myRequests>>>; onChanged: () => void }) {
  const list = q.data?.requests ?? [];
  if (!list.length) return null;
  return (
    <Stack gap="sm">
      {list.slice(0, 10).map((r) => (
        <Paper key={r.id} withBorder p="md">
          <Group justify="space-between" wrap="wrap" gap="xs" mb="xs">
            <div>
              <Text fw={600} size="sm">{r.sectionLabel}</Text>
              <Text size="xs" c="dimmed">Requested {fmtAd(nptDate(r.created_at))}</Text>
            </div>
            <Group gap="xs">
              <Badge variant="light" color={STATUS[r.status]?.color ?? 'gray'}>{STATUS[r.status]?.label ?? r.status}</Badge>
              {r.status === 'pending' ? (
                <Button size="compact-xs" variant="subtle" color="red" onClick={async () => {
                  try { await people.cancelRequest(r.id); toast.ok('Request cancelled.'); onChanged(); } catch (e) { toast.err(e); }
                }}>Cancel</Button>
              ) : null}
            </Group>
          </Group>
          <ChangeList changes={r.summary.changes ?? []} />
          {r.status === 'rejected' && r.decision_reason ? <Text size="sm" c="red.7" mt="xs">HR: {r.decision_reason}</Text> : null}
        </Paper>
      ))}
    </Stack>
  );
}
