export const HISTORY_ORGANIZATION_STORAGE_KEY = "tobkiri-history-organization-v1";
export const HISTORY_ORGANIZATION_ROOT = "__root__";

export type HistoryOrganizationV1 = {
  schemaVersion: 1;
  revision: number;
  updatedAt: string;
  groupChildren: Record<string, string[]>;
  chatGroups: Record<string, string>;
  chatOrder: Record<string, string[]>;
};

export type HistoryOrganizationGroup<TChat> = {
  id: string;
  chats: TChat[];
  subGroups: HistoryOrganizationGroup<TChat>[];
};

export type HistoryOrganizationLoadResult =
  | { status: "empty" }
  | { status: "ready"; organization: HistoryOrganizationV1 }
  | { status: "unavailable"; message: string }
  | { status: "corrupt"; message: string; raw: string };

export type HistoryOrganizationSaveResult =
  | { ok: true; organization: HistoryOrganizationV1 }
  | { ok: false; reason: "conflict" | "unavailable" | "corrupt"; message: string };

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string" && item.length > 0);
}

export function parseHistoryOrganization(raw: string): HistoryOrganizationV1 {
  const parsed: unknown = JSON.parse(raw);
  if (!parsed || typeof parsed !== "object") throw new Error("History organization must be an object.");
  const record = parsed as Record<string, unknown>;
  if (record.schemaVersion !== 1) throw new Error("Unsupported history organization version.");
  if (!Number.isSafeInteger(record.revision) || Number(record.revision) < 0) {
    throw new Error("History organization revision is invalid.");
  }
  if (typeof record.updatedAt !== "string") throw new Error("History organization timestamp is invalid.");
  const groupChildren = record.groupChildren;
  const chatGroups = record.chatGroups;
  const chatOrder = record.chatOrder;
  if (!groupChildren || typeof groupChildren !== "object" || Array.isArray(groupChildren)) {
    throw new Error("History group hierarchy is invalid.");
  }
  if (!chatGroups || typeof chatGroups !== "object" || Array.isArray(chatGroups)) {
    throw new Error("History chat membership is invalid.");
  }
  if (!chatOrder || typeof chatOrder !== "object" || Array.isArray(chatOrder)) {
    throw new Error("History chat order is invalid.");
  }
  for (const value of Object.values(groupChildren)) {
    if (!isStringArray(value)) throw new Error("History group order is invalid.");
  }
  for (const value of Object.values(chatGroups)) {
    if (typeof value !== "string" || !value) throw new Error("History chat group is invalid.");
  }
  for (const value of Object.values(chatOrder)) {
    if (!isStringArray(value)) throw new Error("History chat order is invalid.");
  }
  return parsed as HistoryOrganizationV1;
}

function defaultStorage(): StorageLike | null {
  return typeof localStorage === "undefined" ? null : localStorage;
}

export function loadHistoryOrganization(
  storage: StorageLike | null = defaultStorage(),
): HistoryOrganizationLoadResult {
  if (!storage) return { status: "empty" };
  let raw: string | null;
  try {
    raw = storage.getItem(HISTORY_ORGANIZATION_STORAGE_KEY);
  } catch {
    return { status: "unavailable", message: "History organization storage is unavailable." };
  }
  if (!raw) return { status: "empty" };
  try {
    return { status: "ready", organization: parseHistoryOrganization(raw) };
  } catch (error) {
    return {
      status: "corrupt",
      message: error instanceof Error ? error.message : "History organization is corrupt.",
      raw,
    };
  }
}

export function organizationFromGroups<TChat extends { id: string }>(
  groups: HistoryOrganizationGroup<TChat>[],
  revision: number,
  updatedAt = new Date().toISOString(),
): HistoryOrganizationV1 {
  const groupChildren: Record<string, string[]> = { [HISTORY_ORGANIZATION_ROOT]: groups.map((group) => group.id) };
  const chatGroups: Record<string, string> = {};
  const chatOrder: Record<string, string[]> = {};
  const visit = (group: HistoryOrganizationGroup<TChat>) => {
    groupChildren[group.id] = group.subGroups.map((child) => child.id);
    chatOrder[group.id] = group.chats.map((chat) => chat.id);
    for (const chat of group.chats) chatGroups[chat.id] = group.id;
    group.subGroups.forEach(visit);
  };
  groups.forEach(visit);
  return { schemaVersion: 1, revision, updatedAt, groupChildren, chatGroups, chatOrder };
}

function createsParentCycle(parentByChild: Map<string, string>, childId: string, parentId: string): boolean {
  let cursor = parentId;
  while (cursor !== HISTORY_ORGANIZATION_ROOT) {
    if (cursor === childId) return true;
    const next = parentByChild.get(cursor);
    if (!next) return false;
    cursor = next;
  }
  return false;
}

