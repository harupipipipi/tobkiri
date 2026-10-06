import type { ChatMessage, SavedTurn, SavedTurnContent } from "./api";

const SAVED_MESSAGE_ID = /^message:[a-f0-9]{64}$/;
const STABLE_TURN_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/;

export type OptimisticSavedTurnOverlay = {
  clientId: string;
  conversationId: string | null;
  expectedUserMessageId: string | null;
  message: ChatMessage;
  operationId: string | null;
  requestFingerprint: string | null;
  viewTicket: SavedTurnViewTicket;
};

export type SavedTurnViewTicket = {
  conversationId: string | null;
  epoch: number;
  workspaceTabId: string;
};

export class SavedTurnViewFence {
  private conversationId: string | null;
  private epoch = 0;
  private workspaceTabId: string;

  constructor(workspaceTabId: string, conversationId: string | null) {
    this.workspaceTabId = workspaceTabId;
    this.conversationId = conversationId;
  }

  synchronize(workspaceTabId: string, conversationId: string | null): boolean {
    if (this.workspaceTabId === workspaceTabId && this.conversationId === conversationId) return false;
    this.workspaceTabId = workspaceTabId;
    this.conversationId = conversationId;
    this.epoch += 1;
    return true;
  }

  /** Invalidates reads when the backing saved store changes in the same view. */
  invalidate(): void {
    this.epoch += 1;
  }

  capture(): SavedTurnViewTicket {
    return {
      workspaceTabId: this.workspaceTabId,
      conversationId: this.conversationId,
      epoch: this.epoch,
    };
  }

  matches(ticket: SavedTurnViewTicket): boolean {
    return this.workspaceTabId === ticket.workspaceTabId
      && this.conversationId === ticket.conversationId
      && this.epoch === ticket.epoch;
  }

  adoptConversation(
    ticket: SavedTurnViewTicket,
    conversationId: string,
  ): SavedTurnViewTicket | null {
    if (
      ticket.conversationId !== null
      || this.conversationId !== null
      || !STABLE_TURN_ID.test(conversationId)
      || !this.matches(ticket)
    ) return null;
    this.conversationId = conversationId;
    this.epoch += 1;
    return this.capture();
  }
}

/**
 * Allows recovery of a failed fresh-conversation draft only in the exact view
 * that adopted its durable root. A later tab/conversation transition, a live
 * pending root, or new composer state always wins over the old draft.
 */
export function shouldRestoreUnwrittenSavedTurnDraft(
  draft: Pick<OptimisticSavedTurnOverlay, "conversationId" | "operationId" | "viewTicket">,
  {
    activeConversationId,
    activeOperationId,
    activeViewTicket,
    composerIsEmpty,
    conversationIsEmpty,
  }: {
    activeConversationId: string | null;
    activeOperationId: string | null;
    activeViewTicket: SavedTurnViewTicket;
    composerIsEmpty: boolean;
    conversationIsEmpty: boolean;
  },
): boolean {
  return draft.conversationId !== null
    && draft.operationId !== null
    && draft.conversationId === activeConversationId
    && activeOperationId === null
    && draft.viewTicket.workspaceTabId === activeViewTicket.workspaceTabId
    && draft.viewTicket.conversationId === activeViewTicket.conversationId
    && draft.viewTicket.epoch === activeViewTicket.epoch
    && composerIsEmpty
    && conversationIsEmpty;
}

export type SavedTurnComposerPresentationInput = {
  activeConversationId: string | null;
  activeSavedTurnOperationId: string | null;
  canonicalMessageCount: number;
  visibleOverlayCount: number;
};

/**
 * Keeps controls inert only while the current fresh saved submission has a
 * visible local overlay but has not yet acquired both a conversation and an
 * operation ID. Other generating paths retain their existing controls.
 */
export function savedTurnComposerControlsReady({
  activeConversationId,
  activeSavedTurnOperationId,
  canonicalMessageCount,
  visibleOverlayCount,
}: SavedTurnComposerPresentationInput): boolean {
  return !(
    canonicalMessageCount === 0
    && visibleOverlayCount > 0
    && (!activeConversationId || !activeSavedTurnOperationId)
  );
}

/**
 * Keeps the ordinary composer available while the current view owns a
 * visible pending overlay. This includes the short creation interval before
 * a fresh conversation has an ID, as well as the interval after its durable
 * root is registered but before the owner appends a canonical message. This
 * is presentation-only: it does not create, alter, or replay a saved turn.
 */
export function savedTurnComposerPresentation({
  activeConversationId,
  activeSavedTurnOperationId,
  canonicalMessageCount,
  visibleOverlayCount,
}: SavedTurnComposerPresentationInput): {
  isNewConversation: boolean;
  showConversationComposer: boolean;
  showNewConversationStage: boolean;
} {
  const isNewConversation = activeConversationId === null || canonicalMessageCount === 0;
  const hasVisiblePendingSubmission = canonicalMessageCount === 0
    && visibleOverlayCount > 0;
  return {
    isNewConversation,
    showConversationComposer: !isNewConversation || hasVisiblePendingSubmission,
    showNewConversationStage: isNewConversation && !hasVisiblePendingSubmission,
  };
}

