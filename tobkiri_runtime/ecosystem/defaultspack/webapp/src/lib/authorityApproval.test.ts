import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AuthorityApprovalWindow } from "../components/AuthorityApprovalWindow";

import {
  authorityApprovalConfig,
  authorityApprovalRiskTone,
  authorityApprovalRuntimeContent,
  authorityApprovalSettledLabel,
  authorityApprovalShouldRetryWithFreshContext,
  authorityApprovalTitle,
  authorityRelatedPermissions,
  authorityRequestSettledStatus,
  pendingAuthorityApproval,
  resolvePendingAuthorityApproval,
} from "./authorityApproval";
import {
  browserAuthorityApprovalPath,
  browserApprovalTokenizedPath,
} from "./authorityApprovalBrowserToken";
import "./authorityApprovalBrowserSecurity.test";

const SRC_ROOT = resolve(import.meta.dirname, "..");

test("approval window announces loading and prevents overlapping refreshes", () => {
  const markup = renderToStaticMarkup(createElement(AuthorityApprovalWindow));
  assert.match(markup, /role="status"/);
  assert.match(markup, /承認リクエストを読み込み中/);
  assert.match(markup, /<button[^>]*disabled=""[^>]*aria-label="承認リクエストを再読み込み"/);
});
const RISKY_AUTHORITY_FOLLOWUP_PHRASES = [
  "Thank you for granting",
  "approved provider",
  "approved model",
  "I can now use",
  "使用を許可しました",
];

function authorityApprovalWindowSource(): string {
  return readFileSync(resolve(SRC_ROOT, "components", "AuthorityApprovalWindow.tsx"), "utf8");
}

function assertNoRiskyAuthorityFollowupPhrases(text: string): void {
  for (const phrase of RISKY_AUTHORITY_FOLLOWUP_PHRASES) {
    assert.equal(text.includes(phrase), false, `unexpected risky phrase: ${phrase}`);
  }
}

function runtimeMetadataJson(content: string): Record<string, unknown> {
  const marker = "Resume metadata JSON:\n";
  const start = content.indexOf(marker);
  assert.notEqual(start, -1);
  return JSON.parse(content.slice(start + marker.length));
}

test("authority approval title describes app provider key and endpoint without duplicating provider", () => {
  const title = authorityApprovalTitle({
    permissionId: "model.invoke",
    resource: {
      app_display_name: "defaultspack v2",
      provider_display_name: "OpenCode Go provider",
      model_display_name: "DeepSeek V4 Pro via OpenCode Go",
      credential_label: "OpenCode Go API key",
      endpoint_url: "https://opencode.ai/zen/go/v1/chat/completions",
    },
  });

  assert.equal(
    title,
    "defaultspack v2 / OpenCode Go provider に OpenCode Go API key の使用と https://opencode.ai/zen/go/v1/chat/completions へのアクセスを許可しますか？",
  );
  assert.equal(title.includes("provider provider"), false);
});

test("authority approval runtime content silently resumes without risky chatter", () => {
  const content = authorityApprovalRuntimeContent({
    requestId: "approval-1",
    principalId: "local-user",
    permissionId: "model.invoke",
    resource: {
      provider_id: "opencode-go",
      model_id: "deepseek-v4-pro",
      endpoint_url: "https://opencode.ai/zen/go/v1/chat/completions",
    },
  }, "token-1");

  assertNoRiskyAuthorityFollowupPhrases(content);
  assert.match(content, /Silent internal resume/);
  assert.match(content, /Continue the interrupted\/original user request/i);
  assert.match(content, /never mention approval, authority, API keys, providers, model access/i);
  assert.match(content, /do not thank the user for permission/i);
  assert.match(content, /Request id: approval-1/);
  assert.match(content, /Permission id: model\.invoke/);

  const metadata = runtimeMetadataJson(content);
  assert.deepEqual(metadata, {
    request_id: "approval-1",
    permission_id: "model.invoke",
    resource: {
      provider_id: "opencode-go",
      model_id: "deepseek-v4-pro",
      endpoint_url: "https://opencode.ai/zen/go/v1/chat/completions",
    },
    approval_token: "token-1",
  });
});

