import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { ModelPinButton } from "../features/models/ModelPinButton";
import { useModelPins } from "../features/models/useModelPins";
import { orderPinnedModels, type ModelPinState } from "../features/models/modelPins";
import { modelPinIdentityForProfile } from "../features/models/modelPinIdentity";
import { Check, Search } from "lucide-react";
import type { ModelProfile } from "../lib/api";
import { ViewportPopover } from "../ui/layers/ViewportPopover";
import { modelProfileConnectionId, savedModelProfileForOption } from "../features/models/modelSelectionIdentity";
import { DEFAULT_MODEL_SELECTOR_SCHEMA, filterModelProfilesBySelector, modelSelectorSchemaForSurface, type ModelSelectorSchema } from "../features/models/modelSelectorSchema";

/** Compare saved routes including the execution connection and model binding. */
export function registeredDropdownSameRoute(left: ModelProfile, right: ModelProfile): boolean {
  return left.profile_id === right.profile_id && left.provider_id === right.provider_id
    && left.model_id === right.model_id && modelProfileConnectionId(left) === modelProfileConnectionId(right);
}

/** Registered routes retain their exact connection identity when selected. */
export function registeredDropdownSelection(profiles: ModelProfile[], profile: ModelProfile, generating = false): ModelProfile | null {
  if (generating || profile.availability?.available === false || profile.availability?.status === "unavailable") return null;
  const saved = savedModelProfileForOption(profiles.filter((saved) => registeredDropdownSameRoute(saved, profile)), {
    value: profile.profile_id, registered_profile_id: profile.profile_id,
    label: profile.display_name ?? profile.model_id ?? profile.profile_id,
    provider_id: profile.provider_id, model_id: profile.model_id,
    connection_id: modelProfileConnectionId(profile),
  });
  return saved?.availability?.available === false || saved?.availability?.status === "unavailable" ? null : saved;
}

/** Filter only registered profiles; provider prefixes scope the remaining text. */
export function registeredDropdownProfiles(profiles: ModelProfile[], schema: ModelSelectorSchema, query: string, selected: ModelProfile | null, pins: ModelPinState = []): ModelProfile[] {
  const resolved = modelSelectorSchemaForSurface(schema, "composer");
  const tokens = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const trigger = resolved.layout.provider_trigger;
  const eligible = profiles.filter((profile) => filterModelProfilesBySelector([{
    ...profile, availability: { ...profile.availability,
      configured: profile.route_configured || profile.local || profile.availability?.configured || profile.availability?.active || ["configured", "active"].includes(String(profile.availability?.status ?? "")),
    },
  }], resolved, "composer").length > 0);
  const result = eligible.filter((profile) => tokens.every((token) => {
    if (token.startsWith(trigger)) return `${profile.provider_id ?? ""} ${profile.provider_display_name ?? ""}`.toLocaleLowerCase().includes(token.slice(trigger.length));
    return [profile.display_name, profile.model_id, profile.provider_id, profile.provider_display_name, modelProfileConnectionId(profile), ...(profile.capability_tags ?? [])].join(" ").toLocaleLowerCase().includes(token);
  }));
  if (!query.trim() && selected && resolved.layout.selected_position === "first") {
    const index = result.findIndex((profile) => registeredDropdownSameRoute(profile, selected));
    if (index > 0) result.unshift(...result.splice(index, 1));
  }
  return orderPinnedModels(result, pins, modelPinIdentityForProfile);
}

/** Ignore model navigation and confirmation while an IME owns the keyboard. */
export function registeredDropdownKeyboardBlocked(composing: boolean, nativeComposing: boolean, keyCode: number): boolean {
  return composing || nativeComposing || keyCode === 229;
}

