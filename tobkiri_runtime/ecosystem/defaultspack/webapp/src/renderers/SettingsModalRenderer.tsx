import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { AlertTriangle, ArrowRight, Check, ChevronDown, Copy, Loader2, MessageCircle, MoreVertical, Pencil, Plus, Search, Trash2, X } from "lucide-react";

import { cn } from "../lib/cn";
import type { CodexAppServerConfig, ModelSearchItem, SettingsSection } from "../lib/api";
import { PlacementHtmlRenderer } from "../components/PlacementHtmlRenderer";
import { AppsSettingsPanel } from "../components/AppsSettingsPanel";
import { CredentialTransferModal } from "../components/CredentialTransferModal";
import { ToolExperienceSettingsPanel } from "../components/ToolExperienceSettingsPanel";
import { MobilePairingApproval } from "../components/MobilePairingApproval";
import { normalizeLocale, t } from "../lib/i18n";
import { buildBuiltinPlacementManifests, filterPlacementCandidates, normalizePinnedPlacements, togglePinnedPlacement, type PlacementManifest } from "../lib/placement";
import { selectedApisForModel, toggleModelApiRoute, updateModelApiRouteText } from "../lib/modelApiRoutes";
import { settingsFieldSearchText, settingsSectionSearchText } from "../lib/settingsSearch";
import { reviewConnectionDraft, reviewOAuthDestination, type CredentialImportReview, type OAuthDestinationReview } from "../lib/oauthConnectionReview";
import { settingsApiResources } from "../features/settings/resources/settingsApiResources";
import { availabilityCopy, type ModelAvailabilityAfterKeySave } from "../features/settings/resources/useModelAvailability";
import { providerBrandAsset } from "../features/connections/providerBrandAssets";
import { ContinuitySettingsField } from "../features/continuity/ContinuitySettingsField";
import {
  ModelSearchPicker,
  modelProviderOptions,
  parseModelProviderQuery,
  parseModelSelectorSchema,
  type ModelSelectorSchema,
} from "../features/models";
import type { SettingsModalRendererProps, SettingsSaveState } from "./types";
import type { DesktopPermissionStatus, DesktopSystemInfo } from "../lib/desktopSystemInfo";
import {
  buildCodexAppServerPrelude,
  buildControlCenterSections,
  buildAccountConnectionPrelude,
  filterControlCenterSections,
  mapSettingsSectionId,
  localizedSettingsSourceLabel,
  safeSettingsLabel,
  type AccountConnectionPreludeCard,
  type AccountConnectionScopeModeOption,
  type CodexAppServerPrelude,
  type ControlCenterField,
  type ControlCenterSection,
} from "../settings/controlCenter";
import {
  createSettingsFieldRendererRegistry,
  SettingsFieldRendererHost,
  type SettingsFieldRendererProps,
} from "./settings/fieldRendererRegistry";
import { builtinSettingsFieldRendererEntries } from "./settings/builtinSettingsFieldRenderers";
import { ModelRoutingOverview } from "./settings/ModelRoutingOverview";
import { ProfileSettingsPanel } from "./settings/ProfileSettingsPanel";
import { buildSettingsProfileWorkspace } from "./settings/settingsProfileModel";

const settingsModalFieldRendererRegistry = createSettingsFieldRendererRegistry([
  ...builtinSettingsFieldRendererEntries,
  {
    id: "builtin-settings-mobile-pairing-review",
    types: ["mobile_pairing_review"],
    renderers: ["mobile_pairing_review", "MobilePairingApproval"],
    component: "MobilePairingApproval",
    render: MobilePairingReviewField,
  },
  {
    id: "builtin-settings-model-routing",
    types: ["model_api_routes"],
    renderers: ["model_routing", "model_api_routes", "ModelApiRoutesSettingsField"],
    component: "ModelApiRoutesSettingsField",
    render: ModelApiRoutesSettingsFieldRenderer,
  },
  {
    id: "builtin-settings-continuity",
    types: ["continuity"],
    renderers: ["continuity", "ContinuitySettingsField"],
    component: "ContinuitySettingsField",
    render: ContinuitySettingsField,
  },
]);

export function settingsCloseRequiresConfirmation(saveState: SettingsSaveState): boolean {
  // Setting changes are persisted by App.tsx's parent-owned save queue, so
  // unmounting this modal does not cancel an in-flight save. Blocking close
  // while that queue is running makes a normal change-then-close interaction
  // look broken. Only stop the user when a save has actually failed and there
  // are still dirty values that may need attention.
  return saveState.status === "error" && (saveState.dirtyKeys?.length ?? 0) > 0;
}

export function toggleSettingsRowSelection(currentKey: string, clickedKey: string): string {
  return currentKey === clickedKey ? "" : clickedKey;
}

type PendingOAuthReview = OAuthDestinationReview & {
  popup: Window | null;
  scopes: string[];
};

function MobilePairingReviewField({ sectionId, field, value, onChange }: SettingsFieldRendererProps) {
  const pairingId = String(value ?? "").trim();
  const originRef = useRef<HTMLInputElement>(null);
  const [dismissedId, setDismissedId] = useState("");
  const visible = pairingId.length > 0 && dismissedId !== pairingId;
  return (
    <div className="space-y-3" data-settings-renderer="mobile_pairing_review">
      <label className="block text-sm text-zinc-300">
        {field.label}
        <input
          ref={originRef}
          value={String(value ?? "")}
          onChange={(event) => { setDismissedId(""); onChange(sectionId, field.id, event.target.value); }}
          placeholder="pair-…"
          autoComplete="off"
          spellCheck={false}
          className="mt-2 h-10 w-full rounded-md border border-white/[0.09] bg-white/[0.04] px-3 font-mono text-sm outline-none focus:border-indigo-400/50"
        />
      </label>
      <p className="text-xs leading-5 text-zinc-500">PCで作成したpairing IDを入力すると、authoritative requestを再取得して安全に確認します。</p>
      {visible ? <MobilePairingApproval pairingId={pairingId} originRef={originRef} onClose={() => setDismissedId(pairingId)} /> : null}
      {!visible && pairingId ? <button type="button" onClick={() => setDismissedId("")} className="text-xs underline">接続要求をもう一度開く</button> : null}
    </div>
  );
}

function formatReadonlyValue(value: unknown, fallback: unknown): string {
  const resolved = value ?? fallback ?? "";
  if (typeof resolved === "boolean") return resolved ? "保存済み" : "未設定";
  if (resolved && typeof resolved === "object") return JSON.stringify(resolved, null, 2);
  return String(resolved);
}

function formFieldString(value: unknown, fallback: unknown = ""): string {
  const resolved = value ?? fallback ?? "";
  if (typeof resolved === "string") return resolved;
  if (typeof resolved === "number" || typeof resolved === "boolean") return String(resolved);
  try {
    return JSON.stringify(resolved, null, 2);
  } catch {
    return "";
  }
}

function colorFieldValue(value: unknown, fallback: unknown): string {
  const resolved = String(value ?? fallback ?? "#ffffff").trim();
  return /^#[0-9a-fA-F]{6}$/.test(resolved) ? resolved : "#ffffff";
}

function fieldRecord(field: SettingsSection["fields"][number]): Record<string, unknown> {
  return field as SettingsSection["fields"][number] & Record<string, unknown>;
}

function settingsFieldVisible(field: SettingsSection["fields"][number], sectionValues: Record<string, unknown>): boolean {
  const visibleWhen = fieldRecord(field).visible_when;
  if (!visibleWhen || typeof visibleWhen !== "object" || Array.isArray(visibleWhen)) return true;
  const condition = visibleWhen as Record<string, unknown>;
  const conditionField = String(condition.field ?? condition.setting ?? "").trim();
  if (!conditionField) return true;
  const value = sectionValues[conditionField];
  let matches = true;
  if ("equals" in condition) {
    matches = value === condition.equals;
  } else if ("not_equals" in condition) {
    matches = value !== condition.not_equals;
  } else if ("truthy" in condition) {
    matches = Boolean(value) === Boolean(condition.truthy);
  } else {
    matches = Boolean(value);
  }
  return condition.not === true ? !matches : matches;
}

function settingsFieldTakesFullWidth(field: SettingsSection["fields"][number]): boolean {
  const type = String(field.type);
  return (
    type === "textarea"
    || type === "secret"
    || type === "api_keys"
    || type === "api_key_setup"
    || type === "external_tokens"
    || type === "public_url"
    || type === "model_api_routes"
    || type === "continuity"
    || type === "device_lock"
    || type === "mobile_pairing_review"
    || type === "slash_commands"
    || field.id.endsWith("_setup_guide")
  );
}

function permissionStatusLabel(permission: DesktopPermissionStatus): string {
  if (permission.granted === true || permission.status === "granted") return "Granted";
  if (permission.granted === false || permission.status === "missing") return "Missing";
  if (permission.status === "not_checked") return "Manual check";
  if (permission.status === "unsupported") return "Unsupported";
  return permission.status || "Unknown";
}

function permissionBadgeClass(permission: DesktopPermissionStatus): string {
  if (permission.granted === true || permission.status === "granted") {
    return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300";
  }
  if (permission.granted === false || permission.status === "missing") {
    return "border-rose-500/30 bg-rose-500/10 text-rose-300";
  }
  if (permission.status === "not_checked") {
    return "border-amber-500/30 bg-amber-500/10 text-amber-200";
  }
  return "border-zinc-800 bg-zinc-900 text-zinc-400";
}

function SystemInfoPanel({ info }: { info?: DesktopSystemInfo | null }) {
  if (!info) {
    return (
      <div className="rounded-lg border border-white/[0.07] bg-white/[0.03] p-4 text-sm leading-6 text-zinc-400">
        Tobkiri Launcher の権限状態を取得できませんでした。Tobkiri Launcherを起動し、Accessibility / Screen Recording / Input Monitoring を許可してください。
      </div>
    );
  }
  const versionRows = [
    ["App", info.display_version],
    ["Viewer", info.viewer_version],
    ["Channel", info.build_channel],
    ["Platform", [info.platform, info.platform_release].filter(Boolean).join(" ")],
  ];
  const unverified = !info.reliable || (info.source !== "launcher_tauri" && info.source !== "viewer_tauri" && info.source !== "viewer_broker");
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        {versionRows.map(([label, value]) => (
          <div key={label} className="flex items-center justify-between gap-3 rounded-lg border border-white/[0.07] bg-white/[0.035] px-3 py-2.5">
            <span className="text-xs text-zinc-500">{label}</span>
            <span className="font-mono text-xs text-zinc-200">{value || "unknown"}</span>
          </div>
        ))}
      </div>
      <section className="space-y-3 rounded-xl border border-white/[0.07] bg-white/[0.03] p-4">
        <div>
          <h4 className="text-sm font-medium text-zinc-100">Permission Host</h4>
          <p className="mt-1 text-xs leading-5 text-zinc-500">
            macOSの承認対象は {info.permission_subject || "Tobkiri Launcher"} です。
            DefaultspackはTobkiri Launcher経由で、許可された操作だけを実行します。
          </p>
        </div>
        <div className="flex flex-wrap gap-2 text-[11px] text-zinc-400">
          <span className="rounded-full border border-white/[0.07] bg-white/[0.045] px-2.5 py-1">画面を見る</span>
          <span className="rounded-full border border-white/[0.07] bg-white/[0.045] px-2.5 py-1">クリック・キーボード操作</span>
          <span className="rounded-full border border-white/[0.07] bg-white/[0.045] px-2.5 py-1">ブラウザ操作</span>
        </div>
      </section>
      {unverified ? (
        <section className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm leading-6 text-amber-100">
          Viewer permission status is unverified. Open Tobkiri Launcher or reconnect Viewer broker.
        </section>
      ) : (
      <section className="space-y-3">
        <div>
          <h4 className="text-sm font-medium text-zinc-100">macOS Permissions</h4>
          <p className="mt-1 text-xs text-zinc-500">Computer Use と画面確認に使う macOS 側の承認状態です。</p>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {info.permissions.map((permission) => (
            <div key={permission.id} className="rounded-lg border border-white/[0.07] bg-white/[0.035] p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-zinc-100">{permission.label}</div>
                  <p className="mt-1 text-xs leading-5 text-zinc-500">{permission.detail}</p>
                </div>
                <span className={cn("shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium", permissionBadgeClass(permission))}>
                  {permissionStatusLabel(permission)}
                </span>
              </div>
              {permission.settings_hint && (
                <p className="mt-3 rounded-md border border-white/[0.07] bg-white/[0.04] px-2.5 py-2 text-[11px] text-zinc-500">
                  {permission.settings_hint}
                </p>
              )}
            </div>
          ))}
        </div>
      </section>
      )}
    </div>
  );
}

