import { useCallback, useRef, useState } from "react";

import {
  createCompanyOperationId,
  pendingCompanyAction,
  rejectedCompanyAction,
  type CompanyActionState,
  type CompanyMutationReceipt,
} from "./companyWorkspaceState";

export function useCompanyMutation<TPayload, TValue>(
  action: string,
  submitter?: (
    payload: TPayload,
    operationId: string,
  ) => Promise<CompanyMutationReceipt<TValue>>,
) {
  const [state, setState] = useState<CompanyActionState>({ phase: "idle" });
  const lastAttempt = useRef<{ payload: TPayload; operationId: string } | null>(null);
  const pending = useRef(false);
  const latestSubmitter = useRef(submitter);
  latestSubmitter.current = submitter;

  const submit = useCallback(async (
    payload: TPayload,
    operationId = createCompanyOperationId(action),
  ): Promise<CompanyMutationReceipt<TValue>> => {
    const run = latestSubmitter.current;
    if (!run || pending.current) {
      return {
        operationId,
        phase: "rejected",
        error: pending.current ? "This action is already pending." : "This action is unavailable.",
        retryable: false,
      };
    }
    pending.current = true;
    lastAttempt.current = { payload, operationId };
    setState(pendingCompanyAction(operationId));
    try {
      const receipt = await run(payload, operationId);
      if (receipt.phase === "committed") {
        lastAttempt.current = null;
        setState({ phase: "committed", operationId, message: "Saved", updatedAt: Date.now() });
      } else {
        setState({
          phase: "rejected",
          operationId,
          message: receipt.error ?? "The action was not saved.",
          retryable: receipt.retryable ?? true,
          ambiguous: receipt.ambiguous,
        });
      }
      return receipt;
    } catch (error) {
      const rejected = rejectedCompanyAction(operationId, error);
      setState(rejected);
      return {
        operationId,
        phase: "rejected",
        error: rejected.message,
        retryable: rejected.retryable,
        ambiguous: rejected.ambiguous,
      };
    } finally {
      pending.current = false;
    }
  }, [action]);

  const retry = useCallback(() => {
    const attempt = lastAttempt.current;
    if (!attempt) return Promise.resolve(null);
    return submit(attempt.payload, attempt.operationId);
  }, [submit]);

  return {
    state,
    pending: state.phase === "pending",
    canRetry: state.phase === "rejected" && Boolean(state.retryable && lastAttempt.current),
    submit,
    retry,
  };
}
