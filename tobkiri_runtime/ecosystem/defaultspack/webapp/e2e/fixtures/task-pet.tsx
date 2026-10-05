import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { TaskPet } from "../../src/components/TaskPet";
import type { SavedTurnEventSnapshot } from "../../src/lib/api";
import "../../src/index.css";

const scope = { profileId: "default", conversationId: "pet-conversation", turnId: "pet-turn" };
function snapshot(completed: boolean): SavedTurnEventSnapshot {
  const status = completed ? "completed" : "running";
  const revision = completed ? 2 : 1;
  const identity = { turn_id: scope.turnId, conversation_id: scope.conversationId, operation_id: scope.turnId, request_id: "pet-request", turn_revision: revision, status };
  return {
    ...identity, events: [],
    turn: { id: scope.turnId, conversation_id: scope.conversationId, status, revision },
    terminal: completed ? { ...identity, result_reference: {
      conversation_id: scope.conversationId, conversation_revision: 2,
      user_message_id: "pet-user", assistant_message_id: "pet-assistant",
      outcome_digest: `sha256:${"a".repeat(64)}`,
    } } : null,
  };
}
function Fixture() {
  const [completed, setCompleted] = useState(false);
  const [longText, setLongText] = useState(false);
  const [count, setCount] = useState(0);
  return <main className="rumi-app-shell p-8">
    <h1>親の会話画面</h1>
    <button onClick={() => setCompleted(true)}>タスクを完了</button>
    <button onClick={() => setLongText(true)}>長いタスクを表示</button>
    <button onClick={() => setCount((value) => value + 1)}>親画面を操作</button>
    <p role="status">親画面の操作回数: {count}</p>
    <TaskPet profileId={scope.profileId} scope={scope} snapshot={snapshot(completed)} snapshotProfileId={scope.profileId} taskText={longText ? "日本語の長いタスクを独立したペットウィンドウで見守っています。".repeat(12) : "独立ウィンドウでタスクを見守る"} activityText="保存済みの実行状態を確認しています" />
  </main>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
