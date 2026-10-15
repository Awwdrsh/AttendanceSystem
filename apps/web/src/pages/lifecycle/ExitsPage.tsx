import { ActionIcon, Alert, Anchor, Badge, Button, Drawer, Group, List, Modal, Paper, SegmentedControl, Select, SimpleGrid, Stack, Switch, Text, TagsInput, Textarea, TextInput } from '@mantine/core';
import { IconAlertTriangle, IconCash, IconPlus, IconTrash } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { EXIT_KIND_LABEL, lifecycle, RESIGN_REASONS, type ExitKind, type ExitQuestion, type ExitRow } from '../../api/lifecycle';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { Field, StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { bsMonthLabel, fmtAd, fmtMoney, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { TaskRow } from './CaseDrawer';
import { progressCell } from './OnboardingPage';
import { TemplatesPanel } from './TemplatesPanel';

const ICON = { size: 16, stroke: 1.6 } as const;
const STATUS: Record<ExitRow['status'], { label: string; color: string }> = {
  pending: { label: 'Awaiting approval', color: 'yellow' }, accepted: { label: 'Serving notice', color: 'blue' }, completed: { label: 'Left', color: 'gray' },
  rejected: { label: 'Not accepted', color: 'gray' }, withdrawn: { label: 'Withdrawn', color: 'gray' }, cancelled: { label: 'Cancelled', color: 'gray' },
};
export const exitStatusBadge = (s: ExitRow['status']) => <Badge variant="light" color={STATUS[s].color}>{STATUS[s].label}</Badge>;
const lwdOf = (x: ExitRow) => x.accepted_lwd ?? x.proposed_lwd;

function ExitDrawer({ exitId, onClose, onChanged }: { exitId: string | null; onClose: () => void; onChanged: () => void }) {
  const q = useQuery(() => (exitId ? lifecycle.exit(exitId) : Promise.resolve(null)), [exitId]);
  const x = q.data?.exit;
  const c = useQuery(() => (x?.case_id ? lifecycle.case(x.case_id) : Promise.resolve(null)), [x?.case_id]);
  const iv = useQuery(() => (x?.interview_done ? lifecycle.interview(x.id) : Promise.resolve(null)), [x?.id, x?.interview_done]);
  const [lwd, setLwd] = useState<string | null>(null);
  const [cancel, setCancel] = useState(false); const [reason, setReason] = useState('');
  const [force, setForce] = useState(false);
  useEffect(() => { setLwd(x ? lwdOf(x) : null); }, [x?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const reload = async () => { await Promise.all([q.reload(), c.reload()]); onChanged(); };
  const act = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); toast.ok(ok); await reload(); } catch (e) { toast.err(e); } };
  const s = q.data?.settlement; const b = q.data?.blockers;
  const open = x && ['pending', 'accepted'].includes(x.status);
  const past = x ? lwdOf(x) <= todayNpt() : false;
  return (
    <Drawer opened={!!exitId} onClose={onClose} position="right" size="xl" title={x ? <div><Text fw={600}>{x.name_en}</Text><Text size="xs" c="dimmed">{EXIT_KIND_LABEL[x.kind]}, {x.voluntary ? 'voluntary' : 'involuntary'}</Text></div> : 'Exit'}>
      {x ? (
        <Stack gap="lg">
          <Group gap="xs">{exitStatusBadge(x.status)}{x.reason_category ? <Badge variant="outline" color="gray">{x.reason_category}</Badge> : null}</Group>
          <SimpleGrid cols={{ base: 2, sm: 3 }}>
            <Field label="Submitted">{fmtAd(x.submitted_on)}</Field>
            <Field label="Last working day">{fmtAd(lwdOf(x))}</Field>
            <Field label="Joined">{fmtAd(x.join_date)}</Field>
          </SimpleGrid>
          {x.reason ? <Text size="sm" c="dimmed" style={{ whiteSpace: 'pre-wrap' }}>“{x.reason}”</Text> : null}
          {open ? (
            <Group align="flex-end" gap="sm">
              <DateField label="Change last working day" value={lwd} onChange={setLwd} />
              <Button variant="default" disabled={!lwd || lwd === lwdOf(x)} onClick={() => lwd && act(() => lifecycle.updateExit(x.id, { lastWorkingDay: lwd }), 'Last working day changed.')}>Save</Button>
            </Group>
          ) : null}

          {s ? (
            <Paper withBorder p="md">
              <Group gap="xs" mb="xs"><IconCash {...ICON} /><Text fw={600} size="sm">Final settlement</Text>{s.run?.locked ? <Badge size="sm" color="teal" variant="light">Paid in a locked run</Badge> : null}</Group>
              {s.steps.length ? <List size="sm" spacing={4}>{s.steps.map((t) => <List.Item key={t}>{t}</List.Item>)}</List> : <Text size="sm" c="dimmed">Nothing left to do in payroll.</Text>}
              <Group gap="lg" mt="sm">
                {s.period ? <Text size="xs" c="dimmed">Pay period {bsMonthLabel(s.period.bsYear, s.period.bsMonth)}</Text> : null}
                {s.activeLoans ? <Text size="xs" c="dimmed">Loans outstanding {fmtMoney(s.loanOutstanding)}</Text> : null}
                {s.encashableLeaveDays ? <Text size="xs" c="dimmed">Encashable leave {s.encashableLeaveDays.toFixed(1)} days</Text> : null}
              </Group>
              <Anchor component={Link} to={s.run ? `/payroll/runs/${s.run.id}` : '/payroll'} size="sm" mt="xs" display="inline-block">{s.run ? 'Open the settlement run' : 'Go to payroll'}</Anchor>
            </Paper>
          ) : null}

          <div>
            <SectionTitle hint={b && (b.openTasks || b.assetsHeld) ? `${b.openTasks} open, ${b.assetsHeld} ${b.assetsHeld === 1 ? 'asset' : 'assets'} not returned` : 'Everything is cleared.'}>Clearance</SectionTitle>
            {b?.assetsHeld ? <Alert color="orange" variant="light" icon={<IconAlertTriangle {...ICON} />} mb="sm">Still holds {b.assetsHeld} company {b.assetsHeld === 1 ? 'asset' : 'assets'}. Record the return under Onboarding, Assets (or mark it lost).</Alert> : null}
            {c.data?.tasks.length ? <Paper withBorder p="md"><Stack gap="md">{c.data.tasks.map((t) => <TaskRow key={t.id} task={t} onChanged={reload} canSkip={c.data?.case.status === 'active'} />)}</Stack></Paper>
              : <Text size="sm" c="dimmed">{x.status === 'pending' ? 'The clearance checklist starts once the resignation is accepted.' : 'No clearance checklist.'}</Text>}
          </div>

          <div>
            <SectionTitle hint="Confidential: only HR can read it.">Exit interview</SectionTitle>
            {iv.data?.interview ? (
              <Paper withBorder p="md"><Stack gap="sm">
                <Text size="xs" c="dimmed">{iv.data.interview.byEmployee ? 'Filled in by the employee' : `Recorded by ${iv.data.interview.recordedBy}`}, {fmtAd(iv.data.interview.createdAt.slice(0, 10))}</Text>
                {iv.data.interview.answers.map((a) => <div key={a.questionId}><Text size="sm" fw={500}>{a.question}</Text><Text size="sm" c="dimmed">{a.kind === 'rating' ? `${a.answer} of 5` : String(a.answer)}</Text></div>)}
              </Stack></Paper>
            ) : <InterviewForm exitId={x.id} onDone={reload} />}
          </div>

          {open ? (
            <Group justify="space-between" wrap="wrap">
              <Button variant="subtle" color="red" onClick={() => setCancel(true)}>Cancel this exit</Button>
              {x.status === 'accepted' ? (
                <Group gap="sm">
                  {b?.openTasks ? <Switch label="Skip open tasks" checked={force} onChange={(e) => setForce(e.currentTarget.checked)} /> : null}
                  <Button disabled={!past || !!b?.assetsHeld || (!!b?.openTasks && !force)} onClick={() => act(() => lifecycle.completeExit(x.id, force), 'Exit completed.')}>{past ? 'Complete exit' : `Complete after ${fmtAd(lwdOf(x))}`}</Button>
                </Group>
              ) : null}
            </Group>
          ) : null}
          <Modal opened={cancel} onClose={() => setCancel(false)} title="Cancel this exit" centered>
            <Stack>
              <Text size="sm" c="dimmed">The employee stays on. Their exit date is cleared and the clearance checklist closes.</Text>
              <Textarea label="Reason" value={reason} onChange={(e) => setReason(e.currentTarget.value)} autosize minRows={2} />
              <Group justify="flex-end"><Button variant="default" onClick={() => setCancel(false)}>Back</Button><Button color="red" disabled={reason.trim().length < 3} onClick={async () => { await act(() => lifecycle.cancelExit(x.id, reason.trim()), 'Exit cancelled.'); setCancel(false); }}>Cancel exit</Button></Group>
            </Stack>
          </Modal>
        </Stack>
      ) : null}
    </Drawer>
  );
}

/** The exit interview questionnaire, filled in by the leaver or by HR on their behalf. */
export function InterviewForm({ exitId, onDone }: { exitId: string; onDone: () => void }) {
  const q = useQuery(() => lifecycle.questions(), []);
  const [answers, setAnswers] = useState<Record<string, string | number>>({});
  const [busy, setBusy] = useState(false);
  const qs = (q.data?.questions ?? []).filter((x) => x.active);
  const submit = async () => {
    setBusy(true);
    try {
      await lifecycle.submitInterview(exitId, Object.entries(answers).filter(([, v]) => v !== '').map(([questionId, answer]) => ({ questionId, answer })));
      toast.ok('Thank you. Your answers were saved.'); onDone();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  if (!qs.length) return <Text size="sm" c="dimmed">No questions set up.</Text>;
  return (
    <Paper withBorder p="md">
      <Stack gap="md">
        {qs.map((x) => x.kind === 'text'
          ? <Textarea key={x.id} label={x.question} value={String(answers[x.id] ?? '')} onChange={(e) => setAnswers({ ...answers, [x.id]: e.currentTarget.value })} autosize minRows={2} />
          : x.kind === 'rating'
            ? <div key={x.id}><Text size="sm" fw={500} mb={4}>{x.question}</Text><SegmentedControl value={String(answers[x.id] ?? '')} onChange={(v) => setAnswers({ ...answers, [x.id]: Number(v) })} data={['1', '2', '3', '4', '5']} /></div>
            : <Select key={x.id} label={x.question} value={(answers[x.id] as string | undefined) ?? null} onChange={(v) => setAnswers({ ...answers, [x.id]: v ?? '' })} data={x.options} />)}
        <Group justify="flex-end"><Button loading={busy} disabled={!Object.values(answers).some((v) => v !== '')} onClick={submit}>Save answers</Button></Group>
      </Stack>
    </Paper>
  );
}

function QuestionsEditor() {
  const q = useQuery(() => lifecycle.questions(), []);
  const [rows, setRows] = useState<Array<Omit<ExitQuestion, 'id' | 'sort'> & { id?: string }>>([]);
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (q.data) setRows(q.data.questions.map(({ sort: _s, ...r }) => r)); }, [q.data]);
  const set = (i: number, patch: Partial<ExitQuestion>) => setRows((r) => r.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  const save = async () => {
    setBusy(true);
    try { await lifecycle.saveQuestions(rows.filter((r) => r.question.trim()).map((r) => ({ ...(r.id ? { id: r.id } : {}), question: r.question.trim(), kind: r.kind, options: r.options, active: r.active }))); toast.ok('Questions saved.'); await q.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack gap="md" maw={760}>
      <SectionTitle hint="Answers are encrypted and only HR can read them. Removing a question switches it off; old answers keep it.">Exit interview questions</SectionTitle>
      {rows.map((r, i) => (
        <Paper key={r.id ?? `new-${i}`} withBorder p="sm">
          <Group align="flex-end" gap="xs" wrap="nowrap">
            <TextInput label={`Question ${i + 1}`} value={r.question} onChange={(e) => set(i, { question: e.currentTarget.value })} style={{ flex: 1 }} />
            <Select label="Answer" w={130} value={r.kind} allowDeselect={false} onChange={(v) => v && set(i, { kind: v as ExitQuestion['kind'] })} data={[{ value: 'text', label: 'Text' }, { value: 'rating', label: 'Rating 1–5' }, { value: 'choice', label: 'Choice' }]} />
            <Switch label="On" mb={8} checked={r.active} onChange={(e) => set(i, { active: e.currentTarget.checked })} />
            {!r.id ? <ActionIcon variant="subtle" color="red" mb={4} onClick={() => setRows(rows.filter((_, j) => j !== i))} aria-label="Remove"><IconTrash {...ICON} /></ActionIcon> : null}
          </Group>
          {r.kind === 'choice' ? <TagsInput mt="xs" label="Options" description="Press Enter after each option." value={r.options} onChange={(v) => set(i, { options: v })} /> : null}
        </Paper>
      ))}
      <Group justify="space-between">
        <Button variant="subtle" leftSection={<IconPlus {...ICON} />} onClick={() => setRows([...rows, { question: '', kind: 'text', options: [], active: true }])}>Add question</Button>
        <Button loading={busy} onClick={save}>Save questions</Button>
      </Group>
    </Stack>
  );
}

function ExitsList() {
  const [status, setStatus] = useState('open');
  const q = useQuery(() => lifecycle.exits(status === 'all' ? undefined : status), [status]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [rec, setRec] = useState<{ employeeId: string | null; kind: ExitKind; lwd: string | null; voluntary: boolean; reasonCategory: string | null; reason: string } | null>(null);
  const [confirmRec, setConfirmRec] = useState(false);
  const rows = q.data?.exits ?? [];
  const save = async () => {
    if (!rec?.employeeId || !rec.lwd) return;
    try {
      const r = await lifecycle.recordExit({ employeeId: rec.employeeId, kind: rec.kind, lastWorkingDay: rec.lwd, voluntary: rec.voluntary, reasonCategory: rec.reasonCategory, reason: rec.reason || null });
      toast.ok('Exit recorded and clearance started.'); setRec(null); await q.reload(); setOpenId(r.id);
    } catch (e) { toast.err(e); }
  };
  return (
    <Stack gap="md">
      {status === 'open' ? (
        <StatStrip stats={[
          { key: 'p', label: 'Waiting for approval', value: rows.filter((r) => r.status === 'pending').length },
          { key: 'n', label: 'Serving notice', value: rows.filter((r) => r.status === 'accepted').length },
          { key: 'c', label: 'Clearance overdue', value: rows.filter((r) => r.status === 'accepted' && lwdOf(r) < todayNpt()).length, tone: rows.some((r) => r.status === 'accepted' && lwdOf(r) < todayNpt()) ? 'warn' : 'ok' },
        ]} />
      ) : null}
      <Group justify="space-between" wrap="wrap">
        <SegmentedControl value={status} onChange={setStatus} data={[{ value: 'open', label: 'In progress' }, { value: 'completed', label: 'Left' }, { value: 'all', label: 'All' }]} />
        <Button leftSection={<IconPlus {...ICON} />} onClick={() => setRec({ employeeId: null, kind: 'resignation', lwd: null, voluntary: true, reasonCategory: null, reason: '' })}>Record an exit</Button>
      </Group>
      <DataTable<ExitRow> rows={rows} rowKey={(r) => r.id} loading={q.loading} error={q.error} onRowClick={(r) => setOpenId(r.id)}
        emptyTitle={status === 'open' ? 'Nobody is leaving right now' : 'No exits recorded'} emptyHint="Resignations submitted by employees appear here, or record a termination, retirement or contract end."
        columns={[
          { key: 'name', header: 'Employee', render: (r) => <div><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{[r.emp_code, r.designation].filter(Boolean).join(', ')}</Text></div> },
          { key: 'kind', header: 'Type', render: (r) => <div><Text size="sm">{EXIT_KIND_LABEL[r.kind]}</Text><Text size="xs" c="dimmed">{r.reason_category ?? (r.voluntary ? 'Voluntary' : 'Involuntary')}</Text></div> },
          { key: 'lwd', header: 'Last working day', render: (r) => <span className="num">{fmtAd(lwdOf(r))}</span> },
          { key: 'status', header: 'Status', render: (r) => exitStatusBadge(r.status) },
          { key: 'clear', header: 'Clearance', render: (r) => r.tasks_total ? progressCell(r.tasks_done, r.tasks_total) : <Text size="xs" c="dimmed">—</Text> },
          { key: 'assets', header: 'Assets held', align: 'right', render: (r) => <Text size="sm" c={r.assets_held ? 'orange' : 'dimmed'} className="num">{r.assets_held}</Text> },
        ]} />
      <ExitDrawer exitId={openId} onClose={() => setOpenId(null)} onChanged={() => { void q.reload(); }} />
      <Modal opened={!!rec} onClose={() => setRec(null)} title="Record an exit" centered size="lg">
        {rec ? (
          <Stack>
            <EmployeePicker value={rec.employeeId} onChange={(id) => setRec({ ...rec, employeeId: id })} required />
            <SimpleGrid cols={{ base: 1, sm: 2 }}>
              <Select label="Type" value={rec.kind} allowDeselect={false} onChange={(v) => v && setRec({ ...rec, kind: v as ExitKind, voluntary: v === 'resignation' || v === 'retirement' })}
                data={(Object.keys(EXIT_KIND_LABEL) as ExitKind[]).map((k) => ({ value: k, label: EXIT_KIND_LABEL[k] }))} />
              <DateField label="Last working day" value={rec.lwd} onChange={(v) => setRec({ ...rec, lwd: v })} required />
              <Select label="Main reason" clearable value={rec.reasonCategory} onChange={(v) => setRec({ ...rec, reasonCategory: v })} data={[...RESIGN_REASONS, 'Performance', 'Misconduct', 'Redundancy', 'Contract ended']} />
              <Switch label="Voluntary (the employee chose to leave)" mt={{ sm: 30 }} checked={rec.voluntary} onChange={(e) => setRec({ ...rec, voluntary: e.currentTarget.checked })} />
            </SimpleGrid>
            <Textarea label="Notes" value={rec.reason} onChange={(e) => setRec({ ...rec, reason: e.currentTarget.value })} autosize minRows={2} />
            <Group justify="flex-end"><Button variant="default" onClick={() => setRec(null)}>Cancel</Button><Button disabled={!rec.employeeId || !rec.lwd} onClick={() => setConfirmRec(true)}>Record exit</Button></Group>
          </Stack>
        ) : null}
      </Modal>
      <ConfirmDialog opened={confirmRec} onClose={() => setConfirmRec(false)} title="Record this exit?" confirmLabel="Record exit"
        message={`The employee's exit date becomes ${rec?.lwd ? fmtAd(rec.lwd) : 'the last working day'}. They can sign in until then, and the exit clearance checklist starts now.`} onConfirm={save} />
    </Stack>
  );
}

export default function ExitsPage() {
  const [tab, setTab] = useState('exits');
  return (
    <Stack gap="md">
      <PageHeader title="Exits" subtitle="Resignations and other exits: approval, notice, clearance by each department, the exit interview and the final settlement." />
      <SegmentedControl value={tab} onChange={setTab} style={{ alignSelf: 'flex-start' }}
        data={[{ value: 'exits', label: 'Exits' }, { value: 'templates', label: 'Clearance checklists' }, { value: 'questions', label: 'Interview questions' }]} />
      {tab === 'exits' ? <ExitsList /> : null}
      {tab === 'templates' ? <TemplatesPanel kind="offboarding" /> : null}
      {tab === 'questions' ? <QuestionsEditor /> : null}
    </Stack>
  );
}
