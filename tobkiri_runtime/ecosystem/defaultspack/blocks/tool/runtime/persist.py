"""
blocks.tool.runtime.persist — ランタイムtoolをJSONにエクスポート（永続化）する

input_data:
  - name: str（任意）— 単一tool指定
  - names: list（任意）— 複数tool指定
  nameまたはnamesのどちらかが必須。
"""


from blocks._common import ok, error


def run(input_data, context):
    """ランタイムtoolをJSONにエクスポート（永続化）する"""
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")

    name = input_data.get("name")
    names = input_data.get("names")

    if name is None and names is None:
        return error("name or names is required", "MISSING_PARAM")

    if name is not None and names is None:
        names = [name]

    if not isinstance(names, list) or len(names) == 0:
        return error("names must be a non-empty list", "INVALID_PARAM")

    from domain.tool.runtime_creator import RuntimeToolCreator

    creator = RuntimeToolCreator()

    persisted = []
    failed = []

    for tool_name in names:
        try:
            result = creator.persist_tool(tool_name)
            persisted.append({
                "name": result["name"],
                "persisted": result["persisted"],
                "json_path": result["json_path"],
            })
        except ValueError as exc:
            failed.append({"name": tool_name, "error": str(exc)})
        except Exception as exc:
            failed.append({"name": tool_name, "error": "Persist failed: {}".format(exc)})

    if not persisted and failed:
        return error(
            "All tools failed to persist: {}".format(
                "; ".join(f["name"] + ": " + f["error"] for f in failed)
            ),
            "PERSIST_ERROR",
        )

    result_data = {
        "persisted": persisted,
        "count": len(persisted),
    }
    if failed:
        result_data["failed"] = failed

    return ok(result_data)
