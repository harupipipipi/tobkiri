"""blocks.template.gallery_get — ギャラリーテンプレート詳細取得ブロック。

入力:
    {"id": str}

出力:
    {"status": "ok", "data": {"entry": dict}}
"""



from blocks._common import ok, error
from domain.template.gallery import get_gallery


def run(input_data, context):
    """ギャラリーテンプレートの詳細情報を返す。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    entry_id = input_data.get("id")
    if not entry_id:
        return error("'id' is required", "MISSING_PARAM")

    gallery = get_gallery()
    entry = gallery.get_entry(entry_id)
    if entry is None:
        return error(f"Template entry '{entry_id}' not found", "NOT_FOUND")

    return ok({"entry": entry.to_dict()})
