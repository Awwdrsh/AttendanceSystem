import { Badge, Button, Group, Paper, SimpleGrid, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { EXIT_KIND_LABEL, lifecycle } from '../../api/lifecycle';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { Field } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { fmtAd } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { CaseDrawer } from './CaseDrawer';
import { progressCell } from './OnboardingPage';
import { ProbationReviewModal } from './ProbationReviewModal';

const REC: Record<string, string> = { confirm: 'Confirm', extend: 'Extend', terminate: 'End employment' };

/** Employee page tab: probation and confirmation, onboarding and exit checklists, exits, and assets held. */
export function LifecycleSection({ employeeId }: { employeeId: string }) {
  const { hasRole } = useAuth();
  const q = useQuery(() => lifecycle.employeeSummary(employeeId), [employeeId]);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [review, setReview] = useState(false);
  const d = q.data;
  if (q.error) return <Text size="sm" c="dimmed">{q.error}</Text>;
  if (!d) return null;
  const hr = hasRole('hr');
  const onboarding = d.cases.find((c) => c.kind === 'onboarding');
  const start = async () => { try { const r = await lifecycle.startOnboarding({ employeeId }); toast.ok('Onboarding started.'); await q.reload(); setCaseId(r.id); } catch (e) { toast.err(e); } };
  const pendingReview = d.probation.reviews.some((r) => r.status === 'pending');
  return (
    <Stack gap="lg">
      <Paper withBorder p="lg">
        <SectionTitle actions={!d.probation.confirmedOn && d.employee.status === 'active' ? <Button size="xs" variant="default" disabled={pendingReview} onClick={() => setReview(true)}>{pendingReview ? 'Review waiting for HR' : 'Probation review'}</Button> : null}>Probation</SectionTitle>
        <SimpleGrid cols={{ base: 2, sm: 3 }}>
          <Field label="Joined">{fmtAd(d.employee.join_date)}</Field>
          <Field label="Probation ends">{fmtAd(d.probation.end)}</Field>
          <Field label="Confirmed">{d.probation.confirmedOn ? fmtAd(d.probation.confirmedOn) : 'Not yet'}</Field>
        </SimpleGrid>
        {d.probation.reviews.length ? (
          <Stack gap={4} mt="md">
            {d.probation.reviews.map((r) => (
              <Group key={r.id} gap="xs"><Badge size="sm" variant="light" color={r.status === 'approved' ? 'teal' : r.status === 'pending' ? 'yellow' : 'gray'}>{r.status}</Badge>
                <Text size="sm">{REC[r.recommendation]}{r.extend_to ? ` to ${fmtAd(r.extend_to)}` : ''}, rated {r.rating} of 5</Text><Text size="xs" c="dimmed">by {r.submitted_by_name}, {fmtAd(r.created_at.slice(0, 10))}</Text></Group>
            ))}
          </Stack>
        ) : null}
      </Paper>

      <div>
        <SectionTitle actions={hr && !onboarding && d.employee.status === 'active' ? <Button size="xs" variant="default" onClick={start}>Start onboarding</Button> : null}>Checklists</SectionTitle>
        <DataTable rows={d.cases} rowKey={(c) => c.id} onRowClick={(c) => setCaseId(c.id)} emptyTitle="No onboarding or exit checklist" emptyHint={hr ? 'Start onboarding to give HR, the manager and the joiner their tasks.' : 'HR starts these.'}
          columns={[
            { key: 'k', header: 'Checklist', render: (c) => (c.kind === 'onboarding' ? 'Onboarding' : 'Exit clearance') },
            { key: 'd', header: 'From', render: (c) => <span className="num">{fmtAd(c.anchor_date)}</span> },
            { key: 'p', header: 'Progress', render: (c) => progressCell(c.done, c.total) },
            { key: 's', header: 'Status', render: (c) => <Badge variant="light" color={c.status === 'active' ? 'blue' : c.status === 'completed' ? 'teal' : 'gray'}>{c.status === 'active' ? 'In progress' : c.status}</Badge> },
          ]} />
      </div>

      <div>
        <SectionTitle actions={hr ? <Button component={Link} to="/employees/exits" size="xs" variant="subtle">Exits</Button> : null}>Exits</SectionTitle>
        {d.exits.length ? (
          <Stack gap={4}>{d.exits.map((x) => <Group key={x.id} gap="xs"><Badge size="sm" variant="light" color={['pending', 'accepted'].includes(x.status) ? 'orange' : 'gray'}>{x.status}</Badge><Text size="sm">{EXIT_KIND_LABEL[x.kind]}, last working day {fmtAd(x.accepted_lwd ?? x.proposed_lwd)}</Text>{x.reason_category ? <Text size="xs" c="dimmed">{x.reason_category}</Text> : null}</Group>)}</Stack>
        ) : <Text size="sm" c="dimmed">No resignation or exit recorded.</Text>}
      </div>

      <div>
        <SectionTitle actions={hr ? <Button component={Link} to="/employees/onboarding" size="xs" variant="subtle">Asset register</Button> : null}>Assets</SectionTitle>
        <DataTable rows={d.assets} rowKey={(a) => a.assignment_id} emptyTitle="No company assets handed over"
          columns={[
            { key: 'n', header: 'Asset', render: (a) => <div><Text size="sm">{a.name}</Text><Text size="xs" c="dimmed" ff="monospace">{a.tag}</Text></div> },
            { key: 't', header: 'Type', render: (a) => a.type_name ?? '—' },
            { key: 'o', header: 'Handed over', render: (a) => <span className="num">{fmtAd(a.assigned_on)}</span> },
            { key: 'r', header: 'Returned', render: (a) => a.returned_on ? <span className="num">{fmtAd(a.returned_on)}</span> : <Badge size="sm" variant="light" color="blue">With them</Badge> },
          ]} />
      </div>
      <CaseDrawer caseId={caseId} onClose={() => setCaseId(null)} onChanged={() => { void q.reload(); }} />
      <ProbationReviewModal employee={review ? { id: d.employee.id, name_en: d.employee.name_en, probation_end: d.employee.probation_end } : null} onClose={() => setReview(false)} onDone={() => { void q.reload(); }} />
    </Stack>
  );
}