test("host authority runtime content keeps the same no-mention no-thanks guardrail", () => {
  const content = authorityApprovalRuntimeContent({
    requestId: "host-approval-1",
    principalId: "local-user",
    permissionId: "host.process.open_url",
    resource: {
      operation: "host.process.open_url.preview",
      caller_pack_id: "defaultspack",
    },
  }, "host-token-1");

  assertNoRiskyAuthorityFollowupPhrases(content);
  assert.match(content, /Retry the same host operation once/);
  assert.match(content, /never mention approval, authority, API keys, providers, model access/i);
  assert.match(content, /do not thank the user for permission/i);

  const metadata = runtimeMetadataJson(content);
  assert.equal(metadata.request_id, "host-approval-1");
  assert.equal(metadata.permission_id, "host.process.open_url");
  assert.equal(metadata.approval_token, "host-token-1");
  assert.deepEqual(metadata.resource, {
    operation: "host.process.open_url.preview",
    caller_pack_id: "defaultspack",
  });
});

test("interactive approval window is tokenless and uses the dedicated resource", () => {
  const source = authorityApprovalWindowSource();
  const resourceSource = readFileSync(resolve(SRC_ROOT, "features", "chat", "resources", "authorityApprovalResources.ts"), "utf8");

  assert.match(source, /interactiveApprovalResources\.get\(requestId\)/);
  assert.match(source, /interactiveApprovalResources\.approve\(requestId, \{[\s\S]*confirmation_text:[\s\S]*ui_operator:/);
  assert.match(source, /interactiveApprovalResources\.deny\(requestId, \{[\s\S]*ui_operator:/);
  assert.doesNotMatch(source, /sendAuthorityResume|authorityApprovalRuntimeContent|decision\.token|request\.resource|selectedScope/);
  assert.doesNotMatch(resourceSource, /sendMessage|approveAuthorityApproval|denyAuthorityApproval|AuthorityApprovalDecision/);
});

test("interactive approval errors keep a semantic alert icon separate from one stable copy action", () => {
  const source = authorityApprovalWindowSource();
  const noticeSource = readFileSync(resolve(SRC_ROOT, "components", "ErrorNotice.tsx"), "utf8");

  assert.match(source, /import \{ ErrorNotice \} from "\.\/ErrorNotice"/);
  assert.match(source, /function ApprovalError\([\s\S]*errorIcon="approval"/);
  assert.match(source, /copyLabel="承認エラーをコピー"/);
  assert.match(noticeSource, /noticeRole = severity === "warning" \? "status" : "alert"/);
  assert.match(noticeSource, /liveMode = severity === "warning" \? "polite" : "assertive"/);
  assert.match(noticeSource, /CircleAlert/);
  assert.match(noticeSource, /<Copy aria-hidden="true" data-copy-icon="" size=\{14\} \/>/);
  assert.match(source, /<ApprovalError message="request_id が見つかりません。" \/>/);
  assert.match(source, /<ApprovalError message="この承認に必要な確認情報を取得できませんでした。安全のため操作できません。" \/>/);
  assert.doesNotMatch(noticeSource, /<Check\b/);
});

test("interactive approval API exposes only the redacted projection and exact decision bodies", () => {
  const apiSource = readFileSync(resolve(SRC_ROOT, "lib", "api.ts"), "utf8");
  const typeStart = apiSource.indexOf("export type InteractiveApprovalRequest = {");
  const typeEnd = apiSource.indexOf("\n};", typeStart);
  const typeSource = apiSource.slice(typeStart, typeEnd + 3);

  assert.match(typeSource, /request_id: string;/);
  assert.match(typeSource, /request_snapshot_digest: string;/);
  assert.match(typeSource, /state: string;/);
  assert.match(typeSource, /expires_at: number;/);
  assert.match(typeSource, /typed_confirmation_required: boolean;/);
  assert.match(typeSource, /typed_confirmation_digest: string \| null;/);
  assert.match(typeSource, /redacted_metadata: Record<string, string>;/);
  assert.doesNotMatch(typeSource, /token|grant|receipt|scope|resource|config|approval_id/i);
  assert.match(apiSource, /defaultspackContractRoute\("api\/interactive-approval\/v1\/list"\)/);
  assert.match(apiSource, /defaultspackContractRoute\("api\/interactive-approval\/v1\/get"\)[\s\S]*body: JSON\.stringify\(\{ request_id: requestId \}\)/);
  assert.match(apiSource, /defaultspackContractRoute\("api\/interactive-approval\/v1\/approve"\)[\s\S]*request_id: requestId,[\s\S]*confirmation_text: options\.confirmation_text,[\s\S]*ui_operator: options\.ui_operator/);
  assert.match(apiSource, /defaultspackContractRoute\("api\/interactive-approval\/v1\/deny"\)[\s\S]*request_id: requestId,[\s\S]*ui_operator: options\.ui_operator/);
  assert.match(authorityApprovalWindowSource(), /getAuthorityApprovalContext\(requestId, \{[\s\S]*decision: "approve",[\s\S]*requestSnapshotDigest: current\.request_snapshot_digest,[\s\S]*typedConfirmationDigest: current\.typed_confirmation_digest/);
  assert.match(authorityApprovalWindowSource(), /getAuthorityApprovalContext\(requestId, \{[\s\S]*decision: "deny",[\s\S]*requestSnapshotDigest: current\.request_snapshot_digest,[\s\S]*typedConfirmationDigest: null/);
});

test("pending authority approval detects persisted assistant metadata", () => {
  const approval = pendingAuthorityApproval([
    {
      id: "u1",
      role: "user",
      content: [{ type: "text", text: "hello" }],
      rawText: "hello",
    },
    {
      id: "a1",
      role: "agent",
      content: [{ type: "text", text: "モデル/API の使用許可が必要です。承認後に続行します。" }],
      rawText: "モデル/API の使用許可が必要です。承認後に続行します。",
      metadata: {
        pendingAuthorityApproval: {
          request_id: "auth-metadata-1",
          principal_id: "local-user",
          permission_id: "model.invoke",
          resource: { provider_id: "opencode-go" },
        },
      },
    },
  ]);

  assert.equal(approval?.requestId, "auth-metadata-1");
  assert.equal(approval?.permissionId, "model.invoke");
  assert.deepEqual(approval?.resource, { provider_id: "opencode-go" });
});

test("pending authority resolver replaces stale conversation metadata with matching live request", () => {
  const resolved = resolvePendingAuthorityApproval(
    {
      requestId: "auth_old_approved",
      principalId: "profile:default-profile__graph:defaultspack.startup",
      permissionId: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        api_id: "legacy",
        model_id: "deepseek-v4-flash",
        model_ref: "opencode-go/deepseek-v4-flash",
        pack_id: "defaultspack",
      },
    },
    [{
      request_id: "auth_live_pending",
      status: "pending",
      principal_id: "defaultspack",
      permission_id: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        api_id: "legacy",
        model_id: "deepseek-v4-flash",
        model_ref: "opencode-go/deepseek-v4-flash",
        pack_id: "defaultspack",
      },
      risk_level: "medium",
      reason: "use provider",
    }],
  );

  assert.equal(resolved?.requestId, "auth_live_pending");
  assert.equal(resolved?.principalId, "defaultspack");
});

test("scoped pending authority resolver does not borrow a grant match from another conversation", () => {
  const resolved = resolvePendingAuthorityApproval(
    {
      requestId: "auth_stale_conv_a",
      conversationId: "conversation-a",
      principalId: "profile:default-profile__graph:defaultspack.startup",
      permissionId: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        api_id: "legacy",
        model_id: "deepseek-v4-flash",
        model_ref: "opencode-go/deepseek-v4-flash",
        pack_id: "defaultspack",
      },
    },
    [{
      request_id: "auth_pending_conv_b",
      status: "pending",
      conversation_id: "conversation-b",
      principal_id: "profile:default-profile__graph:defaultspack.startup",
      permission_id: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        api_id: "legacy",
        model_id: "deepseek-v4-flash",
        model_ref: "opencode-go/deepseek-v4-flash",
        pack_id: "defaultspack",
      },
    }],
    {
      conversationId: "conversation-a",
      requireConversationMatch: true,
      requirePrincipalMatch: true,
    },
  );

  assert.equal(resolved, null);
});

test("scoped pending authority resolver keeps grant fallback within the same conversation and principal", () => {
  const resolved = resolvePendingAuthorityApproval(
    {
      requestId: "auth_stale_conv_a",
      conversationId: "conversation-a",
      principalId: "conversation:conversation-a",
      permissionId: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        model_id: "deepseek-v4-flash",
      },
    },
    [{
      request_id: "auth_pending_conv_a",
      status: "pending",
      conversation_id: "conversation-a",
      principal_id: "conversation:conversation-a",
      permission_id: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        model_id: "deepseek-v4-flash",
      },
    }],
    {
      conversationId: "conversation-a",
      requireConversationMatch: true,
      requirePrincipalMatch: true,
    },
  );

  assert.equal(resolved?.requestId, "auth_pending_conv_a");
  assert.equal(resolved?.conversationId, "conversation-a");
  assert.equal(resolved?.principalId, "conversation:conversation-a");
});

test("scoped pending authority resolver still accepts an exact request id match", () => {
  const resolved = resolvePendingAuthorityApproval(
    {
      requestId: "auth_exact_pending",
      conversationId: "conversation-a",
      principalId: "conversation:conversation-a",
      permissionId: "model.invoke",
      resource: { provider_id: "opencode-go" },
    },
    [{
      request_id: "auth_exact_pending",
      status: "pending",
      conversation_id: "conversation-b",
      principal_id: "conversation:conversation-b",
      permission_id: "model.invoke",
      resource: { provider_id: "opencode-go" },
    }],
    {
      conversationId: "conversation-a",
      requireConversationMatch: true,
      requirePrincipalMatch: true,
    },
  );

  assert.equal(resolved?.requestId, "auth_exact_pending");
  assert.equal(resolved?.conversationId, "conversation-b");
});

test("pending authority resolver does not expose stale metadata when no live request matches", () => {
  const resolved = resolvePendingAuthorityApproval(
    {
      requestId: "auth_old_approved",
      principalId: "profile:default-profile__graph:defaultspack.startup",
      permissionId: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        model_id: "deepseek-v4-flash",
      },
    },
    [{
      request_id: "auth_other_pending",
      status: "pending",
      principal_id: "defaultspack",
      permission_id: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        model_id: "qwen3.7-plus",
      },
    }],
  );

  assert.equal(resolved, null);
});

test("authority approval config accumulates distinct host action aliases", () => {
  assert.deepEqual(
    authorityApprovalConfig({
      permissionId: "host.process.open_url",
      resource: {
        host_action: "host.process.open_url",
        operation: "host.process.open_url.preview",
      },
    }),
    {
      host_actions: ["host.process.open_url", "host.process.open_url.preview"],
    },
  );
});

test("authority approval config dedupes matching host action aliases", () => {
  assert.deepEqual(
    authorityApprovalConfig({
      permissionId: "host.process.open_url",
      resource: {
        host_action: "host.process.open_url",
        operation: "host.process.open_url",
      },
    }),
    {
      host_actions: ["host.process.open_url"],
    },
  );
});

test("authority related permissions helper can identify provider-scoped network approval", () => {
  assert.deepEqual(
    authorityRelatedPermissions({
      permissionId: "model.invoke",
      resource: {
        provider_id: "opencode-go",
        api_id: "legacy",
        model_id: "deepseek-v4-pro",
      },
    }),
    ["api_key.use", "network.egress"],
  );
});

test("authority approval browser helper builds credential-free same-origin paths", () => {
  assert.equal(browserAuthorityApprovalPath("auth 1", "/ambient-debug?authority_approved=1"), "/approval?request_id=auth+1&return_to=%2Fambient-debug%3Fauthority_approved%3D1");
  assert.equal(browserApprovalTokenizedPath("/finger-recording?authority_approved=1"), "/finger-recording?authority_approved=1");
  assert.equal(browserApprovalTokenizedPath("https://external.invalid/fake"), null);
  assert.equal(browserApprovalTokenizedPath("/finger-recording?browser_approval_token=fake"), null);
});

test("interactive approval window never chooses a scope or related permission", () => {
  const source = authorityApprovalWindowSource();

  assert.doesNotMatch(source, /allowed_scopes|related_permissions|selectedScope|authorityApprovalConfig|authorityRelatedPermissions/);
  assert.doesNotMatch(source, /ApprovalDecisionSurface|authorityApprovalViewModel/);
});

test("interactive approval uses the redacted confirmation phrase or fails closed", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /request\?\.redacted_metadata\.confirmation_phrase\?\.trim\(\) \?\? ""/);
  assert.match(source, /const confirmationUnavailable = Boolean\([\s\S]*typed_confirmation_required && !confirmationPhrase/);
  assert.match(source, /confirmationText\.trim\(\) !== confirmationPhrase/);
  assert.match(source, /confirmationUnavailable \? null : !nativeApprovalAvailable/);
  assert.doesNotMatch(source, /typed_confirmation_phrase/);
});