/** Composer's compact dropdown searches saved routes without a catalogue request. */
export function ComposerRegisteredModelDropdown({ profiles, selectedProfile, isGenerating, placement = "above", onSelect, onClose, selectorSchema = DEFAULT_MODEL_SELECTOR_SCHEMA, onOpenModelManager, anchorRef }: {
  profiles: ModelProfile[]; selectedProfile: ModelProfile | null; isGenerating: boolean;
  placement?: "above" | "below"; onSelect: (profile: ModelProfile) => void; onClose: () => void;
  selectorSchema?: ModelSelectorSchema; onOpenModelManager?: () => void;
  anchorRef?: RefObject<HTMLElement | null>;
}) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [activeRouteKey, setActiveRouteKey] = useState<string | null>(null);
  const modelPins = useModelPins();
  const routeKey = (profile: ModelProfile) => JSON.stringify([profile.profile_id, profile.provider_id, modelProfileConnectionId(profile), profile.model_id]);
  const schema = modelSelectorSchemaForSurface(selectorSchema, "composer");
  const pageSize = Math.max(1, schema.layout.max_visible_options);
  const [limit, setLimit] = useState(pageSize);
  const composing = useRef(false);
  const fallbackAnchor = useRef<HTMLSpanElement | null>(null);
  const activeOption = useRef<HTMLButtonElement | null>(null);
  const id = useId();
  const matching = registeredDropdownProfiles(profiles, selectorSchema, query, selectedProfile, modelPins.pins);
  const visible = matching.slice(0, limit);
  const retainedIndex = activeRouteKey === null ? -1 : visible.findIndex((profile) => routeKey(profile) === activeRouteKey);
  const resolvedIndex = retainedIndex >= 0 ? retainedIndex : Math.min(activeIndex, Math.max(0, visible.length - 1));
  const active = visible[resolvedIndex];
  useEffect(() => { setLimit(pageSize); setActiveIndex(0); setActiveRouteKey(null); }, [query, pageSize]);
  useEffect(() => { activeOption.current?.scrollIntoView?.({ block: "nearest" }); }, [activeIndex]);
  const select = (profile: ModelProfile) => {
    if (composing.current) return;
    const saved = registeredDropdownSelection(profiles, profile, isGenerating);
    if (!saved) return;
    onSelect(saved); onClose();
  };
  const groupKey = (profile: ModelProfile) => modelPins.isPinned(modelPinIdentityForProfile(profile))
    ? "pinned" : JSON.stringify([schema.layout.group_by === "none" ? "" : profile.provider_id ?? ""]);
  const togglePin = (profile: ModelProfile) => {
    const identity = modelPinIdentityForProfile(profile);
    if (!modelPins.enabled || !identity) return;
    if (active) setActiveRouteKey(routeKey(active));
    const nextPins = modelPins.isPinned(identity)
      ? modelPins.pins.filter((pin) => pin !== identity) : [...modelPins.pins, identity];
    const nextMatching = registeredDropdownProfiles(profiles, selectorSchema, query, selectedProfile, nextPins);
    const retainedRank = nextMatching.findIndex((item) => registeredDropdownSameRoute(item, profile));
    const activeRank = active ? nextMatching.findIndex((item) => registeredDropdownSameRoute(item, active)) : -1;
    setLimit((previous) => Math.max(previous, retainedRank + 1, activeRank + 1));
    modelPins.toggle(identity);
  };
  return <>
    {!anchorRef && <span ref={fallbackAnchor} aria-hidden="true" />}
    <ViewportPopover anchorRef={anchorRef ?? fallbackAnchor} onClose={onClose} label="モデル選択を閉じる" closeOnEscape={false} preferredPlacement={placement} desiredWidth={schema.layout.popover_width_px} maxHeight={schema.layout.popover_max_height_px} className="rumi-popover">
      <div className="border-b border-white/[0.06] p-2.5">
        <div className="mb-2 flex justify-between gap-2 text-[10px] text-zinc-500"><span className="truncate">現在: {selectedProfile?.display_name ?? selectedProfile?.model_id ?? "未選択"}</span><span>↑↓ で移動</span></div>
        {schema.layout.show_search && <label className="flex h-9 items-center gap-2 rounded-lg border border-white/[0.08] bg-black/25 px-2.5"><Search size={14} className="text-zinc-500" /><input autoFocus value={query} onChange={(event) => { setQuery(event.target.value); setActiveRouteKey(null); }} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onKeyDown={(event) => {
          if (registeredDropdownKeyboardBlocked(composing.current, event.nativeEvent.isComposing, event.keyCode)) return;
          if (event.key === "Escape") { event.preventDefault(); onClose(); }
          else if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); const next = Math.max(0, Math.min(visible.length - 1, resolvedIndex + (event.key === "ArrowDown" ? 1 : -1))); setActiveIndex(next); setActiveRouteKey(visible[next] ? routeKey(visible[next]) : null); }
          else if (schema.layout.model_confirm_keys.includes(event.key) && active) { event.preventDefault(); select(active); }
        }} role="combobox" aria-label="モデルを検索" aria-expanded="true" aria-controls={`${id}-options`} aria-activedescendant={active ? `${id}-${encodeURIComponent(routeKey(active))}` : undefined} placeholder={`モデルを検索... ${schema.layout.provider_trigger} でプロバイダー`} className="min-w-0 flex-1 bg-transparent text-sm text-zinc-200 outline-none" /></label>}
      </div>
      <div onKeyDown={(event) => {
        if (event.key === "Escape" && !event.defaultPrevented
          && !registeredDropdownKeyboardBlocked(composing.current, event.nativeEvent.isComposing, event.nativeEvent.keyCode)) {
          event.preventDefault(); event.stopPropagation(); onClose();
        }
      }} id={`${id}-options`} role="listbox" aria-label="登録済みモデル" className="max-h-64 overflow-y-auto py-1">
        {visible.map((profile, index) => {
          const group = groupKey(profile);
          const showHeading = index === 0 || groupKey(visible[index - 1]) !== group;
          const unavailable = !registeredDropdownSelection(profiles, profile);
          const current = selectedProfile !== null && registeredDropdownSameRoute(selectedProfile, profile);
          return <div key={routeKey(profile)} role="presentation">
            {showHeading && (group === "pinned" || JSON.parse(group)[0]) && <div className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-zinc-600">
              {group === "pinned" ? "ピン留め" : profile.provider_display_name ?? profile.provider_id}
              {schema.layout.show_provider_count ? ` · ${visible.filter((item) => groupKey(item) === group).length}` : ""}
            </div>}
            <div className="flex items-center" role="presentation"><button id={`${id}-${encodeURIComponent(routeKey(profile))}`} ref={active === profile ? activeOption : undefined} type="button" role="option" aria-selected={current} disabled={isGenerating || unavailable} onMouseMove={() => { setActiveIndex(index); setActiveRouteKey(routeKey(profile)); }} onClick={() => select(profile)} className={`flex min-w-0 flex-1 items-center justify-between gap-2 border-l-2 px-3 py-1.5 text-left transition-colors hover:bg-white/[0.06] disabled:opacity-50 ${active === profile ? "border-sky-400 bg-sky-400/[0.09]" : current ? "border-zinc-600 bg-zinc-800/60" : "border-transparent"}`}>
              <span className="min-w-0"><span className="block truncate text-[13px] text-zinc-200">{profile.display_name ?? profile.model_id}</span><span className="block truncate text-[10px] text-zinc-500">{profile.provider_display_name ?? profile.provider_id} · {modelProfileConnectionId(profile)}/{profile.model_id}</span>{schema.layout.show_capability_tags && <span className="flex flex-wrap gap-1">{(profile.capability_tags ?? []).slice(0, 4).map((tag) => <span key={tag} className="rounded border border-zinc-700 px-1 text-[9px] text-zinc-400">{tag}</span>)}</span>}</span>
              {unavailable ? <span className="text-[10px] text-zinc-500">利用不可</span> : current ? <Check size={12} className="shrink-0 text-sky-300" /> : <span className="text-[10px] text-zinc-500">{profile.max_context_tokens ?? profile.max_context ?? ""}</span>}
            </button><ModelPinButton label={`${profile.display_name ?? profile.model_id ?? profile.profile_id} · ${modelProfileConnectionId(profile)}/${profile.model_id ?? ""}`}
              pinned={modelPins.isPinned(modelPinIdentityForProfile(profile))}
              disabled={!modelPins.enabled || !modelPinIdentityForProfile(profile)} onToggle={() => togglePin(profile)} />
            </div>
          </div>;
        })}
        {!visible.length && <div className="px-3 py-4 text-center text-xs text-zinc-500">登録済みモデルが見つかりません</div>}
      </div>
      {matching.length > limit && <button type="button" onClick={() => setLimit((previous) => previous + pageSize)} className="w-full border-t border-white/[0.06] px-3 py-2 text-xs text-sky-300">さらに表示</button>}
      {onOpenModelManager && <button type="button" onClick={() => { onClose(); onOpenModelManager(); }} className="w-full border-t border-white/[0.06] px-3 py-2 text-xs text-zinc-400">モデル設定を開く</button>}
    </ViewportPopover>
  </>;
}
