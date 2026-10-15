import { useEffect, useState } from 'react';
import { regularisation as regApi } from '../api';
import { ApiError } from '../api/client';
import { fmtTime, todayNpt } from './format';

/**
 * Clock-ins made without a connection. A punch always takes the server's time, so an offline tap cannot become a
 * punch; it is kept on this device and, once back online, sent as a missed-punch correction with the time on the phone.
 * The manager approves it like any other correction.
 */
export interface OfflinePunch { id: string; direction: 'in' | 'out'; at: string; localDate: string; userId: string }

const KEY = 'hajiri.offlinePunches';
const EVENT = 'hajiri:offline-punches';

export function queuedPunches(userId?: string): OfflinePunch[] {
  try {
    const all = JSON.parse(localStorage.getItem(KEY) ?? '[]') as OfflinePunch[];
    return Array.isArray(all) ? all.filter((p) => !userId || p.userId === userId) : [];
  } catch { return []; }
}

function save(list: OfflinePunch[]): void {
  localStorage.setItem(KEY, JSON.stringify(list));
  window.dispatchEvent(new Event(EVENT));
}

export function queueOfflinePunch(userId: string, direction: 'in' | 'out'): OfflinePunch {
  const p: OfflinePunch = { id: crypto.randomUUID(), direction, at: new Date().toISOString(), localDate: todayNpt(), userId };
  save([...queuedPunches(), p].slice(-20));
  return p;
}

export function isOfflineError(e: unknown): boolean {
  return e instanceof ApiError && e.status === 0;
}

/**
 * Send what is waiting. Returns how many were sent and the ones the server refused (they are dropped with the reason;
 * a network failure keeps them for the next try).
 */
export async function flushOfflinePunches(userId: string): Promise<{ sent: number; refused: Array<{ punch: OfflinePunch; message: string }> }> {
  let sent = 0; const refused: Array<{ punch: OfflinePunch; message: string }> = [];
  for (const p of queuedPunches(userId)) {
    try {
      await regApi.create({
        localDate: p.localDate, kind: 'missed_punch', punchTime: p.at, punchDirection: p.direction,
        reason: `Clocked ${p.direction} at ${fmtTime(p.at)} while offline (time from my phone); sent automatically when the connection came back.`,
      });
      sent++;
      save(queuedPunches().filter((x) => x.id !== p.id));
    } catch (e) {
      if (isOfflineError(e)) break;
      refused.push({ punch: p, message: e instanceof Error ? e.message : 'refused' });
      save(queuedPunches().filter((x) => x.id !== p.id));
    }
  }
  return { sent, refused };
}

export function useOnline(): boolean {
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine);
  useEffect(() => {
    const on = () => setOnline(true); const off = () => setOnline(false);
    window.addEventListener('online', on); window.addEventListener('offline', off);
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off); };
  }, []);
  return online;
}

export function useQueuedPunches(userId?: string): OfflinePunch[] {
  const [list, setList] = useState(() => queuedPunches(userId));
  useEffect(() => {
    const update = () => setList(queuedPunches(userId));
    update();
    window.addEventListener(EVENT, update); window.addEventListener('storage', update);
    return () => { window.removeEventListener(EVENT, update); window.removeEventListener('storage', update); };
  }, [userId]);
  return list;
}
