import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Check, Loader2, Send, ShieldCheck, Smartphone, X } from "lucide-react";

import { ErrorNotice } from "./ErrorNotice";
import { cn } from "../lib/cn";
import { mobileApiResources } from "../features/mobile/resources/mobileApiResources";
import type { CredentialTransfer, MobileDevice } from "../lib/api";

type CredentialTransferModalProps = {
  providerId: string;
  providerLabel?: string;
  apiId?: string;
  onClose: () => void;
};

const terminalStates = new Set(["completed", "rejected", "expired", "revoked", "cancelled"]);

export function credentialTransferCanClose(status: string | null, busy: boolean): boolean {
  return !busy && !["awaiting_confirmation", "pending"].includes(status ?? "");
}

export function credentialTransferFocusTarget(
  currentIndex: number,
  focusableCount: number,
  backwards: boolean,
): number | null {
  if (focusableCount <= 0) return null;
  if (currentIndex < 0) return backwards ? focusableCount - 1 : 0;
  if (backwards && currentIndex <= 0) return focusableCount - 1;
  if (!backwards && currentIndex >= focusableCount - 1) return 0;
  return currentIndex;
}

export function CredentialTransferModal({
  providerId,
  providerLabel,
  apiId = "main",
  onClose,
}: CredentialTransferModalProps) {
  const [devices, setDevices] = useState<MobileDevice[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [transfer, setTransfer] = useState<CredentialTransfer | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  const displayName = providerLabel || providerId;
  const selectedDevice = devices.find((device) => device.device_id === selectedId);

  const requestClose = () => {
    if (busy) return;
    if (!credentialTransferCanClose(transfer?.status ?? null, busy)) {
      setError("有効な転送があります。先に転送をキャンセルしてください。");
      return;
    }
    onClose();
  };

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    headingRef.current?.focus();
    return () => previousFocusRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? []).filter((element) => element.getAttribute("aria-hidden") !== "true");
      if (focusable.length === 0) {
        event.preventDefault();
        headingRef.current?.focus();
        return;
      }
      const currentIndex = focusable.findIndex((element) => element === document.activeElement);
      const target = credentialTransferFocusTarget(currentIndex, focusable.length, event.shiftKey);
      if (target !== null && target !== currentIndex) {
        event.preventDefault();
        focusable[target].focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose, transfer]);

  useEffect(() => {
    let disposed = false;
    void mobileApiResources.listDevices()
      .then((result) => {
        if (disposed) return;
        setDevices((result.devices ?? []).filter((device) =>
          device.status !== "revoked" &&
          device.scopes?.includes("credentials.request") &&
          device.encryption_key_configured !== false,
        ));
      })
      .catch(() => {
        if (!disposed) setError("ペア済み端末を読み込めませんでした。接続を確認して再試行してください。");
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => { disposed = true; };
  }, []);

  useEffect(() => {
    if (!transfer || terminalStates.has(transfer.status) || transfer.status === "awaiting_confirmation") return;
    let disposed = false;
    const poll = window.setInterval(() => {
      void mobileApiResources.getCredentialTransferStatus(transfer.transfer_id)
        .then((result) => {
          if (!disposed) setTransfer(result.transfer);
        })
        .catch(() => {
          if (!disposed) setError("転送状態を確認できませんでした。転送は自動的に失効します。");
        });
    }, 1000);
    return () => {
      disposed = true;
      window.clearInterval(poll);
    };
  }, [transfer]);

  const createTransfer = async () => {
    if (!selectedDevice) return;
    setBusy(true);
    setError("");
    try {
      const result = await mobileApiResources.createCredentialTransfer({
        device_id: selectedDevice.device_id,
        provider_id: providerId,
        api_id: apiId,
        provider_label: displayName,
      });
      setTransfer(result.transfer);
    } catch (value) {
      setError(value instanceof Error ? value.message : "転送要求を作成できませんでした。");
    } finally {
      setBusy(false);
    }
  };

  const confirmTransfer = async () => {
    if (!transfer) return;
    setBusy(true);
    setError("");
    try {
      const result = await mobileApiResources.confirmCredentialTransfer(transfer.transfer_id, {
        device_id: transfer.device_id,
        provider_id: transfer.provider_id,
        api_id: transfer.api_id,
        user_confirmed: true,
      });
      setTransfer(result.transfer);
    } catch (value) {
      setError(value instanceof Error ? value.message : "転送を確認できませんでした。");
    } finally {
      setBusy(false);
    }
  };

  const stopTransfer = async (revoke: boolean) => {
    if (!transfer) return;
    setBusy(true);
    setError("");
    try {
      const result = revoke
        ? await mobileApiResources.revokeCredentialTransfer(transfer.transfer_id)
        : await mobileApiResources.cancelCredentialTransfer(transfer.transfer_id);
      setTransfer(result.transfer);
    } catch (value) {
      setError(value instanceof Error ? value.message : "転送を停止できませんでした。");
    } finally {
      setBusy(false);
    }
  };

  const terminal = transfer ? terminalStates.has(transfer.status) : false;
  const recoveryNeeded = transfer && ["expired", "revoked", "rejected"].includes(transfer.status);

  return (
    <AnimatePresence>
      <motion.div className="fixed inset-0 rumi-layer-modal flex items-center justify-center bg-black/60 p-3 sm:p-4" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
        <motion.div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="credential-transfer-title" aria-describedby="credential-transfer-security-note" className="relative max-h-[calc(100dvh-24px)] w-full max-w-lg overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-950 shadow-2xl" initial={{ scale: 0.97, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: 0.97, opacity: 0 }}>
          <button type="button" aria-label="転送画面を閉じる" onClick={requestClose} disabled={busy} className="absolute right-3 top-3 rounded p-1 text-zinc-500 hover:text-zinc-200 disabled:opacity-40"><X size={18} /></button>
          <div className="p-5 sm:p-6">
            <div className="flex items-start gap-3 pr-8">
              <div className="grid h-10 w-10 shrink-0 place-items-center rounded-full border border-emerald-500/30 bg-emerald-500/10"><ShieldCheck size={20} className="text-emerald-300" /></div>
              <div className="min-w-0">
                <h3 ref={headingRef} tabIndex={-1} id="credential-transfer-title" className="text-base font-semibold text-zinc-100 outline-none">暗号化して端末へ転送</h3>
                <p className="mt-1 text-xs leading-5 text-zinc-500">{displayName} / {apiId} を、確認した1台だけに90秒間送信します。</p>
              </div>
            </div>

            {!transfer ? (
              <div className="mt-5">
                <p className="mb-2 text-xs font-medium text-zinc-300">受信する端末</p>
                {loading ? (
                  <div className="grid h-24 place-items-center text-zinc-500"><Loader2 size={18} className="animate-spin" /></div>
                ) : devices.length === 0 ? (
                  <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-sm text-zinc-400">安全な転送に対応したペア済み端末がありません。端末を再ペアリングして「credential request」を許可してください。</div>
                ) : (
                  <div role="radiogroup" aria-label="受信端末" className="space-y-2">
                    {devices.map((device) => (
                      <button key={device.device_id} role="radio" aria-checked={selectedId === device.device_id} type="button" onClick={() => setSelectedId(device.device_id)} className={cn("flex w-full items-center gap-3 rounded-lg border px-3 py-3 text-left", selectedId === device.device_id ? "border-emerald-500/60 bg-emerald-500/10" : "border-zinc-800 bg-zinc-900/50 hover:border-zinc-700")}>
                        <Smartphone size={16} className="shrink-0 text-zinc-400" />
                        <span className="min-w-0 flex-1"><span className="block truncate text-sm text-zinc-200">{device.label}</span><span className="block truncate text-[11px] text-zinc-500">Profile: {device.profile_id || "default"} · {device.platform || "mobile"}</span></span>
                        <span className={cn("h-4 w-4 rounded-full border", selectedId === device.device_id ? "border-emerald-300 bg-emerald-400" : "border-zinc-600")} />
                      </button>
                    ))}
                  </div>
                )}
                <button type="button" disabled={!selectedDevice || busy} onClick={() => void createTransfer()} className="mt-4 flex w-full items-center justify-center gap-2 rounded-lg border border-emerald-600 bg-emerald-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:border-zinc-800 disabled:bg-zinc-900 disabled:text-zinc-600">{busy ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}転送内容を確認</button>
              </div>
            ) : transfer.status === "awaiting_confirmation" ? (
              <div className="mt-5">
                <div className="space-y-2 rounded-lg border border-zinc-800 bg-zinc-900/50 p-4 text-sm">
                  <InfoRow label="Recipient" value={`${transfer.device_label} / ${transfer.profile_id}`} />
                  <InfoRow label="Provider" value={`${transfer.provider_label} / ${transfer.api_id}`} />
                  <InfoRow label="Expiry" value="90 seconds, one redemption" />
                </div>
                <p className="mt-3 text-xs leading-5 text-amber-100">受信端末とprovider/accountが正しいことを確認してください。確認後にのみ、PC内の保存済みcredentialが端末公開鍵へ暗号化されます。</p>
                <div className="mt-4 flex flex-col-reverse gap-2 sm:flex-row">
                  <button type="button" disabled={busy} onClick={() => void stopTransfer(false)} className="flex-1 rounded-lg border border-zinc-700 px-4 py-2.5 text-sm text-zinc-300 hover:border-zinc-500">キャンセル</button>
                  <button type="button" disabled={busy} onClick={() => void confirmTransfer()} className="flex flex-1 items-center justify-center gap-2 rounded-lg border border-emerald-600 bg-emerald-600 px-4 py-2.5 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50">{busy ? <Loader2 size={15} className="animate-spin" /> : <ShieldCheck size={15} />}この端末へ暗号化して送る</button>
                </div>
              </div>
            ) : (
              <div className="mt-5">
                <TransferState transfer={transfer} />
                {recoveryNeeded && (
                  <ErrorNotice
                    className="mt-3 p-3 text-xs leading-5"
                    copyLabel="転送復旧の注意をコピー"
                    message="意図しない端末や画面共有が疑われる場合は、端末のペアリングを解除し、provider側でAPI keyをローテーションしてください。"
                    severity="warning"
                  />
                )}
                <div className="mt-4 flex gap-2">
                  {!terminal && transfer.status !== "accepted" && <button type="button" disabled={busy} onClick={() => void stopTransfer(false)} className="flex-1 rounded-lg border border-rose-500/40 px-4 py-2.5 text-sm text-rose-200 hover:border-rose-400">転送をキャンセル</button>}
                  <button type="button" onClick={requestClose} className="flex-1 rounded-lg border border-zinc-700 px-4 py-2.5 text-sm text-zinc-200 hover:border-zinc-500">閉じる</button>
                </div>
              </div>
            )}

            {error && (
              <ErrorNotice
                className="mt-3 px-3 py-2 text-xs"
                copyLabel="認証情報転送エラーをコピー"
                message={error}
              />
            )}
            <p id="credential-transfer-security-note" className="mt-4 text-[11px] leading-5 text-zinc-600">画面やURLにはcredentialを表示しません。転送は端末IDと暗号鍵に結び付き、期限切れまたは1回の受領で無効になります。</p>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return <div className="grid grid-cols-[80px_minmax(0,1fr)] gap-3"><span className="text-zinc-500">{label}</span><span className="break-words text-zinc-200">{value}</span></div>;
}

function TransferState({ transfer }: { transfer: CredentialTransfer }) {
  const copy: Record<string, { title: string; body: string; tone: string }> = {
    pending: { title: "受信端末の確認待ち", body: "端末で承認すると、暗号化credentialを1回だけ受け取れます。", tone: "text-sky-200" },
    accepted: { title: "端末へ配信済みです", body: "配信後は取り消せません。端末でsecure storageへの保存を再試行し、完了確認を送信してください。", tone: "text-emerald-200" },
    completed: { title: "安全に転送しました", body: "受信端末がsecure storageへの保存を確認しました。", tone: "text-emerald-200" },
    rejected: { title: "端末が拒否しました", body: "credentialは端末へ渡されていません。", tone: "text-amber-200" },
    expired: { title: "転送は期限切れです", body: "暗号化payloadは破棄され、再利用できません。", tone: "text-amber-200" },
    revoked: { title: "転送を取り消しました", body: "この転送は再利用できません。", tone: "text-rose-200" },
    cancelled: { title: "転送をキャンセルしました", body: "credentialは端末へ渡されていません。", tone: "text-zinc-300" },
  };
  const item = copy[transfer.status] ?? copy.pending;
  return <div aria-live="polite" className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4"><div className="flex items-center gap-2">{["pending", "accepted"].includes(transfer.status) ? <Loader2 size={16} className="animate-spin text-sky-300" /> : <Check size={16} className={item.tone} />}<p className={cn("text-sm font-medium", item.tone)}>{item.title}</p></div><p className="mt-2 text-xs leading-5 text-zinc-500">{item.body}</p></div>;
}
