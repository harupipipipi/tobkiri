import { useCallback, useEffect, useRef, useState } from "react";

import { mobileApiResources, type MobilePairingApi, type MobilePairingReview, type MobilePairingStatus } from "../features/mobile/resources/mobileApiResources";
import { PairingRequestGate, pairingDecisionReason, pairingErrorCode, pairingSettlement, type PairingDecision, type PairingSettlement } from "../features/mobile/mobilePairingReview";
import { ErrorNotice } from "./ErrorNotice";

type Props = {
  pairingId: string;
  api?: MobilePairingApi;
  onClose?: (outcome: "keep-pending" | PairingSettlement) => void;
  originRef?: { current: HTMLElement | null };
};

const settlementCopy: Record<PairingSettlement, string> = {
  approved: "接続を承認しました",
  rejected: "接続要求を拒否しました",
  expired: "接続要求の期限が切れました",
  revoked: "接続要求は取り消されました",
  "already-settled": "接続要求はすでに処理されています",
};

export function MobilePairingApproval({ pairingId, api = mobileApiResources, onClose, originRef }: Props) {
  const [status, setStatus] = useState<MobilePairingStatus | null>(null);
  const [review, setReview] = useState<MobilePairingReview | null>(null);
  const [loading, setLoading] = useState(true);
  const [pollError, setPollError] = useState("");
  const [decision, setDecision] = useState<PairingDecision | "">("");
  const [decisionError, setDecisionError] = useState("");
  const [settlement, setSettlement] = useState<PairingSettlement | null>(null);
  const [closeReview, setCloseReview] = useState(false);
  const mounted = useRef(true);
  const gate = useRef(new PairingRequestGate());
  const refreshGeneration = useRef(0);
  const closeButton = useRef<HTMLButtonElement>(null);

  const announceSettlement = useCallback((next: PairingSettlement) => {
    setSettlement(next);
    setCloseReview(false);
  }, []);

  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current;
    setLoading(true);
    setPollError("");
    try {
      const authoritative = await api.getPairingStatus(pairingId);
      if (!mounted.current || generation !== refreshGeneration.current) return;
      if (authoritative.pairing_id !== pairingId) throw new Error("pairing status mismatch");
      setStatus(authoritative);
      const settled = pairingSettlement(authoritative.status);
      if (settled) {
        announceSettlement(settled);
        return;
      }
      const details = await api.getPairingReview(pairingId);
      if (!mounted.current || generation !== refreshGeneration.current) return;
      if (details.pairing.pairing_id !== pairingId) throw new Error("pairing review mismatch");
      setReview(details);
    } catch (error) {
      if (!mounted.current || generation !== refreshGeneration.current) return;
      setPollError(error instanceof Error ? error.message : "接続要求を確認できませんでした");
    } finally {
      if (mounted.current && generation === refreshGeneration.current) setLoading(false);
    }
  }, [announceSettlement, api, pairingId]);

  useEffect(() => {
    mounted.current = true;
    gate.current.invalidate();
    refreshGeneration.current += 1;
    setStatus(null);
    setReview(null);
    setSettlement(null);
    setDecisionError("");
    void refresh();
    const timer = window.setInterval(() => { if (!gate.current.busy) void refresh(); }, 2000);
    return () => {
      mounted.current = false;
      gate.current.invalidate();
      refreshGeneration.current += 1;
      window.clearInterval(timer);
    };
  }, [pairingId, refresh]);

  const settle = async (nextDecision: PairingDecision) => {
    const generation = gate.current.begin();
    if (generation === null || settlement) return;
    setDecision(nextDecision);
    setDecisionError("");
    try {
      if (nextDecision === "approve") {
        if (!review) throw new Error("接続要求の詳細を再取得してください");
        await api.approvePairing(pairingId, { claim_hash: review.claim_hash, scopes: review.claim.requested_scopes });
      } else {
        await api.rejectPairing(pairingId, pairingDecisionReason(nextDecision));
      }
      if (!mounted.current) return;
      const authoritative = await api.getPairingStatus(pairingId);
      if (!mounted.current || !gate.current.finish(generation)) return;
      if (authoritative.pairing_id !== pairingId) {
        setDecisionError("接続要求の状態が一致しません。状態を再確認してください");
        return;
      }
      setStatus(authoritative);
      const settled = pairingSettlement(authoritative.status);
      if (settled) {
        announceSettlement(settled);
      } else {
        setDecisionError("処理結果を確認中です。サーバーの状態が確定するまで完了とは表示しません。");
        void refresh();
      }
    } catch (error) {
      if (!mounted.current || !gate.current.finish(generation)) return;
      const code = pairingErrorCode(error);
      if (code !== "failed") {
        await refresh();
        if (mounted.current) setDecisionError("処理結果を確認中です。サーバーの状態を再確認してください。");
      } else {
        setDecisionError(error instanceof Error ? error.message : "操作に失敗しました");
      }
    } finally {
      if (mounted.current) setDecision("");
    }
  };

  const close = (outcome: "keep-pending" | PairingSettlement) => {
    if (gate.current.busy) return;
    onClose?.(outcome);
    queueMicrotask(() => originRef?.current?.focus());
  };

  if (settlement) {
    return (
      <section role="status" aria-live="polite" className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 p-4" data-testid="pairing-settlement">
        <h3 className="font-semibold text-emerald-100">{settlementCopy[settlement]}</h3>
        <p className="mt-1 text-sm text-emerald-100/75">結果はPCの authoritative pairing record に保存されました。</p>
        <button type="button" className="mt-3 rounded-md border border-emerald-400/30 px-3 py-2 text-sm" onClick={() => close(settlement)}>完了</button>
      </section>
    );
  }

  const busy = decision !== "";
  return (
    <section role="dialog" aria-modal="true" aria-labelledby="pairing-review-title" className="relative rounded-xl border border-zinc-700 bg-zinc-950 p-5" data-pairing-status={status?.status ?? "loading"}>
      <button ref={closeButton} type="button" disabled={busy} aria-label={busy ? "処理中は閉じられません" : "閉じ方を確認"} onClick={() => setCloseReview(true)} className="absolute right-3 top-3 rounded p-2 disabled:opacity-40">×</button>
      <h2 id="pairing-review-title" className="pr-10 text-lg font-semibold">スマホの接続要求を確認</h2>
      <p className="mt-2 text-sm text-zinc-400">閉じるだけでは拒否されません。要求を保留したまま閉じるか、明示的に拒否・キャンセルできます。</p>

      {loading && !review ? <p role="status" className="mt-4">authoritative stateを確認しています…</p> : null}
      {pollError ? (
        <ErrorNotice
          className="mt-4"
          copyLabel="接続状態エラーをコピー"
          message={`接続状態を確認できません。要求は変更されていません。 ${pollError}`}
          severity="warning"
        >
          <button type="button" disabled={busy || loading} onClick={() => void refresh()} className="mt-2 underline">再試行</button>
        </ErrorNotice>
      ) : null}
      {review ? (
        <dl className="mt-4 grid gap-2 text-sm">
          <div><dt className="text-zinc-500">端末</dt><dd>{review.claim.device_label}</dd></div>
          <div><dt className="text-zinc-500">確認コード</dt><dd>{review.claim.verification_code ?? "未提供"}</dd></div>
          <div><dt className="text-zinc-500">要求権限</dt><dd>{review.claim.requested_scopes.join("、") || "なし"}</dd></div>
        </dl>
      ) : null}
      {busy ? <p role="status" aria-live="assertive" className="mt-4">{decision === "approve" ? "承認" : "拒否"}を処理中です。この画面は閉じられません。</p> : null}
      {decisionError ? (
        <ErrorNotice
          className="mt-4 text-sm"
          copyLabel="ペアリング処理エラーをコピー"
          message={`${decisionError}。状態を再確認してから再試行できます。`}
        />
      ) : null}
      <div className="mt-5 flex flex-wrap gap-2">
        <button type="button" disabled={busy || !review || Boolean(pollError)} onClick={() => void settle("approve")} className="rounded bg-emerald-300 px-3 py-2 text-zinc-950 disabled:opacity-40">承認</button>
        <button type="button" disabled={busy} onClick={() => void settle("reject")} className="rounded border border-red-400/40 px-3 py-2 text-red-200 disabled:opacity-40">要求を拒否</button>
      </div>

      {closeReview ? (
        <div role="alertdialog" aria-modal="true" aria-labelledby="pairing-close-title" className="mt-5 rounded-lg border border-amber-500/30 bg-amber-500/10 p-4">
          <h3 id="pairing-close-title" className="font-semibold">接続要求をどうしますか？</h3>
          <div className="mt-3 flex flex-wrap gap-2">
            <button autoFocus type="button" onClick={() => close("keep-pending")} className="rounded border px-3 py-2">保留したまま閉じる</button>
            <button type="button" onClick={() => void settle("reject")} className="rounded border px-3 py-2">要求を拒否</button>
            <button type="button" onClick={() => void settle("cancel")} className="rounded border px-3 py-2">ペアリングをキャンセル</button>
            <button type="button" onClick={() => { setCloseReview(false); closeButton.current?.focus(); }} className="rounded px-3 py-2">確認に戻る</button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
