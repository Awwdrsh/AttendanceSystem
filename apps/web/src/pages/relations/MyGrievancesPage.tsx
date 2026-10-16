import { Alert, Badge, Button, Drawer, Group, Modal, Paper, Select, Stack, Text, Textarea, TextInput, UnstyledButton } from '@mantine/core';
import { IconLock, IconPlus } from '@tabler/icons-react';
import { useState } from 'react';
import type { UploadedFile } from '../../api/files';
import { GRIEVANCE_CATEGORY_LABEL, relations, type Grievance, type GrievanceCategory } from '../../api/relations';
import { EmptyState } from '../../components/EmptyState';
import { FileUpload } from '../../components/FileUpload';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { GrievanceStatusBadge, GrievanceThread } from './GrievanceThread';

const CATEGORIES = (Object.keys(GRIEVANCE_CATEGORY_LABEL) as GrievanceCategory[]).map((v) => ({ value: v, label: GRIEVANCE_CATEGORY_LABEL[v] }));

function FileGrievance({ opened, onClose, onCreated }: { opened: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const [category, setCategory] = useState<GrievanceCategory | null>(null);
  const [subject, setSubject] = useState(''); const [description, setDescription] = useState(''); const [concerns, setConcerns] = useState('');
  const [files, setFiles] = useState<UploadedFile[]>([]); const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!category) return;
    setBusy(true);
    try {
      const r = await relations.fileGrievance({ category, subject: subject.trim(), description: description.trim(), concerns: concerns.trim() || null, fileIds: files.map((f) => f.id) });
      toast.ok(`Grievance G-${r.number} filed. The committee will be in touch.`);
      setCategory(null); setSubject(''); setDescription(''); setConcerns(''); setFiles([]);
      onCreated(r.id);
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={opened} onClose={onClose} title="Raise a grievance" centered size="lg">
      <Stack>
        <Alert variant="light" color="grape" icon={<IconLock size={16} stroke={1.6} />}>
          Your grievance goes to the grievance committee and HR, never to your manager. It is confidential, not anonymous: the committee needs to know who you are to look into it.
        </Alert>
        <Select label="What is it about?" data={CATEGORIES} value={category} onChange={(v) => setCategory(v as GrievanceCategory)} allowDeselect={false} required />
        <TextInput label="Subject" value={subject} onChange={(e) => setSubject(e.currentTarget.value)} maxLength={160} required />
        <Textarea label="What happened?" description="Dates, places and who was there help the committee." value={description} onChange={(e) => setDescription(e.currentTarget.value)} autosize minRows={5} maxLength={8000} required />
        <TextInput label="Who or what it concerns (optional)" value={concerns} onChange={(e) => setConcerns(e.currentTarget.value)} maxLength={500} />
        <Group gap="xs">
          {files.map((f) => <Badge key={f.id} variant="light" color="gray">{f.filename}</Badge>)}
          {files.length < 5 ? <FileUpload purpose="grievance_attachment" value={null} label="Attach evidence" onChange={(f) => f && setFiles([...files, f])} /> : null}
        </Group>
        <Group justify="flex-end"><Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!category || !subject.trim() || !description.trim()} onClick={submit}>Submit grievance</Button></Group>
      </Stack>
    </Modal>
  );
}

export function GrievanceRow({ g, onOpen, showName }: { g: Grievance; onOpen: () => void; showName?: boolean }) {
  return (
    <UnstyledButton onClick={onOpen} p="sm" style={{ borderRadius: 8, display: 'block', width: '100%' }} className="hj-row-hover">
      <Group justify="space-between" wrap="nowrap" gap="sm">
        <div style={{ minWidth: 0 }}>
          <Text size="sm" fw={500} truncate>{g.subject}</Text>
          <Text size="xs" c="dimmed">G-{g.number}, {GRIEVANCE_CATEGORY_LABEL[g.category]}{showName && g.employeeName ? `, ${g.employeeName}` : ''}, updated {relativeTime(g.updatedAt)}</Text>
        </div>
        <GrievanceStatusBadge status={g.status} />
      </Group>
    </UnstyledButton>
  );
}

/** Employee: raise and follow grievances. Committee members who are not HR also handle cases from here. */
export default function MyGrievancesPage() {
  const q = useQuery(() => relations.grievances('mine'), []);
  const committee = useQuery(() => relations.committee(), []);
  const handle = useQuery(() => relations.grievances('handle'), [committee.data?.isMember], { enabled: !!committee.data?.isMember });
  const [creating, setCreating] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const mine = q.data?.grievances ?? [];
  return (
    <Stack gap="lg">
      <PageHeader title="Grievances" subtitle="Raise a formal complaint about how you are treated at work. A committee hears it, not your manager."
        actions={<Button leftSection={<IconPlus size={16} stroke={1.6} />} onClick={() => setCreating(true)}>Raise a grievance</Button>} />
      {q.error ? <EmptyState title="Could not load your grievances" hint={q.error} /> : null}
      {!q.loading && !q.error && !mine.length ? <EmptyState title="No grievances" hint="If something at work is unfair or unsafe, raise it here. Only HR and the grievance committee see it." action={{ label: 'Raise a grievance', onClick: () => setCreating(true) }} /> : null}
      {mine.length ? <Paper withBorder p={4}><Stack gap={0}>{mine.map((g) => <GrievanceRow key={g.id} g={g} onOpen={() => setOpenId(g.id)} />)}</Stack></Paper> : null}
      {committee.data?.isMember ? (
        <div>
          <SectionTitle hint="You are on the grievance committee. Keep these confidential.">Cases for the committee</SectionTitle>
          {(handle.data?.grievances ?? []).length
            ? <Paper withBorder p={4}><Stack gap={0}>{handle.data!.grievances.map((g) => <GrievanceRow key={g.id} g={g} showName onOpen={() => setOpenId(g.id)} />)}</Stack></Paper>
            : <EmptyState compact title="No cases" hint="New grievances appear here and you get a notification." />}
        </div>
      ) : null}
      <FileGrievance opened={creating} onClose={() => setCreating(false)} onCreated={(id) => { setCreating(false); void q.reload(); setOpenId(id); }} />
      <Drawer opened={!!openId} onClose={() => setOpenId(null)} position="right" size="lg" title={<Text fw={600}>Grievance</Text>}>
        {openId ? <GrievanceThread id={openId} onChanged={() => { void q.reload(); void handle.reload(); }} /> : null}
      </Drawer>
    </Stack>
  );
}
