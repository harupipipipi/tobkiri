import { AlertTriangle, Check, ChevronDown, Loader2, Search, Shield, Sparkles, Wrench } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { ModelSearchItem, SidebarItem, ToolCatalogResponse, ToolCatalogService, ToolCatalogTool, ToolSelectionMode, ToolSelectionStrategy } from "../lib/api";
import { toolResources } from "../features/tools/resources/toolResources";
import { cn } from "../lib/cn";
import { ErrorNotice } from "./ErrorNotice";
import { ToolSettingsPanel } from "./ToolSettingsPanel";
import { ModalFoundation } from "./ModalFoundation";

type PermissionDecision = "auto" | "confirm" | "block";
type ToolSettings = Record<string, unknown>;
type SettingsValues = Record<string, Record<string, unknown>>;

const MODE_OPTIONS: Array<{ value: ToolSelectionMode; label: string; note: string; badge?: string }> = [
  { value: "auto", label: "自動で選ぶ", note: "依頼に必要な機能だけをRumiが選びます", badge: "推奨" },
  { value: "review", label: "使う前に確認", note: "候補を確認してから回答を開始します" },
  { value: "manual", label: "自分で選ぶ", note: "選んだ機能だけを候補にします" },
  { value: "none", label: "機能を使わない", note: "このメッセージでは外部機能を使いません" },
];

const STRATEGY_OPTIONS: Array<{ value: ToolSelectionStrategy; label: string; note: string; warning?: boolean }> = [
  { value: "hybrid", label: "自動：ベクトルで絞って別のAIが決める", note: "普段はこのままで、速さと精度を両立します" },
  { value: "semantic", label: "ベクトルで選ぶ", note: "本文との近さだけで候補を絞ります" },
  { value: "catalog_ai", label: "別のAIがすべての機能から選ぶ", note: "機能カタログを補助AIに渡して選ばせます" },
  { value: "all_with_hints", label: "すべて読み込む＋おすすめを付ける", note: "全スキーマを渡し、推奨順と理由も添えます", warning: true },
  { value: "all_schemas", label: "すべて読み込む", note: "全ツールスキーマをそのまま渡します", warning: true },
  { value: "lexical", label: "軽量キーワードで選ぶ", note: "埋め込みや補助AIを使わず、ローカルな語句一致で選びます" },
];

const STANDARD_PERMISSION_ROWS: Array<{ keys: string[]; label: string; note: string; allowAuto?: boolean }> = [
  { keys: ["read"], label: "読む", note: "ファイル、ブラウザ、外部サービスから情報を読む" },
  { keys: ["search"], label: "検索する", note: "Web、リポジトリ、接続済みサービスから探す" },
  { keys: ["create"], label: "作る", note: "ファイル、ドキュメント、チケットなどを新規作成する" },
  { keys: ["update"], label: "更新する", note: "既存のファイル、ドキュメント、チケットなどを編集する" },
  { keys: ["send"], label: "送信する", note: "メール、Slack、外部APIへ内容を送る" },
  { keys: ["execute"], label: "実行する", note: "コマンド、コード、端末操作を実行する" },
  { keys: ["computer"], label: "コンピュータ操作", note: "クリック、キーボード入力、画面操作を行う" },
  { keys: ["delete"], label: "削除・push・reset", note: "破壊的または戻しにくい操作を行う", allowAuto: false },
];

const ACTION_LABELS: Record<string, string> = {
  read: "読む",
  search: "検索",
  create: "作成",
  update: "更新",
  send: "送信",
  execute: "実行",
  computer: "PC操作",
  delete: "削除・push・reset",
};

const DECISION_LABELS: Record<PermissionDecision, string> = {
  auto: "自動で許可",
  confirm: "確認する",
  block: "使わない",
};

const TRACE_OPTIONS = [
  { value: "none", label: "なし", note: "選定の記録を残しません" },
  { value: "summary", label: "要約", note: "選ばれた機能と理由だけを記録します" },
  { value: "full", label: "詳細", note: "補助AIの入出力を非表示の子会話に残します" },
] as const;

