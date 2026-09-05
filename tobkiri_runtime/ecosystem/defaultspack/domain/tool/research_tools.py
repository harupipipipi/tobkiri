from __future__ import annotations

import re
from typing import Any

from ._agent_os_common import err, ok, read_text_file, write_text_file, workspace
from .export_tools import artifact_export
from .schema_adapter import list_or_empty


def source_extract(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    source = str(arguments.get("source") or arguments.get("path") or arguments.get("url") or "")
    if not source:
        return err("'source' is required", "INVALID_INPUT")
    try:
        if source.startswith(("http://", "https://")):
            return ok({"source": source, "network_required": True, "content": "", "title": source})
        ws = workspace(context)
        path = ws.resolve(source, must_exist=True)
        text = read_text_file(path, max_bytes=2 * 1024 * 1024)
        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else path.name
        clean = re.sub(r"<[^>]+>", " ", text)
        clean = re.sub(r"\s+", " ", clean).strip()
        return ok({"source": ws.relative(path), "title": title, "content": clean[:120_000], "length": len(clean)})
    except Exception as exc:
        return err(str(exc), "SOURCE_EXTRACT_FAILED")


def source_rank(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    query = str(arguments.get("query") or "").lower()
    sources = arguments.get("sources")
    if not isinstance(sources, list):
        sources = []
    terms = [term for term in re.split(r"\W+", query) if term]
    ranked = []
    for source in sources:
        text = str(source.get("content") if isinstance(source, dict) else source).lower()
        score = sum(text.count(term) for term in terms)
        ranked.append({"source": source, "score": score})
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ok({"query": query, "ranked_sources": ranked})


def wide_research(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    query = str(arguments.get("query") or "")
    if not query:
        return err("'query' is required", "INVALID_INPUT")
    depth = str(arguments.get("depth") or "standard")
    output_path = str(arguments.get("output_path") or "research/report.md")
    local_sources = list_or_empty(arguments.get("local_sources"))
    try:
        ws = workspace(context)
        extracted = []
        for source in local_sources[: int(arguments.get("max_sources") or 8)]:
            result = source_extract({"source": str(source)}, context)
            widget = result.get("widget") if isinstance(result, dict) else {}
            data = widget.get("data") if isinstance(widget, dict) else {}
            if data:
                extracted.append(data)
        lines = [
            f"# Research Report: {query}",
            "",
            f"- Depth: {depth}",
            f"- Sources: {len(extracted)}",
            "",
            "## Findings",
        ]
        if extracted:
            for item in extracted:
                lines.append(f"- **{item.get('title')}**: {str(item.get('content') or '')[:500]}")
        else:
            lines.append("- No external browsing was performed by this local tool run.")
        lines.extend(["", "## Confidence", "Local artifact-backed draft; verify live sources for time-sensitive claims."])
        output = ws.resolve(output_path)
        write_text_file(output, "\n".join(lines) + "\n")
        exports = []
        for fmt in arguments.get("export_formats") or []:
            exported = artifact_export({"path": ws.relative(output), "format": str(fmt)}, context)
            exports.append(exported.get("widget", {}).get("data", {}))
        return ok({"query": query, "path": ws.relative(output), "sources": extracted, "exports": exports})
    except Exception as exc:
        return err(str(exc), "WIDE_RESEARCH_FAILED")


def research_report_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or arguments.get("report_path") or "research/report.md")
    return artifact_export({**arguments, "path": path, "format": str(arguments.get("format") or "html")}, context)
