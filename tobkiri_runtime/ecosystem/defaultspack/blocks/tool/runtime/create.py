"""
blocks.tool.runtime.create — AI記述からtool定義を生成する

input_data:
  - description: str（必須）— 自然言語でのtool説明
  - model: str（任意）— AIモデル指定 ("provider/model")
  - name: str（任意）— 手動指定時のtool名
  - parameters: dict（任意）— 手動指定時のJSON Schema
  - handler_code: str（任意）— 手動指定時のhandlerコード

手動指定フィールド (name, description, parameters, handler_code) が
全て揃っている場合はAI生成をスキップし、そのまま定義を返す。
"""


from blocks._common import ok, error


def run(input_data, context):
    """AI記述からtool定義を生成、または手動指定の定義を返す"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    description = input_data.get("description")
    if not description or not isinstance(description, str):
        return error("description is required and must be a non-empty string", "MISSING_PARAM")

    # 手動指定モード: name, parameters, handler_code が全て指定されている場合
    manual_name = input_data.get("name")
    manual_params = input_data.get("parameters")
    manual_code = input_data.get("handler_code")

    if manual_name and manual_params and manual_code:
        tool_def = {
            "name": manual_name,
            "description": description,
            "parameters": manual_params,
            "handler_code": manual_code,
        }
        return ok({"tool_def": tool_def, "source": "manual"})

    # AI生成モード
    from domain.tool.runtime_creator import RuntimeToolCreator

    creator = RuntimeToolCreator()
    model = input_data.get("model")

    try:
        tool_def = creator.generate_from_description(description, model=model)
    except RuntimeError as exc:
        return error("AI generation failed: {}".format(exc), "GENERATION_ERROR")

    return ok({"tool_def": tool_def, "source": "ai"})
