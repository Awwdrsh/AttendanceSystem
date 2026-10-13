import { randomUUID } from 'node:crypto';
import type { NormalizedPunch } from '../punch/event.js';
import {
  assertAllowedCommand, COMMAND_ACK_TIMEOUT_MS, COMMAND_MAX_ATTEMPTS, DEFAULT_MAX_PENDING, NEAR_DUPLICATE_WINDOW_MS,
  type DeviceRecord, type DeviceTouch, type InsertOutcome, type Store, type StoreOptions, type TemplateRecord,
} from './types.js';

export interface MemoryCommand {
  id: number; deviceId: string; command: string; status: 'queued' | 'sent' | 'acked' | 'failed'; returnCode?: number; attempts: number; sentAt?: Date;
}

/** In-memory store for tests and simulator runs. */
export class MemoryStore implements Store {
  devices = new Map<string, DeviceRecord & DeviceTouch>();
  punches: NormalizedPunch[] = [];
  templates: TemplateRecord[] = [];
  recomputeQueue: Array<{ employeeId: string; localDate: string; reason: string }> = [];
  mappings = new Map<string, string>(); // `${deviceId}:${pin}` -> employeeId
  commands: MemoryCommand[] = [];
  private nextCmd = 1;

  constructor(readonly opts: StoreOptions = {}) {}

  addDevice(d: Partial<DeviceRecord> & { serialNo: string }): DeviceRecord {
    const rec: DeviceRecord = { id: randomUUID(), vendor: 'zkteco', protocol: 'adms', status: 'active', tzOffsetMin: 345, attlogStamp: '0', oplogStamp: '0', commKeyHash: null, ...d };
    this.devices.set(rec.serialNo, rec);
    return rec;
  }

  async getDevice(serialNo: string) { return this.devices.get(serialNo) ?? null; }

  async registerPending(serialNo: string, info: { vendor: string; protocol: string; pushVersion?: string }) {
    const existing = this.devices.get(serialNo);
    if (existing) return existing;
    const pending = [...this.devices.values()].filter((d) => d.status === 'pending').length;
    if (pending >= (this.opts.maxPendingDevices ?? DEFAULT_MAX_PENDING)) return null;
    return this.addDevice({ serialNo, vendor: info.vendor, protocol: info.protocol, status: 'pending', ...(info.pushVersion ? { pushVersion: info.pushVersion } : {}) });
  }

  async touch(serialNo: string, patch: DeviceTouch) {
    const d = this.devices.get(serialNo);
    if (d) Object.assign(d, patch);
  }

  async resolveEmployee(deviceId: string, pin: string) { return this.mappings.get(`${deviceId}:${pin}`) ?? null; }

  async insertPunch(p: NormalizedPunch): Promise<InsertOutcome> {
    if (this.punches.some((q) => q.idempotencyKey === p.idempotencyKey)) return 'duplicate';
    const near = this.punches.some((q) => q.deviceId === p.deviceId && q.deviceUserPin === p.deviceUserPin && q.verifyMode === p.verifyMode
      && Math.abs(q.punchedAt.getTime() - p.punchedAt.getTime()) <= NEAR_DUPLICATE_WINDOW_MS);
    if (near) return 'near_duplicate';
    this.punches.push(p);
    return 'inserted';
  }

  async enqueueRecompute(employeeId: string, localDate: string, reason: string) {
    if (!this.recomputeQueue.some((q) => q.employeeId === employeeId && q.localDate === localDate)) this.recomputeQueue.push({ employeeId, localDate, reason });
  }

  async insertTemplate(t: TemplateRecord) { this.templates.push(t); }

  async enqueueCommand(deviceId: string, command: string) {
    assertAllowedCommand(command, this.opts.allowDangerousCommands);
    const id = this.nextCmd++;
    this.commands.push({ id, deviceId, command, status: 'queued', attempts: 0 });
    return id;
  }

  async popQueuedCommands(deviceId: string, now = new Date()) {
    for (const c of this.commands) {
      if (c.deviceId === deviceId && c.status === 'sent' && c.sentAt && now.getTime() - c.sentAt.getTime() > COMMAND_ACK_TIMEOUT_MS) {
        c.status = c.attempts < COMMAND_MAX_ATTEMPTS ? 'queued' : 'failed';
      }
    }
    const q = this.commands.filter((c) => c.deviceId === deviceId && c.status === 'queued');
    for (const c of q) { c.status = 'sent'; c.sentAt = now; c.attempts++; }
    return q.map((c) => ({ id: c.id, command: c.command }));
  }

  async ackCommand(id: number, returnCode: number) {
    const c = this.commands.find((x) => x.id === id);
    if (c && c.status === 'sent') { c.status = returnCode >= 0 ? 'acked' : 'failed'; c.returnCode = returnCode; }
  }

  async listDevices(filter: { status?: DeviceRecord['status']; protocol?: string } = {}) {
    return [...this.devices.values()].filter((d) => (!filter.status || d.status === filter.status) && (!filter.protocol || d.protocol === filter.protocol));
  }

  async countPunches(deviceId: string, localDate: string) {
    return this.punches.filter((p) => p.deviceId === deviceId && p.localDate === localDate).length;
  }
}
