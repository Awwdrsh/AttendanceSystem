import { Anchor, Badge, Button, Group, Paper, Stack, Text } from '@mantine/core';
import { IconBalloon, IconClipboardList, IconConfetti, IconPin, IconUserPlus } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { engage } from '../../api/engage';
import { fmtDate } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;

/** Home: the latest pinned announcement, today's birthdays and anniversaries, and a survey waiting for an answer. Hidden when there is nothing to show. */
export function HomeCompanyCard() {
  const q = useQuery(() => engage.home(), []);
  const d = q.data;
  if (!d || (!d.announcement && !d.celebrations.length && !d.survey)) return null;
  return (
    <Paper withBorder p="lg">
      <Group justify="space-between" mb="sm">
        <Group gap={8}><IconConfetti {...ICON} /><Text fw={600}>Company</Text></Group>
        <Anchor component={Link} to="/engage" size="sm">Open Engage</Anchor>
      </Group>
      <Stack gap="md">
        {d.announcement ? (
          <div>
            <Group gap={6} mb={2}><IconPin size={14} stroke={1.6} color="var(--hj-hydro)" /><Text size="sm" fw={600}>{d.announcement.title}</Text>
              {d.announcement.requiresAck && !d.announcement.acked ? <Badge size="xs" color="orange" variant="light">Please read</Badge> : null}</Group>
            <Text size="sm" c="dimmed" lineClamp={2}>{d.announcement.body}</Text>
          </div>
        ) : null}
        {d.celebrations.length ? (
          <Group gap="xs">
            {d.celebrations.slice(0, 4).map((c) => (
              <Badge key={c.targetId} variant="light" color={c.kind === 'birthday' ? 'pink' : c.kind === 'joined' ? 'blue' : 'brand'} size="lg" radius="sm"
                leftSection={c.kind === 'birthday' ? <IconBalloon size={12} /> : c.kind === 'joined' ? <IconUserPlus size={12} /> : <IconConfetti size={12} />} tt="none">
                {c.name}{c.kind === 'birthday' ? ': birthday' : c.kind === 'anniversary' ? `: ${c.years} ${c.years === 1 ? 'year' : 'years'}` : ': joined'}
              </Badge>
            ))}
            {d.celebrations.length > 4 ? <Text size="xs" c="dimmed">and {d.celebrations.length - 4} more</Text> : null}
          </Group>
        ) : null}
        {d.survey ? (
          <Group justify="space-between" wrap="nowrap" gap="sm">
            <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
              <IconClipboardList {...ICON} />
              <Text size="sm" truncate>{d.survey.title} <Text span c="dimmed" size="xs">until {fmtDate(d.survey.closesOn)}</Text></Text>
            </Group>
            <Button size="xs" component={Link} to="/engage/surveys" style={{ flex: 'none' }}>{d.survey.kind === 'poll' ? 'Vote' : 'Answer'}{d.surveysWaiting > 1 ? ` (${d.surveysWaiting})` : ''}</Button>
          </Group>
        ) : null}
      </Stack>
    </Paper>
  );
}
