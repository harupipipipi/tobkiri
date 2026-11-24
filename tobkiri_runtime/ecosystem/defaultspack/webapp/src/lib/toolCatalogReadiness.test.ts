import test from "node:test";
import assert from "node:assert/strict";
import { toolSidebarReadiness } from "./toolCatalogReadiness";
import type { SidebarItem, ToolCatalogTool } from "./api";

const legacy: SidebarItem = { id: "calculator", label: "計算", category: "tool", description: "Local math",
  origin: { kind: "legacy" }, tool_info: { source_pack_id: "defaults", setup_state: { status: "ok" } } };
const calculator: ToolCatalogTool = { tool_id: "calculator", service_id: "local", service_label: "Local",
  name: "Calculator", action_class: "read", connection_status: "connected" };

test("legacy tool ready flags cannot advertise missing canonical adapters", () => {
  const item = toolSidebarReadiness(legacy, undefined);
  assert.equal(item.origin?.kind, "profile_tool_catalog");
  assert.equal(item.tool_info?.setup_state?.status, "missing");
  assert.equal(item.badge, "Unavailable");
});

test("an authenticated connected adapter keeps existing tool copy and presentation", () => {
  const item = toolSidebarReadiness(legacy, calculator);
  assert.equal(item.tool_info?.setup_state?.status, "ok");
  assert.equal(item.tool_info?.source_pack_id, "defaults");
  assert.equal(item.label, "計算");
  assert.equal(item.badge, null);
  assert.equal(legacy.origin?.kind, "legacy");
});

test("published but disconnected tools remain unavailable and unrelated sidebar items stay unchanged", () => {
  assert.equal(toolSidebarReadiness(legacy, { ...calculator, connection_status: "disconnected" }).tool_info?.setup_state?.status, "missing");
  const history: SidebarItem = { id: "history", label: "History", category: "system" };
  assert.equal(toolSidebarReadiness(history, undefined), history);
});
