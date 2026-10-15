import { Box, Group, Stack, Text } from '@mantine/core';
import { useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { compact, niceScale, useVizColors } from './palette';

export interface Series { key: string; label: string; values: Array<number | null>; color?: string }

const W = 640;

/** Small floating label that follows the hovered mark; text stays in ink colours, a swatch carries identity. */
function HoverTip({ x, y, children }: { x: number; y: number; children: ReactNode }) {
  return (
    <Box style={{
      position: 'absolute', left: `${x}%`, top: y, transform: `translate(${x > 70 ? '-100%' : x < 30 ? '0' : '-50%'}, -100%)`, pointerEvents: 'none', zIndex: 2,
      background: 'var(--hj-raised, var(--mantine-color-body))', border: '1px solid var(--hj-line)', borderRadius: 8, padding: '6px 10px',
      boxShadow: '0 4px 14px rgba(0,0,0,.12)', whiteSpace: 'nowrap',
    }}>{children}</Box>
  );
}

function TipRows({ title, rows, format }: { title: string; rows: Array<{ label: string; value: number | null; color: string }>; format: (n: number) => string }) {
  return (
    <Stack gap={2}>
      <Text size="xs" fw={600}>{title}</Text>
      {rows.map((r) => (
        <Group key={r.label} gap={6} wrap="nowrap">
          <span aria-hidden style={{ width: 8, height: 8, borderRadius: 2, background: r.color, display: 'inline-block' }} />
          <Text size="xs" c="dimmed">{r.label}</Text>
          <Text size="xs" fw={600} className="num" ml="auto">{r.value === null ? '—' : format(r.value)}</Text>
        </Group>
      ))}
    </Stack>
  );
}

function useHoverIndex(n: number, left: number, right: number) {
  const ref = useRef<SVGSVGElement>(null);
  const [i, setI] = useState<number | null>(null);
  const onMove = (e: MouseEvent<SVGSVGElement>) => {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const x = ((e.clientX - box.left) / box.width) * W;
    const idx = Math.floor(((x - left) / (W - left - right)) * n);
    setI(idx >= 0 && idx < n ? idx : null);
  };
  return { ref, i, onMove, onLeave: () => setI(null) };
}

/**
 * Columns per month. Several series are drawn side by side, or stacked when `stacked`; bars have rounded data-ends on the
 * baseline side away, a 2px gap between neighbours, and a hover tooltip for the whole month.
 */
export function ColumnChart({ labels, series, height = 200, stacked = false, money = false, format, ariaLabel }: {
  labels: string[]; series: Series[]; height?: number; stacked?: boolean; money?: boolean; format?: (n: number) => string; ariaLabel: string;
}) {
  const c = useVizColors();
  const left = 40; const right = 6; const top = 10; const bottom = 24;
  const n = labels.length;
  const fmt = format ?? ((v: number) => (money ? `Rs ${Math.round(v).toLocaleString('en-IN')}` : v.toLocaleString('en-US')));
  const sums = labels.map((_, i) => (stacked ? series.reduce((s, x) => s + (x.values[i] ?? 0), 0) : Math.max(0, ...series.map((x) => x.values[i] ?? 0))));
  const { top: maxV, ticks } = niceScale(Math.max(0, ...sums));
  const plotH = height - top - bottom;
  const slot = (W - left - right) / Math.max(n, 1);
  const groupW = Math.min(slot * 0.7, 46);
  const barW = stacked ? groupW : Math.max(2, (groupW - (series.length - 1) * 2) / series.length);
  const y = (v: number) => top + plotH - (v / maxV) * plotH;
  const hover = useHoverIndex(n, left, right);
  const colorOf = (s: Series, k: number) => s.color ?? c.series[k % c.series.length]!;
  const every = Math.ceil(n / 12);
  return (
    <Box pos="relative">
      <svg ref={hover.ref} viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-label={ariaLabel} onMouseMove={hover.onMove} onMouseLeave={hover.onLeave} style={{ display: 'block' }}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={left} x2={W - right} y1={y(t)} y2={y(t)} stroke={t === 0 ? c.axis : c.grid} strokeWidth={1} />
            <text x={left - 6} y={y(t) + 3} textAnchor="end" fontSize={10} fill={c.muted}>{compact(t, money)}</text>
          </g>
        ))}
        {labels.map((l, i) => {
          const gx = left + slot * i + (slot - groupW) / 2;
          return (
            <g key={i}>
              {hover.i === i ? <rect x={left + slot * i} y={top} width={slot} height={plotH} fill={c.grid} opacity={0.5} /> : null}
              {series.map((s, k) => {
                const v = s.values[i] ?? 0;
                if (v <= 0) return null;
                const base = stacked ? series.slice(0, k).reduce((sum, o) => sum + Math.max(0, o.values[i] ?? 0), 0) : 0;
                const x = stacked ? gx : gx + k * (barW + 2);
                const yTop = y(base + v);
                // A 2px surface gap separates stacked segments.
                const h = Math.max(1, y(base) - yTop - (base > 0 ? 2 : 0));
                const r = Math.min(4, barW / 2, h);
                const isTop = !stacked || series.slice(k + 1).every((o) => (o.values[i] ?? 0) <= 0);
                return isTop
                  ? <path key={s.key} d={`M${x},${yTop + h} V${yTop + r} Q${x},${yTop} ${x + r},${yTop} H${x + barW - r} Q${x + barW},${yTop} ${x + barW},${yTop + r} V${yTop + h} Z`} fill={colorOf(s, k)} />
                  : <rect key={s.key} x={x} y={yTop} width={barW} height={h} fill={colorOf(s, k)} />;
              })}
              {i % every === 0 ? <text x={left + slot * i + slot / 2} y={height - 8} textAnchor="middle" fontSize={10} fill={c.muted}>{l}</text> : null}
            </g>
          );
        })}
      </svg>
      {hover.i !== null ? (
        <HoverTip x={((left + slot * (hover.i + 0.5)) / W) * 100} y={top + 4}>
          <TipRows title={labels[hover.i]!} format={fmt} rows={series.map((s, k) => ({ label: s.label, value: s.values[hover.i!] ?? null, color: colorOf(s, k) }))} />
        </HoverTip>
      ) : null}
    </Box>
  );
}

