import {
  Alert,
  Anchor,
  Badge,
  Button,
  Divider,
  Drawer,
  Group,
  Menu,
  Modal,
  MultiSelect,
  NumberInput,
  Paper,
  Rating,
  Select,
  SimpleGrid,
  Skeleton,
  Stack,
  Switch,
  Table,
  Tabs,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Timeline,
} from '@mantine/core';
import {
  IconCalendarPlus,
  IconChevronDown,
  IconDownload,
  IconFileText,
  IconMail,
  IconPhone,
  IconUserCheck,
  IconUserX,
  IconFileDollar,
  IconRefresh,
} from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { useAuth } from '../../auth/AuthProvider';
import { hiring, MODE_LABEL, RECOMMENDATION, SOURCE_LABEL } from '../../api/hiring';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { addDays, fmtDateShort, fmtMoney, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ApplicationStatusBadge, fmtWhen, ICON, OfferStatusBadge, openInTab, ScoreBadge, useLookups } from './shared';

export const DEFAULT_CRITERIA = ['Skills', 'Experience', 'Communication', 'Culture fit'];

// ---------------------------------------------------------------- scorecard

/** A panel member's scorecard: 1–5 per criterion and an overall recommendation. */
export function ScorecardModal({ interview, onClose, onSaved }) {
  const [ratings, setRatings] = useState({});
  const [rec, setRec] = useState(null);
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setRatings({});
    setRec(null);
    setNotes('');
  }, [interview?.id]);
  const save = async () => {
    if (!interview || !rec) return;
    setBusy(true);
    try {
      await hiring.scorecard(interview.id, { ratings, recommendation: rec, notes: notes || null });
      toast.ok('Scorecard saved.');
      onSaved();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      opened={!!interview}
      onClose={onClose}
      title={<Text fw={600}>Scorecard{interview?.candidate ? `: ${interview.candidate}` : ''}</Text>}
      size="md"
    >
      <Stack gap="md">
        {DEFAULT_CRITERIA.map((c) => (
          <Group key={c} justify="space-between">
            <Text size="sm">{c}</Text>
            <Rating value={ratings[c] ?? 0} onChange={(v) => setRatings((r) => ({ ...r, [c]: v }))} aria-label={`${c} rating`} />
          </Group>
        ))}
        <Select
          label="Recommendation"
          required
          value={rec}
          onChange={(v) => setRec(v)}
          data={Object.keys(RECOMMENDATION)
            .reverse()
            .map((k) => ({ value: k, label: RECOMMENDATION[k].label }))}
        />
        <Textarea
          label="Notes"
          description="Evidence for your ratings: what the candidate said or did."
          autosize
          minRows={3}
          maxLength={4000}
          value={notes}
          onChange={(e) => setNotes(e.currentTarget.value)}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!rec || Object.keys(ratings).filter((k) => ratings[k] > 0).length === 0}>
            Save scorecard
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

// ---------------------------------------------------------------- interview

function InterviewModal({ opened, appId, onClose, onSaved }) {
  const lookups = useLookups();
  const { user } = useAuth();
  const [f, setF] = useState({
    title: 'Interview',
    date: addDays(todayNpt(), 2),
    time: '11:00',
    durationMin: 45,
    mode: 'in_person',
    location: '',
    panel: [],
    notifyCandidate: true,
  });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opened && user) setF((x) => ({ ...x, panel: x.panel.length ? x.panel : [user.id] }));
  }, [opened, user]);
  const save = async () => {
    setBusy(true);
    try {
      await hiring.scheduleInterview(appId, { ...f, location: f.location || null });
      toast.ok(
        f.notifyCandidate
          ? 'Interview scheduled. The panel and the candidate have been told.'
          : 'Interview scheduled. The panel has been told.',
      );
      onSaved();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>Schedule an interview</Text>} size="lg">
      <Stack gap="md">
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <TextInput label="Title" value={f.title} onChange={(e) => setF({ ...f, title: e.currentTarget.value })} maxLength={80} />
          <Select
            label="How"
            allowDeselect={false}
            value={f.mode}
            onChange={(v) => v && setF({ ...f, mode: v })}
            data={[
              { value: 'in_person', label: 'In person' },
              { value: 'video', label: 'Video call' },
              { value: 'phone', label: 'Phone' },
            ]}
          />
          <DateField label="Date" value={f.date} onChange={(v) => setF({ ...f, date: v })} minDate={todayNpt()} required />
          <Group grow>
            <TextInput
              label="Time (Nepal)"
              type="time"
              value={f.time}
              onChange={(e) => setF({ ...f, time: e.currentTarget.value })}
              required
            />
            <NumberInput
              label="Minutes"
              min={10}
              max={480}
              step={15}
              value={f.durationMin}
              onChange={(v) => setF({ ...f, durationMin: Number(v) || 45 })}
            />
          </Group>
        </SimpleGrid>
        <TextInput
          label={f.mode === 'video' ? 'Meeting link' : f.mode === 'phone' ? 'Who calls whom' : 'Where'}
          value={f.location}
          onChange={(e) => setF({ ...f, location: e.currentTarget.value })}
          maxLength={300}
        />
        <MultiSelect
          label="Panel"
          description="Each panel member is notified and fills in a scorecard."
          searchable
          value={f.panel}
          onChange={(v) => setF({ ...f, panel: v })}
          maxValues={10}
          data={lookups.users.map((u) => ({ value: u.id, label: u.name }))}
        />
        <Switch
          label="E-mail the invitation to the candidate"
          checked={f.notifyCandidate}
          onChange={(e) => setF({ ...f, notifyCandidate: e.currentTarget.checked })}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!f.date || !/^\d{2}:\d{2}$/.test(f.time) || f.panel.length === 0}>
            Schedule
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

