import {
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Checkbox,
  Container,
  FileButton,
  Group,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core';
import { IconArrowLeft, IconBriefcase, IconCircleCheck, IconMapPin, IconPaperclip, IconSearch } from '@tabler/icons-react';
import { useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { errorMessage } from '../../api/client';
import { fileToDataUrl } from '../../api/files';
import { careers, EMPLOYMENT, experienceText, WORK_MODE } from '../../api/hiring';
import { Brand } from '../../components/Brand';
import { EmptyState } from '../../components/EmptyState';
import { fmtDateShort, fmtMoney } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

const CV_MAX = 4 * 1024 * 1024;

/** Plain text with "- " bullets, rendered without any HTML from the job post. */
function Prose({ text }) {
  const blocks = [];
  let bullets = [];
  const flush = (k) => {
    if (bullets.length) {
      blocks.push(
        <ul key={`u${k}`} style={{ margin: '4px 0 12px', paddingLeft: 20 }}>
          {bullets.map((b, i) => (
            <li key={i}>
              <Text size="sm" span>
                {b}
              </Text>
            </li>
          ))}
        </ul>,
      );
      bullets = [];
    }
  };
  text.split('\n').forEach((line, i) => {
    const t = line.trim();
    if (/^[-*•]\s+/.test(t)) {
      bullets.push(t.replace(/^[-*•]\s+/, ''));
      return;
    }
    flush(i);
    if (t)
      blocks.push(
        <Text key={i} size="sm" mb={8}>
          {t}
        </Text>,
      );
  });
  flush(-1);
  return <div>{blocks}</div>;
}

function Meta({ job }) {
  const exp = experienceText(job.experience_min, job.experience_max);
  const salary =
    job.salary_min || job.salary_max
      ? `NPR ${[job.salary_min, job.salary_max]
          .filter(Boolean)
          .map((x) => fmtMoney(x, { decimals: 0 }))
          .join(' – ')} a month`
      : null;
  return (
    <Group gap={6} wrap="wrap">
      <Badge variant="light" color="brand">
        {WORK_MODE[job.work_mode]}
      </Badge>
      <Badge variant="light" color="gray">
        {EMPLOYMENT[job.employment_type]}
      </Badge>
      {job.location ? (
        <Badge variant="outline" color="gray" leftSection={<IconMapPin size={11} />}>
          {job.location}
        </Badge>
      ) : null}
      {exp ? (
        <Badge variant="outline" color="gray">
          {exp}
        </Badge>
      ) : null}
      {salary ? (
        <Badge variant="outline" color="teal">
          {salary}
        </Badge>
      ) : null}
    </Group>
  );
}

function Shell({ company, children }) {
  return (
    <Box mih="100vh" style={{ background: 'var(--mantine-color-body)' }}>
      <Box style={{ borderBottom: '1px solid var(--hj-line, #e6e7ea)' }}>
        <Container size="md" py="sm">
          <Group justify="space-between">
            <Brand />
            <Text size="sm" c="dimmed">
              {company ? `Careers at ${company}` : 'Careers'}
            </Text>
          </Group>
        </Container>
      </Box>
      <Container size="md" py="xl">
        {children}
      </Container>
    </Box>
  );
}

function JobList({ company }) {
  const [search, setSearch] = useState('');
  const [mode, setMode] = useState('all');
  const [category, setCategory] = useState(null);
  const q = useQuery(
    () => careers.jobs({ company, q: search.trim() || undefined, workMode: mode === 'all' ? null : mode, category }),
    [company, search, mode, category],
  );
  const suffix = company ? `?company=${encodeURIComponent(company)}` : '';
  return (
    <Shell {...(q.data ? { company: q.data.company.name } : {})}>
      <Stack gap="lg">
        <div>
          <Title order={1} fz={30} fw={700} style={{ letterSpacing: '-0.02em' }}>
            {q.data ? `Work with ${q.data.company.name}` : 'Open positions'}
          </Title>
          <Text c="dimmed" mt={4}>
            Find a role that fits you and apply in a few minutes. All you need is your CV as a PDF.
          </Text>
        </div>
        <Group gap="sm" wrap="wrap">
          <TextInput
            style={{ flex: '1 1 220px' }}
            placeholder="Search jobs"
            leftSection={<IconSearch size={16} />}
            value={search}
            onChange={(e) => setSearch(e.currentTarget.value)}
            maxLength={100}
            aria-label="Search jobs"
          />
          <SegmentedControl
            value={mode}
            onChange={setMode}
            data={[
              { value: 'all', label: 'Any' },
              { value: 'onsite', label: 'On site' },
              { value: 'hybrid', label: 'Hybrid' },
              { value: 'remote', label: 'Remote' },
            ]}
          />
          {q.data?.categories.length ? (
            <Select
              w={180}
              placeholder="All categories"
              clearable
              value={category}
              onChange={setCategory}
              data={q.data.categories}
              aria-label="Category"
            />
          ) : null}
        </Group>
        {q.error ? <EmptyState title="Careers page not available" hint="This page may have moved. Check the link you were given." /> : null}
        {q.loading && !q.data ? (
          <Stack>
            {[0, 1, 2].map((i) => (
              <Skeleton key={i} h={92} radius="lg" />
            ))}
          </Stack>
        ) : null}
        {q.data && q.data.jobs.length === 0 ? (
          <EmptyState icon={<IconBriefcase size={20} />} title="No open positions right now" hint="Please check back soon." />
        ) : null}
        <Stack gap="sm">
          {(q.data?.jobs ?? []).map((j) => (
            <Paper
              key={j.id}
              component={Link}
              to={`/careers/${j.id}${suffix}`}
              withBorder
              radius="lg"
              p="md"
              style={{ textDecoration: 'none', color: 'inherit' }}
            >
              <Group justify="space-between" align="flex-start" wrap="wrap" gap="sm">
                <div>
                  <Text fw={600} size="lg">
                    {j.title}
                  </Text>
                  <Text size="sm" c="dimmed" mb={8}>
                    {[j.department, j.category].filter(Boolean).join(' · ') || ' '}
                  </Text>
                  <Meta job={j} />
                </div>
                {j.deadline ? (
                  <Text size="xs" c="dimmed">
                    Apply by {fmtDateShort(j.deadline)}
                  </Text>
                ) : null}
              </Group>
            </Paper>
          ))}
        </Stack>
      </Stack>
    </Shell>
  );
}

function JobDetail({ jobId, company }) {
  const q = useQuery(() => careers.job(jobId), [jobId]);
  const back = `/careers${company ? `?company=${encodeURIComponent(company)}` : ''}`;
  const [f, setF] = useState({ name: '', email: '', phone: '', experienceYears: '', currentEmployer: '', coverNote: '', website: '' });
  const [answers, setAnswers] = useState({});
  const [cv, setCv] = useState(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [done, setDone] = useState(null);
  const job = q.data?.job;

  const submit = async () => {
    if (!job || !cv) return;
    setBusy(true);
    setError(null);
    try {
      if (cv.size > CV_MAX) throw new Error('Your CV is larger than 4 MB. Please send a smaller PDF.');
      if (cv.type !== 'application/pdf') throw new Error('Please attach your CV as a PDF.');
      const r = await careers.apply(job.id, {
        name: f.name.trim(),
        email: f.email.trim(),
        phone: f.phone.trim(),
        experienceYears: f.experienceYears === '' ? null : f.experienceYears,
        currentEmployer: f.currentEmployer.trim() || null,
        coverNote: f.coverNote.trim() || null,
        answers,
        cvFilename: cv.name,
        cvDataUrl: await fileToDataUrl(cv),
        consent: true,
        ...(f.website ? { website: f.website } : {}),
      });
      setDone(r.message);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const required = (job?.questions ?? [])
    .filter((x) => x.required)
    .every((x) => answers[x.id] !== undefined && String(answers[x.id]).trim() !== '');
  const ready =
    f.name.trim().length >= 2 &&
    /^\S+@\S+\.\S+$/.test(f.email) &&
    /^[0-9+\- ()]{6,30}$/.test(f.phone.trim()) &&
    !!cv &&
    consent &&
    required;

  return (
    <Shell {...(q.data ? { company: q.data.company.name } : {})}>
      <Stack gap="lg">
        <Anchor component={Link} to={back} size="sm">
          <Group gap={4}>
            <IconArrowLeft size={14} />
            All open positions
          </Group>
        </Anchor>
        {q.error ? <EmptyState title="This job is no longer accepting applications" hint="It may have been filled or closed." /> : null}
        {!job && !q.error ? <Skeleton h={300} radius="lg" /> : null}
        {job ? (
          <>
            <div>
              <Title order={1} fz={28} fw={700} style={{ letterSpacing: '-0.02em' }}>
                {job.title}
              </Title>
              <Text c="dimmed" size="sm" mt={2} mb="sm">
                {[q.data?.company.name, job.department].filter(Boolean).join(' · ')}
                {job.deadline ? ` · Apply by ${fmtDateShort(job.deadline)}` : ''}
              </Text>
              <Meta job={job} />
            </div>
            {job.description ? (
              <Paper withBorder radius="lg" p="lg">
                <Text fw={600} mb="sm">
                  About the role
                </Text>
                <Prose text={job.description} />
              </Paper>
            ) : null}
            {job.requirements ? (
              <Paper withBorder radius="lg" p="lg">
                <Text fw={600} mb="sm">
                  What you need
                </Text>
                <Prose text={job.requirements} />
              </Paper>
            ) : null}
            <Paper withBorder radius="lg" p="lg" id="apply">
              {done ? (
                <Stack align="center" gap="xs" py="lg" ta="center">
                  <IconCircleCheck size={40} stroke={1.5} color="var(--mantine-color-teal-6)" />
                  <Text fw={600} size="lg">
                    {done}
                  </Text>
                  <Text c="dimmed" size="sm">
                    We have sent a confirmation to your e-mail. We will contact you if your profile matches the role.
                  </Text>
                </Stack>
              ) : (
                <Stack gap="md">
                  <Text fw={600} size="lg">
                    Apply for this job
                  </Text>
                  <SimpleGrid cols={{ base: 1, sm: 2 }}>
                    <TextInput
                      label="Full name"
                      required
                      autoComplete="name"
                      value={f.name}
                      onChange={(e) => setF({ ...f, name: e.currentTarget.value })}
                      maxLength={120}
                    />
                    <TextInput
                      label="E-mail"
                      required
                      type="email"
                      autoComplete="email"
                      value={f.email}
                      onChange={(e) => setF({ ...f, email: e.currentTarget.value })}
                      maxLength={200}
                    />
                    <TextInput
                      label="Phone"
                      required
                      type="tel"
                      autoComplete="tel"
                      value={f.phone}
                      onChange={(e) => setF({ ...f, phone: e.currentTarget.value })}
                      maxLength={30}
                    />
                    <NumberInput
                      label="Years of work experience"
                      min={0}
                      max={60}
                      decimalScale={1}
                      value={f.experienceYears}
                      onChange={(v) => setF({ ...f, experienceYears: v === '' ? '' : Number(v) })}
                    />
                    <TextInput
                      label="Current or last employer"
                      value={f.currentEmployer}
                      onChange={(e) => setF({ ...f, currentEmployer: e.currentTarget.value })}
                      maxLength={120}
                    />
                  </SimpleGrid>
                  {(job.questions ?? []).map((x) =>
                    x.kind === 'yes_no' ? (
                      <Select
                        key={x.id}
                        label={x.label}
                        required={x.required}
                        value={answers[x.id] ?? null}
                        onChange={(v) =>
                          setAnswers((a) => {
                            const n = { ...a };
                            if (v) n[x.id] = v;
                            else delete n[x.id];
                            return n;
                          })
                        }
                        data={[
                          { value: 'yes', label: 'Yes' },
                          { value: 'no', label: 'No' },
                        ]}
                        clearable={!x.required}
                      />
                    ) : x.kind === 'number' ? (
                      <NumberInput
                        key={x.id}
                        label={x.label}
                        required={x.required}
                        min={0}
                        value={answers[x.id] ?? ''}
                        onChange={(v) =>
                          setAnswers((a) => {
                            const n = { ...a };
                            if (v === '') delete n[x.id];
                            else n[x.id] = Number(v);
                            return n;
                          })
                        }
                      />
                    ) : (
                      <TextInput
                        key={x.id}
                        label={x.label}
                        required={x.required}
                        maxLength={1000}
                        value={answers[x.id] ?? ''}
                        onChange={(e) => {
                          const v = e.currentTarget.value;
                          setAnswers((a) => ({ ...a, [x.id]: v }));
                        }}
                      />
                    ),
                  )}
                  <Textarea
                    label="Why are you a good fit?"
                    description="Optional"
                    autosize
                    minRows={3}
                    maxLength={4000}
                    value={f.coverNote}
                    onChange={(e) => setF({ ...f, coverNote: e.currentTarget.value })}
                  />
                  {/* Hidden from people; bots fill it in. */}
                  <input
                    type="text"
                    name="website"
                    tabIndex={-1}
                    autoComplete="off"
                    aria-hidden="true"
                    value={f.website}
                    onChange={(e) => setF({ ...f, website: e.currentTarget.value })}
                    style={{ position: 'absolute', left: '-10000px', width: 1, height: 1, opacity: 0 }}
                  />
                  <Group gap="sm">
                    <FileButton accept="application/pdf" onChange={setCv}>
                      {(props) => (
                        <Button {...props} variant="default" leftSection={<IconPaperclip size={16} />}>
                          {cv ? 'Change CV' : 'Attach CV (PDF, max 4 MB)'}
                        </Button>
                      )}
                    </FileButton>
                    {cv ? (
                      <Text size="sm" c={cv.size > CV_MAX ? 'red' : 'dimmed'}>
                        {cv.name}
                        {cv.size > CV_MAX ? ' is larger than 4 MB' : ''}
                      </Text>
                    ) : null}
                  </Group>
                  <Checkbox
                    checked={consent}
                    onChange={(e) => setConsent(e.currentTarget.checked)}
                    label={`I agree that ${q.data?.company.name ?? 'the company'} may store and use my details and CV to consider me for this and similar roles. Details of people who are not selected are deleted after a few months.`}
                  />
                  {error ? (
                    <Alert color="red" variant="light">
                      {error}
                    </Alert>
                  ) : null}
                  <Group justify="flex-end">
                    <Button size="md" onClick={submit} loading={busy} disabled={!ready}>
                      Submit application
                    </Button>
                  </Group>
                </Stack>
              )}
            </Paper>
          </>
        ) : null}
      </Stack>
    </Shell>
  );
}

/** Public careers site: /careers lists open jobs; /careers/<jobId> shows one with the application form. No sign-in. */
export default function CareersPage() {
  const params = useParams();
  const [search] = useSearchParams();
  const company = search.get('company');
  const jobId = (params['*'] ?? '').split('/')[0];
  return jobId ? <JobDetail jobId={jobId} company={company} /> : <JobList company={company} />;
}
