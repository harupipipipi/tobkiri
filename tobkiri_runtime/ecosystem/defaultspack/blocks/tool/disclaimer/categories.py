"""
blocks/tool/disclaimer/categories.py — 免責カテゴリの CRUD。

HTTP メソッドに応じて動作を分岐する:
  GET    — 一覧取得
  POST   — 新規作成
  PUT    — 更新（name をパスパラメータから取得）
  DELETE — 削除（name をパスパラメータから取得）

input_data:
  GET:    （なし、またはフィルタ条件）
  POST:   name, label, keywords, disclaimer
  PUT:    name（パスから）, label?, keywords?, disclaimer?
  DELETE: name（パスから）

戻り値 (ok):
  GET:    {"categories": [...]}
  POST:   {category_dict}
  PUT:    {category_dict}
  DELETE: {"deleted": true, "name": str}
"""


from blocks._common import ok, error
from domain.tool.disclaimer_manager import DisclaimerManager


def run(input_data, context):
    """免責カテゴリの CRUD 操作を行う。"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    method = input_data.get("_method", "GET").upper()
    manager = DisclaimerManager()

    if method == "GET":
        return _handle_list(manager)
    elif method == "POST":
        return _handle_create(input_data, manager)
    elif method == "PUT":
        return _handle_update(input_data, manager)
    elif method == "DELETE":
        return _handle_delete(input_data, manager)
    else:
        return error("Unsupported method: {}".format(method), "METHOD_NOT_ALLOWED")


def _handle_list(manager):
    """全カテゴリを返す。"""
    categories = manager.list_categories()
    return ok({"categories": categories})


def _handle_create(input_data, manager):
    """新規カテゴリを作成する。"""
    name = input_data.get("name")
    if not name:
        return error("name is required", "MISSING_PARAM")
    if not isinstance(name, str):
        return error("name must be a string", "INVALID_PARAM")

    label = input_data.get("label", "")
    keywords = input_data.get("keywords", [])
    disclaimer = input_data.get("disclaimer", "")

    if not isinstance(keywords, list):
        return error("keywords must be a list of strings", "INVALID_PARAM")
    if not isinstance(disclaimer, str):
        return error("disclaimer must be a string", "INVALID_PARAM")

    result = manager.create_category(
        name=name,
        label=label,
        keywords=keywords,
        disclaimer=disclaimer,
    )
    if result is None:
        return error(
            "Category '{}' already exists".format(name),
            "ALREADY_EXISTS",
        )
    return ok(result)


def _handle_update(input_data, manager):
    """カテゴリを更新する。"""
    name = input_data.get("name")
    if not name:
        return error("name is required (from path parameter)", "MISSING_PARAM")
    if not isinstance(name, str):
        return error("name must be a string", "INVALID_PARAM")

    label = input_data.get("label")
    keywords = input_data.get("keywords")
    disclaimer = input_data.get("disclaimer")

    if keywords is not None and not isinstance(keywords, list):
        return error("keywords must be a list of strings", "INVALID_PARAM")
    if disclaimer is not None and not isinstance(disclaimer, str):
        return error("disclaimer must be a string", "INVALID_PARAM")

    result = manager.update_category(
        name=name,
        label=label,
        keywords=keywords,
        disclaimer=disclaimer,
    )
    if result is None:
        return error(
            "Category '{}' not found".format(name),
            "NOT_FOUND",
        )
    return ok(result)


def _handle_delete(input_data, manager):
    """カテゴリを削除する。"""
    name = input_data.get("name")
    if not name:
        return error("name is required (from path parameter)", "MISSING_PARAM")
    if not isinstance(name, str):
        return error("name must be a string", "INVALID_PARAM")

    deleted = manager.delete_category(name)
    if not deleted:
        return error(
            "Category '{}' not found".format(name),
            "NOT_FOUND",
        )
    return ok({"deleted": True, "name": name})
