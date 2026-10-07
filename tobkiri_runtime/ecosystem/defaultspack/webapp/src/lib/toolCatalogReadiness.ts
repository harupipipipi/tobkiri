import type { SidebarItem, ToolCatalogTool } from "./api";

/** The authenticated tool catalog, rather than legacy UI metadata, sets readiness. */
export function toolSidebarReadiness(item: SidebarItem, tool: ToolCatalogTool | undefined): SidebarItem {
  if (item.category !== "tool" && item.category !== "capability") return item;
  const available = tool?.connection_status === "connected";
  return {
    ...item,
    badge: available ? null : "Unavailable",
    tool_info: {
      ...item.tool_info,
      ...(tool ? { service_id: tool.service_id } : {}),
      setup_state: { ...item.tool_info?.setup_state, status: available ? "ok" : "missing" },
    },
    origin: { ...item.origin, kind: "profile_tool_catalog" },
  };
}
