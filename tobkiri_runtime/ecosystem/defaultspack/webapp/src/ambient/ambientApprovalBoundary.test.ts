import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";

const SRC_ROOT = resolve(import.meta.dirname, "..");
const REPOSITORY_ROOT = resolve(import.meta.dirname, "../../../../../../");
const sourceCache = new Map<string, string>();

function readCachedSource(filePath: string): string {
  const cached = sourceCache.get(filePath);
  if (cached !== undefined) return cached;
  const source = readFileSync(filePath, "utf8").replace(/\r\n/g, "\n");
  sourceCache.set(filePath, source);
  return source;
}

function readSource(...parts: string[]): string {
  return readCachedSource(resolve(SRC_ROOT, ...parts));
}

function readRepositorySource(...parts: string[]): string {
  return readCachedSource(resolve(REPOSITORY_ROOT, ...parts));
}

test("ambient panel cannot render or persist its own Rumi approval", () => {
  const source = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.doesNotMatch(source, /RumiPermissionApprovalDialog/);
  assert.doesNotMatch(source, /ambientTriggerClient\.grantPermission/);
  assert.match(source, /openAuthorityApprovalWindow\(AMBIENT_AUTHORITY_REQUEST_ID\)/);
});

test("ambient mini authority settlement never sends its own resume", () => {
  const source = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.doesNotMatch(source, /sendAuthorityResume/);
  assert.doesNotMatch(source, /authorityApprovalRuntimeContent/);
  assert.match(source, /waitForMiniAuthorityContinuation/);
  assert.match(source, /承認後の続行がまだ完了していません。もう一度送信してください。/);
});

test("ambient mini authority CTA resolves stale request metadata before opening", () => {
  const source = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(source, /resolveMiniAuthorityApprovalTarget/);
  assert.match(source, /api\.listAuthorityRequests\(\{ status: "pending" \}\)/);
  assert.match(source, /const miniAuthorityApprovalConversationId = miniConversation\?\.id \?\? miniConversationId \?\? null/);
  assert.match(source, /sourceConversationId: miniAuthorityApprovalConversationId/);
  assert.match(source, /resolvePendingAuthorityApproval\(approval, pending\.pending \?\? \[\], \{[\s\S]*conversationId: currentConversationId,[\s\S]*requireConversationMatch: true,[\s\S]*requirePrincipalMatch: true,[\s\S]*\}\)/);
  assert.match(source, /authorityRequestSettledStatus\(currentRequest\.status\)/);
  assert.match(source, /openAuthorityApprovalWindow\(resolvedApproval\.requestId\)/);
  assert.doesNotMatch(source, /openAuthorityApprovalWindow\(approval\.requestId\)/);
});

