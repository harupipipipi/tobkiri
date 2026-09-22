from __future__ import annotations

import json
from typing import Any

from ._agent_os_common import err, ok, write_text_file, workspace
from .artifact_tools import artifact_zip
from .preview_tools import html_preview
from .sandbox_tools import sandbox_exec


def project_scaffold(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(arguments.get("name") or "webapp").strip().replace("/", "-")
    template = str(arguments.get("template") or "static_html")
    try:
        ws = workspace(context)
        root = ws.ensure_dir(str(arguments.get("path") or f"webapps/{name}"))
        index = root / "index.html"
        if template in {"static_html", "plain_js"}:
            write_text_file(
                index,
                "<!doctype html><html><head><meta charset=\"utf-8\"><title>{}</title></head>"
                "<body><main id=\"app\"><h1>{}</h1></main><script src=\"app.js\"></script></body></html>\n".format(name, name),
            )
            write_text_file(root / "app.js", "document.body.dataset.rumiWebapp = 'ready';\n")
        elif template == "vite_react":
            write_text_file(index, "<!doctype html><div id=\"root\"></div><script type=\"module\" src=\"/src/main.jsx\"></script>\n")
            write_text_file(root / "src" / "main.jsx", "import React from 'react';\nimport {{ createRoot }} from 'react-dom/client';\ncreateRoot(document.getElementById('root')).render(<h1>{}</h1>);\n".format(name))
            write_text_file(root / "package.json", json.dumps({"scripts": {"build": "vite"}, "dependencies": {"@vitejs/plugin-react": "latest", "vite": "latest", "react": "latest", "react-dom": "latest"}}, indent=2))
        else:
            return err("unsupported template: " + template, "UNSUPPORTED_TEMPLATE")
        return ok({"path": ws.relative(root), "template": template, "files": [ws.relative(path) for path in root.rglob("*") if path.is_file()]})
    except Exception as exc:
        return err(str(exc), "PROJECT_SCAFFOLD_FAILED")


def webapp_build(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    if not path:
        return err("'path' is required", "INVALID_INPUT")
    command = arguments.get("command")
    if not command:
        return ok({"path": path, "built": True, "note": "static build requires no command"})
    return sandbox_exec({"command": command, "cwd": path, "timeout": arguments.get("timeout") or 120}, context)


def webapp_preview(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    html_path = path if path.endswith((".html", ".htm")) else path.rstrip("/") + "/index.html"
    return html_preview({**arguments, "path": html_path}, context)


def webapp_lint(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    try:
        ws = workspace(context)
        root = ws.resolve(path, must_exist=True, allow_root=True)
        index = root if root.is_file() else root / "index.html"
        issues = []
        if not index.exists():
            issues.append("missing index.html")
        return ok({"path": ws.relative(root) if root != ws.root else ".", "issues": issues, "ok": not issues})
    except Exception as exc:
        return err(str(exc), "WEBAPP_LINT_FAILED")


def static_site_export(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    path = str(arguments.get("path") or "")
    return artifact_zip({"path": path, "output_path": arguments.get("output_path") or "exports/static-site.zip"}, context)


def webapp_export_static(arguments: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    return static_site_export(arguments, context)
