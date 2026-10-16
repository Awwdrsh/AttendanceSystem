import { Alert, Anchor, Avatar, Badge, Button, Group, Modal, Paper, Stack, Text, Tooltip } from '@mantine/core';
import { IconClockExclamation } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { attendanceTools, OUT_COLOR, OUT_LABEL } from '../../api/attendanceTools';
import { SectionTitle } from '../../components/PageHeader';
import { bsMonthLabel, fmtDateShort, todayNpt } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const dayCount = (n: number) => `${n} ${n === 1 ? 'day' : 'days'}`;
const initials = (name: string) => name.split(/\s+/).filter(Boolean).map((w) => w[0]).join('').slice(0, 2).toUpperCase();

/** Home: who is away today and over the next week (leave, travel, from home, field). Leave types are never shown. */
export function WhosOutCard() {
  const q = useQuery(() => attendanceTools.whosOut(7), []);
  const days = q.data?.days ?? [];
  const today = todayNpt();
  const todayPeople = days.find((d) => d.date === today)?.people ?? [];
  const later = days.filter((d) => d.date !== today && d.people.length);
  if (q.loading || q.error || (!todayPeople.length && !later.length)) return null;
  const scope = q.data?.scope === 'company' ? 'the company' : q.data?.scope === 'department' ? 'your department' : 'your team';
  return (
    <div>
      <SectionTitle hint={`People in ${scope} who are away`}>Who's out</SectionTitle>
      <Paper withBorder radius="lg" p="md">
        <Stack gap="sm">
          <div>
            <Text size="xs" c="dimmed" fw={600} tt="uppercase" mb={6}>Today</Text>
            {todayPeople.length ? (
              <Group gap="xs">
                {todayPeople.map((p) => (
                  <Tooltip key={p.employeeId} label={`${OUT_LABEL[p.kind]}${p.detail ? `: ${p.detail}` : ''}`}>
                    <Badge size="lg" radius="xl" variant="light" color={OUT_COLOR[p.kind]} leftSection={<Avatar size={18} radius="xl" color={OUT_COLOR[p.kind]}>{initials(p.name)}</Avatar>}>
                      {p.name}
                    </Badge>
                  </Tooltip>
                ))}
              </Group>
            ) : <Text size="sm" c="dimmed">Everyone is in today.</Text>}
          </div>
          {later.length ? (
            <div>
              <Text size="xs" c="dimmed" fw={600} tt="uppercase" mb={6}>Next days</Text>
              <Stack gap={4}>
                {later.map((d) => (
                  <Group key={d.date} gap="xs" wrap="nowrap" align="flex-start">
                    <Text size="sm" c="dimmed" w={110} style={{ flexShrink: 0 }}>{fmtDateShort(d.date)}</Text>
                    <Text size="sm">{d.people.map((p) => `${p.name} (${OUT_LABEL[p.kind].toLowerCase()})`).join(', ')}</Text>
                  </Group>
                ))}
              </Stack>
            </div>
          ) : null}
        </Stack>
      </Paper>
    </div>
  );
}

/** Shown when a late clock-in was refused: explains why and offers the time request in one tap. */
export function LateClockInModal({ message, onClose }: { message: string | null; onClose: () => void }) {
  return (
    <Modal opened={!!message} onClose={onClose} centered radius="lg" title={<Group gap="xs"><IconClockExclamation size={18} stroke={1.6} /><Text fw={600}>Clock-in is closed</Text></Group>}>
      <Stack>
        <Text size="sm">{message}</Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Close</Button>
          <Button component={Link} to="/me/attendance?correct=1" onClick={onClose}>Raise a time request</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** My attendance: how many late days the month has under the company's late-arrival rule, and any penalty. */
export function LatePenaltyNote({ bsYear, bsMonth }: { bsYear: number; bsMonth: number }) {
  const q = useQuery(() => attendanceTools.myLate({ bsYear, bsMonth }), [bsYear, bsMonth]);
  const d = q.data;
  if (!d || !d.enabled || (!d.lateDays && !d.applied)) return null;
  const left = d.rule.lateCount - (d.lateDays % d.rule.lateCount);
  return (
    <Alert variant="light" color={d.applied || d.projectedDays ? 'orange' : 'gray'} icon={<IconClockExclamation size={16} stroke={1.6} />}
      title={`${d.lateDays} late ${d.lateDays === 1 ? 'day' : 'days'} in ${bsMonthLabel(bsYear, bsMonth)}`}>
      <Text size="sm">
        Every {d.rule.lateCount} late days in a month count as {dayCount(d.rule.penaltyDays)} of leave, or unpaid days if no leave is left.{' '}
        {d.applied
          ? `A penalty of ${dayCount(d.applied.penaltyDays)} was applied: ${d.applied.leaveDays} from leave and ${d.applied.unpaidDays} unpaid.`
          : d.projectedDays ? `So far this is ${dayCount(d.projectedDays)}; HR applies it before payroll.` : `${left} more late ${left === 1 ? 'day' : 'days'} this month would count as a penalty.`}
        {' '}<Anchor component={Link} to="/me/attendance?correct=1" size="sm">Raise a time request</Anchor> if a day was recorded wrongly.
      </Text>
    </Alert>
  );
}
