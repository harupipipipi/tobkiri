import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ProjectFolderList } from "./ProjectFolderList";
import { appendProjectFolderSelections, consumeProjectFolderSelections, emptyProjectFolderDraft } from "../../lib/projectFolderDraft";

const noop = () => undefined;
const draft = appendProjectFolderSelections(emptyProjectFolderDraft(), {
  selections: [
    { selection_id: "private-token-a", display_name: "source", expires_in_ms: 10000 },
    { selection_id: "private-token-b", display_name: "source", expires_in_ms: 10000 },
  ], primary_selection_id: "private-token-b",
}, 100);

test("review list distinguishes same-name choices with one explicit primary and no ticket leakage", () => {
  const markup = renderToStaticMarkup(<ProjectFolderList draft={draft} onRemove={noop} onPrimaryChange={noop} />);
  assert.match(markup, /Linked folders \(2\)/);
  assert.match(markup, /source \(1\)/);
  assert.match(markup, /source \(2\)/);
  assert.match(markup, /aria-label="Use source \(2\) as primary folder"/);
  assert.equal((markup.match(/aria-pressed="true"/g) ?? []).length, 1);
  assert.equal((markup.match(/aria-label="Remove source/g) ?? []).length, 2);
  assert.doesNotMatch(markup, /private-token|selection_id|expires_in_ms/);
});

test("busy list disables mutation controls and submitted draft remains visible", () => {
  const markup = renderToStaticMarkup(<ProjectFolderList draft={consumeProjectFolderSelections(draft)} disabled onRemove={noop} onPrimaryChange={noop} />);
  assert.equal((markup.match(/disabled=""/g) ?? []).length, 5);
  assert.equal((markup.match(/Submitted for approval/g) ?? []).length, 2);
  assert.match(markup, /source \(1\)/);
});

test("a standalone draft does not render an empty folder list", () => {
  assert.equal(renderToStaticMarkup(<ProjectFolderList draft={emptyProjectFolderDraft()} onRemove={noop} onPrimaryChange={noop} />), "");
});
