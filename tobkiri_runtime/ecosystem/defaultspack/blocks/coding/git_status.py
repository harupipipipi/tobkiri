"""defaults.coding.git_status — Gitステータス取得ブロック（スタブ）"""

from blocks._common import ok, error
from domain.coding.contract_adapter import GIT_READ, invoke_coding_contract, workspace_id


def _legacy_status(output):
    branch = ""
    staged = []
    modified = []
    untracked = []
    for line in str(output or "").splitlines():
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ").strip()
        elif line.startswith("? "):
            untracked.append(line[2:].strip())
        elif line.startswith(("1 ", "2 ")):
            fields = line.split()
            if len(fields) < 9:
                continue
            status = fields[1]
            path = fields[-1]
            if status[0] != ".":
                staged.append(path)
            if len(status) > 1 and status[1] != ".":
                modified.append(path)
    return {
        "branch": branch,
        "clean": not (staged or modified or untracked),
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
    }


def run(input_data, context=None):
    """Gitリポジトリのステータスを返す（スタブ）。

    input_data:
        {} (パラメータなし)

    returns:
        {"status":"ok","data":{"branch":str,"clean":bool,"staged":[str],"modified":[str],"untracked":[str]}}
    """
    try:
        selected_workspace_id = workspace_id(input_data)
        result = invoke_coding_contract(
            GIT_READ,
            "status",
            {"workspace_id": selected_workspace_id},
        )
        projected = _legacy_status(result.get("output"))
        projected["workspace_id"] = selected_workspace_id
        projected["repository_root"] = result.get("repository_root")
        return ok(projected)
    except Exception as e:
        return error(str(e), code="GIT_ERROR")
