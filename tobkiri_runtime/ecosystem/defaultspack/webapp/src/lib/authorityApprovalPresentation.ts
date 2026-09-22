import type { InteractiveApprovalRequest } from "./api";

export const AUTHORITY_DETAIL_UNAVAILABLE = "利用できません";

export type ApprovalAuthorityDetails = {
  targetPrincipal: string;
  baseScope: string;
  maxUses: string;
  remainingUses: string;
};

function availableInteger(value: number | null | undefined): string {
  return Number.isSafeInteger(value) && Number(value) >= 0
    ? String(value)
    : AUTHORITY_DETAIL_UNAVAILABLE;
}

function availableScope(value: Record<string, unknown> | null | undefined): string {
  if (!value || Array.isArray(value)) return AUTHORITY_DETAIL_UNAVAILABLE;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return AUTHORITY_DETAIL_UNAVAILABLE;
  }
}

/** Build a fail-closed, secret-free display projection for the approval UI. */
export function approvalAuthorityDetails(
  request: InteractiveApprovalRequest,
): ApprovalAuthorityDetails {
  const targetPrincipal = request.target_principal_id?.trim();
  return {
    targetPrincipal: targetPrincipal || AUTHORITY_DETAIL_UNAVAILABLE,
    baseScope: availableScope(request.base_scope),
    maxUses: availableInteger(request.max_uses),
    remainingUses: availableInteger(request.remaining_uses),
  };
}
