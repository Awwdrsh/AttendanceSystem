import { Badge, Group } from '@mantine/core';
import type { AttendanceStatus } from '../api/types';

type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'idle' | 'accent';

/** Every status gets a coloured dot and a word, so colour is never the only signal. */
const MAP: Record<string, { tone: Tone; label: string }> = {
  Present: { tone: 'ok', label: 'Present' },
  Working: { tone: 'ok', label: 'Clocked in' },
  Late: { tone: 'warn', label: 'Late' },
  Absent: { tone: 'bad', label: 'Absent' },
  OnLeave: { tone: 'info', label: 'On leave' },
  HalfDayLeave: { tone: 'info', label: 'Half-day leave' },
  HalfDay: { tone: 'warn', label: 'Half day' },
  Holiday: { tone: 'idle', label: 'Holiday' },
  WeeklyOff: { tone: 'idle', label: 'Weekly off' },
  Incomplete: { tone: 'warn', label: 'Missing punch' },
  WFH: { tone: 'info', label: 'Remote' },
  OffShift: { tone: 'idle', label: 'Off shift' },
  Pending: { tone: 'accent', label: 'Pending' },
  pending: { tone: 'accent', label: 'Pending' },
  approved: { tone: 'ok', label: 'Approved' },
  rejected: { tone: 'bad', label: 'Rejected' },
  cancelled: { tone: 'idle', label: 'Cancelled' },
  detected: { tone: 'accent', label: 'Needs review' },
  posted: { tone: 'ok', label: 'Posted' },
  comp_off: { tone: 'info', label: 'Comp-off' },
  active: { tone: 'ok', label: 'Active' },
  disabled: { tone: 'bad', label: 'Disabled' },
  exited: { tone: 'idle', label: 'Exited' },
  draft: { tone: 'idle', label: 'Draft' },
  preflight_failed: { tone: 'bad', label: 'Blocked' },
  computed: { tone: 'accent', label: 'Calculated' },
  reviewed: { tone: 'info', label: 'Reviewed' },
  approved_run: { tone: 'ok', label: 'Approved' },
  locked: { tone: 'ok', label: 'Locked' },
  disbursed: { tone: 'ok', label: 'Paid' },
  filed: { tone: 'ok', label: 'Filed' },
  published: { tone: 'ok', label: 'Published' },
  submitted: { tone: 'accent', label: 'Submitted' },
  verified: { tone: 'ok', label: 'Verified' },
  online: { tone: 'ok', label: 'Online' },
  offline: { tone: 'bad', label: 'Offline' },
  NotComputed: { tone: 'idle', label: 'Not calculated' },
};

const TONE: Record<Tone, { color: string; dot: string }> = {
  ok: { color: 'teal', dot: 'var(--hj-ok)' },
  warn: { color: 'orange', dot: 'var(--hj-warn)' },
  bad: { color: 'red', dot: 'var(--hj-bad)' },
  info: { color: 'blue', dot: 'var(--hj-info)' },
  idle: { color: 'gray', dot: 'var(--hj-idle)' },
  accent: { color: 'brand', dot: 'var(--mantine-color-brand-6)' },
};

/** Today's "Incomplete" just means the person has not clocked out yet. */
export function liveStatus(status: string | null | undefined, opts: { date: string; today: string; lastOut: string | null | undefined; firstIn: string | null | undefined }): string | null | undefined {
  if (opts.date === opts.today && status === 'Incomplete' && opts.firstIn && !opts.lastOut) return 'Working';
  return status;
}

export function toneOf(status: string): Tone { return MAP[status]?.tone ?? 'idle'; }
export function toneColor(status: string): string { return TONE[toneOf(status)].dot; }

function Pill({ tone, label }: { tone: Tone; label: string }) {
  const t = TONE[tone];
  return (
    <Badge variant="light" color={t.color} size="md" radius="sm" leftSection={<span className="hj-dot" style={{ background: t.dot }} aria-hidden />}
      styles={{ root: { paddingInline: 8, fontWeight: 500 } }}>{label}</Badge>
  );
}

export function StatusBadge({ status, late }: { status: AttendanceStatus | string | null | undefined; late?: number | null }) {
  if (!status) return <Pill tone="idle" label="—" />;
  const m = MAP[status] ?? { tone: 'idle' as Tone, label: humanize(status) };
  return (
    <Group gap={4} wrap="nowrap" display="inline-flex">
      <Pill tone={m.tone} label={m.label} />
      {late && late > 0 ? <Pill tone="warn" label={`${late} min late`} /> : null}
    </Group>
  );
}

export function statusLabel(status: string, _lang: 'en' = 'en'): string {
  return MAP[status]?.label ?? humanize(status);
}

function humanize(s: string): string {
  const spaced = s.replace(/_/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
