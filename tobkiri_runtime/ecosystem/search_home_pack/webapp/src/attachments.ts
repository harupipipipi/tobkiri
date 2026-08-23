export const SEARCH_HOME_TEXT_LIMIT_BYTES = 120_000;
export const SEARCH_HOME_IMAGE_LIMIT_BYTES = 5 * 1024 * 1024;

export type AttachmentPreparationErrorCode =
  | "IMAGE_EXTENSION_MISMATCH"
  | "IMAGE_SIGNATURE_MISMATCH"
  | "IMAGE_TOO_LARGE"
  | "INVALID_UTF8"
  | "TEXT_TOO_LARGE"
  | "UNSUPPORTED_TYPE";

const ATTACHMENT_ERROR_MESSAGES: Record<AttachmentPreparationErrorCode, string> = {
  IMAGE_EXTENSION_MISMATCH: "The image filename extension does not match its declared type.",
  IMAGE_SIGNATURE_MISMATCH: "The image bytes do not match the declared PNG, JPEG, GIF, or WebP type.",
  IMAGE_TOO_LARGE: "Images must be 5 MB or smaller.",
  INVALID_UTF8: "Text and code files must contain valid UTF-8 text.",
  TEXT_TOO_LARGE: "Text and code files must be 120 KB or smaller.",
  UNSUPPORTED_TYPE: "This file type is not supported. Attach text/code, PNG, JPEG, GIF, or WebP.",
};

export class AttachmentPreparationError extends Error {
  readonly code: AttachmentPreparationErrorCode;

  constructor(code: AttachmentPreparationErrorCode) {
    super(ATTACHMENT_ERROR_MESSAGES[code]);
    this.name = "AttachmentPreparationError";
    this.code = code;
  }
}

export function attachmentPreparationMessage(error: unknown): string {
  if (error instanceof AttachmentPreparationError) {
    return ATTACHMENT_ERROR_MESSAGES[error.code];
  }
  return "The file could not be prepared. Choose a supported file and try again.";
}

const TEXT_MIME_TYPES = new Set([
  "application/csv",
  "application/javascript",
  "application/json",
  "application/ld+json",
  "application/toml",
  "application/typescript",
  "application/xml",
  "image/svg+xml",
]);
const TEXT_EXTENSIONS = new Set([
  "c", "cfg", "conf", "cpp", "cs", "css", "csv", "go", "graphql", "h", "hpp", "html", "ini",
  "java", "js", "json", "jsx", "kt", "log", "lua", "md", "mdx", "mjs", "php", "properties", "py",
  "rb", "rs", "sh", "sql", "svg", "toml", "ts", "tsx", "txt", "xml", "yaml", "yml", "zsh",
]);
const IMAGE_MIME_TYPES = new Set(["image/gif", "image/jpeg", "image/png", "image/webp"]);
const IMAGE_EXTENSIONS = new Set(["gif", "jpeg", "jpg", "png", "webp"]);
const GENERIC_BINARY_MIME_TYPES = new Set(["", "application/octet-stream"]);

export type SearchHomeAttachment = {
  id: string;
  name: string;
  size: number;
  type: string;
  content?: string;
  dataUrl?: string;
};

function extension(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? name;
  const dot = base.lastIndexOf(".");
  return dot >= 0 ? base.slice(dot + 1).toLowerCase() : "";
}

export function isSearchHomeTextFile(file: Pick<File, "name" | "type">): boolean {
  const mime = file.type.toLowerCase();
  const suffix = extension(file.name);
  if (IMAGE_EXTENSIONS.has(suffix)) {
    return false;
  }
  if (mime.startsWith("text/") || TEXT_MIME_TYPES.has(mime)) {
    return true;
  }
  return GENERIC_BINARY_MIME_TYPES.has(mime) && TEXT_EXTENSIONS.has(suffix);
}

export function attachmentSupportLabel(): string {
  return "Text/code up to 120 KB, or PNG, JPEG, GIF, WebP up to 5 MB. AI actions only.";
}

export async function encodeSearchHomeAttachment(file: File): Promise<SearchHomeAttachment> {
  const name = (file.name.split(/[\\/]/).pop() ?? file.name)
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/`/g, "'")
    .trim()
    .slice(0, 240) || "attachment";
  const id = `search-home-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  if (isSearchHomeTextFile(file)) {
    if (file.size > SEARCH_HOME_TEXT_LIMIT_BYTES) {
      throw new AttachmentPreparationError("TEXT_TOO_LARGE");
    }
    const bytes = new Uint8Array(await file.arrayBuffer());
    let content: string;
    try {
      content = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      throw new AttachmentPreparationError("INVALID_UTF8");
    }
    if (bytes.byteLength > SEARCH_HOME_TEXT_LIMIT_BYTES) {
      throw new AttachmentPreparationError("TEXT_TOO_LARGE");
    }
    return {
      id,
      name,
      size: bytes.byteLength,
      type: GENERIC_BINARY_MIME_TYPES.has(file.type.toLowerCase()) ? "text/plain" : file.type,
      content,
    };
  }
  if (IMAGE_MIME_TYPES.has(file.type.toLowerCase())) {
    const suffix = extension(name);
    const matchingExtensions: Record<string, Set<string>> = {
      "image/gif": new Set(["gif"]),
      "image/jpeg": new Set(["jpeg", "jpg"]),
      "image/png": new Set(["png"]),
      "image/webp": new Set(["webp"]),
    };
    if (suffix && !matchingExtensions[file.type.toLowerCase()]?.has(suffix)) {
      throw new AttachmentPreparationError("IMAGE_EXTENSION_MISMATCH");
    }
    if (file.size > SEARCH_HOME_IMAGE_LIMIT_BYTES) {
      throw new AttachmentPreparationError("IMAGE_TOO_LARGE");
    }
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (!matchesImageSignature(bytes, file.type.toLowerCase())) {
      throw new AttachmentPreparationError("IMAGE_SIGNATURE_MISMATCH");
    }
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 32_768) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 32_768));
    }
    return { id, name, size: file.size, type: file.type, dataUrl: `data:${file.type};base64,${btoa(binary)}` };
  }
  throw new AttachmentPreparationError("UNSUPPORTED_TYPE");
}

function matchesImageSignature(bytes: Uint8Array, mime: string): boolean {
  if (mime === "image/png") {
    return [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
      .every((value, index) => bytes[index] === value);
  }
  if (mime === "image/jpeg") {
    return bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  }
  if (mime === "image/gif") {
    const header = new TextDecoder("ascii").decode(bytes.subarray(0, 6));
    return header === "GIF87a" || header === "GIF89a";
  }
  if (mime === "image/webp") {
    const decoder = new TextDecoder("ascii");
    return bytes.length >= 12
      && decoder.decode(bytes.subarray(0, 4)) === "RIFF"
      && decoder.decode(bytes.subarray(8, 12)) === "WEBP";
  }
  return false;
}

export function attachmentSupportsAction(action: "smart" | "answer" | "google" | "open"): boolean {
  return action === "smart" || action === "answer";
}
