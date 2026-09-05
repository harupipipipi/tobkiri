import type { ToolPreviewItem } from "../components/ToolPreview";
import {
  conversationArtifactFileUrl,
  defaultspackCanonicalRouteKey,
  type ChatActivityEvent,
  type ChatMessage,
} from "./api";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
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

function isHtmlPath(path: string): boolean {
  return /\.(html?|xhtml)$/i.test(path);
}

function isDiffPath(path: string): boolean {
  return /\.(diff|patch)$/i.test(path);
}

function isImageDataUrl(value: string): boolean {
  return /^data:image\/[a-z0-9.+-]+;base64,/i.test(value);
}

function dataUrlName(value: string): string {
  const match = value.match(/^data:image\/([a-z0-9.+-]+);/i);
  const extension = match?.[1]?.replace("jpeg", "jpg").split("+")[0] || "png";
  return `screenshot.${extension}`;
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

const PREVIEW_URL_KEYS = new Set([
  "url",
  "page_url",
  "pageUrl",
  "preview_url",
  "previewUrl",
  "local_url",
  "localUrl",
  "current_url",
  "currentUrl",
]);

export function collectArtifactPaths(value: unknown, paths: string[] = [], seen = new Set<string>()): string[] {
  if (Array.isArray(value)) {
    value.forEach((item) => collectArtifactPaths(item, paths, seen));
    return paths;
  }
  if (!isRecord(value)) return paths;

  const preferredPath = stringValue(value.model_image_path)
    || stringValue(value.screenshot_path)
    || stringValue(value.workspace_path)
    || stringValue(value.path);
  if (preferredPath && !seen.has(preferredPath)) {
    seen.add(preferredPath);
    paths.push(preferredPath);
  }
  Object.entries(value).forEach(([key, entry]) => {
    if (key === "path" || key === "workspace_path" || key === "screenshot_path" || key === "model_image_path" || key === "data_url" || key === "dataUrl") return;
    collectArtifactPaths(entry, paths, seen);
  });
  return paths;
}

export function collectInlineImageUrls(value: unknown, urls: string[] = [], seen = new Set<string>()): string[] {
  if (Array.isArray(value)) {
    value.forEach((item) => collectInlineImageUrls(item, urls, seen));
    return urls;
  }
  if (!isRecord(value)) return urls;

  const dataUrl = stringValue(value.data_url) || stringValue(value.dataUrl);
  if (dataUrl && isImageDataUrl(dataUrl) && !seen.has(dataUrl)) {
    seen.add(dataUrl);
    urls.push(dataUrl);
  }
  Object.entries(value).forEach(([key, entry]) => {
    if (key === "data_url" || key === "dataUrl") return;
    collectInlineImageUrls(entry, urls, seen);
  });
  return urls;
}

function isPreviewableUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function isLocalPreviewUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    return host === "localhost" || host === "127.0.0.1" || host === "0.0.0.0" || host === "::1" || host === "[::1]";
  } catch {
    return false;
  }
}

function normalizePreviewUrl(value: string): string {
  try {
    const url = new URL(value);
    url.hash = "";
    return url.href;
  } catch {
    return value.trim();
  }
}

export function isHumanOperatorCanvasPreview(preview: ToolPreviewItem): boolean {
  if (preview.data.type !== "web") return false;
  return normalizePreviewUrl(preview.data.url).includes(
    defaultspackCanonicalRouteKey("api/human-operator/conversations/"),
  );
}

export function collectPreviewUrls(value: unknown, urls: string[] = [], seen = new Set<string>()): string[] {
  if (Array.isArray(value)) {
    value.forEach((item) => collectPreviewUrls(item, urls, seen));
    return urls;
  }
  if (!isRecord(value)) return urls;

  for (const key of PREVIEW_URL_KEYS) {
    const url = stringValue(value[key]);
    if (url && isPreviewableUrl(url) && isLocalPreviewUrl(url)) {
      const normalized = normalizePreviewUrl(url);
      if (!seen.has(normalized)) {
        seen.add(normalized);
        urls.push(normalized);
      }
    }
  }
  Object.entries(value).forEach(([key, entry]) => {
    if (PREVIEW_URL_KEYS.has(key) || key === "href" || key === "dom_snapshot" || key === "domSnapshot") return;
    collectPreviewUrls(entry, urls, seen);
  });
  return urls;
}

