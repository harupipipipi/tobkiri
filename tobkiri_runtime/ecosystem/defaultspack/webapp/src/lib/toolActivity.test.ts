import test from "node:test";
import assert from "node:assert/strict";

import { buildToolActivityGroups, buildToolActivityItems, summarizeToolArguments, toolFolderFor } from "./toolActivity";

test("formats calculator arguments as a compact activity title", () => {
  assert.equal(summarizeToolArguments("calculator", { expression: "13829+12312" }), "13829+12312");
  assert.equal(summarizeToolArguments("calculator", { a: 13829, operation: "+", b: 12312 }), "13829 + 12312");
});

test("groups real tool logs into folder-like sections", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "calculator",
      arguments: { expression: "13829+12312" },
      result: { status: "ok", data: { result: 26141 } },
    },
    {
      tool_name: "web_search",
      arguments: { query: "今日の天気 東京" },
      result: { status: "ok", data: { results: [{ title: "weather" }] } },
    },
  ]);

  assert.equal(groups.length, 2);
  assert.equal(groups[0].id, "calculation");
  assert.equal(groups[0].items[0].title, "計算: 13829+12312");
  assert.equal(groups[0].items[0].detail, "26141");
  assert.equal(groups[1].id, "web/search");
  assert.equal(groups[1].items[0].title, "Webで検索: 今日の天気 東京");
});

test("polishes calculator result prose into the answer", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "calculator",
      arguments: { expression: "13829+12312" },
      result: { status: "ok", data: { output: "Calculated: 13829+12312 = 26141" } },
    },
  ]);

  assert.equal(groups[0].items[0].detail, "26141");
});

test("does not create activity from text-only claims", () => {
  assert.deepEqual(buildToolActivityGroups([], []), []);
});

test("uses running tool_call events when a log has not arrived yet", () => {
  const groups = buildToolActivityGroups([], [
    {
      type: "tool_call",
      phase: "tool_call",
      tool_name: "coding_file_list",
      arguments: { path: "src" },
      message: "coding_file_list を使用中",
    },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].id, "coding/files");
  assert.equal(groups[0].items[0].status, "running");
  assert.equal(groups[0].items[0].title, "ファイル一覧を確認: src");
});

test("summarizes terminal commands as user-facing activity", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "coding_terminal_exec",
      arguments: { command: "gh repo view --json defaultBranchRef" },
      result: { status: "ok", data: { exit_code: 0, stdout: "{\"defaultBranchRef\":{\"name\":\"main\"}}" } },
    },
  ]);

  const item = groups[0].items[0];
  assert.equal(groups[0].id, "coding/git");
  assert.equal(item.title, "GitHub 情報を確認");
  assert.equal(item.detail, "終了コード 0");
  assert.equal(item.input, "gh repo view --json defaultBranchRef");
});

test("groups file-oriented terminal commands with file activity", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "coding_terminal_exec",
      arguments: { command: "sed -n '1,80p' src/App.tsx" },
      result: { status: "ok", data: { exit_code: 0, stdout: "import React from 'react';" } },
    },
  ]);

  assert.equal(groups[0].id, "coding/files");
  assert.equal(groups[0].label, "ファイル");
  assert.equal(groups[0].items[0].title, "ファイルを確認");
});

test("keeps generic GitHub terminal commands inside Git activity", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "coding_terminal_exec",
      arguments: { command: "gh pr list --state open" },
      result: { status: "ok", data: { exit_code: 0, stdout: "" } },
    },
  ]);

  assert.equal(groups[0].id, "coding/git");
  assert.equal(groups[0].items[0].title, "GitHub を操作");
});

test("summarizes nested JSON string results instead of surfacing raw payloads", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "coding_git_status",
      arguments: {},
      result: {
        status: "ok",
        data: {
          result: JSON.stringify({
            branch: "main",
            clean: false,
            staged: [],
            modified: ["src/App.tsx"],
            untracked: ["notes.md"],
          }),
          widget: {
            branch: "main",
            clean: false,
            staged: [],
            modified: ["src/App.tsx"],
            untracked: ["notes.md"],
          },
        },
      },
    },
    {
      tool_name: "coding_terminal_exec",
      arguments: { command: "git branch -a" },
      result: {
        status: "ok",
        data: {
          result: JSON.stringify({
            command: "git branch -a",
            exit_code: 0,
            stdout: "* main\n  remotes/origin/main\n",
          }),
        },
      },
    },
  ]);

  assert.equal(groups[0].items[0].detail, "ブランチ main · 2件の変更");
  assert.equal(groups[0].items[1].detail, "終了コード 0");
  assert.doesNotMatch(groups[0].items[0].detail, /[{"]/);
  assert.doesNotMatch(groups[0].items[1].detail, /command|stdout/);
});

test("surfaces file edits without exposing the whole diff as the main activity", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "coding_file_patch",
      arguments: { path: "src/App.tsx", old: "before", new: "after" },
      result: { status: "ok", data: { path: "src/App.tsx", patched: true, diff: "-before\n+after\n" } },
    },
  ]);

  const item = groups[0].items[0];
  assert.equal(item.title, "ファイルを編集: App.tsx");
  assert.equal(item.detail, "変更しました: App.tsx");
});

