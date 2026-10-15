import {
  Alert, Badge, Button, Code, Drawer, Group, MultiSelect, Paper, Progress, ScrollArea, SegmentedControl, Select, SimpleGrid, Stack, Switch, Text, Textarea, TextInput, Tooltip, UnstyledButton,
} from '@mantine/core';
import { IconArchive, IconBell, IconCheck, IconEye, IconFilePlus, IconPlus, IconPrinter, IconSearch, IconX } from '@tabler/icons-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { settings as settingsApi } from '../../api';
import { documents as docsApi, nptDateOf, type DocCategory, type EmployeeDoc, type LetterRequest, type LetterTemplate, type Policy } from '../../api/documents';
import type { UploadedFile } from '../../api/files';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { EmptyState } from '../../components/EmptyState';
import { FileUpload } from '../../components/FileUpload';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ExpiryText } from './EmployeeDocuments';
import { IssueLetterDrawer, LetterFrame, LetterTable, RequestList } from './Letters';

const ICON = { size: 16, stroke: 1.6 } as const;
type Section = 'review' | 'letters' | 'templates' | 'policies' | 'categories';

// ---------------------------------------------------------------- to review
function ReviewSection() {
  const q = useQuery(() => docsApi.queue(30), []);
  const reqs = useQuery(() => docsApi.requests({ status: 'open' }), []);
  const [issuing, setIssuing] = useState<LetterRequest | null>(null);
  const [rejecting, setRejecting] = useState<EmployeeDoc | null>(null); const [reason, setReason] = useState('');
  const pending = q.data?.pending ?? []; const expiring = q.data?.expiring ?? [];
  const verify = async (d: EmployeeDoc) => { try { await docsApi.verify(d.id, 'verify'); toast.ok(`${d.title} verified.`); await q.reload(); } catch (e) { toast.err(e); } };
  const reject = async () => {
    if (!rejecting) return;
    try { await docsApi.verify(rejecting.id, 'reject', reason.trim()); toast.ok('Sent back to the employee.'); setRejecting(null); setReason(''); await q.reload(); } catch (e) { toast.err(e); }
  };
  const expired = expiring.filter((d) => (d.days_left ?? 0) < 0).length;
  return (
    <Stack gap="xl">
      <StatStrip stats={[
        { key: 'p', label: 'Documents to verify', value: pending.length, ...(pending.length ? { tone: 'warn' as const } : {}) },
        { key: 'r', label: 'Letter requests', value: (reqs.data?.requests ?? []).length, ...((reqs.data?.requests ?? []).length ? { tone: 'warn' as const } : {}) },
        { key: 'e', label: 'Expiring in 30 days', value: expiring.length - expired },
        { key: 'x', label: 'Expired', value: expired, ...(expired ? { tone: 'bad' as const } : {}) },
      ]} />
      <div>
        <SectionTitle hint="Uploads by employees wait here until you check them against the original.">Documents to verify</SectionTitle>
        <DataTable<EmployeeDoc> rows={pending} rowKey={(d) => d.id} loading={q.loading} error={q.error} emptyTitle="Nothing to verify" emptyHint="New uploads by employees will appear here."
          columns={[
            { key: 'emp', header: 'Employee', render: (d) => <div><Text size="sm" component={Link} to={`/employees/${d.employee_id}`}>{d.name_en}</Text><Text size="xs" c="dimmed">{d.emp_code}</Text></div> },
            { key: 'doc', header: 'Document', render: (d) => <div><Text size="sm">{d.title}</Text><Text size="xs" c="dimmed">{d.category_name}{d.version > 1 ? ` · version ${d.version}` : ''}</Text></div> },
            { key: 'exp', header: 'Expiry', render: (d) => <ExpiryText date={d.expiry_date} /> },
            { key: 'at', header: 'Uploaded', render: (d) => <Text size="sm">{fmtDate(nptDateOf(d.created_at))}</Text> },
            { key: 'act', header: '', align: 'right', render: (d) => (
              <Group gap={4} justify="flex-end" wrap="nowrap">
                <Button size="compact-sm" variant="subtle" leftSection={<IconEye size={14} />} component="a" href={docsApi.fileUrl(d.id)} target="_blank" rel="noopener">Open</Button>
                <Button size="compact-sm" leftSection={<IconCheck size={14} />} onClick={() => verify(d)}>Verify</Button>
                <Button size="compact-sm" variant="default" leftSection={<IconX size={14} />} onClick={() => { setRejecting(d); setReason(''); }}>Send back</Button>
              </Group>
            ) },
          ]} />
      </div>
      <div>
        <SectionTitle hint="Employees ask for these from their profile. Preparing one issues it with the next reference number.">Letter requests</SectionTitle>
        {(reqs.data?.requests ?? []).length ? <RequestList requests={reqs.data?.requests ?? []} hr onIssue={(r) => setIssuing(r)} />
          : <EmptyState compact title="No open letter requests" hint="Requests for salary certificates, experience letters and similar appear here." />}
      </div>
      <div>
        <SectionTitle hint="Passports, licences and other documents expiring within 30 days, or already expired. Reminders go out automatically at 30, 7 and 0 days.">Expiring documents</SectionTitle>
        <DataTable<EmployeeDoc> rows={expiring} rowKey={(d) => d.id} loading={q.loading} emptyTitle="Nothing expiring soon" emptyHint="Add expiry dates to passports, licences and permits to track them here."
          columns={[
            { key: 'emp', header: 'Employee', render: (d) => <div><Text size="sm" component={Link} to={`/employees/${d.employee_id}`}>{d.name_en}</Text><Text size="xs" c="dimmed">{d.emp_code}</Text></div> },
            { key: 'doc', header: 'Document', render: (d) => <Text size="sm">{d.title}</Text> },
            { key: 'exp', header: 'Expiry', render: (d) => <ExpiryText date={d.expiry_date} /> },
            { key: 'act', header: '', align: 'right', render: (d) => <Button size="compact-sm" variant="subtle" component="a" href={docsApi.fileUrl(d.id)} target="_blank" rel="noopener">Open</Button> },
          ]} />
      </div>
      <IssueLetterDrawer opened={!!issuing} onClose={() => setIssuing(null)} request={issuing} onIssued={() => { void reqs.reload(); }} />
      <Drawer opened={!!rejecting} onClose={() => setRejecting(null)} position="right" size="sm" title={<Text fw={600}>Send back {rejecting?.title}</Text>}>
        <Stack gap="md">
          <Text size="sm">{rejecting?.name_en} will be asked to upload it again with your reason.</Text>
          <Textarea label="Reason" required autosize minRows={2} maxLength={300} value={reason} onChange={(e) => setReason(e.currentTarget.value)} placeholder="e.g. The scan is cut off; please upload both sides." />
          <Group justify="flex-end"><Button variant="default" onClick={() => setRejecting(null)}>Cancel</Button><Button color="red" disabled={!reason.trim()} onClick={reject}>Send back</Button></Group>
        </Stack>
      </Drawer>
    </Stack>
  );
}

