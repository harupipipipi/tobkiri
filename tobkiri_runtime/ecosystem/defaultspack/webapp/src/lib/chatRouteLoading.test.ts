import test from "node:test";
import assert from "node:assert/strict";

import {
  initialActiveWorkspaceTabIdForPathname,
  initialWorkspaceTabsForPathname,
  workspaceKindForPathname,
  workspaceUrlForKind,
} from "./workspaceRouting";
import { loadConversationForRefresh, resolveSupersededConversationRedirect } from "./chatRouteLoading";
import { ConversationViewLoader, conversationOwnsSelectedView, type ConversationLoadTicket } from "./conversationView";
import { SavedTurnViewFence } from "./optimisticSavedTurn";
import { isGenerationActiveForView } from "./pendingChat";

type ViewConversation = { id: string; title: string; messages: string[]; metadata?: unknown };

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Exercise the loader callbacks used by ChatApp, including selection, loaded
// body, route, preview and tab metadata publication, rather than a parallel
// implementation of the asynchronous loading algorithm.
function conversationViewHarness(read: (id: string) => Promise<ViewConversation>) {
  const fence = new SavedTurnViewFence("tab-a", "a");
  const loader = new ConversationViewLoader(fence);
  const cache = new Map<string, ViewConversation>();
  const tabs = new Map<string, { conversationId: string | null; title: string }>();
  const state = {
    tab: "tab-a", id: "a" as string | null,
    body: null as ViewConversation | null, route: "a" as string | null,
    previews: [] as string[], errors: [] as string[],
  };
  const syncTab = () => {
    if (conversationOwnsSelectedView(fence.capture(), state.tab, state.id, state.body?.id ?? null)) {
      tabs.set(state.tab, { conversationId: state.id, title: state.body?.title ?? "New Conversation" });
    }
  };
  const load = (tab: string, id: string | null, onViewLoad?: (ticket: ConversationLoadTicket) => void) => loader.load({
    workspaceTabId: tab,
    conversationId: id,
    readConversation: read,
    publishSelection: (ticket) => {
      state.tab = ticket.workspaceTabId;
      state.id = ticket.conversationId;
      state.body = ticket.conversationId ? cache.get(ticket.conversationId) ?? null : null;
      state.previews = [];
      syncTab();
      onViewLoad?.(ticket);
    },
    publishConversation: (conversation) => {
      state.body = conversation;
      cache.set(conversation.id, conversation);
      syncTab();
    },
    publishRoute: (id) => { state.route = id; },
    refreshPreview: (id, ticket) => {
      if (loader.matches(ticket) && id) state.previews = [id];
    },
  });
  const activate = (tab: string, id: string | null) => load(tab, id)
    .catch((error: Error) => { state.errors.push(error.message); });
  return { loader, fence, cache, tabs, state, load, activate };
}

test("actual view load callbacks discard late B body, route, preview and metadata after A -> B -> A", async () => {
  const lateB = deferred<ViewConversation>();
  const a = { id: "a", title: "A", messages: ["a body"] };
  const harness = conversationViewHarness((id) => id === "b" ? lateB.promise : Promise.resolve(a));
  harness.cache.set("a", a);
  harness.tabs.set("tab-b", { conversationId: "b", title: "B" });
  await harness.activate("tab-a", "a");
  const loadingB = harness.activate("tab-b", "b");
  assert.equal(harness.state.id, "b", "target changes before the read returns");
  assert.equal(harness.state.body?.id ?? null, null, "A body is cleared while uncached B loads");
  assert.deepEqual(harness.tabs.get("tab-b"), { conversationId: "b", title: "B" });
  await harness.activate("tab-a", "a");
  lateB.resolve({ id: "b", title: "Late B", messages: ["wrong body"] });
  await loadingB;
  assert.equal(harness.state.tab, "tab-a");
  assert.equal(harness.state.id, "a");
  assert.equal(harness.state.body?.id, "a");
  assert.equal(harness.state.route, "a");
  assert.deepEqual(harness.state.previews, ["a"]);
  assert.deepEqual(harness.tabs.get("tab-b"), { conversationId: "b", title: "B" });
  assert.deepEqual(harness.tabs.get("tab-a"), { conversationId: "a", title: "A" });
});

