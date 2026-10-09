import type { ComposerCommandItem } from "./api";

/** Approval changes own their draft until capability checks and persistence settle. */
export function approvalCommandOwnsDraft(command: ComposerCommandItem | undefined): boolean {
  return command?.execution.type === "frontend"
    && ["toggle_yolo", "toggle_ultra_yolo"].includes(command.execution.action);
}

export async function completeApprovalCommandDraft(
  execute: () => Promise<boolean | void>,
  draftIsCurrent: () => boolean,
  clearDraft: () => void,
): Promise<void> {
  const accepted = await execute();
  if (accepted === true && draftIsCurrent()) clearDraft();
}
