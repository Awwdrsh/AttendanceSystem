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
  const asUtc = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]), Number(m[6]));
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
