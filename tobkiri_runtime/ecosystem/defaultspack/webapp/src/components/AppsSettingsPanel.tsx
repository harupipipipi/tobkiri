import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import QRCode from "qrcode";
import {
  Apple,
  AppWindow,
  Check,
  ChevronDown,
  Copy,
  Loader2,
  QrCode,
  RefreshCw,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Trash2,
} from "lucide-react";

import { cn } from "../lib/cn";
import { mobileApiResources } from "../features/mobile/resources/mobileApiResources";
import type { MobileDevice, P2PPairing } from "../features/mobile/resources/mobileApiResources";
import { allowCleartextMobileQr } from "../lib/mobileCleartextQr";
import { buildMobilePairingBaseUrls } from "../lib/mobilePairingUrls";
import { ErrorNotice } from "./ErrorNotice";
import { MobilePairingApproval } from "./MobilePairingApproval";
import {
  LiquidButton,
  LiquidCard,
  LiquidPill,
  SecurityRow,
  SoftCheck,
  StatusDots,
} from "./liquidParts";

type AppsSettingsPanelProps = {
  kernelBaseUrl?: string;
  cloudflarePagesUrl?: string;
};

type MobilePairQrPayload = {
  kind: "rumi_mobile_pair_v1";
  version: 1;
  pairingId: string;
  code: string;
  pickupSecret: string;
  baseUrls: string[];
  manifestUrl: string;
  roles: ("mobile_client" | "mobile_approver")[];
  serverPublicKey: string;
  expiresAt: number;
};

