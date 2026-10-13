import type { Logger } from 'pino';
import type { FieldCipher } from './crypto.js';
import type { Db } from './db/pool.js';

/** Shared dependencies handed to every module router. */
export interface AppDeps {
  db: Db;
  cipher: FieldCipher;
  pepper: string;
  logger?: Logger;
  secureCookie: boolean;
}
