import test from "node:test";
import assert from "node:assert/strict";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { CompanyAgentList } from "../components/company/CompanyAgentList";
import { CompanyChannelView, companyMessageChannelId, visibleCompanyMessagesForChannel } from "../components/company/CompanyChannelView";
import { CompanyP2PPanel } from "../components/company/CompanyP2PPanel";
import { CompanyTaskBoard } from "../components/company/CompanyTaskBoard";
import { CompanyTree } from "../components/company/CompanyTree";
import {
  CompanyWorkspacePanel,
  MIMO_CODING_COMPANY_ID,
  companyIdFromConversationTitle,
  enrichCompanyRecordWithLoadedResources,
  resolveActiveChannelId,
  resolveCompanyMessageListOptions,
  resolveCompanyWorkspaceHint,
  resolveCompanyWorkspaceHintFromGroup,
  resolveEffectiveCompanies,
  loadEnabledP2PDetails,
  resolveSelectedCompanyId,
  resolveSelectedCompanyRecord,
} from "../components/company/CompanyWorkspacePanel";
import { buildCompactHistoryRailItems, buildGroupsFromChats } from "../components/HistoryBoard";
import { defaultspackRendererIds, defaultspackRenderers, resolveDefaultspackRenderers } from "./defaultspackRenderers";

test("defaultspack renderer registry covers visible shell regions", () => {
  assert.deepEqual([...defaultspackRendererIds].sort(), [
    "activity_preview",
    "chat_header",
    "chat_messages",
    "composer",
    "history",
    "right_sidebar",
    "settings_modal",
    "title_bar",
  ]);
});

test("defaultspack renderer registry exposes render modules", () => {
  assert.equal(typeof defaultspackRenderers.titleBar, "function");
  assert.equal(typeof defaultspackRenderers.historyBoard, "function");
  assert.equal(typeof defaultspackRenderers.chatHeader, "function");
  assert.equal(typeof defaultspackRenderers.chatMessages, "function");
  assert.equal(typeof defaultspackRenderers.composer, "function");
  assert.equal(typeof defaultspackRenderers.toolPreviewPanel, "function");
  assert.equal(typeof defaultspackRenderers.rightSidebar, "function");
  assert.equal(typeof defaultspackRenderers.settingsModal, "function");
  assert.equal(typeof defaultspackRenderers.surfaceTemplate, "function");
});

test("defaultspack renderer resolver keeps builtin fallback for untrusted modules", () => {
  const resolved = resolveDefaultspackRenderers({
    shell: {
      layout: {
        id: "test",
        regions: [
          { id: "composer", renderer: "custom_composer", enabled: true },
        ],
      },
      renderers: [
        {
          id: "custom_composer",
          component: "CustomComposer",
          module: "https://example.com/composer.js",
          trust: "local",
        },
      ],
    },
    sidebar: { filters: [], items: [] },
    settings: { sections: [], values: {} },
    chat_rendering: { renderers: [] },
    extension_points: [],
  });

  assert.equal(resolved.composer, defaultspackRenderers.composer);
});

test("company agent list renders operational role details", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyAgentList, {
      agents: [
        {
          agent_id: "reviewer",
          display_name: "Reviewer",
          role_key: "reviewer",
          model: "stub/default",
          allowed_tools: ["coding_git_diff"],
          aliases: ["review"],
        },
      ],
    }),
  );

  assert.match(html, /Reviewer/);
  assert.match(html, /@review/);
});

