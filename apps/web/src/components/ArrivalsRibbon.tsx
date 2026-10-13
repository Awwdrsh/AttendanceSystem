import { Group, Text, Tooltip } from '@mantine/core';
import { fmtTime, NPT_OFFSET_MIN } from '../lib/format';

export interface Arrival { id: string; name: string; at: string | null; late: boolean }

const minuteOfDay = (iso: string) => { const d = new Date(Date.parse(iso) + NPT_OFFSET_MIN * 60_000); return d.getUTCHours() * 60 + d.getUTCMinutes(); };
const hhmm = (m: number) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;

/**
 * The day as a ribbon: every clock-in is a mark on a time axis, with the shift start drawn as a line.
 * Marks stack upward when people arrive within the same few minutes, so the morning rush has a shape.
 */
export function ArrivalsRibbon({ arrivals, shiftStart = '10:00', grace = 15 }: { arrivals: Arrival[]; shiftStart?: string; grace?: number }) {
  const [sh, sm] = shiftStart.split(':').map(Number);
  const start = (sh ?? 10) * 60 + (sm ?? 0);
  const times = arrivals.filter((a) => a.at).map((a) => minuteOfDay(a.at!));
  // Zoom to the morning that actually happened, always keeping the shift start in view.
  const from = Math.floor((Math.min(start - 60, ...times) - 10) / 30) * 30;
  const to = Math.ceil((Math.max(start + 60, ...times) + 10) / 30) * 30;
  const span = to - from;
  const pos = (m: number) => `${((Math.min(Math.max(m, from), to) - from) / span) * 100}%`;
  const bucket = Math.max(2, Math.round((to - from) / 60)); // minutes per column
  const stacks = new Map<number, number>();
  const marks = arrivals.filter((a) => a.at).map((a) => {
    const m = minuteOfDay(a.at!);
    const b = Math.floor(m / bucket);
    const level = stacks.get(b) ?? 0; stacks.set(b, level + 1);
    return { ...a, m, level };
  });
  const hours: number[] = []; for (let t = Math.ceil(from / 30) * 30; t <= to; t += 30) hours.push(t);
  const maxLevel = Math.max(3, ...marks.map((m) => m.level + 1));
  const rowH = 11;
  const height = maxLevel * rowH + 8;

  return (
    <div>
      <div style={{ position: 'relative', height, borderBottom: '1px solid var(--hj-line)' }}>
        <div style={{ position: 'absolute', left: pos(start), width: `${(grace / span) * 100}%`, top: 0, bottom: 0, background: 'var(--mantine-color-brand-light)', borderLeft: '1.5px solid var(--mantine-color-brand-6)' }} />
        {marks.map((m) => (
          <Tooltip key={m.id} label={`${m.name}, ${fmtTime(m.at)}${m.late ? ' (late)' : ''}`}>
            <div style={{ position: 'absolute', left: `calc(${pos(Math.floor(m.m / bucket) * bucket)} + 1px)`, bottom: 4 + m.level * rowH, width: 9, height: 9, borderRadius: 2,
              background: m.late ? 'var(--hj-warn)' : 'var(--mantine-color-brand-6)' }} />
          </Tooltip>
        ))}
      </div>
      <div style={{ position: 'relative', height: 20 }}>
        {hours.map((h) => <Text key={h} size="xs" c="dimmed" className="num" style={{ position: 'absolute', left: pos(h), transform: 'translateX(-50%)', top: 4 }}>{hhmm(h)}</Text>)}
      </div>
      <Group gap="lg" mt={6}>
        <Group gap={6}><span style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--mantine-color-brand-6)' }} /><Text size="xs" c="dimmed">On time</Text></Group>
        <Group gap={6}><span style={{ width: 9, height: 9, borderRadius: 2, background: 'var(--hj-warn)' }} /><Text size="xs" c="dimmed">Late</Text></Group>
        <Group gap={6}><span style={{ width: 10, height: 10, background: 'var(--mantine-color-brand-light)', borderLeft: '1.5px solid var(--mantine-color-brand-6)' }} /><Text size="xs" c="dimmed">Shift start {shiftStart} and grace</Text></Group>
      </Group>
    </div>
  );
}
