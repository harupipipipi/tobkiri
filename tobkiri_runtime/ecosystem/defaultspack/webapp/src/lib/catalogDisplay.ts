import type { CatalogDisplayMetadata, ToolUiMetadata } from "./api";

export type CatalogDisplaySurface = "composer" | "sidebar";

export type CatalogDisplayItem = {
  id?: string;
  ui?: CatalogDisplayMetadata & Partial<ToolUiMetadata>;
  metadata?: Record<string, unknown>;
};

export type ResolvedCatalogDisplay = {
  icon?: string;
  image?: string;
};

const ICON_TOKEN = /^[a-z0-9][a-z0-9._/-]{0,127}$/i;
const STATIC_RASTER_IMAGE = /^\/static\/(?:[a-z0-9._-]+\/)*[a-z0-9._-]+\.(?:avif|gif|jpe?g|png|webp)$/i;

function nonEmptyString(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const normalized = value.trim();
  return normalized || undefined;
}

function metadataDisplay(metadata: Record<string, unknown> | undefined): CatalogDisplayMetadata {
  if (!metadata) return {};
  const declared = metadata.display;
  const display = declared && typeof declared === "object" && !Array.isArray(declared)
    ? declared as Record<string, unknown>
    : {};
  return {
    icon: nonEmptyString(display.icon) ?? nonEmptyString(metadata.icon),
    image: nonEmptyString(display.image) ?? nonEmptyString(metadata.image),
  };
}

export function safeCatalogImagePath(value: unknown): string | undefined {
  const normalized = nonEmptyString(value);
  if (!normalized || !STATIC_RASTER_IMAGE.test(normalized)) return undefined;
  const segments = normalized.slice("/static/".length).split("/");
  if (segments.some((segment) => segment === "." || segment === "..")) return undefined;
  return normalized;
}

export function resolveCatalogDisplay(
  item: CatalogDisplayItem,
  surface: CatalogDisplaySurface,
): ResolvedCatalogDisplay {
  const ui = item.ui ?? {};
  const legacy = metadataDisplay(item.metadata);
  const iconCandidates = surface === "composer"
    ? [ui.composer_icon, ui.item_icon, ui.icon, ui.group_icon, legacy.icon]
    : [ui.item_icon, ui.icon, ui.group_icon, ui.composer_icon, legacy.icon];
  const icon = iconCandidates
    .map(nonEmptyString)
    .find((candidate) => candidate !== undefined && ICON_TOKEN.test(candidate));
  const image = safeCatalogImagePath(ui.image ?? legacy.image);
  return {
    ...(icon ? { icon } : {}),
    ...(image ? { image } : {}),
  };
}
