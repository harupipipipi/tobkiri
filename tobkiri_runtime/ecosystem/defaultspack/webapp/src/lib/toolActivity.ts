import { conversationArtifactFileUrl, type ChatActivityEvent, type ToolLogEntry } from "./api";
import { boundedDurationLabel, elapsedDurationLabel, formatCompactDuration, timestampMs } from "./duration";

export type ToolActivityStatus = "running" | "waiting_approval" | "completed" | "failed" | "blocked";

export type RunActivityKind = "tool" | "progress";

export type RunActivityItem = {
  id: string;
  kind: RunActivityKind;
  runId?: string;
  toolCallId?: string;
  providerAttempt?: number | string;
  providerAttemptGeneration?: number | string;
  startSeq?: number;
  endSeq?: number;
  startedAt?: number | string;
  completedAt?: number | string;
  folder: string;
  folderLabel: string;
  title: string;
  detail: string;
  durationLabel?: string;
  nextStep?: string;
  nextAction?: string;
  status: ToolActivityStatus;
  timestamp?: number | string;
  orderIndex?: number;
};

export type ToolActivityItem = RunActivityItem & {
  kind: "tool";
  toolName: string;
  input: string;
  artifacts?: ToolActivityArtifact[];
  supported: boolean;
  rawJson?: string;
};

export type ProgressActivityItem = RunActivityItem & {
  kind: "progress";
  phase?: string;
};

export type ToolActivityGroup = {
  id: string;
  label: string;
  items: ToolActivityItem[];
};

export type ToolActivityArtifact = {
  name: string;
  path: string;
  url?: string;
  kind: "image" | "file";
};

const FOLDER_RULES: Array<[RegExp, string, string]> = [
  [/^sandbox_|sandbox/i, "sandbox/coding", "Sandbox"],
  [/calculator|calc|math/i, "calculation", "計算"],
  [/web|search/i, "web/search", "Web検索"],
  [/reddit/i, "web/reddit", "Reddit検索"],
  [/browser|computer/i, "browser", "ブラウザ"],
  [/todo|task/i, "planning/todo", "Todo"],
  [/delegate|subagent|agent/i, "agent/delegation", "委任"],
  [/terminal|shell|exec/i, "coding/terminal", "ターミナル"],
  [/file|read|write|list/i, "coding/files", "ファイル"],
  [/git|branch|commit|diff/i, "coding/git", "Git"],
];

