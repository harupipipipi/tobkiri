import { expect, test, type Page, type Route } from "@playwright/test";

test.use({ viewport: { width: 1440, height: 900 } });

// These specs exercise mocked UI contracts only. Live MCP proof is covered by
// the Python integration tests that assert tool_logs and tool_call events.
const now = 1_785_000_000_000;
const historyChatDropMime = "application/rumi-history-chat";

type ApiMockOptions = {
  beforeWorkspaceFileReadResponse?: (payload: Record<string, unknown>) => Promise<void> | void;
  initialSettingsValues?: Record<string, Record<string, unknown>>;
  onStreamRequest?: (payload: Record<string, unknown>) => void;
  streamEvents?: (message: Record<string, unknown>) => Record<string, unknown>[];
  conversationMutator?: (conversation: ReturnType<typeof smokeConversation>) => void;
  onApprovalDecision?: (decision: "approve" | "deny", payload: Record<string, unknown>) => void;
  onAuthorityRequest?: (requestId: string) => void;
  authorityRequestStatus?: () => "pending" | "approved" | "denied";
  codingApprovalAfterTerminal?: boolean;
  codingApprovalAfterRestore?: boolean;
  structuredComposer?: boolean;
};

function ok(data: unknown) {
  return { status: "ok", data };
}

function smokeConversation() {
  return {
    id: "c-smoke",
    title: "Preview Calendar Chat",
    created_at: now - 60_000,
    updated_at: now,
    model: "stub/default",
    conversation_kind: "coding",
    tags: ["coding"],
    is_starred: false,
    is_pinned: false,
    is_archived: false,
    messages: [
      {
        id: "m-user",
        role: "user",
        content: [{ type: "text", text: "Show the current @Web Search state." }],
        raw_text: "Show the current @Web Search state.",
        created_at: now - 20_000,
        conversation_id: "c-smoke",
        parent_id: null,
        children_ids: [],
        sequence_number: 1,
        finish_reason: null,
        usage: null,
        widget: null,
        metadata: {
          mentions: [{
            id: "web_search",
            kind: "tool",
            label: "Web Search",
            syntax: "@Web Search",
          }],
        },
      },
      {
        id: "m-assistant",
        role: "assistant",
        content: [{ type: "text", text: "Preview smoke response with tool timeline." }],
        raw_text: "Preview smoke response with tool timeline.",
        created_at: now - 10_000,
        conversation_id: "c-smoke",
        parent_id: "m-user",
        children_ids: [],
        sequence_number: 2,
        finish_reason: "stop",
        usage: { total_tokens: 42 },
        widget: null,
        model: "stub/default",
        metadata: {
          timing: {
            thinking_started_at: now - 15_000,
            completed_at: now - 10_000,
          },
        },
        events: [
          {
            type: "tool_call_started",
            phase: "tool_call_started",
            tool_call_id: "call-files",
            tool_name: "coding_file_list",
            arguments: { path: "src" },
            timestamp: now - 14_000,
          },
          {
            type: "tool_call_completed",
            phase: "tool_call_completed",
            tool_call_id: "call-files",
            tool_name: "coding_file_list",
            arguments: { path: "src" },
            display_text: "Listed 2 files",
            next_step: "Ready for implementation",
            timestamp: now - 11_000,
          },
        ],
        tool_logs: [
          {
            tool_name: "web_search",
            tool_call_id: "call-web",
            arguments: { query: "defaultspack smoke" },
            result: { status: "ok", data: { summary: "1 result" } },
            timestamp: now - 9_000,
          },
        ],
      },
    ],
  };
}

const smokeProfile = {
  profile_id: "stub/default",
  qualified_model_id: "stub/default",
  provider_id: "stub",
  provider_display_name: "Stub",
  model_id: "default",
  display_name: "Stub Default",
  max_context: -1,
  max_context_tokens: -1,
  supports_thinking: false,
  supports_tool_calling: true,
  supports_vision: false,
  local: true,
  availability: { local: true, configured: true },
};

const googleProfile = {
  profile_id: "google/gemini-2.5-flash",
  qualified_model_id: "google/gemini-2.5-flash",
  provider_id: "google",
  provider_display_name: "Google",
  model_id: "gemini-2.5-flash",
  display_name: "Gemini 2.5 Flash",
  max_context: 1_000_000,
  max_context_tokens: 1_000_000,
  supports_thinking: true,
  supports_tool_calling: true,
  supports_vision: true,
  local: false,
  availability: { configured: true },
};

const embeddingProfile = {
  profile_id: "google/text-embedding-004",
  qualified_model_id: "google/text-embedding-004",
  provider_id: "google",
  provider_display_name: "Google",
  model_id: "text-embedding-004",
  display_name: "Text Embedding 004",
  type: "embedding",
  max_context: 2048,
  max_context_tokens: 2048,
  supports_thinking: false,
  supports_tool_calling: false,
  supports_vision: false,
  local: false,
  configured: true,
  requires_api_key: false,
  capability_tags: ["embedding"],
  recommended_roles: ["tool_embedding"],
  availability: { configured: true },
};

const opencodeProfile = {
  profile_id: "opencode-go/qwen3.5-plus",
  qualified_model_id: "opencode-go/qwen3.5-plus",
  provider_id: "opencode-go",
  provider_display_name: "OpenCode Go",
  model_id: "qwen3.5-plus",
  display_name: "Qwen3.5 Plus via OpenCode Go",
  max_context: 128_000,
  max_context_tokens: 128_000,
  supports_thinking: false,
  supports_tool_calling: true,
  supports_vision: false,
  local: false,
  availability: { configured: true },
};

const opencodeZenProfile = {
  profile_id: "opencode-zen/minimax-m3-free",
  qualified_model_id: "opencode-zen/minimax-m3-free",
  provider_id: "opencode-zen",
  provider_display_name: "OpenCode Zen",
  model_id: "minimax-m3-free",
  display_name: "MiniMax M3 Free via OpenCode Zen",
  max_context: 200_000,
  max_context_tokens: 200_000,
  supports_thinking: true,
  supports_tool_calling: true,
  supports_vision: true,
  local: false,
  availability: { configured: false, status: "requires_api_key" },
};

const sidebarItems = [
  {
    id: "web_search",
    label: "Web Search",
    category: "tool",
    description: "Search the web from the composer.",
    tags: ["research"],
    risk: "medium",
    ui: {
      group_id: "research",
      group_label: "Research",
      group_icon: "search",
      item_icon: "web_search",
      widget_kind: "tool_toggle",
      drop_capabilities: ["composer.toggle_chip"],
      composer_label: "Web Search",
      composer_description: "Search the web.",
    },
    panel: {
      kind: "tool",
      title: "Web Search",
      notes: ["Mocked for Playwright smoke coverage."],
    },
  },
  {
    id: "github_issue_search",
    label: "GitHub Issues",
    category: "tool",
    description: "Search GitHub issues and pull requests.",
    tags: ["github", "issues"],
    risk: "medium",
    ui: {
      group_id: "github",
      group_label: "GitHub",
      item_icon: "git",
      service_id: "github",
      widget_kind: "tool_toggle",
      drop_capabilities: ["composer.toggle_chip"],
      composer_label: "GitHub Issues",
      composer_description: "Search GitHub issues.",
    },
    panel: {
      kind: "tool",
      title: "GitHub Issues",
      notes: ["Mocked for service-level selection coverage."],
    },
  },
  {
    id: "𐐀tool",
    label: "𐐀tool",
    category: "tool",
    description: "Supplementary-plane Unicode tool.",
    tags: ["unicode"],
    risk: "low",
    ui: {
      group_id: "research",
      group_label: "Research",
      widget_kind: "tool_toggle",
      drop_capabilities: ["composer.toggle_chip"],
      composer_label: "𐐀tool",
    },
  },
  {
    id: "scheduler",
    label: "Scheduler",
    category: "tool",
    description: "Schedule and trigger controls.",
    ui: { item_icon: "calendar" },
    panel: {
      kind: "actions",
      title: "Scheduler",
      notes: ["Calendar and trigger smoke surface."],
      actions: [
        {
          id: "schedules.list",
          label: "Calendar",
          icon: "schedules",
          method: "GET",
        },
      ],
    },
  },
];

const catalogSkills = [
  {
    id: "feedback/live-review",
    label: "Live Review",
    description: "Require evidence-backed verification.",
    triggers: ["PR97_LIVE_REALITY_REVIEW"],
    applies_to_tools: ["web_search"],
    aliases: ["reality", "live-review"],
  },
];

const settingsValues = {
  general: {
    language: "en",
    composer_placeholder: "Message Rumi...",
    keyboard_button_navigation: true,
    show_activity_in_messages: true,
  },
  models: {
    preferred_model: "stub/default",
    favorite_profiles: ["stub/default"],
  },
  preview: {
    max_items: 12,
    auto_open: false,
    default_mode: "auto",
  },
  calendar: {
    agent_current_chat: false,
    agent_model: "",
    agent_task_default: false,
    default_time: "09:00",
    quick_add_enabled: true,
    default_item_type: "task",
    week_start: "sunday",
    show_outside_days: true,
    show_time_picker: true,
    dim_weekends: true,
    task_color: "blue",
    time_slot_minutes: 15,
    event_color: "green",
    max_items_per_day: 3,
  },
  chat_rendering: {
    unknown_block_strategy: "hidden",
    show_widgets: true,
  },
  sidebar: {
    pinned_item_ids: ["web_search", "scheduler"],
    starred_item_ids: [],
    custom_tool_tags: {},
  },
  tools: {
    default_mode: "auto",
    selection_strategy: "hybrid",
    semantic_candidate_limit: 24,
    final_tool_limit: 8,
    catalog_ai_direct_limit: 80,
    selector_trace: "summary",
    standard_permissions: {
      read: "auto",
      search: "auto",
      create: "confirm",
      update: "confirm",
      send: "confirm",
      execute: "confirm",
      computer: "confirm",
      delete: "confirm",
    },
    service_permission_overrides: {},
    embedding_model: "",
  },
  commands: {},
};

