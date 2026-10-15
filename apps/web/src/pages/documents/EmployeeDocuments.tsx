import { ActionIcon, Badge, Button, Drawer, Group, Menu, Paper, Select, SimpleGrid, Stack, Switch, Text, Textarea, TextInput, Tooltip } from '@mantine/core';
import {
  IconAlertTriangle, IconCheck, IconDots, IconDownload, IconEye, IconEyeOff, IconFileText, IconHistory, IconPencil, IconPhoto, IconPlus, IconRefresh, IconTrash, IconX,
} from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { documents as docsApi, fileSize, nptDateOf, type DocCategory, type EmployeeDoc } from '../../api/documents';
import type { UploadedFile } from '../../api/files';
import { DateField } from '../../components/BsDatePicker';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { EmptyState } from '../../components/EmptyState';
import { FileUpload } from '../../components/FileUpload';
import { fmtDate, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 } as const;

const STATUS: Record<EmployeeDoc['status'], { label: string; color: string }> = {
  pending: { label: 'Awaiting HR check', color: 'yellow' },
  verified: { label: 'Verified', color: 'brand' },
  rejected: { label: 'Not accepted', color: 'red' },
};

export function DocStatus({ status }: { status: EmployeeDoc['status'] }) {
  const s = STATUS[status];
  return <Badge variant="light" color={s.color} size="sm">{s.label}</Badge>;
}

function daysUntil(ad: string): number {
  return Math.round((Date.parse(`${ad}T00:00:00Z`) - Date.parse(`${todayNpt()}T00:00:00Z`)) / 86_400_000);
}

/** "Expires 3 Magh 2083", in orange within 30 days and red once expired. */
export function ExpiryText({ date }: { date: string | null }) {
  if (!date) return <Text size="xs" c="dimmed">No expiry</Text>;
  const d = daysUntil(date);
  const color = d < 0 ? 'red' : d <= 30 ? 'orange' : 'dimmed';
  return (
    <Group gap={4} wrap="nowrap">
      {d <= 30 ? <IconAlertTriangle size={13} stroke={1.6} color={`var(--mantine-color-${d < 0 ? 'red' : 'orange'}-6)`} /> : null}
      <Text size="xs" c={color}>{d < 0 ? `Expired ${fmtDate(date)}` : d === 0 ? 'Expires today' : `Expires ${fmtDate(date)}${d <= 30 ? ` (${d} days)` : ''}`}</Text>
    </Group>
  );
}

interface FormState { categoryId: string | null; title: string; file: UploadedFile | null; expiryDate: string | null; visibleToEmployee: boolean; notes: string; replaces: EmployeeDoc | null }

/**
 * An employee's documents, grouped by category. `self` is the employee's own view (upload, replace, remove an upload HR
 * has not checked yet); `hr` adds verification, HR-only documents, expiry editing and version history.
 */