test("busy A to settled cached B selects B body and ordinary controls before B read completes", async () => {
  const lateB = deferred<ViewConversation>();
  const harness = conversationViewHarness(() => lateB.promise);
  harness.cache.set("b", { id: "b", title: "Hello", messages: ["hello", "reply"] });
  const loadingB = harness.activate("tab-b", "b");
  const pending = { a: true } as Record<string, boolean>;
  assert.equal(harness.state.body?.id, "b");
  assert.deepEqual(harness.state.body?.messages, ["hello", "reply"]);
  assert.equal(harness.state.route, "b");
  assert.equal(isGenerationActiveForView({
    activeConversationId: harness.state.id,
    activeViewTicket: harness.fence.capture(),
    globalIsGenerating: true,
    isConversationPending: Boolean(pending[harness.state.id!]),
    streamingConversationId: "a",
    submissionViewTicket: { workspaceTabId: "tab-a", conversationId: "a", epoch: 0 },
  }), false, "the selected target cannot inherit A Stop/steer mode");
  assert.deepEqual(harness.tabs.get("tab-b"), { conversationId: "b", title: "Hello" });
  lateB.resolve({ id: "b", title: "Hello", messages: ["hello", "reply"] });
  await loadingB;
});

test("late completed and rejected reads cannot publish a redirect or unrelated current-view error", async () => {
  const lateB = deferred<ViewConversation>();
  const lateC = deferred<ViewConversation>();
  const reads: string[] = [];
  const a = { id: "a", title: "A", messages: ["current completion"] };
  const harness = conversationViewHarness((id) => {
    reads.push(id);
    return id === "b" ? lateB.promise : id === "c" ? lateC.promise : Promise.resolve(a);
  });
  const loadingB = harness.activate("tab-b", "b");
  const loadingC = harness.activate("tab-c", "c");
  await harness.activate("tab-a", "a");
  lateB.resolve({ id: "b", title: "Superseded", messages: [], metadata: {
    superseded: true, active_conversation_id: "foreign-root",
  } });
  lateC.reject(new Error("unrelated old failure"));
  await Promise.all([loadingB, loadingC]);
  assert.equal(harness.state.body?.id, "a");
  assert.equal(harness.state.route, "a");
  assert.deepEqual(harness.state.errors, []);
  assert.equal(reads.includes("foreign-root"), false);
  await harness.activate("tab-a", "a");
  const currentFailure = harness.activate("tab-c", "c");
  await currentFailure;
  assert.deepEqual(harness.state.errors, ["unrelated old failure"], "a current failure still surfaces");
});

test("preview and poll publication tickets expire immediately on selection before effect cleanup", async () => {
  const next = deferred<ViewConversation>();
  const harness = conversationViewHarness(() => next.promise);
  const previewTicket = harness.loader.capture();
  const pollTicket = harness.fence.capture();
  const loading = harness.activate("tab-b", "b");
  assert.equal(harness.loader.matches(previewTicket), false);
  assert.equal(harness.fence.matches(pollTicket), false);
  next.resolve({ id: "b", title: "B", messages: [] });
  await loading;
});

test("same-ID superseded loads cannot publish an older body or failure into the newer load", async () => {
  const first = deferred<ViewConversation>();
  const second = deferred<ViewConversation>();
  const third = deferred<ViewConversation>();
  const results = [first, second, third];
  const harness = conversationViewHarness(() => results.shift()!.promise);
  const older = harness.activate("tab-a", "a");
  const newer = harness.activate("tab-a", "a");
  second.resolve({ id: "a", title: "Latest A", messages: ["new body"] });
  await newer;
  first.resolve({ id: "a", title: "Old A", messages: ["old body"] });
  await older;
  assert.equal(harness.state.body?.title, "Latest A");
  assert.deepEqual(harness.tabs.get("tab-a"), { conversationId: "a", title: "Latest A" });
  const obsoleteFailure = harness.activate("tab-a", "a");
  harness.loader.select("tab-a", "a", () => {});
  third.reject(new Error("superseded same-root error"));
  await obsoleteFailure;
  assert.deepEqual(harness.state.errors, []);
});

test("a selected existing target needs its matching record before owner-dependent submit or metadata writes", () => {
  const ticket = { workspaceTabId: "tab-b", conversationId: "b", epoch: 1 };
  assert.equal(conversationOwnsSelectedView(ticket, "tab-b", "b", null), false);
  assert.equal(conversationOwnsSelectedView(ticket, "tab-b", "b", "a"), false);
  assert.equal(conversationOwnsSelectedView(ticket, "tab-b", "b", "b"), true);
  assert.equal(conversationOwnsSelectedView(ticket, "tab-a", "a", "a"), false);
  assert.equal(conversationOwnsSelectedView({ ...ticket, conversationId: null }, "tab-b", null, null), true);
});

