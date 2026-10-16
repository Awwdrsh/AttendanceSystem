import { Badge, Button, Checkbox, Drawer, Group, Modal, Progress, SegmentedControl, Select, Stack, Text, Textarea, TextInput } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { people } from '../../api/people';
import { KYE_SECTION_LABEL, relations, type Completeness, type KyeCampaign, type KyeResponse, type KyeSection } from '../../api/relations';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { addDays, fmtAd, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const SECTIONS = Object.keys(KYE_SECTION_LABEL) as KyeSection[];

function Launch({ opened, onClose, onDone }: { opened: boolean; onClose: () => void; onDone: () => void }) {
  const structure = useQuery(() => people.structure(), [opened], { enabled: opened });
  const [title, setTitle] = useState('Annual record check'); const [message, setMessage] = useState('');
  const [sections, setSections] = useState<KyeSection[]>(['personal', 'contact', 'emergency', 'family', 'bank']);
  const [aud, setAud] = useState<'all' | 'branch' | 'department'>('all'); const [audId, setAudId] = useState<string | null>(null);
  const [due, setDue] = useState<string | null>(addDays(todayNpt(), 14)); const [busy, setBusy] = useState(false);
  return (
    <Modal opened={opened} onClose={onClose} title="Ask employees to review their records" centered size="lg">
      <Stack>
        <TextInput label="Title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} maxLength={160} required />
        <Textarea label="Message (optional)" value={message} onChange={(e) => setMessage(e.currentTarget.value)} autosize maxLength={2000} />
        <Checkbox.Group label="Sections to review" value={sections} onChange={(v) => setSections(v as KyeSection[])}>
          <Group mt={6} gap="md">{SECTIONS.map((s) => <Checkbox key={s} value={s} label={KYE_SECTION_LABEL[s]} />)}</Group>
        </Checkbox.Group>
        <Group grow>
          <Select label="Who" value={aud} onChange={(v) => { setAud(v as 'all'); setAudId(null); }} allowDeselect={false} data={[{ value: 'all', label: 'Everyone' }, { value: 'branch', label: 'One branch' }, { value: 'department', label: 'One department' }]} />
          {aud !== 'all' ? <Select label={aud === 'branch' ? 'Branch' : 'Department'} searchable value={audId} onChange={setAudId}
            data={(aud === 'branch' ? structure.data?.branches ?? [] : structure.data?.units ?? []).map((x) => ({ value: x.id, label: x.name_en }))} /> : <span />}
        </Group>
        <DateField label="Confirm by" value={due} onChange={setDue} minDate={todayNpt()} required />
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!title.trim() || !sections.length || !due || (aud !== 'all' && !audId)} onClick={async () => {
            setBusy(true);
            try { const r = await relations.launchCampaign({ title: title.trim(), message: message.trim() || null, sections, audienceKind: aud, audienceId: audId, dueDate: due! }); toast.ok(`Sent to ${r.employees} employees.`); onDone(); onClose(); }
            catch (e) { toast.err(e); } finally { setBusy(false); }
          }}>Send</Button></Group>
      </Stack>
    </Modal>
  );
}