const settingsSections = [
  {
    id: "tools",
    label: "機能と接続",
    description: "機能の選定、接続、実行時権限を管理します。",
    fields: [],
  },
  {
    id: "calendar",
    label: "カレンダー",
    description: "Calendar behavior.",
    fields: Array.from({ length: 14 }, (_, index) => ({
      id: `calendar_field_${index + 1}`,
      label: `Calendar Field ${index + 1}`,
      type: "text",
      default: "",
    })),
  },
];

const toolCatalogServices = [
  {
    service_id: "web",
    label: "Web検索",
    summary: "Web、検索、オンライン情報を扱います",
    connection_status: "connected",
    tool_count: 1,
    action_classes: ["search"],
  },
  {
    service_id: "github",
    label: "GitHub",
    summary: "リポジトリ、Issue、Pull Requestを扱います",
    connection_status: "connected",
    tool_count: 1,
    action_classes: ["search"],
  },
  {
    service_id: "calendar",
    label: "Calendar",
    summary: "予定やカレンダーを扱います",
    connection_status: "connected",
    tool_count: 1,
    action_classes: ["read"],
  },
];

const toolCatalogTools = [
  {
    tool_id: "web_search",
    service_id: "web",
    service_label: "Web検索",
    name: "Web Search",
    summary: "Search the web.",
    action_class: "search",
    risk: "medium",
    connection_status: "connected",
    minimum_permission: "auto",
    tags: ["research"],
  },
  {
    tool_id: "github_issue_search",
    service_id: "github",
    service_label: "GitHub",
    name: "GitHub Issues",
    summary: "Search GitHub issues and pull requests.",
    action_class: "search",
    risk: "medium",
    connection_status: "connected",
    minimum_permission: "auto",
    tags: ["github"],
  },
  {
    tool_id: "scheduler",
    service_id: "calendar",
    service_label: "Calendar",
    name: "Scheduler",
    summary: "Schedule and trigger controls.",
    action_class: "read",
    risk: "low",
    connection_status: "connected",
    minimum_permission: "auto",
    tags: ["calendar"],
  },
];

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(ok(data)),
  });
}

async function fulfillStream(route: Route, message: Record<string, unknown>) {
  await route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: [
      `data: ${JSON.stringify({ type: "message", message })}`,
      "",
      `data: ${JSON.stringify({ type: "done", message })}`,
      "",
    ].join("\n"),
  });
}

async function fulfillStreamEvents(route: Route, events: Record<string, unknown>[]) {
  await route.fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join(""),
  });
}

