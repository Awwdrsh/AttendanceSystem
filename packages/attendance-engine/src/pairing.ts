import type { Pair, PairingMode, Punch } from './types.js';

export interface PairingResult {
  pairs: Pair[];
  /** Punches that could not be paired, in order. The last one being an IN means a missing OUT. */
  unpaired: Punch[];
}

/**
 * Pair ordered punches into IN/OUT intervals.
 * - alternating: IN, OUT, IN, OUT... regardless of device direction.
 * - strict_log_type: honour device direction; a second IN while one is open is ignored (first IN wins);
 *   an OUT with nothing open is ignored. Punches with unknown direction fall back to alternating roles.
 * - first_last: one pair from the first and last punch (breaks included).
 */
export function pairPunches(punches: readonly Punch[], mode: PairingMode): PairingResult {
  const pairs: Pair[] = [];
  const unpaired: Punch[] = [];
  const list = [...punches].sort((a, b) => a.at - b.at || a.id.localeCompare(b.id));

  if (mode === 'first_last') {
    if (list.length >= 2) {
      const first = list[0]!;
      const last = list[list.length - 1]!;
      pairs.push({ inMs: first.at, outMs: last.at, inId: first.id, outId: last.id });
    } else if (list.length === 1) {
      unpaired.push(list[0]!);
    }
    return { pairs, unpaired };
  }

  if (mode === 'alternating') {
    for (let i = 0; i < list.length; i += 2) {
      const a = list[i]!;
      const b = list[i + 1];
      if (b) pairs.push({ inMs: a.at, outMs: b.at, inId: a.id, outId: b.id });
      else unpaired.push(a);
    }
    return { pairs, unpaired };
  }

  // strict_log_type
  let open: Punch | null = null;
  for (const p of list) {
    const role = p.direction === 'unknown' ? (open ? 'out' : 'in') : p.direction;
    if (role === 'in') {
      if (open) unpaired.push(p);
      else open = p;
    } else if (open) {
      pairs.push({ inMs: open.at, outMs: p.at, inId: open.id, outId: p.id });
      open = null;
    } else {
      unpaired.push(p);
    }
  }
  if (open) unpaired.push(open);
  return { pairs, unpaired };
}
