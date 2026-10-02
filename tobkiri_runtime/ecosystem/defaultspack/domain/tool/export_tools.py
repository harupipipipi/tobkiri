from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from ._agent_os_common import (
    err,
    ok,
    read_csv_rows,
    read_text_file,
    write_csv_rows,
    write_minimal_docx,
    write_minimal_pptx,
    write_minimal_xlsx,
    write_simple_pdf,
    write_text_file,
    workspace,
    zip_path,
)
from .preview_tools import image_render


def _output_path(input_path: Path, fmt: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    return f"exports/{input_path.stem}.{fmt}"


def _json_to_rows(text: str) -> list[list[Any]]:
    data = json.loads(text)
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        headers = sorted({key for item in data for key in item})
        return [headers] + [[item.get(key, "") for key in headers] for item in data]
    if isinstance(data, list):
        return [[item] if not isinstance(item, list) else item for item in data]
    if isinstance(data, dict):
        return [["key", "value"]] + [[key, value] for key, value in data.items()]
    return [["value"], [data]]


def artifact_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    fmt = str(arguments.get("format") or arguments.get("output_format") or "").strip(".").lower()
    if not path:
        return err("'path' is required", "INVALID_INPUT")
    try:
        ws = workspace(context)
        source = ws.resolve(path, must_exist=True, allow_root=True)
        if source.is_dir():
            fmt = fmt or "zip"
        if not fmt:
            return err("'format' is required", "INVALID_INPUT")
        output = ws.resolve(_output_path(source, fmt, arguments.get("output_path")))
        if fmt == "zip":
            data = zip_path(source, output, root=ws.root)
        elif fmt == "html":
            content = read_text_file(source)
            if source.suffix.lower() in {".html", ".htm"}:
                html_content = content
            else:
                html_content = "<!doctype html><meta charset=\"utf-8\"><pre>{}</pre>".format(html.escape(content))
            write_text_file(output, html_content)
            data = {"path": ws.relative(output), "size": output.stat().st_size}
        elif fmt == "pdf":
            write_simple_pdf(output, read_text_file(source) if source.is_file() else source.name)
            data = {"path": ws.relative(output), "size": output.stat().st_size}
        elif fmt == "png":
            rendered = image_render({"text": read_text_file(source)[:1000], "output_path": ws.relative(output)}, context)
            return rendered
        elif fmt == "docx":
            write_minimal_docx(output, read_text_file(source).splitlines() or [source.name])
            data = {"path": ws.relative(output), "size": output.stat().st_size}
        elif fmt == "pptx":
            title = source.stem
            bullets = [line.strip("- ") for line in read_text_file(source).splitlines() if line.strip()][:8]
            write_minimal_pptx(output, [{"title": title, "bullets": bullets}])
            data = {"path": ws.relative(output), "size": output.stat().st_size}
        elif fmt == "xlsx":
            if source.suffix.lower() == ".csv":
                rows = read_csv_rows(source)
            elif source.suffix.lower() == ".json":
                rows = _json_to_rows(read_text_file(source))
            else:
                rows = [[line] for line in read_text_file(source).splitlines()]
            write_minimal_xlsx(output, rows)
            data = {"path": ws.relative(output), "size": output.stat().st_size}
        elif fmt == "csv":
            if source.suffix.lower() == ".json":
                rows = _json_to_rows(read_text_file(source))
            else:
                rows = [[line] for line in read_text_file(source).splitlines()]
            write_csv_rows(output, rows)
            data = {"path": ws.relative(output), "size": output.stat().st_size}
        else:
            return err("unsupported export format: " + fmt, "UNSUPPORTED_FORMAT")
        data["format"] = fmt
        data["source_path"] = ws.relative(source) if source != ws.root else "."
        return ok(data)
    except Exception as exc:
        return err(str(exc), "EXPORT_FAILED")


def pdf_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return artifact_export({**arguments, "format": "pdf"}, context)


def doc_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return artifact_export({**arguments, "format": str(arguments.get("format") or "docx")}, context)


def slides_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return artifact_export({**arguments, "format": str(arguments.get("format") or "pptx")}, context)


def sheet_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return artifact_export({**arguments, "format": str(arguments.get("format") or "xlsx")}, context)
