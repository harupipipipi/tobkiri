export function manualRuntimeModeSelectionEnabled(
  settingsValues: Record<string, Record<string, unknown>>,
): boolean {
  // Only an explicitly persisted JSON boolean unlocks manual mode selection.
  // Missing or malformed state remains in the agent-first default.
  return settingsValues.general?.manual_runtime_mode_selection === true;
}
