import { Button, Group, Modal, Rating, SegmentedControl, Stack, Text, Textarea } from '@mantine/core';
import { useState } from 'react';
import { lifecycle } from '../../api/lifecycle';
import { DateField } from '../../components/BsDatePicker';
import { addDays, fmtAd, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';

/** The manager's probation review: a rating, a recommendation and comments, sent to HR for approval. */
export function ProbationReviewModal({ employee, onClose, onDone }: { employee: { id: string; name_en: string; probation_end: string | null } | null; onClose: () => void; onDone: () => void }) {
  const [rating, setRating] = useState(4);
  const [rec, setRec] = useState<'confirm' | 'extend' | 'terminate'>('confirm');
  const [extendTo, setExtendTo] = useState<string | null>(null);
  const [lwd, setLwd] = useState<string | null>(null);
  const [comments, setComments] = useState('');
  const [busy, setBusy] = useState(false);
  const base = employee?.probation_end && employee.probation_end > todayNpt() ? employee.probation_end : todayNpt();
  const submit = async () => {
    if (!employee) return;
    setBusy(true);
    try {
      await lifecycle.submitReview({ employeeId: employee.id, rating, recommendation: rec, comments: comments.trim(), extendTo: rec === 'extend' ? extendTo : null, lastWorkingDay: rec === 'terminate' ? lwd : null });
      toast.ok('Review sent to HR for approval.'); setComments(''); onDone(); onClose();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Modal opened={!!employee} onClose={onClose} title={employee ? `Probation review: ${employee.name_en}` : ''} centered size="md">
      {employee ? (
        <Stack>
          <Text size="sm" c="dimmed">{employee.probation_end ? `Probation ends on ${fmtAd(employee.probation_end)}.` : 'No probation end date is recorded.'} HR approves your recommendation before it takes effect.</Text>
          <div>
            <Text size="sm" fw={500} mb={4}>Overall rating</Text>
            <Group gap="sm"><Rating value={rating} onChange={setRating} size="lg" /><Text size="sm" c="dimmed">{['', 'Poor', 'Below expectations', 'Meets expectations', 'Exceeds expectations', 'Outstanding'][rating]}</Text></Group>
          </div>
          <SegmentedControl value={rec} onChange={(v) => { setRec(v as typeof rec); if (v === 'extend' && !extendTo) setExtendTo(addDays(base, 90)); if (v === 'terminate' && !lwd) setLwd(base); }} fullWidth
            data={[{ value: 'confirm', label: 'Confirm' }, { value: 'extend', label: 'Extend' }, { value: 'terminate', label: 'End employment' }]} />
          {rec === 'extend' ? <DateField label="New probation end" value={extendTo} onChange={setExtendTo} minDate={addDays(base, 1)} required /> : null}
          {rec === 'terminate' ? <DateField label="Last working day" value={lwd} onChange={setLwd} required /> : null}
          <Textarea label="Comments" description="Strengths, gaps and the reason for your recommendation." value={comments} onChange={(e) => setComments(e.currentTarget.value)} autosize minRows={3} required />
          <Group justify="flex-end">
            <Button variant="default" onClick={onClose}>Cancel</Button>
            <Button loading={busy} {...(rec === 'terminate' ? { color: 'red' } : {})} disabled={comments.trim().length < 3 || (rec === 'extend' && !extendTo) || (rec === 'terminate' && !lwd)} onClick={submit}>Send for approval</Button>
          </Group>
        </Stack>
      ) : null}
    </Modal>
  );
}
