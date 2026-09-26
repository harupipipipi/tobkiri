import type { CSSProperties, KeyboardEvent } from "react";

import {
  normalizeSelectedIndex,
  reviewRouteDestination,
  selectedCandidate,
  selectedCandidateUrl,
  type RouteCandidate,
  type RouteDecision,
} from "./routerTypes";
import {
  resolveRouteReviewKeyboardAction,
  routeReviewKeyboardAction,
} from "./routeReviewKeyboard";

const styles: Record<string, CSSProperties> = {
  card: {
    marginTop: 24,
    padding: 20,
    border: "1px solid #2f2f34",
    borderRadius: 24,
    background: "#111111",
    boxShadow: "0 18px 48px rgba(0, 0, 0, 0.28)",
  },
  headingRow: {
    display: "flex",
    alignItems: "flex-start",
    justifyContent: "space-between",
    gap: 16,
    flexWrap: "wrap",
  },
  eyebrow: {
    margin: 0,
    color: "#99f6e4",
    fontSize: "0.75rem",
    fontWeight: 700,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
  },
  heading: {
    margin: "5px 0 0",
    color: "#fafafa",
    fontSize: "1.25rem",
    lineHeight: 1.3,
  },
  muted: {
    margin: "8px 0 0",
    color: "#a1a1aa",
    fontSize: "0.88rem",
    lineHeight: 1.55,
  },
  host: {
    display: "inline-flex",
    alignItems: "center",
    minHeight: 34,
    maxWidth: "100%",
    padding: "0 12px",
    border: "1px solid #3f3f46",
    borderRadius: 999,
    background: "#18181b",
    color: "#e4e4e7",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    fontSize: "0.78rem",
    overflowWrap: "anywhere",
  },
  destination: {
    marginTop: 16,
    padding: 14,
    border: "1px solid #27272a",
    borderRadius: 16,
    background: "#0b0b0b",
  },
  url: {
    margin: "8px 0 0",
    color: "#d4d4d8",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
    fontSize: "0.78rem",
    lineHeight: 1.5,
    overflowWrap: "anywhere",
  },
  warningList: {
    display: "flex",
    flexWrap: "wrap",
    gap: 8,
    margin: "12px 0 0",
    padding: 0,
    listStyle: "none",
  },
  warning: {
    padding: "5px 9px",
    border: "1px solid rgba(251, 191, 36, 0.36)",
    borderRadius: 999,
    background: "rgba(120, 53, 15, 0.22)",
    color: "#fde68a",
    fontSize: "0.74rem",
  },
  blocked: {
    marginTop: 12,
    padding: 12,
    border: "1px solid rgba(244, 63, 94, 0.4)",
    borderRadius: 12,
    background: "rgba(127, 29, 29, 0.2)",
    color: "#fecdd3",
    fontSize: "0.86rem",
    lineHeight: 1.5,
  },
  candidateList: {
    display: "grid",
    gap: 8,
    marginTop: 16,
  },
  candidateButton: {
    width: "100%",
    minHeight: 48,
    display: "grid",
    gridTemplateColumns: "minmax(0, 1fr) auto",
    gap: 12,
    alignItems: "center",
    padding: "10px 12px",
    border: "1px solid #27272a",
    borderRadius: 14,
    background: "#151515",
    color: "#d4d4d8",
    cursor: "pointer",
    textAlign: "left",
  },
  candidateButtonActive: {
    borderColor: "rgba(20, 184, 166, 0.7)",
    background: "#0d1f1c",
    color: "#ffffff",
  },
  candidateMain: {
    minWidth: 0,
    display: "grid",
    gap: 4,
  },
  candidateTitle: {
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontSize: "0.9rem",
  },
  candidateHost: {
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    color: "#71717a",
    fontSize: "0.76rem",
  },
  candidateState: {
    color: "#a1a1aa",
    fontSize: "0.72rem",
    whiteSpace: "nowrap",
  },
  actions: {
    display: "flex",
    flexWrap: "wrap",
    gap: 10,
    marginTop: 18,
  },
  primaryButton: {
    minHeight: 44,
    padding: "0 18px",
    border: 0,
    borderRadius: 999,
    background: "#f5f5f4",
    color: "#111111",
    cursor: "pointer",
    fontWeight: 700,
  },
  secondaryButton: {
    minHeight: 44,
    padding: "0 16px",
    border: "1px solid #3f3f46",
    borderRadius: 999,
    background: "#18181b",
    color: "#e4e4e7",
    cursor: "pointer",
    fontWeight: 600,
  },
  disabledButton: {
    cursor: "not-allowed",
    opacity: 0.45,
  },
  status: {
    margin: "12px 0 0",
    color: "#a7f3d0",
    fontSize: "0.82rem",
  },
  error: {
    margin: "12px 0 0",
    color: "#fecaca",
    fontSize: "0.82rem",
  },
  shortcutPanel: {
    marginTop: 16,
    padding: 12,
    border: "1px solid #3f3f46",
    borderRadius: 14,
    background: "#18181b",
    color: "#d4d4d8",
    fontSize: "0.82rem",
    lineHeight: 1.55,
  },
  shortcutKeys: {
    margin: "6px 0 0",
    color: "#a7f3d0",
  },
};