async function installDefaultspackApiMocks(page: Page, options: ApiMockOptions = {}) {
  await page.addInitScript(() => {
    localStorage.clear();
  });

  let currentSettingsValues: Record<string, Record<string, unknown>> = JSON.parse(JSON.stringify({
    ...settingsValues,
    ...(options.initialSettingsValues ?? {}),
    general: {
      ...settingsValues.general,
      ...(options.initialSettingsValues?.general ?? {}),
    },
  }));
  let conversationToolPreferences: Record<string, unknown> = {};
  let codingApprovalRequest: Record<string, unknown> | null = null;
  const codingCheckpoints: Record<string, unknown>[] = options.codingApprovalAfterRestore
    ? [{ snapshot_id: "checkpoint-1", path: "/repo/.rumi/checkpoints/checkpoint-1" }]
    : [];
  const mcpServers = [
    { server_id: "filesystem", name: "Filesystem MCP", transport: "stdio", connected: true, permissions: { approved: true }, tools: ["mcp_fs_read_file"] },
  ];

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const conversation = smokeConversation();
    options.conversationMutator?.(conversation);

    if (path === "/api/health") {
      return fulfill(route, { status: "ok", pack: "defaultspack", ts: "2026-05-20T00:00:00Z" });
    }

    if (path === "/api/ambient/status" || path === "/api/ambient/permissions/check") {
      return fulfill(route, {
        ambient_monitor: { enabled: false },
        services: {
          voice_wake_monitor: { enabled: false, status: "paused" },
          gesture_wake_monitor: { enabled: false, status: "paused" },
        },
        permissions: { rumi: {}, os: {} },
        routing: { mode: "selected_chat" },
      });
    }

    if (path.startsWith("/api/authority/requests/") && method === "GET") {
      const requestId = decodeURIComponent(path.slice("/api/authority/requests/".length));
      options.onAuthorityRequest?.(requestId);
      return fulfill(route, {
        request_id: requestId,
        status: options.authorityRequestStatus?.() ?? "pending",
        principal_id: "browser-qa-principal",
        permission_id: "ambient.trigger.dispatch",
        resource: { pack_id: "rumi_ambient_trigger_pack" },
        reason: "Browser QA",
        risk_level: "medium",
        created_at: "2026-07-19T00:00:00Z",
        expires_at: "2099-07-19T00:05:00Z",
      });
    }

    if (path === "/api/ui/catalog") {
      return fulfill(route, {
        app: { id: "defaultspack", name: "Rumi", account: { display_name: "Smoke User", plan_label: "Local" } },
        agent_service: { profiles: [], capabilities: [], presets: [] },
        sidebar: {
          filters: [
            { id: "all", label: "All" },
            { id: "tool", label: "Tools" },
            { id: "system", label: "System" },
          ],
          items: sidebarItems,
        },
        settings: { sections: settingsSections, values: currentSettingsValues },
        chat_rendering: { renderers: [] },
        composer_inputs: options.structuredComposer ? [{
          id: "contract_composer",
          label: "入力オプション",
          description: "送信時の補助情報を設定します。",
          modes: ["chat", "coding", "agent"],
          enabled: true,
          fields: [
            { id: "intent", type: "select", label: "目的", default: "review", options: [{ value: "review", label: "レビュー" }] },
            { id: "detail", type: "select", label: "詳細度", default: "rich", options: [{ value: "rich", label: "リッチ" }] },
            { id: "tone", type: "select", label: "文体", default: "natural", options: [{ value: "natural", label: "自然" }] },
            { id: "note", type: "text", label: "補足", placeholder: "任意の補足" },
          ],
        }] : [],
        skills: catalogSkills,
        extension_points: [],
      });
    }

    if (path === "/api/ui/settings" && method === "PUT") {
      const payload = request.postDataJSON() as {
        values?: Record<string, Record<string, unknown>>;
      };
      currentSettingsValues = JSON.parse(JSON.stringify(payload.values ?? currentSettingsValues));
      return fulfill(route, { sections: settingsSections, values: currentSettingsValues });
    }

    if (path === "/api/ui/settings") {
      return fulfill(route, { sections: settingsSections, values: currentSettingsValues });
    }

    if (path === "/api/ui/commands") {
      return fulfill(route, {
        commands: [
          {
            id: "coding",
            name: "coding",
            label: "Coding Mode",
            description: "Toggle coding mode.",
            category: "mode",
            visibility: "default",
            risk: "low",
            modes: ["chat", "coding", "agent"],
            execution: { type: "frontend", action: "set_mode_coding" },
          },
          {
            id: "yolo",
            name: "yolo",
            label: "Full Access (YOLO)",
            description: "Toggle Full Access and Ask approval.",
            category: "mode",
            visibility: "default",
            risk: "medium",
            modes: ["chat", "coding", "agent"],
            execution: { type: "frontend", action: "toggle_ultra_yolo" },
          },
        ],
      });
    }

    if (path === "/api/ui/commands/execute" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      return fulfill(route, {
        executed: true,
        action: payload.command === "coding"
          ? "set_mode_coding"
          : payload.command === "yolo"
            ? "toggle_ultra_yolo"
            : "",
      });
    }

    if (path === "/api/ai/profiles") {
      return fulfill(route, { profiles: [smokeProfile, googleProfile, opencodeProfile, opencodeZenProfile], count: 4 });
    }

    if (path === "/api/ai/models/search" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const types = Array.isArray(payload.type)
        ? payload.type.map((item) => String(item).trim())
        : [String(payload.type ?? "").trim()];
      const models = types.includes("embedding")
        ? [embeddingProfile]
        : [smokeProfile, googleProfile, opencodeProfile, opencodeZenProfile];
      return fulfill(route, { models, count: models.length });
    }

    if (path === "/api/tools/catalog") {
      return fulfill(route, {
        services: toolCatalogServices,
        tools: toolCatalogTools,
        count: toolCatalogTools.length,
      });
    }

    if (path === "/api/tools/selection/preview" && method === "POST") {
      return fulfill(route, {
        preview_id: "preview-tool-selection",
        expires_at: "2026-05-20T00:05:00Z",
        decision: {
          selected_tools: ["web_search", "github_issue_search"],
          selected_services: toolCatalogServices.slice(0, 2),
          recommendations: [
            { tool_id: "web_search", confidence: 0.8, reason: "web search requested" },
            { tool_id: "github_issue_search", confidence: 0.7, reason: "GitHub context requested" },
          ],
          permission_summary: { auto: 2, confirm: 0, block: 0 },
          metadata: {},
        },
      });
    }

    if (path === "/api/chat/conversations" && method === "GET") {
      return fulfill(route, { conversations: [{ ...conversation, messages: [] }], total: 1 });
    }

    if (path === "/api/chat/conversations" && method === "POST") {
      return fulfill(route, conversation);
    }

    if (path === "/api/chat/conversations/c-smoke/stream" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onStreamRequest?.(payload);
      const message = {
        id: "m-assistant-streamed",
        role: "assistant",
        content: [{ type: "text", text: "Structured response accepted." }],
        raw_text: "Structured response accepted.",
        created_at: now + 1_000,
        conversation_id: "c-smoke",
        parent_id: "m-user-sent",
        children_ids: [],
        sequence_number: 4,
        finish_reason: "stop",
        usage: null,
        widget: null,
        model: "stub/default",
        metadata: {},
        events: [],
        tool_logs: [],
      };
      if (options.streamEvents) {
        return fulfillStreamEvents(route, options.streamEvents(message));
      }
      return fulfillStream(route, message);
    }

    if (path === "/api/chat/conversations/c-smoke") {
      return fulfill(route, conversation);
    }

    if ((path === "/api/conversations/c-smoke/tool-preferences" || path === "/api/chat/conversations/c-smoke/tool-preferences") && method === "PUT") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      conversationToolPreferences = (payload.preferences && typeof payload.preferences === "object" && !Array.isArray(payload.preferences))
        ? payload.preferences as Record<string, unknown>
        : {};
      return fulfill(route, { conversation_id: "c-smoke", preferences: conversationToolPreferences });
    }

    if (path === "/api/conversations/c-smoke/tool-preferences" || path === "/api/chat/conversations/c-smoke/tool-preferences") {
      return fulfill(route, { conversation_id: "c-smoke", preferences: conversationToolPreferences });
    }

    if (path === "/api/ui/conversations/c-smoke/preview") {
      return fulfill(route, {
        conversation_id: "c-smoke",
        previews: [
          {
            id: "preview-calendar",
            toolStepId: "call-files",
            timestamp: now - 8_000,
            data: {
              type: "file",
              filename: "calendar-smoke.json",
              size: "tool artifact",
              content: '{ "job": "nightly-review", "status": "ready" }',
            },
          },
        ],
        summary: { file: 1 },
      });
    }

    if (path === "/api/chat/steer") {
      return fulfill(route, { items: [] });
    }

    if (path === "/api/agent/schedules") {
      return fulfill(route, {
        schedules: [
          { id: "nightly-review", name: "nightly-review", schedule: "every 1h", next_run_at: "2026-05-20T12:00:00Z" },
        ],
      });
    }

    if (path === "/api/coding/workspaces") {
      return fulfill(route, {
        workspaces: [{ workspace_id: "ws-main", label: "Main Repo", root_path: "/repo", trusted: true }],
        selected_workspace_id: "ws-main",
      });
    }

    if (path === "/api/coding/context") {
      return fulfill(route, {
        branch: "main",
        root_folder: "/repo",
        workspace_id: "ws-main",
        workspace_root: "/repo",
        directory: ".",
        files: ["src/App.tsx", "README.md"],
        entries: [
          { name: "src", path: "src", is_dir: true, size: 0 },
          { name: "README.md", path: "README.md", is_dir: false, size: 200 },
        ],
        git: { branch: "main", clean: false, modified: ["src/App.tsx"], untracked: [], staged: [] },
      });
    }

    if (path === "/api/coding/files/read" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      await options.beforeWorkspaceFileReadResponse?.(payload);
      return fulfill(route, {
        path: String(payload.path ?? "README.md"),
        content: "# Fixture\n",
        size: 10,
        encoding: "utf-8",
        workspace_id: "ws-main",
        workspace_root: "/repo",
      });
    }

    if (path === "/api/coding/git/branch") {
      return fulfill(route, { branch: "main", branches: ["main", "codex/pr97"], workspace_id: "ws-main" });
    }

    if (path === "/api/coding/git/status") {
      return fulfill(route, { branch: "main", clean: false, modified: ["src/App.tsx"], untracked: [], staged: [] });
    }

    if (path === "/api/coding/git/diff") {
      return fulfill(route, { diff: "-old\n+new", files_changed: 1, files: ["src/App.tsx"], workspace_id: "ws-main" });
    }

    if (path === "/api/coding/approvals/approve" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onApprovalDecision?.("approve", payload);
      if (codingApprovalRequest?.request_id === payload.approval_request_id) {
        codingApprovalRequest = { ...codingApprovalRequest, status: "approved" };
      }
      return fulfill(route, {
        request_id: payload.approval_request_id,
        approved: true,
        token: "approved-mcp-token",
      });
    }

    if (path === "/api/coding/approvals/deny" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      options.onApprovalDecision?.("deny", payload);
      return fulfill(route, { request_id: payload.approval_request_id, approved: false, status: "denied" });
    }

    if (path === "/api/coding/terminal/exec" && method === "POST" && options.codingApprovalAfterTerminal) {
      const payload = request.postDataJSON() as Record<string, unknown>;
      codingApprovalRequest = {
        request_id: "apr-terminal-write",
        operation: "terminal.exec",
        risk_level: "high",
        status: "pending",
        display_summary: "terminal.exec: write qa-file.txt",
        created_at: now,
      };
      return fulfill(route, {
        command: String(payload.command ?? ""),
        classification: "high",
        risk_reasons: ["write"],
        approval_required: true,
        approval_request_id: "apr-terminal-write",
        exit_code: null,
        stdout: "",
        stderr: "",
      });
    }

    if (path === "/api/coding/files/restore" && method === "POST" && options.codingApprovalAfterRestore) {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const snapshotId = String(payload.snapshot_id ?? "checkpoint-1");
      if (payload.approval_token) {
        return fulfill(route, { restored: true, snapshot_id: snapshotId });
      }
      codingApprovalRequest = {
        request_id: `apr-${snapshotId}-restore`,
        operation: "file.restore",
        risk_level: "high",
        status: "pending",
        display_summary: `file.restore: ${snapshotId}`,
        created_at: now,
      };
      return fulfill(route, {
        approval_required: true,
        approval_request: codingApprovalRequest,
      });
    }

    if (path === "/api/coding/approvals") {
      const requests = codingApprovalRequest ? [codingApprovalRequest] : [];
      return fulfill(route, { requests, pending: requests, count: requests.length });
    }

    if (path === "/api/coding/checkpoints") {
      if (method === "POST") {
        const checkpoint = {
          snapshot_id: "checkpoint-2",
          path: "/repo/.rumi/checkpoints/checkpoint-2",
        };
        codingCheckpoints.unshift(checkpoint);
        return fulfill(route, { checkpoint, workspace_id: "ws-main", workspace_root: "/repo" });
      }
      return fulfill(route, {
        checkpoints: codingCheckpoints,
        workspace_id: "ws-main",
        workspace_root: "/repo",
      });
    }

    if (path === "/api/coding/rumi-log") {
      return fulfill(route, {
        rumi_dir: "/repo/.rumi",
        events_path: "/repo/.rumi/events.jsonl",
        events: [],
        summary: {
          total: 0,
          by_kind: {},
          by_status: {},
          agent_ids: [],
          commit_count: 0,
          push_count: 0,
          plan_count: 0,
          task_count: 0,
          conversation_count: 0,
          mention_count: 0,
          last_event_at: null,
          last_commit_hash: null,
        },
        workspace_id: "ws-main",
        workspace_root: "/repo",
        created: false,
      });
    }

    if (path === "/api/browser/artifacts") {
      return fulfill(route, {
        artifacts: [{ artifact_id: "browser-1", session_id: "s1", action: "browser.session", created_at: "2026-05-20T00:00:00Z", url: "https://example.com" }],
        count: 1,
      });
    }

    if (path === "/api/tools/mcp" && method === "POST") {
      const payload = request.postDataJSON() as { server?: Record<string, unknown> };
      const server = {
        server_id: String(payload.server?.server_id ?? "contract_digest"),
        name: String(payload.server?.name ?? payload.server?.server_id ?? "contract_digest"),
        transport: "stdio",
        connected: false,
        permissions: { approved: false },
        tools: [],
      };
      mcpServers.push(server);
      return fulfill(route, { server });
    }

    if (path === "/api/tools/mcp/connect" && method === "POST") {
      const payload = request.postDataJSON() as Record<string, unknown>;
      const serverId = String(payload.server_id ?? payload.server_name ?? "contract_digest");
      if (!payload.approval_token) {
        return fulfill(route, {
          approval_required: true,
          approval_request_id: "apr-mcp-contract",
          server_id: serverId,
        });
      }
      const server = mcpServers.find((item) => item.server_id === serverId);
      if (server) {
        server.connected = true;
        server.permissions = { approved: true };
        server.tools = [`mcp__${serverId}__digest`];
      }
      return fulfill(route, {
        server_id: serverId,
        server_name: serverId,
        status: "connected",
        tools: [`mcp__${serverId}__digest`],
        permission: { approved: true, source: "approval" },
      });
    }

    if (path === "/api/tools/mcp") {
      return fulfill(route, {
        servers: mcpServers,
        count: mcpServers.length,
      });
    }

    return fulfill(route, {});
  });
}

async function openDefaultspack(page: Page, path = "/chat", options: ApiMockOptions = {}) {
  await installDefaultspackApiMocks(page, options);
  await page.goto(path);
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();
}

async function openCodingWidget(page: Page, options: ApiMockOptions = {}) {
  await openDefaultspack(page, "/chat", options);
  await page.locator("textarea.rumi-composer-textarea").fill("/coding");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  const codingWidgetButton = page.getByRole("button", { name: "Coding widget" });
  await expect(codingWidgetButton).toBeVisible();
  await codingWidgetButton.click();
  await expect(page.locator(".coding-cockpit")).toBeVisible();
  await page.getByRole("button", { name: "Workspace", exact: true }).click();
}

test("document scroll fallback survives small and keyboard-like viewports", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 520 });
  await openDefaultspack(page, "/static/chat");

  await expect(page.locator(".rumi-app-shell")).toBeVisible();
  await expect(page.locator(".rumi-workspace-main")).toHaveCSS("min-height", "0px");

  for (const viewport of [
    { width: 390, height: 520 },
    { width: 320, height: 620 },
    { width: 390, height: 340 },
  ]) {
    await page.setViewportSize(viewport);
    await page.evaluate(() => {
      document.querySelector("[data-qa-scroll-fallback]")?.remove();
      const fallbackProbe = document.createElement("div");
      fallbackProbe.dataset.qaScrollFallback = "true";
      fallbackProbe.style.height = "80vh";
      fallbackProbe.style.pointerEvents = "none";
      document.body.appendChild(fallbackProbe);
      window.scrollTo(0, 0);
    });

    await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).overflowY)).not.toBe("hidden");
    await page.mouse.wheel(0, viewport.height);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  }
});

