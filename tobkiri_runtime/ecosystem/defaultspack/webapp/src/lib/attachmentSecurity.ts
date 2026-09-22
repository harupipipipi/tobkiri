import type { AttachedFile, AttachmentSecurityFinding, AttachmentSecurityReview } from "../renderers/types";

type FindingPattern = {
  kind: AttachmentSecurityFinding["kind"];
  severity: AttachmentSecurityFinding["severity"];
  expression: RegExp;
  capture?: number;
};

const HIGH_RISK_NAME = /(^|[._-])(\.env|credentials?|secrets?|auth|cookies?|private[_-]?key|id_(?:rsa|dsa|ecdsa|ed25519))([._-]|$)|\.(?:pem|key|p12|pfx|keystore|log|dump)$/i;
const BINARY_EXTENSIONS = new Set(["zip", "exe", "dll", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "wasm", "bin"]);
const TEXT_EXTENSIONS = new Set(["bash", "bat", "c", "cfg", "conf", "cpp", "cs", "css", "csv", "env", "go", "graphql", "h", "hpp", "html", "ini", "java", "js", "json", "jsx", "kt", "log", "lua", "md", "mdx", "mjs", "php", "properties", "ps1", "py", "rb", "rs", "sh", "sql", "svg", "toml", "ts", "tsx", "txt", "xml", "yaml", "yml", "zsh"]);
const BINARY_MIME = /^(?:application\/(?:octet-stream|pdf|zip|x-7z-compressed|x-rar-compressed|vnd\.ms-|vnd\.openxmlformats-officedocument)|audio\/|image\/(?!svg\+xml)|video\/)/i;
const HIGH_ENTROPY_CANDIDATE = /\b[A-Za-z0-9_+/=-]{32,128}\b/g;
const FINDING_PATTERNS: FindingPattern[] = [
  { kind: "private_key", severity: "high", expression: /-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----/g },
  { kind: "authorization_header", severity: "high", expression: /\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+([^\s]+)/gi, capture: 1 },
  { kind: "cookie", severity: "high", expression: /\b(?:cookie|set-cookie)\s*:\s*([^\r\n]+)/gi, capture: 1 },
  { kind: "connection_string", severity: "high", expression: /\b[a-z][a-z0-9+.-]{1,20}:\/\/[^\s/:@]+:([^\s/@]+)@[^\s]+/gi, capture: 1 },
  { kind: "aws_access_key", severity: "high", expression: /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g },
  { kind: "provider_token", severity: "high", expression: /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|npm_[A-Za-z0-9]{20,}|[sr]k_live_[A-Za-z0-9]{16,}|hf_[A-Za-z0-9]{20,})\b/g },
  { kind: "named_secret", severity: "high", expression: /\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|token|secret)\s*[:=]\s*["']?([^\s"',;]{6,})/gi, capture: 1 },
];

function lineForOffset(content: string, offset: number): number {
  let line = 1;
  for (let index = 0; index < offset; index += 1) {
    if (content.charCodeAt(index) === 10) line += 1;
  }
  return line;
}

