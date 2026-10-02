"""defaults.dev.advanced.live_edit — リアルタイムプロンプト編集（履歴付き）handler

入力:
    {
        "prompt_name": str,       # プロンプト名 or "system"
        "new_body": str,          # 新しいプロンプト本文
        "variables": dict (任意)  # レンダリング確認用の変数
    }

出力:
    {"status": "ok", "data": {"prompt_name": str, "updated": true, "content": str,
                               "edit_id": str, "previous_content": str}}

edit_prompt_live との違い:
    - 編集前の状態を UsageTracker に保存（ロールバック可能）
    - 編集後のレンダリング結果をプレビューとして返す
"""

from blocks._common import ok, error

from domain.prompt.manager import get_manager
from domain.dev.usage_tracker import UsageTracker


def _get_current_body(manager, prompt_name: str) -> str:
    """プロンプトの現在の本文を取得する。"""
    if prompt_name == "system":
        return manager.get_system_prompt()

    prompt = manager.get_prompt_by_name(prompt_name)
    if prompt is not None:
        return prompt.get("body", prompt.get("content", ""))

    prompt = manager.get_prompt(prompt_name)
    if prompt is not None:
        return prompt.get("body", prompt.get("content", ""))

    return ""


def _apply_edit(manager, prompt_name: str, new_body: str) -> dict:
    """プロンプトに編集を適用し、結果を返す。"""
    if prompt_name == "system":
        manager.set_system_prompt(str(new_body))
        return {
            "prompt_name": "system",
            "updated": True,
            "content": manager.get_system_prompt(),
            "prompt_id": None,
            "created": False,
        }

    # 名前で検索
    prompt = manager.get_prompt_by_name(prompt_name)
    if prompt is not None:
        updated = manager.update_prompt(prompt_name, {"content": str(new_body)})
        if updated is None:
            return {"updated": False}
        return {
            "prompt_name": prompt_name,
            "updated": True,
            "content": updated.get("content", updated.get("body", "")),
            "prompt_id": updated.get("id"),
            "created": False,
        }

    # ID で検索
    prompt = manager.get_prompt(prompt_name)
    if prompt is not None:
        name = prompt.get("name", "")
        if name:
            updated = manager.update_prompt(name, {"content": str(new_body)})
            if updated is not None:
                return {
                    "prompt_name": prompt_name,
                    "updated": True,
                    "content": updated.get("content", updated.get("body", "")),
                    "prompt_id": updated.get("id"),
                    "created": False,
                }

    # 新規作成
    new_prompt = manager.create_prompt({
        "name": prompt_name,
        "content": str(new_body),
        "variables": [],
    })
    return {
        "prompt_name": prompt_name,
        "updated": True,
        "content": new_prompt["content"],
        "prompt_id": new_prompt["id"],
        "created": True,
    }


def _render_preview(body: str, variables: dict) -> str:
    """変数を適用したプレビュー文字列を生成する。

    {{var_name}} を variables dict の値で置換する。
    未定義の変数はそのまま残す。
    """
    import re
    def replacer(match):
        var_name = match.group(1).strip()
        if var_name in variables:
            return str(variables[var_name])
        return match.group(0)
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", replacer, body)


def run(input_data: dict, context: dict) -> dict:
    prompt_name = input_data.get("prompt_name")
    new_body = input_data.get("new_body")
    variables = input_data.get("variables") or {}

    if not prompt_name:
        return error("prompt_name is required", "INVALID_INPUT")
    if new_body is None:
        return error("new_body is required", "INVALID_INPUT")

    manager = get_manager()
    tracker = UsageTracker()

    # 編集前の本文を取得
    body_before = _get_current_body(manager, prompt_name)

    # 編集を適用
    result = _apply_edit(manager, prompt_name, new_body)
    if not result.get("updated", False):
        return error("Failed to update prompt", "INTERNAL_ERROR")

    # 編集履歴を記録
    edit_entry = tracker.record_edit(
        prompt_name=prompt_name,
        body_before=body_before,
        body_after=str(new_body),
    )

    # レンダリングプレビュー
    preview = _render_preview(str(new_body), variables)

    return ok({
        "prompt_name": result["prompt_name"],
        "updated": True,
        "content": result["content"],
        "prompt_id": result.get("prompt_id"),
        "edit_id": edit_entry["edit_id"],
        "previous_content": body_before,
        "preview": preview,
        "_created": result.get("created", False),
    })