test("tool hub search suggestions close on outside click while keeping filtered actions usable", async ({ page }) => {
  await openDefaultspack(page);

  await page.locator('button[title="機能"]').click();
  const search = page.getByPlaceholder("機能を検索");
  await search.fill("web");
  await expect(page.getByTestId("tool-manager-candidates")).toBeVisible();
  await expect(page.getByTestId("tool-manager-candidates")).toContainText("Web Search");

  await page.getByRole("heading", { name: "機能" }).click();
  await expect(page.getByTestId("tool-manager-candidates")).toBeHidden();
  await expect(search).toHaveValue("web");

  await page.getByRole("button", { name: "表示中を今回使う" }).click();
  await expect(page.locator(".rumi-composer-frame")).toContainText("Web Search");
});

test("composer approval menu opens action permissions while selection modes live in settings", async ({ page }) => {
  await openDefaultspack(page);

  await page.getByRole("button", { name: "アクションの承認方法" }).click();
  const approvalMenu = page.getByRole("menu", { name: "アクションの承認方法" });
  await expect(approvalMenu).toContainText("Codex アクションの承認方法");
  await expect(approvalMenu).toContainText("承認を求める");
  await expect(approvalMenu).toContainText("代理で承認");
  await expect(approvalMenu).toContainText("フルアクセス");
  await expect(approvalMenu).toContainText("カスタム（設定）");
  await expect(approvalMenu).not.toContainText("自動で選ぶ");

  await approvalMenu.getByRole("button", { name: "詳細はこちら" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Tools & MCP" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Tools & MCP" }).first()).toBeVisible();
  await expect(page.getByText("Installed tools, MCP servers, discovered tools, visibility, and approval policy.")).toBeVisible();
  await expect(page.getByText("Tools are not logins")).toBeVisible();
  await expect(page.getByText("Safety rules")).toBeVisible();
  await expect(page.getByText("Tool source → Tools & MCP")).toBeVisible();
  await expect(page.getByText("MCP servers can require a connection")).toBeVisible();
});

test("slash yolo toggles Full Access back to Ask without a duplicate status chip", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  const approval = page.getByRole("button", { name: "アクションの承認方法" });
  await expect(approval).toContainText("承認");

  await composer.fill("/yolo");
  await composer.press("Enter");
  await expect(approval).toContainText("フル");
  await expect(page.locator('[data-composer-widget="active-command-state"]')).toHaveCount(0);
  await expect(page.locator('[data-composer-widget="yolo-status"]')).toHaveCount(0);

  await composer.fill("/yolo");
  await composer.press("Enter");
  await expect(approval).toContainText("承認");
  await expect(page.locator('[data-composer-widget="active-command-state"]')).toHaveCount(0);
});

test("new chat structured options open above the compact composer and apply values", async ({ page }) => {
  await openDefaultspack(page, "/chat", { structuredComposer: true });
  await page.getByRole("button", { name: "New Chat", exact: true }).click();

  const options = page.locator('[data-structured-composer="contract_composer"] > button[aria-haspopup="dialog"]');
  await expect(options).toHaveAttribute("aria-expanded", "false");
  await expect(options).toContainText("3/4");
  await options.click();

  const dialog = page.getByRole("dialog", { name: "入力オプション" });
  await expect(dialog).toBeVisible();
  await expect(options).toHaveAttribute("aria-expanded", "true");
  await dialog.getByLabel("補足").fill("比較対象を含める");
  await dialog.getByRole("button", { name: "入力に反映" }).click();
  await expect(dialog).toBeHidden();
  await expect(options).toContainText("4/4");

  const panelHeight = await page.locator(".rumi-composer-main-panel").evaluate((element) => element.getBoundingClientRect().height);
  expect(panelHeight).toBeLessThanOrEqual(133);
});

test("browser approval uses the shared user-first decision surface at narrow width", async ({ page }) => {
  let denialPayload: Record<string, unknown> | null = null;
  await page.setViewportSize({ width: 390, height: 844 });
  await openDefaultspack(page, "/static/chat", {
    conversationMutator: (conversation) => {
      conversation.messages[1].events.push({
        type: "approval_requested",
        phase: "approval_requested",
        approval_required: true,
        approval_request_id: "approval-browser-contract",
        tool_name: "browser_computer",
        action: "browser.open_url",
        risk_level: "medium",
        display_summary: "example.test を開き、ページ内容を外部サイトから読み込みます。",
        payload: { url: "https://example.test/long/path" },
        timestamp: now,
      });
    },
    onApprovalDecision: (decision, payload) => {
      if (decision === "deny") denialPayload = payload;
    },
  });

  const surface = page.locator('[data-approval-source="browser"]');
  await expect(surface).toBeVisible();
  await expect(surface).toContainText("Rumi が許可を求めています");
  await expect(surface).toContainText("https://example.test/long/path");
  await expect(surface).toContainText("必要な理由");
  await expect(surface).toContainText("許可範囲");
  await expect(surface.getByText("技術的な詳細")).toBeVisible();
  await expect(surface.locator("pre")).toBeHidden();
  await expect(surface.getByRole("button", { name: "拒否（2）" })).toBeVisible();
  await expect(surface.getByRole("button", { name: "許可（3）" })).toBeVisible();

  const composer = page.locator("textarea.rumi-composer-textarea");
  await composer.fill("2");
  await page.keyboard.press("3");
  await expect(composer).toHaveValue("23");
  await expect(surface).toBeVisible();

  await surface.getByRole("button", { name: "拒否（2）" }).click();
  await expect(surface).toBeHidden();
  expect(denialPayload).toMatchObject({ approval_request_id: "approval-browser-contract" });
});

