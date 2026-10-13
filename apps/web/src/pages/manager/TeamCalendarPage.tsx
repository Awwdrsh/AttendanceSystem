import { Badge, Button, Group, Paper, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { leave as leaveApi, shifts as shiftApi } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { addDays, dualDate, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

/** Who is off in the next weeks: one row per day with initials (plan §13.5 "teammates' initials on days they are off"). */
export default function TeamCalendarPage() {
  const { t, lang, numerals } = useI18n();
  const [start, setStart] = useState(addDays(todayNpt(), -((new Date(todayNpt()).getUTCDay()))));
  const end = addDays(start, 27);
  const q = useQuery(() => leaveApi.teamCalendar(start, end), [start]);
  const cal = useQuery(() => shiftApi.calendar(start, end), [start]);
  const offMap = new Map((q.data?.days ?? []).map((d) => [d.date, d.off]));
  const days: string[] = []; for (let i = 0; i < 28; i++) days.push(addDays(start, i));
  const hol = new Map((cal.data?.days ?? []).map((d) => [d.date, d]));
  return (
    <Stack>
      <PageHeader title={t('teamCalendar')} actions={<Group gap="xs"><Button variant="default" onClick={() => setStart(addDays(start, -28))}>‹ 4 {t('week')}</Button><Button variant="default" onClick={() => setStart(addDays(todayNpt(), -new Date(todayNpt()).getUTCDay()))}>{t('today')}</Button><Button variant="default" onClick={() => setStart(addDays(start, 28))}>4 {t('week')} ›</Button></Group>} />
      <Paper withBorder p="xs" radius="md">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 4 }}>
          {['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'].map((d, i) => <Text key={d} ta="center" size="xs" fw={600} c={i === 6 ? 'red' : 'dimmed'}>{d}</Text>)}
          {days.map((d) => {
            const off = offMap.get(d) ?? []; const h = hol.get(d);
            return (
              <div key={d} style={{ minHeight: 84, border: '1px solid var(--mantine-color-default-border)', borderRadius: 8, padding: 4, background: d === todayNpt() ? 'var(--mantine-color-teal-light)' : h?.weeklyOff ? 'var(--mantine-color-gray-light)' : undefined }}>
                <Text size="xs" fw={600}>{dualDate(d, lang, numerals).bs.slice(5)} <Text span c="dimmed">{d.slice(5)}</Text></Text>
                {h?.holidays.length ? <Text size="xs" c="dimmed">★ {h.holidays[0]!.name_en}</Text> : null}
                <Group gap={2} mt={2}>{off.map((o) => <Badge key={o.employeeId + o.type} size="xs" variant={o.status === 'pending' ? 'outline' : 'light'} color={o.type === 'LWP' ? 'gray' : 'blue'} title={`${o.nameEn} · ${o.type} (${o.status})`}>{o.nameEn.split(' ').map((w) => w[0]).join('').slice(0, 3)}</Badge>)}</Group>
              </div>
            );
          })}
        </div>
      </Paper>
      <Text size="xs" c="dimmed">{lang === 'ne' ? 'ठोस = स्वीकृत, रेखा = विचाराधीन' : 'Solid = approved, outlined = pending. Hover for the name and type.'}</Text>
    </Stack>
  );
}
