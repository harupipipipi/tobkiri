import test from "node:test";
import assert from "node:assert/strict";

import type { SidebarItem } from "./api";
import {
  spotlightShortcutLabel,
  spotlightShortcutMatchesEvent,
  spotlightSidebarResults,
} from "./spotlightNavigation";

const available = { enabled: true, disabledToolIds: new Set<string>() };
const items: SidebarItem[] = [
  { id: "search", label: "Web Search", category: "tool", description: "Find pages", tags: ["internet"] },
  { id: "notes", label: "Notes", category: "widget", description: "memo" },
  { id: "system", label: "System", category: "system" },
  { id: " ", label: "Invalid", category: "tool" },
];

test("catalog results use real IDs and exact tool or widget categories", () => {
  const tools = spotlightSidebarResults(items, "tool", "", available);
  assert.deepEqual(tools.map(({ id, kind }) => ({ id, kind })), [{ id: "search", kind: "tool" }]);
  const widgets = spotlightSidebarResults(items, "widget", "", available);
  assert.deepEqual(widgets.map(({ id, kind }) => ({ id, kind })), [{ id: "notes", kind: "widget" }]);
  assert.deepEqual(spotlightSidebarResults(items, "tool", "no such item", available), []);
  assert.deepEqual(spotlightSidebarResults([], "widget", "notes", available), []);
  assert.equal(spotlightSidebarResults(items, "tool", "WEB", available)[0]?.id, "search");
  assert.equal(spotlightSidebarResults(items, "tool", "pages", available)[0]?.id, "search");
  assert.equal(spotlightSidebarResults(items, "widget", "memo", available)[0]?.id, "notes");
});

test("disabled catalog tools remain discoverable with visible status", () => {
  const disabled = spotlightSidebarResults(items, "tool", "search", {
    enabled: true, disabledToolIds: new Set(["search"]),
  });
  assert.equal(disabled[0]?.id, "search");
  assert.ok(disabled[0]?.badge);
  const unavailable = spotlightSidebarResults([
    { ...items[0], tool_info: { setup_state: { status: "missing" } } },
  ], "tool", "search", available);
  assert.equal(unavailable[0]?.id, "search");
  assert.ok(unavailable[0]?.badge);
  assert.deepEqual(spotlightSidebarResults(items, "tool", "", {
    enabled: false, disabledToolIds: new Set<string>(),
  }), []);
  assert.deepEqual(items[0], {
    id: "search", label: "Web Search", category: "tool", description: "Find pages", tags: ["internet"],
  });
});

test("default Mac shortcut supports Ctrl and Cmd without modifying custom bindings", () => {
  assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", { key: "k", ctrlKey: true }, { isMac: true }), true);
  assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", { key: "k", metaKey: true }, { isMac: true }), true);
  assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", { key: "k", metaKey: true }, { isMac: false }), false);
  assert.equal(spotlightShortcutMatchesEvent("Ctrl+P", { key: "p", metaKey: true }, { isMac: true }), false);
  assert.equal(spotlightShortcutMatchesEvent("Cmd+P", { key: "p", metaKey: true }, { isMac: true }), true);
  assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", { key: "k", ctrlKey: true, shiftKey: true }, { isMac: true }), false);
  assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", { key: "k", ctrlKey: true, altKey: true }, { isMac: true }), false);
  assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", { key: "k", ctrlKey: true, metaKey: true }, { isMac: true }), false);
  assert.equal(spotlightShortcutMatchesEvent("off", { key: "k", ctrlKey: true }), false);
  assert.equal(spotlightShortcutLabel("Ctrl+K", true), "Ctrl+K / Cmd+K");
  assert.equal(spotlightShortcutLabel("Ctrl+K", false), "Ctrl+K");
  assert.equal(spotlightShortcutLabel("Cmd+P", true), "Cmd+P");
});

test("spotlight shortcut respects consumed events, repeats, IME and input policy", () => {
  const chord = { key: "k", ctrlKey: true };
  for (const override of [{ defaultPrevented: true }, { repeat: true }, { isComposing: true }, { keyCode: 229 }]) {
    assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", { ...chord, ...override }), false);
  }
  for (const target of [
    { tagName: "input", type: "text" },
    { tagName: "textarea" },
    { tagName: "select" },
    { isContentEditable: true },
    { getAttribute: (name: string) => name === "role" ? "textbox" : null },
  ]) {
    const event = { ...chord, target: target as unknown as EventTarget };
    assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", event, { allowTextInput: false }), false);
    assert.equal(spotlightShortcutMatchesEvent("Ctrl+K", event, { allowTextInput: true }), true);
  }
});
