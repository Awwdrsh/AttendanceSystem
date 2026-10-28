import { Badge, Group, Paper, Stack, Text } from '@mantine/core';
import { lifecycle } from '../../api/lifecycle';
import { SectionTitle } from '../../components/PageHeader';
import { useQuery } from '../../lib/useQuery';
import { TaskRow } from './CaseDrawer';

/**
 * Home card: a joiner's getting-started tasks (or a leaver's clearance tasks), plus onboarding and exit tasks the
 * signed-in person owes for someone else. Renders nothing when there is nothing to do.
 */
export function MyChecklistCard() {
  const q = useQuery(() => lifecycle.myTasks(), []);
  const tasks = (q.data?.tasks ?? []).map((t) => ({ ...t, can_complete: true }));
  if (!tasks.length) return null;
  const own = tasks.filter((t) => t.own);
  const others = tasks.filter((t) => !t.own);
  const reload = () => {
    void q.reload();
  };
  const group = (list) => {
    const byPerson = new Map();
    for (const t of list) byPerson.set(t.employee_id, [...(byPerson.get(t.employee_id) ?? []), t]);
    return [...byPerson.values()];
  };
  return (
    <Paper withBorder p="lg">
      {own.length ? (
        <>
          <SectionTitle
            hint={
              own[0]?.kind === 'offboarding'
                ? 'Tasks to finish before your last day.'
                : 'A few things to set you up. Tick them off as you go.'
            }
          >
            {own[0]?.kind === 'offboarding' ? 'Before you leave' : 'Getting started'}
          </SectionTitle>
          <Stack gap="md" mb={others.length ? 'lg' : 0}>
            {own.map((t) => (
              <TaskRow key={t.id} task={t} onChanged={reload} />
            ))}
          </Stack>
        </>
      ) : null}
      {others.length ? (
        <>
          <SectionTitle hint="Onboarding and exit tasks waiting on you.">Checklist tasks for others</SectionTitle>
          <Stack gap="lg">
            {group(others)
              .slice(0, 6)
              .map((list) => (
                <Stack key={list[0].employee_id} gap="sm">
                  <Group gap={6}>
                    <Text size="sm" fw={600}>
                      {list[0].employee_name}
                    </Text>
                    <Badge size="xs" variant="light" color={list[0].kind === 'offboarding' ? 'red' : 'blue'}>
                      {list[0].kind === 'offboarding' ? 'Leaving' : 'Joining'}
                    </Badge>
                  </Group>
                  {list.slice(0, 5).map((t) => (
                    <TaskRow key={t.id} task={t} onChanged={reload} />
                  ))}
                  {list.length > 5 ? (
                    <Text size="xs" c="dimmed">
                      and {list.length - 5} more
                    </Text>
                  ) : null}
                </Stack>
              ))}
          </Stack>
        </>
      ) : null}
    </Paper>
  );
}
