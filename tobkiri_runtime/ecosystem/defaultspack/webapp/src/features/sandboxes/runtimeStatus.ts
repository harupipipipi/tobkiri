import { normalizeRuntimeStatus } from "./types";
import type {
  RuntimeDoctorIssue,
  RuntimeDoctorResult,
  RuntimeProviderStatus,
  RuntimeProvidersResponse,
  RuntimeOperationSupport,
} from "./types";

const DESKTOP_RUNTIME_CAPABILITIES = [
  "sandbox.desktop",
  "sandbox.desktop_input",
  "sandbox.snapshot",
] as const;

const DIAGNOSTIC_SECRET_KEY_PATTERN =
  /(api[_-]?key|authorization|bearer|credential|password|private[_-]?key|secret|token)/i;

export type RuntimeAvailability =
  | {
      status: "ready";
      selectedProvider: RuntimeProviderStatus | null;
      providers: RuntimeProviderStatus[];
      missing: RuntimeDoctorIssue[];
      message: string;
    }
  | {
      status: "needs_setup" | "checking" | "unavailable" | "error" | "registered";
      selectedProvider: RuntimeProviderStatus | null;
      providers: RuntimeProviderStatus[];
      missing: RuntimeDoctorIssue[];
      message: string;
    };

function providerIsReady(provider: RuntimeProviderStatus): boolean {
  return provider.ready === true && provider.host_platform_supported !== false && provider.diagnostics?.probe_status !== "not_run";
}

function providerCapabilities(provider: RuntimeProviderStatus): Set<string> {
  const capabilities = provider.capabilities ?? [];
  if (typeof capabilities === "string") {
    return new Set(capabilities.split(/\s+/).map((capability) => capability.trim()).filter(Boolean));
  }
  return new Set(capabilities);
}

export function providerSupportsDesktop(
  provider: RuntimeProviderStatus | null | undefined,
): boolean {
  if (!provider) return false;
  const capabilities = providerCapabilities(provider);
  return DESKTOP_RUNTIME_CAPABILITIES.every((capability) =>
    capabilities.has(capability),
  );
}

export function providerIsDesktopReady(
  provider: RuntimeProviderStatus | null | undefined,
): boolean {
  return Boolean(
    provider && providerIsReady(provider) && providerSupportsDesktop(provider),
  );
}

function providerNeedsSetup(provider: RuntimeProviderStatus): boolean {
  const status = normalizeRuntimeStatus(provider.status);
  return (
    status === "needs_setup" || status === "installing" || status === "updating"
  );
}

export function providerLabel(
  provider: RuntimeProviderStatus | null | undefined,
): string {
  if (!provider) return "Auto";
  return provider.label || provider.provider_id.replace(/_/g, " ");
}

export function providerStatusTone(
  provider: RuntimeProviderStatus,
): "success" | "warning" | "danger" | "idle" {
  const status = normalizeRuntimeStatus(provider.status);
  if (providerIsDesktopReady(provider)) return "success";
  if (providerIsReady(provider) && !providerSupportsDesktop(provider))
    return "idle";
  if (providerNeedsSetup(provider)) return "warning";
  if (status === "error" || status === "failed" || status === "unavailable")
    return "danger";
  return "idle";
}

