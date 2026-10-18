

from blocks._common import ok, error
from domain.chat.tool_embedding_index import ToolEmbeddingIndex
from domain.tool.registry import ToolRegistry


def run(input_data, context):
    try:
        result = ToolEmbeddingIndex().rebuild(
            ToolRegistry().list_tools(),
            model=str((input_data or {}).get("model") or "lexical"),
        )
    except Exception as exc:
        return error("tool embedding index rebuild failed: " + str(exc), "REBUILD_FAILED")
    return ok(result)
