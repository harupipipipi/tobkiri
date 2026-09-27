import { useEffect, useId, useRef, useState } from "react";
import { Info } from "lucide-react";

import type { ModelSearchItem } from "../../../lib/api";
import {
  enrichModelSelectOptions,
  ModelSearchPicker,
  findSelectedModelOption,
  modelProviderOptions,
  modelFieldOptionToModelSelectOption,
  modelOptionNeedsVisionRecommendation,
  modelOptionThinkingLevels,
  modelSearchItemToModelSelectOption,
  parseModelProviderQuery,
  parseModelSelectorSchema,
  type ModelSelectorSchema,
  type ModelSelectOption,
} from "../../../features/models";
import { settingsApiResources } from "../../../features/settings/resources/settingsApiResources";
import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { fieldOptions, modelSelectTargetFieldId, SettingsFieldShell } from "./settingsFieldRendererUtils";

export function SettingsModelSearchField({
  value,
  options,
  onChange,
  thinkingLevelsByProfile,
  onThinkingLevelChange,
  recommendVision = false,
  placeholder = "model/provider/特徴メモで検索",
  selectorSchema,
}: {
  value: string;
  options: ModelSelectOption[];
  onChange: (value: string) => void;
  thinkingLevelsByProfile?: Record<string, unknown>;
  onThinkingLevelChange?: (value: string, option: ModelSelectOption | null) => void;
  recommendVision?: boolean;
  placeholder?: string;
  selectorSchema?: ModelSelectorSchema;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [remoteResults, setRemoteResults] = useState<ModelSearchItem[]>([]);
  const [rememberedSelectedOption, setRememberedSelectedOption] = useState<ModelSelectOption | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const searchRequestSeq = useRef(0);
  const trimmedQuery = query.trim();
  const resolvedSelectorSchema = selectorSchema ?? parseModelSelectorSchema(undefined);
  const providerQueryState = parseModelProviderQuery(
    query,
    modelProviderOptions(options),
    resolvedSelectorSchema.layout.provider_trigger,
  );
  const remoteOptions = remoteResults.map(modelSearchItemToModelSelectOption);
  const rememberedOptions = rememberedSelectedOption?.value === value
    && !options.some((option) => (
      option.value === value || option.qualified_model_id === value
    ))
    ? [rememberedSelectedOption]
    : [];
  const pickerOptions = rememberedOptions.length > 0
    ? [...options, ...rememberedOptions]
    : options;
  const selectedOption = findSelectedModelOption(pickerOptions, value, remoteOptions);
  const supportedThinkingLevels = modelOptionThinkingLevels(selectedOption);
  const storedThinkingLevel = String(thinkingLevelsByProfile?.[value] ?? "").trim().toLowerCase();
  const defaultThinkingLevel = String(selectedOption?.default_thinking_level ?? "").trim().toLowerCase();
  const selectedThinkingLevel = supportedThinkingLevels.includes(storedThinkingLevel)
    ? storedThinkingLevel
    : supportedThinkingLevels.includes(defaultThinkingLevel)
      ? defaultThinkingLevel
      : supportedThinkingLevels.includes("medium")
        ? "medium"
        : supportedThinkingLevels[0] ?? "";
  const needsVisionRecommendation = recommendVision
    && modelOptionNeedsVisionRecommendation(selectedOption, value);

  useEffect(() => {
    if (!open) return;
    searchRequestSeq.current += 1;
    const requestSeq = searchRequestSeq.current;
    if (!trimmedQuery || providerQueryState.active) {
      setRemoteResults([]);
      setBusy(false);
      setError("");
      return;
    }
    let disposed = false;
    const timer = window.setTimeout(() => {
      setBusy(true);
      setError("");
      settingsApiResources.searchModels({
        query: providerQueryState.providerId ? providerQueryState.modelQuery : trimmedQuery,
        max_results: 30,
        ...(providerQueryState.providerId ? { provider_id: providerQueryState.providerId } : {}),
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
    providerQueryState.active,
    providerQueryState.modelQuery,
    providerQueryState.providerId,
    trimmedQuery,
  ]);

  return (
    <div data-settings-renderer="model_select">
      <ModelSearchPicker
        value={value}
        options={pickerOptions}
        remoteResults={remoteResults}
        query={query}
        loading={busy}
        error={error}
        placeholder={placeholder}
        selectorSchema={selectorSchema}
        surface="settings"
        open={open}
        onOpenChange={setOpen}
        onChange={onChange}
        onSelectedOptionChange={setRememberedSelectedOption}
        onQueryChange={setQuery}
      />
      {supportedThinkingLevels.length > 0 && onThinkingLevelChange && (
        <label className="mt-3 flex items-center gap-3 text-xs text-zinc-400" data-settings-model-thinking>
          <span className="shrink-0">考える深さ</span>
          <select
            value={selectedThinkingLevel}
            onChange={(event) => onThinkingLevelChange(event.target.value, selectedOption)}
            className="min-h-8 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-900 px-2 text-xs text-zinc-200 outline-none hover:border-zinc-700 focus:border-emerald-500/70"
            aria-label="考える深さ"
          >
            {supportedThinkingLevels.map((level) => (
              <option key={level} value={level}>{thinkingLevelLabel(level)}</option>
            ))}
          </select>
        </label>
      )}
      {needsVisionRecommendation && <VisionRecommendation />}
    </div>
  );
}

export function BuiltinModelSelectRenderer({
  sectionId,
  field,
  value,
  sectionValues,
  modelProfiles = [],
  onChange,
}: SettingsFieldRendererProps) {
  const targetFieldId = modelSelectTargetFieldId(field);
  const selectedValue = String(sectionValues?.[targetFieldId] ?? value ?? field.default ?? "");
  const options = enrichModelSelectOptions(
    fieldOptions(field).map(modelFieldOptionToModelSelectOption),
    modelProfiles,
  );
  const thinkingLevelsByProfile = objectRecord(sectionValues?.thinking_level_by_profile);
  const isLightweightModel = targetFieldId === "lightweight_model";
  return (
    <SettingsFieldShell field={field}>
      <SettingsModelSearchField
        value={selectedValue}
        options={options}
        selectorSchema={parseModelSelectorSchema(field.selector_schema)}
        thinkingLevelsByProfile={thinkingLevelsByProfile}
        recommendVision={isLightweightModel}
        onChange={(nextValue) => onChange(sectionId, targetFieldId, nextValue)}
        onThinkingLevelChange={(nextLevel, selectedOption) => {
          const nextValues = withThinkingLevelForModel(
            thinkingLevelsByProfile,
            selectedValue,
            selectedOption,
            nextLevel,
          );
          if (nextValues) onChange(sectionId, "thinking_level_by_profile", nextValues);
        }}
      />
    </SettingsFieldShell>
  );
}

export function withThinkingLevelForModel(
  current: Record<string, unknown>,
  modelId: string,
  option: ModelSelectOption | null,
  requestedLevel: string,
): Record<string, unknown> | null {
  const levels = modelOptionThinkingLevels(option);
  const level = String(requestedLevel ?? "").trim().toLowerCase();
  if (!modelId || !levels.includes(level) || current[modelId] === level) return null;
  return { ...current, [modelId]: level };
}

function objectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function thinkingLevelLabel(level: string): string {
  const labels: Record<string, string> = {
    none: "なし",
    low: "Low",
    medium: "Medium",
    high: "High",
    xhigh: "XHigh",
  };
  return labels[level] ?? level;
}

function VisionRecommendation() {
  const [open, setOpen] = useState(false);
  const tooltipId = useId();
  return (
    <span
      className="relative mt-2 inline-flex items-center gap-1.5 text-[11px] text-red-300"
      data-settings-vision-recommendation
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <span>Vision対応モデルを推奨</span>
      <button
        type="button"
        aria-label="Vision対応モデルを推奨する理由"
        aria-describedby={open ? tooltipId : undefined}
        aria-expanded={open}
        onClick={() => setOpen(true)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        className="rounded text-red-300/80 outline-none hover:text-red-200 focus-visible:ring-1 focus-visible:ring-red-300/70"
      >
        <Info size={13} aria-hidden="true" />
      </button>
      {open && (
        <span
          id={tooltipId}
          role="tooltip"
          className="absolute bottom-full left-0 rumi-layer-local-popover mb-1 w-60 rounded-md border border-red-400/25 bg-zinc-950 px-2 py-1.5 leading-4 text-zinc-300 shadow-xl"
        >
          画像の添付や引き継ぎを使う場合は、Vision対応の軽量モデルを選ぶと処理が安定します。
        </span>
      )}
    </span>
  );
}