test("company task board renders dispatched completed runs", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyTaskBoard, {
      agents: [{ agent_id: "minimax_worker", role_key: "minimax_worker" }],
      tasks: [
        {
          id: "task-1",
          company_id: "operations-company",
          title: "Live MiniMax smoke",
          target_agent_ids: ["minimax_worker"],
          status: "completed",
        },
      ],
      runs: [
        {
          link_id: "link-1",
          company_id: "operations-company",
          task_id: "task-1",
          agent_id: "minimax_worker",
          run_id: "agent-1",
          status: "completed",
          agent_run: {
            status: "completed",
            model: "stub/default",
            result_preview: "Visible MiniMax result",
            conversation: [
              {
                role: "user",
                label: "Assignment",
                content: "Run a real MiniMax task through Company Workspace.",
              },
              {
                role: "assistant",
                label: "Agent reply",
                content: "Visible MiniMax result",
              },
            ],
          },
        },
      ],
      onCreateTask: () => {},
      onDispatchTask: () => {},
      onCreateResearchTask: () => {},
    }),
  );

  assert.match(html, /Live MiniMax smoke/);
  assert.match(html, /minimax_worker/);
  assert.match(html, /completed/);
  assert.match(html, /stub\/default/);
  assert.match(html, /Subagent Conversation/);
  assert.match(html, /Deep research with DuckDuckGo/);
  assert.match(html, /Run a real MiniMax task through Company Workspace/);
  assert.match(html, /Agent reply/);
  assert.match(html, /Visible MiniMax result/);
});

test("company task board exposes chosen-status moves and confirmed deletion entry", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyTaskBoard, {
      agents: [],
      tasks: [{
        id: "task-blocked",
        company_id: "operations-company",
        title: "Recover blocked work",
        status: "blocked",
      }],
      onUpdateTask: () => {},
      onDeleteTask: () => {},
    }),
  );

  assert.match(html, /aria-label="Move Recover blocked work to status"/);
  assert.match(html, /<option value="queued">queued<\/option>/);
  assert.match(html, /<option value="completed">completed<\/option>/);
  assert.match(html, /aria-label="Delete Recover blocked work"/);
});

test("company workspace renders a visible empty state before a chat exists", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyWorkspacePanel, {
      activeConversationId: null,
      activeConversationTitle: "New Conversation",
    }),
  );

  assert.match(html, /Main Agent &amp; Subagents/);
  assert.match(html, /Subagent Team/);
  assert.match(html, /Subagent Team options/);
  assert.doesNotMatch(html, />Routes</);
  assert.doesNotMatch(html, />P2P</);
  assert.match(html, /Start or send a chat message to create its Subagent Team/);
  assert.doesNotMatch(html, /Rumi Operations Company/);
});

test("company workspace selects and renders the first global MiMo company without a chat or hint", () => {
  const companies = [
    {
      id: MIMO_CODING_COMPANY_ID,
      name: "MiMo Coding Company",
      agent_count: 7,
      task_count: 6,
    },
    {
      id: "operations-company",
      name: "Rumi Operations Company",
      agent_count: 9,
      task_count: 1,
    },
  ];
  const selectedId = resolveSelectedCompanyId({
    activeConversationId: null,
    activeCompanyId: null,
    hintedCompanyId: null,
    statusCompany: null,
    companies,
  });

  const html = renderToStaticMarkup(
    createElement(CompanyTree, {
      companies,
      activeCompanyId: selectedId,
    }),
  );

  assert.equal(selectedId, MIMO_CODING_COMPANY_ID);
  assert.match(html, /MiMo Coding Company/);
  assert.match(html, /7 Agents/);
  assert.match(html, /6 tasks/);
  assert.doesNotMatch(html, /Start or send a chat message/);
});

test("company workspace keeps global companies visible for conversation-scoped groups", () => {
  const effectiveCompanies = resolveEffectiveCompanies({
    activeConversationId: "chat-1",
    activeCompanyIdHint: null,
    activeCompany: {
      id: "chat-team-1",
      name: "Executive Team",
      agent_count: 2,
      task_count: 0,
    },
    companies: [
      {
        id: "mimo-coding-company",
        name: "MiMo Coding Company",
        agent_count: 7,
        task_count: 6,
      },
      {
        id: "operations-company",
        name: "Rumi Operations Company",
        agent_count: 9,
        task_count: 0,
      },
    ],
  });

  assert.deepEqual(effectiveCompanies.map((company) => company.id), [
    "chat-team-1",
    "mimo-coding-company",
    "operations-company",
  ]);
});

test("company workspace prefers global MiMo company over empty conversation-scoped MiMo status", () => {
  const selectedId = resolveSelectedCompanyId({
    activeConversationId: "chat-1",
    activeCompanyId: null,
    hintedCompanyId: null,
    statusCompany: {
      id: "employee",
      name: "MiMo Coding Company: kickoff review",
      agent_count: 0,
      task_count: 0,
    },
    companies: [
      {
        id: MIMO_CODING_COMPANY_ID,
        name: "MiMo Coding Company",
        agent_count: 7,
        task_count: 6,
      },
    ],
  });

  assert.equal(selectedId, MIMO_CODING_COMPANY_ID);
});

