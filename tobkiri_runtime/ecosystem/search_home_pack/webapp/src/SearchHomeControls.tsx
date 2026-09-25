import { type ChangeEvent, type FormEvent, useEffect, useMemo, useRef, useState } from "react";

import type { SearchHomeModel } from "./api";
import {
  SEARCH_HOME_ACTIONS,
  searchHomeCopy,
  searchHomeModelId,
  searchHomeModelLabel,
  searchHomeModelStatus,
  searchHomeProviderLabel,
  type SearchAction,
} from "./searchHomeLocale";

export type { SearchAction } from "./searchHomeLocale";

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
    () => models.find((model) => searchHomeModelId(model) === selectedModel) ?? null,
    [models, selectedModel],
  );
  const selectedModelLabel = selectedModel
    ? selectedModelItem
      ? searchHomeModelLabel(selectedModelItem)
      : searchHomeCopy.model.selectedFallback
    : searchHomeCopy.model.defaultLabel;
  const selectedModelStatus = selectedModelItem
    ? searchHomeModelStatus(selectedModelItem)
    : searchHomeCopy.model.defaultStatus;
  const filteredModels = useMemo(() => {
    const needle = modelFilter.trim().toLowerCase();
    return models.filter((model) => {
      const id = searchHomeModelId(model);
      if (!id) return false;
      if (!needle) return true;
      return [
        id,
        searchHomeModelLabel(model),
        searchHomeProviderLabel(model),
        model.provider_id,
        model.provider_display_name,
        model.model_id,
        searchHomeModelStatus(model),
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

  const activeAction = SEARCH_HOME_ACTIONS[selectedActionIndex]?.id ?? "smart";
  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    onExecute(activeAction);
  };

  return (
    <>
      <div className="hero-header">
        <div>
          <span className="product-mark">{searchHomeCopy.productName}</span>
          <h1>{searchHomeCopy.heading}</h1>
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
            <div className={`model-popover${selectedModelItem ? " model-popover-with-details" : ""}`}>
              <div className="model-popover-head">
                <strong>{searchHomeCopy.model.pickerTitle}</strong>
                <span>{searchHomeCopy.model.availableCount(models.length)}</span>
              </div>
              <input
                aria-label={searchHomeCopy.model.filterLabel}
                autoComplete="off"
                className="model-filter"
                onChange={(event) => setModelFilter(event.target.value)}
                placeholder={searchHomeCopy.model.filterPlaceholder}
                ref={modelFilterRef}
                value={modelFilter}
              />
              {selectedModelItem ? (
                <details className="model-technical-details">
                  <summary>{searchHomeCopy.model.technicalDetails}</summary>
                  <dl>
                    <div><dt>{searchHomeCopy.model.profileId}</dt><dd>{selectedModelItem.profile_id}</dd></div>
                    {selectedModelItem.qualified_model_id ? <div><dt>{searchHomeCopy.model.qualifiedModelId}</dt><dd>{selectedModelItem.qualified_model_id}</dd></div> : null}
                    {selectedModelItem.model_id ? <div><dt>{searchHomeCopy.model.modelId}</dt><dd>{selectedModelItem.model_id}</dd></div> : null}
                  </dl>
                </details>
              ) : null}
              <div aria-label={searchHomeCopy.model.listLabel} className="model-list" role="listbox">
                <button
                  aria-selected={!selectedModel}
                  className={`model-option${!selectedModel ? " model-option-active" : ""}`}
                  onClick={() => selectModel("")}
                  role="option"
                  type="button"
                >
                  <span className="model-option-main">
                    <strong>{searchHomeCopy.model.defaultLabel}</strong>
                    <small>{searchHomeCopy.model.defaultDescription}</small>
                  </span>
                  <span className="model-option-side">
                    <span>{searchHomeCopy.model.automaticProvider}</span>
                    <span className="model-badges"><span className="model-badge">{searchHomeCopy.model.defaultStatus}</span></span>
                  </span>
                </button>
                {filteredModels.map((model) => {
                  const value = searchHomeModelId(model);
                  if (!value) return null;
                  const active = value === selectedModel;
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
                        <strong>{searchHomeModelLabel(model)}</strong>
                        <small>{searchHomeProviderLabel(model)}</small>
                      </span>
                      <span className="model-option-side">
                        <span className="model-badges"><span className="model-badge">{searchHomeModelStatus(model)}</span></span>
                      </span>
                    </button>
                  );
                })}
                {filteredModels.length === 0 ? <div className="model-empty">{searchHomeCopy.model.noMatches}</div> : null}
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
              aria-label={searchHomeCopy.search.attachLabel}
              className="icon-button"
              title={searchHomeCopy.search.attachLabel}
              type="button"
              onClick={() => fileInputRef.current?.click()}
            >+</button>
            <input
              aria-label={searchHomeCopy.search.inputLabel}
              className="search-input"
              value={input}
              onBlur={() => setIsFocused(false)}
              onChange={(event) => onInputChange(event.target.value)}
              onFocus={() => setIsFocused(true)}
              onKeyDown={(event) => {
                if (!input.trim()) return;
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  onSelectedActionIndexChange((selectedActionIndex + 1) % SEARCH_HOME_ACTIONS.length);
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  onSelectedActionIndexChange((selectedActionIndex - 1 + SEARCH_HOME_ACTIONS.length) % SEARCH_HOME_ACTIONS.length);
                }
              }}
              placeholder={searchHomeCopy.search.placeholder}
              autoComplete="off"
              spellCheck={false}
              autoFocus
            />
            <button className="submit-button" type="submit" disabled={!input.trim() || loading || answerLoading}>
              <span>{loading || answerLoading ? searchHomeCopy.search.working : searchHomeCopy.search.submit}</span>
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
                <button aria-label={searchHomeCopy.search.removeFile} className="remove-file" type="button" onClick={() => setAttachedFile(null)}>×</button>
              </div>
            </div>
          ) : null}

          {input.trim() ? (
            <div className="action-list" role="listbox" aria-label={searchHomeCopy.search.actionsLabel}>
              {SEARCH_HOME_ACTIONS.map((action, index) => (
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
