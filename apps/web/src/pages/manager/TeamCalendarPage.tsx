import { ActionIcon, Button, Divider, Group, Paper, ScrollArea, SegmentedControl, Stack, Text, Tooltip } from '@mantine/core';
import { IconChevronLeft, IconChevronRight } from '@tabler/icons-react';
import { Fragment, useState } from 'react';
import { leave as leaveApi, shifts as shiftApi } from '../../api';
import type { TeamCalendarDay } from '../../api/types';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { addDays, fmtAd, fmtDateLong, fmtDateShort, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

type Off = TeamCalendarDay['off'][number];
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const weekStartOf = (d: string) => addDays(d, -new Date(`${d}T00:00:00Z`).getUTCDay());

/** One teammate off on a day: first name with a dot; pending requests are outlined. */
function OffChip({ o }: { o: Off }) {
  const pending = o.status === 'pending';
  const color = o.type === 'LWP' ? 'var(--hj-idle)' : 'var(--hj-info)';
  return (
    <Tooltip label={`${o.nameEn}, ${o.type}${o.halfDay ? ' (half day)' : ''}${pending ? ', pending' : ''}`}>
      <Group gap={5} wrap="nowrap" px={6} h={20} style={{
        borderRadius: 4, fontSize: 12, lineHeight: '20px', minWidth: 0,
        background: pending ? 'transparent' : 'var(--mantine-color-blue-light)',
        border: pending ? '1px dashed var(--hj-line)' : '1px solid transparent',
      }}>
        <span className="hj-dot" style={{ background: color }} aria-hidden />
        <Text size="xs" truncate>{o.nameEn.split(' ')[0]}</Text>
      </Group>
    </Tooltip>
  );
}

/** Who is off over the next four weeks, as a calendar grid or a day-by-day list. */
export default function TeamCalendarPage() {
  const today = todayNpt();
  const [start, setStart] = useState(weekStartOf(today));
  const [view, setView] = useState<'grid' | 'list'>('grid');
  const end = addDays(start, 27);
  const q = useQuery(() => leaveApi.teamCalendar(start, end), [start]);
  const cal = useQuery(() => shiftApi.calendar(start, end), [start]);
  const offMap = new Map((q.data?.days ?? []).map((d) => [d.date, d.off]));
  const days: string[] = []; for (let i = 0; i < 28; i++) days.push(addDays(start, i));
  const hol = new Map((cal.data?.days ?? []).map((d) => [d.date, d]));
  const listDays = days.filter((d) => (offMap.get(d) ?? []).length > 0 || (hol.get(d)?.holidays.length ?? 0) > 0);

  return (
    <Stack gap="md">
      <PageHeader title="Team calendar" subtitle={`${fmtAd(start)} to ${fmtAd(end)}. See who is off before you approve leave.`}
        actions={<>
          <Group gap={4} wrap="nowrap">
            <Tooltip label="Previous 4 weeks"><ActionIcon variant="default" size={36} aria-label="Previous 4 weeks" onClick={() => setStart(addDays(start, -28))}><IconChevronLeft size={16} stroke={1.6} /></ActionIcon></Tooltip>
            <Button variant="default" onClick={() => setStart(weekStartOf(today))}>Today</Button>
            <Tooltip label="Next 4 weeks"><ActionIcon variant="default" size={36} aria-label="Next 4 weeks" onClick={() => setStart(addDays(start, 28))}><IconChevronRight size={16} stroke={1.6} /></ActionIcon></Tooltip>
          </Group>
          <SegmentedControl value={view} onChange={(v) => setView(v as 'grid' | 'list')} data={[{ value: 'grid', label: 'Calendar' }, { value: 'list', label: 'List' }]} />
        </>} />

      {q.error ? <EmptyState title="Could not load the team calendar" hint={`${q.error}. Refresh the page to try again.`} /> : view === 'grid' ? (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          <ScrollArea type="auto">
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(96px, 1fr))', minWidth: 700 }}>
              {WEEKDAYS.map((d) => <Text key={d} size="xs" c="dimmed" fw={500} px="sm" py={8} style={{ borderBottom: '1px solid var(--hj-line)' }}>{d}</Text>)}
              {days.map((d, i) => {
                const off = offMap.get(d) ?? []; const h = hol.get(d);
                const isToday = d === today;
                const dayNum = Number(d.slice(8, 10));
                return (
                  <div key={d} style={{
                    minHeight: 104, padding: 8, display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0,
                    borderLeft: i % 7 ? '1px solid var(--hj-line-soft)' : undefined,
                    borderTop: i >= 7 ? '1px solid var(--hj-line-soft)' : undefined,
                    background: h?.weeklyOff || h?.holidays.length ? 'var(--hj-canvas)' : undefined,
                  }}>
                    <Group gap={6} wrap="nowrap">
                      <Text size="xs" fw={isToday ? 600 : 500} className="num" c={isToday ? 'white' : 'inherit'}
                        style={isToday ? { background: 'var(--mantine-color-brand-6)', borderRadius: 999, minWidth: 22, height: 22, lineHeight: '22px', textAlign: 'center' } : {}}>
                        {i === 0 || dayNum === 1 ? fmtDateShort(d) : dayNum}
                      </Text>
                    </Group>
                    {h?.holidays.length ? <Text size="xs" c="dimmed" truncate title={h.holidays.map((x) => x.name_en).join(', ')}>{h.holidays[0]!.name_en}</Text> : null}
                    <Stack gap={3}>
                      {off.slice(0, 3).map((o) => <OffChip key={o.employeeId + o.type} o={o} />)}
                      {off.length > 3 ? <Tooltip label={off.slice(3).map((o) => o.nameEn).join(', ')}><Text size="xs" c="dimmed">+{off.length - 3} more</Text></Tooltip> : null}
                    </Stack>
                  </div>
                );
              })}
            </div>
          </ScrollArea>
        </Paper>
      ) : listDays.length === 0 ? (
        <EmptyState title="Everyone is in for these four weeks" hint="No approved or pending leave and no public holidays in this range." />
      ) : (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          {listDays.map((d, idx) => {
            const off = offMap.get(d) ?? []; const h = hol.get(d);
            return (
              <Fragment key={d}>
                {idx > 0 ? <Divider color="var(--hj-line-soft)" /> : null}
                <Group px="md" py="sm" align="flex-start" wrap="wrap" gap="md">
                  <div style={{ width: 150 }}>
                    <Text size="sm" fw={500}>{fmtDateLong(d)}</Text>
                    {d === today ? <Text size="xs" c="brand">Today</Text> : null}
                  </div>
                  <Stack gap={6} style={{ flex: 1, minWidth: 220 }}>
                    {h?.holidays.map((x) => <Text key={x.name_en} size="sm" c="dimmed">Public holiday: {x.name_en}</Text>)}
                    {off.map((o) => (
                      <Group key={o.employeeId + o.type} gap="sm" wrap="wrap">
                        <Text size="sm" fw={500}>{o.nameEn}</Text>
                        <Text size="xs" c="dimmed">{o.empCode}</Text>
                        <Text size="sm">{o.type}{o.halfDay ? ' (half day)' : ''}</Text>
                        <StatusBadge status={o.status} />
                      </Group>
                    ))}
                  </Stack>
                </Group>
              </Fragment>
            );
          })}
        </Paper>
      )}
      {view === 'grid' ? <Text size="xs" c="dimmed">Filled names are approved leave, outlined names are pending. Hover a name for the leave type. Shaded days are weekly offs or holidays.</Text> : null}
    </Stack>
  );
}
