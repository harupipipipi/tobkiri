import { type ChangeEvent, type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  answerInput,
  loadModelSettings,
  loadModels,
  loadRouteState,
  MODEL_SETTINGS_KEY,
  persistRouteStateRemotely,
  routeInput,
  setPreferredModel,
  type SearchHomeModel,
} from "./api";
import {
  buildBrowserCompanionRouteMessage,
  normalizeSelectedIndex,
  persistRouteSessionState,
  reviewRouteDestination,
  ROUTE_SESSION_STORAGE_KEY,
  selectedCandidateUrl,
  type RouteCandidate,
  type RouteDecision,
  type RouteSessionState,
} from "./routerTypes";
import { NavigationReview } from "./NavigationReview";
import { conversationHref, normalizeAnswerResponse, type AnswerResult } from "./answerState";
import { evaluateExplicitDestinationInput } from "./destinationPolicy";
import {
  SEARCH_HOME_ACTIONS,
  searchHomeCopy,
  searchHomeModelId,
  searchHomeModelLabel,
  searchHomeModelLabelForReference,
  searchHomeModelStatus,
  searchHomeProviderLabel,
  type SearchAction,
} from "./searchHomeLocale";

const ROUTE_DECISION_STORAGE_KEY = "rumi-search-home-route-decision";
const ANSWER_ROUTE_TYPES = new Set(["ASK_AI", "ASK_AI_WITH_SEARCH"]);

