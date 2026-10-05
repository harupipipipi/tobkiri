import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Bell, Check, CircleAlert, Pin, RefreshCw, X } from "lucide-react";

import { ErrorCopyAction, errorNoticeCopyText } from "./ErrorNotice";
import { LayerPortal } from "../ui/layers/LayerPortal";
import type { ChatMessagesRendererProps } from "../renderers/types";

type ChatNotification = {
  id: string;
  tone: "error" | "warning" | "success";
  title: string;
  message: string;
};

export const CHAT_NOTIFICATION_DURATION_MS = 8_000;

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
      title: "処理を完了できませんでした",
      message: error,
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
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
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
        <p className="rumi-chat-notification-app">Tobkiri</p>
        <p className="rumi-chat-notification-title">{notice.title}</p>
        <p className="rumi-chat-notification-message">{notice.message}</p>
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
            copyText={errorNoticeCopyText(notice.title, notice.message)}
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
