import { Button, Group, Modal, MultiSelect, PasswordInput, Stack, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { auth as authApi } from '../../api';
import type { AppUserRow, Role } from '../../api/types';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { PageHeader } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { useI18n } from '../../i18n';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ROLES: Role[] = ['admin', 'hr', 'finance', 'manager', 'employee'];

export default function UsersPage() {
  const { t } = useI18n();
  const { hasRole } = useAuth();
  const q = useQuery(() => authApi.users(), []);
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState(''); const [name, setName] = useState(''); const [password, setPassword] = useState(''); const [roles, setRoles] = useState<string[]>(['employee']); const [employeeId, setEmployeeId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isAdmin = hasRole('admin');
  return (
    <Stack>
      <PageHeader title={t('users')} subtitle="Admins manage sign-ins and roles. Employees see only their own data; managers approve for their team; HR runs attendance; finance approves payroll." actions={isAdmin ? <Button onClick={() => setOpen(true)}>{t('create')}</Button> : null} />
      <DataTable<AppUserRow> rows={q.data?.users ?? []} rowKey={(u) => u.id} loading={q.loading} error={q.error} emptyTitle="No users."
        columns={[
          { key: 'name', header: t('name'), render: (u) => <><Text size="sm" fw={500}>{u.name}</Text><Text size="xs" c="dimmed">{u.email}</Text></> },
          { key: 'roles', header: 'Roles', render: (u) => isAdmin ? <MultiSelect size="xs" data={ROLES} value={u.roles} onChange={async (v) => { if (!v.length) return; try { await authApi.updateUser(u.id, { roles: v as Role[] }); await q.reload(); } catch (e) { toast.err(e); } }} /> : u.roles.join(', ') },
          { key: 'emp', header: 'Employee', render: (u) => u.employee_id ? 'linked' : '—' },
          { key: 'last', header: 'Last sign-in', render: (u) => relativeTime(u.last_login_at) },
          { key: 'status', header: t('status'), render: (u) => <StatusBadge status={u.status} /> },
          { key: 'a', header: '', render: (u) => isAdmin ? <Button size="xs" variant="subtle" color={u.status === 'active' ? 'red' : 'teal'} onClick={async () => { try { await authApi.updateUser(u.id, { status: u.status === 'active' ? 'disabled' : 'active' }); await q.reload(); } catch (e) { toast.err(e); } }}>{u.status === 'active' ? t('disable') : t('activate')}</Button> : null },
        ]} />
      <Modal opened={open} onClose={() => setOpen(false)} title="New user" centered>
        <Stack>
          <TextInput label={t('email')} value={email} onChange={(e) => setEmail(e.currentTarget.value)} required />
          <TextInput label={t('name')} value={name} onChange={(e) => setName(e.currentTarget.value)} required />
          <PasswordInput label="Temporary password" description="At least 12 characters; the user must change it at first sign-in." value={password} onChange={(e) => setPassword(e.currentTarget.value)} required />
          <MultiSelect label="Roles" data={ROLES} value={roles} onChange={setRoles} />
          <EmployeePicker label="Link to employee (optional)" value={employeeId} onChange={setEmployeeId} />
          <Group justify="flex-end"><Button loading={busy} disabled={!email || !name || password.length < 12 || !roles.length} onClick={async () => { setBusy(true); try { await authApi.createUser({ email, name, password, roles: roles as Role[], ...(employeeId ? { employeeId } : {}) }); toast.ok('User created.'); setOpen(false); setEmail(''); setName(''); setPassword(''); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('create')}</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
