import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Bell, Check, CircleAlert, Pin, RefreshCw, X } from "lucide-react";

import { ErrorCopyAction, errorNoticeCopyText } from "./ErrorNotice";
import { LayerPortal } from "../ui/layers/LayerPortal";
import type { ChatMessagesRendererProps } from "../renderers/types";

type ChatNotification = {
  id: string;
  tone: "error" | "warning" | "success";
  title: string;
  message: string;
  copyText?: string;
};

export const CHAT_NOTIFICATION_DURATION_MS = 8_000;

const useCommittedLayoutEffect = typeof document === "undefined"
  ? useEffect
  : useLayoutEffect;

/** Promotes a short first error line while preserving the exact copy payload. */
export function chatErrorPresentation(error: string): {
  title: string;
  message: string;
  copyText: string;
} {
  const lines = error.split(/\r\n?|\n/);
  const firstLine = lines[0].trim();
  const promoteFirstLine = firstLine.length > 0
    && Array.from(firstLine).length <= 120;
  return {
    title: promoteFirstLine ? firstLine : "処理を完了できませんでした",
    message: promoteFirstLine ? lines.slice(1).join("\n").trim() : error,
    copyText: error,
  };
}

/** Floating chat notices retain their details after the banner folds away. */
export function ChatNotifications({
  error,
  completionNotice,
  onDismissError,
  onDismissCompletionNotice,
  onRetry,
}: Pick<ChatMessagesRendererProps,
  "error" | "completionNotice" | "onDismissError" | "onDismissCompletionNotice" | "onRetry"
>) {
  const notifications = useMemo<ChatNotification[]>(() => [
    ...(error ? [{
      id: `error:${error}`,
      tone: "error" as const,
      ...chatErrorPresentation(error),
    }] : []),
    ...(completionNotice ? [{
      id: `completion:${completionNotice.tone}:${completionNotice.title}:${completionNotice.message}`,
      ...completionNotice,
    }] : []),
  ], [error, completionNotice]);
  const identity = notifications.map((notice) => notice.id).join("\n");
  const [foldedIds, setFoldedIds] = useState<string[]>([]);
  const [pinnedIds, setPinnedIds] = useState<string[]>([]);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [scrollable, setScrollable] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const pointerRef = useRef<{ x: number; y: number } | null>(null);
  const reconcileInteraction = useCallback(() => {
    const container = containerRef.current;
    if (!container) {
      pointerRef.current = null;
      setHovered(false);
      setFocused(false);
      setScrollable(false);
      return;
    }
    const ownerDocument = container.ownerDocument;
    const isScrollable = container.scrollHeight > container.clientHeight;
    setScrollable(isScrollable);
    setFocused(container.contains(ownerDocument.activeElement));
    const pointer = pointerRef.current;
    const hit = pointer && typeof ownerDocument.elementFromPoint === "function"
      ? ownerDocument.elementFromPoint(pointer.x, pointer.y)
      : null;
    // The overflowing stack accepts pointer events for its scrollbar/padding;
    // otherwise hit testing counts only live cards and reopen controls.
    setHovered(pointer
      ? Boolean(hit && container.contains(hit))
      : (isScrollable && container.matches(":hover"))
        || Array.from(container.children).some((child) => child.matches(":hover")));
  }, []);
  const paused = hovered || focused;
  const visibleNotifications = notifications.filter((notice) => !foldedIds.includes(notice.id));
  const foldedCount = notifications.length - visibleNotifications.length;

  useEffect(() => {
    const activeIds = notifications.map((notice) => notice.id);
    setFoldedIds((current) => current.filter((id) => activeIds.includes(id)));
    setPinnedIds((current) => current.filter((id) => activeIds.includes(id)));
    if (!identity) {
      setHovered(false);
      setFocused(false);
    }
  }, [identity]);

  // Removal of the focused/hovered node need not dispatch blur or mouseleave.
  // Check the committed DOM even when the remaining notice identity is stable.
  useCommittedLayoutEffect(() => {
    reconcileInteraction();
  });

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const ownerDocument = container.ownerDocument;
    const ownerWindow = ownerDocument.defaultView;
    const trackPointer = (event: MouseEvent) => {
      pointerRef.current = { x: event.clientX, y: event.clientY };
      reconcileInteraction();
    };
    const leaveDocument = (event: MouseEvent) => {
      if (event.relatedTarget === null) {
        pointerRef.current = null;
        setHovered(false);
      }
    };
    const observer = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(reconcileInteraction);
    observer?.observe(container);
    Array.from(container.children).forEach((child) => observer?.observe(child));
    ownerDocument.addEventListener("mousemove", trackPointer, true);
    ownerDocument.addEventListener("mouseout", leaveDocument, true);
    ownerDocument.addEventListener("scroll", reconcileInteraction, true);
    ownerWindow?.addEventListener("resize", reconcileInteraction);
    container.addEventListener("animationend", reconcileInteraction);
    return () => {
      observer?.disconnect();
      ownerDocument.removeEventListener("mousemove", trackPointer, true);
      ownerDocument.removeEventListener("mouseout", leaveDocument, true);
      ownerDocument.removeEventListener("scroll", reconcileInteraction, true);
      ownerWindow?.removeEventListener("resize", reconcileInteraction);
      container.removeEventListener("animationend", reconcileInteraction);
    };
  }, [identity, foldedIds, reconcileInteraction]);

  useEffect(() => {
    const foldableIds = notifications
      .filter((notice) => !foldedIds.includes(notice.id) && !pinnedIds.includes(notice.id))
      .map((notice) => notice.id);
    if (foldableIds.length === 0 || paused) return undefined;
    const timer = window.setTimeout(
      () => setFoldedIds((current) => [...new Set([...current, ...foldableIds])]),
      CHAT_NOTIFICATION_DURATION_MS,
    );
    return () => window.clearTimeout(timer);
  }, [identity, foldedIds, pinnedIds, paused]);

  if (notifications.length === 0) return null;

  const content = (
    <div
      className="rumi-chat-notifications rumi-layer-toast"
      data-chat-notifications=""
      data-notifications-scrollable={scrollable || undefined}
      ref={containerRef}
      onMouseEnter={(event) => {
        pointerRef.current = { x: event.clientX, y: event.clientY };
        reconcileInteraction();
      }}
      onMouseLeave={(event) => {
        pointerRef.current = { x: event.clientX, y: event.clientY };
        reconcileInteraction();
      }}
      onFocusCapture={() => setFocused(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false);
      }}
    >
      {foldedCount > 0 ? (
        <button
          type="button"
          className="rumi-chat-notifications-reopen"
          aria-label={`通知を表示 (${foldedCount}件)`}
          onClick={() => setFoldedIds([])}
        >
          <Bell size={17} aria-hidden="true" />
          <span>{foldedCount}</span>
        </button>
      ) : null}
      {visibleNotifications.map((notice) => (
        <NotificationCard
          key={notice.id}
          notice={notice}
          pinned={pinnedIds.includes(notice.id)}
          onTogglePin={() => setPinnedIds((current) => current.includes(notice.id)
            ? current.filter((id) => id !== notice.id)
            : [...current, notice.id])}
          onDismiss={() => {
            const dismiss = notice.tone === "error" ? onDismissError : onDismissCompletionNotice;
            if (dismiss) dismiss();
            else setFoldedIds((current) => [...new Set([...current, notice.id])]);
          }}
        >
          {notice.tone === "error" && onRetry ? (
            <button type="button" className="rumi-chat-notification-retry" onClick={onRetry}>
              <RefreshCw size={13} aria-hidden="true" />
              再試行
            </button>
          ) : null}
        </NotificationCard>
      ))}
    </div>
  );
  // Server rendering keeps the accessible notice available without a DOM portal.
  return typeof document === "undefined" ? content : <LayerPortal layer="toast">{content}</LayerPortal>;
}

