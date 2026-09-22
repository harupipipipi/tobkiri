from __future__ import annotations

import json
import mimetypes
import os
import signal
import sys
import threading
import time
import types
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractRouteError,
    HTTPContractTarget,
    resolve_contract_route,
)

_SETTINGS_MODEL_KEY = "preferred" + "_model"
_SEARCH_HOME_ROUTE_POLICIES = {
    ("GET", "/api/health"): {"approval_required": False},
    ("GET", "/api/models"): {"approval_required": False},
    ("GET", "/api/settings"): {"approval_required": False},
    ("GET", "/api/route-state"): {"approval_required": False},
    ("POST", "/api/route"): {"approval_required": False},
    ("POST", "/api/answer"): {"approval_required": False},
    ("POST", "/api/settings/model"): {"approval_required": False},
    ("POST", "/api/route-state"): {"approval_required": False},
}


def _search_home_contract_routes() -> dict[tuple[str, str], HTTPContractBinding]:
    """Build Search Home's immutable routes for the generic Host parser."""

    return {
        (method, path): HTTPContractBinding(
            method=method,
            path=path,
            presentation="search_home_result",
            targets=(
                HTTPContractTarget(
                    contribution_id=f"search-home.{method.lower()}.{path[5:].replace('/', '.')}",
                    contract_id="search-home.ui.v1",
                    operation_id=path.removeprefix("/api/").replace("/", "."),
                    provider_id="search-home.desktop",
                    function_id="search-home.desktop",
                ),
            ),
            application_id="search_home_pack",
            route_namespace="search_home_pack",
        )
        for (method, path) in _SEARCH_HOME_ROUTE_POLICIES
    }


_SEARCH_HOME_CONTRACT_ROUTES = _search_home_contract_routes()


def _pack_root() -> Path:
    return Path(__file__).resolve().parent


def _ensure_import_path() -> None:
    pack_root = _pack_root()
    configured_roots = (
        os.environ.get("RUMI_APP_DIR"),
        os.environ.get("RUMI_CORE_DIR"),
        os.environ.get("REPO"),
    )
    for path in (
        pack_root.parents[1],
        *(Path(root) for root in configured_roots if root),
        *(Path(root) / "tobkiri_runtime" for root in configured_roots if root),
    ):
        root = str(path)
        if root and root not in sys.path:
            sys.path.insert(0, root)
    _install_ecosystem_pack_alias(pack_root, "search_home_pack")


def _install_ecosystem_pack_alias(pack_root: Path, pack_id: str) -> None:
    ecosystem_dirs = _candidate_ecosystem_dirs(pack_root)
    ecosystem = sys.modules.get("ecosystem")
    if ecosystem is None:
        ecosystem = types.ModuleType("ecosystem")
        ecosystem.__path__ = [str(path) for path in ecosystem_dirs]  # type: ignore[attr-defined]
        sys.modules["ecosystem"] = ecosystem
    elif not hasattr(ecosystem, "__path__"):
        ecosystem.__path__ = [str(path) for path in ecosystem_dirs]  # type: ignore[attr-defined]
    else:
        paths = list(getattr(ecosystem, "__path__", []))
        for ecosystem_dir in ecosystem_dirs:
            ecosystem_path = str(ecosystem_dir)
            if ecosystem_path not in paths:
                paths.insert(0, ecosystem_path)
        ecosystem.__path__ = paths  # type: ignore[attr-defined]

    module_name = f"ecosystem.{pack_id}"
    pack_module = sys.modules.get(module_name)
    pack_path = str(pack_root)
    if pack_module is None:
        pack_module = types.ModuleType(module_name)
        pack_module.__path__ = [pack_path]  # type: ignore[attr-defined]
        pack_module.__package__ = module_name
        sys.modules[module_name] = pack_module
    else:
        paths = list(getattr(pack_module, "__path__", []))
        if pack_path not in paths:
            paths.insert(0, pack_path)
            pack_module.__path__ = paths  # type: ignore[attr-defined]
    setattr(ecosystem, pack_id, pack_module)


