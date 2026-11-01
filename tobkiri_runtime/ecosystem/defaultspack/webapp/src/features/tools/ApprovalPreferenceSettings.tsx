import type { ModelProfile } from "../../lib/api";
import type { ActionApprovalMode } from "./ActionApprovalControl";
import { APPROVAL_MODE_DEFINITIONS, approvalModeAvailable, normalizeApprovalMode, readApprovalPreferences } from "./approvalPreferences";

export function ApprovalPreferenceSettings({
  tools,
  availableModes = [],
  reviewerModels = [],
  onSettingChange,
}: {
  tools: Record<string, unknown>;
  availableModes?: readonly ActionApprovalMode[];
  reviewerModels?: readonly ModelProfile[];
  onSettingChange: (sectionId: string, fieldId: string, value: unknown) => void;
}) {
  const preferences = readApprovalPreferences(tools);
  // Canonical presentation maps registered model_profile_id to profile_id.
  // These admitted records are preferences; Host capture still verifies the route.
  const registeredModels = reviewerModels.filter((model, index) => (
    typeof model.profile_id === "string" && model.profile_id.trim() === model.profile_id
    && model.profile_id.length > 0
    && reviewerModels.findIndex((candidate) => candidate.profile_id === model.profile_id) === index
  ));
  const reviewerReference = typeof tools.approval_reviewer_model === "string" ? tools.approval_reviewer_model : "";
  const reviewerRegistered = registeredModels.some((model) => model.profile_id === reviewerReference);
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
      <label className="block text-sm text-zinc-300">
        承認を審査する別のAI
        <select className="mt-2 block w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2"
          value={reviewerReference}
          onChange={(event) => {
            const reference = event.target.value;
            if (reference === "" || registeredModels.some((model) => model.profile_id === reference)) {
              onSettingChange("tools", "approval_reviewer_model", reference);
            }
          }}>
          <option value="">未選択（代理承認は利用できません）</option>
          {reviewerReference && !reviewerRegistered && (
            <option value={reviewerReference} disabled>保存されたモデル（現在利用できません）</option>
          )}
          {registeredModels.map((model) => (
            <option key={model.profile_id} value={model.profile_id}>
              {model.display_name}{model.provider_display_name || model.provider_id ? ` / ${model.provider_display_name || model.provider_id}` : ""}
            </option>
          ))}
        </select>
      </label>
      {!reviewerRegistered && <p role="status" className="text-xs text-amber-300">登録済みの審査モデルを選んでください。選択できるモデルがない場合は、モデルの設定を確認してください。</p>}
      <p className="text-xs leading-5 text-zinc-500">この設定だけでは代理承認は有効になりません。ランタイムが審査モデルと接続先を確認し、高リスク操作や審査できない操作は実行を止め、人の承認を求めます。</p>
      <dl className="space-y-2 text-xs leading-5 text-zinc-500">
        {APPROVAL_MODE_DEFINITIONS.map((option) => (
          <div key={option.mode}><dt className="text-zinc-300">{option.label}</dt><dd>{option.description}</dd></div>
        ))}
      </dl>
    </section>
  );
}