function failedStatus(value: unknown): boolean {
  const status = String(value ?? "").trim().toLowerCase();
  return status === "error" || status === "failed" || status === "failure" || status === "denied" || status === "rejected" || status === "cancelled" || status === "canceled";
}

function toolResultFailed(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(toolResultFailed);
  if (!isRecord(value)) return false;
  if (value.is_error === true || value.ok === false || value.success === false) return true;
  if (failedStatus(value.status) || failedStatus(value.phase) || failedStatus(value.outcome)) return true;
  if (isRecord(value.data) && toolResultFailed(value.data)) return true;
  if (isRecord(value.result) && toolResultFailed(value.result)) return true;
  return false;
}

function toolResultPendingApproval(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(toolResultPendingApproval);
  if (!isRecord(value)) return false;
  if (value.approval_required === true || value.requires_approval === true) return true;
  const status = String(value.status ?? value.phase ?? value.outcome ?? "").trim().toLowerCase();
  if (status === "approval_required" || status === "requires_approval" || status === "pending_approval") return true;
  if (isRecord(value.widget) && toolResultPendingApproval(value.widget)) return true;
  if (isRecord(value.data) && toolResultPendingApproval(value.data)) return true;
  if (isRecord(value.result) && toolResultPendingApproval(value.result)) return true;
  if (isRecord(value.output) && toolResultPendingApproval(value.output)) return true;
  return false;
}

export function streamActivityEventKey(event: ChatActivityEvent): string {
  const callId = typeof event.tool_call_id === "string" ? event.tool_call_id.trim() : "";
  const runId = typeof event.run_id === "string" ? event.run_id.trim() : "";
  const conversationId = typeof event.conversation_id === "string" ? event.conversation_id.trim() : "";
  const stateRevision = Number(event.state_revision ?? -1);
  if (
    event.type === "browser_state_invalidated"
    || event.type === "browser_state_snapshot"
    || event.type === "browser_dom_snapshot"
    || event.type === "browser_screenshot"
  ) {
    const revisionPart = Number.isFinite(stateRevision) && stateRevision >= 0 ? `:${stateRevision}` : "";
    const payload = event.snapshot ?? event.dom_snapshot ?? event.screenshot ?? event.invalidated;
    const identity = isRecord(payload)
      ? stringValue(payload.path) || stringValue(payload.model_image_path) || stringValue(payload.url)
      : "";
    const scope = `${conversationId}:${runId}`;
    if (callId) return `call:${scope}:${callId}:${event.type}${revisionPart}:${identity}`;
    return `browser:${scope}:${event.type}${revisionPart}:${identity}`;
  }
  if (callId) return `call:${callId}`;
  const toolName = typeof event.tool_name === "string" ? event.tool_name.trim() : "";
  const args = event.arguments && typeof event.arguments === "object" ? event.arguments : {};
  if (toolName) return `tool:${toolName}:${JSON.stringify(args)}`;
  return `event:${event.type}:${event.phase ?? ""}:${event.message ?? ""}`;
}

function isToolStartEvent(event: ChatActivityEvent): boolean {
  return (
    event.type === "tool_call" ||
    event.type === "tool_call_started" ||
    event.phase === "tool_call" ||
    event.phase === "tool_call_started"
  );
}

function isToolEndEvent(event: ChatActivityEvent): boolean {
  return (
    event.type === "tool_call_completed" ||
    event.type === "tool_result" ||
    event.phase === "tool_call_completed" ||
    event.phase === "tool_result"
  );
}

