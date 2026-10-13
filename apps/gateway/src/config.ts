import 'dotenv/config';
import { z } from 'zod';

const bool = z.enum(['true', 'false']).default('false').transform((v) => v === 'true');

const schema = z.object({
  PORT: z.coerce.number().int().positive().default(8080),
  DATABASE_URL: z.string().min(1).optional(),
  LOG_LEVEL: z.enum(['fatal', 'error', 'warn', 'info', 'debug', 'trace']).default('info'),
  GATEWAY_SIGNING_KEY: z.string().optional(),
  /** Same key as the API (base64 of 32 bytes). Templates are discarded when absent; never stored in plaintext. */
  DATA_ENCRYPTION_KEY: z.string().min(40).optional(),
  /** Unknown terminals are parked as pending; beyond this many pending rows new ones are refused (plan §12.5 rogue devices). */
  GATEWAY_MAX_PENDING_DEVICES: z.coerce.number().int().positive().default(50),
  /** Punches further than this in the future are flagged clock_skew (plan §7.7, §12.5 skew window). */
  GATEWAY_MAX_CLOCK_SKEW_MIN: z.coerce.number().int().positive().default(10),
  /** Raw request archive directory (plan §8.2 "raw payload archive"). */
  GATEWAY_RAW_DIR: z.string().default('data/raw'),
  /** CLEAR DATA / firmware commands are refused unless explicitly enabled. SHELL is always refused. */
  GATEWAY_ALLOW_DANGEROUS_COMMANDS: bool,
});

export type Config = z.infer<typeof schema>;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const r = schema.safeParse(env);
  if (!r.success) throw new Error(`Invalid environment: ${r.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ')}`);
  return r.data;
}
