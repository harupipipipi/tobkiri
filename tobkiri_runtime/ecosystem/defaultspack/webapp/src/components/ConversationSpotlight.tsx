import { useEffect, useId, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { CalendarDays, Clock, MessageSquare, Search, SlidersHorizontal, Star, X } from "lucide-react";

import type { ConversationSearchResult } from "../lib/api";
import { cn } from "../lib/cn";
import { SPOTLIGHT_FILTERS, type SpotlightFilter } from "../lib/conversationSpotlight";
import { formatRelativeTime } from "../lib/chat";
import { t, type LocaleSetting } from "../lib/i18n";
import { ModalFoundation } from "./ModalFoundation";

export function ConversationSpotlight({
  isOpen,
  query,
  filter,
  results,
  resultTotal,
  selectedIndex,
  loading,
  locale,
  shortcutLabel,
  onQueryChange,
  onFilterChange,
  onKeyDown,
  onClose,
  onOpenResult,
}: {
  isOpen: boolean;
  query: string;
  filter: SpotlightFilter;
  results: ConversationSearchResult[];
  resultTotal: number;
  selectedIndex: number;
  loading: boolean;
  locale: LocaleSetting;
  shortcutLabel?: string;
  onQueryChange: (value: string) => void;
  onFilterChange: (value: SpotlightFilter) => void;
  onKeyDown: (event: ReactKeyboardEvent<HTMLInputElement>) => void;
  onClose: () => void;
  onOpenResult: (result: ConversationSearchResult | undefined) => void;
}) {
  const generatedId = useId().replace(/:/g, "");
  const queryId = `conversation-spotlight-query-${generatedId}`;
  const listboxId = `conversation-spotlight-results-${generatedId}`;
  const queryRef = useRef<HTMLInputElement | null>(null);
  const selectedResult = results[selectedIndex];
  const optionId = (conversationId: string) => (
    `conversation-spotlight-option-${encodeURIComponent(conversationId)}`
  );
  const activeDescendant = selectedResult
    ? optionId(selectedResult.conversation_id)
    : undefined;
  const statusMessage = loading
    ? t(locale, "spotlight.loading")
    : results.length === 0
      ? query.trim()
        ? t(locale, "spotlight.emptyResults")
        : t(locale, "spotlight.emptyQuery")
      : t(locale, "spotlight.resultCount", { count: resultTotal });

  useEffect(() => {
    if (!isOpen || !activeDescendant) return;
    document.getElementById(activeDescendant)?.scrollIntoView({ block: "nearest" });
  }, [activeDescendant, isOpen]);

  if (!isOpen) return null;
  return (
    <ModalFoundation
      title={t(locale, "spotlight.title")}
      description={t(locale, "spotlight.description")}
      onClose={onClose}
      initialFocusRef={queryRef}
      backdropClassName="fixed inset-0 rumi-layer-modal flex items-start justify-center bg-black/45 px-4 pt-[9dvh] backdrop-blur-sm rumi-anim-fade-in motion-reduce:animate-none motion-reduce:backdrop-blur-none"
      panelClassName="w-full max-w-2xl overflow-hidden rounded-3xl border border-white/10 bg-zinc-950/95 shadow-[0_32px_120px_rgba(0,0,0,0.65)] ring-1 ring-white/5 rumi-anim-pop-in motion-reduce:animate-none outline-none"
    >
        <div className="flex items-center gap-3 border-b border-zinc-800/80 px-4 py-3">
          <Search size={20} className="text-emerald-300" />
          <input
            ref={queryRef}
            id={queryId}
            value={query}
            autoFocus
            role="combobox"
            aria-label={t(locale, "spotlight.queryLabel")}
            aria-autocomplete="list"
            aria-expanded="true"
            aria-controls={listboxId}
            aria-activedescendant={activeDescendant}
            onChange={(event) => onQueryChange(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={t(locale, "spotlight.placeholder")}
            className="min-w-0 flex-1 bg-transparent text-lg text-zinc-100 outline-none placeholder:text-zinc-600"
          />
          <div
            role="note"
            className="hidden rounded-full border border-zinc-800 px-2 py-1 text-[10px] uppercase tracking-[0.18em] text-zinc-500 sm:block"
            aria-label={t(locale, "spotlight.shortcutHint", {
              shortcut: shortcutLabel || t(locale, "spotlight.shortcut"),
            })}
          >
            {shortcutLabel || t(locale, "spotlight.shortcut")}
          </div>
          <button type="button" aria-label={t(locale, "spotlight.close")} onClick={onClose} className="flex min-h-10 min-w-10 items-center justify-center rounded-full p-1.5 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200">
            <X size={16} />
          </button>
        </div>

        <div
          role="group"
          aria-label={t(locale, "spotlight.filter")}
          className="flex flex-wrap items-center gap-2 border-b border-zinc-900 px-4 py-3"
        >
          <span aria-hidden="true" className="flex items-center gap-1 text-[11px] uppercase tracking-[0.18em] text-zinc-600">
            <SlidersHorizontal size={13} /> {t(locale, "spotlight.filter")}
          </span>
          {SPOTLIGHT_FILTERS.map((item) => (
            <button
              key={item.id}
              type="button"
              aria-pressed={filter === item.id}
              onClick={() => {
                onFilterChange(item.id);
                queryRef.current?.focus();
              }}
              className={cn(
                "rounded-full border px-3 py-1 text-xs transition-colors",
                filter === item.id
                  ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-200"
                  : "border-zinc-800 bg-zinc-900/50 text-zinc-500 hover:border-zinc-700 hover:text-zinc-300",
              )}
            >
              {t(locale, item.labelKey)}
            </button>
          ))}
          {loading && <span aria-hidden="true" className="ml-auto text-xs text-zinc-500">{t(locale, "spotlight.loading")}</span>}
        </div>

        <div
          id={listboxId}
          role="listbox"
          aria-label={t(locale, "spotlight.resultsLabel")}
          aria-busy={loading}
          className="max-h-[min(62vh,560px)] overflow-y-auto p-2"
        >
          {results.length === 0 ? (
            <div aria-hidden="true" className="px-6 py-10 text-center text-sm text-zinc-500">
              {query.trim() ? t(locale, "spotlight.emptyResults") : t(locale, "spotlight.emptyQuery")}
            </div>
          ) : (
            <div className="space-y-1">
              {results.map((result, index) => {
                const selected = index === selectedIndex;
                const firstMatch = result.matches?.[0];
                return (
                  <button
                    key={result.conversation_id}
                    id={optionId(result.conversation_id)}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    tabIndex={-1}
                    onClick={() => onOpenResult(result)}
                    className={cn(
                      "group w-full rounded-2xl px-4 py-3 text-left transition-colors",
                      selected ? "bg-zinc-800/85 text-zinc-100" : "text-zinc-300 hover:bg-zinc-900",
                    )}
                  >
                    <div className="flex items-center gap-3">
                      <div className={cn("flex h-10 w-10 items-center justify-center rounded-2xl border", selected ? "border-emerald-400/50 bg-emerald-400/10 text-emerald-200" : "border-zinc-800 bg-zinc-900 text-zinc-500")}>
                        {result.is_starred ? <Star size={17} /> : <MessageSquare size={17} />}
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="truncate text-sm font-medium">{result.title}</p>
                          {Number(result.exact_score ?? 0) > 0 && <span className="rounded-full bg-sky-500/15 px-2 py-0.5 text-[10px] text-sky-200">{t(locale, "spotlight.exact")}</span>}
                          {Number(result.semantic_score ?? 0) > Number(result.exact_score ?? 0) && <span className="rounded-full bg-violet-500/15 px-2 py-0.5 text-[10px] text-violet-200">{t(locale, "spotlight.vector")}</span>}
                        </div>
                        <p className="mt-1 truncate text-xs text-zinc-500">{firstMatch?.snippet ?? t(locale, "spotlight.recent")}</p>
                      </div>
                      <div className="hidden shrink-0 items-center gap-2 text-[11px] text-zinc-600 sm:flex">
                        <CalendarDays size={13} />
                        <span>{result.updated_at ? formatRelativeTime(result.updated_at) : t(locale, "spotlight.recentDate")}</span>
                      </div>
                    </div>
                    {result.matches && result.matches.length > 1 && (
                      <div className="ml-[52px] mt-2 flex items-center gap-1 text-[11px] text-zinc-600">
                        <Clock size={12} />
                        {t(locale, "spotlight.matches", { count: result.matches.length })}
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
        <span role="status" aria-live="polite" aria-atomic="true" className="sr-only">
          {statusMessage}
        </span>
    </ModalFoundation>
  );
}
