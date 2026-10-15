import { Alert, Badge, Button, Checkbox, Divider, Drawer, Group, MultiSelect, NumberInput, Paper, SimpleGrid, Stack, Text, Textarea } from '@mantine/core';
import { IconCheck, IconPrinter, IconX } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { learning as lrnApi, performance as perfApi, type Recommendation, type ReviewDetail } from '../../api/performance';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { fmtDateShort } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { ColleaguePicker, ICON, PhaseBadge, RatingPicker, ratingLabel } from './parts';

type Draft = Record<string, { rating: number | null; comment: string }>;

const TAG_COLOR = { positive: 'teal', improvement: 'orange', neutral: 'gray' } as const;

/**
 * One review, shaped by who is looking: the employee fills in the self review, nominates peers and acknowledges;
 * the manager rates, sees the journal and peer feedback and recommends; HR also calibrates.
 */
export function ReviewForm({ reviewId, onClose, onChanged }: { reviewId: string | null; onClose: () => void; onChanged: () => void }) {
  const q = useQuery(() => (reviewId ? perfApi.review(reviewId) : Promise.resolve(null)), [reviewId]);
  const d = q.data;
  return (
    <Drawer opened={!!reviewId} onClose={onClose} position="right" size="xl" title={d ? `${d.cycle.name}: ${d.review.name_en}` : 'Review'}>
      {!d ? <Text c="dimmed" size="sm">{q.loading ? 'Loading…' : q.error ?? ''}</Text> : <ReviewBody key={d.review.id} d={d} reload={async () => { await q.reload(); onChanged(); }} />}
    </Drawer>
  );
}