// ---------------------------------------------------------------- letters register
function LettersSection() {
  const [search, setSearch] = useState(''); const [term, setTerm] = useState('');
  useEffect(() => { const t = setTimeout(() => setTerm(search.trim()), 300); return () => clearTimeout(t); }, [search]);
  const q = useQuery(() => docsApi.letters(term ? { q: term } : {}), [term]);
  const [issuing, setIssuing] = useState(false);
  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="wrap">
        <TextInput leftSection={<IconSearch {...ICON} />} placeholder="Search by reference, employee or letter" value={search} onChange={(e) => setSearch(e.currentTarget.value)} w={{ base: '100%', sm: 340 }} />
        <Button leftSection={<IconFilePlus {...ICON} />} onClick={() => setIssuing(true)}>Issue a letter</Button>
      </Group>
      <LetterTable letters={q.data?.letters ?? []} loading={q.loading} error={q.error} hr showEmployee onChanged={() => { void q.reload(); }} />
      <IssueLetterDrawer opened={issuing} onClose={() => setIssuing(false)} onIssued={() => { void q.reload(); }} />
    </Stack>
  );
}

// ---------------------------------------------------------------- templates
interface TplForm { id?: string; code: string; name: string; subject: string; body: string; requestable: boolean; visibleToEmployee: boolean; active: boolean; builtin: boolean }

