import { createPrivateKey, createPublicKey, generateKeyPairSync, sign, type KeyObject } from 'node:crypto';

/** Ed25519 signer for canonical punch events (plan §8.3: "signs the canonical event with the gateway key"). */
export class Signer {
  private constructor(private readonly key: KeyObject, readonly publicKeyPem: string, readonly ephemeral: boolean) {}

  static fromBase64Pkcs8(b64: string): Signer {
    const key = createPrivateKey({ key: Buffer.from(b64, 'base64'), format: 'der', type: 'pkcs8' });
    if (key.asymmetricKeyType !== 'ed25519') throw new Error('GATEWAY_SIGNING_KEY must be an Ed25519 key');
    return new Signer(key, createPublicKey(key).export({ type: 'spki', format: 'pem' }).toString(), false);
  }

  static ephemeral(): Signer {
    const { privateKey } = generateKeyPairSync('ed25519');
    return new Signer(privateKey, createPublicKey(privateKey).export({ type: 'spki', format: 'pem' }).toString(), true);
  }

  sign(canonical: string): string {
    return sign(null, Buffer.from(canonical, 'utf8'), this.key).toString('base64');
  }
}
