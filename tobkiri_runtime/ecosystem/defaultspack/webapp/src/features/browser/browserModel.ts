import type { BrowserApproval } from "../../lib/browserApproval";

export type BrowserResult = Record<string, unknown>;
export type BrowserActionRunner = (action: string, payload?: BrowserResult) => Promise<BrowserResult | null>;
export type BrowserExtensionDraft = {
  files: Record<string, string>;
  name: string;
  version: string;
  permissions: string[];
  hostPermissions: string[];
};

export function browserRecord(value: unknown): BrowserResult | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as BrowserResult
    : null;
}

export function browserRows(value: unknown): BrowserResult[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap<BrowserResult>((item) => {
    const row = browserRecord(item);
    return row ? [row] : [];
  });
}

export function browserText(value: unknown, fallback = ""): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}

function manifestStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function contentScriptMatches(manifest: BrowserResult): string[] {
  return browserRows(manifest.content_scripts).flatMap((script) => manifestStrings(script.matches));
}

/** Unwrap the complete host result, preserving request-backed approval facts. */
export function normalizeBrowserResult(value: unknown): BrowserResult {
  const outer = browserRecord(value);
  if (!outer) throw new Error("ブラウザから有効な応答を受け取れませんでした。");
  const result = browserRecord(outer.widget) ?? outer;
  if (result.approval_required === true || result.requires_approval === true) return result;
  if (outer.is_error === true || result.is_error === true || result.success === false
    || result.status === "error" || result.status === "denied" || result.status === "unavailable") {
    throw new Error(browserText(result.message ?? result.reason ?? result.error_type,
      "ブラウザ操作を完了できませんでした。"));
  }
  return result;
}

/** Show operation scope without echoing imported sessions or extension code. */
export function browserApprovalDetails(action: string, payload: BrowserResult): BrowserResult {
  if (action === "browser.cookies.import") {
    return {
      profile_id: payload.profile_id,
      format: payload.format,
      domains: payload.domains,
      cookie_count: Array.isArray(payload.cookies) ? payload.cookies.length : undefined,
      content_bytes: typeof payload.content === "string" ? new TextEncoder().encode(payload.content).length : undefined,
    };
  }
  if (action === "browser.extensions.install") {
    const files = browserRecord(payload.files) ?? {};
    let manifest: BrowserResult | null = null;
    try { manifest = browserRecord(JSON.parse(browserText(files["manifest.json"]))); } catch { /* The draft validates this earlier. */ }
    return {
      profile_id: payload.profile_id,
      name: manifest?.name,
      permissions: manifest?.permissions,
      host_permissions: manifest?.host_permissions,
      optional_permissions: manifest?.optional_permissions,
      optional_host_permissions: manifest?.optional_host_permissions,
      content_script_matches: manifest ? contentScriptMatches(manifest) : [],
      file_count: Object.keys(files).length,
    };
  }
  return { ...payload };
}

export function browserPendingApproval(
  result: BrowserResult,
  action: string,
  payload: BrowserResult,
): BrowserApproval | null {
  if (result.approval_required !== true && result.requires_approval !== true) return null;
  const requestId = browserText(result.approval_request_id ?? result.request_id).trim();
  if (!requestId) throw new Error("承認リクエストを確認できませんでした。状態を更新して再試行してください。");
  return {
    action,
    payload: browserApprovalDetails(action, payload),
    requestId,
    toolName: "browser_computer",
    argsHash: browserText(result.args_hash),
    riskLevel: browserText(result.risk_level, "high"),
    summary: browserText(result.display_summary ?? result.message),
  };
}

/** Validate a managed-browser URL before requesting host navigation. */
export function browserNavigationUrl(raw: string): string {
  const url = new URL(raw.trim());
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new Error("認証情報を含まない http または https の URL を入力してください。");
  }
  return url.href;
}

/** Prepare unpacked, text-based extensions with a root Manifest V3 file. */
export async function readBrowserExtensionFiles(files: readonly File[]): Promise<BrowserExtensionDraft> {
  if (!files.length || files.length > 256) throw new Error("拡張機能は 1〜256 個のテキストファイルを選択してください。");
  if (files.some((file) => file.size > 1024 * 1024)
    || files.reduce((size, file) => size + file.size, 0) > 4 * 1024 * 1024) {
    throw new Error("拡張機能は各ファイル 1 MB 以下、合計 4 MB 以下にしてください。");
  }
  const paths = files.map((file) => file.webkitRelativePath || file.name);
  const firstRoot = paths[0].split("/")[0];
  const stripRoot = paths.every((path) => path.startsWith(`${firstRoot}/`));
  const contents: Record<string, string> = Object.create(null) as Record<string, string>;
  for (let index = 0; index < files.length; index += 1) {
    const path = stripRoot ? paths[index].slice(firstRoot.length + 1) : paths[index];
    if (!path || /[\\\u0000-\u001f:]/.test(path) || path.startsWith("/")
      || path.split("/").some((part) => !part || part === "." || part === "..")) {
      throw new Error("拡張機能ファイルの相対パスが無効です。");
    }
    if (Object.prototype.hasOwnProperty.call(contents, path)) throw new Error(`ファイル名が重複しています: ${path}`);
    if (path === "manifest.json" && files[index].size > 64 * 1024) {
      throw new Error("manifest.json は 64 KB 以下にしてください。");
    }
    const bytes = await files[index].arrayBuffer();
    try { contents[path] = new TextDecoder("utf-8", { fatal: true }).decode(bytes); }
    catch { throw new Error("拡張機能は UTF-8 のテキストファイルを選択してください。"); }
    if (contents[path].includes("\u0000")) throw new Error("バイナリファイルを含む拡張機能は読み込めません。");
  }
  let manifest: BrowserResult | null;
  try { manifest = browserRecord(JSON.parse(contents["manifest.json"])); }
  catch { throw new Error("拡張機能のルートに有効な manifest.json が必要です。"); }
  if (!manifest || manifest.manifest_version !== 3 || !browserText(manifest.name).trim() || !browserText(manifest.version).trim()) {
    throw new Error("name と version を持つ Manifest V3 の拡張機能を選択してください。");
  }
  return {
    files: contents,
    name: browserText(manifest.name),
    version: browserText(manifest.version),
    permissions: [
      ...manifestStrings(manifest.permissions),
      ...manifestStrings(manifest.optional_permissions).map((permission) => `${permission} (任意)`),
    ],
    hostPermissions: [...new Set([
      ...manifestStrings(manifest.host_permissions),
      ...manifestStrings(manifest.optional_host_permissions).map((host) => `${host} (任意)`),
      ...contentScriptMatches(manifest).map((host) => `${host} (Content Scripts)`),
    ])],
  };
}

export function browserCookieDomains(raw: string): string[] {
  const domains = raw.split(/[\s,]+/).map((domain) => domain.trim()).filter(Boolean);
  if (domains.some((domain) => !/^\.?[a-z\d](?:[a-z\d.-]*[a-z\d])?$/i.test(domain))) {
    throw new Error("Cookie の対象はドメイン名をカンマで区切って入力してください。");
  }
  return [...new Set(domains)];
}
