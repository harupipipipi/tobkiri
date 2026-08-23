import { Component, useMemo, useRef, type ErrorInfo, type ReactNode } from "react";

export const SURFACE_TEMPLATE_API_VERSION = "io.tobkiri.surface-template.v1";
export const SURFACE_RENDERER_API_VERSION = "io.tobkiri.surface-renderer.v1";

export type SurfacePattern =
  | "content"
  | "problem"
  | "notice"
  | "progress"
  | "resource_input"
  | "choice"
  | "form"
  | "confirmation"
  | "collection"
  | "detail";

export type SurfaceSelector = string;

export type SurfaceOutcome = {
  pattern: SurfacePattern;
  data?: SurfaceSelector;
  items?: SurfaceSelector;
  code?: SurfaceSelector;
  message?: SurfaceSelector;
  current?: SurfaceSelector;
  total?: SurfaceSelector;
  level?: SurfaceSelector;
  dedupe_key?: SurfaceSelector;
  title?: SurfaceSelector;
  details?: SurfaceSelector;
};

export type SurfaceTemplate = {
  surface_api_version: typeof SURFACE_TEMPLATE_API_VERSION;
  template_id: string;
  version: string;
  input: {
    pattern: SurfacePattern;
    bind_to?: SurfaceSelector;
    accepts?: { resource_kinds?: string[]; media_types?: string[]; multiple?: boolean };
    resource_kind?: string;
    effect?: string;
    options?: SurfaceSelector;
    label?: string;
  };
  outcomes: {
    success?: SurfaceOutcome;
    error?: SurfaceOutcome;
    progress?: SurfaceOutcome;
    notice?: SurfaceOutcome;
  };
  actions?: Array<{
    contract_id: string;
    operation_id: string;
    payload_binding: Record<string, SurfaceSelector>;
    label?: string;
    sensitive?: boolean;
  }>;
  security?: { class?: "ordinary" | "trusted" | "sensitive"; requires_trusted_renderer?: boolean };
};

export type SurfaceEvent = Record<string, unknown>;

export type SurfaceIntent = {
  template_id: string;
  pattern: SurfacePattern;
  payload: Record<string, unknown>;
};

type SurfaceAction = NonNullable<SurfaceTemplate["actions"]>[number];

type SurfaceTemplateRendererProps = {
  template: SurfaceTemplate;
  event: SurfaceEvent;
  onAction?: (action: SurfaceAction, payload: Record<string, unknown>) => void;
  trustedRenderer?: boolean;
};

const interactivePatterns = new Set<SurfacePattern>([
  "resource_input",
  "choice",
  "form",
  "confirmation",
]);

const fallbackPatterns = new Set<SurfacePattern>([
  "content",
  "problem",
  "notice",
  "progress",
  "collection",
  "detail",
]);

export function resolveSurfaceSelector(selector: SurfaceSelector, value: unknown): unknown {
  const tokens = parseSurfaceSelector(selector);
  let current = value;
  for (const token of tokens) {
    if (typeof token === "number") {
      if (!Array.isArray(current) || token >= current.length) return undefined;
      current = current[token];
      continue;
    }
    if (!isRecord(current) || ["__proto__", "prototype", "constructor"].includes(token)) {
      return undefined;
    }
    current = current[token];
  }
  return current;
}

export function parseSurfaceSelector(selector: SurfaceSelector): Array<string | number> {
  if (selector === "$") return [];
  if (typeof selector !== "string" || selector.length > 256 || !selector.startsWith("$")) {
    throw new Error("Surface selector is outside the bounded path language");
  }
  const body = selector.slice(1);
  const tokens: Array<string | number> = [];
  let index = 0;
  while (index < body.length) {
    if (body[index] === ".") {
      index += 1;
      const match = /^[A-Za-z][A-Za-z0-9_]*/.exec(body.slice(index));
      if (!match) throw new Error("Surface selector contains an empty member");
      tokens.push(match[0]);
      index += match[0].length;
    } else if (body[index] === "[") {
      const end = body.indexOf("]", index + 1);
      const raw = end >= 0 ? body.slice(index + 1, end) : "";
      if (!/^(0|[1-9][0-9]*)$/.test(raw)) {
        throw new Error("Surface selector contains an invalid index");
      }
      tokens.push(Number(raw));
      index = end + 1;
    } else {
      const match = /^[A-Za-z][A-Za-z0-9_]*/.exec(body.slice(index));
      if (!match) throw new Error("Surface selector contains an invalid member");
      tokens.push(match[0]);
      index += match[0].length;
    }
    if (tokens.length > 16) throw new Error("Surface selector exceeds maximum depth");
  }
  if (tokens.some((token) => typeof token === "string" && ["__proto__", "prototype", "constructor"].includes(token))) {
    throw new Error("Surface selector contains a forbidden member");
  }
  return tokens;
}

