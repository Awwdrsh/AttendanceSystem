import { Badge, Button, Group, Modal, Paper, SimpleGrid, Stack, Text, Textarea } from '@mantine/core';
import { IconClipboardCheck } from '@tabler/icons-react';
import { useState } from 'react';
import { performance as perfApi } from '../../api/performance';
import { EmptyState } from '../../components/EmptyState';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDateShort } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { PhaseBadge, RatingPicker, ratingLabel } from './parts';
import { ReviewForm } from './ReviewForm';

function nextStep(r) {
  if (r.released_at) return r.acknowledged_at ? { label: 'Acknowledged', color: 'gray' } : { label: 'Results ready', color: 'brand' };
  if (!r.self_submitted_at && (r.cycle_status === 'self_review' || r.cycle_status === 'manager_review'))
    return { label: 'Self review to do', color: 'blue' };
  return { label: 'With your manager', color: 'gray' };
}

/** My reviews in each cycle, and peer feedback colleagues asked me for. */
export default function MyReviewsPage() {
  const q = useQuery(() => perfApi.reviews('mine'), []);
  const peers = useQuery(() => perfApi.peerRequests(), []);
  const [open, setOpen] = useState(null);
  const [peer, setPeer] = useState(null);
  const reviews = q.data?.reviews ?? [];
  const pending = (peers.data?.requests ?? []).filter((p) => p.status === 'approved');

  return (
    <Stack gap="lg">
      {pending.length ? (
        <Stack gap="xs">
          <SectionTitle hint="Your answers are combined anonymously with others before the colleague sees them.">
            Peer feedback requested
          </SectionTitle>
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            {pending.map((p) => (
              <Paper key={p.id} withBorder radius="lg" p="md">
                <Group justify="space-between" wrap="nowrap">
                  <div>
                    <Text fw={600} size="sm">
                      {p.employee_name}
                    </Text>
                    <Text size="xs" c="dimmed">
                      {p.cycle_name}
                      {p.peer_due ? ` · due ${fmtDateShort(p.peer_due)}` : ''}
                    </Text>
                  </div>
                  <Button size="xs" onClick={() => setPeer(p)}>
                    Give feedback
                  </Button>
                </Group>
              </Paper>
            ))}
          </SimpleGrid>
        </Stack>
      ) : null}
      <SectionTitle hint="Rate yourself against your goals and competencies, then read your manager's review when it is released.">
        Performance reviews
      </SectionTitle>
      {!q.loading && reviews.length === 0 ? (
        <EmptyState
          icon={<IconClipboardCheck size={20} stroke={1.6} />}
          title="No reviews yet"
          hint="When HR opens a review cycle, your self review appears here."
        />
      ) : (
        <Stack gap="sm">
          {reviews.map((r) => {
            const s = nextStep(r);
            return (
              <Paper key={r.id} withBorder radius="lg" p="md" style={{ cursor: 'pointer' }} onClick={() => setOpen(r.id)}>
                <Group justify="space-between" wrap="wrap" gap="xs">
                  <div>
                    <Group gap="xs">
                      <Text fw={600}>{r.cycle_name}</Text>
                      <PhaseBadge phase={r.cycle_status} />
                    </Group>
                    <Text size="xs" c="dimmed">
                      {fmtDateShort(r.period_start)} to {fmtDateShort(r.period_end)}
                      {!r.self_submitted_at && r.self_due ? ` · self review due ${fmtDateShort(r.self_due)}` : ''}
                    </Text>
                  </div>
                  <Group gap="xs">
                    {r.final_rating ? (
                      <Text size="sm" fw={600}>
                        {ratingLabel(r.final_rating, r.rating_labels)}
                      </Text>
                    ) : null}
                    <Badge variant="light" color={s.color}>
                      {s.label}
                    </Badge>
                  </Group>
                </Group>
              </Paper>
            );
          })}
        </Stack>
      )}
      <ReviewForm reviewId={open} onClose={() => setOpen(null)} onChanged={() => void q.reload()} />
      <PeerModal req={peer} onClose={() => setPeer(null)} onDone={() => void peers.reload()} />
    </Stack>
  );
}

function PeerModal({ req, onClose, onDone }) {
  const [rating, setRating] = useState(null);
  const [strengths, setStrengths] = useState('');
  const [improvements, setImprovements] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!req || !rating) return;
    setBusy(true);
    try {
      await perfApi.submitPeer(req.id, { rating, strengths: strengths || null, improvements: improvements || null });
      toast.ok('Thanks, feedback sent.');
      setRating(null);
      setStrengths('');
      setImprovements('');
      onDone();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal opened={!!req} onClose={onClose} title={req ? `Feedback for ${req.employee_name}` : ''}>
      {req ? (
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            {req.cycle_name}. Shared anonymously, combined with other colleagues' answers.
          </Text>
          <Text size="sm" fw={500}>
            Overall
          </Text>
          <RatingPicker labels={req.rating_labels} value={rating} onChange={setRating} />
          {rating ? (
            <Text size="xs" c="dimmed">
              {ratingLabel(rating, req.rating_labels)}
            </Text>
          ) : null}
          <Textarea
            label="What do they do well?"
            autosize
            minRows={2}
            value={strengths}
            onChange={(e) => setStrengths(e.currentTarget.value)}
          />
          <Textarea
            label="What could they do better?"
            autosize
            minRows={2}
            value={improvements}
            onChange={(e) => setImprovements(e.currentTarget.value)}
          />
          <Group justify="flex-end">
            <Button onClick={submit} loading={busy} disabled={!rating}>
              Send feedback
            </Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  );
}
