import { useEffect, useRef, useState } from "react";
import { ModelPinButton } from "./ModelPinButton";
import { useModelPins } from "./useModelPins";
import { orderPinnedModels } from "./modelPins";
import { modelPinIdentityForOption } from "./modelPinIdentity";
import { Check, ChevronDown, X } from "lucide-react";
import { ViewportPopover } from "../../ui/layers/ViewportPopover";
import { ErrorNotice } from "../../components/ErrorNotice";
import type { ModelSearchItem } from "../../lib/api";
import { cn } from "../../lib/cn";
import { SharedSearchTemplate } from "../search/SharedSearchTemplate";
import { type SearchPreset } from "../search/searchQuery";
import { parseConfirmedSearchQuery } from "../search/searchQueryState";
import { useSearchQueryState } from "../search/useSearchQueryState";
import { useModelCatalogSearch } from "../search/useModelCatalogSearch";
import {
  buildVisibleModelOptions, filterModelOptionsByProvider, findSelectedModelOption,
  modelSearchItemToModelSelectOption, modelSelectDisplay, type ModelSelectOption,
} from "./modelSelect";
import {
  DEFAULT_MODEL_SELECTOR_SCHEMA, filterModelOptionsBySelector,
  modelSelectorSchemaForSurface, type ModelSelectorSchema, type ModelSelectorSurface,
} from "./modelSelectorSchema";

export type ModelSearchPickerVariant = "settings" | "compact";