function candidateRiskLabels(candidate: RouteCandidate | null): string[] {
  if (!candidate) return [];
  const labels: string[] = [];
  if (candidate.redirected) labels.push("リダイレクト後のURLです");
  if (candidate.looks_like_login) labels.push("ログイン画面の可能性があります");
  if (candidate.looks_like_paywall) labels.push("有料記事の可能性があります");
  if (candidate.looks_like_404) labels.push("見つからないページの可能性があります");
  if (candidate.looks_like_ad_heavy) labels.push("広告が多い可能性があります");
  return labels;
}

export function NavigationReview({
  decision,
  selectedIndex,
  onSelectIndex,
  onOpenSelected,
  onOpenFallback,
  onCopy,
  onCancel,
  shortcutsRequireReview = false,
  onEnableShortcuts,
  status,
  error,
}: {
  decision: RouteDecision;
  selectedIndex: number;
  onSelectIndex: (index: number) => void;
  onOpenSelected: () => void;
  onOpenFallback: () => void;
  onCopy: () => void;
  onCancel: () => void;
  shortcutsRequireReview?: boolean;
  onEnableShortcuts?: () => void;
  status?: string | null;
  error?: string | null;
}) {
  const normalizedIndex = normalizeSelectedIndex(decision, selectedIndex);
  const candidate = selectedCandidate(decision, normalizedIndex);
  const rawDestination = selectedCandidateUrl(decision, normalizedIndex);
  const destination = reviewRouteDestination(rawDestination);
  const fallback = reviewRouteDestination(decision.fallback_url);
  const riskLabels = candidateRiskLabels(candidate);
  const selectedTitle = candidate?.title || (destination.ok ? "選択した移動先" : "ブロックされた移動先");
  const shortcutsEnabled = !shortcutsRequireReview;
  const handleReviewKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    const action = routeReviewKeyboardAction(
      {
        altKey: event.altKey,
        ctrlKey: event.ctrlKey,
        currentTarget: event.currentTarget,
        defaultPrevented: event.defaultPrevented,
        isComposing: event.nativeEvent.isComposing,
        key: event.key,
        metaKey: event.metaKey,
        repeat: event.repeat,
        shiftKey: event.shiftKey,
        target: event.target,
      },
      shortcutsEnabled,
    );
    if (!action) return;
    event.preventDefault();
    const result = resolveRouteReviewKeyboardAction(
      decision,
      normalizedIndex,
      action,
    );
    if (!result.shouldConfirm) {
      onSelectIndex(result.nextIndex);
      return;
    }
    if (destination.ok) onOpenSelected();
  };

  return (
    <section
      aria-describedby="route-review-shortcuts"
      aria-labelledby="route-review-title"
      aria-keyshortcuts={shortcutsEnabled ? "ArrowLeft ArrowRight Enter" : undefined}
      onKeyDown={handleReviewKeyDown}
      style={styles.card}
      tabIndex={0}
    >
      <div style={styles.headingRow}>
        <div>
          <p style={styles.eyebrow}>移動前の確認</p>
          <h2 id="route-review-title" style={styles.heading}>
            {selectedTitle}
          </h2>
          <p style={styles.muted}>
            Search Homeは自動では移動しません。ホストと警告を確認してから開いてください。
          </p>
        </div>
        <span style={styles.host}>{destination.ok ? destination.host : "ブロック"}</span>
      </div>

      <div id="route-review-shortcuts" style={styles.shortcutPanel}>
        <strong>{shortcutsEnabled ? "候補レビューのキーボード操作" : "復元した候補は再確認が必要です"}</strong>
        <p style={styles.shortcutKeys}>
          {shortcutsEnabled
            ? `この枠にフォーカス中のみ ← / → で候補を選択。別の Enter 操作で「${destination.ok ? destination.url : "ブロックされた移動先"}」を開きます。`
            : "移動先を再検証し、現在のセッションで確認するまでショートカットは動作しません。"}
        </p>
        <p style={styles.muted}>
          ブラウザ、支援技術、OS、IMEとの競合を避けるため、修飾キー付き操作、キーリピート、変換中のキーは無視します。
        </p>
        {shortcutsRequireReview ? (
          <button
            disabled={!destination.ok}
            onClick={onEnableShortcuts}
            style={{
              ...styles.secondaryButton,
              ...(!destination.ok ? styles.disabledButton : {}),
              marginTop: 10,
            }}
            type="button"
          >
            移動先を再検証してショートカットを有効にする
          </button>
        ) : null}
      </div>

      <div style={styles.destination}>
        {destination.ok ? (
          <>
            <strong>{destination.protocol === "https:" ? "HTTPS" : "HTTP"} の移動先</strong>
            <p style={styles.url}>{destination.url}</p>
            {destination.warnings.length || riskLabels.length ? (
              <ul aria-label="移動先の注意" style={styles.warningList}>
                {[...destination.warnings, ...riskLabels].map((warning) => (
                  <li key={warning} style={styles.warning}>
                    {warning}
                  </li>
                ))}
              </ul>
            ) : null}
          </>
        ) : (
          <div role="alert" style={styles.blocked}>
            <strong>この移動先は開けません。</strong>
            <div>{destination.message}</div>
          </div>
        )}
      </div>

      {decision.target_candidates.length > 1 ? (
        <div aria-label="移動先候補" style={styles.candidateList}>
          {decision.target_candidates.map((item, index) => {
            const itemReview = reviewRouteDestination(item.final_url || item.url);
            const active = index === normalizedIndex;
            return (
              <button
                aria-pressed={active}
                key={`${item.final_url || item.url}-${index}`}
                onClick={() => onSelectIndex(index)}
                style={{
                  ...styles.candidateButton,
                  ...(active ? styles.candidateButtonActive : {}),
                }}
                type="button"
              >
                <span style={styles.candidateMain}>
                  <strong style={styles.candidateTitle}>{item.title || `候補 ${index + 1}`}</strong>
                  <span style={styles.candidateHost}>{itemReview.ok ? itemReview.host : itemReview.message}</span>
                </span>
                <span style={styles.candidateState}>{itemReview.ok ? `${index + 1}/${decision.target_candidates.length}` : "ブロック"}</span>
              </button>
            );
          })}
        </div>
      ) : null}

      <div style={styles.actions}>
        <button
          disabled={!destination.ok}
          onClick={onOpenSelected}
          style={{
            ...styles.primaryButton,
            ...(!destination.ok ? styles.disabledButton : {}),
          }}
          type="button"
        >
          この移動先を開く
        </button>
        <button
          onClick={onCopy}
          style={styles.secondaryButton}
          type="button"
        >
          {destination.ok ? "URLをコピー" : "ブロック詳細をコピー"}
        </button>
        {fallback.ok && fallback.url !== (destination.ok ? destination.url : "") ? (
          <button onClick={onOpenFallback} style={styles.secondaryButton} type="button">
            Google検索を開く
          </button>
        ) : null}
        <button onClick={onCancel} style={styles.secondaryButton} type="button">
          キャンセル
        </button>
      </div>

      <div aria-live="polite" aria-atomic="true">
        {status ? <p style={styles.status}>{status}</p> : null}
        {error ? <p role="alert" style={styles.error}>{error}</p> : null}
      </div>
    </section>
  );
}
