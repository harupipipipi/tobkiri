import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  answerInput,
  loadModelSettings,
  loadModels,
  MODEL_SETTINGS_KEY,
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
import { normalizeAnswerResponse, type AnswerResult } from "./answerState";
import { evaluateExplicitDestinationInput } from "./destinationPolicy";
import { SearchHomeControls } from "./SearchHomeControls";
import { captureSearchRequest, requestErrorMessage, type SearchAction, type SearchRequest } from "./searchRequest";

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
    route_type: "ASK_AI",
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
      selected_tools: [],
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
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelLoadError, setModelLoadError] = useState("");
  const [modelSaving, setModelSaving] = useState(false);
  const [modelSaveError, setModelSaveError] = useState<{ model: string; message: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [answerLoading, setAnswerLoading] = useState(false);
  const [answerResult, setAnswerResult] = useState<(AnswerResult & { query: string; requestedModel: string }) | null>(null);
  const [requestFailure, setRequestFailure] = useState<{ request: SearchRequest; message: string } | null>(null);
  const [navigationStatus, setNavigationStatus] = useState("");
  const [navigationError, setNavigationError] = useState("");
  const committedNavigationRef = useRef(false);
  const answerRequestRef = useRef(0);
  const modelSelectionChangedRef = useRef(false);
  const modelLoadRevisionRef = useRef(0);
  const busyRef = useRef(false);

  const refreshModels = useCallback(async () => {
    const revision = ++modelLoadRevisionRef.current;
    setModelsLoading(true);
    setModelLoadError("");
    const [catalog, settings] = await Promise.allSettled([loadModels(), loadModelSettings()]);
    if (revision !== modelLoadRevisionRef.current) return;
    const failures: string[] = [];
    if (catalog.status === "fulfilled") {
      setModels(Array.isArray(catalog.value.models) ? catalog.value.models : []);
    } else {
      failures.push(requestErrorMessage(catalog.reason, "モデル一覧を読み込めませんでした。"));
    }
    if (settings.status === "fulfilled") {
      if (!modelSelectionChangedRef.current) setSelectedModel(String(settings.value.models?.[MODEL_SETTINGS_KEY] || ""));
    } else {
      failures.push(requestErrorMessage(settings.reason, "共有モデル設定を読み込めませんでした。"));
    }
    setModelLoadError(failures.join(" "));
    setModelsLoading(false);
  }, []);

  useEffect(() => {
    void refreshModels();
    return () => { modelLoadRevisionRef.current += 1; };
  }, [refreshModels]);

  const saveModel = useCallback(async (model: string) => {
    setModelSaving(true);
    setModelSaveError(null);
    try {
      await setPreferredModel(model);
    } catch (error) {
      setModelSaveError({ model, message: requestErrorMessage(error, "モデル選択を共有設定に保存できませんでした。") });
    } finally {
      setModelSaving(false);
    }
  }, []);

  const selectModel = useCallback((model: string) => {
    modelSelectionChangedRef.current = true;
    setSelectedModel(model);
    void saveModel(model);
  }, [saveModel]);

  const currentDecision = useMemo(() => {
    if (!decision) {
      return null;
    }
    return {
      ...decision,
      selected_index: normalizeSelectedIndex(decision, selectedIndex),
    };
  }, [decision, selectedIndex]);

  const persistRouteState = useCallback((nextDecision: RouteDecision, nextIndex: number) => {
    const storage = typeof window !== "undefined" ? window.sessionStorage : null;
    const session = saveDecisionToSessionStorage(storage, nextDecision, nextIndex);
    if (session) {
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

  }, []);

  const runAnswer = useCallback(
    async (request: SearchRequest, baseDecision: RouteDecision = syntheticAnswerDecision(request.input)) => {
      setDecision(baseDecision);
      setSelectedIndex(-1);
      persistRouteState(baseDecision, -1);
      const requestRevision = ++answerRequestRef.current;
      setAnswerResult(null);
      setRequestFailure(null);
      setAnswerLoading(true);
      try {
        const payload = await answerInput(request.input, request.model);
        if (requestRevision !== answerRequestRef.current) return;
        setAnswerResult({ ...normalizeAnswerResponse(payload), query: request.input, requestedModel: request.model });
      } catch (error) {
        if (requestRevision !== answerRequestRef.current) return;
        setRequestFailure({ request: { ...request, action: "answer" }, message: requestErrorMessage(error, "AIの回答を受信できませんでした。接続を確認して再試行してください。") });
      } finally {
        if (requestRevision === answerRequestRef.current) setAnswerLoading(false);
      }
    },
    [persistRouteState],
  );

  const executeSearch = useCallback(
    async (action: SearchAction, retryRequest?: SearchRequest) => {
      const request = retryRequest ?? captureSearchRequest(input, selectedModel, action);
      if (!request || busyRef.current) return;
      const query = request.input;
      busyRef.current = true;
      committedNavigationRef.current = false;
      setLoading(true);
      setRequestFailure(null);
      setAnswerResult(null);
      setDecision(null);
      setSelectedIndex(-1);
      setNavigationError("");
      setNavigationStatus("");
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
          await runAnswer(request);
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

        const nextDecision = await routeInput(query, request.model, action === "open" ? "open" : "smart");
        const nextIndex = normalizeSelectedIndex(nextDecision, nextDecision.selected_index);
        setDecision(nextDecision);
        setSelectedIndex(nextIndex);
        persistRouteState(nextDecision, nextIndex);
        if (isAnswerRoute(nextDecision)) {
          await runAnswer(request, nextDecision);
          return;
        }
      } catch (submitError) {
        setRequestFailure({ request, message: requestErrorMessage(submitError, "検索を完了できませんでした。接続を確認して再試行してください。") });
      } finally {
        busyRef.current = false;
        setLoading(false);
      }
    },
    [
      input,
      persistRouteState,
      runAnswer,
      selectedModel,
    ],
  );

  return (
    <main className="app-shell">
      <section className="hero-search">
        <SearchHomeControls
          input={input}
          onInputChange={setInput}
          models={models}
          selectedModel={selectedModel}
          onSelectModel={selectModel}
          selectedActionIndex={selectedActionIndex}
          onSelectedActionIndexChange={setSelectedActionIndex}
          loading={loading}
          answerLoading={answerLoading}
          modelsLoading={modelsLoading}
          modelSaving={modelSaving}
          onExecute={(action) => void executeSearch(action)}
        />

        {modelLoadError ? (
          <section className="status-notice status-notice-error" role="alert">
            <div><strong>モデル設定の読み込みに失敗しました</strong><p>{modelLoadError}</p></div>
            <button disabled={modelsLoading} type="button" onClick={() => void refreshModels()}>再読み込み</button>
          </section>
        ) : null}

        {modelSaveError ? (
          <section className="status-notice status-notice-error" role="alert">
            <div><strong>モデル選択を保存できませんでした</strong><p>{modelSaveError.message}</p><p>{modelSaveError.model || "Defaultsの既定モデル"}</p><p>この検索では選択したモデルを使用できます。共有設定への保存を再試行できます。</p></div>
            <button disabled={modelSaving} type="button" onClick={() => void saveModel(modelSaveError.model)}>保存を再試行</button>
          </section>
        ) : null}

        {answerLoading ? (
          <section className="answer-card" aria-busy="true" aria-live="polite">
            <strong>AIで回答を作成しています…</strong>
            <p>選択したモデルの知識で回答します。回答が届くまでお待ちください。</p>
          </section>
        ) : loading ? <p className="search-progress" role="status">検索候補を確認しています…</p> : null}

        {requestFailure ? (
          <section className="answer-card answer-card-error" role="alert">
            <strong>検索を完了できませんでした</strong>
            <p>{requestFailure.message}</p>
            <dl>
              <div><dt>送信した入力</dt><dd>{requestFailure.request.input}</dd></div>
              <div><dt>モデル</dt><dd>{requestFailure.request.model || "Defaultsの既定モデル"}</dd></div>
            </dl>
            <button disabled={loading || answerLoading} type="button" onClick={() => void executeSearch(requestFailure.request.action, requestFailure.request)}>同じ入力・モデルで再試行</button>
          </section>
        ) : null}

        {answerResult ? (
          <section className={`answer-card answer-card-${answerResult.kind}`} aria-live="polite" aria-labelledby="search-answer-title">
            <header>
              <div>
                <span>{answerResult.kind === "success" ? "AIの回答" : "回答の状態"}</span>
                <h2 id="search-answer-title">{answerResult.message}</h2>
              </div>
              <span>{answerResult.model || answerResult.requestedModel || "Defaultsの既定モデル"}</span>
            </header>
            {answerResult.answer ? <p className="answer-text">{answerResult.answer}</p> : null}
            <p className="answer-warning">AIの回答はモデルの知識に基づきます。最新情報を調べるには「Googleで検索」を選んでください。</p>
            <dl>
              <div><dt>検索内容</dt><dd>{answerResult.query}</dd></div>
              <div><dt>ツールの使用</dt><dd>{answerResult.usedToolsCount ? `${answerResult.usedToolsCount} 回` : "使用なし"}</dd></div>
            </dl>
            <div className="answer-actions">
              <button disabled={loading || answerLoading} type="button" onClick={() => void executeSearch("answer", { input: answerResult.query, model: answerResult.requestedModel, action: "answer" })}>同じ入力・モデルで再試行</button>
              <button type="button" onClick={() => { setAnswerResult(null); setRequestFailure(null); }}>閉じる</button>
            </div>
            <p className="answer-privacy-note">この画面の回答は保存されません。再読み込みすると消えます。</p>
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
              : `Tobkiri Search blocked destination: ${destination.code}. ${destination.message}`;
            void navigator.clipboard.writeText(details).then(() => {
              setNavigationError("");
              setNavigationStatus("コピーしました。");
            }).catch((error: unknown) => {
              setNavigationError(requestErrorMessage(error, "コピーできませんでした。"));
            });
          }}
          onCancel={() => {
            setDecision(null);
            setSelectedIndex(-1);
          }}
          status={navigationStatus}
          error={navigationError}
        />
      ) : null}

    </main>
  );
}
