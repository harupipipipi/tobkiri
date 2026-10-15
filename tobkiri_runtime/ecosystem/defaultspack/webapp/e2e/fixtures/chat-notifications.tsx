import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { ChatNotifications } from "../../src/components/ChatNotifications";
import "../../src/index.css";

function Preview() {
  const [error, setError] = useState<string | null>("Load failed");
  const [retried, setRetried] = useState(false);
  const [completionMessage, setCompletionMessage] = useState<string | null>(null);
  return <div className="rumi-app-shell bg-zinc-950 p-6 text-zinc-100">
    <header className="border-b border-white/10 pb-4 text-sm text-zinc-400">Tobkiri · 通知プレビュー</header>
    <main className="mx-auto mt-40 max-w-2xl space-y-10">
      <p className="ml-auto w-fit rounded-3xl bg-white/10 px-5 py-3">hello</p>
      <p>Hello! How can I help you today?</p>
      <p className="text-sm text-zinc-500">会話のレイアウトを動かさず、通知を画面上部に表示します。</p>
      <div className="flex flex-wrap gap-3 text-xs">
        <button className="rounded-lg border border-zinc-700 px-3 py-2" onClick={() => setError("Load failed")}>エラー通知</button>
        <button className="rounded-lg border border-zinc-700 px-3 py-2" onClick={() => setError(null)}>エラーを解消</button>
        <button className="rounded-lg border border-zinc-700 px-3 py-2" onClick={() => setError("詳細な通信エラー\n" + "保存結果の確認を待っています。".repeat(120))}>長い通知</button>
        <button className="rounded-lg border border-zinc-700 px-3 py-2" onClick={() => setCompletionMessage("返答は会話に保存されています。")}>完了通知</button>
        <button className="rounded-lg border border-zinc-700 px-3 py-2" onClick={() => setCompletionMessage("返答は会話に保存されています。".repeat(120))}>長い完了通知</button>
      </div>
      {retried && <p>再試行が押されました</p>}
    </main>
    <ChatNotifications
      error={error}
      completionNotice={completionMessage ? { tone: "success", title: "送信を確認しました", message: completionMessage } : null}
      onDismissError={() => setError(null)}
      onDismissCompletionNotice={() => setCompletionMessage(null)}
      onRetry={() => setRetried(true)}
    />
  </div>;
}

createRoot(document.getElementById("root")!).render(<Preview />);
