"""blocks.prompt.advanced.build — ステップバイステップ プロンプト構築 API

入力:
    {
        "name":        str,              # プロンプト名
        "description": str,              # (optional) 説明
        "sections": [                    # セクション定義リスト
            {
                "id":        str,        # セクション一意ID
                "body":      str,        # テンプレート本文
                "order":     int,        # (optional, default=0) 並び順
                "label":     str,        # (optional) 表示ラベル
                "enabled":   bool,       # (optional, default=True)
                "condition": dict|null   # (optional) 条件定義
            },
            ...
        ],
        "variables":   dict,             # (optional) 変数 {name: value}
        "parent":      str|null,         # (optional) 継承元プロンプト名
        "metadata":    dict,             # (optional)
        "save":        bool              # (optional, default=True) PromptManager に保存するか
    }

出力:
    {
        "status": "ok",
        "data": {
            "prompt":  {...},    # 作成/更新されたプロンプト dict (save=True の場合)
            "builder": {...},    # ビルダー状態
            "template": {...}    # ビルドされた PromptTemplate の dict 表現
        }
    }
"""


from blocks._common import ok, error
from domain.prompt.builder import PromptBuilder
from domain.prompt.manager import get_manager


def run(input_data: dict, context: dict) -> dict:
    name = input_data.get("name")
    if not name:
        return error("'name' is required", "INVALID_INPUT")

    sections = input_data.get("sections", [])
    if not isinstance(sections, list):
        return error("'sections' must be a list", "INVALID_INPUT")

    description = input_data.get("description", "")
    variables = input_data.get("variables", {})
    parent = input_data.get("parent")
    metadata = input_data.get("metadata", {})
    save = input_data.get("save", True)

    builder = PromptBuilder(
        name=name,
        description=description,
        metadata=metadata,
    )

    for sec in sections:
        sec_id = sec.get("id", "")
        if not sec_id:
            return error("Each section must have an 'id'", "INVALID_INPUT")

        body = sec.get("body", "")
        order = sec.get("order", 0)
        label = sec.get("label", "")
        enabled = sec.get("enabled", True)
        condition = sec.get("condition")

        if condition and isinstance(condition, dict):
            builder.add_conditional_section(
                section_id=sec_id,
                body=body,
                condition=condition,
                order=order,
                label=label,
            )
        else:
            builder.add_section(
                section_id=sec_id,
                body=body,
                order=order,
                label=label,
                enabled=enabled,
            )

    if isinstance(variables, dict):
        builder.set_variables(variables)

    if parent:
        builder.inherit_from(parent)

    # コンテキスト変数の注入
    ctx_vars = {}
    if context:
        manager = get_manager()
        ctx_vars = manager.inject_context_variables({}, context)

    template = builder.build(context_variables=ctx_vars)
    template_dict = template.to_dict()

    result_data = {
        "builder": builder.to_dict(),
        "template": template_dict,
    }

    if save:
        manager = get_manager()
        existing = manager.get_prompt_by_name(name)
        if existing:
            update_fields = {
                "body": template.body,
                "content": template.body,
                "description": description,
                "variables": template.variables,
                "metadata": template.metadata,
            }
            prompt = manager.update_prompt(name, update_fields)
        else:
            prompt_data = {
                "name": name,
                "body": template.body,
                "content": template.body,
                "description": description,
                "variables": template.variables,
                "metadata": template.metadata,
            }
            prompt = manager.create_prompt(prompt_data)
        result_data["prompt"] = prompt

    return ok(result_data)
