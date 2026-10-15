import { Badge, Button, Group, Paper, SegmentedControl, Skeleton, Stack, Text } from '@mantine/core';
import { IconCalendarEvent } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { hiring, MODE_LABEL, type Interview } from '../../api/hiring';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateLong, fmtTime, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';
import { CandidateDrawer, ScorecardModal } from './CandidateDrawer';
import { nptDateOf } from './shared';

/** Interviews by day: the ones you sit on, or every interview for the jobs you can see, with scorecards to fill in. */
export default function InterviewsPage() {
  const [scope, setScope] = useState<'mine' | 'all'>('mine');
  const [when, setWhen] = useState<'upcoming' | 'past'>('upcoming');
  const today = todayNpt();
  const range = when === 'upcoming' ? { from: today } : { from: addDays(today, -90), to: addDays(today, -1) };
  const q = useQuery(() => hiring.interviews({ scope, ...range }), [scope, when]);
  const [open, setOpen] = useState<string | null>(null);
  const [scoring, setScoring] = useState<Interview | null>(null);

  const days = useMemo(() => {
    const list = [...(q.data?.interviews ?? [])];
    if (when === 'past') list.reverse();
    const map = new Map<string, Interview[]>();
    for (const i of list) { const d = nptDateOf(i.starts_at); map.set(d, [...(map.get(d) ?? []), i]); }
    return [...map.entries()];
  }, [q.data, when]);

  return (
    <Stack gap="lg">
      <PageHeader title="Interviews" subtitle="Fill in your scorecard soon after each interview, while it is fresh."
        actions={<>
          <SegmentedControl value={scope} onChange={(v) => setScope(v as 'mine' | 'all')} data={[{ value: 'mine', label: 'My interviews' }, { value: 'all', label: 'All' }]} />
          <SegmentedControl value={when} onChange={(v) => setWhen(v as 'upcoming' | 'past')} data={[{ value: 'upcoming', label: 'Upcoming' }, { value: 'past', label: 'Last 90 days' }]} />
        </>} />
      {q.loading && !q.data ? <Skeleton h={160} radius="lg" /> : null}
      {q.data && days.length === 0 ? <EmptyState icon={<IconCalendarEvent size={20} />} title={when === 'upcoming' ? 'No interviews coming up' : 'No interviews in the last 90 days'}
        hint="Schedule interviews from a candidate's card in Candidates." /> : null}
      {days.map(([day, list]) => (
        <div key={day}>
          <SectionTitle>{day === today ? `Today, ${fmtDateLong(day)}` : fmtDateLong(day)}</SectionTitle>
          <Stack gap="sm">
            {list.map((i) => (
              <Paper key={i.id} withBorder radius="md" p="sm" style={{ opacity: i.status === 'cancelled' ? 0.55 : 1 }}>
                <Group justify="space-between" wrap="wrap" gap="sm">
                  <Group gap="md" wrap="nowrap">
                    <Text fw={600} className="num" w={52}>{fmtTime(i.starts_at)}</Text>
                    <div>
                      <Text size="sm" fw={600}>{i.candidate} <Text span c="dimmed" fw={400}>· {i.job}</Text></Text>
                      <Text size="xs" c="dimmed">{i.title} · {i.duration_min} min · {MODE_LABEL[i.mode]}{i.location ? ` · ${i.location}` : ''}</Text>
                      <Text size="xs" c="dimmed">Panel: {(i.panel_names ?? []).join(', ')}</Text>
                    </div>
                  </Group>
                  <Group gap="xs">
                    {i.status !== 'scheduled' ? <Badge variant="light" color={i.status === 'cancelled' ? 'gray' : 'teal'} size="sm">{i.status === 'cancelled' ? 'Cancelled' : 'Done'}</Badge> : null}
                    <Badge variant="light" color="gray" size="sm">{i.scorecards ?? 0} scorecards</Badge>
                    {i.on_panel && i.status !== 'cancelled'
                      ? <Button size="xs" variant={i.my_scorecard ? 'default' : 'filled'} onClick={() => setScoring(i)}>{i.my_scorecard ? 'Update scorecard' : 'Add scorecard'}</Button> : null}
                    <Button size="xs" variant="subtle" onClick={() => setOpen(i.application_id ?? null)}>Open candidate</Button>
                  </Group>
                </Group>
              </Paper>
            ))}
          </Stack>
        </div>
      ))}
      <CandidateDrawer applicationId={open} onClose={() => setOpen(null)} onChanged={() => void q.reload()} />
      <ScorecardModal interview={scoring} onClose={() => setScoring(null)} onSaved={() => void q.reload()} />
    </Stack>
  );
}
