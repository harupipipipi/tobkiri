import { Check, ChevronUp, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import type { TemplateComposerInput } from "../lib/api";
import {
  initialComposerFieldValues,
  normalizeComposerFields,
  structuredComposerPayload,
  type StructuredComposerValues,
} from "../lib/structuredComposer";

type StructuredComposerPanelProps = {
  composerInput: TemplateComposerInput | null;
  values?: StructuredComposerValues;
  onApply: (values: StructuredComposerValues) => void;
  compact?: boolean;
};

type StructuredSelectProps = {
  label: string;
  value: string;
  options: Array<{ value: string; label?: string }>;
  required: boolean;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChange: (value: string) => void;
};

function StructuredSelect({
  label,
  value,
  options,
  required,
  open,
  onOpenChange,
  onChange,
}: StructuredSelectProps) {
  const allOptions = required ? options : [{ value: "", label: "指定なし" }, ...options];
  const selected = allOptions.find((option) => option.value === value) ?? allOptions[0];

  return (
    <div className="relative">
      <button
        type="button"
        aria-label={label}
        aria-haspopup="listbox"
        aria-expanded={open}
        data-structured-select
        onClick={() => onOpenChange(!open)}
        className={`group flex h-9 w-full items-center gap-2.5 rounded-lg border px-3 text-left text-xs outline-none transition-all ${
          open
            ? "border-sky-400/60 bg-sky-400/[0.08] shadow-[0_0_0_3px_rgba(56,189,248,0.08)]"
            : "border-[#484848] bg-gradient-to-b from-[#262626] to-[#202020] hover:border-zinc-500 hover:from-[#2a2a2a]"
        }`}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${open ? "bg-sky-300" : "bg-zinc-500"}`} />
        <span className="min-w-0 flex-1 truncate font-medium text-zinc-100">{selected?.label || selected?.value || "選択"}</span>
        <span className={`grid h-5 w-5 shrink-0 place-items-center rounded-md border transition-colors ${open ? "border-sky-400/30 bg-sky-400/10 text-sky-200" : "border-white/[0.07] bg-black/15 text-zinc-500 group-hover:text-zinc-300"}`}>
          <ChevronUp size={12} className={`transition-transform ${open ? "" : "rotate-180"}`} />
        </span>
      </button>

      {open && (
        <div role="listbox" aria-label={`${label}の選択肢`} className="absolute inset-x-0 top-[calc(100%+6px)] rumi-layer-local-popover overflow-hidden rounded-xl border border-white/[0.1] bg-[#202124]/[0.98] p-1 shadow-[0_18px_45px_rgba(0,0,0,0.45)] backdrop-blur-xl">
          {allOptions.map((option) => {
            const optionLabel = option.label || option.value;
            const selectedOption = option.value === value;
            return (
              <button
                key={option.value || "__empty__"}
                type="button"
                role="option"
                aria-selected={selectedOption}
                onClick={() => {
                  onChange(option.value);
                  onOpenChange(false);
                }}
                className={`flex min-h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-xs transition-colors ${
                  selectedOption ? "bg-sky-400/[0.11] text-sky-100" : "text-zinc-300 hover:bg-white/[0.06] hover:text-white"
                }`}
              >
                <span className="min-w-0 flex-1 truncate">{optionLabel}</span>
                {selectedOption && <Check size={13} className="shrink-0 text-sky-300" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function StructuredComposerPanel({
  composerInput,
  values: appliedValues = {},
  onApply,
  compact = false,
}: StructuredComposerPanelProps) {
  const fields = useMemo(() => normalizeComposerFields(composerInput?.fields), [composerInput?.fields]);
  const panelId = useId();
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLElement | null>(null);
  const [openSelectId, setOpenSelectId] = useState<string | null>(null);
  const [values, setValues] = useState<StructuredComposerValues>(() => ({
    ...initialComposerFieldValues(fields),
    ...appliedValues,
  }));

  useEffect(() => {
    setValues({ ...initialComposerFieldValues(fields), ...appliedValues });
    setOpen(false);
    setOpenSelectId(null);
  }, [composerInput?.id]);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    panel?.querySelector<HTMLElement>("button[data-structured-select], input, textarea")?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [open]);

  if (fields.length === 0) return null;

  const configuredCount = fields.filter((field) => Boolean(values[field.id]?.trim())).length;
  const apply = () => {
    onApply(structuredComposerPayload(fields, values));
    setOpen(false);
  };

  return (
    <div className={compact ? "relative" : "relative px-3 pt-2"} data-structured-composer={composerInput?.id}>
      <button
        type="button"
        aria-label={`${composerInput?.label || "入力オプション"} ${configuredCount}/${fields.length}`}
        aria-controls={panelId}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((current) => !current)}
        title={`${composerInput?.label || "入力オプション"} (${configuredCount}/${fields.length})`}
        className={compact
          ? "group relative flex h-9 min-w-9 items-center justify-center gap-1 rounded-xl border border-white/[0.08] bg-white/[0.045] px-2 text-zinc-400 transition-colors hover:border-white/[0.15] hover:bg-white/[0.07] hover:text-zinc-100"
          : "group flex max-w-full items-center gap-2 rounded-lg border border-zinc-700 bg-[#303030] px-2.5 py-1.5 text-left text-[11px] text-zinc-400 transition-colors hover:border-zinc-500 hover:text-zinc-200"}
      >
        <SlidersHorizontal size={13} className="shrink-0 text-zinc-400" />
        {!compact && <span className="truncate font-medium text-zinc-300">{composerInput?.label || "入力オプション"}</span>}
        <span className={compact ? "text-[9px] tabular-nums text-zinc-500" : "text-[9px] text-zinc-500"}>{configuredCount}/{fields.length}</span>
        {!compact && <ChevronUp size={12} className={`shrink-0 transition-transform ${open ? "" : "rotate-180"}`} />}
      </button>

      {open && (
        <>
          <button type="button" tabIndex={-1} aria-label="テンプレートを閉じる" className="fixed inset-0 rumi-layer-local-popover cursor-default bg-transparent" onClick={() => setOpen(false)} />
          <section id={panelId} ref={panelRef} role="dialog" aria-label={composerInput?.label || "入力オプション"} className="absolute bottom-[calc(100%+8px)] left-0 rumi-layer-global-overlay w-[min(560px,calc(100vw-24px))] overflow-visible rumi-popover rumi-structured-panel-enter">
            <header className="flex items-start justify-between gap-4 border-b border-[#3d3d3d] px-4 py-3">
              <div>
                <div className="flex items-center gap-2 text-sm font-semibold text-zinc-100">
                  <SlidersHorizontal size={15} className="text-zinc-400" />
                  {composerInput?.label || "入力オプション"}
                </div>
                <p className="mt-1 text-[11px] leading-5 text-zinc-500">{composerInput?.description || "選択内容を構造化JSONとしてAIへの入力に追加します。"}</p>
              </div>
              <button type="button" onClick={() => setOpen(false)} className="rounded-lg p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200" aria-label="閉じる"><X size={15} /></button>
            </header>
            <div className={`grid max-h-[min(420px,58vh)] gap-x-4 gap-y-4 p-4 sm:grid-cols-2 ${openSelectId ? "overflow-visible" : "overflow-y-auto"}`}>
              {fields.map((field) => (
                <div key={field.id} className={`grid content-start gap-1.5 ${field.type === "textarea" ? "sm:col-span-2" : ""}`}>
                  <span className="flex items-center gap-1.5 text-[11px] font-semibold text-zinc-300">
                    {field.label || field.id}
                    {field.required && <span className="text-zinc-500">*</span>}
                  </span>
                  {field.type === "select" ? (
                    <StructuredSelect
                      label={field.label || field.id}
                      value={values[field.id] || ""}
                      options={field.options ?? []}
                      required={Boolean(field.required)}
                      open={openSelectId === field.id}
                      onOpenChange={(nextOpen) => setOpenSelectId(nextOpen ? field.id : null)}
                      onChange={(nextValue) => setValues((current) => ({ ...current, [field.id]: nextValue }))}
                    />
                  ) : field.type === "textarea" ? (
                    <textarea
                      aria-label={field.label || field.id}
                      rows={3}
                      value={values[field.id] || ""}
                      onChange={(event) => setValues((current) => ({ ...current, [field.id]: event.target.value }))}
                      placeholder={field.placeholder}
                      className="min-h-20 resize-y rounded-lg border border-[#484848] bg-[#202020] px-3 py-2.5 text-xs leading-5 text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-400"
                    />
                  ) : (
                    <input
                      aria-label={field.label || field.id}
                      value={values[field.id] || ""}
                      onChange={(event) => setValues((current) => ({ ...current, [field.id]: event.target.value }))}
                      placeholder={field.placeholder}
                      className="h-9 rounded-lg border border-[#484848] bg-[#202020] px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-400"
                    />
                  )}
                  {field.description && <span className="text-[10px] leading-4 text-zinc-600">{field.description}</span>}
                </div>
              ))}
            </div>
            <footer className="flex items-center justify-end gap-3 border-t border-[#3d3d3d] px-4 py-3">
              <button type="button" onClick={apply} className="flex h-8 items-center gap-2 rounded-lg bg-zinc-100 px-3.5 text-xs font-semibold text-zinc-950 hover:bg-white">
                <Check size={14} /> 入力に反映
              </button>
            </footer>
          </section>
        </>
      )}
    </div>
  );
}