/** Lines over months with ≥8px markers and a crosshair tooltip; gaps where a month has no value. */
export function LineChart({ labels, series, height = 200, suffix = '', format, ariaLabel, minZero = true }: {
  labels: string[]; series: Series[]; height?: number; suffix?: string; format?: (n: number) => string; ariaLabel: string; minZero?: boolean;
}) {
  const c = useVizColors();
  const left = 40; const right = 10; const top = 12; const bottom = 24;
  const n = labels.length;
  const fmt = format ?? ((v: number) => `${v.toLocaleString('en-US')}${suffix}`);
  const all = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const { top: maxV, ticks } = niceScale(Math.max(minZero ? 0 : Math.min(...all, 0), ...all, 0));
  const plotH = height - top - bottom;
  const slot = (W - left - right) / Math.max(n, 1);
  const x = (i: number) => left + slot * i + slot / 2;
  const y = (v: number) => top + plotH - (v / maxV) * plotH;
  const hover = useHoverIndex(n, left, right);
  const colorOf = (s: Series, k: number) => s.color ?? c.series[k % c.series.length]!;
  const every = Math.ceil(n / 12);
  return (
    <Box pos="relative">
      <svg ref={hover.ref} viewBox={`0 0 ${W} ${height}`} width="100%" role="img" aria-label={ariaLabel} onMouseMove={hover.onMove} onMouseLeave={hover.onLeave} style={{ display: 'block' }}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={left} x2={W - right} y1={y(t)} y2={y(t)} stroke={t === 0 ? c.axis : c.grid} strokeWidth={1} />
            <text x={left - 6} y={y(t) + 3} textAnchor="end" fontSize={10} fill={c.muted}>{compact(t)}{suffix && t !== 0 ? suffix : ''}</text>
          </g>
        ))}
        {hover.i !== null ? <line x1={x(hover.i)} x2={x(hover.i)} y1={top} y2={top + plotH} stroke={c.axis} strokeDasharray="3 3" /> : null}
        {series.map((s, k) => {
          const segs: string[] = []; let cur = '';
          s.values.forEach((v, i) => { if (v === null) { if (cur) segs.push(cur); cur = ''; } else cur += `${cur ? 'L' : 'M'}${x(i)},${y(v)} `; });
          if (cur) segs.push(cur);
          return (
            <g key={s.key}>
              {segs.map((d, j) => <path key={j} d={d} fill="none" stroke={colorOf(s, k)} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />)}
              {s.values.map((v, i) => (v === null ? null : (
                <circle key={i} cx={x(i)} cy={y(v)} r={hover.i === i ? 5 : 4} fill={colorOf(s, k)} stroke={c.surface} strokeWidth={2} />
              )))}
            </g>
          );
        })}
        {labels.map((l, i) => (i % every === 0 ? <text key={i} x={x(i)} y={height - 8} textAnchor="middle" fontSize={10} fill={c.muted}>{l}</text> : null))}
      </svg>
      {hover.i !== null ? (
        <HoverTip x={(x(hover.i) / W) * 100} y={top + 4}>
          <TipRows title={labels[hover.i]!} format={fmt} rows={series.map((s, k) => ({ label: s.label, value: s.values[hover.i!] ?? null, color: colorOf(s, k) }))} />
        </HoverTip>
      ) : null}
    </Box>
  );
}

