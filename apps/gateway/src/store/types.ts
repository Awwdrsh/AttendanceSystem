import type { NormalizedPunch } from '../punch/event.js';

export interface DeviceRecord {
  id: string;
  serialNo: string;
  vendor: string;
  protocol: string;
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
  /** device clock minus server clock, seconds (plan §7.7 drift alert at > 60 s). */
  clockDriftSec?: number;
}

export type InsertOutcome = 'inserted' | 'duplicate' | 'near_duplicate';

export interface TemplateRecord {
  deviceId: string;
  deviceUserPin: string;
  employeeId: string | null;
  modality: 'fingerprint' | 'face' | 'palm' | 'unknown';
  vendorFormat: string;
  ciphertext: Buffer;
  keyVersion: number;
  aadHash: string;
}

export interface Store {
  getDevice(serialNo: string): Promise<DeviceRecord | null>;
  /** Unknown SN: park as pending for admin approval. Returns null when the pending cap is reached. */
  registerPending(serialNo: string, info: { vendor: string; protocol: string; pushVersion?: string }): Promise<DeviceRecord | null>;
  touch(serialNo: string, patch: DeviceTouch): Promise<void>;
  resolveEmployee(deviceId: string, pin: string, localDate: string): Promise<string | null>;
  /** Exact duplicate (idempotency key) or near duplicate (same device+pin+mode within ±5 s) are both ignored. */
  insertPunch(p: NormalizedPunch): Promise<InsertOutcome>;
  /** Ask the API worker to recompute the day (plan §10.3). Idempotent. */
  enqueueRecompute(employeeId: string, localDate: string, reason: string): Promise<void>;
  insertTemplate(t: TemplateRecord): Promise<void>;
  enqueueCommand(deviceId: string, command: string): Promise<number>;
  popQueuedCommands(deviceId: string): Promise<Array<{ id: number; command: string }>>;
  ackCommand(id: number, returnCode: number): Promise<void>;
  listDevices(filter?: { status?: DeviceRecord['status']; protocol?: string }): Promise<DeviceRecord[]>;
  countPunches(deviceId: string, localDate: string): Promise<number>;
}

export interface StoreOptions {
  maxPendingDevices?: number;
  allowDangerousCommands?: boolean;
}

export const NEAR_DUPLICATE_WINDOW_MS = 5_000;
export const DEFAULT_MAX_PENDING = 50;

/**
 * Command allow-list (plan Appendix B, §12.5): SHELL is remote code execution and is never emitted. CLEAR DATA and
 * firmware/file transfer verbs wipe or reprogram the terminal and need an explicit opt-in.
 */
export function assertAllowedCommand(command: string, allowDangerous = false): void {
  if (/^\s*SHELL\b/i.test(command)) throw new Error('SHELL commands are forbidden');
  if (!allowDangerous && /^\s*(CLEAR\s+DATA|CLEAR\s+ALL|UPGRADE|UPDATE\s+FIRMWARE|PUTFILE|GETFILE|RELOAD\s+OPTIONS|SET\s+OPTION\s+WebServerURL)/i.test(command)) {
    throw new Error('dangerous command refused (set GATEWAY_ALLOW_DANGEROUS_COMMANDS=true to allow)');
  }
}
