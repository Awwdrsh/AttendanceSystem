import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Modal,
  Paper,
  Radio,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Textarea,
} from '@mantine/core';
import { IconCertificate, IconSchool } from '@tabler/icons-react';
import { useState } from 'react';
import { learning as lrnApi } from '../../api/performance';
import { EmptyState } from '../../components/EmptyState';
import { FileUpload } from '../../components/FileUpload';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { fmtDateTime, RatingPicker } from './parts';

export const ENROLL_STATUS = {
  pending_approval: { label: 'Awaiting approval', color: 'yellow' },
  enrolled: { label: 'Enrolled', color: 'blue' },
  completed: { label: 'Completed', color: 'brand' },
  rejected: { label: 'Not approved', color: 'gray' },
  cancelled: { label: 'Cancelled', color: 'gray' },
  no_show: { label: 'Did not attend', color: 'red' },
};
export const DELIVERY = { online: 'Online', classroom: 'Classroom', blended: 'Blended' };

export function CertificateExpiry({ e }) {
  if (!e.certificate_expires_on) return null;
  const today = todayNpt();
  const soon = e.certificate_expires_on <= new Date(Date.parse(`${today}T00:00:00Z`) + 30 * 86_400_000).toISOString().slice(0, 10);
  const past = e.certificate_expires_on < today;
  return (
    <Badge size="sm" variant="light" color={past ? 'red' : soon ? 'orange' : 'gray'}>
      {past ? 'Expired' : 'Valid until'} {fmtDateShort(e.certificate_expires_on)}
    </Badge>
  );
}

