import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { ComposerRenderer } from "../../src/renderers/ComposerRenderer";
import "../../src/index.css";

function Preview() {
  const [input, setInput] = useState("");
  const [waiting, setWaiting] = useState(true);
  const [sendCount, setSendCount] = useState(0);
  const [controlCount, setControlCount] = useState(0);
  return <div className="rumi-app-shell min-h-screen bg-zinc-950 p-6 text-zinc-100">
    <header className="border-b border-white/10 pb-4 text-sm text-zinc-400">Tobkiri · 送信状態の復旧プレビュー</header>
    <main className="mx-auto flex max-w-3xl flex-col gap-10 pb-8 pt-8">
      <p className="ml-auto w-fit rounded-3xl bg-white/10 px-5 py-3">hello ✓</p>
      <p>Hello! How can I help you today?</p>
      <p className="ml-auto w-fit rounded-3xl bg-white/10 px-5 py-3">あなたは元気？ ✓</p>
      <p>はい、とても元気です！ありがとうございます。</p>
      <p className="ml-auto w-fit rounded-3xl bg-white/10 px-5 py-3">使えるtool教えて ◷</p>
      <ComposerRenderer
        input={input}
        placeholder="タスクを入力..."
        isGenerating={waiting}
        steerControlsReady={!waiting}
        selectedProfile={{ profile_id: "stub/default", display_name: "Preview", provider_id: "stub", model_id: "default" }}
        favoriteProfiles={[]}
        thinkingLevel={null}
        contextUsage={{ ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" }}
        inlineExtensions={[]}
        belowExtensions={[]}
        voiceInputEnabled={false}
        onInputChange={setInput}
        onModelProfileSelect={() => undefined}
        onThinkingLevelChange={() => undefined}
        onSubmit={() => setSendCount((count) => count + 1)}
        onStopGenerating={() => setControlCount((count) => count + 1)}
        onSteerSubmit={() => setControlCount((count) => count + 1)}
        pendingRecovery={{
          message: waiting
            ? "送信結果を確認できません。下書きは編集できます。"
            : "待機を解除しました。未確認の送信記録は保持しています。",
          entries: [{ id: "third-request", label: "結果未確認", submittedText: "使えるtool教えて" }],
          ...(waiting ? { onDetach: () => setWaiting(false) } : {}),
        }}
      />
      <output data-testid="request-count">送信 {sendCount}・実行操作 {controlCount}</output>
    </main>
  </div>;
}

createRoot(document.getElementById("root")!).render(<Preview />);
