import { Box, Calculator, ChevronRight, CircleAlert, Clock, Copy, ExternalLink, FileText, GitBranch, Globe2, Image as ImageIcon, Loader2, Monitor, RefreshCw, Terminal, Wrench, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ArtifactPreviewDialog, type ArtifactPreviewDialogItem } from "../components/ArtifactPreviewDialog";
import { ErrorCopyAction, ErrorNotice, copyTextWithFallback } from "../components/ErrorNotice";
import { PromptUsageDisclosure } from "../components/prompts/PromptUsageDisclosure";
import { cn } from "../lib/cn";
import { elapsedDurationLabel, formatCompactDuration, timestampMs } from "../lib/duration";
import { buildToolActivityGroups, buildToolActivityItems, toolFolderFor, type RunActivityItem, type ToolActivityGroup, type ToolActivityItem, type ToolActivityStatus } from "../lib/toolActivity";
import type { ChatActivityEvent, ChatContentBlock } from "../lib/api";
import {
  AUTHORITY_FOLLOWUP_TEXT,
  AUTHORITY_WAITING_TEXT,
  sanitizeAssistantAuthorityBoilerplate,
} from "../lib/authorityApproval";
import { chatMessageResources, type BrowserScreenshot } from "../features/chat/resources/chatMessageResources";
import { classifyChatLink, openChatLink } from "../lib/chatLinkPolicy";
import { classifyUntrustedImageUrl, extractImageBlockUrl, imageBlockAttachmentId } from "../lib/untrustedImagePolicy";
import { safeUnknownBlockDetails } from "../lib/chatBlockPresentation";
import type { ChatMessagesRendererProps } from "./types";

export { AUTHORITY_FOLLOWUP_TEXT, sanitizeAssistantAuthorityBoilerplate };

type ImagePreviewDetail = {
  label: string;
  value: string;
};

type ImagePreviewRequest = {
  src: string;
  title: string;
  alt: string;
  subtitle?: string;
  href?: string;
  details?: ImagePreviewDetail[];
};

const LOG_PREVIEW_MIN_CHARS = 1200;
const LOG_PREVIEW_MAX_CHARS = 2200;
const LOG_PREVIEW_HEAD_CHARS = 1300;
const LOG_PREVIEW_TAIL_CHARS = 620;
const AUTHORITY_PENDING_TITLE = "承認待ち";
const AUTHORITY_PENDING_DETAIL = "別ウィンドウで承認してください";
const markdownPlugins = [remarkGfm];
const LOG_LIKE_TOKENS = [
  "\\n",
  "\"stdout\"",
  "\"stderr\"",
  "\"exit_code\"",
  "\"classification\"",
  "\"risk_reasons\"",
  "\"cwd\"",
  "approval_required",
  "coding_terminal_exec",
  "subprocess.run",
  "rootdir:",
  "pytest",
  "Traceback",
  "platform ",
];

type CompactLogPreview = {
  omitted: boolean;
  omittedChars: number;
  text: string;
};

type ToolActivityTraySummary = {
  durationLabel: string;
  failedCount: number;
  itemCount: number;
  label: string;
  nextAction: string;
  runningCount: number;
  visibleTitle: string;
};

type MessageToolActivityState = {
  items: RunActivityItem[];
  hasRunningItems: boolean;
  summary: ToolActivityTraySummary;
};

type TaskDurationState = {
  label: string;
  running: boolean;
};

function activityEventValue(event: ChatActivityEvent, key: string): unknown {
  const data = isRecord(event.data) ? event.data : {};
  return event[key] ?? data[key];
}

function firstTimestamp(values: unknown[]): number | null {
  let first: number | null = null;
  for (const value of values) {
    const timestamp = timestampMs(value);
    if (timestamp === null) continue;
    first = first === null ? timestamp : Math.min(first, timestamp);
  }
  return first;
}

function lastTimestamp(values: unknown[]): number | null {
  let last: number | null = null;
  for (const value of values) {
    const timestamp = timestampMs(value);
    if (timestamp === null) continue;
    last = last === null ? timestamp : Math.max(last, timestamp);
  }
  return last;
}

function humanTaskDuration(durationMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
  const days = Math.floor(totalSeconds / 86_400);
  const hours = Math.floor((totalSeconds % 86_400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (days > 0) return hours > 0 ? `${days}日${hours}時間` : `${days}日`;
  if (hours > 0) return minutes > 0 ? `${hours}時間${minutes}分` : `${hours}時間`;
  if (minutes > 0) return seconds > 0 ? `${minutes}分${seconds}秒` : `${minutes}分`;
  return `${seconds}秒`;
}

function humanTaskDurationLabel(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  const durationMs = compactDurationMs(text);
  return durationMs === null ? text : humanTaskDuration(durationMs);
}

function taskDurationTimestamps(
  message: ChatMessagesRendererProps["messages"][number],
  items: RunActivityItem[],
): { completedAt: number | null; startedAt: number | null } {
  const metadata = messageMetadataRecord(message);
  const startValues: unknown[] = [
    metadata.thinking_started_at,
    metadata.thinkingStartedAt,
    metadata.request_started_at,
    metadata.requestStartedAt,
    metadata.started_at,
    metadata.startedAt,
    message.createdAt,
  ];
  const completedValues: unknown[] = [
    metadata.completed_at,
    metadata.completedAt,
    metadata.finished_at,
    metadata.finishedAt,
  ];

  for (const event of message.events ?? []) {
    startValues.push(
      activityEventValue(event, "started_at"),
      activityEventValue(event, "startedAt"),
      event.timestamp,
    );
    completedValues.push(
      activityEventValue(event, "completed_at"),
      activityEventValue(event, "completedAt"),
      activityEventValue(event, "finished_at"),
      activityEventValue(event, "finishedAt"),
      event.timestamp,
    );
  }

  for (const log of message.toolLogs ?? []) {
    startValues.push(log.started_at, log.startedAt);
    completedValues.push(log.completed_at, log.completedAt, log.finished_at, log.finishedAt, log.timestamp);
  }

  for (const item of items) {
    startValues.push(item.startedAt, item.timestamp);
    completedValues.push(item.completedAt, item.timestamp);
  }

  return {
    completedAt: lastTimestamp(completedValues),
    startedAt: firstTimestamp(startValues),
  };
}

export function taskDurationForMessage(
  message: ChatMessagesRendererProps["messages"][number],
  items: RunActivityItem[] = [],
  now = Date.now(),
  running = isAwaitingStreamFinalMessage(message),
): TaskDurationState | null {
  const { completedAt, startedAt } = taskDurationTimestamps(message, items);
  const metadataDuration = humanTaskDurationLabel(message.metadata?.thinkingDuration);
  const duration = running && startedAt !== null
    ? humanTaskDuration(now - startedAt)
    : metadataDuration || (startedAt !== null && completedAt !== null && completedAt >= startedAt
      ? humanTaskDuration(completedAt - startedAt)
      : "");
  if (!duration) return null;
  return {
    label: `${running ? "実行中" : "実行時間"} ${duration}`,
    running,
  };
}

function attemptGeneration(value: unknown): number | string | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) return value.trim();
  return undefined;
}

export function toolActivityPreviewKey(
  toolCallId: string,
  providerAttemptGeneration?: number | string,
): string {
  const callId = String(toolCallId ?? "").trim();
  return providerAttemptGeneration === undefined
    ? callId
    : `${callId}::provider-attempt:${providerAttemptGeneration}`;
}

export function previewableToolActivityKeys(events: ChatActivityEvent[]): Set<string> {
  const keys = new Set<string>();
  for (const event of events) {
    if (
      event.type !== "browser_screenshot"
      && event.type !== "browser_state_snapshot"
      && event.type !== "browser_dom_snapshot"
      && event.type !== "tool_call_completed"
    ) continue;
    if (activityEventValue(event, "provider_attempt_discarded") === true) continue;
    const callId = String(activityEventValue(event, "tool_call_id") ?? "").trim();
    if (!callId) continue;
    const generation = attemptGeneration(
      activityEventValue(event, "provider_attempt_generation"),
    );
    keys.add(toolActivityPreviewKey(callId, generation));
  }
  return keys;
}

export function toolActivityPreviewId(
  item: Pick<RunActivityItem, "toolCallId" | "providerAttemptGeneration">,
  previewableKeys: Set<string>,
): string | undefined {
  if (!item.toolCallId) return undefined;
  const key = toolActivityPreviewKey(
    item.toolCallId,
    item.providerAttemptGeneration,
  );
  return previewableKeys.has(key) ? item.toolCallId : undefined;
}

function shortDetail(value: unknown, limit = 420): string {
  let text = "";
  if (typeof value === "string") {
    text = value;
  } else if (typeof value === "number" || typeof value === "boolean") {
    text = String(value);
  } else if (value !== null && value !== undefined) {
    try {
      text = JSON.stringify(value);
    } catch {
      text = String(value);
    }
  }
  const compact = text.replace(/\s+/g, " ").trim();
  return compact.length > limit ? `${compact.slice(0, limit - 3)}...` : compact;
}

function countNeedle(text: string, needle: string): number {
  if (!needle) return 0;
  let count = 0;
  let index = text.indexOf(needle);
  while (index !== -1) {
    count += 1;
    index = text.indexOf(needle, index + needle.length);
  }
  return count;
}