function compact(value: unknown, maxLength = 96): string {
  if (value === null || value === undefined) return "";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function lastPathSegment(value: string): string {
  const normalized = value.replace(/\\/g, "/").replace(/\/+$/, "");
  return normalized.split("/").filter(Boolean).pop() || value;
}

function terminalActionTitle(command: string): string {
  const text = command.trim();
  const lowered = text.toLowerCase();
  if (!text) return "ターミナルで作業";
  if (/^(rg|grep|fd|find)\b/.test(lowered)) return "コードを検索";
  if (/^(sed|cat|less|head|tail|nl)\b/.test(lowered)) return "ファイルを確認";
  if (/^git\s+(status|diff|show|log|branch|rev-parse)\b/.test(lowered)) return "Git 状態を確認";
  if (/^git\s+add\b/.test(lowered)) return "変更をステージ";
  if (/^git\s+commit\b/.test(lowered)) return "コミットを作成";
  if (/^git\s+push\b/.test(lowered)) return "変更を push";
  if (/^(npm|pnpm|yarn)\s+(test|run\s+test)\b/.test(lowered) || /^(python\s+-m\s+pytest|pytest|cargo\s+test)\b/.test(lowered)) return "テストを実行";
  if (/^(npm|pnpm|yarn)\s+(run\s+)?build\b/.test(lowered) || /^cargo\s+build\b/.test(lowered)) return "ビルドを実行";
  if (/^(npm|pnpm|yarn)\s+(run\s+)?lint\b/.test(lowered) || /^(ruff|eslint)\b/.test(lowered)) return "lint を実行";
  if (/^gh\s+(repo|pr|issue)\s+view\b/.test(lowered)) return "GitHub 情報を確認";
  if (/^gh\s+pr\s+(create|edit)\b/.test(lowered)) return "PR を更新";
  if (/^gh\b/.test(lowered)) return "GitHub を操作";
  if (/^git\b/.test(lowered)) return "Git を操作";
  return "ターミナルで作業";
}

function folderForTerminalCommand(args: Record<string, unknown>): { id: string; label: string } | null {
  const command = pickString(args, ["command", "cmd"]).trim().toLowerCase();
  if (!command) return null;
  if (/^(git|gh)\b/.test(command)) return { id: "coding/git", label: "Git" };
  if (/^(rg|grep|fd|find|sed|cat|less|head|tail|nl)\b/.test(command)) return { id: "coding/files", label: "ファイル" };
  return null;
}

function activityFolderFor(toolName: string, args: Record<string, unknown>): { id: string; label: string } {
  const lowerName = toolName.toLowerCase();
  if (lowerName.startsWith("sandbox_")) {
    if (lowerName.includes("terminal") || lowerName.includes("exec")) return { id: "sandbox/terminal", label: "Sandbox" };
    if (lowerName.includes("diff")) return { id: "sandbox/diff", label: "Sandbox" };
    if (lowerName.includes("artifact") || lowerName.includes("export")) return { id: "sandbox/artifacts", label: "Sandbox" };
    return { id: "sandbox/files", label: "Sandbox" };
  }
  if (lowerName.includes("terminal") || lowerName.includes("shell") || lowerName.includes("exec")) {
    return folderForTerminalCommand(args) ?? toolFolderFor(toolName);
  }
  return toolFolderFor(toolName);
}

function humanToolTitle(toolName: string, args: Record<string, unknown>, folderLabel: string, argumentSummary: string): string {
  const lowerName = toolName.toLowerCase();
  if (lowerName.startsWith("sandbox_")) {
    const path = pickString(args, ["path", "filename", "directory", "glob"]);
    const label = path ? lastPathSegment(path) : "";
    if (lowerName.includes("terminal") || lowerName.includes("exec")) return "Sandboxでコマンドを実行";
    if (lowerName.includes("write") || lowerName.includes("patch")) return label ? `Sandboxで編集: ${label}` : "Sandboxでファイルを編集";
    if (lowerName.includes("read")) return label ? `Sandboxで確認: ${label}` : "Sandboxでファイルを確認";
    if (lowerName.includes("diff")) return "Sandboxの差分を確認";
    if (lowerName.includes("artifact") || lowerName.includes("export")) return "Sandbox成果物をまとめる";
    return "Sandboxで作業";
  }
  if (lowerName.includes("terminal") || lowerName.includes("shell") || lowerName.includes("exec")) {
    return terminalActionTitle(pickString(args, ["command", "cmd"]));
  }
  if (lowerName.includes("git")) {
    if (lowerName.includes("push")) return "変更を push";
    if (lowerName.includes("commit")) return "コミットを作成";
    if (lowerName.includes("diff")) return "差分を確認";
    if (lowerName.includes("branch")) return "ブランチを確認";
    return "Git 状態を確認";
  }
  if (lowerName.includes("file")) {
    const path = pickString(args, ["path", "filename", "directory", "glob"]);
    const label = path ? lastPathSegment(path) : "";
    if (lowerName.includes("write") || lowerName.includes("patch") || lowerName.includes("create") || lowerName.includes("delete")) {
      return label ? `ファイルを編集: ${label}` : "ファイルを編集";
    }
    if (lowerName.includes("list")) {
      return label ? `ファイル一覧を確認: ${label}` : "ファイル一覧を確認";
    }
    return label ? `ファイルを確認: ${label}` : "ファイルを確認";
  }
  if (lowerName.includes("search") || lowerName.includes("web") || lowerName.includes("reddit")) {
    return argumentSummary ? `Webで検索: ${compact(argumentSummary, 56)}` : "Webで検索";
  }
  if (lowerName.includes("browser") || lowerName.includes("computer")) {
    const action = pickString(args, ["action"]) || "画面を操作";
    if (action.includes("screenshot")) return "画面を確認";
    if (action.includes("click")) return "画面をクリック";
    if (action.includes("type")) return "文字を入力";
    if (action.includes("open")) return "ブラウザを開く";
    return "画面を操作";
  }
  if (lowerName.includes("subagent") || lowerName.includes("agent") || lowerName.includes("delegate")) return "サブエージェントに依頼";
  if (lowerName.includes("todo") || lowerName.includes("task")) return "タスクを整理";
  if (lowerName.includes("calculator") || lowerName.includes("calc")) return argumentSummary ? `計算: ${argumentSummary}` : "計算";
  return `${folderLabel}を使用`;
}

function jsonBlock(value: unknown, maxLength = 1600): string {
  let text = "";
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value ?? "");
  }
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function pickString(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
  }
  return "";
}

export function toolFolderFor(toolName: string): { id: string; label: string } {
  for (const [pattern, id, label] of FOLDER_RULES) {
    if (pattern.test(toolName)) return { id, label };
  }
  return { id: "tools", label: "Tools" };
}

function isSupportedToolActivity(toolName: string, explicitSummary = ""): boolean {
  return toolFolderFor(toolName).id !== "tools" || Boolean(explicitSummary.trim());
}

export function summarizeToolArguments(toolName: string, args?: Record<string, unknown>): string {
  if (!args) return "";
  const lowerName = toolName.toLowerCase();
  if (lowerName.startsWith("sandbox_")) {
    if (lowerName.includes("terminal") || lowerName.includes("exec")) return pickString(args, ["command", "cmd"]);
    return pickString(args, ["path", "filename", "directory", "glob", "sandbox_id"]);
  }
  if (lowerName.includes("calculator") || lowerName.includes("calc")) {
    const expression = pickString(args, ["expression", "expr", "input", "query"]);
    if (expression) return expression;
    const left = pickString(args, ["a", "left", "x"]);
    const right = pickString(args, ["b", "right", "y"]);
    const op = pickString(args, ["operator", "operation", "op"]);
    if (left && right) return [left, op, right].filter(Boolean).join(" ");
  }
  if (lowerName.includes("search") || lowerName.includes("web") || lowerName.includes("reddit")) {
    return pickString(args, ["query", "q", "search_query", "text", "url"]);
  }
  if (lowerName.includes("file")) {
    return pickString(args, ["path", "filename", "directory", "glob"]);
  }
  if (lowerName.includes("todo")) {
    return pickString(args, ["title", "task", "action", "todo_id"]);
  }
  if (lowerName.includes("subagent") || lowerName.includes("agent") || lowerName.includes("delegate")) {
    return pickString(args, ["task", "title", "prompt"]);
  }
  if (lowerName.includes("browser") || lowerName.includes("computer")) {
    const action = pickString(args, ["action"]);
    const target = pickString(args, ["url", "app", "application", "browser", "name", "title"]);
    const text = pickString(args, ["text", "key"]);
    const x = pickString(args, ["x"]);
    const y = pickString(args, ["y"]);
    const x1 = pickString(args, ["x1", "from_x"]);
    const y1 = pickString(args, ["y1", "from_y"]);
    const x2 = pickString(args, ["x2", "to_x"]);
    const y2 = pickString(args, ["y2", "to_y"]);
    const coords = x && y ? `(${x}, ${y})` : "";
    const dragCoords = x1 && y1 && x2 && y2 ? `(${x1}, ${y1}) -> (${x2}, ${y2})` : "";
    return [action, target, text, dragCoords || coords].filter(Boolean).join(" ");
  }
  if (lowerName.includes("terminal") || lowerName.includes("exec") || lowerName.includes("shell")) {
    return pickString(args, ["command", "cmd"]);
  }
  return compact(args);
}

