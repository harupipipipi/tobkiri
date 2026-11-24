import type { ModelAccessPolicy } from "./modelAccessPolicy";

export type ModelAccessDisclosureState = {
  settingsOpen: boolean;
  modelListOpen: boolean;
};

export type ModelAccessDisclosureAction =
  | { type: "loading" }
  | { type: "loaded" }
  | { type: "toggle-settings"; open: boolean }
  | { type: "toggle-model-list"; open: boolean };

export const initialModelAccessDisclosureState: ModelAccessDisclosureState = {
  settingsOpen: false,
  modelListOpen: false,
};

/** Keep large access controls closed while loading and model lists collapsed. */
export function modelAccessDisclosureReducer(
  state: ModelAccessDisclosureState,
  action: ModelAccessDisclosureAction,
): ModelAccessDisclosureState {
  if (action.type === "loading") return initialModelAccessDisclosureState;
  if (action.type === "loaded") {
    return initialModelAccessDisclosureState;
  }
  if (action.type === "toggle-settings") {
    return { ...state, settingsOpen: action.open };
  }
  return { ...state, modelListOpen: action.open };
}
/** Describe the saved policy without treating legacy inheritance as denial. */
export function modelAccessPolicySummary(
  loading: boolean,
  policy: ModelAccessPolicy | null | undefined,
): string {
  if (loading) return "設定を確認中";
  if (policy === undefined) return "状態を確認できません";
  if (policy === null) return "既存の許可設定を使用";
  return policy.mode === "all"
    ? "全モデルを許可"
    : `個別指定・${policy.model_ids.length}件`;
}
