"""
blocks.tool.runtime.register — 検証済みtool定義をランタイムで登録する

input_data:
  - tool_def: dict（必須）— 登録するtool定義
    - name: str
    - description: str
    - parameters: dict (JSON Schema)
    - handler_code: str
  - tags: list（任意）— 追加タグ
"""


from blocks._common import ok, error


def run(input_data, context):
    """tool定義をランタイムで登録する（再起動不要）"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    tool_def = input_data.get("tool_def")
    if tool_def is None or not isinstance(tool_def, dict):
        return error("tool_def (dict) is required", "MISSING_PARAM")

    tags = input_data.get("tags")
    if tags is not None and isinstance(tags, list):
        tool_def["tags"] = tags

    from domain.tool.runtime_creator import RuntimeToolCreator

    creator = RuntimeToolCreator()

    try:
        registered = creator.register_runtime_tool(tool_def)
    except ValueError as exc:
        return error(str(exc), "VALIDATION_ERROR")
    except Exception as exc:
        return error("Registration failed: {}".format(exc), "REGISTER_ERROR")

    return ok({
        "tool_id": registered.get("tool_id", ""),
        "name": registered.get("name", ""),
        "summary": registered.get("summary", ""),
        "tags": registered.get("tags", []),
        "created_at": registered.get("created_at", ""),
        "runtime": registered.get("runtime", True),
    })
