// Writes ciphertexts, password hashes and tokens from the TS crypto helpers so server/hajiri/core/crypto.py can be
// checked against them. Run: tsx src/dump-crypto-parity.ts <out.json>
import { writeFileSync } from 'node:fs';
import { FieldCipher, hashPassword, sha256Hex } from './crypto.js';

const key = Buffer.alloc(32, 7).toString('base64');
const c = FieldCipher.fromBase64(key);
const values = ['ABCDE1234F', 'नेपाल बैंक 0123456789', '', ' pan-with-space '];
const pepper = 'parity-test-pepper';
const passwords = ['correct horse battery', 'पासवर्ड-१२३४५६७८९', 'x'.repeat(128)];
writeFileSync(process.argv[2]!, JSON.stringify({
  key, pepper,
  cipher: values.map((v) => ({ plain: v, aad: `employee:${v.length}`, blob: c.encrypt(v, `employee:${v.length}`).toString('base64'), bidx: c.blindIndex(v) })),
  passwords: await Promise.all(passwords.map(async (p) => ({ password: p, hash: await hashPassword(p, pepper) }))),
  sha256: values.map((v) => ({ in: v, out: sha256Hex(v) })),
}, null, 1));
