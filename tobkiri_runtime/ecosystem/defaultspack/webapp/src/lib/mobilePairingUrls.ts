const LOOPBACK_HOSTS = new Set(["", "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"]);

export function isLoopbackHost(host: string): boolean {
  return LOOPBACK_HOSTS.has(host.trim().replace(/^\[|\]$/g, "").toLowerCase());
}

export function normalizeMobileBaseUrl(value: string, options?: { allowCleartext?: boolean }): string {
  const raw = value.trim();
  if (!raw) return "";
  try {
    const defaultScheme = options?.allowCleartext === true ? "http" : "https";
    const url = new URL(raw.includes("://") ? raw : `${defaultScheme}://${raw}`);
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    if (url.protocol === "http:" && options?.allowCleartext !== true) return "";
    if (isLoopbackHost(url.hostname)) return "";
    return url.origin;
  } catch {
    return "";
  }
}

export function buildMobilePairingBaseUrls(
  values: Array<string | undefined | null>,
  options?: { allowCleartext?: boolean },
): string[] {
  const seen = new Set<string>();
  const urls: string[] = [];
  for (const value of values) {
    const normalized = normalizeMobileBaseUrl(String(value ?? ""), options);
    if (!normalized || seen.has(normalized)) continue;
    seen.add(normalized);
    urls.push(normalized);
  }
  return urls;
}
