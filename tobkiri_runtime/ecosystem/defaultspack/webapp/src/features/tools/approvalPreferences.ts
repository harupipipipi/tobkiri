import type { ActionApprovalMode } from "./ActionApprovalControl";

export type ApprovalPreferences = Readonly<{
  controlVisible: boolean;
  selectedMode: ActionApprovalMode;
  fixedMode: ActionApprovalMode;
}>;

export const APPROVAL_MODE_DEFINITIONS: ReadonlyArray<{
  mode: ActionApprovalMode;
  label: string;
  description: string;
}> = [
  { mode: "ask", label: "人が承認", description: "承認が必要な操作は、あなたが確認してから実行します。" },
  { mode: "agent", label: "別のAIが承認", description: "別のAIが操作ごとに審査します。危険な操作や判定できない操作は、理由を知らせて停止します。" },
  { mode: "full", label: "追加承認なし", description: "許可された範囲の操作を追加の確認なしで実行します。禁止された操作は実行できません。" },
];

export function normalizeApprovalMode(value: unknown): ActionApprovalMode {
  return value === "agent" || value === "full" ? value : "ask";
}

/** Read canonical settings without writing defaults or replacing legacy values. */
export function readApprovalPreferences(tools: Record<string, unknown> = {}): ApprovalPreferences {
  return Object.freeze({
    controlVisible: typeof tools.show_action_approval_control === "boolean" ? tools.show_action_approval_control : true,
    selectedMode: normalizeApprovalMode(tools.action_approval_mode),
    fixedMode: normalizeApprovalMode(tools.fixed_action_approval_mode),
  });
}

/** Capture once when constructing a turn; subsequent settings edits cannot change it. */
export function captureApprovalMode(tools: Record<string, unknown> = {}): ActionApprovalMode {
  const preferences = readApprovalPreferences(tools);
  return preferences.controlVisible ? preferences.selectedMode : preferences.fixedMode;
}

/** Host-advertised support controls presentation, never grants execution authority. */
export function availableApprovalModes(hostModes: readonly unknown[] = []): ActionApprovalMode[] {
  return ["ask", ...(["agent", "full"] as const).filter((mode) => hostModes.includes(mode))];
}

export function approvalModeAvailable(mode: ActionApprovalMode, hostModes: readonly unknown[] = []): boolean {
  return availableApprovalModes(hostModes).includes(mode);
}

/** Reject an unsupported fixed preference before any turn or draft mutation. */
export function captureSupportedApprovalMode(
  tools: Record<string, unknown> = {},
  hostModes: readonly unknown[] = [],
): ActionApprovalMode {
  const mode = captureApprovalMode(tools);
  if (!approvalModeAvailable(mode, hostModes)) {
    throw new Error("選択した承認方式は現在利用できません。設定で「人が承認」を選んでください。下書きは保持しています。");
  }
  return mode;
}

/** Change a preference only; the Host still authorizes each submitted operation. */
export async function requestApprovalPreferenceChange(
  tools: Record<string, unknown> = {},
  nextMode: ActionApprovalMode,
  hostModes: readonly unknown[],
  onChange: (mode: ActionApprovalMode) => void | Promise<void>,
  onError: (message: string) => void,
): Promise<boolean> {
  if (!readApprovalPreferences(tools).controlVisible) {
    onError("承認方式は設定で固定されています。変更するには承認方式の設定を確認してください。");
    return false;
  }
  if (!approvalModeAvailable(nextMode, hostModes)) {
    onError("選択した承認方式は現在利用できません。承認方式は変更していません。");
    return false;
  }
  try {
    await onChange(nextMode);
    return true;
  } catch {
    onError("承認方式を保存できませんでした。承認方式の設定を確認してから再試行してください。");
    return false;
  }
}
