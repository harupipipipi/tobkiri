import type { SettingsSection } from "./api";

export type PlacementSurface =
  | "left_sidebar"
  | "right_sidebar"
  | "top_bar"
  | "bottom_bar"
  | "composer"
  | "settings"
  | "floating_panel";

export type PlacementOrientation = "horizontal" | "vertical" | "both";

export type PlaceableSourceType =
  | "tool"
  | "setting"
  | "settings_section"
  | "command"
  | "runtime_status"
  | "runtime_toggle"
  | "model"
  | "widget"
  | "integration"
  | "custom";

export type PlacementRendererKind = "component" | "template" | "html";

export type PlacementManifest = {
  id: string;
  label: string;
  description?: string;
  icon?: string;
  source: {
    type: PlaceableSourceType;
    sourceId?: string;
  };
  renderer: {
    kind: PlacementRendererKind;
    trusted?: boolean;
    componentId?: string;
    templateId?: string;
    html?: string;
    action?: {
      type: "open_panel" | "open_settings_section" | "toggle_yolo";
      target?: string;
    };
  };
  placements: Array<{
    surface: PlacementSurface;
    orientation: PlacementOrientation;
  }>;
  constraints?: {
    configurable?: boolean;
    settings_only?: boolean;
    requires_capabilities?: string[];
    requires_context?: string[];
    requires_settings?: string[];
  };
};

export type PinnedPlacement = {
  id: string;
  surface: PlacementSurface;
};

export type UiSettingsValues = Record<string, Record<string, unknown>>;

export type PlacementFilterOptions = {
  surface: PlacementSurface;
  orientation: Exclude<PlacementOrientation, "both">;
  configurableOnly?: boolean;
  availableCapabilities?: string[];
  availableContext?: string[];
  availableSettings?: string[];
};

export type ToggleablePlacementCandidate = {
  manifest: PlacementManifest;
  pinned: boolean;
};

export type PlacementHtmlBlockReason =
  | "empty_html"
  | "oversized_html"
  | "unverified_active_content";

export type PlacementRenderingResolution =
  | { kind: "component" }
  | { kind: "template" }
  | {
      kind: "blocked_html";
      reason: PlacementHtmlBlockReason;
      sourceLabel: string;
      byteLength: number;
      message: string;
    }
  | { kind: "unsupported" };

export const PINNED_PLACEMENTS_STORAGE_KEY = "rumi-ui-placements";
export const PLACEMENT_HTML_MAX_SOURCE_BYTES = 64 * 1024;

export function normalizePinnedPlacements(value: unknown): PinnedPlacement[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((entry) => (entry && typeof entry === "object" ? entry as Record<string, unknown> : {}))
    .map((entry) => ({
      id: String(entry.id ?? "").trim(),
      surface: String(entry.surface ?? "").trim() as PlacementSurface,
    }))
    .filter((entry) => entry.id && entry.surface);
}

function supportsOrientation(actual: PlacementOrientation, requested: Exclude<PlacementOrientation, "both">): boolean {
  return actual === "both" || actual === requested;
}

function includesAll(required: string[] | undefined, actual: string[]): boolean {
  return !required?.length || required.every((value) => actual.includes(value));
}

export function filterPlacementCandidates(
  manifests: PlacementManifest[],
  options: PlacementFilterOptions,
): PlacementManifest[] {
  const capabilities = options.availableCapabilities ?? [];
  const context = options.availableContext ?? [];
  const settings = options.availableSettings ?? [];
  return manifests.filter((manifest) => {
    const placement = manifest.placements.find((candidate) => (
      candidate.surface === options.surface
      && supportsOrientation(candidate.orientation, options.orientation)
    ));
    if (!placement) return false;
    const constraints = manifest.constraints ?? {};
    if (constraints.settings_only && options.surface !== "settings") return false;
    if (options.configurableOnly && constraints.configurable === false) return false;
    if (!includesAll(constraints.requires_capabilities, capabilities)) return false;
    if (!includesAll(constraints.requires_context, context)) return false;
    if (!includesAll(constraints.requires_settings, settings)) return false;
    return true;
  });
}

export function buildToggleablePlacementCandidates(
  manifests: PlacementManifest[],
  pinnedPlacements: PinnedPlacement[],
  options: PlacementFilterOptions,
): ToggleablePlacementCandidate[] {
  return filterPlacementCandidates(manifests, options).map((manifest) => ({
    manifest,
    pinned: pinnedPlacements.some((placement) => (
      placement.id === manifest.id && placement.surface === options.surface
    )),
  }));
}

export function readPinnedPlacements(storage: Pick<Storage, "getItem"> | null | undefined): PinnedPlacement[] {
  try {
    return normalizePinnedPlacements(storage?.getItem(PINNED_PLACEMENTS_STORAGE_KEY)
      ? JSON.parse(storage?.getItem(PINNED_PLACEMENTS_STORAGE_KEY) ?? "[]")
      : []);
  } catch {
    return [];
  }
}

export function writePinnedPlacements(
  storage: Pick<Storage, "setItem"> | null | undefined,
  placements: PinnedPlacement[],
): PinnedPlacement[] {
  const normalized = normalizePinnedPlacements(placements);
  try {
    storage?.setItem(PINNED_PLACEMENTS_STORAGE_KEY, JSON.stringify(normalized));
  } catch {
    // Storage may be unavailable. Caller still receives normalized data.
  }
  return normalized;
}

