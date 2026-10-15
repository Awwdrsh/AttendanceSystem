import { Badge, Button, Group, Modal, NumberInput, Paper, Progress, SegmentedControl, Select, Stack, Switch, Text, TagsInput } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { lifecycle, type CaseSummary, type LifecyclePolicy, type ProbationRow } from '../../api/lifecycle';
import { DateField } from '../../components/BsDatePicker';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { StatStrip } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { addDays, fmtAd, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { AssetsPanel } from './AssetsPanel';
import { CaseDrawer } from './CaseDrawer';
import { ProbationReviewModal } from './ProbationReviewModal';
import { TemplatesPanel } from './TemplatesPanel';

const ICON = { size: 16, stroke: 1.6 } as const;

export function progressCell(done: number, total: number, overdue = 0) {
  return (
    <Stack gap={2} miw={120}>
      <Group gap={6} justify="space-between"><Text size="xs" c="dimmed">{done} of {total}</Text>{overdue ? <Text size="xs" c="red">{overdue} overdue</Text> : null}</Group>
      <Progress value={total ? (done / total) * 100 : 0} size="sm" radius="xl" {...(overdue ? { color: 'orange' } : {})} />
    </Stack>
  );
}

function Joiners() {
  const [show, setShow] = useState<'active' | 'completed'>('active');
  const q = useQuery(() => lifecycle.cases({ kind: 'onboarding', status: show }), [show]);
  const templates = useQuery(() => lifecycle.templates('onboarding'), []);
  const [openId, setOpenId] = useState<string | null>(null);
  const [start, setStart] = useState<{ employeeId: string | null; templateId: string | null; anchorDate: string | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = q.data?.cases ?? [];
  const today = todayNpt(); const soon = addDays(today, 30);
  const begin = async () => {
    if (!start?.employeeId) return;
    setBusy(true);
    try {
      const r = await lifecycle.startOnboarding({ employeeId: start.employeeId, templateId: start.templateId, ...(start.anchorDate ? { anchorDate: start.anchorDate } : {}) });
      toast.ok('Onboarding started.'); setStart(null); await q.reload(); setOpenId(r.id);
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack gap="md">
      {show === 'active' ? (
        <StatStrip stats={[
          { key: 'a', label: 'Onboarding now', value: rows.length },
          { key: 'u', label: 'Joining in the next 30 days', value: rows.filter((r) => r.anchor_date >= today && r.anchor_date <= soon).length },
          { key: 'o', label: 'Overdue tasks', value: rows.reduce((n, r) => n + r.overdue, 0), tone: rows.some((r) => r.overdue) ? 'warn' : 'ok' },
        ]} />
      ) : null}
      <Group justify="space-between" wrap="wrap">
        <SegmentedControl value={show} onChange={(v) => setShow(v as typeof show)} data={[{ value: 'active', label: 'In progress' }, { value: 'completed', label: 'Completed' }]} />
        <Button leftSection={<IconPlus {...ICON} />} onClick={() => setStart({ employeeId: null, templateId: null, anchorDate: null })}>Start onboarding</Button>
      </Group>
      <DataTable<CaseSummary> rows={rows} rowKey={(r) => r.id} loading={q.loading} error={q.error} onRowClick={(r) => setOpenId(r.id)}
        emptyTitle={show === 'active' ? 'Nobody is onboarding right now' : 'No completed onboardings yet'}
        emptyHint="Onboarding starts by itself when you add an employee, or start it here for someone already on the list."
        columns={[
          { key: 'name', header: 'Joiner', render: (r) => <div><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{[r.emp_code, r.designation].filter(Boolean).join(', ')}</Text></div> },
          { key: 'join', header: 'Joins', render: (r) => <span className="num">{fmtAd(r.anchor_date)}</span> },
          { key: 'tpl', header: 'Checklist', render: (r) => <Text size="sm" c="dimmed">{r.template_name ?? 'Custom'}</Text> },
          { key: 'progress', header: 'Progress', render: (r) => progressCell(r.done, r.total, r.overdue) },
          { key: 'next', header: 'Next due', render: (r) => <span className="num">{r.next_due ? fmtAd(r.next_due) : '—'}</span> },
        ]} />
      <CaseDrawer caseId={openId} onClose={() => setOpenId(null)} onChanged={() => { void q.reload(); }} />
      <Modal opened={!!start} onClose={() => setStart(null)} title="Start onboarding" centered>
        {start ? (
          <Stack>
            <EmployeePicker value={start.employeeId} onChange={(id) => setStart({ ...start, employeeId: id })} required />
            <Select label="Checklist" placeholder="Best match for their department and branch" clearable value={start.templateId} onChange={(v) => setStart({ ...start, templateId: v })}
              data={(templates.data?.templates ?? []).filter((t) => t.active).map((t) => ({ value: t.id, label: t.name }))} />
            <DateField label="Count due dates from" description="Leave empty to use the join date" value={start.anchorDate} onChange={(v) => setStart({ ...start, anchorDate: v })} />
            <Group justify="flex-end"><Button variant="default" onClick={() => setStart(null)}>Cancel</Button><Button loading={busy} disabled={!start.employeeId} onClick={begin}>Start</Button></Group>
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}

export function ProbationPanel() {
  const [within, setWithin] = useState('60');
  const q = useQuery(() => lifecycle.probation(Number(within)), [within]);
  const [review, setReview] = useState<ProbationRow | null>(null);
  const [confirm, setConfirm] = useState<{ row: ProbationRow; on: string | null } | null>(null);
  const doConfirm = async () => {
    if (!confirm?.on) return;
    try { await lifecycle.confirm(confirm.row.id, confirm.on); toast.ok(`${confirm.row.name_en} is confirmed.`); setConfirm(null); await q.reload(); } catch (e) { toast.err(e); }
  };
  return (
    <Stack gap="md">
      <SectionTitle hint="People whose probation ends soon. Managers are reminded before the end; their review comes to HR for approval."
        actions={<SegmentedControl size="xs" value={within} onChange={setWithin} data={[{ value: '30', label: '30 days' }, { value: '60', label: '60 days' }, { value: '180', label: '6 months' }]} />}>
        Probation ending
      </SectionTitle>
      <DataTable<ProbationRow> rows={q.data?.employees ?? []} rowKey={(r) => r.id} loading={q.loading} error={q.error}
        emptyTitle="No probation periods ending soon" emptyHint="Set a probation end date on an employee's profile to track it here."
        columns={[
          { key: 'name', header: 'Employee', render: (r) => <div><Text size="sm" fw={500}>{r.name_en}</Text><Text size="xs" c="dimmed">{[r.emp_code, r.designation].filter(Boolean).join(', ')}</Text></div> },
          { key: 'mgr', header: 'Manager', render: (r) => r.manager_name ?? '—' },
          { key: 'end', header: 'Probation ends', render: (r) => <div><Text size="sm" className="num">{fmtAd(r.probation_end)}</Text><Text size="xs" c={r.days_left < 0 ? 'red' : r.days_left <= 7 ? 'orange' : 'dimmed'}>{r.days_left < 0 ? `${-r.days_left} days ago` : r.days_left === 0 ? 'Today' : `In ${r.days_left} days`}</Text></div> },
          {
            key: 'review', header: 'Review', render: (r) => r.last_review
              ? <Badge variant="light" color={r.last_review.status === 'pending' ? 'yellow' : r.last_review.status === 'approved' ? 'teal' : 'gray'}>{r.last_review.status === 'pending' ? 'Waiting for HR' : r.last_review.status === 'approved' ? (r.last_review.recommendation === 'extend' ? 'Extended' : 'Approved') : 'Not approved'}</Badge>
              : <Text size="sm" c="dimmed">Not reviewed</Text>,
          },
          {
            key: 'act', header: '', align: 'right', render: (r) => (
              <Group gap={6} justify="flex-end" wrap="nowrap">
                <Button size="xs" variant="default" disabled={r.last_review?.status === 'pending'} onClick={() => setReview(r)}>Review</Button>
                <Button size="xs" variant="subtle" onClick={() => setConfirm({ row: r, on: r.probation_end })}>Confirm now</Button>
              </Group>
            ),
          },
        ]} />
      <ProbationReviewModal employee={review} onClose={() => setReview(null)} onDone={() => { void q.reload(); }} />
      <Modal opened={!!confirm} onClose={() => setConfirm(null)} title={confirm ? `Confirm ${confirm.row.name_en}` : ''} centered>
        {confirm ? (
          <Stack>
            <Text size="sm" c="dimmed">Records the confirmation directly, without a manager review.</Text>
            <DateField label="Confirmed from" value={confirm.on} onChange={(v) => setConfirm({ ...confirm, on: v })} required />
            <Group justify="flex-end"><Button variant="default" onClick={() => setConfirm(null)}>Cancel</Button><Button disabled={!confirm.on} onClick={doConfirm}>Confirm employment</Button></Group>
          </Stack>
        ) : null}
      </Modal>
    </Stack>
  );
}

function LifecycleSettings() {
  const q = useQuery(() => lifecycle.policy(), []);
  const [p, setP] = useState<LifecyclePolicy | null>(null); const [busy, setBusy] = useState(false);
  useEffect(() => { if (q.data) setP(q.data.policy); }, [q.data]);
  if (!p) return null;
  const save = async () => { setBusy(true); try { await lifecycle.savePolicy(p); toast.ok('Settings saved.'); } catch (e) { toast.err(e); } finally { setBusy(false); } };
  return (
    <Paper withBorder p="lg" maw={640}>
      <Stack gap="md">
        <Switch label="Start onboarding when an employee is added" description="Uses the best matching onboarding checklist." checked={p.autoStartOnboarding} onChange={(e) => setP({ ...p, autoStartOnboarding: e.currentTarget.checked })} />
        <NumberInput label="Resignation notice (calendar days)" description="Shown to employees when they resign; shorter notice is flagged to approvers." value={p.noticeDays} min={0} max={180} onChange={(v) => setP({ ...p, noticeDays: Number(v) || 0 })} maw={320} />
        <TagsInput label="Probation reminders (days before the end)" description="Press Enter after each number." value={p.probationReminderDays.map(String)}
          onChange={(v) => setP({ ...p, probationReminderDays: v.map(Number).filter((n) => Number.isInteger(n) && n > 0 && n <= 90).slice(0, 4) })} maw={360} />
        <Switch label="Leavers fill in the exit interview themselves" description="Turn off if HR holds every exit interview in person." checked={p.selfExitInterview} onChange={(e) => setP({ ...p, selfExitInterview: e.currentTarget.checked })} />
        <Group justify="flex-end"><Button loading={busy} onClick={save}>Save settings</Button></Group>
      </Stack>
    </Paper>
  );
}

export default function OnboardingPage() {
  const [tab, setTab] = useState('joiners');
  return (
    <Stack gap="md">
      <PageHeader title="Onboarding" subtitle="Getting new joiners ready: checklists for HR, managers and the joiner, probation reviews and the equipment they receive." />
      <SegmentedControl value={tab} onChange={setTab} style={{ alignSelf: 'flex-start', maxWidth: '100%', overflowX: 'auto' }}
        data={[{ value: 'joiners', label: 'Joiners' }, { value: 'probation', label: 'Probation' }, { value: 'templates', label: 'Checklists' }, { value: 'assets', label: 'Assets' }, { value: 'settings', label: 'Settings' }]} />
      {tab === 'joiners' ? <Joiners /> : null}
      {tab === 'probation' ? <ProbationPanel /> : null}
      {tab === 'templates' ? <TemplatesPanel kind="onboarding" /> : null}
      {tab === 'assets' ? <AssetsPanel /> : null}
      {tab === 'settings' ? <LifecycleSettings /> : null}
    </Stack>
  );
}
