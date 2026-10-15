"""
blocks/agent/org/define_role.py — ロール定義ブロック

POST /api/agent/org/roles

input_data:
    role_key      : str (必須) ロールキー
    display_name  : str (必須) 表示名
    system_prompt : str (必須) システムプロンプト
    allowed_tools : list[str] (任意) 利用可能ツール
    context_limit : int (任意) コンテキスト上限（デフォルト 128000）
"""


from blocks._common import ok, error
from domain.agent.role_registry import RoleRegistry


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    role_key = input_data.get("role_key")
    if not role_key or not isinstance(role_key, str) or not role_key.strip():
        return error("role_key is required and must be a non-empty string")

    display_name = input_data.get("display_name")
    if not display_name or not isinstance(display_name, str) or not display_name.strip():
        return error("display_name is required and must be a non-empty string")

    system_prompt = input_data.get("system_prompt")
    if not system_prompt or not isinstance(system_prompt, str) or not system_prompt.strip():
        return error("system_prompt is required and must be a non-empty string")

    allowed_tools = input_data.get("allowed_tools", [])
    if not isinstance(allowed_tools, list):
        return error("allowed_tools must be a list")

    context_limit = input_data.get("context_limit", 128000)
    if not isinstance(context_limit, int) or context_limit < 1:
        return error("context_limit must be a positive integer")

    registry = RoleRegistry()
    role = registry.define_role(
        role_key=role_key.strip(),
        display_name=display_name.strip(),
        system_prompt=system_prompt.strip(),
        allowed_tools=allowed_tools,
        context_limit=context_limit,
    )

    return ok(role)
