import { Avatar, Badge, Button, FileInput, Group, Modal, Select, Stack, Text, TextInput, Textarea } from '@mantine/core';
import { IconBuildingPlus, IconLink, IconPencil, IconPhoto } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { group as groupApi } from '../../api/group';
import { useCompanies } from '../../auth/CompaniesProvider';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { companyChanges, validCompanyCode } from '../../lib/group';
import { fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const MAX_LOGO_BYTES = 140_000; // the stored data URL is at most 200,000 characters

function readLogo(file) {
  return new Promise((resolve, reject) => {
    if (!file) return resolve(null);
    if (!/^image\/(png|jpeg|svg\+xml)$/.test(file.type)) return reject(new Error('Use a PNG, JPEG or SVG image.'));
    if (file.size > MAX_LOGO_BYTES) return reject(new Error('The logo is too large; use an image under 140 KB.'));
    const r = new FileReader();
    r.onload = () => resolve(String(r.result));
    r.onerror = () => reject(new Error('Could not read that file.'));
    r.readAsDataURL(file);
  });
}

const BLANK = { code: '', nameEn: '', nameNe: '', pan: '', address: '', logoDataUrl: null };

/** Add (no `company`) or edit a company of the group: names and the company profile (PAN, address, logo). */
function CompanyForm({ company, opened, onClose, onSaved, startsGroup }) {
  const [v, setV] = useState(BLANK);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState({});
  const editing = !!company;
  useEffect(() => {
    if (!opened) return;
    setV(company ? { ...BLANK, ...company, nameNe: company.nameNe ?? '', pan: company.pan ?? '', address: company.address ?? '' } : BLANK);
    setErr({});
  }, [opened, company]);
  const set = (k) => (e) => setV((x) => ({ ...x, [k]: e?.currentTarget ? e.currentTarget.value : e }));

  const save = async () => {
    const e = {};
    if (!editing && !validCompanyCode(v.code.trim())) e.code = 'Use 2 to 20 letters, digits, - or _.';
    if (!v.nameEn.trim()) e.nameEn = 'Required';
    if (v.pan && !/^\d{9}$/.test(v.pan)) e.pan = 'PAN has 9 digits';
    setErr(e);
    if (Object.keys(e).length) return;
    setBusy(true);
    try {
      if (editing) {
        const changes = companyChanges(company, { ...v, nameEn: v.nameEn.trim() });
        if (Object.keys(changes).length) await groupApi.updateCompany(company.id, changes);
        toast.ok(`${v.nameEn} saved.`);
      } else {
        const body = { code: v.code.trim().toUpperCase(), nameEn: v.nameEn.trim() };
        for (const k of ['nameNe', 'pan', 'address', 'logoDataUrl']) if (v[k]) body[k] = v[k];
        await groupApi.createCompany(body);
        toast.ok(
          startsGroup
            ? `${body.nameEn} added. Your company and ${body.nameEn} now form a group, and you are its group admin.`
            : `${body.nameEn} added with the standard leave types, approval flows and settings. You are its admin.`,
        );
      }
      onSaved();
      onClose();
    } catch (ex) {
      toast.err(ex);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      opened={opened}
      onClose={busy ? () => undefined : onClose}
      title={
        <Text fw={600}>{editing ? `Edit ${company.nameEn}` : startsGroup ? 'Start a group: add a sister company' : 'Add a company'}</Text>
      }
      size="lg"
    >
      <Stack gap="sm">
        {!editing ? (
          <Text size="sm" c="dimmed">
            The new company starts with the standard Nepal defaults (leave types, approval flows, settings). Its people, payroll and records
            are its own; group views add them up.
          </Text>
        ) : null}
        <Group grow align="flex-start">
          <TextInput
            label="Code"
            description="Short and unique, e.g. BETA"
            value={v.code}
            onChange={set('code')}
            disabled={editing}
            error={err.code}
            required={!editing}
          />
          <TextInput label="Name (English)" value={v.nameEn} onChange={set('nameEn')} error={err.nameEn} required />
        </Group>
        <Group grow align="flex-start">
          <TextInput label="Name (Nepali)" value={v.nameNe} onChange={set('nameNe')} />
          <TextInput label="PAN" description="9 digits" value={v.pan} onChange={set('pan')} error={err.pan} />
        </Group>
        <Textarea label="Address" autosize minRows={2} value={v.address} onChange={set('address')} />
        <Group align="flex-end" gap="sm">
          {v.logoDataUrl ? <Avatar src={v.logoDataUrl} size={44} radius="sm" /> : null}
          <FileInput
            style={{ flex: 1 }}
            label="Logo"
            description="PNG, JPEG or SVG under 140 KB"
            accept="image/png,image/jpeg,image/svg+xml"
            leftSection={<IconPhoto size={16} stroke={1.6} />}
            placeholder={v.logoDataUrl ? 'Replace logo' : 'Choose an image'}
            clearable
            onChange={async (file) => {
              try {
                const url = await readLogo(file);
                if (url) setV((x) => ({ ...x, logoDataUrl: url }));
              } catch (ex) {
                toast.err(ex, 'Logo not used');
              }
            }}
          />
          {v.logoDataUrl ? (
            <Button variant="subtle" color="red" size="xs" onClick={() => setV((x) => ({ ...x, logoDataUrl: null }))}>
              Remove logo
            </Button>
          ) : null}
        </Group>
        <Group justify="flex-end" mt="sm">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy}>
            {editing ? 'Save' : 'Add company'}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function AttachModal({ opened, onClose, onSaved }) {
  const q = useQuery(() => (opened ? groupApi.attachable() : Promise.resolve({ items: [] })), [opened]);
  const [companyId, setCompanyId] = useState(null);
  const [busy, setBusy] = useState(false);
  const items = q.data?.items ?? [];
  return (
    <Modal opened={opened} onClose={busy ? () => undefined : onClose} title={<Text fw={600}>Bring an existing company in</Text>}>
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          Only a standalone company where you are admin can join the group, so nobody can pull in another firm's data.
        </Text>
        {items.length || q.loading ? (
          <Select
            label="Company"
            data={items.map((c) => ({ value: c.id, label: `${c.name} (${c.code})` }))}
            value={companyId}
            onChange={setCompanyId}
            placeholder={q.loading ? 'Loading' : 'Choose a company'}
          />
        ) : (
          <EmptyState compact title="No company to attach" hint="You are not admin of any company outside a group." />
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            disabled={!companyId}
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                const r = await groupApi.attach(companyId);
                toast.ok(`${r.name} is now part of the group.`);
                onSaved();
                onClose();
              } catch (e) {
                toast.err(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            Attach
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export default function GroupCompaniesPage() {
  const { access, reload } = useCompanies();
  const q = useQuery(() => (access.admin ? groupApi.companies() : Promise.resolve({ items: [] })), [access.admin]);
  const [adding, setAdding] = useState(false);
  const [attaching, setAttaching] = useState(false);
  const [editing, setEditing] = useState(null);
  const saved = () => {
    void q.reload();
    void reload();
  };

  if (!access.admin && access.startGroup)
    return (
      <Stack gap="lg">
        <PageHeader title="Companies" subtitle="Your company is not part of a group yet." />
        <EmptyState
          title="Run several companies from one sign-in"
          hint="Add a sister company to start a group. You become its group admin and can then give people access, transfer employees and copy policies between the companies."
          icon={<IconBuildingPlus size={20} stroke={1.6} />}
          action={{ label: 'Add a sister company', onClick: () => setAdding(true) }}
        />
        <CompanyForm opened={adding} onClose={() => setAdding(false)} onSaved={() => window.location.reload()} startsGroup />
      </Stack>
    );

  return (
    <Stack gap="lg">
      <PageHeader
        title="Companies"
        subtitle="The companies of the group, their profile and head count. Each keeps its own people, payroll and settings."
        actions={
          <>
            <Button variant="default" size="sm" leftSection={<IconLink size={16} stroke={1.6} />} onClick={() => setAttaching(true)}>
              Attach existing
            </Button>
            <Button size="sm" leftSection={<IconBuildingPlus size={16} stroke={1.6} />} onClick={() => setAdding(true)}>
              Add company
            </Button>
          </>
        }
      />
      <DataTable
        rows={q.data?.items ?? []}
        rowKey={(c) => c.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="No companies"
        columns={[
          {
            key: 'name',
            header: 'Company',
            render: (c) => (
              <Group gap="sm" wrap="nowrap">
                <Avatar src={c.logoDataUrl} size={32} radius="sm" color="brand" variant="light">
                  {c.code.slice(0, 2)}
                </Avatar>
                <div style={{ minWidth: 0 }}>
                  <Group gap={6} wrap="nowrap">
                    <Text size="sm" fw={500} truncate>
                      {c.nameEn}
                    </Text>
                    {c.active ? (
                      <Badge size="xs" variant="light">
                        Active
                      </Badge>
                    ) : null}
                  </Group>
                  <Text size="xs" c="dimmed" truncate>
                    {c.code}
                    {c.nameNe ? ` · ${c.nameNe}` : ''}
                  </Text>
                </div>
              </Group>
            ),
          },
          { key: 'pan', header: 'PAN', render: (c) => <span className="num">{c.pan ?? '—'}</span> },
          { key: 'address', header: 'Address', render: (c) => c.address ?? '—' },
          { key: 'headcount', header: 'Active people', align: 'right' },
          { key: 'createdAt', header: 'Added', render: (c) => fmtDate(String(c.createdAt).slice(0, 10)) },
          {
            key: 'edit',
            header: '',
            align: 'right',
            render: (c) => (
              <Button size="xs" variant="subtle" leftSection={<IconPencil size={14} stroke={1.6} />} onClick={() => setEditing(c)}>
                Edit
              </Button>
            ),
          },
        ]}
      />
      <CompanyForm opened={adding} onClose={() => setAdding(false)} onSaved={saved} />
      <CompanyForm company={editing} opened={!!editing} onClose={() => setEditing(null)} onSaved={saved} />
      <AttachModal opened={attaching} onClose={() => setAttaching(false)} onSaved={saved} />
    </Stack>
  );
}
