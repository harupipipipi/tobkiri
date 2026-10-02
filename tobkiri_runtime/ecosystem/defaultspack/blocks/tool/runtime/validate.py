"""
blocks.tool.runtime.validate — tool定義を検証する

input_data:
  - tool_def: dict（必須）— 検証対象のtool定義
    - name: str
    - description: str
    - parameters: dict (JSON Schema)
    - handler_code: str
"""


from blocks._common import ok, error


def run(input_data, context):
    """tool定義を検証し、結果を返す"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    tool_def = input_data.get("tool_def")
    if tool_def is None or not isinstance(tool_def, dict):
        return error("tool_def (dict) is required", "MISSING_PARAM")

    from domain.tool.runtime_creator import RuntimeToolCreator

    creator = RuntimeToolCreator()
    result = creator.validate_tool_definition(tool_def)

    return ok(result)
