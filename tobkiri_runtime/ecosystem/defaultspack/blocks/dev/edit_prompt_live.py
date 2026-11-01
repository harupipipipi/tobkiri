"""defaults.dev.edit_prompt_live — プロンプトをその場で編集して即時反映する handler

入力:
    {
        "prompt_name": str,       # プロンプトIDまたは "system" でシステムプロンプト
        "new_body": str           # 新しいプロンプト本文
    }

出力:
    {"status": "ok", "data": {"prompt_name": str, "updated": true, "content": str}}
"""

from blocks._common import ok, error

from domain.prompt.manager import get_manager


def run(input_data: dict, context: dict) -> dict:
    prompt_name = input_data.get("prompt_name")
    new_body = input_data.get("new_body")

    if not prompt_name:
        return error("prompt_name is required", "INVALID_INPUT")
    if new_body is None:
        return error("new_body is required", "INVALID_INPUT")

    manager = get_manager()

    # システムプロンプトの特別処理
    if prompt_name == "system":
        manager.set_system_prompt(str(new_body))
        return ok({
            "prompt_name": "system",
            "updated": True,
            "content": manager.get_system_prompt(),
        })

    # P1-3: manager.update_prompt() 公開メソッド経由で更新する
    # まず name でプロンプトを検索
    prompt = manager.get_prompt_by_name(prompt_name)

    if prompt is not None:
        # 既存プロンプトを update_prompt() 経由で更新
        updated = manager.update_prompt(prompt_name, {"content": str(new_body)})
        if updated is None:
            return error("Failed to update prompt", "INTERNAL_ERROR")
        return ok({
            "prompt_name": prompt_name,
            "updated": True,
            "content": updated.get("content", updated.get("body", "")),
            "prompt_id": updated.get("id"),
        })

    # ID で検索（後方互換）
    prompt = manager.get_prompt(prompt_name)
    if prompt is not None:
        # ID 指定の場合は name を取得して update_prompt() を使う
        name = prompt.get("name", "")
        if name:
            updated = manager.update_prompt(name, {"content": str(new_body)})
            if updated is not None:
                return ok({
                    "prompt_name": prompt_name,
                    "updated": True,
                    "content": updated.get("content", updated.get("body", "")),
                    "prompt_id": updated.get("id"),
                })

    # 存在しないなら新規作成（create_prompt も公開メソッド）
    new_prompt = manager.create_prompt({
        "name": prompt_name,
        "content": str(new_body),
        "variables": [],
    })
    return ok({
        "prompt_name": prompt_name,
        "updated": True,
        "content": new_prompt["content"],
        "prompt_id": new_prompt["id"],
        "_created": True,
    })
