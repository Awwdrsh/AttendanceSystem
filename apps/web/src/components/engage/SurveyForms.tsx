import { ActionIcon, Alert, Badge, Button, Checkbox, Group, Modal, NumberInput, Paper, Radio, SegmentedControl, Select, SimpleGrid, Stack, Switch, Table, Text, TextInput, Textarea, UnstyledButton } from '@mantine/core';
import { IconLock, IconPlus, IconTrash } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { engage, type Answer, type Question, type QuestionDraft, type QuestionKind, type SurveyFull, type SurveyInput, type SurveyResults } from '../../api/engage';
import { insights } from '../../api/insights';
import { addDays, fmtDate, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { DateField } from '../BsDatePicker';
import { HBars } from '../charts/Charts';
import { useVizColors } from '../charts/palette';

export const KIND_LABEL: Record<QuestionKind, string> = {
  rating: 'Rating 1 to 5', enps: 'Recommend score 0 to 10', single: 'Pick one', multi: 'Pick any', text: 'Written answer',
};

function ScaleButtons({ from, to, value, onChange, lowLabel, highLabel }: { from: number; to: number; value: number | undefined; onChange: (v: number) => void; lowLabel: string; highLabel: string }) {
  const nums = Array.from({ length: to - from + 1 }, (_, i) => from + i);
  return (
    <div>
      <Group gap={6} wrap="wrap">
        {nums.map((n) => (
          <UnstyledButton key={n} onClick={() => onChange(n)} aria-pressed={value === n} aria-label={String(n)}
            style={{ width: 38, height: 38, borderRadius: 8, textAlign: 'center', fontWeight: 600, border: `1px solid ${value === n ? 'var(--hj-hydro)' : 'var(--hj-line)'}`,
              background: value === n ? 'var(--hj-hydro)' : 'transparent', color: value === n ? 'var(--hj-on-hydro, #fff)' : undefined }}>{n}</UnstyledButton>
        ))}
      </Group>
      <Group justify="space-between" mt={4} maw={nums.length * 44}><Text size="xs" c="dimmed">{lowLabel}</Text><Text size="xs" c="dimmed">{highLabel}</Text></Group>
    </div>
  );
}

/** One question, answered. */
export function QuestionInput({ q, value, onChange }: { q: Question; value: Answer | undefined; onChange: (a: Answer) => void }) {
  const set = (patch: Omit<Answer, 'questionId'>) => onChange({ questionId: q.id, ...patch });
  switch (q.kind) {
    case 'rating': return <ScaleButtons from={1} to={5} value={value?.value} onChange={(v) => set({ value: v })} lowLabel="Strongly disagree" highLabel="Strongly agree" />;
    case 'enps': return <ScaleButtons from={0} to={10} value={value?.value} onChange={(v) => set({ value: v })} lowLabel="Not at all likely" highLabel="Extremely likely" />;
    case 'single': return (
      <Radio.Group value={value?.choices?.[0] ?? ''} onChange={(v) => set({ choices: [v] })}>
        <Stack gap={8}>{q.options.map((o) => <Radio key={o} value={o} label={o} />)}</Stack>
      </Radio.Group>
    );
    case 'multi': return (
      <Checkbox.Group value={value?.choices ?? []} onChange={(v) => set({ choices: v })}>
        <Stack gap={8}>{q.options.map((o) => <Checkbox key={o} value={o} label={o} />)}</Stack>
      </Checkbox.Group>
    );
    default: return <Textarea autosize minRows={3} maxLength={2000} value={value?.text ?? ''} onChange={(e) => set({ text: e.currentTarget.value })} aria-label={q.prompt} />;
  }
}

const isAnswered = (q: Question, a: Answer | undefined) => !!a && (a.value !== undefined || !!a.choices?.length || !!a.text?.trim());

/** Answer a survey or poll. Anonymous surveys say so up front. */
export function SurveyAnswerModal({ id, opened, onClose, onDone }: { id: string | null; opened: boolean; onClose: () => void; onDone: (results?: SurveyResults) => void }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof engage.survey>> | null>(null);
  const [answers, setAnswers] = useState<Record<string, Answer>>({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!opened || !id) return;
    setData(null); setAnswers({});
    engage.survey(id).then(setData).catch((e) => { toast.err(e); onClose(); });
  }, [opened, id]); // eslint-disable-line react-hooks/exhaustive-deps
  const missing = data ? data.questions.filter((q) => q.required && !isAnswered(q, answers[q.id])).length : 0;
  return (
    <Modal opened={opened} onClose={onClose} title={data?.survey.title ?? 'Survey'} centered size="lg">
      {!data ? <Text size="sm" c="dimmed">Loading…</Text> : (
        <Stack gap="lg">
          {data.survey.description ? <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{data.survey.description}</Text> : null}
          {data.survey.anonymous ? (
            <Alert variant="light" color="brand" icon={<IconLock size={16} />} p="sm">
              Anonymous. Your name is not stored with your answers, and results only appear once at least {data.survey.minGroup} people have answered.
            </Alert>
          ) : <Text size="xs" c="dimmed">HR can see who gave each answer.</Text>}
          {data.questions.map((q, i) => (
            <Stack key={q.id} gap={8}>
              <Text size="sm" fw={600}>{i + 1}. {q.prompt}{q.required ? '' : <Text span c="dimmed" fw={400}> (optional)</Text>}</Text>
              <QuestionInput q={q} value={answers[q.id]} onChange={(a) => setAnswers((cur) => ({ ...cur, [q.id]: a }))} />
            </Stack>
          ))}
          <Group justify="space-between">
            <Text size="xs" c="dimmed">Open until {fmtDate(data.survey.closesOn)}</Text>
            <Group>
              <Button variant="default" onClick={onClose}>Later</Button>
              <Button loading={busy} disabled={missing > 0} onClick={async () => {
                setBusy(true);
                try {
                  const list = data.questions.map((q) => answers[q.id]).filter((a): a is Answer => !!a && isAnswered(data.questions.find((q) => q.id === a.questionId)!, a))
                    .map((a) => ({ ...a, ...(a.text !== undefined ? { text: a.text.trim() } : {}) }));
                  const r = await engage.respond(data.survey.id, list);
                  toast.ok(data.survey.kind === 'poll' ? 'Vote counted.' : 'Thanks, your answers are in.');
                  onDone(r.results); onClose();
                } catch (e) { toast.err(e, 'Not sent'); } finally { setBusy(false); }
              }}>{missing > 0 ? `${missing} to answer` : 'Submit'}</Button>
            </Group>
          </Group>
        </Stack>
      )}
    </Modal>
  );
}

