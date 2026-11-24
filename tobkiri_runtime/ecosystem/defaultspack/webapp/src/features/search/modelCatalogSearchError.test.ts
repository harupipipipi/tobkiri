import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  MODEL_CATALOG_SEARCH_FAILED,
  modelCatalogSearchError,
} from "./modelCatalogSearchError";

describe("model catalog search error copy", () => {
  it("maps a strict leading HTTP status to fixed safe copy", () => {
    assert.match(modelCatalogSearchError(new Error("HTTP 403 Forbidden\nsecret")), /権限/);
    assert.match(modelCatalogSearchError(new Error("HTTP 404 Not Found\nsecret")), /利用できません/);
    assert.match(modelCatalogSearchError(new Error("HTTP 429 Too Many Requests")), /少し待って/);
    assert.match(modelCatalogSearchError(new Error("HTTP 503 Service Unavailable")), /しばらくして/);
  });

  it("does not expose backend details or parse statuses outside the leading label", () => {
    const detail = "HTTP 403 Forbidden\ncredential-secret https://private.example";
    const copy = modelCatalogSearchError(new Error(detail));
    assert.equal(copy.includes("credential-secret"), false);
    assert.equal(copy.includes("private.example"), false);
    assert.equal(modelCatalogSearchError(new Error("request failed: HTTP 403 Forbidden")), MODEL_CATALOG_SEARCH_FAILED);
    assert.equal(modelCatalogSearchError(new Error("HTTP 4030 Forbidden")), MODEL_CATALOG_SEARCH_FAILED);
    assert.equal(modelCatalogSearchError(new Error("defaultspack API error (FORBIDDEN)")), MODEL_CATALOG_SEARCH_FAILED);
  });

  it("uses fixed local-connection copy for a fetch TypeError and generic copy otherwise", () => {
    const localCopy = modelCatalogSearchError(new TypeError("Failed to fetch https://secret.example"));
    assert.match(localCopy, /Tobkiri Launcherとの接続/);
    assert.equal(localCopy.includes("secret.example"), false);
    assert.equal(modelCatalogSearchError(new Error("unknown failure")), MODEL_CATALOG_SEARCH_FAILED);
    assert.equal(modelCatalogSearchError({ message: "HTTP 403 Forbidden" }), MODEL_CATALOG_SEARCH_FAILED);
  });
});
