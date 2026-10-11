import type {RuntimeStatus} from './apiTypes';

export type ViewerRuntimeHealthState = {
  runtimeReady: boolean;
  runtimeStatus: RuntimeStatus;
  runtimeError: string | null;
  runtimeDisconnected: boolean;
  lastRuntimeHealthyAt: number | null;
};

export type RuntimeBannerTone = "success" | "warning" | "danger";

export function runtimeMonitorDelay(state: ViewerRuntimeHealthState): number {
  if (
    state.runtimeDisconnected
    || state.runtimeStatus === "error"
    || state.runtimeStatus === "profile_reconfirmation_required"
  ) return 2_500;
  if (!state.runtimeReady) return 350;
  return 15_000;
}

function formatElapsedLabel(timestamp: number | null, now = Date.now()): string {
  if (!timestamp) return "たった今";
  const elapsed = Math.max(0, now - timestamp);
  const seconds = Math.floor(elapsed / 1000);
  if (seconds < 10) return "たった今";
  if (seconds < 60) return `${seconds}秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}分前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}時間前`;
  const days = Math.floor(hours / 24);
  return `${days}日前`;
}

export function describeRuntimeBadge(
  state: ViewerRuntimeHealthState,
  now = Date.now(),
): {
  tone: RuntimeBannerTone;
  label: string;
  detail: string;
  showOfflineBadge: boolean;
} {
  if (state.runtimeDisconnected) {
    return {
      tone: "danger",
      label: "Reconnecting",
      detail: `最後に安定していたのは ${formatElapsedLabel(state.lastRuntimeHealthyAt, now)}。接続を静かにつなぎ直しています。`,
      showOfflineBadge: true,
    };
  }
  if (state.runtimeReady) {
    return {
      tone: "success",
      label: "Stable",
      detail: "ローカル runtime は安定動作中です。",
      showOfflineBadge: false,
    };
  }
  if (state.runtimeStatus === "error") {
    return {
      tone: "danger",
      label: "Attention",
      detail: state.runtimeError || "起動に必要な準備で止まっています。",
      showOfflineBadge: true,
    };
  }
  if (state.runtimeStatus === "profile_reconfirmation_required") {
    return {
      tone: "warning",
      label: "Profile reconfirmation required",
      detail: "Review and activate the exact Defaults v4 transaction before using local operations.",
      showOfflineBadge: false,
    };
  }
  return {
    tone: "warning",
    label: "Preparing",
    detail: "Tobkiri を開くための足場を整えています。",
    showOfflineBadge: false,
  };
}

export function describeRuntimeBanner(
  state: ViewerRuntimeHealthState,
  now = Date.now(),
): {
  tone: Exclude<RuntimeBannerTone, "success">;
  title: string;
  detail: string;
} {
  if (state.runtimeDisconnected) {
    return {
      tone: "danger",
      title: "接続がほどけても、いまの画面はここに残します。",
      detail: `${formatElapsedLabel(state.lastRuntimeHealthyAt, now)} までは安定していました。Tobkiri Launcher は再接続を続けています。必要なら Launcher を再起動できます。`,
    };
  }
  if (state.runtimeStatus === "error") {
    return {
      tone: "danger",
      title: "起動は止まりましたが、復帰の道筋は残しています。",
      detail: state.runtimeError || "Tobkiri Launcher を再起動して原因を確認できます。",
    };
  }
  if (state.runtimeStatus === "profile_reconfirmation_required") {
    return {
      tone: "warning",
      title: "Profile reconfirmation is required before runtime operations can resume.",
      detail: "Open Setup to review the exact Defaults v4 transaction and activate it.",
    };
  }
  return {
    tone: "warning",
    title: "いまは静かに起動中です。",
    detail: "準備が整い次第、そのまま操作を続けられます。",
  };
}
