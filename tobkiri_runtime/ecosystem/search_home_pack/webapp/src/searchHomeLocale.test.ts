import assert from "node:assert/strict";
import test from "node:test";

import type { SearchHomeModel } from "./api";
import {
  SEARCH_HOME_ACTIONS,
  searchHomeCopy,
  searchHomeModelLabel,
  searchHomeModelLabelForReference,
  searchHomeModelStatus,
  searchHomeProviderLabel,
} from "./searchHomeLocale";

const model: SearchHomeModel = {
  profile_id: "internal-profile-123",
  qualified_model_id: "provider/internal-profile-123:model-private-id",
  model_id: "gpt-5-mini",
  label: "高速モデル",
  provider_id: "openai-internal",
  provider_display_name: "OpenAI",
  configured: true,
};

test("Japanese task labels do not expose router or node implementation jargon", () => {
  const visibleActionCopy = SEARCH_HOME_ACTIONS
    .flatMap((action) => [action.title, action.subtitle("調べたいこと")])
    .join("\n");

  assert.doesNotMatch(
    visibleActionCopy,
    /Smart Resolve|AI Answer|Open Best URL|defaultspack|node|route/i,
  );
  assert.deepEqual(
    SEARCH_HOME_ACTIONS.map((action) => action.title),
    ["おすすめで探す", "AIに質問", "Googleで検索", "候補サイトを確認"],
  );
});

test("friendly model and provider names stay primary while raw ids need disclosure", () => {
  assert.equal(searchHomeModelLabel(model), "高速モデル");
  assert.equal(searchHomeProviderLabel(model), "OpenAI");
  assert.equal(searchHomeModelStatus(model), "利用可能");
  assert.equal(
    searchHomeModelLabelForReference([model], model.qualified_model_id ?? ""),
    "高速モデル",
  );
  assert.equal(
    searchHomeModelLabelForReference([], model.profile_id),
    searchHomeCopy.model.selectedFallback,
  );
});

test("raw profile identifiers never become a friendly-name fallback", () => {
  const unnamed: SearchHomeModel = {
    profile_id: "private-profile-id",
    qualified_model_id: "private/qualified-id",
    provider_id: "",
  };

  assert.equal(searchHomeModelLabel(unnamed), searchHomeCopy.model.unknownLabel);
  assert.equal(searchHomeProviderLabel(unnamed), searchHomeCopy.model.unknownProvider);
  assert.equal(searchHomeModelStatus(unnamed), "利用状況を確認");
});

test("legacy product names are normalized only in user-facing model copy", () => {
  const legacy: SearchHomeModel = {
    profile_id: "rumi-meta-profile",
    label: "Rumi Meta Provider / Rumi Auto",
    provider_display_name: "Rumi Meta Provider",
  };

  assert.equal(searchHomeModelLabel(legacy), "Tobkiri Meta Provider / Tobkiri Auto");
  assert.equal(searchHomeProviderLabel(legacy), "Tobkiri Meta Provider");
  assert.equal(legacy.profile_id, "rumi-meta-profile");
});
