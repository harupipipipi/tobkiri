import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import type { SearchHomeModel } from "./api";
import { SEARCH_ACTIONS, searchActionIndexForKey, type SearchAction } from "./searchRequest";

function modelLabel(model: SearchHomeModel): string {
  return model.label || model.display_name || model.profile_id || model.qualified_model_id || "モデル";
}

function modelId(model: SearchHomeModel): string {
  return model.profile_id || model.qualified_model_id || "";
}

function modelProviderLabel(model: SearchHomeModel): string {
  return model.provider_display_name || model.provider_id || "モデル";
}

function modelStatusLabel(model: SearchHomeModel): string {
  const availability = model.availability ?? {};
  if (model.configured || availability.configured || availability.active || availability.available) return "利用可能";
  if (model.metadata?.settings_only) return "設定済み・状態未確認";
  if (model.requires_api_key) return "APIキーが必要";
  if (availability.status === "download_required") return "ダウンロードが必要";
  return "未設定";
}

function modelBadges(model: SearchHomeModel): string[] {
  const badges: string[] = [];
  if (model.local) badges.push("ローカル");
  if (model.supports_image_input || model.supports_vision) badges.push("画像");
  if (model.supports_tool_calling) badges.push("ツール");
  if (model.supports_thinking) badges.push("推論");
  return badges;
}

