import { SavedTurnViewFence, type SavedTurnViewTicket } from "./optimisticSavedTurn";
import { loadConversationForRefresh, resolveSupersededConversationRedirect } from "./chatRouteLoading";

export type ConversationLoadTicket = SavedTurnViewTicket & { loadSequence: number };

/** Owns selection and reads together, including an A -> B -> A transition. */
export class ConversationViewLoader {
  private loadSequence = 0;

  constructor(private readonly viewFence: SavedTurnViewFence) {}

  capture(): ConversationLoadTicket {
    return { ...this.viewFence.capture(), loadSequence: this.loadSequence };
  }

  matches(ticket: ConversationLoadTicket): boolean {
    return ticket.loadSequence === this.loadSequence && this.viewFence.matches(ticket);
  }

  /** Select the target before a read can yield or expose the previous root. */
  select(
    workspaceTabId: string,
    conversationId: string | null,
    publishSelection: (ticket: ConversationLoadTicket) => void,
  ): ConversationLoadTicket {
    this.loadSequence += 1;
    this.viewFence.synchronize(workspaceTabId, conversationId);
    const ticket = this.capture();
    publishSelection(ticket);
    return ticket;
  }

  /** Update the global list, but let only the initiating view choose a target. */
  async refresh<T extends { id: string }>({
    ticket, preferredId, locationConversationId, readList, publishList, loadConversation, onViewLoad,
  }: {
    ticket: ConversationLoadTicket;
    preferredId?: string | null;
    locationConversationId: () => string | null;
    readList: () => Promise<{ conversations: T[] }>;
    publishList: (conversations: T[]) => void;
    loadConversation: (
      id: string | null,
      workspaceTabId: string,
      onViewLoad: (ticket: ConversationLoadTicket) => void,
    ) => Promise<void>;
    onViewLoad?: (ticket: ConversationLoadTicket) => void;
  }): Promise<ConversationLoadTicket | null> {
    let refreshTicket = ticket;
    try {
      const result = await readList();
      publishList(result.conversations);
      if (!this.matches(refreshTicket)) return null;
      await loadConversationForRefresh({
        preferredId,
        activeConversationId: ticket.conversationId,
        locationChatId: locationConversationId(),
        listedConversations: result.conversations,
        loadConversation: (id) => {
          if (!this.matches(refreshTicket)) return Promise.resolve();
          return loadConversation(id, ticket.workspaceTabId, (next) => {
            refreshTicket = next;
            onViewLoad?.(next);
          });
        },
        isCurrentView: () => this.matches(refreshTicket),
      });
      return this.matches(refreshTicket) ? refreshTicket : null;
    } catch (error) {
      if (this.matches(refreshTicket)) throw error;
      return null;
    }
  }

  /** Publish only reads, redirects, and errors still owned by this selection. */
  async load<T extends { id: string; metadata?: unknown }>({
    workspaceTabId,
    conversationId,
    readConversation,
    publishSelection,
    publishConversation,
    publishRoute,
    refreshPreview,
  }: {
    workspaceTabId: string;
    conversationId: string | null;
    readConversation: (id: string) => Promise<T>;
    publishSelection: (ticket: ConversationLoadTicket) => void;
    publishConversation: (conversation: T, ticket: ConversationLoadTicket) => void;
    publishRoute?: (id: string | null) => void;
    refreshPreview?: (id: string | null, ticket: ConversationLoadTicket) => void;
  }): Promise<void> {
    let ticket = this.select(workspaceTabId, conversationId, publishSelection);
    publishRoute?.(conversationId);
    if (conversationId === null) {
      refreshPreview?.(null, ticket);
      return;
    }
    const redirectedIds = new Set<string>();
    try {
      while (ticket.conversationId && this.matches(ticket)) {
        const targetId = ticket.conversationId;
        const conversation = await readConversation(targetId);
        if (!this.matches(ticket)) return;
        if (conversation.id !== targetId) {
          throw new Error("Loaded conversation does not match the selected view.");
        }
        const redirect = resolveSupersededConversationRedirect(conversation, targetId);
        if (redirect) {
          if (redirectedIds.has(redirect)) throw new Error("Conversation redirect cycle.");
          redirectedIds.add(targetId);
          ticket = this.select(workspaceTabId, redirect, publishSelection);
          publishRoute?.(redirect);
          continue;
        }
        publishConversation(conversation, ticket);
        if (this.matches(ticket)) refreshPreview?.(targetId, ticket);
        return;
      }
    } catch (error) {
      if (this.matches(ticket)) throw error;
    }
  }
}

/** A loaded record may update tab metadata only for the selected target. */
export function conversationOwnsSelectedView(
  ticket: SavedTurnViewTicket,
  workspaceTabId: string,
  conversationId: string | null,
  loadedConversationId: string | null,
): boolean {
  return ticket.workspaceTabId === workspaceTabId
    && ticket.conversationId === conversationId
    && (conversationId === null ? loadedConversationId === null
      : loadedConversationId === conversationId);
}
