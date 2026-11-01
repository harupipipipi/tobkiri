from __future__ import annotations

from typing import Any

from ._agent_os_common import err, ok, read_text_file, write_minimal_docx, write_text_file, workspace
from .export_tools import artifact_export


def doc_create(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    title = str(arguments.get("title") or "Document")
    content = str(arguments.get("content") or arguments.get("markdown") or "")
    output_path = str(arguments.get("output_path") or f"documents/{title.lower().replace(' ', '-')}.md")
    try:
        ws = workspace(context)
        output = ws.resolve(output_path)
        if output.suffix.lower() == ".docx":
            paragraphs = [title, *content.splitlines()]
            write_minimal_docx(output, paragraphs)
        else:
            write_text_file(output, f"# {title}\n\n{content}".strip() + "\n")
        return ok({"path": ws.relative(output), "title": title, "size": output.stat().st_size})
    except Exception as exc:
        return err(str(exc), "DOC_CREATE_FAILED")


def doc_update(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    if not path:
        return err("'path' is required", "INVALID_INPUT")
    try:
        ws = workspace(context)
        target = ws.resolve(path, must_exist=True)
        content = read_text_file(target)
        if arguments.get("replace") is True:
            updated = str(arguments.get("content") or "")
        else:
            updated = content + str(arguments.get("content") or arguments.get("append") or "")
        write_text_file(target, updated)
        return ok({"path": ws.relative(target), "size": target.stat().st_size})
    except Exception as exc:
        return err(str(exc), "DOC_UPDATE_FAILED")


def doc_to_pdf(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return artifact_export({**arguments, "format": "pdf"}, context)


def doc_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return artifact_export({**arguments, "format": str(arguments.get("format") or "docx")}, context)
