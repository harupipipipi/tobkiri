import { useMemo, useState } from "react";

import {
  previewClientDiagnostic,
  readClientDiagnosticPrivacyMode,
  writeClientDiagnosticPrivacyMode,
  type ClientDiagnosticPrivacyMode,
} from "../lib/clientDiagnostics";

type Props = { locale?: string };

const USER_SELECTABLE_MODES: Array<{
  mode: Extract<ClientDiagnosticPrivacyMode, "standard" | "local_only" | "disabled">;
  label: [string, string];
  description: [string, string];
}> = [
  {
    mode: "local_only",
    label: ["Keep diagnostics local", "診断を端末内だけに保つ"],
    description: [
      "Do not make diagnostic network requests. Safe recovery details can still appear on this device.",
      "診断のネットワーク送信を行いません。安全化された復旧情報はこの端末上で確認できます。",
    ],
  },
  {
    mode: "standard",
    label: ["Share redacted diagnostics", "編集済みの診断を共有する"],
    description: [
      "Send only the versioned allowlisted schema shown below, with short server retention.",
      "下に表示するバージョン固定・許可リスト方式の項目だけを、短期保持で送信します。",
    ],
  },
  {
    mode: "disabled",
    label: ["Disable diagnostics", "診断を無効にする"],
    description: [
      "Do not prepare or send global client diagnostics.",
      "グローバルなクライアント診断を作成・送信しません。",
    ],
  },
];

export function ClientDiagnosticPrivacyPanel({ locale = "ja" }: Props) {
  const japanese = locale.toLowerCase().startsWith("ja");
  const copy = (text: [string, string]) => text[japanese ? 1 : 0];
  const [mode, setMode] = useState<ClientDiagnosticPrivacyMode>(() => readClientDiagnosticPrivacyMode());
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const preview = useMemo(() => previewClientDiagnostic({
    source: "settings.preview",
    category: "diagnostic_preview",
    level: "info",
    message: "Redacted diagnostic preview",
    conversationId: "example-not-a-real-conversation",
    detail: {
      error_name: "ExampleError",
      error_code: "EXAMPLE",
      route: "/src/example.tsx",
      line: 12,
      column: 4,
      stack: "at Example (/src/example.tsx:12:4)",
    },
  }), []);
  const previewText = useMemo(() => JSON.stringify(preview, null, 2), [preview]);

  const updateMode = (nextMode: ClientDiagnosticPrivacyMode) => {
    setMode(writeClientDiagnosticPrivacyMode(nextMode));
    setCopyState("idle");
  };

  const copyPreview = async () => {
    try {
      await navigator.clipboard.writeText(previewText);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  return (
    <section className="space-y-4 rounded-lg border border-zinc-800 bg-zinc-950/40 p-4" data-testid="client-diagnostic-privacy-panel">
      <div>
        <h3 className="text-sm font-medium text-zinc-100">{copy(["Client diagnostic privacy", "クライアント診断のプライバシー"])}</h3>
        <p className="mt-1 text-xs leading-5 text-zinc-500">
          {copy([
            "Remote reporting is opt-in. Private sessions can also force reporting off regardless of this setting.",
            "リモート送信は明示的な選択が必要です。非公開セッションでは、この設定に関係なく送信を停止できます。",
          ])}
        </p>
      </div>

      <fieldset className="space-y-2">
        <legend className="sr-only">{copy(["Diagnostic reporting mode", "診断送信モード"])}</legend>
        {USER_SELECTABLE_MODES.map((option) => (
          <label key={option.mode} className="flex cursor-pointer gap-3 rounded-lg border border-zinc-800 bg-black/20 p-3 text-sm hover:border-zinc-700">
            <input
              type="radio"
              name="client-diagnostic-privacy"
              value={option.mode}
              checked={mode === option.mode}
              onChange={() => updateMode(option.mode)}
              className="mt-1"
            />
            <span>
              <span className="block font-medium text-zinc-200">{copy(option.label)}</span>
              <span className="mt-1 block text-xs leading-5 text-zinc-500">{copy(option.description)}</span>
            </span>
          </label>
        ))}
      </fieldset>

      <div className="rounded-lg border border-zinc-800 bg-black/20 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="text-xs font-medium text-zinc-300">{copy(["Inspect before copy or opt-in", "コピー・送信許可の前に確認"])}</p>
            <p className="mt-1 text-[11px] leading-4 text-zinc-600">
              {copy([
                "This synthetic preview shows every public field; it contains no current error, chat, prompt, path, or credential.",
                "この合成プレビューは公開項目をすべて示します。現在のエラー、会話、プロンプト、パス、認証情報は含みません。",
              ])}
            </p>
          </div>
          <button type="button" onClick={copyPreview} className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 focus-visible:ring-2">
            {copy(["Copy safe preview", "安全なプレビューをコピー"])}
          </button>
        </div>
        <pre className="mt-3 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-md bg-zinc-950 p-3 text-[11px] text-zinc-400" data-testid="client-diagnostic-safe-preview">
          {previewText}
        </pre>
        <p role="status" aria-live="polite" className="mt-2 text-xs text-zinc-500">
          {copyState === "copied" ? copy(["Safe preview copied.", "安全なプレビューをコピーしました。"])
            : copyState === "failed" ? copy(["Copy failed. Select the visible preview manually.", "コピーできませんでした。表示中のプレビューを手動で選択してください。"])
              : mode === "standard" ? copy(["Remote reporting: enabled", "リモート送信: 有効"])
                : copy(["Remote reporting: off", "リモート送信: オフ"])}
        </p>
      </div>
    </section>
  );
}
