import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  CircleDashed,
  Copy,
  FileLock2,
  Filter,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Server,
  Star,
  Trash2,
  UserRound,
  X,
} from "lucide-react";

import { ErrorNotice } from "../../components/ErrorNotice";
import { cn } from "../../lib/cn";
import { normalizeLocale, type LocaleSetting } from "../../lib/i18n";
import type { SettingChangeHandler, SettingsLoadState, SettingsSaveState } from "../types";
import { ModelRoutingOverview } from "./ModelRoutingOverview";
import {
  createProfileRecord,
  deleteProfileRecord,
  duplicateProfileRecord,
  renameProfileRecord,
  type SettingsProfileRecord,
  type SettingsProfileWorkspace,
} from "./settingsProfileModel";

type ProfileFilter = "all" | "ready" | "attention" | "editable";
type EditMode = "create" | "duplicate" | "rename" | "delete" | null;

type ProfileSettingsPanelProps = {
  workspace: SettingsProfileWorkspace;
  locale?: LocaleSetting;
  loadState?: SettingsLoadState;
  saveState?: SettingsSaveState;
  requestedProfileId?: string;
  selectionRequestVersion?: number;
  onSettingChange: SettingChangeHandler;
  onOpenSection?: (sectionId: string) => void;
  onRetryLoad?: () => void;
};

function readinessClasses(readiness: SettingsProfileRecord["readiness"]): string {
  if (readiness === "ready" || readiness === "local") return "border-emerald-400/25 bg-emerald-400/[0.07] text-emerald-200";
  if (readiness === "blocked") return "border-red-400/25 bg-red-400/[0.07] text-red-200";
  if (readiness === "needs_connection") return "border-amber-300/25 bg-amber-300/[0.07] text-amber-100";
  return "border-white/10 bg-white/[0.04] text-zinc-400";
}

function sourceLabel(profile: SettingsProfileRecord, ja: boolean): string {
  if (profile.source === "settings") return ja ? "ユーザー設定" : "User settings";
  if (profile.source === "catalog") return ja ? "実行時カタログ" : "Runtime catalog";
  return ja ? "モデルレジストリ" : "Model registry";
}

function normalizeSearch(value: string): string {
  return value.trim().toLocaleLowerCase();
}

function profileSearchText(profile: SettingsProfileRecord): string {
  return [
    profile.name,
    profile.id,
    profile.description,
    profile.role,
    profile.providerId,
    profile.modelId,
    profile.sourceLabel,
    ...profile.routeRefs,
    ...profile.capabilityTags,
  ].join(" ").toLocaleLowerCase();
}

function readinessLabel(profile: SettingsProfileRecord, ja: boolean): string {
  if (!ja) return profile.readiness.replace(/_/g, " ");
  if (profile.readiness === "ready") return "利用可能";
  if (profile.readiness === "local") return "ローカル";
  if (profile.readiness === "needs_connection") return "未接続";
  if (profile.readiness === "blocked") return "利用不可";
  return "未確認";
}

function readinessReason(profile: SettingsProfileRecord, ja: boolean): string {
  if (!ja) return profile.readinessReason;
  if (profile.readiness === "local") return "この端末で利用できるローカルモデルです。外部Providerの認証情報は不要です。";
  if (profile.readiness === "ready") return `${profile.providerId || "Provider"} の接続とモデル経路を確認済みです。`;
  if (profile.readiness === "needs_connection") return `${profile.providerId || "Provider"} のアカウント接続またはAPIキーを設定してください。`;
  if (profile.readiness === "blocked") return `この経路は現在利用できません。${profile.readinessReason ? ` ${profile.readinessReason}` : "接続権限とProvider設定を確認してください。"}`;
  return "この経路の接続状態はまだ確認できていません。";
}

