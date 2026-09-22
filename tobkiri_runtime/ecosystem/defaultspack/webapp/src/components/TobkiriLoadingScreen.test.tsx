import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { renderToStaticMarkup } from "react-dom/server";

import {
  TOBKIRI_LOADING_ANIMATION_URL,
  TOBKIRI_LOADING_LABEL,
  TOBKIRI_STARTUP_ERROR_LABEL,
  TobkiriLoadingScreen,
} from "./TobkiriLoadingScreen";
import { HostBootstrap, HostBootstrapFallback } from "../host/HostBootstrap";

test("renders the Tobkiri Launcher animation as the accessible shell loading state", () => {
  const markup = renderToStaticMarkup(<TobkiriLoadingScreen />);

  assert.match(markup, /role="status"/);
  assert.match(markup, new RegExp(`aria-label="${TOBKIRI_LOADING_LABEL}"`));
  assert.match(markup, /aria-live="polite"/);
  assert.match(markup, /data-tobkiri-loading-screen=""/);
  assert.match(markup, /data-loading-scene="launcher"/);
  assert.match(markup, new RegExp(`src="${TOBKIRI_LOADING_ANIMATION_URL}"`));
  assert.match(markup, /motion-reduce:hidden/);
  assert.match(markup, /hidden aspect-\[2\/1\].*motion-reduce:flex/);
  assert.match(markup, />Tobkiri</);
  assert.doesNotMatch(markup, /Loading selected interface/);
});

test("shows detailed startup readiness without leaving the loading boundary", () => {
  const markup = renderToStaticMarkup(
    <TobkiriLoadingScreen
      steps={[
        { id: "backend", label: "バックエンドとの接続を確認しました", status: "ready" },
        { id: "capabilities", label: "ツール・スキル・@候補を読み込んでいます…", status: "loading" },
        { id: "commands", label: "/コマンドとモデル設定を準備します", status: "pending" },
      ]}
    />,
  );

  assert.match(markup, /aria-label="ツール・スキル・@候補を読み込んでいます…"/);
  assert.match(markup, /data-startup-readiness-steps=""/);
  assert.match(markup, /data-startup-step="backend" data-status="ready"/);
  assert.match(markup, /data-startup-step="capabilities" data-status="loading"/);
  assert.match(markup, /data-startup-step="commands" data-status="pending"/);
  assert.doesNotMatch(markup, /Tobkiriを読み込んでいます/);
});

test("keeps failures inside the startup boundary and offers a retry", () => {
  const markup = renderToStaticMarkup(
    <TobkiriLoadingScreen
      error="ツール情報を取得できませんでした。"
      onRetry={() => undefined}
      steps={[
        { id: "capabilities", label: "ツール・スキル・@候補を準備できませんでした", status: "error" },
      ]}
    />,
  );

  assert.match(markup, /role="alert"/);
  assert.match(markup, new RegExp(TOBKIRI_STARTUP_ERROR_LABEL));
  assert.match(markup, /Launcherから起動し直すか/);
  assert.match(markup, />再試行</);
  assert.match(markup, /<summary[^>]*>技術詳細<\/summary>/);
  assert.match(markup, /ツール情報を取得できませんでした/);
  assert.doesNotMatch(markup, /data-startup-readiness-steps/);
  assert.doesNotMatch(markup, /data-status="error"/);
});

test("uses the branded loading screen while the dynamic interface catalog loads", () => {
  const markup = renderToStaticMarkup(
    <HostBootstrap route="/chat" fallback={<div>Fallback</div>} />,
  );

  assert.match(markup, /data-tobkiri-loading-screen=""/);
  assert.doesNotMatch(markup, /Fallback/);
  assert.doesNotMatch(markup, /Loading selected interface/);
});

test("keeps /chat on the scoped Pack v4 unavailable screen instead of the legacy fallback", () => {
  const markup = renderToStaticMarkup(
    <HostBootstrapFallback
      route="/chat"
      reason="The active profile does not provide a Pack v4 conversation."
      onRetry={() => undefined}
      fallback={<div data-legacy-chat-app="">Legacy ChatApp</div>}
    />,
  );

  assert.match(markup, /data-conversation-surface="v4-unavailable"/);
  assert.match(markup, /Tobkiri Conversation is unavailable/);
  assert.match(markup, />Retry</);
  assert.doesNotMatch(markup, /Legacy ChatApp|data-legacy-chat-app/);
});

test("vendors the exact local animation shipped by Tobkiri Launcher", async () => {
  const defaultspackAsset = await readFile(
    new URL("../../public/assets/tobkiri-startup-blade-cut.svg", import.meta.url),
  );
  const launcherAsset = await readFile(
    new URL(
      "../../../../../../tobkiri_launcher/frontend/public/assets/tobkiri-startup-blade-cut.svg",
      import.meta.url,
    ),
  );

  assert.deepEqual(defaultspackAsset, launcherAsset);
});
