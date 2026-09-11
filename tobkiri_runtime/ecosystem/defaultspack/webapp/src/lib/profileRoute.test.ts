import assert from "node:assert/strict";
import test from "node:test";

import {
  applicationPathname,
  parseProfileScreenPath,
  profileScreenPath,
  profileScreenUrlFromLocation,
} from "./profileRoute";

test("profile screen paths preserve persistent IDs and declared routes", () => {
  assert.equal(profileScreenPath("profile-a", "/coding"), "/p/profile-a/coding");
  assert.equal(profileScreenPath("利用者", "/chat"), "/p/%E5%88%A9%E7%94%A8%E8%80%85/chat");
  assert.equal(profileScreenPath("profile!'()*", "/chat"), "/p/profile%21%27%28%29%2A/chat");
  assert.deepEqual(parseProfileScreenPath("/p/profile-a/coding"), {
    profileId: "profile-a",
    applicationRoute: "/coding",
  });
  assert.deepEqual(parseProfileScreenPath("/p/profile-a"), {
    profileId: "profile-a",
    applicationRoute: null,
  });
  assert.equal(applicationPathname("/p/profile-a/chat"), "/chat");
  assert.equal(
    profileScreenUrlFromLocation("/coding?chat=one#panel", "https://example.test/p/profile-a/chat"),
    "/p/profile-a/coding?chat=one#panel",
  );
});

test("unqualified, malformed, traversal and noncanonical paths fail closed", () => {
  for (const path of [
    "/chat",
    "/p/",
    "/p/profile%2Fa/chat",
    "/p/profile%2Da/chat",
    "/p/profile%2da/chat",
    "/p/profile%ZZ/chat",
    "/p/profile-a/../chat",
    "/p/profile-a/chat/",
    "/p/profile-a//chat",
  ]) {
    assert.equal(parseProfileScreenPath(path), null, path);
  }
  assert.throws(() => profileScreenPath("../other", "/chat"));
  assert.throws(() => profileScreenPath("界".repeat(43), "/chat"));
  assert.throws(() => profileScreenPath("profile-a", "/chat?code=x"));
});
