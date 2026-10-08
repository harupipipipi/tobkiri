import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { loadApprovalPolicyCapabilities, type ApprovalPolicyApplicability,
  type ApprovalPolicyCapabilities } from "./approvalPolicyCapabilities";

const ASK_ONLY = ["ask"] as const;

/** Display current Host support; neither preferences nor this hook authorize actions. */
export function useApprovalPolicyCapabilities(target: ApprovalPolicyApplicability | null) {
  const profileId = target?.profile_id ?? "";
  const activationId = target?.activation_id ?? "";
  const conversationId = target?.conversation_id ?? null;
  const workspaceId = target?.workspace_id ?? "";
  const key = JSON.stringify([profileId, activationId, conversationId, workspaceId]);
  const [state, setState] = useState<{ key: string; projection: ApprovalPolicyCapabilities | null;
    reason: string } | null>(null);

  useEffect(() => {
    if (!profileId || !activationId || !workspaceId) return;
    const captured = Object.freeze({ profile_id: profileId, activation_id: activationId,
      conversation_id: conversationId, workspace_id: workspaceId });
    let current = true;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    const assertCurrent = () => { if (!current) throw new Error("approval view changed"); };
    const refresh = async () => {
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      try {
        const projection = await loadApprovalPolicyCapabilities(captured,
          (input, requestSignal) => api.actionApprovalPolicyCapabilities(input, { signal: requestSignal }),
          assertCurrent, signal);
        assertCurrent();
        setState({ key, projection, reason: projection.reason });
        refreshTimer = setTimeout(() => {
          if (!current) return;
          setState({ key, projection: null, reason: "承認方式の状態を更新しています。" });
          void refresh();
        }, Math.max(1, projection.expires_at * 1000 - Date.now()));
      } catch {
        if (current && !signal.aborted) {
          setState({ key, projection: null, reason: "承認方式の状態を確認できません。人が承認する方式を利用できます。" });
        }
      }
    };
    void refresh();
    return () => {
      current = false;
      controller?.abort();
      if (refreshTimer !== undefined) clearTimeout(refreshTimer);
    };
  }, [profileId, activationId, conversationId, workspaceId, key]);

  const current = state?.key === key ? state : null;
  const projection = current?.projection;
  const captureCurrentSupport = () => projection && projection.expires_at > Date.now() / 1000
    ? projection : null;
  const valid = captureCurrentSupport();
  return {
    availableModes: valid ? valid.available_modes : ASK_ONLY,
    captureCurrentSupport,
    captureAvailableModes: () => captureCurrentSupport()?.available_modes ?? ASK_ONLY,
    reason: current?.reason ?? "承認方式の状態を確認しています。",
  };
}
