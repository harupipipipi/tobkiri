import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
} from "react";
import { Check, ChevronDown, Loader2, Search, X } from "lucide-react";

import { ErrorNotice } from "../../components/ErrorNotice";
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
  modelSelectOptionAvailability,
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

export function nextModelOptionIndex(
  current: number,
  optionCount: number,
  key: string,
  pageSize = 10,
): number {
  if (optionCount <= 0) return -1;
  if (key === "Home") return 0;
  if (key === "End") return optionCount - 1;
  if (key === "ArrowDown") return current < 0 ? 0 : Math.min(optionCount - 1, current + 1);
  if (key === "ArrowUp") return current < 0 ? optionCount - 1 : Math.max(0, current - 1);
  if (key === "PageDown") return Math.min(optionCount - 1, Math.max(0, current) + pageSize);
  if (key === "PageUp") return Math.max(0, (current < 0 ? 0 : current) - pageSize);
  return current;
}

export function modelPickerShouldExpandResults(
  current: number,
  visibleCount: number,
  totalCount: number,
  key: string,
  pageSize = 10,
): boolean {
  if (totalCount <= visibleCount) return false;
  if (key === "End") return true;
  if (key === "ArrowDown") return current >= visibleCount - 1;
  if (key === "PageDown") return Math.max(0, current) + pageSize >= visibleCount;
  return false;
}

export function modelPickerResultMessage({
  total,
  visible,
  remote,
  loading,
  error,
  providers = false,
}: {
  total: number;
  visible: number;
  remote: number;
  loading: boolean;
  error: string;
  providers?: boolean;
}): string {
  if (loading) return "Loading model results.";
  if (error) return `Model search failed. ${error}`;
  const subject = providers ? "provider" : "model";
  return `${total} ${subject} result${total === 1 ? "" : "s"}.`
    + (!providers && remote > 0 ? ` ${remote} from remote search.` : "")
    + (visible < total ? ` Showing ${visible}.` : "");
}

export function reconcileActiveOptionIndex({
  keys,
  activeKey,
  selectedKey,
  current,
}: {
  keys: string[];
  activeKey: string;
  selectedKey?: string;
  current: number;
}): number {
  if (keys.length === 0) return -1;
  const preserved = activeKey ? keys.indexOf(activeKey) : -1;
  if (preserved >= 0) return preserved;
  const selected = selectedKey ? keys.indexOf(selectedKey) : -1;
  if (selected >= 0) return selected;
  return Math.min(Math.max(0, current), keys.length - 1);
}

