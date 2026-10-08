import type { ViewInputContext } from "./catalogViewRegistry";
import {
  surfacePublicInput, surfaceReadPath, type SurfaceNode, type SurfaceRequest,
} from "./surfaceTemplateContract";

/** Bind captured source/context values without taking renderer-owned authority. */
export function surfaceRequestPayload(
  request: SurfaceRequest, snapshot: unknown, context: ViewInputContext,
  additions: Record<string, unknown> = {},
): Record<string, unknown> | null {
  const payload: Record<string, unknown> = { ...(request.input ?? {}) };
  for (const [key, path] of Object.entries(request.source_bindings ?? {})) {
    const value = surfaceReadPath(snapshot, path);
    if (value === undefined || Object.prototype.hasOwnProperty.call(payload, key)) return null;
    payload[key] = value;
  }
  for (const [key, name] of Object.entries(request.context_bindings ?? {})) {
    const value = context[name];
    if (!value || Object.prototype.hasOwnProperty.call(payload, key)) return null;
    payload[key] = value;
  }
  for (const [key, value] of Object.entries(additions)) {
    if (Object.prototype.hasOwnProperty.call(payload, key)) return null;
    payload[key] = value;
  }
  return surfacePublicInput(payload) ? payload : null;
}

export type SurfaceCollectionItem = { id: string; label: string };
/** Collection IDs are source data, not paths or capability references. */
export function surfaceCollection(node: SurfaceNode, snapshot: unknown): SurfaceCollectionItem[] | null {
  const source = surfaceReadPath(snapshot, node.items_path);
  if (!Array.isArray(source) || source.length > 256) return null;
  const items: SurfaceCollectionItem[] = [];
  const ids = new Set<string>();
  for (const row of source) {
    const id = surfaceReadPath(row, node.id_path);
    const label = surfaceReadPath(row, node.label_path);
    if (typeof id !== "string" || id.length < 1 || id.length > 256 || ids.has(id)
      || typeof label !== "string" || label.length < 1 || label.length > 256) return null;
    ids.add(id); items.push({ id, label });
  }
  return items;
}

export const surfaceDisplayText = (value: unknown): string => typeof value === "string" ? value.slice(0, 4096)
  : typeof value === "number" && Number.isFinite(value) ? String(value)
    : typeof value === "boolean" ? value ? "Yes" : "No" : "Unavailable";
