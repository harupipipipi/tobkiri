export type TaskPetHideEvidence = {
  targetIsHide: boolean;
  trustedPrimaryClick: boolean;
  matchingHideMessage: boolean;
  childClosed: boolean;
  parentOpen: boolean;
  persistedHidden: boolean;
};

/** Check action failures before waiting for application evidence; never swallow an unrelated error. */
export function taskPetHideActionOutcome(
  click: PromiseSettledResult<void>,
  close: PromiseSettledResult<unknown>,
): "click-acknowledged" | "target-closed-during-click" {
  const expectedCloseError = click.status === "rejected" && click.reason instanceof Error
    && /^locator\.click: Target page, context or browser has been closed(?:\r?\n|$)/.test(click.reason.message);
  if (click.status === "rejected" && !expectedCloseError) throw click.reason;
  if (close.status === "rejected") throw close.reason;
  return expectedCloseError ? "target-closed-during-click" : "click-acknowledged";
}

/** Only the normal Hide click's exact close race is an alternate acknowledgment. */
export function taskPetHideAcknowledgment(
  click: PromiseSettledResult<void>,
  close: PromiseSettledResult<unknown>,
  evidence: TaskPetHideEvidence,
): "click-acknowledged" | "target-closed-after-verified-hide" {
  const outcome = taskPetHideActionOutcome(click, close);
  for (const key of ["targetIsHide", "trustedPrimaryClick", "matchingHideMessage", "childClosed", "parentOpen", "persistedHidden"] as const) {
    if (evidence[key] !== true) throw new Error(`Pet Hide was not established: ${key}`);
  }
  return outcome === "target-closed-during-click" ? "target-closed-after-verified-hide" : outcome;
}