/** Results of a survey or poll: distributions, averages, eNPS, department breakdown (above the threshold) and written answers. */
export function SurveyResultsView({ results, anonymous }: { results: SurveyResults; anonymous: boolean }) {
  const c = useVizColors();
  if (results.hidden) {
    return (
      <Alert variant="light" color="gray" icon={<IconLock size={16} />}>
        {results.responses} of {results.audience} answered. Results appear once at least {results.minGroup} people have answered, so nobody can be singled out.
      </Alert>
    );
  }
  return (
    <Stack gap="lg">
      <Text size="sm" c="dimmed">{results.responses} of {results.audience} answered ({results.audience ? Math.round((results.responses / results.audience) * 100) : 0}%).</Text>
      {results.questions.map((q, i) => (
        <Paper key={q.questionId} withBorder p="md" radius="md">
          <Group justify="space-between" align="flex-start" mb="sm" wrap="nowrap">
            <Text size="sm" fw={600}>{i + 1}. {q.prompt}</Text>
            <Text size="xs" c="dimmed" style={{ flex: 'none' }}>{q.answered} answers</Text>
          </Group>
          {q.enps ? (
            <SimpleGrid cols={{ base: 2, xs: 4 }} mb="sm">
              <div><Text size="xs" c="dimmed">eNPS</Text><Text fw={700} fz={22} className="num">{q.enps.score > 0 ? '+' : ''}{q.enps.score}</Text></div>
              <div><Text size="xs" c="dimmed">Promoters (9–10)</Text><Text fw={600} className="num">{q.enps.promoters}</Text></div>
              <div><Text size="xs" c="dimmed">Passives (7–8)</Text><Text fw={600} className="num">{q.enps.passives}</Text></div>
              <div><Text size="xs" c="dimmed">Detractors (0–6)</Text><Text fw={600} className="num">{q.enps.detractors}</Text></div>
            </SimpleGrid>
          ) : q.average !== undefined && q.average !== null ? <Text size="sm" mb="sm">Average <Text span fw={700} className="num">{q.average}</Text> out of 5</Text> : null}
          {q.distribution ? <HBars data={q.distribution.map((d) => ({ label: q.kind === 'rating' || q.kind === 'enps' ? `Score ${d.label}` : d.label, value: d.count }))} max={11} color={c.series[0]!} /> : null}
          {q.breakdown && q.breakdown.length ? (
            <Table mt="sm" fz="xs" verticalSpacing={4}>
              <Table.Thead><Table.Tr><Table.Th>Department</Table.Th><Table.Th ta="right">Answers</Table.Th><Table.Th ta="right">{q.kind === 'enps' ? 'eNPS' : 'Average'}</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>{q.breakdown.map((b) => <Table.Tr key={b.group}><Table.Td>{b.group}</Table.Td><Table.Td ta="right" className="num">{b.responses}</Table.Td><Table.Td ta="right" className="num">{b.average ?? '—'}</Table.Td></Table.Tr>)}</Table.Tbody>
            </Table>
          ) : null}
          {q.texts ? (q.texts.length ? (
            <Stack gap={6}>
              {q.texts.map((t, j) => (
                <Paper key={j} p="xs" radius="sm" bg="var(--hj-canvas)">
                  <Text size="sm" style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{t.text}</Text>
                  {!anonymous && t.name ? <Text size="xs" c="dimmed" mt={2}>{t.name}</Text> : null}
                </Paper>
              ))}
            </Stack>
          ) : <Text size="sm" c="dimmed">No written answers.</Text>) : null}
        </Paper>
      ))}
    </Stack>
  );
}

