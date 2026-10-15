import { Anchor, Badge, Group, SegmentedControl, Stack, Text } from '@mantine/core';
import { IconMapPin } from '@tabler/icons-react';
import { useState } from 'react';
import { BREAK_LABEL, field as fieldApi, osmLink } from '../../api/field';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateShort, fmtTime, minutesToHm, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

/** The employee's own field visits and breaks over the last week or month. */
export default function MyFieldVisitsPage() {
  const [span, setSpan] = useState('7');
  const to = todayNpt(); const from = addDays(to, -(Number(span) - 1));
  const me = useQuery(() => fieldApi.me(), []);
  const visits = useQuery(() => fieldApi.visits({ from, to }), [from, to]);
  const breaks = useQuery(() => fieldApi.breaks({ from, to }), [from, to]);
  const v = visits.data?.visits ?? []; const b = breaks.data?.breaks ?? [];

  return (
    <Stack gap="lg">
      <SectionTitle hint="Places you visited for work and the breaks you took."
        actions={<SegmentedControl size="xs" value={span} onChange={setSpan} data={[{ value: '7', label: '7 days' }, { value: '30', label: '30 days' }]} />}>Visits and breaks</SectionTitle>
      <StatStrip stats={[
        { key: 'v', label: 'Visits', value: v.length },
        { key: 'vt', label: 'Time on visits', value: minutesToHm(v.reduce((s, x) => s + x.minutes, 0)) },
        { key: 'b', label: 'Break time', value: minutesToHm(b.reduce((s, x) => s + (x.minutes ?? 0), 0)) },
        { key: 'o', label: 'Breaks over limit', value: b.filter((x) => x.over_limit).length, ...(b.some((x) => x.over_limit) ? { tone: 'warn' as const } : {}) },
      ]} />
      {me.data && !me.data.fieldWork && v.length === 0 ? (
        <EmptyState icon={<IconMapPin size={20} stroke={1.6} />} title="Field visits are not turned on for you"
          hint="If your work takes you to clients or sites, ask HR to turn on field visits. You can then start and end visits from Home." />
      ) : (
        <DataTable rows={v} rowKey={(x) => x.id} loading={visits.loading} error={visits.error} emptyTitle="No visits in this period"
          emptyHint="Start a visit from Home when you reach a client or site."
          columns={[
            { key: 'date', header: 'Day', render: (x) => fmtDateShort(x.local_date) },
            { key: 'place', header: 'Place', render: (x) => <div><Text size="sm" fw={500}>{x.place}</Text>{x.purpose ? <Text size="xs" c="dimmed" lineClamp={1}>{x.purpose}</Text> : null}</div> },
            { key: 'time', header: 'Time', render: (x) => <span className="num">{fmtTime(x.started_at)} to {x.ended_at ? fmtTime(x.ended_at) : 'now'}</span> },
            { key: 'dur', header: 'Duration', align: 'right', render: (x) => <span className="num">{minutesToHm(x.minutes)}</span> },
            { key: 'outcome', header: 'Outcome', render: (x) => x.ended_at ? <Text size="sm" c="dimmed" lineClamp={2}>{x.outcome ?? '—'}</Text> : <Badge variant="light">In progress</Badge> },
            { key: 'map', header: 'Map', render: (x) => x.start_lat !== null && x.start_lng !== null ? <Anchor href={osmLink(x.start_lat, x.start_lng)} target="_blank" rel="noopener noreferrer" size="sm">Open</Anchor> : <Text size="xs" c="dimmed">No location</Text> },
          ]} />
      )}
      <div>
        <SectionTitle>Breaks</SectionTitle>
        <DataTable rows={b} rowKey={(x) => x.id} loading={breaks.loading} error={breaks.error} emptyTitle="No breaks in this period" emptyHint="Breaks you start from Home appear here."
          columns={[
            { key: 'date', header: 'Day', render: (x) => fmtDateShort(x.local_date) },
            { key: 'kind', header: 'Break', render: (x) => <Group gap={6}>{BREAK_LABEL[x.kind]}{x.paid ? null : <Badge size="xs" variant="light" color="gray">Unpaid</Badge>}</Group> },
            { key: 'time', header: 'Time', render: (x) => <span className="num">{fmtTime(x.started_at)} to {x.ended_at ? fmtTime(x.ended_at) : 'now'}</span> },
            { key: 'min', header: 'Minutes', align: 'right', render: (x) => <Text size="sm" className="num" {...(x.over_limit ? { c: 'red' } : {})}>{minutesToHm(x.minutes ?? 0)}{x.over_limit ? ` (limit ${x.max_min} min)` : ''}</Text> },
          ]} />
      </div>
    </Stack>
  );
}