type HydratedRouteState = RouteDecision | null;
function isObjectLike(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function googleSearchUrl(query: string): string {
  return `https://www.google.com/search?q=${encodeURIComponent(query).replace(/%20/g, "+")}`;
}

function coerceCandidate(value: unknown): RouteCandidate | null {
  if (!isObjectLike(value)) {
    return null;
  }
  const url = typeof value.url === "string" ? value.url : "";
  const finalUrl = typeof value.final_url === "string" ? value.final_url : url;
  if (!url && !finalUrl) {
    return null;
  }
  return {
    url: url || finalUrl,
    final_url: finalUrl || url,
    title: typeof value.title === "string" ? value.title : "",
    snippet: typeof value.snippet === "string" ? value.snippet : "",
    domain: typeof value.domain === "string" ? value.domain : "",
    source: typeof value.source === "string" ? value.source : "session",
    status: typeof value.status === "number" ? value.status : null,
    canonical_url: typeof value.canonical_url === "string" ? value.canonical_url : "",
    content_type: typeof value.content_type === "string" ? value.content_type : "",
    redirected: Boolean(value.redirected),
    looks_like_login: Boolean(value.looks_like_login),
    looks_like_paywall: Boolean(value.looks_like_paywall),
    looks_like_404: Boolean(value.looks_like_404),
    looks_like_ad_heavy: Boolean(value.looks_like_ad_heavy),
    is_search_results: Boolean(value.is_search_results),
    heuristic_score: typeof value.heuristic_score === "number" ? value.heuristic_score : null,
    screenshot_path: typeof value.screenshot_path === "string" ? value.screenshot_path : "",
  };
}

function coerceRouteDecision(value: unknown): RouteDecision | null {
  if (!isObjectLike(value)) {
    return null;
  }
  const query = typeof value.query === "string" ? value.query : "";
  const targetUrl = typeof value.target_url === "string" ? value.target_url : "";
  const fallbackUrl = typeof value.fallback_url === "string" ? value.fallback_url : "";
  if (!query && !targetUrl && !fallbackUrl) {
    return null;
  }
  const targetCandidates = Array.isArray(value.target_candidates)
    ? value.target_candidates
        .map((candidate) => coerceCandidate(candidate))
        .filter((candidate): candidate is RouteCandidate => candidate !== null)
    : [];
  return {
    route_type: typeof value.route_type === "string" ? value.route_type : "GOOGLE_REDIRECT",
    query,
    target_url: targetUrl || fallbackUrl,
    target_candidates: targetCandidates,
    selected_index: typeof value.selected_index === "number" ? value.selected_index : 0,
    fallback_url: fallbackUrl || targetUrl,
    resolution_reason: typeof value.resolution_reason === "string" ? value.resolution_reason : "restored_state",
    used_ai_judge: Boolean(value.used_ai_judge),
    used_visual_judge: Boolean(value.used_visual_judge),
    metadata: isObjectLike(value.metadata) ? value.metadata : {},
  };
}

function decisionFromSessionState(value: unknown): RouteDecision | null {
  if (!isObjectLike(value)) {
    return null;
  }
  const query = typeof value.query === "string" ? value.query : "";
  const targetUrl = typeof value.target_url === "string" ? value.target_url : "";
  const fallbackUrl = typeof value.fallback_url === "string" ? value.fallback_url : "";
  const selectedIndex = typeof value.selected_index === "number" ? value.selected_index : 0;
  const candidates = Array.isArray(value.target_candidates)
    ? value.target_candidates
        .map((candidate) => coerceCandidate(candidate))
        .filter((candidate): candidate is RouteCandidate => candidate !== null)
    : [];
  if (!query && !targetUrl && !fallbackUrl && candidates.length === 0) {
    return null;
  }
  return {
    route_type: "GOOGLE_REDIRECT",
    query,
    target_url: targetUrl || fallbackUrl,
    target_candidates: candidates,
    selected_index: selectedIndex,
    fallback_url: fallbackUrl || targetUrl,
    resolution_reason: "restored_session_state",
    used_ai_judge: false,
    used_visual_judge: false,
    metadata: {},
  };
}

function loadDecisionFromSessionStorage(storage: Storage | null): HydratedRouteState {
  if (!storage) {
    return null;
  }
  storage.removeItem(ROUTE_DECISION_STORAGE_KEY);
  try {
    const session = storage.getItem(ROUTE_SESSION_STORAGE_KEY);
    if (session) {
      const parsed = JSON.parse(session) as Partial<RouteSessionState>;
      const issuedAt = Date.parse(String(parsed.issued_at || ""));
      const expiresAt = Date.parse(String(parsed.expires_at || ""));
      const now = Date.now();
      if (!/^[A-Za-z0-9_-]{16,128}$/.test(String(parsed.state_id || "")) ||
          !Number.isFinite(issuedAt) || !Number.isFinite(expiresAt) ||
          issuedAt > now + 30_000 || expiresAt <= now ||
          expiresAt - issuedAt > 6 * 60 * 60 * 1000) {
        storage.removeItem(ROUTE_SESSION_STORAGE_KEY);
        return null;
      }
      return decisionFromSessionState(parsed);
    }
  } catch {
    // Ignore malformed session payloads.
  }
  return null;
}

function saveDecisionToSessionStorage(storage: Storage | null, decision: RouteDecision, selectedIndex: number): RouteSessionState | null {
  if (!storage) {
    return null;
  }
  const session = persistRouteSessionState(storage, decision, selectedIndex);
  storage.removeItem(ROUTE_DECISION_STORAGE_KEY);
  return session;
}

function isAnswerRoute(decision: RouteDecision): boolean {
  return ANSWER_ROUTE_TYPES.has(decision.route_type || "");
}

function syntheticAnswerDecision(query: string): RouteDecision {
  return {
    route_type: "ASK_AI_WITH_SEARCH",
    query,
    target_url: "",
    target_candidates: [],
    selected_index: -1,
    fallback_url: googleSearchUrl(query),
    resolution_reason: "manual:defaultspack_chat_node",
    used_ai_judge: true,
    used_visual_judge: false,
    metadata: {
      answer_required: true,
      defaultspack_node: "blocks.chat.send",
      selected_tools: ["web_search"],
    },
  };
}

export default function App() {
  const [input, setInput] = useState("");
  const [decision, setDecision] = useState<RouteDecision | null>(null);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [selectedActionIndex, setSelectedActionIndex] = useState(0);
  const [models, setModels] = useState<SearchHomeModel[]>([]);
  const [selectedModel, setSelectedModel] = useState("");
  const [modelPickerOpen, setModelPickerOpen] = useState(false);
  const [modelFilter, setModelFilter] = useState("");
  const [loading, setLoading] = useState(false);
  const [answerLoading, setAnswerLoading] = useState(false);
  const [answerResult, setAnswerResult] = useState<(AnswerResult & { query: string; requestedModel: string }) | null>(null);
  const [answerTransportError, setAnswerTransportError] = useState("");
  const [isFocused, setIsFocused] = useState(false);
  const [attachedFile, setAttachedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const modelPickerRef = useRef<HTMLDivElement | null>(null);
  const modelFilterRef = useRef<HTMLInputElement | null>(null);
  const committedNavigationRef = useRef(false);
  const answerRequestRef = useRef(0);

  const currentDecision = useMemo(() => {
    if (!decision) {
      return null;
    }
    return {
      ...decision,
      selected_index: normalizeSelectedIndex(decision, selectedIndex),
    };
  }, [decision, selectedIndex]);

  const activeAction = SEARCH_HOME_ACTIONS[selectedActionIndex]?.id ?? "smart";
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
      if (!id) {
        return false;
      }
      if (!needle) {
        return true;
      }
      const text = [
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
        .toLowerCase();
      return text.includes(needle);
    });
  }, [modelFilter, models]);

  const persistRouteState = useCallback((nextDecision: RouteDecision, nextIndex: number) => {
    const storage = typeof window !== "undefined" ? window.sessionStorage : null;
    const session = saveDecisionToSessionStorage(storage, nextDecision, nextIndex);
    if (session) {
      persistRouteStateRemotely(session);
      window.postMessage(
        buildBrowserCompanionRouteMessage(nextDecision, nextIndex),
        window.location.origin,
      );
    }
  }, []);

  const navigate = useCallback(
    (nextDecision: RouteDecision, nextIndex: number, rawDestination: string) => {
      const destination = reviewRouteDestination(rawDestination);
      if (!destination.ok || committedNavigationRef.current) {
        return;
      }
      committedNavigationRef.current = true;
      setSelectedIndex(nextIndex);
      persistRouteState(nextDecision, nextIndex);
      window.location.assign(destination.url);
    },
    [persistRouteState],
  );

  useEffect(() => {
    committedNavigationRef.current = false;
    const restored = loadDecisionFromSessionStorage(typeof window !== "undefined" ? window.sessionStorage : null);
    if (restored) {
      const normalizedIndex = normalizeSelectedIndex(restored, restored.selected_index);
      setDecision(restored);
      setSelectedIndex(normalizedIndex);
      setInput(restored.query);
      return;
    }

    void loadRouteState().then((payload) => {
      if (!payload) {
        return;
      }
      const restoredDecision = coerceRouteDecision(payload) ?? decisionFromSessionState(payload);
      if (!restoredDecision) {
        return;
      }
      setDecision(restoredDecision);
      setSelectedIndex(normalizeSelectedIndex(restoredDecision, restoredDecision.selected_index));
      if (restoredDecision.query) {
        setInput(restoredDecision.query);
      }
    });
  }, []);

  useEffect(() => {
    void Promise.all([loadModels(), loadModelSettings()])
      .then(([modelsPayload, settingsPayload]) => {
        setModels(Array.isArray(modelsPayload.models) ? modelsPayload.models : []);
        const preferred = String(settingsPayload.models?.[MODEL_SETTINGS_KEY] || "");
        if (preferred) {
          setSelectedModel(preferred);
        }
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!modelPickerOpen) {
      return;
    }

    const onPointerDown = (event: PointerEvent) => {
      if (modelPickerRef.current && !modelPickerRef.current.contains(event.target as Node)) {
        setModelPickerOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setModelPickerOpen(false);
      }
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

  const runAnswer = useCallback(
    async (query: string, baseDecision: RouteDecision = syntheticAnswerDecision(query)) => {
      setDecision(baseDecision);
      setSelectedIndex(-1);
      persistRouteState(baseDecision, -1);
      const requestRevision = ++answerRequestRef.current;
      setAnswerResult(null);
      setAnswerTransportError("");
      setAnswerLoading(true);
      try {
        const payload = await answerInput(query, selectedModel);
        if (requestRevision !== answerRequestRef.current) return;
        setAnswerResult({ ...normalizeAnswerResponse(payload), query, requestedModel: selectedModel });
      } catch {
        if (requestRevision !== answerRequestRef.current) return;
        setAnswerTransportError(searchHomeCopy.answer.transportError);
      } finally {
        if (requestRevision === answerRequestRef.current) setAnswerLoading(false);
      }
    },
    [persistRouteState, selectedModel],
  );

  const executeSearch = useCallback(
    async (action: SearchAction) => {
      const query = input.trim();
      if (!query || loading || answerLoading) {
        return;
      }
      committedNavigationRef.current = false;
      setLoading(true);
      try {
        const explicitDestination = evaluateExplicitDestinationInput(query);
        if (explicitDestination?.verdict === "block") {
          const blockedDecision: RouteDecision = {
            route_type: "BLOCKED_DESTINATION_INPUT",
            query: "Blocked URL input",
            target_url: query,
            target_candidates: [],
            selected_index: -1,
            fallback_url: query,
            resolution_reason: `input_policy:${explicitDestination.reason}`,
            used_ai_judge: false,
            used_visual_judge: false,
            metadata: { input_policy_blocked: true },
          };
          setInput("");
          setDecision(blockedDecision);
          setSelectedIndex(-1);
          return;
        }
        if (action === "answer") {
          await runAnswer(query);
          return;
        }
        if (action === "google") {
          const fallbackDecision: RouteDecision = {
            route_type: "GOOGLE_REDIRECT",
            query,
            target_url: googleSearchUrl(query),
            target_candidates: [],
            selected_index: -1,
            fallback_url: googleSearchUrl(query),
            resolution_reason: "manual:google_search",
            used_ai_judge: false,
            used_visual_judge: false,
            metadata: {},
          };
          setDecision(fallbackDecision);
          setSelectedIndex(-1);
          return;
        }

        const nextDecision = await routeInput(query, selectedModel);
        const nextIndex = normalizeSelectedIndex(nextDecision, nextDecision.selected_index);
        setDecision(nextDecision);
        setSelectedIndex(nextIndex);
        persistRouteState(nextDecision, nextIndex);
        if (isAnswerRoute(nextDecision)) {
          await runAnswer(query, nextDecision);
          return;
        }
      } catch (submitError) {
        console.warn("Search Home route failed", submitError);
      } finally {
        setLoading(false);
      }
    },
    [
      answerLoading,
      input,
      loading,
      navigate,
      persistRouteState,
      runAnswer,
      selectedModel,
    ],
  );

  const handleSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      void executeSearch(activeAction);
    },
    [activeAction, executeSearch],
  );

  const selectModel = useCallback((nextModel: string) => {
    setSelectedModel(nextModel);
    setModelPickerOpen(false);
    setModelFilter("");
    void setPreferredModel(nextModel).catch(() => undefined);
  }, []);

  const handleFileChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const nextFile = event.target.files?.[0] ?? null;
    setAttachedFile(nextFile);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, []);

  return (
    <main className="app-shell">
      <section className="hero-search">
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
              <span className="model-trigger-caret" aria-hidden="true">
                ˅
              </span>
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
                      <div>
                        <dt>{searchHomeCopy.model.profileId}</dt>
                        <dd>{selectedModelItem.profile_id}</dd>
                      </div>
                      {selectedModelItem.qualified_model_id ? (
                        <div>
                          <dt>{searchHomeCopy.model.qualifiedModelId}</dt>
                          <dd>{selectedModelItem.qualified_model_id}</dd>
                        </div>
                      ) : null}
                      {selectedModelItem.model_id ? (
                        <div>
                          <dt>{searchHomeCopy.model.modelId}</dt>
                          <dd>{selectedModelItem.model_id}</dd>
                        </div>
                      ) : null}
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
                      <span className="model-badges">
                        <span className="model-badge">{searchHomeCopy.model.defaultStatus}</span>
                      </span>
                    </span>
                  </button>
                  {filteredModels.map((model) => {
                    const value = searchHomeModelId(model);
                    if (!value) {
                      return null;
                    }
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
                          <span className="model-badges">
                            <span className="model-badge">{searchHomeModelStatus(model)}</span>
                          </span>
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
              >
                +
              </button>
              <input
                aria-label={searchHomeCopy.search.inputLabel}
                className="search-input"
                value={input}
                onBlur={() => setIsFocused(false)}
                onChange={(event) => setInput(event.target.value)}
                onFocus={() => setIsFocused(true)}
                onKeyDown={(event) => {
                  if (!input.trim()) {
                    return;
                  }
                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setSelectedActionIndex((current) => (current + 1) % SEARCH_HOME_ACTIONS.length);
                  }
                  if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setSelectedActionIndex((current) => (
                      current - 1 + SEARCH_HOME_ACTIONS.length
                    ) % SEARCH_HOME_ACTIONS.length);
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
                  <span className="file-glyph" aria-hidden="true">
                    □
                  </span>
                  <span className="file-meta">
                    <strong>{attachedFile.name}</strong>
                    <span>{(attachedFile.size / 1024 / 1024).toFixed(2)} MB</span>
                  </span>
                  <button aria-label={searchHomeCopy.search.removeFile} className="remove-file" type="button" onClick={() => setAttachedFile(null)}>
                    ×
                  </button>
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
                      setSelectedActionIndex(index);
                      void executeSearch(action.id);
                    }}
                    onMouseEnter={() => setSelectedActionIndex(index)}
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

        {answerLoading ? (
          <section className="answer-card" aria-busy="true" aria-live="polite">
            <strong>{searchHomeCopy.answer.inProgressTitle}</strong>
            <p>{searchHomeCopy.answer.inProgressDetail}</p>
          </section>
        ) : null}

        {answerTransportError ? (
          <section className="answer-card answer-card-error" role="alert">
            <strong>{searchHomeCopy.answer.requestFailedTitle}</strong>
            <p>{answerTransportError}</p>
            <button type="button" onClick={() => void runAnswer(input.trim())}>{searchHomeCopy.answer.retry}</button>
          </section>
        ) : null}

        {answerResult ? (
          <section className={`answer-card answer-card-${answerResult.kind}`} aria-live="polite" aria-labelledby="search-answer-title">
            <header>
              <div>
                <span>{answerResult.kind === "success" ? searchHomeCopy.answer.successLabel : searchHomeCopy.answer.statusLabel}</span>
                <h2 id="search-answer-title">{answerResult.message}</h2>
              </div>
              <span>{searchHomeModelLabelForReference(models, answerResult.model || answerResult.requestedModel)}</span>
            </header>
            {answerResult.answer ? <p className="answer-text">{answerResult.answer}</p> : null}
            {answerResult.degradedReason ? <p className="answer-warning">{searchHomeCopy.answer.toolsUnavailable}</p> : null}
            <dl>
              <div><dt>{searchHomeCopy.answer.originalQuery}</dt><dd>{answerResult.query}</dd></div>
              <div><dt>{searchHomeCopy.answer.toolsUsed}</dt><dd>{answerResult.usedToolsCount ? searchHomeCopy.answer.toolCount(answerResult.usedToolsCount) : searchHomeCopy.answer.noTools}</dd></div>
            </dl>
            <div className="answer-actions">
              {answerResult.conversationId ? (
                <a href={conversationHref(answerResult.conversationId)}>{searchHomeCopy.answer.openConversation}</a>
              ) : null}
              <button type="button" onClick={() => void runAnswer(answerResult.query)}>{searchHomeCopy.answer.retry}</button>
              <button type="button" onClick={() => { setAnswerResult(null); setAnswerTransportError(""); }}>{searchHomeCopy.answer.dismiss}</button>
            </div>
            <p className="answer-privacy-note">{searchHomeCopy.answer.privacyNote}</p>
          </section>
        ) : null}
      </section>

      {currentDecision && !isAnswerRoute(currentDecision) ? (
        <NavigationReview
          decision={currentDecision}
          selectedIndex={selectedIndex}
          onSelectIndex={(nextIndex) => {
            setSelectedIndex(nextIndex);
            persistRouteState(currentDecision, nextIndex);
          }}
          onOpenSelected={() =>
            navigate(currentDecision, selectedIndex, selectedCandidateUrl(currentDecision, selectedIndex))
          }
          onOpenFallback={() => navigate(currentDecision, -1, currentDecision.fallback_url)}
          onCopy={() => {
            const destination = reviewRouteDestination(selectedCandidateUrl(currentDecision, selectedIndex));
            const details = destination.ok
              ? destination.url
              : searchHomeCopy.review.blockedClipboard(destination.code, destination.message);
            void navigator.clipboard.writeText(details);
          }}
          onCancel={() => {
            setDecision(null);
            setSelectedIndex(-1);
          }}
        />
      ) : null}

    </main>
  );
}
