import { Check, FilePenLine, LoaderCircle, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import type { PromptStudioPrompt } from "../../../lib/api";
import {
  settingsFeatureResources,
  type PromptStudioData,
} from "../../../features/settings/resources/settingsFeatureResources";
import type { SettingsFieldRendererProps } from "../fieldRendererRegistry";
import { SettingsFieldShell } from "./settingsFieldRendererUtils";

function promptId(prompt: PromptStudioPrompt): string {
  return String(prompt.prompt_id ?? prompt.id ?? "").trim();
}

function promptBody(prompt: PromptStudioPrompt | null | undefined): string {
  return String(prompt?.body ?? prompt?.content ?? "");
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "応答の方針を読み込めませんでした。";
}

function usableStudio(data: PromptStudioData | null | undefined): data is PromptStudioData {
  return data != null && Array.isArray(data.prompts);
}

/** Select and author the prompt profile that new conversations actually receive. */
export function PromptProfileField({ sectionId, field, value, onChange }: SettingsFieldRendererProps) {
  const [studio, setStudio] = useState<PromptStudioData | null>(null);
  const [selectedPromptId, setSelectedPromptId] = useState(String(value ?? field.default ?? "").trim());
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadStudio = useCallback(async (requestedPromptId?: string) => {
    setBusy(true);
    setError(null);
    try {
      const data = await settingsFeatureResources.getPromptStudio(requestedPromptId ? { prompt_id: requestedPromptId } : undefined);
      if (!usableStudio(data)) {
        throw new Error("応答の方針を読み込めませんでした。もう一度お試しください。");
      }
      setStudio(data);
      const selected = data.prompts.find((prompt) => promptId(prompt) === requestedPromptId)
        ?? data.selected_prompt
        ?? data.prompts[0]
        ?? null;
      if (selected) setDraft(promptBody(selected));
    } catch (loadError) {
      setError(errorText(loadError));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadStudio(selectedPromptId || undefined);
  }, [loadStudio]);

  const prompts = studio?.prompts ?? [];
  const selectedPrompt = useMemo(
    () => prompts.find((prompt) => promptId(prompt) === selectedPromptId)
      ?? studio?.selected_prompt
      ?? null,
    [prompts, selectedPromptId, studio?.selected_prompt],
  );
  const selectedId = selectedPrompt ? promptId(selectedPrompt) : "";
  const editable = Boolean(selectedPrompt) && selectedPrompt?.read_only !== true && selectedPrompt?.editable !== false;

  const choosePrompt = (nextId: string) => {
    setSelectedPromptId(nextId);
    onChange(sectionId, field.id, nextId);
    const nextPrompt = prompts.find((prompt) => promptId(prompt) === nextId) ?? null;
    setDraft(promptBody(nextPrompt));
    setMessage(nextId ? "新しい会話にこの応答の方針を使います。" : "新しい会話は既定の応答方針を使います。");
  };

  const save = async () => {
    if (!studio || !selectedPrompt || !selectedId || !editable || busy) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await settingsFeatureResources.savePrompt({
        profile_id: studio.profile_id,
        prompt_id: selectedId,
        body: draft,
        expected_body_hash: selectedPrompt.body_hash,
        expected_exists: true,
        reason: "Updated from Tobkiri Settings personalization",
      });
      setMessage("応答の方針を保存しました。次の会話から使われます。");
      await loadStudio(selectedId);
    } catch (saveError) {
      setError(errorText(saveError));
    } finally {
      setBusy(false);
    }
  };

  const createOverride = async () => {
    if (!studio || !selectedPrompt || !selectedId || busy) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await settingsFeatureResources.createPromptOverride({
        profile_id: studio.profile_id,
        prompt_id: selectedId,
        body: draft,
        expected_body_hash: selectedPrompt.body_hash,
        expected_exists: true,
        reason: "Editable personalization from Tobkiri Settings",
      });
      setMessage("編集用のコピーを作成しました。");
      await loadStudio(selectedId);
    } catch (overrideError) {
      setError(errorText(overrideError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsFieldShell field={field}>
      <div className="space-y-3" data-settings-renderer="prompt_profile">
        <div className="rounded-xl border border-zinc-800 bg-zinc-950/45 p-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p className="text-xs font-medium text-zinc-200">新しい会話の応答の方針</p>
              <p className="mt-1 text-[11px] leading-4 text-zinc-500">ここで選んだシステムプロンプトを、新しく始める会話にだけ適用します。既存の会話は変わりません。</p>
            </div>
            <button type="button" onClick={() => void loadStudio(selectedPromptId || undefined)} disabled={busy} className="inline-flex h-7 w-7 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-800 hover:text-zinc-100 disabled:opacity-50" aria-label="応答の方針を更新" title="更新">
              <RefreshCw size={13} className={busy ? "animate-spin" : ""} aria-hidden="true" />
            </button>
          </div>
          <label className="mt-3 block space-y-1 text-[11px] text-zinc-500">
            <span>使う方針</span>
            <select value={selectedPromptId} onChange={(event) => choosePrompt(event.target.value)} disabled={busy} className="min-h-10 w-full rounded-lg border border-zinc-800 bg-zinc-900 px-3 text-sm text-zinc-100 outline-none focus:border-zinc-600 disabled:opacity-50">
              <option value="">既定の応答方針</option>
              {prompts.map((prompt) => {
                const id = promptId(prompt);
                return <option key={id} value={id}>{prompt.name || id}</option>;
              })}
            </select>
          </label>
        </div>

        {selectedPrompt && (
          <div className="rounded-xl border border-zinc-800 bg-zinc-950/30 p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <p className="truncate text-xs font-medium text-zinc-200">{selectedPrompt.name || selectedId}</p>
                <p className="mt-0.5 text-[11px] text-zinc-500">{selectedPrompt.description || "この方針は新しい会話の最初に適用されます。"}</p>
              </div>
              <span className="rounded-full border border-zinc-800 px-2 py-0.5 text-[10px] text-zinc-500">{editable ? "編集できます" : "読み取り専用"}</span>
            </div>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              disabled={!editable || busy}
              spellCheck={false}
              aria-label={`${selectedPrompt.name || selectedId} の内容`}
              className="mt-3 min-h-36 w-full resize-y rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-[12px] leading-5 text-zinc-100 outline-none placeholder:text-zinc-600 focus:border-zinc-600 disabled:cursor-not-allowed disabled:opacity-60"
            />
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <p className="text-[11px] leading-4 text-zinc-500">変更は保存後、新しく始める会話に反映されます。</p>
              {editable ? (
                <button type="button" disabled={busy} onClick={() => void save()} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-200 bg-zinc-100 px-3 text-xs font-medium text-zinc-950 transition-colors hover:bg-white disabled:opacity-50">
                  {busy ? <LoaderCircle size={13} className="animate-spin" aria-hidden="true" /> : <Check size={13} aria-hidden="true" />}保存
                </button>
              ) : (
                <button type="button" disabled={busy} onClick={() => void createOverride()} className="inline-flex min-h-9 items-center gap-1.5 rounded-lg border border-zinc-700 px-3 text-xs text-zinc-200 transition-colors hover:bg-zinc-800 disabled:opacity-50">
                  <FilePenLine size={13} aria-hidden="true" />編集用コピーを作る
                </button>
              )}
            </div>
          </div>
        )}

        {!busy && prompts.length === 0 && !error && <p className="rounded-lg border border-zinc-800 bg-zinc-950/30 px-3 py-2 text-[11px] leading-5 text-zinc-500">利用できる応答の方針はまだありません。方針を作成すると、ここで新しい会話に選べます。</p>}
        {message && <p role="status" className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-[11px] leading-5 text-emerald-100">{message}</p>}
        {error && <p role="alert" className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-[11px] leading-5 text-red-100">{error}</p>}
      </div>
    </SettingsFieldShell>
  );
}
