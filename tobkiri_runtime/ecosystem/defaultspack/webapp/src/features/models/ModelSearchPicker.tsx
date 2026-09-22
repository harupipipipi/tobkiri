import { useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Check, ChevronDown, Loader2, Search, X } from "lucide-react";

import type { ModelSearchItem } from "../../lib/api";
import { cn } from "../../lib/cn";
import {
  buildVisibleModelOptions,
  filterModelOptionsByProvider,
  filterModelProviderOptions,
  findSelectedModelOption,
  modelSearchItemToModelSelectOption,
  modelProviderOptions,
  parseModelProviderQuery,
  modelSelectDisplay,
  type ModelSelectOption,
} from "./modelSelect";
import {
  DEFAULT_MODEL_SELECTOR_SCHEMA,
  filterModelOptionsBySelector,
  modelSelectorSchemaForSurface,
  type ModelSelectorSchema,
  type ModelSelectorSurface,
} from "./modelSelectorSchema";

export type ModelSearchPickerVariant = "settings" | "compact";

export function ModelSearchPicker({
  value,
  options = [],
  remoteResults = [],
  query,
  loading = false,
  error = "",
  placeholder = "model/provider/特徴メモで検索",
  emptyText = "一致するモデルがありません。",
  clearLabel,
  variant = "settings",
  maxVisibleOptions,
  selectorSchema = DEFAULT_MODEL_SELECTOR_SCHEMA,
  surface = "settings",
  open: controlledOpen,
  onOpenChange,
  onChange,
  onQueryChange,
  onSearch,
}: {
  value: string;
  options?: ModelSelectOption[];
  remoteResults?: ModelSearchItem[];
  query: string;
  loading?: boolean;
  error?: string;
  placeholder?: string;
  emptyText?: string;
  clearLabel?: string;
  variant?: ModelSearchPickerVariant;
  maxVisibleOptions?: number;
  selectorSchema?: ModelSelectorSchema;
  surface?: ModelSelectorSurface;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  onChange: (value: string) => void;
  onQueryChange: (value: string) => void;
  onSearch?: (query: string) => void;
}) {
  const [internalOpen, setInternalOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const [popoverStyle, setPopoverStyle] = useState<CSSProperties | null>(null);
  const open = controlledOpen ?? internalOpen;
  const trimmedQuery = query.trim();
  const resolvedSchema = useMemo(
    () => modelSelectorSchemaForSurface(selectorSchema, surface),
    [selectorSchema, surface],
  );
  const filteredOptions = useMemo(
    () => filterModelOptionsBySelector(options, resolvedSchema, surface),
    [options, resolvedSchema, surface],
  );
  const remoteOptions = useMemo(
    () => filterModelOptionsBySelector(
      remoteResults.map(modelSearchItemToModelSelectOption),
      resolvedSchema,
      surface,
    ),
    [remoteResults, resolvedSchema, surface],
  );
  const providers = useMemo(
    () => modelProviderOptions([...filteredOptions, ...remoteOptions]),
    [filteredOptions, remoteOptions],
  );
  const providerState = useMemo(
    () => parseModelProviderQuery(query, providers, resolvedSchema.layout.provider_trigger),
    [providers, query, resolvedSchema.layout.provider_trigger],
  );
  const visibleProviders = useMemo(
    () => filterModelProviderOptions(providers, providerState.providerQuery),
    [providerState.providerQuery, providers],
  );
  const selected = findSelectedModelOption(options, value, remoteOptions);
  const visibleSelected = filteredOptions.find(
    (option) => option.value === value || option.qualified_model_id === value,
  ) ?? remoteOptions.find(
    (option) => option.value === value || option.qualified_model_id === value,
  ) ?? null;
  const selectedDisplay = selected ? modelSelectDisplay(selected) : null;
  const visibleOptions = useMemo(() => {
    const providerFilteredOptions = filterModelOptionsByProvider(filteredOptions, providerState.providerId);
    const providerFilteredRemoteOptions = filterModelOptionsByProvider(remoteOptions, providerState.providerId);
    const providerSelected = providerState.providerId
      ? filterModelOptionsByProvider(visibleSelected ? [visibleSelected] : [], providerState.providerId)[0] ?? null
      : visibleSelected;
    const built = buildVisibleModelOptions({
      options: providerFilteredOptions,
      selected: providerSelected,
      remoteOptions: providerFilteredRemoteOptions,
      query: providerState.providerId ? providerState.modelQuery : trimmedQuery,
    });
    const configuredLimit = resolvedSchema.layout.max_visible_options;
    const limit = typeof maxVisibleOptions === "number" ? maxVisibleOptions : configuredLimit;
    return built.slice(0, limit);
  }, [
    filteredOptions,
    maxVisibleOptions,
    providerState.modelQuery,
    providerState.providerId,
    remoteOptions,
    resolvedSchema.layout.max_visible_options,
    trimmedQuery,
    visibleSelected,
  ]);

  function setOpen(nextOpen: boolean) {
    if (controlledOpen === undefined) setInternalOpen(nextOpen);
    onOpenChange?.(nextOpen);
  }

  function pick(value: string) {
    onChange(value);
    setOpen(false);
  }

  function search() {
    onSearch?.(query);
  }

  function pickProvider(providerId: string) {
    onQueryChange(`${resolvedSchema.layout.provider_trigger}${providerId} `);
  }

  const compact = variant === "compact";

  useLayoutEffect(() => {
    if (!open) {
      setPopoverStyle(null);
      return undefined;
    }
    const update = () => {
      const rect = buttonRef.current?.getBoundingClientRect();
      if (!rect) return;
      const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
      const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
      const margin = 8;
      const desiredWidth = Math.max(
        rect.width,
        compact ? Math.min(280, resolvedSchema.layout.popover_width_px) : resolvedSchema.layout.popover_width_px,
      );
      const width = Math.min(desiredWidth, Math.max(220, viewportWidth - margin * 2));
      const left = Math.min(Math.max(margin, rect.left), Math.max(margin, viewportWidth - width - margin));
      const belowTop = rect.bottom + 6;
      const belowHeight = Math.max(0, viewportHeight - belowTop - margin);
      const aboveHeight = Math.max(0, rect.top - margin - 6);
      const forcedPlacement = resolvedSchema.layout.placement;
      const preferAbove = forcedPlacement === "above"
        || (forcedPlacement === "auto" && belowHeight < 220 && aboveHeight > belowHeight);
      const availableHeight = preferAbove ? aboveHeight : belowHeight;
      const configuredMaxHeight = compact
        ? Math.min(330, resolvedSchema.layout.popover_max_height_px)
        : resolvedSchema.layout.popover_max_height_px;
      const maxHeight = Math.max(180, Math.min(configuredMaxHeight, availableHeight));
      const top = preferAbove ? Math.max(margin, rect.top - 6 - maxHeight) : belowTop;
      setPopoverStyle({
        left,
        top,
        width,
        maxHeight,
      });
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [
    compact,
    open,
    resolvedSchema.layout.placement,
    resolvedSchema.layout.popover_max_height_px,
    resolvedSchema.layout.popover_width_px,
  ]);

  return (
    <div className="relative min-w-0" data-model-search-picker={variant}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen(!open)}
        className={cn(
          "flex w-full items-center justify-between gap-2 border text-left outline-none transition-colors",
          compact
            ? "h-7 rounded-md border-zinc-800 bg-zinc-950 px-2 text-[11px] text-zinc-200 hover:border-zinc-700 focus:border-sky-500/70"
            : "rounded-lg border-zinc-800 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 hover:border-zinc-700 focus:border-emerald-500/70",
        )}
        style={!compact ? { minHeight: resolvedSchema.layout.trigger_height_px } : undefined}
        title={selectedDisplay?.subtitle || value || "モデルを選択"}
      >
        <span className="min-w-0">
          <span className="block truncate">{selectedDisplay?.label || value || "モデルを選択"}</span>
          {!compact && selectedDisplay?.subtitle && (
            <span className="block truncate text-[11px] text-zinc-500">{selectedDisplay.subtitle}</span>
          )}
        </span>
        <ChevronDown size={compact ? 12 : 14} className={cn("shrink-0 text-zinc-500 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <>
          <button type="button" aria-label="モデル検索を閉じる" className="fixed inset-0 rumi-layer-panel cursor-default" onClick={() => setOpen(false)} />
          <div
            className={cn(
              "fixed rumi-layer-local-popover flex flex-col overflow-hidden border border-zinc-700 bg-zinc-950 shadow-2xl",
              compact ? "rounded-md" : "rounded-lg",
            )}
            style={popoverStyle ?? undefined}
          >
            {resolvedSchema.layout.show_search && (
              <label className={cn(
                "m-2 flex items-center gap-2 border border-zinc-800 bg-black/30 px-3 text-zinc-500 focus-within:border-zinc-600 focus-within:text-zinc-300",
                compact ? "h-8 rounded-md text-[11px]" : "h-9 rounded-lg text-xs",
              )}>
                <Search size={compact ? 13 : 14} />
                <input
                  autoFocus
                  value={query}
                  onChange={(event) => onQueryChange(event.target.value)}
                  onKeyDown={(event) => {
                    const confirmsProvider = providerState.active
                      && (event.key === resolvedSchema.layout.provider_confirm_key || event.key === "Enter");
                    if (confirmsProvider && visibleProviders[0]) {
                      event.preventDefault();
                      pickProvider(visibleProviders[0].provider_id);
                      return;
                    }
                    if (event.key === "Enter") {
                      event.preventDefault();
                      search();
                    }
                    if (event.key === "Escape") {
                      setOpen(false);
                    }
                  }}
                  placeholder={placeholder}
                  className="min-w-0 flex-1 bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
                />
                {loading && <Loader2 size={13} className="animate-spin text-zinc-500" />}
                {query && (
                  <button
                    type="button"
                    onClick={() => onQueryChange("")}
                    className="rounded p-0.5 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300"
                    aria-label="モデル検索をクリア"
                  >
                    <X size={13} />
                  </button>
                )}
                {onSearch && !providerState.active && (
                  <button
                    type="button"
                    onClick={search}
                    disabled={loading}
                    className="rounded p-0.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40"
                    aria-label="モデルを検索"
                  >
                    <Search size={13} />
                  </button>
                )}
              </label>
            )}
            {error && <div className="border-t border-zinc-800 px-3 py-2 text-[11px] text-rose-300">{error}</div>}
            {clearLabel && value && (
              <button
                type="button"
                onClick={() => pick("")}
                className="flex w-full items-center justify-between border-t border-zinc-800 px-3 py-2 text-left text-[11px] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
              >
                <span>{clearLabel}</span>
                <X size={12} />
              </button>
            )}
            <div
              className="min-h-0 overflow-y-auto border-t border-zinc-800 p-1"
              style={popoverStyle?.maxHeight ? {
                maxHeight: Math.max(
                  120,
                  Number(popoverStyle.maxHeight) - (resolvedSchema.layout.show_search ? 96 : 24),
                ),
              } : undefined}
            >
              {providerState.active ? (
                visibleProviders.length > 0 ? visibleProviders.map((provider) => (
                  <button
                    key={provider.provider_id}
                    type="button"
                    onClick={() => pickProvider(provider.provider_id)}
                    className={cn(
                      "flex w-full items-center justify-between rounded-md text-left text-zinc-300 transition-colors hover:bg-zinc-900 hover:text-zinc-100",
                      compact ? "px-2 py-1.5 text-[11px]" : "px-2.5 py-2 text-sm",
                    )}
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-medium">@{provider.provider_id}</span>
                      {provider.label !== provider.provider_id && (
                        <span className="block truncate text-[11px] text-zinc-500">{provider.label}</span>
                      )}
                    </span>
                    {resolvedSchema.layout.show_provider_count && (
                      <span className="rounded-full border border-zinc-800 px-2 py-0.5 text-[10px] text-zinc-500">
                        {provider.model_count}
                      </span>
                    )}
                  </button>
                )) : (
                  <div className="px-3 py-5 text-xs text-zinc-600">一致するproviderがありません。</div>
                )
              ) : visibleOptions.length > 0 ? visibleOptions.map((option) => {
                const active = option.value === value || option.qualified_model_id === value;
                const display = modelSelectDisplay(option);
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => pick(option.value)}
                    className={cn(
                      "flex w-full items-start justify-between gap-3 rounded-md text-left transition-colors",
                      compact ? "px-2 py-1.5" : "px-2.5 py-2",
                      active ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200",
                    )}
                  >
                    <span className="min-w-0">
                      <span className={cn("block truncate font-medium text-zinc-100", compact ? "text-[11px]" : "text-sm")}>{display.label}</span>
                      <span className="block truncate text-[11px] text-zinc-500">{display.subtitle}</span>
                    </span>
                    <span className="flex max-w-[160px] flex-wrap justify-end gap-1">
                      {display.badges.slice(0, compact ? 2 : 4).map((badge) => (
                        <span key={badge.id} className="rounded-full border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400">
                          {badge.label}
                        </span>
                      ))}
                      {active && <Check size={13} className="mt-1 shrink-0 text-emerald-300" />}
                    </span>
                  </button>
                );
              }) : (
                <div className="px-3 py-5 text-xs text-zinc-600">{emptyText}</div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