/** My training: what I am enrolled in, assessments, certificates, and the catalogue to request more. */
export default function MyLearningPage() {
  const [view, setView] = useState('mine');
  const mine = useQuery(() => lrnApi.enrollments({ scope: 'mine' }), []);
  const cat = useQuery(() => lrnApi.courses(), [], { enabled: view === 'catalogue' });
  const [quiz, setQuiz] = useState(null);
  const [rate, setRate] = useState(null);
  const [cert, setCert] = useState(null);
  const [join, setJoin] = useState(null);
  const rows = mine.data?.enrollments ?? [];
  const act = async (fn, msg) => {
    try {
      await fn();
      toast.ok(msg);
      await mine.reload();
    } catch (e) {
      toast.err(e);
    }
  };

  return (
    <Stack gap="lg">
      <SectionTitle
        hint="Courses you are enrolled in, assessments to take and your certificates. Request a course from the catalogue; your manager approves it."
        actions={
          <SegmentedControl
            size="xs"
            value={view}
            onChange={(v) => setView(v)}
            data={[
              { value: 'mine', label: 'My learning' },
              { value: 'catalogue', label: 'Catalogue' },
            ]}
          />
        }
      >
        Learning
      </SectionTitle>
      {view === 'mine' ? (
        !mine.loading && rows.length === 0 ? (
          <EmptyState
            icon={<IconSchool size={20} stroke={1.6} />}
            title="No training yet"
            hint="Browse the catalogue and request a course that helps you grow."
            action={{ label: 'Open catalogue', onClick: () => setView('catalogue') }}
          />
        ) : (
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            {rows.map((e) => (
              <Paper key={e.id} withBorder radius="lg" p="md">
                <Stack gap={6}>
                  <Group justify="space-between" wrap="nowrap" align="flex-start">
                    <div style={{ minWidth: 0 }}>
                      <Text fw={600} size="sm">
                        {e.title}
                      </Text>
                      <Text size="xs" c="dimmed">
                        {DELIVERY[e.delivery]}
                        {e.mandatory ? ' · Mandatory' : ''}
                        {e.starts_at ? ` · ${fmtDateTime(e.starts_at)}${e.venue ? `, ${e.venue}` : ''}` : ''}
                      </Text>
                    </div>
                    <Badge variant="light" color={ENROLL_STATUS[e.status].color} style={{ flex: 'none' }}>
                      {ENROLL_STATUS[e.status].label}
                    </Badge>
                  </Group>
                  {e.score !== null ? (
                    <Text size="xs" c="dimmed">
                      Best score {Number(e.score)}%{e.pass_mark ? ` (pass ${e.pass_mark}%)` : ''}
                    </Text>
                  ) : null}
                  <Group gap="xs">
                    <CertificateExpiry e={e} />
                  </Group>
                  <Group gap="xs" wrap="wrap">
                    {e.status === 'enrolled' && e.questions > 0 ? (
                      <Button size="xs" onClick={() => setQuiz(e)}>
                        Take assessment
                      </Button>
                    ) : null}
                    {e.status === 'enrolled' && e.questions === 0 && e.delivery === 'online' ? (
                      <Button size="xs" onClick={() => act(() => lrnApi.complete(e.id), 'Marked complete.')}>
                        Mark complete
                      </Button>
                    ) : null}
                    {e.status === 'completed' ? (
                      <>
                        <Button
                          size="xs"
                          variant="default"
                          leftSection={<IconCertificate size={14} />}
                          onClick={() => window.open(lrnApi.certificateUrl(e.id), '_blank', 'noopener')}
                        >
                          Certificate
                        </Button>
                        {e.certificate_file_id ? (
                          <Anchor size="xs" href={lrnApi.certificateFileUrl(e.id)} target="_blank" rel="noopener">
                            Uploaded certificate
                          </Anchor>
                        ) : (
                          <Button size="xs" variant="subtle" onClick={() => setCert(e)}>
                            Upload certificate
                          </Button>
                        )}
                        {e.feedback_rating ? (
                          <Text size="xs" c="dimmed">
                            You rated {e.feedback_rating}/5
                          </Text>
                        ) : (
                          <Button size="xs" variant="subtle" onClick={() => setRate(e)}>
                            Rate course
                          </Button>
                        )}
                      </>
                    ) : null}
                    {e.status === 'pending_approval' || (e.status === 'enrolled' && e.source === 'self') ? (
                      <Button size="xs" variant="subtle" color="gray" onClick={() => act(() => lrnApi.cancel(e.id), 'Cancelled.')}>
                        Cancel
                      </Button>
                    ) : null}
                  </Group>
                </Stack>
              </Paper>
            ))}
          </SimpleGrid>
        )
      ) : !cat.loading && (cat.data?.courses ?? []).length === 0 ? (
        <EmptyState icon={<IconSchool size={20} stroke={1.6} />} title="The catalogue is empty" hint="HR adds courses here." />
      ) : (
        <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
          {(cat.data?.courses ?? []).map((c) => {
            const open = rows.find((e) => e.course_id === c.id && ['pending_approval', 'enrolled'].includes(e.status));
            return (
              <Paper key={c.id} withBorder radius="lg" p="md">
                <Stack gap={6}>
                  <Group justify="space-between" wrap="nowrap" align="flex-start">
                    <Text fw={600} size="sm">
                      {c.title}
                    </Text>
                    {c.mandatory ? (
                      <Badge size="sm" variant="light" color="orange">
                        Mandatory
                      </Badge>
                    ) : null}
                  </Group>
                  <Text size="xs" c="dimmed">
                    {[DELIVERY[c.delivery], c.duration_hours ? `${Number(c.duration_hours)} h` : null, c.provider, c.category]
                      .filter(Boolean)
                      .join(' · ')}
                  </Text>
                  {c.description ? (
                    <Text size="sm" lineClamp={3}>
                      {c.description}
                    </Text>
                  ) : null}
                  {c.next_session ? (
                    <Text size="xs" c="dimmed">
                      Next session {fmtDateTime(c.next_session)}
                    </Text>
                  ) : null}
                  <Group justify="space-between">
                    {c.url ? (
                      <Anchor size="xs" href={c.url} target="_blank" rel="noopener noreferrer">
                        Course link
                      </Anchor>
                    ) : (
                      <span />
                    )}
                    {open ? (
                      <Badge variant="light" color={ENROLL_STATUS[open.status].color}>
                        {ENROLL_STATUS[open.status].label}
                      </Badge>
                    ) : (
                      <Button size="xs" onClick={() => setJoin(c)}>
                        Request to join
                      </Button>
                    )}
                  </Group>
                </Stack>
              </Paper>
            );
          })}
        </SimpleGrid>
      )}
      <QuizModal e={quiz} onClose={() => setQuiz(null)} onDone={() => void mine.reload()} />
      <RateModal e={rate} onClose={() => setRate(null)} onDone={() => void mine.reload()} />
      <CertificateModal e={cert} onClose={() => setCert(null)} onDone={() => void mine.reload()} />
      <JoinModal
        course={join}
        onClose={() => setJoin(null)}
        onDone={() => {
          setView('mine');
          void mine.reload();
        }}
      />
    </Stack>
  );
}