function SignatoryCard() {
  const q = useQuery(() => docsApi.letterSettings(), []);
  const [f, setF] = useState<{ signatoryName: string; signatoryTitle: string; refPrefix: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (q.data) setF({ signatoryName: q.data.settings.signatoryName ?? '', signatoryTitle: q.data.settings.signatoryTitle ?? '', refPrefix: q.data.settings.refPrefix }); }, [q.data]);
  if (!f) return null;
  const save = async () => {
    setBusy(true);
    try { await docsApi.saveLetterSettings({ signatoryName: f.signatoryName.trim() || null, signatoryTitle: f.signatoryTitle.trim() || null, refPrefix: f.refPrefix.trim() }); toast.ok('Letter settings saved.'); }
    catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Paper withBorder radius="lg" p="md">
      <Text fw={600} mb={4}>Letterhead and signature</Text>
      <Text size="xs" c="dimmed" mb="sm">Letters use the company name, address and logo from Settings, Company & rules. References read like {f.refPrefix || 'HR'}/2083/0001 and restart every BS year.</Text>
      <SimpleGrid cols={{ base: 1, sm: 4 }} spacing="sm">
        <TextInput label="Signed by" value={f.signatoryName} maxLength={80} onChange={(e) => setF({ ...f, signatoryName: e.currentTarget.value })} placeholder="e.g. Sita Sharma" />
        <TextInput label="Title" value={f.signatoryTitle} maxLength={80} onChange={(e) => setF({ ...f, signatoryTitle: e.currentTarget.value })} placeholder="e.g. HR Manager" />
        <TextInput label="Reference prefix" value={f.refPrefix} maxLength={20} onChange={(e) => setF({ ...f, refPrefix: e.currentTarget.value })} placeholder="e.g. CV/HR" />
        <Button mt={{ sm: 25 }} loading={busy} disabled={!/^[A-Za-z0-9/_-]{1,20}$/.test(f.refPrefix.trim())} onClick={save}>Save</Button>
      </SimpleGrid>
    </Paper>
  );
}

