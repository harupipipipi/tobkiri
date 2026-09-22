import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import {
  ModelSearchPicker,
  modelPickerShouldExpandResults,
  modelPickerResultMessage,
  nextModelOptionIndex,
  reconcileActiveOptionIndex,
} from "./ModelSearchPicker";
import { parseModelSelectorSchema } from "./modelSelectorSchema";

test("combobox navigation clamps arrows, home, end, and page movement", () => {
  assert.equal(nextModelOptionIndex(-1, 20, "ArrowDown"), 0);
  assert.equal(nextModelOptionIndex(-1, 20, "ArrowUp"), 19);
  assert.equal(nextModelOptionIndex(0, 20, "ArrowUp"), 0);
  assert.equal(nextModelOptionIndex(19, 20, "ArrowDown"), 19);
  assert.equal(nextModelOptionIndex(8, 20, "Home"), 0);
  assert.equal(nextModelOptionIndex(8, 20, "End"), 19);
  assert.equal(nextModelOptionIndex(4, 20, "PageDown"), 14);
  assert.equal(nextModelOptionIndex(14, 20, "PageUp"), 4);
});

test("combobox navigation fails closed for an empty option list", () => {
  assert.equal(nextModelOptionIndex(0, 0, "ArrowDown"), -1);
});

test("keyboard navigation reveals truncated results before focus reaches a dead end", () => {
  assert.equal(modelPickerShouldExpandResults(1, 2, 20, "ArrowDown"), true);
  assert.equal(modelPickerShouldExpandResults(0, 2, 20, "ArrowDown"), false);
  assert.equal(modelPickerShouldExpandResults(0, 2, 20, "End"), true);
  assert.equal(modelPickerShouldExpandResults(0, 2, 20, "PageDown"), true);
  assert.equal(modelPickerShouldExpandResults(0, 2, 2, "End"), false);
});

test("async result replacement preserves active identity before falling back", () => {
  assert.equal(reconcileActiveOptionIndex({
    keys: ["model:c", "model:a", "model:b"],
    activeKey: "model:b",
    selectedKey: "model:a",
    current: 1,
  }), 2);
  assert.equal(reconcileActiveOptionIndex({
    keys: ["model:c", "model:a"],
    activeKey: "model:removed",
    selectedKey: "model:a",
    current: 7,
  }), 1);
  assert.equal(reconcileActiveOptionIndex({
    keys: [],
    activeKey: "model:removed",
    current: 0,
  }), -1);
});

test("result announcements cover loading, failures, providers, remote provenance, and limits", () => {
  assert.equal(modelPickerResultMessage({
    total: 75, visible: 5, remote: 12, loading: false, error: "",
  }), "75 model results. 12 from remote search. Showing 5.");
  assert.equal(modelPickerResultMessage({
    total: 2, visible: 2, remote: 0, loading: false, error: "", providers: true,
  }), "2 provider results.");
  assert.equal(modelPickerResultMessage({
    total: 0, visible: 0, remote: 0, loading: true, error: "",
  }), "Loading model results.");
  assert.equal(modelPickerResultMessage({
    total: 0, visible: 0, remote: 0, loading: false, error: "offline",
  }), "Model search failed. offline");
});

test("open picker exposes one combobox/listbox contract, selection, count, and continuation", () => {
  const markup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "provider/model-2",
    query: "",
    open: true,
    maxVisibleOptions: 2,
    options: [1, 2, 3].map((index) => ({
      value: `provider/model-${index}`,
      label: `Long model name ${index}`,
      provider_id: "provider",
      model_id: `model-${index}`,
    })),
    onChange: () => undefined,
    onQueryChange: () => undefined,
  }));

  assert.match(markup, /role="combobox"/);
  assert.match(markup, /aria-autocomplete="list"/);
  const controlledId = markup.match(/role="combobox"[^>]+aria-controls="([^"]+)"/)?.[1];
  assert(controlledId);
  assert.match(markup, new RegExp(`id="${controlledId}" role="listbox"`));
  assert.match(markup, /role="option" aria-selected="true"/);
  assert.match(markup, /3 model results\. Showing 2\./);
  assert.match(markup, /Show all 3 results/);
  assert.match(markup, /Alt\+Backspace clears the query/);
  assert.doesNotMatch(markup, /<button[^>]+role="option"/);
  assert.match(markup, /<\/div><button type="button" class="m-1[^>]*>Show all 3 results<\/button>/);
});