export function runtimeAvailability(
  providersResponse: RuntimeProvidersResponse | null,
  doctor: RuntimeDoctorResult | null,
  error: string | null,
  checking = false,
): RuntimeAvailability {
  const providers = doctor?.providers ?? providersResponse?.providers ?? [];
  const selectedProviderId =
    doctor?.selected_provider_id ??
    providersResponse?.selected_provider_id ??
    providersResponse?.default_provider_id ??
    null;
  const preferredProvider =
    providers.find((provider) => provider.provider_id === selectedProviderId) ??
    providers.find((provider) => provider.selected) ??
    providers[0] ??
    null;
  const readyProvider =
    providers.find(
      (provider) =>
        provider.provider_id === selectedProviderId &&
        providerIsDesktopReady(provider),
    ) ??
    providers.find(
      (provider) => provider.selected && providerIsDesktopReady(provider),
    ) ??
    providers.find(providerIsDesktopReady) ??
    null;
  const selectedProvider = readyProvider ?? preferredProvider;
  const missing = [
    ...(doctor?.missing ?? []),
    ...providers.flatMap((provider) => provider.missing ?? []),
  ];

  if (checking && !doctor && !providersResponse && !error) {
    return {
      status: "checking",
      selectedProvider,
      providers,
      missing,
      message: "Checking runtime providers.",
    };
  }

  if (error && providers.length === 0 && !doctor) {
    return {
      status: "unavailable",
      selectedProvider,
      providers,
      missing: [
        {
          code: "runtime_api_unavailable",
          severity: "error",
          message:
            "The runtime API is not available from this defaultspack backend.",
          detail: error,
        },
      ],
      message: "Runtime provider API is unavailable.",
    };
  }

  if (doctor?.diagnostics?.probe_status === "not_run"
      || providersResponse?.diagnostics?.probe_status === "not_run"
      || (providers.length > 0 && providers.every((provider) => provider.diagnostics?.probe_status === "not_run"))) {
    return { status: error ? "error" : "registered", selectedProvider: preferredProvider, providers, missing,
      message: error ? `登録情報は表示できますが、診断情報の取得に失敗しました。${error}` : "実行環境は登録済みですが、ゲストの動作は未診断です。" };
  }

  if (
    providers.some(providerIsDesktopReady) ||
    (providers.length === 0 &&
      normalizeRuntimeStatus(doctor?.status) === "ready")
  ) {
    const provider = readyProvider ?? selectedProvider;
    return {
      status: "ready",
      selectedProvider: provider,
      providers,
      missing,
      message: provider
        ? `${providerLabel(provider)} is ready.`
        : "Runtime provider is ready.",
    };
  }

  if (
    providers.some(providerNeedsSetup) ||
    normalizeRuntimeStatus(doctor?.status) === "needs_setup"
  ) {
    return {
      status: "needs_setup",
      selectedProvider,
      providers,
      missing,
      message:
        doctor?.message ||
        selectedProvider?.message ||
        "Selected runtime provider needs setup before desktops can start.",
    };
  }

  if (normalizeRuntimeStatus(doctor?.status) === "error" || error) {
    return {
      status: "error",
      selectedProvider,
      providers,
      missing,
      message:
        doctor?.message || error || "Runtime provider reported an error.",
    };
  }

  return {
    status: "unavailable",
    selectedProvider,
    providers,
    missing,
    message:
      doctor?.message ||
      selectedProvider?.message ||
      "No desktop-capable runtime provider is available.",
  };
}

export function diagnosticsText(value: {
  providersResponse?: RuntimeProvidersResponse | null;
  doctor?: RuntimeDoctorResult | null;
  error?: string | null;
}): string {
  return JSON.stringify(
    {
      runtime_providers: value.providersResponse,
      runtime_doctor: value.doctor,
      error: value.error ?? undefined,
    },
    (_key, diagnosticValue) => redactDiagnosticsValue(diagnosticValue),
    2,
  );
}

export function redactDiagnosticsValue(value: unknown): unknown {
  return redactDiagnosticsValueInner(value, new WeakSet<object>(), "");
}

function redactDiagnosticsValueInner(
  value: unknown,
  seen: WeakSet<object>,
  key: string,
): unknown {
  if (key && DIAGNOSTIC_SECRET_KEY_PATTERN.test(key)) {
    return "[redacted]";
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  if (seen.has(value)) {
    return "[circular]";
  }
  seen.add(value);
  if (Array.isArray(value)) {
    return value.map((item) => redactDiagnosticsValueInner(item, seen, ""));
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([entryKey, item]) => [
      entryKey,
      redactDiagnosticsValueInner(item, seen, entryKey),
    ]),
  );
}

/** Explicit host support overrides legacy responses without support metadata. */
export function runtimeOperationAllowed(support: RuntimeOperationSupport | undefined, operation: keyof RuntimeOperationSupport): boolean {
  return support?.[operation] !== false;
}

export const DESKTOP_UNSUPPORTED_REASON = "この画面ではゲストの診断・作成・起動・操作がまだ接続されていません。一覧を更新して接続状況を確認してください。";

export async function runSupportedRuntimeOperation<T>(support: RuntimeOperationSupport | undefined, operation: keyof RuntimeOperationSupport, request: () => Promise<T>): Promise<T> {
  if (!runtimeOperationAllowed(support, operation)) throw new Error(DESKTOP_UNSUPPORTED_REASON);
  return request();
}

export function providerReadinessLabel(provider: RuntimeProviderStatus): string {
  if (provider.host_platform_supported === false) return "このOS対象外";
  if (!providerSupportsDesktop(provider)) return "デスクトップ非対応";
  if (provider.diagnostics?.probe_status === "not_run") return "登録済み・未診断";
  return provider.status;
}

/** Unknown metadata cannot authorize requests against saved desktop seats. */
export function runtimeOperationSupportForMetadata(providers: RuntimeProvidersResponse | null, doctor: RuntimeDoctorResult | null): RuntimeOperationSupport {
  if (!providers && !doctor) return {
    create: false, setup: false, lifecycle: false, delete: false,
    access: false, control: false, frame: false, doctor: false,
  };
  return { ...providers?.operation_support, ...doctor?.operation_support };
}