/**
 * Editable combobox/listbox following the WAI-ARIA APG focus contract.
 * Focus stays on the input while aria-activedescendant tracks an option;
 * Enter selects, Escape cancels, and Tab closes without changing selection.
 */
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
  const [activeIndex, setActiveIndex] = useState(-1);
  const [showAll, setShowAll] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listboxRef = useRef<HTMLDivElement | null>(null);
  const optionRefs = useRef<Array<HTMLDivElement | null>>([]);
  const activeKeyRef = useRef("");
  const [popoverStyle, setPopoverStyle] = useState<CSSProperties | null>(null);
  const instanceId = useId().replace(/:/g, "");
  const triggerId = `model-picker-${instanceId}-trigger`;
  const inputId = `model-picker-${instanceId}-input`;
  const listboxId = `model-picker-${instanceId}-listbox`;
  const statusId = `model-picker-${instanceId}-status`;
  const instructionsId = `model-picker-${instanceId}-instructions`;
  const selectedDescriptionId = `model-picker-${instanceId}-selected`;
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
  const remoteOptionValues = useMemo(
    () => new Set(remoteOptions.flatMap((option) => (
      [option.value, option.qualified_model_id].filter(Boolean) as string[]
    ))),
    [remoteOptions],
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
  const allVisibleOptions = useMemo(() => {
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
      resultLimit: Number.MAX_SAFE_INTEGER,
    });
    return built;
  }, [
    filteredOptions,
    providerState.modelQuery,
    providerState.providerId,
    remoteOptions,
    trimmedQuery,
    visibleSelected,
  ]);
  const configuredLimit = typeof maxVisibleOptions === "number"
    ? Math.max(0, maxVisibleOptions)
    : resolvedSchema.layout.max_visible_options;
  const hasLimit = allVisibleOptions.length > configuredLimit;
  const showContinuation = !providerState.active && hasLimit && !showAll;
  const visibleOptions = hasLimit && !showAll
    ? allVisibleOptions.slice(0, configuredLimit)
    : allVisibleOptions;
  const remoteResultCount = allVisibleOptions.filter((option) => (
    remoteOptionValues.has(option.value)
      || Boolean(option.qualified_model_id && remoteOptionValues.has(option.qualified_model_id))
  )).length;
  const compact = variant === "compact";
  const activeCount = providerState.active ? visibleProviders.length : visibleOptions.length;
  const activeOptionId = activeIndex >= 0 && activeIndex < activeCount
    ? `${listboxId}-option-${activeIndex}`
    : undefined;
  const resultMessage = modelPickerResultMessage({
    total: providerState.active ? visibleProviders.length : allVisibleOptions.length,
    visible: providerState.active ? visibleProviders.length : visibleOptions.length,
    remote: remoteResultCount,
    loading,
    error,
    providers: providerState.active,
  });

  function activeKeyAt(index: number): string {
    if (providerState.active) {
      const provider = visibleProviders[index];
      return provider ? `provider:${provider.provider_id}` : "";
    }
    const option = visibleOptions[index];
    return option ? `model:${option.value}` : "";
  }

  function updateActiveIndex(next: number | ((current: number) => number)) {
    setActiveIndex((current) => {
      const resolved = typeof next === "function" ? next(current) : next;
      activeKeyRef.current = activeKeyAt(resolved);
      return resolved;
    });
  }

  function setOpen(nextOpen: boolean) {
    if (controlledOpen === undefined) setInternalOpen(nextOpen);
    onOpenChange?.(nextOpen);
  }

  function closeAndRestoreFocus() {
    setOpen(false);
    window.setTimeout(() => buttonRef.current?.focus(), 0);
  }

  function closeAndMoveFocus(backward: boolean) {
    setOpen(false);
    window.setTimeout(() => {
      const trigger = buttonRef.current;
      if (!trigger) return;
      const candidates = Array.from(document.querySelectorAll<HTMLElement>(
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.tabIndex >= 0 && element.getClientRects().length > 0);
      const triggerIndex = candidates.indexOf(trigger);
      const target = triggerIndex >= 0 ? candidates[triggerIndex + (backward ? -1 : 1)] : null;
      target?.focus();
    }, 0);
  }

  function pick(nextValue: string) {
    onChange(nextValue);
    closeAndRestoreFocus();
  }

  function search() {
    onSearch?.(query);
  }

  function pickProvider(providerId: string) {
    onQueryChange(`${resolvedSchema.layout.provider_trigger}${providerId} `);
    updateActiveIndex(0);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }

  function handleComboboxKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (
      event.key === "Enter"
      && (event.ctrlKey || event.metaKey)
      && onSearch
      && !providerState.active
    ) {
      event.preventDefault();
      search();
      return;
    }
    if (event.key === "Backspace" && event.altKey && query) {
      event.preventDefault();
      onQueryChange("");
      setShowAll(false);
      updateActiveIndex(0);
      return;
    }
    if (event.key === "Delete" && event.altKey && clearLabel && value) {
      event.preventDefault();
      pick("");
      return;
    }
    const confirmsProvider = providerState.active
      && !event.shiftKey
      && (
        event.key === resolvedSchema.layout.provider_confirm_key
        || event.key === "Enter"
      );
    if (confirmsProvider) {
      event.preventDefault();
      const provider = visibleProviders[Math.max(0, activeIndex)];
      if (provider) pickProvider(provider.provider_id);
      return;
    }
    if (["ArrowDown", "ArrowUp", "Home", "End", "PageDown", "PageUp"].includes(event.key)) {
      event.preventDefault();
      if (
        !providerState.active
        && showContinuation
        && modelPickerShouldExpandResults(
          activeIndex,
          visibleOptions.length,
          allVisibleOptions.length,
          event.key,
        )
      ) {
        const nextIndex = event.key === "End"
          ? allVisibleOptions.length - 1
          : Math.min(
            allVisibleOptions.length - 1,
            Math.max(0, activeIndex) + (event.key === "ArrowDown" ? 1 : 10),
          );
        const nextOption = allVisibleOptions[nextIndex];
        activeKeyRef.current = nextOption ? `model:${nextOption.value}` : "";
        setShowAll(true);
        setActiveIndex(nextIndex);
        return;
      }
      updateActiveIndex((current) => nextModelOptionIndex(current, activeCount, event.key));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const active = visibleOptions[activeIndex];
      if (active) pick(active.value);
      else search();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeAndRestoreFocus();
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      closeAndMoveFocus(event.shiftKey);
    }
  }

  useEffect(() => {
    if (!open) return;
    setShowAll(false);
    window.setTimeout(() => {
      if (resolvedSchema.layout.show_search) inputRef.current?.focus();
      else listboxRef.current?.focus();
    }, 0);
  }, [open, resolvedSchema.layout.show_search]);

  useEffect(() => {
    if (!open) return;
    updateActiveIndex((current) => {
      const keys = providerState.active
        ? visibleProviders.map((provider) => `provider:${provider.provider_id}`)
        : visibleOptions.map((option) => `model:${option.value}`);
      const selectedOption = visibleOptions.find((option) => (
        option.value === value || option.qualified_model_id === value
      ));
      return reconcileActiveOptionIndex({
        keys,
        activeKey: activeKeyRef.current,
        selectedKey: providerState.active || !selectedOption
          ? undefined
          : `model:${selectedOption.value}`,
        current,
      });
    });
  }, [activeCount, open, providerState.active, value, visibleOptions, visibleProviders]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    optionRefs.current[activeIndex]?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, open]);

  useEffect(() => {
    if (!open) return undefined;
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeAndRestoreFocus();
    };
    const handleFocus = (event: FocusEvent) => {
      if (rootRef.current?.contains(event.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("keydown", handleEscape);
    document.addEventListener("focusin", handleFocus);
    return () => {
      document.removeEventListener("keydown", handleEscape);
      document.removeEventListener("focusin", handleFocus);
    };
  }, [open]);

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
      setPopoverStyle({ left, top, width, maxHeight });
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
    <div ref={rootRef} className="relative min-w-0" data-model-search-picker={variant}>
      <button
        ref={buttonRef}
        id={triggerId}
        type="button"
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          event.preventDefault();
          updateActiveIndex(event.key === "ArrowUp" ? activeCount - 1 : 0);
          setOpen(true);
        }}
        aria-label={`Model: ${selectedDisplay?.label || value || "none selected"}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-describedby={selectedDisplay?.subtitle ? selectedDescriptionId : undefined}
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
          {selectedDisplay?.subtitle && (
            <span id={selectedDescriptionId} className={compact ? "sr-only" : "block truncate text-[11px] text-zinc-500"}>{selectedDisplay.subtitle}</span>
          )}
        </span>
        <ChevronDown size={compact ? 12 : 14} aria-hidden="true" className={cn("shrink-0 text-zinc-500 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <>
          <button type="button" tabIndex={-1} aria-label="Cancel model selection" className="fixed inset-0 rumi-layer-panel cursor-default" onClick={closeAndRestoreFocus} />
          <div
            className={cn(
              "fixed rumi-layer-local-popover flex flex-col overflow-hidden border border-zinc-700 bg-zinc-950 shadow-2xl",
              compact ? "rounded-md" : "rounded-lg",
            )}
            style={popoverStyle ?? undefined}
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              closeAndRestoreFocus();
            }}
          >
            {resolvedSchema.layout.show_search && (
              <div className={cn(
                "m-2 flex items-center gap-2 border border-zinc-800 bg-black/30 px-3 text-zinc-500 focus-within:border-zinc-600 focus-within:text-zinc-300",
                compact ? "h-8 rounded-md text-[11px]" : "h-9 rounded-lg text-xs",
              )}>
                <Search size={compact ? 13 : 14} aria-hidden="true" />
                <input
                  ref={inputRef}
                  id={inputId}
                  role="combobox"
                  aria-label="モデルを検索して選択"
                  aria-autocomplete="list"
                  aria-expanded="true"
                  aria-controls={listboxId}
                  aria-activedescendant={activeOptionId}
                  aria-describedby={`${statusId} ${instructionsId}`}
                  value={query}
                  onChange={(event) => {
                    onQueryChange(event.target.value);
                    setShowAll(false);
                    updateActiveIndex(0);
                  }}
                  onKeyDown={handleComboboxKeyDown}
                  placeholder={placeholder}
                  className="min-w-0 flex-1 bg-transparent text-zinc-200 outline-none placeholder:text-zinc-600"
                />
                {loading && <Loader2 size={13} className="animate-spin text-zinc-500" aria-hidden="true" />}
                {query && (
                  <button
                    type="button"
                    onClick={() => {
                      onQueryChange("");
                      setShowAll(false);
                      updateActiveIndex(0);
                      inputRef.current?.focus();
                    }}
                    className="rounded p-0.5 text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300"
                    aria-label="モデル検索をクリア"
                    aria-keyshortcuts="Alt+Backspace"
                  >
                    <X size={13} aria-hidden="true" />
                  </button>
                )}
                {onSearch && !providerState.active && (
                  <button
                    type="button"
                    onClick={search}
                    disabled={loading}
                    className="rounded p-0.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 disabled:opacity-40"
                    aria-label="リモートモデルを検索"
                    aria-keyshortcuts="Control+Enter Meta+Enter"
                  >
                    <Search size={13} aria-hidden="true" />
                  </button>
                )}
              </div>
            )}
            <div id={statusId} role="status" aria-live="polite" className="border-t border-zinc-800 px-3 py-1.5 text-[11px] text-zinc-500">
              {resultMessage}
            </div>
            <span id={instructionsId} className="sr-only">
              Use arrow, Home, End, and Page keys to move; moving beyond the displayed results reveals the remainder. Enter selects; Escape cancels; Tab closes or confirms a provider when configured. Alt+Backspace clears the query. Alt+Delete clears the selected model when available. Control+Enter or Command+Enter searches remotely when available.
            </span>
            {error && (
              <ErrorNotice
                className="rounded-none border-x-0 border-b-0 px-3 py-2 text-[11px]"
                copyLabel="モデル検索エラーをコピー"
                message={error}
              />
            )}
            {clearLabel && value && (
              <button
                type="button"
                onClick={() => pick("")}
                aria-keyshortcuts="Alt+Delete"
                className="flex w-full items-center justify-between border-t border-zinc-800 px-3 py-2 text-left text-[11px] text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100"
              >
                <span>{clearLabel}</span>
                <X size={12} />
              </button>
            )}
            <div
              ref={listboxRef}
              id={listboxId}
              role="listbox"
              tabIndex={resolvedSchema.layout.show_search ? -1 : 0}
              aria-label={providerState.active ? "Provider results" : "Model results"}
              aria-labelledby={!resolvedSchema.layout.show_search ? triggerId : undefined}
              aria-busy={loading}
              aria-activedescendant={!resolvedSchema.layout.show_search ? activeOptionId : undefined}
              onKeyDown={!resolvedSchema.layout.show_search ? handleComboboxKeyDown : undefined}
              className="min-h-0 overflow-y-auto border-t border-zinc-800 p-1 outline-none"
              style={popoverStyle?.maxHeight ? {
                maxHeight: Math.max(
                  120,
                  Number(popoverStyle.maxHeight)
                    - (resolvedSchema.layout.show_search ? 112 : 56)
                    - (showContinuation ? 44 : 0),
                ),
              } : undefined}
            >
              {providerState.active ? (
                visibleProviders.length > 0 ? visibleProviders.map((provider, index) => (
                  <div
                    ref={(element) => { optionRefs.current[index] = element; }}
                    id={`${listboxId}-option-${index}`}
                    key={provider.provider_id}
                    role="option"
                    aria-selected={false}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => updateActiveIndex(index)}
                    onClick={() => pickProvider(provider.provider_id)}
                    className={cn(
                      "flex w-full cursor-pointer items-center justify-between rounded-md text-left text-zinc-300 transition-colors hover:bg-zinc-900 hover:text-zinc-100",
                      compact ? "px-2 py-1.5 text-[11px]" : "px-2.5 py-2 text-sm",
                      index === activeIndex && "bg-zinc-800 text-zinc-100",
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
                        {provider.model_count} models
                      </span>
                    )}
                  </div>
                )) : (
                  <div className="px-3 py-5 text-xs text-zinc-600">一致するproviderがありません。</div>
                )
              ) : visibleOptions.length > 0 ? visibleOptions.map((option, index) => {
                const selectedOption = option.value === value || option.qualified_model_id === value;
                const activeOption = index === activeIndex;
                const display = modelSelectDisplay(option);
                const remote = remoteOptionValues.has(option.value)
                  || Boolean(option.qualified_model_id && remoteOptionValues.has(option.qualified_model_id));
                return (
                  <div
                    ref={(element) => { optionRefs.current[index] = element; }}
                    id={`${listboxId}-option-${index}`}
                    key={option.value}
                    role="option"
                    aria-selected={selectedOption}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => updateActiveIndex(index)}
                    onClick={() => pick(option.value)}
                    className={cn(
                      "flex w-full cursor-pointer items-start justify-between gap-3 rounded-md text-left transition-colors",
                      compact ? "px-2 py-1.5" : "px-2.5 py-2",
                      activeOption ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200",
                    )}
                  >
                    <span className="min-w-0">
                      <span className={cn("block truncate font-medium text-zinc-100", compact ? "text-[11px]" : "text-sm")}>{display.label}</span>
                      <span className="block truncate text-[11px] text-zinc-500">{display.subtitle}</span>
                      <span className="sr-only">
                        {modelSelectOptionAvailability(option)}
                        {remote ? " Remote search result." : " Local result."}
                      </span>
                    </span>
                    <span className="flex max-w-[160px] flex-wrap justify-end gap-1">
                      {display.badges.slice(0, compact ? 2 : 4).map((badge) => (
                        <span key={badge.id} className="rounded-full border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400">
                          {badge.label}
                        </span>
                      ))}
                      {remote && (
                        <span className="rounded-full border border-sky-700/70 px-1.5 py-0.5 text-[10px] text-sky-300">
                          remote
                        </span>
                      )}
                      {selectedOption && <Check size={13} className="mt-1 shrink-0 text-emerald-300" aria-label="Selected model" />}
                    </span>
                  </div>
                );
              }) : (
                <div className="px-3 py-5 text-xs text-zinc-600">{emptyText}</div>
              )}
            </div>
            {showContinuation && (
              <button
                type="button"
                onClick={() => {
                  setShowAll(true);
                  inputRef.current?.focus();
                }}
                className="m-1 rounded-md border border-zinc-800 px-3 py-2 text-xs text-zinc-300 hover:bg-zinc-900"
              >
                Show all {allVisibleOptions.length} results
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