function useQrDataUrl(value: string): { dataUrl: string | null; error: string | null } {
  const [dataUrl, setDataUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (!value.trim()) {
      setDataUrl(null);
      setError(null);
      return;
    }
    QRCode.toDataURL(value, { errorCorrectionLevel: "M", margin: 2, width: 320 })
      .then((url) => {
        if (!cancelled) {
          setDataUrl(url);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setDataUrl(null);
          setError(err instanceof Error ? err.message : "QR生成に失敗しました");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [value]);

  return { dataUrl, error };
}

function windowOrigin() {
  if (typeof window === "undefined") return "";
  return window.location?.origin ?? "";
}

function friendlyScope(scope: string) {
  const labels: Record<string, string> = {
    "chat.read": "PCのチャットを読む",
    "chat.write": "PCへメッセージを送る",
    "tools.observe": "PCの作業状況を見る",
    "authority.request.list": "承認一覧を見る",
    "authority.request.read": "承認内容を見る",
    "authority.request.approve": "PCの承認を許可",
    "authority.request.deny": "PCの拒否を許可",
    "credentials.request": "API設定を受け取る",
  };
  return labels[scope] ?? scope;
}

function formatRelativeTime(value: string | number | undefined): string {
  if (!value) return "不明";
  const date = new Date(value);
  const now = Date.now();
  const diffMs = now - date.getTime();
  if (diffMs < 0) return "たった今";
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 1) return "たった今";
  if (diffMin < 60) return `${diffMin}分前`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}時間前`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}日前`;
}

function CopyField({
  label,
  value,
  placeholder,
  onChange,
  readOnly,
}: {
  label: string;
  value: string;
  placeholder?: string;
  onChange?: (next: string) => void;
  readOnly?: boolean;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <label className="block space-y-2">
      <span className="text-[11px] font-bold uppercase tracking-[0.16em] text-zinc-500">{label}</span>
      <div className="flex items-center gap-2 rounded-2xl border border-white/10 bg-black/20 p-1.5">
        <input
          type="text"
          value={value}
          readOnly={readOnly}
          placeholder={placeholder}
          onChange={(event) => onChange?.(event.target.value)}
          className="min-w-0 flex-1 bg-transparent px-3 py-2 font-mono text-sm text-zinc-100 outline-none placeholder:text-zinc-600"
        />
        <button
          type="button"
          disabled={!value}
          onClick={() => {
            void navigator.clipboard.writeText(value);
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          }}
          className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-white/10 bg-white/5 text-zinc-400 transition hover:bg-white/10 hover:text-white disabled:opacity-30"
          title="コピー"
        >
          {copied ? <Check size={14} className="text-emerald-300" /> : <Copy size={14} />}
        </button>
      </div>
    </label>
  );
}

function PairingProgress({
  pairing,
  isExpired,
  onRestart,
  onOpenApproval,
}: {
  pairing: P2PPairing | null;
  isExpired: boolean;
  onRestart: () => void;
  onOpenApproval: () => void;
}) {
  if (!pairing) {
    return (
      <div className="rounded-3xl border border-white/10 bg-white/[.045] p-4">
        <div className="flex items-center gap-3 text-sm text-zinc-300">
          <QrCode size={16} className="text-cyan-200" />
          QRを作ると、スマホ側に接続の入口が表示されます。
        </div>
      </div>
    );
  }

  if (pairing.status === "approved") {
    return (
      <div className="rumi-success-spark rounded-3xl border border-emerald-300/25 bg-emerald-300/10 p-4">
        <div className="flex items-center gap-3">
          <SoftCheck />
          <div>
            <div className="text-sm font-extrabold text-emerald-50">つながりました</div>
            <p className="mt-1 text-xs leading-5 text-emerald-100/75">
              このスマホからチャットを読んだり送ったりできます。
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (pairing.status === "rejected") {
    return (
      <ErrorNotice
        className="rounded-3xl border-rose-300/25 bg-rose-400/10 p-4 text-xs leading-5 text-rose-100"
        copyLabel="ペアリング拒否の内容をコピー"
        copyText="今回は接続しませんでした\n\n必要になったら、新しいQRを作り直せます。"
        message="必要になったら、新しいQRを作り直せます。"
        messageClassName="mt-1 text-rose-100/70"
        title="今回は接続しませんでした"
        titleClassName="text-sm text-rose-100"
      >
        <LiquidButton type="button" quiet className="mt-3" onClick={onRestart}>
          <RefreshCw size={14} />
          新しいQRを作る
        </LiquidButton>
      </ErrorNotice>
    );
  }

  if (pairing.status === "expired" || isExpired) {
    return (
      <ErrorNotice
        className="rounded-3xl border-amber-300/25 bg-amber-300/10 p-4 text-xs leading-5 text-amber-100"
        copyLabel="QR期限切れの内容をコピー"
        copyText="QRの期限が切れました\n\n安全のため、接続用QRは短時間だけ使えます。"
        message="安全のため、接続用QRは短時間だけ使えます。"
        messageClassName="mt-1 text-amber-100/75"
        severity="warning"
        title="QRの期限が切れました"
        titleClassName="text-sm text-amber-100"
      >
        <LiquidButton type="button" quiet className="mt-3" onClick={onRestart}>
          <RefreshCw size={14} />
          新しいQRを作る
        </LiquidButton>
      </ErrorNotice>
    );
  }

  if (pairing.status === "claimed") {
    return (
      <div className="rounded-3xl border border-cyan-200/25 bg-cyan-200/10 p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <div className="text-sm font-extrabold text-cyan-50">接続要求が届きました</div>
            <p className="mt-1 text-xs leading-5 text-cyan-100/75">
              確認コードとできることを見て、PC側で承認してください。
            </p>
          </div>
          <LiquidButton type="button" className="sm:shrink-0" onClick={onOpenApproval}>
            <ShieldCheck size={14} />
            承認を見る
          </LiquidButton>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-3xl border border-white/10 bg-white/[.045] p-4">
      <div className="flex items-center gap-3 text-sm text-zinc-300">
        <span className="text-cyan-200">
          <StatusDots />
        </span>
        スマホでQRを読み込んでいます
      </div>
    </div>
  );
}

function PairingV2Section({
  kernelBaseUrl,
  onPairingApproved,
}: {
  kernelBaseUrl?: string;
  onPairingApproved: () => void;
}) {
  const [pairing, setPairing] = useState<P2PPairing | null>(null);
  const [manualBaseUrl, setManualBaseUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showApproval, setShowApproval] = useState(false);
  const mountedRef = useRef(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopPolling();
    };
  }, [stopPolling]);

  const startPairing = useCallback(async () => {
    setBusy(true);
    setError("");
    setPairing(null);
    setManualBaseUrl("");
    setShowApproval(false);
    try {
      const result = await mobileApiResources.startPairing({
        capabilities: ["chat.read", "chat.write", "tools.observe"],
      });
      if (!mountedRef.current) return;
      setPairing(result.pairing);
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : "ペアリングの開始に失敗しました");
      }
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    if (!pairing || pairing.status !== "pending") {
      stopPolling();
      return;
    }

    let disposed = false;
    const poll = async () => {
      try {
        const status = await mobileApiResources.getPairingStatus(pairing.pairing_id);
        if (disposed || !mountedRef.current) return;
        if (status.status === "claimed") {
          setPairing((prev) => (prev ? { ...prev, status: "claimed" } : prev));
          setShowApproval(true);
          stopPolling();
        } else if (status.status === "approved" || status.status === "rejected" || status.status === "expired") {
          stopPolling();
          setPairing((prev) => (prev ? { ...prev, status: status.status } : prev));
          if (status.status === "approved") onPairingApproved();
        }
      } catch {
        // ignore transient poll errors
      }
    };

    void poll();
    pollRef.current = setInterval(() => void poll(), 2000);
    return () => {
      disposed = true;
      stopPolling();
    };
  }, [onPairingApproved, pairing, stopPolling]);

  const advertisedBaseUrls = useMemo(
    () => pairing?.base_urls?.filter((value): value is string => typeof value === "string") ?? [],
    [pairing?.base_urls],
  );
  const currentOrigin = windowOrigin();
  const qrBaseUrls = useMemo(
    () => buildMobilePairingBaseUrls(
      [manualBaseUrl, ...advertisedBaseUrls, kernelBaseUrl, currentOrigin],
      { allowCleartext: allowCleartextMobileQr() },
    ),
    [advertisedBaseUrls, currentOrigin, kernelBaseUrl, manualBaseUrl],
  );

  const qrPayload: MobilePairQrPayload | null = pairing && qrBaseUrls.length > 0 ? {
    kind: "rumi_mobile_pair_v1",
    version: 1,
    pairingId: pairing.pairing_id,
    code: pairing.code,
    pickupSecret: pairing.pickup_secret ?? "",
    baseUrls: qrBaseUrls,
    manifestUrl: `${qrBaseUrls[0].replace(/\/+$/, "")}/api/mobile/v1/manifest`,
    roles: ["mobile_client", "mobile_approver"],
    serverPublicKey: "",
    expiresAt: pairing.expires_at,
  } : null;

  const qr = useQrDataUrl(qrPayload ? JSON.stringify(qrPayload) : "");
  const isExpired = pairing ? pairing.expires_at < Date.now() : false;
  const isFinished = pairing?.status === "approved" || pairing?.status === "rejected" || pairing?.status === "expired" || isExpired;
  const hasActivePairing = Boolean(pairing && !isFinished);

  return (
    <LiquidCard className="p-5">
      <div className="flex flex-col gap-5 lg:flex-row lg:items-start">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <LiquidPill tone="cyan">QRでかんたん</LiquidPill>
            <LiquidPill tone="mint">暗号化 pickup</LiquidPill>
            <LiquidPill tone="violet">最小権限</LiquidPill>
          </div>

          <h4 className="mt-4 text-2xl font-black tracking-tight text-white">スマホをつなぐ</h4>
          <p className="mt-2 max-w-xl text-sm leading-6 text-zinc-300">
            スマホのRumiでQRを読み込むと、接続リクエストがこのPCに届きます。
            確認コードが一致したら、PC側で承認してください。
          </p>

          <div className="mt-5">
            <PairingProgress
              pairing={pairing}
              isExpired={isExpired}
              onRestart={() => void startPairing()}
              onOpenApproval={() => setShowApproval(true)}
            />
          </div>

          {!hasActivePairing && pairing?.status !== "approved" && (
            <LiquidButton
              type="button"
              disabled={busy}
              busy={busy}
              onClick={() => void startPairing()}
              className="mt-5"
            >
              <Smartphone size={16} />
              {busy ? "接続用のQRを準備しています" : "スマホをつなぐ"}
            </LiquidButton>
          )}

          {hasActivePairing && (
            <div className="mt-5 space-y-4">
              <CopyField
                label="PC HTTPS URL"
                value={manualBaseUrl || qrBaseUrls[0] || ""}
                onChange={setManualBaseUrl}
                placeholder="https://your-rumi.example.com"
              />

              {qrBaseUrls.length === 0 && (
                <div className="rounded-2xl border border-amber-300/25 bg-amber-300/10 px-4 py-3 text-xs leading-5 text-amber-100">
                  release版AndroidではHTTPS URLが必要です。Cloudflare Tunnel/PagesなどでPCへ届くURLを入力してください。
                </div>
              )}

              <div className="grid gap-3 sm:grid-cols-3">
                {["QRを読む", "合言葉を確認", "承認して完了"].map((step, index) => (
                  <div key={step} className="rounded-2xl border border-white/10 bg-white/[.045] p-3">
                    <div className="text-[11px] uppercase text-zinc-500">Step {index + 1}</div>
                    <div className="mt-1 text-sm font-bold text-zinc-100">{step}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {pairing?.status === "approved" && (
            <div className="mt-4 space-y-2">
              <SecurityRow>APIキー転送やPC承認操作は、この接続には含まれていません。</SecurityRow>
              <SecurityRow>必要になったら上の設定から端末を外せます。</SecurityRow>
            </div>
          )}

          {error && (
            <ErrorNotice
              className="mt-4 rounded-2xl border-rose-300/25 bg-rose-400/10 px-4 py-3 text-xs text-rose-100"
              copyLabel="ペアリングエラーをコピー"
              message={error}
            />
          )}
        </div>

        <div className="w-full shrink-0 lg:w-72">
          <div className="rumi-qr-stage p-5">
            {hasActivePairing && qr.dataUrl ? (
              <img src={qr.dataUrl} alt="ペアリングQR" className="relative h-56 w-56 rounded-3xl bg-white p-3" />
            ) : (
              <div className="relative grid h-56 w-56 place-items-center rounded-3xl border border-white/10 bg-black/20 text-center text-xs text-zinc-500">
                <div>
                  <QrCode className="mx-auto mb-3 text-zinc-600" />
                  {busy ? (
                    "QRを準備しています"
                  ) : (
                    qr.error ? "QRを生成できませんでした" : "スマホをつなぐとQRが出ます"
                  )}
                </div>
              </div>
            )}
          </div>
          {qr.error && (
            <ErrorNotice
              className="mt-3 px-3 py-2 text-[11px]"
              copyLabel="QR生成エラーをコピー"
              message={qr.error}
            />
          )}

          {hasActivePairing && (
            <div className="mt-3 text-center">
              <div className="text-[11px] uppercase tracking-[0.18em] text-cyan-200/80">確認コード</div>
              <div className="mt-2 inline-flex rounded-2xl border border-cyan-200/20 bg-cyan-200/10 px-5 py-2 text-lg font-black tracking-wider text-cyan-100">
                {pairing?.code}
              </div>
              <div className="mt-2 text-xs text-zinc-500">スマホ側の表示と照合します</div>
            </div>
          )}
        </div>
      </div>

      {pairing && showApproval && (
        <MobilePairingApproval
          pairingId={pairing.pairing_id}
          onClose={(outcome) => {
            setShowApproval(false);
            if (outcome === "keep-pending") return;
            setPairing((prev) => (prev ? { ...prev, status: outcome } : prev));
            if (outcome === "approved") onPairingApproved();
          }}
        />
      )}
    </LiquidCard>
  );
}

function DeviceManagementSection({ refreshKey }: { refreshKey: number }) {
  const [devices, setDevices] = useState<MobileDevice[]>([]);
  const [loading, setLoading] = useState(true);
  const [revoking, setRevoking] = useState<string>("");
  const mountedRef = useRef(true);

  const loadDevices = useCallback(async () => {
    setLoading(true);
    try {
      const result = await mobileApiResources.listDevices();
      if (mountedRef.current) setDevices(result.devices ?? []);
    } catch {
      // silent
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void loadDevices();
    return () => {
      mountedRef.current = false;
    };
  }, [loadDevices, refreshKey]);

  const handleRevoke = async (deviceId: string) => {
    setRevoking(deviceId);
    try {
      await mobileApiResources.revokeDevice(deviceId);
      if (mountedRef.current) {
        setDevices((prev) => prev.filter((device) => device.device_id !== deviceId));
      }
    } catch {
      // silent
    } finally {
      if (mountedRef.current) setRevoking("");
    }
  };

  return (
    <LiquidCard className="p-5">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h4 className="text-lg font-black text-white">つながっているスマホ</h4>
          <p className="mt-1 text-sm text-zinc-400">ペア済み端末を管理します。</p>
        </div>
        <ShieldCheck className="text-emerald-200" size={20} />
      </div>

      {loading ? (
        <div className="mt-4 flex items-center gap-2 text-sm text-zinc-400">
          <Loader2 size={16} className="animate-spin" />
          端末を確認しています
        </div>
      ) : devices.length === 0 ? (
        <div className="mt-4 rounded-3xl border border-dashed border-white/[.12] bg-white/[.03] p-6 text-center">
          <div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-white/[.08] text-zinc-400">
            <Smartphone size={22} />
          </div>
          <div className="mt-3 text-sm font-bold text-zinc-200">まだ端末はありません</div>
          <div className="mt-1 text-xs text-zinc-500">上のQRから最初のスマホをつなげます。</div>
        </div>
      ) : (
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          {devices.map((device) => (
            <div key={device.device_id} className="rumi-device-bubble p-4">
              <div className="flex items-start gap-3">
                <div className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl border border-white/10 bg-white/10 text-cyan-100">
                  <Smartphone size={20} />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm font-extrabold text-white">{device.label || "Rumi Mobile"}</div>
                  <div className="mt-0.5 text-[11px] text-zinc-500">
                    {device.last_seen_at ? `最終接続: ${formatRelativeTime(device.last_seen_at)}` : "接続待ち"}
                  </div>
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {(device.scopes ?? []).slice(0, 3).map((scope) => (
                      <span key={scope} className="rounded-full bg-white/[.07] px-2 py-1 text-[10px] text-zinc-300">
                        {friendlyScope(scope)}
                      </span>
                    ))}
                  </div>
                </div>
                <button
                  type="button"
                  disabled={revoking === device.device_id}
                  onClick={() => void handleRevoke(device.device_id)}
                  className="rounded-xl border border-white/10 p-2 text-zinc-500 transition hover:border-rose-300/30 hover:bg-rose-400/10 hover:text-rose-100 disabled:opacity-40"
                  title="この端末を外す"
                >
                  {revoking === device.device_id ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </LiquidCard>
  );
}

function PagesQrSection({ cloudflarePagesUrl }: { cloudflarePagesUrl?: string }) {
  const [pagesUrl, setPagesUrl] = useState(cloudflarePagesUrl ?? "");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setPagesUrl(cloudflarePagesUrl ?? "");
  }, [cloudflarePagesUrl]);

  const pagesQr = useQrDataUrl(pagesUrl.trim());

  return (
    <details
      className="rumi-glass-card p-4"
      open={open}
      onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-bold text-zinc-300">
        <ChevronDown size={14} className={cn("transition-transform", open && "rotate-180")} />
        HTTPS接続の準備
      </summary>
      {open && (
        <div className="mt-4 grid gap-4 lg:grid-cols-[1fr_220px]">
          <div>
            <p className="text-sm leading-6 text-zinc-400">
              release版AndroidではHTTPSが必要です。Cloudflare Tunnel/PagesなどでPCへ届くURLを用意すると、スマホから安定して接続できます。
            </p>
            <div className="mt-4">
              <CopyField
                label="Cloudflare / HTTPS URL"
                value={pagesUrl}
                onChange={setPagesUrl}
                placeholder="https://rumi-mobile.pages.dev"
              />
            </div>
          </div>
          <div className="rumi-qr-stage p-4">
            {pagesQr.dataUrl ? (
              <img src={pagesQr.dataUrl} alt="Cloudflare Pages QR" className="relative h-40 w-40 rounded-2xl bg-white p-2" />
            ) : (
              <div className="relative text-center text-xs text-zinc-500">URLを入れるとQRが出ます</div>
            )}
          </div>
        </div>
      )}
    </details>
  );
}

function AppAvailabilityCard() {
  return (
    <LiquidCard className="p-5">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <div className="grid h-11 w-11 place-items-center rounded-2xl border border-white/10 bg-white/10 text-zinc-200">
            <Apple size={20} />
          </div>
          <div className="min-w-0 flex-1">
            <h4 className="text-lg font-black text-white">アプリを入手</h4>
            <p className="mt-1 text-sm text-zinc-400">現在は配信準備中です。公開後はここから開けます。</p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <span className="inline-flex items-center gap-2 rounded-full border border-amber-300/25 bg-amber-300/10 px-3 py-1 text-[11px] font-bold text-amber-100">
            <AppWindow size={12} />
            App Store準備中
          </span>
          <span className="inline-flex items-center gap-2 rounded-full border border-cyan-200/25 bg-cyan-200/10 px-3 py-1 text-[11px] font-bold text-cyan-100">
            <Apple size={12} />
            TestFlight準備中
          </span>
        </div>
      </div>
    </LiquidCard>
  );
}

export function AppsSettingsPanel({ kernelBaseUrl, cloudflarePagesUrl }: AppsSettingsPanelProps) {
  const [deviceRefreshKey, setDeviceRefreshKey] = useState(0);
  const refreshDevices = useCallback(() => setDeviceRefreshKey((value) => value + 1), []);

  return (
    <section className="rumi-liquid-shell space-y-5 p-5 sm:p-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/10 px-3 py-1 text-[11px] text-zinc-200">
            <Sparkles size={12} className="text-cyan-200" />
            Rumi Mobile
          </div>
          <h3 className="mt-4 text-3xl font-black tracking-tight text-white">Rumi Mobile</h3>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-zinc-300">
            スマホを近づけるように、PC の Rumi と安全につなぎます。
            QRを読み込むだけ。端末トークンはスマホの鍵で包んで受け渡します。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <LiquidPill tone="mint">HTTPS 推奨</LiquidPill>
          <LiquidPill tone="cyan">暗号化 pickup</LiquidPill>
          <LiquidPill tone="violet">最小権限</LiquidPill>
        </div>
      </div>

      <PairingV2Section kernelBaseUrl={kernelBaseUrl} onPairingApproved={refreshDevices} />

      <DeviceManagementSection refreshKey={deviceRefreshKey} />

      <PagesQrSection cloudflarePagesUrl={cloudflarePagesUrl} />

      <AppAvailabilityCard />
    </section>
  );
}
