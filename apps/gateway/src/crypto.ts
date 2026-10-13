import { createCipheriv, createHash, randomBytes } from 'node:crypto';

/**
 * Template encryption in exactly the API's FieldCipher format (apps/api/src/crypto.ts):
 *   version(1) | iv(12) | tag(16) | ciphertext, AES-256-GCM, plaintext = base64 text of the template bytes.
 * Storing the base64 text (not raw bytes) keeps the two implementations byte-compatible: the API decrypts to the
 * same base64 string. plan §9.1 decision 5, §12.3.
 */
export class TemplateCipher {
  constructor(private readonly key: Buffer, readonly keyVersion = 1) {
    if (key.length !== 32) throw new Error('DATA_ENCRYPTION_KEY must be 32 bytes (base64 of 32 random bytes)');
  }

  static fromBase64(b64: string): TemplateCipher { return new TemplateCipher(Buffer.from(b64, 'base64')); }

  encrypt(plaintext: string, aad: string): Buffer {
    const iv = randomBytes(12);
    const c = createCipheriv('aes-256-gcm', this.key, iv);
    c.setAAD(Buffer.from(aad, 'utf8'));
    const ct = Buffer.concat([c.update(plaintext, 'utf8'), c.final()]);
    return Buffer.concat([Buffer.from([this.keyVersion]), iv, c.getAuthTag(), ct]);
  }
}

export function templateAad(subject: string, modality: string): string {
  return `template:${subject}:${modality}:attendance`;
}

export function aadHash(aad: string): string {
  return createHash('sha256').update(aad, 'utf8').digest('hex');
}
