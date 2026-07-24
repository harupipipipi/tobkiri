import {
  Component,
  Suspense,
  lazy,
  useMemo,
  useState,
  type ComponentType,
  type ReactNode,
} from "react";

import type {
  FrontendCapabilityClient,
  FrontendCatalog,
  VerifiedFrontendContribution,
} from "./frontendContracts";

const quarantined = new Set<string>();

const quarantineKey = (item: VerifiedFrontendContribution) =>
  `${item.resolved_plan_hash}:${item.owner_pack_id}:${item.contribution_id}`;

export const resetFrontendHostQuarantineForTests = () => quarantined.clear();

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
          onError={() => quarantined.add(quarantineKey(item))}
        >
          <ContributionView
            item={item}
            capabilities={capabilities}
          />
        </ContributionBoundary>
      ))}
    </div>
  );
}

function ContributionView({
  item,
  capabilities,
}: {
  item: VerifiedFrontendContribution;
  capabilities: FrontendCapabilityClient;
}) {
  if (item.mode === "declarative") {
    return <DeclarativeView item={item} capabilities={capabilities} />;
  }
  if (item.mode === "isolated") {
    return <IsolatedView item={item} />;
  }
  return <BuiltinModuleView item={item} />;
}

function DeclarativeView({
  item,
  capabilities,
}: {
  item: VerifiedFrontendContribution;
  capabilities: FrontendCapabilityClient;
}) {
  const view = item.view ?? {};
  const title = String(view.title ?? item.label);
  const body = String(view.body ?? item.description ?? "");
  const [result, setResult] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const invoke = async () => {
    if (!item.action_contract || busy) return;
    setBusy(true);
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
      }));
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
      {result !== null && <GenericValue value={result} />}
    </section>
  );
}

function IsolatedView({
  item,
}: {
  item: VerifiedFrontendContribution;
}) {
  return <HostFallback title={`${item.label} requires a dedicated isolated origin`} />;
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
      quarantined.add(quarantineKey(item));
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