test("a late refresh load failure cannot choose a fallback in a newly selected view", async () => {
  const failure = deferred<void>();
  let current = true;
  const attempted: Array<string | null> = [];
  const refreshing = loadConversationForRefresh({
    locationChatId: "missing",
    listedConversations: [{ id: "fallback" }],
    isCurrentView: () => current,
    loadConversation: async (id) => { attempted.push(id); await failure.promise; },
  });
  current = false;
  failure.reject(new Error("old missing conversation"));
  await refreshing;
  assert.deepEqual(attempted, ["missing"]);
});

test("mutation refresh retains its initiating ticket while updating the global list after A to B", async () => {
  const a = { id: "a", title: "A updated", messages: ["A completion"] };
  const b = { id: "b", title: "B", messages: ["B body"] };
  const harness = conversationViewHarness((id) => Promise.resolve(id === "a" ? a : b));
  await harness.activate("tab-a", "a");
  const initiatingTicket = harness.loader.capture();
  await harness.activate("tab-b", "b");
  const listed: ViewConversation[][] = [];
  const result = await harness.loader.refresh({
    ticket: initiatingTicket, preferredId: "a", locationConversationId: () => "b",
    readList: async () => ({ conversations: [a, b] }),
    publishList: (conversations) => { listed.push(conversations); },
    loadConversation: (id, tab, onViewLoad) => harness.load(tab, id, onViewLoad),
  });
  assert.equal(result, null);
  assert.deepEqual(listed, [[a, b]], "the authorized global list refresh still completes");
  assert.equal(harness.state.id, "b");
  assert.equal(harness.state.body?.id, "b");
  assert.equal(harness.state.route, "b");
  assert.deepEqual(harness.tabs.get("tab-b"), { conversationId: "b", title: "B" });
});

test("an owning mutation refresh loads A in its original tab and returns its renewed ticket", async () => {
  const a = { id: "a", title: "A updated", messages: ["updated body"] };
  const harness = conversationViewHarness(() => Promise.resolve(a));
  const ticket = harness.loader.capture();
  const result = await harness.loader.refresh({
    ticket, preferredId: "a", locationConversationId: () => "a",
    readList: async () => ({ conversations: [a] }), publishList: () => {},
    loadConversation: (id, tab, onViewLoad) => harness.load(tab, id, onViewLoad),
  });
  assert.ok(result);
  assert.equal(harness.loader.matches(result), true);
  assert.equal(harness.state.tab, "tab-a");
  assert.equal(harness.state.body?.title, "A updated");
  assert.equal(harness.state.route, "a");
});

test("an obsolete mutation list failure does not become a new view error", async () => {
  const listed = deferred<{ conversations: ViewConversation[] }>();
  const harness = conversationViewHarness(() => Promise.resolve({ id: "b", title: "B", messages: [] }));
  const refreshing = harness.loader.refresh({
    ticket: harness.loader.capture(), preferredId: "a", locationConversationId: () => "a",
    readList: () => listed.promise, publishList: () => { assert.fail("failed list cannot publish"); },
    loadConversation: (id, tab, onViewLoad) => harness.load(tab, id, onViewLoad),
  });
  await harness.activate("tab-b", "b");
  listed.reject(new Error("late A list error"));
  assert.equal(await refreshing, null);
  assert.equal(harness.state.id, "b");
});

test("an owning refresh renews its caller ticket before a current load failure", async () => {
  const harness = conversationViewHarness(() => Promise.resolve({ id: "a", title: "A", messages: [] }));
  let ticket = harness.loader.capture();
  await assert.rejects(harness.loader.refresh({
    ticket, preferredId: "a", locationConversationId: () => "a",
    readList: async () => ({ conversations: [{ id: "a" }] }), publishList: () => {},
    onViewLoad: (next) => { ticket = next; },
    loadConversation: (id, tab, onViewLoad) => harness.loader.load({
      conversationId: id, workspaceTabId: tab,
      readConversation: async () => { throw new Error("current A load failed"); },
      publishSelection: onViewLoad, publishConversation: () => {},
    }),
  }), /current A load failed/);
  assert.equal(harness.loader.matches(ticket), true, "the current caller can still publish its error and cleanup");
});

