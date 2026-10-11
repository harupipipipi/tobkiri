import type { ComposerCommandItem } from "./api";
import type { TaskPetHandle } from "../components/TaskPet";

/** Identify the reserved command token while preserving escaped literal text. */
export function isTaskPetCommandInput(rawInput: string): boolean {
  return /^\/pet(?:\s|$)/i.test(rawInput.trim());
}

/** Recognize only the built-in benign pet presentation command. */
export function isLocalTaskPetCommand(command: ComposerCommandItem): boolean {
  return command.id === "pet" && command.name === "pet"
    && (!command.canonical_id || command.canonical_id === "defaultspack:pet")
    && command.risk === "low"
    && command.execution.type === "frontend"
    && command.execution.action === "open_task_pet";
}

/** Consume the reserved pet command even when its catalogue is unavailable. */
export function consumeLocalTaskPetInput(
  rawInput: string,
  enabled: boolean,
  parse: (raw: string) => { command: ComposerCommandItem } | null,
  dispatch: (command: ComposerCommandItem, raw: string) => void,
  unavailable: () => void,
): boolean {
  if (!isTaskPetCommandInput(rawInput)) return false;
  const parsed = enabled ? parse(rawInput) : null;
  if (!parsed || !isLocalTaskPetCommand(parsed.command)) {
    unavailable();
    return true;
  }
  dispatch(parsed.command, rawInput.trim());
  return true;
}

/** Open the current profile's pet directly while retaining the user's gesture. */
export async function executeLocalTaskPetCommand(
  command: ComposerCommandItem,
  profileId: string,
  controller: TaskPetHandle | null,
  onError: (message: string) => void,
): Promise<boolean> {
  if (!isLocalTaskPetCommand(command)) return false;
  if (!controller || controller.profileId !== profileId) {
    onError("ペットの準備ができていません。もう一度 /pet を実行してください。");
    return false;
  }
  try {
    // Invoke before the first await: browser window.open needs user activation.
    return await controller.open();
  } catch {
    onError("ペットウィンドウを開けませんでした。もう一度 /pet を実行してください。");
    return false;
  }
}

/** Clear the submitted command only if its draft still owns the completion. */
export async function completeLocalTaskPetSubmission(
  open: () => Promise<boolean | void>,
  draftIsCurrent: () => boolean,
  clearDraft: () => void,
  submittedDraft: string,
): Promise<void> {
  const opened = await open();
  const draft = submittedDraft.trim();
  const petDraft = ["/", "/p", "/pe"].includes(draft.toLowerCase())
    || isTaskPetCommandInput(draft);
  if (opened === true && petDraft
    && draftIsCurrent()) clearDraft();
}
