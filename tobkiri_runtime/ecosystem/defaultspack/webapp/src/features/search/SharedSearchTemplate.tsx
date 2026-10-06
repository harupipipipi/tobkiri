import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { Search } from "lucide-react";
import { cn } from "../../lib/cn";
import { InlineSearchInput } from "./InlineSearchInput";
import {
  applySearchSuggestion, parseSearchQuery, suggestSearchFilters,
  type SearchKind, type SearchPreset, type SearchSuggestion,
} from "./searchQuery";

export type SharedSearchItem<T> = {
  key: string; kind: SearchKind; title: string; description?: string;
  badge?: string | null; value: T;
};

/** Shared inline query editing and result navigation; callers own selection effects. */
export function SharedSearchTemplate<T>({
  query, onQueryChange, preset, items, onSelect, loading = false, error, onRetry,
  emptyMessage = "一致する結果はありません。", inputLabel = "検索", placeholder = "検索",
  inputRef: externalInputRef, autoFocus = false, onEscape, trailingControls, renderItem,
}: {
  query: string; onQueryChange: (query: string) => void; preset?: SearchPreset;
  items: SharedSearchItem<T>[]; onSelect: (item: SharedSearchItem<T>) => void;
  loading?: boolean; error?: string | null; onRetry?: () => void; emptyMessage?: string;
  inputLabel?: string; placeholder?: string; inputRef?: RefObject<HTMLInputElement | null>;
  autoFocus?: boolean; onEscape?: () => void; trailingControls?: ReactNode;
  renderItem?: (item: SharedSearchItem<T>) => ReactNode;
}) {
  const ownInputRef = useRef<HTMLInputElement>(null);
  const inputRef = externalInputRef ?? ownInputRef;
  const listRef = useRef<HTMLDivElement>(null);
  const [caret, setCaret] = useState(query.length);
  const [index, setIndex] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const parsed = parseSearchQuery(query, preset);
  const suggestions = dismissed ? [] : suggestSearchFilters(query, caret).filter((suggestion) =>
    !parseSearchQuery(applySearchSuggestion(query, suggestion), preset).conflict,
  );
  const count = suggestions.length || (loading || error || parsed.conflict ? 0 : items.length);
  const activeIndex = Math.min(index, Math.max(count - 1, 0));
  const id = useId();
  useEffect(() => { setIndex(0); setDismissed(false); }, [query]);
  useEffect(() => {
    listRef.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
  }, [activeIndex, query]);
  const chooseSuggestion = (suggestion: SearchSuggestion) => {
    const next = applySearchSuggestion(query, suggestion);
    const value = suggestion.end === query.length ? `${next} ` : next;
    onQueryChange(value);
    const nextCaret = suggestion.start + suggestion.token.length + (suggestion.end === query.length ? 1 : 0);
    setCaret(nextCaret);
    inputRef.current?.focus();
    requestAnimationFrame(() => inputRef.current?.setSelectionRange(nextCaret, nextCaret));
  };
  return <div className="min-w-0">
    <div className="flex items-center gap-3 px-5 py-4">
      <Search size={18} className="shrink-0 text-zinc-400" aria-hidden="true" />
      <InlineSearchInput
        ref={inputRef} role="combobox" aria-label={inputLabel} aria-autocomplete="list"
        aria-expanded="true" aria-controls={`${id}-list`}
        aria-activedescendant={count ? `${id}-option-${activeIndex}` : undefined}
        autoFocus={autoFocus} autoCorrect="off" autoCapitalize="none" spellCheck={false} value={query} placeholder={placeholder}
        onChange={(event) => { setCaret(event.target.selectionStart ?? event.target.value.length); onQueryChange(event.target.value); }}
        onSelect={(event) => setCaret(event.currentTarget.selectionStart ?? query.length)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            const step = event.key === "ArrowDown" ? 1 : -1;
            setIndex((activeIndex + step + Math.max(count, 1)) % Math.max(count, 1));
          } else if (event.key === "Enter" || (event.key === "Tab" && !event.shiftKey && suggestions.length)) {
            event.preventDefault();
            if (suggestions.length) chooseSuggestion(suggestions[activeIndex]);
            else if (!loading && !error && !parsed.conflict && items[activeIndex]) onSelect(items[activeIndex]);
          } else if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            if (suggestions.length) setDismissed(true);
            else onEscape?.();
          }
        }}
        className="min-w-0 flex-1 bg-transparent text-base text-zinc-100 outline-none placeholder:text-zinc-400"
      />
      {trailingControls}
    </div>
    <div ref={listRef} id={`${id}-list`} role="listbox" aria-label={suggestions.length ? "参照候補" : "検索結果"} className="max-h-[min(62dvh,520px)] overflow-y-auto px-2 pb-2">
      {suggestions.length ? suggestions.map((suggestion, optionIndex) => <button
        id={`${id}-option-${optionIndex}`} key={suggestion.token} type="button" role="option" tabIndex={-1}
        aria-selected={optionIndex === activeIndex} onClick={() => chooseSuggestion(suggestion)}
        className={cn("flex w-full items-center justify-between gap-3 rounded-xl px-4 py-3 text-left text-sm text-zinc-100", optionIndex === activeIndex && "bg-white/[0.08]")}
      ><span>{suggestion.label}</span><span className="shrink-0 text-blue-400">{suggestion.token}</span></button>) : <>
        {parsed.conflict ? <p role="status" className="px-4 py-8 text-sm text-zinc-400">固定の検索条件とフィルターが一致しません。</p>
          : error ? <div role="alert" className="px-4 py-5 text-sm text-zinc-400"><p>{error}</p>{onRetry && <button type="button" onClick={onRetry} className="mt-2 rounded border border-white/10 px-3 py-1">再試行</button>}</div>
          : loading ? <p role="status" className="px-4 py-8 text-sm text-zinc-400">検索中…</p>
          : items.length === 0 ? <p role="status" className="px-4 py-8 text-sm text-zinc-400">{emptyMessage}</p>
          : items.map((item, optionIndex) => <button
            id={`${id}-option-${optionIndex}`} key={item.key} type="button" role="option" tabIndex={-1}
            aria-selected={optionIndex === activeIndex} onClick={() => onSelect(item)}
            className={cn("flex min-h-11 w-full items-center gap-3 rounded-xl px-4 py-2 text-left", optionIndex === activeIndex ? "bg-white/[0.08] text-zinc-100" : "text-zinc-300 hover:bg-white/[0.04]")}
          >{renderItem ? renderItem(item) : <><span className="min-w-0 flex-1"><span className="block truncate text-sm">{item.title}</span>{item.description && <span className="mt-0.5 block truncate text-xs text-zinc-400">{item.description}</span>}</span>{item.badge && <span className="shrink-0 text-xs text-zinc-500">{item.badge}</span>}</>}</button>)}
      </>}
    </div>
  </div>;
}