test("authority approval risk tones render critical and high as danger", () => {
  assert.match(authorityApprovalRiskTone("critical"), /red-600/);
  assert.match(authorityApprovalRiskTone("critical"), /ring-red/);
  assert.match(authorityApprovalRiskTone("high"), /red-500/);
  assert.doesNotMatch(authorityApprovalRiskTone("critical"), /sky/);
  assert.doesNotMatch(authorityApprovalRiskTone("high"), /sky/);
});

test("authority request settled status recognizes approved and denied requests", () => {
  assert.equal(authorityRequestSettledStatus("approved"), "approved");
  assert.equal(authorityRequestSettledStatus("denied"), "denied");
  assert.equal(authorityRequestSettledStatus("rejected"), "denied");
  assert.equal(authorityRequestSettledStatus("pending"), null);
  assert.equal(authorityApprovalSettledLabel("approved"), "承認済み");
  assert.equal(authorityApprovalSettledLabel("denied"), "拒否済み");
});

test("authority approval context retry only matches stale native ui_operator failures", () => {
  assert.equal(
    authorityApprovalShouldRetryWithFreshContext(new Error("HTTP 403\n詳細: ui_operator expired")),
    true,
  );
  assert.equal(
    authorityApprovalShouldRetryWithFreshContext("HTTP 403\n詳細: ui_operator source is invalid"),
    true,
  );
  assert.equal(
    authorityApprovalShouldRetryWithFreshContext("HTTP 403\n詳細: ui_operator request mismatch"),
    true,
  );
  assert.equal(
    authorityApprovalShouldRetryWithFreshContext("HTTP 409\n詳細: Authority request is approved"),
    false,
  );
  assert.equal(
    authorityApprovalShouldRetryWithFreshContext("Typed confirmation is required for this host operation"),
    false,
  );
});

