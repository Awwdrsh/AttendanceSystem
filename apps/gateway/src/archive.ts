import { appendFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import path from 'node:path';

const MAX_FILE_BYTES = 50 * 1024 * 1024;

/**
 * Append-only raw payload archive (plan §8.2): data/raw/<SN>/<YYYY-MM-DD>[.n].log. One file per device per day;
 * when a day file passes 50 MB the suffix increments. Bodies are written verbatim; nothing else logs them.
 */
export class RawArchive {
  private readonly disabledSns = new Set<string>();
  constructor(readonly dir: string | null) {}

  append(sn: string, kind: string, body: string | Buffer, now = new Date()): string | null {
    if (!this.dir) return null;
    const safeSn = sn.replace(/[^A-Za-z0-9_.-]/g, '_') || 'unknown';
    const day = now.toISOString().slice(0, 10);
    const folder = path.join(this.dir, safeSn);
    try {
      mkdirSync(folder, { recursive: true });
      let n = 0;
      let file = path.join(folder, `${day}.log`);
      while (existsSync(file) && statSync(file).size >= MAX_FILE_BYTES) {
        n++;
        file = path.join(folder, `${day}.${n}.log`);
      }
      const header = `--- ${now.toISOString()} ${kind} ${Buffer.byteLength(body)}B\n`;
      appendFileSync(file, Buffer.concat([Buffer.from(header), Buffer.isBuffer(body) ? body : Buffer.from(body), Buffer.from('\n')]));
      return file;
    } catch {
      if (!this.disabledSns.has(safeSn)) this.disabledSns.add(safeSn);
      return null;
    }
  }
}
