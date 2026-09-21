"""defaults.coding.file_search — ファイル検索ブロック"""

from blocks._common import ok, error
from domain.coding.contract_adapter import FILE_INSPECT, invoke_coding_contract, workspace_id


def run(input_data, context=None):
    """globパターンでファイルを検索する。

    input_data:
        pattern (str): 検索パターン（glob形式）
        directory (str, optional): 検索ディレクトリ（デフォルト: "."）

    returns:
        {"status":"ok","data":{"pattern":str,"matches":[str]}}
    """
    pattern = input_data.get("pattern")
    if not pattern:
        return error("'pattern' is required", code="INVALID_INPUT")

    directory = input_data.get("directory", ".")

    try:
        selected_workspace_id = workspace_id(input_data)
        result = invoke_coding_contract(
            FILE_INSPECT,
            "search",
            {
                "workspace_id": selected_workspace_id,
                "pattern": pattern,
                "directory": directory,
            },
        )
        result["workspace_id"] = selected_workspace_id
        return ok(result)
    except NotADirectoryError as e:
        return error(str(e), code="DIR_NOT_FOUND")
    except PermissionError as e:
        return error(str(e), code="PATH_RESTRICTED")
    except ValueError as e:
        return error(str(e), code="PATH_TRAVERSAL")
    except Exception as e:
        return error(str(e), code="SEARCH_ERROR")