test("settings modal contains focus, dismisses nested layers in order, and restores its opener", async ({ page }) => {
  await installDefaultspackApiMocks(page);
  await page.goto("/static/");
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();

  const opener = page.getByTitle("Settings").last();
  await opener.focus();
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Settings" });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeFocused();
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await expect(page.getByRole("button", { name: "Close settings" })).toBeVisible();

  const backgroundState = await page.getByTestId("settings-modal-layer").evaluate((layer) => (
    Array.from(layer.parentElement?.children ?? [])
      .filter((element) => element !== layer)
      .map((element) => ({ inert: (element as HTMLElement).inert, ariaHidden: element.getAttribute("aria-hidden") }))
  ));
  expect(backgroundState.length).toBeGreaterThan(0);
  expect(backgroundState.every((state) => state.inert && state.ariaHidden === "true")).toBe(true);

  const focusWrapResult = await dialog.evaluate((element) => {
    const selector = "button:not([disabled]),[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex='-1'])";
    const focusable = Array.from(element.querySelectorAll<HTMLElement>(selector)).filter((item) => item.offsetParent !== null);
    focusable.at(-1)?.focus();
    return focusable.length;
  });
  expect(focusWrapResult).toBeGreaterThan(1);
  await page.keyboard.press("Tab");
  await expect.poll(() => page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null)).toBe(true);

  const placementTrigger = page.getByRole("button", { name: "Add an item to Settings" });
  await placementTrigger.click();
  await expect(page.getByRole("menu", { name: "Add an item to Settings" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu", { name: "Add an item to Settings" })).toBeHidden();
  await expect(dialog).toBeVisible();
  await expect(placementTrigger).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.click();
  await expect(dialog).toBeVisible();
  await page.getByTestId("settings-modal-layer").click({ position: { x: 2, y: 2 } });
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.click();
  await expect(dialog).toBeVisible();
  await page.setViewportSize({ width: 390, height: 640 });
  const narrowBounds = await dialog.boundingBox();
  expect(narrowBounds).not.toBeNull();
  expect(narrowBounds!.x).toBeGreaterThanOrEqual(0);
  expect(narrowBounds!.y).toBeGreaterThanOrEqual(0);
  expect(narrowBounds!.x + narrowBounds!.width).toBeLessThanOrEqual(390);
  expect(narrowBounds!.y + narrowBounds!.height).toBeLessThanOrEqual(640);
  await page.getByRole("button", { name: "Close settings" }).click();
  await expect(dialog).toBeHidden();
});

test("ambient approval return hints settle only after one-time correlation and backend verification", async ({ page }, testInfo) => {
  let authorityStatus: "pending" | "approved" | "denied" = "pending";
  const authorityRequests: string[] = [];
  const consoleErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  await openDefaultspack(page, "/ambient-debug?authority_approved=1", {
    authorityRequestStatus: () => authorityStatus,
    onAuthorityRequest: (requestId) => authorityRequests.push(requestId),
  });

  const verificationFailure = page.getByText(
    "承認結果を確認できませんでした。承認画面を開いたまま、もう一度確認してください。",
    { exact: true },
  );
  await expect(verificationFailure).toBeVisible();
  await expect.poll(() => page.url()).not.toContain("authority_approved");
  expect(authorityRequests).toEqual([]);

  const createReturnPath = () => page.evaluate(async () => {
    const loadEvents = Function("return import('/src/lib/authorityApprovalEvents.ts')") as () => Promise<{
      createAuthorityApprovalReturnPath: (requestId: string, currentHref: string) => string;
    }>;
    const events = await loadEvents();
    return events.createAuthorityApprovalReturnPath("rumi_ambient_trigger_pack", window.location.href);
  });

  const pendingPath = await createReturnPath();
  const pendingUrl = new URL(pendingPath, page.url()).href;
  await page.goto(pendingUrl);
  await expect.poll(() => authorityRequests.length).toBe(1);
  await expect(verificationFailure).toBeVisible();
  expect(authorityRequests).toEqual(["rumi_ambient_trigger_pack"]);
  await expect.poll(() => page.url()).not.toContain("authority_return");
  expect(await page.evaluate(() => sessionStorage.getItem("rumi.authority.approval.return.v1"))).toBeNull();

  authorityStatus = "approved";
  const approvedPath = await createReturnPath();
  const approvedUrl = new URL(approvedPath, page.url()).href;
  await page.goto(approvedUrl);
  await expect(page.getByText(
    "承認を確認しました。次にMacのマイク/カメラを確認します。",
    { exact: true },
  )).toBeVisible();
  await expect.poll(() => authorityRequests.length).toBe(2);

  await page.goto(approvedUrl);
  await expect(verificationFailure).toBeVisible();
  await page.waitForTimeout(100);
  expect(authorityRequests).toHaveLength(2);

  await page.setViewportSize({ width: 390, height: 720 });
  const panelBounds = await page.locator('section[aria-label="指で録音"]').boundingBox();
  expect(panelBounds).not.toBeNull();
  expect(panelBounds!.x).toBeGreaterThanOrEqual(0);
  expect(panelBounds!.x + panelBounds!.width).toBeLessThanOrEqual(390);
  expect(consoleErrors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("authority-return-verification-mobile.png"), fullPage: true });
});

test("tool hub service selections can be scoped to the conversation and survive reload", async ({ page }) => {
  await openDefaultspack(page);

  await page.locator('button[title="機能"]').click();
  await page.getByRole("button", { name: "この会話" }).click();
  const githubCard = page.locator("div.rounded-md").filter({ hasText: "GitHub" }).first();
  await expect(githubCard).toBeVisible();
  await githubCard.getByTitle("サービスを使う").click();
  await expect(githubCard).toContainText("会話固定");

  await page.reload();
  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();
  await page.locator('button[title="機能"]').click();
  await page.getByRole("button", { name: "この会話" }).click();
  const reloadedGithubCard = page.locator("div.rounded-md").filter({ hasText: "GitHub" }).first();
  await expect(reloadedGithubCard).toContainText("会話固定");
});

test("composer at mention selects tools skills and services with semantic metadata", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });

  const composer = page.locator("textarea.rumi-composer-textarea");
  await composer.fill("Use @web");
  const mentions = page.getByTestId("composer-at-mention-candidates");
  await expect(mentions).toBeVisible();
  await expect(mentions).toContainText("@Web Search");
  await expect(mentions).not.toContainText("web_search");

  await composer.press("Enter");
  await expect(composer).toHaveValue("Use @Web Search ");
  await expect(page.locator(".rumi-composer-frame")).toContainText("Web Search");

  await composer.pressSequentially("@live");
  await expect(mentions).toBeVisible();
  await expect(mentions).toContainText("@Live Review");
  await expect(mentions).not.toContainText("feedback/live-review");
  await page.getByRole("option", { name: /@live review/i }).click();
  await expect(composer).toHaveValue("Use @Web Search @Live Review ");
  await expect(page.locator(".rumi-composer-frame")).toContainText("Live Review");

  await composer.press("End");
  await composer.pressSequentially("@gith");
  await expect(mentions).toBeVisible();
  const githubService = page.getByRole("option").filter({ hasText: "@GitHub" }).filter({ hasText: "service" });
  await expect(githubService).toBeVisible();
  await githubService.click();
  await expect(composer).toHaveValue("Use @Web Search @Live Review @GitHub ");

  await page.locator(".rumi-send-button").click();
  await expect.poll(() => streamRequests.length).toBe(1);

  const request = streamRequests[0];
  expect(request.tools).toEqual(["web_search", "github_issue_search"]);
  const params = request.params as Record<string, unknown>;
  const toolSelection = params.tool_selection as Record<string, unknown>;
  expect(toolSelection.mode).toBe("manual");
  expect(toolSelection.scope).toBe("turn");
  expect(toolSelection.include).toEqual([
    { kind: "tool", id: "web_search" },
    { kind: "tool", id: "github_issue_search" },
  ]);

  const message = request.message as Record<string, unknown>;
  expect(message.content).toBe("Use @Web Search @Live Review @GitHub");
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.selected_tools).toEqual(["web_search", "github_issue_search"]);
  expect(metadata.skills).toEqual(["feedback/live-review"]);
  expect(metadata.skill_mentions).toEqual([{ id: "feedback/live-review", label: "Live Review" }]);
  expect(metadata.mentions).toEqual([
    { id: "web_search", kind: "tool", label: "Web Search", syntax: "@Web Search" },
    { id: "feedback/live-review", kind: "skill", label: "Live Review", syntax: "@Live Review" },
    { id: "github", kind: "service", label: "GitHub", syntax: "@GitHub" },
  ]);
  expect(metadata.dropped_widgets).toEqual([
    expect.objectContaining({
      id: "web_search",
      type: "tool",
      label: "Web Search",
      widgetKind: "tool_toggle",
      sourceItemId: "web_search",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          id: "web_search",
          kind: "tool",
          label: "Web Search",
          syntax: "@Web Search",
          tool_id: "web_search",
        },
        tool: expect.objectContaining({
          id: "web_search",
          label: "Web Search",
          tags: ["research"],
        }),
      }),
    }),
    expect.objectContaining({
      id: "feedback/live-review",
      type: "skill",
      label: "Live Review",
      widgetKind: "skill_prompt",
      sourceItemId: "feedback/live-review",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          id: "feedback/live-review",
          kind: "skill",
          label: "Live Review",
          syntax: "@Live Review",
          skill_id: "feedback/live-review",
        },
        skill: expect.objectContaining({
          id: "feedback/live-review",
          label: "Live Review",
          aliases: ["reality", "live-review"],
        }),
      }),
    }),
    expect.objectContaining({
      id: "mention-service:github",
      type: "service",
      label: "GitHub",
      widgetKind: "service_reference",
      sourceItemId: "github",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          id: "github",
          kind: "service",
          label: "GitHub",
          syntax: "@GitHub",
        },
        service: {
          id: "github",
          label: "GitHub",
          tool_ids: ["github_issue_search"],
        },
      }),
    }),
  ]);
});

test("composer removes semantic tool state after an escaped edit", async ({ page }) => {
  const escapedRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => escapedRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });

  await composer.fill("Use @web");
  await composer.press("Enter");
  await composer.fill("Use \\@Web Search");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => escapedRequests.length).toBe(1);

  const escapedRequest = escapedRequests[0];
  expect(escapedRequest.tools).toBeUndefined();
  const escapedSelection = (escapedRequest.params as Record<string, unknown>)
    .tool_selection as Record<string, unknown>;
  expect(escapedSelection).toMatchObject({ mode: "manual", include: [] });
  const escapedMetadata = (escapedRequest.message as Record<string, unknown>)
    .metadata as Record<string, unknown>;
  expect(escapedMetadata.mentions).toBeUndefined();
  expect(escapedMetadata.selected_tools).toBeUndefined();
  expect(escapedMetadata.dropped_widgets).toEqual([]);
});

test("composer removes semantic tool state after its chip is toggled off", async ({ page }) => {
  const chipRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => chipRequests.push(payload),
  });
  const chipComposer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await chipComposer.fill("Use @web");
  await chipComposer.press("Enter");
  await page.locator('.rumi-composer-frame button[title="Search the web."]').click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => chipRequests.length).toBe(1);
  const chipRequest = chipRequests[0];
  expect(chipRequest.tools).toBeUndefined();
  const chipMetadata = (chipRequest.message as Record<string, unknown>)
    .metadata as Record<string, unknown>;
  expect(chipMetadata.mentions).toBeUndefined();
  expect(chipMetadata.selected_tools).toBeUndefined();
  expect(chipMetadata.dropped_widgets).toEqual([]);
});

test("composer reconciles removed service tools before submit", async ({ page }) => {
  const serviceRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => serviceRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });

  await composer.fill("Use @gith");
  await page.getByRole("option").filter({ hasText: "@GitHub" }).filter({ hasText: "service" }).click();
  await page.getByRole("button", { name: "GitHub Issues の今回指定を解除" }).click();
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => serviceRequests.length).toBe(1);
  const serviceRequest = serviceRequests[0];
  expect(serviceRequest.tools).toBeUndefined();
  const serviceMetadata = (serviceRequest.message as Record<string, unknown>)
    .metadata as Record<string, unknown>;
  expect(serviceMetadata.mentions).toBeUndefined();
  expect(serviceMetadata.selected_tools).toBeUndefined();
  expect(serviceMetadata.dropped_widgets).toEqual([]);
});

test("slash and mention candidates share one full-width JSON palette", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.fill("@");
  const mentions = page.getByTestId("composer-at-mention-candidates");
  await expect(mentions).toBeVisible();
  await expect(mentions).toHaveAttribute("data-json-list-template", "composer-at-mention");
  const mentionBox = await mentions.boundingBox();
  expect(mentionBox).not.toBeNull();

  await composer.fill("/");
  const commands = page.getByTestId("composer-slash-command-candidates");
  await expect(commands).toBeVisible();
  await expect(commands).toHaveAttribute("data-json-list-template", "composer-slash-command");
  await expect(commands).toContainText("/coding");
  const commandBox = await commands.boundingBox();
  expect(commandBox).not.toBeNull();

  expect(Math.abs(commandBox!.x - mentionBox!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(commandBox!.width - mentionBox!.width)).toBeLessThanOrEqual(1);
  await expect(composer).toHaveAttribute("aria-controls", "composer-slash-command-listbox");
  await expect(composer).toHaveAttribute("aria-activedescendant", "composer-slash-command-option-0");
});

test("composer removes file mention metadata when its attachment is removed", async ({ page }) => {
  const fileRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => fileRequests.push(payload),
  });
  const fileComposer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await fileComposer.fill("/coding");
  await fileComposer.press("Enter");
  await fileComposer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toBeVisible();
  await page.getByRole("button", { name: "README.md を削除" }).click();
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => fileRequests.length).toBe(1);
  const fileMessage = fileRequests[0].message as Record<string, unknown>;
  expect(fileMessage.attachments).toBeUndefined();
  const fileMetadata = fileMessage.metadata as Record<string, unknown>;
  expect(fileMetadata.mentions).toBeUndefined();
  expect(fileMetadata.attachments).toEqual([]);
  expect(fileMetadata.dropped_widgets).toEqual([]);
});