async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function apiProviderRows(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function namedApiRows(provider: Record<string, unknown>): Array<Record<string, unknown>> {
  const apis = provider.apis;
  return Array.isArray(apis) ? apis.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function registeredApiRows(providers: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  const rows: Array<Record<string, unknown>> = providers.flatMap((provider) => (
    namedApiRows(provider).map((api) => ({
      ...api,
      provider_id: api.provider_id ?? provider.provider_id,
    }))
  ));
  return rows.filter((api) => Boolean(api.configured));
}

function modelRouteOptions(field: SettingsSection["fields"][number]): NonNullable<SettingsSection["fields"][number]["options"]> {
  return Array.isArray(field.options)
    ? field.options.filter((item) => Boolean(item) && typeof item === "object")
    : [];
}

function fieldApiProviderRows(field: SettingsSection["fields"][number]): Array<Record<string, unknown>> {
  return Array.isArray(field.api_keys)
    ? field.api_keys.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    : [];
}

function routeProviderForOption(option: SettingsModelOption | NonNullable<SettingsSection["fields"][number]["options"]>[number] | undefined, modelId: string): string {
  const provider = String(option?.provider_id ?? "").trim();
  if (provider) return provider;
  return modelId.includes("/") ? modelId.split("/", 1)[0] ?? "" : "";
}

function apiRefForRoute(api: Record<string, unknown>, fallbackProvider: string): string {
  const providerId = String(api.provider_id ?? fallbackProvider ?? "").trim();
  const apiId = String(api.api_id ?? "").trim();
  return providerId && apiId ? `${providerId}/${apiId}` : "";
}

function oauthProviderRows(providers: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  return providers.filter((provider) => {
    const oauth = provider.oauth;
    return Boolean(oauth) && typeof oauth === "object" && Boolean((oauth as Record<string, unknown>).supported);
  });
}

const profileReferenceLabelKeys = ["display_name", "displayName", "label", "name", "title"];
const profileReferenceIdKeys = [
  "profile_id",
  "profileId",
  "active_profile_id",
  "activeProfileId",
  "selected_profile_id",
  "selectedProfileId",
  "model_profile_id",
  "modelProfileId",
  "qualified_model_id",
  "qualifiedModelId",
  "id",
  "key",
  "value",
  "preferred_model",
  "preferredModel",
  "model_id",
  "modelId",
];
const profileReferenceSettingKeys = [
  "active_profile",
  "active_profile_id",
  "selected_profile_id",
  "profile_id",
  "default_profile",
  "default_profile_id",
  "preferred_model",
];

function profileReferenceScalar(value: unknown): string {
  if (typeof value === "string") return value.trim();
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return "";
}

function profileReferenceRecordValue(record: Record<string, unknown>, keys: readonly string[]): string {
  for (const key of keys) {
    try {
      const normalized = profileReferenceScalar(record[key]);
      if (normalized) return normalized;
    } catch {
      // Treat malformed runtime objects as unavailable instead of coercing them.
    }
  }
  return "";
}

function profileReferenceValue(value: unknown, preferLabel: boolean): string {
  const scalar = profileReferenceScalar(value);
  if (scalar) return scalar;
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const record = value as Record<string, unknown>;
  const primaryKeys = preferLabel ? profileReferenceLabelKeys : profileReferenceIdKeys;
  const fallbackKeys = preferLabel ? profileReferenceIdKeys : profileReferenceLabelKeys;
  return profileReferenceRecordValue(record, primaryKeys) || profileReferenceRecordValue(record, fallbackKeys);
}

function profileReferenceId(value: unknown): string {
  return profileReferenceValue(value, false);
}

function profileReferenceLabel(value: unknown): string {
  return profileReferenceValue(value, true);
}

function normalizeProfileReferenceSettings(settingsValues: Record<string, Record<string, unknown>>): Record<string, Record<string, unknown>> {
  let normalizedSettingsValues = settingsValues;
  for (const sectionId of ["profiles", "profile", "adaptive", "models"]) {
    const sectionValues = settingsValues[sectionId];
    if (!sectionValues) continue;
    let normalizedSectionValues = sectionValues;
    for (const key of profileReferenceSettingKeys) {
      const value = sectionValues[key];
      if (!value || typeof value !== "object" || Array.isArray(value)) continue;
      const normalized = profileReferenceId(value);
      if (!normalized) continue;
      if (normalizedSectionValues === sectionValues) normalizedSectionValues = { ...sectionValues };
      normalizedSectionValues[key] = normalized;
    }
    if (normalizedSectionValues !== sectionValues) {
      if (normalizedSettingsValues === settingsValues) normalizedSettingsValues = { ...settingsValues };
      normalizedSettingsValues[sectionId] = normalizedSectionValues;
    }
  }
  return normalizedSettingsValues;
}

function normalizeProfileReferenceCatalog(catalog: SettingsModalRendererProps["catalog"]): SettingsModalRendererProps["catalog"] {
  if (!catalog) return catalog;
  const normalizedValues = normalizeProfileReferenceSettings(catalog.settings.values);
  if (normalizedValues === catalog.settings.values) return catalog;
  return {
    ...catalog,
    settings: {
      ...catalog.settings,
      values: normalizedValues,
    },
  };
}

function activeSettingsProfileLabel(
  settingsValues: Record<string, Record<string, unknown>>,
  catalog: SettingsModalRendererProps["catalog"],
): string {
  const candidates = [
    settingsValues.profiles?.active_profile,
    settingsValues.profiles?.profile_id,
    settingsValues.profile?.active_profile,
    settingsValues.profile?.profile_id,
    settingsValues.models?.active_profile,
    settingsValues.models?.selected_profile_id,
    settingsValues.models?.preferred_model,
    catalog?.settings?.values?.profiles?.active_profile,
    catalog?.settings?.values?.models?.preferred_model,
  ].map(profileReferenceLabel).filter(Boolean);
  return candidates[0] ?? "No active profile reported";
}

function apiRowLabel(api: Record<string, unknown>): string {
  return String(api.label ?? `${api.provider_id}:${api.api_id}:***`);
}

function externalTokenProviderRows(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function namedTokenRows(provider: Record<string, unknown>): Array<Record<string, unknown>> {
  const tokens = provider.tokens;
  return Array.isArray(tokens) ? tokens.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object") : [];
}

function registeredExternalTokenRows(providers: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  const rows: Array<Record<string, unknown>> = providers.flatMap((provider) => (
    namedTokenRows(provider).map((token) => ({
      ...token,
      provider_id: token.provider_id ?? provider.provider_id,
    }))
  ));
  return rows.filter((token) => Boolean(token.configured));
}

function requiredExternalTokenRows(providers: Array<Record<string, unknown>>): Array<Record<string, unknown>> {
  return providers.flatMap((provider) => {
    const required = provider.required_tokens;
    if (!Array.isArray(required)) return [];
    return required
      .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
      .map((item) => ({ ...item, provider_id: provider.provider_id }));
  });
}

function MaskedApiLabel({ api }: { api: Record<string, unknown> }) {
  const providerId = String(api.provider_id ?? "");
  const apiId = String(api.api_id ?? "");
  const fallback = apiRowLabel(api);
  if (!providerId || !apiId) {
    return <span className="truncate text-xs text-zinc-300">{fallback}</span>;
  }
  return (
    <span className="inline-flex max-w-full items-center overflow-hidden font-mono text-xs leading-5 text-zinc-500">
      <span className="truncate">{providerId}</span>
      <span className="px-0.5 text-zinc-600">:</span>
      <span className="truncate">{apiId}</span>
      <span className="px-0.5 text-zinc-600">:</span>
      <span className="tracking-normal text-zinc-500">***</span>
    </span>
  );
}

function MaskedExternalTokenLabel({ token }: { token: Record<string, unknown> }) {
  const providerId = String(token.provider_id ?? "");
  const tokenId = String(token.token_id ?? "");
  const fallback = String(token.label ?? `${providerId}:${tokenId}:***`);
  if (!providerId || !tokenId) {
    return <span className="truncate text-xs text-zinc-300">{fallback}</span>;
  }
  return (
    <span className="inline-flex max-w-full items-center overflow-hidden font-mono text-xs leading-5 text-zinc-500">
      <span className="truncate">{providerId}</span>
      <span className="px-0.5 text-zinc-600">:</span>
      <span className="truncate">{tokenId}</span>
      <span className="px-0.5 text-zinc-600">:</span>
      <span className="tracking-normal text-zinc-500">***</span>
    </span>
  );
}

function externalTokenKindOptions(providerId: string): Array<{ value: string; label: string }> {
  const common: Record<string, Array<{ value: string; label: string }>> = {
    line: [
      { value: "channel_secret", label: "Messaging APIチャネルシークレット" },
      { value: "channel_access_token", label: "Messaging APIチャネルアクセストークン" },
      { value: "reply_token", label: "返信トークン" },
    ],
    discord: [
      { value: "bot_token", label: "Botトークン" },
      { value: "webhook_url", label: "Webhook URL" },
      { value: "application_id", label: "アプリケーションID" },
      { value: "public_key", label: "公開鍵" },
    ],
    slack: [
      { value: "bot_token", label: "Botトークン" },
      { value: "signing_secret", label: "署名シークレット" },
      { value: "app_token", label: "アプリトークン" },
      { value: "channel_id", label: "チャンネルID" },
    ],
    generic: [
      { value: "webhook_shared_secret", label: "Webhook共有シークレット" },
      { value: "webhook_url", label: "Webhook URL" },
      { value: "callback_url", label: "コールバックURL" },
    ],
    web: [
      { value: "callback_url", label: "コールバックURL" },
    ],
  };
  return common[providerId] ?? [
    { value: "token", label: "トークン" },
    { value: "webhook_url", label: "Webhook URL" },
  ];
}

function CustomSelect({
  value,
  options,
  onChange,
  className,
}: {
  value: string;
  options: Array<{ value: string; label: string }>;
  onChange: (value: string) => void;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.value === value) ?? options[0];
  return (
    <div className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        title={selected?.label ?? value}
        className="flex min-h-10 w-full items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-left text-sm text-zinc-200 outline-none transition-colors hover:border-zinc-700"
      >
        <span className="min-w-0 flex-1 truncate">{selected?.label ?? value}</span>
        <ChevronDown size={14} className={cn("shrink-0 text-zinc-500 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <>
          <button type="button" aria-label="close select" className="fixed inset-0 rumi-layer-panel cursor-default" onClick={() => setOpen(false)} />
          <div className="absolute left-0 top-[calc(100%+6px)] rumi-layer-local-popover max-h-56 w-[min(360px,calc(100vw-32px))] max-w-[calc(100vw-32px)] overflow-y-auto rumi-popover p-1">
            {options.map((option) => (
              <button
                key={option.value}
                type="button"
                title={option.label}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                }}
                className={cn(
                  "flex w-full items-start justify-between gap-2 rounded-md px-2.5 py-1.5 text-left text-sm transition-colors",
                  option.value === value ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200",
                )}
              >
                <span className="min-w-0 flex-1 whitespace-normal break-all leading-5">{option.label}</span>
                {option.value === value && <Check size={13} className="mt-1 shrink-0 text-emerald-300" />}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

type SettingsModelOption = {
  value: string;
  label: string;
  provider_id?: string;
  provider_display_name?: string;
  model_id?: string;
  qualified_model_id?: string;
  configured?: boolean;
  local?: boolean;
  supports_vision?: boolean;
  supports_image_input?: boolean;
  supports_tool_calling?: boolean;
  supports_thinking?: boolean;
  supports_fast?: boolean;
  speed_tier?: string;
  quality_tier?: string;
  cost_tier?: string;
  knowledge_level?: number;
  capability_tags?: string[];
  recommended_roles?: string[];
  notes?: string;
};

const MODEL_PICKER_QUERY_RESULT_LIMIT = 60;

function modelFieldOptionToOption(option: NonNullable<SettingsSection["fields"][number]["options"]>[number]): SettingsModelOption {
  return {
    value: String(option.value ?? ""),
    label: String(option.label ?? option.value ?? ""),
    provider_id: option.provider_id,
    provider_display_name: option.provider_display_name,
    model_id: option.model_id,
    qualified_model_id: option.qualified_model_id,
    configured: option.configured,
    local: option.local,
    supports_vision: option.supports_vision,
    supports_image_input: option.supports_image_input,
    supports_tool_calling: option.supports_tool_calling,
    supports_thinking: option.supports_thinking,
    supports_fast: option.supports_fast,
    speed_tier: option.speed_tier,
    quality_tier: option.quality_tier,
    cost_tier: option.cost_tier,
    knowledge_level: option.knowledge_level,
    capability_tags: option.capability_tags,
    recommended_roles: option.recommended_roles,
    notes: option.notes,
  };
}

function modelSearchItemToOption(item: ModelSearchItem): SettingsModelOption {
  return {
    value: String(item.profile_id ?? item.qualified_model_id ?? `${item.provider_id ?? ""}/${item.model_id ?? ""}`),
    label: String(item.label ?? item.display_name ?? item.profile_id ?? ""),
    provider_id: item.provider_id,
    provider_display_name: item.provider_display_name,
    model_id: item.model_id,
    qualified_model_id: item.qualified_model_id,
    configured: item.configured,
    local: Boolean(item.local),
    supports_vision: item.supports_vision,
    supports_image_input: item.supports_image_input,
    supports_tool_calling: item.supports_tool_calling,
    supports_thinking: item.supports_thinking,
    supports_fast: item.supports_fast,
    speed_tier: item.speed_tier,
    quality_tier: item.quality_tier,
    cost_tier: item.cost_tier,
    knowledge_level: item.knowledge_level,
    capability_tags: item.capability_tags,
    recommended_roles: item.recommended_roles,
    notes: item.notes,
  };
}

function modelOptionSearchText(option: SettingsModelOption): string {
  return [
    option.value,
    option.label,
    option.provider_id,
    option.provider_display_name,
    option.model_id,
    option.qualified_model_id,
    option.speed_tier,
    option.quality_tier,
    option.cost_tier,
    option.notes,
    ...(option.capability_tags ?? []),
    ...(option.recommended_roles ?? []),
  ].filter(Boolean).join(" ").toLowerCase();
}

function normalizeModelSearchText(value: string): string {
  return value.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function modelOptionMatchesSearch(option: SettingsModelOption, query: string): boolean {
  const rawText = modelOptionSearchText(option);
  const normalizedText = normalizeModelSearchText(rawText);
  const rawQuery = query.trim().toLowerCase();
  const normalizedQuery = normalizeModelSearchText(rawQuery);
  if (!normalizedQuery) return true;
  if (rawText.includes(rawQuery) || normalizedText.includes(normalizedQuery)) return true;
  return normalizedQuery.split(/\s+/).every((token) => normalizedText.includes(token) || rawText.includes(token));
}

function dedupeModelOptions(options: SettingsModelOption[]): SettingsModelOption[] {
  const seen = new Set<string>();
  const deduped: SettingsModelOption[] = [];
  for (const option of options) {
    if (!option.value || seen.has(option.value)) continue;
    seen.add(option.value);
    deduped.push(option);
  }
  return deduped;
}

export function buildVisibleModelOptions({
  options,
  selected,
  remoteOptions,
  query,
}: {
  options: SettingsModelOption[];
  selected?: SettingsModelOption | null;
  remoteOptions?: SettingsModelOption[];
  query?: string;
}): SettingsModelOption[] {
  const trimmedQuery = String(query ?? "").trim();
  const localMatches = trimmedQuery
    ? options.filter((option) => modelOptionMatchesSearch(option, trimmedQuery))
    : options;
  const merged = dedupeModelOptions([
    ...(selected ? [selected] : []),
    ...localMatches,
    ...(remoteOptions ?? []),
  ]);
  if (!trimmedQuery) return merged;
  return merged.slice(0, MODEL_PICKER_QUERY_RESULT_LIMIT);
}

function modelOptionBadges(option: SettingsModelOption): string[] {
  const badges: string[] = [];
  if (option.configured) badges.push("設定済み");
  if (option.local) badges.push("ローカル");
  if (option.supports_vision || option.supports_image_input) badges.push("画像");
  if (option.supports_tool_calling) badges.push("ツール");
  if (option.supports_thinking) badges.push("推論");
  if (option.supports_fast || option.speed_tier === "fast") badges.push("高速");
  if (option.cost_tier && option.cost_tier !== "unknown") badges.push(option.cost_tier);
  return badges.slice(0, 4);
}

function parseModelAllowlist(value: unknown, fallback: unknown): string[] {
  const source = value ?? fallback ?? "";
  const items = Array.isArray(source)
    ? source.map((item) => String(item ?? ""))
    : String(source).split(/\r?\n|,/);
  return Array.from(new Set(items.map((item) => item.trim()).filter(Boolean)));
}

function serializeModelAllowlist(items: string[]): string {
  return Array.from(new Set(items.map((item) => item.trim()).filter(Boolean))).join("\n");
}

function SettingsModelSearchSelect({
  value,
  options,
  onChange,
  placeholder = "モデルを検索",
  selectorSchema,
}: {
  value: string;
  options: SettingsModelOption[];
  onChange: (value: string) => void;
  placeholder?: string;
  selectorSchema?: ModelSelectorSchema;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [remoteResults, setRemoteResults] = useState<ModelSearchItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const searchRequestSeq = useRef(0);
  const trimmedQuery = query.trim();
  const resolvedSelectorSchema = selectorSchema ?? parseModelSelectorSchema(undefined);
  const providerState = parseModelProviderQuery(
    query,
    modelProviderOptions(options),
    resolvedSelectorSchema.layout.provider_trigger,
  );

  useEffect(() => {
    if (!open) return;
    searchRequestSeq.current += 1;
    const requestSeq = searchRequestSeq.current;
    let disposed = false;
    setRemoteResults([]);
    if (providerState.active) {
      setBusy(false);
      setError("");
      return;
    }
    setBusy(Boolean(trimmedQuery));
    setError("");
    const timer = window.setTimeout(() => {
      if (!trimmedQuery) return;
      settingsApiResources.searchModels({
        query: providerState.providerId ? providerState.modelQuery : trimmedQuery,
        max_results: 30,
        ...(providerState.providerId ? { provider_id: providerState.providerId } : {}),
      })
        .then((result) => {
          if (disposed || requestSeq !== searchRequestSeq.current) return;
          setRemoteResults(result.models ?? []);
        })
        .catch((searchError: unknown) => {
          if (disposed || requestSeq !== searchRequestSeq.current) return;
          setRemoteResults([]);
          setError(searchError instanceof Error ? searchError.message : "モデル検索に失敗しました");
        })
        .finally(() => {
          if (!disposed && requestSeq === searchRequestSeq.current) setBusy(false);
        });
    }, 160);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [
    open,
    providerState.active,
    providerState.modelQuery,
    providerState.providerId,
    trimmedQuery,
  ]);

  return (
    <ModelSearchPicker
      value={value}
      options={options}
      remoteResults={remoteResults}
      query={query}
      loading={busy}
      error={error}
      placeholder={placeholder}
      selectorSchema={resolvedSelectorSchema}
      surface="settings"
      open={open}
      onOpenChange={setOpen}
      onChange={onChange}
      onQueryChange={setQuery}
    />
  );
}

function ModelAllowlistField({
  value,
  fallback,
  options,
  onChange,
}: {
  value: unknown;
  fallback: unknown;
  options: SettingsModelOption[];
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [remoteResults, setRemoteResults] = useState<ModelSearchItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const searchRequestSeq = useRef(0);
  const selectedModels = parseModelAllowlist(value, fallback);
  const selectedSet = useMemo(() => new Set(selectedModels), [selectedModels]);
  const selectedOptions = useMemo(() => {
    const byId = new Map(options.flatMap((option) => [option.value, option.qualified_model_id].filter(Boolean).map((id) => [String(id), option] as const)));
    return selectedModels.map((modelId) => ({ modelId, option: byId.get(modelId) }));
  }, [options, selectedModels]);
  const trimmedQuery = query.trim();

  useEffect(() => {
    if (!open) return;
    searchRequestSeq.current += 1;
    const requestSeq = searchRequestSeq.current;
    let disposed = false;
    setRemoteResults([]);
    setBusy(true);
    setError("");
    const timer = window.setTimeout(() => {
      settingsApiResources.searchModels({ query: trimmedQuery, max_results: 50 })
        .then((result) => {
          if (disposed || requestSeq !== searchRequestSeq.current) return;
          setRemoteResults(result.models ?? []);
        })
        .catch((searchError: unknown) => {
          if (disposed || requestSeq !== searchRequestSeq.current) return;
          setRemoteResults([]);
          setError(searchError instanceof Error ? searchError.message : "モデル検索に失敗しました");
        })
        .finally(() => {
          if (!disposed && requestSeq === searchRequestSeq.current) setBusy(false);
        });
    }, trimmedQuery ? 160 : 0);
    return () => {
      disposed = true;
      window.clearTimeout(timer);
    };
  }, [open, trimmedQuery]);

  const candidateOptions = useMemo(() => {
    const localMatches = trimmedQuery
      ? options.filter((option) => modelOptionMatchesSearch(option, trimmedQuery))
      : options;
    return dedupeModelOptions([
      ...localMatches,
      ...remoteResults.map(modelSearchItemToOption),
    ])
      .filter((option) => !selectedSet.has(option.value))
      .slice(0, 50);
  }, [options, remoteResults, selectedSet, trimmedQuery]);

  const commit = (items: string[]) => onChange(serializeModelAllowlist(items));
  const addModel = (modelId: string) => {
    const cleaned = modelId.trim();
    if (!cleaned || selectedSet.has(cleaned)) return;
    commit([...selectedModels, cleaned]);
    setOpen(false);
    setQuery("");
  };
  const removeModel = (modelId: string) => {
    commit(selectedModels.filter((item) => item !== modelId));
  };

  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-white/[0.08] bg-white/[0.025] p-3">
        <div className="mb-2 flex items-center justify-between gap-3 text-[11px] text-zinc-500">
          <span>利用するモデル</span>
          <span>{selectedModels.length}件</span>
        </div>
        <div className="flex flex-wrap gap-2">
        {selectedOptions.length > 0 ? selectedOptions.map(({ modelId, option }) => (
          <span
            key={modelId}
            className="inline-flex max-w-full items-center gap-1.5 rounded-full border border-white/[0.09] bg-white/[0.04]/90 px-2.5 py-1 text-xs text-zinc-200"
            title={modelId}
          >
            <span className="max-w-[260px] truncate">{option?.label || modelId}</span>
            {option?.provider_id ? <span className="text-[10px] text-zinc-500">· {option.provider_id}</span> : null}
            <button
              type="button"
              onClick={() => removeModel(modelId)}
              className="rounded-full p-0.5 text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100"
              aria-label={`${option?.label || modelId} を利用するモデルから削除`}
            >
              <X size={12} />
            </button>
          </span>
        )) : (
          <span className="px-1 py-1 text-xs text-zinc-600">まだモデルを選択していません。モデルカタログから追加してください。</span>
        )}
        </div>
      </div>
      <div className="relative">
        <button
          type="button"
          onClick={() => setOpen((current) => !current)}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors",
            open
              ? "border-emerald-500/60 bg-emerald-500/15 text-emerald-100"
              : "border-zinc-800 bg-zinc-900 text-zinc-200 hover:border-zinc-700",
          )}
        >
          <Plus size={14} />
          モデルを追加
        </button>
        {open && (
          <>
            <button
              type="button"
              aria-label="モデル追加を閉じる"
              className="fixed inset-0 rumi-layer-panel cursor-default"
              onClick={() => setOpen(false)}
            />
            <div className="absolute left-0 top-[calc(100%+8px)] rumi-layer-local-popover w-[min(560px,calc(100vw-32px))] overflow-hidden rounded-xl border border-zinc-700 bg-zinc-950 shadow-2xl">
              <label className="m-2 flex h-9 items-center gap-2 rounded-lg border border-zinc-800 bg-black/30 px-3 text-xs text-zinc-500 focus-within:border-zinc-600 focus-within:text-zinc-300">
                <Search size={14} />
                <input
                  autoFocus
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="モデル名、提供元、用途で検索"
                  className="min-w-0 flex-1 bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
                />
                {busy && <Loader2 size={13} className="animate-spin text-zinc-500" />}
                {query && (
                  <button
                    type="button"
                    onClick={() => setQuery("")}
                    className="rounded p-0.5 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300"
                    aria-label="モデル検索をクリア"
                  >
                    <X size={13} />
                  </button>
                )}
              </label>
              {error && <div className="border-t border-zinc-800 px-3 py-2 text-[11px] text-rose-300">{error}</div>}
              <div className="max-h-72 overflow-y-auto border-t border-zinc-800 p-1">
                {candidateOptions.length > 0 ? candidateOptions.map((option) => {
                  const badges = modelOptionBadges(option);
                  return (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => addModel(option.value)}
                      className="flex w-full items-start justify-between gap-3 rounded-md px-2.5 py-2 text-left text-zinc-400 transition-colors hover:bg-zinc-900 hover:text-zinc-100"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-medium text-zinc-100">{option.label || option.value}</span>
                        <span className="block truncate text-[11px] text-zinc-500">{[option.provider_id, option.model_id || option.qualified_model_id].filter(Boolean).join(" · ") || "提供元情報なし"}</span>
                      </span>
                      <span className="flex max-w-[170px] flex-wrap justify-end gap-1">
                        {badges.map((badge) => (
                          <span key={badge} className="rounded-full border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400">
                            {badge}
                          </span>
                        ))}
                        <Plus size={13} className="mt-1 shrink-0 text-emerald-300" />
                      </span>
                    </button>
                  );
                }) : (
                  <div className="px-3 py-5 text-xs text-zinc-600">
                    {busy ? "モデルを読み込んでいます..." : "追加できるモデルがありません。"}
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function publicUrlConfig(value: unknown, fallback: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, unknown>;
  if (fallback && typeof fallback === "object" && !Array.isArray(fallback)) return fallback as Record<string, unknown>;
  return {};
}

function providerAccentClass(providerId: string): string {
  switch (providerId) {
    case "cloudflare":
      return "from-orange-500 via-amber-400 to-orange-700";
    case "google":
      return "from-sky-500 via-emerald-400 to-yellow-400";
    case "github":
      return "from-zinc-200 via-zinc-500 to-zinc-800";
    case "codex":
      return "from-violet-500 via-cyan-400 to-emerald-400";
    default:
      return "from-cyan-500 via-sky-500 to-violet-500";
  }
}

function statusBadgeClass(status: string, connected: boolean, canConnect: boolean): string {
  const normalized = status.toLowerCase();
  if (connected || normalized === "connected" || normalized === "configured") {
    return "border-emerald-500/35 bg-emerald-500/10 text-emerald-300";
  }
  if (normalized.includes("approval")) {
    return "border-amber-500/35 bg-amber-500/10 text-amber-200";
  }
  if (normalized.includes("blocked") || normalized.includes("mismatch") || normalized.includes("rejected") || normalized.includes("error")) {
    return "border-rose-500/35 bg-rose-500/10 text-rose-200";
  }
  if (canConnect) {
    return "border-cyan-500/35 bg-cyan-500/10 text-cyan-200";
  }
  return "border-white/[0.09] bg-white/[0.04] text-zinc-400";
}

function capabilityToneClass(tone: "enabled" | "approval" | "rejected" | "scope" | "neutral"): string {
  switch (tone) {
    case "enabled":
      return "border-emerald-500/25 bg-emerald-500/10 text-emerald-200";
    case "approval":
      return "border-amber-500/30 bg-amber-500/10 text-amber-100";
    case "rejected":
      return "border-rose-500/25 bg-rose-500/10 text-rose-200";
    case "scope":
      return "border-sky-500/25 bg-sky-500/10 text-sky-200";
    default:
      return "border-zinc-800 bg-zinc-950 text-zinc-400";
  }
}

function plainRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function plainRecordList(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.map(plainRecord).filter((item) => Object.keys(item).length > 0) : [];
}

function cloudflareProvisioningRows(provisioning: Record<string, unknown>, japanese = false): Array<{ label: string; ready: boolean; value: string }> {
  const environment = plainRecord(provisioning.environment);
  const status = String(provisioning.environment_status || environment.status || "").trim();
  if (!status && Object.keys(environment).length === 0) return [];
  return [
    { label: japanese ? "実行環境" : "Sandbox", ready: Boolean(provisioning.sandbox_ready || environment.sandbox_ready), value: provisioning.sandbox_ready || environment.sandbox_ready ? japanese ? "利用可能" : "Ready" : japanese ? "利用不可" : "Blocked" },
    { label: "Pages", ready: Boolean(provisioning.pages_ready || environment.pages_ready), value: provisioning.pages_ready || environment.pages_ready ? japanese ? "利用可能" : "Ready" : japanese ? "要確認" : "Check" },
    { label: japanese ? "固定トンネル" : "Named tunnel", ready: Boolean(provisioning.stable_pc_tunnel_ready || environment.stable_pc_tunnel_ready), value: provisioning.stable_pc_tunnel_ready || environment.stable_pc_tunnel_ready ? japanese ? "利用可能" : "Ready" : japanese ? "未設定" : "Missing" },
    { label: japanese ? "PC接続" : "PC bridge", ready: Boolean(provisioning.pc_tool_bridge_ready || environment.pc_tool_bridge_ready), value: provisioning.pc_tool_bridge_ready || environment.pc_tool_bridge_ready ? japanese ? "利用可能" : "Ready" : japanese ? "未設定" : "Missing" },
  ];
}

function cloudflareProvisioningFacts(provisioning: Record<string, unknown>, japanese = false): string[] {
  const constraints = plainRecord(provisioning.constraints);
  const facts: string[] = [];
  if (constraints.cloudflare_sandbox_requires_workers_paid) facts.push(japanese ? "実行環境にはWorkers有料プランが必要" : "Sandbox: Workers Paid plan");
  if (constraints.pages_dev_is_not_a_pc_tunnel_hostname) facts.push(japanese ? "pages.devはPC接続先には使えません" : "pages.dev is not a PC tunnel");
  if (constraints.all_tools_cloudflare_native_supported === false) facts.push(japanese ? "Cloudflareだけで使える機能には制限があります" : "Cloudflare-native tools: partial");
  if (constraints.pc_local_tools_require_pc_bridge) facts.push(japanese ? "PC上の機能にはPC接続が必要" : "PC-local tools: PC bridge");
  if (constraints.wrangler_diagnostics_require_explicit_command_or_local_install) facts.push(japanese ? "診断にはWranglerの設定が必要" : "Wrangler: explicit command or local install");
  return facts;
}

function cloudflareProvisioningBlockers(provisioning: Record<string, unknown>, japanese = false): string[] {
  const blockers = plainRecordList(provisioning.blockers);
  return blockers
    .map((item) => japanese
      ? ({
          CLOUDFLARE_WRANGLER_MISSING: "Cloudflareの診断を使うにはWranglerを設定してください。",
          CLOUDFLARE_CONTAINERS_PAID_PLAN_REQUIRED: "Cloudflareの実行環境にはWorkers有料プランが必要です。",
          CLOUDFLARE_PC_TUNNEL_ENV_NOT_CONFIGURED: "PCへ接続する固定トンネルを設定してください。",
        }[String(item.code || "")] ?? "Cloudflareの実行環境を確認してください。")
      : String(item.message || item.code || "").trim())
    .filter(Boolean)
    .slice(0, 3);
}

function compactCredentialRef(value: string): string {
  const text = value.trim();
  if (text.length <= 32) return text;
  return `${text.slice(0, 14)}…${text.slice(-10)}`;
}

function importPlaceholderForProvider(providerId: string, locale: "en" | "ja" = "en"): string {
  if (providerId === "cloudflare") {
    return [
      "{",
      '  "schema": "rumi.connection.credential_bundle.v1",',
      '  "provider_id": "cloudflare",',
      '  "credentials": { "access_token": "..." },',
      '  "scopes": ["account:read", "pages:write"],',
      '  "requested_capabilities": ["cloudflare.pages.project.write"]',
      "}",
    ].join("\n");
  }
  if (providerId === "github") {
    return [
      "{",
      '  "schema": "rumi.connection.credential_bundle.v1",',
      '  "provider_id": "github",',
      '  "credentials": { "access_token": "..." },',
      '  "scopes": ["read:user", "repo"],',
      '  "requested_capabilities": ["github.repo.read"]',
      "}",
    ].join("\n");
  }
  if (providerId === "google") {
    return locale === "ja" ? "Google OAuthクライアントJSONまたは認証情報セットJSONを貼り付けます。トークンは安全に保存され、再表示されません。" : "Paste Google OAuth client JSON or a credential bundle JSON. Token values are stored in SecretsStore and never echoed back.";
  }
  return locale === "ja" ? "認証情報セットJSONまたは環境変数形式のトークンを貼り付けます。秘密情報はTobkiriの秘密情報ストレージだけに保存します。" : "Paste credential bundle JSON or .env-style token lines. Raw secrets are stored only in Rumi secret storage.";
}

function connectionDraftHelp(providerId: string, locale: "en" | "ja" = "en"): string {
  if (locale === "ja") {
    if (providerId === "cloudflare") return "トークンを直接読み込むか、セルフホストのブラウザ接続用OAuthクライアントを設定します。書き込み操作には承認が必要です。";
    if (providerId === "github") return "細かな権限を設定したトークン、またはOAuthトークンを読み込みます。Rumiが利用できる操作は、選んだ権限の範囲に限られます。";
    if (providerId === "google") return "ブラウザ接続で使う権限を選ぶか、セルフホスト用クライアントJSONを貼り付けます。Gmail本文など影響の大きい権限は接続前に表示します。";
    return "ここから読み込むと、秘密情報そのものを設定画面に残さず安全に保存できます。";
  }
  if (providerId === "cloudflare") {
    return "Use credential JSON for direct token import, or OAuth client JSON for self-host browser connect. Pages/Workers write capabilities require approval.";
  }
  if (providerId === "github") {
    return "Import a fine-grained token or OAuth token. Requested capabilities limit what Rumi can actually use.";
  }
  if (providerId === "google") {
    return "Choose a scope mode for browser OAuth, or paste self-host client JSON. Restricted Gmail modes are labeled before connect.";
  }
  return "Do not paste secrets into .env as the primary path. Import here so Rumi can store a credential_ref and keep raw values out of Settings.";
}

function ProviderOAuthPanel({
  sectionId,
  fieldId,
  providers,
  onRefresh,
}: {
  sectionId: string;
  fieldId: string;
  providers: Array<Record<string, unknown>>;
  onRefresh: (sectionId: string, fieldId: string, value: unknown) => void;
}) {
  const [clientDrafts, setClientDrafts] = useState<Record<string, string>>({});
  const [busyAction, setBusyAction] = useState("");
  const [messages, setMessages] = useState<Record<string, { tone: "success" | "error"; text: string }>>({});
  const [oauthReviews, setOauthReviews] = useState<Record<string, PendingOAuthReview>>({});
  const [draftReviews, setDraftReviews] = useState<Record<string, CredentialImportReview>>({});
  const oauthProviders = oauthProviderRows(providers);

  if (!oauthProviders.length) {
    return null;
  }

  const refresh = (providerId: string) => onRefresh(sectionId, fieldId, { action: "oauth_refresh", provider_id: providerId });

  const beginOAuthReview = async (providerId: string) => {
    let popup: Window | null = null;
    try {
      popup = window.open("", `rumi-oauth-${providerId}`, "popup=yes,width=560,height=760");
      setBusyAction(`${providerId}:start`);
      const result = await settingsApiResources.startProviderOAuth(
        providerId,
        providerId === "google" ? { scopeMode: "google_ai", services: ["identity", "generative_language"] } : undefined,
      );
      const destination = reviewOAuthDestination(providerId, result.authorize_url);
      setOauthReviews((current) => ({ ...current, [providerId]: { ...destination, popup, scopes: result.scopes ?? [] } }));
      setMessages((current) => ({ ...current, [providerId]: { tone: "success", text: "Review the provider destination and permissions before opening OAuth." } }));
    } catch {
      if (popup && !popup.closed) popup.close();
      setMessages((current) => ({ ...current, [providerId]: { tone: "error", text: "OAuth could not be started because the destination was not approved." } }));
    } finally {
      setBusyAction("");
    }
  };

  const confirmOAuthReview = (providerId: string) => {
    const review = oauthReviews[providerId];
    if (!review) return;
    const popup = review.popup && !review.popup.closed
      ? review.popup
      : window.open("", `rumi-oauth-${providerId}`, "popup=yes,width=560,height=760");
    if (!popup) {
      setMessages((current) => ({ ...current, [providerId]: { tone: "error", text: "Popup was blocked. Your Settings draft was preserved; allow a popup and retry." } }));
      return;
    }
    popup.location.replace(review.authorizeUrl);
    popup.focus();
    setOauthReviews((current) => {
      const next = { ...current };
      delete next[providerId];
      return next;
    });
    setMessages((current) => ({ ...current, [providerId]: { tone: "success", text: "Authorization page opened. Connection is not complete until the provider callback is verified." } }));
  };

  const cancelOAuthReview = (providerId: string) => {
    const review = oauthReviews[providerId];
    if (review?.popup && !review.popup.closed) review.popup.close();
    setOauthReviews((current) => {
      const next = { ...current };
      delete next[providerId];
      return next;
    });
  };

  return (
    <div className="space-y-3">
      {oauthProviders.map((provider) => {
        const providerId = String(provider.provider_id ?? "");
        const oauth = provider.oauth as Record<string, unknown>;
        const connected = Boolean(oauth.connected);
        const clientConfigured = Boolean(oauth.client_configured);
        const connectEnabled = Boolean(oauth.connect_enabled);
        const clientCanClear = oauth.client_can_clear !== false;
        const expiresAt = String(oauth.expires_at ?? "");
        const hint = String(oauth.config_hint ?? "");
        const scopes = Array.isArray(oauth.scopes) ? oauth.scopes.map((scope) => String(scope)).filter(Boolean) : [];
        const capabilities = Array.isArray(oauth.capabilities) ? oauth.capabilities.map((capability) => String(capability)).filter(Boolean) : [];
        const approvalRequiredCapabilities = Array.isArray(oauth.approval_required_capabilities)
          ? oauth.approval_required_capabilities.map((capability) => String(capability)).filter(Boolean)
          : [];
        const rejectedCapabilities = Array.isArray(oauth.rejected_capabilities)
          ? oauth.rejected_capabilities.map((capability) => String(capability)).filter(Boolean)
          : [];
        const credentialRef = oauth.credential_ref && typeof oauth.credential_ref === "object" && !Array.isArray(oauth.credential_ref)
          ? oauth.credential_ref as Record<string, unknown>
          : {};
        const credentialRefId = String(credentialRef.credential_id ?? "");
        const draft = clientDrafts[providerId] ?? "";
        const oauthReview = oauthReviews[providerId];
        const draftReview = draftReviews[providerId];
        const isBusy = busyAction.startsWith(`${providerId}:`) || Boolean(oauthReview);
        const banner = messages[providerId];
        const oauthSurfaceLabel = providerId === "google" ? "Google AI browser login" : `${providerId} browser login`;
        const stateLabel = connected ? "Connected" : String(oauth.status_label ?? "") || (connectEnabled ? "Ready to connect" : "Client config needed");
        const stateTone = connected
          ? "border-emerald-800 bg-emerald-950/20 text-emerald-300"
          : connectEnabled
            ? "border-cyan-800 bg-cyan-950/20 text-cyan-300"
            : "border-zinc-800 bg-zinc-950 text-zinc-400";
        const clientPlaceholder = providerId === "cloudflare"
          ? "Paste Cloudflare OAuth client JSON, credential JSON, or .env token lines"
          : "Paste OAuth client JSON, credential JSON, or .env token lines";

        return (
          <div key={providerId} className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h4 className="text-sm font-medium text-zinc-100">{oauthSurfaceLabel}</h4>
                  <span className={cn("rounded-full border px-2 py-0.5 text-[11px]", stateTone)}>
                    {stateLabel}
                  </span>
                </div>
                <p className="mt-1 text-xs leading-5 text-zinc-500">
                  {connected
                    ? "Connected with a stored credential reference."
                    : hint}
                </p>
                {credentialRefId && (
                  <p className="mt-1 text-[11px] text-zinc-600">Credential ref: {credentialRefId}</p>
                )}
                {expiresAt && (
                  <p className="mt-1 text-[11px] text-zinc-600">Access token expires at: {expiresAt}</p>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={isBusy || !connectEnabled}
                  title={connectEnabled ? undefined : String(oauth.disabled_reason ?? hint)}
                  onClick={() => void beginOAuthReview(providerId)}
                  className={cn(
                    "rounded-lg border px-3 py-2 text-xs transition-colors",
                    isBusy || !connectEnabled
                      ? "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600"
                      : "border-cyan-700 bg-cyan-950/30 text-cyan-100 hover:border-cyan-500 hover:bg-cyan-900/35",
                  )}
                >
                  {isBusy && busyAction === `${providerId}:start` ? "Opening..." : connected ? "Reconnect in browser" : "Connect in browser"}
                </button>
                <button
                  type="button"
                  disabled={isBusy || !connected}
                  onClick={async () => {
                    try {
                      setBusyAction(`${providerId}:disconnect`);
                      await settingsApiResources.disconnectProviderOAuth(providerId);
                      refresh(providerId);
                      setMessages((current) => ({
                        ...current,
                        [providerId]: { tone: "success", text: "Browser login disconnected." },
                      }));
                    } catch (errorValue) {
                      setMessages((current) => ({
                        ...current,
                        [providerId]: {
                          tone: "error",
                          text: errorValue instanceof Error ? errorValue.message : "Failed to disconnect browser login.",
                        },
                      }));
                    } finally {
                      setBusyAction("");
                    }
                  }}
                  className={cn(
                    "rounded-lg border px-3 py-2 text-xs transition-colors",
                    isBusy || !connected
                      ? "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600"
                      : "border-white/[0.09] bg-white/[0.04] text-zinc-200 hover:border-zinc-500",
                  )}
                >
                  Disconnect
                </button>
                <button
                  type="button"
                  disabled={isBusy || !clientConfigured || !clientCanClear}
                  onClick={async () => {
                    try {
                      setBusyAction(`${providerId}:clear`);
                      await settingsApiResources.clearProviderOAuthClientConfig(providerId);
                      setClientDrafts((current) => ({ ...current, [providerId]: "" }));
                      refresh(providerId);
                      setMessages((current) => ({
                        ...current,
                        [providerId]: { tone: "success", text: "Saved OAuth client config cleared." },
                      }));
                    } catch (errorValue) {
                      setMessages((current) => ({
                        ...current,
                        [providerId]: {
                          tone: "error",
                          text: errorValue instanceof Error ? errorValue.message : "Failed to clear OAuth client config.",
                        },
                      }));
                    } finally {
                      setBusyAction("");
                    }
                  }}
                  className={cn(
                    "rounded-lg border px-3 py-2 text-xs transition-colors",
                    isBusy || !clientConfigured || !clientCanClear
                      ? "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600"
                      : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:text-zinc-200",
                  )}
                >
                  Clear client
                </button>
              </div>
            </div>
            <div className="mt-4 grid gap-3 lg:grid-cols-[minmax(0,1.4fr)_auto]">
              <textarea
                value={draft}
                onChange={(event) => {
                  const nextValue = event.target.value;
                  setClientDrafts((current) => ({ ...current, [providerId]: nextValue }));
                  setDraftReviews((current) => {
                    const next = { ...current };
                    delete next[providerId];
                    return next;
                  });
                  setMessages((current) => {
                    if (!(providerId in current)) return current;
                    const next = { ...current };
                    delete next[providerId];
                    return next;
                  });
                }}
                placeholder={clientPlaceholder}
                className="min-h-28 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none focus:border-cyan-500"
              />
              <div className="flex flex-col justify-between gap-3">
                <button
                  type="button"
                  disabled={isBusy || !draft.trim()}
                  onClick={() => {
                    try {
                      setDraftReviews((current) => ({ ...current, [providerId]: reviewConnectionDraft(draft) }));
                    } catch {
                      setMessages((current) => ({ ...current, [providerId]: { tone: "error", text: "Credential data must be valid, reviewable JSON before it can be saved." } }));
                    }
                  }}
                  className={cn(
                    "rounded-lg border px-3 py-2 text-sm transition-colors",
                    isBusy || !draft.trim()
                      ? "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600"
                      : "border-zinc-100 bg-zinc-100 text-zinc-950",
                  )}
                >
                  {isBusy && busyAction === `${providerId}:save` ? "Saving..." : "Import credential JSON / save client"}
                </button>
                {scopes.length > 0 && (
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-[11px] text-zinc-500">
                    Scopes: {scopes.join(", ")}
                  </div>
                )}
                {capabilities.length > 0 && (
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-[11px] text-zinc-500">
                    Capabilities: {capabilities.join(", ")}
                  </div>
                )}
                {approvalRequiredCapabilities.length > 0 && (
                  <div className="rounded-lg border border-amber-800 bg-amber-950/20 px-3 py-2 text-[11px] text-amber-100/80">
                    Approval required: {approvalRequiredCapabilities.join(", ")}
                  </div>
                )}
                {rejectedCapabilities.length > 0 && (
                  <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-[11px] text-zinc-500">
                    Not granted: {rejectedCapabilities.join(", ")}
                  </div>
                )}
              </div>
            </div>
            {oauthReview && (
              <div className="mt-3 rounded-lg border border-amber-500/35 bg-amber-500/10 p-3 text-xs text-amber-50" role="status">
                <div className="font-medium">Review external authorization</div>
                <p className="mt-1 break-all text-amber-100/80">{oauthReview.host}{oauthReview.path}</p>
                <p className="mt-1 text-amber-100/75">The provider may let you choose an account. This only opens the provider page; connection is verified after its callback.</p>
                {oauthReview.scopes.length > 0 && <p className="mt-1 text-amber-100/75">Requested scopes: {oauthReview.scopes.join(", ")}</p>}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" onClick={() => confirmOAuthReview(providerId)} className="rounded border border-amber-300 bg-amber-100 px-2.5 py-1.5 text-xs font-medium text-zinc-950">Open reviewed provider page</button>
                  <button type="button" onClick={() => cancelOAuthReview(providerId)} className="rounded border border-amber-300/40 px-2.5 py-1.5 text-xs text-amber-50">Cancel</button>
                </div>
              </div>
            )}
            {draftReview && (
              <div className="mt-3 rounded-lg border border-violet-500/35 bg-violet-500/10 p-3 text-xs text-violet-100" role="status">
                <div className="font-medium">Review before saving</div>
                <p className="mt-1 text-violet-100/75">{draftReview.kind === "connection_import" ? "Credential import" : "OAuth client configuration"}; {draftReview.secretFieldCount} secret field(s) detected and redacted from this review.</p>
                {draftReview.fields.length > 0 && <p className="mt-1 text-violet-100/75">Non-secret fields: {draftReview.fields.join(", ")}</p>}
                {draftReview.endpoints.length > 0 && <p className="mt-1 text-violet-100/75">HTTPS endpoints: {draftReview.endpoints.join(", ")}</p>}
                {draftReview.scopes.length > 0 && <p className="mt-1 text-violet-100/75">Requested scopes: {draftReview.scopes.join(", ")}</p>}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" onClick={async () => {
                    try {
                      setBusyAction(`${providerId}:save`);
                      if (draftReview.kind === "connection_import") await settingsApiResources.importProviderConnection(providerId, draft);
                      else await settingsApiResources.saveProviderOAuthClientConfig(providerId, draft);
                      setClientDrafts((current) => ({ ...current, [providerId]: "" }));
                      setDraftReviews((current) => { const next = { ...current }; delete next[providerId]; return next; });
                      refresh(providerId);
                      setMessages((current) => ({ ...current, [providerId]: { tone: "success", text: "Saved. Status will be refreshed from the local authority." } }));
                    } catch {
                      setMessages((current) => ({ ...current, [providerId]: { tone: "error", text: "The credential was not confirmed as saved. Review the local status and retry if needed." } }));
                    } finally { setBusyAction(""); }
                  }} className="rounded border border-violet-300 bg-violet-100 px-2.5 py-1.5 text-xs font-medium text-zinc-950">Confirm and save</button>
                  <button type="button" onClick={() => setDraftReviews((current) => { const next = { ...current }; delete next[providerId]; return next; })} className="rounded border border-violet-300/40 px-2.5 py-1.5 text-xs text-violet-100">Keep editing</button>
                </div>
              </div>
            )}
            {banner && (
              <p className={cn("mt-3 text-[11px]", banner.tone === "success" ? "text-emerald-400" : "text-rose-300")}>
                {banner.text}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

function PublicUrlField({
  sectionId,
  field,
  value,
  onChange,
}: {
  sectionId: string;
  field: SettingsSection["fields"][number];
  value: unknown;
  onChange: (sectionId: string, fieldId: string, value: unknown) => void;
}) {
  const config = publicUrlConfig(value, field.default);
  const [providerId, setProviderId] = useState(String(config.provider_id ?? "cloudflare_quick_tunnel"));
  const [localUrl, setLocalUrl] = useState(String(config.local_url ?? "http://127.0.0.1:8766"));
  const [routePath, setRoutePath] = useState(String(config.route_path ?? settingsApiResources.canonicalRouteKey("api/integrations/line/webhook")));
  const [result, setResult] = useState<Record<string, unknown> | null>(
    config.result && typeof config.result === "object" ? config.result as Record<string, unknown> : null,
  );
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const next = publicUrlConfig(value, field.default);
    setProviderId(String(next.provider_id ?? "cloudflare_quick_tunnel"));
    setLocalUrl(String(next.local_url ?? "http://127.0.0.1:8766"));
    setRoutePath(String(next.route_path ?? settingsApiResources.canonicalRouteKey("api/integrations/line/webhook")));
    setResult(next.result && typeof next.result === "object" ? next.result as Record<string, unknown> : null);
  }, [field.default, value]);

  const routeOptions = [
    { value: settingsApiResources.canonicalRouteKey("api/integrations/line/webhook"), label: "LINE webhook" },
    { value: settingsApiResources.canonicalRouteKey("api/integrations/discord/interactions"), label: "Discord interactions" },
    { value: settingsApiResources.canonicalRouteKey("api/integrations/discord/events"), label: "Discord events" },
    { value: settingsApiResources.canonicalRouteKey("api/integrations/slack/events"), label: "Slack events" },
    { value: settingsApiResources.canonicalRouteKey("api/webhooks/inbound/{webhook_id}"), label: "Generic webhook" },
  ];
  const providerOptions = [
    { value: "cloudflare_quick_tunnel", label: "Cloudflare Quick Tunnel" },
    { value: "static", label: "Static URL" },
  ];
  const publicUrl = String(result?.public_url ?? "");
  const error = String(result?.error ?? "");

  const persist = (nextResult: Record<string, unknown> | null) => {
    onChange(sectionId, field.id, {
      provider_id: providerId,
      local_url: localUrl,
      route_path: routePath,
      result: nextResult,
    });
  };

  const createUrl = async () => {
    setBusy(true);
    setCopied(false);
    try {
      const next = await settingsApiResources.createPublicUrl({
        provider_id: providerId,
        local_url: localUrl,
        route_path: routePath,
      });
      setResult(next);
      persist(next);
    } catch (errorValue) {
      const next = { ok: false, error: errorValue instanceof Error ? errorValue.message : "Failed to create URL" };
      setResult(next);
      persist(next);
    } finally {
      setBusy(false);
    }
  };

  const closeUrl = async () => {
    const urlId = String(result?.url_id ?? "");
    if (urlId && urlId !== "static") {
      await settingsApiResources.closePublicUrl(urlId).catch(console.error);
    }
    setResult(null);
    persist(null);
  };

  return (
    <div className="space-y-3">
      <div className="grid gap-3 md:grid-cols-[1.1fr_1.1fr_1.2fr]">
        <label className="space-y-1.5">
          <span className="text-[11px] font-medium uppercase text-zinc-500">URL provider</span>
          <CustomSelect value={providerId} onChange={setProviderId} options={providerOptions} />
        </label>
        <label className="space-y-1.5">
          <span className="text-[11px] font-medium uppercase text-zinc-500">Local Rumi URL</span>
          <input
            value={localUrl}
            onChange={(event) => setLocalUrl(event.target.value)}
            className="w-full rounded-lg border border-white/[0.09] bg-white/[0.04] px-3 py-2 text-sm text-zinc-100 outline-none focus:border-cyan-500"
            placeholder="http://127.0.0.1:8766"
          />
        </label>
        <label className="space-y-1.5">
          <span className="text-[11px] font-medium uppercase text-zinc-500">Webhook route</span>
          <CustomSelect value={routePath} onChange={setRoutePath} options={routeOptions} />
        </label>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={createUrl}
          disabled={busy || !localUrl.trim() || !routePath.trim()}
          className={cn(
            "inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors",
            busy || !localUrl.trim() || !routePath.trim()
              ? "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600"
              : "border-cyan-700 bg-cyan-950/35 text-cyan-100 hover:border-cyan-500 hover:bg-cyan-900/35",
          )}
        >
          {busy ? <Loader2 size={15} className="animate-spin" /> : <span className="h-2 w-2 rounded-full bg-cyan-300" />}
          {providerId === "cloudflare_quick_tunnel" ? "Cloudflare URLを発行" : "Webhook URLを作成"}
        </button>
        {publicUrl && (
          <button
            type="button"
            onClick={() => {
              void copyTextToClipboard(publicUrl).then(() => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1600);
              });
            }}
            className="inline-flex items-center gap-2 rounded-lg border border-white/[0.09] bg-white/[0.04] px-3 py-2 text-sm text-zinc-200 hover:border-zinc-500"
          >
            <Copy size={14} />
            {copied ? "コピー済み" : "Webhook URLをコピー"}
          </button>
        )}
        {result && (
          <button
            type="button"
            onClick={() => void closeUrl()}
            className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-500 hover:text-zinc-200"
          >
            Clear / Close
          </button>
        )}
      </div>
      {publicUrl && (
        <div className="rounded-lg border border-emerald-800/60 bg-emerald-950/20 px-3 py-2 font-mono text-xs text-emerald-200 break-all">
          {publicUrl}
        </div>
      )}
      {!publicUrl && error && (
        <div className="rounded-lg border border-amber-800/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-200">
          {error}
        </div>
      )}
    </div>
  );
}

const BUILTIN_API_PROVIDER_IDS: string[] = [
  "anthropic",
  "cerebras",
  "deepseek",
  "gitlawb-opengateway",
  "glm",
  "google",
  "groq",
  "llama_cpp",
  "lmstudio",
  "longcat",
  "mistral",
  "moonshotai",
  "nvidia",
  "ollama",
  "opencode-go",
  "opencode-zen",
  "openai",
  "openai_compatible",
  "openrouter",
  "perplexity",
  "together",
  "vllm",
  "xai",
  "xiaomi-token-plan-ams",
  "xiaomi-token-plan-cn",
  "xiaomi-token-plan-sgp",
];

const BUILTIN_EXTERNAL_PROVIDER_IDS: string[] = [
  "cloudflare",
  "codex",
  "discord",
  "generic",
  "github",
  "line",
  "slack",
  "web",
];

type ApiProviderOption = {
  provider_id: string;
  label: string;
  kind: "llm" | "custom";
  builtin: boolean;
};

function normalizeProviderKind(value: unknown): "llm" | "custom" {
  return String(value ?? "").trim().toLowerCase() === "custom" ? "custom" : "llm";
}

function collectApiProviderOptions(providers: Array<Record<string, unknown>>): ApiProviderOption[] {
  const options = new Map<string, ApiProviderOption>();
  for (const builtinId of BUILTIN_API_PROVIDER_IDS) {
    options.set(builtinId, { provider_id: builtinId, label: builtinId, kind: "llm", builtin: true });
  }
  for (const builtinId of BUILTIN_EXTERNAL_PROVIDER_IDS) {
    options.set(builtinId, { provider_id: builtinId, label: builtinId, kind: "custom", builtin: true });
  }
  for (const provider of providers) {
    const providerId = String(provider.provider_id ?? "").trim();
    if (!providerId) continue;
    const builtin = Boolean(provider.builtin) || BUILTIN_API_PROVIDER_IDS.includes(providerId) || BUILTIN_EXTERNAL_PROVIDER_IDS.includes(providerId);
    const kind = provider.kind == null && BUILTIN_EXTERNAL_PROVIDER_IDS.includes(providerId)
      ? "custom"
      : normalizeProviderKind(provider.kind);
    const label = String(provider.label ?? providerId);
    options.set(providerId, { provider_id: providerId, label, kind, builtin });
  }
  return Array.from(options.values()).sort((a, b) => {
    if (a.builtin !== b.builtin) return a.builtin ? -1 : 1;
    return a.provider_id.localeCompare(b.provider_id);
  });
}

function preferredApiProviderId(value: unknown): string {
  const providers = apiProviderRows(value);
  const configuredProviderIds = registeredApiRows(providers)
    .map((api) => String(api.provider_id ?? "").trim())
    .filter(Boolean);
  return configuredProviderIds.find((providerId) => providerId === "openrouter")
    ?? configuredProviderIds[0]
    ?? "openrouter";
}

function collectExternalProviderOptions(providers: Array<Record<string, unknown>>): ApiProviderOption[] {
  const options = new Map<string, ApiProviderOption>();
  for (const providerId of BUILTIN_EXTERNAL_PROVIDER_IDS) {
    options.set(providerId, { provider_id: providerId, label: providerId, kind: "custom", builtin: true });
  }
  for (const provider of providers) {
    const providerId = String(provider.provider_id ?? "").trim();
    if (!providerId) continue;
    options.set(providerId, {
      provider_id: providerId,
      label: String(provider.label ?? providerId),
      kind: "custom",
      builtin: BUILTIN_EXTERNAL_PROVIDER_IDS.includes(providerId),
    });
  }
  return Array.from(options.values()).sort((a, b) => {
    if (a.builtin !== b.builtin) return a.builtin ? -1 : 1;
    return a.provider_id.localeCompare(b.provider_id);
  });
}

function SearchableProviderSelect({
  value,
  options,
  onChange,
  onAddCustom,
  placeholder = "provider を検索",
  className,
  addCustomLabel = "Add custom provider...",
  showKindControls = true,
  showProviderBadges = true,
}: {
  value: string;
  options: ApiProviderOption[];
  onChange: (next: string) => void;
  onAddCustom?: (option: { providerId: string; label: string; kind: "llm" | "custom" }) => void;
  placeholder?: string;
  className?: string;
  addCustomLabel?: string;
  showKindControls?: boolean;
  showProviderBadges?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [draftId, setDraftId] = useState("");
  const [draftKind, setDraftKind] = useState<"llm" | "custom">("custom");
  const trimmedQuery = query.trim().toLowerCase();
  const selected = options.find((option) => option.provider_id === value) ?? null;
  const filtered = useMemo(() => {
    if (!trimmedQuery) return options;
    return options.filter((option) =>
      option.provider_id.toLowerCase().includes(trimmedQuery)
      || option.label.toLowerCase().includes(trimmedQuery),
    );
  }, [options, trimmedQuery]);

  const closeAll = () => {
    setOpen(false);
    setCreating(false);
    setDraftId("");
    setQuery("");
  };

  const submitDraft = () => {
    const cleaned = draftId.trim().toLowerCase().replace(/[^a-z0-9_.-]+/g, "_").replace(/^[-_.]+|[-_.]+$/g, "");
    if (!cleaned) return;
    if (onAddCustom) {
      onAddCustom({ providerId: cleaned, label: cleaned, kind: draftKind });
    }
    onChange(cleaned);
    closeAll();
  };

  const selectedLabel = selected?.label ?? value;

  return (
    <div className={cn("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        title={selectedLabel || undefined}
        className="flex min-h-10 w-full items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-left text-sm text-zinc-200 outline-none transition-colors hover:border-zinc-700"
      >
        <span className="min-w-0 flex-1">
          {selected ? (
            <span className="flex min-w-0 flex-wrap items-center gap-1.5">
              <span className="min-w-0 break-all leading-5">{selected.label}</span>
              {showProviderBadges && !selected.builtin && (
                <span className="rounded-full border border-zinc-700 px-1.5 text-[9px] uppercase text-zinc-400">
                  {selected.kind === "custom" ? "non-llm" : "custom"}
                </span>
              )}
            </span>
          ) : (
            value || "provider を選択"
          )}
        </span>
        <ChevronDown size={14} className={cn("shrink-0 text-zinc-500 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <>
          <button type="button" aria-label="close provider select" className="fixed inset-0 rumi-layer-panel cursor-default" onClick={closeAll} />
          <div className="absolute left-0 top-[calc(100%+6px)] rumi-layer-local-popover w-[min(520px,calc(100vw-32px))] max-w-[calc(100vw-32px)] overflow-hidden rumi-popover">
            <label className="m-2 flex h-9 items-center gap-2 rounded-lg border border-zinc-800 bg-black/30 px-3 text-xs text-zinc-500 focus-within:border-zinc-600 focus-within:text-zinc-300">
              <Search size={14} />
              <input
                autoFocus
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={placeholder}
                className="min-w-0 flex-1 bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
              />
              {query && (
                <button
                  type="button"
                  onClick={() => setQuery("")}
                  className="rounded p-0.5 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300"
                  aria-label="clear provider search"
                >
                  <X size={13} />
                </button>
              )}
            </label>
            <div className="max-h-64 overflow-y-auto border-t border-zinc-800 p-1">
              {filtered.length > 0 ? filtered.map((option) => {
                const active = option.provider_id === value;
                return (
                  <button
                    key={option.provider_id}
                    type="button"
                    title={option.provider_id === option.label ? option.label : `${option.label} (${option.provider_id})`}
                    onClick={() => {
                      onChange(option.provider_id);
                      closeAll();
                    }}
                    className={cn(
                      "flex w-full items-start justify-between gap-2 rounded-md px-2.5 py-2 text-left text-sm transition-colors",
                      active ? "bg-zinc-800 text-zinc-100" : "text-zinc-300 hover:bg-zinc-900",
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block whitespace-normal break-all leading-5">{option.label}</span>
                      {option.provider_id !== option.label && (
                        <span className="block whitespace-normal break-all text-[11px] leading-4 text-zinc-500">{option.provider_id}</span>
                      )}
                      {showProviderBadges && !option.builtin && option.kind === "custom" && (
                        <span className="mt-1 inline-flex rounded-full border border-zinc-700 px-1.5 text-[9px] uppercase text-zinc-400">non-llm</span>
                      )}
                      {showProviderBadges && !option.builtin && option.kind === "llm" && (
                        <span className="mt-1 inline-flex rounded-full border border-zinc-700 px-1.5 text-[9px] uppercase text-zinc-400">custom</span>
                      )}
                    </span>
                    {active && <Check size={13} className="mt-1 shrink-0 text-emerald-300" />}
                  </button>
                );
              }) : (
                <div className="px-3 py-3 text-xs text-zinc-600">一致する provider がありません。</div>
              )}
            </div>
            {creating ? (
              <div className="space-y-2 border-t border-zinc-800 bg-zinc-950/80 p-3">
                <input
                  autoFocus
                  value={draftId}
                  onChange={(event) => setDraftId(event.target.value)}
                  placeholder="provider id (例: tavily, searchapi)"
                  className="w-full rounded-md border border-white/[0.09] bg-white/[0.04] px-2.5 py-1.5 text-sm text-zinc-200 outline-none"
                  onKeyDown={(event) => {
                    if (event.key === "Enter") {
                      event.preventDefault();
                      submitDraft();
                    }
                  }}
                />
                {showKindControls && (
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="text-zinc-500">種類:</span>
                    {(["llm", "custom"] as const).map((option) => (
                      <button
                        key={option}
                        type="button"
                        onClick={() => setDraftKind(option)}
                        className={cn(
                          "rounded-full border px-2.5 py-1 transition-colors",
                          draftKind === option
                            ? "border-emerald-500/60 bg-emerald-500/15 text-emerald-200"
                            : "border-zinc-700 text-zinc-400 hover:text-zinc-200",
                        )}
                      >
                        {option === "llm" ? "LLM" : "Non-LLM (search等)"}
                      </button>
                    ))}
                  </div>
                )}
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => {
                      setCreating(false);
                      setDraftId("");
                    }}
                    className="rounded-md border border-zinc-800 px-2.5 py-1 text-xs text-zinc-400 hover:text-zinc-200"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={submitDraft}
                    disabled={!draftId.trim()}
                    className={cn(
                      "rounded-md border px-2.5 py-1 text-xs",
                      draftId.trim()
                        ? "border-zinc-100 bg-zinc-100 text-zinc-950"
                        : "border-zinc-800 bg-zinc-900 text-zinc-600",
                    )}
                  >
                    Add
                  </button>
                </div>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="flex w-full items-center gap-2 border-t border-zinc-800 px-3 py-2 text-left text-xs text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
              >
                <span className="text-base leading-none">+</span>
                {addCustomLabel}
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function ApiQuickAddForm({
  providerOptions,
  defaultProviderId,
  onSubmit,
  onCancel,
  className,
}: {
  providerOptions: ApiProviderOption[];
  defaultProviderId?: string;
  onSubmit: (payload: { provider_id: string; name: string; value: string; kind: "llm" | "custom" }) => void | Promise<void>;
  onCancel?: () => void;
  className?: string;
}) {
  const initialProvider = defaultProviderId
    || providerOptions.find((option) => option.builtin)?.provider_id
    || providerOptions[0]?.provider_id
    || "";
  const [providerId, setProviderId] = useState(initialProvider);
  const [name, setName] = useState("main");
  const [secret, setSecret] = useState("");
  const selected = providerOptions.find((option) => option.provider_id === providerId);
  const kind: "llm" | "custom" = selected?.kind ?? "llm";

  const ready = providerId.trim() && name.trim() && secret.trim();
  return (
    <div className={cn("space-y-2", className)}>
      <div className="grid gap-2 md:grid-cols-[180px_minmax(120px,1fr)_minmax(180px,2fr)_auto]">
        <SearchableProviderSelect
          value={providerId}
          options={providerOptions}
          onChange={setProviderId}
          onAddCustom={(option) => {
            // The parent will register the custom provider on save.
            setProviderId(option.providerId);
          }}
        />
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="名前 (例: main, work)"
          className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
        />
        <input
          type="password"
          autoComplete="off"
          value={secret}
          onChange={(event) => setSecret(event.target.value)}
          placeholder={`${providerId || "provider"} API key`}
          className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
        />
        <div className="flex items-center gap-1.5">
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="rounded-lg border border-zinc-800 px-2.5 py-2 text-xs text-zinc-400 hover:text-zinc-200"
            >
              Cancel
            </button>
          )}
          <button
            type="button"
            disabled={!ready}
            onClick={() => {
              if (!ready) return;
              void onSubmit({
                provider_id: providerId.trim(),
                name: name.trim(),
                value: secret,
                kind,
              });
              setSecret("");
            }}
            className={cn(
              "rounded-lg border px-3 py-2 text-xs",
              ready
                ? "border-zinc-100 bg-zinc-100 text-zinc-950"
                : "border-zinc-800 bg-zinc-900 text-zinc-600 cursor-not-allowed",
            )}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  );
}

function DeviceLockField({ field }: { field: SettingsSection["fields"][number] }) {
  const record = fieldRecord(field);
  const deviceKind = String(record.device_kind ?? "videoinput");
  const lockMessage = String(record.lock_message ?? "デバイスが見つかりません。接続してから再読み込みしてください。");
  const availableMessage = String(record.available_message ?? "デバイスを検出しました。");
  const checkingMessage = String(record.checking_message ?? "デバイスを確認しています。");
  const [state, setState] = useState<"checking" | "available" | "missing" | "unavailable">("checking");

  useEffect(() => {
    let cancelled = false;
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.enumerateDevices) {
      setState("unavailable");
      return;
    }
    navigator.mediaDevices.enumerateDevices()
      .then((devices) => {
        if (cancelled) return;
        setState(devices.some((device) => device.kind === deviceKind) ? "available" : "missing");
      })
      .catch(() => {
        if (!cancelled) setState("unavailable");
      });
    return () => {
      cancelled = true;
    };
  }, [deviceKind]);

  const blocked = state === "missing" || state === "unavailable";
  const message = state === "checking"
    ? checkingMessage
    : blocked
      ? lockMessage
      : availableMessage;

  return (
    <div
      data-settings-renderer="device_lock"
      data-device-state={state}
      className={cn(
        "flex items-start gap-3 rounded-lg border px-3 py-2.5 text-sm",
        blocked
          ? "border-red-500/30 bg-red-500/10 text-red-100"
          : state === "checking"
            ? "border-white/[0.09] bg-white/[0.04]/60 text-zinc-300"
            : "border-emerald-500/30 bg-emerald-500/10 text-emerald-100",
      )}
    >
      {state === "checking" ? (
        <Loader2 size={15} className="mt-0.5 shrink-0 animate-spin" />
      ) : blocked ? (
        <AlertTriangle size={15} className="mt-0.5 shrink-0" />
      ) : (
        <Check size={15} className="mt-0.5 shrink-0" />
      )}
      <span className="min-w-0 flex-1 leading-6">{message}</span>
    </div>
  );
}

function SettingsField({
  sectionId,
  field,
  value,
  sectionValues,
  onChange,
}: {
  sectionId: string;
  field: SettingsSection["fields"][number];
  value: unknown;
  sectionValues?: Record<string, unknown>;
  onChange: (sectionId: string, fieldId: string, value: unknown) => void;
}) {
  const [secretDraft, setSecretDraft] = useState("");
  const [secretState, setSecretState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [secretError, setSecretError] = useState("");
  const [apiProvider, setApiProvider] = useState(() => preferredApiProviderId(value));
  const [apiName, setApiName] = useState("main");
  const [apiSecret, setApiSecret] = useState("");
  const [apiBaseUrl, setApiBaseUrl] = useState("");
  const [apiAllowedModels, setApiAllowedModels] = useState("");
  const [apiDefaultModel, setApiDefaultModel] = useState("");
  const [apiQuotaLabel, setApiQuotaLabel] = useState("");
  const [apiNotes, setApiNotes] = useState("");
  const [apiSaveState, setApiSaveState] = useState<"idle" | "saving" | "saved">("idle");
  const [apiSaveError, setApiSaveError] = useState("");
  const [apiAvailability, setApiAvailability] = useState<ModelAvailabilityAfterKeySave | null>(null);
  const [apiActionBusyKey, setApiActionBusyKey] = useState("");
  const [apiActionMessage, setApiActionMessage] = useState("");
  const [pendingApiDeleteKey, setPendingApiDeleteKey] = useState("");
  const [credentialTransfer, setCredentialTransfer] = useState<{
    providerId: string;
    providerLabel?: string;
    apiId?: string;
    refreshOnClose?: boolean;
  } | null>(null);
  const [tokenProvider, setTokenProvider] = useState("line");
  const [tokenName, setTokenName] = useState("main");
  const [tokenKind, setTokenKind] = useState("channel_access_token");
  const [tokenSecret, setTokenSecret] = useState("");
  const [tokenSaveState, setTokenSaveState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [tokenSaveError, setTokenSaveError] = useState("");
  const [tokenActionMessage, setTokenActionMessage] = useState("");
  const [tokenBusyKey, setTokenBusyKey] = useState("");
  const [pendingTokenDeleteKey, setPendingTokenDeleteKey] = useState("");
  const [renamingKey, setRenamingKey] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
  const [openApiMenuKey, setOpenApiMenuKey] = useState("");
  const [selectedTokenKey, setSelectedTokenKey] = useState("");
  const [openTokenMenuKey, setOpenTokenMenuKey] = useState("");
  const [routeApiSearchQuery, setRouteApiSearchQuery] = useState("");
  const [routeShowAllProviders, setRouteShowAllProviders] = useState(false);
  const [routeInlineAddOpen, setRouteInlineAddOpen] = useState(false);
  const routeOptions = modelRouteOptions(field);
  const routeOptionKey = routeOptions.map((option) => String(option.value ?? "")).join("|");
  const preferredRouteModel = field.type === "model_api_routes" ? String(sectionValues?.preferred_model ?? "").trim() : "";
  const [routeModel, setRouteModel] = useState(() => preferredRouteModel || String(routeOptions[0]?.value ?? ""));
  const [routeModelTouched, setRouteModelTouched] = useState(false);
  useEffect(() => {
    if (field.type !== "api_keys") return;
    const connectionOptions = collectApiProviderOptions(apiProviderRows(value))
      .filter((option) => option.kind === "custom");
    if (connectionOptions.some((option) => option.provider_id === apiProvider)) return;
    setApiProvider(connectionOptions[0]?.provider_id ?? "");
  }, [apiProvider, field.type, value]);
  useEffect(() => {
    if (field.type !== "model_api_routes") return;
    if (!routeOptions.length) {
      if (routeModel) setRouteModel("");
      return;
    }
    const hasCurrent = routeOptions.some((option) => String(option.value ?? "") === routeModel);
    const hasPreferred = routeOptions.some((option) => String(option.value ?? "") === preferredRouteModel);
    if (!hasCurrent) {
      setRouteModel(hasPreferred ? preferredRouteModel : String(routeOptions[0]?.value ?? ""));
      setRouteModelTouched(false);
      return;
    }
    if (!routeModelTouched && hasPreferred && routeModel !== preferredRouteModel) {
      setRouteModel(preferredRouteModel);
    }
  }, [field.type, preferredRouteModel, routeModel, routeModelTouched, routeOptionKey, routeOptions]);
  const commonLabel = <span className="text-sm text-zinc-300">{field.label}</span>;
  const isSecretConfigured = Boolean(value);
  const refreshSensitiveField = () => onChange(sectionId, field.id, { action: "refresh" });
  const saveSecretValue = async () => {
    const draft = secretDraft;
    const providerId = String(field.provider_id ?? field.id.replace(/_api_key$/, "")).trim();
    if (!providerId || !draft.trim() || secretState === "saving") return;
    setSecretState("saving");
    setSecretError("");
    try {
      await settingsApiResources.saveProviderApiKey(providerId, draft);
      setSecretDraft("");
      setSecretState("saved");
      refreshSensitiveField();
    } catch (errorValue) {
      setSecretState("error");
      setSecretError(errorValue instanceof Error ? errorValue.message : "Credential save failed.");
    }
  };

  let control: ReactElement;
  switch (String(field.type)) {
    case "device_lock":
      control = <DeviceLockField field={field} />;
      break;
    case "model_api_routes": {
      const routeText = String(value ?? "");
      const selectedModel = routeModel || String(routeOptions[0]?.value ?? "");
      const selectedOption = routeOptions.find((option) => String(option.value ?? "") === selectedModel);
      const selectedProvider = routeProviderForOption(selectedOption, selectedModel);
      const isLocalModel = Boolean(selectedOption?.local) || selectedProvider === "stub";
      const providerRows = fieldApiProviderRows(field);
      const providerOptionsForRoutes = collectApiProviderOptions(providerRows)
        .filter((option) => option.kind === "llm");
      const allRegisteredApis = registeredApiRows(providerRows);
      // Hide non-LLM keys from the routes UI (they're not used for chat models).
      const llmRegisteredApis = allRegisteredApis.filter((apiRow) => normalizeProviderKind(apiRow.kind) !== "custom");
      const currentProviderApis = llmRegisteredApis.filter((apiRow) => String(apiRow.provider_id ?? "") === selectedProvider);
      const visibleApis = routeShowAllProviders ? llmRegisteredApis : currentProviderApis;
      const trimmedSearch = routeApiSearchQuery.trim().toLowerCase();
      const filteredApis = trimmedSearch
        ? visibleApis.filter((apiRow) => {
            const haystack = [
              String(apiRow.name ?? ""),
              String(apiRow.api_id ?? ""),
              String(apiRow.provider_id ?? ""),
            ].join(" ").toLowerCase();
            return haystack.includes(trimmedSearch);
          })
        : visibleApis;
      const selectedApis = selectedApisForModel(routeText, selectedModel);
      const hasOtherProviderApis = llmRegisteredApis.some((apiRow) => String(apiRow.provider_id ?? "") !== selectedProvider);
      const handleQuickAddApi = async (payload: { provider_id: string; name: string; value: string; kind: "llm" | "custom" }) => {
        await settingsApiResources.saveProviderApiKey(payload.provider_id, payload.value, {
          apiId: payload.name,
          name: payload.name,
          kind: payload.kind,
          defaultModel: selectedModel,
        });
        const routeRef = `${payload.provider_id}/${payload.name}`;
        const nextRouteText = selectedApisForModel(routeText, selectedModel).includes(routeRef)
          ? routeText
          : toggleModelApiRoute(routeText, selectedModel, routeRef);
        onChange(sectionId, field.id, nextRouteText);
        setRouteInlineAddOpen(false);
      };
      control = (
        <div className="space-y-4" data-settings-renderer="model_routing">
          <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(180px,0.42fr)]">
            <label className="space-y-1.5">
              <span className="text-[11px] font-medium text-zinc-500">1. 設定するモデル</span>
              <SettingsModelSearchSelect
                value={selectedModel}
                options={routeOptions.map(modelFieldOptionToOption)}
                placeholder="model/provider/notes で検索"
                selectorSchema={parseModelSelectorSchema(field.selector_schema)}
                onChange={(nextModel) => {
                  setRouteModelTouched(true);
                  setRouteModel(nextModel);
                }}
              />
            </label>
            <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 px-3 py-2">
              <p className="text-[11px] font-medium text-zinc-500">接続プロバイダー</p>
              <p className="mt-1 font-mono text-sm text-zinc-300">{selectedProvider || "unknown"}</p>
            </div>
          </div>

          {!isLocalModel && (
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <span className="text-[11px] font-medium text-zinc-500">2. 使用するAPIキー</span>
                <span className="text-[11px] text-zinc-500">選んだ API key ごとに別 model 扱いになります</span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <label className="flex h-9 min-w-[200px] flex-1 items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-900 px-3 text-xs text-zinc-500 focus-within:border-zinc-600 focus-within:text-zinc-300">
                  <Search size={13} />
                  <input
                    value={routeApiSearchQuery}
                    onChange={(event) => setRouteApiSearchQuery(event.target.value)}
                    placeholder="API key を検索"
                    className="min-w-0 flex-1 bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
                  />
                  {routeApiSearchQuery && (
                    <button
                      type="button"
                      onClick={() => setRouteApiSearchQuery("")}
                      className="rounded p-0.5 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300"
                      aria-label="clear api search"
                    >
                      <X size={12} />
                    </button>
                  )}
                </label>
                {hasOtherProviderApis && (
                  <label className="flex items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-950/70 px-2.5 py-1.5 text-[11px] text-zinc-400">
                    <input
                      type="checkbox"
                      checked={routeShowAllProviders}
                      onChange={(event) => setRouteShowAllProviders(event.target.checked)}
                      className="h-3.5 w-3.5 accent-emerald-400"
                    />
                    別 provider も表示
                  </label>
                )}
                <button
                  type="button"
                  onClick={() => setRouteInlineAddOpen((current) => !current)}
                  className={cn(
                    "inline-flex min-h-11 items-center gap-2 rounded-lg border px-4 py-2 text-sm font-semibold transition-colors",
                    routeInlineAddOpen
                      ? "border-emerald-400/70 bg-emerald-400/20 text-emerald-100"
                      : "border-emerald-500/40 bg-emerald-500/10 text-emerald-100 hover:border-emerald-400/70 hover:bg-emerald-500/15",
                  )}
                  title="新しい API key を追加"
                >
                  <Plus size={16} aria-hidden />
                  API keyを追加
                </button>
              </div>
              {routeInlineAddOpen && (
                <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
                  <ApiQuickAddForm
                    providerOptions={providerOptionsForRoutes}
                    defaultProviderId={selectedProvider}
                    onSubmit={handleQuickAddApi}
                    onCancel={() => setRouteInlineAddOpen(false)}
                  />
                  <p className="mt-2 text-[10px] text-zinc-600">
                    保存後すぐにこのモデルへ割り当てられるようになります。
                  </p>
                </div>
              )}
              {filteredApis.length > 0 ? (
                <div className="space-y-1.5 rounded-lg border border-white/[0.07] bg-white/[0.03] p-2">
                  {filteredApis.map((apiRow) => {
                    const apiProviderId = String(apiRow.provider_id ?? "");
                    const routeRef = apiRefForRoute(apiRow, apiProviderId || selectedProvider);
                    const active = selectedApis.includes(routeRef);
                    const variantNumber = selectedApis.indexOf(routeRef) + 1;
                    const crossProvider = apiProviderId && apiProviderId !== selectedProvider;
                    return (
                      <button
                        key={routeRef}
                        type="button"
                        onClick={() => onChange(sectionId, field.id, toggleModelApiRoute(routeText, selectedModel, routeRef))}
                        className={cn(
                          "flex w-full items-center justify-between gap-3 rounded-md border px-3 py-2 text-left text-xs transition-colors",
                          active
                            ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-100"
                            : "border-zinc-800 bg-zinc-950/60 text-zinc-300 hover:border-zinc-700",
                        )}
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          {active ? (
                            <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-emerald-400 px-1.5 text-[10px] font-semibold text-zinc-950">
                              v{variantNumber}
                            </span>
                          ) : (
                            <span className="h-5 w-5 rounded-full border border-zinc-700" />
                          )}
                          <span className="min-w-0 truncate font-medium">{String(apiRow.name ?? apiRow.api_id ?? "")}</span>
                          <MaskedApiLabel api={apiRow} />
                        </span>
                        {crossProvider && (
                          <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-amber-200">
                            別 provider
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              ) : (
                <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-3 text-sm text-amber-100">
                  {trimmedSearch
                    ? "検索条件に一致する API key がありません。"
                    : routeShowAllProviders
                      ? "登録済みの API key がありません。"
                      : `${selectedProvider || "このprovider"} の API key がありません。+ API key で追加するか、「接続」から登録してください。`}
                </div>
              )}
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 px-3 py-2 text-xs text-zinc-500">
                {selectedApis.length > 0 ? (
                  <span>
                    変種: <span className="font-mono text-zinc-300">{selectedApis.join(" / ")}</span>
                    {selectedApis.length >= 2 && <span className="ml-2 text-zinc-600">→ それぞれ別の model として composer に並びます</span>}
                  </span>
                ) : (
                  <span>このモデルに API key を選ぶと、その key を使う model variant が composer に追加されます。未選択時は provider 既定キーを使用します。</span>
                )}
              </div>
            </div>
          )}

          <details className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3">
            <summary className="cursor-pointer text-xs text-zinc-500">Advanced: route text</summary>
            <textarea
              value={routeText}
              onChange={(event) => onChange(sectionId, field.id, updateModelApiRouteText(event.target.value, "", []))}
              className="mt-3 min-h-28 w-full resize-y rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-xs text-zinc-300 outline-none focus:border-zinc-700"
              placeholder="google/gemini-2.5-pro: google/main, google/backup"
            />
          </details>
        </div>
      );
      break;
    }
    case "api_keys": {
      const providers = apiProviderRows(value);
      const allProviderOptions = collectApiProviderOptions(providers);
      const providerOptions = allProviderOptions.filter((option) => option.kind === "custom");
      const registeredApis = registeredApiRows(providers).filter((api) => {
        const option = allProviderOptions.find((candidate) => candidate.provider_id === String(api.provider_id ?? ""));
        return normalizeProviderKind(api.kind ?? option?.kind) === "custom";
      });
      const selectedProviderOption = providerOptions.find((option) => option.provider_id === apiProvider);
      const selectedKind: "llm" | "custom" = selectedProviderOption?.kind ?? "llm";
      const isCustomProvider = !selectedProviderOption?.builtin;
      const resetApiSaveFeedback = () => {
        setApiSaveState("idle");
        setApiSaveError("");
        setApiAvailability(null);
        setApiActionMessage("");
      };
      const refreshApiKeyField = () => onChange(sectionId, field.id, { action: "oauth_refresh" });
      const renameProviderApiKey = async (apiRow: Record<string, unknown>) => {
        const providerId = String(apiRow.provider_id ?? "").trim();
        const apiId = String(apiRow.api_id ?? "").trim();
        const nextName = renameDraft.trim();
        const key = String(apiRow.key ?? `${providerId}:${apiId}`);
        if (!providerId || !apiId || !nextName || apiActionBusyKey) return;
        setApiActionBusyKey(key);
        setApiSaveError("");
        setApiActionMessage("");
        try {
          await settingsApiResources.renameProviderApiKey(providerId, apiId, nextName);
          setRenamingKey("");
          setApiActionMessage(`Renamed “${String(apiRow.name ?? apiId)}” to “${nextName}”.`);
          refreshApiKeyField();
        } catch (errorValue) {
          setApiSaveError(errorValue instanceof Error ? errorValue.message : "API key rename failed.");
        } finally {
          setApiActionBusyKey("");
        }
      };
      const deleteProviderApiKey = async (apiRow: Record<string, unknown>) => {
        const providerId = String(apiRow.provider_id ?? "").trim();
        const apiId = String(apiRow.api_id ?? "").trim();
        const key = String(apiRow.key ?? `${providerId}:${apiId}`);
        if (!providerId || !apiId || apiActionBusyKey) return;
        setApiActionBusyKey(key);
        setApiSaveError("");
        setApiActionMessage("");
        try {
          await settingsApiResources.deleteProviderApiKey(providerId, apiId);
          setPendingApiDeleteKey("");
          setOpenApiMenuKey("");
          setApiActionMessage(`Deleted API key “${String(apiRow.name ?? apiId)}”.`);
          refreshApiKeyField();
        } catch (errorValue) {
          setApiSaveError(errorValue instanceof Error ? errorValue.message : "API key delete failed.");
        } finally {
          setApiActionBusyKey("");
        }
      };
      const handleSubmitApi = async () => {
        if (!apiProvider.trim() || !apiName.trim() || !apiSecret.trim() || apiActionBusyKey) return;
        setApiSaveState("saving");
        setApiSaveError("");
        setApiAvailability(null);
        setApiActionMessage("");
        const allowedModels = apiAllowedModels.split(",").map((item) => item.trim()).filter(Boolean);
        try {
          const result = await settingsApiResources.saveProviderApiKey(apiProvider, apiSecret, {
            apiId: apiName,
            name: apiName,
            baseUrl: apiBaseUrl.trim() || undefined,
            allowedModels: allowedModels.length ? allowedModels : undefined,
            defaultModel: apiDefaultModel.trim() || undefined,
            quotaLabel: apiQuotaLabel.trim() || undefined,
            notes: apiNotes.trim() || undefined,
            kind: selectedKind,
          });
          setApiAvailability(result.model_availability ?? {
            status: "route_required",
            provider_id: apiProvider,
            api_id: apiName,
            candidate_models: [],
            reason: "Saved, but the backend did not confirm model availability. Choose a model route before using this key.",
          });
          const savedProviderId = apiProvider;
          const savedApiId = apiName;
          setCredentialTransfer({
            providerId: savedProviderId,
            providerLabel: selectedProviderOption?.label,
            apiId: savedApiId,
            refreshOnClose: true,
          });
          setApiSecret("");
          setApiBaseUrl("");
          setApiAllowedModels("");
          setApiDefaultModel("");
          setApiQuotaLabel("");
          setApiNotes("");
          setApiSaveState("saved");
        } catch (saveError) {
          setApiSaveState("idle");
          setApiSaveError(saveError instanceof Error ? saveError.message : "API key save failed.");
        }
      };
      const apiFeedback = apiSaveState === "saved" ? availabilityCopy(apiAvailability) : null;
      control = (
        <div className="space-y-4">
          <ProviderOAuthPanel
            sectionId={sectionId}
            fieldId={field.id}
            providers={providers}
            onRefresh={onChange}
          />
          <div className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950/70">
            <div className="divide-y divide-zinc-800/80">
              {registeredApis.length > 0 ? registeredApis.map((api) => {
                const key = String(api.key ?? `${api.provider_id}:${api.api_id}`);
                const isRenaming = renamingKey === key;
                const isMenuOpen = openApiMenuKey === key;
                const apiProviderOption = providerOptions.find((option) => option.provider_id === String(api.provider_id ?? ""));
                const apiKind = normalizeProviderKind(api.kind ?? apiProviderOption?.kind);
                return (
                  <div
                    key={key}
                    aria-busy={apiActionBusyKey === key}
                    className="flex items-center gap-3 px-3 py-2.5 transition-colors hover:bg-zinc-900/40"
                  >
                    <div className="min-w-0 flex-1">
                      {isRenaming ? (
                        <div className="flex items-center gap-1.5">
                          <input
                            value={renameDraft}
                            autoFocus
                            onChange={(event) => setRenameDraft(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key !== "Enter") return;
                              event.preventDefault();
                              void renameProviderApiKey(api);
                            }}
                            className="min-w-0 flex-1 rounded-md border border-white/[0.09] bg-white/[0.04] px-2 py-1 text-xs text-zinc-200 outline-none"
                          />
                          <button
                            type="button"
                            disabled={!renameDraft.trim() || Boolean(apiActionBusyKey)}
                            onClick={(event) => {
                              event.stopPropagation();
                              void renameProviderApiKey(api);
                            }}
                            className="rounded-md border border-zinc-700 p-1 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                            title={apiActionBusyKey === key ? "Renaming…" : "Rename"}
                            aria-label={apiActionBusyKey === key ? "Renaming API key" : "Rename API key"}
                          >
                            {apiActionBusyKey === key ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                          </button>
                          <button
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              setRenamingKey("");
                            }}
                            className="rounded-md border border-zinc-800 p-1 text-zinc-500 hover:text-zinc-300"
                          >
                            <X size={13} />
                          </button>
                        </div>
                      ) : (
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium text-zinc-200">{String(api.name ?? api.api_id ?? "")}</span>
                          <span className="rounded-full border border-white/[0.09] bg-white/[0.04] px-2 py-0.5 text-[10px] uppercase tracking-wide text-zinc-400">
                            {String(api.provider_id ?? "")}
                          </span>
                          {apiKind === "custom" && (
                            <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] uppercase tracking-wide text-amber-200">
                              non-llm
                            </span>
                          )}
                          <MaskedApiLabel api={api} />
                        </div>
                      )}
                    </div>
                    <div className="relative flex justify-end">
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          setOpenApiMenuKey(isMenuOpen ? "" : key);
                          setPendingApiDeleteKey("");
                        }}
                        className="flex h-8 w-8 items-center justify-center rounded-lg border border-zinc-800 text-zinc-500 transition-colors hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-200"
                        title="Actions"
                        aria-haspopup="menu"
                        aria-expanded={isMenuOpen}
                      >
                        <MoreVertical size={15} />
                      </button>
                      {isMenuOpen && (
                        <>
                          <button
                            type="button"
                            aria-label="close api menu"
                            className="fixed inset-0 rumi-layer-panel cursor-default"
                            onClick={(event) => {
                              event.stopPropagation();
                              setOpenApiMenuKey("");
                              setPendingApiDeleteKey("");
                            }}
                          />
                          <div role="menu" className="absolute right-0 top-[calc(100%+6px)] rumi-layer-local-popover w-52 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-950 py-1 shadow-2xl">
                            {pendingApiDeleteKey === key ? (
                              <div role="none" className="space-y-2 px-3 py-2.5">
                                <p className="break-words text-xs font-medium text-rose-200">Delete “{String(api.name ?? api.api_id ?? "API key")}”?</p>
                                <p className="text-[10px] leading-4 text-zinc-500">The credential reference and its routing entry will be removed. This cannot be undone.</p>
                                <div className="flex gap-1.5">
                                  <button
                                    type="button"
                                    disabled={Boolean(apiActionBusyKey)}
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      void deleteProviderApiKey(api);
                                    }}
                                    className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md border border-rose-400/30 bg-rose-400/[0.08] px-2 py-1.5 text-[11px] font-medium text-rose-100 hover:bg-rose-400/[0.13] disabled:opacity-40"
                                  >
                                    {apiActionBusyKey === key ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} />}
                                    Delete
                                  </button>
                                  <button
                                    type="button"
                                    disabled={Boolean(apiActionBusyKey)}
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      setPendingApiDeleteKey("");
                                    }}
                                    className="rounded-md border border-white/10 px-2 py-1.5 text-[11px] text-zinc-300 hover:bg-white/[0.05] disabled:opacity-40"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <>
                                <button
                                  type="button"
                                  role="menuitem"
                                  disabled={Boolean(apiActionBusyKey)}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setRenamingKey(key);
                                    setRenameDraft(String(api.name ?? api.api_id ?? ""));
                                    setOpenApiMenuKey("");
                                  }}
                                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                                >
                                  <Pencil size={13} />
                                  Rename
                                </button>
                                <button
                                  type="button"
                                  role="menuitem"
                                  disabled={Boolean(apiActionBusyKey)}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setPendingApiDeleteKey(key);
                                  }}
                                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-rose-300 hover:bg-rose-950/30 disabled:opacity-40"
                                >
                                  <Trash2 size={13} />
                                  Delete…
                                </button>
                              </>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                );
              }) : (
                <div className="px-3 py-5 text-xs text-zinc-600">No registered API keys yet.</div>
              )}
            </div>
          </div>

          <div className="space-y-2">
            <div className="grid gap-2 md:grid-cols-[180px_minmax(120px,1fr)_minmax(180px,2fr)_auto]">
              <SearchableProviderSelect
                value={apiProvider}
                options={providerOptions}
                onChange={(next) => {
                  setApiProvider(next);
                  resetApiSaveFeedback();
                }}
                onAddCustom={(option) => {
                  onChange(sectionId, field.id, {
                    action: "register_provider",
                    provider_id: option.providerId,
                    label: option.label,
                    kind: option.kind,
                  });
                  setApiProvider(option.providerId);
                  resetApiSaveFeedback();
                }}
              />
              <input
                value={apiName}
                onChange={(event) => {
                  setApiName(event.target.value);
                  resetApiSaveFeedback();
                }}
                placeholder="名前 (例: main, work)"
                className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
              />
              <input
                type="password"
                autoComplete="off"
                value={apiSecret}
                onChange={(event) => {
                  setApiSecret(event.target.value);
                  resetApiSaveFeedback();
                }}
                placeholder={`${apiProvider || "provider"} API key`}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    event.preventDefault();
                    handleSubmitApi();
                  }
                }}
                className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
              />
              <button
                type="button"
                disabled={apiSaveState === "saving" || Boolean(apiActionBusyKey) || !apiProvider.trim() || !apiName.trim() || !apiSecret.trim()}
                onClick={handleSubmitApi}
                className={cn(
                  "rounded-lg border px-3 py-2 text-xs transition-colors",
                  apiSaveState !== "saving" && !apiActionBusyKey && apiProvider.trim() && apiName.trim() && apiSecret.trim()
                    ? "bg-zinc-100 text-zinc-950 border-zinc-100"
                    : "bg-zinc-900 text-zinc-600 border-zinc-800 cursor-not-allowed",
                )}
              >
                {apiSaveState === "saving" ? "Saving" : "Save"}
              </button>
            </div>
            {isCustomProvider && (
              <p className="text-[11px] text-zinc-500">
                {selectedKind === "custom"
                  ? "Non-LLM provider として保存されます。AI provider 自動切替には使われず、認識用にだけ保存します。"
                  : "Custom LLM provider として保存されます。"}
              </p>
            )}
            <details className="rounded-lg border border-white/[0.07] bg-white/[0.03] px-3 py-2 text-xs">
              <summary className="cursor-pointer select-none text-zinc-400 hover:text-zinc-200">Advanced (任意): base_url / model 制限 / quota / notes</summary>
              <div className="mt-3 grid gap-2 md:grid-cols-2">
                <input
                  value={apiBaseUrl}
                  onChange={(event) => {
                    setApiBaseUrl(event.target.value);
                    resetApiSaveFeedback();
                  }}
                  placeholder="base_url (optional)"
                  className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
                />
                <input
                  value={apiDefaultModel}
                  onChange={(event) => {
                    setApiDefaultModel(event.target.value);
                    resetApiSaveFeedback();
                  }}
                  placeholder="default model for this API"
                  className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
                />
                <input
                  value={apiAllowedModels}
                  onChange={(event) => {
                    setApiAllowedModels(event.target.value);
                    resetApiSaveFeedback();
                  }}
                  placeholder="allowed models, comma separated"
                  className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
                />
                <input
                  value={apiQuotaLabel}
                  onChange={(event) => {
                    setApiQuotaLabel(event.target.value);
                    resetApiSaveFeedback();
                  }}
                  placeholder="quota label"
                  className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
                />
                <textarea
                  value={apiNotes}
                  onChange={(event) => {
                    setApiNotes(event.target.value);
                    resetApiSaveFeedback();
                  }}
                  placeholder="notes for routing"
                  className="min-h-20 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none md:col-span-2"
                />
              </div>
              <p className="mt-2 text-[10px] text-zinc-600">
                次に保存する API key にだけ適用されます。通常はそのまま空欄で大丈夫です。
              </p>
            </details>
          </div>
          {apiFeedback?.text && (
            <div
              className={cn(
                "rounded-lg border px-3 py-2 text-[11px]",
                apiFeedback.tone === "success"
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-100",
              )}
            >
              {apiFeedback.text}
            </div>
          )}
          {apiActionMessage && (
            <div role="status" className="rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-[11px] text-emerald-300">
              {apiActionMessage}
            </div>
          )}
          {apiSaveError && (
            <div role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[11px] text-rose-200">
              {apiSaveError}
            </div>
          )}
        </div>
      );
      break;
    }
    case "external_tokens": {
      const providers = externalTokenProviderRows(value);
      const providerOptions = collectExternalProviderOptions(providers);
      const registeredTokens = registeredExternalTokenRows(providers);
      const requiredTokens = requiredExternalTokenRows(providers);
      const tokenHintByProvider: Record<string, string> = {
        line: "LINE: Messaging API Channel Secret / Access Tokenを貼ります。返信は受信元 conversation へ返り、push時だけExplicit Target IDを使います。",
        discord: "Discord Bot + Channel: Bot Tokenを貼り、Channel IDはExplicit Target ID欄へ。Webhook mode: Webhook URLを貼ります。",
        slack: "Slack: Signing Secret / Bot Tokenを貼り、Channel IDやThread TSはTarget欄へ。",
        generic: "Generic: shared secretやcallback URLを貼ります。",
      };
      const saveExternalToken = async () => {
        const providerId = tokenProvider.trim();
        const tokenId = tokenName.trim();
        const secret = tokenSecret;
        if (!providerId || !tokenId || !secret.trim() || tokenSaveState === "saving") return;
        setTokenSaveState("saving");
        setTokenSaveError("");
        setTokenActionMessage("");
        try {
          await settingsApiResources.saveExternalToken(providerId, secret, { tokenId, name: tokenId, kind: tokenKind });
          setTokenSecret("");
          setTokenSaveState("saved");
          setTokenActionMessage(`Saved “${tokenId}” and verified the backend response.`);
          refreshSensitiveField();
        } catch (errorValue) {
          setTokenSaveState("error");
          setTokenSaveError(errorValue instanceof Error ? errorValue.message : "External credential save failed.");
        }
      };
      const renameExternalToken = async (token: Record<string, unknown>) => {
        const providerId = String(token.provider_id ?? "").trim();
        const tokenId = String(token.token_id ?? "").trim();
        const nextName = renameDraft.trim();
        const key = String(token.key ?? `${providerId}:${tokenId}`);
        if (!providerId || !tokenId || !nextName || tokenBusyKey) return;
        setTokenBusyKey(key);
        setTokenSaveState("saving");
        setTokenSaveError("");
        setTokenActionMessage("");
        try {
          await settingsApiResources.renameExternalToken(providerId, tokenId, nextName);
          setRenamingKey("");
          setTokenSaveState("saved");
          setTokenActionMessage(`Renamed “${String(token.name ?? tokenId)}” to “${nextName}”.`);
          refreshSensitiveField();
        } catch (errorValue) {
          setTokenSaveState("error");
          setTokenSaveError(errorValue instanceof Error ? errorValue.message : "External credential rename failed.");
        } finally {
          setTokenBusyKey("");
        }
      };
      const deleteExternalToken = async (token: Record<string, unknown>) => {
        const providerId = String(token.provider_id ?? "").trim();
        const tokenId = String(token.token_id ?? "").trim();
        const key = String(token.key ?? `${providerId}:${tokenId}`);
        if (!providerId || !tokenId || tokenBusyKey) return;
        setTokenBusyKey(key);
        setTokenSaveState("saving");
        setTokenSaveError("");
        setTokenActionMessage("");
        try {
          await settingsApiResources.deleteExternalToken(providerId, tokenId);
          if (selectedTokenKey === key) setSelectedTokenKey("");
          setPendingTokenDeleteKey("");
          setOpenTokenMenuKey("");
          setTokenSaveState("saved");
          setTokenActionMessage(`Deleted credential “${String(token.name ?? tokenId)}”.`);
          refreshSensitiveField();
        } catch (errorValue) {
          setTokenSaveState("error");
          setTokenSaveError(errorValue instanceof Error ? errorValue.message : "External credential delete failed.");
        } finally {
          setTokenBusyKey("");
        }
      };
      control = (
        <div className="space-y-4">
          <div className="overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950/70">
            <div className="grid grid-cols-[36px_minmax(220px,1.4fr)_minmax(130px,0.8fr)_minmax(120px,0.8fr)_48px] items-center gap-3 border-b border-white/[0.07] bg-white/[0.045] px-3 py-2 text-[11px] font-medium text-zinc-500">
              <span className="h-4 w-4 rounded border border-cyan-500/70" />
              <span>Token</span>
              <span>Kind</span>
              <span>Endpoints</span>
              <span />
            </div>
            <div className="divide-y divide-zinc-800/80">
              {registeredTokens.length > 0 ? registeredTokens.map((token) => {
                const key = String(token.key ?? `${token.provider_id}:${token.token_id}`);
                const isRenaming = renamingKey === key;
                const isMenuOpen = openTokenMenuKey === key;
                const isSelected = selectedTokenKey === key;
                const endpointIds = Array.isArray(token.endpoint_ids) ? token.endpoint_ids.map(String).join(", ") : "";
                return (
                  <div
                    key={key}
                    aria-busy={tokenBusyKey === key}
                    className={cn(
                      "grid grid-cols-[36px_minmax(220px,1.4fr)_minmax(130px,0.8fr)_minmax(120px,0.8fr)_48px] items-center gap-3 px-3 py-3 transition-colors",
                      isSelected ? "bg-zinc-900/85" : "bg-zinc-950/20 hover:bg-zinc-900/45",
                    )}
                    onClick={() => setSelectedTokenKey((currentKey) => toggleSettingsRowSelection(currentKey, key))}
                  >
                    <span className={cn("h-4 w-4 rounded border", isSelected ? "border-cyan-400 bg-cyan-500/20" : "border-cyan-500/70")} />
                    <div className="min-w-0">
                      {isRenaming ? (
                        <div className="flex items-center gap-1.5">
                          <input
                            value={renameDraft}
                            autoFocus
                            onChange={(event) => setRenameDraft(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key !== "Enter") return;
                              event.preventDefault();
                              void renameExternalToken(token);
                            }}
                            aria-invalid={tokenSaveState === "error" && tokenBusyKey === key}
                            className="min-w-0 flex-1 rounded-md border border-white/[0.09] bg-white/[0.04] px-2 py-1 text-xs text-zinc-200 outline-none"
                          />
                          <button
                            type="button"
                            disabled={!renameDraft.trim() || Boolean(tokenBusyKey)}
                            onClick={(event) => {
                              event.stopPropagation();
                              void renameExternalToken(token);
                            }}
                            className="rounded-md border border-zinc-700 p-1 text-zinc-300 hover:bg-zinc-800 disabled:opacity-40"
                            title={tokenBusyKey === key ? "Renaming…" : "Rename"}
                            aria-label={tokenBusyKey === key ? "Renaming token" : "Rename token"}
                          >
                            {tokenBusyKey === key ? <Loader2 size={13} className="animate-spin" /> : <Check size={13} />}
                          </button>
                          <button
                            type="button"
                            onClick={(event) => {
                              event.stopPropagation();
                              setRenamingKey("");
                            }}
                            className="rounded-md border border-zinc-800 p-1 text-zinc-500 hover:text-zinc-300"
                          >
                            <X size={13} />
                          </button>
                        </div>
                      ) : (
                        <>
                          <div className="truncate text-sm font-medium text-zinc-200">{String(token.name ?? token.token_id ?? "")}</div>
                          <MaskedExternalTokenLabel token={token} />
                        </>
                      )}
                    </div>
                    <span className="truncate text-sm text-zinc-500">{String(token.kind ?? "token")}</span>
                    <span className="truncate text-sm text-zinc-500">{endpointIds || "None"}</span>
                    <div className="relative flex justify-end">
                      <button
                        type="button"
                        onClick={(event) => {
                          event.stopPropagation();
                          setOpenTokenMenuKey(isMenuOpen ? "" : key);
                          setPendingTokenDeleteKey("");
                        }}
                        className="flex h-8 w-8 items-center justify-center rounded-lg border border-zinc-800 text-zinc-500 transition-colors hover:border-zinc-700 hover:bg-zinc-900 hover:text-zinc-200"
                        title="Actions"
                          aria-haspopup="menu"
                          aria-expanded={isMenuOpen}
                      >
                        <MoreVertical size={15} />
                      </button>
                      {isMenuOpen && (
                        <>
                          <button
                            type="button"
                            aria-label="close token menu"
                            className="fixed inset-0 rumi-layer-panel cursor-default"
                            onClick={(event) => {
                              event.stopPropagation();
                              setOpenTokenMenuKey("");
                              setPendingTokenDeleteKey("");
                            }}
                          />
                          <div role="menu" className="absolute right-0 top-[calc(100%+6px)] rumi-layer-local-popover w-52 overflow-hidden rounded-lg border border-zinc-700 bg-zinc-950 py-1 shadow-2xl">
                            {pendingTokenDeleteKey === key ? (
                              <div role="none" className="space-y-2 px-3 py-2.5">
                                <p className="break-words text-xs font-medium text-rose-200">Delete “{String(token.name ?? token.token_id ?? "token")}”?</p>
                                <p className="text-[10px] leading-4 text-zinc-500">The stored credential and endpoint references will be removed. This cannot be undone.</p>
                                <div className="flex gap-1.5">
                                  <button
                                    type="button"
                                    disabled={Boolean(tokenBusyKey)}
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      void deleteExternalToken(token);
                                    }}
                                    className="inline-flex flex-1 items-center justify-center gap-1.5 rounded-md border border-rose-400/30 bg-rose-400/[0.08] px-2 py-1.5 text-[11px] font-medium text-rose-100 hover:bg-rose-400/[0.13] disabled:opacity-40"
                                  >
                                    {tokenBusyKey === key ? <Loader2 size={11} className="animate-spin" /> : <Trash2 size={11} />}
                                    Delete
                                  </button>
                                  <button
                                    type="button"
                                    disabled={Boolean(tokenBusyKey)}
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      setPendingTokenDeleteKey("");
                                    }}
                                    className="rounded-md border border-white/10 px-2 py-1.5 text-[11px] text-zinc-300 hover:bg-white/[0.05] disabled:opacity-40"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              </div>
                            ) : (
                              <>
                                <button
                                  type="button"
                                  role="menuitem"
                                  disabled={Boolean(tokenBusyKey)}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setRenamingKey(key);
                                    setRenameDraft(String(token.name ?? token.token_id ?? ""));
                                    setOpenTokenMenuKey("");
                                  }}
                                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-zinc-300 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-40"
                                >
                                  <Pencil size={13} />
                                  Rename
                                </button>
                                <button
                                  type="button"
                                  role="menuitem"
                                  disabled={Boolean(tokenBusyKey)}
                                  onClick={(event) => {
                                    event.stopPropagation();
                                    setPendingTokenDeleteKey(key);
                                  }}
                                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-rose-300 hover:bg-rose-950/30 disabled:cursor-not-allowed disabled:opacity-40"
                                >
                                  <Trash2 size={13} />
                                  Delete…
                                </button>
                              </>
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                );
              }) : (
                <div className="px-3 py-5 text-xs text-zinc-600">No registered external tokens yet.</div>
              )}
            </div>
          </div>
          {requiredTokens.length > 0 && (
            <div className="flex flex-wrap gap-2 text-[11px]">
              {requiredTokens.map((token) => (
                <span
                  key={`${String(token.provider_id)}:${String(token.kind)}`}
                  className={cn(
                    "rounded-md border px-2 py-1",
                    token.configured ? "border-emerald-800 bg-emerald-950/25 text-emerald-300" : "border-zinc-800 bg-zinc-950 text-zinc-500",
                  )}
                >
                  {String(token.provider_id)} / {String(token.kind)}: {token.configured ? "configured" : "missing"}
                </span>
              ))}
            </div>
          )}
          <div className="rounded-lg border border-zinc-800 bg-zinc-950/70 p-3">
            <div className="mb-3 text-xs leading-5 text-zinc-400">{tokenHintByProvider[tokenProvider] ?? "値は保存後に再表示しません。"}</div>
            <div className="grid gap-3 md:grid-cols-[150px_1fr_1fr_1.4fr_auto]">
              <label className="space-y-1.5">
                <span className="text-[11px] font-medium uppercase text-zinc-500">Provider</span>
                <SearchableProviderSelect
                  value={tokenProvider}
                  onChange={(nextProvider) => {
                    setTokenProvider(nextProvider);
                    setTokenKind(externalTokenKindOptions(nextProvider)[0]?.value ?? "token");
                    setTokenSaveState("idle");
                    setTokenSaveError("");
                    setTokenActionMessage("");
                  }}
                  onAddCustom={(option) => {
                    setTokenProvider(option.providerId);
                    setTokenKind(externalTokenKindOptions(option.providerId)[0]?.value ?? "token");
                    setTokenSaveState("idle");
                    setTokenSaveError("");
                    setTokenActionMessage("");
                  }}
                  options={providerOptions}
                  addCustomLabel="Add external provider..."
                  showKindControls={false}
                  showProviderBadges={false}
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] font-medium uppercase text-zinc-500">Token ID</span>
                <input
                  value={tokenName}
                  onChange={(event) => {
                    setTokenName(event.target.value);
                    setTokenSaveState("idle");
                    setTokenSaveError("");
                    setTokenActionMessage("");
                  }}
                  placeholder="main"
                  className="w-full rounded-lg border border-white/[0.09] bg-white/[0.04] px-3 py-2 text-sm text-zinc-200 outline-none focus:border-cyan-500"
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] font-medium uppercase text-zinc-500">Paste Kind</span>
                <CustomSelect
                  value={tokenKind}
                  onChange={(nextKind) => {
                    setTokenKind(nextKind);
                    setTokenSaveState("idle");
                    setTokenSaveError("");
                    setTokenActionMessage("");
                  }}
                  options={externalTokenKindOptions(tokenProvider)}
                />
              </label>
              <label className="space-y-1.5">
                <span className="text-[11px] font-medium uppercase text-zinc-500">Secret / URL Value</span>
                <input
                  type="password"
                  autoComplete="off"
                  value={tokenSecret}
                  onChange={(event) => {
                    setTokenSecret(event.target.value);
                    setTokenSaveState("idle");
                    setTokenSaveError("");
                    setTokenActionMessage("");
                  }}
                  aria-invalid={tokenSaveState === "error"}
                  aria-describedby={tokenSaveError ? `${sectionId}-${field.id}-token-error` : undefined}
                  placeholder={tokenKind === "webhook_url" ? "https://discord.com/api/webhooks/..." : `${tokenProvider} ${tokenKind}`}
                  className="w-full rounded-lg border border-white/[0.09] bg-white/[0.04] px-3 py-2 text-sm text-zinc-200 outline-none focus:border-cyan-500"
                />
              </label>
              <div className="flex items-end">
                <button
                  type="button"
                  disabled={!tokenProvider.trim() || !tokenName.trim() || !tokenSecret.trim() || tokenSaveState === "saving" || Boolean(tokenBusyKey)}
                  onClick={() => { void saveExternalToken(); }}
                  className={cn(
                    "w-full rounded-lg border px-3 py-2 text-sm transition-colors",
                    tokenProvider.trim() && tokenName.trim() && tokenSecret.trim() && tokenSaveState !== "saving" && !tokenBusyKey
                      ? "border-zinc-100 bg-zinc-100 text-zinc-950"
                      : "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600",
                  )}
                >
                  {tokenSaveState === "saving" && !tokenBusyKey ? "Saving…" : "Save"}
                </button>
              </div>
            </div>
          </div>
          {tokenSaveState === "saved" && (
            <p role="status" className="text-[11px] text-emerald-400">{tokenActionMessage || "Saved and verified by the backend."}</p>
          )}
          {tokenSaveState === "error" && tokenSaveError && (
            <div id={`${sectionId}-${field.id}-token-error`} role="alert" className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[11px] leading-5 text-rose-200">
              <p>{tokenSaveError}</p>
              <p className="text-rose-200/70">A new secret remains only in this input until the backend confirms it was stored.</p>
            </div>
          )}
        </div>
      );
      break;
    }
    case "public_url":
      control = (
        <PublicUrlField
          sectionId={sectionId}
          field={field}
          value={value}
          onChange={onChange}
        />
      );
      break;
    case "secret":
      control = (
        <div className="space-y-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <input
              type="password"
              autoComplete="off"
              value={secretDraft}
              placeholder={isSecretConfigured ? "Saved securely" : "Not set"}
              aria-invalid={secretState === "error"}
              onChange={(event) => {
                setSecretDraft(event.target.value);
                setSecretState("idle");
                setSecretError("");
              }}
              onKeyDown={(event) => {
                if (event.key !== "Enter" || !secretDraft.trim() || secretState === "saving") return;
                event.preventDefault();
                void saveSecretValue();
              }}
              className="min-w-[220px] flex-1 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 outline-none"
            />
            <button
              type="button"
              disabled={!secretDraft.trim() || secretState === "saving"}
              onClick={() => { void saveSecretValue(); }}
              className={cn(
                "rounded-lg border px-3 py-2 text-xs transition-colors",
                secretDraft.trim() && secretState !== "saving"
                  ? "border-zinc-100 bg-zinc-100 text-zinc-950"
                  : "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600",
              )}
            >
              {secretState === "saving" ? "Saving…" : "Save"}
            </button>
            <span className={cn("min-w-16 text-[11px]", secretState === "error" ? "text-red-300" : secretState === "saved" || isSecretConfigured ? "text-emerald-400" : "text-zinc-500")} aria-live="polite">
              {secretState === "saving" ? "Verifying…" : secretState === "saved" || isSecretConfigured ? "Saved" : ""}
            </span>
          </div>
          {secretError ? <p className="text-[11px] leading-5 text-red-300" role="alert">{secretError} The value remains only in this input so you can correct or retry it.</p> : null}
        </div>
      );
      break;
    case "toggle":
      control = (
        <button
          type="button"
          aria-label={field.label}
          aria-pressed={Boolean(value)}
          onClick={() => onChange(sectionId, field.id, !Boolean(value))}
          className={cn("w-10 h-6 rounded-full relative transition-colors", Boolean(value) ? "bg-emerald-500" : "bg-zinc-700")}
        >
          <span className={cn("absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform", Boolean(value) && "translate-x-4")} />
        </button>
      );
      break;
    case "select":
      control = field.id === "preferred_model" ? (
        <SettingsModelSearchSelect
          value={formFieldString(value, field.default)}
          onChange={(nextValue) => onChange(sectionId, field.id, nextValue)}
          options={(field.options ?? []).map(modelFieldOptionToOption)}
          placeholder="model/provider/特徴メモで検索"
        />
      ) : (
        <CustomSelect
          value={formFieldString(value, field.default)}
          onChange={(nextValue) => onChange(sectionId, field.id, nextValue)}
          options={(field.options ?? []).map((option) => ({ value: String(option.value), label: option.label }))}
        />
      );
      break;
    case "number":
      control = (
        <input
          type="number"
          value={Number(value ?? field.default ?? 0)}
          min={field.min}
          max={field.max}
          onChange={(event) => onChange(sectionId, field.id, Number(event.target.value))}
          className="bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-sm text-zinc-200 outline-none w-28"
        />
      );
      break;
    case "color": {
      const colorValue = colorFieldValue(value, field.default);
      control = (
        <div className="flex min-w-0 items-center gap-2">
          <input
            type="color"
            value={colorValue}
            onChange={(event) => onChange(sectionId, field.id, event.target.value.toUpperCase())}
            className="h-10 w-12 shrink-0 cursor-pointer rounded-lg border border-zinc-800 bg-zinc-900 p-1"
            aria-label={field.label}
          />
          <input
            type="text"
            value={colorValue.toUpperCase()}
            onChange={(event) => onChange(sectionId, field.id, event.target.value)}
            className="min-w-[110px] bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 font-mono text-sm text-zinc-200 outline-none"
          />
        </div>
      );
      break;
    }
    case "readonly":
      control = (
        <div className="group/readonly flex min-w-0 items-start justify-between gap-3 rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2">
          <div className="min-w-0 flex-1 whitespace-pre-wrap break-all text-sm leading-6 text-zinc-300 select-text">{formatReadonlyValue(value, field.default)}</div>
          <button
            type="button"
            onClick={() => void copyTextToClipboard(formatReadonlyValue(value, field.default))}
            className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-zinc-800 text-zinc-500 opacity-70 transition-colors hover:border-zinc-600 hover:text-zinc-200 group-hover/readonly:opacity-100"
            title="Copy"
          >
            <Copy size={13} />
          </button>
        </div>
      );
      break;
    case "textarea":
      control = field.id === "model_allowlist" ? (
        <ModelAllowlistField
          value={value}
          fallback={field.default}
          options={(field.options ?? []).map(modelFieldOptionToOption)}
          onChange={(nextValue) => onChange(sectionId, field.id, nextValue)}
        />
      ) : (
        <textarea
          value={formFieldString(value, field.default)}
          onChange={(event) => onChange(sectionId, field.id, event.target.value)}
          className="w-full h-20 bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-sm text-zinc-200 outline-none resize-none"
        />
      );
      break;
    default: {
      const isComposerPlaceholder = sectionId === "general" && field.id === "composer_placeholder";
      const textValue = formFieldString(value, field.default);
      const defaultComposerPlaceholder = formFieldString(field.default, "メッセージを入力...");
      const isUncustomizedComposerPlaceholder = isComposerPlaceholder
        && textValue.replace(/…/g, "...") === defaultComposerPlaceholder.replace(/…/g, "...");
      control = (
        <input
          type="text"
          value={isUncustomizedComposerPlaceholder ? "" : textValue}
          placeholder={isComposerPlaceholder ? defaultComposerPlaceholder : undefined}
          onChange={(event) => onChange(sectionId, field.id, event.target.value)}
          className="bg-zinc-900 border border-zinc-800 rounded-lg px-3 py-2 text-sm text-zinc-200 outline-none placeholder:text-zinc-600 min-w-[240px]"
        />
      );
      break;
    }
  }

  return (
    <div className="space-y-1.5 min-w-0">
      <div className="flex flex-col gap-2">
        {commonLabel}
        {control}
      </div>
      {field.help && <p className="text-[11px] text-zinc-500">{field.help}</p>}
      {credentialTransfer && (
        <CredentialTransferModal
          providerId={credentialTransfer.providerId}
          providerLabel={credentialTransfer.providerLabel}
          apiId={credentialTransfer.apiId}
          onClose={() => {
            const shouldRefresh = credentialTransfer.refreshOnClose;
            setCredentialTransfer(null);
            if (shouldRefresh) {
              onChange(sectionId, field.id, {
                action: "oauth_refresh",
              });
            }
          }}
        />
      )}
    </div>
  );
}

function SettingsFieldFallback(props: SettingsFieldRendererProps) {
  return <SettingsField {...props} field={props.field as SettingsSection["fields"][number]} />;
}

function ModelApiRoutesSettingsFieldRenderer(props: SettingsFieldRendererProps) {
  return <SettingsField {...props} field={props.field as SettingsSection["fields"][number]} />;
}

export function SettingsModalRenderer({
  isOpen,
  activeSectionId: requestedSectionId,
  catalog,
  health,
  previewsCount,
  settingsSections,
  settingsValues,
  desktopSystemInfo,
  modelProfiles = [],
  activeModelProfileId,
  backendConnectionState = "online",
  backendConnectionNote,
  saveState = { status: "idle", dirtyKeys: [] },
  loadState = { status: "ready" },
  modelProfilesLoadState = { status: "ready" },
  locale = "ja",
  onClose,
  onStartSettingsChat,
  onOpenSection,
  onRetryLoad,
  onRetrySave,
  onSettingChange,
}: SettingsModalRendererProps) {
  const isJapanese = normalizeLocale(locale) === "ja";
  const localizedCopy = (english: string, japanese: string) => isJapanese ? japanese : english;
  const prefersReducedMotion = useReducedMotion();
  const layerRef = useRef<HTMLDivElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const dialogTitleRef = useRef<HTMLHeadingElement | null>(null);
  const placementMenuRef = useRef<HTMLDivElement | null>(null);
  const placementTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const closeConfirmationRef = useRef<HTMLDivElement | null>(null);
  const stayInSettingsRef = useRef<HTMLButtonElement | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const [activeSectionId, setActiveSectionId] = useState<ControlCenterSection["id"]>(
    () => {
      if (!requestedSectionId) return "quick_setup";
      const mapped = mapSettingsSectionId(requestedSectionId);
      if (mapped) return mapped;
      const sourceSection = buildControlCenterSections(settingsSections, locale).find((section) => (
        section.sourceSections.some((source) => source.id === requestedSectionId)
      ));
      return sourceSection?.id ?? "packs_extensions";
    },
  );
  const [settingsDisplayMode, setSettingsDisplayMode] = useState<"standard" | "advanced" | "developer">(
    () => requestedSectionId === "external_custom" || requestedSectionId === "debug" ? "developer" : "standard",
  );
  const [settingsSearch, setSettingsSearch] = useState("");
  const [profileSelectionRequest, setProfileSelectionRequest] = useState<{ id: string; version: number } | null>(null);
  const [placementMenuOpen, setPlacementMenuOpen] = useState(false);
  const [closeConfirmationOpen, setCloseConfirmationOpen] = useState(false);
  const [connectionBusy, setConnectionBusy] = useState("");
  const [connectionMessages, setConnectionMessages] = useState<Record<string, { tone: "success" | "error"; text: string }>>({});
  const [connectionScopeModes, setConnectionScopeModes] = useState<Record<string, string>>({});
  const [connectionCredentialDrafts, setConnectionCredentialDrafts] = useState<Record<string, string>>({});
  const [connectionOAuthReviews, setConnectionOAuthReviews] = useState<Record<string, PendingOAuthReview>>({});
  const [connectionDraftReviews, setConnectionDraftReviews] = useState<Record<string, CredentialImportReview>>({});
  const [expandedConnectionProviderId, setExpandedConnectionProviderId] = useState("");
  const [codexAppServerDraft, setCodexAppServerDraft] = useState<CodexAppServerConfig>({
    transport: "off",
    enabled: false,
    baseUrl: "",
    websocketUrl: "",
    unixSocketPath: "",
    wsTokenFile: "",
    sharedSecretFile: "",
    toolSourceEnabled: false,
    automationEndpointEnabled: false,
  });
  const normalizedSearch = settingsSearch.trim().toLowerCase();
  const dirtySettingsKeys = saveState.dirtyKeys ?? [];
  const hasUnconfirmedSettingsChanges = settingsCloseRequiresConfirmation(saveState);
  const dismissCloseConfirmation = useCallback(() => {
    setCloseConfirmationOpen(false);
    requestAnimationFrame(() => closeButtonRef.current?.focus());
  }, []);
  const requestClose = useCallback(() => {
    if (hasUnconfirmedSettingsChanges) {
      setPlacementMenuOpen(false);
      setCloseConfirmationOpen(true);
      return;
    }
    onClose();
  }, [hasUnconfirmedSettingsChanges, onClose]);
  const sidebarSettings = settingsValues.sidebar ?? {};
  const controlCenterSections = useMemo(
    () => buildControlCenterSections(settingsSections, locale),
    [locale, settingsSections],
  );
  const accountConnectionCards = useMemo(
    () => buildAccountConnectionPrelude(settingsValues, locale),
    [locale, settingsValues],
  );
  const codexAppServerPrelude = useMemo<CodexAppServerPrelude>(
    () => buildCodexAppServerPrelude(settingsValues),
    [settingsValues],
  );
  const profileWorkspaceSettingsValues = useMemo(
    () => normalizeProfileReferenceSettings(settingsValues),
    [settingsValues],
  );
  const profileWorkspaceCatalog = useMemo(
    () => normalizeProfileReferenceCatalog(catalog),
    [catalog],
  );
  const normalizedActiveModelProfileId = profileReferenceId(activeModelProfileId);
  const profileWorkspace = useMemo(
    () => buildSettingsProfileWorkspace({
      settingsSections,
      settingsValues: profileWorkspaceSettingsValues,
      catalog: profileWorkspaceCatalog,
      modelProfiles,
      activeModelProfileId: normalizedActiveModelProfileId || undefined,
    }),
    [modelProfiles, normalizedActiveModelProfileId, profileWorkspaceCatalog, profileWorkspaceSettingsValues, settingsSections],
  );
  const profileOwnedFieldKeys = useMemo(() => {
    const collection = profileWorkspace.editableCollection;
    if (!collection) return new Set<string>();
    return new Set([
      `${collection.sectionId}:${collection.fieldId}`,
      ...(collection.activeFieldId ? [`${collection.sectionId}:${collection.activeFieldId}`] : []),
      ...(collection.defaultFieldId ? [`${collection.sectionId}:${collection.defaultFieldId}`] : []),
    ]);
  }, [profileWorkspace.editableCollection]);
  const activeProfileLabel = useMemo(
    () => profileWorkspace.profiles.find((profile) => profile.active)?.name
      ?? activeSettingsProfileLabel(settingsValues, catalog),
    [catalog, profileWorkspace.profiles, settingsValues],
  );
  const placementManifestMap = useMemo(
    () => new Map(buildBuiltinPlacementManifests(settingsSections).map((manifest) => [manifest.id, manifest])),
    [settingsSections],
  );
  const pinnedPlacements = useMemo(
    () => normalizePinnedPlacements(sidebarSettings.ui_placements),
    [sidebarSettings.ui_placements],
  );
  const pinnedSettingsPlacements = useMemo(
    () => pinnedPlacements
      .filter((placement) => placement.surface === "settings")
      .map((placement) => placementManifestMap.get(placement.id))
      .filter((manifest): manifest is PlacementManifest => Boolean(manifest)),
    [pinnedPlacements, placementManifestMap],
  );
  const settingsPlacementCandidates = useMemo(() => (
    filterPlacementCandidates([...placementManifestMap.values()], {
      surface: "settings",
      orientation: "vertical",
      configurableOnly: true,
    }).filter((manifest) => !pinnedPlacements.some((placement) => (
      placement.id === manifest.id && placement.surface === "settings"
    )))
  ), [pinnedPlacements, placementManifestMap]);
  const profileSearchMatches = useMemo(() => {
    if (!normalizedSearch) return [];
    return profileWorkspace.profiles.filter((profile) => ([
      profile.name, profile.id, profile.role, profile.description, profile.providerId, profile.modelId,
      ...profile.routeRefs, ...profile.capabilityTags,
    ].join(" ").toLowerCase().includes(normalizedSearch)));
  }, [normalizedSearch, profileWorkspace.profiles]);
  const visibleSections = useMemo(() => {
    const filtered = filterControlCenterSections(controlCenterSections, settingsSearch);
    if (!normalizedSearch || profileSearchMatches.length === 0 || filtered.some((section) => section.id === "profiles")) return filtered;
    const profilesSection = controlCenterSections.find((section) => section.id === "profiles");
    return profilesSection ? [...filtered, profilesSection].sort((left, right) => left.order - right.order) : filtered;
  }, [controlCenterSections, normalizedSearch, profileSearchMatches.length, settingsSearch]);
  const settingsSearchMatches = useMemo(() => {
    if (!normalizedSearch) return [];
    return controlCenterSections.flatMap((section) => section.fields
      .filter((field) => !profileOwnedFieldKeys.has(`${field.sourceSectionId}:${field.id}`))
      .filter((field) => settingsFieldSearchText(field).includes(normalizedSearch))
      .map((field) => ({ section, field })));
  }, [controlCenterSections, normalizedSearch, profileOwnedFieldKeys]);
  const navigationGroups = useMemo(() => ([
    {
      id: "everyday",
      label: localizedCopy("Everyday", "日常設定"),
      sectionIds: ["quick_setup", "models_api", "workspace_ui"],
    },
    {
      id: "connections_features",
      label: localizedCopy("Connections & features", "接続と機能"),
      sectionIds: ["accounts_connections", "features", "tools_mcp", "computer_automation"],
    },
    {
      id: "management",
      label: localizedCopy("Management", "管理"),
      sectionIds: ["privacy_security", "profiles", "packs_extensions"],
    },
    {
      id: "details",
      label: localizedCopy("Details", "詳細"),
      sectionIds: ["advanced", "diagnostics"],
    },
  ]), [isJapanese]);
  useEffect(() => {
    if (!requestedSectionId) return;
    const mappedSectionId = mapSettingsSectionId(requestedSectionId);
    const sourceSection = controlCenterSections.find((section) => (
      section.sourceSections.some((source) => source.id === requestedSectionId)
    ));
    const target = mappedSectionId ?? sourceSection?.id ?? "packs_extensions";
    if (controlCenterSections.some((section) => section.id === target)) {
      setActiveSectionId(target);
    }
  }, [controlCenterSections, isOpen, requestedSectionId]);
  useEffect(() => {
    if (!normalizedSearch) return;
    if (!visibleSections.some((section) => section.id === activeSectionId)) {
      setActiveSectionId(visibleSections[0]?.id ?? "quick_setup");
    }
  }, [activeSectionId, normalizedSearch, visibleSections]);
  useEffect(() => {
    if (!isOpen) setCloseConfirmationOpen(false);
  }, [isOpen]);
  useEffect(() => {
    if (!closeConfirmationOpen || hasUnconfirmedSettingsChanges) return;
    dismissCloseConfirmation();
  }, [closeConfirmationOpen, dismissCloseConfirmation, hasUnconfirmedSettingsChanges]);
  useEffect(() => {
    if (!closeConfirmationOpen) return;
    requestAnimationFrame(() => stayInSettingsRef.current?.focus());
  }, [closeConfirmationOpen]);
  useEffect(() => {
    if (!placementMenuOpen) return;
    const handlePointerDown = () => setPlacementMenuOpen(false);
    document.addEventListener("pointerdown", handlePointerDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
    };
  }, [placementMenuOpen]);
  useEffect(() => {
    if (!isOpen) return;
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const layer = layerRef.current;
    const parent = layer?.parentElement;
    const backgroundSiblings = parent
      ? Array.from(parent.children).filter((element) => element !== layer && !element.contains(layer))
      : [];
    const previousState = backgroundSiblings.map((element) => ({
      element: element as HTMLElement,
      inert: (element as HTMLElement).inert,
      ariaHidden: element.getAttribute("aria-hidden"),
    }));
    for (const { element } of previousState) {
      element.inert = true;
      element.setAttribute("aria-hidden", "true");
    }
    const frame = requestAnimationFrame(() => dialogTitleRef.current?.focus());
    return () => {
      cancelAnimationFrame(frame);
      for (const { element, inert, ariaHidden } of previousState) {
        element.inert = inert;
        if (ariaHidden === null) element.removeAttribute("aria-hidden");
        else element.setAttribute("aria-hidden", ariaHidden);
      }
      const opener = openerRef.current;
      requestAnimationFrame(() => {
        if (opener?.isConnected && !opener.hasAttribute("disabled")) opener.focus();
      });
    };
  }, [isOpen]);
  useEffect(() => {
    if (!isOpen) return;
    const focusableSelector = [
      "button:not([disabled])",
      "[href]",
      "input:not([disabled])",
      "select:not([disabled])",
      "textarea:not([disabled])",
      "[tabindex]:not([tabindex='-1'])",
    ].join(",");
    const handleDialogKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        if (closeConfirmationOpen) {
          dismissCloseConfirmation();
        } else if (placementMenuOpen) {
          setPlacementMenuOpen(false);
          placementTriggerRef.current?.focus();
        } else {
          requestClose();
        }
        return;
      }
      if (event.key !== "Tab") return;
      const scope = closeConfirmationOpen
        ? closeConfirmationRef.current
        : placementMenuOpen
          ? placementMenuRef.current
          : dialogRef.current;
      if (!scope) return;
      const focusable = Array.from(scope.querySelectorAll<HTMLElement>(focusableSelector))
        .filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true" && element.offsetParent !== null);
      if (focusable.length === 0) {
        event.preventDefault();
        (closeConfirmationOpen
          ? stayInSettingsRef.current
          : placementMenuOpen
            ? placementMenuRef.current
            : dialogTitleRef.current)?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !scope.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !scope.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleDialogKeyDown);
    return () => document.removeEventListener("keydown", handleDialogKeyDown);
  }, [closeConfirmationOpen, dismissCloseConfirmation, isOpen, placementMenuOpen, requestClose]);
  useEffect(() => {
    if (!placementMenuOpen) return;
    requestAnimationFrame(() => {
      placementMenuRef.current?.querySelector<HTMLElement>("button:not([disabled])")?.focus();
    });
  }, [placementMenuOpen]);
  useEffect(() => {
    setCodexAppServerDraft({
      transport: codexAppServerPrelude.transport,
      enabled: codexAppServerPrelude.enabled,
      baseUrl: codexAppServerPrelude.baseUrl,
      websocketUrl: codexAppServerPrelude.websocketUrl,
      unixSocketPath: codexAppServerPrelude.unixSocketPath,
      wsTokenFile: codexAppServerPrelude.wsTokenFile,
      sharedSecretFile: codexAppServerPrelude.sharedSecretFile,
      toolSourceEnabled: codexAppServerPrelude.toolSourceStatus !== "disabled",
      automationEndpointEnabled: codexAppServerPrelude.automationEndpointStatus !== "disabled",
    });
  }, [
    codexAppServerPrelude.automationEndpointStatus,
    codexAppServerPrelude.baseUrl,
    codexAppServerPrelude.enabled,
    codexAppServerPrelude.sharedSecretFile,
    codexAppServerPrelude.toolSourceStatus,
    codexAppServerPrelude.transport,
    codexAppServerPrelude.unixSocketPath,
    codexAppServerPrelude.websocketUrl,
    codexAppServerPrelude.wsTokenFile,
  ]);
  const activeSection = visibleSections.find((section) => section.id === activeSectionId)
    ?? visibleSections[0]
    ?? controlCenterSections[0];
  const fieldSourceValues = (field: ControlCenterField) => settingsValues[field.sourceSectionId] ?? {};
  const profilePanelOwnsField = (field: ControlCenterField) => (
    activeSection?.id === "profiles"
    && field.controlSectionId === "profiles"
    && profileOwnedFieldKeys.has(`${field.sourceSectionId}:${field.id}`)
  );
  const primaryFields = activeSection?.fields.filter((field) => !field.advanced && !profilePanelOwnsField(field) && settingsFieldVisible(field, fieldSourceValues(field))) ?? [];
  const advancedFields = activeSection?.fields.filter((field) => field.advanced && !profilePanelOwnsField(field) && settingsFieldVisible(field, fieldSourceValues(field))) ?? [];
  const activeSectionOwnText = [
    activeSection?.id ?? "",
    activeSection?.label ?? "",
    activeSection?.description ?? "",
  ].join(" ").toLowerCase();
  const fieldFilter = (field: SettingsSection["fields"][number]) => (
    !normalizedSearch
    || activeSectionOwnText.includes(normalizedSearch)
    || settingsFieldSearchText(field).includes(normalizedSearch)
  );
  const visiblePrimaryFields = primaryFields.filter(fieldFilter);
  const visibleAdvancedFields = advancedFields.filter(fieldFilter);
  const updatePinnedPlacements = (
    updater: (current: ReturnType<typeof normalizePinnedPlacements>) => ReturnType<typeof normalizePinnedPlacements>,
  ) => {
    onSettingChange("sidebar", "ui_placements", updater(pinnedPlacements));
  };
  const openSection = (sectionId: string) => {
    setActiveSectionId(mapSettingsSectionId(sectionId) ?? "quick_setup");
    onOpenSection?.(sectionId);
  };
  const refreshConnectionStatus = (providerId: string, activeDiagnostics = false) => {
    onSettingChange("apis", "api_keys", providerId === "codex"
      ? { action: "oauth_refresh" }
      : { action: "oauth_refresh", provider_id: providerId, active_diagnostics: activeDiagnostics });
  };
  const selectedConnectionScopeMode = (card: AccountConnectionPreludeCard): AccountConnectionScopeModeOption | undefined => {
    const selectedId = connectionScopeModes[card.providerId] || card.scopeMode || card.scopeModes[0]?.id || "";
    return card.scopeModes.find((option) => option.id === selectedId) ?? card.scopeModes[0];
  };
  const startAccountConnection = async (card: AccountConnectionPreludeCard, scopeModeOption?: AccountConnectionScopeModeOption) => {
    if (!card.connectAction) return;
    const selectedOption = scopeModeOption ?? selectedConnectionScopeMode(card);
    const scopeMode = selectedOption?.id ?? card.scopeMode;
    const services = selectedOption?.services ?? card.services;
    let popup: Window | null = null;
    try {
      popup = window.open("", `rumi-oauth-${card.providerId}`, "popup=yes,width=560,height=760");
      setConnectionBusy(`${card.providerId}:start`);
      const result = await settingsApiResources.startProviderOAuth(card.providerId, { scopeMode, services });
      const destination = reviewOAuthDestination(card.providerId, result.authorize_url);
      setConnectionOAuthReviews((current) => ({
        ...current,
        [card.providerId]: { ...destination, popup, scopes: result.scopes ?? [] },
      }));
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: { tone: "success", text: `${card.label} destination is ready for review; no external page has opened yet.` },
      }));
    } catch {
      if (popup && !popup.closed) popup.close();
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: {
          tone: "error",
          text: `Failed to start ${card.label} OAuth because the provider destination was not approved.`,
        },
      }));
    } finally {
      setConnectionBusy("");
    }
  };

  const confirmAccountConnectionOAuth = (card: AccountConnectionPreludeCard) => {
    const review = connectionOAuthReviews[card.providerId];
    if (!review) return;
    const popup = review.popup && !review.popup.closed
      ? review.popup
      : window.open("", `rumi-oauth-${card.providerId}`, "popup=yes,width=560,height=760");
    if (!popup) {
      setConnectionMessages((current) => ({ ...current, [card.providerId]: { tone: "error", text: "Popup was blocked. Settings state was preserved; allow a popup and retry." } }));
      return;
    }
    popup.location.replace(review.authorizeUrl);
    popup.focus();
    setConnectionOAuthReviews((current) => {
      const next = { ...current };
      delete next[card.providerId];
      return next;
    });
    setConnectionMessages((current) => ({ ...current, [card.providerId]: { tone: "success", text: "Authorization page opened. It is not connected until the local callback status is verified." } }));
  };

  const cancelAccountConnectionOAuth = (card: AccountConnectionPreludeCard) => {
    const review = connectionOAuthReviews[card.providerId];
    if (review?.popup && !review.popup.closed) review.popup.close();
    setConnectionOAuthReviews((current) => {
      const next = { ...current };
      delete next[card.providerId];
      return next;
    });
  };

  const saveConnectionCredential = async (card: AccountConnectionPreludeCard) => {
    if (card.credential?.kind !== "codex_access_token") return;
    const draft = String(connectionCredentialDrafts[card.providerId] ?? "").trim();
    if (!draft) {
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: { tone: "error", text: "Token is required." },
      }));
      return;
    }
    try {
      setConnectionBusy(`${card.providerId}:save_credential`);
      await settingsApiResources.saveCodexAccessToken(draft);
      setConnectionCredentialDrafts((current) => ({ ...current, [card.providerId]: "" }));
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: { tone: "success", text: "Codex token saved." },
      }));
      refreshConnectionStatus(card.providerId);
    } catch (errorValue) {
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: {
          tone: "error",
          text: errorValue instanceof Error ? errorValue.message : "Failed to save Codex token.",
        },
      }));
    } finally {
      setConnectionBusy("");
    }
  };

  const clearConnectionCredential = async (card: AccountConnectionPreludeCard) => {
    if (card.credential?.kind !== "codex_access_token") return;
    try {
      setConnectionBusy(`${card.providerId}:clear_credential`);
      await settingsApiResources.clearCodexAccessToken();
      setConnectionCredentialDrafts((current) => ({ ...current, [card.providerId]: "" }));
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: { tone: "success", text: "Codex token cleared." },
      }));
      refreshConnectionStatus(card.providerId);
    } catch (errorValue) {
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: {
          tone: "error",
          text: errorValue instanceof Error ? errorValue.message : "Failed to clear Codex token.",
        },
      }));
    } finally {
      setConnectionBusy("");
    }
  };

  const saveAccountConnectionJson = async (card: AccountConnectionPreludeCard) => {
    const draft = String(connectionCredentialDrafts[card.providerId] ?? "").trim();
    if (!draft) {
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: { tone: "error", text: "JSON is required." },
      }));
      return;
    }
    try {
      setConnectionDraftReviews((current) => ({ ...current, [card.providerId]: reviewConnectionDraft(draft) }));
      setConnectionMessages((current) => ({ ...current, [card.providerId]: { tone: "success", text: "Review the redacted credential summary before saving." } }));
    } catch {
      setConnectionMessages((current) => ({
        ...current,
        [card.providerId]: {
          tone: "error",
          text: "Credential data must be valid, reviewable JSON before it can be saved.",
        },
      }));
    }
  };

  const confirmAccountConnectionJson = async (card: AccountConnectionPreludeCard) => {
    const review = connectionDraftReviews[card.providerId];
    const draft = String(connectionCredentialDrafts[card.providerId] ?? "").trim();
    if (!review || !draft) return;
    try {
      setConnectionBusy(`${card.providerId}:save_json`);
      if (review.kind === "connection_import") await settingsApiResources.importProviderConnection(card.providerId, draft);
      else await settingsApiResources.saveProviderOAuthClientConfig(card.providerId, draft);
      setConnectionCredentialDrafts((current) => ({ ...current, [card.providerId]: "" }));
      setConnectionDraftReviews((current) => {
        const next = { ...current };
        delete next[card.providerId];
        return next;
      });
      setConnectionMessages((current) => ({ ...current, [card.providerId]: { tone: "success", text: "Saved. Local connection status will now be refreshed." } }));
      refreshConnectionStatus(card.providerId);
    } catch {
      setConnectionMessages((current) => ({ ...current, [card.providerId]: { tone: "error", text: "The credential was not confirmed as saved. Check local status and retry if necessary." } }));
    } finally {
      setConnectionBusy("");
    }
  };

  const saveCodexAppServer = async () => {
    try {
      setConnectionBusy("codex_app_server:save");
      const result = await settingsApiResources.saveCodexAppServerConfig(codexAppServerDraft);
      const appServerStatus = String(result.app_server?.connection_status || "");
      const appServerBlockedReason = String(result.app_server?.blocked_reason || "");
      const appServerMessage = appServerStatus === "transport_url_mismatch"
        ? appServerBlockedReason || "Codex App Server config saved, but transport and URL do not match."
        : appServerStatus === "url_secret_rejected"
          ? appServerBlockedReason || "Codex App Server URL query strings are not allowed."
        : appServerStatus === "blocked_auth_required"
          ? appServerBlockedReason || "Codex App Server config saved, but App Server auth is required."
          : "Codex App Server config saved.";
      setConnectionMessages((current) => ({
        ...current,
        codex_app_server: {
          tone: appServerStatus === "transport_url_mismatch" || appServerStatus === "blocked_auth_required" || appServerStatus === "url_secret_rejected" ? "error" : "success",
          text: appServerMessage,
        },
      }));
      refreshConnectionStatus("codex");
    } catch (errorValue) {
      setConnectionMessages((current) => ({
        ...current,
        codex_app_server: {
          tone: "error",
          text: errorValue instanceof Error ? errorValue.message : "Failed to save Codex App Server config.",
        },
      }));
    } finally {
      setConnectionBusy("");
    }
  };

  const clearCodexAppServer = async () => {
    try {
      setConnectionBusy("codex_app_server:clear");
      await settingsApiResources.clearCodexAppServerConfig();
      setConnectionMessages((current) => ({
        ...current,
        codex_app_server: { tone: "success", text: "Codex App Server config cleared." },
      }));
      refreshConnectionStatus("codex");
    } catch (errorValue) {
      setConnectionMessages((current) => ({
        ...current,
        codex_app_server: {
          tone: "error",
          text: errorValue instanceof Error ? errorValue.message : "Failed to clear Codex App Server config.",
        },
      }));
    } finally {
      setConnectionBusy("");
    }
  };

  const probeCodexAppServer = async () => {
    try {
      setConnectionBusy("codex_app_server:probe");
      const result = await settingsApiResources.probeCodexAppServer();
      const probeStatus = String(result.probe?.status ?? "unknown");
      const account = result.account && typeof result.account === "object" && !Array.isArray(result.account)
        ? result.account as Record<string, unknown>
        : {};
      const accountLabel = String(account.account_label || account.email || "");
      const accountAuthMethodLabel = String(account.auth_method_label || account.auth_method || "");
      setConnectionMessages((current) => ({
        ...current,
        codex_app_server: {
          tone: probeStatus === "ok" ? "success" : "error",
          text: accountLabel
            ? `Probe: ${probeStatus}; Codex provider via ${accountAuthMethodLabel || "account"}: ${accountLabel}`
            : `Probe: ${probeStatus}`,
        },
      }));
      refreshConnectionStatus("codex");
    } catch (errorValue) {
      setConnectionMessages((current) => ({
        ...current,
        codex_app_server: {
          tone: "error",
          text: errorValue instanceof Error ? errorValue.message : "Failed to probe Codex App Server.",
        },
      }));
    } finally {
      setConnectionBusy("");
    }
  };

  const settingsFieldAnchorId = (field: ControlCenterField) => `settings-field-${field.sourceSectionId}-${field.id}`.replace(/[^a-zA-Z0-9_-]/g, "-");
  const openSearchMatch = (sectionId: ControlCenterSection["id"], field: ControlCenterField) => {
    setActiveSectionId(sectionId);
    onOpenSection?.(sectionId);
    requestAnimationFrame(() => {
      const target = document.getElementById(settingsFieldAnchorId(field));
      target?.scrollIntoView({ block: "center", behavior: prefersReducedMotion ? "auto" : "smooth" });
      target?.querySelector<HTMLElement>("input, select, textarea, button, [tabindex]:not([tabindex='-1'])")?.focus();
    });
  };
  const renderField = (field: ControlCenterField) => (
    <div
      id={settingsFieldAnchorId(field)}
      data-settings-field={`${field.sourceSectionId}.${field.id}`}
      key={`${field.sourceSectionId}.${field.id}`}
      className={cn(
        "min-w-0 border-b border-white/[0.07] px-1 py-4 transition-colors focus-within:border-indigo-400/35",
        settingsFieldTakesFullWidth(field) ? "lg:col-span-2" : "",
      )}
    >
      {field.sourceSectionLabel && field.sourceSectionId !== activeSection?.id && (
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="rounded-md border border-white/[0.07] bg-white/[0.045] px-2 py-1 text-[10px] font-medium uppercase tracking-normal text-zinc-500">
            {field.sourceSectionLabel}
          </span>
        </div>
      )}
      <SettingsFieldRendererHost
        registry={settingsModalFieldRendererRegistry}
        componentBindings={catalog?.component_bindings ?? []}
        fallbackRenderer={SettingsFieldFallback}
        sectionId={field.sourceSectionId}
        field={field as SettingsFieldRendererProps["field"]}
        value={
          field.type === "secret" && field.configured_field
            ? settingsValues[field.sourceSectionId]?.[field.configured_field]
            : settingsValues[field.sourceSectionId]?.[field.id] ?? field.default
        }
        sectionValues={settingsValues[field.sourceSectionId] ?? {}}
        onChange={onSettingChange}
      />
    </div>
  );

  const renderSettingsPlacement = (manifest: PlacementManifest) => {
    const action = manifest.renderer.action;
    const settingsTarget = action?.type === "open_settings_section" ? action.target ?? "" : "";
    const placementLabel = localizedSettingsSourceLabel(
      String(manifest.source.sourceId ?? settingsTarget),
      manifest.label,
      locale,
    );
    const placementDescription = isJapanese
      ? "この項目を設定画面からすぐ開けるようにします。"
      : manifest.description;
    return (
      <div key={manifest.id} className="rounded-lg border border-white/[0.07] bg-white/[0.03] p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h4 className="text-sm font-medium text-zinc-100">{placementLabel}</h4>
            {placementDescription && (
              <p className="mt-1 text-xs leading-5 text-zinc-500">{placementDescription}</p>
            )}
          </div>
          <button
            type="button"
            onClick={() => updatePinnedPlacements((current) => togglePinnedPlacement(current, { id: manifest.id, surface: "settings" }))}
            className="rounded-md border border-zinc-800 px-2 py-1 text-[11px] text-zinc-400 hover:border-zinc-600 hover:text-zinc-200"
          >
            {isJapanese ? "表示を外す" : "Unpin"}
          </button>
        </div>
        {manifest.renderer.kind === "html" ? (
          <div className="mt-3 h-44 overflow-hidden rounded-lg">
            <PlacementHtmlRenderer manifest={manifest} />
          </div>
        ) : action?.type === "open_settings_section" && settingsTarget ? (
          <button
            type="button"
            onClick={() => openSection(settingsTarget)}
            className="mt-3 rounded-lg border border-zinc-700 bg-zinc-950/35 px-3 py-2 text-xs text-zinc-200 hover:border-zinc-500"
          >
            このセクションを開く
          </button>
        ) : (
          <p className="mt-3 text-xs text-zinc-500">
            {isJapanese ? "この項目は設定画面で利用できます。" : "This item is available in Settings."}
          </p>
        )}
      </div>
    );
  };

  const renderSectionPrelude = (section: ControlCenterSection): ReactElement | null => {
    if (section.id === "profiles") {
      return (
        <ProfileSettingsPanel
          workspace={profileWorkspace}
          locale={locale}
          loadState={modelProfilesLoadState}
          saveState={saveState}
          requestedProfileId={profileSelectionRequest?.id}
          selectionRequestVersion={profileSelectionRequest?.version}
          onSettingChange={onSettingChange}
          onOpenSection={openSection}
          onRetryLoad={onRetryLoad}
        />
      );
    }
    if (section.id === "quick_setup") {
      return (
        <section className="overflow-hidden rounded-2xl border border-indigo-300/15 bg-[#0b0d10] shadow-[0_24px_90px_rgba(0,0,0,0.28)]">
          <div className="grid gap-6 p-5 sm:p-7 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
            <div className="flex min-w-0 gap-4">
              <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl border border-indigo-300/20 bg-indigo-300/10 text-indigo-100">
                <MessageCircle size={20} />
              </span>
              <div className="min-w-0">
                <h4 className="text-base font-semibold text-zinc-50">
                  {localizedCopy("Configure with AI", "AIと設定する")}
                </h4>
                <p className="mt-2 max-w-2xl text-xs leading-5 text-zinc-400">
                  {localizedCopy(
                    "Open the normal home chat with the Settings skill mentioned. Ask questions, compare options, and approve changes without learning a separate interface.",
                    "通常のホームチャットに設定用Skillをメンションします。設定場所の質問、比較、変更の確認まで、いつもの入力欄で続けられます。",
                  )}
                </p>
                <span className="mt-3 inline-flex items-center rounded-full border border-cyan-400/20 bg-cyan-400/[0.07] px-2.5 py-1 font-mono text-[11px] text-cyan-100">
                  @Settings
                </span>
              </div>
            </div>
            <button
              type="button"
              onClick={onStartSettingsChat}
              disabled={!onStartSettingsChat}
              className="inline-flex min-h-11 items-center justify-center gap-2 rounded-xl bg-zinc-100 px-4 py-2.5 text-xs font-semibold text-zinc-950 transition-colors hover:bg-white disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
            >
              {localizedCopy("Open Settings Mode", "Settings Modeを開く")}
              <ArrowRight size={15} />
            </button>
          </div>
          <div className="border-t border-white/[0.07] bg-white/[0.018] px-5 py-3 text-[11px] leading-5 text-zinc-500 sm:px-7">
            {localizedCopy(
              "Remove the @Settings mention from the composer to return to regular Tobkiri mode.",
              "入力欄から @Settings メンションを削除すると、通常のTobkiriへ戻ります。",
            )}
          </div>
        </section>
      );
    }
    if (
      section.id === "accounts_connections"
      || (section.id === "tools_mcp" && mapSettingsSectionId(requestedSectionId) === "accounts_connections")
    ) {
      const connectedCount = accountConnectionCards.filter((card) => card.connected || card.credential?.configured).length;
      const approvalCount = accountConnectionCards.reduce((sum, card) => sum + card.approvalRequiredCapabilities.length, 0);
      const blockedCount = accountConnectionCards.filter((card) => card.disabledReason && !card.connected && !card.credential?.configured).length;
      return (
        <div className="space-y-4">
          <div className="overflow-hidden rounded-xl border border-white/[0.07] bg-white/[0.03]">
            <div className="px-4 py-4 sm:px-5">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="max-w-2xl">
                  <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-cyan-200/80">{localizedCopy("Accounts & Connections", "アカウントと接続")}</div>
                  <h3 className="mt-2 text-base font-semibold text-zinc-50">{localizedCopy("Manage sign-in, credentials, and permissions separately", "ログイン、認証情報、権限を分けて管理します")}</h3>
                  <p className="mt-2 text-xs leading-5 text-zinc-400">
                    {localizedCopy("OAuth and API tokens stay in secret storage. Settings shows only connection state and the permissions Rumi may request.", "OAuthやAPIのトークンは秘密情報ストレージへ保存します。この画面には接続状態と、Rumiが利用を求める権限だけを表示します。")}
                  </p>
                </div>
                <div className="grid min-w-[220px] grid-cols-3 gap-2 text-center text-[11px]">
                  <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-zinc-200">
                    <div className="text-base font-semibold">{connectedCount}</div>
                    <div className="text-[10px] text-emerald-200/70">{localizedCopy("connected", "接続済み")}</div>
                  </div>
                  <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-zinc-200">
                    <div className="text-base font-semibold">{approvalCount}</div>
                    <div className="text-[10px] text-amber-100/70">{localizedCopy("approval", "承認待ち")}</div>
                  </div>
                  <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 px-3 py-2 text-zinc-200">
                    <div className="text-base font-semibold">{blockedCount}</div>
                    <div className="text-[10px] text-zinc-500">{localizedCopy("needs setup", "設定が必要")}</div>
                  </div>
                </div>
              </div>
            </div>
            <div className="grid gap-3 border-t border-zinc-800 px-4 py-3 text-[11px] text-zinc-500 sm:grid-cols-3 sm:px-5">
              <div><span className="text-zinc-300">{localizedCopy("1. Connect", "1. 接続")}</span> — {localizedCopy("Use browser sign-in or import a credential bundle.", "ブラウザでログインするか、認証情報セットを読み込みます。")}</div>
              <div><span className="text-zinc-300">{localizedCopy("2. Store", "2. 保存")}</span> — {localizedCopy("Raw secrets stay in Rumi secret storage.", "秘密情報そのものはTobkiriの秘密情報ストレージに保存します。")}</div>
              <div><span className="text-zinc-300">{localizedCopy("3. Govern", "3. 権限管理")}</span> — {localizedCopy("High-risk capabilities require approval.", "影響の大きい操作には承認が必要です。")}</div>
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            {accountConnectionCards.map((card) => {
              const oauthReview = connectionOAuthReviews[card.providerId];
              const draftReview = connectionDraftReviews[card.providerId];
              const isBusy = connectionBusy === `${card.providerId}:start` || Boolean(oauthReview);
              const jsonBusy = connectionBusy === `${card.providerId}:save_json`;
              const message = connectionMessages[card.providerId];
              const selectedScopeOption = selectedConnectionScopeMode(card);
              const selectedScopeModeId = selectedScopeOption?.id ?? card.scopeMode ?? "";
              const selectedScopes = selectedScopeOption?.scopes.length ? selectedScopeOption.scopes : card.scopes;
              const hasPermissionSummary = selectedScopes.length > 0 || card.credentialRef || card.capabilities.length > 0 || card.approvalRequiredCapabilities.length > 0 || card.rejectedCapabilities.length > 0 || card.expiresAt;
              const cloudflareRows = card.providerId === "cloudflare" ? cloudflareProvisioningRows(card.provisioning, isJapanese) : [];
              const cloudflareFacts = card.providerId === "cloudflare" ? cloudflareProvisioningFacts(card.provisioning, isJapanese) : [];
              const cloudflareBlockers = card.providerId === "cloudflare" ? cloudflareProvisioningBlockers(card.provisioning, isJapanese) : [];
              const expanded = expandedConnectionProviderId === card.providerId;
              const brandAsset = providerBrandAsset(card.providerId);
              return (
                <article key={card.providerId} className="relative overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950/55">
                  <div className="p-4 sm:p-5">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex min-w-0 gap-3">
                        {brandAsset ? (
                          <img
                            src={brandAsset}
                            alt=""
                            aria-hidden="true"
                            className="h-9 w-9 shrink-0 rounded-lg border border-white/[0.09] bg-white object-cover"
                          />
                        ) : (
                          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-white/[0.09] bg-white/[0.04] text-xs font-semibold text-zinc-300">
                            {card.label.slice(0, 2).toUpperCase()}
                          </div>
                        )}
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <h4 className="text-sm font-semibold text-zinc-50">{card.label}</h4>
                            <span className={cn("rounded-full border px-2 py-0.5 text-[10px] font-medium", statusBadgeClass(card.status, card.connected, card.canConnect))}>
                              {card.statusLabel}
                            </span>
                          </div>
                          <p className="mt-1 text-xs leading-5 text-zinc-500">{card.description}</p>
                        </div>
                      </div>
                      <button
                        type="button"
                        aria-expanded={expanded}
                        aria-controls={`settings-connection-${card.providerId}`}
                        onClick={() => setExpandedConnectionProviderId(expanded ? "" : card.providerId)}
                        className="shrink-0 rounded-lg border border-zinc-800 bg-black/20 px-3 py-2 text-xs text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-100"
                      >
                        {expanded ? localizedCopy("Hide details", "詳細を閉じる") : localizedCopy("Open details", "詳細を開く")}
                      </button>
                    </div>

                    <div className="mt-4 grid gap-2 sm:grid-cols-3">
                      <div className="rounded-xl border border-zinc-800 bg-black/20 px-3 py-2">
                        <div className="text-[10px] uppercase tracking-[0.14em] text-zinc-600">{localizedCopy("Identity", "ログイン")}</div>
                        <div className="mt-1 text-xs text-zinc-200">{card.connected ? localizedCopy("Connected", "接続済み") : card.canConnect ? localizedCopy("Ready", "接続できます") : localizedCopy("Needs setup", "設定が必要")}</div>
                      </div>
                      <div className="rounded-xl border border-zinc-800 bg-black/20 px-3 py-2">
                        <div className="text-[10px] uppercase tracking-[0.14em] text-zinc-600">{localizedCopy("Credential", "認証情報")}</div>
                        <div className="mt-1 break-all text-xs text-zinc-200">{card.credentialRef ? compactCredentialRef(card.credentialRef) : card.credential?.configured ? localizedCopy("Stored", "保存済み") : localizedCopy("Not stored", "未保存")}</div>
                      </div>
                      <div className="rounded-xl border border-zinc-800 bg-black/20 px-3 py-2">
                        <div className="text-[10px] uppercase tracking-[0.14em] text-zinc-600">{localizedCopy("Permission", "利用権限")}</div>
                        <div className="mt-1 text-xs text-zinc-200">{card.approvalRequiredCapabilities.length ? localizedCopy("Approval needed", "承認が必要") : card.capabilities.length ? localizedCopy("Granted", "許可済み") : localizedCopy("Limited", "制限あり")}</div>
                      </div>
                    </div>

                    <div
                      id={`settings-connection-${card.providerId}`}
                      hidden={!expanded}
                      className={cn(!expanded && "hidden")}
                    >

                    {card.providerId === "cloudflare" && (cloudflareRows.length > 0 || cloudflareFacts.length > 0 || cloudflareBlockers.length > 0) && (
                      <div className="mt-4 rounded-xl border border-zinc-800 bg-black/20 p-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="text-xs font-medium text-zinc-200">{localizedCopy("Cloudflare runtime", "Cloudflare実行環境")}</div>
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="rounded-full border border-zinc-800 px-2 py-0.5 text-[10px] text-zinc-500">{localizedCopy("Sandbox + PC bridge", "実行環境とPC接続")}</span>
                            <button
                              type="button"
                              onClick={() => {
                                setConnectionMessages((current) => ({
                                  ...current,
                                  [card.providerId]: { tone: "success", text: "Cloudflare diagnostics requested." },
                                }));
                                refreshConnectionStatus(card.providerId, true);
                              }}
                              className="rounded-lg border border-zinc-700 px-2 py-1 text-[10px] text-zinc-300 transition-colors hover:border-zinc-500 hover:text-zinc-100"
                            >
                              {localizedCopy("Run diagnostics", "診断を実行")}
                            </button>
                          </div>
                        </div>
                        {cloudflareRows.length > 0 && (
                          <div className="mt-3 grid gap-2 sm:grid-cols-4">
                            {cloudflareRows.map((row) => (
                              <div key={row.label} className="rounded-lg border border-zinc-800 bg-zinc-950 px-2.5 py-2">
                                <div className="text-[10px] uppercase tracking-[0.14em] text-zinc-600">{row.label}</div>
                                <div className={cn("mt-1 text-xs", row.ready ? "text-emerald-200" : "text-amber-100")}>{row.value}</div>
                              </div>
                            ))}
                          </div>
                        )}
                        {cloudflareFacts.length > 0 && (
                          <div className="mt-3 flex flex-wrap gap-1.5">
                            {cloudflareFacts.map((fact) => (
                              <span key={fact} className={cn("rounded-full border px-2 py-0.5 text-[10px]", capabilityToneClass("neutral"))}>{fact}</span>
                            ))}
                          </div>
                        )}
                        {cloudflareBlockers.length > 0 && (
                          <div className="mt-3 space-y-1.5">
                            {cloudflareBlockers.map((blocker) => (
                              <div key={blocker} className="rounded-lg border border-amber-500/25 bg-amber-500/10 px-2.5 py-1.5 text-[11px] leading-5 text-amber-100/85">{blocker}</div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                    {card.scopeModes.length > 0 && (
                      <div className="mt-4 rounded-xl border border-zinc-800 bg-black/20 p-3" role="radiogroup" aria-label={`${card.label} ${localizedCopy("OAuth permission mode", "OAuth権限の選択")}`}>
                        <div className="mb-2 flex items-center justify-between gap-2">
                          <div className="text-xs font-medium text-zinc-200">{localizedCopy("Choose permission mode", "利用する権限を選択")}</div>
                          <div className="text-[10px] text-zinc-600">{localizedCopy("before browser connect", "ブラウザで接続する前に選びます")}</div>
                        </div>
                        <div className="grid gap-2">
                          {card.scopeModes.map((mode) => {
                            const selected = mode.id === selectedScopeModeId;
                            return (
                              <label
                                key={mode.id}
                                className={cn(
                                  "block cursor-pointer rounded-lg border px-3 py-2 transition-colors",
                                  selected
                                    ? "border-cyan-500/60 bg-cyan-500/10"
                                    : "border-zinc-800 bg-zinc-950 hover:border-zinc-700",
                                )}
                              >
                                <input
                                  type="radio"
                                  name={`oauth-scope-mode-${card.providerId}`}
                                  value={mode.id}
                                  checked={selected}
                                  onChange={() => setConnectionScopeModes((current) => ({ ...current, [card.providerId]: mode.id }))}
                                  className="sr-only"
                                />
                                <span className="flex flex-wrap items-center gap-2">
                                  <span className="text-xs font-medium text-zinc-100">{mode.label}</span>
                                  {mode.restricted && <span className={cn("rounded-full border px-2 py-0.5 text-[10px]", capabilityToneClass("approval"))}>{localizedCopy("Restricted", "制限付き")}</span>}
                                </span>
                                <span className="mt-1 block text-[11px] leading-5 text-zinc-500">{mode.description}</span>
                                {mode.restricted && mode.warning && (
                                  <span className="mt-2 block rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-[11px] leading-5 text-amber-100/80">{mode.warning}</span>
                                )}
                              </label>
                            );
                          })}
                        </div>
                      </div>
                    )}

                    {hasPermissionSummary && (
                      <div className="mt-4 rounded-xl border border-zinc-800 bg-zinc-950/80 p-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div className="text-xs font-medium text-zinc-200">{localizedCopy("Resolved permission state", "現在の利用権限")}</div>
                          {card.expiresAt && <div className="text-[10px] text-zinc-600">{localizedCopy("expires", "有効期限")} {card.expiresAt}</div>}
                        </div>
                        {card.credentialRef && <div className="mt-2 text-[11px] text-zinc-500">{localizedCopy("Stored credential", "保存済みの認証情報")}: <span className="font-mono text-zinc-300">{compactCredentialRef(card.credentialRef)}</span></div>}
                        <div className="mt-3 space-y-2">
                          {selectedScopes.length > 0 && (
                            <div>
                              <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-zinc-600">{localizedCopy("Selected permissions", "選択した権限")}</div>
                              <div className="flex flex-wrap gap-1.5">{selectedScopes.map((scope) => <span key={scope} className={cn("rounded-full border px-2 py-0.5 text-[10px]", capabilityToneClass("scope"))}>{scope}</span>)}</div>
                            </div>
                          )}
                          {card.capabilities.length > 0 && (
                            <div>
                              <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-zinc-600">{localizedCopy("Enabled capabilities", "利用できる操作")}</div>
                              <div className="flex flex-wrap gap-1.5">{card.capabilities.map((capability) => <span key={capability} className={cn("rounded-full border px-2 py-0.5 text-[10px]", capabilityToneClass("enabled"))}>{capability}</span>)}</div>
                            </div>
                          )}
                          {card.approvalRequiredCapabilities.length > 0 && (
                            <div>
                              <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-amber-300/70">{localizedCopy("Needs approval", "承認が必要")}</div>
                              <div className="flex flex-wrap gap-1.5">{card.approvalRequiredCapabilities.map((capability) => <span key={capability} className={cn("rounded-full border px-2 py-0.5 text-[10px]", capabilityToneClass("approval"))}>{capability}</span>)}</div>
                            </div>
                          )}
                          {card.rejectedCapabilities.length > 0 && (
                            <div>
                              <div className="mb-1 text-[10px] uppercase tracking-[0.14em] text-rose-300/70">{localizedCopy("Not granted", "許可されていません")}</div>
                              <div className="flex flex-wrap gap-1.5">{card.rejectedCapabilities.map((capability) => <span key={capability} className={cn("rounded-full border px-2 py-0.5 text-[10px]", capabilityToneClass("rejected"))}>{capability}</span>)}</div>
                            </div>
                          )}
                        </div>
                      </div>
                    )}

                    {card.credential ? (
                      <div className="mt-4 rounded-xl border border-violet-500/20 bg-violet-500/10 p-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="text-xs font-medium text-violet-100">{localizedCopy("Codex access token", "Codexアクセストークン")}</div>
                            <p className="mt-1 text-[11px] leading-5 text-violet-100/65">{localizedCopy("This is separate from a model API key or App Server authentication and is stored only on this device.", "モデル用APIキーやApp Serverの認証とは別の情報として、この端末だけに保存します。")}</p>
                          </div>
                          <span className={cn("rounded-full border px-2 py-0.5 text-[10px]", card.credential.configured ? capabilityToneClass("enabled") : capabilityToneClass("approval"))}>{card.credential.configured ? localizedCopy("Saved", "保存済み") : localizedCopy("Missing", "未設定")}</span>
                        </div>
                        <input
                          type="password"
                          autoComplete="off"
                          value={connectionCredentialDrafts[card.providerId] ?? ""}
                          onChange={(event) => {
                            setConnectionCredentialDrafts((current) => ({ ...current, [card.providerId]: event.target.value }));
                            setConnectionDraftReviews((current) => {
                              const next = { ...current };
                              delete next[card.providerId];
                              return next;
                            });
                          }}
                          placeholder={card.credential.placeholder}
                          className="mt-3 h-10 w-full rounded-lg border border-zinc-800 bg-black px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-violet-500"
                        />
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button type="button" disabled={connectionBusy === `${card.providerId}:save_credential`} onClick={() => void saveConnectionCredential(card)} className="rounded-lg border border-violet-500/50 bg-violet-500/15 px-3 py-1.5 text-xs text-violet-100 transition-colors hover:border-violet-400 disabled:cursor-not-allowed disabled:border-zinc-800 disabled:bg-zinc-900 disabled:text-zinc-600">
                            {connectionBusy === `${card.providerId}:save_credential` ? localizedCopy("Saving...", "保存中...") : card.credential.saveLabel}
                          </button>
                          <button type="button" disabled={!card.credential.canClear || connectionBusy === `${card.providerId}:clear_credential`} onClick={() => void clearConnectionCredential(card)} className="rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200 disabled:cursor-not-allowed disabled:text-zinc-700">
                            {connectionBusy === `${card.providerId}:clear_credential` ? localizedCopy("Clearing...", "削除中...") : card.credential.clearLabel}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="mt-4 rounded-xl border border-zinc-800 bg-black/20 p-3">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <div>
                            <div className="text-xs font-medium text-zinc-200">{localizedCopy("Credential bundle / client config", "認証情報セット・クライアント設定")}</div>
                            <p className="mt-1 text-[11px] leading-5 text-zinc-500">{connectionDraftHelp(card.providerId, isJapanese ? "ja" : "en")}</p>
                          </div>
                          <span className="rounded-full border border-zinc-800 px-2 py-0.5 text-[10px] text-zinc-500">{localizedCopy("Secrets are never shown again", "秘密情報は再表示しません")}</span>
                        </div>
                        <textarea
                          value={connectionCredentialDrafts[card.providerId] ?? ""}
                          onChange={(event) => {
                            setConnectionCredentialDrafts((current) => ({ ...current, [card.providerId]: event.target.value }));
                            setConnectionDraftReviews((current) => {
                              const next = { ...current };
                              delete next[card.providerId];
                              return next;
                            });
                          }}
                          placeholder={importPlaceholderForProvider(card.providerId, isJapanese ? "ja" : "en")}
                          spellCheck={false}
                          className="mt-3 min-h-28 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-[11px] leading-5 text-zinc-100 outline-none placeholder:text-zinc-700 focus:border-cyan-600"
                        />
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button type="button" disabled={jsonBusy || !String(connectionCredentialDrafts[card.providerId] ?? "").trim()} onClick={() => void saveAccountConnectionJson(card)} className="rounded-lg border border-zinc-100 bg-zinc-100 px-3 py-1.5 text-xs text-zinc-950 transition-colors hover:bg-white disabled:cursor-not-allowed disabled:border-zinc-800 disabled:bg-zinc-900 disabled:text-zinc-600">
                            {jsonBusy ? localizedCopy("Saving...", "保存中...") : localizedCopy("Import credential JSON / save client", "認証情報を読み込んで保存")}
                          </button>
                          <button type="button" disabled={!card.canConnect || isBusy} onClick={() => void startAccountConnection(card, selectedScopeOption)} title={card.disabledReason || `${card.primaryLabel}${selectedScopeOption ? ` using ${selectedScopeOption.label}` : ""}`} className={cn("rounded-lg border px-3 py-1.5 text-xs transition-colors", !card.canConnect || isBusy ? "cursor-not-allowed border-zinc-800 bg-zinc-900 text-zinc-600" : "border-cyan-700 bg-cyan-950/30 text-cyan-100 hover:border-cyan-500 hover:bg-cyan-900/35")}>
                            {isBusy ? localizedCopy("Opening...", "接続を開いています...") : card.primaryLabel}
                          </button>
                          <button type="button" onClick={() => openSection(card.configureSectionId)} className="rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 hover:border-zinc-600 hover:text-zinc-200">
                            {card.configureLabel}
                          </button>
                        </div>
                        {draftReview && (
                          <div className="mt-3 rounded-lg border border-violet-500/35 bg-violet-500/10 p-3 text-[11px] leading-5 text-violet-100" role="status">
                            <div className="font-medium">{localizedCopy("Review before saving", "保存前の確認")}</div>
                            <p className="mt-1 text-violet-100/75">{draftReview.kind === "connection_import" ? localizedCopy("Credential import", "認証情報の読み込み") : localizedCopy("OAuth client configuration", "OAuthクライアント設定")} — {localizedCopy(`${draftReview.secretFieldCount} secret field(s) are redacted.`, `秘密情報の項目 ${draftReview.secretFieldCount} 件は表示しません。`)}</p>
                            {draftReview.fields.length > 0 && <p className="mt-1 text-violet-100/75">{localizedCopy("Non-secret fields", "秘密情報以外の項目")}: {draftReview.fields.join(", ")}</p>}
                            {draftReview.endpoints.length > 0 && <p className="mt-1 text-violet-100/75">HTTPS endpoints: {draftReview.endpoints.join(", ")}</p>}
                            {draftReview.scopes.length > 0 && <p className="mt-1 text-violet-100/75">{localizedCopy("Requested scopes", "要求される権限")}: {draftReview.scopes.join(", ")}</p>}
                            <p className="mt-2 text-amber-100/85">{localizedCopy("Saving may replace the existing local connection. This action remains subject to local approval and audit policy.", "保存すると既存のローカル接続を置き換える場合があります。ローカルの承認・監査ポリシーはこの画面では変更されません。")}</p>
                            <div className="mt-3 flex flex-wrap gap-2">
                              <button type="button" disabled={jsonBusy} onClick={() => void confirmAccountConnectionJson(card)} className="rounded border border-violet-300 bg-violet-100 px-2.5 py-1.5 text-xs font-medium text-zinc-950 disabled:opacity-50">{jsonBusy ? localizedCopy("Saving...", "保存中...") : localizedCopy("Confirm and save", "確認して保存")}</button>
                              <button type="button" onClick={() => setConnectionDraftReviews((current) => { const next = { ...current }; delete next[card.providerId]; return next; })} className="rounded border border-violet-300/40 px-2.5 py-1.5 text-xs text-violet-100">{localizedCopy("Keep editing", "編集を続ける")}</button>
                            </div>
                          </div>
                        )}
                        {oauthReview && (
                          <div className="mt-3 rounded-lg border border-amber-500/35 bg-amber-500/10 p-3 text-[11px] leading-5 text-amber-50" role="status">
                            <div className="font-medium">{localizedCopy("Review external authorization", "外部認可ページの確認")}</div>
                            <p className="mt-1 break-all text-amber-100/80">{oauthReview.host}{oauthReview.path}</p>
                            <p className="mt-1 text-amber-100/75">{localizedCopy("Choose the expected provider account. Opening this page does not mean the connection completed.", "想定したプロバイダーアカウントを選択してください。このページを開いても接続完了ではありません。")}</p>
                            {oauthReview.scopes.length > 0 && <p className="mt-1 text-amber-100/75">{localizedCopy("Requested scopes", "要求される権限")}: {oauthReview.scopes.join(", ")}</p>}
                            <div className="mt-3 flex flex-wrap gap-2">
                              <button type="button" onClick={() => confirmAccountConnectionOAuth(card)} className="rounded border border-amber-300 bg-amber-100 px-2.5 py-1.5 text-xs font-medium text-zinc-950">{localizedCopy("Open reviewed provider page", "確認したページを開く")}</button>
                              <button type="button" onClick={() => cancelAccountConnectionOAuth(card)} className="rounded border border-amber-300/40 px-2.5 py-1.5 text-xs text-amber-50">{localizedCopy("Cancel", "キャンセル")}</button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}

                    {card.disabledReason && !card.connected && !card.credential?.configured && (
                      <div className="mt-4 grid gap-2 sm:grid-cols-2">
                        <p className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-[11px] leading-5 text-zinc-500">{card.officialAppDescription}</p>
                        <p className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-[11px] leading-5 text-zinc-500">{card.selfHostDescription}</p>
                      </div>
                    )}
                    {message && (
                      <p className={cn("mt-4 rounded-lg border px-3 py-2 text-[11px] leading-5", message.tone === "success" ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-200" : "border-rose-500/25 bg-rose-500/10 text-rose-200")}>{message.text}</p>
                    )}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>

          <div className="rounded-2xl border border-sky-500/20 bg-sky-500/10 p-4">
            <div className="text-sm font-medium text-sky-100">{localizedCopy("Open-source / official app", "オープンソース版・公式アプリ")}</div>
            <p className="mt-1 text-xs leading-5 text-sky-100/75">
              {localizedCopy("Official secrets are not bundled. The official app can provide hosted sign-in, while self-hosted installations can import credentials or configure their own OAuth client.", "公式の秘密情報はアプリに同梱しません。公式アプリではホスト型ログインを利用でき、セルフホスト版では認証情報の読み込みや独自OAuthクライアントの設定ができます。")}
            </p>
          </div>
        </div>
      );
    }
    if (section.id === "tools_mcp") {
      const appServerMessage = connectionMessages.codex_app_server;
      const appServerTransportOptions: Array<{ value: NonNullable<CodexAppServerConfig["transport"]>; label: string; detail: string }> = [
        { value: "off", label: "Off", detail: "Disable Codex App Server integration." },
        { value: "stdio", label: "stdio", detail: "Local process transport. Best default for trusted desktop use." },
        { value: "unix", label: "Unix socket", detail: "Local socket transport with file-system boundaries." },
        { value: "websocket_loopback", label: "WebSocket loopback", detail: "Only localhost / 127.0.0.1 / ::1 endpoints." },
        { value: "websocket_remote", label: "WebSocket remote", detail: "Requires separate App Server auth before use." },
      ];
      const appServerToggleFields: Array<["enabled" | "toolSourceEnabled" | "automationEndpointEnabled", string, string]> = [
        ["enabled", "Enabled", "Allow Rumi to use this App Server configuration."],
        ["toolSourceEnabled", "Tool source", "Expose threads, turns, approvals, and events as tool capabilities."],
        ["automationEndpointEnabled", "Automation endpoint", "Show readiness in Computer & Automation."],
      ];
      const appServerBlocked = Boolean(codexAppServerPrelude.blockedReason);
      return (
        <div className="space-y-4">
          <div className="grid gap-4 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
            <div className="space-y-4">
              <div className="rounded-xl border border-zinc-800 bg-zinc-950/45 p-4">
                <div className="text-[11px] font-medium text-zinc-500">Tools & MCP</div>
                <h3 className="mt-1 text-sm font-semibold text-zinc-50">ツールとログインは別に管理されます</h3>
                <p className="mt-2 text-xs leading-5 text-zinc-500">MCP servers and tool sources define callable actions. Account login, OAuth tokens, and access tokens remain in Accounts & Connections.</p>
                <div className="mt-4 grid gap-2 text-[11px]">
                  <div className="rounded-lg border border-zinc-800 bg-black/20 px-3 py-2"><span className="text-zinc-300">Credential</span> → Accounts & Connections</div>
                  <div className="rounded-lg border border-zinc-800 bg-black/20 px-3 py-2"><span className="text-zinc-300">Tool source</span> → Tools & MCP</div>
                  <div className="rounded-lg border border-zinc-800 bg-black/20 px-3 py-2"><span className="text-zinc-300">Readiness</span> → Computer & Automation</div>
                </div>
              </div>
              <div className="rounded-xl border border-amber-500/20 bg-amber-500/8 p-4">
                <div className="flex items-center gap-2 text-sm font-medium text-amber-100"><AlertTriangle className="h-4 w-4" /> Safety rules</div>
                <ul className="mt-3 space-y-2 text-[11px] leading-5 text-amber-100/75">
                  <li>Remote WebSocket requires a separate App Server token or shared secret.</li>
                  <li>Codex access token is never reused as App Server auth.</li>
                  <li>URL query strings are rejected so secrets cannot leak into CLI args or logs.</li>
                </ul>
              </div>
            </div>

            <div className="relative overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950/55">
              <div className="p-4 sm:p-5">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div>
                    <div className="text-sm font-semibold text-zinc-50">Codex App Server</div>
                    <p className="mt-1 text-xs leading-5 text-zinc-500">Use Codex as a local/rich agent integration for threads, turns, streamed events, and approvals.</p>
                    {codexAppServerPrelude.accountLabel && (
                      <p className="mt-2 max-w-full break-all text-xs leading-5 text-emerald-300/90">
                        Connected Codex provider via {codexAppServerPrelude.accountAuthMethodLabel || "account"}: {codexAppServerPrelude.accountLabel}
                        {codexAppServerPrelude.accountPlanType && <span className="text-emerald-300/65"> ({codexAppServerPrelude.accountPlanType})</span>}
                      </p>
                    )}
                  </div>
                  <span className={cn("rounded-full border px-2 py-0.5 text-[10px] font-medium", statusBadgeClass(codexAppServerPrelude.status, codexAppServerPrelude.configured, !appServerBlocked))}>{codexAppServerPrelude.statusLabel}</span>
                </div>

                <div className="mt-4 grid gap-3 md:grid-cols-2">
                  <div className="space-y-2">
                    <div className="text-[10px] uppercase tracking-[0.14em] text-zinc-600">Transport</div>
                    <div className="grid gap-2">
                      {appServerTransportOptions.map((option) => {
                        const selected = codexAppServerDraft.transport === option.value;
                        return (
                          <button
                            key={option.value}
                            type="button"
                            onClick={() => setCodexAppServerDraft((current) => ({ ...current, transport: option.value, enabled: option.value === "off" ? false : current.enabled }))}
                            className={cn("rounded-lg border px-3 py-2 text-left transition-colors", selected ? "border-zinc-500 bg-zinc-800/70" : "border-zinc-800 bg-black/20 hover:border-zinc-700")}
                          >
                            <div className="text-xs font-medium text-zinc-100">{option.label}</div>
                            <div className="mt-0.5 text-[11px] leading-4 text-zinc-500">{option.detail}</div>
                          </button>
                        );
                      })}
                    </div>
                  </div>
                  <div className="rounded-xl border border-zinc-800 bg-black/20 p-3">
                    <div className="text-[10px] uppercase tracking-[0.14em] text-zinc-600">Current state</div>
                    <div className="mt-3 grid gap-2 text-[11px]">
                      <div className="flex justify-between gap-2"><span className="text-zinc-500">Transport</span><span className="text-zinc-200">{codexAppServerPrelude.transport}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-zinc-500">Network</span><span className="text-zinc-200">{codexAppServerPrelude.loopback ? "Loopback" : "Remote"}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-zinc-500">Auth</span><span className={codexAppServerPrelude.authRequired && !codexAppServerPrelude.authConfigured ? "text-amber-200" : "text-zinc-200"}>{codexAppServerPrelude.authConfigured ? `${codexAppServerPrelude.authKind || "auth"} via ${codexAppServerPrelude.authSource}` : codexAppServerPrelude.authRequired ? "Required" : "Not required"}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-zinc-500">Tool source</span><span className="text-zinc-200">{codexAppServerPrelude.toolSourceStatus}</span></div>
                      <div className="flex justify-between gap-2"><span className="text-zinc-500">Automation</span><span className="text-zinc-200">{codexAppServerPrelude.automationEndpointStatus}</span></div>
                    </div>
                  </div>
                </div>

                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <label className="space-y-1 text-[11px] text-zinc-500"><span>Unix socket path</span><input value={codexAppServerDraft.unixSocketPath ?? ""} onChange={(event) => setCodexAppServerDraft((current) => ({ ...current, unixSocketPath: event.target.value }))} placeholder="/tmp/rumi-codex.sock" className="h-10 w-full rounded-lg border border-zinc-800 bg-black px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-cyan-700" /></label>
                  <label className="space-y-1 text-[11px] text-zinc-500"><span>Base URL</span><input value={codexAppServerDraft.baseUrl ?? ""} onChange={(event) => setCodexAppServerDraft((current) => ({ ...current, baseUrl: event.target.value }))} placeholder="http://127.0.0.1:7331" className="h-10 w-full rounded-lg border border-zinc-800 bg-black px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-cyan-700" /></label>
                  <label className="space-y-1 text-[11px] text-zinc-500"><span>WebSocket URL</span><input value={codexAppServerDraft.websocketUrl ?? ""} onChange={(event) => setCodexAppServerDraft((current) => ({ ...current, websocketUrl: event.target.value }))} placeholder="ws://127.0.0.1:7331/ws" className="h-10 w-full rounded-lg border border-zinc-800 bg-black px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-cyan-700" /></label>
                  <label className="space-y-1 text-[11px] text-zinc-500"><span>WS token file</span><input value={codexAppServerDraft.wsTokenFile ?? ""} onChange={(event) => setCodexAppServerDraft((current) => ({ ...current, wsTokenFile: event.target.value }))} placeholder="~/.config/rumi/codex-app-server.token" className="h-10 w-full rounded-lg border border-zinc-800 bg-black px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-cyan-700" /></label>
                  <label className="space-y-1 text-[11px] text-zinc-500 sm:col-span-2"><span>Shared secret file</span><input value={codexAppServerDraft.sharedSecretFile ?? ""} onChange={(event) => setCodexAppServerDraft((current) => ({ ...current, sharedSecretFile: event.target.value }))} placeholder="~/.config/rumi/codex-app-server.secret" className="h-10 w-full rounded-lg border border-zinc-800 bg-black px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-cyan-700" /></label>
                </div>

                <div className="mt-4 grid gap-2 sm:grid-cols-3">
                  {appServerToggleFields.map(([key, label, detail]) => (
                    <label key={key} className="rounded-xl border border-zinc-800 bg-black/20 px-3 py-2 text-xs text-zinc-300">
                      <span className="flex items-center gap-2"><input type="checkbox" checked={codexAppServerDraft[key]} onChange={(event) => setCodexAppServerDraft((current) => ({ ...current, [key]: event.target.checked }))} className="h-3.5 w-3.5 rounded border-zinc-700 bg-zinc-950 text-cyan-500" />{label}</span>
                      <span className="mt-1 block text-[10px] leading-4 text-zinc-600">{detail}</span>
                    </label>
                  ))}
                </div>

                {codexAppServerPrelude.blockedReason && <p className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] leading-5 text-amber-100/80">{codexAppServerPrelude.blockedReason}</p>}
                <div className="mt-4 flex flex-wrap gap-2">
                  <button type="button" disabled={connectionBusy === "codex_app_server:save"} onClick={() => void saveCodexAppServer()} className="rounded-lg border border-cyan-700 bg-cyan-950/30 px-3 py-1.5 text-xs text-cyan-100 transition-colors hover:border-cyan-500 hover:bg-cyan-900/35 disabled:cursor-not-allowed disabled:border-zinc-800 disabled:bg-zinc-900 disabled:text-zinc-600">{connectionBusy === "codex_app_server:save" ? "Saving..." : "Save config"}</button>
                  <button type="button" disabled={connectionBusy === "codex_app_server:probe"} onClick={() => void probeCodexAppServer()} className="rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200 disabled:cursor-not-allowed disabled:text-zinc-700">{connectionBusy === "codex_app_server:probe" ? "Probing..." : "Probe"}</button>
                  <button type="button" disabled={connectionBusy === "codex_app_server:clear"} onClick={() => void clearCodexAppServer()} className="rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200 disabled:cursor-not-allowed disabled:text-zinc-700">{connectionBusy === "codex_app_server:clear" ? "Clearing..." : "Clear"}</button>
                </div>
                {appServerMessage && <p className={cn("mt-4 rounded-lg border px-3 py-2 text-[11px] leading-5", appServerMessage.tone === "success" ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-200" : "border-rose-500/25 bg-rose-500/10 text-rose-200")}>{appServerMessage.text}</p>}
              </div>
            </div>
          </div>
        </div>
      );
    }
    if (section.id === "computer_automation") {
      return (
        <div className="rounded-lg border border-rose-500/20 bg-rose-500/10 p-4">
          <div className="text-sm font-medium text-rose-100">Computer actions are high-impact</div>
          <p className="mt-1 text-xs leading-5 text-rose-100/75">
            Screen observation, clicking, typing, scrolling, browser automation, checkpoint/resume, and cloud continuation stay together here.
          </p>
        </div>
      );
    }
    return null;
  };

  const renderRightHelp = (section: ControlCenterSection | undefined): ReactElement => (
    <aside data-settings-right-help className="hidden min-w-0 overflow-y-auto border-l border-white/[0.07] bg-black/15 p-4 2xl:block">
      <div className="sticky top-4 space-y-4">
        <ModelRoutingOverview workspace={profileWorkspace} locale={locale} onOpenSection={openSection} compact />
        <section className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4">
          <div className="text-xs font-medium uppercase tracking-normal text-zinc-500">{localizedCopy("Control Center", "設定ガイド")}</div>
          <h3 className="mt-2 text-sm font-medium text-zinc-100">{section?.label ?? localizedCopy("Settings", "設定")}</h3>
          <p className="mt-2 text-xs leading-5 text-zinc-500">{section?.help ?? localizedCopy("Settings are grouped by user intent and risk.", "設定は目的と影響に応じて整理されています。")}</p>
        </section>
        <section className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-4">
          <div className="text-xs font-medium uppercase tracking-normal text-zinc-500">{localizedCopy("Active profile", "使用中のプロファイル")}</div>
          <div className="mt-2 break-words text-sm text-zinc-100">{isJapanese && activeProfileLabel === "No active profile reported" ? "使用中のプロファイルは報告されていません" : activeProfileLabel}</div>
          <p className="mt-2 text-xs leading-5 text-zinc-500">
            {activeProfileLabel === "No active profile reported"
              ? localizedCopy("Profile-aware settings will show live state after the runtime reports a profile.", "バックエンドが使用中のプロファイルを返すと、プロファイル別の設定状態を表示します。")
              : localizedCopy("Settings below apply to the profile reported by the runtime.", "以下の設定はruntimeが報告したプロファイルに適用されます。")}
          </p>
        </section>
        <section className="border border-zinc-800 bg-zinc-950/50 p-4">
          <div className="text-xs font-medium uppercase tracking-normal text-zinc-500">{localizedCopy("Save state", "保存状態")}</div>
          <div className="mt-2 text-xs text-zinc-300">
            {saveState.status === "saving"
              ? localizedCopy("Saving changes…", "変更を保存中…")
              : saveState.status === "error"
                ? localizedCopy("Save requires attention", "保存を確認してください")
                : (saveState.dirtyKeys?.length ?? 0) > 0
                  ? localizedCopy(`${saveState.dirtyKeys?.length ?? 0} unsaved changes`, `未保存の変更 ${saveState.dirtyKeys?.length ?? 0}件`)
                  : localizedCopy("All changes saved", "すべて保存済み")}
          </div>
          {saveState.message ? <p className="mt-2 text-[11px] leading-5 text-zinc-500">{saveState.message}</p> : null}
          {saveState.status === "error" && (saveState.dirtyKeys?.length ?? 0) > 0 && onRetrySave ? <button type="button" onClick={onRetrySave} className="mt-3 text-xs font-medium text-red-300 hover:text-red-200">{localizedCopy("Retry save", "保存を再試行")}</button> : null}
        </section>
        <section className="border border-zinc-800 bg-zinc-950/50 p-4">
          <div className="text-xs font-medium uppercase tracking-normal text-zinc-500">{localizedCopy("Source sections", "設定の提供元")}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {(section?.sourceSections ?? []).slice(0, 8).map((source) => (
              <span key={source.id} className="rounded-md border border-zinc-800 px-2 py-1 text-[10px] text-zinc-500">
                {localizedSettingsSourceLabel(source.id, source.label, locale)}
              </span>
            ))}
            {(section?.sourceSections.length ?? 0) === 0 && (
              <span className="text-xs text-zinc-600">{localizedCopy("Provided by the settings registry", "設定システムから提供されています")}</span>
            )}
          </div>
        </section>
      </div>
    </aside>
  );

  return (
    <AnimatePresence>
      {isOpen && (
        <div ref={layerRef} className="fixed inset-0 rumi-layer-modal flex items-center justify-center" data-testid="settings-modal-layer">
          <motion.div
            initial={prefersReducedMotion ? false : { opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={prefersReducedMotion ? { opacity: 1 } : { opacity: 0 }}
            transition={{ duration: prefersReducedMotion ? 0 : 0.16 }}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={requestClose}
            aria-hidden="true"
          />
          <motion.div
            ref={dialogRef}
            data-settings-modal
            role="dialog"
            aria-modal="true"
            aria-labelledby="rumi-settings-dialog-title"
            initial={prefersReducedMotion ? false : { opacity: 0, scale: 0.98, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={prefersReducedMotion ? { opacity: 1 } : { opacity: 0, scale: 0.98, y: 8 }}
            transition={prefersReducedMotion ? { duration: 0 } : { type: "spring", stiffness: 360, damping: 34 }}
            className="relative flex h-[min(920px,calc(100dvh-20px))] w-[min(1480px,calc(100vw-16px))] min-w-0 flex-col overflow-hidden rounded-xl border border-white/10 bg-[#0d0f11] shadow-2xl shadow-black/60 max-sm:h-[calc(100dvh-4px)] max-sm:w-screen max-sm:rounded-none max-sm:border-x-0"
          >
            <header
              className="flex min-w-0 items-center justify-between gap-4 border-b border-zinc-800 px-4 py-2 sm:px-5"
              inert={closeConfirmationOpen ? true : undefined}
              aria-hidden={closeConfirmationOpen || undefined}
            >
              <div className="min-w-0 flex-1">
                <h2
                  ref={dialogTitleRef}
                  id="rumi-settings-dialog-title"
                  tabIndex={-1}
                  className="text-lg font-medium text-zinc-100 outline-none"
                >
                  {t(locale, "settings.title")}
                </h2>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setSettingsDisplayMode((current) => current === "standard" ? "advanced" : current === "advanced" ? "developer" : "standard")}
                  className="rounded-lg border border-white/[0.08] bg-white/[0.03] px-3 py-2 text-xs font-medium text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200"
                  aria-label={localizedCopy("Change settings display mode", "設定の表示モードを変更")}
                  title={localizedCopy("Cycle standard, advanced, and developer settings", "標準・上級者・開発者向け設定を切り替えます")}
                >
                  {localizedCopy("View", "表示")}: {settingsDisplayMode === "standard" ? localizedCopy("Standard", "標準") : settingsDisplayMode === "advanced" ? localizedCopy("Advanced", "上級者") : localizedCopy("Developer", "開発者")}
                </button>
                <div className="relative">
                  <button
                    ref={placementTriggerRef}
                    type="button"
                    onClick={() => setPlacementMenuOpen((current) => !current)}
                    className="rounded-lg border border-white/[0.07] bg-white/[0.035] px-2.5 py-2 text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
                    title={t(locale, "settings.addPlacement")}
                    aria-label={t(locale, "settings.addPlacement")}
                    aria-haspopup="menu"
                    aria-expanded={placementMenuOpen}
                  >
                    <Plus size={15} />
                  </button>
                  {placementMenuOpen && (
                    <div
                      ref={placementMenuRef}
                      role="menu"
                      tabIndex={-1}
                      aria-label={t(locale, "settings.addPlacement")}
                      className="absolute right-0 top-[calc(100%+8px)] rumi-layer-local-popover w-72 overflow-hidden rounded-xl border border-zinc-700 bg-zinc-950 p-1.5 shadow-2xl"
                    >
                      <div className="border-b border-zinc-800 px-2 py-2 text-[11px] text-zinc-500">
                        {localizedCopy("Items available to add", "追加できる設定項目")}
                      </div>
                      <div className="max-h-72 overflow-y-auto py-1">
                        {settingsPlacementCandidates.length > 0 ? settingsPlacementCandidates.map((manifest) => (
                          <button
                            key={manifest.id}
                            type="button"
                            role="menuitem"
                            onClick={() => {
                              updatePinnedPlacements((current) => togglePinnedPlacement(current, { id: manifest.id, surface: "settings" }));
                              setPlacementMenuOpen(false);
                            }}
                            className="flex w-full items-start justify-between gap-2 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-zinc-900"
                          >
                            <span className="min-w-0">
                              <span className="block truncate text-sm text-zinc-100">{localizedSettingsSourceLabel(String(manifest.source.sourceId ?? ""), manifest.label, locale)}</span>
                              {(manifest.description || isJapanese) && (
                                <span className="mt-0.5 block text-[11px] leading-5 text-zinc-500">{isJapanese ? "設定画面からすぐ開けるように追加します。" : manifest.description}</span>
                              )}
                            </span>
                            <Plus size={14} className="mt-0.5 flex-shrink-0 text-zinc-500" />
                          </button>
                        )) : (
                          <p className="px-2.5 py-3 text-[11px] text-zinc-500">追加できる項目はありません。</p>
                        )}
                      </div>
                    </div>
                  )}
                </div>
                <button ref={closeButtonRef} type="button" onClick={requestClose} aria-label={t(locale, "settings.close")} className="flex h-10 w-10 items-center justify-center rounded-lg text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200">
                  <X size={18} />
                </button>
              </div>
            </header>
            <div
              className="grid min-h-0 min-w-0 flex-1 lg:grid-cols-[220px_minmax(0,1fr)] 2xl:grid-cols-[220px_minmax(0,1fr)_280px]"
              inert={closeConfirmationOpen ? true : undefined}
              aria-hidden={closeConfirmationOpen || undefined}
            >
              <nav className="min-w-0 overflow-x-auto border-b border-white/7 bg-black/20 p-3 lg:overflow-y-auto lg:border-b-0 lg:border-r" aria-label={localizedCopy("Settings categories", "設定カテゴリ")}>
                <label className="mb-3 flex h-10 items-center gap-2 rounded-lg border border-zinc-800 bg-black/30 px-3 text-xs text-zinc-500 transition-colors focus-within:border-zinc-600 focus-within:text-zinc-300">
                  <Search size={14} />
                  <input
                    value={settingsSearch}
                    onChange={(event) => setSettingsSearch(event.target.value)}
                    placeholder={t(locale, "settings.searchPlaceholder")}
                    aria-label={t(locale, "settings.searchPlaceholder")}
                    className="min-w-0 flex-1 bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
                  />
                  {settingsSearch && (
                    <button
                      type="button"
                      onClick={() => setSettingsSearch("")}
                      className="rounded p-0.5 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300"
                      aria-label={t(locale, "settings.clearSearch")}
                    >
                      <X size={13} />
                    </button>
                  )}
                </label>
                <div className="flex gap-4 lg:block">
                  {navigationGroups.map((group) => {
                    const groupSections = group.sectionIds
                      .map((sectionId) => visibleSections.find((section) => section.id === sectionId))
                      .filter((section): section is ControlCenterSection => Boolean(section));
                    if (groupSections.length === 0) return null;
                    return (
                      <div key={group.id} className="flex shrink-0 gap-1.5 lg:mb-5 lg:block lg:last:mb-0">
                        <div className="hidden px-2 pb-1.5 text-[9px] font-medium uppercase tracking-[0.16em] text-zinc-700 lg:block">{group.label}</div>
                        {groupSections.map((section) => {
                          return (
                            <button
                              key={section.id}
                              type="button"
                              onClick={() => openSection(section.id)}
                              aria-current={activeSection?.id === section.id ? "page" : undefined}
                              className={cn(
                                "group relative mb-0 flex min-h-11 min-w-[154px] shrink-0 items-center justify-between gap-3 overflow-hidden border-l-2 px-3 py-2.5 text-left text-xs transition-colors lg:mb-0.5 lg:min-w-0 lg:w-full",
                                activeSection?.id === section.id
                                  ? "border-indigo-300 bg-white/[0.065] text-zinc-100"
                                  : "border-transparent text-zinc-500 hover:bg-white/[0.035] hover:text-zinc-300",
                              )}
                            >
                              <span className="min-w-0">
                                <span className="block truncate font-medium">{section.label}</span>
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    );
                  })}
                  {visibleSections.length === 0 && (
                    <div className="border border-zinc-800 bg-zinc-950/60 px-3 py-4 text-xs text-zinc-500">
                      {t(locale, "settings.noSections")}
                    </div>
                  )}
                </div>
              </nav>

              <main className="min-w-0 space-y-7 overflow-y-auto p-4 sm:p-6" id="settings-content">
                {normalizedSearch ? (
                  <section className="border border-white/[0.08] bg-black/15" aria-labelledby="settings-search-results-title">
                    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.07] px-4 py-3">
                      <div>
                        <h3 id="settings-search-results-title" className="text-sm font-medium text-zinc-100">{localizedCopy("Search results", "検索結果")}</h3>
                        <p className="mt-1 text-[11px] text-zinc-500">{localizedCopy(
                          `${settingsSearchMatches.length + profileSearchMatches.length} matches across settings and profiles`,
                          `設定とプロファイルから ${settingsSearchMatches.length + profileSearchMatches.length}件`,
                        )}</p>
                      </div>
                      <button type="button" onClick={() => setSettingsSearch("")} className="text-xs font-medium text-zinc-500 hover:text-zinc-200">{localizedCopy("Clear search", "検索をクリア")}</button>
                    </div>
                    <div className="grid max-h-56 overflow-y-auto sm:grid-cols-2">
                      {settingsSearchMatches.slice(0, 16).map(({ section, field }) => (
                        <button
                          key={`${section.id}:${field.sourceSectionId}:${field.id}`}
                          type="button"
                          onClick={() => openSearchMatch(section.id, field)}
                          className="min-w-0 border-b border-white/[0.06] px-4 py-3 text-left hover:bg-white/[0.035] sm:odd:border-r"
                        >
                          <span className="block truncate text-xs font-medium text-zinc-200">{field.label}</span>
                          <span className="mt-1 block truncate text-[10px] text-zinc-600">{section.label} · {field.sourceSectionLabel ?? field.sourceSectionId}</span>
                        </button>
                      ))}
                      {profileSearchMatches.slice(0, 8).map((profile) => (
                        <button
                          key={`profile-search:${profile.id}`}
                          type="button"
                          onClick={() => {
                            setActiveSectionId("profiles");
                            setProfileSelectionRequest((current) => ({
                              id: profile.id,
                              version: (current?.version ?? 0) + 1,
                            }));
                          }}
                          className="min-w-0 border-b border-white/[0.06] px-4 py-3 text-left hover:bg-white/[0.035] sm:odd:border-r"
                        >
                          <span className="block truncate text-xs font-medium text-zinc-200">{profile.name}</span>
                          <span className="mt-1 block truncate font-mono text-[10px] text-zinc-600">{localizedCopy("Profile", "プロファイル")} · {profile.id}</span>
                        </button>
                      ))}
                      {settingsSearchMatches.length === 0 && profileSearchMatches.length === 0 ? (
                        <p className="px-4 py-6 text-xs text-zinc-500 sm:col-span-2">{t(locale, "settings.noFields")}</p>
                      ) : null}
                    </div>
                  </section>
                ) : null}
                {pinnedSettingsPlacements.length > 0 && (
                  <section className="space-y-3">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <h3 className="text-sm font-medium text-zinc-100">{localizedCopy("Pinned placements", "固定した項目")}</h3>
                        <p className="mt-1 text-xs text-zinc-500">{localizedCopy("Open controls pinned to the Settings surface.", "Settings画面に固定した項目をここから開けます。")}</p>
                      </div>
                      <span className="rounded-full border border-zinc-800 px-2 py-1 text-[10px] text-zinc-500">
                        {localizedCopy(`${pinnedSettingsPlacements.length} pinned`, `${pinnedSettingsPlacements.length}件`)}
                      </span>
                    </div>
                    <div className="grid gap-3 lg:grid-cols-2">
                      {pinnedSettingsPlacements.map(renderSettingsPlacement)}
                    </div>
                  </section>
                )}
                {activeSection && (
                  <section className="space-y-4">
                    <div className="sticky -top-7 rumi-layer-panel -mx-1 flex flex-wrap items-start justify-between gap-3 border-b border-white/7 bg-[#0e1012] px-1 py-4">
                      <div>
                      <h3 className="text-xl font-semibold tracking-tight text-zinc-100">{activeSection.label}</h3>
                      {activeSection.description && <p className="mt-1 max-w-2xl text-xs leading-5 text-zinc-500">{activeSection.description}</p>}
                      </div>
                    </div>
                    {renderSectionPrelude(activeSection)}
                    {activeSection.id === "tools_mcp" && (
                      <ToolExperienceSettingsPanel
                        tools={(catalog?.sidebar.items ?? []).filter((item) => item.category === "tool")}
                        settingsValues={settingsValues}
                        onSettingChange={onSettingChange}
                        displayMode={settingsDisplayMode}
                      />
                    )}
                    {activeSection.id === "computer_automation" && (
                      <SystemInfoPanel info={desktopSystemInfo} />
                    )}
                    <div className="grid gap-4 2xl:grid-cols-2">
                      {visiblePrimaryFields.map(renderField)}
                    </div>
                    {normalizedSearch && activeSection.id !== "profiles" && visiblePrimaryFields.length === 0 && visibleAdvancedFields.length === 0 && (
                      <div className="rounded-lg border border-white/[0.07] bg-white/[0.03] p-4 text-sm text-zinc-500">
                        {t(locale, "settings.noFields")}
                      </div>
                    )}
                    {!normalizedSearch && settingsSections.length === 0 && (
                      <div className="rounded-lg border border-white/[0.07] bg-white/[0.03] p-4 text-sm text-zinc-500">
                        {localizedCopy(
                          "Loading built-in settings and provider information…",
                          "組み込み設定とProvider情報を読み込んでいます…",
                        )}
                      </div>
                    )}
                    {!normalizedSearch && activeSection.id !== "profiles" && activeSection.id !== "quick_setup" && settingsSections.length > 0 && visiblePrimaryFields.length === 0 && visibleAdvancedFields.length === 0 && (
                      <div className="rounded-lg border border-white/[0.07] bg-white/[0.03] p-4 text-sm text-zinc-500">
                        {localizedCopy(
                          "Pack or provider contributions for this section will appear here after registry validation.",
                          "パックや外部サービスから追加される設定は、利用可能になるとここに表示されます。",
                        )}
                      </div>
                    )}
                    {visibleAdvancedFields.length > 0 && settingsDisplayMode !== "standard" && (
                      <details className="rounded-lg border border-white/[0.07] bg-white/[0.03]">
                        <summary className="cursor-pointer list-none px-4 py-3 text-xs font-medium text-zinc-400 transition-colors hover:text-zinc-200">
                          {t(locale, "settings.advanced")}
                          <span className="mt-1 block font-normal leading-5 text-zinc-600">{t(locale, "settings.advancedHelp")}</span>
                        </summary>
                        <div className="grid gap-4 border-t border-zinc-800 p-4 lg:grid-cols-2">
                          {visibleAdvancedFields.map(renderField)}
                        </div>
                      </details>
                    )}
                    {visibleAdvancedFields.length > 0 && settingsDisplayMode === "standard" && activeSection.id !== "quick_setup" && (
                      <button
                        type="button"
                        onClick={() => setSettingsDisplayMode("advanced")}
                        className="w-full rounded-xl border border-dashed border-white/[0.09] px-4 py-3 text-left text-xs text-zinc-500 transition-colors hover:border-zinc-600 hover:bg-white/[0.025] hover:text-zinc-300"
                      >
                        <span className="font-medium text-zinc-400">{localizedCopy("Advanced settings are hidden", "上級者向け設定は非表示です")}</span>
                        <span className="mt-1 block text-[11px]">{localizedCopy(`${visibleAdvancedFields.length} low-frequency controls · Show advanced settings`, `低頻度の項目 ${visibleAdvancedFields.length}件 · 上級者向け設定を表示`)}</span>
                      </button>
                    )}
                  </section>
                )}

              {activeSection?.id === "diagnostics" && <details className="rounded-lg border border-zinc-800 bg-zinc-950/30">
                <summary className="cursor-pointer list-none px-4 py-3 text-xs font-medium text-zinc-500 transition-colors hover:text-zinc-300">
                  {t(locale, "settings.developerDiagnostics")}
                </summary>
                <div className="space-y-6 border-t border-zinc-800 p-4">
                  <section className="space-y-3">
                    <h3 className="text-sm font-medium text-zinc-100">Extension Points</h3>
                    <div className="grid gap-3 md:grid-cols-3">
                      {(catalog?.extension_points ?? []).map((point) => (
                        <div key={point.id} className="rounded-lg border border-white/[0.07] bg-white/[0.035] p-3 space-y-2">
                          <div className="text-sm text-zinc-200">{point.id}</div>
                          <div className="text-[11px] text-zinc-500 font-mono break-all">{point.path}</div>
                        </div>
                      ))}
                    </div>
                  </section>

                  <section className="space-y-3">
                    <h3 className="text-sm font-medium text-zinc-100">Parts</h3>
                    <div className="grid gap-3 md:grid-cols-2">
                      {(catalog?.parts ?? []).map((part) => (
                        <div key={part.id} className="rounded-lg border border-white/[0.07] bg-white/[0.035] p-3 space-y-2">
                          <div className="flex items-center justify-between gap-3">
                            <div className="text-sm text-zinc-200">{part.label ?? part.id}</div>
                            <div className="text-[10px] text-zinc-500 font-mono">{part.kind}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>

                  <section className="space-y-3">
                    <h3 className="text-sm font-medium text-zinc-100">System Status</h3>
                    <textarea
                      className="w-full h-28 bg-zinc-900 border border-zinc-800 rounded-lg p-3 text-sm text-zinc-300 resize-none focus:border-indigo-400/50 outline-none font-mono"
                      value={JSON.stringify(
                        {
                          health,
                          previewCount: previewsCount,
                          chatRenderers: catalog?.chat_rendering.renderers ?? [],
                          componentBindings: catalog?.component_bindings ?? [],
                          diagnostics: catalog?.diagnostics ?? [],
                        },
                        null,
                        2,
                      )}
                      readOnly
                    />
                  </section>
                </div>
              </details>}
              </main>
              {renderRightHelp(activeSection)}
            </div>
            <AnimatePresence>
              {closeConfirmationOpen ? (
                <motion.div
                  ref={closeConfirmationRef}
                  className="absolute inset-0 rumi-layer-local-popover flex items-center justify-center bg-black/75 p-4 backdrop-blur-sm"
                  role="alertdialog"
                  aria-modal="true"
                  aria-labelledby="settings-close-confirmation-title"
                  aria-describedby="settings-close-confirmation-description"
                  initial={prefersReducedMotion ? false : { opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={prefersReducedMotion ? { opacity: 1 } : { opacity: 0 }}
                  transition={{ duration: prefersReducedMotion ? 0 : 0.12 }}
                  onClick={(event) => event.stopPropagation()}
                >
                  <div className="w-full max-w-lg border border-amber-300/25 bg-[#111315] shadow-2xl shadow-black/60">
                    <div className="border-b border-white/[0.08] px-5 py-4">
                      <div className="flex items-start gap-3">
                        <AlertTriangle size={18} className="mt-0.5 shrink-0 text-amber-300" aria-hidden="true" />
                        <div className="min-w-0">
                          <h3 id="settings-close-confirmation-title" className="text-sm font-semibold text-zinc-100">
                            {localizedCopy("Settings changes are not confirmed", "未確定の設定変更があります")}
                          </h3>
                          <p id="settings-close-confirmation-description" className="mt-1 text-xs leading-5 text-zinc-400">
                            {saveState.status === "saving"
                              ? localizedCopy("A save request is still in progress. Closing does not cancel the request, but you will no longer see its result here.", "保存処理が進行中です。閉じても処理は中止されませんが、この画面では結果を確認できなくなります。")
                              : localizedCopy("These edits are visible locally but have not been accepted by the backend. Keep Settings open to retry or review the validation error.", "変更は画面上に保持されていますが、Backendでは受理されていません。Settingsを開いたまま再試行するか、入力エラーを確認してください。")}
                          </p>
                        </div>
                      </div>
                    </div>
                    {dirtySettingsKeys.length > 0 ? (
                      <div className="border-b border-white/[0.08] px-5 py-3">
                        <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-zinc-600">{localizedCopy("Unconfirmed fields", "未確定の項目")}</div>
                        <ul className="mt-2 space-y-1 font-mono text-[11px] text-amber-100/80">
                          {dirtySettingsKeys.slice(0, 5).map((key) => <li key={key} className="break-all">{key}</li>)}
                          {dirtySettingsKeys.length > 5 ? <li>{localizedCopy(`+${dirtySettingsKeys.length - 5} more`, `ほか ${dirtySettingsKeys.length - 5}件`)}</li> : null}
                        </ul>
                      </div>
                    ) : null}
                    <div className="flex flex-wrap justify-end gap-2 px-5 py-4">
                      <button
                        ref={stayInSettingsRef}
                        type="button"
                        onClick={dismissCloseConfirmation}
                        className="rounded-md border border-indigo-300/30 bg-indigo-300/[0.08] px-3 py-2 text-xs font-medium text-indigo-100 hover:bg-indigo-300/[0.13]"
                      >
                        {localizedCopy("Stay in Settings", "Settingsに戻る")}
                      </button>
                      {dirtySettingsKeys.length > 0 && onRetrySave ? (
                        <button
                          type="button"
                          onClick={() => {
                            dismissCloseConfirmation();
                            onRetrySave();
                          }}
                          className="rounded-md border border-white/10 px-3 py-2 text-xs font-medium text-zinc-200 hover:bg-white/[0.05]"
                        >
                          {localizedCopy("Retry save", "保存を再試行")}
                        </button>
                      ) : null}
                      <button
                        type="button"
                        onClick={onClose}
                        className="rounded-md border border-red-300/25 px-3 py-2 text-xs font-medium text-red-200 hover:bg-red-300/[0.08]"
                      >
                        {localizedCopy("Close anyway", "そのまま閉じる")}
                      </button>
                    </div>
                  </div>
                </motion.div>
              ) : null}
            </AnimatePresence>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}