const TABS = [
  { id: "basic", label: "基本" },
  { id: "permissions", label: "権限" },
  { id: "connections", label: "接続" },
  { id: "advanced", label: "高度な設定" },
] as const;

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" && value.trim() ? value : fallback;
}

function numberValue(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(Math.round(parsed), min), max);
}

function boolValue(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function permissionValue(value: unknown, fallback: PermissionDecision = "confirm"): PermissionDecision {
  return value === "auto" || value === "confirm" || value === "block" ? value : fallback;
}

function mostRestrictivePermission(values: PermissionDecision[]): PermissionDecision {
  if (values.includes("block")) return "block";
  if (values.includes("confirm")) return "confirm";
  return "auto";
}

function modelMatchesEmbedding(model: ModelSearchItem): boolean {
  const haystack = [
    model.display_name,
    model.label,
    model.model_id,
    model.qualified_model_id,
    model.provider_id,
    ...(Array.isArray(model.capability_tags) ? model.capability_tags : []),
    ...(Array.isArray(model.recommended_roles) ? model.recommended_roles : []),
  ].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes("embed") || haystack.includes("embedding") || haystack.includes("ベクトル");
}

function modelLabel(model: ModelSearchItem): string {
  return String(model.display_name || model.label || model.model_id || model.profile_id || "Model");
}

function modelSubtitle(model: ModelSearchItem): string {
  const parts = [model.provider_display_name || model.provider_id, model.configured ? "設定済み" : model.requires_api_key ? "API key 必要" : ""].filter(Boolean);
  return parts.join(" / ");
}

function serviceStatusLabel(status?: string): string {
  if (status === "connected") return "接続済み";
  if (status === "setup_required") return "設定が必要";
  if (status === "blocked") return "停止中";
  return "利用可能";
}

function summarizeServicePermission(serviceId: string, tools: ToolCatalogTool[], standard: Record<string, unknown>, overrides: Record<string, unknown>): string {
  const serviceOverrides = recordValue(overrides[serviceId]);
  const actionClasses = [...new Set(tools.filter((tool) => tool.service_id === serviceId).map((tool) => tool.action_class || "read_search"))];
  if (actionClasses.length === 0) return "権限設定なし";
  const labels = actionClasses.slice(0, 3).map((actionClass) => {
    const decision = permissionValue(serviceOverrides[actionClass] ?? standard[actionClass], "confirm");
    return `${ACTION_LABELS[actionClass] ?? actionClass}: ${DECISION_LABELS[decision]}`;
  });
  return labels.concat(actionClasses.length > 3 ? [`他${actionClasses.length - 3}`] : []).join(" / ");
}

function RadioCard<T extends string>({
  value,
  selected,
  label,
  note,
  badge,
  onSelect,
}: {
  value: T;
  selected: boolean;
  label: string;
  note: string;
  badge?: string;
  onSelect: (value: T) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(value)}
      className={cn(
        "flex min-h-[88px] items-start gap-3 rounded-lg border px-4 py-3 text-left transition-colors",
        selected ? "border-sky-500/60 bg-sky-500/10 text-zinc-50" : "border-zinc-800 bg-zinc-950/35 text-zinc-300 hover:border-zinc-700 hover:bg-zinc-900/50",
      )}
    >
      <span className={cn("mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full border", selected ? "border-sky-400 bg-sky-400 text-zinc-950" : "border-zinc-700")}>
        {selected && <Check size={13} />}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2 text-sm font-medium">
          {label}
          {badge && <span className="rounded-full border border-sky-500/30 px-2 py-0.5 text-[10px] text-sky-200">{badge}</span>}
        </span>
        <span className="mt-1 block text-xs leading-5 text-zinc-500">{note}</span>
      </span>
    </button>
  );
}