const blankQuestion = (kind: QuestionKind = 'rating'): QuestionDraft => ({ kind, prompt: '', options: kind === 'single' || kind === 'multi' ? ['', ''] : [], required: kind !== 'text' });

const TEMPLATES: Record<string, { title: string; kind: 'survey' | 'poll'; questions: QuestionDraft[]; recurrence: 'none' | 'weekly' | 'monthly' }> = {
  pulse: { title: 'Monthly pulse', kind: 'survey', recurrence: 'monthly', questions: [
    { kind: 'enps', prompt: 'How likely are you to recommend CoreValley as a place to work?', options: [], required: true },
    { kind: 'rating', prompt: 'I have what I need to do my job well.', options: [], required: true },
    { kind: 'rating', prompt: 'My manager supports my growth.', options: [], required: true },
    { kind: 'text', prompt: 'What is one thing we could do better?', options: [], required: false },
  ] },
  poll: { title: 'Quick poll', kind: 'poll', recurrence: 'none', questions: [{ kind: 'single', prompt: '', options: ['', ''], required: true }] },
  blank: { title: '', kind: 'survey', recurrence: 'none', questions: [blankQuestion()] },
};

/** Create or edit a draft survey or poll. */
export function SurveyBuilder({ opened, onClose, onSaved, editing }: { opened: boolean; onClose: () => void; onSaved: () => void; editing: SurveyFull | null }) {
  const [form, setForm] = useState<SurveyInput>(() => ({ kind: 'survey', title: '', description: '', anonymous: true, minGroup: 5, audienceKind: 'all', audienceId: null,
    opensOn: todayNpt(), closesOn: addDays(todayNpt(), 7), recurrence: 'none', questions: [blankQuestion()] }));
  const [opts, setOpts] = useState<{ branches: Array<{ id: string; name: string }>; orgUnits: Array<{ id: string; name: string }> }>({ branches: [], orgUnits: [] });
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!opened) return;
    insights.filters().then(setOpts).catch(() => undefined);
    if (editing) {
      engage.survey(editing.id).then((r) => setForm({
        kind: r.survey.kind, title: r.survey.title, description: r.survey.description ?? '', anonymous: r.survey.anonymous, minGroup: r.survey.minGroup,
        audienceKind: r.survey.audienceKind, audienceId: r.survey.audienceId, opensOn: r.survey.opensOn, closesOn: r.survey.closesOn, recurrence: r.survey.recurrence,
        questions: r.questions.map((q) => ({ kind: q.kind, prompt: q.prompt, options: q.options, required: q.required })),
      })).catch((e) => toast.err(e));
    } else applyTemplate('pulse');
  }, [opened, editing]); // eslint-disable-line react-hooks/exhaustive-deps
  const applyTemplate = (t: keyof typeof TEMPLATES) => {
    const tpl = TEMPLATES[t]!;
    setForm((f) => ({ ...f, title: tpl.title, kind: tpl.kind, recurrence: tpl.recurrence, anonymous: tpl.kind === 'survey' ? true : f.anonymous, questions: tpl.questions.map((q) => ({ ...q, options: [...q.options] })) }));
  };
  const setQ = (i: number, patch: Partial<QuestionDraft>) => setForm((f) => ({ ...f, questions: f.questions.map((q, j) => (j === i ? { ...q, ...patch } : q)) }));
  const valid = form.title.trim() && form.questions.length && form.questions.every((q) => q.prompt.trim() && (q.kind !== 'single' && q.kind !== 'multi' || q.options.filter((o) => o.trim()).length >= 2))
    && form.closesOn >= form.opensOn && (form.audienceKind === 'all' || form.audienceId);
  return (
    <Modal opened={opened} onClose={onClose} title={editing ? 'Edit draft' : 'New survey or poll'} centered size="xl">
      <Stack>
        {!editing ? (
          <Group gap="xs">
            <Text size="sm" c="dimmed">Start from</Text>
            <Button size="compact-sm" variant="default" onClick={() => applyTemplate('pulse')}>Monthly pulse with eNPS</Button>
            <Button size="compact-sm" variant="default" onClick={() => applyTemplate('poll')}>Quick poll</Button>
            <Button size="compact-sm" variant="default" onClick={() => applyTemplate('blank')}>Blank</Button>
          </Group>
        ) : null}
        <SegmentedControl value={form.kind} onChange={(v) => setForm((f) => ({ ...f, kind: v as 'survey' | 'poll', ...(v === 'poll' ? { questions: [f.questions.find((q) => q.kind === 'single' || q.kind === 'multi') ?? blankQuestion('single')] } : {}) }))}
          data={[{ value: 'survey', label: 'Survey' }, { value: 'poll', label: 'Poll (one question, results after voting)' }]} />
        <TextInput label="Title" required maxLength={160} value={form.title} onChange={(e) => { const v = e.currentTarget.value; setForm((f) => ({ ...f, title: v })); }} />
        <Textarea label="Introduction (optional)" autosize minRows={2} maxLength={2000} value={form.description ?? ''} onChange={(e) => { const v = e.currentTarget.value; setForm((f) => ({ ...f, description: v })); }} />
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <DateField label="Opens" value={form.opensOn} onChange={(v) => v && setForm((f) => ({ ...f, opensOn: v }))} minDate={todayNpt()} required />
          <DateField label="Closes" value={form.closesOn} onChange={(v) => v && setForm((f) => ({ ...f, closesOn: v }))} minDate={form.opensOn} required />
          <Select label="Who is it for?" value={form.audienceKind} allowDeselect={false} onChange={(v) => setForm((f) => ({ ...f, audienceKind: (v ?? 'all') as SurveyInput['audienceKind'], audienceId: null }))}
            data={[{ value: 'all', label: 'Everyone' }, { value: 'branch', label: 'A branch' }, { value: 'department', label: 'A department' }]} />
          {form.audienceKind !== 'all' ? (
            <Select label={form.audienceKind === 'branch' ? 'Branch' : 'Department'} required searchable value={form.audienceId ?? null} onChange={(v) => setForm((f) => ({ ...f, audienceId: v }))}
              data={(form.audienceKind === 'branch' ? opts.branches : opts.orgUnits).map((o) => ({ value: o.id, label: o.name }))} />
          ) : (
            <Select label="Repeat" value={form.recurrence} allowDeselect={false} onChange={(v) => setForm((f) => ({ ...f, recurrence: (v ?? 'none') as SurveyInput['recurrence'] }))}
              data={[{ value: 'none', label: 'Once' }, { value: 'weekly', label: 'Every week' }, { value: 'monthly', label: 'Every month' }]} />
          )}
        </SimpleGrid>
        <Group gap="lg" align="flex-end">
          <Switch label="Anonymous" description="Names are never stored with answers." checked={form.anonymous} onChange={(e) => { const v = e.currentTarget.checked; setForm((f) => ({ ...f, anonymous: v })); }} />
          {form.anonymous ? <NumberInput label="Show results from" description="answers (protects small groups)" min={3} max={50} w={190} value={form.minGroup} onChange={(v) => setForm((f) => ({ ...f, minGroup: typeof v === 'number' ? v : 5 }))} /> : null}
        </Group>
        <Text fw={600} size="sm" mt="xs">Questions</Text>
        {form.questions.map((q, i) => (
          <Paper key={i} withBorder p="sm" radius="md">
            <Stack gap="xs">
              <Group gap="xs" align="flex-end" wrap="nowrap">
                <TextInput style={{ flex: 1 }} label={`Question ${i + 1}`} maxLength={300} value={q.prompt} onChange={(e) => setQ(i, { prompt: e.currentTarget.value })} required />
                <Select w={200} label="Type" allowDeselect={false} value={q.kind}
                  data={(Object.keys(KIND_LABEL) as QuestionKind[]).filter((k) => form.kind === 'survey' || k === 'single' || k === 'multi').map((k) => ({ value: k, label: KIND_LABEL[k] }))}
                  onChange={(v) => { const k = (v ?? 'rating') as QuestionKind; setQ(i, { kind: k, options: k === 'single' || k === 'multi' ? (q.options.length >= 2 ? q.options : ['', '']) : [] }); }} />
                {form.kind === 'survey' && form.questions.length > 1 ? (
                  <ActionIcon variant="subtle" color="gray" mb={4} aria-label={`Remove question ${i + 1}`} onClick={() => setForm((f) => ({ ...f, questions: f.questions.filter((_, j) => j !== i) }))}><IconTrash size={16} /></ActionIcon>
                ) : null}
              </Group>
              {q.kind === 'single' || q.kind === 'multi' ? (
                <Stack gap={6} pl="sm">
                  {q.options.map((o, j) => (
                    <Group key={j} gap={6} wrap="nowrap">
                      <TextInput size="xs" style={{ flex: 1 }} placeholder={`Option ${j + 1}`} maxLength={120} value={o} onChange={(e) => { const v = e.currentTarget.value; setQ(i, { options: q.options.map((x, k) => (k === j ? v : x)) }); }} />
                      {q.options.length > 2 ? <ActionIcon size="sm" variant="subtle" color="gray" aria-label="Remove option" onClick={() => setQ(i, { options: q.options.filter((_, k) => k !== j) })}><IconTrash size={14} /></ActionIcon> : null}
                    </Group>
                  ))}
                  {q.options.length < 12 ? <Button size="compact-xs" variant="subtle" w="fit-content" leftSection={<IconPlus size={12} />} onClick={() => setQ(i, { options: [...q.options, ''] })}>Add option</Button> : null}
                </Stack>
              ) : null}
              {form.kind === 'survey' ? <Checkbox size="xs" label="Answer required" checked={q.required} onChange={(e) => setQ(i, { required: e.currentTarget.checked })} /> : null}
            </Stack>
          </Paper>
        ))}
        {form.kind === 'survey' && form.questions.length < 30 ? (
          <Button variant="default" w="fit-content" leftSection={<IconPlus size={14} />} onClick={() => setForm((f) => ({ ...f, questions: [...f.questions, blankQuestion()] }))}>Add question</Button>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button loading={busy} disabled={!valid} onClick={async () => {
            setBusy(true);
            const body: SurveyInput = { ...form, title: form.title.trim(), description: form.description?.trim() || null,
              questions: form.questions.map((q) => ({ ...q, prompt: q.prompt.trim(), options: q.options.map((o) => o.trim()).filter(Boolean) })) };
            try {
              if (editing) await engage.updateSurvey(editing.id, body); else await engage.createSurvey(body);
              toast.ok('Saved as a draft. Publish it when you are ready.'); onSaved(); onClose();
            } catch (e) { toast.err(e); } finally { setBusy(false); }
          }}>Save draft</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export function statusBadge(s: SurveyFull, today = todayNpt()) {
  if (s.status === 'draft') return <Badge variant="light" color="gray">Draft</Badge>;
  if (s.status === 'closed') return <Badge variant="light" color="gray">Closed</Badge>;
  if (s.opensOn > today) return <Badge variant="light" color="blue">Opens {fmtDate(s.opensOn)}</Badge>;
  return <Badge variant="light" color="brand">Open</Badge>;
}