test("company workspace uses status company id when conversation status is thin", () => {
  const selectedId = resolveSelectedCompanyId({
    activeConversationId: "chat-1",
    activeCompanyId: null,
    hintedCompanyId: null,
    statusCompany: null,
    statusCompanyId: MIMO_CODING_COMPANY_ID,
    companies: [],
  });

  assert.equal(selectedId, MIMO_CODING_COMPANY_ID);
});

test("company workspace keeps active MiMo company visible when only runtime resources load", () => {
  const selectedCompany = resolveSelectedCompanyRecord({
    selectedId: MIMO_CODING_COMPANY_ID,
    selectedCompanyDetails: null,
    statusCompany: null,
    listedSelectedCompany: null,
  });

  assert.ok(selectedCompany);
  const enrichedCompany = enrichCompanyRecordWithLoadedResources(selectedCompany, {
    agents: [
      { agent_id: "engineer", agent_name: "Engineer", role_key: "coding" },
    ],
    channels: [
      { id: "ops-company", name: "Ops Company", message_count: 301 },
    ],
    tasks: [],
  });

  const html = renderToStaticMarkup(
    createElement(CompanyTree, {
      companies: [enrichedCompany],
      activeCompanyId: MIMO_CODING_COMPANY_ID,
    }),
  );

  assert.equal(enrichedCompany.id, MIMO_CODING_COMPANY_ID);
  assert.match(html, /MiMo Coding Company/);
  assert.match(html, /1 Agents/);
  assert.doesNotMatch(html, /No Subagent Team loaded/);
});

test("company tree does not claim the Subagent Team is missing while loading", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyTree, {
      companies: [],
      activeCompanyId: null,
      busy: true,
      emptyMessage: "No Subagent Team loaded.",
    }),
  );

  assert.match(html, /Loading Subagent Team/);
  assert.doesNotMatch(html, /No Subagent Team loaded/);
});

test("company tree disambiguates companies with duplicate display names", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyTree, {
      companies: [
        {
          id: "chat-team-596c-f34f566e04",
          name: "Executive Team",
          agent_count: 9,
          task_count: 0,
        },
        {
          id: "chat-team-a59c-baebff19d9",
          name: "Executive Team",
          agent_count: 9,
          task_count: 0,
        },
      ],
      activeCompanyId: "chat-team-a59c-baebff19d9",
    }),
  );

  assert.match(html, /ID: chat-team-596c-f34f566e04/);
  assert.match(html, /ID: chat-team-a59c-baebff19d9/);
});

test("company workspace header identifies the selected company", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyWorkspacePanel, {
      activeConversationId: "chat-1",
      activeConversationTitle: "Executive Team chat",
      activeCompanyIdHint: "chat-team-a59c-baebff19d9",
    }),
  );

  assert.match(html, /Executive Team chat/);
  assert.match(html, /Company ID: chat-team-a59c-baebff19d9/);
});

test("company workspace resolves MiMo company hints from group and profile context", () => {
  assert.equal(resolveCompanyWorkspaceHint({
    groupId: "company:mimo-coding-company",
  }), "mimo-coding-company");
  assert.equal(resolveCompanyWorkspaceHint({
    groupId: "group-coding",
  }), null);
  assert.equal(resolveCompanyWorkspaceHint({
    conversationKind: "mimo_coding_company",
  }), "mimo-coding-company");
  assert.equal(resolveCompanyWorkspaceHint({
    profileId: "defaultspack.mimo_coding_company",
  }), "mimo-coding-company");
  assert.equal(resolveCompanyWorkspaceHint({
    tags: ["company", "mimo-coding-company"],
  }), "mimo-coding-company");
});

