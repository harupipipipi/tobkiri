"""blocks.prompt.advanced.version — バージョン CRUD API

HTTP メソッドに応じた処理を行う:
    GET  /api/prompts/{name}/versions          → バージョン一覧
    POST /api/prompts/{name}/versions          → 新規バージョン作成
    PUT  /api/prompts/{name}/versions/{version} → バージョン切替

入力 (POST):
    {
        "name":    str,      # URLパスから注入
        "label":   str,      # (optional) バージョンラベル
        "summary": str       # (optional) バージョン説明
    }

入力 (PUT):
    {
        "name":    str,      # URLパスから注入
        "version": str|int   # URLパスから注入
    }

出力:
    {"status": "ok", "data": {...}}
"""


from blocks._common import ok, error
from domain.prompt.versioning import get_version_manager


def run(input_data: dict, context: dict) -> dict:
    name = input_data.get("name")
    if not name:
        return error("'name' is required", "INVALID_INPUT")

    # HTTP メソッドの判定: context に _http_method がある場合はそれを使用
    # なければ input_data のフィールドから推定する
    http_method = ""
    if context:
        http_method = context.get("_http_method", "")
    if not http_method:
        http_method = input_data.get("_http_method", "")

    # version パスパラメータがある場合は PUT (切替)
    version_param = input_data.get("version")

    vm = get_version_manager()

    # PUT: バージョン切替
    if http_method.upper() == "PUT" or version_param is not None:
        try:
            version_num = int(version_param)
        except (TypeError, ValueError):
            return error("'version' must be an integer", "INVALID_INPUT")

        try:
            result = vm.switch_version(name, version_num)
        except ValueError as e:
            return error(str(e), "NOT_FOUND")

        return ok({
            "action": "switched",
            "name": name,
            "version": version_num,
            "prompt": result,
        })

    # POST: 新規バージョン作成
    if http_method.upper() == "POST":
        label = input_data.get("label", "")
        summary = input_data.get("summary", "")

        try:
            result = vm.create_version(name, label=label, summary=summary)
        except ValueError as e:
            return error(str(e), "NOT_FOUND")

        return ok({
            "action": "created",
            "name": name,
            "version_info": {
                "version": result["version"],
                "label": result["label"],
                "created_at": result["created_at"],
                "summary": result["summary"],
            },
            "prompt": result.get("prompt"),
        })

    # GET: バージョン一覧 (デフォルト)
    result = vm.list_versions(name)
    return ok({
        "action": "list",
        "name": result["name"],
        "active_version": result["active_version"],
        "versions": result["versions"],
        "total": len(result["versions"]),
    })
