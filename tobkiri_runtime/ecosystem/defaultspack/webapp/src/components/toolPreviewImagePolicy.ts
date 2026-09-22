export const MAX_INLINE_PREVIEW_IMAGE_URL_LENGTH = 5 * 1024 * 1024;

const MAX_INLINE_PREVIEW_IMAGE_DIMENSION = 8192;
const MAX_INLINE_PREVIEW_IMAGE_PIXELS = 16 * 1024 * 1024;

/** Return a bounded raster data URL only when its header and dimensions agree. */
export function safePreviewImageUrl(
  url: string | undefined,
  _baseUrl?: string,
): string | undefined {
  if (!url || url.length > MAX_INLINE_PREVIEW_IMAGE_URL_LENGTH) return undefined;
  const match = /^data:image\/(png|jpe?g|gif|webp);base64,([a-z0-9+/]+={0,2})$/i.exec(url);
  if (!match || match[2].length % 4 !== 0) return undefined;
  let bytes: Uint8Array;
  try {
    const decoded = atob(match[2]);
    bytes = Uint8Array.from(decoded, (character) => character.charCodeAt(0));
  } catch {
    return undefined;
  }
  const dimensions = inlineRasterDimensions(match[1].toLowerCase(), bytes);
  if (!dimensions) return undefined;
  const [width, height] = dimensions;
  if (
    width <= 0
    || height <= 0
    || width > MAX_INLINE_PREVIEW_IMAGE_DIMENSION
    || height > MAX_INLINE_PREVIEW_IMAGE_DIMENSION
    || width * height > MAX_INLINE_PREVIEW_IMAGE_PIXELS
  ) return undefined;
  return url;
}

function inlineRasterDimensions(kind: string, bytes: Uint8Array): [number, number] | null {
  if (kind === 'png') {
    const signature = [137, 80, 78, 71, 13, 10, 26, 10];
    if (bytes.length < 24 || !signature.every((value, index) => bytes[index] === value)) return null;
    return [readUint32BE(bytes, 16), readUint32BE(bytes, 20)];
  }
  if (kind === 'gif') {
    const header = String.fromCharCode(...bytes.slice(0, 6));
    if (bytes.length < 10 || (header !== 'GIF87a' && header !== 'GIF89a')) return null;
    return [bytes[6] | (bytes[7] << 8), bytes[8] | (bytes[9] << 8)];
  }
  if (kind === 'jpg' || kind === 'jpeg') return jpegDimensions(bytes);
  if (kind === 'webp') return webpDimensions(bytes);
  return null;
}

function readUint32BE(bytes: Uint8Array, offset: number): number {
  return (((bytes[offset] << 24) >>> 0)
    + (bytes[offset + 1] << 16)
    + (bytes[offset + 2] << 8)
    + bytes[offset + 3]);
}

function jpegDimensions(bytes: Uint8Array): [number, number] | null {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return null;
  const sizeMarkers = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);
  let offset = 2;
  while (offset + 8 < bytes.length) {
    if (bytes[offset] !== 0xff) {
      offset += 1;
      continue;
    }
    while (offset < bytes.length && bytes[offset] === 0xff) offset += 1;
    const marker = bytes[offset++];
    if (marker === 0xd9 || marker === 0xda) return null;
    if (offset + 1 >= bytes.length) return null;
    const length = (bytes[offset] << 8) | bytes[offset + 1];
    if (length < 2 || offset + length > bytes.length) return null;
    if (sizeMarkers.has(marker) && length >= 7) {
      return [
        (bytes[offset + 5] << 8) | bytes[offset + 6],
        (bytes[offset + 3] << 8) | bytes[offset + 4],
      ];
    }
    offset += length;
  }
  return null;
}

function webpDimensions(bytes: Uint8Array): [number, number] | null {
  const ascii = (offset: number, length: number) => (
    String.fromCharCode(...bytes.slice(offset, offset + length))
  );
  if (bytes.length < 30 || ascii(0, 4) !== 'RIFF' || ascii(8, 4) !== 'WEBP') return null;
  const chunk = ascii(12, 4);
  if (chunk === 'VP8X') {
    return [
      1 + bytes[24] + (bytes[25] << 8) + (bytes[26] << 16),
      1 + bytes[27] + (bytes[28] << 8) + (bytes[29] << 16),
    ];
  }
  if (chunk === 'VP8L' && bytes[20] === 0x2f) {
    return [
      1 + bytes[21] + ((bytes[22] & 0x3f) << 8),
      1 + (bytes[22] >> 6) + (bytes[23] << 2) + ((bytes[24] & 0x0f) << 10),
    ];
  }
  if (
    chunk === 'VP8 '
    && bytes[23] === 0x9d
    && bytes[24] === 0x01
    && bytes[25] === 0x2a
  ) {
    return [
      (bytes[26] | (bytes[27] << 8)) & 0x3fff,
      (bytes[28] | (bytes[29] << 8)) & 0x3fff,
    ];
  }
  return null;
}