def _candidate_ecosystem_dirs(pack_root: Path) -> list[Path]:
    candidates: list[Path] = []
    if pack_root.parent.name == "ecosystem":
        candidates.append(pack_root.parent)
    for root in (
        os.environ.get("RUMI_APP_DIR"),
        os.environ.get("RUMI_CORE_DIR"),
        os.environ.get("REPO"),
    ):
        if root:
            candidates.append(Path(root) / "ecosystem")
            candidates.append(Path(root) / "tobkiri_runtime" / "ecosystem")

    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve()
        except OSError:
            path = candidate
        key = str(path)
        if key in seen or not candidate.exists() or not candidate.is_dir():
            continue
        seen.add(key)
        resolved.append(candidate)
    return resolved


def route_state_path(*, root: Path | None = None) -> Path:
    base = root or (_pack_root() / "user_data" / "shared" / "search_home")
    return base / "route_state.json"


def persist_route_state(state: dict[str, Any], *, root: Path | None = None) -> Path:
    path = route_state_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _sanitize_route_state_for_persistence(dict(state or {}))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _sanitize_route_state_for_persistence(value: Any, key: str = "") -> Any:
    """Remove secret-bearing URLs from backend-restored route state."""
    from ecosystem.search_home_pack.domain.safe_url import url_safe_for_persistence

    if isinstance(value, dict):
        return {
            str(child_key): _sanitize_route_state_for_persistence(child, str(child_key))
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_route_state_for_persistence(child, key) for child in value]
    if isinstance(value, str) and (key.endswith("_url") or key == "url"):
        return url_safe_for_persistence(value)
    if isinstance(value, str) and key == "query" and "://" in value:
        return value if url_safe_for_persistence(value) else ""
    return value


