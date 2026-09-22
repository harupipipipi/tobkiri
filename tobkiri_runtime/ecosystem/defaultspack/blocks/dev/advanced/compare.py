"""defaults.dev.advanced.compare — リプレイ比較（A/Bテスト）handler

入力:
    {
        "request_id": str,                # 再実行する元リクエストID
        "variant_a": {                    # バリアントA のオーバーライド
            "model": str (任意),
            "system_prompt": str (任意),
            "tools": list (任意)
        },
        "variant_b": {                    # バリアントB のオーバーライド
            "model": str (任意),
            "system_prompt": str (任意),
            "tools": list (任意)
        }
    }

出力:
    {"status": "ok", "data": {"original": {...}, "variant_a": {...}, "variant_b": {...},
                               "comparison": {...}}}

過去のリクエストを2つの異なる設定で再実行し、結果を比較する。
"""

import time
from blocks._common import ok, error, gen_id

from domain.dev.inspector import Inspector
from domain.ai_client.client import AIClient
from domain.prompt.manager import get_manager
from domain.tool.registry import ToolRegistry
from domain.dev.profiler import Profiler


def _resolve_tools(tools_input):
    """ツールパラメータを解決する。

    文字列リスト（ツール名）の場合は ToolRegistry から定義を取得して変換。
    dict リストの場合はそのまま返す。

    Returns:
        (resolved_tools, tool_names)
    """
    if not tools_input:
        return [], []

    registry = ToolRegistry()
    resolved = []
    names = []

    for item in tools_input:
        if isinstance(item, str):
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
                names.append(item + " (not found)")
        elif isinstance(item, dict):
            resolved.append(item)
            func_name = item.get("name", item.get("function", {}).get("name", "unknown"))
            names.append(func_name)

    return resolved, names


def _build_messages(original_log, system_prompt_override):
    """オリジナルログからメッセージリストを構築する。"""
    messages = []

    if system_prompt_override is not None:
        messages.append({"role": "system", "content": str(system_prompt_override)})
    elif original_log.get("prompt_used"):
        messages.append({"role": "system", "content": original_log["prompt_used"]})
    else:
        manager = get_manager()
        current_sys = manager.get_system_prompt()
        if current_sys:
            messages.append({"role": "system", "content": current_sys})

    context_info = original_log.get("context_info", {})
    original_messages = context_info.get("messages", [])
    if original_messages:
        for msg in original_messages:
            if msg.get("role") != "system":
                messages.append(msg)
    elif len(messages) <= 1:
        messages.append({"role": "user", "content": "(compare: no original messages found)"})

    return messages


def _execute_variant(original_log, overrides, variant_label, inspector, profiler):
    """一つのバリアントを実行して結果を返す。

    Returns:
        {"variant": str, "model": str, "result": dict, "replay_request_id": str,
         "overrides_applied": dict, "duration_ms": float}
    """
    model = overrides.get("model") or original_log.get("model", "stub/default")
    system_prompt = overrides.get("system_prompt")
    tools_override = overrides.get("tools")

    if tools_override is not None:
        resolved_tools, tool_names = _resolve_tools(tools_override)
    else:
        resolved_tools = original_log.get("tools_called", [])
        tool_names = list(resolved_tools) if resolved_tools else []

    messages = _build_messages(original_log, system_prompt)

    client = AIClient()
    replay_request_id = gen_id()

    start_time = time.perf_counter()
    try:
        result = client.complete(
            model=model,
            messages=messages,
            tools=resolved_tools if resolved_tools else None,
            params={},
        )
    except Exception as exc:
        result = {
            "content": f"[compare error] {exc}",
            "finish_reason": "error",
            "usage": {},
        }
    duration_ms = (time.perf_counter() - start_time) * 1000

    # プロファイラに記録
    profiler.record_api_call(
        model=model,
        duration_ms=duration_ms,
        token_count=result.get("usage", {}).get("total_tokens", 0),
        metadata={"variant": variant_label, "original_request_id": original_log["request_id"]},
    )

    # Inspectorにログ記録
    effective_prompt = system_prompt if system_prompt is not None else original_log.get("prompt_used", "")
    inspector.log_request(
        request_id=replay_request_id,
        conversation_id=original_log.get("conversation_id"),
        model=model,
        prompt_used=effective_prompt,
        tools_called=tool_names,
        context_info={
            "compare_of": original_log["request_id"],
            "variant": variant_label,
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

    return {
        "variant": variant_label,
        "model": model,
        "result": result,
        "replay_request_id": replay_request_id,
        "overrides_applied": overrides_applied,
        "duration_ms": round(duration_ms, 3),
    }


def _compute_comparison(variant_a_result, variant_b_result):
    """2つのバリアント結果から比較情報を生成する。

    Returns:
        {"duration_diff_ms": float, "same_finish_reason": bool,
         "a_content_length": int, "b_content_length": int,
         "content_length_diff": int, "a_token_count": int, "b_token_count": int}
    """
    a_content = variant_a_result.get("result", {}).get("content", "")
    b_content = variant_b_result.get("result", {}).get("content", "")
    a_finish = variant_a_result.get("result", {}).get("finish_reason", "")
    b_finish = variant_b_result.get("result", {}).get("finish_reason", "")
    a_tokens = variant_a_result.get("result", {}).get("usage", {}).get("total_tokens", 0)
    b_tokens = variant_b_result.get("result", {}).get("usage", {}).get("total_tokens", 0)

    a_len = len(a_content) if isinstance(a_content, str) else 0
    b_len = len(b_content) if isinstance(b_content, str) else 0

    return {
        "duration_diff_ms": round(
            variant_a_result["duration_ms"] - variant_b_result["duration_ms"], 3
        ),
        "same_finish_reason": a_finish == b_finish,
        "a_finish_reason": a_finish,
        "b_finish_reason": b_finish,
        "a_content_length": a_len,
        "b_content_length": b_len,
        "content_length_diff": a_len - b_len,
        "a_token_count": a_tokens,
        "b_token_count": b_tokens,
        "content_identical": a_content == b_content,
    }


def run(input_data: dict, context: dict) -> dict:
    request_id = input_data.get("request_id")
    if not request_id:
        return error("request_id is required", "INVALID_INPUT")

    variant_a_overrides = input_data.get("variant_a") or {}
    variant_b_overrides = input_data.get("variant_b") or {}

    inspector = Inspector()
    profiler = Profiler()

    original_log = inspector.get_log(request_id)
    if original_log is None:
        return error(
            f"Request log not found: {request_id}",
            "NOT_FOUND",
        )

    # バリアントA を実行
    variant_a_result = _execute_variant(
        original_log, variant_a_overrides, "A", inspector, profiler,
    )

    # バリアントB を実行
    variant_b_result = _execute_variant(
        original_log, variant_b_overrides, "B", inspector, profiler,
    )

    # 比較
    comparison = _compute_comparison(variant_a_result, variant_b_result)

    return ok({
        "original": {
            "request_id": original_log["request_id"],
            "model": original_log["model"],
            "prompt_used": original_log["prompt_used"],
            "timestamp": original_log["timestamp"],
        },
        "variant_a": variant_a_result,
        "variant_b": variant_b_result,
        "comparison": comparison,
    })