test("composer supplementary-plane mention keeps textarea and parser indices aligned", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });

  await composer.fill("先𐐀 @𐐀");
  await expect(page.getByRole("option", { name: /@𐐀tool/i })).toBeVisible();
  await composer.press("Enter");
  await expect(composer).toHaveValue("先𐐀 @𐐀tool ");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);
  expect(streamRequests[0].tools).toEqual(["𐐀tool"]);
  const metadata = (streamRequests[0].message as Record<string, unknown>)
    .metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "𐐀tool", kind: "tool", label: "𐐀tool", syntax: "@𐐀tool" },
  ]);
});

test("composer keeps a no-space mention disabled after its chip is toggled off", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });

  await composer.fill("Use @𐐀");
  await composer.press("Enter");
  await page.getByRole("button", { name: "𐐀tool", exact: true }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-selected-tool-ids")))
    .toBe("[]");
  await composer.press("End");
  await composer.pressSequentially(" and summarize the result");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);

  expect(streamRequests[0].tools).toBeUndefined();
  const metadata = (streamRequests[0].message as Record<string, unknown>)
    .metadata as Record<string, unknown>;
  expect(metadata.mentions).toBeUndefined();
  expect(metadata.selected_tools).toBeUndefined();
  expect(metadata.dropped_widgets).toEqual([]);
});

test("editing and reselecting a dismissed no-space mention restores it", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });

  await composer.fill("Use @𐐀");
  await composer.press("Enter");
  await page.getByRole("button", { name: "𐐀tool", exact: true }).click();
  await composer.fill("Use again @𐐀");
  await composer.press("Enter");
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);

  expect(streamRequests[0].tools).toEqual(["𐐀tool"]);
  const metadata = (streamRequests[0].message as Record<string, unknown>)
    .metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "𐐀tool", kind: "tool", label: "𐐀tool", syntax: "@𐐀tool" },
  ]);
});

test("workspace mention waits for its attachment before submit", async ({ page }) => {
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();

  const pendingSend = page.getByRole("button", { name: "ファイルを読み込み中" });
  await expect(pendingSend).toBeDisabled();
  await expect(page.getByRole("status", { name: "README.md を読み込み中" }).first()).toBeVisible();
  await composer.press("Enter");
  expect(streamRequests).toHaveLength(0);

  releaseRead();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toBeVisible();
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);
  const message = streamRequests[0].message as Record<string, unknown>;
  expect(message.attachments).toEqual([
    expect.objectContaining({ name: "README.md", sourcePath: "README.md" }),
  ]);
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "README.md", kind: "file", label: "README.md", syntax: "@README.md" },
  ]);
});

test("cancelling a pending workspace mention discards its late result", async ({ page }) => {
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await page.getByRole("button", { name: "README.md の読み込みを取り消す" }).click();

  releaseRead();
  await expect(page.getByRole("status", { name: "README.md を読み込み中" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "README.md を削除" })).toHaveCount(0);
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);
  const message = streamRequests[0].message as Record<string, unknown>;
  expect(message.attachments).toBeUndefined();
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.mentions).toBeUndefined();
  expect(metadata.attachments).toEqual([]);
});

test("cancelling one pending workspace mention preserves another transaction", async ({ page }) => {
  let releaseReadme!: () => void;
  let releaseApp!: () => void;
  const readmeGate = new Promise<void>((resolve) => { releaseReadme = resolve; });
  const appGate = new Promise<void>((resolve) => { releaseApp = resolve; });
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: (payload) => (
      payload.path === "README.md" ? readmeGate : appGate
    ),
    onStreamRequest: (payload) => streamRequests.push(payload),
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await composer.press("End");
  await composer.pressSequentially(" @src");
  await page.getByRole("option").filter({ hasText: "@src/App.tsx" }).click();

  await page.getByRole("button", { name: "README.md の読み込みを取り消す" }).click();
  releaseApp();
  await expect(page.getByRole("button", { name: "App.tsx を削除" })).toBeVisible();
  releaseReadme();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toHaveCount(0);
  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);

  const message = streamRequests[0].message as Record<string, unknown>;
  expect(message.attachments).toEqual([
    expect.objectContaining({ name: "src/App.tsx", sourcePath: "src/App.tsx" }),
  ]);
  const metadata = message.metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "src/App.tsx", kind: "file", label: "src/App.tsx", syntax: "@src/App.tsx" },
  ]);
});

test("starting a new draft discards a pending workspace mention result", async ({ page }) => {
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  await page.getByTitle("New Chat").first().click();
  await expect(page.locator(".rumi-new-chat-stage")).toBeVisible();

  releaseRead();
  await expect(page.getByRole("button", { name: "README.md を削除" })).toHaveCount(0);
  await expect(page.getByRole("status", { name: "README.md を読み込み中" })).toHaveCount(0);
});

test("migrated keyboard navigation marker keeps composer controls reachable", async ({ page }) => {
  await openDefaultspack(page, "/chat", {
    initialSettingsValues: {
      general: {
        settings_version: 2,
        keyboard_button_navigation: true,
        keyboard_button_navigation_source: "legacy_default_migrated",
        composer_placeholder: "Migrated placeholder",
      },
    },
  });

  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.focus();
  await composer.press("Tab");
  await expect(composer).not.toBeFocused();
});

test("composer mention keyboard and ARIA contracts stay predictable at Unicode and empty boundaries", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });

  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  const mentions = page.getByTestId("composer-at-mention-candidates");

  await composer.fill("@");
  await expect(mentions).toBeVisible();
  await expect(mentions.getByRole("option").first()).toBeVisible();

  await composer.fill("調べて@web");
  await expect(mentions).toBeVisible();
  await expect(composer).toHaveAttribute("aria-expanded", "true");
  await expect(composer).toHaveAttribute("aria-controls", "composer-at-mention-listbox");
  await expect(composer).toHaveAttribute("aria-activedescendant", "composer-at-mention-option-0");
  await expect(page.getByRole("option", { name: /@web search/i })).toHaveAttribute("aria-selected", "true");

  await composer.fill("調べて @web_search。");
  await expect(mentions).toBeHidden();
  await expect(composer).toHaveAttribute("aria-expanded", "false");

  await composer.fill("mail@example.com https://example.com/@name \\@web_search @@web_search");
  await expect(mentions).toBeHidden();

  await composer.fill("ユーザー@example.com https://example.com/日本@pm");
  await expect(mentions).toBeHidden();

  await composer.fill("https://example.com。@web");
  await expect(mentions).toBeVisible();
  await expect(page.getByRole("option", { name: /@web search/i })).toBeVisible();

  await composer.fill("https://example.com)@web");
  await expect(mentions).toBeVisible();
  await expect(page.getByRole("option", { name: /@web search/i })).toBeVisible();

  await composer.fill("@this_candidate_does_not_exist");
  await expect(mentions).toBeVisible();
  await expect(page.getByTestId("composer-at-mention-empty")).toBeVisible();
  await expect(composer).not.toHaveAttribute("aria-activedescendant");
  await composer.press("Tab");
  await expect(composer).not.toBeFocused();

  await composer.focus();
  await composer.press("Escape");
  await expect(mentions).toBeHidden();

  await composer.fill("@this_candidate_does_not_exist");
  await composer.press("Shift+Enter");
  await expect(composer).toHaveValue("@this_candidate_does_not_exist\n");
  expect(streamRequests).toHaveLength(0);

  await composer.fill("@this_candidate_does_not_exist");
  await composer.press("Enter");
  await expect.poll(() => streamRequests.length).toBe(1);
  const sentMessage = streamRequests[0].message as Record<string, unknown>;
  expect(sentMessage.content).toBe("@this_candidate_does_not_exist");
});

test("coding file mentions keep stable semantic metadata through submit", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });

  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  await composer.fill("確認@README.md");
  const readmeOption = page.getByRole("option").filter({ hasText: "@README.md" });
  await expect(readmeOption).toBeVisible();
  await readmeOption.click();
  await expect(composer).toHaveValue("確認@README.md ");
  await expect(page.locator(".rumi-composer-frame")).toContainText("README.md");

  await page.getByRole("button", { name: "メッセージを送信" }).click();
  await expect.poll(() => streamRequests.length).toBe(1);
  const sentMessage = streamRequests[0].message as Record<string, unknown>;
  const metadata = sentMessage.metadata as Record<string, unknown>;
  expect(metadata.mentions).toEqual([
    { id: "README.md", kind: "file", label: "README.md", syntax: "@README.md" },
  ]);
  expect(metadata.dropped_widgets).toEqual([
    expect.objectContaining({
      id: "mention-file:README.md",
      type: "file",
      label: "README.md",
      sourceItemId: "README.md",
      metadata: expect.objectContaining({
        source: "composer_at_mention",
        mention: {
          file_path: "README.md",
          id: "README.md",
          kind: "file",
          label: "README.md",
          syntax: "@README.md",
        },
      }),
    }),
  ]);
});

test("composer controls are keyboard reachable, visibly named, and at least 44px", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.focus();
  await composer.press("Tab");
  await expect(composer).not.toBeFocused();

  for (const control of [
    page.getByRole("button", { name: "ファイルを添付" }),
    page.getByRole("button", { name: "音声入力を開始" }),
    page.getByRole("button", { name: "アクションの承認方法" }),
    page.getByRole("button", { name: /^モデル:/ }),
    page.getByRole("button", { name: "メッセージを送信" }),
  ]) {
    const box = await control.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThanOrEqual(44);
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
});

