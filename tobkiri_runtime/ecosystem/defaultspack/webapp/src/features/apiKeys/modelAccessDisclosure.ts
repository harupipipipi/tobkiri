export type ModelAccessDisclosureState = {
  settingsOpen: boolean;
  modelListOpen: boolean;
};

export type ModelAccessDisclosureAction =
  | { type: "loading" }
  | { type: "loaded"; hasPolicy: boolean }
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
    return { settingsOpen: !action.hasPolicy, modelListOpen: false };
  }
  if (action.type === "toggle-settings") {
    return { ...state, settingsOpen: action.open };
  }
  return { ...state, modelListOpen: action.open };
}
