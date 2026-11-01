import assert from "node:assert/strict";
import test from "node:test";
import { type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ToolSelectionControlSettings, ToolSelectionControlSettingsView } from "./ToolSelectionControlSettings";
import { readToolSelectionControlPreference, saveToolSelectionControlPreference } from "../lib/toolSelectionControlPreference";

test("the visibility switch labels its browser and Profile scope without a Host save", () => {
  const html = renderToStaticMarkup(<ToolSelectionControlSettings profileId="profile-a" />);
  assert.match(html, /role="switch" aria-checked="false"/);
  assert.match(html, /このブラウザーの現在の Profile に保存します/);
  assert.match(html, /メッセージごとに自動・手動・ツールなし/);
  assert.doesNotMatch(html, /保存済み|Backend|Host confirmed/);
});

test("the actual switch click writes only a display Boolean and failed writes stay unchecked", () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  let error: string | null = null;
  const click = (broken = false) => {
    const view = ToolSelectionControlSettingsView({
      visible: readToolSelectionControlPreference("a", storage), disabled: false, error,
      onChange: (visible) => { error = saveToolSelectionControlPreference("a", visible, broken ? null : storage) ? null : "保存できませんでした"; },
    });
    const button = view.props.children[0] as ReactElement<{ onClick: () => void }>;
    button.props.onClick();
  };
  click(true);
  assert.equal(readToolSelectionControlPreference("a", storage), false);
  assert.equal(error, "保存できませんでした");
  click();
  assert.equal(readToolSelectionControlPreference("a", storage), true);
  assert.equal(error, null);
  assert.equal(readToolSelectionControlPreference("b", storage), false);
  assert.deepEqual([...values.values()], ["true"]);
});

test("storage failure is shown as an alert, and an unavailable Profile disables the switch", () => {
  const html = renderToStaticMarkup(<ToolSelectionControlSettingsView
    visible={false} disabled={false} error="表示設定を保存できませんでした。" onChange={() => undefined}
  />);
  assert.match(html, /role="alert"[^>]*>表示設定を保存できませんでした/);
  assert.match(html, /aria-checked="false"/);
  const unavailable = renderToStaticMarkup(<ToolSelectionControlSettings profileId="unavailable" />);
  assert.match(unavailable, /disabled=""/);
  assert.match(unavailable, /Profile を確認してから/);
});
