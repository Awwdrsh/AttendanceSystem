// Reads ciphertexts and hashes made by server/hajiri/core/crypto.py and checks the TS helpers accept them.
import { readFileSync } from 'node:fs';
import { FieldCipher, verifyPassword } from './crypto.js';

const f = JSON.parse(readFileSync(process.argv[2]!, 'utf8'));
const c = FieldCipher.fromBase64(f.key);
const bad: string[] = [];
for (const x of f.cipher) if (c.decrypt(Buffer.from(x.blob, 'base64'), x.aad) !== x.plain) bad.push(`decrypt ${x.plain}`);
for (const x of f.passwords) if (!(await verifyPassword(x.password, x.hash, f.pepper))) bad.push(`verify ${x.password}`);
console.log(bad.length ? `MISMATCH ${bad.join(', ')}` : 'OK');