export function applyHistoryOrganization<TChat extends { id: string }, TGroup extends HistoryOrganizationGroup<TChat>>(
  baseGroups: TGroup[],
  organization: HistoryOrganizationV1 | null,
): TGroup[] {
  if (!organization) return baseGroups;
  const groupsById = new Map<string, TGroup>();
  const chatsById = new Map<string, TChat>();
  const baseGroupOrder: string[] = [];
  const baseChatOrder: string[] = [];
  const collect = (groups: TGroup[]) => {
    for (const group of groups) {
      if (groupsById.has(group.id)) continue;
      groupsById.set(group.id, group);
      baseGroupOrder.push(group.id);
      for (const chat of group.chats) {
        if (!chatsById.has(chat.id)) {
          chatsById.set(chat.id, chat);
          baseChatOrder.push(chat.id);
        }
      }
      collect(group.subGroups as TGroup[]);
    }
  };
  collect(baseGroups);

  const parentByChild = new Map<string, string>();
  for (const [parentId, childIds] of Object.entries(organization.groupChildren)) {
    if (parentId !== HISTORY_ORGANIZATION_ROOT && !groupsById.has(parentId)) continue;
    for (const childId of childIds) {
      if (!groupsById.has(childId) || parentByChild.has(childId) || childId === parentId) continue;
      if (!createsParentCycle(parentByChild, childId, parentId)) parentByChild.set(childId, parentId);
    }
  }
  for (const groupId of baseGroupOrder) {
    if (!parentByChild.has(groupId)) parentByChild.set(groupId, HISTORY_ORGANIZATION_ROOT);
  }

  const assignedChats = new Set<string>();
  const chatsForGroup = new Map<string, TChat[]>();
  const assignChat = (chatId: string, groupId: string) => {
    const chat = chatsById.get(chatId);
    if (!chat || assignedChats.has(chatId) || !groupsById.has(groupId)) return;
    assignedChats.add(chatId);
    chatsForGroup.set(groupId, [...(chatsForGroup.get(groupId) ?? []), chat]);
  };
  for (const [groupId, chatIds] of Object.entries(organization.chatOrder)) {
    for (const chatId of chatIds) assignChat(chatId, organization.chatGroups[chatId] ?? groupId);
  }
  for (const chatId of baseChatOrder) {
    const storedGroupId = organization.chatGroups[chatId];
    if (storedGroupId) assignChat(chatId, storedGroupId);
  }
  for (const groupId of baseGroupOrder) {
    const baseGroup = groupsById.get(groupId);
    for (const chat of baseGroup?.chats ?? []) assignChat(chat.id, groupId);
  }

  const buildChildren = (parentId: string, ancestors: Set<string>): TGroup[] => {
    const preferred = organization.groupChildren[parentId] ?? [];
    const children = [
      ...preferred,
      ...baseGroupOrder.filter((groupId) => !preferred.includes(groupId)),
    ].filter((groupId, index, all) => parentByChild.get(groupId) === parentId && all.indexOf(groupId) === index);
    return children.flatMap((groupId) => {
      if (ancestors.has(groupId)) return [];
      const source = groupsById.get(groupId);
      if (!source) return [];
      const nextAncestors = new Set(ancestors).add(groupId);
      return [{
        ...source,
        chats: chatsForGroup.get(groupId) ?? [],
        subGroups: buildChildren(groupId, nextAncestors),
      } as TGroup];
    });
  };
  return buildChildren(HISTORY_ORGANIZATION_ROOT, new Set());
}

export function saveHistoryOrganization<TChat extends { id: string }>(
  groups: HistoryOrganizationGroup<TChat>[],
  expectedRevision: number,
  storage: StorageLike | null = defaultStorage(),
  updatedAt = new Date().toISOString(),
): HistoryOrganizationSaveResult {
  if (!storage) return { ok: false, reason: "unavailable", message: "History organization storage is unavailable." };
  const current = loadHistoryOrganization(storage);
  if (current.status === "corrupt") return { ok: false, reason: "corrupt", message: current.message };
  if (current.status === "unavailable") return { ok: false, reason: "unavailable", message: current.message };
  const currentRevision = current.status === "ready" ? current.organization.revision : 0;
  if (currentRevision !== expectedRevision) {
    return { ok: false, reason: "conflict", message: "History organization changed in another window." };
  }
  const organization = organizationFromGroups(groups, currentRevision + 1, updatedAt);
  try {
    storage.setItem(HISTORY_ORGANIZATION_STORAGE_KEY, JSON.stringify(organization));
    return { ok: true, organization };
  } catch {
    return { ok: false, reason: "unavailable", message: "History organization could not be saved." };
  }
}

export function resetHistoryOrganization(storage: StorageLike | null = defaultStorage()): boolean {
  if (!storage) return false;
  try {
    storage.removeItem(HISTORY_ORGANIZATION_STORAGE_KEY);
    return true;
  } catch {
    return false;
  }
}