def load_route_state(*, root: Path | None = None) -> dict[str, Any]:
    path = route_state_path(root=root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(data) if isinstance(data, dict) else {}


def clear_route_state(*, root: Path | None = None) -> None:
    path = route_state_path(root=root)
    if path.exists():
        path.unlink()


def _web_root(pack_root: Path) -> Path:
    if (pack_root / "ui" / "index.html").is_file():
        return pack_root / "ui"
    return pack_root / "webapp" / "dist"


def _surface_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def _health_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/api/health"


def _wait_until_ready(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    health_url = _health_url(host, port)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    return False


def _open_desktop_surface(url: str, title: str = "Rumi Search Home") -> str:
    if os.environ.get("SEARCH_HOME_OPEN_BROWSER", "1") == "0":
        return "disabled"

    surface = os.environ.get("SEARCH_HOME_SURFACE", "browser").strip().lower()
    if surface == "webview":
        try:
            import webview  # type: ignore[import-not-found]
        except Exception:
            webbrowser.open(url)
            return "webview_unavailable"

        window = webview.create_window(title, url)
        webview.start()
        return "webview" if window is not None else "webview_unavailable"

    webbrowser.open(url)
    return "browser"


def _make_handler(pack_root: Path):
    from ecosystem.search_home_pack.domain.defaultspack_bridge import DefaultspackBridge
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    web_root = _web_root(pack_root)
    bridge = DefaultspackBridge()
    resolver = SearchTargetResolver(bridge=bridge)

    class SearchHomeHandler(BaseHTTPRequestHandler):
        server_version = "RumiSearchHome/0.2"
        _contract_routes = _SEARCH_HOME_CONTRACT_ROUTES

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _resolve_contract_path(self, method: str, path: str) -> str | None:
            try:
                resolved = resolve_contract_route(
                    self,
                    method,
                    path,
                    namespace="search_home_pack",
                )
            except HTTPContractRouteError as exc:
                self._json_response(
                    {
                        "status": "error",
                        "error": {"code": exc.code, "message": str(exc)},
                    },
                    status=HTTPStatus(exc.status),
                )
                return None
            return resolved.path if resolved is not None else path

        def do_GET(self) -> None:  # noqa: N802
            request_path = urlparse(self.path).path
            resolved_path = self._resolve_contract_path("GET", request_path)
            if resolved_path is None:
                return
            path = resolved_path
            if path in {"/health", "/api/health"}:
                self._json_response(
                    {
                        "status": "ok",
                        "pack_id": "search_home_pack",
                        "route_state_path": str(route_state_path(root=pack_root / "user_data" / "shared" / "search_home")),
                    }
                )
                return
            if path == "/api/route-state":
                self._json_response(load_route_state(root=pack_root / "user_data" / "shared" / "search_home"))
                return
            if path == "/api/models":
                self._json_response(bridge.list_models())
                return
            if path == "/api/settings":
                self._json_response({"models": bridge.model_settings()})
                return
            self._serve_static(path)

        def do_POST(self) -> None:  # noqa: N802
            request_path = urlparse(self.path).path
            resolved_path = self._resolve_contract_path("POST", request_path)
            if resolved_path is None:
                return
            path = resolved_path
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._json_response(
                    {
                        "status": "error",
                        "error": {"message": str(exc), "code": "INVALID_INPUT"},
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            if path == "/api/route":
                selected_model = str(payload.get("model") or payload.get(_SETTINGS_MODEL_KEY) or "").strip()
                decision = resolver.resolve(
                    str(payload.get("input") or ""),
                    context={"source": "search_home.route", _SETTINGS_MODEL_KEY: selected_model},
                )
                persist_route_state(decision.to_dict(), root=pack_root / "user_data" / "shared" / "search_home")
                self._json_response(decision.to_dict())
                return
            if path == "/api/answer":
                answer = bridge.answer_query(
                    str(payload.get("input") or payload.get("query") or ""),
                    model_ref_override=str(payload.get("model") or payload.get(_SETTINGS_MODEL_KEY) or "").strip(),
                    use_search=bool(payload.get("use_search", True)),
                    context={"source": "search_home.answer"},
                )
                status = HTTPStatus.OK if answer.get("status") == "ok" else HTTPStatus.BAD_GATEWAY
                self._json_response(answer, status=status)
                return
            if path == "/api/settings/model":
                try:
                    result = bridge.set_selected_model(str(payload.get("model") or payload.get("profile_id") or ""))
                except ValueError as exc:
                    self._json_response(
                        {"status": "error", "error": {"message": str(exc), "code": "INVALID_INPUT"}},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._json_response({"status": "ok", "data": result})
                return
            if path == "/api/route-state":
                persist_route_state(payload, root=pack_root / "user_data" / "shared" / "search_home")
                self._json_response({"status": "ok", "saved": True})
                return
            self._json_response(
                {"status": "error", "error": {"message": "not found", "code": "NOT_FOUND"}},
                status=HTTPStatus.NOT_FOUND,
            )

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON body: {exc}") from exc
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload

        def _json_response(self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_static(self, path: str) -> None:
            index_path = web_root / "index.html"
            if not index_path.is_file():
                self._json_response(
                    {
                        "status": "error",
                        "error": {
                            "message": "search_home UI bundle is missing; rebuild webapp assets",
                            "code": "UI_NOT_BUILT",
                        },
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return

            normalized = PurePosixPath(path.lstrip("/"))
            candidate = (web_root / normalized).resolve()
            try:
                candidate.relative_to(web_root.resolve())
            except ValueError:
                candidate = index_path

            if path in {"", "/"} or not candidate.is_file():
                candidate = index_path

            mime_type, _ = mimetypes.guess_type(candidate.name)
            body = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{mime_type or 'text/html'}; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return SearchHomeHandler


class SearchHomeServer:
    def __init__(self, host: str, port: int, pack_root: Path) -> None:
        handler = _make_handler(pack_root)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)


def main() -> int:
    _ensure_import_path()
    host = os.environ.get("SEARCH_HOME_HOST", "127.0.0.1")
    port = int(os.environ.get("SEARCH_HOME_PORT", "8777"))
    pack_root = _pack_root()
    url = _surface_url(host, port)
    server: SearchHomeServer | None = None
    try:
        server = SearchHomeServer(host, port, pack_root)
        server.start()
    except OSError:
        server = None
        if not _wait_until_ready(host, port, timeout=1.0):
            raise

    if not _wait_until_ready(host, port, timeout=10.0):
        raise RuntimeError("Search Home server did not become ready in time")

    surface_result = _open_desktop_surface(url, title="Rumi Search Home")
    if surface_result == "webview":
        if server is not None:
            server.stop()
        return 0

    stop = False

    def _handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not stop:
            time.sleep(0.5)
    finally:
        if server is not None:
            server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
