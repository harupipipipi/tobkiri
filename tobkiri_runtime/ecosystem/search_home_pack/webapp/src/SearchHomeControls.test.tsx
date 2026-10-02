import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { SearchHomeControls } from "./SearchHomeControls";

const props = {
  input: "",
  models: [{ profile_id: "local/model-1b", label: "Model 1B", local: true, configured: true }],
  selectedModel: "local/model-1b",
  selectedActionIndex: 0,
  loading: false,
  answerLoading: false,
  onInputChange: () => undefined,
  onSelectModel: () => undefined,
  onSelectedActionIndexChange: () => undefined,
  onExecute: () => undefined,
};

test("search-first controls render shared Defaults provenance and no unsupported attachments", () => {
  const html = renderToStaticMarkup(<SearchHomeControls {...props} />);
  assert.match(html, /Tobkiri Search/);
  assert.match(html, /Tobkiri Defaultsと共有/);
  assert.match(html, /Web検索はGoogleで/);
  assert.match(html, /Model 1B/);
  assert.match(html, /role="combobox"/);
  assert.match(html, /aria-label="検索"/);
  assert.doesNotMatch(html, /type="file"|Attach file|ファイルを添付/);
});

test("typed input has keyboard selection and truthful AI versus web search actions", () => {
  const html = renderToStaticMarkup(<SearchHomeControls {...props} input="今日のニュース" selectedActionIndex={1} />);
  assert.match(html, /aria-activedescendant="search-action-answer"/);
  assert.match(html, /id="search-action-answer"/);
  assert.match(html, /Googleで検索/);
  assert.match(html, /AIに質問/);
  assert.match(html, /モデルの知識で回答します/);
  assert.match(html, /サイト・URLを開く/);
});

test("loading disables submission and shows visible model loading/saving status", () => {
  const busy = renderToStaticMarkup(<SearchHomeControls {...props} input="質問" answerLoading modelsLoading />);
  assert.match(busy, /aria-busy="true"/);
  assert.match(busy, /モデルを確認中/);
  assert.match(busy, /class="submit-button"[^>]*disabled/);
  const saving = renderToStaticMarkup(<SearchHomeControls {...props} modelSaving />);
  assert.match(saving, /共有設定に保存中/);
  assert.match(saving, /class="model-trigger"[^>]*disabled/);
});
