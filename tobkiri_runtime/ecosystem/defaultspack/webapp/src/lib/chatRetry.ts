import type { SavedTurnViewTicket } from "./optimisticSavedTurn";

export type ChatRetryOwnership = {
  errorMessage: string;
  errorGeneration: number;
  storeId: string | null;
  profileId: string;
  viewTicket: SavedTurnViewTicket;
};

export type ChatRetryContext = {
  conversationId: string | null;
  error: string | null;
  errorGeneration: number;
  storeId: string | null;
  profileId: string;
  viewTicket: SavedTurnViewTicket;
  isGenerating: boolean;
  hasPendingSavedTurn: boolean;
  hasActiveSubmission: boolean;
};

/** Allows a pre-start retry only while its original error and owner remain current. */
export function chatRetryEligible(
  retry: ChatRetryOwnership | null,
  context: ChatRetryContext,
): boolean {
  return Boolean(retry
    && retry.storeId !== null && retry.storeId === context.storeId
    && retry.profileId === context.profileId
    && retry.viewTicket.conversationId === context.conversationId
    && retry.errorMessage === context.error
    && retry.errorGeneration === context.errorGeneration
    && retry.viewTicket.epoch === context.viewTicket.epoch
    && retry.viewTicket.workspaceTabId === context.viewTicket.workspaceTabId
    && retry.viewTicket.conversationId === context.viewTicket.conversationId
    && !context.isGenerating
    && !context.hasPendingSavedTurn
    && !context.hasActiveSubmission);
}
