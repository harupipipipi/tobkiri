"""defaults.coding.git_diff — Git差分取得ブロック（スタブ）"""

from blocks._common import ok, error
from domain.coding.contract_adapter import GIT_READ, invoke_coding_contract, workspace_id


def run(input_data, context=None):
    """Git差分を返す（スタブ）。

    input_data:
        ref (str|null, optional): 比較先リファレンス

    returns:
        {"status":"ok","data":{"diff":str,"files_changed":int}}
    """
    ref = input_data.get("ref")

    try:
        selected_workspace_id = workspace_id(input_data)
        result = invoke_coding_contract(
            GIT_READ,
            "diff",
            {"workspace_id": selected_workspace_id, "ref": ref},
        )
        diff = str(result.get("output") or "")
        return ok(
            {
                "diff": diff,
                "files_changed": sum(
                    1 for line in diff.splitlines() if line.startswith("diff --git ")
                ),
                "workspace_id": selected_workspace_id,
                "repository_root": result.get("repository_root"),
            }
        )
    except Exception as e:
        return error(str(e), code="GIT_ERROR")
