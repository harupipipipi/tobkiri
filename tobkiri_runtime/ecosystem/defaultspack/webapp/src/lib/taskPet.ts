import type { SavedTurnEventSnapshot } from "./api";

export type TaskPetMood = "idle" | "thinking" | "waiting" | "completed" | "error" | "cancelled";
export type TaskPetScope = { profileId: string; conversationId: string; turnId: string };
export type TaskPetViewModel = {
  key: string | null;
  revision: number;
  mood: TaskPetMood;
  label: string;
  title: string;
  detail: string;
};

type PreferenceStorage = Pick<Storage, "getItem" | "setItem">;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

export function truncateTaskPetText(value: string | null | undefined, limit: number): string {
  const normalized = String(value ?? "").replace(/\s+/g, " ").trim();
  return normalized.length <= limit ? normalized : `${normalized.slice(0, Math.max(1, limit - 1)).trimEnd()}…`;
}

export function taskPetScopeKey(scope: TaskPetScope | null): string | null {
  if (!scope || ![scope.profileId, scope.conversationId, scope.turnId].every((value) => IDENTIFIER.test(value))) return null;
  return JSON.stringify([scope.profileId, scope.conversationId, scope.turnId]);
}

/** Present only a Host snapshot bound to the selected Profile/conversation/turn. */
export function taskPetViewModel(
  scope: TaskPetScope | null,
  snapshot: SavedTurnEventSnapshot | null,
  taskText?: string | null,
  activityText?: string | null,
): TaskPetViewModel {
  const idle: TaskPetViewModel = { key: null, revision: 0, mood: "idle", label: "Tobkiri ペット", title: "ここで見守っています", detail: "タスクを始めると、進み具合をここに表示します。" };
  const key = taskPetScopeKey(scope);
  if (!scope || !key || !snapshot || snapshot.turn_id !== scope.turnId
    || snapshot.conversation_id !== scope.conversationId || snapshot.operation_id !== scope.turnId
    || !snapshot.request_id || snapshot.turn.id !== scope.turnId
    || snapshot.turn.conversation_id !== scope.conversationId
    || snapshot.turn.status !== snapshot.status
    || snapshot.turn.revision !== snapshot.turn_revision
    || !Number.isSafeInteger(snapshot.turn_revision) || snapshot.turn_revision < 1) return idle;
  const task = truncateTaskPetText(taskText, 92);
  const activity = truncateTaskPetText(activityText, 118);
  const view = { key, revision: snapshot.turn_revision };
  const terminal = snapshot.terminal;
  if (terminal) {
    if (terminal.turn_id !== scope.turnId || terminal.conversation_id !== scope.conversationId
      || terminal.operation_id !== scope.turnId || terminal.request_id !== snapshot.request_id
      || terminal.turn_revision !== snapshot.turn_revision || terminal.status !== snapshot.status) return idle;
    if (terminal.status === "completed") {
      const reference = terminal.result_reference;
      if (!reference || reference.conversation_id !== scope.conversationId
        || !Number.isSafeInteger(reference.conversation_revision) || reference.conversation_revision < 1
        || !IDENTIFIER.test(reference.assistant_message_id) || !IDENTIFIER.test(reference.user_message_id)
        || !/^sha256:[0-9a-f]{64}$/.test(reference.outcome_digest)) return idle;
      return { ...view, mood: "completed", label: "完了", title: "タスクが完了しました", detail: task || "回答を確認できます。" };
    }
    if (terminal.status === "failed") return { ...view, mood: "error", label: "要確認", title: "タスクが途中で止まりました", detail: "会話画面で状態を確認してください。" };
    if (terminal.status === "cancelled") return { ...view, mood: "cancelled", label: "取消済み", title: "タスクを取り消しました", detail: "再開する場合は会話から新しい指示を送ってください。" };
    return idle;
  }
  if (snapshot.status === "running") return { ...view, mood: "thinking", label: "実行中", title: task || "タスクを進めています", detail: activity || "会話の実行状況を確認しています。" };
  if (["waiting", "waiting_approval", "awaiting_user", "pending_approval"].includes(snapshot.status)) return { ...view, mood: "waiting", label: "確認待ち", title: task || "入力または承認を待っています", detail: "会話画面で必要な操作を確認してください。" };
  return idle;
}

/** Bind a returned snapshot to the Profile captured when its request began. */
export function taskPetBoundViewModel(
  profileId: string,
  scope: TaskPetScope | null,
  snapshotProfileId: string | null,
  snapshot: SavedTurnEventSnapshot | null,
  taskText?: string | null,
  activityText?: string | null,
): TaskPetViewModel {
  return taskPetViewModel(
    scope?.profileId === profileId && snapshotProfileId === profileId ? scope : null,
    snapshot, taskText, activityText,
  );
}

/** Observe authoritative transitions; an idle UI or historical result cannot notify. */
export class TaskPetCompletionObserver {
  private activeKey: string | null = null;
  private revision = 0;
  private consumed = new Set<string>();

  observe(view: TaskPetViewModel): "completed" | "error" | null {
    if (!view.key || (this.activeKey && this.activeKey !== view.key)) {
      this.activeKey = null;
      this.revision = 0;
    }
    if (view.key === this.activeKey && view.revision < this.revision) return null;
    if (view.mood === "thinking" || view.mood === "waiting") {
      this.activeKey = view.key;
      this.revision = view.revision;
      return null;
    }
    if (!view.key || view.key !== this.activeKey) return null;
    this.activeKey = null;
    this.revision = 0;
    if (!['completed', 'error'].includes(view.mood) || this.consumed.has(view.key)) return null;
    this.consumed.add(view.key);
    if (this.consumed.size > 128) this.consumed.delete(this.consumed.values().next().value!);
    return view.mood as "completed" | "error";
  }
}

export function shouldSendDesktopNotification(
  permission: NotificationPermission,
  visibility: DocumentVisibilityState,
  explicitlyEnabled: boolean,
  hiddenByOverlay: boolean,
): boolean {
  return explicitlyEnabled && !hiddenByOverlay && permission === "granted" && visibility === "hidden";
}

export function taskPetPreferenceKey(profileId: string, preference: "enabled" | "notifications"): string {
  return `tobkiri.task-pet.${preference}.v2:${encodeURIComponent(profileId)}`;
}

export function loadTaskPetPreference(storage: PreferenceStorage | null, profileId: string, preference: "enabled" | "notifications"): boolean {
  try {
    const value = storage?.getItem(taskPetPreferenceKey(profileId, preference));
    return value === "true" || (value !== "false" && preference === "enabled");
  } catch { return preference === "enabled"; }
}

export function saveTaskPetPreference(storage: PreferenceStorage | null, profileId: string, preference: "enabled" | "notifications", enabled: boolean): boolean {
  try {
    if (!storage) return false;
    storage.setItem(taskPetPreferenceKey(profileId, preference), String(enabled));
    return true;
  } catch { return false; }
}