function formatCalculatorResult(summary: string): string {
  const calculatedMatch = summary.match(/=\s*([-+]?[\d.,]+(?:\.\d+)?)\s*$/);
  if (calculatedMatch) return calculatedMatch[1];
  return summary;
}

function isGenericCompletionSummary(summary: string, toolName: string): boolean {
  const normalized = summary.trim().toLowerCase();
  if (!normalized) return true;
  if (normalized === "completed" || normalized === "complete" || normalized === "done" || normalized === "完了") return true;
  const lowerToolName = toolName.trim().toLowerCase();
  return Boolean(lowerToolName && normalized.startsWith(lowerToolName) && /\b(completed|complete|done)\b/.test(normalized));
}

function parseJsonRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "string") return null;
  const text = value.trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return null;
  try {
    const parsed = JSON.parse(text);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function normalizedToolResultData(result: Record<string, unknown>): Record<string, unknown> {
  const rawData = result.data && typeof result.data === "object" ? result.data as Record<string, unknown> : result;
  const parsedResult = parseJsonRecord(rawData.result);
  const widget = isRecord(rawData.widget) ? rawData.widget : {};
  const data = { ...rawData, ...(parsedResult ?? {}), ...widget };
  if (parsedResult) delete data.result;
  return data;
}

function countArray(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function summarizeGitResult(data: Record<string, unknown>): string {
  const commitHash = pickString(data, ["commit_hash", "commit"]);
  if (commitHash) return `コミットしました: ${commitHash}`;
  if (data.pushed === true) {
    const branch = pickString(data, ["branch"]);
    return branch ? `pushしました: ${branch}` : "pushしました";
  }
  const branch = pickString(data, ["branch"]);
  if (branch || typeof data.clean === "boolean") {
    const changedCount = countArray(data.staged) + countArray(data.modified) + countArray(data.untracked);
    const state = data.clean === true ? "変更なし" : `${changedCount}件の変更`;
    return branch ? `ブランチ ${branch} · ${state}` : state;
  }
  return "";
}

function summarizeToolResult(toolName: string, result: unknown): string {
  if (!result || typeof result !== "object") {
    const summary = compact(result, 120);
    return isGenericCompletionSummary(summary, toolName) ? "" : summary;
  }
  const record = result as Record<string, unknown>;
  const data = normalizedToolResultData(record);
  const lowerName = toolName.toLowerCase();
  if (data.status === "error" && data.error) {
    const error = isRecord(data.error) ? pickString(data.error, ["message", "code"]) : compact(data.error, 120);
    if (error) return error;
  }
  if (lowerName.startsWith("sandbox_")) {
    const diffSummary = pickString(data, ["diff_summary"]);
    if (diffSummary) return diffSummary;
    const artifactCount = Array.isArray(data.artifact_paths) ? data.artifact_paths.length : 0;
    if (artifactCount) return `Sandbox成果物 ${artifactCount} 件`;
    const path = pickString(data, ["path"]);
    const label = path ? lastPathSegment(path) : "ファイル";
    if (data.written === true) return `Sandbox内に保存: ${label}`;
    if (data.patched === true) return `Sandbox内で変更: ${label}`;
    if (typeof data.content === "string") return `Sandbox内で確認: ${label}`;
    const exitCode = pickString(data, ["exit_code"]);
    if (exitCode) return `Sandbox終了コード ${exitCode}`;
  }
  if (lowerName.includes("file")) {
    const widget = isRecord(data.widget) ? data.widget : {};
    const fileData = { ...data, ...widget };
    const path = pickString(fileData, ["path"]);
    const label = path ? lastPathSegment(path) : "ファイル";
    if (fileData.written === true) return `保存しました: ${label}`;
    if (fileData.patched === true) return `変更しました: ${label}`;
    if (fileData.deleted === true) return `削除しました: ${label}`;
    if (typeof fileData.content === "string" || typeof data.result === "string") {
      return fileData.truncated === true ? `一部を読みました: ${label}` : `読みました: ${label}`;
    }
  }
  const command = pickString(data, ["command", "cmd"]).toLowerCase();
  if (lowerName.includes("git") || /^(git|gh)\b/.test(command)) {
    const gitSummary = summarizeGitResult(data);
    if (gitSummary) return gitSummary;
  }
  if (lowerName.includes("terminal") || lowerName.includes("shell") || lowerName.includes("exec")) {
    const exitCode = pickString(data, ["exit_code"]);
    if (exitCode) return `終了コード ${exitCode}`;
    if (data.approval_required === true) return "承認待ち";
  }
  const direct = pickString(data, ["summary", "result", "message", "output", "title"]);
  if (direct) {
    const formatted = toolName.toLowerCase().includes("calc") ? formatCalculatorResult(direct) : direct;
    return isGenericCompletionSummary(formatted, toolName) ? "" : formatted;
  }
  if (Array.isArray(data.results)) return `${data.results.length} 件の結果`;
  if (Array.isArray(data.items)) return `${data.items.length} 件の項目`;
  if (Array.isArray(data.files)) return `${data.files.length} 件のファイル`;
  return compact(data, 120);
}

function explicitToolText(value: Record<string, unknown>): string {
  return pickString(value, ["display_text", "display_summary", "summary", "result_summary"]);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object";
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function basename(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  return normalized.split("/").filter(Boolean).pop() || "artifact";
}

function isImagePath(path: string): boolean {
  return /\.(png|jpe?g|gif|webp|svg)$/i.test(path);
}

function isImageDataUrl(value: string): boolean {
  return /^data:image\/[a-z0-9.+-]+;base64,/i.test(value);
}

function dataUrlName(value: string): string {
  const match = value.match(/^data:image\/([a-z0-9.+-]+);/i);
  const extension = match?.[1]?.replace("jpeg", "jpg").split("+")[0] || "png";
  return `screenshot.${extension}`;
}

function collectArtifacts(value: unknown, conversationId?: string, artifacts: ToolActivityArtifact[] = [], seen = new Set<string>()): ToolActivityArtifact[] {
  if (!isRecord(value)) {
    if (Array.isArray(value)) {
      for (const item of value) collectArtifacts(item, conversationId, artifacts, seen);
    }
    return artifacts;
  }

  const inlineUrl = stringValue(value.data_url) || stringValue(value.dataUrl);
  if (inlineUrl && isImageDataUrl(inlineUrl)) {
    const key = `inline:${inlineUrl.length}:${inlineUrl.slice(0, 96)}`;
    if (!seen.has(key)) {
      seen.add(key);
      artifacts.push({
        name: pickString(value, ["name", "filename", "title"]) || dataUrlName(inlineUrl),
        path: key,
        kind: "image",
        url: inlineUrl,
      });
    }
  }

  const preferredPath = stringValue(value.model_image_path) || stringValue(value.screenshot_path) || stringValue(value.path);
  if (preferredPath) {
    const key = preferredPath;
    if (!seen.has(key)) {
      seen.add(key);
      const kind = isImagePath(preferredPath) ? "image" : "file";
      artifacts.push({
        name: basename(preferredPath),
        path: preferredPath,
        kind,
        url: conversationId ? conversationArtifactFileUrl(conversationId, preferredPath) : undefined,
      });
    }
  }

  for (const [key, item] of Object.entries(value)) {
    if (key === "path" || key === "screenshot_path" || key === "model_image_path" || key === "data_url" || key === "dataUrl") continue;
    if (isRecord(item) || Array.isArray(item)) collectArtifacts(item, conversationId, artifacts, seen);
  }
  return artifacts;
}

/** Decodes canonical saved-tool JSON for display without changing the stored log. */
function savedToolLogForDisplay(log: ToolLogEntry): ToolLogEntry {
  if (typeof log.result !== "string") return log;
  const result = parseJsonRecord(log.result);
  return result && ["success", "error"].includes(String(result.status))
    && Object.prototype.hasOwnProperty.call(result, "result") && Object.prototype.hasOwnProperty.call(result, "error")
    ? { ...log, result } : log;
}

function statusForLog(log: ToolLogEntry): ToolActivityStatus {
  const result = log.result;
  if (!isRecord(result)) return "completed";
  const data = isRecord(result.data) ? result.data : result;
  const widget = isRecord(data.widget) ? data.widget : {};
  if (
    result.status === "error" ||
    data.status === "error" ||
    data.is_error === true ||
    widget.is_error === true
  ) {
    return "failed";
  }
  return "completed";
}

function numberValue(value: unknown): number | undefined {
  const numeric = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(numeric) ? numeric : undefined;
}

function seqValue(value: unknown): number | undefined {
  const numeric = numberValue(value);
  return numeric !== undefined && numeric >= 0 ? numeric : undefined;
}

function minDefined(left: number | undefined, right: number | undefined): number | undefined {
  if (left === undefined) return right;
  if (right === undefined) return left;
  return Math.min(left, right);
}

function maxDefined(left: number | undefined, right: number | undefined): number | undefined {
  if (left === undefined) return right;
  if (right === undefined) return left;
  return Math.max(left, right);
}

function timestampValue(value: unknown): number | string | undefined {
  return typeof value === "number" || typeof value === "string" ? value : undefined;
}

function eventKey(event: ChatActivityEvent): string {
  const toolCallId = eventToolCallId(event);
  const args = eventArguments(event);
  const baseKey = toolCallId || `${eventToolName(event) || "tool"}:${JSON.stringify(args)}`;
  return attemptScopedKey(baseKey, eventAttemptGeneration(event));
}

function eventData(event: ChatActivityEvent): Record<string, unknown> {
  return isRecord(event.data) ? event.data : {};
}

function eventValue(event: ChatActivityEvent, key: string): unknown {
  return event[key] ?? eventData(event)[key];
}

function eventPayloadValue(event: ChatActivityEvent, key: string): unknown {
  const data = eventData(event);
  return data[key] ?? event[key];
}

function pickEventString(event: ChatActivityEvent, keys: string[]): string {
  for (const key of keys) {
    const value = eventValue(event, key);
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
  }
  return "";
}

function eventToolName(event: ChatActivityEvent): string {
  return pickEventString(event, ["tool_name", "toolName"]);
}

function eventToolCallId(event: ChatActivityEvent): string {
  return pickEventString(event, ["tool_call_id", "toolCallId"]);
}

function attemptValue(value: unknown): number | string | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) return value.trim();
  return undefined;
}

function eventAttempt(event: ChatActivityEvent): number | string | undefined {
  return attemptValue(eventValue(event, "provider_attempt"));
}

function eventAttemptGeneration(event: ChatActivityEvent): number | string | undefined {
  return attemptValue(eventValue(event, "provider_attempt_generation"));
}

function logAttempt(log: ToolLogEntry): number | string | undefined {
  return attemptValue(log.provider_attempt);
}

function logAttemptGeneration(log: ToolLogEntry): number | string | undefined {
  return attemptValue(log.provider_attempt_generation);
}

function attemptScopedKey(baseKey: string, generation: number | string | undefined): string {
  return generation === undefined ? baseKey : `${baseKey}::provider-attempt:${generation}`;
}

function eventArguments(event: ChatActivityEvent): Record<string, unknown> {
  const value = eventValue(event, "arguments");
  return isRecord(value) ? value : {};
}

function isToolActivityEvent(event: ChatActivityEvent): boolean {
  return (
    event.type === "tool_started" ||
    event.type === "tool_completed" ||
    event.type === "tool_call" ||
    event.type === "tool_call_started" ||
    event.type === "tool_call_completed" ||
    event.type === "tool_result" ||
    event.type === "approval_requested" ||
    event.phase === "tool_blocked" ||
    event.type === "tool_blocked" ||
    event.phase === "tool_call" ||
    event.phase === "tool_call_started" ||
    event.phase === "tool_call_completed" ||
    event.phase === "tool_started" ||
    event.phase === "tool_completed" ||
    event.phase === "tool_result" ||
    event.phase === "approval_requested"
  );
}

function isProgressActivityEvent(event: ChatActivityEvent): boolean {
  return event.type === "assistant_progress" || event.phase === "assistant_progress";
}

function eventRank(event: ChatActivityEvent): number {
  if (
    event.type === "tool_completed" ||
    event.type === "tool_call_completed" ||
    event.type === "tool_result" ||
    event.phase === "tool_completed" ||
    event.phase === "tool_call_completed" ||
    event.phase === "tool_result"
  ) {
    return 2;
  }
  if (event.type === "approval_requested" || event.phase === "approval_requested") return 1;
  return 0;
}

function activityStatusRank(status: ToolActivityStatus): number {
  if (status === "failed" || status === "blocked") return 3;
  if (status === "completed") return 2;
  if (status === "waiting_approval") return 1;
  return 0;
}

function monotonicActivityStatus(
  current: ToolActivityStatus | undefined,
  candidate: ToolActivityStatus,
): ToolActivityStatus {
  if (!current || activityStatusRank(candidate) >= activityStatusRank(current)) {
    return candidate;
  }
  return current;
}

function statusForEvent(event: ChatActivityEvent): ToolActivityStatus {
  if (event.type === "approval_requested" || event.phase === "approval_requested") return "waiting_approval";
  if (event.type === "tool_blocked" || event.phase === "tool_blocked") return "blocked";
  if (
    event.type === "tool_completed" ||
    event.type === "tool_call_completed" ||
    event.type === "tool_result" ||
    event.phase === "tool_completed" ||
    event.phase === "tool_call_completed" ||
    event.phase === "tool_result"
  ) {
    const result = eventValue(event, "result");
    if (eventValue(event, "is_error") === true || (isRecord(result) && statusForLog({ result }) === "failed")) return "failed";
    return "completed";
  }
  return "running";
}

function isStartEvent(event: ChatActivityEvent): boolean {
  return (
    event.type === "tool_started" ||
    event.type === "tool_call" ||
    event.type === "tool_call_started" ||
    event.phase === "tool_started" ||
    event.phase === "tool_call" ||
    event.phase === "tool_call_started"
  );
}

function isEndEvent(event: ChatActivityEvent): boolean {
  return (
    event.type === "tool_completed" ||
    event.type === "tool_call_completed" ||
    event.type === "tool_result" ||
    event.phase === "tool_completed" ||
    event.phase === "tool_call_completed" ||
    event.phase === "tool_result"
  );
}

function isApprovalEvent(event: ChatActivityEvent): boolean {
  return event.type === "approval_requested" || event.phase === "approval_requested";
}

function isBlockedEvent(event: ChatActivityEvent): boolean {
  return event.type === "tool_blocked" || event.phase === "tool_blocked";
}

function mergeActivityEvents(base: ChatActivityEvent, update: ChatActivityEvent): ChatActivityEvent {
  const merged: ChatActivityEvent = { ...base, ...update };
  const baseStartedAt = base.started_at ?? base.startedAt ?? (isStartEvent(base) ? base.timestamp : undefined);
  const updateStartedAt = update.started_at ?? update.startedAt ?? (isStartEvent(update) ? update.timestamp : undefined);
  const baseCompletedAt = base.completed_at ?? base.completedAt ?? (isEndEvent(base) ? base.timestamp : undefined);
  const updateCompletedAt = update.completed_at ?? update.completedAt ?? (isEndEvent(update) ? update.timestamp : undefined);
  const startedAt = baseStartedAt ?? updateStartedAt;
  const completedAt = updateCompletedAt ?? baseCompletedAt;
  if (startedAt !== undefined) merged.started_at = startedAt;
  if (completedAt !== undefined) merged.completed_at = completedAt;
  for (const key of ["arguments", "result", "artifact", "artifacts", "output", "message", "timestamp"]) {
    if (merged[key] === undefined && base[key] !== undefined) {
      merged[key] = base[key];
    }
  }
  return merged;
}

function resultValueForEvent(event: ChatActivityEvent): unknown {
  return eventValue(event, "result") ?? eventValue(event, "output") ?? eventValue(event, "artifact") ?? eventValue(event, "artifacts");
}

function durationLabelFromLog(log: ToolLogEntry): string {
  const result = isRecord(log.result) ? log.result : {};
  const data = isRecord(result.data) ? result.data : result;
  const directMs = Number(data.duration_ms ?? data.elapsed_ms ?? result.duration_ms ?? result.elapsed_ms);
  if (Number.isFinite(directMs) && directMs >= 0) return formatCompactDuration(directMs);
  const seconds = Number(data.duration_seconds ?? data.elapsed_seconds ?? result.duration_seconds ?? result.elapsed_seconds);
  if (Number.isFinite(seconds) && seconds >= 0) return formatCompactDuration(seconds * 1000);
  return boundedDurationLabel(data.started_at ?? result.started_at, data.completed_at ?? data.finished_at ?? result.completed_at ?? result.finished_at);
}

function durationLabelFromEvent(event: ChatActivityEvent, status: ToolActivityStatus, now = Date.now()): string {
  const startedAt = event.started_at ?? event.startedAt ?? (isStartEvent(event) ? event.timestamp : undefined);
  const completedAt = event.completed_at ?? event.completedAt ?? (isEndEvent(event) ? event.timestamp : undefined);
  if (status === "running") return elapsedDurationLabel(startedAt, now);
  return boundedDurationLabel(startedAt, completedAt);
}

function collectEventArtifacts(event: ChatActivityEvent, conversationId?: string): ToolActivityArtifact[] {
  const artifacts: ToolActivityArtifact[] = [];
  const seen = new Set<string>();
  collectArtifacts(eventValue(event, "result"), conversationId, artifacts, seen);
  collectArtifacts(eventValue(event, "artifact"), conversationId, artifacts, seen);
  collectArtifacts(eventValue(event, "artifacts"), conversationId, artifacts, seen);
  collectArtifacts(eventValue(event, "output"), conversationId, artifacts, seen);
  return artifacts;
}

function collectCallArtifacts(log: ToolLogEntry | undefined, event: ChatActivityEvent | undefined, conversationId?: string): ToolActivityArtifact[] {
  const artifacts: ToolActivityArtifact[] = [];
  const seen = new Set<string>();
  if (log) collectArtifacts(log.result, conversationId, artifacts, seen);
  if (event) {
    collectArtifacts(eventValue(event, "result"), conversationId, artifacts, seen);
    collectArtifacts(eventValue(event, "artifact"), conversationId, artifacts, seen);
    collectArtifacts(eventValue(event, "artifacts"), conversationId, artifacts, seen);
    collectArtifacts(eventValue(event, "output"), conversationId, artifacts, seen);
  }
  return artifacts;
}

type AccumulatedToolCall = {
  key: string;
  event?: ChatActivityEvent;
  log?: ToolLogEntry;
  orderIndex: number;
  startSeq?: number;
  endSeq?: number;
  startedAt?: number | string;
  completedAt?: number | string;
  status?: ToolActivityStatus;
  providerAttempt?: number | string;
  providerAttemptGeneration?: number | string;
};

function eventStartedAt(event: ChatActivityEvent): number | string | undefined {
  return timestampValue(event.started_at) ?? timestampValue(event.startedAt) ?? (isStartEvent(event) ? event.timestamp : undefined);
}

function eventCompletedAt(event: ChatActivityEvent): number | string | undefined {
  return timestampValue(event.completed_at) ?? timestampValue(event.completedAt) ?? (isEndEvent(event) ? event.timestamp : undefined);
}

function updateAccumulatedCallFromEvent(call: AccumulatedToolCall, event: ChatActivityEvent, index: number): void {
  const seq = seqValue(event.seq);
  call.orderIndex = Math.min(call.orderIndex, index);
  call.providerAttempt = call.providerAttempt ?? eventAttempt(event);
  call.providerAttemptGeneration = call.providerAttemptGeneration ?? eventAttemptGeneration(event);
  if (isStartEvent(event)) {
    call.startSeq = minDefined(call.startSeq, seq);
    call.startedAt = call.startedAt ?? eventStartedAt(event);
  }
  if (isEndEvent(event) || isApprovalEvent(event) || isBlockedEvent(event)) {
    call.endSeq = maxDefined(call.endSeq, seq);
    call.completedAt = call.completedAt ?? eventCompletedAt(event) ?? event.timestamp;
  }
  if (call.startSeq === undefined && seq !== undefined && !call.event) {
    call.startSeq = seq;
  }
  call.status = monotonicActivityStatus(call.status, statusForEvent(event));
  const existing = call.event;
  if (!existing || eventRank(event) >= eventRank(existing)) {
    call.event = existing ? mergeActivityEvents(existing, event) : event;
  } else {
    call.event = mergeActivityEvents(event, existing);
  }
}

function updateAccumulatedCallFromLog(call: AccumulatedToolCall, log: ToolLogEntry, index: number, eventCount: number): void {
  call.log = log;
  call.providerAttempt = call.providerAttempt ?? logAttempt(log);
  call.providerAttemptGeneration = call.providerAttemptGeneration ?? logAttemptGeneration(log);
  call.orderIndex = Math.min(call.orderIndex, eventCount + index);
  call.completedAt = call.completedAt ?? log.timestamp;
  call.endSeq = maxDefined(call.endSeq, seqValue(log.seq));
  call.status = monotonicActivityStatus(call.status, statusForLog(log));
  if (call.startSeq === undefined) call.startSeq = seqValue(log.seq);
  if (call.startedAt === undefined && !call.event) call.startedAt = log.timestamp;
}

function logKey(log: ToolLogEntry): string {
  const id = typeof log.tool_call_id === "string" ? log.tool_call_id.trim() : "";
  const baseKey = id || `${log.tool_name ?? "tool"}:${JSON.stringify(log.arguments ?? {})}`;
  return attemptScopedKey(baseKey, logAttemptGeneration(log));
}

function statusForProgressEvent(event: ChatActivityEvent): ToolActivityStatus {
  const status = String(eventPayloadValue(event, "status") ?? "").toLowerCase();
  if (status === "completed" || status === "complete" || status === "done") return "completed";
  if (status === "failed" || status === "error") return "failed";
  if (status === "blocked") return "blocked";
  return "running";
}

function progressItemFromEvent(event: ChatActivityEvent, index: number): ProgressActivityItem {
  const seq = seqValue(event.seq);
  const status = statusForProgressEvent(event);
  const summary = pickEventString(event, ["summary", "display_text", "display_summary", "message"]) || "作業状況";
  const nextAction = pickEventString(event, ["next_action", "nextAction", "next_step", "nextStep"]);
  const timestampValue = event.timestamp;
  return {
    id: `progress-${seq ?? index}`,
    kind: "progress",
    runId: typeof event.run_id === "string" ? event.run_id : undefined,
    startSeq: seq,
    endSeq: status === "running" ? undefined : seq,
    startedAt: timestampValue,
    completedAt: status === "running" ? undefined : timestampValue,
    folder: "progress",
    folderLabel: "作業状況",
    title: compact(summary, 120),
    detail: "",
    nextStep: nextAction ? `次: ${compact(nextAction, 120)}` : undefined,
    nextAction: nextAction ? compact(nextAction, 120) : undefined,
    status,
    timestamp: timestampValue,
    orderIndex: index,
    phase: String(eventPayloadValue(event, "phase") ?? ""),
  };
}

function durationLabelFromActivity(
  startedAt: number | string | undefined,
  completedAt: number | string | undefined,
  status: ToolActivityStatus,
  fallbackLog: ToolLogEntry | undefined,
  now = Date.now(),
): string {
  if (startedAt !== undefined || completedAt !== undefined) {
    if (status === "running") return elapsedDurationLabel(startedAt, now);
    return boundedDurationLabel(startedAt, completedAt);
  }
  return fallbackLog ? durationLabelFromLog(fallbackLog) : "";
}

function itemOrderSeq(item: RunActivityItem): number | undefined {
  return item.startSeq ?? item.endSeq;
}

export function compareActivityOrder(left: RunActivityItem, right: RunActivityItem): number {
  const leftSeq = itemOrderSeq(left);
  const rightSeq = itemOrderSeq(right);
  if (leftSeq !== undefined && rightSeq !== undefined && leftSeq !== rightSeq) return leftSeq - rightSeq;
  if (leftSeq !== undefined && rightSeq === undefined) return -1;
  if (leftSeq === undefined && rightSeq !== undefined) return 1;

  const leftTime = timestampMs(left.startedAt ?? left.timestamp);
  const rightTime = timestampMs(right.startedAt ?? right.timestamp);
  if (leftTime !== null && rightTime !== null && leftTime !== rightTime) return leftTime - rightTime;
  if (leftTime !== null && rightTime === null) return -1;
  if (leftTime === null && rightTime !== null) return 1;

  const leftIndex = left.orderIndex ?? 0;
  const rightIndex = right.orderIndex ?? 0;
  if (leftIndex !== rightIndex) return leftIndex - rightIndex;
  return String(left.toolCallId ?? left.id).localeCompare(String(right.toolCallId ?? right.id));
}

function toolItemFromCall(call: AccumulatedToolCall, options: { conversationId?: string; now?: number }): ToolActivityItem | null {
  const source = call.log ?? call.event;
  if (!source) return null;
  const event = call.event;
  const log = call.log;
  const toolName = String(log?.tool_name ?? (event ? eventToolName(event) : "")).trim();
  if (!toolName) return null;
  const args = (log?.arguments && typeof log.arguments === "object")
    ? log.arguments as Record<string, unknown>
    : event
      ? eventArguments(event)
      : {};
  const folder = activityFolderFor(toolName, args);
  const argumentSummary = summarizeToolArguments(toolName, args);
  const status = call.status ?? (log ? statusForLog(log) : event ? statusForEvent(event) : "completed");
  const resultSource = log ? log.result : event ? resultValueForEvent(event) : undefined;
  const resultSummary = summarizeToolResult(toolName, resultSource);
  const explicitSummary = log && isRecord(log.result) ? explicitToolText(log.result) : event ? pickEventString(event, ["display_text", "display_summary", "summary", "result_summary"]) : "";
  const eventMessage = event ? pickEventString(event, ["message"]) : "";
  const defaultDetail =
    status === "running"
      ? "使用中"
      : status === "waiting_approval"
        ? "承認待ち"
        : status === "blocked"
          ? "停止しました"
          : status === "failed"
            ? "失敗"
            : "";
  const fallbackDetail = status === "completed" && isGenericCompletionSummary(eventMessage || defaultDetail, toolName)
    ? ""
    : eventMessage || defaultDetail;
  const supported = isSupportedToolActivity(toolName, explicitSummary);
  const title = humanToolTitle(toolName, args, folder.label, argumentSummary);
  const startedAt = call.startedAt ?? (event ? eventStartedAt(event) : undefined);
  const completedAt = call.completedAt ?? (event ? eventCompletedAt(event) : undefined) ?? log?.timestamp;
  const timestampValue = startedAt ?? completedAt ?? event?.timestamp ?? log?.timestamp;
  return {
    id: `tool-${call.startSeq ?? call.orderIndex}-${call.key}`,
    kind: "tool",
    runId: typeof event?.run_id === "string" ? event.run_id : undefined,
    toolName,
    toolCallId: log?.tool_call_id ?? (event ? eventToolCallId(event) : undefined),
    providerAttempt: call.providerAttempt,
    providerAttemptGeneration: call.providerAttemptGeneration,
    startSeq: call.startSeq,
    endSeq: call.endSeq,
    startedAt,
    completedAt,
    folder: folder.id,
    folderLabel: folder.label,
    input: argumentSummary,
    title,
    detail: explicitSummary || resultSummary || fallbackDetail,
    durationLabel: durationLabelFromActivity(startedAt, completedAt, status, log, options.now),
    nextStep: event ? pickEventString(event, ["next_step", "nextStep"]) : undefined,
    nextAction: event ? pickEventString(event, ["next_action", "nextAction", "next_step", "nextStep"]) : undefined,
    status,
    timestamp: timestampValue,
    orderIndex: call.orderIndex,
    artifacts: collectCallArtifacts(log, event, options.conversationId),
    supported,
    rawJson: undefined,
  };
}

export function buildToolActivityGroups(
  toolLogs: ToolLogEntry[] = [],
  events: ChatActivityEvent[] = [],
  options: { conversationId?: string; now?: number } = {},
): ToolActivityGroup[] {
  const items = buildToolActivityItems(toolLogs, events, options).filter((item): item is ToolActivityItem => item.kind === "tool");
  const groups: ToolActivityGroup[] = [];
  const folderCounts = new Map<string, number>();
  for (const item of items) {
    const previous = groups[groups.length - 1];
    if (previous && previous.label === item.folderLabel && previous.items[0]?.folder === item.folder) {
      previous.items.push(item);
      continue;
    }
    const count = folderCounts.get(item.folder) ?? 0;
    folderCounts.set(item.folder, count + 1);
    groups.push({
      id: count === 0 ? item.folder : `${item.folder}-${count}`,
      label: item.folderLabel,
      items: [item],
    });
  }
  return groups;
}

export function buildToolActivityItems(
  toolLogs: ToolLogEntry[] = [],
  events: ChatActivityEvent[] = [],
  options: { conversationId?: string; now?: number } = {},
): Array<ToolActivityItem | ProgressActivityItem> {
  const calls = new Map<string, AccumulatedToolCall>();
  const progressItems: ProgressActivityItem[] = [];

  events.forEach((event, index) => {
    if (isProgressActivityEvent(event)) {
      progressItems.push(progressItemFromEvent(event, index));
      return;
    }
    if (!isToolActivityEvent(event) || !eventToolName(event)) return;
    const key = eventKey(event);
    const existing = calls.get(key) ?? { key, orderIndex: index };
    updateAccumulatedCallFromEvent(existing, event, index);
    calls.set(key, existing);
  });

  toolLogs.forEach((storedLog, index) => {
    const log = savedToolLogForDisplay(storedLog);
    if (typeof log.tool_name !== "string" || !log.tool_name.trim()) return;
    const key = logKey(log);
    const existing = calls.get(key) ?? { key, orderIndex: events.length + index };
    updateAccumulatedCallFromLog(existing, log, index, events.length);
    calls.set(key, existing);
  });

  const toolItems = [...calls.values()]
    .map((call) => toolItemFromCall(call, options))
    .filter((item): item is ToolActivityItem => item !== null);
  return [...toolItems, ...progressItems].sort(compareActivityOrder);
}
