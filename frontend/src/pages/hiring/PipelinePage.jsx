import {
  ActionIcon,
  Badge,
  Box,
  Button,
  Group,
  Menu,
  Modal,
  NumberInput,
  Paper,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
  UnstyledButton,
} from '@mantine/core';
import { IconArrowRight, IconCalendarEvent, IconDots, IconPlus, IconStarFilled, IconUsers } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useAuth } from '../../auth/AuthProvider';
import { hiring } from '../../api/hiring';
import { EmptyState } from '../../components/EmptyState';
import { FileUpload } from '../../components/FileUpload';
import { PageHeader } from '../../components/PageHeader';
import { relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { CandidateDrawer } from './CandidateDrawer';
import { fmtWhen, ICON, ScoreBadge } from './shared';

function Card({ c, stages, onOpen, onMove }) {
  const next = stages[stages.indexOf(c.stage) + 1];
  return (
    <Paper withBorder radius="md" p="sm" style={{ opacity: c.status === 'active' ? 1 : 0.6 }}>
      <Group justify="space-between" wrap="nowrap" gap={4} align="flex-start">
        <UnstyledButton onClick={onOpen} style={{ minWidth: 0, flex: 1 }}>
          <Text size="sm" fw={600} truncate>
            {c.name}
          </Text>
          <Text size="xs" c="dimmed" truncate>
            {c.experience_years === null ? 'Experience not given' : `${Number(c.experience_years)} yrs`}
            {c.current_employer ? ` · ${c.current_employer}` : ''}
          </Text>
        </UnstyledButton>
        {c.status === 'active' ? (
          <Menu position="bottom-end" withinPortal>
            <Menu.Target>
              <ActionIcon variant="subtle" color="gray" size="sm" aria-label={`Move ${c.name}`}>
                <IconDots size={14} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              <Menu.Label>Move to</Menu.Label>
              {stages
                .slice(0, -1)
                .filter((s) => s !== c.stage)
                .map((s) => (
                  <Menu.Item key={s} onClick={() => onMove(s)}>
                    {s}
                  </Menu.Item>
                ))}
            </Menu.Dropdown>
          </Menu>
        ) : null}
      </Group>
      <Group gap={6} mt={6} wrap="wrap">
        <ScoreBadge score={c.score} knockedOut={c.knocked_out} />
        {c.rating ? (
          <Badge size="sm" variant="light" color="yellow" leftSection={<IconStarFilled size={10} />}>
            {c.rating}
          </Badge>
        ) : null}
        {c.other_applications > 0 ? (
          <Badge size="sm" variant="light" color="grape">
            Applied before
          </Badge>
        ) : null}
        {c.status !== 'active' ? (
          <Badge size="sm" variant="light" color="gray">
            {c.status === 'hired' ? 'Hired' : 'Closed'}
          </Badge>
        ) : null}
        {c.tags.slice(0, 3).map((t) => (
          <Badge key={t} size="sm" variant="outline" color="gray">
            {t}
          </Badge>
        ))}
      </Group>
      <Group justify="space-between" mt={6} gap={4}>
        <Text size="xs" c="dimmed">
          {c.next_interview ? (
            <>
              <IconCalendarEvent size={12} style={{ verticalAlign: -2 }} /> {fmtWhen(c.next_interview)}
            </>
          ) : (
            relativeTime(c.stage_changed_at)
          )}
        </Text>
        {c.status === 'active' && next && next !== stages[stages.length - 1] ? (
          <Button size="compact-xs" variant="subtle" rightSection={<IconArrowRight size={12} />} onClick={() => onMove(next)}>
            {next}
          </Button>
        ) : null}
      </Group>
    </Paper>
  );
}

const BLANK = { name: '', email: '', phone: '', source: 'referral', experienceYears: '', currentEmployer: '', coverNote: '' };

function AddCandidate({ opened, jobId, onClose, onSaved }) {
  const [f, setF] = useState(BLANK);
  const [cv, setCv] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opened) {
      setF(BLANK);
      setCv(null);
    }
  }, [opened]);
  const save = async () => {
    setBusy(true);
    try {
      const r = await hiring.addCandidate(jobId, {
        name: f.name,
        email: f.email,
        phone: f.phone || null,
        source: f.source,
        experienceYears: f.experienceYears === '' ? null : f.experienceYears,
        currentEmployer: f.currentEmployer || null,
        coverNote: f.coverNote || null,
        cvFileId: cv?.id ?? null,
      });
      toast.ok('Candidate added.');
      onSaved(r.id);
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>Add a candidate</Text>} size="lg">
      <Stack gap="md">
        <Text size="sm" c="dimmed">
          For referrals and CVs that reached you directly. If the job has required screening questions, the candidate should apply through
          the careers page instead.
        </Text>
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <TextInput
            label="Full name"
            required
            value={f.name}
            onChange={(e) => setF({ ...f, name: e.currentTarget.value })}
            maxLength={120}
          />
          <TextInput
            label="E-mail"
            required
            type="email"
            value={f.email}
            onChange={(e) => setF({ ...f, email: e.currentTarget.value })}
            maxLength={200}
          />
          <TextInput label="Phone" value={f.phone} onChange={(e) => setF({ ...f, phone: e.currentTarget.value })} maxLength={30} />
          <Select
            label="Source"
            allowDeselect={false}
            value={f.source}
            onChange={(v) => v && setF({ ...f, source: v })}
            data={[
              { value: 'referral', label: 'Referral' },
              { value: 'job_board', label: 'Job board' },
              { value: 'manual', label: 'Direct' },
              { value: 'other', label: 'Other' },
            ]}
          />
          <NumberInput
            label="Years of experience"
            min={0}
            max={60}
            decimalScale={1}
            value={f.experienceYears}
            onChange={(v) => setF({ ...f, experienceYears: v === '' ? '' : Number(v) })}
          />
          <TextInput
            label="Current employer"
            value={f.currentEmployer}
            onChange={(e) => setF({ ...f, currentEmployer: e.currentTarget.value })}
            maxLength={120}
          />
        </SimpleGrid>
        <Textarea
          label="Notes"
          autosize
          minRows={2}
          maxLength={4000}
          value={f.coverNote}
          onChange={(e) => setF({ ...f, coverNote: e.currentTarget.value })}
        />
        <FileUpload purpose="hiring_cv" value={cv} onChange={setCv} label="Attach CV (PDF)" accept="application/pdf" />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={f.name.trim().length < 2 || !/^\S+@\S+\.\S+$/.test(f.email)}>
            Add candidate
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

/** One job's candidates as columns by stage, best screening score first; open a card for the full profile and actions. */
export default function PipelinePage() {
  const { hasRole } = useAuth();
  const [params, setParams] = useSearchParams();
  const jobs = useQuery(() => hiring.jobs(), []);
  const jobId = params.get('job') ?? jobs.data?.jobs.find((j) => j.status === 'open')?.id ?? jobs.data?.jobs[0]?.id ?? null;
  const [includeClosed, setIncludeClosed] = useState(false);
  const board = useQuery(
    () => (jobId ? hiring.board(jobId, includeClosed ? 'all' : 'active') : Promise.resolve(null)),
    [jobId, includeClosed],
  );
  const [open, setOpen] = useState(params.get('candidate'));
  const [adding, setAdding] = useState(false);
  const columns = useMemo(() => {
    const stages = board.data?.stages ?? [];
    return stages.map((s) => ({ stage: s, cards: (board.data?.applications ?? []).filter((c) => c.stage === s) }));
  }, [board.data]);
  const stages = board.data?.stages ?? [];

  const move = async (c, stage) => {
    try {
      await hiring.move(c.id, stage);
      toast.ok(`${c.name} moved to ${stage}.`);
      await board.reload();
    } catch (e) {
      toast.err(e);
    }
  };
  const job = jobs.data?.jobs.find((j) => j.id === jobId);

  return (
    <Stack gap="lg">
      <PageHeader
        title="Candidates"
        subtitle="Candidates by stage. The screening score ranks each column; open a card to see why."
        actions={
          <>
            <Select
              w={280}
              placeholder="Choose a job"
              searchable
              allowDeselect={false}
              value={jobId}
              onChange={(v) => v && setParams({ job: v })}
              data={(jobs.data?.jobs ?? []).map((j) => ({
                value: j.id,
                label: `${j.title}${j.status === 'open' ? '' : ` (${j.status})`}`,
              }))}
            />
            {hasRole('hr') && jobId ? (
              <Button leftSection={<IconPlus {...ICON} />} onClick={() => setAdding(true)}>
                Add candidate
              </Button>
            ) : null}
          </>
        }
      />
      {!jobs.loading && !jobs.data?.jobs.length ? (
        <EmptyState
          icon={<IconUsers size={20} />}
          title="No jobs yet"
          hint="Create a job under Jobs and publish it; applications will appear here."
        />
      ) : null}
      {jobId ? (
        <>
          <Group justify="space-between">
            <Text size="sm" c="dimmed">
              {job ? `${job.active ?? 0} in progress · ${job.applications ?? 0} applications in total` : ''}
            </Text>
            <Switch
              size="sm"
              label="Show hired and closed"
              checked={includeClosed}
              onChange={(e) => setIncludeClosed(e.currentTarget.checked)}
            />
          </Group>
          <ScrollArea type="auto" offsetScrollbars>
            <Group align="flex-start" wrap="nowrap" gap="md" pb="sm">
              {columns.map((col) => (
                <Box key={col.stage} style={{ flex: '0 0 268px', width: 268 }}>
                  <Group justify="space-between" mb={8} px={2}>
                    <Text size="sm" fw={600}>
                      {col.stage}
                    </Text>
                    <Badge size="sm" variant="light" color="gray">
                      {col.cards.length}
                    </Badge>
                  </Group>
                  <Stack gap="xs" p={6} style={{ background: 'var(--hj-surface-2, rgba(0,0,0,0.03))', borderRadius: 12, minHeight: 120 }}>
                    {col.cards.map((c) => (
                      <Card key={c.id} c={c} stages={stages} onOpen={() => setOpen(c.id)} onMove={(s) => move(c, s)} />
                    ))}
                    {col.cards.length === 0 ? (
                      <Text size="xs" c="dimmed" ta="center" py="md">
                        No one here
                      </Text>
                    ) : null}
                  </Stack>
                </Box>
              ))}
            </Group>
          </ScrollArea>
        </>
      ) : null}
      <CandidateDrawer
        applicationId={open}
        onClose={() => setOpen(null)}
        onChanged={() => {
          void board.reload();
          void jobs.reload();
        }}
      />
      {jobId ? (
        <AddCandidate
          opened={adding}
          jobId={jobId}
          onClose={() => setAdding(false)}
          onSaved={(id) => {
            void board.reload();
            setOpen(id);
          }}
        />
      ) : null}
    </Stack>
  );
}
