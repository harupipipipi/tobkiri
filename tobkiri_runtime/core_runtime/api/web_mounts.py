"""Fixed, registry-free static mounts for the Pack v4 host shell."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler as _HTTPHandlerBase
else:
    _HTTPHandlerBase = object

from .api_response import APIResponse


class WebMountEntry(TypedDict):
    """One immutable first-party static mount."""

    path_prefix: str
    web_root: Path
    spa_fallback: bool
    index_file: str
    auth_required: bool


class WebMountMixin(_HTTPHandlerBase):
    """Serve only first-party roots compiled into the runtime."""

    _CLIENT_DISCONNECT_EXCEPTIONS: tuple[type[OSError], ...]

    if TYPE_CHECKING:
        def _send_response(
            self,
            response: APIResponse,
            status: int = 200,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None: ...

        def _get_cors_origin(self, origin: str) -> str | None: ...

    _MIME_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".map": "application/json",
    }

    @staticmethod
    def _fixed_web_mounts() -> tuple[WebMountEntry, ...]:
        core_root = Path(__file__).resolve().parent.parent
        runtime_root = core_root.parent
        return (
            {
                "path_prefix": "/panel",
                "web_root": core_root / "core_pack" / "core_control_panel" / "web",
                "spa_fallback": True,
                "index_file": "index.html",
                "auth_required": True,
            },
            {
                "path_prefix": "/setup",
                "web_root": core_root / "core_pack" / "core_setup" / "web",
                "spa_fallback": True,
                "index_file": "index.html",
                "auth_required": False,
            },
            {
                "path_prefix": "/desktops",
                "web_root": runtime_root / "ecosystem" / "defaultspack" / "ui",
                "spa_fallback": True,
                "index_file": "shell.html",
                "auth_required": True,
            },
        )

    def _match_web_mount(self, request_path: str) -> WebMountEntry | None:
        for mount in self._fixed_web_mounts():
            prefix = mount["path_prefix"]
            if request_path == prefix or request_path.startswith(f"{prefix}/"):
                return mount
        return None

    def _serve_static_file(
        self,
        request_path: str,
        mount: WebMountEntry | None = None,
    ) -> None:
        selected = mount or self._match_web_mount(request_path)
        if selected is None:
            self._send_response(APIResponse(False, error="Not found"), 404)
            return
        prefix = selected["path_prefix"]
        root = selected["web_root"].resolve()
        relative = request_path[len(prefix):]
        if not relative or relative == "/":
            relative = f"/{selected['index_file']}"
        try:
            target = (root / relative.lstrip("/")).resolve()
            target.relative_to(root)
        except (OSError, ValueError):
            self._send_response(APIResponse(False, error="Forbidden"), 403)
            return
        if not target.is_file():
            fallback = root / selected["index_file"]
            if selected["spa_fallback"] and "." not in target.name and fallback.is_file():
                target = fallback
            else:
                self._send_response(APIResponse(False, error="Not found"), 404)
                return
        try:
            data = target.read_bytes()
        except OSError:
            self._send_response(APIResponse(False, error="Read error"), 500)
            return
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                self._MIME_TYPES.get(target.suffix.lower(), "application/octet-stream"),
            )
            self.send_header("Content-Length", str(len(data)))
            origin = self._get_cors_origin(self.headers.get("Origin", ""))
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(data)
        except self._CLIENT_DISCONNECT_EXCEPTIONS:
            self.close_connection = True


__all__ = ["WebMountEntry", "WebMountMixin"]
