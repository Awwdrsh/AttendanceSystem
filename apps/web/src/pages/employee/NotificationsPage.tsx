import { Button, Paper, Stack, Text } from '@mantine/core';
import { notifications as nApi } from '../../api';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function NotificationsPage() {
  const { t } = useI18n();
  const q = useQuery(() => nApi.list({ limit: 50 }), []);
  const rows = q.data?.notifications ?? [];
  return (
    <Stack>
      <PageHeader title={t('notifications')} subtitle={q.data ? `${q.data.unread} unread` : undefined} actions={<Button variant="light" onClick={async () => { try { await nApi.readAll(); await q.reload(); } catch (e) { toast.err(e); } }}>{t('markAllRead')}</Button>} />
      {!q.loading && rows.length === 0 ? <EmptyState title={t('nothingHere')} hint="Approvals, corrections and payslip notices appear here." /> : null}
      {rows.map((n) => (
        <Paper key={n.id} withBorder p="sm" radius="md" onClick={async () => { if (!n.read_at) { try { await nApi.read(n.id); q.setData(q.data ? { ...q.data, unread: Math.max(0, q.data.unread - 1), notifications: q.data.notifications.map((x) => (x.id === n.id ? { ...x, read_at: new Date().toISOString() } : x)) } : null); } catch { /* ignore */ } } }}
          style={{ cursor: 'pointer', borderLeft: n.read_at ? undefined : '4px solid var(--mantine-color-teal-6)' }}>
          <Text fw={n.read_at ? 400 : 600}>{n.title}</Text>
          <Text size="sm">{n.body}</Text>
          <Text size="xs" c="dimmed">{relativeTime(n.created_at)}</Text>
        </Paper>
      ))}
    </Stack>
  );
}
