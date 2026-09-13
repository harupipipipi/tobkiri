"""
domain.tool.ai_operator — AI 操作ループ。
スクリーンショット → AI分析 → アクション実行 → 繰り返し。
"""
import time
import uuid
import threading
from typing import Any

from domain.tool.container_manager import (
    get_container,
    exec_in_container,
)
from domain.tool.screen_controller import (
    take_screenshot,
    send_mouse_click,
    send_keyboard_input,
    send_mouse_move,
    send_mouse_drag,
    send_scroll,
)
from domain.ai_client.client import AIClient


# ---------------------------------------------------------------------------
# グローバル設定
# ---------------------------------------------------------------------------
_settings = {
    "mode": "fast",
    "fast_model": "google/gemini-2.0-flash",
    "heavy_model": "anthropic/claude-sonnet-4-20250514",
    "default_model": "google/gemini-2.0-flash",
    "max_steps": 50,
    "step_delay": 1.0,
}

_tasks: dict[str, "OperatorTask"] = {}
_task_lock = threading.Lock()


def get_settings():
    """現在の設定を返す"""
    return dict(_settings)


def update_settings(new_settings):
    """設定を更新する"""
    allowed_keys = ("mode", "fast_model", "heavy_model", "default_model", "max_steps", "step_delay")
    updated = {}
    for k in allowed_keys:
        if k in new_settings:
            _settings[k] = new_settings[k]
            updated[k] = new_settings[k]
    return dict(_settings)


def _select_model():
    """現在の mode に基づいてモデルを選択する"""
    mode = _settings.get("mode", "fast")
    if mode == "heavy":
        return _settings.get("heavy_model", "anthropic/claude-sonnet-4-20250514")
    elif mode == "fast":
        return _settings.get("fast_model", "google/gemini-2.0-flash")
    return _settings.get("default_model", "google/gemini-2.0-flash")


# ---------------------------------------------------------------------------
# タスク情報
# ---------------------------------------------------------------------------

