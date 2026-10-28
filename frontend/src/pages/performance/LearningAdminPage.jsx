import {
  ActionIcon,
  Anchor,
  Badge,
  Button,
  Divider,
  Drawer,
  Group,
  Modal,
  MultiSelect,
  NumberInput,
  Paper,
  Radio,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';
import { IconPlus, IconTrash } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { useAuth } from '../../auth/AuthProvider';
import { learning as lrnApi, performance as perfApi } from '../../api/performance';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { addDays, fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { CertificateExpiry, DELIVERY, ENROLL_STATUS } from './MyLearningPage';
import { fmtDateTime, ICON, nptInstant } from './parts';

/** Training for managers (their team) and HR (catalogue, sessions, assessments, certificates and spend). */
export default function LearningAdminPage() {
  const { hasRole } = useAuth();
  const hr = hasRole('hr');
  const [view, setView] = useState('courses');
  return (
    <Stack gap="lg">
      <SegmentedControl
        value={view}
        onChange={(v) => setView(v)}
        style={{ alignSelf: 'flex-start' }}
        data={[
          { value: 'courses', label: 'Courses' },
          { value: 'enrollments', label: hr ? 'Enrolments' : 'My team' },
          ...(hr
            ? [
                { value: 'expiring', label: 'Certificates' },
                { value: 'cost', label: 'Spend' },
              ]
            : []),
        ]}
      />
      {view === 'courses' ? <Courses hr={hr} /> : null}
      {view === 'enrollments' ? <Enrollments hr={hr} /> : null}
      {view === 'expiring' && hr ? <Expiring /> : null}
      {view === 'cost' && hr ? <Cost /> : null}
    </Stack>
  );
}

function Courses({ hr }) {
  const q = useQuery(() => lrnApi.courses(), []);
  const [edit, setEdit] = useState(null);
  const [assign, setAssign] = useState(null);
  return (
    <Stack gap="md">
      <SectionTitle
        hint={
          hr
            ? 'The training catalogue. Click a course to edit it, schedule sessions and write its assessment.'
            : 'Enrol your team in a course.'
        }
        actions={
          hr ? (
            <Button
              size="xs"
              leftSection={<IconPlus {...ICON} />}
              onClick={() =>
                setEdit({ delivery: 'classroom', source: 'internal', cost: '0', mandatory: false, mandatory_for: [], active: true })
              }
            >
              New course
            </Button>
          ) : undefined
        }
      >
        Courses
      </SectionTitle>
      <DataTable
        rows={q.data?.courses ?? []}
        rowKey={(c) => c.id}
        loading={q.loading}
        error={q.error}
        onRowClick={(c) => (hr ? setEdit(c) : setAssign(c))}
        emptyTitle="No courses yet"
        emptyHint={
          hr ? 'Add internal sessions, external programmes and mandatory trainings such as fire safety.' : 'HR has not added courses yet.'
        }
        columns={[
          {
            key: 'title',
            header: 'Course',
            render: (c) => (
              <div>
                <Text size="sm" fw={500}>
                  {c.title}
                </Text>
                <Text size="xs" c="dimmed">
                  {[c.code, DELIVERY[c.delivery], c.provider].filter(Boolean).join(' · ')}
                </Text>
              </div>
            ),
          },
          {
            key: 'flags',
            header: '',
            render: (c) => (
              <Group gap={4}>
                {c.mandatory ? (
                  <Badge size="sm" variant="light" color="orange">
                    Mandatory
                  </Badge>
                ) : null}
                {c.questions ? (
                  <Badge size="sm" variant="light" color="grape">
                    Assessment
                  </Badge>
                ) : null}
                {!c.active ? (
                  <Badge size="sm" variant="light" color="gray">
                    Inactive
                  </Badge>
                ) : null}
              </Group>
            ),
          },
          {
            key: 'next',
            header: 'Next session',
            render: (c) => <Text size="sm">{c.next_session ? fmtDateTime(c.next_session) : '—'}</Text>,
          },
          {
            key: 'people',
            header: 'Enrolled / done',
            align: 'right',
            render: (c) => (
              <Text size="sm">
                {c.enrolled ?? 0} / {c.completed ?? 0}
              </Text>
            ),
          },
          {
            key: 'cost',
            header: 'Cost per person',
            align: 'right',
            render: (c) => <Text size="sm">{Number(c.cost) ? fmtMoney(c.cost) : 'Free'}</Text>,
          },
          {
            key: 'assign',
            header: '',
            render: (c) => (
              <Button
                size="compact-xs"
                variant="light"
                onClick={(e) => {
                  e.stopPropagation();
                  setAssign(c);
                }}
              >
                Assign
              </Button>
            ),
          },
        ]}
      />
      <CourseDrawer course={edit} onClose={() => setEdit(null)} onSaved={() => void q.reload()} />
      <AssignModal course={assign} onClose={() => setAssign(null)} onDone={() => void q.reload()} hr={hr} />
    </Stack>
  );
}

function CourseDrawer({ course, onClose, onSaved }) {
  const [f, setF] = useState({});
  const [key, setKey] = useState(null);
  const k = course ? (course.id ?? 'new') : null;
  if (k !== key) {
    setKey(k);
    setF(course ?? {});
  }
  const detail = useQuery(() => (course?.id ? lrnApi.course(course.id) : Promise.resolve(null)), [course?.id]);
  const set = (n, v) => setF((x) => ({ ...x, [n]: v }));
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const body = {
        code: f.code,
        title: f.title,
        description: f.description || null,
        category: f.category || null,
        delivery: f.delivery,
        source: f.source,
        provider: f.provider || null,
        durationHours: f.duration_hours ? Number(f.duration_hours) : null,
        url: f.url || null,
        cost: Number(f.cost || 0).toFixed(2),
        mandatory: !!f.mandatory,
        mandatoryFor: f.mandatory_for ?? [],
        validityMonths: f.validity_months || null,
        passMark: f.pass_mark || null,
        active: f.active ?? true,
      };
      if (course?.id) await lrnApi.updateCourse(course.id, body);
      else await lrnApi.createCourse(body);
      toast.ok('Course saved.');
      onSaved();
      if (!course?.id) onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  const mandatory = async () => {
    if (!course?.id) return;
    try {
      const r = await lrnApi.assignMandatory(course.id);
      toast.ok(`${r.added} people enrolled.`);
      onSaved();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <Drawer opened={!!course} onClose={onClose} position="right" size="xl" title={course?.id ? (f.title ?? 'Course') : 'New course'}>
      <Stack gap="sm">
        <SimpleGrid cols={{ base: 1, sm: 3 }}>
          <TextInput label="Code" required value={f.code ?? ''} onChange={(e) => set('code', e.currentTarget.value)} />
          <TextInput
            label="Title"
            required
            style={{ gridColumn: 'span 2' }}
            value={f.title ?? ''}
            onChange={(e) => set('title', e.currentTarget.value)}
          />
        </SimpleGrid>
        <Textarea
          label="Description"
          autosize
          minRows={2}
          value={f.description ?? ''}
          onChange={(e) => set('description', e.currentTarget.value)}
        />
        <SimpleGrid cols={{ base: 2, sm: 3 }}>
          <Select
            label="Delivery"
            value={f.delivery ?? 'classroom'}
            onChange={(v) => set('delivery', v ?? 'classroom')}
            allowDeselect={false}
            data={[
              { value: 'classroom', label: 'Classroom' },
              { value: 'online', label: 'Online' },
              { value: 'blended', label: 'Blended' },
            ]}
          />
          <Select
            label="Source"
            value={f.source ?? 'internal'}
            onChange={(v) => set('source', v ?? 'internal')}
            allowDeselect={false}
            data={[
              { value: 'internal', label: 'Internal' },
              { value: 'external', label: 'External' },
            ]}
          />
          <TextInput label="Category" value={f.category ?? ''} onChange={(e) => set('category', e.currentTarget.value)} />
          <TextInput label="Provider" value={f.provider ?? ''} onChange={(e) => set('provider', e.currentTarget.value)} />
          <NumberInput
            label="Hours"
            min={0}
            value={f.duration_hours ? Number(f.duration_hours) : ''}
            onChange={(v) => set('duration_hours', v === '' ? null : String(v))}
          />
          <NumberInput
            label="Cost per person (NPR)"
            min={0}
            value={Number(f.cost ?? 0)}
            onChange={(v) => set('cost', String(Number(v) || 0))}
          />
        </SimpleGrid>
        <TextInput label="Link" placeholder="https://" value={f.url ?? ''} onChange={(e) => set('url', e.currentTarget.value)} />
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <NumberInput
            label="Certificate valid for (months)"
            description="Empty: never expires"
            min={1}
            max={120}
            value={f.validity_months ?? ''}
            onChange={(v) => set('validity_months', v === '' ? null : Number(v))}
          />
          <NumberInput
            label="Pass mark (%)"
            min={1}
            max={100}
            value={f.pass_mark ?? ''}
            onChange={(v) => set('pass_mark', v === '' ? null : Number(v))}
          />
        </SimpleGrid>
        <Switch label="Mandatory" checked={!!f.mandatory} onChange={(e) => set('mandatory', e.currentTarget.checked)} />
        {f.mandatory ? (
          <TextInput
            label="Mandatory for designations"
            description="Comma separated; empty means everyone"
            value={(f.mandatory_for ?? []).join(', ')}
            onChange={(e) =>
              set(
                'mandatory_for',
                e.currentTarget.value
                  .split(',')
                  .map((s) => s.trim())
                  .filter(Boolean),
              )
            }
          />
        ) : null}
        <Switch label="Active" checked={f.active ?? true} onChange={(e) => set('active', e.currentTarget.checked)} />
        <Group justify="space-between">
          {course?.id && f.mandatory ? (
            <Button variant="light" onClick={mandatory}>
              Enrol everyone it applies to
            </Button>
          ) : (
            <span />
          )}
          <Button onClick={save} loading={busy} disabled={!f.code?.trim() || !f.title?.trim()}>
            Save course
          </Button>
        </Group>
        {course?.id && detail.data ? (
          <>
            <Divider label="Sessions" labelPosition="left" />
            <Sessions
              courseId={course.id}
              sessions={detail.data.sessions}
              onChanged={() => {
                void detail.reload();
                onSaved();
              }}
            />
            <Divider label="Assessment" labelPosition="left" />
            <QuestionsEditor
              courseId={course.id}
              initial={detail.data.questions}
              onSaved={() => {
                void detail.reload();
                onSaved();
              }}
            />
          </>
        ) : null}
      </Stack>
    </Drawer>
  );
}

function Sessions({ courseId, sessions, onChanged }) {
  const [date, setDate] = useState(addDays(todayNpt(), 7));
  const [start, setStart] = useState('10:00');
  const [end, setEnd] = useState('13:00');
  const [venue, setVenue] = useState('');
  const [trainer, setTrainer] = useState('');
  const [cap, setCap] = useState('');
  const add = async () => {
    if (!date) return;
    try {
      await lrnApi.addSession(courseId, {
        startsAt: nptInstant(date, start),
        endsAt: nptInstant(date, end),
        venue: venue || null,
        trainer: trainer || null,
        capacity: cap === '' ? null : cap,
      });
      toast.ok('Session added.');
      onChanged();
    } catch (e) {
      toast.err(e);
    }
  };
  const setStatus = async (id, status) => {
    try {
      await lrnApi.updateSession(id, { status });
      onChanged();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <Stack gap="xs">
      {sessions.length === 0 ? (
        <Text size="sm" c="dimmed">
          No sessions. Online courses do not need one.
        </Text>
      ) : (
        sessions.map((s) => (
          <Group key={s.id} justify="space-between" wrap="nowrap">
            <div>
              <Text size="sm">
                {fmtDateTime(s.starts_at)}
                {s.venue ? ` · ${s.venue}` : ''}
              </Text>
              <Text size="xs" c="dimmed">
                {[s.trainer, `${s.taken}${s.capacity ? ` of ${s.capacity}` : ''} seats`].filter(Boolean).join(' · ')}
              </Text>
            </div>
            {s.status === 'scheduled' ? (
              <Group gap={4}>
                <Button size="compact-xs" variant="subtle" onClick={() => setStatus(s.id, 'completed')}>
                  Held
                </Button>
                <Button size="compact-xs" variant="subtle" color="red" onClick={() => setStatus(s.id, 'cancelled')}>
                  Cancel
                </Button>
              </Group>
            ) : (
              <Badge size="sm" variant="light" color={s.status === 'completed' ? 'brand' : 'gray'}>
                {s.status}
              </Badge>
            )}
          </Group>
        ))
      )}
      <Paper withBorder radius="md" p="sm">
        <SimpleGrid cols={{ base: 2, sm: 3 }}>
          <DateField label="Date" value={date} onChange={setDate} />
          <TextInput label="From" type="time" value={start} onChange={(e) => setStart(e.currentTarget.value)} />
          <TextInput label="To" type="time" value={end} onChange={(e) => setEnd(e.currentTarget.value)} />
          <TextInput label="Venue" value={venue} onChange={(e) => setVenue(e.currentTarget.value)} />
          <TextInput label="Trainer" value={trainer} onChange={(e) => setTrainer(e.currentTarget.value)} />
          <NumberInput label="Seats" min={1} value={cap} onChange={(v) => setCap(v === '' ? '' : Number(v))} />
        </SimpleGrid>
        <Group justify="flex-end" mt="xs">
          <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} onClick={add} disabled={!date}>
            Add session
          </Button>
        </Group>
      </Paper>
    </Stack>
  );
}

function QuestionsEditor({ courseId, initial, onSaved }) {
  const [qs, setQs] = useState(() => initial.map((x) => ({ prompt: x.prompt, options: x.options, correctIndex: x.correct_index ?? 0 })));
  const [busy, setBusy] = useState(false);
  const upd = (i, p) => setQs((xs) => xs.map((x, j) => (j === i ? { ...x, ...p } : x)));
  const save = async () => {
    setBusy(true);
    try {
      await lrnApi.saveQuestions(
        courseId,
        qs.filter((x) => x.prompt.trim()),
      );
      toast.ok('Assessment saved.');
      onSaved();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Stack gap="xs">
      <Text size="xs" c="dimmed">
        Multiple-choice questions, graded automatically. Learners never see the answers. Leave empty for no assessment.
      </Text>
      {qs.map((x, i) => (
        <Paper key={i} withBorder radius="md" p="sm">
          <Group gap="xs" wrap="nowrap" align="flex-end">
            <TextInput
              style={{ flex: 1 }}
              label={`Question ${i + 1}`}
              value={x.prompt}
              onChange={(e) => upd(i, { prompt: e.currentTarget.value })}
            />
            <ActionIcon
              variant="subtle"
              color="gray"
              mb={4}
              aria-label="Remove question"
              onClick={() => setQs((xs) => xs.filter((_, j) => j !== i))}
            >
              <IconTrash size={14} />
            </ActionIcon>
          </Group>
          <Radio.Group value={String(x.correctIndex)} onChange={(v) => upd(i, { correctIndex: Number(v) })} mt="xs">
            <Stack gap={4}>
              {x.options.map((o, j) => (
                <Group key={j} gap="xs" wrap="nowrap">
                  <Radio value={String(j)} aria-label="Correct answer" />
                  <TextInput
                    size="xs"
                    style={{ flex: 1 }}
                    value={o}
                    placeholder={`Option ${j + 1}`}
                    onChange={(e) => {
                      const v = e.currentTarget.value;
                      upd(i, { options: x.options.map((y, k) => (k === j ? v : y)) });
                    }}
                  />
                  {x.options.length > 2 ? (
                    <ActionIcon
                      size="sm"
                      variant="subtle"
                      color="gray"
                      aria-label="Remove option"
                      onClick={() =>
                        upd(i, {
                          options: x.options.filter((_, k) => k !== j),
                          correctIndex: x.correctIndex >= j && x.correctIndex > 0 ? x.correctIndex - 1 : x.correctIndex,
                        })
                      }
                    >
                      <IconTrash size={12} />
                    </ActionIcon>
                  ) : null}
                </Group>
              ))}
            </Stack>
          </Radio.Group>
          {x.options.length < 6 ? (
            <Button size="compact-xs" variant="subtle" mt={4} onClick={() => upd(i, { options: [...x.options, ''] })}>
              Add option
            </Button>
          ) : null}
        </Paper>
      ))}
      <Group justify="space-between">
        <Button
          size="xs"
          variant="default"
          leftSection={<IconPlus size={14} />}
          onClick={() => setQs((xs) => [...xs, { prompt: '', options: ['', ''], correctIndex: 0 }])}
        >
          Add question
        </Button>
        <Button size="xs" onClick={save} loading={busy}>
          Save assessment
        </Button>
      </Group>
    </Stack>
  );
}

function AssignModal({ course, onClose, onDone, hr }) {
  const { user } = useAuth();
  const people = useQuery(() => perfApi.colleagues(), [!!course], { enabled: !!course });
  const detail = useQuery(() => (course ? lrnApi.course(course.id) : Promise.resolve(null)), [course?.id]);
  const [ids, setIds] = useState([]);
  const [session, setSession] = useState(null);
  const [busy, setBusy] = useState(false);
  const options = (people.data?.colleagues ?? []).filter((c) => hr || c.manager_id === user?.employeeId);
  const send = async () => {
    if (!course) return;
    setBusy(true);
    try {
      const r = await lrnApi.assign({ courseId: course.id, employeeIds: ids, ...(session ? { sessionId: session } : {}) });
      toast.ok(`${r.added} enrolled${r.skipped ? `, ${r.skipped} already enrolled` : ''}.`);
      setIds([]);
      onDone();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={!!course} onClose={onClose} title={course ? `Assign ${course.title}` : ''}>
      <Stack gap="sm">
        <MultiSelect
          label="People"
          searchable
          value={ids}
          onChange={setIds}
          data={options.map((c) => ({ value: c.id, label: `${c.name_en}${c.designation ? `, ${c.designation}` : ''}` }))}
        />
        {(detail.data?.sessions ?? []).some((s) => s.status === 'scheduled') ? (
          <Select
            label="Session"
            clearable
            value={session}
            onChange={setSession}
            data={(detail.data?.sessions ?? [])
              .filter((s) => s.status === 'scheduled')
              .map((s) => ({
                value: s.id,
                label: `${fmtDateTime(s.starts_at)}${s.venue ? `, ${s.venue}` : ''}${s.capacity ? ` (${s.taken}/${s.capacity})` : ''}`,
              }))}
          />
        ) : null}
        <Group justify="flex-end">
          <Button onClick={send} loading={busy} disabled={!ids.length}>
            Enrol
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

function Enrollments({ hr }) {
  const [status, setStatus] = useState(null);
  const [course, setCourse] = useState(null);
  const courses = useQuery(() => lrnApi.courses(), []);
  const q = useQuery(
    () => lrnApi.enrollments({ scope: hr ? 'all' : 'team', ...(course ? { courseId: course } : {}), ...(status ? { status } : {}) }),
    [hr, course, status],
  );
  const act = async (fn, msg) => {
    try {
      await fn();
      toast.ok(msg);
      await q.reload();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <Stack gap="md">
      <SectionTitle
        hint="Record attendance after a classroom session; online courses complete themselves when the learner passes."
        actions={
          <Group gap="xs">
            <Select
              size="xs"
              w={200}
              placeholder="All courses"
              clearable
              value={course}
              onChange={setCourse}
              data={(courses.data?.courses ?? []).map((c) => ({ value: c.id, label: c.title }))}
            />
            <Select
              size="xs"
              w={170}
              placeholder="Any status"
              clearable
              value={status}
              onChange={setStatus}
              data={Object.entries(ENROLL_STATUS).map(([v, s]) => ({ value: v, label: s.label }))}
            />
          </Group>
        }
      >
        {hr ? 'Enrolments' : "Your team's training"}
      </SectionTitle>
      <DataTable
        rows={q.data?.enrollments ?? []}
        rowKey={(e) => e.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="No enrolments"
        emptyHint="Assign a course from the Courses tab."
        columns={[
          {
            key: 'who',
            header: 'Employee',
            render: (e) => (
              <div>
                <Text size="sm" fw={500}>
                  {e.name_en}
                </Text>
                <Text size="xs" c="dimmed">
                  {e.emp_code}
                </Text>
              </div>
            ),
          },
          {
            key: 'course',
            header: 'Course',
            render: (e) => (
              <div>
                <Text size="sm">{e.title}</Text>
                <Text size="xs" c="dimmed">
                  {e.starts_at ? fmtDateTime(e.starts_at) : DELIVERY[e.delivery]}
                  {e.source === 'review' ? ' · from review' : e.source === 'mandatory' ? ' · mandatory' : ''}
                </Text>
              </div>
            ),
          },
          {
            key: 'status',
            header: 'Status',
            render: (e) => (
              <Group gap={4}>
                <Badge size="sm" variant="light" color={ENROLL_STATUS[e.status].color}>
                  {ENROLL_STATUS[e.status].label}
                </Badge>
                <CertificateExpiry e={e} />
              </Group>
            ),
          },
          {
            key: 'score',
            header: 'Score',
            align: 'right',
            render: (e) => <Text size="sm">{e.score !== null ? `${Number(e.score)}%` : '—'}</Text>,
          },
          {
            key: 'actions',
            header: '',
            render: (e) => (
              <Group gap={4} wrap="nowrap">
                {e.status === 'enrolled' && e.attended === null ? (
                  <>
                    <Button
                      size="compact-xs"
                      variant="light"
                      onClick={() => act(() => lrnApi.attendance(e.id, true), 'Attendance recorded.')}
                    >
                      Attended
                    </Button>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      color="red"
                      onClick={() => act(() => lrnApi.attendance(e.id, false), 'Marked absent.')}
                    >
                      Absent
                    </Button>
                  </>
                ) : null}
                {e.status === 'enrolled' && e.attended && e.questions > 0 ? (
                  <Text size="xs" c="dimmed">
                    Awaiting assessment
                  </Text>
                ) : null}
                {e.status === 'completed' ? (
                  <Anchor size="xs" href={lrnApi.certificateUrl(e.id)} target="_blank" rel="noopener">
                    Certificate
                  </Anchor>
                ) : null}
                {e.certificate_file_id ? (
                  <Anchor size="xs" href={lrnApi.certificateFileUrl(e.id)} target="_blank" rel="noopener">
                    Upload
                  </Anchor>
                ) : null}
                {['pending_approval', 'enrolled'].includes(e.status) ? (
                  <Button size="compact-xs" variant="subtle" color="gray" onClick={() => act(() => lrnApi.cancel(e.id), 'Cancelled.')}>
                    Cancel
                  </Button>
                ) : null}
              </Group>
            ),
          },
        ]}
      />
    </Stack>
  );
}

function Expiring() {
  const [days, setDays] = useState('60');
  const q = useQuery(() => lrnApi.expiring(Number(days)), [days]);
  return (
    <Stack gap="md">
      <SectionTitle
        hint="Certificates that have expired or expire soon. People and HR are reminded 30 days ahead."
        actions={
          <SegmentedControl
            size="xs"
            value={days}
            onChange={setDays}
            data={[
              { value: '30', label: '30 days' },
              { value: '60', label: '60 days' },
              { value: '180', label: '6 months' },
            ]}
          />
        }
      >
        Certificates expiring
      </SectionTitle>
      <DataTable
        rows={q.data?.enrollments ?? []}
        rowKey={(e) => e.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="Nothing expiring"
        emptyHint="All certificates are valid for this period."
        columns={[
          {
            key: 'who',
            header: 'Employee',
            render: (e) => (
              <Text size="sm" fw={500}>
                {e.name_en}
              </Text>
            ),
          },
          {
            key: 'course',
            header: 'Course',
            render: (e) => (
              <Text size="sm">
                {e.title}
                {e.mandatory ? ' (mandatory)' : ''}
              </Text>
            ),
          },
          { key: 'exp', header: 'Validity', render: (e) => <CertificateExpiry e={e} /> },
        ]}
      />
    </Stack>
  );
}

function Cost() {
  const [from, setFrom] = useState(addDays(todayNpt(), -365));
  const [to, setTo] = useState(todayNpt());
  const q = useQuery(() => (from && to ? lrnApi.costReport(from, to) : Promise.resolve(null)), [from, to]);
  const seats = useMemo(() => (q.data?.byCourse ?? []).reduce((s, x) => s + x.seats, 0), [q.data]);
  return (
    <Stack gap="md">
      <SectionTitle
        hint="Training spend from the cost per person of each course, counting enrolled and completed seats."
        actions={
          <Group gap="xs">
            <DateField value={from} onChange={setFrom} w={170} />
            <DateField value={to} onChange={setTo} w={170} />
          </Group>
        }
      >
        Training spend
      </SectionTitle>
      <StatStrip
        stats={[
          { key: 't', label: 'Total spend (NPR)', value: q.data ? fmtMoney(q.data.total) : '—' },
          { key: 's', label: 'Seats', value: seats },
        ]}
      />
      <DataTable
        rows={q.data?.byCourse ?? []}
        rowKey={(r) => r.id}
        loading={q.loading}
        error={q.error}
        emptyTitle="No training in this period"
        columns={[
          { key: 'title', header: 'Course', render: (r) => <Text size="sm">{r.title}</Text> },
          { key: 'seats', header: 'Seats', align: 'right', render: (r) => <Text size="sm">{r.seats}</Text> },
          { key: 'done', header: 'Completed', align: 'right', render: (r) => <Text size="sm">{r.completed}</Text> },
          {
            key: 'rating',
            header: 'Rating',
            align: 'right',
            render: (r) => <Text size="sm">{r.rating ? `${Number(r.rating).toFixed(1)}/5` : '—'}</Text>,
          },
          { key: 'cost', header: 'Spend', align: 'right', render: (r) => <Text size="sm">{fmtMoney(r.cost)}</Text> },
        ]}
      />
      <DataTable
        rows={q.data?.byDepartment ?? []}
        rowKey={(r) => r.department}
        loading={q.loading}
        emptyTitle="No departments"
        columns={[
          { key: 'd', header: 'Department', render: (r) => <Text size="sm">{r.department}</Text> },
          { key: 's', header: 'Seats', align: 'right', render: (r) => <Text size="sm">{r.seats}</Text> },
          { key: 'c', header: 'Spend', align: 'right', render: (r) => <Text size="sm">{fmtMoney(r.cost)}</Text> },
        ]}
      />
    </Stack>
  );
}
