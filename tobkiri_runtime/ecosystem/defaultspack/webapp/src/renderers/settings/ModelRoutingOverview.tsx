import { useId } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, CircleDashed, PlugZap, Server, UserRound } from "lucide-react";

import { cn } from "../../lib/cn";
import { normalizeLocale, type LocaleSetting } from "../../lib/i18n";
import type { SettingsProfileRecord, SettingsProfileWorkspace } from "./settingsProfileModel";

type ModelRoutingOverviewProps = {
  workspace: SettingsProfileWorkspace;
  locale?: LocaleSetting;
  onOpenSection?: (sectionId: string) => void;
  compact?: boolean;
};

function activeRecord(workspace: SettingsProfileWorkspace): SettingsProfileRecord | undefined {
  return workspace.profiles.find((profile) => profile.active)
    ?? workspace.profiles.find((profile) => profile.id === workspace.activeProfileId || profile.modelId === workspace.activeProfileId)
    ?? workspace.profiles.find((profile) => profile.default)
    ?? workspace.profiles[0];
}

function localizedReadinessLabel(profile: SettingsProfileRecord, ja: boolean): string {
  if (!ja) return profile.readiness.replace(/_/g, " ");
  if (profile.readiness === "ready") return "利用可能";
  if (profile.readiness === "local") return "ローカル";
  if (profile.readiness === "needs_connection") return "未接続";
  if (profile.readiness === "blocked") return "利用不可";
  return "未確認";
}

function localizedReadinessReason(profile: SettingsProfileRecord, ja: boolean): string {
  if (!ja) return profile.readinessReason;
  if (profile.readiness === "local") return "この端末で利用できるローカルモデルです。外部Providerの認証情報は不要です。";
  if (profile.readiness === "ready") return `${profile.providerId || "Provider"} の接続とモデル経路を確認済みです。`;
  if (profile.readiness === "needs_connection") return `${profile.providerId || "Provider"} のアカウント接続またはAPIキーを設定してください。`;
  if (profile.readiness === "blocked") return `この経路は現在利用できません。${profile.readinessReason ? ` ${profile.readinessReason}` : "接続権限とProvider設定を確認してください。"}`;
  return "この経路の接続状態はまだ確認できていません。";
}

