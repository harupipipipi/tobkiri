import { Check, ChevronUp, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

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
};

export function StructuredComposerPanel({
  composerInput,
  values: appliedValues = {},
  onApply,
}: StructuredComposerPanelProps) {
  const fields = useMemo(() => normalizeComposerFields(composerInput?.fields), [composerInput?.fields]);
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLElement | null>(null);
  const [values, setValues] = useState<StructuredComposerValues>(() => ({
    ...initialComposerFieldValues(fields),
    ...appliedValues,
  }));

  useEffect(() => {
    setValues({ ...initialComposerFieldValues(fields), ...appliedValues });
    setOpen(false);
  }, [composerInput?.id]);

  useEffect(() => {
    if (!open) return;
    const panel = panelRef.current;
    panel?.querySelector<HTMLElement>("select, input, textarea")?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
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
    <div className="relative px-3 pt-2" data-structured-composer={composerInput?.id}>
      <button
        type="button"
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((current) => !current)}
        className="group flex max-w-full items-center gap-2 rounded-lg border border-zinc-700 bg-[#303030] px-2.5 py-1.5 text-left text-[11px] text-zinc-400 transition-colors hover:border-zinc-500 hover:text-zinc-200"
      >
        <SlidersHorizontal size={13} className="shrink-0 text-zinc-400" />
        <span className="truncate font-medium text-zinc-300">{composerInput?.label || "入力オプション"}</span>
        <span className="text-[9px] text-zinc-500">{configuredCount}/{fields.length}</span>
        <ChevronUp size={12} className={`shrink-0 transition-transform ${open ? "" : "rotate-180"}`} />
      </button>

      {open && (
        <>
          <button type="button" tabIndex={-1} aria-label="テンプレートを閉じる" className="fixed inset-0 rumi-layer-local-popover cursor-default bg-transparent" onClick={() => setOpen(false)} />
          <section ref={panelRef} role="dialog" aria-label={composerInput?.label || "入力オプション"} className="absolute bottom-[calc(100%+8px)] left-2 rumi-layer-global-overlay w-[min(620px,calc(100vw-32px))] overflow-hidden rounded-xl border border-[#484848] bg-[#292929] shadow-2xl shadow-black/50 rumi-structured-panel-enter">
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
            <div className="grid max-h-[min(420px,58vh)] gap-x-4 gap-y-4 overflow-y-auto p-4 sm:grid-cols-2">
              {fields.map((field) => (
                <label key={field.id} className={`grid content-start gap-1.5 ${field.type === "textarea" ? "sm:col-span-2" : ""}`}>
                  <span className="flex items-center gap-1.5 text-[11px] font-semibold text-zinc-300">
                    {field.label || field.id}
                    {field.required && <span className="text-zinc-500">*</span>}
                  </span>
                  {field.type === "select" ? (
                    <span className="relative">
                      <select
                        value={values[field.id] || ""}
                        onChange={(event) => setValues((current) => ({ ...current, [field.id]: event.target.value }))}
                        className="h-9 w-full appearance-none rounded-lg border border-[#484848] bg-[#202020] px-3 pr-9 text-xs text-zinc-100 outline-none transition-colors hover:border-zinc-500 focus:border-zinc-400"
                      >
                        {!field.required && <option value="">指定なし</option>}
                        {field.options?.map((option) => <option key={option.value} value={option.value}>{option.label || option.value}</option>)}
                      </select>
                      <ChevronUp size={12} className="pointer-events-none absolute right-3 top-3.5 rotate-180 text-zinc-500" />
                    </span>
                  ) : field.type === "textarea" ? (
                    <textarea
                      rows={3}
                      value={values[field.id] || ""}
                      onChange={(event) => setValues((current) => ({ ...current, [field.id]: event.target.value }))}
                      placeholder={field.placeholder}
                      className="min-h-20 resize-y rounded-lg border border-[#484848] bg-[#202020] px-3 py-2.5 text-xs leading-5 text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-400"
                    />
                  ) : (
                    <input
                      value={values[field.id] || ""}
                      onChange={(event) => setValues((current) => ({ ...current, [field.id]: event.target.value }))}
                      placeholder={field.placeholder}
                      className="h-9 rounded-lg border border-[#484848] bg-[#202020] px-3 text-xs text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-400"
                    />
                  )}
                  {field.description && <span className="text-[10px] leading-4 text-zinc-600">{field.description}</span>}
                </label>
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