function TemplateEditor({ form, setForm, onClose, onSaved }: { form: TplForm; setForm: (f: TplForm) => void; onClose: () => void; onSaved: () => void }) {
  const ph = useQuery(() => docsApi.placeholders(), []);
  const [emp, setEmp] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ html: string; missing: string[]; unknown: string[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!emp || !form.subject.trim() || !form.body.trim()) { setPreview(null); return; }
    let alive = true;
    const t = setTimeout(async () => {
      try { const r = await docsApi.preview({ employeeId: emp, subject: form.subject, body: form.body, purpose: 'the stated purpose', addressedTo: 'The Recipient' }); if (alive) setPreview(r); }
      catch { if (alive) setPreview(null); }
    }, 400);
    return () => { alive = false; clearTimeout(t); };
  }, [emp, form.subject, form.body]);

  const insert = (key: string) => {
    const el = bodyRef.current; const token = `{{${key}}}`;
    if (!el) { setForm({ ...form, body: form.body + token }); return; }
    const s = el.selectionStart ?? form.body.length; const e = el.selectionEnd ?? s;
    const next = form.body.slice(0, s) + token + form.body.slice(e);
    setForm({ ...form, body: next });
    requestAnimationFrame(() => { el.focus(); el.setSelectionRange(s + token.length, s + token.length); });
  };

  const save = async () => {
    setBusy(true);
    try {
      const body = { name: form.name.trim(), subject: form.subject.trim(), body: form.body, requestable: form.requestable, visibleToEmployee: form.visibleToEmployee };
      if (form.id) await docsApi.updateTemplate(form.id, { ...body, active: form.active });
      else await docsApi.createTemplate({ code: form.code.trim(), ...body });
      toast.ok('Template saved.'); onSaved(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };

  return (
    <Stack gap="md">
      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="lg">
        <Stack gap="sm">
          <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="sm">
            <TextInput label="Name" required value={form.name} maxLength={80} onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} />
            <TextInput label="Code" required value={form.code} disabled={!!form.id} maxLength={40} description="Lowercase, digits and _"
              onChange={(e) => setForm({ ...form, code: e.currentTarget.value.toLowerCase().replace(/[^a-z0-9_]/g, '_') })} />
          </SimpleGrid>
          <TextInput label="Subject line" required value={form.subject} maxLength={160} onChange={(e) => setForm({ ...form, subject: e.currentTarget.value })} />
          <Textarea ref={bodyRef} label="Letter text" required autosize minRows={14} maxRows={26} value={form.body} maxLength={20000}
            styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)', fontSize: 12.5, lineHeight: 1.6 } }}
            description="Blank line = new paragraph. Lines starting with - become a list. **text** is bold."
            onChange={(e) => setForm({ ...form, body: e.currentTarget.value })} />
          <div>
            <Text size="sm" fw={500} mb={6}>Insert a detail</Text>
            <ScrollArea.Autosize mah={140}>
              <Group gap={6}>
                {(ph.data?.placeholders ?? []).map((p) => (
                  <Tooltip key={p.key} label={p.label} withinPortal>
                    <UnstyledButton onClick={() => insert(p.key)}><Code fz={11}>{`{{${p.key}}}`}</Code></UnstyledButton>
                  </Tooltip>
                ))}
              </Group>
            </ScrollArea.Autosize>
          </div>
          <Group gap="lg">
            <Switch label="Employees can request it" checked={form.requestable} onChange={(e) => setForm({ ...form, requestable: e.currentTarget.checked })} />
            <Switch label="Employee sees issued letters" checked={form.visibleToEmployee} onChange={(e) => setForm({ ...form, visibleToEmployee: e.currentTarget.checked })} />
            {form.id ? <Switch label="Active" checked={form.active} onChange={(e) => setForm({ ...form, active: e.currentTarget.checked })} /> : null}
          </Group>
        </Stack>
        <Stack gap="sm">
          <EmployeePicker label="Preview for" value={emp} onChange={(v) => setEmp(v)} status="all" />
          {preview?.unknown.length ? <Alert color="red" variant="light" title="Unknown placeholders">{preview.unknown.join(', ')} will not save. Use the list on the left.</Alert> : null}
          {preview?.missing.length ? <Text size="xs" c="orange.8">Missing for this employee: {preview.missing.join(', ')}</Text> : null}
          {preview ? <LetterFrame html={preview.html} height={620} /> : <EmptyState compact title="Live preview" hint="Choose an employee to see the letter exactly as it will print." />}
        </Stack>
      </SimpleGrid>
      <Group justify="flex-end">
        <Button variant="default" onClick={onClose}>Cancel</Button>
        <Button loading={busy} disabled={!form.name.trim() || !form.subject.trim() || !form.body.trim() || (!form.id && !/^[a-z0-9_]{2,40}$/.test(form.code))} onClick={save}>Save template</Button>
      </Group>
    </Stack>
  );
}