test.skip("legacy authority approval window settles already-approved or denied requests on load without pending CTAs", () => {
  const source = authorityApprovalWindowSource();

  assert.match(
    source,
    /const singleSettledStatus = authorityRequestSettledStatus\(single\.status\)[\s\S]*settleAuthorityRequest\(single, singleSettledStatus\)/,
  );
  assert.doesNotMatch(source, /resolvePendingAuthorityApproval\(requestToApproval\(single\), list\.pending \?\? \[\]\)/);
  assert.doesNotMatch(source, /setRequestId\(activePendingApproval\.requestId\)/);
  assert.match(source, /const displayedSettledStatus = decisionSettledStatus \?\? authorityRequestSettledStatus\(request\?\.status\)/);
  assert.match(source, /const approvalContextAvailable = nativeApprovalAvailable/);
  assert.match(source, /const showApprovalControls = Boolean\(request && request\.status === "pending" && approvalContextAvailable && !displayedSettledStatus/);
  assert.match(source, /authorityApprovalSettledLabel\(displayedSettledStatus\)/);
});

test.skip("legacy authority approval window finalizes hidden resume before broadcasting approve settlement", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /function scheduleAuthorityApprovalWindowClose\(fallbackReturnTo = ""\)[\s\S]*closeAuthorityApprovalWindow\(fallbackReturnTo\)/);
  assert.match(source, /const shouldScheduleClose = options\?\.scheduleClose\s*\?\? true/);
  assert.match(source, /scheduleAuthorityApprovalWindowClose\(nativeApprovalAvailableRef\.current \? "" : approvalReturnToFromLocation\(\)\)/);
  assert.match(source, /const decision = await submitApproveOnce\(\);\s*await finalizeApprovedDecision\(request, decision\);/);
  assert.match(source, /const retriedDecision = await submitApproveOnce\(\);\s*await finalizeApprovedDecision\(request, retriedDecision\);/);
  assert.doesNotMatch(source, /settleApprovedDecision\(request,/);
  assert.match(source, /await submitRejectOnce\(\);\s*settleDeniedRequest\(request\);\s*await finalizeDeniedRequest\(request\);/);
});

