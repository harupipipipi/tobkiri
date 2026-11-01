"""
blocks.tool.runtime.list — ランタイム登録済みtool一覧を返す

input_data:
  - tags: list（任意）— タグでフィルタ
"""


from blocks._common import ok, error


def run(input_data, context):
    """ランタイムtool一覧を返す"""
    if not isinstance(input_data, dict):
        input_data = {}

    tags = input_data.get("tags")
    if tags is not None and not isinstance(tags, list):
        return error("tags must be a list", "INVALID_PARAM")

    from domain.tool.runtime_creator import RuntimeToolCreator

    creator = RuntimeToolCreator()
    tools = creator.list_runtime_tools(tags=tags)

    return ok({
        "tools": tools,
        "count": len(tools),
    })
