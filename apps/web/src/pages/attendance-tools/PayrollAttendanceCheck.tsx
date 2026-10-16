import { Anchor, Group, Paper, Text } from '@mantine/core';
import { IconClipboardCheck } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { attendanceTools } from '../../api/attendanceTools';
import { useQuery } from '../../lib/useQuery';

/** Payroll pre-flight: open attendance issues of the month in numbers, with links to fix them and to the late penalties. */
export function PayrollAttendanceCheck({ bsYear, bsMonth }: { bsYear: number; bsMonth: number }) {
  const q = useQuery(() => attendanceTools.issueSummary(bsYear, bsMonth), [bsYear, bsMonth]);
  const s = q.data;
  if (!s) return null;
  const pending = s.pendingLeave + s.pendingTime;
  const clean = !s.missing && !s.absent && !pending;
  return (
    <Paper withBorder p="md">
      <Group justify="space-between" wrap="wrap" gap="sm">
        <Group gap={8} wrap="nowrap">
          <IconClipboardCheck size={16} stroke={1.6} color={clean ? 'var(--hj-ok)' : 'var(--hj-warn)'} />
          <Text size="sm">{clean ? 'No open attendance issues this month.' : `${s.employees} people have open attendance issues: ${s.missing} missing punch(es), ${s.absent} unexplained absence(s), ${pending} request(s) waiting.`}</Text>
        </Group>
        <Group gap="md">
          <Anchor component={Link} to="/attendance/issues" size="sm">Review and remind</Anchor>
          <Anchor component={Link} to="/attendance/penalties" size="sm">Late penalties</Anchor>
        </Group>
      </Group>
    </Paper>
  );
}