function NotificationCard({
  notice,
  pinned,
  onTogglePin,
  onDismiss,
  children,
}: {
  notice: ChatNotification;
  pinned: boolean;
  onTogglePin: () => void;
  onDismiss: () => void;
  children?: ReactNode;
}) {
  const isError = notice.tone === "error";
  const Icon = notice.tone === "success" ? Check : CircleAlert;
  return (
    <article
      className="rumi-chat-notification"
      data-chat-notification-tone={notice.tone}
      data-notification-pinned={pinned || undefined}
      data-chat-completion-notice={isError ? undefined : notice.tone}
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
      aria-atomic="true"
    >
      <span className="rumi-chat-notification-icon" aria-hidden="true">
        <Icon size={19} data-error-icon={isError ? "chat" : undefined} />
      </span>
      <div className="rumi-chat-notification-content">
        <p className="rumi-chat-notification-title">{notice.title} - Tobkiri</p>
        {notice.message && notice.message !== notice.title ? (
          <p className="rumi-chat-notification-message">{notice.message}</p>
        ) : null}
        {children}
      </div>
      <div className="rumi-chat-notification-actions">
        <button
          type="button"
          className="rumi-chat-notification-pin"
          onClick={onTogglePin}
          aria-pressed={pinned}
          aria-label={pinned ? "通知の固定を解除" : "通知を固定"}
          title={pinned ? "固定を解除" : "通知を固定"}
        >
          <Pin size={15} fill={pinned ? "currentColor" : "none"} aria-hidden="true" />
        </button>
        {notice.tone !== "success" ? (
          <ErrorCopyAction
            className="rumi-chat-notification-copy"
            copyText={notice.copyText ?? errorNoticeCopyText(notice.title, notice.message)}
            label={isError ? "チャットエラーをコピー" : "通知をコピー"}
          />
        ) : null}
        <button
          type="button"
          className="rumi-chat-notification-close"
          onClick={onDismiss}
          aria-label={isError ? "エラーを閉じる" : "通知を閉じる"}
          title="閉じる"
        >
          <X size={15} aria-hidden="true" />
        </button>
      </div>
    </article>
  );
}