function attachmentFingerprint(file: Pick<AttachedFile, "name" | "size" | "type" | "truncated" | "source" | "sourcePath" | "content" | "dataUrl">): string {
  const payload = file.content !== undefined ? String(file.content) : String(file.dataUrl ?? "");
  const content = [
    String(file.name ?? ""),
    String(file.size ?? ""),
    String(file.type ?? ""),
    file.truncated ? "1" : "0",
    String(file.source ?? ""),
    String(file.sourcePath ?? ""),
    payload,
  ].join("\u0000");
  let hash = 0x811c9dc5;
  for (let index = 0; index < content.length; index += 1) {
    hash ^= content.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `fnv1a32:${content.length}:${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

function findingId(kind: string, start: number, end: number): string {
  return `${kind}:${start}:${end}`;
}

function looksHighEntropy(value: string): boolean {
  if (!/[a-z]/.test(value) || !/[A-Z]/.test(value) || !/\d/.test(value)) return false;
  const frequencies = new Map<string, number>();
  for (const character of value) frequencies.set(character, (frequencies.get(character) ?? 0) + 1);
  let entropy = 0;
  for (const count of frequencies.values()) {
    const probability = count / value.length;
    entropy -= probability * Math.log2(probability);
  }
  return entropy >= 3.8;
}

export function scanAttachmentSecurity(
  file: Pick<AttachedFile, "name" | "size" | "content" | "dataUrl" | "truncated" | "type" | "source" | "sourcePath">,
  customPatterns: string[] = [],
): AttachmentSecurityReview {
  const content = String(file.content ?? "");
  const findings: AttachmentSecurityFinding[] = [];
  const basename = file.name.split(/[\\/]/).pop() ?? file.name;
  if (HIGH_RISK_NAME.test(file.name) || (basename.startsWith(".") && basename.length > 1)) {
    findings.push({
      id: findingId("high_risk_file", -1, -1),
      kind: "high_risk_file",
      severity: "high",
      line: null,
      start: null,
      end: null,
    });
  }
  const extension = basename.includes(".") ? basename.split(".").pop()?.toLowerCase() ?? "" : "";
  const mime = String(file.type ?? "").toLowerCase();
  const hasBinaryContent = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(content);
  const hasUnscannedDataUrl = file.content == null && Boolean(file.dataUrl);
  if (
    (mime.startsWith("text/") && BINARY_EXTENSIONS.has(extension))
    || (BINARY_MIME.test(mime) && TEXT_EXTENSIONS.has(extension))
    || hasBinaryContent
  ) {
    findings.push({
      id: findingId("mime_mismatch", -1, -1),
      kind: "mime_mismatch",
      severity: "review",
      line: null,
      start: null,
      end: null,
    });
  }
  if (hasUnscannedDataUrl) {
    findings.push({
      id: findingId("data_url_unscanned", -1, -1),
      kind: "data_url_unscanned",
      severity: "review",
      line: null,
      start: null,
      end: null,
    });
  }
  for (const pattern of FINDING_PATTERNS) {
    pattern.expression.lastIndex = 0;
    for (const match of content.matchAll(pattern.expression)) {
      const full = match[0] ?? "";
      const selected = pattern.capture ? match[pattern.capture] ?? "" : full;
      if (!selected || selected === "[REDACTED]" || match.index === undefined) continue;
      const relativeOffset = pattern.capture ? full.lastIndexOf(selected) : 0;
      const start = match.index + Math.max(0, relativeOffset);
      const end = start + selected.length;
      findings.push({
        id: findingId(pattern.kind, start, end),
        kind: pattern.kind,
        severity: pattern.severity,
        line: lineForOffset(content, start),
        start,
        end,
      });
    }
  }
  HIGH_ENTROPY_CANDIDATE.lastIndex = 0;
  for (const match of content.matchAll(HIGH_ENTROPY_CANDIDATE)) {
    const candidate = match[0] ?? "";
    if (!candidate || match.index === undefined || !looksHighEntropy(candidate)) continue;
    const start = match.index;
    const end = start + candidate.length;
    findings.push({
      id: findingId("high_entropy_candidate", start, end),
      kind: "high_entropy_candidate",
      severity: "review",
      line: lineForOffset(content, start),
      start,
      end,
    });
  }
  const normalizedCustomPatterns = [...new Set(customPatterns
    .map((pattern) => pattern.trim())
    .filter((pattern) => pattern.length >= 3 && pattern.length <= 128))]
    .slice(0, 32);
  const lowerContent = content.toLocaleLowerCase();
  for (const pattern of normalizedCustomPatterns) {
    const lowerPattern = pattern.toLocaleLowerCase();
    let start = lowerContent.indexOf(lowerPattern);
    while (start >= 0) {
      const end = start + pattern.length;
      findings.push({
        id: findingId("custom_pattern", start, end),
        kind: "custom_pattern",
        severity: "high",
        line: lineForOffset(content, start),
        start,
        end,
      });
      start = lowerContent.indexOf(lowerPattern, end);
    }
  }
  const deduped = [...new Map(findings.map((finding) => [finding.id, finding])).values()]
    .sort((left, right) => (left.start ?? -1) - (right.start ?? -1));
  const needsReview = deduped.length > 0 || Boolean(file.truncated);
  return {
    version: 1,
    status: needsReview ? "required" : "clear",
    fingerprint: attachmentFingerprint(file),
    scannedCharacters: content.length,
    truncated: Boolean(file.truncated),
    findings: deduped,
  };
}

export function attachmentNeedsSecurityReview(file: AttachedFile): boolean {
  return file.securityReview?.status === "required";
}

export function approveAttachmentSecurityReview(file: AttachedFile): AttachedFile {
  const review = file.securityReview ?? scanAttachmentSecurity(file);
  return { ...file, securityReview: { ...review, status: "approved" } };
}

export function redactAttachmentSecurityFindings(file: AttachedFile, selectedFindingIds?: string[]): AttachedFile {
  const review = file.securityReview ?? scanAttachmentSecurity(file);
  const selected = selectedFindingIds ? new Set(selectedFindingIds) : null;
  let content = String(file.content ?? "");
  const spans = review.findings
    .filter((finding): finding is AttachmentSecurityFinding & { start: number; end: number } => (
      typeof finding.start === "number" && typeof finding.end === "number" && finding.end > finding.start
      && (!selected || selected.has(finding.id))
    ))
    .sort((left, right) => left.start - right.start);
  const merged: Array<{ start: number; end: number }> = [];
  for (const finding of spans) {
    const previous = merged.at(-1);
    if (previous && finding.start <= previous.end) {
      previous.end = Math.max(previous.end, finding.end);
    } else {
      merged.push({ start: finding.start, end: finding.end });
    }
  }
  for (const span of merged.reverse()) {
    content = `${content.slice(0, span.start)}[REDACTED]${content.slice(span.end)}`;
  }
  const rescanned = scanAttachmentSecurity({ ...file, content });
  const stillContainsSensitiveContent = rescanned.findings.some((finding) => (
    typeof finding.start === "number" && typeof finding.end === "number"
  ));
  return {
    ...file,
    content,
    securityReview: {
      ...rescanned,
      status: stillContainsSensitiveContent || rescanned.truncated ? "required" : "redacted",
      redactedFindingCount: spans.length,
    },
  };
}

export function attachmentMetadataOnly(file: AttachedFile): AttachedFile {
  const next = { ...file, content: undefined, dataUrl: undefined };
  return {
    ...next,
    securityReview: {
      ...scanAttachmentSecurity(next),
      status: "metadata_only",
      redactedFindingCount: file.securityReview?.findings.length ?? 0,
    },
  };
}
