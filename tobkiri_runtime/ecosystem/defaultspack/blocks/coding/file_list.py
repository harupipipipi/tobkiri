"""defaults.coding.file_list — ファイル一覧ブロック"""

from blocks._common import ok, error
from domain.coding.contract_adapter import FILE_INSPECT, invoke_coding_contract, workspace_id


def run(input_data, context=None):
    """ディレクトリ内のファイル一覧を返す。

    input_data:
        directory (str, optional): 対象ディレクトリ（デフォルト: "."）
        recursive (bool, optional): 再帰的に取得するか（デフォルト: false）

    returns:
        {"status":"ok","data":{"directory":str,"files":[{"name":str,"path":str,"is_dir":bool,"size":int}]}}
    """
    directory = input_data.get("directory", ".")
    recursive = input_data.get("recursive", False)

    try:
        selected_workspace_id = workspace_id(input_data)
        result = invoke_coding_contract(
            FILE_INSPECT,
            "list",
            {
                "workspace_id": selected_workspace_id,
                "directory": directory,
                "recursive": bool(recursive),
            },
        )
        return ok({
            "directory": directory,
            "files": list(result.get("items") or []),
            "workspace_id": selected_workspace_id,
        })
    except NotADirectoryError as e:
        return error(str(e), code="DIR_NOT_FOUND")
    except PermissionError as e:
        return error(str(e), code="PATH_RESTRICTED")
    except ValueError as e:
        return error(str(e), code="PATH_TRAVERSAL")
    except Exception as e:
        return error(str(e), code="LIST_ERROR")