export function ProfileSettingsPanel({
  workspace,
  locale = "ja",
  loadState = { status: "ready" },
  saveState = { status: "idle", dirtyKeys: [] },
  requestedProfileId,
  selectionRequestVersion = 0,
  onSettingChange,
  onOpenSection,
  onRetryLoad,
}: ProfileSettingsPanelProps) {
  const workspaceHeadingId = useId();
  const operationErrorId = useId();
  const modelOptionsId = useId();
  const ja = normalizeLocale(locale) === "ja";
  const copyText = (english: string, japanese: string) => ja ? japanese : english;
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ProfileFilter>("all");
  const [selectedId, setSelectedId] = useState(() => workspace.profiles.find((profile) => profile.active)?.id ?? workspace.profiles[0]?.id ?? "");
  const [editMode, setEditMode] = useState<EditMode>(null);
  const [operationTargetId, setOperationTargetId] = useState("");
  const [draftName, setDraftName] = useState("");
  const [draftDescription, setDraftDescription] = useState("");
  const [draftModelId, setDraftModelId] = useState("");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [localMessage, setLocalMessage] = useState("");
  const editorRef = useRef<HTMLDivElement | null>(null);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const operationTriggerRef = useRef<HTMLElement | null>(null);
  const profileButtonRefs = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    if (workspace.profiles.some((profile) => profile.id === selectedId)) return;
    setSelectedId(workspace.profiles.find((profile) => profile.active)?.id ?? workspace.profiles[0]?.id ?? "");
  }, [selectedId, workspace.profiles]);

  useEffect(() => {
    setEditMode(null);
    setOperationTargetId("");
    setLocalMessage("");
  }, [selectedId]);

  useEffect(() => {
    if (!editMode) return;
    const frame = requestAnimationFrame(() => nameInputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [editMode]);

  const normalizedQuery = normalizeSearch(query);
  const profiles = useMemo(() => workspace.profiles.filter((profile) => {
    if (normalizedQuery && !profileSearchText(profile).includes(normalizedQuery)) return false;
    if (filter === "ready" && profile.readiness !== "ready" && profile.readiness !== "local") return false;
    if (filter === "attention" && profile.readiness !== "blocked" && profile.readiness !== "needs_connection") return false;
    if (filter === "editable" && !profile.editable) return false;
    return true;
  }), [filter, normalizedQuery, workspace.profiles]);
  const selected = profiles.find((profile) => profile.id === selectedId) ?? profiles[0];
  const operationTarget = workspace.profiles.find((profile) => profile.id === operationTargetId);
  const modelOptions = useMemo(() => [...new Set(workspace.profiles.map((profile) => profile.modelId).filter(Boolean))].sort(), [workspace.profiles]);
  const collection = workspace.editableCollection;
  const editableCount = workspace.profiles.filter((profile) => profile.editable).length;
  const attentionCount = workspace.profiles.filter((profile) => profile.readiness === "blocked" || profile.readiness === "needs_connection").length;
  const activeProfile = workspace.profiles.find((profile) => profile.active);
  const defaultProfile = workspace.profiles.find((profile) => profile.default);

  useEffect(() => {
    if (!profiles.length || profiles.some((profile) => profile.id === selectedId)) return;
    setSelectedId(profiles[0].id);
  }, [profiles, selectedId]);

  useEffect(() => {
    if (!requestedProfileId || !profiles.some((profile) => profile.id === requestedProfileId)) return;
    setSelectedId(requestedProfileId);
    requestAnimationFrame(() => {
      const button = profileButtonRefs.current.get(requestedProfileId);
      button?.scrollIntoView({ block: "nearest" });
      button?.focus();
    });
  }, [profiles, requestedProfileId, selectionRequestVersion]);

  const resetEditor = (restoreFocus = false) => {
    setEditMode(null);
    setOperationTargetId("");
    setDraftName("");
    setDraftDescription("");
    setDraftModelId("");
    setDeleteConfirmation("");
    setLocalMessage("");
    if (restoreFocus) {
      const trigger = operationTriggerRef.current;
      requestAnimationFrame(() => trigger?.focus());
    }
  };

  const startEditor = (mode: Exclude<EditMode, null>) => {
    operationTriggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setOperationTargetId(mode === "create" ? "" : selected?.id ?? "");
    setEditMode(mode);
    setLocalMessage("");
    setDeleteConfirmation("");
    if (mode === "create") {
      setDraftName(copyText("New profile", "新しいプロファイル"));
      setDraftDescription("");
      setDraftModelId(selected?.modelId ?? activeProfile?.modelId ?? "");
    } else if (mode === "duplicate" && selected) {
      setDraftName(copyText(`${selected.name} copy`, `${selected.name} のコピー`));
      setDraftDescription(selected.description);
      setDraftModelId(selected.modelId);
    } else if (mode === "rename" && selected) {
      setDraftName(selected.name);
      setDraftDescription(selected.description);
      setDraftModelId(selected.modelId);
    } else {
      setDraftName("");
      setDraftDescription("");
      setDraftModelId("");
    }
  };

  const focusProfileAt = (index: number) => {
    if (!profiles.length) return;
    const normalizedIndex = Math.min(Math.max(index, 0), profiles.length - 1);
    const next = profiles[normalizedIndex];
    setSelectedId(next.id);
    requestAnimationFrame(() => profileButtonRefs.current.get(next.id)?.focus());
  };

  const handleProfileKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === "ArrowDown" || event.key === "ArrowRight") {
      event.preventDefault();
      focusProfileAt(index + 1);
    } else if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
      event.preventDefault();
      focusProfileAt(index - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusProfileAt(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusProfileAt(profiles.length - 1);
    }
  };

  const validateName = (): string => {
    const name = draftName.trim();
    if (name.length < 2) return copyText("Enter at least two characters.", "2文字以上で入力してください。") ;
    const renameTargetId = editMode === "rename" ? operationTarget?.id : "";
    const duplicate = workspace.profiles.some((profile) => profile.id !== renameTargetId && profile.name.toLocaleLowerCase() === name.toLocaleLowerCase());
    if (duplicate) return copyText("A profile with this name already exists.", "同じ名前のプロファイルが既にあります。") ;
    return "";
  };

  const commitEditor = () => {
    if (!collection) return;
    if (editMode === "delete") {
      if (!operationTarget || !operationTarget.editable) {
        setLocalMessage(copyText("This profile is no longer available as an editable Settings record.", "対象のプロファイルは、編集可能なSettings項目として利用できなくなりました。"));
        return;
      }
      if (operationTarget.active || operationTarget.default || collection.records.length <= 1) {
        setLocalMessage(copyText("The target became active, default, or the last editable profile. Review the current profile state before deleting.", "対象が有効中・既定・最後の編集可能プロファイルのいずれかになりました。現在の状態を確認してから削除してください。"));
        return;
      }
      const confirmed = deleteConfirmation.trim() === operationTarget.name || deleteConfirmation.trim() === operationTarget.id;
      if (!confirmed) {
        setLocalMessage(copyText("Type the exact profile name or id to confirm deletion.", "削除を確定するには、対象の名前またはIDを正確に入力してください。"));
        return;
      }
      const nextRecords = deleteProfileRecord(collection, operationTarget);
      onSettingChange(collection.sectionId, collection.fieldId, nextRecords);
      const nextSelection = workspace.profiles.find((profile) => profile.id !== operationTarget.id)?.id ?? "";
      setSelectedId(nextSelection);
      resetEditor();
      return;
    }

    const validation = validateName();
    if (validation) {
      setLocalMessage(validation);
      return;
    }
    if (editMode === "create") {
      const next = createProfileRecord({ collection, name: draftName, description: draftDescription, modelId: draftModelId });
      const id = String(next[collection.idField] ?? "");
      onSettingChange(collection.sectionId, collection.fieldId, [...collection.records, next]);
      setSelectedId(id);
      resetEditor();
      return;
    }
    if (!operationTarget?.editable) {
      setLocalMessage(copyText("The selected profile is no longer editable.", "選択したプロファイルは編集できなくなりました。"));
      return;
    }
    if (editMode === "duplicate") {
      const next = duplicateProfileRecord({ collection, profile: operationTarget, name: draftName });
      const id = String(next[collection.idField] ?? "");
      onSettingChange(collection.sectionId, collection.fieldId, [...collection.records, next]);
      setSelectedId(id);
      resetEditor();
      return;
    }
    if (editMode === "rename") {
      onSettingChange(collection.sectionId, collection.fieldId, renameProfileRecord(collection, operationTarget, draftName));
      resetEditor();
    }
  };

  // Runtime/model-registry entries report the currently active route, but the Settings
  // contract only exposes a default-model preference for them. Never present that
  // preference as an active-runtime switch: the current conversation remains under
  // the model selector's authority.
  const canSwitch = Boolean(
    selected
    && !selected.active
    && selected.source === "settings"
    && collection?.activeFieldId,
  );
  const canDefault = Boolean(selected) && !selected?.default && (
    (selected.source === "settings" && Boolean(collection?.defaultFieldId))
    || selected.source === "model"
  );

  const activateSelected = () => {
    if (!selected || selected.source !== "settings" || !collection?.activeFieldId) return;
    onSettingChange(collection.sectionId, collection.activeFieldId, selected.id);
  };

  const defaultSelected = () => {
    if (!selected) return;
    if (selected.source === "settings" && collection?.defaultFieldId) {
      onSettingChange(collection.sectionId, collection.defaultFieldId, selected.id);
      return;
    }
    if (selected.source === "model") {
      onSettingChange("models", "preferred_model", selected.modelId || selected.id);
    }
  };

  if (loadState.status === "loading" && workspace.profiles.length === 0) {
    return (
      <div className="flex min-h-52 items-center justify-center border border-white/[0.08] bg-black/15" role="status">
        <span className="inline-flex items-center gap-2 text-sm text-zinc-400"><Loader2 size={16} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />{copyText("Loading profiles…", "プロファイルを読み込んでいます…")}</span>
      </div>
    );
  }

  if (loadState.status === "error" && workspace.profiles.length === 0) {
    const loadError = loadState.message || copyText(
      "Existing settings are preserved. Retry after the backend reconnects.",
      "既存設定は保持されています。Backendの再接続後に再試行してください。",
    );
    return (
      <ErrorNotice
        className="p-5 text-xs leading-5"
        copyLabel={copyText("Copy profile load error", "プロファイル読み込みエラーをコピー")}
        copyText={`${copyText("Profiles unavailable", "プロファイルを取得できません")}\n\n${loadError}`}
        message={loadError}
        messageClassName="mt-1 text-red-100/70"
        title={copyText("Profiles unavailable", "プロファイルを取得できません")}
        titleClassName="text-sm text-red-100"
        trailing={onRetryLoad ? (
          <button type="button" onClick={onRetryLoad} className="mt-3 inline-flex items-center gap-1.5 border border-red-300/30 px-2.5 py-1.5 text-xs font-medium text-red-100 hover:bg-red-300/10"><RefreshCw size={12} aria-hidden="true" />{copyText("Retry", "再試行")}</button>
        ) : undefined}
      />
    );
  }

  if (workspace.profiles.length === 0) {
    return (
      <div className="border border-white/[0.08] bg-black/15 p-6 text-center">
        <UserRound size={24} className="mx-auto text-zinc-600" aria-hidden="true" />
        <h4 className="mt-3 text-sm font-medium text-zinc-200">{copyText("No profiles reported", "プロファイルがありません")}</h4>
        <p className="mx-auto mt-1 max-w-lg text-xs leading-5 text-zinc-500">{copyText("Configure a model route first. Runtime and pack-provided profiles will appear here after validation.", "まずモデル経路を設定してください。実行時またはパック提供のプロファイルは検証後にここへ表示されます。")}</p>
        {onOpenSection ? <button type="button" onClick={() => onOpenSection("models_api")} className="mt-4 border border-white/10 px-3 py-2 text-xs font-medium text-zinc-200 hover:bg-white/[0.05]">{copyText("Open Models & API", "モデルとAPIを開く")}</button> : null}
      </div>
    );
  }

  const actionBusy = saveState.status === "saving";
  const profileDirtyKeys = (saveState.dirtyKeys ?? []).filter((key) => (
    /^(?:profiles?|adaptive)\./.test(key) || key === "models.preferred_model" || key === "models"
  ));
  const profileSavePending = saveState.status === "saving" && profileDirtyKeys.length > 0;
  const profileSaveFailed = saveState.status === "error" && profileDirtyKeys.length > 0;
  const deleteBlockedReason = selected?.active
    ? copyText("Switch away from this active profile before deleting it.", "有効中のプロファイルを削除する前に、別のプロファイルへ切り替えてください。")
    : selected?.default
      ? copyText("Choose another default profile before deleting this one.", "削除する前に別の既定プロファイルを選択してください。")
      : collection && collection.records.length <= 1
        ? copyText("Keep at least one editable profile.", "編集可能なプロファイルを1件以上残してください。")
      : "";
  const switchUnavailableReason = selected && !selected.active && !canSwitch
    ? selected.source === "model"
      ? copyText(
        "Settings can change this model profile's default for future conversations, but switching the current conversation stays in the model picker.",
        "Settingsでは今後の会話に使う既定モデルを変更できます。現在の会話の切替はモデルピッカーから行ってください。",
      )
      : copyText("This runtime does not expose a safe active-profile setting for this entry.", "この項目を有効化するための安全なactive profile設定が実行環境から公開されていません。")
    : "";
  const defaultUnavailableReason = selected && !selected.default && !canDefault
    ? copyText("This runtime does not expose a separate default-profile setting for this entry.", "この項目を既定化するための独立したdefault profile設定が実行環境から公開されていません。")
    : "";

  return (
    <div className="space-y-4" data-settings-profile-panel>
      {loadState.status === "loading" && workspace.profiles.length > 0 ? (
        <div className="flex items-center gap-2 border-l-2 border-zinc-600 bg-white/[0.025] px-3 py-2 text-xs text-zinc-500" role="status">
          <Loader2 size={13} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          {copyText("Refreshing profile and availability data…", "プロファイルと利用可否を再取得しています…")}
        </div>
      ) : null}
      {loadState.status === "error" && workspace.profiles.length > 0 ? (
        <ErrorNotice
          className="rounded-none border-y-0 border-r-0 border-l-2 px-3 py-2.5 text-xs leading-5"
          copyLabel={copyText("Copy profile refresh error", "プロファイル更新エラーをコピー")}
          message={copyText("Showing the last known profile data because the model registry refresh failed.", "モデルレジストリの再取得に失敗したため、直前に確認できたプロファイル情報を表示しています。")}
          severity="warning"
          trailing={onRetryLoad ? <button type="button" onClick={onRetryLoad} className="shrink-0 border border-amber-200/25 px-2 py-1 font-medium hover:bg-amber-200/10">{copyText("Retry", "再試行")}</button> : undefined}
        />
      ) : null}
      <ModelRoutingOverview workspace={workspace} locale={locale} onOpenSection={onOpenSection} />

      {profileSaveFailed ? (
        <ErrorNotice
          className="rounded-none border-y-0 border-r-0 border-l-2 px-3 py-2.5 text-xs leading-5"
          copyLabel={copyText("Copy profile save error", "プロファイル保存エラーをコピー")}
          message={copyText("Profile changes remain local and unconfirmed. Use Retry save in the Settings header after resolving the connection or validation error.", "プロファイルの変更はこの画面に保持されていますが、Backendでは未確定です。接続または入力エラーを解消し、Settings上部の「保存を再試行」を使用してください。")}
        />
      ) : profileSavePending ? (
        <div className="flex items-start gap-2 border-l-2 border-amber-300 bg-amber-300/[0.05] px-3 py-2.5 text-xs leading-5 text-amber-100" role="status">
          <Loader2 size={14} className="mt-0.5 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          <span>{copyText("Saving profile changes and validating the resulting route…", "プロファイル変更を保存し、変更後の経路を検証しています…")}</span>
        </div>
      ) : null}

      <section className="border border-white/[0.08] bg-black/10" aria-labelledby={workspaceHeadingId}>
        <header className="border-b border-white/[0.07] px-4 py-3">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h4 id={workspaceHeadingId} className="text-sm font-medium text-zinc-100">{copyText("Profile workspace", "プロファイル管理")}</h4>
              <p className="mt-1 text-xs leading-5 text-zinc-500">{copyText("Active and default are separate where the backend schema exposes both fields.", "Backend schemaが両方の項目を公開している場合、有効中と既定値は別々に管理されます。")}</p>
            </div>
            {collection ? (
              <button type="button" onClick={() => startEditor("create")} disabled={actionBusy} className="inline-flex items-center gap-1.5 rounded-md border border-indigo-400/30 bg-indigo-400/[0.08] px-2.5 py-1.5 text-xs font-medium text-indigo-100 hover:bg-indigo-400/[0.13] disabled:cursor-not-allowed disabled:opacity-50">
                <Plus size={13} aria-hidden="true" />{copyText("Create profile", "プロファイルを作成")}
              </button>
            ) : (
              <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 px-2.5 py-1 text-[10px] text-zinc-500" title={copyText("This runtime exposes profiles as read-only catalog data.", "この実行環境はプロファイルを読み取り専用カタログとして公開しています。") }>
                <FileLock2 size={11} aria-hidden="true" />{copyText("Runtime managed", "実行環境が管理")}
              </span>
            )}
          </div>

          <dl className="mt-3 grid gap-px overflow-hidden border border-white/[0.07] bg-white/[0.07] sm:grid-cols-4">
            {[
              [copyText("Active", "有効中"), activeProfile?.name || workspace.activeProfileId || "—"],
              [copyText("Default", "既定"), defaultProfile?.name || workspace.defaultProfileId || "—"],
              [copyText("Profiles", "件数"), String(workspace.profiles.length)],
              [copyText("Needs attention", "要確認"), String(attentionCount)],
            ].map(([label, value]) => (
              <div key={label} className="min-w-0 bg-[#111315] px-3 py-2.5">
                <dt className="text-[10px] uppercase tracking-[0.13em] text-zinc-600">{label}</dt>
                <dd className="mt-1 truncate text-xs font-medium text-zinc-200" title={value}>{value}</dd>
              </div>
            ))}
          </dl>
        </header>

        <div className="grid min-h-[420px] min-w-0 lg:grid-cols-[minmax(250px,0.82fr)_minmax(0,1.18fr)]">
          <div className="min-w-0 border-b border-white/[0.07] lg:border-b-0 lg:border-r">
            <div className="space-y-2 border-b border-white/[0.07] p-3">
              <label className="flex h-9 items-center gap-2 border border-white/[0.09] bg-black/20 px-2.5 text-zinc-500 focus-within:border-indigo-400/45 focus-within:text-zinc-300">
                <Search size={13} aria-hidden="true" />
                <span className="sr-only">{copyText("Search profiles", "プロファイルを検索")}</span>
                <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={copyText("Search profile, model, provider…", "名前、モデル、Providerを検索…")} className="min-w-0 flex-1 bg-transparent text-xs text-zinc-200 outline-none placeholder:text-zinc-700" />
                {query ? <button type="button" onClick={() => setQuery("")} className="rounded p-0.5 text-zinc-600 hover:text-zinc-300" aria-label={copyText("Clear profile search", "プロファイル検索をクリア")}><X size={12} /></button> : null}
              </label>
              <div className="flex flex-wrap gap-1" aria-label={copyText("Profile filters", "プロファイルの絞り込み")}>
                {([
                  ["all", copyText("All", "すべて")],
                  ["ready", copyText("Ready", "利用可能")],
                  ["attention", copyText("Attention", "要確認")],
                  ["editable", copyText(`Editable ${editableCount}`, `編集可能 ${editableCount}`)],
                ] as Array<[ProfileFilter, string]>).map(([id, label]) => (
                  <button key={id} type="button" onClick={() => setFilter(id)} aria-pressed={filter === id} className={cn("inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[10px] transition-colors", filter === id ? "border-zinc-600 bg-zinc-800 text-zinc-100" : "border-transparent text-zinc-600 hover:border-white/10 hover:text-zinc-300")}>
                    {id === "all" ? <Filter size={10} aria-hidden="true" /> : null}{label}
                  </button>
                ))}
              </div>
            </div>

            <div className="max-h-[520px] overflow-y-auto p-2" role="listbox" aria-label={copyText("Profiles", "プロファイル") }>
              {profiles.map((profile, index) => {
                const selectedRow = selected?.id === profile.id;
                return (
                  <button
                    key={`${profile.source}:${profile.id}`}
                    onClick={() => setSelectedId(profile.id)}
                    ref={(node) => {
                      if (node) profileButtonRefs.current.set(profile.id, node);
                      else profileButtonRefs.current.delete(profile.id);
                    }}
                    type="button"
                    role="option"
                    aria-selected={selectedRow}
                    tabIndex={selectedRow ? 0 : -1}
                    onKeyDown={(event) => handleProfileKeyDown(event, index)}
                    className={cn(
                      "group mb-1 w-full border-l-2 px-3 py-2.5 text-left transition-colors last:mb-0",
                      selectedRow ? "border-indigo-300 bg-white/[0.065]" : "border-transparent hover:bg-white/[0.035]",
                    )}
                  >
                    <span className="flex min-w-0 items-start justify-between gap-2">
                      <span className="min-w-0">
                        <span className="flex min-w-0 items-center gap-1.5">
                          <strong className="truncate text-xs font-medium text-zinc-200">{profile.name}</strong>
                          {profile.favorite ? <Star size={10} className="shrink-0 fill-current text-amber-300" aria-label={copyText("Favorite", "お気に入り")} /> : null}
                        </span>
                        <span className="mt-1 block truncate font-mono text-[10px] text-zinc-600">{profile.id}</span>
                      </span>
                      <span className={cn("shrink-0 rounded-full border px-1.5 py-0.5 text-[9px] uppercase tracking-[0.1em]", readinessClasses(profile.readiness))}>{readinessLabel(profile, ja)}</span>
                    </span>
                    <span className="mt-2 flex min-w-0 flex-wrap items-center gap-1.5 text-[10px] text-zinc-500">
                      {profile.active ? <span className="inline-flex items-center gap-1 rounded border border-indigo-300/25 bg-indigo-300/[0.07] px-1.5 py-0.5 text-indigo-200"><Check size={9} />{copyText("Active", "有効中")}</span> : null}
                      {profile.default ? <span className="rounded border border-white/10 px-1.5 py-0.5 text-zinc-300">{copyText("Default", "既定")}</span> : null}
                      {profile.managed ? <span>{copyText("Managed", "管理対象")}</span> : <span>{copyText("Editable", "編集可能")}</span>}
                      <span className="truncate">{profile.providerId || copyText("local/inherited", "ローカル/継承")}</span>
                    </span>
                  </button>
                );
              })}
              {profiles.length === 0 ? (
                <div className="px-3 py-8 text-center text-xs leading-5 text-zinc-600">{copyText("No profiles match this search and filter.", "検索条件に一致するプロファイルはありません。")}</div>
              ) : null}
            </div>
          </div>

          <div className="min-w-0 p-4 sm:p-5">
            {selected ? (
              <div className="space-y-5">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <h5 className="truncate text-base font-semibold text-zinc-100">{selected.name}</h5>
                      {selected.active ? <span className="rounded-full border border-indigo-300/25 bg-indigo-300/[0.07] px-2 py-0.5 text-[10px] font-medium text-indigo-100">{copyText("Active now", "現在有効")}</span> : null}
                      {selected.default ? <span className="rounded-full border border-white/10 px-2 py-0.5 text-[10px] text-zinc-300">{copyText("Default", "既定")}</span> : null}
                    </div>
                    <p className="mt-1 break-all font-mono text-[10px] text-zinc-600">{selected.id}</p>
                    {selected.description ? <p className="mt-2 max-w-2xl text-xs leading-5 text-zinc-400">{selected.description}</p> : null}
                  </div>
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-white/10 px-2 py-1 text-[10px] text-zinc-400"><FileLock2 size={10} aria-hidden="true" />{sourceLabel(selected, ja)}</span>
                </div>

                <div className="grid gap-px overflow-hidden border border-white/[0.07] bg-white/[0.07] sm:grid-cols-2">
                  {[
                    [copyText("Role", "役割"), selected.role || "—"],
                    [copyText("Model", "モデル"), selected.modelId || copyText("Inherited", "継承")],
                    [copyText("Provider", "Provider"), selected.providerId || copyText("Local / inherited", "ローカル / 継承")],
                    [copyText("Credential route", "認証経路"), selected.routeRefs.join(" → ") || copyText("Provider default / OAuth", "Provider既定 / OAuth")],
                  ].map(([label, value]) => (
                    <div key={label} className="min-w-0 bg-[#111315] px-3 py-3">
                      <div className="text-[10px] uppercase tracking-[0.12em] text-zinc-600">{label}</div>
                      <div className="mt-1 break-words text-xs text-zinc-300" title={value}>{value}</div>
                    </div>
                  ))}
                </div>

                <div className={cn(
                  "border-l-2 px-3 py-2.5 text-xs leading-5",
                  selected.readiness === "ready" || selected.readiness === "local"
                    ? "border-emerald-400 bg-emerald-400/[0.05] text-emerald-100/80"
                    : selected.readiness === "blocked"
                      ? "border-red-400 bg-red-400/[0.06] text-red-100"
                      : selected.readiness === "needs_connection"
                        ? "border-amber-300 bg-amber-300/[0.05] text-amber-100"
                        : "border-zinc-600 bg-white/[0.025] text-zinc-400",
                )}>
                  <span className="flex items-start gap-2">
                    {selected.readiness === "ready" || selected.readiness === "local"
                      ? <CheckCircle2 size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                      : selected.readiness === "unknown"
                        ? <CircleDashed size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                        : <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />}
                    <span>{readinessReason(selected, ja)}</span>
                  </span>
                </div>

                {selected.capabilityTags.length ? (
                  <div>
                    <div className="text-[10px] uppercase tracking-[0.12em] text-zinc-600">{copyText("Capabilities", "機能")}</div>
                    <div className="mt-2 flex flex-wrap gap-1.5">{selected.capabilityTags.slice(0, 12).map((tag) => <span key={tag} className="rounded border border-white/[0.08] px-2 py-1 text-[10px] text-zinc-400">{tag}</span>)}</div>
                  </div>
                ) : null}

                <div className="flex flex-wrap gap-2 border-t border-white/[0.07] pt-4">
                  <button type="button" onClick={activateSelected} disabled={!canSwitch || actionBusy} title={switchUnavailableReason || undefined} className="rounded-md border border-indigo-400/30 bg-indigo-400/[0.08] px-3 py-2 text-xs font-medium text-indigo-100 hover:bg-indigo-400/[0.13] disabled:cursor-not-allowed disabled:opacity-40">
                    {selected.active ? copyText("Active profile", "有効中のプロファイル") : copyText("Switch to profile", "このプロファイルへ切替")}
                  </button>
                  <button type="button" onClick={defaultSelected} disabled={!canDefault || actionBusy} title={defaultUnavailableReason || undefined} className="rounded-md border border-white/10 px-3 py-2 text-xs font-medium text-zinc-300 hover:bg-white/[0.05] disabled:cursor-not-allowed disabled:opacity-40">
                    {selected.source === "model"
                      ? selected.default
                        ? copyText("Default model", "既定モデル")
                        : copyText("Set as default model", "既定モデルに設定")
                      : selected.default
                        ? copyText("Default profile", "既定のプロファイル")
                        : copyText("Set as default", "既定に設定")}
                  </button>
                  {selected.readiness === "needs_connection" || selected.readiness === "blocked" ? (
                    <button type="button" onClick={() => onOpenSection?.("accounts_connections")} className="rounded-md border border-amber-300/25 px-3 py-2 text-xs font-medium text-amber-100 hover:bg-amber-300/[0.07]">{copyText("Fix connection", "接続を修正")}</button>
                  ) : null}
                </div>
                {switchUnavailableReason || defaultUnavailableReason ? (
                  <p className="text-[11px] leading-5 text-zinc-600">{switchUnavailableReason || defaultUnavailableReason}</p>
                ) : null}

                {selected.editable && collection ? (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="mr-1 text-[10px] uppercase tracking-[0.12em] text-zinc-600">{copyText("Manage", "管理")}</span>
                    <button type="button" onClick={() => startEditor("duplicate")} disabled={actionBusy} className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-200 disabled:opacity-40"><Copy size={12} />{copyText("Duplicate", "複製")}</button>
                    <button type="button" onClick={() => startEditor("rename")} disabled={actionBusy} className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-zinc-400 hover:bg-white/[0.05] hover:text-zinc-200 disabled:opacity-40"><Pencil size={12} />{copyText("Rename", "名前変更")}</button>
                    <button type="button" onClick={() => startEditor("delete")} disabled={actionBusy || Boolean(deleteBlockedReason)} title={deleteBlockedReason || undefined} className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-red-300/80 hover:bg-red-400/[0.08] hover:text-red-200 disabled:cursor-not-allowed disabled:opacity-35"><Trash2 size={12} />{copyText("Delete", "削除")}</button>
                    {deleteBlockedReason ? <span className="basis-full text-[11px] leading-5 text-zinc-600">{deleteBlockedReason}</span> : null}
                  </div>
                ) : (
                  <div className="border-t border-white/[0.07] pt-4 text-xs leading-5 text-zinc-500">
                    <span className="inline-flex items-start gap-2"><FileLock2 size={13} className="mt-0.5 shrink-0" aria-hidden="true" />{copyText("This entry is supplied by the runtime or model registry. Create, rename, duplicate, and delete are available only when an editable profile collection is exposed by the settings schema.", "この項目は実行環境またはモデルレジストリから提供されています。作成・名前変更・複製・削除は、settings schemaが編集可能なプロファイル一覧を公開している場合だけ利用できます。")}</span>
                  </div>
                )}
              </div>
            ) : null}
          </div>
        </div>
      </section>

      {editMode ? (
        <div
          ref={editorRef}
          className={cn("border p-4", editMode === "delete" ? "border-red-400/30 bg-red-400/[0.06]" : "border-indigo-400/25 bg-indigo-400/[0.045]")}
          role="region"
          aria-label={copyText("Profile operation", "プロファイル操作")}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.preventDefault();
            event.stopPropagation();
            resetEditor(true);
          }}
        >
          <div className="flex items-start justify-between gap-3">
            <div>
              <h4 className={cn("text-sm font-medium", editMode === "delete" ? "text-red-100" : "text-zinc-100")}>
                {editMode === "create" ? copyText("Create profile", "プロファイルを作成") : editMode === "duplicate" ? copyText(`Duplicate ${operationTarget?.name ?? "profile"}`, `${operationTarget?.name ?? "プロファイル"}を複製`) : editMode === "rename" ? copyText(`Rename ${operationTarget?.name ?? "profile"}`, `${operationTarget?.name ?? "プロファイル"}の名前を変更`) : copyText(`Delete ${operationTarget?.name ?? "profile"}`, `${operationTarget?.name ?? "プロファイル"}を削除`)}
              </h4>
              <p className="mt-1 text-xs leading-5 text-zinc-500">
                {editMode === "duplicate"
                  ? copyText("Plaintext secrets and tokens are never copied. Existing credential references may be retained.", "平文の秘密情報やtokenは複製しません。既存のcredential referenceは保持される場合があります。")
                  : editMode === "delete"
                    ? copyText("This removes the exact profile definition from Settings. The action cannot be undone here.", "Settingsから対象のプロファイル定義を削除します。この画面からは元に戻せません。")
                    : copyText("Changes use the existing settings patch flow and remain subject to backend validation.", "変更は既存のsettings patch経路を使用し、Backendの検証対象になります。")}
              </p>
            </div>
            <button type="button" onClick={() => resetEditor(true)} className="rounded p-1 text-zinc-500 hover:bg-white/[0.05] hover:text-zinc-200" aria-label={copyText("Cancel profile operation", "プロファイル操作をキャンセル")}><X size={15} /></button>
          </div>

          {editMode === "delete" ? (
            <div className="mt-4 max-w-xl">
              <div className="border border-red-400/20 bg-black/20 px-3 py-2.5 text-xs text-red-100">
                <strong className="block text-sm">{operationTarget?.name}</strong>
                <span className="mt-1 block break-all font-mono text-[10px] text-red-200/60">{operationTarget?.id}</span>
              </div>
              <label className="mt-3 block text-xs text-red-100/80">
                {copyText("Type the profile name or id exactly", "プロファイル名またはIDを正確に入力")}
                <input ref={nameInputRef} value={deleteConfirmation} onChange={(event) => { setDeleteConfirmation(event.target.value); setLocalMessage(""); }} className="mt-2 h-10 w-full border border-red-400/30 bg-black/25 px-3 font-mono text-sm text-red-50 outline-none focus:border-red-300" autoComplete="off" spellCheck={false} aria-invalid={Boolean(localMessage)} aria-describedby={localMessage ? operationErrorId : undefined} />
              </label>
            </div>
          ) : (
            <div className="mt-4 grid max-w-2xl gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-400">
                {copyText("Profile name", "プロファイル名")}
                <input ref={nameInputRef} value={draftName} onChange={(event) => { setDraftName(event.target.value); setLocalMessage(""); }} className="mt-2 h-10 w-full border border-white/10 bg-black/20 px-3 text-sm text-zinc-100 outline-none focus:border-indigo-400/50" autoComplete="off" aria-invalid={Boolean(localMessage)} aria-describedby={localMessage ? operationErrorId : undefined} />
              </label>
              {editMode === "create" ? (
                <label className="text-xs text-zinc-400">
                  {copyText("Initial model profile (optional)", "初期モデルプロファイル（任意）")}
                  <input list={modelOptions.length ? modelOptionsId : undefined} value={draftModelId} onChange={(event) => setDraftModelId(event.target.value)} className="mt-2 h-10 w-full border border-white/10 bg-black/20 px-3 font-mono text-xs text-zinc-100 outline-none focus:border-indigo-400/50" autoComplete="off" spellCheck={false} placeholder={copyText("Leave blank to inherit", "空欄で継承")} />
                  {modelOptions.length ? <datalist id={modelOptionsId}>{modelOptions.map((modelId) => <option key={modelId} value={modelId} />)}</datalist> : null}
                  <span className="mt-1 block text-[10px] leading-4 text-zinc-600">{copyText("Known model ids are suggested; the backend remains authoritative.", "既知のモデルIDを候補表示します。最終的な検証はBackendが行います。")}</span>
                </label>
              ) : null}
              {editMode === "create" ? (
                <label className="text-xs text-zinc-400 sm:col-span-2">
                  {copyText("Purpose (optional)", "用途（任意）")}
                  <textarea value={draftDescription} onChange={(event) => setDraftDescription(event.target.value)} className="mt-2 min-h-20 w-full resize-y border border-white/10 bg-black/20 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-indigo-400/50" />
                </label>
              ) : null}
            </div>
          )}

          {localMessage ? (
            <div className="mt-3" id={operationErrorId}>
              <ErrorNotice
                className="px-3 py-2 text-xs"
                copyLabel={copyText("Copy profile operation error", "プロファイル操作エラーをコピー")}
                message={localMessage}
              />
            </div>
          ) : null}
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" onClick={commitEditor} disabled={actionBusy} className={cn("rounded-md border px-3 py-2 text-xs font-medium disabled:cursor-not-allowed disabled:opacity-50", editMode === "delete" ? "border-red-300/35 bg-red-400/[0.1] text-red-100 hover:bg-red-400/[0.16]" : "border-indigo-300/35 bg-indigo-400/[0.1] text-indigo-100 hover:bg-indigo-400/[0.16]")}>
              {actionBusy ? copyText("Saving…", "保存中…") : editMode === "delete" ? copyText("Delete this profile", "このプロファイルを削除") : editMode === "duplicate" ? copyText("Create duplicate", "複製を作成") : editMode === "rename" ? copyText("Save new name", "新しい名前を保存") : copyText("Create profile", "プロファイルを作成")}
            </button>
            <button type="button" onClick={() => resetEditor(true)} className="rounded-md border border-white/10 px-3 py-2 text-xs font-medium text-zinc-400 hover:bg-white/[0.05]">{copyText("Cancel", "キャンセル")}</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
