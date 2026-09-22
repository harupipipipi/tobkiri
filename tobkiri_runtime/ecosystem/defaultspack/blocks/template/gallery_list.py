"""blocks.template.gallery_list — ギャラリー一覧ブロック。

入力:
    {
        "source_type": str (optional — "tool", "prompt", "unified"),
        "tag": str (optional),
        "query": str (optional — テキスト検索),
    }

出力:
    {"status": "ok", "data": {"templates": [...], "count": int}}
"""



from blocks._common import ok, error
from domain.template.gallery import get_gallery


def run(input_data, context):
    """ギャラリーテンプレート一覧を返す。"""
    if not isinstance(input_data, dict):
        input_data = {}

    source_type = input_data.get("source_type")
    tag = input_data.get("tag")
    query = input_data.get("query")

    gallery = get_gallery()
    entries = gallery.list_entries(
        source_type=source_type,
        tag=tag,
        query=query,
    )

    return ok({
        "templates": entries,
        "count": len(entries),
    })
