import { Button, Group, Paper, Stack, Text, UnstyledButton } from '@mantine/core';
import { IconBell, IconChecks } from '@tabler/icons-react';
import { notifications as nApi } from '../../api';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function NotificationsPage() {
  const q = useQuery(() => nApi.list({ limit: 50 }), []);
  const rows = q.data?.notifications ?? [];
  const unread = q.data?.unread ?? 0;
  const markRead = async (id: string) => {
    try {
      await nApi.read(id);
      q.setData(q.data ? { ...q.data, unread: Math.max(0, q.data.unread - 1), notifications: q.data.notifications.map((x) => (x.id === id ? { ...x, read_at: new Date().toISOString() } : x)) } : null);
    } catch { /* ignore */ }
  };
  return (
    <Stack gap="lg">
      <PageHeader title="Notifications" subtitle={q.data ? (unread ? `${unread} unread` : 'You are all caught up') : undefined}
        actions={<Button variant="default" leftSection={<IconChecks size={16} stroke={1.6} />} disabled={!unread}
          onClick={async () => { try { await nApi.readAll(); await q.reload(); } catch (e) { toast.err(e, 'Could not mark as read'); } }}>Mark all as read</Button>} />
      {q.error ? <EmptyState title="Could not load notifications" hint={q.error} /> : null}
      {!q.loading && !q.error && rows.length === 0 ? <EmptyState icon={<IconBell size={20} stroke={1.6} />} title="No notifications yet" hint="Approvals, corrections and payslip notices will show up here." /> : null}
      {rows.length ? (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          {rows.map((n, i) => (
            <UnstyledButton key={n.id} w="100%" px="lg" py="sm" onClick={() => { if (!n.read_at) void markRead(n.id); }}
              style={{ display: 'block', borderTop: i > 0 ? '1px solid var(--hj-line-soft)' : undefined, cursor: n.read_at ? 'default' : 'pointer' }}>
              <Group wrap="nowrap" align="flex-start" gap="sm">
                <span className="hj-dot" aria-label={n.read_at ? undefined : 'Unread'} style={{ marginTop: 7, background: n.read_at ? 'transparent' : 'var(--mantine-color-brand-6)' }} />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <Group justify="space-between" wrap="nowrap" gap="sm">
                    <Text size="sm" fw={n.read_at ? 400 : 600} truncate>{n.title}</Text>
                    <Text size="xs" c="dimmed" style={{ flex: 'none' }}>{relativeTime(n.created_at)}</Text>
                  </Group>
                  <Text size="sm" c="dimmed">{n.body}</Text>
                </div>
              </Group>
            </UnstyledButton>
          ))}
        </Paper>
      ) : null}
    </Stack>
  );
}
