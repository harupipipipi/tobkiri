import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import type { ConversationSearchResult } from "../lib/api";
import { ConversationSpotlight } from "./ConversationSpotlight";

function result(id: string, title: string): ConversationSearchResult {
  return {
    conversation_id: id,
    title,
    created_at: 1_785_000_000_000,
    updated_at: 1_785_000_000_000,
    is_starred: false,
    is_archived: false,
    match_count: 1,
    matches: [{
      message_id: `message-${id}`,
      role: "user",
      created_at: 1_785_000_000_000,
      snippet: "A deliberately long result excerpt that remains visually bounded.",
      exact: true,
      score: 1,
    }],
  };
}

function renderSpotlight(overrides: Partial<Parameters<typeof ConversationSpotlight>[0]> = {}) {
  return renderToStaticMarkup(
    <ConversationSpotlight
      isOpen
      query="release"
      filter="all"
      results={[result("conversation/one", "First conversation"), result("conversation-two", "Second conversation")]}
      resultTotal={2}
      selectedIndex={1}
      loading={false}
      locale="en"
      shortcutLabel="Ctrl+K"
      onQueryChange={() => undefined}
      onFilterChange={() => undefined}
      onKeyDown={() => undefined}
      onClose={() => undefined}
      onOpenResult={() => undefined}
      {...overrides}
    />,
  );
}

test("conversation spotlight exposes one linked dialog combobox and listbox model", () => {
  const html = renderSpotlight();

  assert.match(html, /role="dialog"/);
  assert.match(html, /aria-modal="true"/);
  assert.match(html, /role="combobox"/);
  assert.match(html, /aria-label="Conversation search query"/);
  assert.match(html, /aria-autocomplete="list"/);
  assert.match(html, /aria-expanded="true"/);
  assert.match(html, /aria-controls="conversation-spotlight-results-/);
  assert.match(html, /aria-activedescendant="conversation-spotlight-option-conversation-two"/);
  assert.match(html, /role="listbox"/);
  assert.match(html, /aria-label="Conversation search results"/);
  assert.match(html, /id="conversation-spotlight-option-conversation%2Fone"/);
  assert.match(html, /role="option" aria-selected="false" tabindex="-1"/);
  assert.match(html, /role="option" aria-selected="true" tabindex="-1"/);
});

test("conversation spotlight exposes pressed filters, named controls, and one status channel", () => {
  const html = renderSpotlight({ filter: "starred", loading: true });

  assert.match(html, /role="group" aria-label="Filter"/);
  assert.match(html, /aria-pressed="true"[^>]*>Starred/);
  assert.match(html, /role="note"[^>]*aria-label="Conversation search shortcut: Ctrl\+K"/);
  assert.match(html, /aria-label="Close conversation search"/);
  assert.match(html, /role="status" aria-live="polite" aria-atomic="true"/);
  assert.match(html, />searching\.\.\.<\/span>/);
});

test("conversation spotlight reports no results without a dangling active option", () => {
  const html = renderSpotlight({ results: [], resultTotal: 0, selectedIndex: 0, loading: false });

  assert.doesNotMatch(html, /aria-activedescendant=/);
  assert.match(html, /aria-busy="false"/);
  assert.match(html, /No matching conversations\.<\/span>/);
});
