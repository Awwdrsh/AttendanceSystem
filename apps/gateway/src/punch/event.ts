import { canonicalJson, localDateOf, sha256Hex, MIN_MS } from '@hajiri/attendance-engine';
import type { Signer } from './signer.js';

export interface NormalizedPunch {
  deviceId: string;
  deviceSn: string;
  vendor: string;
  deviceUserPin: string;
  employeeId: string | null;
  /** UTC instant. */
  punchedAt: Date;
  deviceLocalTime: string;
  localDate: string;
  direction: 'in' | 'out' | 'unknown';
  verifyMode: string;
  source: string;
  raw: Record<string, unknown>;
  idempotencyKey: string;
  gatewaySignature: string;
}

/** 'YYYY-MM-DD HH:MM:SS' in the device's fixed offset -> UTC instant. */
export function deviceTimeToUtc(deviceLocalTime: string, tzOffsetMin: number): Date {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(deviceLocalTime);
  if (!m) throw new RangeError(`bad device time ${deviceLocalTime}`);
  const [y, mo, d, h, mi, sec] = m.slice(1).map(Number) as [number, number, number, number, number, number];
  const asUtc = Date.UTC(y, mo - 1, d, h, mi, sec);
  const t = new Date(asUtc);
  if (mo < 1 || mo > 12 || h > 23 || mi > 59 || sec > 59 || t.getUTCMonth() !== mo - 1 || t.getUTCDate() !== d) throw new RangeError(`invalid device time ${deviceLocalTime}`);
  return new Date(asUtc - tzOffsetMin * MIN_MS);
}

/** plan §7.7: sha256(vendor ‖ device_sn ‖ device_user_pin ‖ device_local_time ‖ verify_mode). */
export function idempotencyKey(p: { vendor: string; deviceSn: string; deviceUserPin: string; deviceLocalTime: string; verifyMode: string }): string {
  return sha256Hex([p.vendor, p.deviceSn, p.deviceUserPin, p.deviceLocalTime, p.verifyMode].join('|'));
}

export function buildPunch(
  base: Omit<NormalizedPunch, 'punchedAt' | 'localDate' | 'idempotencyKey' | 'gatewaySignature'>,
  tzOffsetMin: number,
  signer: Signer,
): NormalizedPunch {
  const punchedAt = deviceTimeToUtc(base.deviceLocalTime, tzOffsetMin);
  const key = idempotencyKey(base);
  const canonical = canonicalJson({
    vendor: base.vendor, deviceSn: base.deviceSn, deviceUserPin: base.deviceUserPin, deviceLocalTime: base.deviceLocalTime,
    punchedAt: punchedAt.toISOString(), direction: base.direction, verifyMode: base.verifyMode, idempotencyKey: key,
  });
  return { ...base, punchedAt, localDate: localDateOf(punchedAt.getTime()), idempotencyKey: key, gatewaySignature: signer.sign(canonical) };
}
