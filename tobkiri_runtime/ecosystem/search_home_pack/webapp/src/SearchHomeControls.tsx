import { type ChangeEvent, type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import type { SearchHomeModel } from "./api";

export type SearchAction = "smart" | "answer" | "google" | "open";

const ACTIONS: Array<{ id: SearchAction; title: string; subtitle: (query: string) => string }> = [
  {
    id: "smart",
    title: "Smart Resolve",
    subtitle: (query) => `質問ならAI回答、サイトなら候補を確認: "${query}"`,
  },
  {
    id: "answer",
    title: "AI Answer",
    subtitle: (query) => `defaultspack nodeで調べて答える: "${query}"`,
  },
  {
    id: "google",
    title: "Google Search",
    subtitle: (query) => `Google検索の移動先を確認: "${query}"`,
  },
  {
    id: "open",
    title: "Open Best URL",
    subtitle: (query) => `候補を解決して移動先を確認: "${query}"`,
  },
];

function modelLabel(model: SearchHomeModel): string {
  return model.label || model.display_name || model.profile_id || model.qualified_model_id || "Model";
}

function modelId(model: SearchHomeModel): string {
  return model.profile_id || model.qualified_model_id || "";
}

function modelProviderLabel(model: SearchHomeModel): string {
  return model.provider_display_name || model.provider_id || "model";
}

function hasModelMetadataFlag(model: SearchHomeModel, key: string): boolean {
  return Boolean(model.metadata && model.metadata[key]);
}

function modelStatusLabel(model: SearchHomeModel): string {
  const availability = model.availability ?? {};
  const status = typeof availability.status === "string" ? availability.status : "";
  if (model.configured || availability.configured || availability.active || availability.available) return "Ready";
  if (hasModelMetadataFlag(model, "settings_only")) return "Settings";
  if (model.requires_api_key) return "Needs key";
  return status ? status.replace(/_/g, " ") : "Catalog";
}

function modelBadges(model: SearchHomeModel): string[] {
  const badges: string[] = [];
  if (model.configured || model.availability?.configured || model.availability?.active || model.availability?.available) {
    badges.push("ready");
  } else if (hasModelMetadataFlag(model, "settings_only")) {
    badges.push("settings");
  }
  if (model.supports_image_input || model.supports_vision) badges.push("vision");
  if (model.supports_tool_calling) badges.push("tools");
  if (model.supports_thinking) badges.push("thinking");
  if (model.local) badges.push("local");
  if (model.requires_api_key && !model.configured) badges.push("key");
  return badges.slice(0, 4);
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
  onExecute: (action: SearchAction) => void;
}) {
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelFilter, setModelFilter] = useState("");
  const [isFocused, setIsFocused] = useState(false);
  const [attachedFile, setAttachedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const modelPickerRef = useRef<HTMLDivElement | null>(null);
  const modelFilterRef = useRef<HTMLInputElement | null>(null);

  const selectedModelItem = useMemo(
    () => models.find((model) => modelId(model) === selectedModel) ?? null,
    [models, selectedModel],
  );
  const selectedModelLabel = selectedModel
    ? selectedModelItem
      ? modelLabel(selectedModelItem)
      : selectedModel
    : "Default model";
  const selectedModelStatus = selectedModelItem ? modelStatusLabel(selectedModelItem) : "default routing";
  const filteredModels = useMemo(() => {
    const needle = modelFilter.trim().toLowerCase();
    return models.filter((model) => {
      const id = modelId(model);
      if (!id) return false;
      if (!needle) return true;
      return [
        id,
        modelLabel(model),
        modelProviderLabel(model),
        model.provider_id,
        model.provider_display_name,
        model.model_id,
        modelStatusLabel(model),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [modelFilter, models]);

  useEffect(() => {
    if (!modelPickerOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (modelPickerRef.current && !modelPickerRef.current.contains(event.target as Node)) setModelPickerOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setModelPickerOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    const focusTimer = window.setTimeout(() => modelFilterRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [modelPickerOpen]);

  const selectModel = (value: string) => {
    onSelectModel(value);
    setModelPickerOpen(false);
    setModelFilter("");
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    setAttachedFile(event.target.files?.[0] ?? null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const activeAction = ACTIONS[selectedActionIndex]?.id ?? "smart";
  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onExecute(activeAction);
  };

  return (
    <>
      <div className="hero-header">
        <div>
          <span className="product-mark">Tobkiri Search Home</span>
          <h1>何を探しましょう？</h1>
        </div>
        <div className="model-control" ref={modelPickerRef}>
          <button
            aria-expanded={modelPickerOpen}
            aria-haspopup="listbox"
            className="model-trigger"
            type="button"
            onClick={() => setModelPickerOpen((open) => !open)}
          >
            <span className="model-trigger-copy">
              <span>{selectedModelLabel}</span>
              <small>{selectedModelStatus}</small>
            </span>
            <span className="model-trigger-caret" aria-hidden="true">˅</span>
          </button>
          {modelPickerOpen ? (
            <div className="model-popover">
              <div className="model-popover-head">
                <strong>Model</strong>
                <span>{models.length} available</span>
              </div>
              <input
                aria-label="Filter models"
                autoComplete="off"
                className="model-filter"
                onChange={(event) => setModelFilter(event.target.value)}
                placeholder="Filter models..."
                ref={modelFilterRef}
                value={modelFilter}
              />
              <div aria-label="Models" className="model-list" role="listbox">
                <button
                  aria-selected={!selectedModel}
                  className={`model-option${!selectedModel ? " model-option-active" : ""}`}
                  onClick={() => selectModel("")}
                  role="option"
                  type="button"
                >
                  <span className="model-option-main">
                    <strong>Default model</strong>
                    <small>Use defaultspack preferred routing</small>
                  </span>
                  <span className="model-option-side">
                    <span>default</span>
                    <span className="model-badges"><span className="model-badge">auto</span></span>
                  </span>
                </button>
                {filteredModels.map((model) => {
                  const value = modelId(model);
                  if (!value) return null;
                  const active = value === selectedModel;
                  const badges = modelBadges(model);
                  if (badges.length === 0) badges.push(modelStatusLabel(model));
                  return (
                    <button
                      aria-selected={active}
                      className={`model-option${active ? " model-option-active" : ""}`}
                      key={value}
                      onClick={() => selectModel(value)}
                      role="option"
                      type="button"
                    >
                      <span className="model-option-main">
                        <strong>{modelLabel(model)}</strong>
                        <small>{value}</small>
                      </span>
                      <span className="model-option-side">
                        <span>{modelProviderLabel(model)}</span>
                        <span className="model-badges">
                          {badges.map((badge) => <span className="model-badge" key={badge}>{badge}</span>)}
                        </span>
                      </span>
                    </button>
                  );
                })}
                {filteredModels.length === 0 ? <div className="model-empty">No matching models</div> : null}
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <form className="hero-form" onSubmit={handleSubmit}>
        <div className={`search-box${isFocused ? " search-box-focused" : ""}`}>
          <div className="search-row">
            <input ref={fileInputRef} className="file-input" type="file" onChange={handleFileChange} />
            <button
              aria-label="Attach file"
              className="icon-button"
              title="ファイルを添付"
              type="button"
              onClick={() => fileInputRef.current?.click()}
            >+</button>
            <input
              aria-label="Search or enter URL"
              className="search-input"
              value={input}
              onBlur={() => setIsFocused(false)}
              onChange={(event) => onInputChange(event.target.value)}
              onFocus={() => setIsFocused(true)}
              onKeyDown={(event) => {
                if (!input.trim()) return;
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  onSelectedActionIndexChange((selectedActionIndex + 1) % ACTIONS.length);
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  onSelectedActionIndexChange((selectedActionIndex - 1 + ACTIONS.length) % ACTIONS.length);
                }
              }}
              placeholder="検索ワードを入力..."
              autoComplete="off"
              spellCheck={false}
              autoFocus
            />
            <button className="submit-button" type="submit" disabled={!input.trim() || loading || answerLoading}>
              <span>{loading || answerLoading ? "Working" : "検索"}</span>
              <span aria-hidden="true">→</span>
            </button>
          </div>

          {attachedFile ? (
            <div className="attachment-strip">
              <div className="attachment-chip">
                <span className="file-glyph" aria-hidden="true">□</span>
                <span className="file-meta">
                  <strong>{attachedFile.name}</strong>
                  <span>{(attachedFile.size / 1024 / 1024).toFixed(2)} MB</span>
                </span>
                <button aria-label="Remove file" className="remove-file" type="button" onClick={() => setAttachedFile(null)}>×</button>
              </div>
            </div>
          ) : null}

          {input.trim() ? (
            <div className="action-list" role="listbox" aria-label="Search actions">
              {ACTIONS.map((action, index) => (
                <button
                  aria-selected={selectedActionIndex === index}
                  className={`action-row${selectedActionIndex === index ? " action-row-active" : ""}`}
                  key={action.id}
                  role="option"
                  type="button"
                  onClick={() => {
                    onSelectedActionIndexChange(index);
                    onExecute(action.id);
                  }}
                  onMouseEnter={() => onSelectedActionIndexChange(index)}
                >
                  <span>
                    <strong>{action.title}</strong>
                    <small>{action.subtitle(input.trim())}</small>
                  </span>
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </form>
    </>
  );
}