function TemplatesSection() {
  const q = useQuery(() => docsApi.templates(), []);
  const [form, setForm] = useState<TplForm | null>(null);
  const open = (t?: LetterTemplate) => setForm(t
    ? { id: t.id, code: t.code, name: t.name, subject: t.subject, body: t.body ?? '', requestable: !!t.requestable, visibleToEmployee: t.visible_to_employee !== false, active: t.active !== false, builtin: !!t.is_builtin }
    : { code: '', name: '', subject: '', body: 'Dear {{employee.title}} {{employee.name}},\n\n', requestable: false, visibleToEmployee: true, active: true, builtin: false });
  return (
    <Stack gap="md">
      <SignatoryCard />
      <Group justify="flex-end"><Button leftSection={<IconPlus {...ICON} />} onClick={() => open()}>New template</Button></Group>
      <DataTable<LetterTemplate> rows={q.data?.templates ?? []} rowKey={(t) => t.id} loading={q.loading} error={q.error} onRowClick={(t) => open(t)}
        emptyTitle="No templates" emptyHint="Create a template for the letters you issue most."
        columns={[
          { key: 'name', header: 'Template', render: (t) => <div><Text size="sm" fw={500} c={t.active === false ? 'dimmed' : 'inherit'}>{t.name}</Text><Text size="xs" c="dimmed" ff="monospace">{t.code}</Text></div> },
          { key: 'subject', header: 'Subject', render: (t) => <Text size="sm">{t.subject}</Text> },
          { key: 'flags', header: '', render: (t) => (
            <Group gap={4}>
              {t.requestable ? <Badge size="sm" variant="light">Requestable</Badge> : null}
              {t.is_builtin ? <Badge size="sm" variant="light" color="gray">Built in</Badge> : null}
              {t.active === false ? <Badge size="sm" variant="light" color="gray">Inactive</Badge> : null}
            </Group>
          ) },
          { key: 'upd', header: 'Updated', render: (t) => <Text size="sm">{fmtDate(nptDateOf(t.updated_at ?? null))}</Text> },
        ]} />
      <Drawer opened={!!form} onClose={() => setForm(null)} position="right" size="100%" title={<Text fw={600}>{form?.id ? `Edit ${form.name}` : 'New letter template'}</Text>}>
        {form ? <TemplateEditor form={form} setForm={setForm} onClose={() => setForm(null)} onSaved={() => { void q.reload(); }} /> : null}
      </Drawer>
    </Stack>
  );
}