test("ambient mini authority browser fallback is debug-only and opens credential-free approval URLs", () => {
  const source = readSource("ambient", "AmbientTriggerPanel.tsx");
  const helperSource = readSource("lib", "authorityApprovalBrowserToken.ts");

  assert.match(source, /const browserApprovalQaEnabled = standalone && debugMode/);
  assert.match(source, /browserApprovalQaEnabled && miniAuthorityApproval && !hasNativeAuthorityApprovalWindow\(\)/);
  assert.match(source, /browserAuthorityApprovalPath\(resolvedApproval\.requestId, ambientAuthorityApprovalReturnPath\(\)\)/);
  assert.match(source, /browserAuthorityApprovalPath\(miniAuthorityApproval\.requestId, ambientAuthorityApprovalReturnPath\(\)\)/);
  assert.match(source, /function ambientAuthorityApprovalReturnPath\(\)/);
  assert.match(source, /url\.searchParams\.set\("authority_approved", "1"\)/);
  assert.match(source, /window\.open\(approvalUrl/);
  assert.doesNotMatch(source, /browserApprovalToken|browser_approval_token/);
  assert.doesNotMatch(source, /window\.open\(["'`]\/approval\?request_id/);
  assert.doesNotMatch(helperSource, /params\.set\("browser_approval_token"/);
  assert.match(helperSource, /params\.set\("return_to", safeReturnTo\)/);
});

test("authority approval route does not render ambient gesture overlay", () => {
  const source = readSource("components", "AuthorityApprovalWindow.tsx");

  assert.doesNotMatch(source, /<AmbientTriggerPanel/);
  assert.doesNotMatch(source, /onApprovalGesture/);
});

test("ambient authority approval cancel and close settle the opener", () => {
  const source = readSource("components", "AuthorityApprovalWindow.tsx");

  assert.match(source, /broadcastAmbientApprovalCancelled/);
  assert.match(source, /requestId:\s*AMBIENT_AUTHORITY_REQUEST_ID,\s*status:\s*"denied"/s);
  assert.match(source, /window\.addEventListener\("pagehide",\s*settleOnClose\)/);
  assert.match(source, /window\.addEventListener\("beforeunload",\s*settleOnClose\)/);
  assert.match(source, /onClick=\{\(\) => void closeWindow\(\)\}/);
});

test.skip("legacy generic authority approval settlements schedule window close", () => {
  const source = readSource("components", "AuthorityApprovalWindow.tsx");

  assert.match(source, /function scheduleAuthorityApprovalWindowClose\(fallbackReturnTo = ""\)/);
  assert.match(source, /if \(await closeCurrentWindow\(\)\) return/);
  assert.match(source, /window\.close\(\)/);
  assert.match(source, /const safeReturnTo = safeSameOriginApprovalPath\(fallbackReturnTo\)/);
  assert.match(source, /const settleAuthorityRequest = useCallback[\s\S]*scheduleAuthorityApprovalWindowClose\(nativeApprovalAvailableRef\.current \? "" : approvalReturnToFromLocation\(\)\);/);
  assert.match(source, /const shouldScheduleClose = options\?\.scheduleClose\s*\?\? true/);
  assert.match(source, /await finalizeApprovedDecision\(request, decision\)/);
  assert.match(source, /await finalizeDeniedRequest\(request\)/);
});

test.skip("legacy generic authority approval load settles already completed backend requests without retargeting", () => {
  const source = readSource("components", "AuthorityApprovalWindow.tsx");

  assert.match(source, /const singleSettledStatus = authorityRequestSettledStatus\(single\.status\)[\s\S]*settleAuthorityRequest\(single, singleSettledStatus\)/);
  assert.doesNotMatch(source, /resolvePendingAuthorityApproval\(requestToApproval\(single\), list\.pending \?\? \[\]\)/);
  assert.doesNotMatch(source, /setRequestId\(activePendingApproval\.requestId\)/);
  assert.match(source, /const displayedSettledStatus = decisionSettledStatus \?\? authorityRequestSettledStatus\(request\?\.status\)/);
  assert.match(source, /authorityApprovalSettledLabel\(displayedSettledStatus\)/);
  assert.match(source, /\{showApprovalControls \? \(/);
});

test.skip("legacy generic authority approval success refetches before settling", () => {
  const source = readSource("components", "AuthorityApprovalWindow.tsx");

  assert.match(source, /const finalizeApprovedDecision = useCallback[\s\S]*readAuthoritySettlementOrNull\(settledRequest\.request_id\)[\s\S]*settleAuthorityRequest\(finalRequest, finalStatus/);
  assert.match(source, /const finalizeDeniedRequest = useCallback[\s\S]*readAuthoritySettlementOrNull\(settledRequest\.request_id\)[\s\S]*settleAuthorityRequest\(finalRequest, finalStatus\)/);
});

test("ambient authority settlement subscribers cannot replay stored settlements", () => {
  const source = readSource("ambient", "AmbientTriggerPanel.tsx");
  const eventSource = readSource("lib", "authorityApprovalEvents.ts");

  assert.match(eventSource, /readStoredAuthorityApprovalSettlement/);
  assert.doesNotMatch(eventSource, /options\?\.replayStored/);
  assert.doesNotMatch(eventSource, /AUTHORITY_APPROVAL_STORAGE_MAX_AGE_MS/);
  assert.match(eventSource, /window\.localStorage\.removeItem\(LEGACY_AUTHORITY_APPROVAL_STORAGE_KEY\)/);
  assert.match(eventSource, /readStoredAuthorityApprovalSettlement[\s\S]*clearStoredAuthorityApprovalSettlement\(\);[\s\S]*return null/);
  assert.match(source, /subscribeAuthorityApprovalSettlements\(\(event\) => \{[\s\S]*miniAuthorityApproval[\s\S]*\}, \{ replayStored: true, replayStoredRequestId: miniAuthorityApproval\.requestId \}\)/);
  assert.match(source, /subscribeAuthorityApprovalSettlements\(\(event\) => \{[\s\S]*AMBIENT_AUTHORITY_REQUEST_ID[\s\S]*\}, \{ replayStored: true, replayStoredRequestId: AMBIENT_AUTHORITY_REQUEST_ID \}\)/);
});

test.skip("legacy generic authority approval stale post failure refetches and settles before error", () => {
  const source = readSource("components", "AuthorityApprovalWindow.tsx");

  assert.equal((source.match(/if \(await settleFromServer\(request\.request_id\)\) return;/g) ?? []).length, 4);
});

test.skip("legacy generic authority approval retries stale native context once without browser bypass", () => {
  const source = readSource("components", "AuthorityApprovalWindow.tsx");

  assert.equal((source.match(/authorityApprovalShouldRetryWithFreshContext\(postError\)/g) ?? []).length, 2);
  assert.match(source, /const submitApproveOnce = async[\s\S]*getApprovalContext\(request\.request_id\)/);
  assert.match(source, /if \(nativeApprovalAvailableRef\.current\)[\s\S]*getAuthorityApprovalContext\(targetRequestId\)/);
  assert.match(source, /throw new Error\("AUTHORITY_BROWSER_TEST_DISABLED"\)/);
  assert.doesNotMatch(source, /browserExchangeRef|BrowserApprovalExchangeSession/);
  assert.match(source, /const retriedDecision = await submitApproveOnce\(\)/);
  assert.match(source, /const submitRejectOnce = async[\s\S]*getApprovalContext\(request\.request_id\)/);
  assert.doesNotMatch(source, /window\.localStorage/);
});

test("ambient browser approval QA remains route-scoped without visible send controls", () => {
  const appSource = readSource("App.tsx");
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(appSource, /pathname === "\/ambient-debug"/);
  assert.match(appSource, /pathname === "\/ambient-debug" \|\| pathname === "\/finger-recording"/);
  assert.match(appSource, /const explicitDebugConversationId = fingerDebugMode \? chatIdFromLocation\(\) : null/);
  assert.match(appSource, /<AmbientTriggerPanel variant="window" debugMode=\{fingerDebugMode\} conversationId=\{explicitDebugConversationId\}/);
  assert.doesNotMatch(appSource, /pathname === "\/viewer"/);
  assert.match(panelSource, /const explicitDebugConversationId = debugMode \? cleanString\(conversationId\) : null/);
  assert.match(panelSource, /explicitDebugConversationId \?\? ambientLinkedConversationId\(status, conversationId\)/);
  assert.doesNotMatch(panelSource, /Browser QA/);
  assert.doesNotMatch(panelSource, /OK送信/);
  assert.doesNotMatch(panelSource, /data-testid="ambient-debug-panel"/);
  assert.doesNotMatch(panelSource, /data-testid="ambient-debug-transcript"/);
  assert.doesNotMatch(panelSource, /data-testid="ambient-debug-simulate-ok"/);
  assert.doesNotMatch(panelSource, /data-testid="ambient-debug-status"/);
});

test("ambient mini window removed browser QA simulated OK payload", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");
  const miniChatSource = readSource("ambient", "AmbientMiniChat.tsx");

  assert.doesNotMatch(panelSource, /Local Browser QA only/);
  assert.doesNotMatch(panelSource, /debug_qa/);
  assert.doesNotMatch(panelSource, /debug-ok-mark\.webm/);
  assert.doesNotMatch(panelSource, /ambient\.debug_qa/);
  assert.match(miniChatSource, /data-testid="ambient-mini-chat"/);
  assert.match(miniChatSource, /data-testid="ambient-mini-chat-output"/);
  assert.match(miniChatSource, /data-testid="ambient-mini-chat-input"/);
});

test("ambient mini chat open button opens the linked chat in the Defaultspack main window", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");
  const miniChatSource = readSource("ambient", "AmbientMiniChat.tsx");
  const desktopSource = readSource("lib", "desktopApproval.ts");

  assert.match(miniChatSource, /data-testid="ambient-mini-chat-open"/);
  assert.match(panelSource, /function openMiniChatConversation\(\)/);
  assert.match(panelSource, /openDefaultspackMainWindow\(path\)/);
  assert.match(panelSource, /window\.open\(defaultspackUrlWithLocalAuth\(path\), "rumi-defaultspack"/);
  assert.doesNotMatch(panelSource, /window\.location\.assign/);
  assert.match(desktopSource, /open_defaultspack_main_window/);
  assert.match(panelSource, /onOpenChat=\{openMiniChatConversation\}/);
});

test("ambient requests never forward browser approval credentials", () => {
  const clientSource = readSource("ambient", "ambientTriggerClient.ts");

  assert.doesNotMatch(clientSource, /browserApprovalToken|browser_approval_token|X-Rumi-Approval-Browser-Token/);
});

test("ambient action failures expand details so auth errors are visible", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(panelSource, /catch \(error\) \{\s*setExpanded\(true\);\s*setErrorMessage\(error instanceof Error \? error\.message : "操作を完了できませんでした。"\)/);
});

test("ambient guidance and failures retain warning and error severity", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(panelSource, /setWarningMessage\("Tobkiriの許可と端末のマイク・カメラ許可がそろってから録音できます。"\)/);
  assert.match(panelSource, /setWarningMessage\("この承認では拒否ジェスチャーは使えません。"\)/);
  assert.match(panelSource, /setErrorMessage\("Tobkiri Launcherの承認ウィンドウを開けませんでした。/);
  assert.match(panelSource, /setErrorMessage\("カメラが見つかりません。接続してからデバイス更新を押してください。"\)/);
  assert.match(panelSource, /messageTone === "warning"[\s\S]*severity="warning"/);
  assert.match(panelSource, /copyLabel="アンビエント操作の警告をコピー"/);
});

test("real OK-mark recording routes audio through transcription before dispatch", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(panelSource, /setPinchDetectorStatus\("transcribing"\)/);
  assert.match(panelSource, /ambientOperationLabels\.transcribing/);
  const pinchSubmitStart = panelSource.indexOf('source: "camera",\n        trigger: "pinch",\n        mode: "dispatch_audio"');
  assert.notEqual(pinchSubmitStart, -1);
  const pinchSubmitEnd = panelSource.indexOf("      });", pinchSubmitStart);
  assert.notEqual(pinchSubmitEnd, -1);
  const pinchSubmitSource = panelSource.slice(pinchSubmitStart, pinchSubmitEnd);
  assert.doesNotMatch(pinchSubmitSource, /audio_data_url: recording\.dataUrl/);
  assert.match(panelSource, /audio_mime_type: recording\.mimeType/);
  assert.match(panelSource, /audio_name: `ok-mark-recording\.\$\{recording\.extension\}`/);
  assert.match(panelSource, /dataUrl: recording\.dataUrl/);
  assert.match(panelSource, /\.\.\.\(transcript \? \{ input_text: transcript \} : \{\}\)/);
  assert.match(panelSource, /setLatestSubmittedInput\(transcript \|\| null\)/);
  assert.doesNotMatch(panelSource, /録音音声を送信しました。文字起こしはまだありません。/);
  assert.doesNotMatch(panelSource, /音声を確認して返答してください。/);
});

test("ambient readout toggle uses stable on off copy", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(panelSource, /aria-pressed=\{readoutEnabled\}/);
  assert.match(panelSource, /readoutEnabled \? "オン" : "オフ"/);
  assert.doesNotMatch(panelSource, /読み上げ: 再生中/);
});

test("ambient mini chat keeps a submitted conversation active over stale routing", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(panelSource, /const miniConversationId = miniConversationIdOverride \|\| linkedAmbientConversationId/);
  assert.match(panelSource, /function ambientSubmittedConversationIdFromResult/);
  assert.match(panelSource, /ambientConversationIdFromNestedResult\(record\?\.pending_approval\)/);
  assert.match(panelSource, /ambientConversationIdFromNestedResult\(record\?\.dispatch\)/);
  assert.match(panelSource, /ambientConversationIdFromNestedResult\(record\?\.dispatch_result\)/);
  assert.match(panelSource, /record\.data, depth \+ 1/);
  assert.match(panelSource, /async function selectMiniChatRoutingConversation[\s\S]*setMiniConversationIdOverride\(null\)[\s\S]*selectConversationForRouting\(chatId\)/);
  assert.match(panelSource, /onSelect=\{\(chatId\) => void selectMiniChatRoutingConversation\(chatId\)\}/);
});

test("ambient mini chat fallback conversation does not override backend routing", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(panelSource, /api\.listConversations\(\{[\s\S]*tag: "ambient"[\s\S]*group_id: "gesture"[\s\S]*limit: 1/);
  assert.doesNotMatch(panelSource, /if \(conversation\?\.id\) setMiniConversationIdOverride\(conversation\.id\)/);
});

test("ambient submission waits for the stored model reply before completing", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(panelSource, /waitForAmbientAssistantResponse/);
  assert.match(panelSource, /setPinchDetectorStatus\("waiting_response"\)/);
  assert.match(panelSource, /outcome\.status === "completed"/);
  assert.match(panelSource, /setPinchDetectorStatus\("completed"\)/);
  assert.match(panelSource, /setLatestSubmittedInput\(null\)/);
});

test("ambient browser-owned monitor keeps camera lifecycle local to the active window", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");
  const trackerSource = readSource("ambient", "useAmbientHandTracker.ts");
  const landmarkerSource = readSource("ambient", "mediaPipeHandLandmarker.ts");

  assert.match(panelSource, /const \[videoElement, setVideoElement\] = useState<HTMLVideoElement \| null>\(null\)/);
  assert.match(panelSource, /const cameraVideoRef = useCallback\(\(node: HTMLVideoElement \| null\) => \{\s*setVideoElement\(node\);/);
  assert.match(trackerSource, /videoElement: HTMLVideoElement \| null/);
  assert.doesNotMatch(trackerSource, /RefObject/);
  assert.doesNotMatch(panelSource, /monitorEnabledRef/);
  assert.match(panelSource, /function replaceCameraStream\(nextStream: MediaStream \| null\)/);
  assert.match(panelSource, /track\.addEventListener\("ended", handleEnded, \{ once: true \}\)/);
  assert.match(panelSource, /track\.addEventListener\("mute", handleEnded, \{ once: true \}\)/);
  assert.match(panelSource, /setErrorMessage\("カメラの接続が切れました。/);
  assert.match(landmarkerSource, /onError\?: \(error: unknown\) => void/);
  assert.match(landmarkerSource, /catch \(error\) \{\s*stopLoop\(error\);/);
  assert.match(panelSource, /async function acquireCameraForMonitoring\(\)/);
  assert.match(panelSource, /if \(!monitorEnabled \|\| cameraStream \|\| cameraAcquireInFlightRef\.current\) return/);
  assert.match(panelSource, /await acquireCameraForMonitoring\(\);[\s\S]*待機を再開しました/);
  assert.match(panelSource, /await ambientTriggerClient\.stopMonitor\(\)\.catch\(\(\) => undefined\);[\s\S]*await refresh\(\{ probeOs: true \}\)/);
});

test("ambient approval gesture requires audit event before executing approval", () => {
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");
  const start = panelSource.indexOf("async function submitApprovalGesture");
  const end = panelSource.indexOf("async function approvePendingApproval", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const submitApprovalGestureSource = panelSource.slice(start, end);

  assert.doesNotMatch(submitApprovalGestureSource, /\.catch\(\(\) => undefined\)/);
  assert.match(submitApprovalGestureSource, /const auditResult = await ambientTriggerClient\.submitEvent\(\{[\s\S]*trigger: "approval_gesture"/);
  assert.match(submitApprovalGestureSource, /if \(auditResult\.status !== "approval_intent"\) \{/);
  assert.match(submitApprovalGestureSource, /await onApprovalGestureRef\.current\?\.\(decision\)/);
});

test("Viewer authenticates every dedicated Defaultspack window and rejects unsafe stale listeners", (context) => {
  const viewerPath = resolve(REPOSITORY_ROOT, "tobkiri_launcher", "src-tauri", "src", "lib.rs");
  const dockPath = resolve(REPOSITORY_ROOT, "tobkiri_launcher", "src-tauri", "src", "dock_registration.rs");
  if (!existsSync(viewerPath) || !existsSync(dockPath)) {
    context.skip("Requires the sibling tobkiri_launcher repository.");
    return;
  }
  const viewerSource = readRepositorySource("tobkiri_launcher", "src-tauri", "src", "lib.rs");
  const dockSource = readRepositorySource("tobkiri_launcher", "src-tauri", "src", "dock_registration.rs");

  assert.match(viewerSource, /authenticated_defaultspack_window_url\(config, authority_approval_url/);
  assert.match(viewerSource, /authenticated_defaultspack_window_url\(config, ambient_trigger_url/);
  assert.match(viewerSource, /authenticated_defaultspack_window_url\(config, finger_recording_url/);
  assert.match(viewerSource, /authenticated_defaultspack_window_url\(config, defaults_console_url/);
  assert.match(viewerSource, /authenticated_defaultspack_window_url\(config, host_permissions_url/);
  assert.match(dockSource, /active HMAC store is encrypted; using the Kernel-managed desktop token cache/);
  assert.match(dockSource, /Viewer did not stop it\. Close that process or free port/);
  assert.match(dockSource, /identify_defaultspack_listener\(&listener, metadata\)/);
});

test("ambient model selection persists to the canonical selected conversation", () => {
  const routingSource = readSource("ambient", "useAmbientRouting.ts");
  const panelSource = readSource("ambient", "AmbientTriggerPanel.tsx");

  assert.match(routingSource, /async function saveRoutingModel\(model: string\)/);
  assert.match(routingSource, /api\.updateConversation\(targetConversationId, \{ model: normalizedModel \}\)/);
  assert.match(panelSource, /onModelCommit=\{\(model\) => void saveRoutingModel\(model\)\}/);
});