test("company workspace resolves global company hints from active conversation titles", () => {
  assert.equal(companyIdFromConversationTitle("MiMo Coding Company: kickoff review"), MIMO_CODING_COMPANY_ID);
  assert.equal(companyIdFromConversationTitle("company:MiMo Coding Company: kickoff review"), MIMO_CODING_COMPANY_ID);
  assert.equal(companyIdFromConversationTitle("company:[stale] MiMo Coding Company"), MIMO_CODING_COMPANY_ID);
  assert.equal(companyIdFromConversationTitle("[stale] MiMo Coding Company"), MIMO_CODING_COMPANY_ID);
  assert.equal(companyIdFromConversationTitle("Operations Company: heartbeat"), "operations-company");
  assert.equal(companyIdFromConversationTitle("Repo Discovery"), null);
});

test("company workspace resolves selected history company groups", () => {
  assert.equal(resolveCompanyWorkspaceHintFromGroup({
    id: "custom-company-mimo-coding-company",
    sourceGroupId: "company:mimo-coding-company",
    chats: [],
    subGroups: [],
  }), "mimo-coding-company");
  assert.equal(resolveCompanyWorkspaceHintFromGroup({
    id: "group-company",
    chats: [{
      metadata: { group_id: "company:mimo-coding-company" },
      tags: [],
    }],
    subGroups: [],
  }), "mimo-coding-company");
  assert.equal(resolveCompanyWorkspaceHintFromGroup({
    id: "group-coding",
    chats: [],
    subGroups: [],
  }), null);
});

test("compact history company group keeps the selectable source group", () => {
  const groups = buildGroupsFromChats([
    {
      id: "mimo-company-chat",
      title: "MiMo coding company",
      date: "Today",
      type: "chat",
      metadata: {
        group_id: "company:mimo-coding-company",
        group_title: "company:mimo-coding-company",
      },
    },
  ]);
  const railGroup = buildCompactHistoryRailItems(groups)
    .find((item) => item.type === "group" && item.id === "company:mimo-coding-company");

  if (!railGroup || railGroup.type !== "group") {
    assert.fail("Expected compact rail to include the company group.");
  }
  assert.equal(resolveCompanyWorkspaceHintFromGroup(railGroup.group), "mimo-coding-company");
});

test("company workspace repairs stale channel selection when switching companies", () => {
  const channels = [
    { id: "ops-company" },
    { id: "qa-findings" },
  ];

  assert.equal(resolveActiveChannelId("qa-findings", channels), "qa-findings");
  assert.equal(resolveActiveChannelId("old-chat-channel", channels), "ops-company");
  assert.equal(resolveActiveChannelId(null, [{ id: "research" }]), "research");
});

test("company channels tab scopes and renders ops-company messages", () => {
  const channels = [
    { id: "ops-company", name: "Ops Company" },
    { id: "general", name: "General" },
  ];
  const resolvedChannelId = resolveActiveChannelId(null, channels);
  const messageOptions = resolveCompanyMessageListOptions(channels, resolvedChannelId);
  const html = renderToStaticMarkup(
    createElement(CompanyChannelView, {
      channels,
      activeChannelId: resolvedChannelId,
      messages: [
        {
          id: "message-ops",
          company_id: "operations-company",
          channel_id: "ops-company",
          sender_id: "ops_lead",
          content: "Ops handoff is visible",
        },
        {
          id: "message-general",
          company_id: "operations-company",
          channel_id: "general",
          sender_id: "pm",
          content: "General chatter hidden from ops channel",
        },
      ],
    }),
  );

  assert.equal(resolvedChannelId, "ops-company");
  assert.deepEqual(messageOptions, { limit: 80, order: "desc", channel_id: "ops-company" });
  assert.match(html, /Ops handoff is visible/);
  assert.doesNotMatch(html, /General chatter hidden from ops channel/);
});

