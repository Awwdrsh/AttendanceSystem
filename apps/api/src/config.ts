import 'dotenv/config';
import { z } from 'zod';

const schema = z.object({
  PORT: z.coerce.number().int().positive().default(3000),
  DATABASE_URL: z.string().min(1),
  LOG_LEVEL: z.enum(['fatal', 'error', 'warn', 'info', 'debug', 'trace']).default('info'),
  /** base64 of 32 random bytes; encrypts PAN, bank accounts, biometric templates. */
  DATA_ENCRYPTION_KEY: z.string().min(40),
  /** Server-side secret mixed into password hashes. */
  PASSWORD_PEPPER: z.string().min(16),
  /** Set true behind HTTPS so the session cookie is Secure. */
  COOKIE_SECURE: z.enum(['true', 'false']).default('false').transform((v) => v === 'true'),
  /** Run the background worker (recompute queue, nightly sweep, notifications) inside this process. */
  RUN_WORKER: z.enum(['true', 'false']).default('true').transform((v) => v === 'true'),
  /** Comma-separated origins allowed for the browser app (CORS). Empty = same-origin only. */
  CORS_ORIGINS: z.string().default(''),
  /** Where the web app build lives when served by the API (optional). */
  WEB_DIST: z.string().optional(),
  SMTP_URL: z.string().optional(),
  SMS_WEBHOOK_URL: z.string().optional(),
});

export type Config = z.infer<typeof schema>;

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const r = schema.safeParse(env);
  if (!r.success) {
    throw new Error(`Invalid environment: ${r.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ')}`);
  }
  return r.data;
}
