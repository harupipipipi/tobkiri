"""defaults.dev.advanced.usage — プロンプト使用状況ダッシュボードを返す handler

入力:
    {
        "prompt_name": str (任意 — 指定時はそのプロンプトの詳細),
        "conversation_id": str (任意 — 指定時はその会話で使われたプロンプト一覧),
        "render_history_limit": int (デフォルト10),
        "sync_from_inspector": bool (デフォルトfalse — Inspectorからの一括取り込み)
    }

出力:
    {"status": "ok", "data": {...}}
"""

from blocks._common import ok, error

from domain.dev.usage_tracker import UsageTracker
from domain.dev.inspector import Inspector


def run(input_data: dict, context: dict) -> dict:
    tracker = UsageTracker()

    # Inspectorからの一括同期（明示的にリクエストされた場合のみ）
    sync_requested = input_data.get("sync_from_inspector", False)
    sync_count = 0
    if sync_requested:
        inspector = Inspector()
        sync_count = tracker.sync_from_inspector(inspector)

    prompt_name = input_data.get("prompt_name")
    conversation_id = input_data.get("conversation_id")
    render_limit = input_data.get("render_history_limit", 10)
    if not isinstance(render_limit, int) or render_limit < 1:
        render_limit = 10
    if render_limit > 200:
        render_limit = 200

    # 特定プロンプトの詳細
    if prompt_name:
        stats = tracker.get_stats(prompt_name)
        if stats is None:
            stats = {
                "prompt_name": prompt_name,
                "call_count": 0,
                "last_used": "",
                "first_used": "",
            }
        render_history = tracker.get_render_history(prompt_name, limit=render_limit)
        conversations = tracker.get_conversations_for_prompt(prompt_name)
        edit_history = tracker.get_edit_history(prompt_name, limit=10)

        data = {
            "stats": stats,
            "render_history": render_history,
            "conversations": conversations,
            "edit_history": edit_history,
        }
        if sync_requested:
            data["sync_count"] = sync_count
        return ok(data)

    # 特定会話で使われたプロンプト一覧
    if conversation_id:
        prompts = tracker.get_prompts_for_conversation(conversation_id)
        prompt_details = []
        for pname in prompts:
            stats = tracker.get_stats(pname)
            prompt_details.append(stats or {
                "prompt_name": pname,
                "call_count": 0,
                "last_used": "",
                "first_used": "",
            })
        data = {
            "conversation_id": conversation_id,
            "prompts": prompt_details,
        }
        if sync_requested:
            data["sync_count"] = sync_count
        return ok(data)

    # 全体ダッシュボード
    all_stats = tracker.get_all_stats()
    conversation_map = tracker.get_conversation_map()

    data = {
        "prompt_stats": all_stats,
        "conversation_count": len(conversation_map),
        "conversation_map": conversation_map,
        "total_prompts_tracked": len(all_stats),
    }
    if sync_requested:
        data["sync_count"] = sync_count
    return ok(data)
