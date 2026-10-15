import { ChevronUp, ExternalLink, Loader2, MessageSquare, Plus, RefreshCcw, ShieldCheck, X } from "lucide-react";

import { HistoryBoard, type ChatItem } from "../components/HistoryBoard";
import type { ModelSearchItem } from "../lib/api";
import { cn } from "../lib/cn";
import { ModelSearchPicker } from "../features/models";
import type { AmbientRoutingMode } from "./ambientTriggerClient";
import { profileScreenUrlFromLocation } from "../lib/profileRoute";

export function RoutingSettings({
  busy,
  mode,
  summary,
  selectedConversationId,
  groupEnabled,
  groupId,
  groupTitle,
  model,
  modelQuery,
  modelResults,
  modelLoading,
  needsNewChatSettings,
  aiSendApprovalRequired,
  onModeChange,
  onPickChat,
  onGroupEnabledChange,
  onGroupIdChange,
  onGroupTitleChange,
  onGroupCommit,
  onModelChange,
  onModelCommit,
  onModelQueryChange,
  onModelSearch,
  onAiSendApprovalRequiredChange,
}: {
  busy: boolean;
  mode: AmbientRoutingMode;
  summary: string;
  selectedConversationId: string | null;
  groupEnabled: boolean;
  groupId: string;
  groupTitle: string;
  model: string;
  modelQuery: string;
  modelResults: ModelSearchItem[];
  modelLoading: boolean;
  needsNewChatSettings: boolean;
  aiSendApprovalRequired: boolean;
  onModeChange: (mode: AmbientRoutingMode) => void;
  onPickChat: () => void;
  onGroupEnabledChange: (enabled: boolean) => void;
  onGroupIdChange: (value: string) => void;
  onGroupTitleChange: (value: string) => void;
  onGroupCommit: () => void;
  onModelChange: (value: string) => void;
  onModelCommit: (model: string) => void;
  onModelQueryChange: (value: string) => void;
  onModelSearch: () => void;
  onAiSendApprovalRequiredChange: (enabled: boolean) => void;
}) {
  return (
    <section className="space-y-2 border-l border-sky-400/35 pl-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-semibold uppercase text-zinc-500">話す先</span>
        <span className="min-w-0 truncate text-[11px] text-zinc-300">{summary}</span>
      </div>
      <div className="grid grid-cols-3 gap-1">
        <RouteModeButton label="選ぶ" active={mode === "selected_chat"} disabled={busy} onClick={() => onModeChange("selected_chat")} />
        <RouteModeButton label="起動ごと" active={mode === "startup_new_chat"} disabled={busy} onClick={() => onModeChange("startup_new_chat")} />
        <RouteModeButton label="毎回新規" active={mode === "always_new_chat"} disabled={busy} onClick={() => onModeChange("always_new_chat")} />
      </div>
      {mode === "selected_chat" && (
        <button type="button" onClick={onPickChat} disabled={busy} className="ambient-mini-button w-full justify-between">
          <span className="inline-flex min-w-0 items-center gap-2">
            <MessageSquare size={14} />
            <span className="truncate">{selectedConversationId ? "チャットを変更" : "チャットを選ぶ"}</span>
          </span>
          <ChevronUp size={13} className="rotate-90 text-zinc-500" />
        </button>
      )}
      {needsNewChatSettings && (
        <div className="space-y-2">
          <button
            type="button"
            onClick={() => onGroupEnabledChange(!groupEnabled)}
            disabled={busy}
            className={cn(
              "flex h-8 w-full items-center justify-between rounded-md border px-2 text-[11px] transition",
              groupEnabled
                ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-100"
                : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100",
            )}
          >
            <span>グループ内に作成</span>
            <span className="font-semibold">{groupEnabled ? "有効" : "無効"}</span>
          </button>
          {groupEnabled && (
            <div className="grid grid-cols-[0.75fr_1fr] gap-1.5">
              <label className="block text-[10px] text-zinc-500">
                グループID
                <input
                  value={groupId}
                  onChange={(event) => onGroupIdChange(event.target.value)}
                  onBlur={onGroupCommit}
                  className="mt-1 h-8 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-200"
                />
              </label>
              <label className="block text-[10px] text-zinc-500">
                表示名
                <input
                  value={groupTitle}
                  onChange={(event) => onGroupTitleChange(event.target.value)}
                  onBlur={onGroupCommit}
                  className="mt-1 h-8 w-full rounded-md border border-zinc-800 bg-zinc-950 px-2 text-xs text-zinc-200"
                />
              </label>
            </div>
          )}
        </div>
      )}
      <button
        type="button"
        onClick={() => onAiSendApprovalRequiredChange(!aiSendApprovalRequired)}
        disabled={busy}
        className={cn(
          "flex h-8 w-full items-center justify-between rounded-md border px-2 text-[11px] transition",
          aiSendApprovalRequired
            ? "border-amber-300/35 bg-amber-400/10 text-amber-50"
            : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100",
        )}
      >
        <span className="inline-flex min-w-0 items-center gap-2">
          <ShieldCheck size={13} />
          <span className="truncate">AI送信前に確認</span>
        </span>
        <span className="shrink-0 font-semibold">{aiSendApprovalRequired ? "有効" : "無効"}</span>
      </button>
      <div className="space-y-1.5 rounded-md border border-zinc-800 bg-zinc-950/60 p-2">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[10px] font-semibold text-zinc-500">送信モデル</span>
        </div>
        <ModelSearchPicker
          value={model}
          remoteResults={modelResults}
          query={modelQuery}
          loading={modelLoading}
          placeholder="すべてから探す"
          variant="compact"
          clearLabel="モデル指定を外す"
          onQueryChange={onModelQueryChange}
          onSearch={() => onModelSearch()}
          onChange={(value) => {
            onModelChange(value);
            onModelCommit(value);
            onModelQueryChange("");
          }}
        />
      </div>
    </section>
  );
}

