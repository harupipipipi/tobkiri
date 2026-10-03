import { useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Search, Star, X } from "lucide-react";

import type { ConversationSearchResult } from "../lib/api";
import { cn } from "../lib/cn";
import { SPOTLIGHT_FILTERS, type SpotlightFilter } from "../lib/conversationSpotlight";
import { formatRelativeTime } from "../lib/chat";
import { t, type LocaleSetting } from "../lib/i18n";
import { ModalFoundation } from "./ModalFoundation";

export function ConversationSpotlight({
  isOpen, query, filter, results, selectedIndex, loading, locale, shortcutLabel,
  onQueryChange, onFilterChange, onKeyDown, onClose, onOpenResult,
}: {
  isOpen: boolean;
  query: string;
  filter: SpotlightFilter;
  results: ConversationSearchResult[];
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
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    listRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex, isOpen]);
  if (!isOpen) return null;
  return (
    <ModalFoundation
      title={t(locale, "spotlight.placeholder")}
      onClose={onClose}
      backdropClassName="fixed inset-0 rumi-layer-modal flex items-start justify-center bg-black/40 px-4 pt-[10dvh]"
      panelClassName="rumi-spotlight-panel w-full max-w-2xl overflow-hidden rounded-2xl border border-white/10 bg-neutral-800 shadow-2xl outline-none"
    >
      <div className="flex items-center gap-3 px-5 py-4">
        <Search size={18} className="shrink-0 text-zinc-400" aria-hidden="true" />
        <input
          role="combobox"
          aria-label={t(locale, "spotlight.placeholder")}
          aria-expanded="true"
          aria-controls="conversation-search-results"
          aria-activedescendant={results[selectedIndex] ? `conversation-search-result-${selectedIndex}` : undefined}
          value={query}
          autoFocus
          onChange={(event) => onQueryChange(event.target.value)}
          onKeyDown={onKeyDown}
          placeholder={t(locale, "spotlight.placeholder")}
          className="min-w-0 flex-1 bg-transparent text-base text-zinc-100 outline-none placeholder:text-zinc-400"
        />
        <button type="button" aria-label={t(locale, "spotlight.close")} onClick={onClose} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-zinc-400 hover:bg-white/5 hover:text-zinc-100">
          <X size={16} />
        </button>
      </div>
      <div className="flex items-center justify-between gap-3 px-5 pb-2 text-xs text-zinc-400">
        <span aria-live="polite">{loading ? t(locale, "spotlight.loading") : query.trim() ? `${results.length}件のチャット` : "最近のチャット"}</span>
        <div className="flex min-w-0 items-center gap-3">
          <select aria-label={t(locale, "spotlight.filter")} value={filter} onChange={(event) => onFilterChange(event.target.value as SpotlightFilter)} className="max-w-28 bg-transparent text-xs text-zinc-400 outline-none">
            {SPOTLIGHT_FILTERS.map((item) => <option key={item.id} value={item.id}>{t(locale, item.labelKey)}</option>)}
          </select>
          <kbd className="hidden text-[11px] text-zinc-500 sm:block">{shortcutLabel}</kbd>
        </div>
      </div>
      <div ref={listRef} id="conversation-search-results" role="listbox" aria-label="チャットの検索結果" className="max-h-[min(62dvh,520px)] overflow-y-auto px-2 pb-2">
        {results.length === 0 ? (
          <p className="px-4 py-8 text-sm text-zinc-400">{query.trim() ? t(locale, "spotlight.emptyResults") : t(locale, "spotlight.emptyQuery")}</p>
        ) : results.map((result, index) => (
          <button
            id={`conversation-search-result-${index}`}
            key={result.conversation_id}
            type="button"
            role="option"
            aria-selected={index === selectedIndex}
            tabIndex={-1}
            onClick={() => onOpenResult(result)}
            className={cn("flex min-h-11 w-full items-center gap-3 rounded-xl px-4 py-2 text-left", index === selectedIndex ? "bg-white/[0.08] text-zinc-100" : "text-zinc-300 hover:bg-white/[0.04]")}
          >
            {result.is_starred ? <Star size={13} className="shrink-0 text-zinc-400" /> : <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-zinc-400" aria-hidden="true" />}
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm">{result.title}</span>
              {query.trim() && result.matches?.[0]?.snippet && <span className="mt-0.5 block truncate text-xs text-zinc-400">{result.matches[0].snippet}</span>}
            </span>
            {result.updated_at && <span className="shrink-0 text-xs text-zinc-500">{formatRelativeTime(result.updated_at)}</span>}
          </button>
        ))}
      </div>
    </ModalFoundation>
  );
}
