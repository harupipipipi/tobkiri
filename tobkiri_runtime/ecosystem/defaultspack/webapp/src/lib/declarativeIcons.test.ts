import assert from "node:assert/strict";
import test from "node:test";

import { BellRing } from "lucide-react";

import {
  declarativeIconForName,
  normalizeDeclarativeIconName,
} from "./declarativeIcons";

test("declarative notification icon aliases resolve to the allowlisted bell", () => {
  for (const alias of [
    "notification",
    "notifications",
    "notify",
    "bell",
    "bell-ring",
    "bell_ring",
    " Bell Ring ",
    "NOTIFICATION",
  ]) {
    assert.equal(declarativeIconForName(alias), BellRing, alias);
  }
});

test("declarative icon normalization is bounded and unknown values use fallback", () => {
  assert.equal(normalizeDeclarativeIconName("  Bell__Ring  "), "bell-ring");
  assert.equal(declarativeIconForName("unknown-pack-icon"), null);
  assert.equal(declarativeIconForName("<svg onload=alert(1)>"), null);
  assert.equal(declarativeIconForName(null), null);
});
