import {
  api,
  type AuthorityUiOperator,
  type InteractiveApprovalRequest,
  type InteractiveApprovalRequestsResponse,
} from "../../../lib/api";

/**
 * The dedicated approval window talks only to the V4 interactive-approval
 * contract. The Host owns effect execution after a decision.
 */
export const interactiveApprovalResources = {
  list() {
    return api.listInteractiveApprovals() as Promise<InteractiveApprovalRequestsResponse>;
  },

  get(requestId: string) {
    return api.getInteractiveApproval(requestId) as Promise<InteractiveApprovalRequest>;
  },

  approve(
    requestId: string,
    options: {
      confirmation_text: string;
      ui_operator: AuthorityUiOperator;
    },
  ) {
    return api.approveInteractiveApproval(requestId, options) as Promise<InteractiveApprovalRequest>;
  },

  deny(requestId: string, options: { ui_operator: AuthorityUiOperator }) {
    return api.denyInteractiveApproval(requestId, options) as Promise<InteractiveApprovalRequest>;
  },
};

export type {
  InteractiveApprovalRequest,
  InteractiveApprovalRequestsResponse,
};
