import hashlib
import os
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

LEGACY_ONLY = True
LEGACY_NOTICE = (
    "domain.agent.multi is legacy-only. Primary company coordination is handled "
    "by domain.company.message_router.CompanySlackRuntime."
)

from blocks._common import gen_id, timestamp  # noqa: E402
from domain.agent.agent_def import AgentDefinition  # noqa: E402
from domain.ai_client.client import AIClient  # noqa: E402
from domain.coding.checkout_isolation import (  # noqa: E402
    CheckoutProvisioner,
    CheckoutRequest,
    CheckoutSecurityError,
    canonical_mode,
)
from domain.coding.workspace_policy import (  # noqa: E402
    require_registered_trusted_workspace,
)
from domain.coding.workspace_resolver import WorkspaceResolver  # noqa: E402


# ---------------------------------------------------------------------------
# MessageBus — エージェント間インメモリメッセージバス
# ---------------------------------------------------------------------------

class MessageBus:
    """エージェント間でメッセージを送受信するインメモリバス。

    shared_messages  : 全エージェントが参照できる共有メッセージ履歴
    private_queues   : エージェント名 → そのエージェント宛のメッセージリスト
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.shared_messages = []
        self.private_queues = {}

    def register_agent(self, agent_name):
        with self._lock:
            if agent_name not in self.private_queues:
                self.private_queues[agent_name] = []

    def post_shared(self, sender, content, turn_number):
        """共有メッセージを投稿する。"""
        msg = {
            "id": "msg_" + gen_id(),
            "sender": sender,
            "content": content,
            "turn": turn_number,
            "timestamp": timestamp(),
        }
        with self._lock:
            self.shared_messages.append(msg)
        return msg

    def post_direct(self, sender, target, content, turn_number):
        """特定エージェント宛にダイレクトメッセージを送る。"""
        msg = {
            "id": "msg_" + gen_id(),
            "sender": sender,
            "target": target,
            "content": content,
            "turn": turn_number,
            "timestamp": timestamp(),
        }
        with self._lock:
            if target in self.private_queues:
                self.private_queues[target].append(msg)
            self.shared_messages.append(msg)
        return msg

    def get_shared_history(self):
        with self._lock:
            return list(self.shared_messages)

    def get_private_messages(self, agent_name):
        with self._lock:
            return list(self.private_queues.get(agent_name, []))

    def to_dict(self):
        with self._lock:
            return {
                "shared_messages": list(self.shared_messages),
                "private_queues": {k: list(v) for k, v in self.private_queues.items()},
            }


# ---------------------------------------------------------------------------
# MultiAgentSession — セッション状態
# ---------------------------------------------------------------------------

class MultiAgentSession:
    """マルチエージェントセッションの全状態を保持する。"""

    def __init__(
        self,
        session_id,
        task,
        agents,
        orchestration,
        max_turns,
        workspace_root=None,
        worktree_mode=None,
        workspace_resolution=None,
        execution_attempt_id=None,
        base_commit=None,
        base_ref=None,
    ):
        self.session_id = session_id
        self.task = task
        self.agents = agents
        self.orchestration = orchestration
        self.max_turns = max_turns
        self.status = "created"
        self.current_turn = 0
        self.message_bus = MessageBus()
        resolved_worktree_mode = canonical_mode(worktree_mode, default="metadata_only")
        self.agent_contexts = {}
        self.shared_context = {
            "workspace": {
                "contract_version": "rumi.agent_workspace.v1",
                "mode": "multi_agent",
                "base_workspace_root": str(Path(str(workspace_root)).expanduser().resolve())
                if workspace_root
                else None,
                "workspace_id": workspace_resolution.workspace_id if workspace_resolution else None,
                "trusted": bool(workspace_resolution.trusted) if workspace_resolution else False,
                "worktree_mode": resolved_worktree_mode,
                "execution_attempt_id": execution_attempt_id,
                "base_commit": base_commit,
                "base_ref": base_ref,
                "merge_strategy": "manual_conflict_report",
            }
        }
        self.result = None
        self.error = None
        self.created_at = timestamp()
        self.updated_at = timestamp()

        for agent in self.agents:
            self.message_bus.register_agent(agent.name)
            workspace = _agent_workspace_contract(
                session_id,
                agent,
                workspace_root=workspace_root,
                worktree_mode=resolved_worktree_mode,
                attempt_id=(
                    f"{execution_attempt_id}:{agent.agent_id or agent.name}"
                    if execution_attempt_id
                    else None
                ),
                base_commit=base_commit,
                base_ref=base_ref,
            )
            agent.workspace = workspace
            self.agent_contexts[agent.name] = {
                "messages": [],
                "status": "idle",
                "turns_taken": 0,
                "done": False,
                "workspace": workspace,
            }

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "task": self.task,
            "agents": [a.to_dict() for a in self.agents],
            "orchestration": self.orchestration,
            "max_turns": self.max_turns,
            "status": self.status,
            "current_turn": self.current_turn,
            "message_bus": self.message_bus.to_dict(),
            "agent_contexts": {
                name: {
                    "status": ctx["status"],
                    "turns_taken": ctx["turns_taken"],
                    "done": ctx["done"],
                    "message_count": len(ctx["messages"]),
                    "workspace": ctx.get("workspace", {}),
                }
                for name, ctx in self.agent_contexts.items()
            },
            "shared_context": self.shared_context,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# MultiAgentOrchestrator — 本体
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"@(\w+)\s*:")

DONE_MARKER = "[DONE]"


def _safe_workspace_segment(value):
    segment = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip(".-")
    return (segment or "agent")[:80]


def _resolve_agent_workspace(workspace_id=None, workspace_root=None, context=None):
    if not workspace_id and not workspace_root:
        return None
    request = {}
    if workspace_id:
        request["workspace_id"] = workspace_id
    elif workspace_root:
        request["workspace_root"] = str(workspace_root)
    resolution = WorkspaceResolver().resolve(request, context or {})
    return require_registered_trusted_workspace(
        resolution,
        operation="agent.multi_execute",
    )


def _workspace_ignore(path):
    parts = set(Path(path).parts)
    return bool(parts & {".git", ".rumi", ".rumi_snapshots", ".rumi_agents", "__pycache__"})


def _workspace_manifest(root):
    manifest = {}
    root_path = Path(root)
    if not root_path.is_dir():
        return manifest
    for path in sorted(root_path.rglob("*")):
        if path.is_symlink() or not path.is_file() or _workspace_ignore(path.relative_to(root_path)):
            continue
        rel = path.relative_to(root_path).as_posix()
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest[rel] = digest.hexdigest()
    return manifest


def _agent_workspace_contract(
    session_id,
    agent_def,
    workspace_root=None,
    worktree_mode=None,
    *,
    attempt_id=None,
    base_commit=None,
    base_ref=None,
):
    resolved_mode = canonical_mode(worktree_mode, default="metadata_only")
    contract = {
        "contract_version": "rumi.agent_workspace.v1",
        "mode": resolved_mode,
        "session_id": session_id,
        "agent_id": agent_def.agent_id,
        "agent_name": agent_def.name,
        "write_scope": "none" if resolved_mode == "metadata_only" else "agent_checkout_root",
        "workspace_root": None,
        "shared_workspace_root": None,
        "base_workspace_root": None,
        "worktree": {
            "mode": resolved_mode,
            "path": None,
            "checkout_id": None,
            "attempt_id": None,
            "lease": None,
            "access_mode": "read_only" if resolved_mode == "metadata_only" else "write",
            "provenance": None,
        },
        "base_manifest": {},
    }
    if not workspace_root:
        if resolved_mode != "metadata_only":
            raise CheckoutSecurityError(
                f"{resolved_mode} requires a trusted repository workspace"
            )
        return contract

    base = Path(str(workspace_root)).expanduser().resolve()
    if not base.is_dir() or base.is_symlink():
        raise CheckoutSecurityError("workspace root must be a real directory")
    contract["base_workspace_root"] = str(base)
    if resolved_mode == "metadata_only":
        # Metadata mode is descriptive only.  In particular it must not hand
        # an agent a writable directory that callers could mistake for an
        # isolated checkout.
        return contract

    allocation_root = base.parent / ".tobkiri-workspaces" / _safe_workspace_segment(session_id)
    allocation_root.mkdir(parents=True, exist_ok=True)
    shared_dir = allocation_root / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    agent_dir = allocation_root / "agents" / _safe_workspace_segment(
        agent_def.agent_id or agent_def.name
    )
    attempt = str(attempt_id or f"{session_id}:{agent_def.agent_id or agent_def.name}")
    reviewer = "review" in str(agent_def.name).casefold() or "review" in str(agent_def.role).casefold()
    request = CheckoutRequest.from_values(
        repository=base,
        allocation_root=allocation_root,
        destination=agent_dir,
        mode=resolved_mode,
        attempt_id=attempt,
        trusted=True,
        base_commit=base_commit,
        base_ref=base_ref,
        read_only=reviewer,
    )
    registry_path = allocation_root / "checkout_registry.v1.json"
    record, lease, _lease_token = CheckoutProvisioner(registry_path=registry_path).provision(request)
    contract.update(
        {
            "workspace_root": record.path,
            "shared_workspace_root": str(shared_dir),
            "worktree": {
                "mode": record.mode,
                "path": record.path,
                "checkout_id": record.checkout_id,
                "attempt_id": record.attempt_id,
                "lease": lease.public_dict() if lease else None,
                "access_mode": record.access_mode,
                "provenance": record.to_dict(),
            },
            "base_manifest": _workspace_manifest(record.path) if record.path else {},
        }
    )
    return contract


def _workspace_merge_report(session):
    changed_by_agent = {}
    owners_by_path = {}
    for agent_name, ctx in session.agent_contexts.items():
        workspace = ctx.get("workspace", {})
        workspace_root = workspace.get("workspace_root")
        base_manifest = workspace.get("base_manifest", {})
        if not workspace_root:
            continue
        current = _workspace_manifest(workspace_root)
        changed = sorted(path for path, digest in current.items() if base_manifest.get(path) != digest)
        deleted = sorted(path for path in base_manifest if path not in current)
        changed_by_agent[agent_name] = {
            "changed_files": changed,
            "deleted_files": deleted,
            "workspace_root": workspace_root,
        }
        for path in changed + deleted:
            owners_by_path.setdefault(path, []).append(agent_name)
    conflicts = [
        {"path": path, "agents": sorted(set(owners))}
        for path, owners in sorted(owners_by_path.items())
        if len(set(owners)) > 1
    ]
    return {
        "merge_strategy": "manual_conflict_report",
        "merge_required": bool(conflicts or any(
            item["changed_files"] or item["deleted_files"]
            for item in changed_by_agent.values()
        )),
        "conflicts": conflicts,
        "agents": changed_by_agent,
    }


class MultiAgentOrchestrator:
    """複数エージェントの協調を管理するオーケストレーター。"""

    def __init__(self):
        self._client = AIClient()

    # ------------------------------------------------------------------
    # AI 呼び出しヘルパー（AgentEngine のパターンを踏襲）
    # ------------------------------------------------------------------

    def _ai_complete(self, messages, model, tools):
        """AIClient 経由で completion を取得する。"""
        try:
            result = self._client.complete(model, messages, tools=tools)
            return {"status": "ok", "data": result}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _parse_ai_response(self, ai_result):
        """AI レスポンスをパースして type/content を返す。"""
        if ai_result.get("status") != "ok":
            return {
                "type": "error",
                "content": ai_result.get("error", "AI call failed"),
            }
        data = ai_result.get("data", {})

        if isinstance(data, dict) and data.get("tool_calls"):
            tool_calls = data["tool_calls"]
            first_call = (
                tool_calls[0]
                if isinstance(tool_calls, list) and len(tool_calls) > 0
                else tool_calls
            )
            return {
                "type": "tool_call",
                "tool_name": first_call.get(
                    "name", first_call.get("function", {}).get("name", "unknown")
                ),
                "tool_args": first_call.get(
                    "args", first_call.get("function", {}).get("arguments", {})
                ),
                "raw": first_call,
            }

        content = ""
        if isinstance(data, dict):
            content = data.get("content", data.get("text", str(data)))
        elif isinstance(data, str):
            content = data
        else:
            content = str(data)
        return {"type": "text", "content": content}

    # ------------------------------------------------------------------
    # メッセージ構築
    # ------------------------------------------------------------------

    def _build_system_prompt(self, agent_def, session):
        """エージェント用のシステムプロンプトを構築する。"""
        parts = []
        if agent_def.system_prompt:
            parts.append(agent_def.system_prompt)

        parts.append(
            "Your name is '"
            + agent_def.name
            + "'. Your role: "
            + agent_def.role
        )

        other_names = [a.name for a in session.agents if a.name != agent_def.name]
        if other_names:
            parts.append(
                "Other agents in this session: "
                + ", ".join(other_names)
                + ". You can address them with @name: message."
            )

        parts.append(
            "The shared task is: " + session.task
        )
        workspace = getattr(agent_def, "workspace", None) or session.agent_contexts.get(agent_def.name, {}).get("workspace", {})
        if workspace.get("workspace_root"):
            parts.append(
                "Your isolated workspace path is: "
                + workspace["workspace_root"]
                + ". Keep edits inside it and report changed paths for merge review."
            )
        parts.append(
            "When you believe the task is fully complete, include '"
            + DONE_MARKER
            + "' in your response."
        )

        return "\n\n".join(parts)

    def _build_messages_for_agent(self, agent_def, session):
        """あるエージェント向けの messages リストを構築する。"""
        messages = []

        system_content = self._build_system_prompt(agent_def, session)
        messages.append({"role": "system", "content": system_content})

        messages.append({"role": "user", "content": "Task: " + session.task})

        shared = session.message_bus.get_shared_history()
        for msg in shared:
            if msg["sender"] == agent_def.name:
                messages.append({"role": "assistant", "content": msg["content"]})
            else:
                messages.append({
                    "role": "user",
                    "content": "[" + msg["sender"] + "]: " + msg["content"],
                })

        private_msgs = session.message_bus.get_private_messages(agent_def.name)
        ctx_private = session.agent_contexts[agent_def.name]["messages"]
        already_in_shared = {m["id"] for m in shared}
        for pm in private_msgs:
            if pm["id"] not in already_in_shared:
                messages.append({
                    "role": "user",
                    "content": "[DM from " + pm["sender"] + "]: " + pm["content"],
                })
        for cm in ctx_private:
            messages.append(cm)

        return messages

    # ------------------------------------------------------------------
    # ターン制御
    # ------------------------------------------------------------------

    def _select_next_agents_round_robin(self, session):
        """ラウンドロビンで次に発言するエージェントを1つ返す。"""
        agents = session.agents
        if not agents:
            return []
        idx = session.current_turn % len(agents)
        agent = agents[idx]
        if session.agent_contexts[agent.name]["done"]:
            for offset in range(1, len(agents)):
                candidate = agents[(idx + offset) % len(agents)]
                if not session.agent_contexts[candidate.name]["done"]:
                    return [candidate]
            return []
        return [agent]

    def _select_next_agents_directed(self, session):
        """直前のメッセージ中の @mention から次の発言者を決定する。"""
        shared = session.message_bus.get_shared_history()
        if not shared:
            return [session.agents[0]] if session.agents else []

        last_msg = shared[-1]
        content = last_msg.get("content", "")
        mentions = _MENTION_RE.findall(content)

        agent_map = {a.name: a for a in session.agents}
        targets = []
        for name in mentions:
            if name in agent_map and not session.agent_contexts[name]["done"]:
                if agent_map[name] not in targets:
                    targets.append(agent_map[name])

        if not targets:
            return self._select_next_agents_round_robin(session)
        return targets

    def _select_next_agents_free(self, session):
        """全エージェント（done でないもの）を返す。"""
        return [
            a for a in session.agents
            if not session.agent_contexts[a.name]["done"]
        ]

    def _select_next_agents(self, session):
        if session.orchestration == "directed":
            return self._select_next_agents_directed(session)
        elif session.orchestration == "free":
            return self._select_next_agents_free(session)
        else:
            return self._select_next_agents_round_robin(session)

    # ------------------------------------------------------------------
    # 1エージェントのターン実行
    # ------------------------------------------------------------------

    def _run_agent_turn(self, agent_def, session):
        """1エージェントの1ターンを実行し、応答を返す。"""
        ctx = session.agent_contexts[agent_def.name]
        ctx["status"] = "thinking"

        messages = self._build_messages_for_agent(agent_def, session)
        ai_result = self._ai_complete(messages, agent_def.model, agent_def.tools)
        parsed = self._parse_ai_response(ai_result)

        if parsed["type"] == "error":
            ctx["status"] = "error"
            return {"agent": agent_def.name, "type": "error", "content": parsed["content"]}

        if parsed["type"] == "tool_call":
            tool_summary = (
                "I need to call tool '"
                + parsed["tool_name"]
                + "' with args: "
                + str(parsed["tool_args"])
            )
            session.message_bus.post_shared(agent_def.name, tool_summary, session.current_turn)
            ctx["status"] = "idle"
            ctx["turns_taken"] += 1
            return {
                "agent": agent_def.name,
                "type": "tool_call",
                "content": tool_summary,
                "tool_name": parsed["tool_name"],
                "tool_args": parsed["tool_args"],
            }

        content = parsed["content"]

        mentions = _MENTION_RE.findall(content)
        agent_names = {a.name for a in session.agents}
        directed_targets = [m for m in mentions if m in agent_names and m != agent_def.name]

        if directed_targets:
            for target in directed_targets:
                session.message_bus.post_direct(
                    agent_def.name, target, content, session.current_turn
                )
        else:
            session.message_bus.post_shared(
                agent_def.name, content, session.current_turn
            )

        if DONE_MARKER in content:
            ctx["done"] = True

        ctx["status"] = "idle"
        ctx["turns_taken"] += 1
        session.updated_at = timestamp()

        return {"agent": agent_def.name, "type": "text", "content": content}

    # ------------------------------------------------------------------
    # free モード用: 並列実行
    # ------------------------------------------------------------------

    def _run_agents_parallel(self, agents, session):
        """複数エージェントを並列で実行する。"""
        results = []
        thread_agent_pairs = []

        result_lock = threading.Lock()

        def _worker(agent_def):
            r = self._run_agent_turn(agent_def, session)
            with result_lock:
                results.append(r)

        for agent_def in agents:
            t = threading.Thread(target=_worker, args=(agent_def,))
            thread_agent_pairs.append((t, agent_def))
            t.start()

        for t, _ad in thread_agent_pairs:
            t.join(timeout=120)

        # タイムアウトしたスレッドのエージェントを処理する
        agents_in_results = set()
        with result_lock:
            agents_in_results = {r["agent"] for r in results}

        for t, ad in thread_agent_pairs:
            if t.is_alive():
                session.agent_contexts[ad.name]["status"] = "timeout"
                if ad.name not in agents_in_results:
                    with result_lock:
                        results.append({
                            "agent": ad.name,
                            "type": "timeout",
                            "content": "Agent timed out after 120 seconds",
                        })

        return results

    # ------------------------------------------------------------------
    # オーケストレーション: メイン実行
    # ------------------------------------------------------------------

    def execute(
        self,
        task,
        agent_dicts,
        orchestration="round_robin",
        max_turns=10,
        workspace_root=None,
        workspace_id=None,
        worktree_mode=None,
        context=None,
        execution_attempt_id=None,
        base_commit=None,
        base_ref=None,
    ):
        """マルチエージェントタスクを開始し、完了まで実行する。

        Parameters
        ----------
        task : str
            タスク記述。
        agent_dicts : list[dict]
            各エージェントの定義 dict。
        orchestration : str
            "round_robin" | "directed" | "free"
        max_turns : int
            最大ターン数。

        Returns
        -------
        dict
            セッション結果。
        """
        session_id = "multi_" + gen_id()

        workspace_resolution = None
        try:
            workspace_resolution = _resolve_agent_workspace(
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                context=context,
            )
        except Exception as exc:
            return {
                "session_id": session_id,
                "status": "error",
                "error": str(exc),
            }
        if workspace_resolution is not None:
            workspace_root = workspace_resolution.root_path

        agents = []
        for ad in agent_dicts:
            agents.append(AgentDefinition.from_dict(ad))

        if not agents:
            return {
                "session_id": session_id,
                "status": "error",
                "error": "at least one agent is required",
            }

        try:
            session = MultiAgentSession(
                session_id=session_id,
                task=task,
                agents=agents,
                orchestration=orchestration,
                max_turns=max_turns,
                workspace_root=workspace_root,
                worktree_mode=worktree_mode,
                workspace_resolution=workspace_resolution,
                execution_attempt_id=execution_attempt_id or session_id,
                base_commit=base_commit,
                base_ref=base_ref,
            )
        except Exception as exc:
            return {
                "session_id": session_id,
                "status": "error",
                "error": str(exc),
            }
        session.status = "running"

        turn_results = []

        for turn in range(max_turns):
            session.current_turn = turn + 1
            session.updated_at = timestamp()

            next_agents = self._select_next_agents(session)
            if not next_agents:
                break

            if session.orchestration == "free" and len(next_agents) > 1:
                turn_res = self._run_agents_parallel(next_agents, session)
            else:
                turn_res = []
                for agent_def in next_agents:
                    r = self._run_agent_turn(agent_def, session)
                    turn_res.append(r)

            turn_results.extend(turn_res)

            all_done = all(
                session.agent_contexts[a.name]["done"] for a in session.agents
            )
            if all_done:
                break

        # --- ステータス3分岐 ---
        all_done = all(
            session.agent_contexts[a.name]["done"] for a in session.agents
        )
        if all_done:
            session.status = "completed"
        elif session.current_turn >= max_turns:
            session.status = "max_turns_reached"
        else:
            session.status = "stopped"

        final_messages = session.message_bus.get_shared_history()
        if final_messages:
            session.result = final_messages[-1].get("content", "")
        else:
            session.result = ""

        session.shared_context["workspace"]["merge_report"] = _workspace_merge_report(session)
        session.updated_at = timestamp()

        return {
            "session_id": session_id,
            "status": session.status,
            "session": session,
            "turn_results": turn_results,
            "result": session.to_dict(),
        }

    def create_session(
        self,
        task,
        agent_dicts,
        orchestration="round_robin",
        max_turns=10,
        workspace_root=None,
        workspace_id=None,
        worktree_mode=None,
        context=None,
        execution_attempt_id=None,
        base_commit=None,
        base_ref=None,
    ):
        """Create a visible multi-agent workspace session without running turns."""
        session_id = "multi_" + gen_id()
        workspace_resolution = None
        try:
            workspace_resolution = _resolve_agent_workspace(
                workspace_id=workspace_id,
                workspace_root=workspace_root,
                context=context,
            )
        except Exception as exc:
            return {
                "session_id": session_id,
                "status": "error",
                "error": str(exc),
            }
        if workspace_resolution is not None:
            workspace_root = workspace_resolution.root_path

        agents = [AgentDefinition.from_dict(ad) for ad in agent_dicts]
        if not agents:
            return {"session_id": session_id, "status": "error", "error": "at least one agent is required"}

        try:
            session = MultiAgentSession(
                session_id=session_id,
                task=task,
                agents=agents,
                orchestration=orchestration,
                max_turns=max_turns,
                workspace_root=workspace_root,
                worktree_mode=worktree_mode,
                workspace_resolution=workspace_resolution,
                execution_attempt_id=execution_attempt_id or session_id,
                base_commit=base_commit,
                base_ref=base_ref,
            )
        except Exception as exc:
            return {
                "session_id": session_id,
                "status": "error",
                "error": str(exc),
            }
        session.status = "created"
        session.shared_context["workspace"]["merge_report"] = _workspace_merge_report(session)
        session.updated_at = timestamp()
        return {
            "session_id": session_id,
            "status": session.status,
            "session": session,
            "result": session.to_dict(),
            "workspace": session.shared_context.get("workspace", {}),
        }

    def merge_report(self, session):
        return _workspace_merge_report(session)

    # ------------------------------------------------------------------
    # 外部メッセージ投入
    # ------------------------------------------------------------------

    def inject_message(self, session, message, target_agent=None):
        """実行中のセッションに外部からメッセージを投入する。"""
        if target_agent:
            agent_names = {a.name for a in session.agents}
            if target_agent not in agent_names:
                return {"status": "error", "error": "agent not found: " + target_agent}
            session.message_bus.post_direct(
                "user", target_agent, message, session.current_turn
            )
            session.agent_contexts[target_agent]["messages"].append({
                "role": "user",
                "content": "[User message]: " + message,
            })
        else:
            session.message_bus.post_shared("user", message, session.current_turn)
            for agent in session.agents:
                session.agent_contexts[agent.name]["messages"].append({
                    "role": "user",
                    "content": "[User message]: " + message,
                })

        session.updated_at = timestamp()

        return {
            "status": "ok",
            "session_id": session.session_id,
            "message": "Message injected successfully",
        }

    # ------------------------------------------------------------------
    # ステータス取得
    # ------------------------------------------------------------------

    def get_status(self, session):
        """セッションの状態を返す。"""
        return session.to_dict()
