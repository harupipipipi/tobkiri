import { applyHistoryOrganization, type HistoryOrganizationGroup, type HistoryOrganizationV1 } from "../features/history/historyOrganization";

type MembershipGroup = {
  id: string;
  sourceGroupId?: string;
  custom?: boolean;
};

export type HistoryMembershipDecision =
  | { kind: "local" }
  | { kind: "blocked" }
  | { kind: "owner"; targetProjectId: string | null };

/** Built-in groups classify owner data; only Projects accept membership writes. */
export function historyMembershipDecision(source: MembershipGroup, target: MembershipGroup): HistoryMembershipDecision {
  const sourceProject = source.custom ? source.sourceGroupId ?? source.id : null;
  const targetProject = target.custom ? target.sourceGroupId ?? target.id : null;
  if (source.id === target.id || (sourceProject && sourceProject === targetProject)) return { kind: "local" };
  if (!sourceProject && !targetProject) return { kind: "blocked" };
  return { kind: "owner", targetProjectId: targetProject };
}

/** Serialize membership writes and fence callbacks after scope invalidation. */
export class HistoryMembershipMove {
  private generation = 0;
  private active: number | null = null;

  get pending(): boolean { return this.active !== null; }

  invalidate(): void { this.generation++; this.active = null; }

  async run(write: () => Promise<void>, current: () => boolean): Promise<
    { kind: "acknowledged" } | { kind: "stale" } | { kind: "busy" } | { kind: "failed"; error: unknown }
  > {
    if (this.pending) return { kind: "busy" };
    const ticket = ++this.generation;
    this.active = ticket;
    try {
      if (!current()) return { kind: "stale" };
      await write();
      return this.active === ticket && current() ? { kind: "acknowledged" } : { kind: "stale" };
    } catch (error) {
      return this.active === ticket && current() ? { kind: "failed", error } : { kind: "stale" };
    } finally {
      if (this.active === ticket) this.active = null;
    }
  }
}


/** Discard only one chat's local placement so owner membership can reproject it. */
export function withoutHistoryChatPlacement(organization: HistoryOrganizationV1 | null, chatId: string): HistoryOrganizationV1 | null {
  if (!organization) return null;
  const chatGroups = { ...organization.chatGroups };
  delete chatGroups[chatId];
  return { ...organization, chatGroups,
    chatOrder: Object.fromEntries(Object.entries(organization.chatOrder)
      .map(([groupId, ids]) => [groupId, ids.filter((id) => id !== chatId)])),
  };
}


/** Apply local ordering and hierarchy while keeping owner-derived membership. */
export function applyCanonicalHistoryOrganization<
  TChat extends { id: string },
  TGroup extends HistoryOrganizationGroup<TChat>,
>(baseGroups: TGroup[], organization: HistoryOrganizationV1 | null): TGroup[] {
  if (!organization) return baseGroups;
  const directMembers = new Map<string, Set<string>>();
  const collect = (groups: HistoryOrganizationGroup<TChat>[]) => {
    for (const group of groups) {
      directMembers.set(group.id, new Set(group.chats.map((chat) => chat.id)));
      collect(group.subGroups);
    }
  };
  collect(baseGroups);
  const sanitized: HistoryOrganizationV1 = {
    ...organization,
    chatGroups: {},
    chatOrder: Object.fromEntries(Object.entries(organization.chatOrder)
      .map(([groupId, ids]) => [groupId,
        ids.filter((id) => directMembers.get(groupId)?.has(id))])),
  };
  return applyHistoryOrganization(baseGroups, sanitized);
}
