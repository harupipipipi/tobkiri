import { useMemo, useState } from "react";
import { Check, ChevronDown, Search, X } from "lucide-react";

import { cn } from "../../../lib/cn";
import {
  DEFAULT_MODEL_SELECTOR_SCHEMA,
  filterProvidersBySelector,
  modelSelectorSchemaForSurface,
  parseModelSelectorSchema,
  type ModelSelectorSchema,
} from "../../../features/models";
import {
  collectApiProviderOptions,
  customProviderRegistrationPayload,
  filterApiProviderOptions,
  type ApiProviderKind,
  type ApiProviderOption,
} from "../../../features/apiKeys/apiKeySetup";
import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { fieldOptionProviderRows, fieldProviderRows, SettingsFieldShell } from "./settingsFieldRendererUtils";

export function SearchableProviderField({
  value,
  options,
  onChange,
  onAddCustom,
  placeholder = "provider を検索",
  selectorSchema = DEFAULT_MODEL_SELECTOR_SCHEMA,
}: {
  value: string;
  options: ApiProviderOption[];
  onChange: (value: string) => void;
  onAddCustom?: (option: { providerId: string; label: string; kind: ApiProviderKind }) => void;
  placeholder?: string;
  selectorSchema?: ModelSelectorSchema;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [draftId, setDraftId] = useState("");
  const [draftKind, setDraftKind] = useState<ApiProviderKind>("custom");
  const selected = options.find((option) => option.provider_id === value) ?? null;
  const resolvedSelectorSchema = useMemo(
    () => modelSelectorSchemaForSurface(selectorSchema, "settings"),
    [selectorSchema],
  );
  const filtered = useMemo(
    () => filterApiProviderOptions(
      filterProvidersBySelector(options, resolvedSelectorSchema, "settings"),
      query,
    ).slice(0, resolvedSelectorSchema.layout.max_visible_options),
    [options, query, resolvedSelectorSchema],
  );

  const closeAll = () => {
    setOpen(false);
    setCreating(false);
    setDraftId("");
    setQuery("");
  };

  const submitDraft = () => {
    const payload = customProviderRegistrationPayload({ providerId: draftId, label: draftId, kind: draftKind });
    if (!payload) return;
    onAddCustom?.({ providerId: payload.provider_id, label: payload.label, kind: payload.kind });
    onChange(payload.provider_id);
    closeAll();
  };

  return (
    <div className="relative" data-settings-renderer="provider_select">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="flex min-h-10 w-full items-center justify-between gap-2 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-left text-sm text-zinc-200 outline-none transition-colors hover:border-zinc-700"
      >
        <span className="min-w-0 flex-1">
          <span className="block truncate">{selected?.label ?? (value || "provider を選択")}</span>
          {selected && selected.provider_id !== selected.label && (
            <span className="block truncate text-[11px] text-zinc-500">{selected.provider_id}</span>
          )}
        </span>
        <ChevronDown size={14} className={cn("shrink-0 text-zinc-500 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <>
          <button type="button" aria-label="close provider select" className="fixed inset-0 rumi-layer-panel cursor-default" onClick={closeAll} />
          <div className="absolute left-0 top-[calc(100%+6px)] rumi-layer-local-popover w-[min(520px,calc(100vw-32px))] max-w-[calc(100vw-32px)] overflow-hidden rounded-lg border border-zinc-700 bg-zinc-950 shadow-2xl">
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
                      {!option.builtin && (
                        <span className="mt-1 inline-flex rounded-full border border-zinc-700 px-1.5 text-[9px] uppercase text-zinc-400">
                          {option.kind === "custom" ? "non-llm" : "custom"}
                        </span>
                      )}
                    </span>
                    {active && <Check size={13} className="mt-1 shrink-0 text-emerald-300" />}
                  </button>
                );
              }) : (
                <div className="px-3 py-3 text-xs text-zinc-600">一致する provider がありません。</div>
              )}
            </div>
            {onAddCustom && (
              creating ? (
                <div className="space-y-2 border-t border-zinc-800 bg-zinc-950/80 p-3">
                  <input
                    autoFocus
                    value={draftId}
                    onChange={(event) => setDraftId(event.target.value)}
                    placeholder="provider id (例: tavily, searchapi)"
                    className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-2.5 py-1.5 text-sm text-zinc-200 outline-none"
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        submitDraft();
                      }
                    }}
                  />
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
                        {option === "llm" ? "LLM" : "Non-LLM"}
                      </button>
                    ))}
                  </div>
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
                  Add custom provider...
                </button>
              )
            )}
          </div>
        </>
      )}
    </div>
  );
}

export function BuiltinProviderSelectRenderer({ sectionId, field, value, sectionValues, onChange }: SettingsFieldRendererProps) {
  const selectorSchema = parseModelSelectorSchema(field.selector_schema);
  const providerOptions = filterProvidersBySelector(collectApiProviderOptions([
    ...fieldOptionProviderRows(field),
    ...fieldProviderRows(field, sectionValues),
  ]), selectorSchema, "settings");
  return (
    <SettingsFieldShell field={field}>
      <SearchableProviderField
        value={String(value ?? field.default ?? providerOptions[0]?.provider_id ?? "")}
        options={providerOptions}
        selectorSchema={selectorSchema}
        onChange={(nextValue) => onChange(sectionId, field.id, nextValue)}
      />
    </SettingsFieldShell>
  );
}