test("attachment remove and cancel actions expose 44px visible focus targets", async ({ page }) => {
  let releaseRead!: () => void;
  const readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
  await openDefaultspack(page, "/chat", {
    beforeWorkspaceFileReadResponse: () => readGate,
  });
  const composer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await composer.fill("/coding");
  await composer.press("Enter");
  await composer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();

  const cancel = page.getByRole("button", { name: "README.md の読み込みを取り消す" });
  const cancelBox = await cancel.boundingBox();
  expect(cancelBox).not.toBeNull();
  expect(cancelBox!.width).toBeGreaterThanOrEqual(44);
  expect(cancelBox!.height).toBeGreaterThanOrEqual(44);
  await cancel.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(cancel).toBeFocused();
  expect(await cancel.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");

  releaseRead();
  const inlineRemove = page.getByRole("button", { name: "README.md を削除" });
  await expect(inlineRemove).toBeVisible();
  const inlineBox = await inlineRemove.boundingBox();
  expect(inlineBox).not.toBeNull();
  expect(inlineBox!.width).toBeGreaterThanOrEqual(44);
  expect(inlineBox!.height).toBeGreaterThanOrEqual(44);
  await inlineRemove.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(inlineRemove).toBeFocused();
  expect(await inlineRemove.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");

  await page.getByTitle("New Chat").first().click();
  const newComposer = page.getByRole("combobox", { name: "Rumiにメッセージを送信" });
  await newComposer.fill("Review @REA");
  await page.getByRole("option").filter({ hasText: "@README.md" }).click();
  const cardRemove = page.getByRole("button", { name: "README.md を削除" });
  const cardBox = await cardRemove.boundingBox();
  expect(cardBox).not.toBeNull();
  expect(cardBox!.width).toBeGreaterThanOrEqual(44);
  expect(cardBox!.height).toBeGreaterThanOrEqual(44);
  await cardRemove.focus();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Shift+Tab");
  await expect(cardRemove).toBeFocused();
  const focusedCardStyle = await cardRemove.evaluate((element) => ({
    outlineStyle: getComputedStyle(element).outlineStyle,
    opacity: getComputedStyle(element).opacity,
  }));
  expect(focusedCardStyle.opacity).toBe("1");
  expect(focusedCardStyle.outlineStyle).not.toBe("none");
});

test("history reload restores localized semantic mention badges", async ({ page }) => {
  await openDefaultspack(page, "/chat");
  await expect(page.getByTestId("message-mention-badge").filter({ hasText: "@Web Search" })).toBeVisible();

  await page.reload();
  await expect(page.getByTestId("message-mention-badge").filter({ hasText: "@Web Search" })).toBeVisible();
});

test("composer browser behavior covers long text popovers and mobile coding trust", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  await page.getByTitle("New Chat").first().click();
  await expect(page.locator(".rumi-new-chat-stage")).toBeVisible();

  const homeComposer = page.locator("textarea.rumi-composer-textarea");
  const longPrompt = Array.from({ length: 80 }, (_, index) => `長文入力 ${index} @README.md`).join("\n");
  await homeComposer.fill(longPrompt);
  await expect(homeComposer).toHaveValue(longPrompt);
  await expect(page.locator(".rumi-composer-mention-overlay")).toHaveCount(0);
  const homeMetrics = await homeComposer.evaluate((element) => {
    const style = window.getComputedStyle(element);
    return {
      color: style.color,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
    };
  });
  expect(homeMetrics.color).not.toBe("rgba(0, 0, 0, 0)");
  expect(homeMetrics.scrollHeight).toBeGreaterThan(homeMetrics.clientHeight);

  await homeComposer.fill("/coding");
  await expect(page.getByText("Commands")).toBeVisible();

  await openDefaultspack(page, "/chat");
  const codingComposer = page.locator("textarea.rumi-composer-textarea");
  await codingComposer.fill("/coding");
  await codingComposer.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  await codingComposer.fill("@REA");
  const mentions = page.getByTestId("composer-at-mention-candidates");
  await expect(mentions).toBeVisible();
  await expect(mentions).toContainText("README.md");
  const mentionBox = await mentions.boundingBox();
  expect(mentionBox).not.toBeNull();
  expect(mentionBox!.x).toBeGreaterThanOrEqual(0);
  expect(mentionBox!.y).toBeGreaterThanOrEqual(0);
  expect(mentionBox!.x + mentionBox!.width).toBeLessThanOrEqual(page.viewportSize()!.width);

  await page.getByLabel("close mention menu").click({ position: { x: 4, y: 4 } });
  await expect(mentions).toBeHidden();

  await page.setViewportSize({ width: 390, height: 820 });
  const workspacePicker = page.locator(".rumi-workspace-picker");
  await expect(workspacePicker).toBeVisible();
  await expect(workspacePicker.locator("svg.text-emerald-300").first()).toBeVisible();
});

test("resizable canvas and tool widgets persist width choices", async ({ page }) => {
  await openDefaultspack(page);

  await page.getByTitle("Canvas を開く").click();
  const preview = page.getByLabel("Activity preview");
  await expect(preview).toBeVisible();
  const canvasHandle = page.getByLabel("Canvas幅を変更");
  await expect(canvasHandle).toBeVisible();
  const canvasBox = await canvasHandle.boundingBox();
  expect(canvasBox).not.toBeNull();
  await page.mouse.move(canvasBox!.x + canvasBox!.width / 2, canvasBox!.y + canvasBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(canvasBox!.x - 80, canvasBox!.y + canvasBox!.height / 2, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-activity-preview-width"))).not.toBeNull();
  const storedCanvasWidth = await page.evaluate(() => Number(localStorage.getItem("rumi-activity-preview-width")));
  expect(storedCanvasWidth).toBeGreaterThanOrEqual(300);

  await page.locator('button[title="機能"]').click();
  await expect(page.getByRole("heading", { name: "機能" })).toBeVisible();
  const toolHandle = page.getByLabel("機能パネル幅を変更");
  await expect(toolHandle).toBeVisible();
  const toolBox = await toolHandle.boundingBox();
  expect(toolBox).not.toBeNull();
  await page.mouse.move(toolBox!.x + toolBox!.width / 2, toolBox!.y + toolBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(toolBox!.x - 90, toolBox!.y + toolBox!.height / 2, { steps: 5 });
  await page.mouse.up();
  await expect.poll(() => page.evaluate(() => localStorage.getItem("rumi-right-sidebar-panel-width"))).not.toBeNull();
  const storedToolWidth = await page.evaluate(() => Number(localStorage.getItem("rumi-right-sidebar-panel-width")));
  expect(storedToolWidth).toBeGreaterThanOrEqual(320);
});

test("open utility panel never covers the Home composer at desktop breakpoints", async ({ page }) => {
  await page.setViewportSize({ width: 980, height: 760 });
  await openDefaultspack(page, "/chat");
  await page.getByTitle("New Chat").first().click();
  await page.locator('button[title="機能"]').click();

  const panel = page.locator(".rumi-right-sidebar-panel");
  const composer = page.locator(".rumi-composer-frame");
  const send = page.locator(".rumi-send-button");
  await expect(panel).toBeVisible();
  await expect(send).toBeVisible();

  const [panelBox, composerBox, sendBox] = await Promise.all([
    panel.boundingBox(),
    composer.boundingBox(),
    send.boundingBox(),
  ]);
  expect(panelBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  expect(sendBox).not.toBeNull();
  expect(composerBox!.x + composerBox!.width).toBeLessThanOrEqual(panelBox!.x + 1);
  expect(sendBox!.x + sendBox!.width).toBeLessThanOrEqual(panelBox!.x + 1);

  const sendOwnsCenterPoint = await send.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return hit === element || element.contains(hit);
  });
  expect(sendOwnsCenterPoint).toBe(true);
});

test("model picker search supports @provider filters", async ({ page }) => {
  await openDefaultspack(page);

  await page.getByRole("button", { name: /Stub Default/ }).click();
  const search = page.getByPlaceholder("モデルを検索... @google");
  await search.fill("@opencode");
  await expect(page.getByText("Qwen3.5 Plus via OpenCode Go")).toBeVisible();
  await expect(page.getByText("MiniMax M3 Free via OpenCode Zen")).toBeVisible();
  await expect(page.getByText("Gemini 2.5 Flash")).toBeHidden();

  await search.fill("@opencode zen");
  await expect(page.getByText("MiniMax M3 Free via OpenCode Zen")).toBeVisible();
  await expect(page.getByText("Qwen3.5 Plus via OpenCode Go")).toBeHidden();

  await search.fill("@google flash");
  await expect(page.getByText("Gemini 2.5 Flash")).toBeVisible();
  await expect(page.getByText("Qwen3.5 Plus via OpenCode Go")).toBeHidden();
});

test("model picker keeps unconfigured opencode zen visible for first-run setup", async ({ page }) => {
  await openDefaultspack(page);

  await page.getByRole("button", { name: /Stub Default/ }).click();
  const search = page.getByPlaceholder("モデルを検索... @google");
  await search.fill("minimax");
  await expect(page.getByText("MiniMax M3 Free via OpenCode Zen")).toBeVisible();
});

test("preview pane opens from the chat canvas peek", async ({ page }) => {
  await openDefaultspack(page);

  await page.getByTitle("Canvas を開く").click();

  const preview = page.getByLabel("Activity preview");
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("calendar-smoke.json");
});

test("calendar action renders a scheduler preview", async ({ page }) => {
  await openDefaultspack(page);

  await page.locator('button[title="機能"]').click();
  const toolManagerSearch = page.getByPlaceholder("機能を検索");
  await toolManagerSearch.fill("scheduler");
  await page.getByTestId("tool-manager-candidates").getByRole("button", { name: /Scheduler/ }).first().click();
  await expect(page.getByText("Calendar and trigger smoke surface.")).toBeVisible();
  await page.locator('button[title="Calendar"]').last().click();

  const preview = page.getByLabel("Activity preview");
  await expect(preview).toContainText("Calendar.json");
  await expect(preview).toContainText("nightly-review");
});

test("calendar mode opens quick add and renders new tasks in blue", async ({ page }) => {
  await openDefaultspack(page, "/coding");

  await page.locator('button[title="Calendar"]').first().click();
  await expect(page.getByLabel("Calendar month")).toBeVisible();

  const now = new Date();
  const dayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-09`;
  const nextDayKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-10`;
  const nextMonth = new Date(now.getFullYear(), now.getMonth() + 1, 1);
  const nextMonthKey = `${nextMonth.getFullYear()}-${String(nextMonth.getMonth() + 1).padStart(2, "0")}-01`;
  const dayLabel = `${now.getFullYear()}年${now.getMonth() + 1}月9日`;
  const nextDayLabel = `${now.getFullYear()}年${now.getMonth() + 1}月10日`;
  await page.getByLabel("次の月").click();
  await expect(page.getByTestId(`calendar-day-${nextMonthKey}`)).toBeVisible();
  await page.getByLabel("今日").click();
  await page.getByTestId(`calendar-day-${dayKey}`).click();
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toBeHidden();
  await expect(page.getByRole("dialog", { name: `${nextDayLabel}に追加` })).toBeHidden();
  await page.getByTestId(`calendar-day-${nextDayKey}`).click();
  await expect(page.getByRole("dialog", { name: `${nextDayLabel}に追加` })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByTestId(`calendar-day-${dayKey}`).click();
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toBeVisible();

  await page.getByPlaceholder("何を追加しますか？").fill("Design review");
  await page.getByRole("button", { name: "追加", exact: true }).click();

  const task = page.getByText("Design review");
  await expect(task).toBeVisible();
  await expect(task).toHaveClass(/bg-blue-500\/90/);
  await task.click();
  await expect(page.getByRole("dialog", { name: `${dayLabel}に追加` })).toContainText("項目を編集");
  await page.getByLabel("カレンダー項目の時刻").click();
  await expect(page.getByRole("listbox", { name: "カレンダー時刻候補" })).toContainText("午前12:30");
  await page.getByPlaceholder("何を追加しますか？").fill("Design review edited");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("Design review edited")).toBeVisible();

  const rangeStart = page.getByTestId(`${"calendar-day"}-${dayKey.replace("-09", "-12")}`);
  const rangeEnd = page.getByTestId(`${"calendar-day"}-${dayKey.replace("-09", "-14")}`);
  const startBox = await rangeStart.boundingBox();
  const endBox = await rangeEnd.boundingBox();
  expect(startBox).not.toBeNull();
  expect(endBox).not.toBeNull();
  await page.mouse.move(startBox!.x + startBox!.width / 2, startBox!.y + startBox!.height / 2);
  await page.mouse.down();
  await page.mouse.move(endBox!.x + endBox!.width / 2, endBox!.y + endBox!.height / 2, { steps: 6 });
  await page.mouse.up();
  await expect(page.getByRole("dialog", { name: `${dayLabel.replace("9日", "12日")} - ${dayLabel.replace("9日", "14日")}に追加` })).toBeVisible();
  await page.getByPlaceholder("何を追加しますか？").fill("Range task");
  await page.getByRole("button", { name: "追加", exact: true }).click();
  await expect(page.getByText("Range task")).toHaveCount(3);

  await page.getByText("Range task").first().click();
  await page.getByRole("button", { name: "削除", exact: true }).click();
  await expect(page.getByText("Range task")).toHaveCount(0);

  await page.getByTitle("Settings").last().click();
  await expect(page.getByRole("heading", { name: "Rumi Control Center" })).toBeVisible();
});

test("history card drag uses rumi history MIME and sends dropped_widgets metadata", async ({ page }) => {
  const streamRequests: Record<string, unknown>[] = [];
  await openDefaultspack(page, "/chat", {
    onStreamRequest: (payload) => streamRequests.push(payload),
  });

  await expect(page.getByText("Preview Calendar Chat").first()).toBeVisible();
  const composer = page.locator(".rumi-composer-frame");
  await expect(composer).toBeVisible();
  const dragEvidence = await page.evaluate((mime) => {
    const source = document.querySelector('[data-testid="history-chat-card-c-smoke"]');
    const target = document.querySelector(".rumi-composer-shell");
    if (!source || !target) throw new Error("history card or composer target not found");
    const dataTransfer = new DataTransfer();
    source.dispatchEvent(new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer }));
    const historyPayload = dataTransfer.getData(mime);
    target.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer }));
    target.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer }));
    return {
      historyPayload,
      plainText: dataTransfer.getData("text/plain"),
    };
  }, historyChatDropMime);

  expect(dragEvidence.plainText).toBe("Preview Calendar Chat");
  expect(JSON.parse(dragEvidence.historyPayload)).toMatchObject({
    conversationId: "c-smoke",
    title: "Preview Calendar Chat",
    conversationKind: "coding",
    tags: ["coding"],
  });
  await expect(composer).toContainText("Preview Calendar Chat");

  await page.locator("textarea.rumi-composer-textarea").fill("Use this dropped chat as context.");
  await page.locator(".rumi-send-button").click();
  await expect.poll(() => streamRequests.length).toBe(1);

  const request = streamRequests[0];
  const message = request.message as Record<string, unknown>;
  const metadata = message.metadata as Record<string, unknown>;
  const droppedWidgets = metadata.dropped_widgets as Array<Record<string, unknown>>;
  expect(droppedWidgets).toHaveLength(1);
  expect(droppedWidgets[0]).toMatchObject({
    id: "conversation:c-smoke",
    type: "conversation",
    widgetKind: "history_context",
    sourceItemId: "c-smoke",
    label: "Preview Calendar Chat",
  });
  expect(droppedWidgets[0].metadata).toMatchObject({
    conversation_id: "c-smoke",
    title: "Preview Calendar Chat",
  });
});