test("company channels tab keeps latest ops messages visible from descending API results", () => {
  const messages = [
    {
      id: "message-newest",
      company_id: "mimo-coding-company",
      channel_id: "ops-company",
      sender_id: "ops_lead",
      content: "Newest ops message",
      created_at: "2026-06-30T12:00:00Z",
    },
    {
      id: "message-general",
      company_id: "mimo-coding-company",
      channel_id: "general",
      sender_id: "pm",
      content: "General message",
      created_at: "2026-06-30T11:30:00Z",
    },
    {
      id: "message-older",
      company_id: "mimo-coding-company",
      channel_id: "ops-company",
      sender_id: "ops_lead",
      content: "Older ops message",
      created_at: "2026-06-30T11:00:00Z",
    },
  ];

  const visible = visibleCompanyMessagesForChannel(messages, "ops-company", 2);
  const html = renderToStaticMarkup(
    createElement(CompanyChannelView, {
      channels: [{ id: "ops-company", name: "Ops Company", message_count: 254 }],
      activeChannelId: "ops-company",
      messages,
    }),
  );

  assert.deepEqual(visible.map((message) => message.id), ["message-older", "message-newest"]);
  assert.match(html, /Older ops message/);
  assert.match(html, /Newest ops message/);
  assert.doesNotMatch(html, /General message/);
  assert.doesNotMatch(html, /No messages in this channel/);
});

test("company channels tab accepts API messages with channel ids in metadata", () => {
  const messages = [
    {
      id: "message-meta-channel",
      company_id: "mimo-coding-company",
      channel_id: "",
      sender_id: "qa_lead",
      content: "Metadata-scoped ops update",
      metadata: { channel_id: "ops-company" },
      created_at: "2026-06-30T12:00:00Z",
    },
  ];

  const visible = visibleCompanyMessagesForChannel(messages, "ops-company", 2);
  const html = renderToStaticMarkup(
    createElement(CompanyChannelView, {
      channels: [{ id: "ops-company", name: "Ops Company", message_count: 260 }],
      activeChannelId: "ops-company",
      messages,
    }),
  );

  assert.equal(companyMessageChannelId(messages[0]), "ops-company");
  assert.deepEqual(visible.map((message) => message.id), ["message-meta-channel"]);
  assert.match(html, /Metadata-scoped ops update/);
  assert.doesNotMatch(html, /No messages in this channel/);
});

test("company channels tab falls back to unscoped messages for a single populated channel", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyChannelView, {
      channels: [{ id: "ops-company", name: "Ops Company", message_count: 260 }],
      activeChannelId: "ops-company",
      messages: [
        {
          id: "message-unscoped",
          company_id: "mimo-coding-company",
          channel_id: "",
          sender_id: "manager",
          content: "Unscoped MiMo activity remains visible",
          created_at: "2026-06-30T12:00:00Z",
        },
      ],
    }),
  );

  assert.match(html, /Unscoped MiMo activity remains visible/);
  assert.doesNotMatch(html, /No messages in this channel/);
  assert.doesNotMatch(html, /Refreshing messages/);
});

test("company tabs avoid empty configured states when card counts are known", () => {
  const channelHtml = renderToStaticMarkup(
    createElement(CompanyChannelView, {
      channels: [{ id: "ops-company", name: "Ops Company", message_count: 254 }],
      activeChannelId: "ops-company",
      messages: [],
    }),
  );
  const taskHtml = renderToStaticMarkup(
    createElement(CompanyTaskBoard, {
      agents: [],
      tasks: [],
      expectedTaskCount: 6,
    }),
  );
  const agentHtml = renderToStaticMarkup(
    createElement(CompanyAgentList, {
      agents: [],
      expectedAgentCount: 7,
    }),
  );

  assert.match(channelHtml, /254 messages recorded/);
  assert.doesNotMatch(channelHtml, /No messages in this channel/);
  assert.match(taskHtml, /6 tasks recorded/);
  assert.doesNotMatch(taskHtml, /No delegated tasks/);
  assert.match(agentHtml, /7 Agents configured/);
  assert.doesNotMatch(agentHtml, /No Subagents configured/);
});

test("company p2p panel disables durable actions while p2p is disabled", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyP2PPanel, {
      status: { p2p: { enabled: false }, peer_count: 0, approved_peer_count: 0 },
      peers: [{ peer_id: "peer-1", label: "Peer one", status: "approved" }],
      onStartPairing: () => {},
      onSendMessage: () => {},
    }),
  );

  assert.match(html, /P2P is disabled/);
  assert.match(html, /RUMI_DEFAULTSPACK_P2P_ENABLED=1/);
  assert.match(html, /restart the backend/);
  assert.match(html, /<input[^>]*disabled[^>]*placeholder="peer label"/);
  assert.match(html, /<button[^>]*disabled[^>]*title="Start pairing"/);
  assert.match(html, /<button[^>]*disabled[^>]*title="Send P2P message"/);
});

