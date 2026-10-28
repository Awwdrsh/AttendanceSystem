import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Divider,
  Group,
  MultiSelect,
  Paper,
  Select,
  Stack,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';
import { IconLock, IconPaperclip } from '@tabler/icons-react';
import { useState } from 'react';
import { auth as authApi } from '../../api';
import { GRIEVANCE_CATEGORY_LABEL, GRIEVANCE_STATUS_LABEL, OUTCOME_LABEL, relations } from '../../api/relations';
import { useAuth } from '../../auth/AuthProvider';
import { DateField } from '../../components/BsDatePicker';
import { EmptyState } from '../../components/EmptyState';
import { fmtAd, fmtDateLong, fmtTime, relativeTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS_COLOR = {
  submitted: 'blue',
  under_review: 'cyan',
  hearing_scheduled: 'violet',
  decided: 'teal',
  appealed: 'orange',
  closed: 'gray',
  withdrawn: 'gray',
};

export function GrievanceStatusBadge({ status }) {
  return (
    <Badge variant="light" color={STATUS_COLOR[status]}>
      {GRIEVANCE_STATUS_LABEL[status]}
    </Badge>
  );
}

const OUTCOMES = Object.keys(OUTCOME_LABEL).map((v) => ({ value: v, label: OUTCOME_LABEL[v] }));
const OPEN = ['submitted', 'under_review', 'hearing_scheduled'];

/** One grievance: the account, the conversation and, for the committee and HR, the hearing, decision and appeal steps. */
export function GrievanceThread({ id, onChanged }) {
  const { hasRole } = useAuth();
  const q = useQuery(() => relations.grievance(id), [id]);
  const [note, setNote] = useState('');
  const [shared, setShared] = useState(false);
  const [hearing, setHearing] = useState(null);
  const [decision, setDecision] = useState(null);
  const [appeal, setAppeal] = useState(null);
  const [appealDecision, setAppealDecision] = useState(null);
  const [busy, setBusy] = useState(false);
  const isHr = hasRole('hr');
  const users = useQuery(() => authApi.users(), [], { enabled: isHr });

  if (q.error) return <EmptyState title="Could not open this grievance" hint={q.error} />;
  if (!q.data)
    return (
      <Text c="dimmed" size="sm">
        Loading…
      </Text>
    );
  const { grievance: g, notes, files, canAppeal, appealUntil } = q.data;
  const handler = g.role !== 'complainant';
  const done = async (fn, msg) => {
    setBusy(true);
    try {
      await fn();
      toast.ok(msg);
      await q.reload();
      onChanged?.();
      return true;
    } catch (e) {
      toast.err(e);
      return false;
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack gap="md">
      <Group justify="space-between" wrap="nowrap" align="flex-start">
        <div style={{ minWidth: 0 }}>
          <Text size="xs" c="dimmed" ff="monospace">
            G-{g.number}
          </Text>
          <Text fw={600}>{g.subject}</Text>
          <Text size="xs" c="dimmed">
            {GRIEVANCE_CATEGORY_LABEL[g.category]}, filed {fmtDateLong(g.createdAt.slice(0, 10))}
            {handler && g.employeeName ? ` by ${g.employeeName} (${g.empCode})` : ''}
          </Text>
        </div>
        <GrievanceStatusBadge status={g.status} />
      </Group>
      <Alert variant="light" color="grape" icon={<IconLock size={16} stroke={1.6} />}>
        {handler
          ? 'Confidential. Only HR and the grievance committee can see this, and each time you open it is recorded.'
          : 'Confidential. Only HR and the grievance committee can see this. Your manager is never shown it.'}
      </Alert>
      <Paper withBorder p="sm">
        <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
          {g.description}
        </Text>
        {g.concerns ? (
          <Text size="xs" c="dimmed" mt="xs">
            Concerns: {g.concerns}
          </Text>
        ) : null}
        {files.length ? (
          <Group gap="xs" mt="sm">
            {files.map((f) => (
              <Anchor key={f.id} size="xs" href={relations.grievanceFileUrl(g.id, f.id)} target="_blank" rel="noopener">
                <IconPaperclip size={12} /> {f.filename}
              </Anchor>
            ))}
          </Group>
        ) : null}
      </Paper>

      {g.hearingAt ? (
        <Text size="sm">
          Hearing: <b>{fmtAd(g.hearingAt.slice(0, 10))}</b> at {fmtTime(g.hearingAt)}
          {g.hearingPlace ? `, ${g.hearingPlace}` : ''}
        </Text>
      ) : null}
      {g.outcome ? (
        <Paper withBorder p="sm" bg="var(--mantine-color-default-hover)">
          <Group gap="xs" mb={4}>
            <Text size="sm" fw={600}>
              Decision
            </Text>
            <Badge variant="light">{OUTCOME_LABEL[g.outcome]}</Badge>
            {g.decidedAt ? (
              <Text size="xs" c="dimmed">
                {relativeTime(g.decidedAt)}
              </Text>
            ) : null}
          </Group>
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {g.findings}
          </Text>
        </Paper>
      ) : null}
      {g.appealReason ? (
        <Paper withBorder p="sm">
          <Text size="sm" fw={600}>
            Appeal
          </Text>
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {g.appealReason}
          </Text>
        </Paper>
      ) : null}
      {g.appealNote ? (
        <Paper withBorder p="sm">
          <Text size="sm" fw={600}>
            Appeal decision: {g.appealOutcome === 'decision_upheld' ? 'original decision stands' : 'decision changed'}
          </Text>
          <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
            {g.appealNote}
          </Text>
        </Paper>
      ) : null}

      <Divider label="Messages" labelPosition="left" />
      {notes.length ? (
        <Stack gap="xs">
          {notes.map((n) => (
            <Paper key={n.id} withBorder p="xs" style={n.shared ? undefined : { borderStyle: 'dashed' }}>
              <Group justify="space-between">
                <Text size="xs" fw={600}>
                  {n.mine ? 'You' : n.fromComplainant ? 'Employee' : n.authorName}
                </Text>
                <Group gap={6}>
                  {!n.shared ? (
                    <Badge size="xs" variant="light" color="gray">
                      Committee only
                    </Badge>
                  ) : null}
                  <Text size="xs" c="dimmed">
                    {relativeTime(n.createdAt)}
                  </Text>
                </Group>
              </Group>
              <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                {n.body}
              </Text>
            </Paper>
          ))}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          No messages yet.
        </Text>
      )}
      {!['closed', 'withdrawn'].includes(g.status) ? (
        <Stack gap="xs">
          <Textarea
            placeholder={handler ? 'Add a note' : 'Send a message to the committee'}
            value={note}
            onChange={(e) => setNote(e.currentTarget.value)}
            autosize
            minRows={2}
            maxLength={4000}
          />
          <Group justify="space-between">
            {handler ? (
              <Checkbox label="Share with the employee" checked={shared} onChange={(e) => setShared(e.currentTarget.checked)} />
            ) : (
              <span />
            )}
            <Button
              size="xs"
              loading={busy}
              disabled={!note.trim()}
              onClick={async () => {
                if (await done(() => relations.addNote(g.id, note.trim(), shared), 'Sent.')) {
                  setNote('');
                  setShared(false);
                }
              }}
            >
              Send
            </Button>
          </Group>
        </Stack>
      ) : null}

      {handler && OPEN.includes(g.status) ? (
        <>
          <Divider label="Committee actions" labelPosition="left" />
          <Group gap="xs">
            {g.status === 'submitted' ? (
              <Button
                size="xs"
                variant="default"
                loading={busy}
                onClick={() => done(() => relations.updateGrievance(g.id, { status: 'under_review' }), 'Marked as under review.')}
              >
                Start review
              </Button>
            ) : null}
            <Button
              size="xs"
              variant="default"
              onClick={() => setHearing({ date: g.hearingAt?.slice(0, 10) ?? null, time: '10:00', place: g.hearingPlace ?? '' })}
            >
              {g.hearingAt ? 'Change hearing' : 'Schedule hearing'}
            </Button>
            <Button size="xs" onClick={() => setDecision({ outcome: null, findings: '' })}>
              Record decision
            </Button>
          </Group>
          {hearing ? (
            <Paper withBorder p="sm">
              <Stack gap="xs">
                <Group grow>
                  <DateField
                    label="Date"
                    value={hearing.date}
                    onChange={(v) => setHearing({ ...hearing, date: v })}
                    minDate={todayNpt()}
                    required
                  />
                  <TextInput
                    label="Time"
                    type="time"
                    value={hearing.time}
                    onChange={(e) => setHearing({ ...hearing, time: e.currentTarget.value })}
                  />
                </Group>
                <TextInput
                  label="Place or link"
                  value={hearing.place}
                  onChange={(e) => setHearing({ ...hearing, place: e.currentTarget.value })}
                  maxLength={200}
                />
                <Group justify="flex-end">
                  <Button size="xs" variant="default" onClick={() => setHearing(null)}>
                    Cancel
                  </Button>
                  <Button
                    size="xs"
                    loading={busy}
                    disabled={!hearing.date || !hearing.time}
                    onClick={async () => {
                      if (
                        await done(
                          () =>
                            relations.updateGrievance(g.id, {
                              hearingAt: `${hearing.date}T${hearing.time}:00+05:45`,
                              hearingPlace: hearing.place.trim() || null,
                            }),
                          'Hearing set. The employee was told.',
                        )
                      )
                        setHearing(null);
                    }}
                  >
                    Save
                  </Button>
                </Group>
              </Stack>
            </Paper>
          ) : null}
          {decision ? (
            <Paper withBorder p="sm">
              <Stack gap="xs">
                <Select
                  label="Outcome"
                  data={OUTCOMES}
                  value={decision.outcome}
                  onChange={(v) => setDecision({ ...decision, outcome: v })}
                  allowDeselect={false}
                />
                <Textarea
                  label="Findings and what will happen"
                  description="The employee reads this."
                  value={decision.findings}
                  onChange={(e) => setDecision({ ...decision, findings: e.currentTarget.value })}
                  autosize
                  minRows={3}
                  maxLength={8000}
                />
                <Group justify="flex-end">
                  <Button size="xs" variant="default" onClick={() => setDecision(null)}>
                    Cancel
                  </Button>
                  <Button
                    size="xs"
                    loading={busy}
                    disabled={!decision.outcome || !decision.findings.trim()}
                    onClick={async () => {
                      if (
                        await done(
                          () => relations.decide(g.id, { outcome: decision.outcome, findings: decision.findings.trim() }),
                          'Decision recorded.',
                        )
                      )
                        setDecision(null);
                    }}
                  >
                    Record decision
                  </Button>
                </Group>
              </Stack>
            </Paper>
          ) : null}
          {isHr ? (
            <MultiSelect
              label="Exclude from this case"
              description="People with a conflict of interest lose access to it."
              size="xs"
              searchable
              clearable
              data={(users.data?.users ?? []).filter((u) => u.status === 'active').map((u) => ({ value: u.id, label: u.name }))}
              value={g.excludedUserIds}
              onChange={(v) => {
                void done(() => relations.updateGrievance(g.id, { excludeUserIds: v }), 'Saved.');
              }}
            />
          ) : null}
        </>
      ) : null}

      {g.role === 'hr' && g.status === 'appealed' ? (
        appealDecision ? (
          <Paper withBorder p="sm">
            <Stack gap="xs">
              <Select
                label="Appeal outcome"
                value={appealDecision.outcome}
                allowDeselect={false}
                onChange={(v) => setAppealDecision({ ...appealDecision, outcome: v })}
                data={[
                  { value: 'decision_upheld', label: 'The original decision stands' },
                  { value: 'decision_changed', label: 'Change the decision' },
                ]}
              />
              {appealDecision.outcome === 'decision_changed' ? (
                <Select
                  label="New outcome"
                  data={OUTCOMES}
                  value={appealDecision.newOutcome}
                  onChange={(v) => setAppealDecision({ ...appealDecision, newOutcome: v })}
                />
              ) : null}
              <Textarea
                label="Reasons"
                value={appealDecision.note}
                onChange={(e) => setAppealDecision({ ...appealDecision, note: e.currentTarget.value })}
                autosize
                minRows={3}
              />
              <Group justify="flex-end">
                <Button size="xs" variant="default" onClick={() => setAppealDecision(null)}>
                  Cancel
                </Button>
                <Button
                  size="xs"
                  loading={busy}
                  disabled={!appealDecision.note.trim() || (appealDecision.outcome === 'decision_changed' && !appealDecision.newOutcome)}
                  onClick={async () => {
                    if (
                      await done(
                        () =>
                          relations.decideAppeal(g.id, {
                            outcome: appealDecision.outcome,
                            note: appealDecision.note.trim(),
                            ...(appealDecision.newOutcome ? { newOutcome: appealDecision.newOutcome } : {}),
                          }),
                        'Appeal decided.',
                      )
                    )
                      setAppealDecision(null);
                  }}
                >
                  Decide appeal
                </Button>
              </Group>
            </Stack>
          </Paper>
        ) : (
          <Button size="xs" onClick={() => setAppealDecision({ outcome: 'decision_upheld', newOutcome: null, note: '' })}>
            Hear the appeal
          </Button>
        )
      ) : null}

      {g.role === 'complainant' ? (
        <Group gap="xs">
          {OPEN.includes(g.status) ? (
            <Button
              size="xs"
              variant="subtle"
              color="gray"
              loading={busy}
              onClick={() => done(() => relations.withdraw(g.id), 'Grievance withdrawn.')}
            >
              Withdraw
            </Button>
          ) : null}
          {canAppeal && appeal === null ? (
            <Button size="xs" variant="default" onClick={() => setAppeal('')}>
              Appeal{appealUntil ? ` (until ${fmtAd(appealUntil.slice(0, 10))})` : ''}
            </Button>
          ) : null}
          {g.status === 'decided' ? (
            <Button size="xs" variant="subtle" loading={busy} onClick={() => done(() => relations.close(g.id), 'Grievance closed.')}>
              Accept and close
            </Button>
          ) : null}
        </Group>
      ) : null}
      {g.role === 'hr' && g.status === 'decided' ? (
        <Group>
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            loading={busy}
            onClick={() => done(() => relations.close(g.id), 'Grievance closed.')}
          >
            Close
          </Button>
        </Group>
      ) : null}
      {appeal !== null ? (
        <Paper withBorder p="sm">
          <Stack gap="xs">
            <Textarea
              label="Why do you disagree with the decision?"
              value={appeal}
              onChange={(e) => setAppeal(e.currentTarget.value)}
              autosize
              minRows={3}
              maxLength={4000}
            />
            <Text size="xs" c="dimmed">
              HR hears appeals. You can appeal only once.
            </Text>
            <Group justify="flex-end">
              <Button size="xs" variant="default" onClick={() => setAppeal(null)}>
                Cancel
              </Button>
              <Button
                size="xs"
                loading={busy}
                disabled={!appeal.trim()}
                onClick={async () => {
                  if (await done(() => relations.appeal(g.id, appeal.trim()), 'Appeal sent to HR.')) setAppeal(null);
                }}
              >
                Send appeal
              </Button>
            </Group>
          </Stack>
        </Paper>
      ) : null}
    </Stack>
  );
}
