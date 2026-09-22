import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";

import type {
  FrontendCapabilityClient,
  FrontendCatalog,
  VerifiedFrontendContribution,
} from "./frontendContracts";
import {
  ConversationV4View,
  frontendActionErrorMessage,
  isConversationV4Contribution,
} from "./ConversationV4View";

export { frontendActionErrorMessage } from "./ConversationV4View";

const quarantined = new Set<string>();

export const ISOLATED_FRONTEND_SANDBOX = "allow-scripts";
// Sandboxed documents have an opaque origin, so a specific target origin
// cannot address them. Responses are still bound to the frame WindowProxy and
// session nonce before this value is used.
export const ISOLATED_FRAME_RESPONSE_TARGET_ORIGIN = "*";

export const frontendContributionRevisionKey = (
  item: VerifiedFrontendContribution,
) => JSON.stringify([
  item.resolved_plan_hash,
  item.owner_pack_id,
  item.contribution_id,
  item.descriptor_hash,
  item.module?.content_hash ?? "",
]);

const quarantineKey = frontendContributionRevisionKey;

export const resetFrontendHostQuarantineForTests = () => quarantined.clear();

export function quarantineFrontendContribution(
  item: VerifiedFrontendContribution,
): void {
  quarantined.add(quarantineKey(item));
}

export function synchronizeFrontendHostQuarantine(
  catalog: FrontendCatalog,
): void {
  const activeKeys = new Set(catalog.contributions.map(quarantineKey));
  for (const key of quarantined) {
    if (!activeKeys.has(key)) quarantined.delete(key);
  }
}

export function contributionsForRoute(
  catalog: FrontendCatalog,
  route: string,
  activePlanHash: string,
): VerifiedFrontendContribution[] {
  if (catalog.plan_hash !== activePlanHash) return [];
  return catalog.contributions.filter((item) => (
    item.kind === "route"
    && item.route === route
    && item.resolved_plan_hash === activePlanHash
    && !catalog.quarantined_pack_ids.includes(item.owner_pack_id)
    && !quarantined.has(quarantineKey(item))
  ));
}

export function DynamicFrontendHost({
  catalog,
  route,
  activePlanHash,
  capabilities,
}: {
  catalog: FrontendCatalog;
  route: string;
  activePlanHash: string;
  capabilities: FrontendCapabilityClient;
}) {
  useEffect(() => {
    synchronizeFrontendHostQuarantine(catalog);
  }, [catalog]);
  const contributions = useMemo(
    () => contributionsForRoute(catalog, route, activePlanHash),
    [activePlanHash, catalog, route],
  );
  if (catalog.plan_hash !== activePlanHash) {
    return <HostFallback title="UI revision changed" />;
  }
  if (contributions.length === 0) {
    return <HostFallback title="This feature is not available in the current profile" />;
  }
  return (
    <div data-rumi-frontend-host data-plan-hash={activePlanHash}>
      {contributions.map((item) => (
        <ContributionBoundary
          key={quarantineKey(item)}
          fallback={<HostFallback title={`${item.label} is unavailable`} />}
          onError={() => quarantineFrontendContribution(item)}
        >
          <ContributionView
            item={item}
            profileId={catalog.profile_id}
            catalogHash={catalog.catalog_hash}
            capabilities={capabilities}
          />
        </ContributionBoundary>
      ))}
    </div>
  );
}

function ContributionView({
  item,
  profileId,
  catalogHash,
  capabilities,
}: {
  item: VerifiedFrontendContribution;
  profileId: string;
  catalogHash: string;
  capabilities: FrontendCapabilityClient;
}) {
  if (isConversationV4Contribution(item)) {
    return (
      <ConversationV4View
        item={item}
        catalogHash={catalogHash}
        capabilities={capabilities}
      />
    );
  }
  if (item.mode === "declarative") {
    return (
      <DeclarativeView
        item={item}
        catalogHash={catalogHash}
        capabilities={capabilities}
      />
    );
  }
  if (item.mode === "isolated") {
    return (
      <IsolatedView
        item={item}
        profileId={profileId}
        catalogHash={catalogHash}
        capabilities={capabilities}
      />
    );
  }
  return <BuiltinModuleView item={item} />;
}

