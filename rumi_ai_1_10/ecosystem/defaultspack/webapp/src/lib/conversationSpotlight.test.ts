import test from "node:test";
import assert from "node:assert/strict";

import type { Conversation } from "./api";
import { conversationMatchesSpotlightFilter, conversationToSearchResult } from "./conversationSpotlight";
import { shortcutLabel, shortcutSpecMatchesEvent } from "./keyboardShortcuts";

function conversation(patch: Partial<Conversation>): Conversation {
  return {
    id: "conv-1",
    title: "Weather Search",
    created_at: Date.now(),
    updated_at: Date.now(),
    messages: [],
    is_starred: false,
    is_archived: false,
    tags: [],
    ...patch,
  } as Conversation;
}

test("spotlight filters starred and recent conversations", () => {
  const starred = conversation({ is_starred: true });
  const old = conversation({ updated_at: Date.now() - 40 * 86_400_000 });

  assert.equal(conversationMatchesSpotlightFilter(starred, "starred"), true);
  assert.equal(conversationMatchesSpotlightFilter(old, "30d"), false);
});

test("spotlight converts recent conversation to search result fallback", () => {
  const result = conversationToSearchResult(conversation({ title: "" }));

  assert.equal(result.title, "New Conversation");
  assert.equal(result.match_count, 0);
});

test("spotlight shortcut supports win and three-key combinations", () => {
  assert.equal(shortcutLabel("Win+Alt+K"), "Win+Alt+K");
  assert.equal(shortcutSpecMatchesEvent("Win+Alt+K", { altKey: true, metaKey: true, key: "k" }), true);
  assert.equal(shortcutSpecMatchesEvent("Ctrl+Alt+K", { ctrlKey: true, altKey: true, key: "K" }), true);
  assert.equal(shortcutSpecMatchesEvent("Ctrl+Alt+K", { ctrlKey: true, altKey: true, shiftKey: true, key: "K" }), false);
});

test("spotlight shortcut can be disabled in text inputs", () => {
  const inputTarget = { tagName: "input", type: "text" } as unknown as EventTarget;

  assert.equal(shortcutSpecMatchesEvent("Ctrl+K", { ctrlKey: true, key: "k", target: inputTarget }), false);
  assert.equal(
    shortcutSpecMatchesEvent("Ctrl+K", { ctrlKey: true, key: "k", target: inputTarget }, { allowTextInput: true }),
    true,
  );
  assert.equal(shortcutSpecMatchesEvent("off", { ctrlKey: true, key: "k" }), false);
  assert.equal(shortcutSpecMatchesEvent("K", { key: "k" }), false);
  assert.equal(shortcutLabel("K"), "Off");
  assert.equal(shortcutSpecMatchesEvent("Ctrl+Esc", { ctrlKey: true, key: "Escape" }), true);
  assert.equal(shortcutLabel("Ctrl+K+L"), "Off");
});

test("spotlight Space shortcut matches the real keyboard event", () => {
  assert.equal(shortcutLabel("Ctrl+Space"), "Ctrl+Space");
  assert.equal(
    shortcutSpecMatchesEvent("Ctrl+Space", { ctrlKey: true, key: " " }),
    true,
  );
  assert.equal(shortcutSpecMatchesEvent("Ctrl+Space", { key: " " }), false);
  assert.equal(
    shortcutSpecMatchesEvent("Ctrl+Space", { ctrlKey: true, key: "" }),
    false,
  );
  assert.equal(
    shortcutSpecMatchesEvent("Ctrl+Space", {
      ctrlKey: true,
      key: " ",
      isComposing: true,
    }),
    false,
  );
});
