import { Anchor, Badge, Checkbox, Group, Paper, Stack, Text } from '@mantine/core';
import { Link } from 'react-router-dom';
import { PRIORITY_COLOR, PRIORITY_LABEL, tasks as tasksApi } from '../../api/tasks';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDateShort } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Home: my open tasks due this week (and overdue ones), ticked off in place. */
export function MyTodoCard() {
  const q = useQuery(() => tasksApi.todo(), []);
  const list = q.data?.tasks ?? [];
  if (q.loading || q.error || !list.length) return null;
  const today = q.data!.today;
  const done = async (id: string) => {
    try {
      const t = await tasksApi.get(id);
      const doneCol = t.board.columns.find((c) => c.done);
      if (doneCol) await tasksApi.update(id, { columnId: doneCol.id });
      toast.ok('Done.'); await q.reload();
    } catch (e) { toast.err(e); }
  };
  return (
    <div>
      <SectionTitle hint="Due this week, and anything overdue" actions={<Anchor component={Link} to="/tasks" size="sm">All tasks</Anchor>}>My to-do</SectionTitle>
      <Paper withBorder radius="lg" p="md">
        <Stack gap={8}>
          {list.slice(0, 8).map((t) => {
            const overdue = !!t.due_date && t.due_date < today;
            return (
              <Group key={t.id} justify="space-between" wrap="nowrap">
                <Checkbox label={<Anchor component={Link} to={`/tasks/${t.board_id}`} size="sm" c="inherit">{t.title}</Anchor>} onChange={() => void done(t.id)} />
                <Group gap={6} wrap="nowrap">
                  {t.priority === 'high' || t.priority === 'urgent' ? <Badge size="xs" variant="light" color={PRIORITY_COLOR[t.priority]}>{PRIORITY_LABEL[t.priority]}</Badge> : null}
                  {t.board_kind === 'team' ? <Text size="xs" c="dimmed">{t.board_name}</Text> : null}
                  {t.due_date ? <Text size="xs" c={overdue ? 'red' : 'dimmed'}>{overdue ? 'Overdue, ' : ''}{fmtDateShort(t.due_date)}</Text> : null}
                </Group>
              </Group>
            );
          })}
          {list.length > 8 ? <Text size="xs" c="dimmed">and {list.length - 8} more</Text> : null}
        </Stack>
      </Paper>
    </div>
  );
}