// ---------------------------------------------------------------- policies
function PoliciesSection() {
  const [showArchived, setShowArchived] = useState(false);
  const q = useQuery(() => docsApi.policies(showArchived), [showArchived]);
  const branches = useQuery(() => settingsApi.branches(), []);
  const units = useQuery(() => settingsApi.orgUnits(), []);
  const [form, setForm] = useState<{ title: string; description: string; file: UploadedFile | null; audience: 'all' | 'branch' | 'department'; audienceIds: string[]; requiresAck: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [acksFor, setAcksFor] = useState<Policy | null>(null);
  const [archiving, setArchiving] = useState<Policy | null>(null);
  const acks = useQuery(() => (acksFor ? docsApi.policyAcks(acksFor.id) : Promise.resolve({ employees: [] })), [acksFor?.id]);

  const publish = async () => {
    if (!form?.file) return;
    setBusy(true);
    try {
      await docsApi.publishPolicy({ title: form.title.trim(), description: form.description.trim() || null, fileId: form.file.id, audience: form.audience, audienceIds: form.audience === 'all' ? [] : form.audienceIds, requiresAck: form.requiresAck });
      toast.ok('Published. Everyone it applies to has been notified.'); setForm(null); await q.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const remind = async (p: Policy) => {
    try { const r = await docsApi.remindPolicy(p.id); toast.ok(r.sent ? `Reminder sent to ${r.sent} ${r.sent === 1 ? 'person' : 'people'}.` : 'Everyone has acknowledged it.'); await acks.reload(); } catch (e) { toast.err(e); }
  };
  const audienceLabel = (p: Policy) => {
    if (!p.audience || p.audience === 'all') return 'Everyone';
    const names = (p.audience_ids ?? []).map((id) => (p.audience === 'branch' ? branches.data?.branches.find((b) => b.id === id)?.name_en : units.data?.orgUnits.find((u) => u.id === id)?.name_en) ?? '').filter(Boolean);
    return names.join(', ') || (p.audience === 'branch' ? 'Selected branches' : 'Selected departments');
  };
  const options = form?.audience === 'branch' ? (branches.data?.branches ?? []).map((b) => ({ value: b.id, label: b.name_en })) : (units.data?.orgUnits ?? []).map((u) => ({ value: u.id, label: u.name_en }));

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="wrap">
        <Switch label="Show archived" checked={showArchived} onChange={(e) => setShowArchived(e.currentTarget.checked)} />
        <Button leftSection={<IconPlus {...ICON} />} onClick={() => setForm({ title: '', description: '', file: null, audience: 'all', audienceIds: [], requiresAck: true })}>Publish a policy</Button>
      </Group>
      <DataTable<Policy> rows={q.data?.policies ?? []} rowKey={(p) => p.id} loading={q.loading} error={q.error}
        emptyTitle="No policies published" emptyHint="Publish the employee handbook, code of conduct or leave policy and track who has read it."
        columns={[
          { key: 't', header: 'Policy', render: (p) => <div><Group gap={6}><Text size="sm" fw={500}>{p.title}</Text>{p.archived_at ? <Badge size="xs" color="gray" variant="light">Archived</Badge> : null}</Group><Text size="xs" c="dimmed">{audienceLabel(p)} · published {fmtDate(nptDateOf(p.published_at))}</Text></div> },
          { key: 'a', header: 'Acknowledged', render: (p) => p.requires_ack ? (
            <Stack gap={4} w={160}>
              <Text size="xs">{p.acks ?? 0} of {p.audience_size ?? 0}</Text>
              <Progress size="sm" value={p.audience_size ? ((p.acks ?? 0) / p.audience_size) * 100 : 0} />
            </Stack>
          ) : <Text size="xs" c="dimmed">For information</Text> },
          { key: 'act', header: '', align: 'right', render: (p) => (
            <Group gap={4} justify="flex-end" wrap="nowrap">
              <Button size="compact-sm" variant="subtle" component="a" href={docsApi.policyUrl(p.id)} target="_blank" rel="noopener" onClick={(e) => e.stopPropagation()}>Open</Button>
              {p.requires_ack ? <Button size="compact-sm" variant="subtle" onClick={() => setAcksFor(p)}>Who has read it</Button> : null}
              {!p.archived_at ? <Button size="compact-sm" variant="subtle" color="gray" leftSection={<IconArchive size={14} />} onClick={() => setArchiving(p)}>Archive</Button> : null}
            </Group>
          ) },
        ]} />

      <Drawer opened={!!form} onClose={() => setForm(null)} position="right" size="md" title={<Text fw={600}>Publish a policy</Text>}>
        {form ? (
          <Stack gap="md">
            <TextInput label="Title" required maxLength={160} value={form.title} onChange={(e) => setForm({ ...form, title: e.currentTarget.value })} placeholder="e.g. Employee handbook 2083" />
            <Textarea label="Short description (optional)" autosize minRows={2} maxLength={2000} value={form.description} onChange={(e) => setForm({ ...form, description: e.currentTarget.value })} />
            <div>
              <Text size="sm" fw={500} mb={6}>Document</Text>
              <FileUpload purpose="company_document" value={form.file} onChange={(f) => setForm({ ...form, file: f, title: form.title || (f?.filename.replace(/\.[a-z]+$/i, '') ?? '') })} label="Choose PDF" />
            </div>
            <Select label="Who it applies to" value={form.audience} allowDeselect={false} onChange={(v) => v && setForm({ ...form, audience: v as 'all' | 'branch' | 'department', audienceIds: [] })}
              data={[{ value: 'all', label: 'Everyone' }, { value: 'branch', label: 'Selected branches' }, { value: 'department', label: 'Selected departments' }]} />
            {form.audience !== 'all' ? <MultiSelect label={form.audience === 'branch' ? 'Branches' : 'Departments'} data={options} value={form.audienceIds} searchable onChange={(v) => setForm({ ...form, audienceIds: v })} /> : null}
            <Switch label="Ask everyone to acknowledge it" description="Employees confirm they have read it; reminders go out weekly until they do." checked={form.requiresAck} onChange={(e) => setForm({ ...form, requiresAck: e.currentTarget.checked })} />
            <Group justify="flex-end">
              <Button variant="default" onClick={() => setForm(null)}>Cancel</Button>
              <Button loading={busy} disabled={!form.title.trim() || !form.file || (form.audience !== 'all' && !form.audienceIds.length)} onClick={publish}>Publish</Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>

      <Drawer opened={!!acksFor} onClose={() => setAcksFor(null)} position="right" size="md" title={<Text fw={600}>{acksFor?.title}</Text>}>
        <Stack gap="md">
          <Group justify="space-between">
            <Text size="sm" c="dimmed">{(acks.data?.employees ?? []).filter((e) => e.acknowledged_at).length} of {(acks.data?.employees ?? []).length} have acknowledged.</Text>
            {acksFor && !acksFor.archived_at ? <Button size="xs" variant="light" leftSection={<IconBell size={14} />} onClick={() => remind(acksFor)}>Remind the rest</Button> : null}
          </Group>
          <DataTable rows={acks.data?.employees ?? []} rowKey={(e) => e.employee_id} loading={acks.loading} emptyTitle="Nobody in the audience"
            columns={[
              { key: 'n', header: 'Employee', render: (e) => <div><Text size="sm">{e.name_en}</Text><Text size="xs" c="dimmed">{e.emp_code}</Text></div> },
              { key: 'a', header: 'Status', render: (e) => e.acknowledged_at ? <Badge variant="light" color="brand" size="sm">Read {fmtDate(nptDateOf(e.acknowledged_at))}</Badge>
                : <Text size="xs" c="dimmed">Not yet{e.last_reminded_at ? `, reminded ${fmtDate(nptDateOf(e.last_reminded_at))}` : ''}</Text> },
            ]} />
        </Stack>
      </Drawer>

      <ConfirmDialog opened={!!archiving} title="Archive policy?" confirmLabel="Archive" color="gray"
        message={archiving ? `${archiving.title} will be hidden from employees and reminders stop. Acknowledgements are kept.` : ''}
        onConfirm={async () => { if (!archiving) return; try { await docsApi.updatePolicy(archiving.id, { archived: true }); toast.ok('Archived.'); await q.reload(); } catch (e) { toast.err(e); } }}
        onClose={() => setArchiving(null)} />
    </Stack>
  );
}

// ---------------------------------------------------------------- categories
function CategoriesSection() {
  const q = useQuery(() => docsApi.categories(), []);
  const [adding, setAdding] = useState<{ name: string; code: string; employeeCanUpload: boolean; expiryRequired: boolean; visibleToEmployee: boolean } | null>(null);
  const toggle = async (c: DocCategory, key: 'employeeCanUpload' | 'expiryRequired' | 'visibleToEmployee' | 'active', value: boolean) => {
    try { await docsApi.updateCategory(c.id, { [key]: value }); await q.reload(); } catch (e) { toast.err(e); }
  };
  const add = async () => {
    if (!adding) return;
    try { await docsApi.createCategory(adding); toast.ok('Document type added.'); setAdding(null); await q.reload(); } catch (e) { toast.err(e); }
  };
  return (
    <Stack gap="md">
      <Group justify="flex-end"><Button leftSection={<IconPlus {...ICON} />} onClick={() => setAdding({ name: '', code: '', employeeCanUpload: true, expiryRequired: false, visibleToEmployee: true })}>New document type</Button></Group>
      <DataTable<DocCategory> rows={q.data?.categories ?? []} rowKey={(c) => c.id} loading={q.loading} error={q.error}
        columns={[
          { key: 'n', header: 'Document type', render: (c) => <div><Text size="sm" fw={500} c={c.active ? 'inherit' : 'dimmed'}>{c.name}</Text><Text size="xs" c="dimmed" ff="monospace">{c.code}</Text></div> },
          { key: 'u', header: 'Employees upload', render: (c) => <Switch size="sm" checked={c.employee_can_upload} onChange={(e) => toggle(c, 'employeeCanUpload', e.currentTarget.checked)} aria-label="Employees can upload" /> },
          { key: 'e', header: 'Expiry date required', render: (c) => <Switch size="sm" checked={c.expiry_required} onChange={(e) => toggle(c, 'expiryRequired', e.currentTarget.checked)} aria-label="Expiry required" /> },
          { key: 'v', header: 'Visible to employee', render: (c) => <Switch size="sm" checked={c.visible_to_employee} onChange={(e) => toggle(c, 'visibleToEmployee', e.currentTarget.checked)} aria-label="Visible to employee" /> },
          { key: 'a', header: 'In use', render: (c) => <Switch size="sm" checked={c.active} onChange={(e) => toggle(c, 'active', e.currentTarget.checked)} aria-label="Active" /> },
        ]} />
      <Drawer opened={!!adding} onClose={() => setAdding(null)} position="right" size="sm" title={<Text fw={600}>New document type</Text>}>
        {adding ? (
          <Stack gap="md">
            <TextInput label="Name" required maxLength={80} value={adding.name} onChange={(e) => {
              const name = e.currentTarget.value;
              setAdding({ ...adding, name, code: adding.code && adding.code !== slug(adding.name) ? adding.code : slug(name) });
            }} placeholder="e.g. Work permit" />
            <TextInput label="Code" required maxLength={40} value={adding.code} onChange={(e) => setAdding({ ...adding, code: slug(e.currentTarget.value) })} />
            <Switch label="Employees can upload it" checked={adding.employeeCanUpload} onChange={(e) => setAdding({ ...adding, employeeCanUpload: e.currentTarget.checked })} />
            <Switch label="Needs an expiry date" checked={adding.expiryRequired} onChange={(e) => setAdding({ ...adding, expiryRequired: e.currentTarget.checked })} />
            <Switch label="Visible to the employee" checked={adding.visibleToEmployee} onChange={(e) => setAdding({ ...adding, visibleToEmployee: e.currentTarget.checked })} />
            <Group justify="flex-end"><Button variant="default" onClick={() => setAdding(null)}>Cancel</Button><Button disabled={!adding.name.trim() || !/^[a-z0-9_]{2,40}$/.test(adding.code)} onClick={add}>Add</Button></Group>
          </Stack>
        ) : null}
      </Drawer>
    </Stack>
  );
}
const slug = (s: string) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40);

/** Employees → Documents & letters: verification queue, letters, templates, company policies and document types. */
export default function DocumentsAdminPage() {
  const [section, setSection] = useState<Section>('review');
  const views = useMemo(() => ({
    review: <ReviewSection />, letters: <LettersSection />, templates: <TemplatesSection />, policies: <PoliciesSection />, categories: <CategoriesSection />,
  }), []);
  return (
    <Stack gap="lg">
      <PageHeader title="Documents & letters" subtitle="Check uploaded documents, issue letters from templates with reference numbers, and publish policies employees must acknowledge."
        actions={<Button variant="default" leftSection={<IconPrinter {...ICON} />} onClick={() => setSection('letters')}>Letters register</Button>} />
      <ScrollArea type="never">
        <SegmentedControl value={section} onChange={(v) => setSection(v as Section)} data={[
          { value: 'review', label: 'To review' }, { value: 'letters', label: 'Letters' }, { value: 'templates', label: 'Templates' },
          { value: 'policies', label: 'Policies' }, { value: 'categories', label: 'Document types' },
        ]} />
      </ScrollArea>
      {views[section]}
    </Stack>
  );
}