test.skip("legacy authority approval browser fallback returns same-tab approvals to a safe ambient route", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /function approvalReturnToFromLocation\(\)/);
  assert.match(source, /url\.origin !== window\.location\.origin/);
  assert.match(source, /const APPROVAL_RETURN_TO_PATHS = \["\/finger-recording", "\/ambient-debug"\] as const/);
  assert.match(source, /function isApprovalReturnToPathAllowed\(pathname: string\)/);
  assert.match(source, /pathname === allowedPath \|\| pathname\.startsWith\(`\$\{allowedPath\}\/`\)/);
  assert.match(source, /!isApprovalReturnToPathAllowed\(url\.pathname\)/);
  assert.doesNotMatch(source, /startsWith\("\/finger-recording"\)/);
  assert.doesNotMatch(source, /startsWith\("\/ambient-debug"\)/);
  assert.match(source, /const safeReturnTo = safeSameOriginApprovalPath\(fallbackReturnTo\)/);
});

test.skip("legacy authority approval route shows the pending picker when request_id is missing", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /const showPendingRequestPicker = pendingRequests\.length > 0 && \(!requestId \|\| pendingRequests\.length > 1\)/);
  assert.match(source, /\{showPendingRequestPicker && \(/);
  assert.doesNotMatch(source, /\{pendingRequests\.length > 1 && \(/);
});

test.skip("legacy authority approval window ignores late settlements for a request that is no longer selected", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /const requestIdRef = useRef\(requestId\)/);
  assert.match(source, /requestIdRef\.current = requestId/);
  assert.match(source, /if \(requestIdRef\.current !== settledRequest\.request_id\) \{\s*return;\s*\}/);
  assert.match(source, /setPendingRequests\(\(current\) => current\.filter\(\(item\) => item\.request_id !== settledRequest\.request_id\)\)[\s\S]*if \(requestIdRef\.current !== settledRequest\.request_id\)/);
});

