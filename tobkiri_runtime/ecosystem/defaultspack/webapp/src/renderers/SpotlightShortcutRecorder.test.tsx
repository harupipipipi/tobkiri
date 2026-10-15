import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { SettingsModalRenderer } from "./SettingsModalRenderer";
import {
  handleSpotlightShortcutCapture,
  SpotlightShortcutRecorder,
  spotlightShortcutFromEvent,
} from "./SpotlightShortcutRecorder";
import { shortcutSpecMatchesEvent } from "../lib/keyboardShortcuts";
import { spotlightShortcutLabel } from "../lib/spotlightNavigation";
import type { SettingsSection } from "../lib/api";

const field: SettingsSection["fields"][number] = {
  id: "spotlight_shortcut", type: "text", label: "会話検索のキー", default: "Ctrl+K",
};

test("recorded Ctrl/Cmd, arrow and function chords match the actual shortcut matcher", () => {
  for (const [event, saved] of [
    [{ key: "k", ctrlKey: true }, "Ctrl+K"],
    [{ key: "p", metaKey: true, altKey: true }, "Cmd+Alt+P"],
    [{ key: "ArrowUp", ctrlKey: true, shiftKey: true }, "Ctrl+Shift+ArrowUp"],
    [{ key: "F6" }, "F6"],
  ] as const) {
    const next = spotlightShortcutFromEvent(event);
    assert.equal(next, saved);
    assert.equal(shortcutSpecMatchesEvent(next, event), true);
  }
  assert.equal(spotlightShortcutLabel("Cmd+Alt+P", true), "Cmd+Alt+P");
  assert.equal(spotlightShortcutLabel("Cmd+Alt+P", false), "Win+Alt+P");
  assert.equal(spotlightShortcutLabel("Ctrl+K", true), "Ctrl+K / Cmd+K");
  assert.equal(spotlightShortcutLabel("Ctrl+K", false), "Ctrl+K");
});

test("modifier-only, IME, repeat and unrepresentable keys never save", () => {
  for (const event of [
    { key: "Shift", shiftKey: true }, { key: "k" }, { key: "Dead", ctrlKey: true },
    { key: "Unidentified", ctrlKey: true }, { key: "+", ctrlKey: true },
    { key: "k", ctrlKey: true, isComposing: true },
    { key: "k", ctrlKey: true, keyCode: 229 },
    { key: "k", ctrlKey: true, repeat: true },
    { key: "k", ctrlKey: true, getModifierState: () => true },
  ]) assert.equal(spotlightShortcutFromEvent(event), null);
});

test("deliberate capture consumes global keys, saves once, and Esc cancels unchanged", () => {
  const saved: string[] = [];
  let prevented = 0;
  let stopped = 0;
  let immediateStopped = 0;
  const event = {
    key: "k", ctrlKey: true,
    preventDefault: () => { prevented += 1; },
    stopPropagation: () => { stopped += 1; },
    stopImmediatePropagation: () => { immediateStopped += 1; },
  };
  assert.equal(handleSpotlightShortcutCapture(event, false, (next) => saved.push(next)), "inactive");
  assert.deepEqual([prevented, stopped, immediateStopped, saved.length], [0, 0, 0, 0]);
  assert.equal(handleSpotlightShortcutCapture(event, true, (next) => saved.push(next)), "recorded");
  assert.deepEqual(saved, ["Ctrl+K"]);
  assert.deepEqual([prevented, stopped, immediateStopped], [1, 1, 1]);
  assert.equal(handleSpotlightShortcutCapture({ ...event, key: "Escape" }, true, (next) => saved.push(next)), "cancelled");
  assert.deepEqual(saved, ["Ctrl+K"]);
});

test("IME keeps native candidate behavior and Tab cancels while permitting focus movement", () => {
  let prevented = false;
  let stopped = false;
  const event = {
    key: "Enter", isComposing: true,
    preventDefault: () => { prevented = true; },
    stopPropagation: () => { stopped = true; },
    stopImmediatePropagation: () => undefined,
  };
  assert.equal(handleSpotlightShortcutCapture(event, true, () => assert.fail("must not save")), "waiting");
  assert.equal(prevented, false);
  assert.equal(stopped, true);
  assert.equal(handleSpotlightShortcutCapture({ ...event, key: "Tab", isComposing: false }, true, () => assert.fail("must not save")), "cancelled");
  assert.equal(prevented, false);
});

test("recorder is initially idle and displays accessible capture and default restore controls", () => {
  const markup = renderToStaticMarkup(<SpotlightShortcutRecorder
    sectionId="general" field={field} value="Cmd+Alt+P" isMac onChange={() => undefined}
  />);
  assert.match(markup, /data-spotlight-shortcut-recorder=""/);
  assert.match(markup, /aria-pressed="false"/);
  assert.match(markup, /Cmd\+Alt\+P/);
  assert.match(markup, /既定に戻す/);
  assert.doesNotMatch(markup, /<input/);
  const defaultMarkup = renderToStaticMarkup(<SpotlightShortcutRecorder
    sectionId="general" field={field} value="Ctrl+K" isMac onChange={() => undefined}
  />);
  assert.doesNotMatch(defaultMarkup, /既定に戻す/);
  assert.match(defaultMarkup, /Ctrl\+K \/ Cmd\+K/);
  const windowsMarkup = renderToStaticMarkup(<SpotlightShortcutRecorder
    sectionId="general" field={field} value="Cmd+Alt+P" isMac={false} onChange={() => undefined}
  />);
  assert.match(windowsMarkup, /Win\+Alt\+P/);
  assert.doesNotMatch(windowsMarkup, /Cmd\+Alt\+P/);
});

test("General text schema routes only spotlight_shortcut to the recorder", () => {
  const markup = renderToStaticMarkup(<SettingsModalRenderer
    isOpen activeSectionId="general" locale="ja" catalog={null} health={null}
    previewsCount={0} settingsSections={[{
      id: "general", label: "General", fields: [
        field,
        { id: "display_note", label: "入力欄の案内文", type: "text", default: "Hello" },
      ],
    }]}
    settingsValues={{ general: { spotlight_shortcut: "Ctrl+Alt+K", display_note: "Type here" } }}
    onClose={() => undefined} onSettingChange={() => undefined}
  />);
  assert.equal((markup.match(/data-spotlight-shortcut-recorder=""/gu) ?? []).length, 1);
  assert.match(markup, /Ctrl\+Alt\+K/);
  assert.match(markup, /value="Type here"/);
  assert.doesNotMatch(markup, /value="Ctrl\+Alt\+K"/);
});
