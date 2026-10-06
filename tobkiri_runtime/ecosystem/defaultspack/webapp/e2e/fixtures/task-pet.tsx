import React, { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { TaskPet, type TaskPetHandle } from "../../src/components/TaskPet";
import { parseSlashCommandInput } from "../../src/App";
import { completeLocalTaskPetSubmission, consumeLocalTaskPetInput, executeLocalTaskPetCommand } from "../../src/lib/taskPetCommand";
import { ComposerRenderer } from "../../src/renderers/ComposerRenderer";
import type { ComposerCommandItem, ModelProfile, SavedTurnEventSnapshot } from "../../src/lib/api";
import "../../src/index.css";

const commands: ComposerCommandItem[] = [{
  id: "pet", name: "pet", label: "Pet", category: "chat", visibility: "default",
  risk: "low", modes: ["chat", "coding", "agent"], canonical_id: "defaultspack:pet",
  execution: { type: "frontend", action: "open_task_pet" },
}];
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
  const controllerRef = useRef<TaskPetHandle | null>(null);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [chatDispatches, setChatDispatches] = useState(0);
  const [generating, setGenerating] = useState(false);
  const [missingKey, setMissingKey] = useState(false);
  const [steerDispatches, setSteerDispatches] = useState(0);
  const inputRef = useRef(input);
  inputRef.current = input;
  const missingKeyProfile: ModelProfile = { profile_id: "missing-key", display_name: "Missing key", provider_id: "openai", metadata: { requires_api_key: true } };
  const submitLocal = (raw: string) => consumeLocalTaskPetInput(raw, true,
    (text) => parseSlashCommandInput(text, commands),
    (command) => {
      const submittedDraft = inputRef.current;
      void completeLocalTaskPetSubmission(
        () => executeLocalTaskPetCommand(command, scope.profileId, controllerRef.current, setError),
        () => inputRef.current === submittedDraft,
        () => setInput(""),
        submittedDraft,
      );
    }, () => setError("ペットの準備ができていません。"));
  const [completed, setCompleted] = useState(false);
  const [longText, setLongText] = useState(false);
  const [count, setCount] = useState(0);
  return <main className="rumi-app-shell p-8">
    <h1>親の会話画面</h1>
    <button onClick={() => setGenerating(true)}>生成中にする</button>
    <button onClick={() => setMissingKey(true)}>APIキーなしにする</button>
    <ComposerRenderer input={input} placeholder="コマンド入力" isGenerating={generating}
      selectedProfile={missingKey ? missingKeyProfile : null} favoriteProfiles={[]}
      thinkingLevel={null} contextUsage={{ ratio: 0, usedTokens: 0, maxContext: 0, label: "0%" }}
      inlineExtensions={[]} belowExtensions={[]} commands={commands} voiceInputEnabled={false}
      onInputChange={setInput} onModelProfileSelect={() => undefined} onThinkingLevelChange={() => undefined}
      onLocalCommandSubmit={submitLocal} onSubmit={() => setChatDispatches((value) => value + 1)}
      onSteerSubmit={() => setSteerDispatches((value) => value + 1)} />
    <output data-testid="chat-dispatches">{chatDispatches}</output>
    <output data-testid="steer-dispatches">{steerDispatches}</output>
    {error && <p role="alert">{error}</p>}
    <button onClick={() => setCompleted(true)}>タスクを完了</button>
    <button onClick={() => setLongText(true)}>長いタスクを表示</button>
    <button onClick={() => setCount((value) => value + 1)}>親画面を操作</button>
    <p role="status">親画面の操作回数: {count}</p>
    <TaskPet controllerRef={controllerRef} onError={setError} profileId={scope.profileId} scope={scope} snapshot={snapshot(completed)} snapshotProfileId={scope.profileId} taskText={longText ? "日本語の長いタスクを独立したペットウィンドウで見守っています。".repeat(12) : "独立ウィンドウでタスクを見守る"} activityText="保存済みの実行状態を確認しています" />
  </main>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