// ---------------------------------------------------------------- offer and hire

function OfferModal({ opened, appId, defaultDesignation, onClose, onSaved }) {
  const [f, setF] = useState({
    designation: defaultDesignation,
    salary: '',
    joinDate: addDays(todayNpt(), 30),
    expiresOn: addDays(todayNpt(), 7),
    notes: '',
  });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opened) setF((x) => ({ ...x, designation: x.designation || defaultDesignation }));
  }, [opened, defaultDesignation]);
  const save = async () => {
    setBusy(true);
    try {
      await hiring.createOffer(appId, {
        designation: f.designation,
        salary: String(f.salary),
        joinDate: f.joinDate,
        expiresOn: f.expiresOn,
        notes: f.notes || null,
      });
      toast.ok('Offer drafted. Another HR user needs to approve it before it goes to the candidate.');
      onSaved();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>Make an offer</Text>} size="lg">
      <Stack gap="md">
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <TextInput
            label="Designation"
            required
            value={f.designation}
            onChange={(e) => setF({ ...f, designation: e.currentTarget.value })}
            maxLength={120}
          />
          <NumberInput
            label="Monthly gross salary (NPR)"
            required
            min={1}
            thousandSeparator=","
            value={f.salary}
            onChange={(v) => setF({ ...f, salary: v === '' ? '' : Number(v) })}
          />
          <DateField label="Joining date" required value={f.joinDate} onChange={(v) => setF({ ...f, joinDate: v })} minDate={todayNpt()} />
          <DateField label="Offer valid until" value={f.expiresOn} onChange={(v) => setF({ ...f, expiresOn: v })} minDate={todayNpt()} />
        </SimpleGrid>
        <Textarea
          label="Extra terms for the letter"
          description="Optional. Blank lines start a new paragraph."
          autosize
          minRows={3}
          maxLength={4000}
          value={f.notes}
          onChange={(e) => setF({ ...f, notes: e.currentTarget.value })}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={f.designation.trim().length < 2 || !f.salary || !f.joinDate}>
            Send for approval
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function HireModal({ opened, appId, joinDate, onClose, onSaved }) {
  const lookups = useLookups();
  const [f, setF] = useState({ empCode: '', gender: null, shiftId: null, probationEnd: null });
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const r = await hiring.hire(appId, {
        empCode: f.empCode.trim(),
        ...(f.gender ? { gender: f.gender } : {}),
        ...(f.shiftId ? { shiftId: f.shiftId } : {}),
        probationEnd: f.probationEnd,
      });
      toast.ok('Employee record created.');
      onSaved(r.employeeId);
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>Hire and create the employee</Text>} size="md">
      <Stack gap="md">
        <Text size="sm" c="dimmed">
          The employee record is filled from the application and the accepted offer
          {joinDate ? `, joining on ${fmtDateShort(joinDate)}` : ''}. Department and manager come from the job; you can change them on the
          employee page.
        </Text>
        <TextInput
          label="Employee code"
          required
          value={f.empCode}
          onChange={(e) => setF({ ...f, empCode: e.currentTarget.value.toUpperCase() })}
          maxLength={32}
          data-autofocus
        />
        <Select
          label="Gender"
          clearable
          value={f.gender}
          onChange={(v) => setF({ ...f, gender: v })}
          data={[
            { value: 'female', label: 'Female' },
            { value: 'male', label: 'Male' },
            { value: 'other', label: 'Other' },
          ]}
        />
        <Select
          label="Shift"
          clearable
          value={f.shiftId}
          onChange={(v) => setF({ ...f, shiftId: v })}
          data={lookups.shifts.map((s) => ({ value: s.id, label: `${s.name_en} (${s.code})` }))}
        />
        <DateField
          label="Probation ends"
          value={f.probationEnd}
          onChange={(v) => setF({ ...f, probationEnd: v })}
          {...(joinDate ? { minDate: joinDate } : {})}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} loading={busy} disabled={!f.empCode.trim()} leftSection={<IconUserCheck {...ICON} />}>
            Hire
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function RejectModal({ opened, appId, name, onClose, onSaved }) {
  const [reason, setReason] = useState('');
  const [notifyCandidate, setNotify] = useState(true);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const r = await hiring.reject(appId, reason, notifyCandidate);
      toast.ok(r.emailed ? `${name} was told by e-mail.` : 'Application closed.');
      onSaved();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={opened} onClose={onClose} title={<Text fw={600}>Not moving forward with {name}</Text>} size="md">
      <Stack gap="md">
        <Textarea
          label="Reason"
          description="Kept for your records; it is not sent to the candidate."
          required
          autosize
          minRows={2}
          maxLength={500}
          value={reason}
          onChange={(e) => setReason(e.currentTarget.value)}
          data-autofocus
        />
        <Switch
          label="Send a polite e-mail to the candidate"
          checked={notifyCandidate}
          onChange={(e) => setNotify(e.currentTarget.checked)}
        />
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button color="red" onClick={save} loading={busy} disabled={reason.trim().length < 3}>
            Close application
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

// ---------------------------------------------------------------- drawer

const EVENT_LABEL = {
  applied: 'Applied',
  stage: 'Moved',
  note: 'Note',
  rating: 'Rating',
  email: 'E-mail sent',
  interview: 'Interview',
  scorecard: 'Scorecard',
  offer: 'Offer',
  hired: 'Hired',
  rejected: 'Not selected',
  reopened: 'Reopened',
};

/** Everything about one candidate for one job: profile, screening score, interviews, offers and activity, with the next actions. */
export function CandidateDrawer({ applicationId, onClose, onChanged }) {
  const { hasRole, user } = useAuth();
  const hr = hasRole('hr');
  const q = useQuery(() => (applicationId ? hiring.application(applicationId) : Promise.resolve(null)), [applicationId]);
  const [modal, setModal] = useState(null);
  const [scoring, setScoring] = useState(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(null);
  const d = q.data;
  const changed = async () => {
    await q.reload();
    onChanged?.();
  };
  const run = async (key, fn, ok) => {
    setBusy(key);
    try {
      await fn();
      if (ok) toast.ok(ok);
      await changed();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(null);
    }
  };

  const a = d?.application;
  const full = d?.access === 'full';
  const active = a?.status === 'active';
  const acceptedOffer = d?.offers.find((o) => o.status === 'accepted');
  const openOffer = d?.offers.find((o) => ['pending_approval', 'approved', 'accepted'].includes(o.status));
  const lastStage = d ? d.job.stages[d.job.stages.length - 1] : undefined;

  return (
    <Drawer opened={!!applicationId} onClose={onClose} position="right" size="xl" title={<Text fw={600}>{a?.name ?? 'Candidate'}</Text>}>
      {!d || !a ? (
        q.error ? (
          <EmptyState title="Could not open this candidate" hint={q.error} />
        ) : (
          <Stack>
            <Skeleton h={60} />
            <Skeleton h={200} />
          </Stack>
        )
      ) : (
        <Stack gap="lg">
          <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
            <Stack gap={4}>
              <Group gap="xs">
                <ApplicationStatusBadge status={a.status} />
                <Badge variant="outline" size="sm">
                  {a.stage}
                </Badge>
                <ScoreBadge score={a.score} knockedOut={a.knocked_out} />
              </Group>
              <Text size="sm" c="dimmed">
                {d.job.title} · {SOURCE_LABEL[a.source] ?? a.source} · applied {relativeTime(a.created_at)}
              </Text>
              <Group gap="md">
                {a.email ? (
                  <Anchor size="sm" href={`mailto:${a.email}`}>
                    <Group gap={4}>
                      <IconMail size={14} />
                      {a.email}
                    </Group>
                  </Anchor>
                ) : null}
                {a.phone ? (
                  <Anchor size="sm" href={`tel:${a.phone}`}>
                    <Group gap={4}>
                      <IconPhone size={14} />
                      {a.phone}
                    </Group>
                  </Anchor>
                ) : null}
              </Group>
            </Stack>
            <Group gap="xs">
              {a.has_cv ? (
                <Button variant="default" size="sm" leftSection={<IconFileText {...ICON} />} onClick={() => openInTab(hiring.cvUrl(a.id))}>
                  CV
                </Button>
              ) : null}
              {full && active ? (
                <Menu position="bottom-end" withinPortal>
                  <Menu.Target>
                    <Button size="sm" rightSection={<IconChevronDown size={14} />}>
                      Actions
                    </Button>
                  </Menu.Target>
                  <Menu.Dropdown>
                    <Menu.Item leftSection={<IconCalendarPlus {...ICON} />} onClick={() => setModal('interview')}>
                      Schedule interview
                    </Menu.Item>
                    {hr && !openOffer ? (
                      <Menu.Item leftSection={<IconFileDollar {...ICON} />} onClick={() => setModal('offer')}>
                        Make an offer
                      </Menu.Item>
                    ) : null}
                    {hr && acceptedOffer ? (
                      <Menu.Item leftSection={<IconUserCheck {...ICON} />} onClick={() => setModal('hire')}>
                        Hire
                      </Menu.Item>
                    ) : null}
                    <Menu.Divider />
                    <Menu.Item color="red" leftSection={<IconUserX {...ICON} />} onClick={() => setModal('reject')}>
                      Not moving forward
                    </Menu.Item>
                  </Menu.Dropdown>
                </Menu>
              ) : null}
              {full && hr && (a.status === 'rejected' || a.status === 'withdrawn') && !a.purged_at ? (
                <Button
                  size="sm"
                  variant="default"
                  leftSection={<IconRefresh {...ICON} />}
                  loading={busy === 'reopen'}
                  onClick={() => run('reopen', () => hiring.reopen(a.id), 'Application reopened.')}
                >
                  Reopen
                </Button>
              ) : null}
              {full && hr ? (
                <Button
                  size="sm"
                  variant="subtle"
                  color="gray"
                  leftSection={<IconDownload {...ICON} />}
                  onClick={() => openInTab(hiring.exportUrl(a.id))}
                >
                  Export data
                </Button>
              ) : null}
            </Group>
          </Group>

          {a.knocked_out ? (
            <Alert color="red" variant="light" title="Did not meet a must-have">
              See the screening answers below.
            </Alert>
          ) : null}
          {a.rejected_reason ? (
            <Alert color="gray" variant="light" title="Not selected">
              {a.rejected_reason}
            </Alert>
          ) : null}
          {a.purged_at ? (
            <Alert color="gray" variant="light">
              This candidate's personal data was removed after the retention period.
            </Alert>
          ) : null}
          {d.access === 'panel' ? (
            <Alert variant="light" color="blue">
              You are on this candidate's interview panel. You see the profile, the CV and your own scorecard.
            </Alert>
          ) : null}

          {full && active ? (
            <Paper withBorder p="sm" radius="md">
              <SimpleGrid cols={{ base: 1, sm: 3 }}>
                <Select
                  label="Stage"
                  allowDeselect={false}
                  value={a.stage}
                  disabled={busy === 'move'}
                  data={d.job.stages.filter((s) => s !== lastStage || s === a.stage).map((s) => ({ value: s, label: s }))}
                  onChange={(v) => v && v !== a.stage && run('move', () => hiring.move(a.id, v), `Moved to ${v}.`)}
                />
                <Stack gap={4}>
                  <Text size="sm" fw={500}>
                    Rating
                  </Text>
                  <Rating value={a.rating ?? 0} onChange={(v) => run('rate', () => hiring.update(a.id, { rating: v || null }))} />
                </Stack>
                <TagsInput label="Tags" maxTags={10} value={a.tags} onChange={(v) => run('tags', () => hiring.update(a.id, { tags: v }))} />
              </SimpleGrid>
            </Paper>
          ) : null}

          <Tabs defaultValue="profile" keepMounted={false}>
            <Tabs.List>
              <Tabs.Tab value="profile">Profile</Tabs.Tab>
              <Tabs.Tab value="interviews">Interviews ({d.interviews.length})</Tabs.Tab>
              {full ? <Tabs.Tab value="offers">Offers ({d.offers.length})</Tabs.Tab> : null}
              {full ? <Tabs.Tab value="activity">Activity</Tabs.Tab> : null}
            </Tabs.List>

            <Tabs.Panel value="profile" pt="md">
              <Stack gap="md">
                <SimpleGrid cols={{ base: 1, sm: 2 }}>
                  <div>
                    <Text size="xs" c="dimmed">
                      Experience
                    </Text>
                    <Text size="sm">{a.experience_years === null ? 'Not given' : `${Number(a.experience_years)} years`}</Text>
                  </div>
                  <div>
                    <Text size="xs" c="dimmed">
                      Current employer
                    </Text>
                    <Text size="sm">{a.current_employer || '—'}</Text>
                  </div>
                </SimpleGrid>
                {a.score_detail.length ? (
                  <div>
                    <Text fw={600} size="sm" mb={6}>
                      Screening score {a.score ?? '—'} of 100
                    </Text>
                    <Table withRowBorders={false} verticalSpacing={4}>
                      <Table.Tbody>
                        {a.score_detail.map((s, i) => (
                          <Table.Tr key={i}>
                            <Table.Td>
                              <Text size="sm">{s.label}</Text>
                              <Text size="xs" c="dimmed">
                                {s.note}
                              </Text>
                            </Table.Td>
                            <Table.Td ta="right">
                              <Text size="sm" className="num">
                                {s.max ? `${s.points} / ${s.max}` : s.note.startsWith('Meets') ? 'OK' : 'Flag'}
                              </Text>
                            </Table.Td>
                          </Table.Tr>
                        ))}
                      </Table.Tbody>
                    </Table>
                    <Text size="xs" c="dimmed" mt={4}>
                      Experience within the job's range earns 40 points and each preferred answer 20. Must-have questions don't score; a
                      wrong answer flags the application.
                    </Text>
                  </div>
                ) : null}
                {d.job.questions.length ? (
                  <div>
                    <Text fw={600} size="sm" mb={6}>
                      Answers
                    </Text>
                    <Stack gap={6}>
                      {d.job.questions.map((qq) => (
                        <div key={qq.id}>
                          <Text size="xs" c="dimmed">
                            {qq.label}
                          </Text>
                          <Text size="sm">
                            {a.answers[qq.id] === undefined ? '—' : String(a.answers[qq.id]).replace(/^yes$/, 'Yes').replace(/^no$/, 'No')}
                          </Text>
                        </div>
                      ))}
                    </Stack>
                  </div>
                ) : null}
                {a.cover_note ? (
                  <div>
                    <Text fw={600} size="sm" mb={6}>
                      Cover note
                    </Text>
                    <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                      {a.cover_note}
                    </Text>
                  </div>
                ) : null}
                {d.otherApplications.length ? (
                  <div>
                    <Text fw={600} size="sm" mb={6}>
                      Other applications by this person
                    </Text>
                    <Stack gap={4}>
                      {d.otherApplications.map((o) => (
                        <Text key={o.id} size="sm">
                          {o.title} · {o.stage} · <ApplicationStatusBadge status={o.status} /> · {fmtDateShort(o.created_at.slice(0, 10))}
                        </Text>
                      ))}
                    </Stack>
                  </div>
                ) : null}
                {a.consent_at ? (
                  <Text size="xs" c="dimmed">
                    Agreed to the processing of their data on {fmtDateShort(a.consent_at.slice(0, 10))}.
                  </Text>
                ) : null}
              </Stack>
            </Tabs.Panel>

            <Tabs.Panel value="interviews" pt="md">
              <Stack gap="sm">
                {d.interviews.length === 0 ? (
                  <EmptyState
                    compact
                    title="No interviews yet"
                    {...(full && active ? { action: { label: 'Schedule interview', onClick: () => setModal('interview') } } : {})}
                  />
                ) : null}
                {d.interviews.map((i) => {
                  const cards = d.scorecards.filter((s) => s.interview_id === i.id);
                  const mine = cards.find((s) => s.reviewer_id === user?.id);
                  const onPanel = !!user && i.panel.includes(user.id);
                  return (
                    <Paper key={i.id} withBorder p="sm" radius="md">
                      <Group justify="space-between" wrap="wrap" gap="xs">
                        <div>
                          <Text size="sm" fw={600}>
                            {i.title}{' '}
                            <Badge
                              size="xs"
                              variant="light"
                              color={i.status === 'cancelled' ? 'gray' : i.status === 'completed' ? 'teal' : 'blue'}
                            >
                              {i.status}
                            </Badge>
                          </Text>
                          <Text size="xs" c="dimmed">
                            {fmtWhen(i.starts_at)} · {i.duration_min} min · {MODE_LABEL[i.mode]}
                            {i.location ? ` · ${i.location}` : ''}
                          </Text>
                          <Text size="xs" c="dimmed">
                            Panel: {(i.panel_names ?? []).join(', ')}
                          </Text>
                        </div>
                        <Group gap="xs">
                          {onPanel && i.status !== 'cancelled' && a.status !== 'hired' ? (
                            <Button size="xs" variant={mine ? 'default' : 'filled'} onClick={() => setScoring({ ...i, candidate: a.name })}>
                              {mine ? 'Update scorecard' : 'Add scorecard'}
                            </Button>
                          ) : null}
                          {full && i.status === 'scheduled' ? (
                            <Menu position="bottom-end" withinPortal>
                              <Menu.Target>
                                <Button size="xs" variant="subtle" color="gray" rightSection={<IconChevronDown size={12} />}>
                                  More
                                </Button>
                              </Menu.Target>
                              <Menu.Dropdown>
                                <Menu.Item
                                  onClick={() =>
                                    run(`i${i.id}`, () => hiring.updateInterview(i.id, { status: 'completed' }), 'Marked as done.')
                                  }
                                >
                                  Mark as done
                                </Menu.Item>
                                <Menu.Item
                                  color="red"
                                  onClick={() =>
                                    run(`i${i.id}`, () => hiring.updateInterview(i.id, { status: 'cancelled' }), 'Interview cancelled.')
                                  }
                                >
                                  Cancel interview
                                </Menu.Item>
                              </Menu.Dropdown>
                            </Menu>
                          ) : null}
                        </Group>
                      </Group>
                      {cards.length ? <Divider my="xs" /> : null}
                      <Stack gap={6}>
                        {cards.map((s) => (
                          <div key={s.id}>
                            <Group gap="xs">
                              <Text size="sm" fw={500}>
                                {s.reviewer}
                              </Text>
                              <Badge size="sm" variant="light" color={RECOMMENDATION[s.recommendation].color}>
                                {RECOMMENDATION[s.recommendation].label}
                              </Badge>
                            </Group>
                            <Text size="xs" c="dimmed">
                              {Object.entries(s.ratings)
                                .map(([k, v]) => `${k} ${v}/5`)
                                .join(' · ')}
                            </Text>
                            {s.notes ? (
                              <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                                {s.notes}
                              </Text>
                            ) : null}
                          </div>
                        ))}
                      </Stack>
                    </Paper>
                  );
                })}
              </Stack>
            </Tabs.Panel>

            {full ? (
              <Tabs.Panel value="offers" pt="md">
                <Stack gap="sm">
                  {d.offers.length === 0 ? (
                    <EmptyState
                      compact
                      title="No offer yet"
                      hint={hr ? 'Make an offer once the panel agrees.' : 'HR makes offers once the panel agrees.'}
                    />
                  ) : null}
                  {d.offers.map((o) => (
                    <Paper key={o.id} withBorder p="sm" radius="md">
                      <Group justify="space-between" wrap="wrap" gap="xs">
                        <div>
                          <Group gap="xs">
                            <Text size="sm" fw={600}>
                              {o.designation}
                            </Text>
                            <OfferStatusBadge status={o.status} />
                          </Group>
                          <Text size="xs" c="dimmed">
                            NPR {fmtMoney(o.salary)} a month · joins {fmtDateShort(o.join_date)}
                            {o.expires_on ? ` · valid until ${fmtDateShort(o.expires_on)}` : ''}
                          </Text>
                        </div>
                        <Group gap="xs">
                          <Button size="xs" variant="default" onClick={() => openInTab(hiring.offerLetterUrl(o.id))}>
                            Letter
                          </Button>
                          {hr && o.status === 'approved' ? (
                            <>
                              <Button
                                size="xs"
                                color="teal"
                                loading={busy === `o${o.id}a`}
                                onClick={() =>
                                  run(
                                    `o${o.id}a`,
                                    () => hiring.respondOffer(o.id, 'accepted'),
                                    'Marked as accepted. You can hire the candidate now.',
                                  )
                                }
                              >
                                Accepted
                              </Button>
                              <Button
                                size="xs"
                                variant="default"
                                loading={busy === `o${o.id}d`}
                                onClick={() => run(`o${o.id}d`, () => hiring.respondOffer(o.id, 'declined'), 'Marked as declined.')}
                              >
                                Declined
                              </Button>
                            </>
                          ) : null}
                          {hr && o.status === 'accepted' && active ? (
                            <Button size="xs" leftSection={<IconUserCheck size={14} />} onClick={() => setModal('hire')}>
                              Hire
                            </Button>
                          ) : null}
                          {hr && ['pending_approval', 'approved', 'accepted'].includes(o.status) && a.status !== 'hired' ? (
                            <Button
                              size="xs"
                              variant="subtle"
                              color="red"
                              loading={busy === `o${o.id}w`}
                              onClick={() => run(`o${o.id}w`, () => hiring.withdrawOffer(o.id), 'Offer withdrawn.')}
                            >
                              Withdraw
                            </Button>
                          ) : null}
                        </Group>
                      </Group>
                    </Paper>
                  ))}
                </Stack>
              </Tabs.Panel>
            ) : null}

            {full ? (
              <Tabs.Panel value="activity" pt="md">
                <Stack gap="md">
                  <Group align="flex-end" gap="xs" wrap="nowrap">
                    <Textarea
                      style={{ flex: 1 }}
                      placeholder="Add a note for the hiring team"
                      autosize
                      minRows={1}
                      maxLength={4000}
                      value={note}
                      onChange={(e) => setNote(e.currentTarget.value)}
                    />
                    <Button
                      disabled={!note.trim()}
                      loading={busy === 'note'}
                      onClick={() =>
                        run(
                          'note',
                          async () => {
                            await hiring.note(a.id, note.trim());
                            setNote('');
                          },
                          'Note added.',
                        )
                      }
                    >
                      Add
                    </Button>
                  </Group>
                  <Timeline bulletSize={10} lineWidth={2}>
                    {d.events.map((e) => (
                      <Timeline.Item
                        key={e.id}
                        title={
                          <Text size="sm" fw={500}>
                            {EVENT_LABEL[e.kind] ?? e.kind}
                          </Text>
                        }
                      >
                        {e.body ? (
                          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                            {e.body}
                          </Text>
                        ) : null}
                        <Text size="xs" c="dimmed">
                          {e.actor ?? 'Candidate'} · {fmtWhen(e.created_at)}
                        </Text>
                      </Timeline.Item>
                    ))}
                  </Timeline>
                </Stack>
              </Tabs.Panel>
            ) : null}
          </Tabs>

          <InterviewModal opened={modal === 'interview'} appId={a.id} onClose={() => setModal(null)} onSaved={changed} />
          <OfferModal
            opened={modal === 'offer'}
            appId={a.id}
            defaultDesignation={d.job.title}
            onClose={() => setModal(null)}
            onSaved={changed}
          />
          <HireModal
            opened={modal === 'hire'}
            appId={a.id}
            joinDate={acceptedOffer?.join_date ?? null}
            onClose={() => setModal(null)}
            onSaved={changed}
          />
          <RejectModal opened={modal === 'reject'} appId={a.id} name={a.name} onClose={() => setModal(null)} onSaved={changed} />
          <ScorecardModal interview={scoring} onClose={() => setScoring(null)} onSaved={changed} />
        </Stack>
      )}
    </Drawer>
  );
}