export function isCompactLogLikeMessageText(text: string): boolean {
  const trimmed = text.trim();
  if (trimmed.length < LOG_PREVIEW_MIN_CHARS) return false;

  const escapedNewlineCount = countNeedle(trimmed, "\\n");
  const hasVeryLongLine = trimmed.split(/\r?\n/).some((line) => line.length > 260);
  const tokenHits = LOG_LIKE_TOKENS.reduce((count, token) => (
    trimmed.includes(token) ? count + 1 : count
  ), 0);
  const hasToolJsonKeys = /[{,]\s*"(stdout|stderr|exit_code|command|classification|risk_reasons|cwd)"\s*:/.test(trimmed);

  return (
    (escapedNewlineCount >= 4 && tokenHits >= 2)
    || (hasVeryLongLine && tokenHits >= 2)
    || (hasToolJsonKeys && tokenHits >= 2)
  );
}

function normalizeLogPreviewText(text: string): string {
  return text
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "  ")
    .replace(/\\"/g, "\"");
}

export function compactLogPreviewText(text: string, maxChars = LOG_PREVIEW_MAX_CHARS): CompactLogPreview {
  const normalized = normalizeLogPreviewText(text).trim();
  if (normalized.length <= maxChars) {
    return { omitted: false, omittedChars: 0, text: normalized };
  }

  const head = normalized.slice(0, LOG_PREVIEW_HEAD_CHARS).trimEnd();
  const tail = normalized.slice(-LOG_PREVIEW_TAIL_CHARS).trimStart();
  const omittedChars = Math.max(0, normalized.length - head.length - tail.length);
  return {
    omitted: true,
    omittedChars,
    text: `${head}\n\n... ${omittedChars.toLocaleString()} chars omitted from chat view ...\n\n${tail}`,
  };
}

function CompactLogBlock({ text }: { text: string }) {
  const preview = compactLogPreviewText(text);
  return (
    <section className="rumi-log-card" aria-label="省略されたログ">
      <div className="rumi-log-card-header">
        <span className="rumi-log-card-kicker">Terminal log</span>
        <span className="rumi-log-card-meta">
          {preview.omitted ? `${preview.omittedChars.toLocaleString()} chars omitted` : "wrapped"}
        </span>
      </div>
      <pre className="rumi-log-card-body">{preview.text}</pre>
      <div className="rumi-log-card-footer">全文はメッセージのコピーから取得できます。</div>
    </section>
  );
}