function PermissionSegmented({
  value,
  allowAuto = true,
  onChange,
}: {
  value: PermissionDecision;
  allowAuto?: boolean;
  onChange: (value: PermissionDecision) => void;
}) {
  const options: PermissionDecision[] = allowAuto ? ["auto", "confirm", "block"] : ["confirm", "block"];
  return (
    <div className="inline-grid min-w-[210px] grid-flow-col rounded-lg border border-zinc-800 bg-zinc-950/70 p-1">
      {options.map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => onChange(option)}
          className={cn(
            "rounded-md px-2.5 py-1.5 text-[11px] transition-colors",
            value === option ? "bg-zinc-700 text-zinc-50" : "text-zinc-500 hover:text-zinc-200",
          )}
        >
          {DECISION_LABELS[option]}
        </button>
      ))}
    </div>
  );
}

function ToggleRow({
  checked,
  title,
  note,
  onChange,
}: {
  checked: boolean;
  title: string;
  note: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className="flex w-full items-center justify-between gap-4 rounded-lg border border-zinc-800 bg-zinc-950/35 px-4 py-3 text-left hover:border-zinc-700"
    >
      <span>
        <span className="block text-sm font-medium text-zinc-100">{title}</span>
        <span className="mt-1 block text-xs leading-5 text-zinc-500">{note}</span>
      </span>
      <span className={cn("flex h-6 w-11 flex-shrink-0 items-center rounded-full border p-0.5 transition-colors", checked ? "border-sky-500/50 bg-sky-500/30" : "border-zinc-700 bg-zinc-900")}>
        <span className={cn("h-4.5 w-4.5 rounded-full bg-zinc-100 transition-transform", checked ? "translate-x-5" : "translate-x-0")} />
      </span>
    </button>
  );
}

function ModelPicker({
  label,
  note,
  value,
  models,
  loading,
  includeAuto = true,
  placeholder,
  onChange,
}: {
  label: string;
  note: string;
  value: string;
  models: ModelSearchItem[];
  loading: boolean;
  includeAuto?: boolean;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return models.slice(0, 60);
    return models.filter((model) => `${modelLabel(model)} ${modelSubtitle(model)} ${model.model_id ?? ""}`.toLowerCase().includes(needle)).slice(0, 60);
  }, [models, query]);
  const selected = models.find((model) => model.profile_id === value || model.qualified_model_id === value || model.model_id === value);
  return (
    <section className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
      <div>
        <h4 className="text-sm font-medium text-zinc-100">{label}</h4>
        <p className="mt-1 text-xs leading-5 text-zinc-500">{note}</p>
      </div>
      <div className="flex flex-col gap-3 lg:flex-row">
        <div className="relative flex-1">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-zinc-600" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={placeholder}
            className="w-full rounded-lg border border-zinc-800 bg-zinc-950 px-9 py-2 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-600 focus:border-zinc-600"
          />
        </div>
        <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-400 lg:min-w-[220px]">
          {selected ? modelLabel(selected) : value ? "保存済みモデル" : "自動"}
        </div>
      </div>
      <div className="max-h-72 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950/50">
        {includeAuto && (
          <button
            type="button"
            onClick={() => onChange("")}
            className={cn("flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left text-sm hover:bg-zinc-900", !value ? "text-sky-200" : "text-zinc-300")}
          >
            <span>
              <span className="block font-medium">自動（おすすめ）</span>
              <span className="block text-xs text-zinc-500">Tobkiriのモデル設定から選びます</span>
            </span>
            {!value && <Check size={16} />}
          </button>
        )}
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-4 text-xs text-zinc-500">
            <Loader2 size={14} className="animate-spin" />
            モデルを読み込んでいます
          </div>
        ) : filtered.length ? filtered.map((model) => {
          const modelValue = String(model.profile_id || model.qualified_model_id || model.model_id || "");
          const selectedModel = value === modelValue || value === model.qualified_model_id || value === model.model_id;
          return (
            <button
              key={modelValue || modelLabel(model)}
              type="button"
              onClick={() => onChange(modelValue)}
              className={cn("flex w-full items-center justify-between gap-3 px-3 py-2.5 text-left text-sm hover:bg-zinc-900", selectedModel ? "text-sky-200" : "text-zinc-300")}
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">{modelLabel(model)}</span>
                <span className="block truncate text-xs text-zinc-500">{modelSubtitle(model)}</span>
              </span>
              {selectedModel && <Check size={16} className="flex-shrink-0" />}
            </button>
          );
        }) : (
          <div className="px-3 py-4 text-xs text-zinc-500">該当するモデルがありません</div>
        )}
      </div>
    </section>
  );
}

