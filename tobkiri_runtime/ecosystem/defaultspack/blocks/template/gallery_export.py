"""blocks.template.gallery_export — テンプレートエクスポートブロック。

入力:
    {"id": str}

出力:
    {"status": "ok", "data": {"export": dict}}
"""



from blocks._common import ok, error
from domain.template.gallery import get_gallery


def run(input_data, context):
    """ギャラリーテンプレートを JSON 共有形式でエクスポートする。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    entry_id = input_data.get("id")
    if not entry_id:
        return error("'id' is required", "MISSING_PARAM")

    gallery = get_gallery()
    export_data = gallery.export_entry(entry_id)
    if export_data is None:
        return error(f"Template entry '{entry_id}' not found", "NOT_FOUND")

    return ok({"export": export_data})