export function ModelRoutingOverview({ workspace, locale = "ja", onOpenSection, compact = false }: ModelRoutingOverviewProps) {
  const headingId = useId();
  const ja = normalizeLocale(locale) === "ja";
  const copy = (english: string, japanese: string) => ja ? japanese : english;
  const profile = activeRecord(workspace);
  const routeReady = profile?.readiness === "ready" || profile?.readiness === "local";
  const needsConnection = profile?.readiness === "needs_connection" || profile?.readiness === "blocked";
  const routeUnknown = profile?.readiness === "unknown";
  const routeLabel = profile?.routeRefs.length
    ? profile.routeRefs.join(" → ")
    : profile?.providerId
      ? copy("Provider default / OAuth", "Provider既定経路 / OAuth")
      : copy("No provider route reported", "Provider経路は未報告");

  if (!profile) {
    return (
      <section className="border border-white/[0.08] bg-black/15 p-4" aria-labelledby={headingId}>
        <div className="flex items-start gap-3">
          <CircleDashed size={17} className="mt-0.5 shrink-0 text-zinc-500" aria-hidden="true" />
          <div className="min-w-0">
            <h4 id={headingId} className="text-sm font-medium text-zinc-200">{copy("Active route", "現在の経路")}</h4>
            <p className="mt-1 text-xs leading-5 text-zinc-500">{copy("No active profile or model route has been reported yet.", "有効なプロファイルまたはモデル経路がまだ報告されていません。")}</p>
            {onOpenSection ? (
              <button type="button" onClick={() => onOpenSection("models_api")} className="mt-3 text-xs font-medium text-indigo-300 hover:text-indigo-200">
                {copy("Open Models & API", "モデルとAPIを開く")}
              </button>
            ) : null}
          </div>
        </div>
      </section>
    );
  }

  const steps = [
    { key: "profile", icon: UserRound, label: copy("Profile", "プロファイル"), value: profile.name, detail: profile.id },
    { key: "model", icon: Server, label: copy("Model", "モデル"), value: profile.modelId || copy("Inherited", "継承"), detail: profile.role },
    { key: "provider", icon: PlugZap, label: copy("Provider", "Provider"), value: profile.providerId || copy("Local / inherited", "ローカル / 継承"), detail: routeLabel },
  ];

  return (
    <section
      className={cn("border border-white/[0.08] bg-black/15", compact ? "p-3" : "p-4")}
      aria-labelledby={headingId}
      data-settings-routing-overview
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            {needsConnection
              ? <AlertTriangle size={15} className="text-amber-300" aria-hidden="true" />
              : routeReady
                ? <CheckCircle2 size={15} className="text-emerald-300" aria-hidden="true" />
                : <CircleDashed size={15} className="text-zinc-500" aria-hidden="true" />}
            <h4 id={headingId} className="text-sm font-medium text-zinc-100">{copy("Active profile route", "現在のプロファイル経路")}</h4>
          </div>
          <p className={cn("mt-1 text-xs leading-5", needsConnection ? "text-amber-200/80" : "text-zinc-500")}>{localizedReadinessReason(profile, ja)}</p>
        </div>
        <span className={cn(
          "rounded-full border px-2 py-1 text-[10px] font-medium uppercase tracking-[0.12em]",
          profile.readiness === "ready" || profile.readiness === "local"
            ? "border-emerald-400/25 bg-emerald-400/[0.07] text-emerald-200"
            : profile.readiness === "blocked"
              ? "border-red-400/25 bg-red-400/[0.07] text-red-200"
              : profile.readiness === "needs_connection"
                ? "border-amber-300/25 bg-amber-300/[0.07] text-amber-100"
                : "border-white/10 bg-white/[0.04] text-zinc-400",
        )}>
          {localizedReadinessLabel(profile, ja)}
        </span>
      </div>

      <ol className={cn("mt-4 grid min-w-0 gap-2", compact ? "grid-cols-1" : "md:grid-cols-[minmax(0,1fr)_18px_minmax(0,1fr)_18px_minmax(0,1fr)]")} aria-label={copy("Profile routing path", "プロファイルの経路")}>
        {steps.map((step, index) => {
          const Icon = step.icon;
          return (
            <li key={step.key} className="contents">
              <div className="min-w-0 border-l border-white/10 pl-3">
                <span className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.13em] text-zinc-600">
                  <Icon size={11} aria-hidden="true" />{step.label}
                </span>
                <strong className="mt-1 block truncate text-xs font-medium text-zinc-200" title={step.value}>{step.value}</strong>
                <span className="mt-0.5 block truncate font-mono text-[10px] text-zinc-600" title={step.detail}>{step.detail}</span>
              </div>
              {index < steps.length - 1 ? <ArrowRight size={14} className={cn("self-center text-zinc-700", compact && "hidden")} aria-hidden="true" /> : null}
            </li>
          );
        })}
      </ol>

      {needsConnection && onOpenSection ? (
        <div className="mt-4 flex flex-wrap gap-2 border-t border-white/[0.06] pt-3">
          <button type="button" onClick={() => onOpenSection("accounts_connections")} className="rounded-md border border-amber-300/25 px-2.5 py-1.5 text-xs font-medium text-amber-100 hover:bg-amber-300/[0.08]">
            {copy("Review connection", "接続を確認")}
          </button>
          <button type="button" onClick={() => onOpenSection("models_api")} className="rounded-md border border-white/10 px-2.5 py-1.5 text-xs font-medium text-zinc-300 hover:bg-white/[0.05]">
            {copy("Review model routing", "モデル経路を確認")}
          </button>
        </div>
      ) : null}
      {routeUnknown && onOpenSection ? (
        <div className="mt-4 border-t border-white/[0.06] pt-3">
          <button type="button" onClick={() => onOpenSection("models_api")} className="rounded-md border border-white/10 px-2.5 py-1.5 text-xs font-medium text-zinc-300 hover:bg-white/[0.05]">
            {copy("Review model routing", "モデル経路を確認")}
          </button>
        </div>
      ) : null}
    </section>
  );
}