test("an owning refresh follows every redirect selection and returns the current redirected ticket", async () => {
  const a = { id: "a", title: "Old A", messages: [], metadata: {
    superseded: true, active_conversation_id: "b",
  } };
  const b = { id: "b", title: "Redirected B", messages: ["B body"] };
  const harness = conversationViewHarness((id) => Promise.resolve(id === "a" ? a : b));
  let ticket = harness.loader.capture();
  const selections: string[] = [];
  const result = await harness.loader.refresh({
    ticket, preferredId: "a", locationConversationId: () => "a",
    readList: async () => ({ conversations: [a, b] }), publishList: () => {},
    loadConversation: (id, tab, onViewLoad) => harness.load(tab, id, onViewLoad),
    onViewLoad: (next) => { ticket = next; selections.push(next.conversationId!); },
  });
  assert.ok(result, "an owned redirect is still the same refresh operation");
  assert.deepEqual(selections, ["a", "b"]);
  assert.deepEqual(result, ticket);
  assert.equal(harness.loader.matches(ticket), true);
  assert.equal(ticket.conversationId, "b");
  assert.equal(harness.state.tab, "tab-a");
  assert.equal(harness.state.body?.id, "b");
  assert.equal(harness.state.route, "b");
  assert.deepEqual(harness.tabs.get("tab-a"), { conversationId: "b", title: "Redirected B" });
  assert.deepEqual(harness.state.previews, ["b"]);
});

test("an owning redirected refresh propagates B failure with its current caller ticket", async () => {
  const harness = conversationViewHarness(async (id) => {
    if (id === "b") throw new Error("current redirected B load failed");
    return { id: "a", title: "Old A", messages: [], metadata: {
      superseded: true, active_conversation_id: "b",
    } };
  });
  let ticket = harness.loader.capture();
  const selections: string[] = [];
  await assert.rejects(harness.loader.refresh({
    ticket, preferredId: "a", locationConversationId: () => "a",
    readList: async () => ({ conversations: [{ id: "a" }, { id: "b" }] }), publishList: () => {},
    loadConversation: (id, tab, onViewLoad) => harness.load(tab, id, onViewLoad),
    onViewLoad: (next) => { ticket = next; selections.push(next.conversationId!); },
  }), /current redirected B load failed/);
  assert.deepEqual(selections, ["a", "b"]);
  assert.equal(harness.loader.matches(ticket), true, "current B error and cleanup remain owned by the caller");
  assert.equal(ticket.conversationId, "b");
  assert.equal(harness.state.id, "b");
  assert.equal(harness.state.body, null);
  assert.equal(harness.state.route, "b");
});

test("a redirected refresh cannot adopt an unrelated selection or publish its late error", async () => {
  const lateB = deferred<ViewConversation>();
  const bRequested = deferred<void>();
  const harness = conversationViewHarness(async (id) => {
    if (id === "a") return { id: "a", title: "Old A", messages: [], metadata: {
      superseded: true, active_conversation_id: "b",
    } };
    if (id === "b") { bRequested.resolve(); return lateB.promise; }
    return { id: "c", title: "Current C", messages: ["C body"] };
  });
  let ticket = harness.loader.capture();
  const refreshing = harness.loader.refresh({
    ticket, preferredId: "a", locationConversationId: () => "a",
    readList: async () => ({ conversations: [{ id: "a" }, { id: "b" }] }), publishList: () => {},
    loadConversation: (id, tab, onViewLoad) => harness.load(tab, id, onViewLoad),
    onViewLoad: (next) => { ticket = next; },
  });
  await bRequested.promise;
  await harness.activate("tab-c", "c");
  lateB.reject(new Error("obsolete redirected B failure"));
  assert.equal(await refreshing, null);
  assert.equal(harness.loader.matches(ticket), false);
  assert.equal(ticket.conversationId, "b", "the old operation keeps its own redirect rather than capturing C");
  assert.equal(harness.state.body?.id, "c");
  assert.equal(harness.state.route, "c");
  assert.deepEqual(harness.state.errors, []);
});

test("refresh conversation loading honors URL chat id missing from the conversation list", async () => {
  const loaded: Array<string | null> = [];

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: null,
    locationChatId: "old-chat",
    listedConversations: [{ id: "recent-chat" }],
    loadConversation: async (conversationId) => {
      loaded.push(conversationId);
    },
  });

  assert.deepEqual(loaded, ["old-chat"]);
});

test("refresh conversation loading falls back when direct URL chat load fails", async () => {
  const loaded: Array<string | null> = [];

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: null,
    locationChatId: "missing-chat",
    listedConversations: [{ id: "recent-chat" }],
    loadConversation: async (conversationId) => {
      loaded.push(conversationId);
      if (conversationId === "missing-chat") {
        throw new Error("HTTP 404");
      }
    },
  });

  assert.deepEqual(loaded, ["missing-chat", "recent-chat"]);
});

test("refresh conversation loading prefers an explicit URL chat over existing active state", async () => {
  const loaded: Array<string | null> = [];

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: "active-chat",
    locationChatId: "url-chat",
    listedConversations: [{ id: "active-chat" }, { id: "url-chat" }],
    loadConversation: async (conversationId) => {
      loaded.push(conversationId);
    },
  });

  assert.deepEqual(loaded, ["url-chat"]);
});