export function CompactRoutingControl({
  busy,
  mode,
  summary,
  selectedConversationId,
  sessionConversationId,
  model,
  modelQuery,
  modelResults,
  modelLoading,
  onModeChange,
  onPickChat,
  onModelChange,
  onModelCommit,
  onModelQueryChange,
  onModelSearch,
}: {
  busy: boolean;
  mode: AmbientRoutingMode;
  summary: string;
  selectedConversationId: string | null;
  sessionConversationId?: string | null;
  model: string;
  modelQuery: string;
  modelResults: ModelSearchItem[];
  modelLoading: boolean;
  onModeChange: (mode: AmbientRoutingMode) => void;
  onPickChat: () => void;
  onModelChange: (value: string) => void;
  onModelCommit: (model: string) => void;
  onModelQueryChange: (value: string) => void;
  onModelSearch: (query?: string) => void;
}) {
  const concreteChatId = mode === "selected_chat" ? selectedConversationId : mode === "startup_new_chat" ? sessionConversationId ?? null : null;
  const createsNewChat = mode === "startup_new_chat" || mode === "always_new_chat";

  function openConcreteChat() {
    if (!concreteChatId) return;
    const url = new URL(profileScreenUrlFromLocation("/chat"), window.location.origin);
    url.searchParams.set("chat", concreteChatId);
    window.location.assign(url.toString());
  }

  return (
    <section className="space-y-2 border-t border-zinc-800/75 pt-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-zinc-500">Defaultspack</span>
        <span className="min-w-0 truncate text-[11px] text-zinc-400">送信先</span>
      </div>
      <div className="grid grid-cols-3 rounded-md border border-zinc-800 bg-zinc-950 p-0.5">
        <SegmentedRouteModeButton label="選ぶ" active={mode === "selected_chat"} disabled={busy} onClick={() => onModeChange("selected_chat")} />
        <SegmentedRouteModeButton label="起動ごと" active={mode === "startup_new_chat"} disabled={busy} onClick={() => onModeChange("startup_new_chat")} />
        <SegmentedRouteModeButton label="毎回新規" active={mode === "always_new_chat"} disabled={busy} onClick={() => onModeChange("always_new_chat")} />
      </div>

      <div className="flex min-w-0 items-center gap-1.5 text-[11px]">
        <span className="inline-flex min-w-0 flex-1 items-center gap-1.5 rounded-md border border-zinc-800 bg-zinc-950/75 px-2 py-1.5 text-zinc-200">
          <MessageSquare size={13} className="shrink-0 text-zinc-500" />
          <span className="truncate">{summary}</span>
        </span>
        {concreteChatId ? (
          <button type="button" onClick={openConcreteChat} className="ambient-mini-button h-7 shrink-0 px-2" title={`/chat?chat=${concreteChatId}`}>
            <ExternalLink size={12} />
            開く
          </button>
        ) : createsNewChat ? (
          <span className="shrink-0 rounded-md border border-zinc-800 px-2 py-1.5 text-[10px] font-semibold text-zinc-400">次の送信で作成</span>
        ) : null}
        {mode === "selected_chat" && (
          <button type="button" onClick={onPickChat} disabled={busy} className="ambient-mini-button h-7 shrink-0 px-2">
            {selectedConversationId ? "変更" : "選択"}
          </button>
        )}
      </div>

      <ModelSearchPicker
        value={model}
        remoteResults={modelResults}
        query={modelQuery}
        loading={modelLoading}
        placeholder="モデルを探す"
        variant="compact"
        clearLabel="指定を外す"
        onQueryChange={onModelQueryChange}
        onSearch={(query) => onModelSearch(query)}
        onChange={(value) => {
          onModelChange(value);
          onModelCommit(value);
          onModelQueryChange("");
        }}
      />
    </section>
  );
}

