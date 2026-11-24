"""blocks.prompt.advanced.inherit — プロンプト継承 API

ベースプロンプトを継承して派生プロンプトを作成する。

入力:
    {
        "name":        str,        # 派生プロンプトの名前 (URLパスから注入)
        "parent":      str,        # (optional) 親プロンプト名。指定時は新規継承設定
        "overrides": [             # (optional) オーバーライドするセクション
            {
                "id":    str,      # セクションID（親のセクションIDと一致させる）
                "body":  str,      # 上書き本文
                "order": int,      # (optional)
                "label": str       # (optional)
            },
            ...
        ],
        "additional_sections": [   # (optional) 追加セクション
            {
                "id":        str,
                "body":      str,
                "order":     int,
                "label":     str,
                "condition": dict|null
            },
            ...
        ],
        "variables": dict,         # (optional) 変数
        "description": str,        # (optional)
        "save": bool               # (optional, default=True)
    }

出力:
    {
        "status": "ok",
        "data": {
            "prompt":      {...},     # 作成されたプロンプト
            "parent_name": str,
            "template":    {...},
            "inheritance_info": {
                "parent_sections":  [...],
                "child_overrides":  [...],
                "merged_sections":  [...]
            }
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

    parent_name = input_data.get("parent", "")
    if not parent_name:
        # 既存プロンプトの継承情報を返す
        manager = get_manager()
        prompt = manager.get_prompt_by_name(name)
        if prompt is None:
            return error(f"Prompt not found: {name}", "NOT_FOUND")
        meta = prompt.get("metadata", {})
        parent = meta.get("parent", "")
        sections = meta.get("sections", [])
        return ok({
            "name": name,
            "parent_name": parent,
            "sections": sections,
            "has_parent": bool(parent),
        })

    # 親の存在確認
    manager = get_manager()
    parent_prompt = manager.get_prompt_by_name(parent_name)
    if parent_prompt is None:
        return error(f"Parent prompt not found: {parent_name}", "NOT_FOUND")

    description = input_data.get("description", "")
    variables = input_data.get("variables", {})
    overrides = input_data.get("overrides", [])
    additional_sections = input_data.get("additional_sections", [])
    save = input_data.get("save", True)

    builder = PromptBuilder(
        name=name,
        description=description,
        metadata={"parent": parent_name},
    )
    builder.inherit_from(parent_name)

    # オーバーライドセクションを追加
    for override in overrides:
        sec_id = override.get("id", "")
        if not sec_id:
            continue
        builder.add_section(
            section_id=sec_id,
            body=override.get("body", ""),
            order=override.get("order", 0),
            label=override.get("label", ""),
            enabled=override.get("enabled", True),
        )

    # 追加セクションを追加
    for sec in additional_sections:
        sec_id = sec.get("id", "")
        if not sec_id:
            continue
        condition = sec.get("condition")
        if condition and isinstance(condition, dict):
            builder.add_conditional_section(
                section_id=sec_id,
                body=sec.get("body", ""),
                condition=condition,
                order=sec.get("order", 0),
                label=sec.get("label", ""),
            )
        else:
            builder.add_section(
                section_id=sec_id,
                body=sec.get("body", ""),
                order=sec.get("order", 0),
                label=sec.get("label", ""),
                enabled=sec.get("enabled", True),
            )

    if isinstance(variables, dict):
        builder.set_variables(variables)

    # コンテキスト変数注入
    ctx_vars = {}
    if context:
        ctx_vars = manager.inject_context_variables({}, context)

    template = builder.build(context_variables=ctx_vars)
    template_dict = template.to_dict()

    # 継承情報の収集
    parent_meta = parent_prompt.get("metadata", {})
    parent_sections = parent_meta.get("sections", [])
    if not parent_sections:
        parent_body = parent_prompt.get("body", parent_prompt.get("content", ""))
        parent_sections = [{
            "id": "main",
            "label": "main",
            "body": parent_body,
            "order": 0,
            "enabled": True,
            "condition": None,
        }]

    override_ids = [o.get("id") for o in overrides if o.get("id")]
    merged_sections = template_dict.get("metadata", {}).get("sections", [])

    result_data = {
        "parent_name": parent_name,
        "template": template_dict,
        "inheritance_info": {
            "parent_sections": parent_sections,
            "child_overrides": override_ids,
            "merged_sections": merged_sections,
        },
    }

    if save:
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
