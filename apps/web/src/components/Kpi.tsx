import { Paper, SimpleGrid, Stack, Text } from '@mantine/core';
import { useI18n } from '../i18n';
import { fmtNum } from '../lib/format';

export function KpiTiles({ tiles }: { tiles: Array<{ key: string; label: string; value: number | string; hint?: string }> }) {
  const { numerals } = useI18n();
  return (
    <SimpleGrid cols={{ base: 2, sm: 3, lg: Math.min(6, tiles.length) }} spacing="sm">
      {tiles.map((t) => (
        <Paper key={t.key} withBorder p="md" radius="md">
          <Stack gap={2}>
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>{t.label}</Text>
            <Text fz={28} fw={700} lh={1}>{typeof t.value === 'number' ? fmtNum(t.value, numerals) : t.value}</Text>
            {t.hint ? <Text size="xs" c="dimmed">{t.hint}</Text> : null}
          </Stack>
        </Paper>
      ))}
    </SimpleGrid>
  );
}

/** One trend line, drawn as plain SVG bars so no chart library is needed (plan §13.2: bars/lines, never gauges or pies). */
export function TrendBars({ points, label, max = 100 }: { points: Array<{ date: string; value: number | null }>; label: string; max?: number }) {
  const w = 600; const h = 120; const pad = 4;
  const n = Math.max(points.length, 1);
  const bw = (w - pad * 2) / n;
  return (
    <Paper withBorder p="md" radius="md">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600} mb="xs">{label}</Text>
      <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label={label}>
        <line x1={pad} x2={w - pad} y1={h - pad} y2={h - pad} stroke="var(--mantine-color-gray-4)" />
        {points.map((p, i) => {
          const v = p.value ?? 0;
          const bh = Math.max(1, ((h - pad * 2) * v) / max);
          return <rect key={p.date} x={pad + i * bw + 1} y={h - pad - bh} width={Math.max(1, bw - 2)} height={bh} fill={p.value === null ? 'var(--mantine-color-gray-3)' : 'var(--mantine-color-teal-5)'}><title>{`${p.date}: ${p.value ?? '—'}`}</title></rect>;
        })}
      </svg>
      {points.length ? <Text size="xs" c="dimmed">{points[0]!.date} → {points[points.length - 1]!.date}</Text> : <Text size="xs" c="dimmed">No data yet.</Text>}
    </Paper>
  );
}