export function createOptimisticSavedTurnOverlay({
  clientId,
  content,
  createdAt,
  viewTicket,
}: {
  clientId: string;
  content: SavedTurnContent;
  createdAt: number;
  viewTicket: SavedTurnViewTicket;
}): OptimisticSavedTurnOverlay {
  const blocks = typeof content === "string" ? [{ type: "text", text: content }] : content;
  const rawText = typeof content === "string" ? content : content[0].text;
  return {
    clientId,
    conversationId: null,
    expectedUserMessageId: null,
    message: {
      id: `local-saved-turn:${clientId}`,
      role: "user",
      content: blocks,
      raw_text: rawText,
      created_at: createdAt,
      conversation_id: "",
      parent_id: null,
      children_ids: [],
      finish_reason: null,
      usage: null,
      widget: null,
      metadata: null,
    },
    operationId: null,
    requestFingerprint: null,
    viewTicket,
  };
}

export function bindOptimisticSavedTurnOverlay(
  overlay: OptimisticSavedTurnOverlay,
  {
    conversationId,
    operationId,
    requestFingerprint,
  }: {
    conversationId: string;
    operationId: string;
    requestFingerprint: string;
  },
): OptimisticSavedTurnOverlay {
  if (
    overlay.conversationId !== null
    || overlay.operationId !== null
    || !STABLE_TURN_ID.test(conversationId)
    || !STABLE_TURN_ID.test(operationId)
    || !requestFingerprint
  ) return overlay;
  return {
    ...overlay,
    conversationId,
    operationId,
    requestFingerprint,
    message: { ...overlay.message, conversation_id: conversationId },
  };
}

export function expectedSavedTurnUserMessageId(
  turn: SavedTurn,
  conversationId: string,
  operationId: string,
): string | null {
  if (turn.id !== operationId || turn.conversation_id !== conversationId) return null;
  if (turn.status === "completed") {
    const reference = turn.result_reference;
    if (!reference || reference.conversation_id !== conversationId
      || !Number.isSafeInteger(reference.conversation_revision)
      || reference.conversation_revision < 1
      || !SAVED_MESSAGE_ID.test(reference.user_message_id)
      || !SAVED_MESSAGE_ID.test(reference.assistant_message_id)
      || !/^sha256:[a-f0-9]{64}$/.test(reference.outcome_digest)) return null;
    return reference.user_message_id;
  }
  if (turn.status !== "running" && turn.status !== "waiting") return null;
  const claim = [...(turn.events ?? [])].reverse().find(
    (event) => event.name === "turn.running"
      && event.details?.phase === "saved_execution_claimed",
  );
  const userMessageId = claim?.details?.user_message_id;
  return typeof userMessageId === "string" && SAVED_MESSAGE_ID.test(userMessageId)
    ? userMessageId
    : null;
}

export function bindOptimisticSavedTurnExpectedUserMessageId(
  overlay: OptimisticSavedTurnOverlay,
  turn: SavedTurn,
): OptimisticSavedTurnOverlay {
  if (!overlay.conversationId || !overlay.operationId) return overlay;
  const expectedUserMessageId = expectedSavedTurnUserMessageId(
    turn,
    overlay.conversationId,
    overlay.operationId,
  );
  if (!expectedUserMessageId || (
    overlay.expectedUserMessageId && overlay.expectedUserMessageId !== expectedUserMessageId
  )) return overlay;
  return overlay.expectedUserMessageId === expectedUserMessageId
    ? overlay
    : { ...overlay, expectedUserMessageId };
}

export function optimisticSavedTurnOverlayHasCanonicalUserMessage(
  overlay: OptimisticSavedTurnOverlay,
  messages: readonly ChatMessage[],
): boolean {
  if (!overlay.conversationId || !overlay.operationId || !overlay.expectedUserMessageId) return false;
  return messages.some((message) => (
    message.id === overlay.expectedUserMessageId
    && message.role === "user"
    && message.conversation_id === overlay.conversationId
    && message.metadata?.turn_id === overlay.operationId
  ));
}

export function shouldDisplayOptimisticSavedTurnOverlay(
  overlay: OptimisticSavedTurnOverlay,
  {
    activeConversationId,
    activeOperationId,
    activeViewTicket,
    canonicalMessages,
  }: {
    activeConversationId: string | null;
    activeOperationId: string | null;
    activeViewTicket: SavedTurnViewTicket;
    canonicalMessages: readonly ChatMessage[];
  },
): boolean {
  if (overlay.conversationId === null) {
    return activeConversationId === null
      && overlay.operationId === null
      && overlay.viewTicket.workspaceTabId === activeViewTicket.workspaceTabId
      && overlay.viewTicket.conversationId === activeViewTicket.conversationId
      && overlay.viewTicket.epoch === activeViewTicket.epoch;
  }
  if (overlay.conversationId !== activeConversationId
    || overlay.operationId !== activeOperationId
    || overlay.viewTicket.workspaceTabId !== activeViewTicket.workspaceTabId) return false;
  return !optimisticSavedTurnOverlayHasCanonicalUserMessage(overlay, canonicalMessages);
}
