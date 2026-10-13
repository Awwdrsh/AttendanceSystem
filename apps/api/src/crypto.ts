import { createCipheriv, createDecipheriv, createHmac, randomBytes, scrypt as scryptCb, timingSafeEqual } from 'node:crypto';

function scrypt(password: string, salt: Buffer, keylen: number, opts: { N: number; r: number; p: number; maxmem: number }): Promise<Buffer> {
  return new Promise((resolve, reject) => scryptCb(password, salt, keylen, opts, (err, key) => (err ? reject(err) : resolve(key))));
}

/**
 * Field-level encryption for PAN, bank accounts and biometric templates (plan §12.2, §9.1 decision 5).
 * AES-256-GCM with a 32-byte data-encryption key from DATA_ENCRYPTION_KEY (base64). Layout:
 *   version(1) | iv(12) | tag(16) | ciphertext
 * Tier 1 of the plan wraps per-record DEKs with an OpenBao/Vault KEK; this implementation uses one versioned key so the
 * storage format is already envelope-shaped (key_version) and can be re-wrapped later without a schema change.
 */
export class FieldCipher {
  constructor(private readonly key: Buffer, readonly keyVersion = 1) {
    if (key.length !== 32) throw new Error('DATA_ENCRYPTION_KEY must be 32 bytes (base64 of 32 random bytes)');
  }

  static fromBase64(b64: string): FieldCipher { return new FieldCipher(Buffer.from(b64, 'base64')); }

  encrypt(plaintext: string, aad: string): Buffer {
    const iv = randomBytes(12);
    const c = createCipheriv('aes-256-gcm', this.key, iv);
    c.setAAD(Buffer.from(aad, 'utf8'));
    const ct = Buffer.concat([c.update(plaintext, 'utf8'), c.final()]);
    return Buffer.concat([Buffer.from([this.keyVersion]), iv, c.getAuthTag(), ct]);
  }

  decrypt(blob: Buffer, aad: string): string {
    if (blob.length < 29) throw new Error('ciphertext too short');
    const iv = blob.subarray(1, 13);
    const tag = blob.subarray(13, 29);
    const ct = blob.subarray(29);
    const d = createDecipheriv('aes-256-gcm', this.key, iv);
    d.setAAD(Buffer.from(aad, 'utf8'));
    d.setAuthTag(tag);
    return Buffer.concat([d.update(ct), d.final()]).toString('utf8');
  }

  /** Deterministic blind index for equality lookups on encrypted fields (e.g. PAN). */
  blindIndex(value: string): string {
    return createHmac('sha256', this.key).update('bidx:' + value.trim().toUpperCase()).digest('hex').slice(0, 32);
  }
}

// ---------------------------------------------------------------- passwords
// scrypt (Node built-in, no native dependency) with a server-side pepper; parameters stored with the hash.
const SCRYPT_N = 1 << 15; const SCRYPT_R = 8; const SCRYPT_P = 1; const KEYLEN = 64;

export async function hashPassword(password: string, pepper = ''): Promise<string> {
  const salt = randomBytes(16);
  const hash = await scrypt(password + pepper, salt, KEYLEN, { N: SCRYPT_N, r: SCRYPT_R, p: SCRYPT_P, maxmem: 128 * 1024 * 1024 });
  return `scrypt$${SCRYPT_N}$${SCRYPT_R}$${SCRYPT_P}$${salt.toString('base64')}$${hash.toString('base64')}`;
}

export async function verifyPassword(password: string, stored: string, pepper = ''): Promise<boolean> {
  const [alg, n, r, p, saltB64, hashB64] = stored.split('$');
  if (alg !== 'scrypt' || !n || !r || !p || !saltB64 || !hashB64) return false;
  const expected = Buffer.from(hashB64, 'base64');
  const actual = await scrypt(password + pepper, Buffer.from(saltB64, 'base64'), expected.length,
    { N: Number(n), r: Number(r), p: Number(p), maxmem: 128 * 1024 * 1024 });
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

/** NIST SP 800-63B-4: length over composition. ≥ 12 chars here (plan §12.4 asks ≥ 15 single-factor; MFA comes later). */
export function passwordPolicyError(pw: string): string | null {
  if (pw.length < 12) return 'Password must be at least 12 characters';
  if (pw.length > 128) return 'Password must be at most 128 characters';
  if (/^(.)\1+$/.test(pw)) return 'Password is too repetitive';
  const common = ['password1234', 'hajiri123456', '123456789012', 'qwertyuiop12'];
  if (common.includes(pw.toLowerCase())) return 'Password is too common';
  return null;
}

export function randomToken(bytes = 32): string { return randomBytes(bytes).toString('base64url'); }
export function sha256Hex(s: string): string { return createHmac('sha256', 'hajiri-session').update(s).digest('hex'); }
