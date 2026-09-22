"""blocks.prompt.advanced.preview — プロンプト レンダリング プレビュー API

テスト用の変数を与えてプロンプトのレンダリング結果を確認する。
保存済みプロンプト名での指定と、直接テンプレート文字列の指定の両方に対応。

入力 (パターン1: 保存済みプロンプト名):
    {
        "name":      str,      # プロンプト名
        "variables": dict,     # テスト変数
        "context_overrides": dict  # (optional) コンテキスト変数のオーバーライド
    }

入力 (パターン2: 直接テンプレート):
    {
        "template":  str,      # テンプレート文字列
        "variables": dict,     # テスト変数
        "sections":  list,     # (optional) セクション定義（ビルダー形式）
        "context_overrides": dict  # (optional)
    }

出力:
    {
        "status": "ok",
        "data": {
            "rendered":         str,     # レンダリング結果
            "template_used":    str,     # 使用されたテンプレート
            "variables_used":   dict,    # 使用された変数（コンテキスト注入後）
            "unresolved_vars":  [str],   # 未解決の変数名リスト
            "sections_active":  [str],   # (ビルダー使用時) 有効になったセクションID
        }
    }
"""


from blocks._common import ok, error
from domain.prompt.renderer import render as render_template
from domain.prompt.template import _VAR_PATTERN
from domain.prompt.manager import get_manager
from domain.prompt.builder import PromptBuilder


def run(input_data: dict, context: dict) -> dict:
    name = input_data.get("name")
    raw_template = input_data.get("template")
    sections_data = input_data.get("sections")
    variables = input_data.get("variables", {})
    context_overrides = input_data.get("context_overrides", {})

    if not name and not raw_template and not sections_data:
        return error(
            "Either 'name', 'template', or 'sections' is required",
            "INVALID_INPUT",
        )

    manager = get_manager()

    # コンテキスト変数を構築
    all_vars = dict(variables)

    # context から自動注入
    if context:
        injected = manager.inject_context_variables({}, context)
        for k, v in injected.items():
            if k not in all_vars:
                all_vars[k] = v

    # context_overrides で上書き
    if context_overrides and isinstance(context_overrides, dict):
        all_vars.update(context_overrides)

    sections_active = []
    template_body = ""

    if sections_data and isinstance(sections_data, list):
        # ビルダー形式: セクションから構築
        builder = PromptBuilder(name=name or "_preview")
        for sec in sections_data:
            sec_id = sec.get("id", "section")
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
        builder.set_variables(all_vars)
        built = builder.build(context_variables=all_vars)
        template_body = built.body
        active_meta_sections = built.metadata.get("sections", [])
        sections_active = [s.get("id", "") for s in active_meta_sections]

    elif name:
        # 保存済みプロンプトを取得
        prompt = manager.get_prompt_by_name(name)
        if prompt is None:
            return error(f"Prompt not found: {name}", "NOT_FOUND")
        template_body = prompt.get("body", prompt.get("content", ""))

        # metadata にセクション情報がある場合、ビルダーで再構築
        meta_sections = prompt.get("metadata", {}).get("sections")
        if meta_sections and isinstance(meta_sections, list):
            builder = PromptBuilder(name=name)
            for sec in meta_sections:
                sec_id = sec.get("id", "section")
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
            builder.set_variables(all_vars)
            built = builder.build(context_variables=all_vars)
            template_body = built.body
            active_meta_sections = built.metadata.get("sections", [])
            sections_active = [s.get("id", "") for s in active_meta_sections]

    elif raw_template:
        template_body = raw_template

    # レンダリング実行
    rendered = render_template(template_body, all_vars)

    # 未解決変数の検出
    unresolved = _VAR_PATTERN.findall(rendered)
    # 実際に置換されずに残っているもののみ
    truly_unresolved = [v for v in unresolved if v not in all_vars]

    return ok({
        "rendered": rendered,
        "template_used": template_body,
        "variables_used": all_vars,
        "unresolved_vars": truly_unresolved,
        "sections_active": sections_active,
    })
