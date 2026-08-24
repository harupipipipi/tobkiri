import assert from "node:assert/strict";
import test from "node:test";

import {
  backendConnectionCopy,
  backendConnectionStateAfterHealthCheck,
  formatLastHealthyLabel,
} from "./backendConnection";

test("connection health transitions online to degraded, offline, and recovered", () => {
  const lastHealthyAt = Date.UTC(2026, 7, 24, 3, 4);

  assert.equal(
    backendConnectionStateAfterHealthCheck(true, lastHealthyAt, 0),
    "online",
  );
  assert.equal(
    backendConnectionStateAfterHealthCheck(false, lastHealthyAt, 1),
    "degraded",
  );
  assert.equal(
    backendConnectionStateAfterHealthCheck(false, lastHealthyAt, 2),
    "degraded",
  );
  assert.equal(
    backendConnectionStateAfterHealthCheck(false, lastHealthyAt, 3),
    "offline",
  );
  assert.equal(
    backendConnectionStateAfterHealthCheck(true, lastHealthyAt, 0),
    "online",
  );
  assert.equal(
    backendConnectionStateAfterHealthCheck(false, null, 1),
    "offline",
  );
});

test("degraded copy identifies local state and a pending send", () => {
  const copy = backendConnectionCopy("degraded", Date.now(), "send", "ja");

  assert.equal(copy.title, "再接続中");
  assert.match(copy.detail, /送信結果を確認中/);
  assert.match(copy.detail, /同じ内容を再送信しないでください/);
  assert.match(copy.detail, /ローカル表示/);
  assert.match(copy.detail, /サーバーでの確定は未確認/);
  assert.equal(copy.actionLabel, "接続を確認");
  assert.doesNotMatch(copy.title + copy.detail, /backend/i);
});

test("offline copy distinguishes on-screen draft and local view from server state", () => {
  const copy = backendConnectionCopy("offline", null, null, "ja");

  assert.equal(copy.title, "オフライン");
  assert.match(copy.detail, /新しい送信はキューに保存されません/);
  assert.match(copy.detail, /入力中の下書きは画面に残ります/);
  assert.match(copy.detail, /キャッシュ/);
  assert.match(copy.detail, /未確認内容/);
});

test("connection copy and last healthy time follow the shared locale", () => {
  const timestamp = Date.UTC(2026, 7, 24, 3, 4);
  const formatted = formatLastHealthyLabel(timestamp, "en");
  const copy = backendConnectionCopy("degraded", timestamp, null, "en");

  assert.equal(copy.title, "Reconnecting");
  assert.ok(formatted);
  assert.match(copy.detail, new RegExp(formatted.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(copy.detail, /cached or locally unconfirmed content/);
  assert.match(copy.detail, /locally unconfirmed content/);
  assert.equal(copy.actionLabel, "Check connection");
});

test("recovered copy does not overstate cached content freshness", () => {
  const copy = backendConnectionCopy("online", Date.now(), null, "en");

  assert.equal(copy.title, "Connected");
  assert.match(copy.detail, /connection has recovered/);
  assert.match(copy.detail, /start a new send/);
  assert.match(copy.detail, /may still include cached content/);
  assert.doesNotMatch(copy.detail, /content is current/);
});

test("recovered copy keeps unresolved sends and approvals distinct", () => {
  const send = backendConnectionCopy("online", Date.now(), "send", "en");
  const approval = backendConnectionCopy("online", Date.now(), "approval", "en");

  assert.match(send.detail, /previous send result is still being confirmed/);
  assert.match(send.detail, /Do not send the same content again/);
  assert.match(approval.detail, /approved action result is still being confirmed/);
  assert.match(approval.detail, /Do not run the same action again/);
  assert.doesNotMatch(approval.detail, /send result/);
});
