import { ModelPinButton } from "../features/models/ModelPinButton";
import { useModelPins } from "../features/models/useModelPins";
import { orderPinnedModels } from "../features/models/modelPins";
import { modelPinIdentityForSearchItem } from "../features/models/modelPinIdentity";
import { useRef } from "react";
import type { ModelProfile, ModelSearchItem } from "../lib/api";
import { Star, X } from "lucide-react";
import { SPOTLIGHT_FILTERS, type SpotlightFilter } from "../lib/conversationSpotlight";
import type { SpotlightResult } from "../lib/spotlightNavigation";
import { formatRelativeTime } from "../lib/chat";
import { t, type LocaleSetting } from "../lib/i18n";
import { SharedSearchTemplate } from "../features/search/SharedSearchTemplate";
import { SEARCH_KIND_LABELS } from "../features/search/searchQuery";
import { parseConfirmedSearchQuery, type SearchQueryState } from "../features/search/searchQueryState";
import { ModalFoundation } from "./ModalFoundation";

/** Global wrapper around the shared search input/filter/result template. */
export function ConversationSpotlight({
  isOpen, queryState, filter, results, modelProfiles = [], modelProfilesReady = true, loading, error, onRetry, modelsComplete, locale, shortcutLabel,
  onQueryStateChange, onFilterChange, onClose, onOpenResult,
}: {
  isOpen: boolean; queryState: SearchQueryState; filter: SpotlightFilter; results: SpotlightResult[];
  modelProfiles?: readonly ModelProfile[]; modelProfilesReady?: boolean;
  loading: boolean; error?: string | null; onRetry?: () => void; modelsComplete?: boolean;
  locale: LocaleSetting; shortcutLabel?: string;
  onQueryStateChange: (state: SearchQueryState) => void; onFilterChange: (value: SpotlightFilter) => void;
  onClose: () => void;
  onOpenResult: (result: SpotlightResult | undefined) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const modelPins = useModelPins();
  const pinIdentity = (model: ModelSearchItem) =>
    modelPinIdentityForSearchItem(model, modelProfiles, modelProfilesReady);
  const modelResults = orderPinnedModels(results.filter((result) => result.kind === "model"),
    modelPins.pins, (result) => result.kind === "model" ? pinIdentity(result.model) : null);
  let modelIndex = 0;
  const orderedResults = results.map((result) => result.kind === "model" ? modelResults[modelIndex++] : result);
  if (!isOpen) return null;
  const parsed = parseConfirmedSearchQuery(queryState);
  const chatFiltersApply = parsed.kinds.includes("chat") && parsed.providerIds.length === 0;
  return <ModalFoundation
    title={t(locale, "spotlight.placeholder")} onClose={onClose} initialFocusRef={inputRef} deferEscapeToContent
    backdropClassName="fixed inset-0 rumi-layer-modal flex items-start justify-center bg-black/40 px-4 pt-[10dvh]"
    panelClassName="rumi-spotlight-panel w-full max-w-2xl overflow-hidden rounded-2xl border border-white/10 bg-neutral-800 shadow-2xl outline-none"
  >
    <SharedSearchTemplate
      queryState={queryState} onQueryStateChange={onQueryStateChange} inputRef={inputRef}
      inputLabel={t(locale, "spotlight.placeholder")} placeholder="検索"
      items={orderedResults.map((result) => ({ key: `${result.kind}:${result.id}`, kind: result.kind, title: result.title, value: result }))}
      onSelect={(item) => onOpenResult(item.value)} onEscape={onClose} loading={loading} error={error} onRetry={onRetry}
      emptyMessage={parsed.kinds.includes("model") ? "一致するモデルはありません。" : parsed.kinds.some((kind) => kind === "widget" || kind === "tool") ? "一致する登録済みウィジェット／ツールはありません。" : t(locale, parsed.text ? "spotlight.emptyResults" : "spotlight.emptyQuery")}
      trailingControls={<button type="button" aria-label={t(locale, "spotlight.close")} onClick={onClose} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-zinc-400 hover:bg-white/5 hover:text-zinc-100"><X size={16} /></button>}
      renderItemAction={({ value: result }) => result.kind === "model" ? <ModelPinButton
        label={`${result.title} · ${result.model.connection_id ?? result.model.provider_id ?? ""}/${result.model.model_id ?? ""}`} pinned={modelPins.isPinned(pinIdentity(result.model))}
        disabled={!modelPins.enabled || !pinIdentity(result.model)}
        onToggle={() => modelPins.toggle(pinIdentity(result.model))} /> : null}
      renderItem={({ value: result }) => <>
        <span className="shrink-0 rounded bg-white/5 px-1.5 py-0.5 text-[10px] text-zinc-500">{SEARCH_KIND_LABELS[result.kind]}</span>
        {result.kind === "chat" && result.conversation.is_starred && <Star size={13} className="shrink-0 text-zinc-400" />}
        <span className="min-w-0 flex-1"><span className="block truncate text-sm">{result.title}</span>
          <span className="block truncate text-xs text-zinc-400">{result.kind === "model" ? `${result.model.provider_display_name || result.model.provider_id} · ${result.model.connection_id ? `${result.model.connection_id} · ` : ""}${result.model.model_id} · ${result.model.provenance === "provider_public_catalog" ? "公開カタログ · " : ""}到達性は未検証` : result.kind === "chat" ? parsed.text && result.conversation.matches?.[0]?.snippet : result.description}</span>
        </span>
        {result.kind === "chat" ? result.conversation.updated_at && <span className="shrink-0 text-xs text-zinc-500">{formatRelativeTime(result.conversation.updated_at)}</span> : result.kind !== "model" && result.badge && <span className="shrink-0 text-xs text-zinc-500">{result.badge}</span>}
      </>}
    />
    {parsed.kinds.includes("model") && !loading && !error && !modelsComplete && results.some((result) => result.kind === "model") && <p className="px-5 pb-2 text-xs text-zinc-400">カタログの一部を表示しています。検索文字でさらに絞り込めます。</p>}
    <div className="flex items-center justify-between gap-3 border-t border-white/5 px-5 py-3 text-xs text-zinc-400">
      <span aria-live="polite">{parsed.kinds.some((kind) => kind !== "chat") ? "期間・スターはチャット検索に適用" : parsed.text ? `${results.length}件` : "最近のチャット"}</span>
      <div className="flex items-center gap-3"><select disabled={!chatFiltersApply} aria-label={t(locale, "spotlight.filter")} value={filter} onChange={(event) => onFilterChange(event.target.value as SpotlightFilter)} className="max-w-28 bg-transparent outline-none">
        {SPOTLIGHT_FILTERS.map((item) => <option key={item.id} value={item.id}>{t(locale, item.labelKey)}</option>)}
      </select><kbd className="hidden text-[11px] text-zinc-500 sm:block">{shortcutLabel}</kbd></div>
    </div>
  </ModalFoundation>;
}