function MessageMarkdown({
  text,
  onOpenImagePreview,
}: {
  text: string;
  onOpenImagePreview?: (image: ImagePreviewRequest) => void;
}) {
  return isCompactLogLikeMessageText(text)
    ? <CompactLogBlock text={text} />
    : (
      <ReactMarkdown
        remarkPlugins={markdownPlugins}
        components={{
          a: ({ href, children }) => <SafeChatLink href={href}>{children}</SafeChatLink>,
          img: ({ src, alt }) => (
            <UntrustedImageBlock
              block={{ type: "image_url", url: src, alt, presentation: "chat" }}
              blockType="markdown-image"
              onOpenImagePreview={onOpenImagePreview}
            />
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    );
}

function linkText(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(linkText).join("");
  return "";
}

function SafeChatLink({ href, children }: { href?: string; children: ReactNode }) {
  const [reviewing, setReviewing] = useState(false);
  const [status, setStatus] = useState("");
  const decision = useMemo(() => classifyChatLink(
    href,
    linkText(children),
    typeof window === "undefined" ? undefined : window.location.origin,
  ), [children, href]);
  const destination = decision.host || decision.normalizedUrl || "invalid destination";

  const open = () => {
    if (!decision.allowed) {
      setStatus(decision.reason || "This destination is blocked.");
    } else if (!openChatLink(decision)) {
      setStatus("The destination could not be opened. Your chat and draft are unchanged; copy the link to continue manually.");
    } else {
      setReviewing(false);
      setStatus(decision.kind === "internal" ? "Opened inside Rumi." : "Opened in a new context without referrer or opener access.");
    }
  };
  const activate = () => {
    if (decision.kind === "internal" && decision.allowed && !decision.textMismatch) open();
    else setReviewing(true);
  };

  return (
    <span className="inline-flex max-w-full flex-col align-baseline">
      <button type="button" className="inline break-all text-left text-sky-300 underline decoration-sky-500/50 underline-offset-2 hover:text-sky-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-sky-400" aria-label={`${linkText(children) || "Link"}; destination ${destination}; ${decision.kind}`} title={decision.normalizedUrl || decision.reason} onClick={activate}>
        {children}
      </button>
      {reviewing && (
        <span role="dialog" aria-modal="false" aria-label={`Review link to ${destination}`} className="my-2 flex max-w-xl flex-col gap-2 rounded-xl border border-zinc-700 bg-zinc-950 p-3 text-xs text-zinc-300 shadow-xl">
          <strong className="text-sm text-zinc-100">Review destination</strong>
          <span className="break-all font-mono text-[11px] text-zinc-400">{decision.normalizedUrl || href || "Malformed target"}</span>
          <span>{decision.reason || (decision.kind === "web" ? "External web page" : decision.kind)}</span>
          <span className="flex flex-wrap gap-2">
            <button type="button" disabled={!decision.allowed} onClick={open} className="rounded-md bg-sky-500/20 px-3 py-1.5 font-semibold text-sky-100 disabled:cursor-not-allowed disabled:opacity-40">{decision.requiresStrongConfirmation ? "Open after review" : "Open"}</button>
            <button type="button" onClick={() => void navigator.clipboard?.writeText(decision.normalizedUrl || href || "").then(() => setStatus("Link copied.")).catch(() => setStatus("Copy failed. Select the destination text instead."))} className="rounded-md border border-zinc-700 px-3 py-1.5">Copy link</button>
            <button autoFocus type="button" onClick={() => { setReviewing(false); setStatus("Cancelled. Your chat and draft are unchanged."); }} className="rounded-md border border-zinc-700 px-3 py-1.5">Cancel</button>
          </span>
        </span>
      )}
      {status && <span role={/could not|blocked|failed/i.test(status) ? "alert" : "status"} className="mt-1 text-[11px] text-zinc-400">{status}</span>}
    </span>
  );
}

function MessageMentionBadges({
  mentions,
}: {
  mentions: NonNullable<NonNullable<ChatMessagesRendererProps["messages"][number]["metadata"]>["mentions"]>;
}) {
  if (mentions.length === 0) return null;
  return (
    <div
      aria-label="このメッセージで指定されたメンション"
      className="mb-2 flex flex-wrap gap-1.5"
      data-testid="message-mention-badges"
    >
      {mentions.map((mention) => (
        <span
          key={`${mention.kind}:${mention.id}`}
          data-testid="message-mention-badge"
          data-mention-kind={mention.kind}
          className="inline-flex min-h-6 max-w-full items-center overflow-hidden text-ellipsis whitespace-nowrap rounded-full border border-sky-400/25 bg-sky-400/10 px-2 text-[11px] font-medium text-sky-100"
          title={`@${mention.label} · ${mention.kind}`}
        >
          @{mention.label}
        </span>
      ))}
    </div>
  );
}

function imageSizeLabel(size: BrowserScreenshot["image_size"]): string {
  const width = Number(size?.width ?? 0);
  const height = Number(size?.height ?? 0);
  return width > 0 && height > 0 ? `${width} x ${height}` : "";
}

function artifactDialogItemFromImagePreview(image: ImagePreviewRequest | null): ArtifactPreviewDialogItem | null {
  if (!image) return null;
  return {
    kind: "image",
    title: image.title,
    subtitle: image.subtitle,
    href: image.href,
    imageUrl: image.src,
    imageAlt: image.alt,
    details: image.details,
  };
}

function appOrigin(): string | undefined {
  return typeof window === "undefined" ? undefined : window.location.origin;
}

function UntrustedImageBlock({
  block,
  blockType,
  onOpenImagePreview,
}: {
  block: ChatContentBlock;
  blockType: string;
  onOpenImagePreview?: (image: ImagePreviewRequest) => void;
}) {
  const rawUrl = extractImageBlockUrl(block);
  const policy = classifyUntrustedImageUrl(rawUrl, {
    appOrigin: appOrigin(),
    attachmentId: imageBlockAttachmentId(block),
    // Chat/tool/import blocks are attacker-controlled. Only an out-of-band,
    // backend-verified attachment channel may set this option in the future.
    trustedAttachment: false,
  });
  const trusted = policy.disposition === "trusted-attachment";
  const [consented, setConsented] = useState(trusted);
  const [loadingRemote, setLoadingRemote] = useState(false);
  const [loadedSrc, setLoadedSrc] = useState(trusted ? policy.normalizedUrl : "");
  const [proxyUrl, setProxyUrl] = useState("");
  const [failed, setFailed] = useState(false);
  const [copied, setCopied] = useState(false);
  const alt = String(block.alt ?? "image").trim() || "image";

  useEffect(() => {
    setConsented(trusted);
    setLoadedSrc(trusted ? policy.normalizedUrl : "");
    setProxyUrl("");
    setFailed(false);
    setCopied(false);
  }, [policy.normalizedUrl, trusted]);

  useEffect(() => () => {
    if (loadedSrc.startsWith("blob:")) URL.revokeObjectURL(loadedSrc);
  }, [loadedSrc]);

  const copyUrl = () => {
    if (!rawUrl) return;
    void copyTextWithFallback(rawUrl).then(setCopied, () => setCopied(false));
  };
  const openPreview = () => onOpenImagePreview?.({
    src: loadedSrc,
    title: alt,
    alt,
    details: [
      { label: "type", value: blockType },
      { label: "source", value: policy.sourceLabel },
    ],
  });
  const loadRemote = async () => {
    setLoadingRemote(true);
    setFailed(false);
    try {
      const loaded = await chatMessageResources.loadRemoteImage(policy.normalizedUrl);
      setLoadedSrc(loaded.blobUrl);
      setProxyUrl(loaded.proxyUrl);
      setConsented(true);
    } catch {
      setFailed(true);
      setConsented(true);
    } finally {
      setLoadingRemote(false);
    }
  };
  const revokeRemote = () => {
    if (loadedSrc.startsWith("blob:")) URL.revokeObjectURL(loadedSrc);
    if (proxyUrl) void chatMessageResources.revokeRemoteImage(proxyUrl);
    setLoadedSrc("");
    setProxyUrl("");
    setFailed(false);
    setConsented(false);
  };

  return (
    <section className="rounded-lg border border-zinc-800 bg-zinc-900/40 p-3 space-y-2" aria-label={`Image: ${alt}`}>
      <div className="flex items-center gap-2 text-xs text-zinc-400">
        <ImageIcon size={12} aria-hidden="true" />
        <span>{alt}</span>
      </div>
      {policy.disposition === "blocked" ? (
        <ErrorNotice
          className="rounded-md border-amber-900/70 bg-amber-950/30 p-3 text-xs text-amber-200"
          copyLabel="画像ブロックの詳細をコピー"
          errorIcon="blocked-image"
          message={`Source: ${policy.sourceLabel} (${policy.reason})`}
          messageClassName="mt-1 break-all text-amber-300/80"
          severity="warning"
          title="Image blocked for safety."
          titleClassName="text-amber-200"
        />
      ) : !consented ? (
        <div className="rounded-md border border-zinc-700 bg-zinc-950/50 p-3 text-xs text-zinc-300">
          <p>Remote image hidden. Loading it will contact this source.</p>
          <p className="mt-1 break-all text-zinc-400">Source: {policy.sourceLabel}</p>
          <button type="button" disabled={loadingRemote} className="mt-3 rounded-md bg-zinc-100 px-3 py-1.5 font-medium text-zinc-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400 disabled:opacity-60" onClick={() => void loadRemote()}>
            {loadingRemote ? "Loading image…" : "Load image"}
          </button>
        </div>
      ) : failed ? (
        <ErrorNotice
          className="rounded-md border-red-900/70 bg-red-950/30 p-3 text-xs text-red-200"
          copyLabel="画像読み込みエラーをコピー"
          errorIcon="image-load"
          message={`Image could not be loaded from ${policy.sourceLabel}.`}
          messageClassName="text-red-200"
        >
          {!trusted ? <button type="button" className="mt-2 underline" onClick={revokeRemote}>Revoke access</button> : null}
        </ErrorNotice>
      ) : (
        <button type="button" className="block max-w-full cursor-zoom-in rounded-lg border border-zinc-800 bg-black/30 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-500" onClick={openPreview}>
          <img
            src={loadedSrc}
            alt={alt}
            className="max-h-72 rounded-lg"
            referrerPolicy="no-referrer"
            crossOrigin="anonymous"
            loading="lazy"
            decoding="async"
            onError={() => setFailed(true)}
          />
        </button>
      )}
      {consented && !trusted && loadedSrc ? <button type="button" className="text-xs text-zinc-400 underline" onClick={revokeRemote}>Revoke remote image</button> : null}
      {rawUrl ? (
        <button type="button" className="text-xs text-zinc-400 underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-500" onClick={copyUrl} aria-label="Copy image URL">
          {copied ? "URL copied" : "Copy URL"}
        </button>
      ) : null}
    </section>
  );
}

function MessageBlock({
  block,
  sanitizeText,
  unknownStrategy,
  onOpenImagePreview,
}: {
  block: ChatContentBlock;
  sanitizeText?: (text: string) => string;
  unknownStrategy: string;
  onOpenImagePreview?: (image: ImagePreviewRequest) => void;
}) {
  const blockType = String(block.type ?? "text");

  if (blockType === "text" || blockType === "markdown") {
    const text = sanitizeText ? sanitizeText(String(block.text ?? "")) : String(block.text ?? "");
    if (!text.trim()) return null;
    return <MessageMarkdown text={text} onOpenImagePreview={onOpenImagePreview} />;
  }

  if (blockType === "code") {
    return (
      <pre className="max-w-full overflow-x-auto overflow-y-auto whitespace-pre rounded-lg bg-zinc-900 p-3 font-mono text-[12px] text-zinc-200">
        <code>{String(block.text ?? "")}</code>
      </pre>
    );
  }

  if (blockType === "image" || blockType === "image_url") {
    if (!shouldRenderImageBlockInChat(block)) return null;
    return <UntrustedImageBlock block={block} blockType={blockType} onOpenImagePreview={onOpenImagePreview} />;
  }

  return (
    <section
      className="max-w-full rounded-lg border border-amber-500/25 bg-amber-500/10 p-3 text-amber-50"
      data-testid="unsupported-chat-block"
      role="status"
      aria-label="未対応のメッセージ内容"
    >
      <p className="text-sm font-medium">この内容は現在のRumiでは表示できません</p>
      <p className="mt-1 text-xs leading-5 text-amber-100/80">
        Rumiを更新して再試行するか、安全な添付ファイルとしてダウンロードできるか送信元に確認してください。
      </p>
      {unknownStrategy === "debug" ? (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer font-medium">開発者向けの制限済み情報</summary>
          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap rounded bg-black/25 p-2 font-mono text-[11px] text-amber-100/75">
            {safeUnknownBlockDetails(block)}
          </pre>
        </details>
      ) : null}
    </section>
  );
}

export function shouldRenderImageBlockInChat(block: ChatContentBlock): boolean {
  return (
    block.show_in_chat === true
    || block.display_in_chat === true
    || block.presentation === "chat"
    || block.intent === "show_to_user"
  );
}

function messageDisplayText(message: ChatMessagesRendererProps["messages"][number], text: string): string {
  return message.role === "agent" ? sanitizeAssistantAuthorityBoilerplate(text) : text;
}

function messageVisibleText(message: ChatMessagesRendererProps["messages"][number]): string {
  const blockText = message.content
    .map((block) => {
      if (String(block.type ?? "text") === "text" || String(block.type ?? "") === "markdown") {
        return String(block.text ?? "");
      }
      return "";
    })
    .join("")
    .trim();
  return messageDisplayText(message, blockText || String(message.rawText ?? "").trim());
}

function messageMetadataRecord(message: ChatMessagesRendererProps["messages"][number]): Record<string, unknown> {
  return isRecord(message.metadata) ? message.metadata as Record<string, unknown> : {};
}

function metadataChildRecord(message: ChatMessagesRendererProps["messages"][number], ...keys: string[]): Record<string, unknown> | null {
  const metadata = messageMetadataRecord(message);
  for (const key of keys) {
    const value = metadata[key];
    if (isRecord(value)) return value;
  }
  return null;
}

function isAuthorityPermissionId(value: unknown): boolean {
  return value === "model.invoke" || value === "api_key.use" || value === "network.egress";
}

function authorityWaitingRequestId(message: ChatMessagesRendererProps["messages"][number]): string {
  const pending = metadataChildRecord(message, "pendingAuthorityApproval", "pending_authority_approval");
  const metadataRequestId = String(pending?.request_id ?? pending?.approval_request_id ?? "").trim();
  if (metadataRequestId) return metadataRequestId;

  for (const event of message.events ?? []) {
    if (event.type !== "approval_requested" && event.phase !== "approval_requested") continue;
    const requestId = String(event.request_id ?? event.approval_request_id ?? "").trim();
    const isAuthority = Boolean(
      event.authority
      || event.approval_kind === "authority"
      || isAuthorityPermissionId(event.permission_id),
    );
    if (isAuthority && requestId) return requestId;
  }
  return "";
}

export function isHiddenAuthorityFollowupMessage(message: ChatMessagesRendererProps["messages"][number]): boolean {
  if (message.role !== "user") return false;
  const followup = metadataChildRecord(message, "authorityFollowup", "authority_followup");
  const chatDisplay = metadataChildRecord(message, "chatDisplay", "chat_display");
  const requestId = String(followup?.request_id ?? followup?.approval_request_id ?? "").trim();
  const hasAuthorityMarker = Boolean(requestId && isAuthorityPermissionId(followup?.permission_id));
  const text = messageVisibleText(message);
  if (chatDisplay?.hidden === true && chatDisplay.reason === "authority_followup" && hasAuthorityMarker) return true;
  return text === AUTHORITY_FOLLOWUP_TEXT && hasAuthorityMarker;
}

export function isAuthorityWaitingMessage(message: ChatMessagesRendererProps["messages"][number]): boolean {
  return (
    message.role === "agent"
    && messageVisibleText(message) === AUTHORITY_WAITING_TEXT
    && Boolean(authorityWaitingRequestId(message) || metadataChildRecord(message, "pendingAuthorityApproval", "pending_authority_approval"))
  );
}

export function isAwaitingStreamFinalMessage(message: ChatMessagesRendererProps["messages"][number]): boolean {
  const thinkingLabel = String(message.metadata?.thinkingLabel ?? "").trim().toLowerCase();
  return thinkingLabel === "streaming" || thinkingLabel === "running";
}

export function visibleChatMessages(messages: ChatMessagesRendererProps["messages"]): ChatMessagesRendererProps["messages"] {
  return messages.filter((message) => !isHiddenAuthorityFollowupMessage(message));
}

export function shouldShowEmptyResponseWarning(
  message: ChatMessagesRendererProps["messages"][number],
  hasToolActivity: boolean,
): boolean {
  return (
    message.role === "agent"
    && !messageVisibleText(message)
    && !hasToolActivity
    && !isAwaitingStreamFinalMessage(message)
  );
}

export function messageCopyText(message: ChatMessagesRendererProps["messages"][number]): string {
  const blockText = message.content
    .map((block) => {
      const blockType = String(block.type ?? "text");
      if (blockType === "text" || blockType === "markdown" || blockType === "code") {
        return String(block.text ?? "");
      }
      if (blockType === "image" || blockType === "image_url") {
        const imageUrl = block.image_url;
        const url = String(
          block.url
          ?? (typeof imageUrl === "object" && imageUrl !== null && "url" in imageUrl ? imageUrl.url : "")
          ?? "",
        );
        return url;
      }
      return "";
    })
    .filter((text) => text.trim().length > 0)
    .join("\n\n")
    .trim();
  return messageDisplayText(message, blockText || String(message.rawText ?? "").trim());
}

export function formatMessageTimestamp(value: unknown): string {
  const timestamp = timestampMs(value);
  if (timestamp === null) return "";
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(timestamp);
}

async function writeClipboardText(text: string): Promise<void> {
  if (await copyTextWithFallback(text)) return;

  await chatMessageResources.writeClipboard(text);
}

function MessageActionBar({
  message,
}: {
  message: ChatMessagesRendererProps["messages"][number];
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");

  const text = messageCopyText(message);
  const timestampLabel = formatMessageTimestamp(message.createdAt);

  const copyMessage = async () => {
    if (!text) return;
    try {
      await writeClipboardText(text);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    window.setTimeout(() => setCopyState("idle"), 1800);
  };

  return (
    <div className="rumi-message-actions mt-1.5 flex min-h-8 items-center justify-start gap-1 opacity-85 transition-opacity group-hover/message:opacity-100 group-focus-within/message:opacity-100">
      <button
        aria-label="コピー"
        className={cn(
          "flex h-8 w-8 items-center justify-center rounded-lg text-zinc-400 transition-colors hover:bg-zinc-800/85 hover:text-zinc-100 focus-visible:bg-zinc-800/85 focus-visible:text-zinc-100",
          copyState === "copied" && "rumi-copy-icon-pop text-emerald-300",
          copyState === "failed" && "rumi-copy-icon-pop text-red-300",
        )}
        data-copy-action="message"
        onClick={() => void copyMessage()}
        title="コピー"
        type="button"
      >
        <Copy aria-hidden="true" data-copy-icon="message" size={14} />
      </button>
      <span className="sr-only" aria-live="polite">{copyState === "copied" ? "メッセージをコピーしました" : copyState === "failed" ? "メッセージをコピーできませんでした" : ""}</span>
      {timestampLabel && (
        <span className="ml-1 shrink-0 font-mono text-[10px] leading-none text-zinc-600 opacity-50 transition-opacity group-hover/message:opacity-100 group-focus-within/message:opacity-100">
          {timestampLabel}
        </span>
      )}
    </div>
  );
}

function WidgetCard({ widget }: { widget: Record<string, unknown> }) {
  if (String(widget.type ?? "") === "repository_evidence") {
    const statistics = isRecord(widget.statistics) ? widget.statistics : {};
    const reasons = isRecord(widget.excluded_reason_counts)
      ? widget.excluded_reason_counts
      : {};
    const excludedCount = Number(statistics.files_excluded ?? 0);
    const selectedCount = Number(statistics.files_selected ?? 0);
    const reasonEntries = Object.entries(reasons)
      .filter(([, value]) => Number.isFinite(Number(value)))
      .sort(([left], [right]) => left.localeCompare(right));
    return (
      <section
        className="mt-2 w-[min(520px,100%)] rounded-lg border border-cyan-500/25 bg-cyan-500/10 p-3"
        aria-label="Repository evidence statistics"
        data-testid="repository-evidence-widget"
      >
        <div className="text-[11px] font-medium uppercase tracking-[0.16em] text-cyan-300">
          Repository evidence
        </div>
        <div className="mt-2 grid grid-cols-2 gap-2">
          <div className="rounded-md border border-cyan-400/15 bg-zinc-950/35 px-2.5 py-2">
            <div className="text-[10px] text-zinc-500">Selected</div>
            <div className="font-mono text-lg text-zinc-100">{selectedCount}</div>
          </div>
          <div className="rounded-md border border-cyan-400/15 bg-zinc-950/35 px-2.5 py-2">
            <div className="text-[10px] text-zinc-500">Excluded</div>
            <div className="font-mono text-lg text-zinc-100">{excludedCount}</div>
          </div>
        </div>
        {reasonEntries.length > 0 && (
          <dl className="mt-2 grid gap-1 text-[11px]">
            {reasonEntries.map(([reason, value]) => (
              <div key={reason} className="flex items-center justify-between gap-3">
                <dt className="min-w-0 truncate text-zinc-400">{reason}</dt>
                <dd className="shrink-0 font-mono text-zinc-200">{Number(value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>
    );
  }
  if (String(widget.kind ?? "") === "conversation_handoff") {
    const title = String(widget.title ?? "移動先");
    const conversationId = String(widget.conversation_id ?? "");
    const urlPath = String(widget.url_path ?? "");
    const deepLink = String(widget.deep_link ?? "");
    const model = typeof widget.model === "string" ? widget.model : "";
    const href = urlPath || deepLink || "#";
    return (
      <div className="mt-2 w-[min(420px,100%)] rounded-lg border border-emerald-500/25 bg-emerald-500/10 p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-emerald-300">{title}</div>
            <div className="mt-1 truncate font-mono text-[12px] text-zinc-200">{conversationId}</div>
            {model && <div className="mt-1 truncate text-[11px] text-zinc-500">{model}</div>}
          </div>
          <a
            href={href}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-emerald-400/30 text-emerald-200 transition-colors hover:bg-emerald-400/10 focus-visible:bg-emerald-400/10 focus-visible:outline-none"
            aria-label="移動先を開く"
            title="移動先を開く"
          >
            <ExternalLink size={15} />
          </a>
        </div>
      </div>
    );
  }
  return (
    <details className="mt-2 rounded-lg border border-blue-500/20 bg-blue-500/10 p-3">
      <summary className="cursor-pointer select-none text-[10px] uppercase tracking-wider text-blue-300">
        Widget details
      </summary>
      <pre className="mt-2 max-w-full overflow-x-auto overflow-y-auto whitespace-pre text-[11px] font-mono text-zinc-200">{JSON.stringify(widget, null, 2)}</pre>
    </details>
  );
}

export function summarizePendingToolNames(toolNames: string[], visibleLimit = 2): { hiddenCount: number; summary: string; totalCount: number; visibleNames: string[] } {
  const uniqueNames = Array.from(new Set(toolNames.map((name) => name.trim()).filter(Boolean)));
  const visibleNames = uniqueNames.slice(0, visibleLimit);
  const hiddenCount = Math.max(0, uniqueNames.length - visibleNames.length);
  const listed = visibleNames.join("、");
  const summary = hiddenCount > 0
    ? `${listed}、その他 ${hiddenCount} 個が見込まれました`
    : listed
      ? `${listed} が見込まれました`
      : "";
  return {
    hiddenCount,
    summary,
    totalCount: uniqueNames.length,
    visibleNames,
  };
}

function compactDurationMs(label: string | undefined): number | null {
  const text = String(label ?? "").trim();
  if (!text) return null;
  const units: Record<string, number> = {
    d: 86_400_000,
    h: 3_600_000,
    m: 60_000,
    s: 1000,
  };
  let total = 0;
  let matched = false;
  for (const match of text.matchAll(/(\d+)\s*([dhms])/g)) {
    matched = true;
    total += Number(match[1]) * units[match[2]];
  }
  return matched ? total : null;
}

export function hasRunningToolActivityGroups(groups: ToolActivityGroup[]): boolean {
  return hasRunningToolActivityItems(groups.flatMap((group) => group.items));
}

function hasRunningToolActivityItems(items: RunActivityItem[]): boolean {
  return items.some((item) => item.status === "running" || item.status === "waiting_approval");
}

function toolActivityDurationLabel(items: RunActivityItem[]): string {
  let firstStart: number | null = null;
  let lastEnd: number | null = null;
  let longestDurationMs = 0;

  for (const item of items) {
    const durationMs = compactDurationMs(item.durationLabel);
    if (durationMs !== null) {
      longestDurationMs = Math.max(longestDurationMs, durationMs);
    }
    const explicitStart = timestampMs(item.startedAt);
    const explicitEnd = timestampMs(item.completedAt);
    const eventTime = timestampMs(item.timestamp);
    const end = explicitEnd ?? eventTime ?? explicitStart;
    if (end === null) continue;
    const start = explicitStart ?? (durationMs !== null ? end - durationMs : eventTime ?? end);
    firstStart = firstStart === null ? start : Math.min(firstStart, start);
    lastEnd = lastEnd === null ? end : Math.max(lastEnd, end);
  }

  if (firstStart !== null && lastEnd !== null && lastEnd >= firstStart) {
    return formatCompactDuration(lastEnd - firstStart);
  }
  return longestDurationMs > 0 ? formatCompactDuration(longestDurationMs) : "";
}

export function summarizeToolActivityGroups(groups: ToolActivityGroup[]): ToolActivityTraySummary {
  return summarizeToolActivityItems(groups.flatMap((group) => group.items));
}

function summarizeToolActivityItems(items: RunActivityItem[]): ToolActivityTraySummary {
  const itemCount = items.length;
  const failedCount = items.filter((item) => item.status === "failed").length;
  const runningCount = items.filter((item) => item.status === "running" || item.status === "waiting_approval").length;
  const toolCount = items.filter((item) => item.kind === "tool").length;
  let visible = items[items.length - 1];
  for (let index = items.length - 1; index >= 0; index -= 1) {
    if (items[index].status === "running" || items[index].status === "waiting_approval") {
      visible = items[index];
      break;
    }
    if ((visible?.status !== "failed" && visible?.status !== "blocked") && (items[index].status === "failed" || items[index].status === "blocked")) {
      visible = items[index];
    }
  }
  const durationLabel = toolActivityDurationLabel(items);
  const baseCount = toolCount || itemCount;
  const label = runningCount > 0
    ? `作業中 · ${baseCount}件${durationLabel ? ` · ${durationLabel}` : ""}`
    : `✓ ${baseCount}件の作業${durationLabel ? ` · ${durationLabel}` : ""}`;
  return {
    durationLabel,
    failedCount,
    itemCount,
    label: failedCount > 0 ? `${label}・${failedCount}件失敗` : label,
    nextAction: visible?.nextAction ?? visible?.nextStep?.replace(/^次:\s*/, "") ?? "",
    runningCount,
    visibleTitle: visible?.title ?? "",
  };
}

function toolActivityGroupStatus(group: ToolActivityGroup): ToolActivityStatus {
  if (group.items.some((item) => item.status === "failed")) return "failed";
  if (group.items.some((item) => item.status === "running")) return "running";
  return "completed";
}

function toolActivityActionLabel(title: string, fallback: string): string {
  const normalized = (title || fallback).split(":")[0]?.trim();
  return normalized || fallback;
}

function summarizeToolActivityGroupDetail(group: ToolActivityGroup): string {
  const counts = new Map<string, number>();
  for (const item of group.items) {
    const label = toolActivityActionLabel(item.title, item.toolName);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const visible = [...counts.entries()].slice(0, 3).map(([label, count]) => (
    count > 1 ? `${label} ${count}件` : label
  ));
  const hiddenCount = Math.max(0, counts.size - visible.length);
  return hiddenCount > 0 ? `${visible.join("、")}、他${hiddenCount}種類` : visible.join("、");
}

function toolActivityGroupStatusText(group: ToolActivityGroup): string {
  const failedCount = group.items.filter((item) => item.status === "failed").length;
  const runningCount = group.items.filter((item) => item.status === "running").length;
  if (failedCount > 0) return `${failedCount}件エラー`;
  if (runningCount > 0) return `${runningCount}件実行中`;
  return "";
}

function toolActivityStateForMessage(
  message: ChatMessagesRendererProps["messages"][number],
  now?: number,
): MessageToolActivityState | null {
  const staticItems = buildToolActivityItems(message.toolLogs ?? [], message.events ?? [], { conversationId: message.conversationId });
  const hasRunningItems = hasRunningToolActivityItems(staticItems);
  const items = hasRunningItems && now !== undefined
    ? buildToolActivityItems(message.toolLogs ?? [], message.events ?? [], { conversationId: message.conversationId, now })
    : staticItems;
  if (items.length === 0) return null;
  return {
    items,
    hasRunningItems: hasRunningToolActivityItems(items),
    summary: summarizeToolActivityItems(items),
  };
}

function hasRunningToolActivityMessage(message: ChatMessagesRendererProps["messages"][number]): boolean {
  return hasRunningToolActivityItems(buildToolActivityItems(message.toolLogs ?? [], message.events ?? [], { conversationId: message.conversationId }));
}

function activityPhase(status: string | null | undefined, toolNames: string[]): { label: string; detail: string } {
  const text = String(status ?? "").toLowerCase();
  if (text.includes("scheduler") || text.includes("待機")) {
    return { label: "待機中", detail: status || "予定時刻まで待機しています" };
  }
  if (text.includes("handoff") || text.includes("移動")) {
    return { label: "移動準備中", detail: status || "新しい会話を準備しています" };
  }
  if (text.includes("許可しました") || text.includes("承認済み")) {
    return { label: "再開しています", detail: status || "承認済みのリクエストを続行しています" };
  }
  if (toolNames.length > 0 || text.includes("tool") || text.includes("実行")) {
    const summary = summarizePendingToolNames(toolNames).summary;
    return { label: "tool 準備中", detail: summary || status || "tool を確認しています" };
  }
  return { label: "考えています", detail: status || "応答を組み立てています" };
}

function useActivityNow(enabled: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return undefined;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [enabled]);
  return now;
}

function RumiActivityLoading({
  status,
  toolNames,
  startedAt,
  compact = false,
}: {
  status?: string | null;
  toolNames: string[];
  startedAt?: number | null;
  compact?: boolean;
}) {
  const phase = activityPhase(status, toolNames);
  const now = useActivityNow(Boolean(startedAt));
  const elapsed = startedAt ? elapsedDurationLabel(startedAt, now) : "";
  const statusLabel = status || phase.detail || phase.label;
  return (
    <div
      className={cn("rumi-activity-loading flex max-w-full items-center gap-3 px-2 py-2 text-zinc-300", compact ? "w-fit" : "w-[min(680px,calc(100vw-48px))]")}
      role="status"
      aria-live="polite"
      aria-label={statusLabel}
    >
      <div className="rumi-loading-bars" aria-hidden="true">
        <span />
      </div>
      <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="min-w-[10rem] flex-1 break-words text-[15px] font-medium leading-5 text-zinc-200">{statusLabel}</span>
        {elapsed && <span aria-hidden="true" className="shrink-0 font-mono text-[12px] leading-none text-zinc-500">{elapsed}</span>}
      </div>
    </div>
  );
}

function screenshotPreviewDetails(screenshot: BrowserScreenshot): ImagePreviewDetail[] {
  const details: ImagePreviewDetail[] = [];
  const size = imageSizeLabel(screenshot.image_size);
  if (screenshot.action) details.push({ label: "action", value: screenshot.action });
  if (size) details.push({ label: "image", value: size });
  if (screenshot.tool_name) details.push({ label: "tool", value: screenshot.tool_name });
  if (screenshot.tool_call_id) details.push({ label: "tool_call", value: screenshot.tool_call_id });
  if (screenshot.click_marker || screenshot.marker) details.push({ label: "marker", value: shortDetail(screenshot.click_marker ?? screenshot.marker) });
  if (screenshot.drag_marker) details.push({ label: "drag", value: shortDetail(screenshot.drag_marker) });
  if (screenshot.target_window) details.push({ label: "target", value: shortDetail(screenshot.target_window) });
  return details;
}

function BrowserScreenshotPreview({
  screenshot,
  compact = false,
  onOpenImagePreview,
}: {
  screenshot: BrowserScreenshot;
  compact?: boolean;
  onOpenImagePreview?: (image: ImagePreviewRequest) => void;
}) {
  const marker = screenshot.click_marker ?? screenshot.marker;
  const dragMarker = screenshot.drag_marker;
  const imageWidth = Number(screenshot.image_size?.width ?? 0);
  const imageHeight = Number(screenshot.image_size?.height ?? 0);
  const markerX = Number(marker?.x ?? NaN);
  const markerY = Number(marker?.y ?? NaN);
  const dragFromX = Number(dragMarker?.from?.x ?? NaN);
  const dragFromY = Number(dragMarker?.from?.y ?? NaN);
  const dragToX = Number(dragMarker?.to?.x ?? NaN);
  const dragToY = Number(dragMarker?.to?.y ?? NaN);
  const canPlaceMarker = Number.isFinite(markerX) && Number.isFinite(markerY) && imageWidth > 0 && imageHeight > 0;
  const canPlaceDrag =
    Number.isFinite(dragFromX) &&
    Number.isFinite(dragFromY) &&
    Number.isFinite(dragToX) &&
    Number.isFinite(dragToY) &&
    imageWidth > 0 &&
    imageHeight > 0;
  const screenshotLabel =
    screenshot.action === "computer.drag"
      ? "ドラッグ位置つきスクリーンショット"
      : screenshot.action === "computer.click"
        ? "クリック位置つきスクリーンショット"
        : "スクリーンショット";
  const openPreview = () => onOpenImagePreview?.({
    src: screenshot.data_url,
    href: screenshot.data_url,
    title: screenshotLabel,
    alt: screenshot.action === "computer.drag" ? "Dragged screen" : screenshot.action === "computer.click" ? "Clicked screen" : "Screen capture",
    subtitle: screenshot.action,
    details: screenshotPreviewDetails(screenshot),
  });

  return (
    <figure className={cn("max-w-full overflow-hidden rounded-lg border border-zinc-800 bg-black/30", compact ? "w-[min(34rem,100%)]" : "w-[min(48rem,100%)]")}>
      <button
        type="button"
        className="relative block max-w-full cursor-zoom-in align-top focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-zinc-500"
        onClick={openPreview}
      >
        <img
          src={screenshot.data_url}
          alt={screenshot.action === "computer.drag" ? "Dragged screen" : screenshot.action === "computer.click" ? "Clicked screen" : "Screen capture"}
          className="block h-auto w-full object-contain"
          style={{ maxHeight: compact ? "min(220px, 30vh)" : "min(360px, 45vh)" }}
        />
        {canPlaceDrag && (
          <svg
            className="pointer-events-none absolute inset-0 h-full w-full"
            viewBox={`0 0 ${imageWidth} ${imageHeight}`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <line
              x1={dragFromX}
              y1={dragFromY}
              x2={dragToX}
              y2={dragToY}
              stroke="rgba(248, 113, 113, 0.95)"
              strokeWidth={Math.max(3, imageWidth / 180)}
              strokeLinecap="round"
            />
            <circle cx={dragFromX} cy={dragFromY} r={Math.max(6, imageWidth / 120)} fill="rgba(251, 191, 36, 0.85)" />
            <circle cx={dragToX} cy={dragToY} r={Math.max(7, imageWidth / 110)} fill="rgba(248, 113, 113, 0.9)" />
          </svg>
        )}
        {canPlaceMarker && !canPlaceDrag && (
          <span
            className="pointer-events-none absolute h-6 w-6 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-red-300 bg-red-500/25 shadow-[0_0_0_4px_rgba(239,68,68,0.22)]"
            style={{ left: `${(markerX / imageWidth) * 100}%`, top: `${(markerY / imageHeight) * 100}%` }}
          >
            <span className="absolute left-1/2 top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-red-200" />
          </span>
        )}
      </button>
      <figcaption className="flex items-center gap-2 border-t border-zinc-800 px-3 py-2 text-[11px] text-zinc-500">
        <ImageIcon size={12} />
        <span>{screenshotLabel}</span>
      </figcaption>
    </figure>
  );
}

function isBrowserToolName(toolName: unknown): boolean {
  return toolName === "browser_companion" || toolName === "browser_computer" || toolName === "browser_use" || toolName === "computer_use";
}

function isBrowserActivityEvent(event: NonNullable<ChatMessagesRendererProps["messages"][number]["events"]>[number]): boolean {
  return (
    isBrowserToolName(event.tool_name)
    || event.type === "browser_screenshot"
    || event.type === "browser_state_invalidated"
    || event.type === "browser_state_snapshot"
    || event.type === "browser_dom_snapshot"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function isImageDataUrl(value: string): boolean {
  return /^data:image\/(?:png|jpeg|gif|webp);base64,/i.test(value);
}

function browserActionForEvent(event: NonNullable<ChatMessagesRendererProps["messages"][number]["events"]>[number]): string | undefined {
  if (typeof event.action === "string") return event.action;
  const args = isRecord(event.arguments) ? event.arguments : {};
  return typeof args.action === "string" ? args.action : undefined;
}

function collectBrowserScreenshots(
  value: unknown,
  event: NonNullable<ChatMessagesRendererProps["messages"][number]["events"]>[number],
  screenshots: BrowserScreenshot[],
  seen: Set<string>,
): BrowserScreenshot[] {
  if (Array.isArray(value)) {
    value.forEach((item) => collectBrowserScreenshots(item, event, screenshots, seen));
    return screenshots;
  }
  if (!isRecord(value)) return screenshots;

  const dataUrl = stringValue(value.data_url) || stringValue(value.dataUrl);
  if (dataUrl && isImageDataUrl(dataUrl) && !seen.has(dataUrl)) {
    seen.add(dataUrl);
    screenshots.push({
      id: `stream-${String(event.tool_call_id ?? event.timestamp ?? screenshots.length)}-${screenshots.length}`,
      run_id: "stream",
      tool_call_id: typeof event.tool_call_id === "string" ? event.tool_call_id : null,
      tool_name: typeof event.tool_name === "string" ? event.tool_name : undefined,
      mime_type: stringValue(value.mime_type) || "image/png",
      data_url: dataUrl,
      action: stringValue(value.action) || browserActionForEvent(event),
      image_size: isRecord(value.image_size) ? value.image_size : undefined,
      click_marker: isRecord(value.click_marker) ? value.click_marker : undefined,
      marker: isRecord(value.marker) ? value.marker : undefined,
      drag_marker: isRecord(value.drag_marker) ? value.drag_marker : undefined,
      target_window: isRecord(value.target_window) ? value.target_window : undefined,
    });
  }

  for (const [key, item] of Object.entries(value)) {
    if (key === "data_url" || key === "dataUrl") continue;
    if (isRecord(item) || Array.isArray(item)) collectBrowserScreenshots(item, event, screenshots, seen);
  }
  return screenshots;
}

export function streamedBrowserScreenshots(message: ChatMessagesRendererProps["messages"][number]): BrowserScreenshot[] {
  const screenshots: BrowserScreenshot[] = [];
  const seen = new Set<string>();
  for (const event of message.events ?? []) {
    if (!isBrowserActivityEvent(event)) continue;
    collectBrowserScreenshots(event, event, screenshots, seen);
    collectBrowserScreenshots(event.result, event, screenshots, seen);
    collectBrowserScreenshots(event.artifact, event, screenshots, seen);
    collectBrowserScreenshots(event.artifacts, event, screenshots, seen);
    collectBrowserScreenshots(event.output, event, screenshots, seen);
  }
  return screenshots;
}

function hasBrowserToolLog(message: ChatMessagesRendererProps["messages"][number]): boolean {
  return (message.toolLogs ?? []).some((log) => isBrowserToolName(log.tool_name));
}

function hasBrowserToolEvent(message: ChatMessagesRendererProps["messages"][number]): boolean {
  return (message.events ?? []).some((event) => isBrowserActivityEvent(event));
}

function hasRunningBrowserToolEvent(message: ChatMessagesRendererProps["messages"][number]): boolean {
  return (message.events ?? []).some((event) => (
    isBrowserToolName(event.tool_name)
    && (
      event.type === "tool_call" ||
      event.type === "tool_call_started" ||
      event.phase === "tool_call" ||
      event.phase === "tool_call_started"
    )
  ));
}

function BrowserScreenshotStrip({
  message,
  onOpenImagePreview,
}: {
  message: ChatMessagesRendererProps["messages"][number];
  onOpenImagePreview?: (image: ImagePreviewRequest) => void;
}) {
  const [screenshots, setScreenshots] = useState<BrowserScreenshot[]>([]);
  const [omittedCount, setOmittedCount] = useState(0);
  const [failed, setFailed] = useState(false);
  const liveScreenshots = streamedBrowserScreenshots(message);
  const hasBrowserLog = hasBrowserToolLog(message);
  const hasBrowserActivity = hasBrowserLog || hasBrowserToolEvent(message);
  const hasRunningBrowserActivity = hasRunningBrowserToolEvent(message);
  const canFetchStoredScreenshots = hasBrowserLog && !message.id.startsWith("optimistic-");

  useEffect(() => {
    let cancelled = false;
    setScreenshots([]);
    setOmittedCount(0);
    setFailed(false);
    if (!message.conversationId || !canFetchStoredScreenshots) return () => {
      cancelled = true;
    };
    void chatMessageResources.getBrowserScreenshots(message.conversationId, message.id)
      .then((result) => {
        if (!cancelled) {
          setScreenshots(result.screenshots ?? []);
          setOmittedCount(Number(result.omitted_count ?? 0));
        }
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [message.conversationId, message.id, canFetchStoredScreenshots]);

  if (!hasBrowserActivity) return null;

  if (liveScreenshots.length > 0 && !canFetchStoredScreenshots) {
    return (
      <div className="mb-4 grid gap-3">
        {liveScreenshots.map((screenshot) => (
          <BrowserScreenshotPreview key={screenshot.id} screenshot={screenshot} onOpenImagePreview={onOpenImagePreview} />
        ))}
      </div>
    );
  }

  if (!canFetchStoredScreenshots && hasRunningBrowserActivity) {
    return (
      <div className="mb-3 flex w-fit items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[11px] text-zinc-400">
        <Loader2 size={12} className="animate-spin text-blue-300" />
        <span>画面操作を実行中</span>
      </div>
    );
  }

  if (screenshots.length === 0) {
    return failed ? (
      <div className="mb-3 rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-2 text-[11px] text-zinc-500">
        スクリーンショットを読み込めませんでした。
      </div>
    ) : null;
  }

  return (
    <div className="mb-4 grid gap-3">
      {omittedCount > 0 && (
        <div className="w-fit rounded-md border border-zinc-800 bg-zinc-950/70 px-2.5 py-1.5 text-[11px] text-zinc-500">
          古いスクリーンショット {omittedCount} 件を省略しています。
        </div>
      )}
      {screenshots.map((screenshot) => (
        <BrowserScreenshotPreview key={screenshot.id} screenshot={screenshot} onOpenImagePreview={onOpenImagePreview} />
      ))}
    </div>
  );
}

function AuthorityPendingNotice() {
  return (
    <div className="inline-flex max-w-full items-center gap-2 rounded-lg border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-[12px] leading-relaxed text-amber-100">
      <Clock size={14} className="shrink-0 text-amber-300" />
      <span className="shrink-0 font-semibold">{AUTHORITY_PENDING_TITLE}</span>
      <span className="min-w-0 text-amber-100/80">{AUTHORITY_PENDING_DETAIL}</span>
    </div>
  );
}

function toolActivityIcon(groupId: string) {
  if (groupId.includes("progress")) return Wrench;
  if (groupId.includes("sandbox")) return Box;
  if (groupId.includes("web")) return Globe2;
  if (groupId.includes("browser")) return Monitor;
  if (groupId.includes("terminal")) return Terminal;
  if (groupId.includes("git")) return GitBranch;
  if (groupId.includes("file")) return FileText;
  if (groupId.includes("calculation")) return Calculator;
  return Wrench;
}

function activityStatusMarker(status: ToolActivityStatus): string {
  if (status === "completed") return "✓";
  if (status === "failed") return "!";
  if (status === "waiting_approval") return "◷";
  if (status === "blocked") return "!";
  return "●";
}

function activityStatusTone(status: ToolActivityStatus): string {
  if (status === "failed" || status === "blocked") return "text-red-300";
  if (status === "waiting_approval") return "text-amber-300";
  if (status === "running") return "text-blue-300";
  return "text-zinc-500";
}

function activityRowTone(status: ToolActivityStatus): string {
  if (status === "failed" || status === "blocked") return "bg-red-500/10 hover:bg-red-500/15";
  if (status === "waiting_approval") return "bg-amber-500/10 hover:bg-amber-500/15";
  if (status === "running") return "bg-blue-500/10 hover:bg-blue-500/15";
  return "hover:bg-zinc-900/50";
}

function isToolActivityItem(item: RunActivityItem): item is ToolActivityItem {
  return item.kind === "tool";
}

function ToolActivityTimelineRow({
  item,
  onOpenToolPreview,
  previewableCallIds,
}: {
  item: RunActivityItem;
  onOpenToolPreview?: (previewId: string) => void;
  previewableCallIds: Set<string>;
}) {
  const artifactPreviewId = isToolActivityItem(item) ? item.artifacts?.find((artifact) => artifact.url)?.path : undefined;
  const previewId = toolActivityPreviewId(item, previewableCallIds) ?? artifactPreviewId;
  const hasPreview = Boolean(previewId);
  const hasError = item.status === "failed" || item.status === "blocked";
  const itemLabel = item.title || item.detail || (isToolActivityItem(item) ? item.toolName : item.folderLabel);
  const statusLabel = hasError
    ? "エラー"
    : item.status === "waiting_approval"
      ? "承認待ち"
      : "";
  const statusLine = [statusLabel, item.detail].filter(Boolean).join(" · ");
  const errorCopyText = [
    "ツール実行エラー",
    itemLabel,
    item.detail && item.detail !== itemLabel ? item.detail : "",
    item.nextStep ? `次: ${item.nextStep}` : "",
  ].filter(Boolean).join("\n");
  const errorIconId = `tool-activity-${item.status}`;
  const Icon = toolActivityIcon(item.folder);
  const body = (
    <>
      {hasError ? (
        <CircleAlert
          aria-hidden="true"
          className="mt-[3px] h-4 w-4 shrink-0 text-red-300"
          data-error-icon={errorIconId}
        />
      ) : (
        <span className={cn("mt-[3px] flex h-4 w-4 shrink-0 items-center justify-center font-mono text-[11px] leading-none", activityStatusTone(item.status))}>
          {activityStatusMarker(item.status)}
        </span>
      )}
      <span className="mt-[5px] flex h-3 w-3 shrink-0 items-center justify-center text-zinc-500" title={item.folderLabel}>
        <Icon size={12} />
      </span>
      <span className="min-w-0 max-w-full flex-1 overflow-hidden">
        <span className="flex min-w-0 max-w-full items-baseline gap-2 text-[12px] leading-4 text-zinc-300">
          <span className="min-w-0 flex-1 truncate">{itemLabel}</span>
          {item.durationLabel && <span className="shrink-0 font-mono text-[10px] text-zinc-600">{item.durationLabel}</span>}
        </span>
        {statusLine && (
          <span className={cn("block max-w-full truncate text-[10px] leading-4", item.status === "failed" || item.status === "blocked" ? "text-red-300" : item.status === "waiting_approval" ? "text-amber-200/80" : "text-zinc-500")}>
            {statusLine}
          </span>
        )}
        {item.nextStep && (
          <span className="block max-w-full truncate text-[10px] leading-4 text-zinc-600">{item.nextStep}</span>
        )}
      </span>
      {hasPreview && <ChevronRight size={12} className="mt-[5px] shrink-0 text-zinc-600" />}
    </>
  );
  const row = hasPreview ? (
    <button
      type="button"
      className={cn("group/tool flex min-h-7 w-full min-w-0 max-w-full items-start gap-2 overflow-hidden rounded px-1.5 py-1 text-left transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-600 sm:min-h-8", activityRowTone(item.status))}
      onClick={() => {
        if (previewId) onOpenToolPreview?.(previewId);
      }}
    >
      {body}
    </button>
  ) : (
    <div className={cn("flex min-h-7 w-full min-w-0 max-w-full items-start gap-2 overflow-hidden rounded px-1.5 py-1 sm:min-h-8", activityRowTone(item.status))}>
      {body}
    </div>
  );
  if (!hasError) return row;
  return (
    <div
      aria-label="ツール実行エラー"
      className="flex min-w-0 items-start gap-1"
      data-error-notice={errorIconId}
      role="group"
    >
      {row}
      <ErrorCopyAction
        className="mt-0.5 h-6 w-6"
        copyText={errorCopyText}
        label="ツール実行エラーをコピー"
      />
    </div>
  );
}

function ToolActivityPanel({
  items,
  isOpen,
  onToggle,
  summary,
  message,
  onOpenToolPreview,
}: {
  items: RunActivityItem[];
  isOpen: boolean;
  onToggle: () => void;
  summary: ToolActivityTraySummary;
  message: ChatMessagesRendererProps["messages"][number];
  onOpenToolPreview?: (previewId: string) => void;
}) {
  if (items.length === 0) return null;
  const previewableCallIds = previewableToolActivityKeys(message.events ?? []);
  return (
    <section className="rumi-tool-activity mb-3 grid w-full max-w-[640px] gap-1 rounded-md border border-zinc-800/70 bg-zinc-950/45 px-2 py-1.5 text-zinc-300" aria-label="ツール履歴">
      <button
        type="button"
        aria-expanded={isOpen}
        aria-label={`作業状況を${isOpen ? "閉じる" : "開く"}: ${summary.label}`}
        className="flex min-w-0 items-center gap-2 rounded px-0.5 py-0.5 text-left transition-colors hover:bg-zinc-900/40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-zinc-600"
        onClick={onToggle}
      >
        <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", summary.failedCount > 0 ? "bg-red-400" : summary.runningCount > 0 ? "animate-pulse bg-blue-300" : "bg-zinc-600")} />
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className="shrink-0 text-[10px] font-medium uppercase tracking-wide text-zinc-500">ツール履歴</span>
            <span className="min-w-0 truncate text-[12px] font-medium leading-4 text-zinc-300">{summary.visibleTitle || summary.label}</span>
            <span className="shrink-0 text-[10px] leading-4 text-zinc-500">{summary.label}</span>
          </span>
          {summary.runningCount > 0 && summary.nextAction && (
            <span className="block truncate text-[10px] leading-4 text-zinc-500">次: {summary.nextAction}</span>
          )}
        </span>
        <span className="shrink-0 text-[10px] text-zinc-500">{isOpen ? "閉じる" : "詳細"}</span>
        <ChevronRight size={12} className={cn("shrink-0 text-zinc-600 transition-transform", isOpen && "rotate-90")} />
      </button>
      {isOpen && (
        <div className="rumi-tool-activity-timeline grid min-w-0 gap-0.5 border-t border-zinc-800/65 pt-1" aria-label="ツール履歴の詳細">
          {items.map((item) => (
            <ToolActivityTimelineRow
              key={item.id}
              item={item}
              onOpenToolPreview={onOpenToolPreview}
              previewableCallIds={previewableCallIds}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function PendingToolTray({
  toolNames,
  toolStartedAt = {},
  inline = false,
}: {
  toolNames: string[];
  toolStartedAt?: Record<string, number>;
  inline?: boolean;
}) {
  const now = useActivityNow(toolNames.some((name) => Boolean(toolStartedAt[name])));
  const summary = summarizePendingToolNames(toolNames);
  if (summary.totalCount === 0) return null;

  return (
    <div className={cn("mt-2 w-[min(820px,calc(100vw-64px))] px-1 py-2", inline ? "" : "ml-5")}>
      <div className="mb-2 flex items-center gap-2 text-[11px] font-medium text-zinc-400">
        <Loader2 size={12} className="animate-spin text-blue-300" />
        <span>見込まれた tool</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {summary.visibleNames.map((name) => {
          const folder = toolFolderFor(name);
          return (
            <span key={name} className="inline-flex max-w-[220px] items-baseline gap-1.5 rounded-md bg-zinc-900/50 px-2 py-1 text-[11px] text-zinc-300" title={folder.label}>
              <span className="truncate">{name}</span>
              {toolStartedAt[name] && <span className="font-mono text-[10px] text-zinc-600">{elapsedDurationLabel(toolStartedAt[name], now)}</span>}
            </span>
          );
        })}
        {summary.hiddenCount > 0 && (
          <span className="inline-flex items-center rounded-md bg-zinc-900/40 px-2 py-1 text-[11px] text-zinc-500">
            その他 {summary.hiddenCount} 個が見込まれました
          </span>
        )}
      </div>
    </div>
  );
}

export function ChatMessagesRenderer({
  error,
  isMessagesRegionVisible,
  isLoading,
  isNewConversation,
  isGenerating,
  pendingStatus,
  pendingToolNames = [],
  pendingStartedAt,
  pendingToolStartedAt = {},
  messages,
  messagesEndRef,
  messagesScrollRef,
  onMessagesScroll,
  unknownBlockStrategy,
  showActivityInMessages,
  showWidgets,
  showPromptUsageInMessages = true,
  onOpenToolPreview,
  onLoadPromptTrace,
  onRetry,
  onDismissError,
}: ChatMessagesRendererProps) {
  const [imagePreview, setImagePreview] = useState<ImagePreviewRequest | null>(null);
  const [openToolActivityByMessageId, setOpenToolActivityByMessageId] = useState<Record<string, boolean | undefined>>({});
  const hasRunningToolActivity = showActivityInMessages && messages.some((message) => message.role === "agent" && hasRunningToolActivityMessage(message));
  const visibleMessages = useMemo(() => visibleChatMessages(messages), [messages]);
  const hasAuthorityPendingMessage = useMemo(() => visibleMessages.some(isAuthorityWaitingMessage), [visibleMessages]);
  const lastVisibleMessage = visibleMessages.length > 0 ? visibleMessages[visibleMessages.length - 1] : null;
  const inlinePendingMessageId = isGenerating && !hasAuthorityPendingMessage && lastVisibleMessage?.role === "agent"
    ? lastVisibleMessage.id
    : null;
  const activityNow = useActivityNow(hasRunningToolActivity || Boolean(inlinePendingMessageId));

  return (
    <>
      {error && (
        <ErrorNotice
          className="rumi-chat-error mx-4 mt-3 rounded-xl border-red-400/25 bg-red-500/[0.09] px-3.5 py-3 text-red-100"
          copyLabel="チャットエラーをコピー"
          errorIcon="chat"
          message={error}
          messageClassName="mt-1 whitespace-pre-wrap text-[12px] leading-5 text-red-100/80"
          title="処理を完了できませんでした"
          titleClassName="text-[12px] text-red-100"
          trailing={onDismissError ? (
            <button
              aria-label="エラーを閉じる"
              className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-red-200/70 hover:bg-red-100/10 hover:text-red-50"
              onClick={onDismissError}
              title="閉じる"
              type="button"
            >
              <X aria-hidden="true" size={15} />
            </button>
          ) : undefined}
        >
          {onRetry ? (
            <button
              className="mt-2.5 inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-red-200/25 bg-red-100/[0.06] px-3 text-[12px] font-semibold text-red-50 hover:bg-red-100/[0.11]"
              onClick={onRetry}
              type="button"
            >
              <RefreshCw aria-hidden="true" size={13} />
              再試行
            </button>
          ) : null}
        </ErrorNotice>
      )}

      {!isMessagesRegionVisible ? null : isLoading ? (
        <div className="flex-1 flex items-center justify-center">
          <RumiActivityLoading status={pendingStatus} toolNames={pendingToolNames} startedAt={pendingStartedAt} />
        </div>
      ) : isNewConversation ? (
        <div className="flex-1" />
      ) : (
        <div
          ref={messagesScrollRef}
          onScroll={onMessagesScroll}
          className="rumi-messages-scroll flex-1 overflow-x-hidden overflow-y-auto overscroll-contain px-4 py-3 sm:px-6 lg:px-8"
        >
          <div className="mx-auto w-full max-w-[980px] min-w-0 space-y-5">
            {visibleMessages.map((message) => {
              const toolActivity = showActivityInMessages && message.role === "agent"
                ? toolActivityStateForMessage(message, activityNow)
                : null;
              const sanitizeMessageText = message.role === "agent" ? sanitizeAssistantAuthorityBoilerplate : undefined;
              const isToolActivityOpen = toolActivity
                ? openToolActivityByMessageId[message.id] ?? toolActivity.hasRunningItems
                : false;
              const isAuthorityPending = isAuthorityWaitingMessage(message);
              // Persisted conversations can retain a stale `thinking.state=streaming`
              // after the request has completed. Only the active tail response should
              // keep advancing its timer; historical messages use their recorded end.
              const isTaskRunning = message.role === "agent"
                && isGenerating
                && inlinePendingMessageId === message.id;
              const taskDuration = message.role === "agent"
                ? taskDurationForMessage(message, toolActivity?.items, activityNow, isTaskRunning)
                : null;
              const toggleToolActivity = () => {
                if (!toolActivity) return;
                setOpenToolActivityByMessageId((current) => {
                  const currentOpen = current[message.id] ?? false;
                  return { ...current, [message.id]: !currentOpen };
                });
              };

              return (
              <div key={message.id} className={cn("rumi-message-row group/message flex min-w-0 gap-3 select-text", message.role === "user" ? "flex-row-reverse sm:pr-2 lg:pr-5" : "sm:pl-1")}>
                <div className={cn("flex min-w-0 flex-col pt-1", message.role === "user" ? "max-w-[88%] items-end sm:max-w-[78%] lg:max-w-[70%]" : "flex-1 items-start")}>
                  {message.role === "agent" && (
                    <div className="mb-1.5 flex max-w-full min-w-0 flex-nowrap items-center gap-2 overflow-hidden">
                      <span className="shrink-0 text-xs font-semibold tracking-wide text-zinc-300">Assistant</span>
                      {taskDuration && (
                        <span className="flex shrink-0 items-center gap-1 text-[10px] text-zinc-500" aria-label={taskDuration.label}>
                          <Clock size={10} aria-hidden="true" /> {taskDuration.label}
                        </span>
                      )}
                    </div>
                  )}

                  <div className={cn("flex min-w-0 max-w-full flex-col", message.role === "user" ? "items-start" : "w-full items-start")}>
                    {toolActivity && (
                      <ToolActivityPanel
                        items={toolActivity.items}
                        isOpen={isToolActivityOpen}
                        onToggle={toggleToolActivity}
                        summary={toolActivity.summary}
                        message={message}
                        onOpenToolPreview={onOpenToolPreview}
                      />
                    )}
                    {(() => {
                      const hasToolActivity = Boolean(toolActivity);
                      return (
                    <div
                      className={cn(
                        "rumi-message-bubble relative max-w-full overflow-x-hidden rounded-2xl px-3 py-3 text-[14px] outline-none select-text sm:px-4",
                        message.role === "user"
                          ? "bg-zinc-800/80 text-zinc-100 rounded-tr-sm shadow-sm border border-zinc-700/50"
                          : "w-full text-zinc-200 bg-transparent",
                      )}
                    >
                      {message.role === "agent" && message.metadata?.thinkingTranscript && (
                        <details className="mb-3 rounded-lg border border-zinc-800 bg-zinc-950/50 px-3 py-2 text-xs text-zinc-400">
                          <summary className="cursor-pointer select-none text-[11px] font-medium text-zinc-300">
                            Trace
                          </summary>
                          <pre className="mt-2 max-h-40 overflow-x-auto overflow-y-auto whitespace-pre font-mono text-[11px] leading-relaxed text-zinc-400">
                            {message.metadata.thinkingTranscript}
                          </pre>
                        </details>
                      )}

                      {message.role === "user" && message.metadata?.mentions && (
                        <MessageMentionBadges mentions={message.metadata.mentions} />
                      )}

                      <div className="rumi-message-content markdown-body min-w-0 max-w-full select-text space-y-4 leading-relaxed">
                        {isAuthorityPending
                          ? (
                              <AuthorityPendingNotice />
                            )
                          : message.content.length > 0 && (messageVisibleText(message) || message.content.some((block) => String(block.type ?? "text") !== "text"))
                          ? message.content.map((block, index) => (
                              <MessageBlock key={`${message.id}-${index}`} block={block} sanitizeText={sanitizeMessageText} unknownStrategy={unknownBlockStrategy} onOpenImagePreview={setImagePreview} />
                            ))
                          : shouldShowEmptyResponseWarning(message, hasToolActivity)
                            ? (
                                <ErrorNotice
                                  className="rounded-lg border-amber-500/20 bg-amber-500/10 px-3 py-2 text-[12px] leading-relaxed text-amber-100"
                                  copyLabel="空の応答エラーをコピー"
                                  errorIcon="empty-response"
                                  message="レスポンス本文が空でした。stream が途中で閉じたか、thinking のみで終了した可能性があります。"
                                  messageClassName="text-amber-100"
                                  severity="warning"
                                />
                              )
                            : <MessageMarkdown text={messageDisplayText(message, message.rawText)} />}
                      </div>

                      {message.role === "agent" && message.metadata?.interrupted && (
                        <ErrorNotice
                          className="mt-3 rounded-lg border-amber-500/25 bg-amber-500/10 px-3 py-2 text-[12px] leading-relaxed text-amber-100"
                          copyLabel="中断した応答の詳細をコピー"
                          errorIcon="interrupted-response"
                          message="応答は途中で中断されました。表示内容は中断前までに届いたものです。"
                          messageClassName="text-amber-100"
                          severity="warning"
                        />
                      )}

                      {showWidgets && message.widget && <WidgetCard widget={message.widget} />}

                      {inlinePendingMessageId === message.id && (
                        <div className="mt-3 grid gap-1.5">
                          <RumiActivityLoading status={pendingStatus} toolNames={pendingToolNames} startedAt={pendingStartedAt} compact />
                          {pendingToolNames.length > 0 && (
                            <PendingToolTray toolNames={pendingToolNames} toolStartedAt={pendingToolStartedAt} inline />
                          )}
                        </div>
                      )}

                      {showPromptUsageInMessages && message.role === "agent" && <PromptUsageDisclosure usage={message.metadata?.promptUsage} loadPromptTrace={onLoadPromptTrace} />}
                    </div>
                      );
                    })()}

                    <MessageActionBar message={message} />
                  </div>
                </div>
              </div>
              );
            })}

            {isGenerating && !hasAuthorityPendingMessage && !inlinePendingMessageId && (
              <div className="flex gap-3">
                <div className="text-zinc-400 text-[13px] flex flex-col gap-1 mt-1.5">
                  <RumiActivityLoading status={pendingStatus} toolNames={pendingToolNames} startedAt={pendingStartedAt} compact />
                  {pendingToolNames.length > 0 && (
                    <PendingToolTray toolNames={pendingToolNames} toolStartedAt={pendingToolStartedAt} />
                  )}
                </div>
              </div>
            )}
            <div ref={messagesEndRef} className="h-2 scroll-mb-4" aria-hidden="true" />
          </div>
        </div>
      )}
      <ArtifactPreviewDialog item={artifactDialogItemFromImagePreview(imagePreview)} onClose={() => setImagePreview(null)} />
    </>
  );
}