/** Part-to-whole for a few categories: a ring with a 2px surface gap between segments, the total in the middle and a labelled legend. */
export function Donut({ data, size = 160, centerLabel, ariaLabel }: { data: Array<{ label: string; count: number }>; size?: number; centerLabel: string; ariaLabel: string }) {
  const c = useVizColors();
  const [hover, setHover] = useState<number | null>(null);
  const total = data.reduce((s, d) => s + d.count, 0);
  const r = 60; const stroke = 18; const circ = 2 * Math.PI * r;
  let offset = 0;
  return (
    <Group gap="lg" wrap="wrap" align="center">
      <svg viewBox="0 0 160 160" width={size} height={size} role="img" aria-label={ariaLabel} style={{ flex: 'none' }}>
        <circle cx={80} cy={80} r={r} fill="none" stroke={c.grid} strokeWidth={stroke} />
        {total > 0 ? data.map((d, i) => {
          if (!d.count) return null;
          const len = (d.count / total) * circ;
          const gap = data.filter((x) => x.count > 0).length > 1 ? 2 : 0;
          const el = (
            <circle key={d.label} cx={80} cy={80} r={r} fill="none" stroke={c.series[i % c.series.length]} strokeWidth={hover === i ? stroke + 4 : stroke}
              strokeDasharray={`${Math.max(0, len - gap)} ${circ}`} strokeDashoffset={-offset} transform="rotate(-90 80 80)"
              onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} style={{ cursor: 'default' }}>
              <title>{`${d.label}: ${d.count}`}</title>
            </circle>
          );
          offset += len;
          return el;
        }) : null}
        <text x={80} y={78} textAnchor="middle" fontSize={22} fontWeight={700} fill={c.text}>{hover !== null ? data[hover]!.count : total}</text>
        <text x={80} y={96} textAnchor="middle" fontSize={10} fill={c.muted}>{hover !== null ? data[hover]!.label : centerLabel}</text>
      </svg>
      <Stack gap={4} style={{ flex: 1, minWidth: 140 }}>
        {data.map((d, i) => (
          <Group key={d.label} gap={8} wrap="nowrap" onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
            <span aria-hidden style={{ width: 10, height: 10, borderRadius: 3, background: c.series[i % c.series.length], flex: 'none' }} />
            <Text size="sm" style={{ flex: 1 }}>{d.label}</Text>
            <Text size="sm" fw={600} className="num">{d.count}</Text>
            <Text size="xs" c="dimmed" w={36} ta="right" className="num">{total ? `${Math.round((d.count / total) * 100)}%` : '—'}</Text>
          </Group>
        ))}
      </Stack>
    </Group>
  );
}

/** Ranked categories as horizontal bars with the value written at the end of each bar. */
export function HBars({ data, format, color, max: maxRows = 10 }: { data: Array<{ label: string; value: number }>; format?: (n: number) => string; color?: string; max?: number }) {
  const c = useVizColors();
  const rows = data.slice(0, maxRows);
  const max = Math.max(1, ...rows.map((d) => d.value));
  const fmt = format ?? ((n: number) => n.toLocaleString('en-US'));
  return (
    <Stack gap={8}>
      {rows.map((d) => (
        <div key={d.label} title={`${d.label}: ${fmt(d.value)}`}>
          <Group justify="space-between" gap="xs" wrap="nowrap" mb={3}>
            <Text size="xs" truncate>{d.label}</Text>
            <Text size="xs" fw={600} className="num" style={{ flex: 'none' }}>{fmt(d.value)}</Text>
          </Group>
          <div style={{ height: 8, borderRadius: 4, background: c.grid }}>
            <div style={{ width: `${Math.max(1.5, (d.value / max) * 100)}%`, height: 8, borderRadius: 4, background: color ?? c.series[0] }} />
          </div>
        </div>
      ))}
      {data.length > maxRows ? <Text size="xs" c="dimmed">and {data.length - maxRows} more (see the table view)</Text> : null}
    </Stack>
  );
}