test("company p2p panel fails closed while p2p status is unavailable", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyP2PPanel, {
      status: null,
      peers: [],
      onStartPairing: () => {},
    }),
  );

  assert.match(html, /P2P status is unavailable/);
  assert.match(html, /Pairing and messaging stay disabled until status loads/);
  assert.match(html, /<button[^>]*disabled[^>]*title="Start pairing"/);
});

test("company workspace does not create p2p identity details while disabled", async () => {
  let calls = 0;
  const details = await loadEnabledP2PDetails(
    { p2p: { enabled: false }, peer_count: 0, approved_peer_count: 0 },
    {
      getP2PIdentity: async () => {
        calls += 1;
        return { identity: { node_id: "node-1" }, p2p: { enabled: false } };
      },
      listP2PPeers: async () => {
        calls += 1;
        return { peers: [] };
      },
    },
  );

  assert.equal(calls, 0);
  assert.deepEqual(details, { identity: null, peers: [] });
});

test("company workspace loads p2p identity details after p2p is enabled", async () => {
  const details = await loadEnabledP2PDetails(
    { p2p: { enabled: true }, peer_count: 1, approved_peer_count: 1 },
    {
      getP2PIdentity: async () => ({ identity: { node_id: "node-1" }, p2p: { enabled: true } }),
      listP2PPeers: async () => ({ peers: [{ peer_id: "peer-1", status: "approved" }] }),
    },
  );

  assert.equal(details.identity?.node_id, "node-1");
  assert.deepEqual(details.peers.map((peer) => peer.peer_id), ["peer-1"]);
});

test("company task board renders agent run errors", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyTaskBoard, {
      agents: [{ agent_id: "stub_worker", role_key: "stub_worker" }],
      tasks: [
        {
          id: "task-err",
          company_id: "operations-company",
          title: "Stub fallback smoke",
          target_agent_ids: ["stub_worker"],
          status: "blocked",
        },
      ],
      runs: [
        {
          link_id: "link-err",
          company_id: "operations-company",
          task_id: "task-err",
          agent_id: "stub_worker",
          run_id: "agent-err",
          status: "error",
          agent_run: {
            status: "error",
            model: "stub/default",
            error: "stub: provider is not configured",
            conversation: [
              {
                role: "user",
                label: "Assignment",
                content: "Try the same task with stub/default.",
              },
              {
                role: "error",
                label: "Agent error",
                content: "stub: provider is not configured",
                is_error: true,
              },
            ],
          },
        },
      ],
    }),
  );

  assert.match(html, /Stub fallback smoke/);
  assert.match(html, /stub\/default/);
  assert.match(html, /Try the same task with stub\/default/);
  assert.match(html, /Agent error/);
  assert.match(html, /stub: provider is not configured/);
});

test("company agent list renders latest agent run errors", () => {
  const html = renderToStaticMarkup(
    createElement(CompanyAgentList, {
      agents: [
        {
          agent_id: "stub_worker",
          display_name: "Stub Worker",
          role_key: "stub_worker",
          model: "stub/default",
          allowed_tools: [],
        },
      ],
      runs: [
        {
          link_id: "link-err",
          company_id: "operations-company",
          task_id: "task-err",
          agent_id: "stub_worker",
          run_id: "agent-err",
          status: "error",
          agent_run: {
            status: "error",
            model: "stub/default",
            error: "stub: provider is not configured",
            conversation: [
              {
                role: "user",
                label: "Assignment",
                content: "Try the same task with stub/default.",
              },
              {
                role: "error",
                label: "Agent error",
                content: "stub: provider is not configured",
                is_error: true,
              },
            ],
          },
        },
      ],
    }),
  );

  assert.match(html, /Stub Worker/);
  assert.match(html, /error/);
  assert.match(html, /Subagent Conversation/);
  assert.match(html, /Try the same task with stub\/default/);
  assert.match(html, /stub: provider is not configured/);
});
