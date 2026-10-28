import { ActionIcon, Button, Divider, Group, Paper, ScrollArea, SegmentedControl, Stack, Text, Tooltip } from '@mantine/core';
import { IconChevronLeft, IconChevronRight } from '@tabler/icons-react';
import { Fragment, useState } from 'react';
import { leave as leaveApi, shifts as shiftApi } from '../../api';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { BS_MONTH_NAMES_EN } from '@/lib/bs-calendar';
import { adSpanLabel } from '../../components/BsDatePicker';
import { bsMonthGrid, nextMonth, prevMonth, safe } from '../../lib/bs';
import { fmtDateLong, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const AD_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** One teammate off on a day: first name with a dot; pending requests are outlined. */
function OffChip({ o }) {
  const pending = o.status === 'pending';
  const color = o.type === 'LWP' ? 'var(--hj-idle)' : 'var(--hj-info)';
  return (
    <Tooltip label={`${o.nameEn}, ${o.type}${o.halfDay ? ' (half day)' : ''}${pending ? ', pending' : ''}`}>
      <Group
        gap={5}
        wrap="nowrap"
        px={6}
        h={20}
        style={{
          borderRadius: 4,
          fontSize: 12,
          lineHeight: '20px',
          minWidth: 0,
          background: pending ? 'transparent' : 'var(--mantine-color-blue-light)',
          border: pending ? '1px dashed var(--hj-line)' : '1px solid transparent',
        }}
      >
        <span className="hj-dot" style={{ background: color }} aria-hidden />
        <Text size="xs" truncate>
          {o.nameEn.split(' ')[0]}
        </Text>
      </Group>
    </Tooltip>
  );
}

/** Who is off in a Nepali (BS) month, as a calendar grid or a day-by-day list. */
export default function TeamCalendarPage() {
  const today = todayNpt();
  const nowBs = safe(today) ?? { year: 2083, month: 1, day: 1 };
  const [month, setMonth] = useState({ year: nowBs.year, month: nowBs.month });
  const [view, setView] = useState('grid');
  const cells = bsMonthGrid(month.year, month.month);
  const start = cells[0].ad;
  const end = cells[cells.length - 1].ad;
  const inMonth = cells.filter((c) => c.inMonth);
  const q = useQuery(() => leaveApi.teamCalendar(start, end), [start]);
  const cal = useQuery(() => shiftApi.calendar(start, end), [start]);
  const offMap = new Map((q.data?.days ?? []).map((d) => [d.date, d.off]));
  const days = cells.map((c) => c.ad);
  const cellOf = new Map(cells.map((c) => [c.ad, c]));
  const hol = new Map((cal.data?.days ?? []).map((d) => [d.date, d]));
  const listDays = days
    .filter((d) => cellOf.get(d)?.inMonth)
    .filter((d) => (offMap.get(d) ?? []).length > 0 || (hol.get(d)?.holidays.length ?? 0) > 0);

  return (
    <Stack gap="md">
      <PageHeader
        title="Team calendar"
        subtitle="See who is off before you approve leave."
        actions={
          <>
            <Group gap={4} wrap="nowrap">
              <Tooltip label="Previous month">
                <ActionIcon
                  variant="default"
                  size={36}
                  aria-label="Previous month"
                  onClick={() => setMonth(prevMonth(month.year, month.month))}
                >
                  <IconChevronLeft size={16} stroke={1.6} />
                </ActionIcon>
              </Tooltip>
              <Button variant="default" onClick={() => setMonth({ year: nowBs.year, month: nowBs.month })}>
                This month
              </Button>
              <Tooltip label="Next month">
                <ActionIcon
                  variant="default"
                  size={36}
                  aria-label="Next month"
                  onClick={() => setMonth(nextMonth(month.year, month.month))}
                >
                  <IconChevronRight size={16} stroke={1.6} />
                </ActionIcon>
              </Tooltip>
            </Group>
            <SegmentedControl
              value={view}
              onChange={(v) => setView(v)}
              data={[
                { value: 'grid', label: 'Calendar' },
                { value: 'list', label: 'List' },
              ]}
            />
          </>
        }
      />

      <Group justify="space-between" align="flex-end">
        <div>
          <Text fz={24} fw={700} lh={1.1} style={{ letterSpacing: '-0.02em' }}>
            {BS_MONTH_NAMES_EN[month.month - 1]}{' '}
            <Text span inherit c="var(--hj-hydro)">
              {month.year}
            </Text>
          </Text>
          <Text className="mono" fz={11} c="dimmed" mt={4} style={{ letterSpacing: '0.08em', textTransform: 'uppercase' }}>
            {inMonth.length ? `${adSpanLabel(inMonth[0].ad, inMonth[inMonth.length - 1].ad)}, ${inMonth.length} days` : ''}
          </Text>
        </div>
      </Group>
      {q.error ? (
        <EmptyState title="Could not load the team calendar" hint={`${q.error}. Refresh the page to try again.`} />
      ) : view === 'grid' ? (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          <ScrollArea type="auto">
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, minmax(96px, 1fr))', minWidth: 700 }}>
              {WEEKDAYS.map((d, i) => (
                <Text
                  key={d}
                  className="mono"
                  fz={10.5}
                  c={i === 6 ? 'var(--hj-bad)' : 'dimmed'}
                  fw={500}
                  px="sm"
                  py={8}
                  style={{
                    borderBottom: '1px solid var(--hj-line)',
                    letterSpacing: '0.08em',
                    textTransform: 'uppercase',
                    background: 'var(--hj-raised)',
                  }}
                >
                  {d}
                </Text>
              ))}
              {days.map((d, i) => {
                const off = offMap.get(d) ?? [];
                const h = hol.get(d);
                const isToday = d === today;
                const c = cellOf.get(d);
                const adDay = Number(d.slice(8, 10));
                return (
                  <div
                    key={d}
                    style={{
                      minHeight: 104,
                      padding: 8,
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 4,
                      minWidth: 0,
                      borderLeft: i % 7 ? '1px solid var(--hj-line-soft)' : undefined,
                      borderTop: i >= 7 ? '1px solid var(--hj-line-soft)' : undefined,
                      background: h?.holidays.length ? 'rgba(180, 83, 9, 0.05)' : c.saturday ? 'rgba(220, 38, 38, 0.035)' : undefined,
                      opacity: c.inMonth ? 1 : 0.45,
                    }}
                  >
                    <Group gap={6} wrap="nowrap" justify="space-between">
                      <Text
                        fz={17}
                        fw={isToday ? 800 : 650}
                        lh={1}
                        className="num"
                        c={isToday ? 'var(--hj-on-hydro)' : c.saturday || h?.holidays.length ? 'var(--hj-bad)' : 'inherit'}
                        style={
                          isToday
                            ? {
                                background: 'var(--hj-hydro)',
                                borderRadius: 6,
                                minWidth: 28,
                                height: 26,
                                lineHeight: '26px',
                                textAlign: 'center',
                              }
                            : { paddingLeft: 2 }
                        }
                      >
                        {c.bs.day}
                      </Text>
                      <Text className="mono" fz={10} c="dimmed">
                        {adDay === 1 || i === 0 ? `${AD_MONTHS[Number(d.slice(5, 7)) - 1]} ${adDay}` : adDay}
                      </Text>
                    </Group>
                    {h?.holidays.length ? (
                      <Text size="xs" c="dimmed" truncate title={h.holidays.map((x) => x.name_en).join(', ')}>
                        {h.holidays[0].name_en}
                      </Text>
                    ) : null}
                    <Stack gap={3}>
                      {off.slice(0, 3).map((o) => (
                        <OffChip key={o.employeeId + o.type} o={o} />
                      ))}
                      {off.length > 3 ? (
                        <Tooltip
                          label={off
                            .slice(3)
                            .map((o) => o.nameEn)
                            .join(', ')}
                        >
                          <Text size="xs" c="dimmed">
                            +{off.length - 3} more
                          </Text>
                        </Tooltip>
                      ) : null}
                    </Stack>
                  </div>
                );
              })}
            </div>
          </ScrollArea>
        </Paper>
      ) : listDays.length === 0 ? (
        <EmptyState title="Everyone is in this month" hint="No approved or pending leave and no public holidays in this month." />
      ) : (
        <Paper withBorder style={{ overflow: 'hidden' }}>
          {listDays.map((d, idx) => {
            const off = offMap.get(d) ?? [];
            const h = hol.get(d);
            return (
              <Fragment key={d}>
                {idx > 0 ? <Divider color="var(--hj-line-soft)" /> : null}
                <Group px="md" py="sm" align="flex-start" wrap="wrap" gap="md">
                  <div style={{ width: 150 }}>
                    <Text size="sm" fw={500}>
                      {fmtDateLong(d)}
                    </Text>
                    {d === today ? (
                      <Text size="xs" c="brand">
                        Today
                      </Text>
                    ) : null}
                  </div>
                  <Stack gap={6} style={{ flex: 1, minWidth: 220 }}>
                    {h?.holidays.map((x) => (
                      <Text key={x.name_en} size="sm" c="dimmed">
                        Public holiday: {x.name_en}
                      </Text>
                    ))}
                    {off.map((o) => (
                      <Group key={o.employeeId + o.type} gap="sm" wrap="wrap">
                        <Text size="sm" fw={500}>
                          {o.nameEn}
                        </Text>
                        <Text size="xs" c="dimmed">
                          {o.empCode}
                        </Text>
                        <Text size="sm">
                          {o.type}
                          {o.halfDay ? ' (half day)' : ''}
                        </Text>
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
      {view === 'grid' ? (
        <Text size="xs" c="dimmed">
          Big numbers are Nepali dates, small ones Gregorian. Saturdays and public holidays are in red. Filled names are approved leave,
          outlined names are pending.
        </Text>
      ) : null}
    </Stack>
  );
}