test("labels sandbox coding tools separately from host tools", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "sandbox_file_write",
      arguments: { path: "src/App.tsx", content: "updated" },
      result: {
        status: "ok",
        data: {
          path: "src/App.tsx",
          written: true,
          host_modified: false,
          sandbox_only: true,
          diff_summary: "Sandbox changed 1 file(s): 1 modified.",
        },
      },
    },
  ]);

  const item = groups[0].items[0];
  assert.equal(groups[0].id, "sandbox/files");
  assert.equal(groups[0].label, "Sandbox");
  assert.equal(item.title, "Sandboxで編集: App.tsx");
  assert.equal(item.detail, "Sandbox changed 1 file(s): 1 modified.");
});

test("updates streamed tool activity when a completion event arrives before the log", () => {
  const groups = buildToolActivityGroups([], [
    {
      type: "tool_call_started",
      phase: "tool_call_started",
      tool_call_id: "call_1",
      tool_name: "computer_use",
      arguments: { action: "click", app: "Notion", x: 120, y: 340 },
      message: "computer_use を使用中",
      timestamp: 1_700_000_000_000,
    },
    {
      type: "tool_call_completed",
      phase: "tool_call_completed",
      tool_call_id: "call_1",
      tool_name: "computer_use",
      arguments: { action: "click", app: "Notion", x: 120, y: 340 },
      message: "computer_use の結果を受け取りました",
      is_error: false,
      timestamp: 1_700_000_003_200,
    },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].items[0].status, "completed");
  assert.equal(groups[0].items[0].input, "click Notion (120, 340)");
  assert.equal(groups[0].items[0].durationLabel, "3s");
});

test("keeps discarded and retried provider attempts as independent activity rows", () => {
  const events = [
    {
      type: "tool_call_started",
      phase: "tool_call_started",
      seq: 1,
      timestamp: 1_000,
      tool_call_id: "call_1",
      tool_name: "coding_file_read",
      provider_attempt: 1,
      provider_attempt_generation: 1,
      arguments: {},
      status: "running",
    },
    {
      type: "tool_call_completed",
      phase: "tool_call_completed",
      seq: 2,
      timestamp: 1_500,
      tool_call_id: "call_1",
      tool_name: "coding_file_read",
      provider_attempt: 1,
      provider_attempt_generation: 1,
      provider_attempt_discarded: true,
      is_error: true,
      status: "failed",
      display_text: "失敗した provider attempt の入力を破棄しました",
    },
    {
      type: "tool_call_started",
      phase: "tool_call_started",
      seq: 4,
      timestamp: 2_000,
      tool_call_id: "call_1",
      tool_name: "coding_file_read",
      provider_attempt: 2,
      provider_attempt_generation: 2,
      arguments: { path: "README.md" },
      status: "running",
    },
  ];

  const retrying = buildToolActivityItems([], events, { now: 2_500 });
  assert.equal(retrying.length, 2);
  assert.deepEqual(retrying.map((item) => item.status), ["failed", "running"]);
  assert.notEqual(retrying[0].id, retrying[1].id);
  assert.equal(retrying[0].providerAttemptGeneration, 1);
  assert.equal(retrying[1].providerAttemptGeneration, 2);
  assert.equal(retrying[1].completedAt, undefined);
  assert.doesNotMatch(retrying[1].detail, /破棄/);

  const completed = buildToolActivityItems(
    [
      {
        tool_name: "coding_file_read",
        tool_call_id: "call_1",
        provider_attempt: 2,
        provider_attempt_generation: 2,
        arguments: { path: "README.md" },
        result: { status: "ok", data: { path: "README.md", content: "ok" } },
        timestamp: 3_000,
      },
    ],
    [
      ...events,
      {
        type: "tool_call_completed",
        phase: "tool_call_completed",
        seq: 5,
        timestamp: 3_000,
        tool_call_id: "call_1",
        tool_name: "coding_file_read",
        provider_attempt: 2,
        provider_attempt_generation: 2,
        arguments: { path: "README.md" },
        is_error: false,
        status: "completed",
      },
    ],
  );

  assert.equal(completed.length, 2);
  assert.deepEqual(completed.map((item) => item.status), ["failed", "completed"]);
  const completedRetry = completed[1];
  assert.equal(completedRetry.kind, "tool");
  if (completedRetry.kind !== "tool") assert.fail("expected a tool activity item");
  assert.equal(completedRetry.startedAt, 2_000);
  assert.equal(completedRetry.completedAt, 3_000);
  assert.equal(completedRetry.input, "README.md");
  assert.equal(completedRetry.detail, "読みました: README.md");
  assert.doesNotMatch(completedRetry.detail, /破棄/);
});