function DeclarativeView({
  item,
  catalogHash,
  capabilities,
}: {
  item: VerifiedFrontendContribution;
  catalogHash: string;
  capabilities: FrontendCapabilityClient;
}) {
  const view = item.view ?? {};
  const title = String(view.title ?? item.label);
  const body = String(view.body ?? item.description ?? "");
  const [result, setResult] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const invoke = async () => {
    if (!item.action_contract || busy) return;
    setBusy(true);
    setActionError(null);
    try {
      setResult(await capabilities.invokeAction({
        contractId: item.action_contract,
        payload: {
          operation: String(view.operation ?? "invoke"),
          input: {},
        },
        contributionId: item.contribution_id,
        ownerPackId: item.owner_pack_id,
        planHash: item.resolved_plan_hash,
        catalogHash,
      }));
    } catch (error) {
      setActionError(frontendActionErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section
      aria-label={item.accessibility.name}
      aria-live={item.accessibility.live === "off" ? undefined : item.accessibility.live}
      data-contribution-id={item.contribution_id}
    >
      <h2>{title}</h2>
      {body && <p>{body}</p>}
      {item.action_contract && (
        <button type="button" disabled={busy} onClick={() => void invoke()}>
          {busy ? "Working…" : String(view.action_label ?? "Continue")}
        </button>
      )}
      {actionError && <p role="alert">{actionError}</p>}
      {result !== null && <GenericValue value={result} />}
    </section>
  );
}

type IsolatedCapabilityRequest = {
  requestId: string;
  nonce: string;
  contractId: string;
  payload: {
    operation: string;
    input: Record<string, unknown>;
  };
};

function IsolatedView({
  item,
  profileId,
  catalogHash,
  capabilities,
}: {
  item: VerifiedFrontendContribution;
  profileId: string;
  catalogHash: string;
  capabilities: FrontendCapabilityClient;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [frameError, setFrameError] = useState<string | null>(null);
  const [frameLoadError, setFrameLoadError] = useState(false);
  const nonce = useMemo(
    () => isolatedFrontendNonce(),
    [frontendContributionRevisionKey(item)],
  );
  const src = useMemo(
    () => isolatedFrontendFrameUrl(item, profileId, nonce),
    [item, nonce, profileId],
  );

  useEffect(() => {
    if (!nonce || !src || !item.isolated) return undefined;
    const allowedContracts = new Set(item.isolated.rpc_contracts);
    const handleMessage = (event: MessageEvent<unknown>) => {
      const frame = frameRef.current?.contentWindow;
      if (!frame || event.source !== frame) return;
      if (event.origin !== "null" && event.origin !== window.location.origin) {
        return;
      }
      const request = parseIsolatedCapabilityRequest(event.data);
      if (
        !request
        || request.nonce !== nonce
        || !allowedContracts.has(request.contractId)
      ) {
        return;
      }
      const respond = (response: Record<string, unknown>) => {
        if (frameRef.current?.contentWindow !== frame) return;
        // The sandbox deliberately gives the frame an opaque origin, for
        // which postMessage requires "*". The exact WindowProxy, one-time
        // frame nonce, and declared contract check above bind this response.
        frame.postMessage(response, ISOLATED_FRAME_RESPONSE_TARGET_ORIGIN);
      };
      const invoke = request.contractId.startsWith("rumi.action.")
        ? capabilities.invokeAction
        : capabilities.readDataSource;
      void invoke({
        contractId: request.contractId,
        payload: request.payload,
        contributionId: item.contribution_id,
        ownerPackId: item.owner_pack_id,
        planHash: item.resolved_plan_hash,
        catalogHash,
      }).then(
        (value) => respond({
          type: "rumi.capability.response",
          requestId: request.requestId,
          nonce,
          ok: true,
          value,
        }),
        (error) => {
          const message = frontendActionErrorMessage(error);
          setFrameError(message);
          respond({
            type: "rumi.capability.response",
            requestId: request.requestId,
            nonce,
            ok: false,
            error: message,
          });
        },
      );
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [capabilities, catalogHash, item, nonce, src]);

  if (!src) {
    return <HostFallback title={`${item.label} requires a dedicated isolated origin`} />;
  }
  if (frameLoadError) {
    return <HostFallback title={`${item.label} is unavailable`} />;
  }
  return (
    <section
      aria-label={item.accessibility.name}
      data-contribution-id={item.contribution_id}
    >
      {frameError && <p role="alert">{frameError}</p>}
      <iframe
        ref={frameRef}
        title={item.accessibility.name}
        src={src}
        sandbox={ISOLATED_FRONTEND_SANDBOX}
        referrerPolicy="no-referrer"
        onError={() => {
          quarantineFrontendContribution(item);
          setFrameLoadError(true);
        }}
      />
    </section>
  );
}

export function isolatedFrontendFrameUrl(
  item: VerifiedFrontendContribution,
  profileId: string,
  nonce: string,
  origin = typeof window === "undefined"
    ? "http://localhost"
    : window.location.origin,
): string | null {
  if (!item.isolated || !nonce) return null;
  try {
    const expectedOrigin = new URL(origin).origin;
    const url = new URL(item.isolated.path, expectedOrigin);
    const expectedPrefix = `/isolated/packs/${item.owner_pack_id}/`;
    if (
      url.origin !== expectedOrigin
      || !url.pathname.startsWith(expectedPrefix)
      || url.search
      || url.hash
    ) {
      return null;
    }
    url.searchParams.set("profile_id", profileId);
    url.hash = new URLSearchParams({ rumi_rpc_nonce: nonce }).toString();
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

export function parseIsolatedCapabilityRequest(
  value: unknown,
): IsolatedCapabilityRequest | null {
  if (!isRecord(value) || value.type !== "rumi.capability.request") return null;
  const requestId = boundedString(value.requestId, 128);
  const nonce = boundedString(value.nonce, 256);
  const contractId = boundedString(value.contractId, 256);
  const payload = isRecord(value.payload) ? value.payload : null;
  const operation = payload ? boundedString(payload.operation, 128) : null;
  const input = payload && isRecord(payload.input) ? payload.input : null;
  if (!requestId || !nonce || !contractId || !operation || !input) return null;
  return {
    requestId,
    nonce,
    contractId,
    payload: { operation, input },
  };
}

function isolatedFrontendNonce(): string {
  if (typeof crypto === "undefined" || !crypto.randomUUID) return "";
  return crypto.randomUUID();
}

function boundedString(value: unknown, maxLength: number): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized && normalized.length <= maxLength ? normalized : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function BuiltinModuleView({ item }: { item: VerifiedFrontendContribution }) {
  const module = item.module;
  if (!module || !isBackendVerifiedBuiltinModule(item)) {
    return <HostFallback title={`${item.label} is unavailable`} />;
  }
  return <VerifiedBuiltinModule item={item} module={module} />;
}

function VerifiedBuiltinModule({
  item,
  module,
}: {
  item: VerifiedFrontendContribution;
  module: NonNullable<VerifiedFrontendContribution["module"]>;
}) {
  const Loaded = useMemo(() => lazy(async () => {
    try {
      const loaded = await import(/* @vite-ignore */ module.path) as Record<string, unknown>;
      const exported = loaded[module.export];
      if (typeof exported !== "function") throw new Error("declared export is missing");
      return { default: exported as ComponentType };
    } catch (error) {
      quarantineFrontendContribution(item);
      throw error;
    }
  }), [item, module.export, module.path]);
  return (
    <Suspense fallback={<HostFallback title={`Loading ${item.label}`} />}>
      <Loaded />
    </Suspense>
  );
}

export function isBackendVerifiedBuiltinModule(item: VerifiedFrontendContribution): boolean {
  if (item.mode !== "same_origin_builtin" || !item.module) return false;
  if (typeof window === "undefined") return false;
  if (!item.build_identity || !item.owner_pack_hash.startsWith("sha256:")) return false;
  if (!item.descriptor_hash.startsWith("sha256:")) return false;
  try {
    const url = new URL(item.module.path, window.location.origin);
    return url.origin === window.location.origin
      && url.pathname.startsWith(`/static/packs/${item.owner_pack_id}/`)
      && url.pathname.endsWith(".js")
      && !url.search
      && !url.hash
      && !url.username
      && !url.password;
  } catch {
    return false;
  }
}

function GenericValue({ value }: { value: unknown }) {
  if (typeof value === "string" || typeof value === "number") {
    return <p>{String(value)}</p>;
  }
  return <pre>{JSON.stringify(value, null, 2)}</pre>;
}

function HostFallback({ title }: { title: string }) {
  return <section role="status" aria-live="polite">{title}</section>;
}

class ContributionBoundary extends Component<{
  children: ReactNode;
  fallback: ReactNode;
  onError: () => void;
}, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    this.props.onError();
  }

  render() {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}
