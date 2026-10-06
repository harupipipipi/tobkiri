import type { ActionApprovalMode } from "../tools/ActionApprovalControl";
import type { ApprovalPolicyApplicability, ApprovalPolicyCapabilities } from "../tools/approvalPolicyCapabilities";

/** Describe the actual scheduled destination for a read-only Host projection. */
export function calendarApprovalPolicyTarget({ profileId, activationId, workspaceId, conversationId, useCurrentChat }: {
  profileId: string;
  activationId: string | null;
  workspaceId: string | null;
  conversationId: string | null;
  useCurrentChat: boolean;
}): ApprovalPolicyApplicability | null {
  if (!profileId || !activationId || !workspaceId) return null;
  return { profile_id: profileId, activation_id: activationId, workspace_id: workspaceId,
    conversation_id: useCurrentChat ? conversationId : null };
}

/** Capture only mode and workspace preferences; each occurrence needs fresh authority. */
export function captureCalendarApprovalTaskContext(
  target: ApprovalPolicyApplicability | null,
  support: ApprovalPolicyCapabilities | null,
  captureMode: (availableModes: readonly ActionApprovalMode[]) => ActionApprovalMode,
  nowSeconds = Date.now() / 1000,
): Readonly<{ mode: ActionApprovalMode; workspaceId: string | null }> {
  const current = target && support && support.expires_at > nowSeconds
    && ["profile_id", "activation_id", "workspace_id", "conversation_id"].every((key) => (
      target[key as keyof ApprovalPolicyApplicability] === support[key as keyof ApprovalPolicyCapabilities]
    )) ? support : null;
  const modes = current?.available_modes ?? [];
  const mode = captureMode(modes);
  if (!["ask", "agent", "full"].includes(mode)
    || (mode !== "ask" && (!current?.workspace_id || !modes.includes(mode)))) {
    throw new Error("この承認方式には確認済みの作業領域が必要です。下書きは保持されています。");
  }
  return Object.freeze({ mode, workspaceId: current?.workspace_id ?? null });
}