test("keeps legacy tool activity merging by call id without attempt generation", () => {
  const items = buildToolActivityItems([], [
    {
      type: "tool_call_started",
      tool_call_id: "legacy_call",
      tool_name: "coding_file_read",
      arguments: { path: "README.md" },
      timestamp: 1_000,
    },
    {
      type: "tool_call_completed",
      tool_call_id: "legacy_call",
      tool_name: "coding_file_read",
      arguments: { path: "README.md" },
      timestamp: 2_000,
      is_error: false,
    },
  ]);

  assert.equal(items.length, 1);
  assert.equal(items[0].status, "completed");
  assert.equal(items[0].toolCallId, "legacy_call");
});

test("keeps terminal tool state monotonic when events arrive out of order", () => {
  const items = buildToolActivityItems([], [
    {
      type: "tool_call_completed",
      seq: 2,
      timestamp: 2_000,
      tool_call_id: "out-of-order",
      tool_name: "coding_file_read",
      provider_attempt_generation: 7,
      is_error: false,
    },
    {
      type: "tool_call_started",
      seq: 1,
      timestamp: 1_000,
      tool_call_id: "out-of-order",
      tool_name: "coding_file_read",
      provider_attempt_generation: 7,
      arguments: { path: "README.md" },
    },
  ]);

  assert.equal(items.length, 1);
  assert.equal(items[0].status, "completed");
  assert.equal(items[0].startedAt, 1_000);
  assert.equal(items[0].completedAt, 2_000);
});

test("keeps cancellation terminal when a success log arrives afterward", () => {
  const items = buildToolActivityItems(
    [
      {
        tool_name: "coding_file_read",
        tool_call_id: "cancelled-call",
        provider_attempt_generation: 8,
        arguments: { path: "README.md" },
        result: { status: "ok", data: { path: "README.md" } },
        timestamp: 3_000,
      },
    ],
    [
      {
        type: "tool_call_started",
        seq: 1,
        timestamp: 1_000,
        tool_call_id: "cancelled-call",
        tool_name: "coding_file_read",
        provider_attempt_generation: 8,
      },
      {
        type: "tool_call_completed",
        seq: 2,
        timestamp: 2_000,
        tool_call_id: "cancelled-call",
        tool_name: "coding_file_read",
        provider_attempt_generation: 8,
        cancelled: true,
        is_error: true,
      },
    ],
  );

  assert.equal(items.length, 1);
  assert.equal(items[0].status, "failed");
  assert.equal(items[0].completedAt, 2_000);
});

test("shows live elapsed time for running streamed tool activity", () => {
  const groups = buildToolActivityGroups([], [
    {
      type: "tool_call_started",
      phase: "tool_call_started",
      tool_call_id: "call_1",
      tool_name: "coding_file_list",
      arguments: { path: "src" },
      timestamp: 1_700_000_010_000,
    },
  ], { now: 1_700_000_072_000 });

  assert.equal(groups[0].items[0].status, "running");
  assert.equal(groups[0].items[0].durationLabel, "1m 2s");
});

test("uses streamed completion results and artifacts before final logs arrive", () => {
  const groups = buildToolActivityGroups([], [
    {
      type: "tool_call_started",
      phase: "tool_call_started",
      tool_call_id: "call_1",
      tool_name: "browser_computer",
      arguments: { action: "computer.screenshot" },
      message: "browser_computer を使用中",
    },
    {
      type: "tool_call_completed",
      phase: "tool_call_completed",
      tool_call_id: "call_1",
      tool_name: "browser_computer",
      result: {
        status: "ok",
        data: {
          summary: "Captured screen",
          widget: {
            data_url: "data:image/png;base64,aW1hZ2U=",
            screenshot_path: "/tmp/rumi/workspace/tools/browser/screen.png",
          },
        },
      },
      message: "browser_computer の結果を受け取りました",
    },
  ], { conversationId: "conv_1" });

  const item = groups[0].items[0];
  assert.equal(item.status, "completed");
  assert.equal(item.input, "computer.screenshot");
  assert.equal(item.detail, "Captured screen");
  assert.equal(item.artifacts?.some((artifact) => artifact.url?.startsWith("data:image/png")), true);
  assert.equal(item.artifacts?.some((artifact) => artifact.path.endsWith("screen.png")), true);
});

