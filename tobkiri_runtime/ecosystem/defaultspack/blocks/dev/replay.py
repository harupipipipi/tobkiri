"""defaults.dev.replay — 過去のリクエストを別のプロンプト/モデルで再実行する handler

入力:
    {
        "request_id": str,
        "overrides": {
            "model": str (任意),
            "system_prompt": str (任意),
            "tools": list (任意)  # ツール名のリスト or ツール定義リスト
        }
    }

出力:
    {"status": "ok", "data": {"original": {...}, "replay_result": {...}, "overrides_applied": {...}}}

P2-3: tools が文字列リスト（ツール名）の場合、ToolRegistry から JSON Schema を取得して変換。
P2-4: システムプロンプトの一時変更をスレッドセーフに（グローバル書き換えしない）。
"""

from blocks._common import ok, error, gen_id, timestamp

from domain.dev.inspector import Inspector
from domain.ai_client.client import AIClient
from domain.prompt.manager import get_manager
from domain.tool.registry import ToolRegistry


def _resolve_tools(tools_input):
    """P2-3: tools パラメータを解決する。

    文字列リスト（ツール名のリスト）の場合は ToolRegistry から
    実際のツール定義（JSON Schema）を取得して AI に渡す形式に変換する。
    既にツール定義 dict のリストであればそのまま返す。

    Returns:
        (resolved_tools, tool_names)
        resolved_tools: AI に渡すツール定義リスト
        tool_names: ツール名リスト（ログ用）
    """
    if not tools_input:
        return [], []

    registry = ToolRegistry()
    resolved = []
    names = []

    for item in tools_input:
        if isinstance(item, str):
            # ツール名 → ToolRegistry から定義を取得
            tool_def = registry.get(item)
            if tool_def is not None:
                schema = tool_def.get("schema", {})
                resolved.append({
                    "type": "function",
                    "function": {
                        "name": tool_def.get("name", item),
                        "description": tool_def.get("summary", ""),
                        "parameters": schema.get("parameters", {}),
                    },
                })
                names.append(tool_def.get("name", item))
            else:
                # 見つからないツール名はスキップ
                names.append(item + " (not found)")
        elif isinstance(item, dict):
            # 既にツール定義 dict の場合はそのまま使用
            resolved.append(item)
            func_name = item.get("name", item.get("function", {}).get("name", "unknown"))
            names.append(func_name)

    return resolved, names


def run(input_data: dict, context: dict) -> dict:
    request_id = input_data.get("request_id")
    if not request_id:
        return error("request_id is required", "INVALID_INPUT")

    overrides = input_data.get("overrides") or {}

    inspector = Inspector()
    original_log = inspector.get_log(request_id)
    if original_log is None:
        return error(
            f"Request log not found: {request_id}",
            "NOT_FOUND",
        )

    # オーバーライドの適用
    model = overrides.get("model") or original_log.get("model", "stub/default")
    system_prompt = overrides.get("system_prompt")
    tools_override = overrides.get("tools")

    # P2-3: tools パラメータの解決
    if tools_override is not None:
        resolved_tools, tool_names = _resolve_tools(tools_override)
    else:
        resolved_tools = original_log.get("tools_called", [])
        tool_names = list(resolved_tools) if resolved_tools else []

    # P2-4: スレッドセーフ — グローバルな PromptManager を書き換えない。
    # メッセージリストにローカルにシステムプロンプトを差し込む方式。
    messages = []

    # システムプロンプトの決定（ローカルに差し込む）
    if system_prompt is not None:
        # オーバーライド指定あり
        messages.append({"role": "system", "content": str(system_prompt)})
    elif original_log.get("prompt_used"):
        # オリジナルのプロンプトを使用
        messages.append({"role": "system", "content": original_log["prompt_used"]})
    else:
        # フォールバック: 現在のシステムプロンプト
        manager = get_manager()
        current_sys = manager.get_system_prompt()
        if current_sys:
            messages.append({"role": "system", "content": current_sys})

    # コンテキストから会話メッセージを復元
    context_info = original_log.get("context_info", {})
    original_messages = context_info.get("messages", [])
    if original_messages:
        # system ロールのメッセージは既にローカルに追加済みなのでスキップ
        for msg in original_messages:
            if msg.get("role") != "system":
                messages.append(msg)
    elif len(messages) <= 1:
        # メッセージが system のみの場合はダミーを追加
        messages.append({"role": "user", "content": "(replay: no original messages found)"})

    # AI呼び出し
    client = AIClient()
    replay_request_id = gen_id()

    try:
        result = client.complete(
            model=model,
            messages=messages,
            tools=resolved_tools if resolved_tools else None,
            params={},
        )
    except Exception as exc:
        result = {
            "content": f"[replay error] {exc}",
            "finish_reason": "error",
            "usage": {},
        }

    # リプレイログを記録
    effective_system_prompt = system_prompt if system_prompt is not None else original_log.get("prompt_used", "")
    inspector.log_request(
        request_id=replay_request_id,
        conversation_id=original_log.get("conversation_id"),
        model=model,
        prompt_used=effective_system_prompt,
        tools_called=tool_names,
        context_info={
            "replay_of": request_id,
            "messages": messages,
        },
    )

    overrides_applied = {}
    if overrides.get("model"):
        overrides_applied["model"] = model
    if system_prompt is not None:
        overrides_applied["system_prompt"] = system_prompt
    if tools_override is not None:
        overrides_applied["tools"] = tool_names

    return ok({
        "original": {
            "request_id": original_log["request_id"],
            "model": original_log["model"],
            "prompt_used": original_log["prompt_used"],
            "timestamp": original_log["timestamp"],
        },
        "replay_result": result,
        "replay_request_id": replay_request_id,
        "overrides_applied": overrides_applied,
    })
