"""blocks.prompt.advanced.conditional — 条件付きセクション設定 API

既存プロンプトに条件付きセクションを追加・更新・削除する。

入力:
    {
        "name":   str,            # プロンプト名 (URLパスから注入)
        "action": str,            # "add" | "update" | "remove" | "list" | "evaluate"
        "section": {              # action が "add" または "update" の場合
            "id":        str,
            "body":      str,
            "order":     int,
            "label":     str,
            "condition": {
                "field":    str,
                "operator": str,
                "value":    Any
            }
        },
        "section_id": str,        # action が "remove" の場合
        "test_variables": dict    # action が "evaluate" の場合
    }

出力:
    {"status": "ok", "data": {...}}
"""


from blocks._common import ok, error
from domain.prompt.manager import get_manager
from domain.prompt.builder import evaluate_condition


def run(input_data: dict, context: dict) -> dict:
    name = input_data.get("name")
    if not name:
        return error("'name' is required", "INVALID_INPUT")

    action = input_data.get("action", "list")

    manager = get_manager()
    prompt = manager.get_prompt_by_name(name)
    if prompt is None:
        return error(f"Prompt not found: {name}", "NOT_FOUND")

    metadata = prompt.get("metadata", {})
    sections = metadata.get("sections", [])
    if not isinstance(sections, list):
        sections = []

    if action == "list":
        conditional_sections = [
            s for s in sections if s.get("condition") is not None
        ]
        return ok({
            "name": name,
            "conditional_sections": conditional_sections,
            "total_sections": len(sections),
        })

    if action == "add":
        section_data = input_data.get("section")
        if not section_data or not isinstance(section_data, dict):
            return error("'section' dict is required for 'add' action", "INVALID_INPUT")

        sec_id = section_data.get("id", "")
        if not sec_id:
            return error("Section 'id' is required", "INVALID_INPUT")

        condition = section_data.get("condition")
        if not condition or not isinstance(condition, dict):
            return error("'condition' dict is required for conditional section", "INVALID_INPUT")

        # 既存セクションIDとの重複チェック
        existing_ids = {s.get("id") for s in sections}
        if sec_id in existing_ids:
            return error(f"Section '{sec_id}' already exists. Use 'update' action.", "DUPLICATE")

        new_section = {
            "id": sec_id,
            "label": section_data.get("label", sec_id),
            "body": section_data.get("body", ""),
            "order": section_data.get("order", len(sections)),
            "enabled": True,
            "condition": condition,
        }
        sections.append(new_section)
        metadata["sections"] = sections
        manager.update_prompt(name, {"metadata": metadata})

        return ok({
            "name": name,
            "added_section": new_section,
            "total_sections": len(sections),
        })

    if action == "update":
        section_data = input_data.get("section")
        if not section_data or not isinstance(section_data, dict):
            return error("'section' dict is required for 'update' action", "INVALID_INPUT")

        sec_id = section_data.get("id", "")
        if not sec_id:
            return error("Section 'id' is required", "INVALID_INPUT")

        found = False
        for i, s in enumerate(sections):
            if s.get("id") == sec_id:
                for key in ("body", "order", "label", "condition", "enabled"):
                    if key in section_data:
                        sections[i][key] = section_data[key]
                found = True
                break

        if not found:
            return error(f"Section '{sec_id}' not found", "NOT_FOUND")

        metadata["sections"] = sections
        manager.update_prompt(name, {"metadata": metadata})

        updated_sec = next(s for s in sections if s.get("id") == sec_id)
        return ok({
            "name": name,
            "updated_section": updated_sec,
        })

    if action == "remove":
        sec_id = input_data.get("section_id", "")
        if not sec_id:
            return error("'section_id' is required for 'remove' action", "INVALID_INPUT")

        original_len = len(sections)
        sections = [s for s in sections if s.get("id") != sec_id]
        if len(sections) == original_len:
            return error(f"Section '{sec_id}' not found", "NOT_FOUND")

        metadata["sections"] = sections
        manager.update_prompt(name, {"metadata": metadata})

        return ok({
            "name": name,
            "removed_section_id": sec_id,
            "total_sections": len(sections),
        })

    if action == "evaluate":
        test_variables = input_data.get("test_variables", {})
        evaluation_results = []
        for s in sections:
            condition = s.get("condition")
            is_active = evaluate_condition(condition, test_variables)
            evaluation_results.append({
                "id": s.get("id"),
                "label": s.get("label", ""),
                "condition": condition,
                "is_active": is_active,
                "enabled": s.get("enabled", True),
                "effective": is_active and s.get("enabled", True),
            })

        return ok({
            "name": name,
            "test_variables": test_variables,
            "evaluation": evaluation_results,
        })

    return error(f"Unknown action: {action}. Must be 'add', 'update', 'remove', 'list', or 'evaluate'.", "INVALID_INPUT")