function JoinModal({ course, onClose, onDone }) {
  const q = useQuery(() => (course ? lrnApi.course(course.id) : Promise.resolve(null)), [course?.id]);
  const [session, setSession] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const open = (q.data?.sessions ?? []).filter((s) => s.status === 'scheduled' && Date.parse(s.starts_at) > Date.now());
  const send = async () => {
    if (!course) return;
    setBusy(true);
    try {
      await lrnApi.selfEnroll({ courseId: course.id, ...(session ? { sessionId: session } : {}), ...(reason.trim() ? { reason } : {}) });
      toast.ok('Sent to your manager for approval.');
      setReason('');
      setSession(null);
      onClose();
      onDone();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={!!course} onClose={onClose} title={course ? `Join ${course.title}` : ''}>
      <Stack gap="sm">
        {open.length ? (
          <Select
            label="Session"
            clearable
            value={session}
            onChange={setSession}
            data={open.map((s) => ({
              value: s.id,
              label: `${fmtDateTime(s.starts_at)}${s.venue ? `, ${s.venue}` : ''}${s.capacity ? ` (${Math.max(0, s.capacity - s.taken)} seats left)` : ''}`,
              disabled: s.capacity !== null && s.taken >= s.capacity,
            }))}
          />
        ) : null}
        {course && Number(course.cost) > 0 ? (
          <Text size="sm" c="dimmed">
            Cost to the company: NPR {fmtMoney(course.cost)}
          </Text>
        ) : null}
        <Textarea label="Why this course?" autosize minRows={2} value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Group justify="flex-end">
          <Button onClick={send} loading={busy}>
            Send request
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function QuizModal({ e, onClose, onDone }) {
  const q = useQuery(() => (e ? lrnApi.assessment(e.id) : Promise.resolve(null)), [e?.id]);
  const [answers, setAnswers] = useState({});
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const qs = q.data?.questions ?? [];
  const close = () => {
    setAnswers({});
    setResult(null);
    onClose();
  };
  const submit = async () => {
    if (!e) return;
    setBusy(true);
    try {
      const r = await lrnApi.attempt(
        e.id,
        qs.map((_, i) => answers[i] ?? -1).map((x) => Math.max(0, x)),
      );
      setResult(r);
      onDone();
    } catch (err) {
      toast.err(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={!!e} onClose={close} title={q.data?.title ?? 'Assessment'} size="lg">
      {result ? (
        <Stack gap="sm">
          <Alert color={result.passed ? 'teal' : 'orange'} variant="light" title={result.passed ? 'Passed' : 'Not passed yet'}>
            {result.correct} of {result.total} correct ({result.score}%).
            {result.passed ? ' Well done.' : ` ${result.attemptsLeft} attempt${result.attemptsLeft === 1 ? '' : 's'} left.`}
          </Alert>
          <Group justify="flex-end">
            {!result.passed && result.attemptsLeft > 0 ? (
              <Button
                variant="default"
                onClick={() => {
                  setResult(null);
                  setAnswers({});
                  void q.reload();
                }}
              >
                Try again
              </Button>
            ) : null}
            <Button onClick={close}>Close</Button>
          </Group>
        </Stack>
      ) : (
        <Stack gap="md">
          <Text size="sm" c="dimmed">
            Pass mark {q.data?.passMark ?? 60}%. {q.data?.attemptsLeft ?? 0} attempts left.
          </Text>
          {qs.map((x, i) => (
            <Radio.Group
              key={x.id ?? i}
              label={`${i + 1}. ${x.prompt}`}
              value={answers[i] !== undefined ? String(answers[i]) : null}
              onChange={(v) => setAnswers((a) => ({ ...a, [i]: Number(v) }))}
            >
              <Stack gap={4} mt={6}>
                {x.options.map((o, j) => (
                  <Radio key={j} value={String(j)} label={o} />
                ))}
              </Stack>
            </Radio.Group>
          ))}
          <Group justify="flex-end">
            <Button onClick={submit} loading={busy} disabled={Object.keys(answers).length < qs.length || !q.data?.attemptsLeft}>
              Submit answers
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}

function RateModal({ e, onClose, onDone }) {
  const [rating, setRating] = useState(null);
  const [comment, setComment] = useState('');
  const send = async () => {
    if (!e || !rating) return;
    try {
      await lrnApi.feedback(e.id, rating, comment || undefined);
      toast.ok('Thanks for the feedback.');
      onClose();
      onDone();
    } catch (err) {
      toast.err(err);
    }
  };
  return (
    <Modal opened={!!e} onClose={onClose} title="How was the course?">
      <Stack gap="sm">
        <RatingPicker labels={['Poor', 'Fair', 'Good', 'Very good', 'Excellent']} value={rating} onChange={setRating} />
        <Textarea
          placeholder="What was useful? What would you change?"
          autosize
          minRows={2}
          value={comment}
          onChange={(ev) => setComment(ev.currentTarget.value)}
        />
        <Group justify="flex-end">
          <Button disabled={!rating} onClick={send}>
            Send
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function CertificateModal({ e, onClose, onDone }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    if (!e || !file) return;
    setBusy(true);
    try {
      await lrnApi.attachCertificate(e.id, { fileId: file.id });
      toast.ok('Certificate added.');
      setFile(null);
      onClose();
      onDone();
    } catch (err) {
      toast.err(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={!!e} onClose={onClose} title="Upload certificate">
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          A PDF or photo of the certificate you received, up to 4 MB. Your manager and HR can see it.
        </Text>
        <FileUpload purpose="training_certificate" value={file} onChange={setFile} label="Choose file" />
        <Group justify="flex-end">
          <Button disabled={!file} loading={busy} onClick={send}>
            Save
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
