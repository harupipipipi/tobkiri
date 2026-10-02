const SECRET_PATTERNS: Array<[RegExp, string]> = [
  [/\b(Authorization\s*:\s*)(?:(?:Bearer|Basic|Token|ApiKey|Api-Key)\s+)?\S+/gi, "$1[redacted]"],
  [/\b((?:Set-)?Cookie\s*:\s*)[^\r\n]+/gi, "$1[redacted]"],
  [/\b(Bearer|Basic)\s+\S+/gi, "$1 [redacted]"],
  [/(https?:\/\/[^:/\s]+:)[^@\s/]+@/gi, "$1[redacted]@"],
  [
    /((?:access[_-]?token|api[_-]?key|client[_-]?secret|password|secret|token)\s*["']?\s*[:=]\s*["']?)[^"',;&\s}]+/gi,
    "$1[redacted]",
  ],
  [/([?&](?:access_token|api_key|client_secret|key|password|secret|token)=)[^&#\s]+/gi, "$1[redacted]"],
  [/\b(?:sk|pk)[-_][A-Za-z0-9_-]{8,}\b/gi, "[redacted credential]"],
  [/\bgh[opsu]_[A-Za-z0-9_]{12,}\b/gi, "[redacted credential]"],
  [/\bxox[baprs]-[A-Za-z0-9-]{8,}\b/gi, "[redacted credential]"],
  [/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, "[redacted credential]"],
  [/\bAIza[A-Za-z0-9_-]{20,}\b/g, "[redacted credential]"],
  [/\bya29\.[A-Za-z0-9_-]{12,}\b/g, "[redacted credential]"],
  [/\b(?:glpat-|npm_|pypi-|hf_)[A-Za-z0-9_-]{12,}\b/gi, "[redacted credential]"],
  [/\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g, "[redacted credential]"],
  [/-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----/gi, "[redacted private key]"],
];

export function describeRequestError(error: unknown, fallback: string): string {
  const raw = errorMessage(error);
  let message = raw.replace(/\s+/g, " ").trim();
  for (const [pattern, replacement] of SECRET_PATTERNS) {
    message = message.replace(pattern, replacement);
  }
  return (message || fallback).slice(0, 280);
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === "string") return error;
  if (!error || typeof error !== "object") return "";
  const record = error as Record<string, unknown>;
  if (typeof record.message === "string") return record.message;
  const nested = record.error;
  return nested && typeof nested === "object" && typeof (nested as Record<string, unknown>).message === "string"
    ? String((nested as Record<string, unknown>).message)
    : "";
}

export function joinRequestErrors(messages: string[]): string {
  return Array.from(new Set(messages.filter(Boolean))).join(" ");
}