test("compact trigger keeps its selected-model description in the accessibility tree", () => {
  const markup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "provider/model-1",
    query: "",
    variant: "compact",
    options: [{ value: "provider/model-1", label: "Model 1", provider_id: "provider", model_id: "model-1" }],
    onChange: () => undefined,
    onQueryChange: () => undefined,
  }));
  const describedBy = markup.match(/aria-describedby="([^"]+)"/)?.[1];
  assert(describedBy);
  assert.match(markup, new RegExp(`id="${describedBy}" class="sr-only"`));
  assert.match(markup, /provider \/ model-1/);
});

test("clear and remote actions expose keyboard equivalents without adding option tab stops", () => {
  const markup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "provider/model-1",
    query: "model",
    open: true,
    clearLabel: "Clear selected model",
    options: [{ value: "provider/model-1", label: "Model 1", provider_id: "provider" }],
    onChange: () => undefined,
    onQueryChange: () => undefined,
    onSearch: () => undefined,
  }));

  assert.match(markup, /aria-keyshortcuts="Alt\+Backspace"/);
  assert.match(markup, /aria-keyshortcuts="Alt\+Delete"/);
  assert.match(markup, /aria-keyshortcuts="Control\+Enter Meta\+Enter"/);
  assert.match(markup, /Alt\+Delete clears the selected model/);
  assert.doesNotMatch(markup, /role="option"[^>]+tabindex/);
});

test("provider query uses the same listbox contract and announces its count", () => {
  const markup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "",
    query: "@open",
    open: true,
    options: [
      { value: "openai/gpt", label: "GPT", provider_id: "openai" },
      { value: "openrouter/sonnet", label: "Sonnet", provider_id: "openrouter" },
    ],
    onChange: () => undefined,
    onQueryChange: () => undefined,
  }));

  assert.match(markup, /aria-label="Provider results"/);
  assert.match(markup, /2 provider results\./);
  assert.match(markup, /role="option" aria-selected="false"/);
  assert.doesNotMatch(markup, /role="option" aria-selected="true"/);
  assert.match(markup, /@openai/);
  assert.match(markup, /@openrouter/);
});

test("selector policy, no-search mode, and schema result limit remain authoritative", () => {
  const selectorSchema = parseModelSelectorSchema({
    layout: { show_search: false, max_visible_options: 1 },
    filters: { exclude_provider_ids: ["blocked"] },
  });
  const markup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "allowed/one",
    query: "",
    open: true,
    selectorSchema,
    options: [
      { value: "allowed/one", label: "Allowed One", provider_id: "allowed" },
      { value: "allowed/two", label: "Allowed Two", provider_id: "allowed" },
      { value: "blocked/hidden", label: "Hidden", provider_id: "blocked" },
    ],
    onChange: () => undefined,
    onQueryChange: () => undefined,
  }));

  assert.doesNotMatch(markup, /role="combobox"/);
  assert.match(markup, /role="listbox" tabindex="0"/);
  assert.match(markup, /2 model results\. Showing 1\./);
  assert.match(markup, /Show all 2 results/);
  assert.doesNotMatch(markup, /Hidden/);
});

test("remote, unavailable, and unconfigured model provenance is semantic", () => {
  const markup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "",
    query: "remote",
    open: true,
    options: [{
      value: "local/unconfigured",
      label: "Local Unconfigured",
      provider_id: "local",
      requires_api_key: true,
      api_key_configured: false,
    }],
    remoteResults: [{
      profile_id: "remote/unavailable",
      display_name: "Remote Unavailable Model With A Very Long Name",
      provider_id: "remote",
      availability: { status: "unavailable", reason: "Unsupported here" },
    }],
    onChange: () => undefined,
    onQueryChange: () => undefined,
  }));

  assert.match(markup, /1 from remote search\./);
  assert.match(markup, /Unavailable\. Unsupported here Remote search result\./);
  assert.match(markup, />remote<\/span>/);
});

test("loading, errors, and empty results expose live regions", () => {
  const loadingMarkup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "", query: "none", open: true, loading: true, options: [],
    onChange: () => undefined, onQueryChange: () => undefined,
  }));
  assert.match(loadingMarkup, /role="status" aria-live="polite"/);
  assert.match(loadingMarkup, /aria-busy="true"/);
  assert.match(loadingMarkup, /Loading model results\./);
  assert.match(loadingMarkup, /一致するモデルがありません。/);

  const errorMarkup = renderToStaticMarkup(createElement(ModelSearchPicker, {
    value: "", query: "none", open: true, error: "Remote unavailable", options: [],
    onChange: () => undefined, onQueryChange: () => undefined,
  }));
  assert.match(errorMarkup, /role="alert"/);
  assert.match(errorMarkup, /Model search failed\. Remote unavailable/);
});
