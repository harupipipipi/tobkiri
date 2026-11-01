

from blocks._common import error, ok


def run(input_data, context=None):
    query = input_data.get("query", "")
    sources = input_data.get("sources", [])
    if not isinstance(sources, list):
        return error("'sources' must be a list", code="INVALID_INPUT")
    lines = ["# Research Report", "", "Query: " + str(query), "", "## Sources"]
    for source in sources:
        if isinstance(source, dict):
            lines.append("- " + str(source.get("title") or source.get("path") or source.get("source_id")))
    lines.extend(["", "## Summary", str(input_data.get("summary", ""))])
    return ok({"type": "markdown", "content": "\n".join(lines), "source_count": len(sources)})