class OperatorTask:
    """AI操作タスクの状態を管理する"""

    def __init__(self, task_id, container_id, instruction, config):
        self.task_id = task_id
        self.container_id = container_id
        self.instruction = instruction
        self.config = config
        self.status = "created"
        self.steps = []
        self.result = None
        self.error = None
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.started_at = None
        self.completed_at = None
        self._abort = False
        self._thread = None

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "container_id": self.container_id,
            "instruction": self.instruction,
            "status": self.status,
            "steps_count": len(self.steps),
            "steps": self.steps[-5:],
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def to_status_dict(self):
        """ステータス確認用（最新5ステップのみ）"""
        return {
            "task_id": self.task_id,
            "container_id": self.container_id,
            "instruction": self.instruction,
            "status": self.status,
            "steps_count": len(self.steps),
            "recent_steps": self.steps[-5:],
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    def to_result_dict(self):
        """結果取得用"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "steps": self.steps,
            "steps_count": len(self.steps),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


# ---------------------------------------------------------------------------
# AI 操作ループ
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an AI that controls a Linux desktop environment by analyzing screenshots and performing actions.

You receive a screenshot and a task instruction. Based on what you see, decide the next action.

Respond in JSON format ONLY, with one of these action types:

1. Move cursor: {"action": "move", "x": <int>, "y": <int>}
2. Click: {"action": "click", "x": <int>, "y": <int>, "button": "left"|"right"}
3. Type text: {"action": "type", "text": "<string>"}
4. Press key: {"action": "key", "key": "<key_name>"} (e.g., "Return", "ctrl+c", "alt+F4")
5. Scroll: {"action": "scroll", "x": <int>, "y": <int>, "direction": "up"|"down", "clicks": <int>}
6. Drag: {"action": "drag", "x1": <int>, "y1": <int>, "x2": <int>, "y2": <int>}
7. Execute command: {"action": "command", "command": "<shell_command>"}
8. Wait: {"action": "wait", "seconds": <int>}
9. Done: {"action": "done", "summary": "<task completion summary>"}

Rules:
- Always analyze the screenshot carefully before acting
- Move the cursor first when you need to inspect hover states or align before clicking
- Use coordinate-based clicks for GUI interaction
- Use commands for terminal/CLI tasks when visible
- Report "done" when the task is complete
- If stuck, try a different approach
- Coordinates are in pixels from top-left corner
"""


def _parse_ai_response(response_text):
    """AIの応答からJSONアクションをパースする"""
    import json as _json
    text = response_text.strip()

    # JSON ブロックを抽出
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        text = text[start:end].strip()
    elif "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        text = text[start:end].strip()

    # 先頭/末尾の余計なテキストを除去して JSON 部分を探す
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1:
        text = text[brace_start:brace_end + 1]

    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        return {"action": "done", "summary": "Failed to parse AI response: {}".format(response_text[:200])}


def _execute_action(container_id, action):
    """AIが決定したアクションを実行する"""
    action_type = action.get("action", "done")

    if action_type == "click":
        return send_mouse_click(
            container_id,
            action.get("x", 0),
            action.get("y", 0),
            action.get("button", "left"),
        )
    elif action_type == "type":
        return send_keyboard_input(container_id, text=action.get("text", ""))
    elif action_type == "key":
        return send_keyboard_input(container_id, key=action.get("key", "Return"))
    elif action_type == "scroll":
        return send_scroll(
            container_id,
            action.get("x", 512),
            action.get("y", 384),
            action.get("direction", "down"),
            action.get("clicks", 3),
        )
    elif action_type == "drag":
        return send_mouse_drag(
            container_id,
            action.get("x1", 0),
            action.get("y1", 0),
            action.get("x2", 100),
            action.get("y2", 100),
        )
    elif action_type == "command":
        return exec_in_container(container_id, action.get("command", "echo ok"))
    elif action_type == "move":
        return send_mouse_move(container_id, action.get("x", 0), action.get("y", 0))
    elif action_type == "wait":
        wait_secs = min(action.get("seconds", 2), 10)
        time.sleep(wait_secs)
        return {"success": True, "action": "wait", "seconds": wait_secs}
    elif action_type == "done":
        return {"success": True, "action": "done", "summary": action.get("summary", "Task completed")}
    else:
        return {"success": False, "action": action_type, "error": "Unknown action type"}


def _run_operator_loop(task):
    """AI操作ループを実行する（別スレッドで動く）"""
    task.status = "running"
    task.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    client = AIClient()
    model = _select_model()
    max_steps = task.config.get("max_steps", _settings.get("max_steps", 50))
    step_delay = task.config.get("step_delay", _settings.get("step_delay", 1.0))

    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "Task: {}\n\nI will now show you screenshots. Decide the next action.".format(task.instruction)},
    ]

    try:
        for step_num in range(1, max_steps + 1):
            if task._abort:
                task.status = "aborted"
                task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                return

            # 1. スクリーンショット取得
            screenshot = take_screenshot(task.container_id)
            screenshot_b64 = screenshot.get("image_base64", "")
            screenshot_error = screenshot.get("error")

            step_info = {
                "step": step_num,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "screenshot_available": bool(screenshot_b64),
            }

            # 2. AI にスクリーンショットを送信して分析
            if screenshot_b64:
                # 画像付きメッセージ
                user_msg = {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "Step {}. Analyze this screenshot and decide the next action for the task: {}".format(
                                step_num, task.instruction
                            ),
                        },
                    ],
                }
            else:
                # 画像なし（local-only or エラー）
                error_detail = screenshot_error or "No screenshot available"
                user_msg = {
                    "role": "user",
                    "content": "Step {}. Screenshot unavailable ({}). The container is in local/CLI mode. Use 'command' action to proceed with task: {}".format(
                        step_num, error_detail, task.instruction
                    ),
                }

            conversation.append(user_msg)

            try:
                response = client.complete(model, conversation)
                content = ""
                if isinstance(response, dict):
                    content = response.get("content", "")
                    if not content and "choices" in response:
                        choices = response.get("choices", [])
                        if choices:
                            msg = choices[0].get("message", {})
                            content = msg.get("content", "")
                if not content:
                    content = '{"action": "done", "summary": "No AI response received"}'
            except Exception as ai_err:
                content = '{{"action": "done", "summary": "AI error: {}"}}'.format(str(ai_err)[:100])

            # AIの応答を会話に追加
            conversation.append({"role": "assistant", "content": content})

            # 3. アクションをパースして実行
            action = _parse_ai_response(content)
            step_info["action"] = action
            step_info["ai_response"] = content[:500]

            action_result = _execute_action(task.container_id, action)
            step_info["action_result"] = action_result

            task.steps.append(step_info)

            # 4. 完了チェック
            if action.get("action") == "done":
                task.status = "completed"
                task.result = action.get("summary", "Task completed")
                task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                return

            # ステップ間のディレイ
            time.sleep(step_delay)

        # max_steps を超えた
        task.status = "completed"
        task.result = "Task reached maximum steps ({})".format(max_steps)
        task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    except Exception as exc:
        task.status = "error"
        task.error = str(exc)
        task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_task(container_id, instruction, config=None):
    """AI操作タスクを作成して非同期で実行を開始する"""
    container = get_container(container_id)
    if container is None:
        raise KeyError("container not found: {}".format(container_id))

    task_id = str(uuid.uuid4())
    task_config = config if config else {}

    task = OperatorTask(
        task_id=task_id,
        container_id=container_id,
        instruction=instruction,
        config=task_config,
    )

    with _task_lock:
        _tasks[task_id] = task

    # 非同期で操作ループを開始
    thread = threading.Thread(target=_run_operator_loop, args=(task,), daemon=True)
    task._thread = thread
    thread.start()

    return task.to_dict()


def get_task_status(task_id):
    """タスクのステータスを返す"""
    task = _tasks.get(task_id)
    if task is None:
        return None
    return task.to_status_dict()


def get_task_result(task_id):
    """タスクの結果を返す"""
    task = _tasks.get(task_id)
    if task is None:
        return None
    return task.to_result_dict()


def abort_task(task_id):
    """タスクを中断する"""
    task = _tasks.get(task_id)
    if task is None:
        raise KeyError("task not found: {}".format(task_id))
    task._abort = True
    # スレッドが終了するまで少し待つ
    if task._thread and task._thread.is_alive():
        task._thread.join(timeout=5)
    if task.status not in ("completed", "error", "aborted"):
        task.status = "aborted"
        task.completed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return task.to_dict()