export function togglePinnedPlacement(
  placements: PinnedPlacement[],
  nextPlacement: PinnedPlacement,
): PinnedPlacement[] {
  const exists = placements.some((placement) => (
    placement.id === nextPlacement.id && placement.surface === nextPlacement.surface
  ));
  if (exists) {
    return placements.filter((placement) => !(
      placement.id === nextPlacement.id && placement.surface === nextPlacement.surface
    ));
  }
  return [...placements, nextPlacement];
}

export function withPinnedPlacements(
  values: UiSettingsValues,
  placements: PinnedPlacement[],
): UiSettingsValues {
  return {
    ...values,
    sidebar: {
      ...(values.sidebar ?? {}),
      ui_placements: normalizePinnedPlacements(placements),
    },
  };
}

function utf8ByteLength(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function placementSourceLabel(manifest: PlacementManifest): string {
  const sourceId = String(manifest.source.sourceId ?? "").trim();
  return sourceId ? `${manifest.source.type}:${sourceId}` : manifest.source.type;
}

export function resolvePlacementHtmlRendering(
  manifest: PlacementManifest,
): PlacementRenderingResolution {
  if (manifest.renderer.kind === "component") return { kind: "component" };
  if (manifest.renderer.kind === "template") return { kind: "template" };
  if (manifest.renderer.kind === "html") {
    const html = String(manifest.renderer.html ?? "");
    const byteLength = utf8ByteLength(html);
    const sourceLabel = placementSourceLabel(manifest);
    if (!html.trim()) {
      return {
        kind: "blocked_html",
        reason: "empty_html",
        sourceLabel,
        byteLength,
        message: "This extension requested an empty HTML placement. Nothing was rendered.",
      };
    }
    if (byteLength > PLACEMENT_HTML_MAX_SOURCE_BYTES) {
      return {
        kind: "blocked_html",
        reason: "oversized_html",
        sourceLabel,
        byteLength,
        message: `This extension requested ${byteLength} bytes of HTML, above the ${PLACEMENT_HTML_MAX_SOURCE_BYTES}-byte limit.`,
      };
    }
    return {
      kind: "blocked_html",
      reason: "unverified_active_content",
      sourceLabel,
      byteLength,
      message: "Arbitrary HTML placements are disabled. Use a verified component or declarative template renderer instead.",
    };
  }
  return { kind: "unsupported" };
}

export function buildBuiltinPlacementManifests(settingsSections: SettingsSection[]): PlacementManifest[] {
  const sectionCandidates = settingsSections.map<PlacementManifest>((section) => ({
    id: `settings-section:${section.id}`,
    label: section.label,
    description: section.description,
    source: { type: "settings_section", sourceId: section.id },
    renderer: {
      kind: "component",
      action: { type: "open_settings_section", target: section.id },
    },
    placements: [
      { surface: "settings", orientation: "vertical" },
      { surface: "right_sidebar", orientation: "vertical" },
    ],
    constraints: {
      configurable: true,
    },
  }));
  return [
    {
      id: "tool-filter-log",
      label: "Tool Filter Log",
      source: { type: "widget", sourceId: "tool-filter-log" },
      renderer: { kind: "component", action: { type: "open_panel", target: "__tool_filter_log__" } },
      placements: [{ surface: "right_sidebar", orientation: "vertical" }],
      constraints: { configurable: true },
    },
    {
      id: "runtime-status",
      label: "Runtime Status",
      source: { type: "runtime_status", sourceId: "runtime-status" },
      renderer: { kind: "component", action: { type: "open_panel", target: "__runtime_status__" } },
      placements: [{ surface: "right_sidebar", orientation: "vertical" }],
      constraints: { configurable: true },
    },
    {
      id: "yolo-switch",
      label: "YOLO Switch",
      source: { type: "runtime_toggle", sourceId: "yolo" },
      renderer: { kind: "component", action: { type: "toggle_yolo" } },
      placements: [
        { surface: "right_sidebar", orientation: "vertical" },
        { surface: "top_bar", orientation: "horizontal" },
      ],
      constraints: { configurable: true },
    },
    {
      id: "model-manager",
      label: "Model Manager",
      source: { type: "model", sourceId: "models" },
      renderer: { kind: "component", action: { type: "open_settings_section", target: "models" } },
      placements: [
        { surface: "right_sidebar", orientation: "vertical" },
        { surface: "top_bar", orientation: "horizontal" },
      ],
      constraints: { configurable: true },
    },
    {
      id: "model-pack-switcher",
      label: "Model Pack Switcher",
      source: { type: "model", sourceId: "model-packs" },
      renderer: { kind: "component", action: { type: "open_settings_section", target: "models" } },
      placements: [
        { surface: "right_sidebar", orientation: "vertical" },
        { surface: "composer", orientation: "horizontal" },
      ],
      constraints: { configurable: true },
    },
    {
      id: "webhook-endpoints",
      label: "Webhook Endpoints",
      source: { type: "integration", sourceId: "external_input" },
      renderer: { kind: "component", action: { type: "open_settings_section", target: "external_input" } },
      placements: [{ surface: "right_sidebar", orientation: "vertical" }],
      constraints: { configurable: true },
    },
    ...sectionCandidates,
  ];
}