test("refresh conversation loading normalizes stale MiMo URL chats to the active chat", async () => {
  const attempts: Array<string | null> = [];
  const activated: Array<string | null> = [];
  let normalizedUrlChatId: string | null = null;
  const conversations: Record<string, { id: string; metadata?: Record<string, unknown> }> = {
    "stale-chat": {
      id: "stale-chat",
      metadata: {
        superseded: true,
        superseded_reason: "mimo_coding_company_inactive_chat",
        active_conversation_id: "active-chat",
        replacement_conversation_id: "active-chat",
      },
    },
    "active-chat": {
      id: "active-chat",
      metadata: {
        profile_id: "defaultspack.mimo_coding_company",
        company_id: "mimo-coding-company",
      },
    },
  };

  const appLikeLoadConversation = async (conversationId: string | null): Promise<void> => {
    attempts.push(conversationId);
    if (!conversationId) {
      activated.push(null);
      normalizedUrlChatId = null;
      return;
    }

    const conversation = conversations[conversationId];
    if (!conversation) {
      throw new Error(`Missing conversation ${conversationId}`);
    }

    const redirectedId = resolveSupersededConversationRedirect(conversation, conversationId);
    if (redirectedId) {
      await appLikeLoadConversation(redirectedId);
      return;
    }

    activated.push(conversationId);
    normalizedUrlChatId = conversationId;
  };

  await loadConversationForRefresh({
    preferredId: null,
    activeConversationId: null,
    locationChatId: "stale-chat",
    listedConversations: [{ id: "active-chat" }],
    loadConversation: appLikeLoadConversation,
  });

  assert.deepEqual(attempts, ["stale-chat", "active-chat"]);
  assert.deepEqual(activated, ["active-chat"]);
  assert.equal(normalizedUrlChatId, "active-chat");
});

test("superseded conversations redirect to active MiMo company conversation", () => {
  assert.equal(
    resolveSupersededConversationRedirect(
      {
        id: "stale-chat",
        metadata: {
          superseded: true,
          active_conversation_id: "live-chat",
        },
      },
      "stale-chat",
    ),
    "live-chat",
  );

  assert.equal(
    resolveSupersededConversationRedirect(
      {
        id: "live-chat",
        metadata: {
          superseded: true,
          active_conversation_id: "live-chat",
        },
      },
      "live-chat",
    ),
    null,
  );
});

test("workspace routing opens desktops route as the desktops workspace", () => {
  const tabs = initialWorkspaceTabsForPathname("/desktops", 1234);

  assert.equal(workspaceKindForPathname("/desktops"), "desktops");
  assert.equal(initialActiveWorkspaceTabIdForPathname("/desktops"), "workspace-tab-route-desktops");
  assert.deepEqual(tabs.map((tab) => tab.kind), ["chat", "desktops"]);
  assert.equal(tabs[1].id, "workspace-tab-route-desktops");
});

test("workspace routing opens calendar route as the calendar workspace", () => {
  const tabs = initialWorkspaceTabsForPathname("/calendar", 1234);

  assert.equal(workspaceKindForPathname("/calendar"), "calendar");
  assert.equal(initialActiveWorkspaceTabIdForPathname("/calendar"), "workspace-tab-route-calendar");
  assert.deepEqual(tabs.map((tab) => tab.kind), ["chat", "calendar"]);
  assert.equal(tabs[1].id, "workspace-tab-route-calendar");
});

test("workspace routing keeps desktops URL separate from chat conversations", () => {
  assert.equal(
    workspaceUrlForKind("desktops", "http://127.0.0.1:8766/p/profile-a/chat?chat=abc&pending=1#panel", "abc"),
    "/p/profile-a/desktops#panel",
  );
  assert.equal(
    workspaceUrlForKind("chat", "http://127.0.0.1:8766/p/profile-a/desktops#panel", "abc"),
    "/p/profile-a/chat?chat=abc#panel",
  );
});

test("workspace routing keeps calendar URL separate from stale chat conversations", () => {
  assert.equal(
    workspaceUrlForKind("calendar", "http://127.0.0.1:8766/p/profile-a/chat?chat=abc&pending=1#panel", "abc"),
    "/p/profile-a/calendar#panel",
  );
  assert.equal(
    workspaceUrlForKind("chat", "http://127.0.0.1:8766/p/profile-a/calendar#panel", "abc"),
    "/p/profile-a/chat?chat=abc#panel",
  );
});
