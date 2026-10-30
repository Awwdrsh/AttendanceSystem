import { Badge, Button, Checkbox, Group, Modal, MultiSelect, PasswordInput, Select, Stack, Text, TextInput } from '@mantine/core';
import { IconKey, IconPencil, IconTrash, IconUserPlus } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { group as groupApi } from '../../api/group';
import { useAuth } from '../../auth/AuthProvider';
import { useCompanies } from '../../auth/CompaniesProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { ROLE_HELP, ROLE_LABEL } from '../../lib/access';
import { accessByPerson, GROUP_ROLE_HELP, GROUP_ROLE_LABEL } from '../../lib/group';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ROLES = ['admin', 'hr', 'manager', 'employee', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive'];
const ROLE_OPTIONS = ROLES.map((r) => ({ value: r, label: ROLE_LABEL[r] }));
const GROUP_ROLES = ['group_admin', 'group_hr', 'group_executive'];

/** The employee select of a company: active employees no account works as yet (plus the current link). */
function EmployeeSelect({ companyId, keep, value, onChange }) {
  const q = useQuery(
    () => (companyId ? groupApi.unlinkedEmployees(companyId, keep ?? undefined) : Promise.resolve({ items: [] })),
    [companyId, keep],
  );
  return (
    <Select
      label="Employee record"
      description="Who this person is in that company (for self-service). Leave empty for an account without one."
      placeholder={companyId ? 'No employee record' : 'Choose a company first'}
      searchable
      clearable
      disabled={!companyId}
      data={(q.data?.items ?? []).map((e) => ({ value: e.id, label: `${e.name} (${e.code})` }))}
      value={value}
      onChange={onChange}
      nothingFoundMessage="No unlinked employee matches"
    />
  );
}

/** Give or change a person's roles and employee record in one company of the group, or invite a new person. */
function AccessForm({ opened, onClose, onSaved, companies, initial, invite }) {
  const [v, setV] = useState({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opened)
      setV({
        email: initial?.email ?? '',
        name: '',
        password: '',
        companyId: initial?.companyId ?? null,
        roles: initial?.roles ?? ['employee'],
        employeeId: initial?.employeeId ?? null,
      });
  }, [opened, initial]);
  const set = (k) => (x) => setV((s) => ({ ...s, [k]: x?.currentTarget ? x.currentTarget.value : x }));
  const editing = !!initial;
  const save = async () => {
    setBusy(true);
    try {
      const body = { email: v.email.trim(), companyId: v.companyId, roles: v.roles, employeeId: v.employeeId || null };
      if (invite) {
        await groupApi.invite({ ...body, name: v.name.trim(), password: v.password });
        toast.ok(`${v.name} can sign in with the temporary password and must change it first.`, 'Account created');
      } else {
        await groupApi.grantAccess(body);
        toast.ok(editing ? 'Access updated.' : 'Access given.');
      }
      onSaved();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const ready = v.email && v.companyId && v.roles?.length && (!invite || (v.name && v.password));
  return (
    <Modal
      opened={opened}
      onClose={busy ? () => undefined : onClose}
      size="lg"
      title={<Text fw={600}>{invite ? 'Invite a new person' : editing ? `Access of ${initial.name}` : 'Give access to a company'}</Text>}
    >
      <Stack gap="sm">
        {invite ? (
          <TextInput label="Name" value={v.name ?? ''} onChange={set('name')} required />
        ) : (
          <Text size="sm" c="dimmed">
            The person needs an account somewhere in the group. Their roles here apply only in the company you choose.
          </Text>
        )}
        <TextInput label="Email" type="email" value={v.email ?? ''} onChange={set('email')} disabled={editing} required />
        <Select
          label={invite ? 'Home company' : 'Company'}
          data={companies.map((c) => ({ value: c.id, label: c.name }))}
          value={v.companyId ?? null}
          onChange={(x) => setV((s) => ({ ...s, companyId: x, employeeId: null }))}
          disabled={editing}
          required
        />
        <MultiSelect
          label="Roles in this company"
          data={ROLE_OPTIONS}
          value={v.roles ?? []}
          onChange={set('roles')}
          description={(v.roles ?? [])
            .map((r) => ROLE_HELP[r])
            .filter(Boolean)
            .join('. ')}
          required
        />
        <EmployeeSelect companyId={v.companyId} keep={initial?.employeeId} value={v.employeeId ?? null} onChange={set('employeeId')} />
        {invite ? (
          <PasswordInput
            label="Temporary password"
            description="They change it at their first sign-in."
            value={v.password ?? ''}
            onChange={set('password')}
            required
          />
        ) : null}
        <Group justify="flex-end" mt="sm">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!ready}>
            {invite ? 'Create account' : 'Save'}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function GroupRolesForm({ opened, onClose, onSaved, initial }) {
  const [email, setEmail] = useState('');
  const [roles, setRoles] = useState([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opened) {
      setEmail(initial?.email ?? '');
      setRoles(initial?.roles ?? []);
    }
  }, [opened, initial]);
  return (
    <Modal opened={opened} onClose={busy ? () => undefined : onClose} title={<Text fw={600}>Group roles</Text>}>
      <Stack gap="sm">
        <TextInput label="Email" value={email} onChange={(e) => setEmail(e.currentTarget.value)} disabled={!!initial} required />
        {GROUP_ROLES.map((r) => (
          <Checkbox
            key={r}
            label={GROUP_ROLE_LABEL[r]}
            description={GROUP_ROLE_HELP[r]}
            checked={roles.includes(r)}
            onChange={(e) => setRoles((x) => (e.currentTarget.checked ? [...x, r] : x.filter((y) => y !== r)))}
          />
        ))}
        <Group justify="flex-end" mt="sm">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            loading={busy}
            disabled={!email || !roles.length}
            onClick={async () => {
              setBusy(true);
              try {
                await groupApi.grantGroupRoles(email.trim(), roles);
                toast.ok('Group roles saved.');
                onSaved();
                onClose();
              } catch (e) {
                toast.err(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

const roleBadges = (roles, labels) => (
  <Group gap={4}>
    {roles.map((r) => (
      <Badge key={r} size="sm" variant="light" color="gray">
        {labels[r] ?? r}
      </Badge>
    ))}
  </Group>
);

export default function GroupAccessPage() {
  const { user } = useAuth();
  const { reload: reloadMe } = useCompanies();
  const [companyId, setCompanyId] = useState(null);
  const companiesQ = useQuery(() => groupApi.companies(), []);
  const q = useQuery(() => groupApi.access(companyId), [companyId]);
  const roles = useQuery(() => groupApi.groupRoles(), []);
  const [form, setForm] = useState(null); // {initial?, invite?}
  const [roleForm, setRoleForm] = useState(null); // {initial?}
  const [revoke, setRevoke] = useState(null);
  const [revokeRoles, setRevokeRoles] = useState(null);
  const companies = (companiesQ.data?.items ?? []).map((c) => ({ id: c.id, name: c.nameEn, code: c.code }));
  const people = accessByPerson(q.data?.items ?? []);
  const saved = () => {
    void q.reload();
    void roles.reload();
    void reloadMe();
  };

  return (
    <Stack gap="lg">
      <PageHeader
        title="Access"
        subtitle="Who can work in which company of the group, with which roles and as which employee. A person can have a different role in each company."
        actions={
          <>
            <Button
              variant="default"
              size="sm"
              leftSection={<IconUserPlus size={16} stroke={1.6} />}
              onClick={() => setForm({ invite: true })}
            >
              Invite new person
            </Button>
            <Button size="sm" leftSection={<IconKey size={16} stroke={1.6} />} onClick={() => setForm({})}>
              Give access
            </Button>
          </>
        }
      />
      <Select
        w={280}
        label="Company"
        placeholder="Every company"
        clearable
        data={companies.map((c) => ({ value: c.id, label: c.name }))}
        value={companyId}
        onChange={setCompanyId}
      />
      <DataTable
        rows={people}
        rowKey={(p) => p.userId}
        loading={q.loading}
        error={q.error}
        emptyTitle="No one has access yet."
        columns={[
          {
            key: 'name',
            header: 'Person',
            width: 240,
            render: (p) => (
              <div style={{ minWidth: 0 }}>
                <Text size="sm" fw={500} truncate>
                  {p.name}
                </Text>
                <Text size="xs" c="dimmed" truncate>
                  {p.email}
                </Text>
              </div>
            ),
          },
          {
            key: 'companies',
            header: 'Companies, roles and employee record',
            render: (p) => (
              <Stack gap={6}>
                {p.companies.map((r) => (
                  <Group key={r.companyId} gap="xs" wrap="nowrap" justify="space-between">
                    <Group gap={6} wrap="wrap" style={{ minWidth: 0 }}>
                      <Text size="sm" fw={500}>
                        {r.companyName}
                      </Text>
                      {r.source === 'home' ? (
                        <Badge size="xs" variant="outline" color="gray">
                          Home
                        </Badge>
                      ) : null}
                      {roleBadges(r.roles, ROLE_LABEL)}
                      <Text size="xs" c="dimmed">
                        {r.employeeName ? `as ${r.employeeName} (${r.employeeCode})` : 'no employee record'}
                      </Text>
                      {r.status !== 'active' ? <StatusBadge status={r.status} /> : null}
                    </Group>
                    <Group gap={2} wrap="nowrap">
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        leftSection={<IconPencil size={13} stroke={1.6} />}
                        onClick={() => setForm({ initial: { ...r } })}
                      >
                        Edit
                      </Button>
                      {r.source === 'membership' && r.userId !== user?.id ? (
                        <Button
                          size="compact-xs"
                          variant="subtle"
                          color="red"
                          leftSection={<IconTrash size={13} stroke={1.6} />}
                          onClick={() => setRevoke(r)}
                        >
                          Remove
                        </Button>
                      ) : null}
                    </Group>
                  </Group>
                ))}
              </Stack>
            ),
          },
        ]}
      />

      <div>
        <SectionTitle
          hint="Group roles reach every company of the group, without an access row in each."
          actions={
            <Button size="xs" variant="light" onClick={() => setRoleForm({})}>
              Give group roles
            </Button>
          }
        >
          Group roles
        </SectionTitle>
        <DataTable
          rows={roles.data?.items ?? []}
          rowKey={(r) => r.userId}
          loading={roles.loading}
          error={roles.error}
          emptyTitle="No one has a group role."
          columns={[
            {
              key: 'name',
              header: 'Person',
              render: (r) => (
                <div>
                  <Text size="sm" fw={500}>
                    {r.name}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {r.email}
                    {r.homeCompany ? ` · ${r.homeCompany}` : ''}
                  </Text>
                </div>
              ),
            },
            { key: 'roles', header: 'Group roles', render: (r) => roleBadges(r.roles, GROUP_ROLE_LABEL) },
            {
              key: 'actions',
              header: '',
              align: 'right',
              render: (r) => (
                <Group gap={2} justify="flex-end" wrap="nowrap">
                  <Button size="compact-xs" variant="subtle" onClick={() => setRoleForm({ initial: r })}>
                    Edit
                  </Button>
                  {r.userId !== user?.id ? (
                    <Button size="compact-xs" variant="subtle" color="red" onClick={() => setRevokeRoles(r)}>
                      Remove
                    </Button>
                  ) : null}
                </Group>
              ),
            },
          ]}
        />
      </div>

      <AccessForm
        opened={!!form}
        onClose={() => setForm(null)}
        onSaved={saved}
        companies={companies}
        initial={form?.initial}
        invite={!!form?.invite}
      />
      <GroupRolesForm opened={!!roleForm} onClose={() => setRoleForm(null)} onSaved={saved} initial={roleForm?.initial} />
      <ConfirmDialog
        opened={!!revoke}
        title="Remove access"
        message={
          revoke
            ? `${revoke.name} will no longer be able to work in ${revoke.companyName}. Their records there stay; any session working there goes back to their home company.`
            : ''
        }
        confirmLabel="Remove access"
        onClose={() => setRevoke(null)}
        onConfirm={async () => {
          try {
            await groupApi.revokeAccess(revoke.userId, revoke.companyId);
            toast.ok('Access removed.');
            saved();
          } catch (e) {
            toast.err(e, 'Not removed');
          }
        }}
      />
      <ConfirmDialog
        opened={!!revokeRoles}
        title="Remove group roles"
        message={revokeRoles ? `${revokeRoles.name} loses every group role. Their own company roles stay.` : ''}
        confirmLabel="Remove group roles"
        onClose={() => setRevokeRoles(null)}
        onConfirm={async () => {
          try {
            await groupApi.revokeGroupRoles(revokeRoles.userId);
            toast.ok('Group roles removed.');
            saved();
          } catch (e) {
            toast.err(e, 'Not removed');
          }
        }}
      />
    </Stack>
  );
}