export function ChatPickerDialog({
  activeChatId,
  selectedChatId,
  chatItems,
  loading,
  creating = false,
  onRefresh,
  onNewChat,
  onSelect,
  onClose,
}: {
  activeChatId: string | null;
  selectedChatId: string | null;
  chatItems: ChatItem[];
  loading: boolean;
  creating?: boolean;
  onRefresh: () => void;
  onNewChat?: () => void;
  onSelect: (chatId: string) => void;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 rumi-layer-modal flex items-end justify-center bg-black/60 px-3 py-4 backdrop-blur-sm sm:items-center">
      <section className="flex h-[min(720px,calc(100vh-32px))] w-[min(520px,calc(100vw-24px))] flex-col overflow-hidden rounded-lg border border-zinc-800 bg-zinc-950 text-zinc-100 shadow-2xl shadow-black/50">
        <header className="flex h-10 items-center gap-2 border-b border-zinc-800 px-3">
          <MessageSquare size={15} className="text-sky-200" />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold">チャットを選ぶ</span>
          <button type="button" onClick={onRefresh} disabled={loading} className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100">
            {loading ? <Loader2 size={13} className="animate-spin" /> : <RefreshCcw size={13} />}
          </button>
          <button type="button" onClick={onClose} className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-zinc-800 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100">
            <X size={13} />
          </button>
        </header>
        {onNewChat && (
          <div className="border-b border-zinc-800/75 p-2">
            <button
              type="button"
              onClick={onNewChat}
              disabled={creating}
              className="flex h-10 w-full items-center gap-2 rounded-lg border border-emerald-300/25 bg-emerald-400/10 px-3 text-left text-sm font-semibold text-emerald-50 transition hover:border-emerald-200/40 hover:bg-emerald-400/15 disabled:cursor-wait disabled:opacity-60"
            >
              {creating ? <Loader2 size={15} className="shrink-0 animate-spin" /> : <Plus size={15} className="shrink-0" />}
              <span className="min-w-0 flex-1 truncate">新規チャットを作る</span>
            </button>
          </div>
        )}
        <div data-testid="ambient-chat-picker-history-template" className="min-h-0 flex-1">
          <HistoryBoard
            activeChatId={activeChatId}
            selectedChatId={selectedChatId}
            selectionMode
            selectionLabel="送信先"
            chatItems={chatItems}
            onChatSelect={onSelect}
            onNewTask={() => onNewChat?.()}
            onSettingsClick={() => undefined}
          />
        </div>
      </section>
    </div>
  );
}

function RouteModeButton({ label, active, disabled, onClick }: { label: string; active: boolean; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "h-8 rounded-md border px-2 text-[11px] font-medium transition",
        active
          ? "border-sky-300/35 bg-sky-400/15 text-sky-100"
          : "border-zinc-800 bg-zinc-950 text-zinc-400 hover:border-zinc-700 hover:text-zinc-100",
      )}
    >
      {label}
    </button>
  );
}

function SegmentedRouteModeButton({ label, active, disabled, onClick }: { label: string; active: boolean; disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "h-7 min-w-0 rounded px-1.5 text-[11px] font-semibold transition",
        active
          ? "bg-zinc-100 text-zinc-950"
          : "text-zinc-500 hover:bg-zinc-900 hover:text-zinc-100",
      )}
    >
      <span className="block truncate">{label}</span>
    </button>
  );
}
