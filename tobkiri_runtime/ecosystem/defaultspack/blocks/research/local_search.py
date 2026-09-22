

from blocks._common import error, ok
from domain.coding.file_ops import FileOps


def run(input_data, context=None):
    query = input_data.get("query")
    if not query:
        return error("'query' is required", code="INVALID_INPUT")
    pattern = input_data.get("pattern", "**/*")
    ops = FileOps(input_data.get("workspace_root"))
    sources = []
    for path in ops.search_files(pattern, input_data.get("directory", "."))[: int(input_data.get("limit", 20))]:
        try:
            content = ops.read_file(path)
        except Exception:
            continue
        if str(query).lower() in content.lower():
            sources.append(
                {
                    "source_id": "local:" + path,
                    "type": "local_file",
                    "title": path,
                    "path": path,
                    "trust_level": "medium",
                    "summary": content[:500],
                }
            )
    return ok({"query": query, "sources": sources, "summary": "Matched " + str(len(sources)) + " local sources."})