function CampaignDetail({ id, onChanged }: { id: string; onChanged: () => void }) {
  const q = useQuery(() => relations.campaign(id), [id]);
  const [busy, setBusy] = useState(false);
  if (!q.data) return <Text size="sm" c="dimmed">{q.error ?? 'Loading…'}</Text>;
  const c = q.data.campaign;
  return (
    <Stack>
      <Text fw={600}>{c.title}</Text>
      <Text size="sm" c="dimmed">Confirm by {fmtAd(c.due_date)}. Sections: {c.sections.map((s) => KYE_SECTION_LABEL[s]).join(', ')}.</Text>
      {c.status === 'open' ? (
        <Group gap="xs">
          <Button size="xs" variant="default" loading={busy} onClick={async () => { setBusy(true); try { const r = await relations.nudge(id); toast.ok(r.nudged ? `Reminded ${r.nudged} people.` : 'Everyone pending was already reminded today.'); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>Remind those pending</Button>
          <Button size="xs" variant="subtle" color="gray" onClick={async () => { try { await relations.closeCampaign(id); toast.ok('Closed.'); onChanged(); await q.reload(); } catch (e) { toast.err(e); } }}>Close</Button>
        </Group>
      ) : null}
      <DataTable<KyeResponse> rows={q.data.responses} rowKey={(r) => r.employee_id}
        columns={[
          { key: 'e', header: 'Employee', render: (r) => <Link to={`/employees/${r.employee_id}`} style={{ color: 'inherit' }}><Text size="sm">{r.name_en}</Text><Text size="xs" c="dimmed">{r.emp_code}</Text></Link> },
          { key: 's', header: 'Response', render: (r) => <Badge variant="light" color={r.status === 'confirmed' ? 'teal' : r.status === 'changes_requested' ? 'orange' : 'gray'}>{r.status === 'confirmed' ? 'Confirmed' : r.status === 'changes_requested' ? 'Asked for changes' : 'Pending'}</Badge> },
          { key: 'c', header: 'Record', render: (r) => r.completeness === null ? '' : <Text size="sm" {...(r.completeness < 100 ? { c: 'orange' } : {})}>{r.completeness}%</Text> },
          { key: 'w', header: 'When', render: (r) => r.responded_at ? relativeTime(r.responded_at) : r.last_nudged_at ? `reminded ${relativeTime(r.last_nudged_at)}` : '' },
        ]} />
    </Stack>
  );
}

/** HR: Know Your Employee campaigns that keep records current, and the record completeness report. */
export default function KyePage() {
  const camps = useQuery(() => relations.campaigns(), []);
  const comp = useQuery(() => relations.completeness(), []);
  const [view, setView] = useState<'incomplete' | 'all'>('incomplete');
  const [launching, setLaunching] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const rows = (comp.data?.employees ?? []).filter((e) => view === 'all' || e.percent < 100);
  const avg = comp.data?.employees.length ? Math.round(comp.data.employees.reduce((s, e) => s + e.percent, 0) / comp.data.employees.length) : null;
  return (
    <Stack gap="lg">
      <PageHeader title="Know your employee" subtitle="Ask employees to review and confirm their records. Changes they need go through the usual profile change approval."
        actions={<Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setLaunching(true)}>New review request</Button>} />
      <div>
        <SectionTitle>Review requests</SectionTitle>
        {!camps.loading && !(camps.data?.campaigns ?? []).length ? <EmptyState compact title="None yet" hint="Send one a year, or before payroll go-live, so bank, ID and family details are current." /> : null}
        {(camps.data?.campaigns ?? []).length ? (
          <DataTable<KyeCampaign> rows={camps.data!.campaigns} rowKey={(r) => r.id} onRowClick={(r) => setOpenId(r.id)}
            columns={[
              { key: 't', header: 'Request', render: (r) => <><Text size="sm">{r.title}</Text><Text size="xs" c="dimmed">by {fmtAd(r.due_date)}</Text></> },
              { key: 'p', header: 'Confirmed', render: (r) => <Group gap="xs" wrap="nowrap"><Progress w={90} value={r.total ? (r.confirmed / r.total) * 100 : 0} /><Text size="xs">{r.confirmed} of {r.total}</Text></Group> },
              { key: 'c', header: 'Asked for changes', render: (r) => r.changes_requested || '' },
              { key: 's', header: 'Status', render: (r) => <Badge variant="light" color={r.status === 'open' ? 'blue' : 'gray'}>{r.status === 'open' ? 'Open' : 'Closed'}</Badge> },
            ]} />
        ) : null}
      </div>
      <div>
        <SectionTitle hint={avg !== null ? `Average record ${avg}% complete. Previous employment is optional and not counted.` : undefined}
          actions={<SegmentedControl size="xs" value={view} onChange={(v) => setView(v as 'all')} data={[{ value: 'incomplete', label: 'Incomplete' }, { value: 'all', label: 'All' }]} />}>Record completeness</SectionTitle>
        <DataTable<Completeness> rows={rows} rowKey={(r) => r.employeeId} loading={comp.loading} error={comp.error} emptyTitle="Every record is complete"
          columns={[
            { key: 'e', header: 'Employee', render: (r) => <Link to={`/employees/${r.employeeId}`} style={{ color: 'inherit' }}><Text size="sm">{r.name}</Text><Text size="xs" c="dimmed">{r.empCode}</Text></Link> },
            { key: 'p', header: 'Complete', render: (r) => <Group gap="xs" wrap="nowrap"><Progress w={80} value={r.percent} color={r.percent < 60 ? 'red' : r.percent < 100 ? 'orange' : 'teal'} /><Text size="xs">{r.percent}%</Text></Group> },
            { key: 'm', header: 'Missing', render: (r) => <Group gap={4}>{r.missing.map((m) => <Badge key={m} size="xs" variant="light" color="gray">{KYE_SECTION_LABEL[m]}</Badge>)}</Group> },
          ]} />
      </div>
      <Launch opened={launching} onClose={() => setLaunching(false)} onDone={() => { void camps.reload(); }} />
      <Drawer opened={!!openId} onClose={() => setOpenId(null)} position="right" size="lg" title={<Text fw={600}>Review request</Text>}>
        {openId ? <CampaignDetail id={openId} onChanged={() => { void camps.reload(); }} /> : null}
      </Drawer>
    </Stack>
  );
}
