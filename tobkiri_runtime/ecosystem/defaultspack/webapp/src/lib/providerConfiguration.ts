/** Client correlation only; approval and execution state remain Host-owned. */
export type ProviderConfigurationStatus = {
  effect_id: string;
  approval_request_id: string | null;
  state: string;
};

export type ProviderConfigurationRequest = {
  connection_name: string;
  protocol: "openai-compatible" | "anthropic";
  endpoint: string;
  key_value: string;
};

type Pending = { connection: string; effect: string | null; digest: string };
type Ports = {
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  prepare: (request: ProviderConfigurationRequest) => Promise<ProviderConfigurationStatus>;
  status: (effect: string) => Promise<ProviderConfigurationStatus>;
  resume: (effect: string) => Promise<ProviderConfigurationStatus>;
  cancel: (effect: string) => Promise<ProviderConfigurationStatus>;
  approval: (id: string) => Promise<{ request_id: string; state: string }>;
  openApproval: (id: string) => Promise<boolean>;
  pause: () => Promise<void>;
};

const STORAGE_KEY = "tobkiri-provider-configuration-pending-v1";
class ConfigurationError extends Error {}
let busy = false;

/** Complete or reconcile one setting, never replaying an uncertain prepare. */
export async function configureProvider(
  request: ProviderConfigurationRequest, ports: Ports,
): Promise<void> {
  if (busy) throw new ConfigurationError("Provider設定は処理中です。承認画面を確認してください。");
  busy = true;
  try {
    const digestBytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(request)));
    const digest = Array.from(new Uint8Array(digestBytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
    const stored = ports.storage.getItem(STORAGE_KEY);
    const pending: Pending = stored ? JSON.parse(stored) : {
      connection: request.connection_name, effect: null, digest,
    };
    if (pending.connection !== request.connection_name || pending.digest !== digest) {
      throw new ConfigurationError("前のProvider設定が未確認です。入力を変更せず同じ設定の保存から状態を確認してください。");
    }
    if (stored && (typeof pending.effect !== "string" || !pending.effect)) {
      throw new ConfigurationError("Provider設定の受付結果が不明です。再送せずHostの承認一覧を確認してください。");
    }
    let status: ProviderConfigurationStatus;
    if (stored) {
      status = await ports.status(pending.effect!);
    } else {
      // Written before sending: response loss must not silently create another effect.
      // Only correlation and a request digest are persisted, never the key or URL.
      ports.storage.setItem(STORAGE_KEY, JSON.stringify(pending));
      status = await ports.prepare(request);
      if (typeof status.effect_id !== "string" || !status.effect_id) {
        throw new ConfigurationError("Provider設定の受付結果が不明です。再送しないでください。");
      }
      pending.effect = status.effect_id;
      ports.storage.setItem(STORAGE_KEY, JSON.stringify(pending));
    }
    let opened = false;
    let resumed = false;
    for (let attempt = 0; attempt < 300; attempt += 1) {
      if (status.effect_id !== pending.effect) throw new ConfigurationError("Provider設定の操作IDが一致しません。");
      if (status.state === "succeeded") {
        ports.storage.removeItem(STORAGE_KEY);
        return;
      }
      if (["failed", "ambiguous", "stale", "cancelled"].includes(status.state)) {
        if (status.state === "cancelled") ports.storage.removeItem(STORAGE_KEY);
        // Keep the ID for inspection, including failures that may have written a key.
        throw new ConfigurationError(`Provider設定は完了していません (${status.state})。操作 ${pending.effect} を確認してください。`);
      }
      if (!["prepared", "approval_pending", "approved", "claimed", "dispatched"].includes(status.state)) {
        throw new ConfigurationError("Provider設定の状態を確認できません。再送しないでください。");
      }
      if (!resumed && ["prepared", "approval_pending", "approved"].includes(status.state)) {
        const id = status.approval_request_id;
        if (!id) throw new ConfigurationError("Provider設定の承認IDがありません。");
        const approval = await ports.approval(id);
        if (approval.request_id !== id) throw new ConfigurationError("Provider設定の承認IDが一致しません。");
        if (approval.state === "approved") {
          resumed = true;
          status = await ports.resume(pending.effect!);
          continue;
        }
        if (["denied", "expired", "cancelled", "rejected"].includes(approval.state)) {
          const cancelled = await ports.cancel(pending.effect!);
          if (cancelled.effect_id !== pending.effect || cancelled.state !== "cancelled") {
            throw new ConfigurationError("Provider設定の取消を確認できません。操作IDを保持しています。");
          }
          ports.storage.removeItem(STORAGE_KEY);
          throw new ConfigurationError("Provider設定は承認されていません。保存は完了していません。");
        }
        if (!opened) {
          opened = await ports.openApproval(id);
          if (!opened) throw new ConfigurationError("Tobkiri Launcherの承認一覧で確認後、同じ接続の保存から再開してください。");
        }
      }
      await ports.pause();
      status = await ports.status(pending.effect!);
    }
    throw new ConfigurationError("Provider設定の確認待ちです。同じ接続の保存から操作IDの照合を再開できます。");
  } catch (error) {
    // Transport exceptions may contain request details. Never propagate those.
    if (error instanceof ConfigurationError) throw error;
    throw new ConfigurationError("Provider設定の結果を確認できません。同じ接続の保存から状態を確認してください。キーは自動再送しません。");
  } finally {
    busy = false;
  }
}
