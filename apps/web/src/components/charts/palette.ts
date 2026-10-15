import { useComputedColorScheme } from '@mantine/core';

/**
 * Chart colours. Series identity uses a fixed categorical order (never cycled); slot 1 is the CoreValley hydro green so
 * single-series charts match the brand. Dark mode uses its own steps chosen for the dark surface, not an inversion.
 */
const LIGHT = ['#12773a', '#2a78d6', '#eb6834', '#6250d6', '#eda100', '#e87ba4', '#1baf7a', '#e34948'];
const DARK = ['#2f9e5c', '#3987e5', '#d95926', '#9085e9', '#c98500', '#d55181', '#199e70', '#e66767'];

export interface VizColors { series: string[]; grid: string; axis: string; text: string; muted: string; surface: string }

export function useVizColors(): VizColors {
  const dark = useComputedColorScheme('light') === 'dark';
  return {
    series: dark ? DARK : LIGHT,
    grid: 'var(--hj-line-soft)',
    axis: 'var(--hj-line)',
    text: 'var(--mantine-color-text)',
    muted: 'var(--hj-muted)',
    surface: 'var(--hj-surface)',
  };
}

/** Compact axis numbers: 1.2K, 3.4L (lakh) and 1.1Cr (crore) for rupee amounts. */
export function compact(n: number, money = false): string {
  const a = Math.abs(n);
  if (money) {
    if (a >= 1e7) return `${(n / 1e7).toFixed(a >= 1e8 ? 0 : 1)}Cr`;
    if (a >= 1e5) return `${(n / 1e5).toFixed(a >= 1e6 ? 0 : 1)}L`;
    if (a >= 1e3) return `${(n / 1e3).toFixed(0)}K`;
    return String(Math.round(n));
  }
  if (a >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `${(n / 1e3).toFixed(0)}K`;
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

/** A "nice" axis maximum and three evenly spaced ticks above zero. */
export function niceScale(max: number): { top: number; ticks: number[] } {
  if (!(max > 0)) return { top: 1, ticks: [0, 1] };
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((s) => s >= raw) ?? raw;
  const top = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let t = 0; t <= top + step / 2; t += step) ticks.push(Math.round(t * 1000) / 1000);
  return { top, ticks };
}
