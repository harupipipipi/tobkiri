import type { McpServerRecord } from "./api";
import type { ComposerExtensionItem, DroppedWidget } from "../renderers/types";

export type ComposerHistoryCandidate = {
  kind: "chat" | "group";
  id: string;
  label: string;
  profileId: string;
  syntax: string;
  available: boolean;
};
export type ComposerMcpCandidate = {
  kind: "mcp";
  id: string;
  label: string;
  syntax: string;
  status: string;
  available: boolean;
  toolIds: string[];
};
export type ComposerEntityCandidate = ComposerHistoryCandidate | ComposerMcpCandidate;

const MCP_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const HISTORY_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

/** Display only authenticated, profile-scoped catalog entries; selection resolves again. */
export function buildComposerHistoryCandidates(
  confirmedCatalog: readonly Omit<ComposerHistoryCandidate, "available">[],
  profileId: string,
): ComposerHistoryCandidate[] {
  const seen = new Set<string>();
  return confirmedCatalog.flatMap((reference) => {
    if (!profileId || reference.profileId !== profileId
      || (reference.kind !== "chat" && reference.kind !== "group")
      || !HISTORY_ID_PATTERN.test(reference.id)
      || reference.syntax !== `@${reference.kind}:${reference.id}`) return [];
    const key = `${reference.kind}:${reference.id}`;
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ ...reference, available: true }];
  });
}

function registeredToolIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item === "string") return [item];
    if (item && typeof item === "object" && "tool_id" in item
      && typeof item.tool_id === "string") return [item.tool_id];
    return [];
  });
}

/** Intersect server registry IDs with enabled tools without guessing tool prefixes. */
export function buildComposerMcpCandidates(
  servers: readonly McpServerRecord[],
  tools: readonly ComposerExtensionItem[],
): ComposerMcpCandidate[] {
  const availableTools = new Set(tools.filter((tool) => !tool.disabled).map((tool) => tool.id));
  const seen = new Set<string>();
  return servers.flatMap((server) => {
    if (!MCP_ID_PATTERN.test(server.server_id) || seen.has(server.server_id)) return [];
    seen.add(server.server_id);
    const status = server.status ?? server.inspect?.status ?? "unknown";
    const connected = server.connected === true;
    const toolIds = [...new Set(registeredToolIds(server.inspect?.tools))]
      .filter((id) => availableTools.has(id));
    const available = connected && status === "connected" && toolIds.length > 0;
    return [{ kind: "mcp" as const, id: server.server_id,
      label: server.name || server.server_name || server.inspect?.name || server.server_id,
      syntax: `@mcp:${server.server_id}`, status, available, toolIds }];
  });
}

/** Build selection metadata only; host execution still uses local approval policy. */
export function composerMcpMentionWidget(candidate: ComposerMcpCandidate): DroppedWidget | null {
  if (!candidate.available || candidate.status !== "connected" || !candidate.toolIds.length
    || !MCP_ID_PATTERN.test(candidate.id) || candidate.syntax !== `@mcp:${candidate.id}`) return null;
  return {
    id: `mention-mcp:${candidate.id}`, type: "service", widgetKind: "service_reference",
    sourceItemId: candidate.id, label: candidate.label, enabled: true,
    metadata: {
      source: "composer_at_mention",
      mention: { kind: "mcp", id: candidate.id, label: candidate.label,
        syntax: candidate.syntax, status: candidate.status, tool_ids: [...candidate.toolIds] },
      service: { id: candidate.id, label: candidate.label, status: candidate.status,
        tool_ids: [...candidate.toolIds] },
    },
  };
}
