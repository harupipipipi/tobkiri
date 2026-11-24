"""blocks.tool.container.settings — AI操作モード設定"""

from blocks._common import ok, error


def run(input_data, context):
    """
    PUT /api/container/settings — 設定を更新する
    GET /api/container/settings — 現在の設定を取得する

    GET の場合 input_data は空dict or None
    PUT の場合 input_data に更新したいキーを含む
    """
    from domain.tool.ai_operator import get_settings, update_settings

    if not isinstance(input_data, dict) or not input_data:
        # GET: 現在の設定を返す
        return ok(get_settings())

    # PUT: 設定を更新する
    # _method フィールドや実際のボディの中身で判断
    # input_data にキーが含まれていれば更新とみなす
    updatable_keys = ("mode", "fast_model", "heavy_model", "default_model", "max_steps", "step_delay")
    has_update = any(k in input_data for k in updatable_keys)

    if has_update:
        result = update_settings(input_data)
        return ok(result)

    # 更新キーがない場合は GET として扱う
    return ok(get_settings())
