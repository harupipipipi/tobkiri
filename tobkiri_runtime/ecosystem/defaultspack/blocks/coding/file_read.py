"""defaults.coding.file_read — ファイル読み取りブロック"""

from blocks._common import ok, error
from domain.coding.contract_adapter import FILE_INSPECT, invoke_coding_contract, workspace_id


def _output_budget(input_data):
    candidates = []
    for key, ratio in (
        ("max_chars", 1),
        ("max_output_chars", 1),
        ("max_tokens", 4),
        ("max_output_tokens", 4),
    ):
        value = input_data.get(key)
        if value is None or value == "":
            continue
        try:
            parsed = int(value)
        except Exception:
            return None, "'{}' must be an integer".format(key)
        if parsed <= 0:
            return None, "'{}' must be > 0".format(key)
        candidates.append(parsed * ratio)
    if not candidates:
        return None, None
    return max(200, min(min(candidates), 120_000)), None


def _clip_content(content, budget):
    text = str(content or "")
    if budget is None or len(text) <= budget:
        return text, False, 0
    clipped = text[: max(0, budget - 28)].rstrip() + "\n[truncated]"
    return clipped, True, len(text) - len(clipped)


def run(input_data, context=None):
    """ファイルを読み取って内容を返す。

    input_data:
        path (str): 読み取るファイルのパス
        start_line (int, optional): 1-based inclusive start line
        end_line (int, optional): 1-based inclusive end line

    returns:
        {"status":"ok","data":{"path":str,"content":str,"size":int,"encoding":"utf-8"}}
    """
    path = input_data.get("path")
    if not path:
        return error("'path' is required", code="INVALID_INPUT")
    start_line = input_data.get("start_line")
    end_line = input_data.get("end_line")
    if start_line is not None:
        try:
            start_line = int(start_line)
        except Exception:
            return error("'start_line' must be an integer", code="INVALID_INPUT")
        if start_line < 1:
            return error("'start_line' must be >= 1", code="INVALID_INPUT")
    if end_line is not None:
        try:
            end_line = int(end_line)
        except Exception:
            return error("'end_line' must be an integer", code="INVALID_INPUT")
        if end_line < 1:
            return error("'end_line' must be >= 1", code="INVALID_INPUT")
    if start_line is not None and end_line is not None and end_line < start_line:
        return error("'end_line' must be >= 'start_line'", code="INVALID_INPUT")
    max_chars, budget_error = _output_budget(input_data)
    if budget_error:
        return error(budget_error, code="INVALID_INPUT")

    try:
        selected_workspace_id = workspace_id(input_data)
        payload = invoke_coding_contract(
            FILE_INSPECT,
            "read",
            {
                "workspace_id": selected_workspace_id,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "max_bytes": 4 * 1024 * 1024,
                "encoding": "utf-8",
            },
        )
        original_content = str(payload.get("content") or "")
        clipped, clipped_for_budget, omitted_chars = _clip_content(original_content, max_chars)
        if clipped_for_budget:
            payload["content"] = clipped
            payload["truncated"] = True
            payload["omitted_chars"] = omitted_chars
            payload["original_size"] = len(original_content.encode("utf-8"))
            payload["returned_size"] = len(clipped.encode("utf-8"))
            payload["max_chars"] = max_chars
            payload["summary"] = "Read {} with compacted output ({} of {} chars).".format(
                path,
                len(clipped),
                len(original_content),
            )
        payload["workspace_id"] = selected_workspace_id
        return ok(payload)
    except FileNotFoundError as e:
        return error(str(e), code="FILE_NOT_FOUND")
    except PermissionError as e:
        return error(str(e), code="PATH_RESTRICTED")
    except ValueError as e:
        return error(str(e), code="PATH_TRAVERSAL")
    except Exception as e:
        return error(str(e), code="READ_ERROR")
