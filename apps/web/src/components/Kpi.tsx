import { Group, Paper, Text, Tooltip } from '@mantine/core';
import type { ReactNode } from 'react';
import { fmtAd } from '../lib/format';

export interface Stat { key: string; label: string; value: number | string; hint?: ReactNode; tone?: 'ok' | 'warn' | 'bad' }

/** A row of headline numbers inside one panel, divided by hairlines. */
export function StatStrip({ stats }: { stats: Stat[] }) {
  return (
    <div className="hj-stats" style={{ ['--cols' as string]: Math.min(stats.length, 6) }}>
      {stats.map((s) => (
        <div className="hj-stat" key={s.key}>
          <Group gap={6} wrap="nowrap">
            {s.tone ? <span className="hj-dot" style={{ background: `var(--hj-${s.tone})` }} aria-hidden /> : null}
            <Text size="sm" c="dimmed" truncate className="hj-stat-label">{s.label}</Text>
          </Group>
          <div className="hj-stat-value">{typeof s.value === 'number' ? s.value.toLocaleString('en-US') : s.value}</div>
          {s.hint ? <Text size="xs" c="dimmed" mt={4}>{s.hint}</Text> : null}
        </div>
      ))}
    </div>
  );
}

/** Older call sites pass {key,label,value,hint}. */
export function KpiTiles({ tiles }: { tiles: Array<{ key: string; label: string; value: number | string; hint?: string }> }) {
  return <StatStrip stats={tiles} />;
}

/**
 * Daily bars with a 0–100 scale. Days with no working data (weekends, holidays) render as a short grey tick,
 * so gaps read as "no work day" instead of "zero attendance".
 */
export function TrendBars({ points, label, max = 100, suffix = '%', height = 160, action }: { points: Array<{ date: string; value: number | null }>; label: string; max?: number; suffix?: string; height?: number; action?: ReactNode }) {
  const w = 720; const h = height; const top = 8; const bottom = 22; const left = 34;
  const plotH = h - top - bottom;
  const n = Math.max(points.length, 1);
  const bw = (w - left) / n;
  const values = points.map((p) => p.value).filter((v): v is number => v !== null);
  const avg = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  const ticks = [0, 50, 100].map((t) => (t * max) / 100);
  return (
    <Paper withBorder p="lg">
      <Group justify="space-between" mb="sm" align="flex-start">
        <div>
          <Text fw={600} size="sm">{label}</Text>
          <Text size="xs" c="dimmed">{points.length ? `${fmtAd(points[0]!.date)} to ${fmtAd(points[points.length - 1]!.date)}` : 'No data yet'}</Text>
        </div>
        <Group gap="lg">
          {avg !== null ? <div style={{ textAlign: 'right' }}><Text size="xs" c="dimmed">Average</Text><Text fw={600} className="num">{Math.round(avg)}{suffix}</Text></div> : null}
          {action}
        </Group>
      </Group>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label={label} preserveAspectRatio="none" style={{ display: 'block', overflow: 'visible' }}>
        {ticks.map((t) => {
          const y = top + plotH - (plotH * t) / max;
          return <g key={t}><line x1={left} x2={w} y1={y} y2={y} stroke="var(--hj-line-soft)" /><text x={left - 8} y={y + 4} textAnchor="end" fontSize="11" fill="var(--hj-muted)">{t}{suffix}</text></g>;
        })}
        {points.map((p, i) => {
          const x = left + i * bw + bw * 0.18;
          const bwInner = Math.max(2, bw * 0.64);
          if (p.value === null) return <rect key={p.date} x={x} y={top + plotH - 2} width={bwInner} height={2} rx={1} fill="var(--hj-line)"><title>{`${fmtAd(p.date)}: no working day`}</title></rect>;
          const bh = Math.max(2, (plotH * p.value) / max);
          return <rect key={p.date} x={x} y={top + plotH - bh} width={bwInner} height={bh} rx={3} fill="var(--hj-hydro)" opacity={i === points.length - 1 ? 1 : 0.78}><title>{`${fmtAd(p.date)}: ${p.value}${suffix}`}</title></rect>;
        })}
        {points.map((p, i) => (i % Math.ceil(n / 6) === 0 || i === n - 1)
          ? <text key={`l${p.date}`} x={left + i * bw + bw / 2} y={h - 4} textAnchor="middle" fontSize="11" fill="var(--hj-muted)">{fmtAd(p.date).replace(/ \d{4}$/, '')}</text> : null)}
      </svg>
    </Paper>
  );
}

/** Small "label: value" pair used in detail panels. */
export function Field({ label, children, tip }: { label: string; children: ReactNode; tip?: string }) {
  const body = <Text size="sm" fw={500}>{children}</Text>;
  return (
    <div>
      <Text size="xs" c="dimmed" mb={2}>{label}</Text>
      {tip ? <Tooltip label={tip}>{body}</Tooltip> : body}
    </div>
  );
}