test("late stream activity after final message does not leave an empty draft pending", async ({ page }) => {
  await openDefaultspack(page, "/chat", {
    streamEvents: (message) => [
      { type: "content_delta", data: { delta: "Structured response accepted." } },
      { type: "assistant_message_completed", data: { message } },
      {
        type: "tool_call_started",
        data: {
          tool_name: "browser_use",
          tool_call_id: "call-late",
          display_text: "browser_use を使用中",
          message: "browser_use を使用中",
        },
      },
      { type: "done", data: { message } },
    ],
  });

  await page.locator("textarea.rumi-composer-textarea").fill("Use browser after final.");
  await page.locator(".rumi-send-button").click();

  await expect(page.getByText("Structured response accepted.")).toBeVisible();
  await expect(page.getByText("レスポンス本文が空でした。stream が途中で閉じたか、thinking のみで終了した可能性があります。")).toBeHidden();
  await expect(page.getByText("tool 準備中")).toBeHidden();
});

test("coding slash command toggles coding mode off again", async ({ page }) => {
  await openDefaultspack(page, "/chat");

  await page.locator("textarea.rumi-composer-textarea").fill("/coding");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/coding(?:\?|$)/);
  await expect(page.getByRole("button", { name: "Coding widget" })).toBeVisible();

  await page.locator("textarea.rumi-composer-textarea").fill("/coding");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/chat(?:\?|$)/);
  await expect(page.getByRole("button", { name: "Coding widget" })).toBeHidden();
});

test("tool timeline shows streamed activity details", async ({ page }) => {
  await openDefaultspack(page);

  await expect(page.locator(".rumi-tool-activity")).toHaveCount(1);
  const toggle = page.getByRole("button", { name: /作業状況を開く:/ });
  await expect(toggle).toBeVisible();
  await expect(toggle).toContainText("詳細");

  await toggle.click();
  const expandedToggle = page.getByRole("button", { name: /作業状況を閉じる:/ });
  await expect(expandedToggle).toBeVisible();
  await expect(expandedToggle).toContainText("閉じる");
  const timeline = page.locator(".rumi-tool-activity");
  await expect(timeline).toBeVisible();
  await expect(timeline).toContainText("ファイル");
  await expect(timeline).toContainText("src");
  await expect(timeline).toContainText("Listed 2 files");
});

test("mocked coding cockpit renders MCP server state", async ({ page }) => {
  await openCodingWidget(page);

  await expect(page.locator(".coding-cockpit")).toBeVisible();
  const mcpServers = page.getByLabel("MCP servers");
  await expect(mcpServers).toContainText("Filesystem MCP");
  await expect(mcpServers).toContainText("approved");
});

test("mocked coding cockpit registers approves and connects an MCP server", async ({ page }) => {
  await openCodingWidget(page);

  await page.getByLabel("MCP server id").fill("contract_digest");
  await page.getByLabel("MCP command").fill("python");
  await page.getByLabel("MCP args").fill("digest_server.py");
  await page.getByTitle("Connect MCP server").click();

  const mcpServers = page.getByLabel("MCP servers");
  await expect(mcpServers).toContainText("contract_digest");
  await expect(mcpServers).toContainText("approved");
  await expect(page.getByText("MCP connected: contract_digest (1 tools)")).toBeVisible();
});

test("coding approval queue refreshes immediately after terminal requests approval", async ({ page }) => {
  await openCodingWidget(page, { codingApprovalAfterTerminal: true });

  const terminal = page.getByRole("region", { name: "Terminal", exact: true });
  await terminal.locator("input").fill("echo qa-file");
  await terminal.getByTitle("Run command").click();

  await expect(terminal).toContainText("Approval required");
  const approvals = page.getByLabel("Approval queue");
  await expect(approvals).toContainText("terminal.exec");
  await expect(approvals.getByRole("button", { name: /許可|Approve/ })).toBeVisible();
  await expect(approvals.getByRole("button", { name: /拒否|Deny/ })).toBeVisible();
});

test("checkpoint create selects the new snapshot and approved restore settles successfully", async ({ page }) => {
  await openCodingWidget(page, { codingApprovalAfterRestore: true });

  const checkpoints = page.getByRole("region", { name: "Checkpoints", exact: true });
  await expect(checkpoints.locator("select")).toHaveValue("checkpoint-1");
  await checkpoints.getByTitle("Create checkpoint").click();
  await expect(checkpoints.locator("select")).toHaveValue("checkpoint-2");
  await expect(checkpoints).toContainText("Created checkpoint-2");
  await checkpoints.getByTitle("Review checkpoint restore").click();
  await checkpoints.getByRole("button", { name: "Confirm restore" }).click();

  await expect(checkpoints).toContainText("Approval required");
  const approvals = page.getByLabel("Approval queue");
  await expect(approvals).toContainText("file.restore");
  await approvals.getByRole("button", { name: /許可|Approve/ }).click();
  await expect(checkpoints).toContainText("Restored checkpoint-2");
  await expect(checkpoints).not.toContainText("Approval required");
});
