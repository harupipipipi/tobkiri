import type { ModelAccessPolicy } from "../features/apiKeys/modelAccessPolicy";
import type { ModelAccessScope } from "../features/apiKeys/resources/providerModelAccessResources";
import type { ProviderConfigurationStatus } from "./providerConfiguration";

export type ModelAccessSaveRequest = ModelAccessScope & { expected_revision: number; model_access: ModelAccessPolicy };
export type ModelAccessEffectPorts = {
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  prepare(request: ModelAccessSaveRequest, correlation: string): Promise<ProviderConfigurationStatus>;
  lookup(correlation: string): Promise<ProviderConfigurationStatus>;
  status(effect: string): Promise<ProviderConfigurationStatus>;
  resume(effect: string): Promise<ProviderConfigurationStatus>;
  cancel(effect: string): Promise<ProviderConfigurationStatus>;
  approval(id: string): Promise<{ request_id: string; state: string }>;
  openApproval(id: string): Promise<boolean>;
  pause(): Promise<void>;
};
type Pending = { scope: string; digest: string; correlation: string; effect: string | null; resumeAttempted?: boolean };
const storageKey = "tobkiri-model-access-pending-v1";
class ModelAccessEffectError extends Error {}
let busy = false;

/** Reconcile the exact Host effect; lost replies never authorize another save. */
export async function saveModelAccessEffect(request: ModelAccessSaveRequest, ports: ModelAccessEffectPorts): Promise<void> {
  if (busy) throw new ModelAccessEffectError("モデル許可設定は処理中です。承認画面を確認してください。");
  busy = true;
  try {
    const scope = JSON.stringify([request.profile_id, request.provider_instance_id]);
    const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(request)));
    const digest = Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, "0")).join("");
    const stored = ports.storage.getItem(storageKey);
    const pending: Pending = stored ? JSON.parse(stored) : { scope, digest, correlation: crypto.randomUUID(), effect: null };
    if (pending.scope !== scope || typeof pending.correlation !== "string") {
      throw new ModelAccessEffectError("前のモデル許可設定を確認してから保存してください。変更後の入力は送信していません。");
    }
    if (pending.digest !== digest) {
      // A lost resume reply may precede a refreshed registry revision. Reconcile
      // only that saved effect; this invocation never dispatches changed input.
      if (pending.resumeAttempted === true && typeof pending.effect === "string" && pending.effect.trim()) {
        const previous = await ports.status(pending.effect);
        if (previous.effect_id !== pending.effect) {
          throw new ModelAccessEffectError("保存の操作IDが一致しません。変更後の入力は送信していません。");
        }
        if (previous.state === "succeeded" || previous.state === "cancelled") {
          ports.storage.removeItem(storageKey);
          throw new ModelAccessEffectError(previous.state === "succeeded"
            ? "前の保存が完了しています。再読み込みして確認してください。新しい入力は送信していません。"
            : "前の保存は取消済みです。再読み込みして確認してください。新しい入力は送信していません。");
        }
      }
      throw new ModelAccessEffectError("前のモデル許可設定を確認してから保存してください。変更後の入力は送信していません。");
    }
    let status: ProviderConfigurationStatus;
    if (stored) {
      if (!pending.effect) {
        const receipt = await ports.lookup(pending.correlation);
        if (!receipt.effect_id) throw new ModelAccessEffectError("保存の受付結果が不明です。再送せず承認一覧を確認してください。");
        pending.effect = receipt.effect_id;
        ports.storage.setItem(storageKey, JSON.stringify(pending));
        status = receipt;
      } else status = await ports.status(pending.effect);
    } else {
      ports.storage.setItem(storageKey, JSON.stringify(pending));
      status = await ports.prepare(request, pending.correlation);
      if (!status.effect_id) throw new ModelAccessEffectError("保存の受付結果が不明です。再送せず承認一覧を確認してください。");
      pending.effect = status.effect_id;
      ports.storage.setItem(storageKey, JSON.stringify(pending));
    }
    let opened = false;
    let resumed = pending.resumeAttempted === true;
    for (let attempt = 0; attempt < 300; attempt += 1) {
      if (status.effect_id !== pending.effect) throw new ModelAccessEffectError("保存の操作IDが一致しません。");
      if (status.state === "succeeded") { ports.storage.removeItem(storageKey); return; }
      if (["failed", "ambiguous", "stale", "cancelled"].includes(status.state)) {
        if (status.state === "cancelled") ports.storage.removeItem(storageKey);
        throw new ModelAccessEffectError(`モデル許可設定は完了していません (${status.state})。承認一覧を確認してください。`);
      }
      if (!["prepared", "approval_pending", "approved", "claimed", "dispatched"].includes(status.state)) {
        throw new ModelAccessEffectError("保存の状態を確認できません。再送せず承認一覧を確認してください。");
      }
      if (!resumed && ["prepared", "approval_pending", "approved"].includes(status.state)) {
        const id = status.approval_request_id;
        if (!id) throw new ModelAccessEffectError("保存の承認IDがありません。");
        const approval = await ports.approval(id);
        if (approval.request_id !== id) throw new ModelAccessEffectError("保存の承認IDが一致しません。");
        if (approval.state === "approved") {
          resumed = true;
          pending.resumeAttempted = true;
          ports.storage.setItem(storageKey, JSON.stringify(pending));
          status = await ports.resume(pending.effect!);
          continue;
        }
        if (["denied", "expired", "cancelled", "rejected"].includes(approval.state)) {
          const cancelled = await ports.cancel(pending.effect!);
          if (cancelled.effect_id !== pending.effect || cancelled.state !== "cancelled") throw new ModelAccessEffectError("保存の取消を確認できません。");
          ports.storage.removeItem(storageKey);
          throw new ModelAccessEffectError("モデル許可設定は承認されていません。保存していません。");
        }
        if (!opened) {
          opened = await ports.openApproval(id);
          if (!opened) throw new ModelAccessEffectError("Tobkiri Launcherの承認一覧で確認後、同じ設定の保存から再開してください。");
        }
      }
      await ports.pause();
      status = await ports.status(pending.effect!);
    }
    throw new ModelAccessEffectError("確認待ちです。同じ設定の保存から操作IDを照合できます。");
  } catch (error) {
    if (error instanceof ModelAccessEffectError) throw error;
    throw new ModelAccessEffectError("保存結果を確認できません。同じ設定の保存から状態を確認してください。自動再送は行いません。");
  } finally { busy = false; }
}