/** Adapt model identities to the same query, catalogue and template as Spotlight. */
export function ModelSearchPicker({
  value, options = [], remoteResults = [], query, loading = false, error = "",
  placeholder = "モデルを検索", emptyText = "一致するモデルがありません。",
  clearLabel, variant = "settings", maxVisibleOptions,
  selectorSchema = DEFAULT_MODEL_SELECTOR_SCHEMA, surface = "settings",
  open: controlledOpen, onOpenChange, onChange, onSelectedOptionChange, onQueryChange,
  onSearch, preset = { kinds: ["model"] }, showTrigger = true, disabled = false,
  catalogOptionAdapter = modelSearchItemToModelSelectOption,
  excludeValues = [], triggerLabel,
}: {
  value: string; options?: ModelSelectOption[]; remoteResults?: ModelSearchItem[];
  query: string; loading?: boolean; error?: string; placeholder?: string;
  emptyText?: string; clearLabel?: string; variant?: ModelSearchPickerVariant;
  maxVisibleOptions?: number; selectorSchema?: ModelSelectorSchema; surface?: ModelSelectorSurface;
  open?: boolean; onOpenChange?: (open: boolean) => void; onChange: (value: string) => void | boolean;
  onSelectedOptionChange?: (option: ModelSelectOption | null) => void;
  onQueryChange: (value: string) => void; onSearch?: (query: string) => void;
  preset?: SearchPreset; showTrigger?: boolean; disabled?: boolean;
  catalogOptionAdapter?: (item: ModelSearchItem) => ModelSelectOption | null;
  excludeValues?: readonly string[];
  triggerLabel?: string;
}) {
  const [internalOpen, setInternalOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const open = controlledOpen ?? internalOpen;
  const [queryState, changeQueryState] = useSearchQueryState(query, onQueryChange);
  const parsed = parseConfirmedSearchQuery(queryState, preset);
  const catalogue = useModelCatalogSearch({
    text: parsed.text, providerIds: parsed.providerIds,
    connectionId: parsed.connectionId,
    enabled: open && !parsed.conflict && parsed.kinds.includes("model"),
  });
  const resolvedSchema = modelSelectorSchemaForSurface(selectorSchema, surface);
  const remoteOptions = [...remoteResults, ...catalogue.models].map(catalogOptionAdapter)
    .filter((option): option is ModelSelectOption => option !== null);
  const selected = findSelectedModelOption(options, value, remoteOptions);
  const selectedDisplay = selected ? modelSelectDisplay(selected) : null;
  const eligible = filterModelOptionsBySelector([...options, ...remoteOptions], resolvedSchema, surface)
    .filter((option) => !excludeValues.includes(option.value)
      && (!option.qualified_model_id || !excludeValues.includes(option.qualified_model_id)));
  const providerFiltered = parsed.providerIds.length
    ? parsed.providerIds.flatMap((provider) => filterModelOptionsByProvider(eligible, provider)) : eligible;
  const retained = providerFiltered.find((option) => option.value === value || option.qualified_model_id === value);
  const pageSize = maxVisibleOptions ?? resolvedSchema.layout.max_visible_options;
  const [visibleLimit, setVisibleLimit] = useState(pageSize);
  const [pinVisibilityFloor, setPinVisibilityFloor] = useState(0);
  useEffect(() => { setVisibleLimit(pageSize); setPinVisibilityFloor(0); }, [query, pageSize]);
  const modelPins = useModelPins();
  const matchingUnpinnedOptions = parsed.conflict || !parsed.kinds.includes("model") ? [] : buildVisibleModelOptions({
    options: providerFiltered, selected: !parsed.text && resolvedSchema.layout.selected_position === "first" ? retained : undefined,
    query: parsed.text, resultLimit: Number.MAX_SAFE_INTEGER,
  });
  const matchingOptions = orderPinnedModels(matchingUnpinnedOptions, modelPins.pins, modelPinIdentityForOption);
  const visibleOptions = matchingOptions.slice(0, Math.max(maxVisibleOptions ?? visibleLimit, pinVisibilityFloor));
  const togglePin = (option: ModelSelectOption) => {
    const identity = modelPinIdentityForOption(option);
    if (!modelPins.enabled || !identity) return;
    const nextPins = modelPins.isPinned(identity)
      ? modelPins.pins.filter((pin) => pin !== identity) : [...modelPins.pins, identity];
    const nextOrder = orderPinnedModels(matchingUnpinnedOptions, nextPins, modelPinIdentityForOption);
    const retainedRank = nextOrder.findIndex((item) => item === option);
    setPinVisibilityFloor((previous) => Math.max(previous, visibleOptions.length + 1, retainedRank + 1));
    modelPins.toggle(identity);
  };
  const setOpen = (next: boolean) => {
    if (controlledOpen === undefined) setInternalOpen(next);
    onOpenChange?.(next);
  };
  const pick = (option: ModelSelectOption | null) => {
    if (disabled || parsed.conflict) return;
    onSelectedOptionChange?.(option);
    if (onChange(option?.value ?? "") === false) return;
    setOpen(false);
  };
  const compact = variant === "compact";

  const searchError = error || catalogue.error;
  const contents = <>
    {searchError && <ErrorNotice message={searchError} severity="warning" copyLabel="モデル検索エラーをコピー"
      className="gap-2 p-2.5 text-sm leading-[22px] [&_[data-copy-action]]:h-6 [&_[data-copy-action]]:w-6 [&_[data-copy-icon]]:size-3"
      iconClassName="h-3.5 w-3.5" />}
    <SharedSearchTemplate
      queryState={queryState} onQueryStateChange={changeQueryState} preset={preset}
      inputLabel="モデルを検索" placeholder={placeholder} autoFocus density="compact"
      items={visibleOptions.map((option) => {
        const display = modelSelectDisplay(option);
        return { key: option.value, kind: "model" as const, title: display.label,
          description: [display.subtitle, option.connection_id].filter(Boolean).join(" · "), badge: display.badges.map((badge) => badge.label).join(" · "), value: option };
      })}
      onSelect={(item) => pick(item.value)} onEscape={() => setOpen(false)}
      loading={visibleOptions.length === 0 && (loading || catalogue.loading)}
      onRetry={() => { catalogue.retry(); onSearch?.(parsed.text); }} emptyMessage={searchError ? "表示できるモデルがありません。再試行してください。" : emptyText}
      trailingControls={searchError
        ? <button type="button" onClick={catalogue.retry} className="rounded px-1 py-0.5 text-[11px] text-zinc-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-400">再試行</button> : undefined}
      renderItemAction={(item) => <ModelPinButton label={`${item.title} · ${item.value.connection_id ?? item.value.provider_id ?? ""}/${item.value.model_id ?? ""}`}
        pinned={modelPins.isPinned(modelPinIdentityForOption(item.value))}
        disabled={!modelPins.enabled || !modelPinIdentityForOption(item.value)}
        onToggle={() => togglePin(item.value)} />}
      renderItem={(item) => <>
        <span className="min-w-0 flex-1"><span className="block truncate text-sm">{item.title}</span>
          <span className="block truncate text-xs text-zinc-400">{item.description}</span>
          {item.badge && <span className="block text-[10px] text-zinc-500">{item.badge}</span>}
        </span>{item.value.value === value && <Check size={14} className="shrink-0 text-emerald-300" />}
      </>}
    />
    {!maxVisibleOptions && matchingOptions.length > visibleOptions.length && <button type="button"
      onClick={() => setVisibleLimit((limit) => Math.max(limit, pinVisibilityFloor) + pageSize)} className="w-full border-t border-zinc-800 px-5 py-2 text-xs text-zinc-400">さらに表示</button>}
    {!parsed.conflict && parsed.kinds.includes("model") && catalogue.complete === false && !catalogue.loading && !catalogue.error && <p className="px-5 py-2 text-xs text-zinc-500">カタログの一部を表示しています。検索語で絞り込んでください。</p>}
    {clearLabel && value && <button type="button" disabled={disabled} onClick={() => pick(null)} className="flex w-full items-center justify-between px-5 py-2 text-xs text-zinc-400">{clearLabel}<X size={12} /></button>}
  </>;
  return <div className="relative min-w-0" data-model-search-picker={variant}>
    {showTrigger && <button ref={buttonRef} type="button" disabled={disabled} onClick={() => setOpen(!open)}
      className={cn("flex w-full items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-left text-sm text-zinc-200", compact && "h-7 px-2 text-[11px]")}
      title={selectedDisplay?.subtitle || value || "モデルを選択"}>
      <span className="min-w-0"><span className="block truncate">{triggerLabel || selectedDisplay?.label || value || "モデルを選択"}</span>
        {!triggerLabel && !compact && selectedDisplay?.subtitle && <span className="block truncate text-[11px] text-zinc-500">{selectedDisplay.subtitle}</span>}
      </span><ChevronDown size={14} className="shrink-0 text-zinc-500" />
    </button>}
    {open && (showTrigger ? <>
      <ViewportPopover anchorRef={buttonRef} onClose={() => setOpen(false)} label="モデル検索を閉じる"
        closeOnEscape={false} preferredPlacement={resolvedSchema.layout.placement}
        desiredWidth={resolvedSchema.layout.popover_width_px} maxHeight={resolvedSchema.layout.popover_max_height_px}
        className="rounded-lg border border-zinc-700 bg-zinc-950 shadow-2xl">{contents}</ViewportPopover>
    </> : contents)}
  </div>;
}