export function mergeStreamActivityEvent(base: ChatActivityEvent, update: ChatActivityEvent): ChatActivityEvent {
  const baseRevision = Number(base.state_revision ?? -1);
  const updateRevision = Number(update.state_revision ?? -1);
  const baseScope = `${String(base.conversation_id ?? "")}:${String(base.run_id ?? "")}:${String(base.tool_call_id ?? base.tool_name ?? "")}`;
  const updateScope = `${String(update.conversation_id ?? "")}:${String(update.run_id ?? "")}:${String(update.tool_call_id ?? update.tool_name ?? "")}`;
  if (
    baseScope === updateScope
    && Number.isFinite(baseRevision)
    && Number.isFinite(updateRevision)
    && updateRevision < baseRevision
  ) {
    return base;
  }
  const merged: ChatActivityEvent = { ...base, ...update };
  const baseStartedAt = base.started_at ?? base.startedAt ?? (isToolStartEvent(base) ? base.timestamp : undefined);
  const updateStartedAt = update.started_at ?? update.startedAt ?? (isToolStartEvent(update) ? update.timestamp : undefined);
  const baseCompletedAt = base.completed_at ?? base.completedAt ?? (isToolEndEvent(base) ? base.timestamp : undefined);
  const updateCompletedAt = update.completed_at ?? update.completedAt ?? (isToolEndEvent(update) ? update.timestamp : undefined);
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

export function upsertStreamActivityEvent(events: ChatActivityEvent[], nextEvent: ChatActivityEvent): ChatActivityEvent[] {
  const key = streamActivityEventKey(nextEvent);
  const index = events.findIndex((event) => streamActivityEventKey(event) === key);
  if (index === -1) return [...events, nextEvent];
  return events.map((event, eventIndex) => (
    eventIndex === index ? mergeStreamActivityEvent(event, nextEvent) : event
  ));
}

function resultValuesForToolEvent(event: ChatActivityEvent): unknown[] {
  return [
    event.result,
    event.artifact,
    event.artifacts,
    event.output,
    event.invalidated,
    event.snapshot,
    event.screenshot,
  ].filter((value) => value !== undefined && !toolResultFailed(value) && !toolResultPendingApproval(value));
}

function artifactPreview(
  {
    id,
    toolStepId,
    timestamp,
    conversationId,
    path,
}: {
  id: string;
  toolStepId: string;
  timestamp: number;
  conversationId: string;
  path: string;
}): ToolPreviewItem {
  const name = basename(path);
  const url = conversationArtifactFileUrl(conversationId, path);
  if (isHtmlPath(path)) {
    return {
      id,
      toolStepId,
      timestamp,
      data: {
        type: "file" as const,
        filename: name,
        size: "HTML preview",
        path,
        url,
        downloadName: name,
        mimeType: "text/html",
      },
    };
  }
  if (isDiffPath(path)) {
    return {
      id,
      toolStepId,
      timestamp,
      data: {
        type: "file" as const,
        filename: name,
        size: "diff",
        path,
        url,
        downloadName: name,
        mimeType: "text/x-diff",
      },
    };
  }
  return {
    id,
    toolStepId,
    timestamp,
    data: isImagePath(path)
      ? {
          type: "image" as const,
          url,
          alt: name,
          path,
        }
      : {
          type: "file" as const,
          filename: name,
          size: "tool artifact",
          path,
          url,
          downloadName: name,
        },
  };
}

function webPreview({
  id,
  toolStepId,
  timestamp,
  url,
}: {
  id: string;
  toolStepId: string;
  timestamp: number;
  url: string;
}): ToolPreviewItem {
  return {
    id,
    toolStepId,
    timestamp,
    data: {
      type: "web" as const,
      url,
      title: url,
    },
  };
}

function inlineImagePreview(
  {
    id,
    toolStepId,
    timestamp,
    url,
}: {
  id: string;
  toolStepId: string;
  timestamp: number;
  url: string;
}): ToolPreviewItem {
  return {
    id,
    toolStepId,
    timestamp,
    data: {
      type: "image" as const,
      url,
      alt: dataUrlName(url),
    },
  };
}

function previewIdentity(preview: ToolPreviewItem): string {
  const data = preview.data;
  if (data.type === "web") return `web:${normalizePreviewUrl(data.url)}`;
  if (data.type === "image") return `image:${data.path || data.url || data.alt}`;
  if (data.type === "file") return `file:${data.path || data.url || `${data.filename}:${data.content ?? ""}`}`;
  return `code:${data.filename}:${data.diff ?? data.content ?? ""}`;
}

function dedupePreviewItems(items: ToolPreviewItem[]): ToolPreviewItem[] {
  const seen = new Set<string>();
  const deduped: ToolPreviewItem[] = [];
  for (const item of items) {
    const identity = previewIdentity(item);
    if (seen.has(identity)) continue;
    seen.add(identity);
    deduped.push(item);
  }
  return deduped;
}

export function toolPreviewsFromMessages(messages: ChatMessage[]): ToolPreviewItem[] {
  const previews = messages.flatMap((message) => {
    const logPreviews = (message.tool_logs ?? []).flatMap((log, index) => {
      if (toolResultFailed(log.result) || toolResultPendingApproval(log.result)) return [];
      const toolName = String(log.tool_name ?? "tool");
      const toolStepId = typeof log.tool_call_id === "string" && log.tool_call_id.trim()
        ? log.tool_call_id.trim()
        : toolName;
      const timestamp = typeof log.timestamp === "number" ? log.timestamp : message.created_at;
      const artifactPreviews = uniqueStrings(collectArtifactPaths(log.result)).map((path, artifactIndex) => artifactPreview({
        id: `message-tool-artifact-${message.id}-${index}-${artifactIndex}`,
        toolStepId,
        timestamp: timestamp + artifactIndex + 0.1,
        conversationId: message.conversation_id,
        path,
      }));
      const inlinePreviews = uniqueStrings(collectInlineImageUrls(log.result)).map((url, imageIndex) => inlineImagePreview({
        id: `message-tool-inline-${message.id}-${index}-${imageIndex}`,
        toolStepId,
        timestamp: timestamp + artifactPreviews.length + imageIndex + 0.1,
        url,
      }));
      const webPreviews = uniqueStrings(collectPreviewUrls(log.result)).map((url, urlIndex) => webPreview({
        id: `message-tool-url-${message.id}-${index}-${urlIndex}`,
        toolStepId,
        timestamp: timestamp + artifactPreviews.length + inlinePreviews.length + urlIndex + 0.1,
        url,
      }));
      return [...artifactPreviews, ...inlinePreviews, ...webPreviews];
    });

    const logKeys = new Set((message.tool_logs ?? []).map((log) => {
      const callId = typeof log.tool_call_id === "string" ? log.tool_call_id.trim() : "";
      if (callId) return `call:${callId}`;
      return `tool:${log.tool_name ?? "tool"}:${JSON.stringify(log.arguments ?? {})}`;
    }));
    const eventMap = new Map<string, ChatActivityEvent>();
    for (const event of message.events ?? []) {
      if (typeof event.tool_name !== "string" || !event.tool_name.trim()) continue;
      const key = streamActivityEventKey(event);
      if (logKeys.has(key)) continue;
      const existing = eventMap.get(key);
      eventMap.set(key, existing ? mergeStreamActivityEvent(existing, event) : event);
    }
    const eventPreviews = [...eventMap.values()].flatMap((event, index) => {
      if (toolResultFailed(event) || toolResultPendingApproval(event)) return [];
      const values = resultValuesForToolEvent(event);
      if (values.length === 0) return [];
      const toolName = String(event.tool_name ?? "tool");
      const toolStepId = String(event.tool_call_id ?? toolName);
      const timestamp = typeof event.timestamp === "number" ? event.timestamp : message.created_at + index + 0.01;
      const eventKey = streamActivityEventKey(event);
      const fileArtifacts = uniqueStrings(values.flatMap((value) => collectArtifactPaths(value)));
      const pathPreviews = fileArtifacts.map((path, artifactIndex) => artifactPreview({
        id: `message-tool-event-artifact-${message.id}-${eventKey}-${artifactIndex}`,
        toolStepId,
        timestamp: timestamp + artifactIndex + 0.1,
        conversationId: message.conversation_id,
        path,
      }));
      const inlinePreviews = uniqueStrings(values.flatMap((value) => collectInlineImageUrls(value))).map((url, imageIndex) => inlineImagePreview({
        id: `message-tool-event-inline-${message.id}-${eventKey}-${imageIndex}`,
        toolStepId,
        timestamp: timestamp + fileArtifacts.length + imageIndex + 0.1,
        url,
      }));
      const urlPreviews = uniqueStrings(values.flatMap((value) => collectPreviewUrls(value))).map((url, urlIndex) => webPreview({
        id: `message-tool-event-url-${message.id}-${eventKey}-${urlIndex}`,
        toolStepId,
        timestamp: timestamp + fileArtifacts.length + inlinePreviews.length + urlIndex + 0.1,
        url,
      }));
      return [...pathPreviews, ...inlinePreviews, ...urlPreviews];
    });

    return [...logPreviews, ...eventPreviews];
  }).sort((a, b) => b.timestamp - a.timestamp);
  return dedupePreviewItems(previews);
}
