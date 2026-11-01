"""blocks.template.convert — tool↔prompt 統一テンプレート変換ブロック。

入力:
    {
        "direction": "tool_to_prompt" | "prompt_to_tool" | "tool_to_unified" | "prompt_to_unified",
        "source_name": str (optional — 登録済みの名前で検索),
        "source_data": dict (optional — 直接定義を渡す),
        "register": bool (optional, default false — 変換結果を登録するか),
        "add_to_gallery": bool (optional, default false — ギャラリーに追加するか),
    }

出力:
    {"status": "ok", "data": {"converted": dict, "unified": dict}}
"""



from blocks._common import ok, error

from domain.template.unified import (
    UnifiedTemplate,
    tool_to_unified,
    prompt_to_unified,
    unified_to_tool,
    unified_to_prompt,
    convert_tool_to_prompt,
    convert_prompt_to_tool,
)
from domain.template.gallery import get_gallery


def run(input_data, context):
    """tool↔prompt 変換を実行する。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    direction = input_data.get("direction", "")
    source_name = input_data.get("source_name")
    source_data = input_data.get("source_data")
    do_register = input_data.get("register", False)
    add_to_gallery = input_data.get("add_to_gallery", False)

    valid_directions = (
        "tool_to_prompt",
        "prompt_to_tool",
        "tool_to_unified",
        "prompt_to_unified",
    )
    if direction not in valid_directions:
        return error(
            f"direction must be one of: {', '.join(valid_directions)}",
            "INVALID_PARAM",
        )

    # ソースデータの解決
    resolved_source = _resolve_source(direction, source_name, source_data)
    if resolved_source is None:
        return error(
            "Could not resolve source. Provide source_name or source_data.",
            "SOURCE_NOT_FOUND",
        )

    # 変換実行
    if direction == "tool_to_prompt":
        unified = tool_to_unified(resolved_source)
        converted = unified_to_prompt(unified)
    elif direction == "prompt_to_tool":
        unified = prompt_to_unified(resolved_source)
        converted = unified_to_tool(unified)
    elif direction == "tool_to_unified":
        unified = tool_to_unified(resolved_source)
        converted = unified.to_dict()
    elif direction == "prompt_to_unified":
        unified = prompt_to_unified(resolved_source)
        converted = unified.to_dict()
    else:
        return error("Unknown direction", "INVALID_PARAM")

    # 登録処理
    registration_result = None
    if do_register:
        registration_result = _register_converted(direction, converted)

    # ギャラリーへの追加
    gallery_entry = None
    if add_to_gallery:
        gallery = get_gallery()
        if direction in ("tool_to_unified", "prompt_to_unified"):
            ut = unified
        else:
            # converted (tool_def or prompt_def) から再度 unified を生成
            if direction == "tool_to_prompt":
                ut = unified  # 既に計算済み
            else:
                ut = unified  # 既に計算済み
        entry = gallery.add_entry(template=ut)
        gallery_entry = entry.to_summary()

    result = {
        "converted": converted,
        "unified": unified.to_dict() if isinstance(unified, UnifiedTemplate) else unified,
    }
    if registration_result is not None:
        result["registration"] = registration_result
    if gallery_entry is not None:
        result["gallery_entry"] = gallery_entry

    return ok(result)


def _resolve_source(direction: str, source_name, source_data):
    """ソースデータを解決する。

    source_data が直接渡されていればそれを返す。
    source_name があれば、direction に応じて ToolRegistry / PromptManager から取得する。
    """
    if source_data is not None and isinstance(source_data, dict):
        return source_data

    if source_name is None:
        return None

    if direction.startswith("tool_"):
        from domain.tool.registry import ToolRegistry
        registry = ToolRegistry()
        tool_def = registry.get(source_name)
        return tool_def
    elif direction.startswith("prompt_"):
        from domain.prompt.manager import get_manager
        manager = get_manager()
        prompt_def = manager.get_prompt_by_name(source_name)
        if prompt_def is None:
            prompt_def = manager.get_prompt(source_name)
        return prompt_def

    return None


def _register_converted(direction: str, converted: dict):
    """変換結果を対応するレジストリに登録する。"""
    if direction == "tool_to_prompt":
        from domain.prompt.manager import get_manager
        manager = get_manager()
        prompt = manager.create_prompt(converted)
        return {"type": "prompt", "id": prompt.get("id", ""), "name": prompt.get("name", "")}
    elif direction == "prompt_to_tool":
        from domain.tool.registry import ToolRegistry
        registry = ToolRegistry()
        name = converted.get("name", converted.get("tool_id", ""))
        existing = registry.get(name)
        if existing is not None:
            return {"type": "tool", "error": f"Tool '{name}' already exists", "registered": False}
        registry.register(converted)
        return {"type": "tool", "name": name, "registered": True}
    else:
        return {"type": "unified", "note": "Unified templates are not registered to tool/prompt registries"}