export function projectSurfaceIntent(
  template: SurfaceTemplate,
  event: SurfaceEvent,
): SurfaceIntent | null {
  const eventKind = String(event.kind ?? event.status ?? event.type ?? "success").toLowerCase();
  const key: "success" | "error" | "progress" | "notice" = eventKind === "error" || eventKind === "failure" || eventKind === "problem"
    ? "error"
    : eventKind === "progress" || eventKind === "working" || eventKind === "pending"
      ? "progress"
      : eventKind === "notice" || eventKind === "warning" || eventKind === "info"
        ? "notice"
        : "success";
  const outcome = template.outcomes[key];
  if (!outcome) return null;
  const payload: Record<string, unknown> = {};
  for (const field of [
    "data", "items", "code", "message", "current", "total", "level", "dedupe_key", "title", "details",
  ] as const) {
    const selector = outcome[field];
    if (selector) payload[field] = resolveSurfaceSelector(selector, event);
  }
  return { template_id: template.template_id, pattern: outcome.pattern, payload };
}

export function isHostResourceHandle(value: unknown): value is string {
  return typeof value === "string"
    && /^handle:[A-Za-z0-9][A-Za-z0-9._:/~-]{0,255}$/.test(value);
}

export function SurfaceTemplateRenderer({
  ...props
}: SurfaceTemplateRendererProps) {
  return (
    <SurfaceTemplateErrorBoundary>
      <SurfaceTemplateRendererBody {...props} />
    </SurfaceTemplateErrorBoundary>
  );
}

function SurfaceTemplateRendererBody({
  template,
  event,
  onAction,
  trustedRenderer = false,
}: SurfaceTemplateRendererProps) {
  const noticeKeys = useRef<Set<string>>(new Set());
  const intent = useMemo(() => projectSurfaceIntent(template, event), [template, event]);
  if (!intent) return <SurfaceFallback message="This operation has no supported surface outcome." />;
  if (intent.pattern === "notice" && typeof intent.payload.dedupe_key === "string") {
    const key = intent.payload.dedupe_key;
    if (noticeKeys.current.has(key)) return null;
    noticeKeys.current.add(key);
  }
  if (
    (template.security?.class && template.security.class !== "ordinary"
      || intent.pattern === "confirmation")
    && !trustedRenderer
  ) {
    return <SurfaceFallback message="This trusted decision surface is unavailable." />;
  }
  const actions = (template.actions ?? []).filter((action) => trustedRenderer || !action.sensitive);
  return (
    <section
      aria-label={template.template_id}
      data-surface-template={template.template_id}
      data-surface-pattern={intent.pattern}
      className="min-w-0 max-w-full motion-safe:transition-opacity"
    >
      <SurfaceIntentView intent={intent} />
      {actions.map((action, index) => (
        <button
          key={`${action.contract_id}/${action.operation_id}/${index}`}
          type="button"
          className="mt-2 min-h-11 max-w-full rounded-md border border-zinc-700 px-3 text-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-300"
          onClick={() => {
            const payload = Object.fromEntries(
              Object.entries(action.payload_binding).map(([key, selector]) => [
                key,
                resolveSurfaceSelector(selector, event),
              ]),
            );
            onAction?.(action, payload);
          }}
        >
          {action.label ?? "Continue"}
        </button>
      ))}
    </section>
  );
}

export class SurfaceTemplateErrorBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // The Host owns diagnostics/audit. Never expose renderer exceptions as pack content.
  }

  render(): ReactNode {
    if (this.state.failed) {
      return <SurfaceFallback message="This surface could not be rendered safely." />;
    }
    return this.props.children;
  }
}

function SurfaceIntentView({ intent }: { intent: SurfaceIntent }): ReactNode {
  const payload = intent.payload;
  if (intent.pattern === "problem") {
    return <p role="alert" aria-live="assertive">{String(payload.message ?? "The operation failed.")}</p>;
  }
  if (intent.pattern === "notice") {
    return <p role="status" aria-live="polite">{String(payload.message ?? "")}</p>;
  }
  if (intent.pattern === "progress") {
    const current = typeof payload.current === "number" ? payload.current : 0;
    const total = typeof payload.total === "number" ? payload.total : undefined;
    return (
      <div role="status" aria-live="polite" aria-label="Progress">
        <progress value={total === undefined ? undefined : current} max={total} />
        <span>{total === undefined ? String(current) : `${current} / ${total}`}</span>
      </div>
    );
  }
  if (intent.pattern === "resource_input") {
    const resource = payload.resource;
    return <p role="status">{isHostResourceHandle(resource) ? resource : "Select a Host-approved resource."}</p>;
  }
  if (intent.pattern === "choice" || intent.pattern === "form" || intent.pattern === "confirmation") {
    return <p role="group">{String(payload.title ?? payload.message ?? "Choose an option.")}</p>;
  }
  if (intent.pattern === "collection" && Array.isArray(payload.items)) {
    return <ul>{payload.items.map((item, index) => <li key={index}>{String(item)}</li>)}</ul>;
  }
  return <pre className="max-w-full overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(payload.data ?? payload, null, 2)}</pre>;
}

function SurfaceFallback({ message }: { message: string }) {
  return <section role="status" aria-live="polite">{message}</section>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export { fallbackPatterns, interactivePatterns };
