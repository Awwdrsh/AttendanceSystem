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
  /** sha256 hex of a per-device shared secret; when set, non-ADMS devices must present it (plan §12.5). */
  commKeyHash: string | null;
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
  /**
   * Hand queued commands to the device and mark them sent. Commands sent but not acknowledged within
   * COMMAND_ACK_TIMEOUT_MS are re-sent once, then marked failed (devices lose polls across reboots/outages).
   */
  popQueuedCommands(deviceId: string, now?: Date): Promise<Array<{ id: number; command: string }>>;
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
export const COMMAND_ACK_TIMEOUT_MS = 10 * 60_000;
export const COMMAND_MAX_ATTEMPTS = 2;

/** Strip characters a terminal might ignore but a regex would not see: controls, zero-width and BOM. */
function normaliseCommand(command: string): string {
  return command.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F​-‏⁠﻿]/g, '');
}

/**
 * Command allow-list (plan Appendix B, §12.5): SHELL is remote code execution and is never emitted. CLEAR DATA and
 * firmware/file transfer verbs wipe or reprogram the terminal and need an explicit opt-in. A command may never contain
 * a line break: the poll response is newline-delimited, so an embedded newline would smuggle a second command past
 * every check (including the database CHECK constraint, which only sees the first line).
 */
export function assertAllowedCommand(command: string, allowDangerous = false): void {
  if (/[\r\n]/.test(command)) throw new Error('commands must be a single line');
  if (command.length > 4096) throw new Error('command too long');
  const c = normaliseCommand(command);
  if (!c.trim()) throw new Error('empty command');
  if (/^\s*SHELL\b/i.test(c) || /^\s*SHELL/i.test(c)) throw new Error('SHELL commands are forbidden');
  if (!allowDangerous && /^\s*(CLEAR\s+DATA|CLEAR\s+ALL|UPGRADE|UPDATE\s+FIRMWARE|PUTFILE|GETFILE|RELOAD\s+OPTIONS|SET\s+OPTIONS?\s+WebServerURL|CONTROL\s+DEVICE)/i.test(c)) {
    throw new Error('dangerous command refused (set GATEWAY_ALLOW_DANGEROUS_COMMANDS=true to allow)');
  }
}