test.skip("legacy authority approval window treats post failure followed by settled GET as settled", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /const settleFromServer = useCallback/);
  assert.equal((source.match(/if \(await settleFromServer\(request\.request_id\)\) return;/g) ?? []).length, 4);
});

test.skip("legacy authority approval window refreshes stale ui_operator once and retries once", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /const submitApproveOnce = async[\s\S]*getApprovalContext\(request\.request_id\)/);
  assert.match(source, /if \(nativeApprovalAvailableRef\.current\)[\s\S]*getAuthorityApprovalContext\(targetRequestId\)/);
  assert.match(source, /if \(!authorityApprovalShouldRetryWithFreshContext\(postError\)\) throw postError;\s*try \{\s*const retriedDecision = await submitApproveOnce\(\);/);
  assert.equal((source.match(/await submitApproveOnce\(\)/g) ?? []).length, 2);
});

test.skip("legacy authority approval browser route is read-only and cleans legacy credentials", () => {
  const source = authorityApprovalWindowSource();
  const tokenSource = readFileSync(resolve(SRC_ROOT, "lib", "authorityApprovalBrowserToken.ts"), "utf8");
  const resourceSource = readFileSync(resolve(SRC_ROOT, "features", "chat", "resources", "authorityApprovalResources.ts"), "utf8");

  assert.match(source, /function hasNativeAuthorityApprovalContext\(\)/);
  assert.doesNotMatch(source, /readBrowserApprovalToken/);
  assert.match(tokenSource, /BROWSER_APPROVAL_TOKEN_STORAGE_KEY/);
  assert.match(tokenSource, /"browser_approval_token"/);
  assert.match(tokenSource, /"approval_browser_token"/);
  assert.match(tokenSource, /"browserApprovalToken"/);
  assert.match(tokenSource, /"sessionStorage"/);
  assert.match(tokenSource, /"localStorage"/);
  assert.match(source, /const approvalContextAvailable = nativeApprovalAvailable/);
  assert.match(source, /request\.status === "pending" && approvalContextAvailable && !displayedSettledStatus/);
  assert.match(source, /throw new Error\("AUTHORITY_BROWSER_TEST_DISABLED"\)/);
  assert.doesNotMatch(source, /BrowserApprovalExchangeSession|browserExchangeRef/);
  assert.match(source, /displayedSettledStatus && \(/);
  assert.match(source, /このリクエストは処理済みです。追加の操作は不要です。/);
  assert.doesNotMatch(resourceSource, /getBrowserAuthorityApprovalContext/);
});

test.skip("legacy authority approval credentials are absent from child window URLs", () => {
  const appSource = readFileSync(resolve(SRC_ROOT, "App.tsx"), "utf8");
  const source = authorityApprovalWindowSource();

  assert.doesNotMatch(appSource, /browserApprovalTokenizedPath/);
  assert.doesNotMatch(source, /browser_approval_token|approval_browser_token|browserApprovalToken/);
});

test.skip("legacy authority approval window explains disabled browser QA approval", () => {
  const source = authorityApprovalWindowSource();

  assert.match(source, /function authorityApprovalErrorMessage/);
  assert.match(source, /AUTHORITY_BROWSER_TEST_DISABLED/);
  assert.match(source, /AUTHORITY_UI_OPERATOR_UNAVAILABLE/);
  assert.match(source, /このDefaultspackではブラウザ承認が無効です。/);
  assert.match(source, /承認操作に必要なTobkiri Launcherの署名secretがありません。/);
  assert.match(source, /setError\(authorityApprovalErrorMessage\(approvalError\)\)/);
  assert.match(source, /setError\(authorityApprovalErrorMessage\(rejectionError\)\)/);
});
