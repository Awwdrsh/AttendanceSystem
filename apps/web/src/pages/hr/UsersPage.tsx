import { Avatar, Badge, Button, Drawer, Group, Modal, MultiSelect, PasswordInput, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { auth as authApi } from '../../api';
import { security } from '../../api/security';
import type { AppUserRow, Role } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { ROLE_HELP, ROLE_LABEL } from '../../lib/access';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ROLES: Role[] = ['admin', 'hr', 'manager', 'employee', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive'];
const ROLE_OPTIONS = ROLES.map((r) => ({ value: r, label: ROLE_LABEL[r] }));

export default function UsersPage() {
  const { hasRole, user: me } = useAuth();
  const [resetFor, setResetFor] = useState<AppUserRow | null>(null); const [resetReason, setResetReason] = useState('');
  const q = useQuery(() => authApi.users(), []);
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState(''); const [name, setName] = useState(''); const [password, setPassword] = useState(''); const [roles, setRoles] = useState<string[]>(['employee']); const [employeeId, setEmployeeId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isAdmin = hasRole('admin');
  return (
    <Stack gap="md">
      <PageHeader title="Users & roles"
        subtitle="Who can sign in and what they can do. Employees see only their own data, managers approve for their team, HR runs attendance, leave and payroll."
        actions={isAdmin ? <Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setOpen(true)}>Add user</Button> : null} />
      <DataTable<AppUserRow> rows={q.data?.users ?? []} rowKey={(u) => u.id} loading={q.loading} error={q.error} emptyTitle="No users yet" {...(isAdmin ? { emptyHint: 'Add a user so they can sign in.' } : {})}
        columns={[
          { key: 'name', header: 'Name', render: (u) => (
            <Group gap="sm" wrap="nowrap">
              <Avatar name={u.name} size={32} radius="xl" color="brand" variant="light" />
              <div style={{ minWidth: 0 }}>
                <Text size="sm" fw={500} truncate>{u.name}</Text>
                <Text size="xs" c="dimmed" truncate>{u.email}</Text>
              </div>
            </Group>
          ) },
          { key: 'roles', header: 'Roles', width: 280, render: (u) => isAdmin
            ? <MultiSelect size="xs" data={ROLE_OPTIONS} value={u.roles} aria-label={`Roles for ${u.name}`} onChange={async (v) => { if (!v.length) { toast.info('A user needs at least one role. Disable the user instead.'); return; } try { await authApi.updateUser(u.id, { roles: v as Role[] }); await q.reload(); } catch (e) { toast.err(e); } }} />
            : <Text size="sm">{u.roles.map((r) => ROLE_LABEL[r as Role] ?? r).join(', ')}</Text> },
          { key: 'emp', header: 'Employee record', render: (u) => u.employee_id ? <Text size="sm">Linked</Text> : <Text size="sm" c="dimmed">Not linked</Text> },
          { key: 'last', header: 'Last sign-in', render: (u) => <Text size="sm" c={u.last_login_at ? 'inherit' : 'dimmed'}>{u.last_login_at ? relativeTime(u.last_login_at) : 'Never'}</Text> },
          { key: 'two', header: 'Two-step', render: (u) => (
            <Group gap={6} wrap="nowrap">
              {u.two_factor ? <Badge size="sm" color="teal" variant="light">On</Badge> : <Text size="sm" c="dimmed">Off</Text>}
              {isAdmin && u.two_factor && u.id !== me?.id ? <Button size="compact-xs" variant="subtle" onClick={() => { setResetFor(u); setResetReason(''); }}>Reset</Button> : null}
            </Group>) },
          { key: 'status', header: 'Status', render: (u) => <StatusBadge status={u.status} /> },
          { key: 'a', header: '', align: 'right', render: (u) => isAdmin ? <Button size="xs" variant="subtle" color={u.status === 'active' ? 'red' : 'gray'} onClick={async () => { try { await authApi.updateUser(u.id, { status: u.status === 'active' ? 'disabled' : 'active' }); await q.reload(); } catch (e) { toast.err(e); } }}>{u.status === 'active' ? 'Disable' : 'Activate'}</Button> : null },
        ]} />
      <Modal opened={!!resetFor} onClose={() => setResetFor(null)} title={<Text fw={600}>Reset two-step sign-in</Text>} centered>
        <Stack>
          <Text size="sm" c="dimmed">For a lost or replaced phone. {resetFor?.name} is signed out everywhere and signs in with their password only until they set two-step sign-in up again (straight away if their role requires it).</Text>
          <TextInput label="Reason" description="Kept in the audit log." value={resetReason} onChange={(e) => setResetReason(e.currentTarget.value)} placeholder="Lost phone" />
          <Group justify="flex-end">
            <Button color="red" disabled={resetReason.trim().length < 3} onClick={async () => {
              if (!resetFor) return;
              try { await security.resetUser(resetFor.id, resetReason.trim()); toast.ok(`Two-step sign-in reset for ${resetFor.name}.`); setResetFor(null); await q.reload(); } catch (e) { toast.err(e); }
            }}>Reset</Button>
          </Group>
        </Stack>
      </Modal>
      <Drawer opened={open} onClose={() => setOpen(false)} title="Add user" position="right" size="md">
        <Stack gap="md">
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label="Full name" value={name} onChange={(e) => setName(e.currentTarget.value)} required />
            <TextInput label="Email" type="email" value={email} onChange={(e) => setEmail(e.currentTarget.value)} required />
          </SimpleGrid>
          <PasswordInput label="Temporary password" description="At least 12 characters. They will be asked to change it at first sign-in." value={password} onChange={(e) => setPassword(e.currentTarget.value)} required
            {...(password.length > 0 && password.length < 12 ? { error: `${12 - password.length} more ${12 - password.length === 1 ? 'character' : 'characters'} needed` } : {})} />
          <MultiSelect label="Roles" description="Combine roles if someone does more than one job." data={ROLE_OPTIONS} value={roles} onChange={setRoles} />
          {roles.map((r) => <Text key={r} size="xs" c="dimmed">{ROLE_LABEL[r as Role]}: {ROLE_HELP[r as Role]}</Text>)}
          <EmployeePicker label="Employee record" description="Optional. Links this sign-in to an employee so they can see their own attendance and payslips." value={employeeId} onChange={setEmployeeId} />
          <Group justify="flex-end" gap="xs">
            <Button variant="default" onClick={() => setOpen(false)}>Cancel</Button>
            <Button loading={busy} disabled={!email || !name || password.length < 12 || !roles.length} onClick={async () => { setBusy(true); try { await authApi.createUser({ email, name, password, roles: roles as Role[], ...(employeeId ? { employeeId } : {}) }); toast.ok('User added.'); setOpen(false); setEmail(''); setName(''); setPassword(''); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Add user</Button>
          </Group>
        </Stack>
      </Drawer>
    </Stack>
  );
}
