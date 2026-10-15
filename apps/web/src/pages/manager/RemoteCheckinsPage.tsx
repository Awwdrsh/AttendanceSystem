import { Badge, Box, Group, Modal, Paper, SegmentedControl, SimpleGrid, Stack, Text, UnstyledButton } from '@mantine/core';
import { IconCameraOff, IconHome, IconMapPin, IconMapPinOff, IconPhotoX } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { punch as punchApi } from '../../api';
import type { RemotePunch } from '../../api/types';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtDateLong, fmtTime, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 14, stroke: 1.6 } as const;
const dirLabel = (d: string) => (d === 'in' ? 'Clocked in' : d === 'out' ? 'Clocked out' : 'Punch');

/** One clock-in from home: the photo large enough to recognise someone, with who, when and whether a location came with it. */
function CheckinCard({ p, onOpen }: { p: RemotePunch; onOpen: (p: RemotePunch) => void }) {
  return (
    <Paper withBorder radius="lg" style={{ overflow: 'hidden' }}>
      <UnstyledButton w="100%" disabled={!p.photo_id || !!p.photo_purged} onClick={() => onOpen(p)} aria-label={`Open photo of ${p.name_en}`}>
        <Box style={{ aspectRatio: '4 / 3', background: 'var(--hj-surface-2, #eef1ee)', display: 'grid', placeItems: 'center' }}>
          {p.photo_id && !p.photo_purged
            ? <img src={punchApi.photoUrl(p.photo_id)} alt={`${p.name_en} at ${fmtTime(p.punched_at)}`} loading="lazy" style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
            : <Stack gap={4} align="center">{p.photo_purged ? <IconPhotoX size={22} stroke={1.5} color="var(--hj-muted)" /> : <IconCameraOff size={22} stroke={1.5} color="var(--hj-muted)" />}
                <Text size="xs" c="dimmed">{p.photo_purged ? 'Deleted after retention period' : 'No photo'}</Text></Stack>}
        </Box>
      </UnstyledButton>
      <Stack gap={6} p="sm">
        <Group justify="space-between" wrap="nowrap" gap="xs">
          <div style={{ minWidth: 0 }}>
            <Text size="sm" fw={600} truncate>{p.name_en}</Text>
            <Text size="xs" c="dimmed" truncate>{p.emp_code}{p.designation ? ` · ${p.designation}` : ''}</Text>
          </div>
          <Badge variant="light" color={p.direction === 'in' ? 'brand' : 'gray'} style={{ flex: 'none' }}>{dirLabel(p.direction)} {fmtTime(p.punched_at)}</Badge>
        </Group>
        <Group gap={6} wrap="nowrap">
          {p.location_shared ? <IconMapPin {...ICON} color="var(--hj-ok)" /> : <IconMapPinOff {...ICON} color="var(--hj-warn)" />}
          <Text size="xs" c="dimmed">{p.location_shared ? `Location shared${p.accuracy_m ? `, within ${Math.round(p.accuracy_m)} m` : ''}` : 'No location shared'}</Text>
        </Group>
        {p.reason ? <Text size="xs" c="dimmed" lineClamp={2}>“{p.reason}”</Text> : null}
      </Stack>
    </Paper>
  );
}

/** Work-from-home check-ins with photos. Managers see their team, HR the whole company. */
export default function RemoteCheckinsPage() {
  const [date, setDate] = useState<string | null>(todayNpt());
  const [span, setSpan] = useState<'day' | 'week'>('day');
  const [dir, setDir] = useState<'in' | 'all'>('in');
  const [open, setOpen] = useState<RemotePunch | null>(null);
  const to = date ?? todayNpt();
  const from = span === 'week' ? addDays(to, -6) : to;
  const q = useQuery(() => punchApi.remote({ from, to }), [from, to]);
  const all = q.data?.punches ?? [];
  const rows = dir === 'in' ? all.filter((p) => p.direction === 'in') : all;
  const byDay = useMemo(() => {
    const m = new Map<string, RemotePunch[]>();
    for (const p of rows) m.set(p.local_date, [...(m.get(p.local_date) ?? []), p]);
    return [...m.entries()];
  }, [rows]);
  const people = new Set(all.map((p) => p.employee_id)).size;
  const withPhoto = all.filter((p) => p.direction === 'in' && p.photo_id && !p.photo_purged).length;
  const noLocation = all.filter((p) => p.direction === 'in' && !p.location_shared).length;

  return (
    <Stack gap="lg">
      <SectionTitle hint="Everyone who clocked in from home, with the photo taken at clock-in. Photos are encrypted and deleted automatically after the retention period."
        actions={<Group gap="xs" wrap="wrap">
          <SegmentedControl size="xs" value={dir} onChange={(v) => setDir(v as 'in' | 'all')} data={[{ value: 'in', label: 'Clock-ins' }, { value: 'all', label: 'Ins and outs' }]} />
          <SegmentedControl size="xs" value={span} onChange={(v) => setSpan(v as 'day' | 'week')} data={[{ value: 'day', label: 'Day' }, { value: 'week', label: '7 days' }]} />
          <DateField value={date} onChange={setDate} maxDate={todayNpt()} w={190} />
        </Group>}>Work from home</SectionTitle>
      <StatStrip stats={[
        { key: 'people', label: 'People from home', value: q.loading && !all.length ? '—' : people },
        { key: 'photo', label: 'Clock-ins with photo', value: q.loading && !all.length ? '—' : withPhoto },
        { key: 'noloc', label: 'Without location', value: q.loading && !all.length ? '—' : noLocation },
      ]} />
      {!q.loading && rows.length === 0 ? (
        <EmptyState icon={<IconHome size={20} stroke={1.6} />} title={span === 'day' ? 'Nobody clocked in from home' : 'No home clock-ins this week'}
          hint="When someone clocks in from home their photo appears here. Pick another day to look back." />
      ) : byDay.map(([day, list]) => (
        <Stack key={day} gap="sm">
          {span === 'week' ? <Text size="sm" fw={600}>{fmtDateLong(day)} <Text span size="sm" c="dimmed" fw={400}>· {list.length}</Text></Text> : null}
          <SimpleGrid cols={{ base: 1, xs: 2, sm: 3, lg: 4 }} spacing="md">
            {list.map((p) => <CheckinCard key={p.punch_id} p={p} onOpen={setOpen} />)}
          </SimpleGrid>
        </Stack>
      ))}
      <Modal opened={!!open} onClose={() => setOpen(null)} centered radius="lg" size="lg"
        title={open ? `${open.name_en}, ${dirLabel(open.direction).toLowerCase()} ${fmtTime(open.punched_at)} on ${fmtDateLong(open.local_date)}` : ''}>
        {open?.photo_id ? <img src={punchApi.photoUrl(open.photo_id)} alt={open.name_en} style={{ width: '100%', borderRadius: 12, display: 'block' }} /> : null}
      </Modal>
    </Stack>
  );
}
