import { useEffect, useState } from "react";
import {
  interactiveApprovalResources as approvals,
  type InteractiveApprovalBatch,
  type InteractiveApprovalRequest,
} from "../features/chat/resources/authorityApprovalResources";
import { getAuthorityApprovalContext, openAuthorityApprovalWindow } from "../lib/desktopApproval";

/** Selection is a proposal; only the native window can authorize its snapshot. */
export function ApprovalBatchPicker() {
  const [items, setItems] = useState<InteractiveApprovalRequest[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await approvals.list();
      setItems(result.approvals.filter(item => item.state === "pending"));
      setSelected([]);
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  const open = async () => {
    setBusy(true);
    setError("");
    try {
      const batch = await approvals.createBatch(selected);
      if (!await openAuthorityApprovalWindow(batch.request_id)) {
        throw new Error("承認はLauncherの専用ウィンドウで行ってください。");
      }
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  return <section aria-label="まとめて承認" className="space-y-3 p-3">
    <button type="button" disabled={busy} onClick={() => void refresh()}>承認待ちの項目を確認</button>
    {items.map(item => <label key={item.request_id} className="block">
      <input type="checkbox" disabled={busy} checked={selected.includes(item.request_id)}
        onChange={event => setSelected(previous => event.target.checked
          ? [...previous, item.request_id] : previous.filter(id => id !== item.request_id))} />
      {item.redacted_metadata.title || item.request_id}
    </label>)}
    <p>選択した要求の正式な操作・対象・有効期間を、専用ウィンドウで確認します。</p>
    <button type="button" disabled={busy || selected.length === 0 || selected.length > 64}
      onClick={() => void open()}>選択した項目を確認する（{selected.length}件）</button>
    {error && <p role="alert">{error}</p>}
  </section>;
}

/** Native proof binds the entire displayed, immutable Host batch. */
export function ApprovalBatchWindow() {
  const requestId = new URLSearchParams(window.location.search).get("request_id") || "";
  const [batch, setBatch] = useState<InteractiveApprovalBatch | null>(null);
  const [texts, setTexts] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    void approvals.getBatch(requestId).then(value => {
      if (value.request_id !== requestId) throw new Error("承認要求が一致しません。");
      if (active) setBatch(value);
    }).catch(cause => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, [requestId]);
  const confirmationsReady = batch?.items.every(item => !item.typed_confirmation_required
    || Boolean(item.redacted_metadata.confirmation_phrase
      && texts[item.request_id]?.trim() === item.redacted_metadata.confirmation_phrase.trim()));
  const settle = async (decision: "approve" | "deny") => {
    if (!batch || busy || batch.state !== "pending") return;
    if (decision === "approve" && !confirmationsReady) return;
    setBusy(true);
    setError("");
    try {
      const current = await approvals.getBatch(requestId);
      if (current.request_id !== requestId || current.state !== "pending") {
        throw new Error("承認要求は処理済み、または期限切れです。");
      }
      if (current.request_snapshot_digest !== batch.request_snapshot_digest) {
        setBatch(current);
        setTexts({});
        throw new Error("承認内容が更新されました。表示を確認して、もう一度操作してください。");
      }
      const context = await getAuthorityApprovalContext(requestId, {
        decision, requestSnapshotDigest: batch.request_snapshot_digest,
        typedConfirmationDigest: null,
      });
      const confirmations = decision === "deny" ? {} : Object.fromEntries(batch.items
        .filter(item => item.typed_confirmation_required)
        .map(item => [item.request_id, texts[item.request_id].trim()]));
      const result = decision === "approve"
        ? await approvals.approveBatch(requestId, confirmations, context.ui_operator)
        : await approvals.denyBatch(requestId, context.ui_operator);
      if (result.request_id !== requestId) throw new Error("承認要求が一致しません。");
      setBatch(result);
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  };
  return <main className="min-h-screen space-y-4 bg-zinc-950 p-5 text-zinc-100">
    <h1>選択した要求をまとめて承認</h1>
    <p>今回は各要求を一度だけ許可します。タスク・セッション・workspaceの継続許可ではありません。</p>
    <p>OS共有resourceの内容はworkspace内に限定されません。</p>
    {batch && <>
      <p>Profile: {batch.profile_id} / 状態: {batch.state}</p>
      <p>承認期限: {new Date(batch.expires_at * 1000).toLocaleString()}</p>
      {batch.items.map(item => <section key={item.request_id} className="space-y-2 rounded border p-3">
        <h2>{item.redacted_metadata.title || item.request_id}</h2>
        <dl>
          <dt>要求元</dt><dd className="break-all">{JSON.stringify(item.caller)}</dd>
          <dt>実行先・操作</dt><dd className="break-all">{JSON.stringify(item.target)}</dd>
          <dt>対象resource・workspaceの制約</dt><dd className="break-all">{JSON.stringify(item.scope)}</dd>
          <dt>今回追加する許可</dt><dd>この要求の一度だけ（{item.lifetime}）</dd>
          <dt>有効期限</dt><dd>{new Date(item.expires_at * 1000).toLocaleString()}</dd>
        </dl>
        {item.typed_confirmation_required && <label className="block">
          確認文: {item.redacted_metadata.confirmation_phrase || "取得できません。承認不可"}
          <input className="block border bg-zinc-900" value={texts[item.request_id] || ""}
            disabled={busy || batch.state !== "pending"}
            onChange={event => setTexts(previous => ({ ...previous, [item.request_id]: event.target.value }))} />
        </label>}
      </section>)}
      <button type="button" disabled={busy || batch.state !== "pending"}
        onClick={() => void settle("deny")}>選択した要求を拒否</button>
      <button type="button" disabled={busy || batch.state !== "pending" || !confirmationsReady}
        onClick={() => void settle("approve")}>表示した要求を今回だけ承認</button>
    </>}
    {error && <p role="alert">{error}</p>}
  </main>;
}
