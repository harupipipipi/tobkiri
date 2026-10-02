"""defaults.dev.advanced.profile — パフォーマンスプロファイル結果を返す handler

入力:
    {
        "view": str (デフォルト "summary" — "summary"|"blocks"|"api"|"memory"|"full"),
        "block_name": str (任意 — viewが"blocks"の時のフィルタ),
        "model": str (任意 — viewが"api"の時のフィルタ),
        "limit": int (デフォルト50),
        "action": str (任意 — "snapshot"でメモリスナップショット取得, "enable"/"disable"で切替,
                        "clear"で全データクリア)
    }

出力:
    {"status": "ok", "data": {...}}
"""

from blocks._common import ok, error

from domain.dev.profiler import Profiler


def run(input_data: dict, context: dict) -> dict:
    profiler = Profiler()

    action = input_data.get("action")
    view = input_data.get("view", "summary")
    limit = input_data.get("limit", 50)
    block_name = input_data.get("block_name")
    model = input_data.get("model")

    if not isinstance(limit, int) or limit < 1:
        limit = 50
    if limit > 500:
        limit = 500

    # アクション処理
    if action == "snapshot":
        label = input_data.get("label", "manual")
        snapshot = profiler.snapshot_memory(label=label)
        return ok({
            "action": "snapshot",
            "snapshot": snapshot,
        })

    if action == "enable":
        profiler.enable()
        return ok({"action": "enable", "enabled": True})

    if action == "disable":
        profiler.disable()
        return ok({"action": "disable", "enabled": False})

    if action == "clear":
        profiler.clear()
        return ok({"action": "clear", "cleared": True})

    # ビュー: summary
    if view == "summary":
        return ok({
            "view": "summary",
            "enabled": profiler.is_enabled,
            "block_summary": profiler.get_block_summary(),
            "api_summary": profiler.get_api_summary(),
        })

    # ビュー: blocks
    if view == "blocks":
        timings = profiler.get_block_timings(limit=limit, block_name=block_name)
        return ok({
            "view": "blocks",
            "block_name_filter": block_name,
            "timings": timings,
            "count": len(timings),
        })

    # ビュー: api
    if view == "api":
        timings = profiler.get_api_timings(limit=limit, model=model)
        return ok({
            "view": "api",
            "model_filter": model,
            "timings": timings,
            "count": len(timings),
        })

    # ビュー: memory
    if view == "memory":
        # 自動的にスナップショットも取る
        profiler.snapshot_memory(label="auto_view")
        snapshots = profiler.get_memory_snapshots(limit=limit)
        return ok({
            "view": "memory",
            "snapshots": snapshots,
            "count": len(snapshots),
        })

    # ビュー: full
    if view == "full":
        profiler.snapshot_memory(label="full_report")
        report = profiler.get_full_report()
        report["view"] = "full"
        report["recent_block_timings"] = profiler.get_block_timings(limit=20)
        report["recent_api_timings"] = profiler.get_api_timings(limit=20)
        report["memory_snapshots"] = profiler.get_memory_snapshots(limit=20)
        return ok(report)

    return error(f"Unknown view: {view}. Use: summary, blocks, api, memory, full", "INVALID_INPUT")