function ReviewBody({ d, reload }: { d: ReviewDetail; reload: () => Promise<void> }) {
  const side = d.role === 'self' ? 'self' : 'manager';
  const editable = side === 'self' ? d.can.editSelf : d.can.editManager;
  const labels = d.cycle.rating_labels;
  const [draft, setDraft] = useState<Draft>({});
  const [summary, setSummary] = useState('');
  const [rec, setRec] = useState<Recommendation>({});
  const [busy, setBusy] = useState(false);
  const [confirmSubmit, setConfirmSubmit] = useState(false);
  const [peers, setPeers] = useState<string[]>([]);
  const [ackComment, setAckComment] = useState('');
  const [final, setFinal] = useState<number | ''>(''); const [reason, setReason] = useState('');
  const courses = useQuery(() => (side === 'manager' ? lrnApi.courses() : Promise.resolve({ courses: [] })), [side]);

  useEffect(() => {
    const init: Draft = {};
    for (const i of d.items) init[i.id] = side === 'self' ? { rating: i.self_rating, comment: i.self_comment ?? '' } : { rating: i.manager_rating, comment: i.manager_comment ?? '' };
    setDraft(init);
    setSummary((side === 'self' ? d.review.self_summary : d.review.manager_summary) ?? '');
    setRec(d.review.recommendation ?? {});
    setFinal(d.review.final_rating ? Number(d.review.final_rating) : d.review.manager_score ? Number(d.review.manager_score) : '');
  }, [d, side]);

  const body = (submit: boolean) => ({
    items: d.items.map((i) => ({ id: i.id, rating: draft[i.id]?.rating ?? null, comment: draft[i.id]?.comment || null })),
    summary: summary || null, submit, ...(side === 'manager' ? { recommendation: { promotion: false, pip: false, trainingCourseIds: [], ...rec } } : {}),
  });
  const save = async (submit: boolean) => {
    setBusy(true);
    try {
      if (side === 'self') await perfApi.saveSelf(d.review.id, body(submit)); else await perfApi.saveManager(d.review.id, body(submit));
      toast.ok(submit ? 'Review submitted.' : 'Draft saved.'); setConfirmSubmit(false); await reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  const act = async (fn: () => Promise<unknown>, msg: string) => { setBusy(true); try { await fn(); toast.ok(msg); await reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } };
  const unrated = d.items.filter((i) => !draft[i.id]?.rating).length;
  const released = !!d.review.released_at;
  const canPrint = d.role !== 'self' || released;
  const deadline = side === 'self' ? d.cycle.self_due : d.cycle.manager_due;

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="wrap">
        <Group gap="xs">
          <PhaseBadge phase={d.cycle.status} />
          <Text size="sm" c="dimmed">{fmtDateShort(d.cycle.period_start)} to {fmtDateShort(d.cycle.period_end)}{d.review.manager_name ? ` · Manager ${d.review.manager_name}` : ''}</Text>
        </Group>
        {canPrint ? <Button size="xs" variant="default" leftSection={<IconPrinter {...ICON} />} onClick={() => window.open(perfApi.printUrl(d.review.id), '_blank', 'noopener')}>Print</Button> : null}
      </Group>
      {editable && deadline ? <Alert variant="light" color="blue">Due {fmtDateShort(deadline)}. Save a draft any time; submitting locks your answers.</Alert> : null}
      {side === 'self' && d.review.self_submitted_at && !released ? <Alert variant="light" color="teal">Submitted. You will see your manager's review when HR releases the results.</Alert> : null}
      {side === 'manager' && d.cycle.status === 'self_review' && !d.review.self_submitted_at ? <Alert variant="light" color="yellow">Waiting for the self review. You can start once it is submitted or the manager review phase opens.</Alert> : null}

      {released ? (
        <Paper withBorder radius="lg" p="md">
          <Group justify="space-between" wrap="wrap">
            <div><Text size="sm" c="dimmed">Final rating</Text><Text fw={700} size="xl" c="brand.7">{ratingLabel(d.review.final_rating, labels)}</Text></div>
            {d.review.self_score ? <div><Text size="sm" c="dimmed">Your self score</Text><Text fw={600}>{ratingLabel(d.review.self_score, labels)}</Text></div> : null}
          </Group>
        </Paper>
      ) : d.role !== 'self' ? (
        <SimpleGrid cols={3}>
          <div><Text size="xs" c="dimmed">Self score</Text><Text fw={600} size="sm">{ratingLabel(d.review.self_score, labels)}</Text></div>
          <div><Text size="xs" c="dimmed">Manager score</Text><Text fw={600} size="sm">{ratingLabel(d.review.manager_score, labels)}</Text></div>
          <div><Text size="xs" c="dimmed">Final rating</Text><Text fw={600} size="sm">{ratingLabel(d.review.final_rating, labels)}</Text></div>
        </SimpleGrid>
      ) : null}

      {(['goal', 'competency'] as const).map((type) => {
        const items = d.items.filter((i) => i.item_type === type);
        return (
          <Stack key={type} gap="xs">
            <Text fw={600}>{type === 'goal' ? `Goals (${Number(d.cycle.goals_weight)}%)` : `Competencies (${Number(d.cycle.competency_weight)}%)`}</Text>
            {items.length === 0 ? <Text size="sm" c="dimmed">{type === 'goal' ? 'No goals for this period. Goals active during the period are added at launch.' : 'No competencies apply to this role.'}</Text> : items.map((i) => (
              <Paper key={i.id} withBorder radius="md" p="sm">
                <Stack gap={6}>
                  <Group justify="space-between" wrap="nowrap" align="flex-start">
                    <div style={{ minWidth: 0 }}>
                      <Text size="sm" fw={500}>{i.title}</Text>
                      {i.detail ? <Text size="xs" c="dimmed" lineClamp={2}>{i.detail}</Text> : null}
                    </div>
                    {type === 'goal' ? <Badge variant="light" color="gray" style={{ flex: 'none' }}>Weight {Number(i.weight)}</Badge> : null}
                  </Group>
                  {side === 'manager' || released ? (
                    <Group gap="lg" wrap="wrap">
                      <Text size="xs" c="dimmed">Self: {ratingLabel(i.self_rating, labels)}</Text>
                      {side === 'self' ? <Text size="xs" c="dimmed">Manager: {ratingLabel(i.manager_rating, labels)}</Text> : null}
                    </Group>
                  ) : null}
                  {side === 'manager' && i.self_comment ? <Text size="xs" c="dimmed">“{i.self_comment}”</Text> : null}
                  {side === 'self' && released && i.manager_comment ? <Text size="sm">Manager: {i.manager_comment}</Text> : null}
                  {editable ? (
                    <>
                      <RatingPicker labels={labels} value={draft[i.id]?.rating ?? null} onChange={(v) => setDraft((x) => ({ ...x, [i.id]: { rating: v, comment: x[i.id]?.comment ?? '' } }))} />
                      <Textarea size="xs" autosize minRows={1} placeholder="Evidence and examples" value={draft[i.id]?.comment ?? ''}
                        onChange={(e) => { const v = e.currentTarget.value; setDraft((x) => ({ ...x, [i.id]: { rating: x[i.id]?.rating ?? null, comment: v } })); }} />
                    </>
                  ) : side === 'manager' ? <Text size="xs">Manager: {ratingLabel(i.manager_rating, labels)}{i.manager_comment ? ` · ${i.manager_comment}` : ''}</Text>
                    : !released ? <Text size="xs">Your rating: {ratingLabel(i.self_rating, labels)}{i.self_comment ? ` · ${i.self_comment}` : ''}</Text> : null}
                </Stack>
              </Paper>
            ))}
          </Stack>
        );
      })}

      <Stack gap="xs">
        <Text fw={600}>{side === 'self' ? 'Your summary' : "Manager's summary"}</Text>
        {editable ? <Textarea autosize minRows={3} placeholder={side === 'self' ? 'Highlights, challenges and what you want to grow in' : 'Overall assessment and next steps'} value={summary} onChange={(e) => setSummary(e.currentTarget.value)} />
          : <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{(side === 'self' ? d.review.self_summary : d.review.manager_summary) || '—'}</Text>}
        {side === 'manager' && d.review.self_summary ? <><Text size="sm" c="dimmed">Employee's summary</Text><Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{d.review.self_summary}</Text></> : null}
        {side === 'self' && released && d.review.manager_summary ? <><Text size="sm" c="dimmed">Manager's summary</Text><Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{d.review.manager_summary}</Text></> : null}
      </Stack>

      {side === 'manager' ? (
        <Stack gap="xs">
          <Text fw={600}>Recommendation</Text>
          <SimpleGrid cols={{ base: 1, sm: 3 }}>
            <NumberInput label="Increment (%)" min={0} max={100} disabled={!editable} value={rec.incrementPct ?? ''} onChange={(v) => setRec((r) => ({ ...r, incrementPct: v === '' ? null : Number(v) }))} />
            <Checkbox mt={28} label="Promotion" disabled={!editable} checked={!!rec.promotion} onChange={(e) => { const c = e.currentTarget.checked; setRec((r) => ({ ...r, promotion: c })); }} />
            <Checkbox mt={28} label="Improvement plan" disabled={!editable} checked={!!rec.pip} onChange={(e) => { const c = e.currentTarget.checked; setRec((r) => ({ ...r, pip: c })); }} />
          </SimpleGrid>
          <MultiSelect label="Training needs" description="Enrolled automatically when the results are released" disabled={!editable} searchable value={rec.trainingCourseIds ?? []}
            onChange={(v) => setRec((r) => ({ ...r, trainingCourseIds: v }))} data={(courses.data?.courses ?? []).map((c) => ({ value: c.id, label: c.title }))} maxValues={10} />
          <Textarea label="Notes" autosize minRows={2} disabled={!editable} value={rec.notes ?? ''} onChange={(e) => { const v = e.currentTarget.value; setRec((r) => ({ ...r, notes: v })); }} />
        </Stack>
      ) : null}

      {editable ? (
        <Group justify="flex-end">
          <Button variant="default" onClick={() => save(false)} loading={busy}>Save draft</Button>
          <Button onClick={() => setConfirmSubmit(true)} disabled={unrated > 0} loading={busy}>{unrated > 0 ? `Rate ${unrated} more` : 'Submit review'}</Button>
        </Group>
      ) : null}

      {d.cycle.peer_enabled ? (
        <>
          <Divider />
          <Stack gap="xs">
            <Text fw={600}>Peer feedback</Text>
            {d.peers.nominations.length === 0 ? <Text size="sm" c="dimmed">No peer reviewers yet.</Text> : d.peers.nominations.map((p) => (
              <Group key={p.id} justify="space-between" wrap="nowrap">
                <div>
                  <Text size="sm">{p.reviewer_name}</Text>
                  {d.role !== 'self' && p.status === 'submitted' ? <Text size="xs" c="dimmed">{ratingLabel(p.rating ?? null, labels)}{p.strengths ? ` · Strengths: ${p.strengths}` : ''}{p.improvements ? ` · To improve: ${p.improvements}` : ''}</Text> : null}
                </div>
                <Group gap={4} wrap="nowrap">
                  <Badge size="sm" variant="light" color={p.status === 'nominated' ? 'yellow' : p.status === 'declined' ? 'gray' : 'teal'}>{p.status === 'nominated' ? 'Awaiting approval' : p.status === 'approved' ? 'Asked' : p.status}</Badge>
                  {d.role !== 'self' && p.status === 'nominated' ? <>
                    <Button size="compact-xs" variant="light" leftSection={<IconCheck size={12} />} onClick={() => act(() => perfApi.decidePeer(p.id, true), 'Approved.')}>Approve</Button>
                    <Button size="compact-xs" variant="subtle" color="gray" leftSection={<IconX size={12} />} onClick={() => act(() => perfApi.decidePeer(p.id, false), 'Declined.')}>Decline</Button>
                  </> : null}
                  {p.status === 'nominated' || (d.role !== 'self' && p.status === 'approved') ? <Button size="compact-xs" variant="subtle" color="red" onClick={() => act(() => perfApi.removePeer(p.id), 'Removed.')}>Remove</Button> : null}
                </Group>
              </Group>
            ))}
            {d.can.nominatePeers ? (
              <Group align="flex-end" wrap="nowrap">
                <div style={{ flex: 1 }}><ColleaguePicker label={d.role === 'self' ? 'Suggest colleagues to review you' : 'Add peer reviewers'} value={peers} onChange={setPeers} exclude={[d.review.employee_id]} max={10} /></div>
                <Button disabled={!peers.length} loading={busy} onClick={() => act(async () => { await perfApi.nominatePeers(d.review.id, peers); setPeers([]); }, d.role === 'self' ? 'Sent to your manager for approval.' : 'Peer reviewers added.')}>Add</Button>
              </Group>
            ) : null}
            {d.role === 'self' && released && d.peers.aggregate ? (
              d.peers.aggregate.shown ? (
                <Paper withBorder radius="md" p="sm">
                  <Text size="sm" fw={500}>From {d.peers.aggregate.count} colleagues (anonymous): {ratingLabel(d.peers.aggregate.averageRating ?? null, labels)}</Text>
                  {d.peers.aggregate.strengths?.map((s, i) => <Text key={`s${i}`} size="sm">+ {s}</Text>)}
                  {d.peers.aggregate.improvements?.map((s, i) => <Text key={`i${i}`} size="sm" c="dimmed">→ {s}</Text>)}
                </Paper>
              ) : <Text size="sm" c="dimmed">Fewer than {d.cycle.peer_min} colleagues responded, so peer feedback is not shown to keep it anonymous.</Text>
            ) : null}
          </Stack>
        </>
      ) : null}

      {d.role !== 'self' ? (
        <>
          <Divider />
          <Stack gap="xs">
            <Text fw={600}>Journal for this period</Text>
            <Text size="xs" c="dimmed">{d.role === 'hr' ? 'Notes from every manager.' : 'Your private notes. Only you and HR see them.'}</Text>
            {d.journal.length === 0 ? <Text size="sm" c="dimmed">No notes.</Text> : d.journal.map((j) => (
              <Paper key={j.id} withBorder radius="md" p="xs">
                <Group gap="xs"><Badge size="sm" variant="light" color={TAG_COLOR[j.tag]}>{j.tag}</Badge><Text size="xs" c="dimmed">{fmtDateShort(j.note_date)} · {j.author_name}</Text></Group>
                <Text size="sm" mt={4} style={{ whiteSpace: 'pre-wrap' }}>{j.note}</Text>
              </Paper>
            ))}
          </Stack>
        </>
      ) : null}

      {d.can.calibrate ? (
        <>
          <Divider />
          <Stack gap="xs">
            <Text fw={600}>Calibrate</Text>
            {d.review.calibration_reason ? <Text size="xs" c="dimmed">Last note: {d.review.calibration_reason}</Text> : null}
            <SimpleGrid cols={{ base: 1, sm: 3 }}>
              <NumberInput label="Final rating" min={1} max={5} step={0.25} decimalScale={2} value={final} onChange={(v) => setFinal(v === '' ? '' : Number(v))} />
              <Textarea style={{ gridColumn: 'span 2' }} label="Reason" placeholder="Why the final rating differs" autosize value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
            </SimpleGrid>
            <Group justify="flex-end"><Button disabled={final === '' || reason.trim().length < 5} loading={busy} onClick={() => act(() => perfApi.calibrate(d.review.id, Number(final), reason), 'Final rating set.')}>Set final rating</Button></Group>
          </Stack>
        </>
      ) : null}

      {d.role === 'self' && released ? (
        <>
          <Divider />
          {d.review.acknowledged_at ? <Text size="sm" c="dimmed">You acknowledged this review on {fmtDateShort(d.review.acknowledged_at.slice(0, 10))}.{d.review.employee_comment ? ` “${d.review.employee_comment}”` : ''}</Text> : (
            <Stack gap="xs">
              <Text fw={600}>Acknowledge</Text>
              <Textarea placeholder="Optional comment for your manager and HR" autosize minRows={2} value={ackComment} onChange={(e) => setAckComment(e.currentTarget.value)} />
              <Group justify="flex-end"><Button loading={busy} onClick={() => act(() => perfApi.acknowledge(d.review.id, ackComment || undefined), 'Thanks, acknowledged.')}>I have read my review</Button></Group>
            </Stack>
          )}
        </>
      ) : null}

      <ConfirmDialog opened={confirmSubmit} title="Submit review?" color="brand" confirmLabel="Submit"
        message="You cannot change your ratings after submitting." onConfirm={() => save(true)} onClose={() => setConfirmSubmit(false)} />
    </Stack>
  );
}