export function EmployeeDocuments({ employeeId, mode }: { employeeId: string | null; mode: 'self' | 'hr' }) {
  const hr = mode === 'hr';
  const [history, setHistory] = useState(false);
  const cats = useQuery(() => docsApi.categories(), []);
  const q = useQuery(() => (hr && employeeId ? docsApi.forEmployee(employeeId, history) : docsApi.mine()), [employeeId, history, hr]);
  const [form, setForm] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  const [rejecting, setRejecting] = useState<EmployeeDoc | null>(null); const [reason, setReason] = useState('');
  const [deleting, setDeleting] = useState<EmployeeDoc | null>(null);
  const [editing, setEditing] = useState<EmployeeDoc | null>(null);
  const [edit, setEdit] = useState<{ title: string; expiryDate: string | null; visibleToEmployee: boolean; notes: string }>({ title: '', expiryDate: null, visibleToEmployee: true, notes: '' });

  const categories = (cats.data?.categories ?? []).filter((c) => c.active && (hr || c.employee_can_upload));
  const catById = useMemo(() => new Map((cats.data?.categories ?? []).map((c) => [c.id, c])), [cats.data]);
  const chosen: DocCategory | undefined = form?.categoryId ? catById.get(form.categoryId) : undefined;
  const docs = q.data?.documents ?? [];

  const open = (replaces?: EmployeeDoc) => setForm({
    categoryId: replaces?.category_id ?? null, title: replaces?.title ?? '', file: null, expiryDate: null,
    visibleToEmployee: replaces?.visible_to_employee ?? true, notes: '', replaces: replaces ?? null,
  });

  const save = async () => {
    if (!form || !employeeId || !form.categoryId || !form.file) return;
    if (chosen?.expiry_required && !form.expiryDate) { toast.err(new Error(`${chosen.name} needs an expiry date.`)); return; }
    setBusy(true);
    try {
      await docsApi.add(employeeId, {
        categoryId: form.categoryId, title: form.title.trim() || chosen?.name || 'Document', fileId: form.file.id,
        expiryDate: form.expiryDate, ...(hr ? { visibleToEmployee: form.visibleToEmployee } : {}), notes: form.notes.trim() || null, replacesId: form.replaces?.id ?? null,
      });
      toast.ok(hr ? 'Document saved.' : 'Uploaded. HR will check it shortly.');
      setForm(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };

  const verify = async (d: EmployeeDoc) => {
    try { await docsApi.verify(d.id, 'verify'); toast.ok(`${d.title} verified.`); await q.reload(); } catch (e) { toast.err(e); }
  };
  const reject = async () => {
    if (!rejecting) return;
    try { await docsApi.verify(rejecting.id, 'reject', reason.trim()); toast.ok('The employee has been asked to upload it again.'); setRejecting(null); setReason(''); await q.reload(); } catch (e) { toast.err(e); }
  };
  const remove = async () => {
    if (!deleting) return;
    try { await docsApi.remove(deleting.id); toast.ok('Document removed.'); setDeleting(null); await q.reload(); } catch (e) { toast.err(e); }
  };
  const saveEdit = async () => {
    if (!editing) return;
    try {
      await docsApi.update(editing.id, { title: edit.title.trim(), expiryDate: edit.expiryDate, visibleToEmployee: edit.visibleToEmployee, notes: edit.notes.trim() || null });
      toast.ok('Document updated.'); setEditing(null); await q.reload();
    } catch (e) { toast.err(e); }
  };

  const ownPendingUpload = (d: EmployeeDoc) => !hr && d.status !== 'verified';

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="wrap">
        <Text size="sm" c="dimmed" maw={560}>
          {hr ? 'Contracts, certificates and IDs on file. Uploads by the employee wait for your check; expiring documents raise reminders at 30, 7 and 0 days.'
            : 'Your certificates and IDs on file with HR. New uploads are checked by HR; you will get a reminder before anything expires.'}
        </Text>
        <Group gap="xs">
          {hr ? <Switch size="sm" label="Show earlier versions" checked={history} onChange={(e) => setHistory(e.currentTarget.checked)} /> : null}
          <Button leftSection={<IconPlus {...ICON} />} onClick={() => open()} disabled={!employeeId}>Upload document</Button>
        </Group>
      </Group>

      {q.error ? <EmptyState title="Could not load documents" hint={q.error} /> : null}
      {!q.loading && !q.error && docs.length === 0 ? (
        <EmptyState icon={<IconFileText size={20} stroke={1.6} />} title="No documents yet"
          hint={hr ? 'Upload the signed contract, citizenship and certificates so they are on file.' : 'Upload your citizenship, PAN card and academic certificates so HR has them on file.'} />
      ) : null}

      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }} spacing="md">
        {docs.map((d) => (
          <Paper key={d.id} withBorder radius="lg" p="md" style={{ opacity: d.is_current ? 1 : 0.6 }}>
            <Stack gap={8}>
              <Group justify="space-between" wrap="nowrap" align="flex-start">
                <Group gap={10} wrap="nowrap" style={{ minWidth: 0 }}>
                  {d.mime === 'application/pdf' ? <IconFileText size={22} stroke={1.5} color="var(--hj-hydro)" /> : <IconPhoto size={22} stroke={1.5} color="var(--hj-hydro)" />}
                  <div style={{ minWidth: 0 }}>
                    <Text size="sm" fw={600} truncate>{d.title}</Text>
                    <Text size="xs" c="dimmed" truncate>{[d.category_name !== d.title ? d.category_name : null, d.version > 1 ? `version ${d.version}` : null].filter(Boolean).join(' · ') || 'Document'}</Text>
                  </div>
                </Group>
                <Menu position="bottom-end" withinPortal>
                  <Menu.Target><ActionIcon variant="subtle" color="gray" aria-label={`Actions for ${d.title}`}><IconDots {...ICON} /></ActionIcon></Menu.Target>
                  <Menu.Dropdown>
                    <Menu.Item leftSection={<IconEye {...ICON} />} component="a" href={docsApi.fileUrl(d.id)} target="_blank" rel="noopener">Open</Menu.Item>
                    <Menu.Item leftSection={<IconDownload {...ICON} />} component="a" href={docsApi.fileUrl(d.id, true)}>Download</Menu.Item>
                    {d.is_current ? <Menu.Item leftSection={<IconRefresh {...ICON} />} onClick={() => open(d)}>Upload a new version</Menu.Item> : null}
                    {hr ? <Menu.Item leftSection={<IconPencil {...ICON} />} onClick={() => { setEditing(d); setEdit({ title: d.title, expiryDate: d.expiry_date, visibleToEmployee: d.visible_to_employee, notes: d.notes ?? '' }); }}>Edit details</Menu.Item> : null}
                    {hr || ownPendingUpload(d) ? <Menu.Item color="red" leftSection={<IconTrash {...ICON} />} onClick={() => setDeleting(d)}>Remove</Menu.Item> : null}
                  </Menu.Dropdown>
                </Menu>
              </Group>
              <Group gap={6} wrap="wrap">
                <DocStatus status={d.status} />
                {hr && !d.visible_to_employee ? <Tooltip label="Only HR can see this document"><Badge variant="light" color="gray" size="sm" leftSection={<IconEyeOff size={11} />}>HR only</Badge></Tooltip> : null}
                {!d.is_current ? <Badge variant="light" color="gray" size="sm" leftSection={<IconHistory size={11} />}>Earlier version</Badge> : null}
              </Group>
              <ExpiryText date={d.expiry_date} />
              {d.status === 'rejected' && d.reject_reason ? <Text size="xs" c="red.7">“{d.reject_reason}”</Text> : null}
              {d.notes ? <Text size="xs" c="dimmed" lineClamp={2}>{d.notes}</Text> : null}
              <Text size="xs" c="dimmed">{fileSize(d.bytes)} · added {fmtDate(nptDateOf(d.created_at))}{d.uploaded_by_name ? ` by ${d.uploaded_by_name}` : ''}</Text>
              {hr && d.status === 'pending' ? (
                <Group gap="xs" mt={4}>
                  <Button size="xs" leftSection={<IconCheck size={14} />} onClick={() => verify(d)}>Verify</Button>
                  <Button size="xs" variant="default" color="red" leftSection={<IconX size={14} />} onClick={() => { setRejecting(d); setReason(''); }}>Not accepted</Button>
                </Group>
              ) : null}
            </Stack>
          </Paper>
        ))}
      </SimpleGrid>

      <Drawer opened={!!form} onClose={() => setForm(null)} position="right" size="md" title={<Text fw={600}>{form?.replaces ? `New version of ${form.replaces.title}` : 'Upload document'}</Text>}>
        {form ? (
          <Stack gap="md">
            <Select label="Document type" required searchable data={categories.map((c) => ({ value: c.id, label: c.name }))} value={form.categoryId} disabled={!!form.replaces}
              onChange={(v) => setForm({ ...form, categoryId: v, title: form.title || (v ? catById.get(v)?.name ?? '' : '') })} allowDeselect={false} />
            <TextInput label="Title" placeholder={chosen?.name ?? 'e.g. Bachelor degree certificate'} value={form.title} maxLength={120} onChange={(e) => setForm({ ...form, title: e.currentTarget.value })} />
            <div>
              <Text size="sm" fw={500} mb={6}>File</Text>
              <FileUpload purpose="employee_document" value={form.file} onChange={(f) => setForm({ ...form, file: f })} label="Choose PDF or photo" />
              <Text size="xs" c="dimmed" mt={6}>PDF, JPG, PNG or WebP, up to 4 MB. Files are stored encrypted.</Text>
            </div>
            <DateField label={chosen?.expiry_required ? 'Expiry date (required)' : 'Expiry date (optional)'} value={form.expiryDate} onChange={(v) => setForm({ ...form, expiryDate: v })}
              {...(chosen?.expiry_required ? { required: true } : {})} description="We will remind you 30 and 7 days before it expires." />
            {hr ? <Switch label="Employee can see this document" checked={form.visibleToEmployee} onChange={(e) => setForm({ ...form, visibleToEmployee: e.currentTarget.checked })} /> : null}
            <Textarea label="Notes (optional)" autosize minRows={2} maxLength={500} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.currentTarget.value })} />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setForm(null)}>Cancel</Button>
              <Button loading={busy} disabled={!form.categoryId || !form.file} onClick={save}>{hr ? 'Save document' : 'Upload for HR check'}</Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>

      <Drawer opened={!!editing} onClose={() => setEditing(null)} position="right" size="md" title={<Text fw={600}>Edit document</Text>}>
        <Stack gap="md">
          <TextInput label="Title" value={edit.title} maxLength={120} onChange={(e) => setEdit({ ...edit, title: e.currentTarget.value })} />
          <DateField label="Expiry date" value={edit.expiryDate} onChange={(v) => setEdit({ ...edit, expiryDate: v })} />
          <Switch label="Employee can see this document" checked={edit.visibleToEmployee} onChange={(e) => setEdit({ ...edit, visibleToEmployee: e.currentTarget.checked })} />
          <Textarea label="Notes" autosize minRows={2} maxLength={500} value={edit.notes} onChange={(e) => setEdit({ ...edit, notes: e.currentTarget.value })} />
          <Group justify="flex-end"><Button variant="default" onClick={() => setEditing(null)}>Cancel</Button><Button onClick={saveEdit} disabled={!edit.title.trim()}>Save</Button></Group>
        </Stack>
      </Drawer>

      <Drawer opened={!!rejecting} onClose={() => setRejecting(null)} position="right" size="sm" title={<Text fw={600}>Not accepted</Text>}>
        <Stack gap="md">
          <Text size="sm">Say what needs fixing. The employee will be asked to upload {rejecting?.title} again.</Text>
          <Textarea label="Reason" required autosize minRows={2} maxLength={300} value={reason} onChange={(e) => setReason(e.currentTarget.value)} placeholder="e.g. The scan is blurry; please upload a clearer copy." />
          <Group justify="flex-end"><Button variant="default" onClick={() => setRejecting(null)}>Cancel</Button><Button color="red" disabled={!reason.trim()} onClick={reject}>Send back</Button></Group>
        </Stack>
      </Drawer>

      <ConfirmDialog opened={!!deleting} title="Remove document?" confirmLabel="Remove"
        message={deleting ? `${deleting.title} will be removed and its file deleted.${deleting.replaces_id && deleting.is_current ? ' The previous version becomes current again.' : ''}` : ''}
        onConfirm={remove} onClose={() => setDeleting(null)} />
    </Stack>
  );
}
