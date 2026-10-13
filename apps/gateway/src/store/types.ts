import type { NormalizedPunch } from '../punch/event.js';

export interface DeviceRecord {
  id: string;
  serialNo: string;
  vendor: string;
  status: 'pending' | 'active' | 'disabled';
  tzOffsetMin: number;
  attlogStamp: string;
  oplogStamp: string;
}

export interface DeviceTouch {
  lastSeenAt?: Date;
  lastHandshakeAt?: Date;
  pushVersion?: string;
  fwVersion?: string;
  attlogStamp?: string;
  oplogStamp?: string;
}

export type InsertOutcome = 'inserted' | 'duplicate' | 'near_duplicate';

export interface Store {
  getDevice(serialNo: string): Promise<DeviceRecord | null>;
  /** Unknown SN: record it as pending so an admin can approve it; no options are sent until then. */
  registerPending(serialNo: string, info: { vendor: string; protocol: string; pushVersion?: string }): Promise<DeviceRecord>;
  touch(serialNo: string, patch: DeviceTouch): Promise<void>;
  resolveEmployee(deviceId: string, pin: string, localDate: string): Promise<string | null>;
  /** Exact duplicate (idempotency key) or near duplicate (same device+pin+mode within ±5 s) are both ignored. */
  insertPunch(p: NormalizedPunch): Promise<InsertOutcome>;
  enqueueCommand(deviceId: string, command: string): Promise<number>;
  popQueuedCommands(deviceId: string): Promise<Array<{ id: number; command: string }>>;
  ackCommand(id: number, returnCode: number): Promise<void>;
}

export const NEAR_DUPLICATE_WINDOW_MS = 5_000;

/** The ADMS SHELL command is remote code execution on the terminal. It is never emitted (plan Appendix B). */
export function assertAllowedCommand(command: string): void {
  if (/^\s*SHELL/i.test(command)) throw new Error('SHELL commands are forbidden');
}
