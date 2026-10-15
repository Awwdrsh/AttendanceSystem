import { BadRequestError } from '../../errors.js';

export const PHOTO_MAX_BYTES = 300 * 1024;
export type PhotoMime = 'image/jpeg' | 'image/png' | 'image/webp';

/** Magic numbers: the declared type must match the bytes, so nothing but a real image is ever stored or served. */
const SIGNATURES: Record<PhotoMime, (b: Buffer) => boolean> = {
  'image/jpeg': (b) => b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff,
  'image/png': (b) => b.length > 8 && b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])),
  'image/webp': (b) => b.length > 12 && b.subarray(0, 4).toString('latin1') === 'RIFF' && b.subarray(8, 12).toString('latin1') === 'WEBP',
};

/** Decode a `data:image/...;base64,` photo from the clock-in screen, checking type, signature and size. */
export function parsePhoto(dataUrl: string): { mime: PhotoMime; data: Buffer } {
  const m = /^data:(image\/(?:jpeg|png|webp));base64,([A-Za-z0-9+/]+={0,2})$/.exec(dataUrl.trim());
  if (!m) throw new BadRequestError('The photo must be a JPEG, PNG or WebP image.');
  const mime = m[1] as PhotoMime;
  const data = Buffer.from(m[2]!, 'base64');
  if (data.length === 0) throw new BadRequestError('The photo is empty. Take it again.');
  if (data.length > PHOTO_MAX_BYTES) throw new BadRequestError('The photo is too large (max 300 KB).');
  if (!SIGNATURES[mime](data)) throw new BadRequestError('The photo could not be read. Take it again.');
  return { mime, data };
}

export const photoAad = (employeeId: string, punchId: string) => `punch_photo:${employeeId}:${punchId}`;