test("uses streamed display text and next step for realtime tool narration", () => {
  const groups = buildToolActivityGroups([], [
    {
      type: "tool_call_completed",
      phase: "tool_call_completed",
      tool_call_id: "call_1",
      tool_name: "browser_computer",
      arguments: { action: "computer.click" },
      display_text: "クリックしました。結果を確認しています。",
      next_step: "画面の変化をもとに次へ進みます。",
      status: "completed",
    },
  ]);

  assert.equal(groups[0].items[0].detail, "クリックしました。結果を確認しています。");
  assert.equal(groups[0].items[0].nextStep, "画面の変化をもとに次へ進みます。");
});

test("hides generic completion text for completed tool activity", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "computer_use",
      arguments: { action: "context" },
      result: { status: "ok", data: { result: "computer_use computer.context completed; artifact: /tmp/screenshot.png" } },
    },
  ]);

  assert.equal(groups[0].items[0].status, "completed");
  assert.equal(groups[0].items[0].detail, "");
  assert.equal(groups[0].items[0].input, "context");
});

test("keeps unsupported tool payloads out of the main timeline", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "mystery_plugin",
      arguments: { value: "abc" },
      result: { status: "ok", data: { answer: 42 } },
    },
  ]);

  const item = groups[0].items[0];
  assert.equal(item.supported, false);
  assert.equal(item.title, "Toolsを使用");
  assert.equal(item.rawJson, undefined);
});

test("preserves chronological file terminal file order instead of category aggregation", () => {
  const items = buildToolActivityItems([], [
    {
      type: "tool_call_started",
      seq: 10,
      timestamp: "2026-06-24T10:20:30Z",
      tool_call_id: "call_file_1",
      tool_name: "coding_file_read",
      arguments: { path: "src/App.tsx" },
    },
    {
      type: "tool_call_started",
      seq: 11,
      timestamp: "2026-06-24T10:20:31Z",
      tool_call_id: "call_terminal",
      tool_name: "coding_terminal_exec",
      arguments: { command: "npm test" },
    },
    {
      type: "tool_call_started",
      seq: 12,
      timestamp: "2026-06-24T10:20:32Z",
      tool_call_id: "call_file_2",
      tool_name: "coding_file_patch",
      arguments: { path: "src/App.tsx" },
    },
  ]);

  assert.deepEqual(
    items.map((item) => item.folder),
    ["coding/files", "coding/terminal", "coding/files"],
  );
});

test("completion logs update rows without moving them from their start sequence", () => {
  const items = buildToolActivityItems(
    [
      {
        tool_name: "coding_file_read",
        tool_call_id: "call_file_1",
        arguments: { path: "src/App.tsx" },
        result: { status: "ok", data: { content: "x", path: "src/App.tsx" } },
        timestamp: "2026-06-24T10:20:40Z",
      },
      {
        tool_name: "coding_terminal_exec",
        tool_call_id: "call_terminal",
        arguments: { command: "npm test" },
        result: { status: "ok", data: { exit_code: 0 } },
        timestamp: "2026-06-24T10:20:33Z",
      },
      {
        tool_name: "coding_file_patch",
        tool_call_id: "call_file_2",
        arguments: { path: "src/App.tsx" },
        result: { status: "ok", data: { patched: true, path: "src/App.tsx" } },
        timestamp: "2026-06-24T10:20:34Z",
      },
    ],
    [
      {
        type: "tool_call_started",
        seq: 10,
        timestamp: "2026-06-24T10:20:30Z",
        tool_call_id: "call_file_1",
        tool_name: "coding_file_read",
        arguments: { path: "src/App.tsx" },
      },
      {
        type: "tool_call_started",
        seq: 11,
        timestamp: "2026-06-24T10:20:31Z",
        tool_call_id: "call_terminal",
        tool_name: "coding_terminal_exec",
        arguments: { command: "npm test" },
      },
      {
        type: "tool_call_started",
        seq: 12,
        timestamp: "2026-06-24T10:20:32Z",
        tool_call_id: "call_file_2",
        tool_name: "coding_file_patch",
        arguments: { path: "src/App.tsx" },
      },
    ],
  );

  assert.deepEqual(
    items.map((item) => item.toolCallId),
    ["call_file_1", "call_terminal", "call_file_2"],
  );
  assert.equal(items[0].status, "completed");
  assert.equal(items[0].detail, "読みました: App.tsx");
});

