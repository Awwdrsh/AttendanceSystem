import { Anchor, Badge, Button, Group, Paper, Stack, Text, Textarea } from '@mantine/core';
import { useState } from 'react';
import { documents } from '../../api/documents';
import { CASE_CATEGORY_LABEL, relations, type DisciplinaryCase } from '../../api/relations';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { fmtAd, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { CaseStatusBadge } from './DisciplinaryPage';

function CaseCard({ k, onChanged }: { k: DisciplinaryCase; onChanged: () => void }) {
  const [reply, setReply] = useState(''); const [busy, setBusy] = useState(false);
  const late = !!k.responseDue && k.responseDue < todayNpt();
  return (
    <Paper withBorder p="md">
      <Stack gap="sm">
        <Group justify="space-between"><Text fw={600}>Case D-{k.number}, {CASE_CATEGORY_LABEL[k.category].toLowerCase()}</Text><CaseStatusBadge status={k.status} /></Group>
        {k.showCause ? <div><Text size="xs" c="dimmed">Notice from HR</Text><Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{k.showCause}</Text></div> : null}
        {k.response ? <div><Text size="xs" c="dimmed">Your response</Text><Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{k.response}</Text></div> : null}
        {k.status === 'show_cause' ? (
          <Stack gap="xs">
            <Text size="sm" {...(late ? { c: 'red' } : {})}>Please respond by <b>{fmtAd(k.responseDue)}</b>{late ? '. The deadline has passed, but you can still reply.' : '.'}</Text>
            <Textarea placeholder="Explain what happened in your own words" value={reply} onChange={(e) => setReply(e.currentTarget.value)} autosize minRows={4} maxLength={8000} />
            <Group justify="flex-end"><Button loading={busy} disabled={!reply.trim()} onClick={async () => {
              setBusy(true);
              try { await relations.respond(k.id, reply.trim()); toast.ok('Your response was sent to HR.'); onChanged(); } catch (e) { toast.err(e); } finally { setBusy(false); }
            }}>Send response</Button></Group>
          </Stack>
        ) : null}
        {k.status === 'decided' ? (
          <div>
            <Group gap="xs"><Text size="xs" c="dimmed">Outcome</Text>{k.stageName ? <Badge variant="light">{k.stageName}</Badge> : <Badge variant="light" color="gray">No action</Badge>}</Group>
            {k.suspensionFrom ? <Text size="sm">Suspended from {fmtAd(k.suspensionFrom)} to {fmtAd(k.suspensionTo)}.</Text> : null}
            <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{k.decisionNote}</Text>
            {k.letterId ? <Anchor size="sm" href={documents.letterUrl(k.letterId)} target="_blank" rel="noopener">Open the letter ({k.letterRef})</Anchor> : null}
          </div>
        ) : null}
      </Stack>
    </Paper>
  );
}

/** The employee's side of disciplinary cases: notices to answer and outcomes. Investigations still with HR do not show. */
export default function MyConductPage() {
  const q = useQuery(() => relations.myCases(), []);
  const cases = q.data?.cases ?? [];
  return (
    <Stack gap="lg">
      <PageHeader title="Conduct" subtitle="Notices from HR that need your response, and the outcome of any disciplinary case. If you think a case is unfair, you can raise a grievance." />
      {q.error ? <EmptyState title="Could not load" hint={q.error} /> : null}
      {!q.loading && !q.error && !cases.length ? <EmptyState title="Nothing here" hint="You have no disciplinary notices or cases." /> : null}
      {cases.map((k) => <CaseCard key={k.id} k={k} onChanged={() => { void q.reload(); }} />)}
    </Stack>
  );
}
