import { appendFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import path from 'node:path';

const MAX_FILE_BYTES = 50 * 1024 * 1024;
/** At most this many 50 MB files per device per day; beyond it bodies are dropped (disk-fill protection). */
const MAX_FILES_PER_DAY = 5;

/** Serial numbers are used as directory names: only [A-Za-z0-9_-], max 64 chars, never '.' sequences. */
export function safeSerial(sn: string): string {
  const s = sn.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 64);
  return s.replace(/^_+$/, '') || 'unknown';
}

/**
 * Append-only raw payload archive (plan §8.2): data/raw/<SN>/<YYYY-MM-DD>[.n].log. One file per device per day;
 * when a day file passes 50 MB the suffix increments, up to MAX_FILES_PER_DAY. Bodies are written verbatim; nothing
 * else logs them.
 */
export class RawArchive {
  dropped = 0;
  constructor(readonly dir: string | null) {}

  append(sn: string, kind: string, body: string | Buffer, now = new Date()): string | null {
    if (!this.dir) return null;
    const safeSn = safeSerial(sn);
    const day = now.toISOString().slice(0, 10);
    const folder = path.join(this.dir, safeSn);
    if (!path.resolve(folder).startsWith(path.resolve(this.dir) + path.sep)) return null;
    try {
      mkdirSync(folder, { recursive: true });
      let n = 0;
      let file = path.join(folder, `${day}.log`);
      while (existsSync(file) && statSync(file).size >= MAX_FILE_BYTES) {
        n++;
        if (n >= MAX_FILES_PER_DAY) { this.dropped++; return null; }
        file = path.join(folder, `${day}.${n}.log`);
      }
      const header = `--- ${now.toISOString()} ${kind.replace(/[\r\n]/g, ' ')} ${Buffer.byteLength(body)}B\n`;
      appendFileSync(file, Buffer.concat([Buffer.from(header), Buffer.isBuffer(body) ? body : Buffer.from(body), Buffer.from('\n')]));
      return file;
    } catch {
      this.dropped++;
      return null;
    }
  }
}