test("uses nested event data and tool_started aliases for timeline order", () => {
  const items = buildToolActivityItems(
    [
      {
        tool_name: "coding_terminal_exec",
        tool_call_id: "call_terminal",
        arguments: { command: "npm test" },
        result: { status: "ok", data: { exit_code: 0 } },
        timestamp: "2026-06-24T10:20:33Z",
      },
      {
        tool_name: "coding_file_read",
        tool_call_id: "call_file_1",
        arguments: { path: "src/App.tsx" },
        result: { status: "ok", data: { content: "x", path: "src/App.tsx" } },
        timestamp: "2026-06-24T10:20:40Z",
      },
    ],
    [
      {
        type: "tool_started",
        seq: 10,
        timestamp: "2026-06-24T10:20:30Z",
        tool_call_id: "call_file_1",
        data: {
          tool_name: "coding_file_read",
          arguments: { path: "src/App.tsx" },
        },
      },
      {
        type: "tool_started",
        seq: 11,
        timestamp: "2026-06-24T10:20:31Z",
        tool_call_id: "call_terminal",
        data: {
          tool_name: "coding_terminal_exec",
          arguments: { command: "npm test" },
        },
      },
    ],
  );

  assert.deepEqual(
    items.map((item) => item.toolCallId),
    ["call_file_1", "call_terminal"],
  );
  assert.equal(items[0].title, "ファイルを確認: App.tsx");
  assert.equal(items[1].title, "テストを実行");
});

test("dedupes started events when a matching completed log exists", () => {
  const groups = buildToolActivityGroups(
    [
      {
        tool_name: "coding_file_list",
        tool_call_id: "call_1",
        arguments: { path: "src" },
        result: { status: "ok", data: { files: ["a.ts"] } },
      },
    ],
    [
      {
        type: "tool_call_started",
        phase: "tool_call_started",
        tool_call_id: "call_1",
        tool_name: "coding_file_list",
        arguments: { path: "src" },
        message: "coding_file_list を使用中",
      },
    ],
  );

  assert.equal(groups.length, 1);
  assert.equal(groups[0].items.length, 1);
  assert.equal(groups[0].items[0].status, "completed");
});

test("marks nested tool errors as failed activity", () => {
  const groups = buildToolActivityGroups([
    {
      tool_name: "computer_use",
      arguments: { action: "type", text: "hello" },
      result: {
        status: "ok",
        data: {
          result: "computer_use computer.type failed",
          is_error: true,
          widget: { is_error: true },
        },
      },
    },
  ]);

  assert.equal(groups[0].items[0].status, "failed");
});

test("attaches tool artifact files to the matching activity item", () => {
  const path = "/tmp/rumi/workspace/tools/computer/click-1.png";
  const groups = buildToolActivityGroups([
    {
      tool_name: "computer_use",
      tool_call_id: "call_1",
      arguments: { action: "click", x: 12, y: 34 },
      result: {
        status: "ok",
        data: {
          widget: {
            path,
            model_image_path: "/tmp/rumi/workspace/tools/computer/click-1-model.jpg",
          },
        },
      },
    },
  ], [], { conversationId: "conv_1" });

  const artifact = groups[0].items[0].artifacts?.[0];
  assert.equal(groups[0].items[0].toolCallId, "call_1");
  assert.equal(artifact?.kind, "image");
  assert.equal(artifact?.name, "click-1-model.jpg");
  assert.match(
    decodeURIComponent(artifact?.url ?? ""),
    /GET \/api\/chat\/conversations\/conv_1\/artifact-file/,
  );
});

test("classifies common tool families", () => {
  assert.equal(toolFolderFor("browser_companion").id, "browser");
  assert.equal(toolFolderFor("browser_computer").id, "browser");
  assert.equal(toolFolderFor("todo").id, "planning/todo");
  assert.equal(toolFolderFor("subagent").id, "agent/delegation");
  assert.equal(toolFolderFor("coding_terminal_exec").id, "coding/terminal");
  assert.equal(toolFolderFor("git_status").id, "coding/git");
});