export function ToolExperienceSettingsPanel({
  tools,
  settingsValues,
  onSettingChange,
  displayMode = "standard",
}: {
  tools: SidebarItem[];
  settingsValues: SettingsValues;
  onSettingChange: (sectionId: string, fieldId: string, value: unknown) => void;
  displayMode?: "standard" | "advanced" | "developer";
}) {
  const [activeTab, setActiveTab] = useState<typeof TABS[number]["id"]>("basic");
  const [catalog, setCatalog] = useState<ToolCatalogResponse | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [connectionFilter, setConnectionFilter] = useState<"all" | "connected" | "setup_required" | "blocked">("all");
  const [models, setModels] = useState<ModelSearchItem[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [previewText, setPreviewText] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewResult, setPreviewResult] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [warningStrategy, setWarningStrategy] = useState<ToolSelectionStrategy | null>(null);

  const toolSettings = settingsValues.tools ?? {};
  const modelSettings = settingsValues.models ?? {};
  const utilityModels = recordValue(modelSettings.utility_models);
  const standardPermissions = recordValue(toolSettings.standard_permissions);
  const serviceOverrides = recordValue(toolSettings.service_permission_overrides);
  const strategy = stringValue(toolSettings.selection_strategy, "hybrid") as ToolSelectionStrategy;
  const defaultMode = stringValue(toolSettings.default_mode, "auto") as ToolSelectionMode;

  useEffect(() => {
    let cancelled = false;
    setCatalogLoading(true);
    toolResources.toolCatalog()
      .then((result) => {
        if (!cancelled) setCatalog(result);
      })
      .catch(() => {
        if (!cancelled) setCatalog(null);
      })
      .finally(() => {
        if (!cancelled) setCatalogLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    setModelsLoading(true);
    Promise.all([
      toolResources.searchModels({ query: "", max_results: 200, type: ["chat", "reasoning"] }),
      toolResources.searchModels({ query: "", max_results: 100, type: "embedding" }),
    ])
      .then(([chatResult, embeddingResult]) => {
        if (!cancelled) {
          setModels([
            ...(Array.isArray(chatResult.models) ? chatResult.models : []),
            ...(Array.isArray(embeddingResult.models) ? embeddingResult.models : []),
          ]);
        }
      })
      .catch(() => {
        if (!cancelled) setModels([]);
      })
      .finally(() => {
        if (!cancelled) setModelsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const services = catalog?.services ?? [];
  const catalogTools = catalog?.tools ?? [];
  const filteredServices = services.filter((service) => connectionFilter === "all" || service.connection_status === connectionFilter);
  const chatModels = models.filter((model) => !modelMatchesEmbedding(model) && model.provider_id !== "human-operator");
  const embeddingModels = models.filter(modelMatchesEmbedding);

  const updateToolSetting = (fieldId: string, value: unknown) => onSettingChange("tools", fieldId, value);
  const updateUtilityModel = (roleId: string, value: string) => {
    onSettingChange("models", "utility_models", { ...utilityModels, [roleId]: value });
  };
  const updateStandardPermission = (keys: string[], value: PermissionDecision) => {
    const next = { ...standardPermissions };
    for (const key of keys) {
      next[key] = value;
    }
    updateToolSetting("standard_permissions", next);
  };
  const updateServiceOverride = (serviceId: string, actionClass: string, value: PermissionDecision) => {
    const nextService = { ...recordValue(serviceOverrides[serviceId]), [actionClass]: value };
    updateToolSetting("service_permission_overrides", { ...serviceOverrides, [serviceId]: nextService });
  };

  const handleStrategySelect = (value: ToolSelectionStrategy) => {
    if ((value === "all_schemas" || value === "all_with_hints") && value !== strategy) {
      setWarningStrategy(value);
      return;
    }
    updateToolSetting("selection_strategy", value);
  };

  const runPreview = async () => {
    setPreviewLoading(true);
    setPreviewResult(null);
    setPreviewError(null);
    try {
      const result = await toolResources.previewToolSelection({
        user_text: previewText || "この依頼に必要な機能を選んで",
        tool_selection: { mode: defaultMode, strategy },
      });
      const selected = result.decision.selected_tools.slice(0, 12).join(", ") || "なし";
      const recommendations = result.decision.recommendations.slice(0, 5).map((item) => item.reason ? `${item.tool_id}: ${item.reason}` : item.tool_id).join("\n");
      setPreviewResult(`選ばれた機能: ${selected}${recommendations ? `\n\n理由:\n${recommendations}` : ""}`);
    } catch (error) {
      setPreviewError(error instanceof Error ? error.message : "選定を試せませんでした");
    } finally {
      setPreviewLoading(false);
    }
  };

  const renderBasic = () => (
    <div className="space-y-5">
      <section className="space-y-3">
        <div>
          <h4 className="text-sm font-medium text-zinc-100">既定の使い方</h4>
          <p className="mt-1 text-xs leading-5 text-zinc-500">Composerでは毎回触らず、ここで普段の機能選定を決めます。</p>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {MODE_OPTIONS.map((option) => (
            <RadioCard
              key={option.value}
              value={option.value}
              selected={defaultMode === option.value}
              label={option.label}
              note={option.note}
              badge={option.badge}
              onSelect={(value) => updateToolSetting("default_mode", value)}
            />
          ))}
        </div>
      </section>
      <section className="grid gap-3 lg:grid-cols-2">
        <ToggleRow
          checked={boolValue(toolSettings.show_selected_tools_in_answer, true)}
          title="選んだ機能を回答内に表示"
          note="どの機能を使ったかを、必要な場面で回答に含めます。"
          onChange={(value) => updateToolSetting("show_selected_tools_in_answer", value)}
        />
        <ToggleRow
          checked={boolValue(toolSettings.expand_selection_reasoning, false)}
          title="選定理由を最初から展開して表示"
          note="候補選定の理由を折りたたまずに表示します。"
          onChange={(value) => updateToolSetting("expand_selection_reasoning", value)}
        />
      </section>
    </div>
  );

  const renderPermissions = () => (
    <div className="space-y-5">
      <section className="rounded-lg border border-sky-500/20 bg-sky-500/10 p-4">
        <div className="flex gap-3">
          <Shield size={18} className="mt-0.5 flex-shrink-0 text-sky-200" />
          <p className="text-xs leading-6 text-sky-100/90">
            機能の選定と、実行時の許可は別です。ここで「確認する」にした操作は、実行前にランタイムの承認UIへ渡されます。
          </p>
        </div>
      </section>
      <section className="space-y-3">
        {STANDARD_PERMISSION_ROWS.map((row) => {
          const value = mostRestrictivePermission(row.keys.map((key) => permissionValue(standardPermissions[key], key === "read" || key === "search" ? "auto" : "confirm")));
          return (
            <div key={row.keys.join("_")} className="flex flex-col gap-3 rounded-lg border border-zinc-800 bg-zinc-950/35 p-4 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h4 className="text-sm font-medium text-zinc-100">{row.label}</h4>
                <p className="mt-1 text-xs leading-5 text-zinc-500">{row.note}</p>
              </div>
              <PermissionSegmented value={value} allowAuto={row.allowAuto !== false} onChange={(next) => updateStandardPermission(row.keys, next)} />
            </div>
          );
        })}
      </section>
      <section className="space-y-3">
        <h4 className="text-sm font-medium text-zinc-100">サービスごとの上書き</h4>
        <div className="space-y-2">
          {services.slice(0, 24).map((service) => {
            const actions = [...new Set(catalogTools.filter((tool) => tool.service_id === service.service_id).map((tool) => tool.action_class || "read_search"))];
            return (
              <details key={service.service_id} className="rounded-lg border border-zinc-800 bg-zinc-950/35">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3">
                  <span>
                    <span className="block text-sm font-medium text-zinc-100">{service.label}</span>
                    <span className="mt-1 block text-xs text-zinc-500">{summarizeServicePermission(service.service_id, catalogTools, standardPermissions, serviceOverrides)}</span>
                  </span>
                  <ChevronDown size={16} className="text-zinc-500" />
                </summary>
                <div className="space-y-2 border-t border-zinc-800 p-4">
                  {actions.map((actionClass) => (
                    <div key={actionClass} className="flex flex-col gap-2 rounded-lg bg-zinc-950/60 px-3 py-2.5 lg:flex-row lg:items-center lg:justify-between">
                      <div className="text-sm text-zinc-300">{ACTION_LABELS[actionClass] ?? actionClass}</div>
                      <PermissionSegmented
                        value={permissionValue(recordValue(serviceOverrides[service.service_id])[actionClass] ?? standardPermissions[actionClass], "confirm")}
                        allowAuto={actionClass !== "dangerous" && actionClass !== "delete"}
                        onChange={(next) => updateServiceOverride(service.service_id, actionClass, next)}
                      />
                    </div>
                  ))}
                </div>
              </details>
            );
          })}
          {!services.length && (
            <div className="rounded-lg border border-zinc-800 bg-zinc-950/35 p-4 text-sm text-zinc-500">
              {catalogLoading ? "サービスを読み込んでいます" : "サービス情報を取得できませんでした"}
            </div>
          )}
        </div>
      </section>
    </div>
  );

  const renderConnections = () => (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {(["all", "connected", "setup_required", "blocked"] as const).map((filter) => (
          <button
            key={filter}
            type="button"
            onClick={() => setConnectionFilter(filter)}
            className={cn("rounded-lg border px-3 py-1.5 text-xs", connectionFilter === filter ? "border-sky-500/50 bg-sky-500/10 text-sky-100" : "border-zinc-800 text-zinc-400 hover:border-zinc-700")}
          >
            {filter === "all" ? "すべて" : filter === "connected" ? "接続済み" : filter === "setup_required" ? "設定が必要" : "停止中"}
          </button>
        ))}
      </div>
      <div className="grid gap-3 lg:grid-cols-2">
        {filteredServices.map((service: ToolCatalogService) => (
          <article key={service.service_id} className="rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h4 className="text-sm font-medium text-zinc-100">{service.label}</h4>
                {service.summary && <p className="mt-1 text-xs leading-5 text-zinc-500">{service.summary}</p>}
              </div>
              <span className="rounded-full border border-zinc-700 px-2 py-1 text-[10px] text-zinc-400">{serviceStatusLabel(service.connection_status)}</span>
            </div>
            <div className="mt-4 grid gap-2 text-xs text-zinc-400">
              <div className="flex justify-between gap-3"><span>内部機能</span><span>{service.tool_count ?? catalogTools.filter((tool) => tool.service_id === service.service_id).length}件</span></div>
              <div className="flex justify-between gap-3"><span>権限</span><span className="text-right">{summarizeServicePermission(service.service_id, catalogTools, standardPermissions, serviceOverrides)}</span></div>
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              <button type="button" onClick={() => setActiveTab("permissions")} className="rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-300 hover:border-zinc-700">権限を調整</button>
              <button type="button" onClick={() => setActiveTab("advanced")} className="rounded-lg border border-zinc-800 px-3 py-1.5 text-xs text-zinc-300 hover:border-zinc-700">詳細を見る</button>
            </div>
          </article>
        ))}
      </div>
      {!filteredServices.length && (
        <div className="rounded-lg border border-zinc-800 bg-zinc-950/35 p-4 text-sm text-zinc-500">
          {catalogLoading ? "接続情報を読み込んでいます" : "条件に合うサービスがありません"}
        </div>
      )}
    </div>
  );

  const renderAdvanced = () => (
    <div className="space-y-6">
      <section className="space-y-3">
        <div>
          <h4 className="text-sm font-medium text-zinc-100">選定方式</h4>
          <p className="mt-1 text-xs leading-5 text-zinc-500">補助AIが見る情報、メインAIに渡すスキーマ量、速度のバランスを選びます。</p>
        </div>
        <div className="grid gap-3 lg:grid-cols-2">
          {STRATEGY_OPTIONS.map((option) => (
            <RadioCard
              key={option.value}
              value={option.value}
              selected={strategy === option.value}
              label={option.label}
              note={option.note}
              badge={option.warning ? "大量のスキーマ" : undefined}
              onSelect={handleStrategySelect}
            />
          ))}
        </div>
      </section>
      <section className="grid gap-3 lg:grid-cols-3">
        <label className="rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
          <span className="block text-sm font-medium text-zinc-100">候補数</span>
          <span className="mt-1 block text-xs text-zinc-500">semantic_candidate_limit</span>
          <input type="number" min={8} max={64} value={numberValue(toolSettings.semantic_candidate_limit, 24, 8, 64)} onChange={(event) => updateToolSetting("semantic_candidate_limit", numberValue(event.target.value, 24, 8, 64))} className="mt-3 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-600" />
        </label>
        <label className="rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
          <span className="block text-sm font-medium text-zinc-100">最終ツール数</span>
          <span className="mt-1 block text-xs text-zinc-500">final_tool_limit</span>
          <input type="number" min={1} max={24} value={numberValue(toolSettings.final_tool_limit, 8, 1, 24)} onChange={(event) => updateToolSetting("final_tool_limit", numberValue(event.target.value, 8, 1, 24))} className="mt-3 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-600" />
        </label>
        <label className="rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
          <span className="block text-sm font-medium text-zinc-100">全カタログ直読み上限</span>
          <span className="mt-1 block text-xs text-zinc-500">catalog_ai_direct_limit</span>
          <input type="number" min={20} max={200} value={numberValue(toolSettings.catalog_ai_direct_limit, 80, 20, 200)} onChange={(event) => updateToolSetting("catalog_ai_direct_limit", numberValue(event.target.value, 80, 20, 200))} className="mt-3 w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-zinc-600" />
        </label>
      </section>
      <div className="grid gap-4 xl:grid-cols-2">
        <ModelPicker
          label="Tool補助モデル"
          note="機能カタログを読む補助AIです。自由入力ではなく、登録済みモデルから選びます。"
          value={stringValue(utilityModels.tool_selector)}
          models={chatModels}
          loading={modelsLoading}
          placeholder="補助AIモデルを検索"
          onChange={(value) => updateUtilityModel("tool_selector", value)}
        />
        <ModelPicker
          label="ベクトルモデル"
          note="ベクトル選定に使う埋め込みモデルです。未設定ならローカル語句選定にフォールバックします。"
          value={stringValue(toolSettings.embedding_model)}
          models={embeddingModels}
          loading={modelsLoading}
          placeholder="埋め込みモデルを検索"
          onChange={(value) => updateToolSetting("embedding_model", value)}
        />
      </div>
      <section className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
        <div>
          <h4 className="text-sm font-medium text-zinc-100">Tool補助の記録</h4>
          <p className="mt-1 text-xs leading-5 text-zinc-500">詳細は非表示の子会話へ保存し、通常の会話ログを汚しません。</p>
        </div>
        <div className="grid gap-2 lg:grid-cols-3">
          {TRACE_OPTIONS.map((option) => (
            <RadioCard
              key={option.value}
              value={option.value}
              selected={stringValue(toolSettings.selector_trace, "summary") === option.value}
              label={option.label}
              note={option.note}
              onSelect={(value) => updateToolSetting("selector_trace", value)}
            />
          ))}
        </div>
      </section>
      <section className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
        <div className="flex items-center gap-2">
          <Sparkles size={16} className="text-sky-200" />
          <h4 className="text-sm font-medium text-zinc-100">選定を試す</h4>
        </div>
        <textarea
          value={previewText}
          onChange={(event) => setPreviewText(event.target.value)}
          rows={3}
          placeholder="例: Gmailから未返信を探してSlackに要約して"
          className="w-full resize-none rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
        />
        <div className="flex items-center gap-3">
          <button type="button" onClick={runPreview} disabled={previewLoading} className="inline-flex items-center gap-2 rounded-lg border border-sky-500/40 bg-sky-500/10 px-3 py-2 text-sm text-sky-100 disabled:opacity-60">
            {previewLoading ? <Loader2 size={14} className="animate-spin" /> : <Wrench size={14} />}
            選定を試す
          </button>
          <span className="text-xs text-zinc-500">現在の設定でプレビューします</span>
        </div>
        {previewError ? (
          <ErrorNotice
            className="text-xs leading-5"
            copyLabel="ツール選定プレビューエラーをコピー"
            message={previewError}
            title="選定を試せませんでした"
          />
        ) : previewResult ? (
          <pre className="max-h-60 overflow-auto rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-xs leading-5 text-zinc-300 whitespace-pre-wrap">{previewResult}</pre>
        ) : null}
      </section>
      <details className="rounded-lg border border-zinc-800 bg-zinc-950/35">
        <summary className="cursor-pointer list-none px-4 py-3 text-sm font-medium text-zinc-100">個別ツールの管理</summary>
        <div className="border-t border-zinc-800 p-4">
          <ToolSettingsPanel
            tools={tools}
            disabledToolIds={Array.isArray(toolSettings.disabled_tool_ids) ? toolSettings.disabled_tool_ids.map((item) => String(item)).filter(Boolean) : []}
            hiddenToolIds={Array.isArray(toolSettings.hidden_tool_ids) ? toolSettings.hidden_tool_ids.map((item) => String(item)).filter(Boolean) : []}
            toolPermissionOverrides={recordValue(toolSettings.tool_permission_overrides)}
            onSettingChange={onSettingChange}
          />
        </div>
      </details>
      {warningStrategy && (
        <ModalFoundation
          variant="alertdialog"
          title="大量のツールスキーマを読み込みます"
          description="この方式はメインAIへ渡す情報が増え、遅くなったりコストが増えたりします。"
          onClose={() => setWarningStrategy(null)}
          backdropClassName="fixed inset-0 rumi-layer-modal flex items-center justify-center bg-black/55 p-4"
          panelClassName="max-w-lg rounded-xl border border-amber-500/30 bg-zinc-950 p-5 shadow-2xl outline-none"
        >
            <div className="flex gap-3">
              <AlertTriangle size={20} className="mt-0.5 flex-shrink-0 text-amber-300" />
              <div>
                <h4 className="text-base font-medium text-zinc-100">大量のツールスキーマを読み込みます</h4>
                <p className="mt-2 text-sm leading-6 text-zinc-400">
                  この方式はメインAIへ渡す情報が増え、遅くなったりコストが増えたりします。確認したうえで保存します。
                </p>
              </div>
            </div>
            <div className="mt-5 flex justify-end gap-2">
              <button type="button" onClick={() => setWarningStrategy(null)} className="rounded-lg border border-zinc-800 px-3 py-2 text-sm text-zinc-300">キャンセル</button>
              <button type="button" onClick={() => { updateToolSetting("selection_strategy", warningStrategy); setWarningStrategy(null); }} className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">保存する</button>
            </div>
        </ModalFoundation>
      )}
    </div>
  );

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2 border-b border-zinc-800 pb-3">
        {TABS.filter((tab) => tab.id !== "advanced" || displayMode === "developer").map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            className={cn("rounded-lg px-3 py-1.5 text-sm transition-colors", activeTab === tab.id ? "bg-zinc-800 text-zinc-50" : "text-zinc-500 hover:bg-zinc-900 hover:text-zinc-200")}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {activeTab === "basic" && renderBasic()}
      {activeTab === "permissions" && renderPermissions()}
      {activeTab === "connections" && renderConnections()}
      {activeTab === "advanced" && displayMode === "developer" && renderAdvanced()}
    </div>
  );
}
