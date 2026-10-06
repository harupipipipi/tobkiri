import type { ActionApprovalMode } from "./ActionApprovalControl";
import { APPROVAL_MODE_DEFINITIONS, approvalModeAvailable, normalizeApprovalMode, readApprovalPreferences } from "./approvalPreferences";

export function ApprovalPreferenceSettings({
  tools,
  availableModes = [],
  onSettingChange,
}: {
  tools: Record<string, unknown>;
  availableModes?: readonly ActionApprovalMode[];
  onSettingChange: (sectionId: string, fieldId: string, value: unknown) => void;
}) {
  const preferences = readApprovalPreferences(tools);
  const selectedAvailable = approvalModeAvailable(preferences.selectedMode, availableModes);
  const fixedAvailable = approvalModeAvailable(preferences.fixedMode, availableModes);
  return (
    <section className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-950/35 p-4">
      <h4 className="text-sm font-medium text-zinc-100">操作の承認</h4>
      <label className="flex items-center gap-3 text-sm text-zinc-300">
        <input type="checkbox" checked={preferences.controlVisible}
          onChange={(event) => onSettingChange("tools", "show_action_approval_control", event.target.checked)} />
        入力欄に承認モードを表示
      </label>
      <p className="text-xs leading-5 text-zinc-500">非表示にすると、下の固定モードをすべての新しい送信に使います。</p>
      {preferences.controlVisible && !selectedAvailable && (
        <div className="space-y-2">
          <p role="status" className="text-xs text-amber-300">保存された入力欄の承認モードは、現在利用できません。</p>
          <button type="button" className="rounded-lg border border-zinc-700 px-3 py-2 text-xs text-zinc-200"
            onClick={() => onSettingChange("tools", "action_approval_mode", "ask")}>
            入力欄のモードを「人が承認」に戻す
          </button>
        </div>
      )}
      <label className="block text-sm text-zinc-300">
        非表示時の固定モード（必須）
        <select className="mt-2 block w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2"
          required value={preferences.fixedMode}
          onChange={(event) => {
            const mode = normalizeApprovalMode(event.target.value);
            if (approvalModeAvailable(mode, availableModes)) onSettingChange("tools", "fixed_action_approval_mode", mode);
          }}>
          {APPROVAL_MODE_DEFINITIONS.map((option) => (
            <option key={option.mode} value={option.mode} disabled={!approvalModeAvailable(option.mode, availableModes)}>
              {option.label}{approvalModeAvailable(option.mode, availableModes) ? "" : "（現在利用できません）"}
            </option>
          ))}
        </select>
      </label>
      {!fixedAvailable && <p role="status" className="text-xs text-amber-300">保存された固定モードは、このランタイムでは利用できません。利用できるモードを選んでください。</p>}
      <dl className="space-y-2 text-xs leading-5 text-zinc-500">
        {APPROVAL_MODE_DEFINITIONS.map((option) => (
          <div key={option.mode}><dt className="text-zinc-300">{option.label}</dt><dd>{option.description}</dd></div>
        ))}
      </dl>
    </section>
  );
}
