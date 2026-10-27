"""defaults.dev.advanced.rollback — プロンプト編集のロールバック handler

入力:
    {
        "prompt_name": str,         # ロールバック対象のプロンプト名 or "system"
        "edit_id": str (任意),      # 特定の編集IDにロールバック。省略時は直前の編集をロールバック
        "steps": int (任意)         # N件前にロールバック（edit_id未指定時、デフォルト1）
    }

出力:
    {"status": "ok", "data": {"prompt_name": str, "rolled_back": true,
                               "restored_content": str, "from_edit_id": str}}
"""

from blocks._common import ok, error

from domain.prompt.manager import get_manager
from domain.dev.usage_tracker import UsageTracker


def _restore_body(manager, prompt_name: str, body: str) -> bool:
    """プロンプトの本文を指定内容に復元する。

    Returns:
        成功時 True、失敗時 False
    """
    if prompt_name == "system":
        manager.set_system_prompt(str(body))
        return True

    # 名前で検索して更新
    prompt = manager.get_prompt_by_name(prompt_name)
    if prompt is not None:
        updated = manager.update_prompt(prompt_name, {"content": str(body)})
        return updated is not None

    # IDで検索して更新
    prompt = manager.get_prompt(prompt_name)
    if prompt is not None:
        name = prompt.get("name", "")
        if name:
            updated = manager.update_prompt(name, {"content": str(body)})
            return updated is not None

    return False


def run(input_data: dict, context: dict) -> dict:
    prompt_name = input_data.get("prompt_name")
    edit_id = input_data.get("edit_id")
    steps = input_data.get("steps", 1)

    if not prompt_name:
        return error("prompt_name is required", "INVALID_INPUT")

    if not isinstance(steps, int) or steps < 1:
        steps = 1

    tracker = UsageTracker()
    manager = get_manager()

    # edit_id 指定時: その編集の body_before に戻す
    if edit_id:
        entry = tracker.get_edit_by_id(prompt_name, edit_id)
        if entry is None:
            return error(
                f"Edit not found: {edit_id} for prompt '{prompt_name}'",
                "NOT_FOUND",
            )
        target_body = entry["body_before"]

        # 現在の本文を取得してロールバック自体も編集履歴に記録
        current_body = ""
        if prompt_name == "system":
            current_body = manager.get_system_prompt()
        else:
            prompt = manager.get_prompt_by_name(prompt_name)
            if prompt is not None:
                current_body = prompt.get("body", prompt.get("content", ""))

        success = _restore_body(manager, prompt_name, target_body)
        if not success:
            return error(
                f"Failed to restore prompt '{prompt_name}'",
                "INTERNAL_ERROR",
            )

        # ロールバック自体を編集履歴として記録
        tracker.record_edit(
            prompt_name=prompt_name,
            body_before=current_body,
            body_after=target_body,
        )

        return ok({
            "prompt_name": prompt_name,
            "rolled_back": True,
            "restored_content": target_body,
            "from_edit_id": edit_id,
            "method": "by_edit_id",
        })

    # edit_id 未指定時: steps件前にロールバック
    edit_history = tracker.get_edit_history(prompt_name, limit=steps + 10)
    if not edit_history:
        return error(
            f"No edit history found for prompt '{prompt_name}'",
            "NOT_FOUND",
        )

    # edit_history は新しい順。steps=1なら最新の1件の body_before に戻す
    # steps=2なら2件前の body_before に戻す
    if steps > len(edit_history):
        # 履歴を遡れる限り遡る（最も古い編集の body_before）
        target_entry = edit_history[-1]
    else:
        target_entry = edit_history[steps - 1]

    target_body = target_entry["body_before"]
    target_edit_id = target_entry["edit_id"]

    # 現在の本文を取得
    current_body = ""
    if prompt_name == "system":
        current_body = manager.get_system_prompt()
    else:
        prompt = manager.get_prompt_by_name(prompt_name)
        if prompt is not None:
            current_body = prompt.get("body", prompt.get("content", ""))

    success = _restore_body(manager, prompt_name, target_body)
    if not success:
        return error(
            f"Failed to restore prompt '{prompt_name}'",
            "INTERNAL_ERROR",
        )

    # ロールバック自体を編集履歴として記録
    tracker.record_edit(
        prompt_name=prompt_name,
        body_before=current_body,
        body_after=target_body,
    )

    return ok({
        "prompt_name": prompt_name,
        "rolled_back": True,
        "restored_content": target_body,
        "from_edit_id": target_edit_id,
        "steps_back": steps,
        "method": "by_steps",
    })
