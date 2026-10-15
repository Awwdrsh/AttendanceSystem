import { Anchor, Avatar, Badge, Button, Group, Modal, SegmentedControl, Stack, Text, UnstyledButton } from '@mantine/core';
import { IconDownload } from '@tabler/icons-react';
import { useState } from 'react';
import { BREAK_LABEL, field as fieldApi, osmLink, type VisitRow } from '../../api/field';
import { download } from '../../api/client';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateLong, fmtDateShort, fmtTime, minutesToHm, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Managers and HR: who went where (field visits) and how long breaks ran. Managers see their team, HR everyone. */
export default function FieldVisitsPage() {
  const [view, setView] = useState<'visits' | 'breaks'>('visits');
  const [date, setDate] = useState<string | null>(todayNpt());
  const [span, setSpan] = useState<'day' | 'week'>('day');
  const [overOnly, setOverOnly] = useState(false);
  const [photo, setPhoto] = useState<VisitRow | null>(null);
  const to = date ?? todayNpt(); const from = span === 'week' ? addDays(to, -6) : to;
  const visits = useQuery(() => fieldApi.visits({ from, to }), [from, to], { enabled: view === 'visits' });
  const breaks = useQuery(() => fieldApi.breaks({ from, to, ...(overOnly ? { overOnly: 1 } : {}) }), [from, to, overOnly], { enabled: view === 'breaks' });
  const v = visits.data?.visits ?? []; const b = breaks.data?.breaks ?? [];
  const exportCsv = async () => {
    try { await download(`/field/${view}?from=${from}&to=${to}&format=csv`, `${view === 'visits' ? 'field-visits' : 'breaks'}-${from}-${to}.csv`); } catch (e) { toast.err(e, 'Export failed'); }
  };

  return (
    <Stack gap="lg">
      <SectionTitle hint={view === 'visits' ? 'Client and site visits by field staff, with arrival and departure times and where they were.' : 'Breaks taken during the day. Breaks longer than their limit are highlighted.'}
        actions={<Group gap="xs" wrap="wrap">
          <SegmentedControl size="xs" value={view} onChange={(x) => setView(x as 'visits' | 'breaks')} data={[{ value: 'visits', label: 'Visits' }, { value: 'breaks', label: 'Breaks' }]} />
          <SegmentedControl size="xs" value={span} onChange={(x) => setSpan(x as 'day' | 'week')} data={[{ value: 'day', label: 'Day' }, { value: 'week', label: '7 days' }]} />
          <DateField value={date} onChange={setDate} maxDate={todayNpt()} w={190} />
          <Button size="xs" variant="default" leftSection={<IconDownload size={14} stroke={1.6} />} onClick={exportCsv}>Excel</Button>
        </Group>}>{view === 'visits' ? 'Field visits' : 'Breaks'}</SectionTitle>

      {view === 'visits' ? (
        <>
          <StatStrip stats={[
            { key: 'people', label: 'People in the field', value: new Set(v.map((x) => x.employee_id)).size },
            { key: 'visits', label: 'Visits', value: v.length },
            { key: 'open', label: 'In progress', value: v.filter((x) => !x.ended_at).length },
            { key: 'time', label: 'Time on visits', value: minutesToHm(v.reduce((s, x) => s + x.minutes, 0)) },
          ]} />
          <DataTable rows={v} rowKey={(x) => x.id} loading={visits.loading} error={visits.error} emptyTitle="No field visits"
            emptyHint="Turn on field visits for staff under Settings, Locations. Their visits then show here."
            columns={[
              { key: 'emp', header: 'Name', sticky: true, render: (x) => <Group gap="sm" wrap="nowrap">
                {x.has_photo ? <UnstyledButton onClick={() => setPhoto(x)} aria-label={`Photo from ${x.place}`}><Avatar src={fieldApi.visitPhotoUrl(x.id)} size={30} radius="xl" /></UnstyledButton> : null}
                <div><Text size="sm" fw={500}>{x.name_en}</Text><Text size="xs" c="dimmed">{x.emp_code}</Text></div></Group> },
              ...(span === 'week' ? [{ key: 'day', header: 'Day', render: (x: VisitRow) => fmtDateShort(x.local_date) }] : []),
              { key: 'place', header: 'Place', render: (x) => <div><Text size="sm" fw={500}>{x.place}</Text>{x.purpose ? <Text size="xs" c="dimmed" lineClamp={1}>{x.purpose}</Text> : null}</div> },
              { key: 'time', header: 'Arrived to left', render: (x) => <span className="num">{fmtTime(x.started_at)} to {x.ended_at ? fmtTime(x.ended_at) : <Badge variant="light" size="sm">now</Badge>}</span> },
              { key: 'dur', header: 'Duration', align: 'right', render: (x) => <span className="num">{minutesToHm(x.minutes)}</span> },
              { key: 'outcome', header: 'Outcome', render: (x) => <Text size="sm" c="dimmed" lineClamp={2}>{x.outcome ?? '—'}</Text> },
              { key: 'map', header: 'Location', render: (x) => <Group gap={8} wrap="nowrap">
                {x.start_lat !== null && x.start_lng !== null ? <Anchor size="sm" href={osmLink(x.start_lat, x.start_lng)} target="_blank" rel="noopener noreferrer">Arrival</Anchor> : <Text size="xs" c="dimmed">Not shared</Text>}
                {x.end_lat !== null && x.end_lng !== null ? <Anchor size="sm" href={osmLink(x.end_lat, x.end_lng)} target="_blank" rel="noopener noreferrer">Departure</Anchor> : null}
              </Group> },
            ]} />
        </>
      ) : (
        <>
          <Group justify="space-between">
            <StatStrip stats={[
              { key: 'n', label: 'Breaks', value: b.length },
              { key: 't', label: 'Total time', value: minutesToHm(b.reduce((s, x) => s + (x.minutes ?? 0), 0)) },
              { key: 'o', label: 'Over limit', value: b.filter((x) => x.over_limit).length, ...(b.some((x) => x.over_limit) ? { tone: 'warn' as const } : {}) },
              { key: 'now', label: 'On a break now', value: b.filter((x) => x.open).length },
            ]} />
          </Group>
          <SegmentedControl size="xs" w="fit-content" value={overOnly ? 'over' : 'all'} onChange={(x) => setOverOnly(x === 'over')} data={[{ value: 'all', label: 'All breaks' }, { value: 'over', label: 'Over limit only' }]} />
          <DataTable rows={b} rowKey={(x) => x.id} loading={breaks.loading} error={breaks.error} emptyTitle={overOnly ? 'No long breaks' : 'No breaks'}
            emptyHint="Breaks people start from Home appear here." highlight={(x) => x.over_limit}
            columns={[
              { key: 'emp', header: 'Name', sticky: true, render: (x) => <div><Text size="sm" fw={500}>{x.name_en}</Text><Text size="xs" c="dimmed">{x.emp_code}</Text></div> },
              ...(span === 'week' ? [{ key: 'day', header: 'Day', render: (x: typeof b[number]) => fmtDateShort(x.local_date) }] : []),
              { key: 'kind', header: 'Break', render: (x) => <Group gap={6}>{BREAK_LABEL[x.kind]}{x.paid ? null : <Badge size="xs" variant="light" color="gray">Unpaid</Badge>}</Group> },
              { key: 'time', header: 'Time', render: (x) => <span className="num">{fmtTime(x.started_at)} to {x.ended_at ? fmtTime(x.ended_at) : 'now'}</span> },
              { key: 'min', header: 'Minutes', align: 'right', render: (x) => <Text size="sm" className="num" {...(x.over_limit ? { c: 'red' } : {})}>{minutesToHm(x.minutes ?? 0)}</Text> },
              { key: 'limit', header: 'Limit', align: 'right', render: (x) => <Text size="sm" c="dimmed" className="num">{x.max_min} min</Text> },
            ]} />
        </>
      )}
      <Modal opened={!!photo} onClose={() => setPhoto(null)} centered radius="lg" size="lg" title={photo ? `${photo.name_en} at ${photo.place}, ${fmtTime(photo.started_at)} on ${fmtDateLong(photo.local_date)}` : ''}>
        {photo ? <img src={fieldApi.visitPhotoUrl(photo.id)} alt={photo.place} style={{ width: '100%', borderRadius: 12, display: 'block' }} /> : null}
      </Modal>
    </Stack>
  );
}
