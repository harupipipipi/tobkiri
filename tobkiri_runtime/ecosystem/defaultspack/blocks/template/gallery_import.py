"""blocks.template.gallery_import — テンプレートインポートブロック。

入力:
    {
        "data": dict | str,           # エクスポート形式の dict または JSON 文字列
        "author": str (optional),
        "overwrite": bool (optional, default false),
    }

出力:
    {"status": "ok", "data": {"entry": dict}}
"""

import json


from blocks._common import ok, error
from domain.template.gallery import get_gallery


def run(input_data, context):
    """テンプレートをギャラリーにインポートする。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    raw_data = input_data.get("data")
    if raw_data is None:
        return error("'data' is required", "MISSING_PARAM")

    # JSON 文字列なら dict にパース
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            return error(f"Invalid JSON: {exc}", "INVALID_JSON")

    if not isinstance(raw_data, dict):
        return error("'data' must be a dict or JSON string", "INVALID_PARAM")

    author = input_data.get("author", "")
    overwrite = input_data.get("overwrite", False)

    gallery = get_gallery()

    try:
        entry = gallery.import_entry(
            data=raw_data,
            author=author,
            overwrite=overwrite,
        )
    except ValueError as exc:
        return error(str(exc), "ALREADY_EXISTS")

    return ok({"entry": entry.to_dict()})
