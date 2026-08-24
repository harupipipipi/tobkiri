import { normalizeLocale, t, type LocaleSetting } from "./i18n";

export type BackendConnectionState = "online" | "degraded" | "offline";
export type BackendPendingOperation = "send" | "approval" | null;

export type BackendConnectionCopy = {
  title: string;
  detail: string;
  actionLabel: string;
};

export function backendConnectionStateAfterHealthCheck(
  succeeded: boolean,
  lastHealthyAt: number | null,
  consecutiveFailures: number,
): BackendConnectionState {
  if (succeeded) return "online";
  return lastHealthyAt !== null && consecutiveFailures < 3
    ? "degraded"
    : "offline";
}

export function formatLastHealthyLabel(
  timestamp: number | null,
  locale: LocaleSetting,
): string | null {
  if (!timestamp) return null;
  try {
    return new Intl.DateTimeFormat(normalizeLocale(locale), {
      hour: "2-digit",
      minute: "2-digit",
    }).format(timestamp);
  } catch {
    return null;
  }
}

export function backendConnectionCopy(
  state: BackendConnectionState,
  lastHealthyAt: number | null,
  pendingOperation: BackendPendingOperation,
  locale: LocaleSetting,
): BackendConnectionCopy {
  if (state === "offline") {
    return {
      title: t(locale, "connection.offline.title"),
      detail: t(
        locale,
        pendingOperation === "send"
          ? "connection.offline.pendingSend"
          : pendingOperation === "approval"
            ? "connection.offline.pendingApproval"
            : "connection.offline.idle",
      ),
      actionLabel: t(locale, "connection.check"),
    };
  }

  if (state === "degraded") {
    const lastHealthy = formatLastHealthyLabel(lastHealthyAt, locale);
    return {
      title: t(locale, "connection.degraded.title"),
      detail: pendingOperation === "send"
        ? t(locale, "connection.degraded.pendingSend")
        : pendingOperation === "approval"
          ? t(locale, "connection.degraded.pendingApproval")
          : lastHealthy
            ? t(locale, "connection.degraded.idleWithTime", {
                time: lastHealthy,
              })
            : t(locale, "connection.degraded.idle"),
      actionLabel: t(locale, "connection.check"),
    };
  }

  return {
    title: t(locale, "connection.online.title"),
    detail: t(
      locale,
      pendingOperation === "send"
        ? "connection.online.pendingSend"
        : pendingOperation === "approval"
          ? "connection.online.pendingApproval"
          : "connection.online.detail",
    ),
    actionLabel: t(locale, "connection.check"),
  };
}
