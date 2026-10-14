import { Avatar, Button, Group, Paper, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconKey } from '@tabler/icons-react';
import { Link } from 'react-router-dom';
import { employees as empApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { Field } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { fmtAd } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const ROLE: Record<string, string> = { admin: 'Admin', hr: 'HR', manager: 'Manager', employee: 'Employee' };
const REGIME: Record<string, string> = { ssf: 'Social Security Fund (SSF)', pf: 'Provident Fund (PF)', none: 'None' };

function initials(name: string | undefined): string {
  const parts = (name ?? '').trim().split(/\s+/).filter(Boolean);
  return ((parts[0]?.[0] ?? '') + (parts.length > 1 ? parts[parts.length - 1]![0] : '')).toUpperCase() || '?';
}

export default function ProfilePage() {
  const { user } = useAuth();
  const emp = useQuery(() => empApi.get(user!.employeeId!), [user?.employeeId], { enabled: !!user?.employeeId });
  const e = emp.data?.employee;
  return (
    <Stack gap="lg">
      <PageHeader title="Profile" subtitle="Your account and employment details. Ask HR if anything here is wrong." />

      <Paper withBorder p="lg">
        <Group gap="md" wrap="nowrap" mb="lg">
          <Avatar color="brand" radius="xl" size={48}>{initials(user?.name)}</Avatar>
          <div style={{ minWidth: 0 }}>
            <Text fw={600} size="lg" truncate>{user?.name}</Text>
            <Text size="sm" c="dimmed" truncate>{[e?.designation, user?.email].filter(Boolean).join(', ')}</Text>
          </div>
        </Group>
        <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} spacing="lg">
          <Field label="Email">{user?.email || '—'}</Field>
          <Field label="Roles">{user?.roles.map((r) => ROLE[r] ?? r).join(', ') || '—'}</Field>
          {e ? <Field label="Employee code">{e.emp_code}</Field> : null}
          {e ? <Field label="Designation">{e.designation || '—'}</Field> : null}
          {e ? <Field label="Joined">{fmtAd(e.join_date)}</Field> : null}
          {e ? <Field label="Retirement scheme">{REGIME[e.regime] ?? e.regime}</Field> : null}
          {e ? <Field label="Bank account">{e.bank_code ? `${e.bank_code}, ending ${e.bank_account_last4 ?? '—'}` : '—'}</Field> : null}
        </SimpleGrid>
      </Paper>

      <div>
        <SectionTitle>Security</SectionTitle>
        <Paper withBorder p="lg">
          <Group justify="space-between" wrap="wrap" gap="sm">
            <div>
              <Text size="sm" fw={500}>Password</Text>
              <Text size="sm" c="dimmed">Changing it signs you out on every device.</Text>
            </div>
            <Button component={Link} to="/change-password" variant="default" leftSection={<IconKey size={16} stroke={1.6} />}>Change password</Button>
          </Group>
        </Paper>
      </div>
    </Stack>
  );
}
