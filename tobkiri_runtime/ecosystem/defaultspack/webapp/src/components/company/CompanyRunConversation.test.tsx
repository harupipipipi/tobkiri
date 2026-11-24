import assert from "node:assert/strict";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import { CompanyRunConversation } from "./CompanyRunConversation";

test("historical company errors are copyable without announcing every item on load", () => {
  const markup = renderToStaticMarkup(
    <CompanyRunConversation
      messages={[
        { role: "error", label: "Agent error", content: "Past run failed", is_error: true },
        { role: "assistant", label: "Agent reply", content: "Already completed" },
      ]}
    />,
  );

  assert.match(markup, /data-error-notice="error"/);
  assert.match(markup, /aria-label="Agent errorをコピー"/);
  assert.match(markup, /data-copy-icon=""/);
  assert.doesNotMatch(markup, /role="alert"|aria-live="assertive"/);
  assert.match(markup, /role="status" aria-live="polite"/);
});