export function SearchHomeControls({
  input,
  onInputChange,
  models,
  selectedModel,
  onSelectModel,
  selectedActionIndex,
  onSelectedActionIndexChange,
  loading,
  answerLoading,
  modelsLoading = false,
  modelSaving = false,
  onExecute,
}: {
  input: string;
  onInputChange: (value: string) => void;
  models: SearchHomeModel[];
  selectedModel: string;
  onSelectModel: (value: string) => void;
  selectedActionIndex: number;
  onSelectedActionIndexChange: (index: number) => void;
  loading: boolean;
  answerLoading: boolean;
  modelsLoading?: boolean;
  modelSaving?: boolean;
  onExecute: (action: SearchAction) => void;
}) {
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelFilter, setModelFilter] = useState("");
  const [isFocused, setIsFocused] = useState(false);
  const modelPickerRef = useRef<HTMLDivElement | null>(null);
  const modelTriggerRef = useRef<HTMLButtonElement | null>(null);
  const modelFilterRef = useRef<HTMLInputElement | null>(null);
  const modelListRef = useRef<HTMLDivElement | null>(null);

  const selectedModelItem = models.find((model) => modelId(model) === selectedModel);
  const selectedModelLabel = selectedModel
    ? selectedModelItem ? modelLabel(selectedModelItem) : selectedModel
    : "Defaultsの既定モデル";
  const selectedModelStatus = selectedModelItem ? modelStatusLabel(selectedModelItem) : "Tobkiri Defaultsと共有";
  const filteredModels = useMemo(() => {
    const needle = modelFilter.trim().toLowerCase();
    return models.filter((model) => modelId(model) && (!needle || [
      modelId(model), modelLabel(model), modelProviderLabel(model), model.model_id, modelStatusLabel(model),
    ].filter(Boolean).join(" ").toLowerCase().includes(needle)));
  }, [modelFilter, models]);

  useEffect(() => {
    if (!modelPickerOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (modelPickerRef.current && !modelPickerRef.current.contains(event.target as Node)) setModelPickerOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setModelPickerOpen(false);
        modelTriggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    modelFilterRef.current?.focus();
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [modelPickerOpen]);

  const selectModel = (value: string) => {
    onSelectModel(value);
    setModelPickerOpen(false);
    setModelFilter("");
    modelTriggerRef.current?.focus();
  };
  const activeAction = SEARCH_ACTIONS[selectedActionIndex]?.id ?? "google";
  const busy = loading || answerLoading;
  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onExecute(activeAction);
  };

  return (
    <>
      <div className="hero-header">
        <div>
          <span className="product-mark">Tobkiri Search</span>
          <h1>何を探しましょう？</h1>
          <p className="hero-description">キーワード、質問、URLをひとつの検索窓に。</p>
          <p className="hero-caption">AIモデルはTobkiri Defaultsと共有。Web検索はGoogleで。</p>
        </div>
        <div className="model-control" ref={modelPickerRef}>
          <button
            aria-expanded={modelPickerOpen}
            aria-haspopup="listbox"
            aria-controls={modelPickerOpen ? "search-model-list" : undefined}
            aria-label={`モデルを選択: ${selectedModelLabel}`}
            className="model-trigger"
            disabled={modelSaving}
            ref={modelTriggerRef}
            type="button"
            onClick={() => setModelPickerOpen((open) => !open)}
          >
            <span className="model-trigger-copy">
              <span>{selectedModelLabel}</span>
              <small>{modelSaving ? "共有設定に保存中…" : modelsLoading ? "モデルを確認中…" : selectedModelStatus}</small>
            </span>
            <span className="model-trigger-caret" aria-hidden="true">˅</span>
          </button>
          {modelPickerOpen ? (
            <div className="model-popover">
              <div className="model-popover-head">
                <strong>モデル</strong>
                <span>{models.length} 件のカタログ</span>
              </div>
              <p className="model-provenance">プロバイダーとモデル設定はTobkiri Defaultsと共有しています。</p>
              <input
                aria-label="モデルを絞り込む"
                autoComplete="off"
                className="model-filter"
                onChange={(event) => setModelFilter(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "ArrowDown" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    modelListRef.current?.querySelector<HTMLButtonElement>("[role=option]")?.focus();
                  }
                }}
                placeholder="モデル名・プロバイダーで絞り込み"
                ref={modelFilterRef}
                value={modelFilter}
              />
              <div
                aria-label="モデル"
                className="model-list"
                id="search-model-list"
                ref={modelListRef}
                role="listbox"
                onKeyDown={(event) => {
                  const options = Array.from(modelListRef.current?.querySelectorAll<HTMLButtonElement>("[role=option]") ?? []);
                  const index = options.indexOf(event.target as HTMLButtonElement);
                  if (index < 0 || event.nativeEvent.isComposing) return;
                  const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1
                    : event.key === "ArrowDown" ? (index + 1) % options.length
                    : event.key === "ArrowUp" ? (index - 1 + options.length) % options.length : -1;
                  if (nextIndex >= 0) {
                    event.preventDefault();
                    options[nextIndex]?.focus();
                  }
                }}
              >
                <button
                  aria-selected={!selectedModel}
                  className={`model-option${!selectedModel ? " model-option-active" : ""}`}
                  onClick={() => selectModel("")}
                  role="option"
                  type="button"
                >
                  <span className="model-option-main"><strong>Defaultsの既定モデル</strong><small>共有設定のモデル選択に従います</small></span>
                  <span className="model-option-side"><span>自動</span></span>
                </button>
                {filteredModels.map((model) => {
                  const value = modelId(model);
                  return (
                    <button
                      aria-selected={value === selectedModel}
                      className={`model-option${value === selectedModel ? " model-option-active" : ""}`}
                      key={value}
                      onClick={() => selectModel(value)}
                      role="option"
                      type="button"
                    >
                      <span className="model-option-main"><strong>{modelLabel(model)}</strong><small>{value}</small></span>
                      <span className="model-option-side">
                        <span>{modelProviderLabel(model)} · {modelStatusLabel(model)}</span>
                        <span className="model-badges">{modelBadges(model).map((badge) => <span className="model-badge" key={badge}>{badge}</span>)}</span>
                      </span>
                    </button>
                  );
                })}
                {filteredModels.length === 0 ? <div className="model-empty">該当するモデルはありません</div> : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <form className="hero-form" onSubmit={handleSubmit} aria-busy={busy}>
        <div className={`search-box${isFocused ? " search-box-focused" : ""}`}>
          <div className="search-row">
            <span className="search-glyph" aria-hidden="true">⌕</span>
            <input
              aria-label="検索キーワード、質問、URL"
              aria-describedby="search-keyboard-hint"
              aria-expanded={Boolean(input.trim())}
              aria-controls={input.trim() ? "search-action-list" : undefined}
              aria-activedescendant={input.trim() ? `search-action-${activeAction}` : undefined}
              aria-autocomplete="list"
              className="search-input"
              value={input}
              onBlur={() => setIsFocused(false)}
              onChange={(event) => onInputChange(event.target.value)}
              onFocus={() => setIsFocused(true)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && event.nativeEvent.isComposing) event.preventDefault();
                if (!input.trim()) return;
                const nextIndex = searchActionIndexForKey(event.key, selectedActionIndex, event.nativeEvent.isComposing);
                if (nextIndex !== null) {
                  event.preventDefault();
                  onSelectedActionIndexChange(nextIndex);
                }
              }}
              placeholder="検索する、AIに質問する、URLを開く…"
              role="combobox"
              autoComplete="off"
              spellCheck={false}
              autoFocus
            />
            <button aria-label={busy ? "検索中" : "検索"} className="submit-button" type="submit" disabled={!input.trim() || busy}>
              <span>{busy ? "検索中…" : "検索"}</span><span aria-hidden="true">→</span>
            </button>
          </div>
          {input.trim() ? (
            <div className="action-list" id="search-action-list" role="listbox" aria-label="検索方法">
              {SEARCH_ACTIONS.map((action, index) => (
                <button
                  aria-selected={selectedActionIndex === index}
                  className={`action-row${selectedActionIndex === index ? " action-row-active" : ""}`}
                  disabled={busy}
                  id={`search-action-${action.id}`}
                  key={action.id}
                  role="option"
                  type="button"
                  onClick={() => {
                    onSelectedActionIndexChange(index);
                    onExecute(action.id);
                  }}
                  onKeyDown={(event) => {
                    const nextIndex = searchActionIndexForKey(event.key, index, event.nativeEvent.isComposing);
                    if (nextIndex !== null) {
                      event.preventDefault();
                      onSelectedActionIndexChange(nextIndex);
                      document.getElementById(`search-action-${SEARCH_ACTIONS[nextIndex].id}`)?.focus();
                    }
                  }}
                  onMouseEnter={() => onSelectedActionIndexChange(index)}
                ><span><strong>{action.title}</strong><small>{action.subtitle}</small></span></button>
              ))}
            </div>
          ) : null}
        </div>
        <p className="search-hint" id="search-keyboard-hint">Enterで検索 · ↑↓で検索方法を選択</p>
      </form>
    </>
  );
}
