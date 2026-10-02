import type { AttachedFile } from "../renderers/types";

const TEXT_TRUNCATE_LIMIT = 120_000;
const IMAGE_INLINE_LIMIT_BYTES = 8 * 1024 * 1024;
const AUDIO_INLINE_LIMIT_BYTES = 25 * 1024 * 1024;
const ATTACHMENT_NAME_LIMIT = 240;

const TEXT_MIME_PREFIXES = ["text/"];
const TEXT_MIME_TYPES = new Set([
  "application/csv",
  "application/graphql",
  "application/javascript",
  "application/json",
  "application/ld+json",
  "application/rtf",
  "application/toml",
  "application/typescript",
  "application/x-httpd-php",
  "application/x-javascript",
  "application/x-sh",
  "application/xhtml+xml",
  "application/xml",
  "image/svg+xml",
]);

const TEXT_EXTENSIONS = new Set([
  "bash",
  "bat",
  "c",
  "cfg",
  "conf",
  "cpp",
  "cs",
  "css",
  "csv",
  "env",
  "go",
  "graphql",
  "h",
  "hpp",
  "html",
  "ini",
  "java",
  "js",
  "json",
  "jsx",
  "kt",
  "log",
  "lua",
  "md",
  "mdx",
  "mjs",
  "php",
  "properties",
  "py",
  "rb",
  "rs",
  "sh",
  "sql",
  "svg",
  "toml",
  "ts",
  "tsx",
  "txt",
  "xml",
  "yaml",
  "yml",
  "zsh",
]);

function fileExtension(name: string): string {
  const basename = name.split(/[\\/]/).pop() ?? name;
  const dotIndex = basename.lastIndexOf(".");
  return dotIndex >= 0 ? basename.slice(dotIndex + 1).toLowerCase() : "";
}

function safeAttachmentName(name: string): string {
  const normalized = name
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, ATTACHMENT_NAME_LIMIT);
  return normalized || "attachment";
}

function longestDelimiterRun(text: string, delimiter: "`" | "~"): number {
  let longest = 0;
  let current = 0;
  for (const character of text) {
    if (character === delimiter) {
      current += 1;
      longest = Math.max(longest, current);
    } else {
      current = 0;
    }
  }
  return longest;
}

function markdownFenceFor(content: string): string {
  const backtickRun = longestDelimiterRun(content, "`");
  const tildeRun = longestDelimiterRun(content, "~");
  const delimiter = backtickRun <= tildeRun ? "`" : "~";
  const longestRun = delimiter === "`" ? backtickRun : tildeRun;
  return delimiter.repeat(Math.max(3, longestRun + 1));
}

export function isTextLikeFile(file: Pick<File, "name" | "type">): boolean {
  const mime = (file.type || "").toLowerCase();
  if (mime && TEXT_MIME_PREFIXES.some((prefix) => mime.startsWith(prefix))) return true;
  if (mime && TEXT_MIME_TYPES.has(mime)) return true;
  return TEXT_EXTENSIONS.has(fileExtension(file.name));
}

export async function fileToAttachment(file: File): Promise<AttachedFile> {
  const base = {
    id: `file-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    name: file.name,
    size: file.size,
    type: file.type || undefined,
    truncated: false,
  };

  if (/^image\//.test(file.type || "")) {
    if (file.size > IMAGE_INLINE_LIMIT_BYTES) {
      return {
        ...base,
        truncated: true,
      };
    }
    const dataUrl = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error ?? new Error("画像を読み込めませんでした"));
      reader.readAsDataURL(file);
    });
    return {
      ...base,
      dataUrl,
    };
  }

  if (/^audio\//.test(file.type || "")) {
    if (file.size > AUDIO_INLINE_LIMIT_BYTES) {
      return {
        ...base,
        truncated: true,
      };
    }
    const dataUrl = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error ?? new Error("音声ファイルを読み込めませんでした"));
      reader.readAsDataURL(file);
    });
    return {
      ...base,
      dataUrl,
    };
  }

  if (!isTextLikeFile(file)) {
    return base;
  }

  const text = await file.text();
  const truncated = text.length > TEXT_TRUNCATE_LIMIT;
  return {
    ...base,
    type: file.type || "text/plain",
    content: truncated ? text.slice(0, TEXT_TRUNCATE_LIMIT) : text,
    truncated,
  };
}

export function buildAttachmentSnippet(file: AttachedFile): string {
  if (file.content === undefined) return "";
  const content = `${file.content}${file.truncated ? "\n..." : ""}`;
  const fence = markdownFenceFor(content);
  return `\n\n添付ファイル: ${safeAttachmentName(file.name)}\n${fence}\n${content}\n${fence}`;
}
