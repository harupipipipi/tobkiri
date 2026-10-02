"""A persistent private Chromium profile controlled through approved CDP calls."""

from __future__ import annotations

import importlib.util
import base64
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from . import devtools
from .cdp import CDPConnection, CDPError, get_json, public_url, validate_websocket_url
from .cookies import normalize_cookie, to_cdp
from .extensions import (
    extension_api_unavailable,
    prepare_extension,
    remove_package,
    unsupported_result,
)
from .storage import checked_path, exclusive_lock, private_directory, read_json, write_json

_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BROWSER_PATH = re.compile(r"^/devtools/browser/[A-Za-z0-9-]{1,128}$")
_TAB_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")


class ManagedBrowser:
    """Own the process, data directories and bounded commands for one browser."""

    def __init__(self, browser_host_root: Path) -> None:
        self.host_root = browser_host_root.absolute()
        self.root = self.host_root / "managed"
        self.runtime_path = self.root / "runtime.json"
        self.extensions_path = self.root / "extensions.json"

    def status(self) -> dict[str, Any]:
        """Report readiness without opening another process or exposing CDP keys."""

        executable = find_executable()
        websocket_available = importlib.util.find_spec("websocket") is not None
        status: dict[str, Any] = {
            "managed": True,
            "supported": True,
            "running": False,
            "executable_available": executable is not None,
            "websocket_available": websocket_available,
        }
        if not websocket_available:
            status["message"] = "Install Tobkiri's browser-cdp optional dependency."
            return status
        try:
            metadata, _port, _url, version = self._validated_runtime()
            status.update(
                {
                    "running": True,
                    "profile_id": metadata["profile_id"],
                    "browser": str(version.get("Browser") or "Chromium")[:128],
                    "active_tab_id": metadata.get("active_tab_id"),
                    "headless": metadata.get("headless") is True,
                }
            )
        except FileNotFoundError:
            status["message"] = (
                "Start the model browser to browse with its private profile."
                if executable
                else "Install Chrome, Edge or Chromium, or configure "
                "RUMI_BROWSER_EXECUTABLE on the server."
            )
        except (OSError, RuntimeError, ValueError, PermissionError):
            status["message"] = "The managed browser is disconnected. Start it again."
        return status

    def has_record(self) -> bool:
        """Distinguish a disconnected managed runtime from legacy navigation."""

        self._check_root()
        checked_path(self.root, self.runtime_path)
        return self.runtime_path.exists()

    def start(
        self,
        profile_id: str,
        cookies: list[dict[str, Any]] | None = None,
        *,
        headless: bool = False,
        replace_cookies: bool = False,
    ) -> dict[str, Any]:
        """Start a detected Chromium executable using an isolated data directory."""

        profile_id = _profile_id(profile_id)
        if not isinstance(headless, bool) or not isinstance(replace_cookies, bool):
            raise ValueError("Managed browser headless and replace_cookies must be booleans")
        self._check_root()
        if importlib.util.find_spec("websocket") is None:
            raise RuntimeError("Install Tobkiri's browser-cdp optional dependency")
        executable = find_executable()
        if executable is None:
            raise RuntimeError(
                "Install Chrome, Edge or Chromium, or configure "
                "RUMI_BROWSER_EXECUTABLE on the server."
            )
        private_directory(self.host_root, self.root)
        with exclusive_lock(self.root):
            current = self.status()
            if current["running"]:
                if current["profile_id"] != profile_id:
                    raise ValueError("Stop the running browser before changing profiles")
                if cookies or replace_cookies:
                    self.set_cookies(profile_id, cookies or [], replace=replace_cookies)
                return {**current, "started": False}
            profile = self._profile_directory(profile_id)
            private_directory(self.root, profile)
            active_port = checked_path(self.root, profile / "DevToolsActivePort")
            try:
                active_port.unlink()
            except FileNotFoundError:
                pass
            command = [
                str(executable),
                f"--user-data-dir={profile}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--enable-automation",
                "--enable-unsafe-extension-debugging",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ]
            if headless:
                command.insert(-1, "--headless=new")
            options: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "shell": False,
                "close_fds": True,
            }
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                options["start_new_session"] = True
            process = subprocess.Popen(command, **options)
            deadline = time.monotonic() + 10
            try:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Chromium could not open the private profile")
                    try:
                        _port, browser_path = self._read_active_port(profile)
                        metadata = {
                            "profile_id": profile_id,
                            "pid": process.pid,
                            "browser_path": browser_path,
                            "started_at": int(time.time()),
                            "headless": headless,
                        }
                        write_json(self.root, self.runtime_path, metadata)
                        self._validated_runtime()
                        break
                    except (FileNotFoundError, OSError, ValueError, RuntimeError):
                        time.sleep(0.1)
                else:
                    raise TimeoutError("Chromium did not open its managed debugging port")
                if cookies or replace_cookies:
                    self.set_cookies(profile_id, cookies or [], replace=replace_cookies)
                restored = self._restore_extensions(profile_id)
                result = {**self.status(), "started": True}
                if not result["running"]:
                    raise RuntimeError("Chromium closed before its startup completed")
                if restored:
                    result["extension_warnings"] = restored
                return result
            except Exception:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                self._remove_runtime_record()
                raise

    def stop(self) -> dict[str, Any]:
        """Close only the browser whose profile and debugging identity validate."""

        self._check_root()
        private_directory(self.host_root, self.root)
        with exclusive_lock(self.root):
            if not self.has_record():
                return {"managed": True, "running": False, "stopped": False}
            try:
                metadata, port, url, _version = self._validated_runtime()
            except (FileNotFoundError, ConnectionError, OSError):
                self._remove_runtime_record()
                return {"managed": True, "running": False, "stopped": False}
            try:
                with CDPConnection(url, port=port) as connection:
                    connection.call("Browser.close")
            except (ConnectionError, OSError):
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    version = get_json(port, "/json/version", timeout=0.2)
                    if version.get("webSocketDebuggerUrl") != url:
                        break
                except (OSError, ValueError):
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("The managed browser did not close")
            self._remove_runtime_record()
            return {
                "managed": True,
                "running": False,
                "stopped": True,
                "profile_id": metadata["profile_id"],
            }

    def tabs(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """List actual page targets in the currently managed profile."""

        metadata, port, _url, _version = self._validated_runtime()
        self._require_profile(payload or {}, metadata)
        tabs = self._page_targets(port)
        active_id = metadata.get("active_tab_id")
        if not any(tab["id"] == active_id for tab in tabs):
            active_id = tabs[0]["id"] if tabs else None
            metadata["active_tab_id"] = active_id
            write_json(self.root, self.runtime_path, metadata)
        return {
            "profile_id": metadata["profile_id"],
            "active_tab_id": metadata.get("active_tab_id"),
            "tabs": [self._public_tab(tab, metadata["profile_id"]) for tab in tabs],
        }

    def navigate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Open or navigate a page target without using the user's browser."""

        url = str(payload.get("url") or "").strip()
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("An HTTP(S) URL without embedded credentials is required")
        metadata, port, browser_url, _version = self._validated_runtime()
        self._require_profile(payload, metadata)
        requested_id = payload.get("tab_id")
        if requested_id is not None:
            tab, connection = self._tab_connection(payload)
            with connection:
                navigation = connection.call("Page.navigate", {"url": url})
                if navigation.get("errorText"):
                    raise RuntimeError("The browser could not navigate to the URL")
            tab_id = tab["id"]
        else:
            with CDPConnection(browser_url, port=port) as connection:
                created = connection.call("Target.createTarget", {"url": url})
            tab_id = str(created.get("targetId") or "")
            if not _TAB_ID.fullmatch(tab_id):
                raise RuntimeError("The browser did not create a page target")
        metadata["active_tab_id"] = tab_id
        write_json(self.root, self.runtime_path, metadata)
        return {
            "opened": True,
            "managed": True,
            "profile_id": metadata["profile_id"],
            "tab_id": tab_id,
            "url": public_url(url),
        }

    def select_tab(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Activate one explicitly selected live page target."""

        requested_id = str(payload.get("tab_id") or "")
        if not requested_id:
            raise ValueError("tab_id is required")
        tab, _connection = self._tab_connection(payload)
        metadata, port, browser_url, _version = self._validated_runtime()
        with CDPConnection(browser_url, port=port) as connection:
            connection.call("Target.activateTarget", {"targetId": tab["id"]})
        metadata["active_tab_id"] = tab["id"]
        write_json(self.root, self.runtime_path, metadata)
        return {"active_tab_id": tab["id"], "selected": True}

    def inspect(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Inspect a bounded DOM snapshot from a selected managed tab."""

        tab, connection = self._tab_connection(payload)
        with connection:
            result = devtools.inspect_page(connection)
        return {**self._tab_result(tab), **result}

    def evaluate(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Run an approved JavaScript expression on a selected managed tab."""

        tab, connection = self._tab_connection(payload)
        with connection:
            result = devtools.evaluate_page(connection, payload)
        return {**self._tab_result(tab), **result}

    def network(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Capture bounded network events on one persistent page connection."""

        tab, connection = self._tab_connection(payload)
        with connection:
            result = devtools.capture_network(connection, payload)
        return {**self._tab_result(tab), **result}

    def screenshot(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Capture the managed tab viewport without observing the user's desktop."""

        tab, connection = self._tab_connection(payload)
        with connection:
            captured = connection.call(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": False,
                },
            )
        data = captured.get("data")
        if not isinstance(data, str) or not data or len(data) > 7 * 1024 * 1024:
            raise ValueError("Managed browser screenshot is unavailable or too large")
        decoded = base64.b64decode(data, validate=True)
        if len(decoded) > 5 * 1024 * 1024 or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Managed browser screenshot is unavailable or too large")
        return {
            **self._tab_result(tab),
            "screenshot": {
                "data_url": "data:image/png;base64," + data,
                "method": "browser_cdp",
                "coordinate_system": "viewport_pixels",
            },
        }

    def get_cookies(self, profile_id: str) -> list[dict[str, Any]] | None:
        """Read live cookies only when this exact managed profile is running."""

        if not self._profile_running(profile_id):
            return None
        metadata, port, url, _version = self._validated_runtime()
        with CDPConnection(url, port=port) as connection:
            result = connection.call("Storage.getCookies")
        cookies = result.get("cookies")
        if not isinstance(cookies, list) or len(cookies) > 5_000:
            raise ValueError("Managed cookie collection exceeds the limit")
        return [normalize_cookie(cookie) for cookie in cookies if isinstance(cookie, dict)]

    def set_cookies(self, profile_id: str, cookies: list[dict[str, Any]], *, replace: bool) -> bool:
        """Apply imported cookies to the matching live browser profile."""

        if not self._profile_running(profile_id):
            return False
        _metadata, port, url, _version = self._validated_runtime()
        with CDPConnection(url, port=port) as connection:
            if replace:
                connection.call("Storage.clearCookies")
            if cookies:
                connection.call(
                    "Storage.setCookies", {"cookies": [to_cdp(cookie) for cookie in cookies]}
                )
        return True

    def delete_cookies(self, profile_id: str, query: Mapping[str, Any]) -> bool:
        """Delete matching cookies without replacing unrelated live cookies."""

        current = self.get_cookies(profile_id)
        if current is None:
            return False
        tab, connection = self._tab_connection({"profile_id": profile_id})
        del tab
        with connection:
            for cookie in current:
                if all(
                    not query.get(key) or str(cookie[key]) == str(query[key])
                    for key in ("name", "domain", "path")
                ):
                    connection.call(
                        "Network.deleteCookies",
                        {key: cookie[key] for key in ("name", "domain", "path")},
                    )
        return True

    def clear_cache(self, profile_id: str) -> bool:
        """Clear the live profile's cache when it is currently managed."""

        if not self._profile_running(profile_id):
            return False
        _tab, connection = self._tab_connection({"profile_id": profile_id})
        with connection:
            connection.call("Network.clearBrowserCache")
        return True

    def delete_profile(self, profile_id: str) -> None:
        """Remove one stopped managed profile and its uploaded packages."""

        profile_id = _profile_id(profile_id)
        current = self.status()
        if current.get("running") and current.get("profile_id") == profile_id:
            self.stop()
        self._check_root()
        for base, directory in (
            (self.root / "profiles", self._profile_directory(profile_id)),
            (self.root / "extensions", self.root / "extensions" / profile_id),
        ):
            checked_path(self.root, directory)
            resolved = directory.resolve()
            relative = resolved.relative_to(base.resolve())
            if relative.parts != (profile_id,):
                raise PermissionError("Managed profile cleanup is outside its directory")
            if directory.exists():
                shutil.rmtree(resolved)
        if self.extensions_path.exists():
            raw = read_json(self.root, self.extensions_path)
            raw.pop(profile_id, None)
            write_json(self.root, self.extensions_path, raw)

    def extensions_list(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """List saved packages, querying Chromium only for the running profile."""

        self._check_root()
        current = self.status()
        requested = (payload or {}).get("profile_id") or current.get("profile_id") or "default"
        profile_id = _profile_id(requested)
        installed = self._extension_records(profile_id)
        if not current.get("running") or current.get("profile_id") != profile_id:
            return {
                "profile_id": profile_id,
                "running": False,
                "supported": None,
                "extensions": [{**item, "installed": True, "loaded": False} for item in installed],
                "message": "Start this profile to load or inspect its extensions.",
            }
        metadata, port, url, _version = self._validated_runtime()
        try:
            with CDPConnection(url, port=port) as connection:
                result = connection.call("Extensions.getExtensions")
        except CDPError as exc:
            if extension_api_unavailable(exc):
                unsupported = unsupported_result("browser.extensions.list")
                return {
                    "supported": False,
                    "profile_id": profile_id,
                    "message": unsupported["message"],
                    "extensions": [
                        {**item, "installed": True, "loaded": False} for item in installed
                    ],
                }
            raise
        live = result.get("extensions")
        live = live if isinstance(live, list) else []
        by_id = {str(item.get("id")): item for item in live if isinstance(item, dict)}
        return {
            "supported": True,
            "profile_id": metadata["profile_id"],
            "extensions": [
                {
                    **item,
                    "installed": True,
                    "loaded": bool(by_id.get(item["extension_id"], {}).get("enabled")),
                }
                for item in installed
            ],
        }

    def extension_install(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Install a validated package using Chromium's optional Extensions API."""

        with exclusive_lock(self.root):
            metadata, port, url, _version = self._validated_runtime()
            self._require_profile(payload, metadata)
            records = self._extension_records(metadata["profile_id"])
            if len(records) >= 100:
                raise ValueError("A managed profile supports at most 100 uploaded extensions")
            directory, package = prepare_extension(self.root, metadata["profile_id"], payload)
            try:
                with CDPConnection(url, port=port) as connection:
                    loaded = connection.call("Extensions.loadUnpacked", {"path": str(directory)})
            except CDPError as exc:
                remove_package(self.root, directory)
                if extension_api_unavailable(exc):
                    return unsupported_result("browser.extensions.install")
                raise
            except Exception:
                remove_package(self.root, directory)
                raise
            extension_id = str(loaded.get("id") or "")
            if not _EXTENSION_ID.fullmatch(extension_id):
                raise RuntimeError("Chromium did not return a valid extension id")
            package["extension_id"] = extension_id
            self._save_extensions(metadata["profile_id"], [*records, package])
            return {"installed": True, "supported": True, "extension": package}

    def extension_remove(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Uninstall a known uploaded package and remove its owned files."""

        extension_id = str(payload.get("extension_id") or "")
        if not _EXTENSION_ID.fullmatch(extension_id):
            raise ValueError("A valid extension_id is required")
        with exclusive_lock(self.root):
            metadata, port, url, _version = self._validated_runtime()
            self._require_profile(payload, metadata)
            records = self._extension_records(metadata["profile_id"])
            package = next((item for item in records if item["extension_id"] == extension_id), None)
            if package is None:
                raise KeyError("Uploaded extension is unavailable")
            try:
                with CDPConnection(url, port=port) as connection:
                    connection.call("Extensions.uninstall", {"id": extension_id})
            except CDPError as exc:
                if extension_api_unavailable(exc):
                    return unsupported_result("browser.extensions.remove")
                raise
            directory = self.root / "extensions" / metadata["profile_id"] / package["package_id"]
            remove_package(self.root, directory)
            self._save_extensions(
                metadata["profile_id"], [item for item in records if item is not package]
            )
            return {"removed": True, "extension_id": extension_id, "supported": True}

    def _validated_runtime(self) -> tuple[dict[str, Any], int, str, dict[str, Any]]:
        self._check_root()
        metadata = read_json(self.root, self.runtime_path)
        if not metadata:
            raise FileNotFoundError("Start the managed browser first")
        profile_id = _profile_id(metadata.get("profile_id"))
        expected_path = str(metadata.get("browser_path") or "")
        if not _BROWSER_PATH.fullmatch(expected_path):
            raise PermissionError("Managed browser identity is invalid")
        profile = self._profile_directory(profile_id)
        port, browser_path = self._read_active_port(profile)
        if browser_path != expected_path:
            raise PermissionError("Managed browser identity changed")
        version = get_json(port, "/json/version")
        if not isinstance(version, dict):
            raise ValueError("Managed browser discovery is invalid")
        url = validate_websocket_url(
            str(version.get("webSocketDebuggerUrl") or ""), port, expected_path
        )
        with CDPConnection(url, port=port) as connection:
            arguments = connection.call("Browser.getBrowserCommandLine").get("arguments")
        if not isinstance(arguments, list) or not self._owned_command_line(arguments, profile):
            raise PermissionError("Debugging endpoint does not belong to the managed profile")
        return metadata, port, url, version

    def _tab_connection(self, payload: Mapping[str, Any]) -> tuple[dict[str, Any], CDPConnection]:
        metadata, port, _url, _version = self._validated_runtime()
        self._require_profile(payload, metadata)
        tabs = self._page_targets(port)
        explicit_id = payload.get("tab_id")
        requested_id = explicit_id if explicit_id is not None else metadata.get("active_tab_id")
        if requested_id is not None:
            requested_id = str(requested_id)
            if not _TAB_ID.fullmatch(requested_id):
                raise ValueError("tab_id is invalid")
            tab = next((tab for tab in tabs if tab["id"] == requested_id), None)
            if tab is None and explicit_id is None:
                tab = tabs[0] if tabs else None
        else:
            tab = tabs[0] if tabs else None
        if tab is None:
            raise KeyError("Managed browser tab is unavailable")
        if explicit_id is None and tab["id"] != metadata.get("active_tab_id"):
            metadata["active_tab_id"] = tab["id"]
            write_json(self.root, self.runtime_path, metadata)
        return tab, CDPConnection(tab["webSocketDebuggerUrl"], port=port)

    def _page_targets(self, port: int) -> list[dict[str, Any]]:
        raw = get_json(port, "/json/list")
        if not isinstance(raw, list) or len(raw) > 512:
            raise ValueError("Managed browser target collection is invalid")
        tabs = []
        for tab in raw:
            if not isinstance(tab, dict) or tab.get("type") != "page":
                continue
            tab_id = str(tab.get("id") or "")
            if not _TAB_ID.fullmatch(tab_id):
                raise ValueError("Managed browser target identity is invalid")
            validate_websocket_url(
                str(tab.get("webSocketDebuggerUrl") or ""), port, f"/devtools/page/{tab_id}"
            )
            tabs.append(tab)
        return tabs

    def _read_active_port(self, profile: Path) -> tuple[int, str]:
        path = checked_path(self.root, profile / "DevToolsActivePort")
        with path.open("r", encoding="utf-8") as handle:
            lines = handle.read(1025).splitlines()
        if len(lines) != 2 or len("\n".join(lines)) > 1024 or not lines[0].isdigit():
            raise ValueError("Managed debugging port file is invalid")
        port = int(lines[0])
        if not 1 <= port <= 65535 or not _BROWSER_PATH.fullmatch(lines[1]):
            raise ValueError("Managed debugging port file is invalid")
        return port, lines[1]

    def _profile_directory(self, profile_id: str) -> Path:
        return checked_path(self.root, self.root / "profiles" / _profile_id(profile_id))

    def _check_root(self) -> None:
        checked_path(self.host_root, self.root)

    def _remove_runtime_record(self) -> None:
        checked_path(self.root, self.runtime_path)
        try:
            self.runtime_path.unlink()
        except FileNotFoundError:
            pass

    def _profile_running(self, profile_id: str) -> bool:
        status = self.status()
        if self.has_record() and not status["running"]:
            raise RuntimeError("The managed browser is disconnected")
        return bool(status["running"] and status.get("profile_id") == profile_id)

    def _extension_records(self, profile_id: str) -> list[dict[str, Any]]:
        raw = read_json(self.root, self.extensions_path)
        records = raw.get(profile_id, [])
        if not isinstance(records, list) or len(records) > 100:
            raise ValueError("Managed extension registry is invalid")
        for record in records:
            if (
                not isinstance(record, dict)
                or not re.fullmatch(r"[a-f0-9]{32}", str(record.get("package_id") or ""))
                or not _EXTENSION_ID.fullmatch(str(record.get("extension_id") or ""))
            ):
                raise ValueError("Managed extension registry is invalid")
        return records

    def _save_extensions(self, profile_id: str, records: list[dict[str, Any]]) -> None:
        raw = read_json(self.root, self.extensions_path)
        raw[profile_id] = records
        write_json(self.root, self.extensions_path, raw)

    def _restore_extensions(self, profile_id: str) -> list[str]:
        records = self._extension_records(profile_id)
        if not records:
            return []
        _metadata, port, url, _version = self._validated_runtime()
        warnings = []
        with CDPConnection(url, port=port) as connection:
            for record in records:
                directory = checked_path(
                    self.root, self.root / "extensions" / profile_id / record["package_id"]
                )
                try:
                    loaded = connection.call("Extensions.loadUnpacked", {"path": str(directory)})
                    if loaded.get("id") != record["extension_id"]:
                        warnings.append("An uploaded extension changed its Chromium identity.")
                except CDPError:
                    warnings.append(
                        "An uploaded extension could not be restored by this Chromium build."
                    )
        return warnings

    @staticmethod
    def _owned_command_line(arguments: list[Any], profile: Path) -> bool:
        data_args = [
            arg for arg in arguments if isinstance(arg, str) and arg.startswith("--user-data-dir=")
        ]
        if len(data_args) != 1:
            return False
        try:
            matches = Path(data_args[0].split("=", 1)[1]).resolve() == profile.resolve()
        except (OSError, ValueError):
            return False
        return (
            matches
            and "--remote-debugging-address=127.0.0.1" in arguments
            and "--remote-debugging-port=0" in arguments
        )

    @staticmethod
    def _require_profile(payload: Mapping[str, Any], metadata: Mapping[str, Any]) -> None:
        requested = payload.get("profile_id")
        if requested is not None and _profile_id(requested) != metadata["profile_id"]:
            raise ValueError("The requested profile is not the running managed profile")

    @staticmethod
    def _public_tab(tab: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
        return {
            "tab_id": str(tab["id"]),
            "profile_id": profile_id,
            "title": str(tab.get("title") or "")[:512],
            "url": public_url(tab.get("url")),
        }

    @staticmethod
    def _tab_result(tab: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "tab_id": tab["id"],
            "title": str(tab.get("title") or "")[:512],
            "url": public_url(tab.get("url")),
        }


def find_executable() -> Path | None:
    """Resolve only trusted server configuration or known installed browsers."""

    configured = os.environ.get("RUMI_BROWSER_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_file() else None
    candidates: list[Path] = []
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.extend(
                    [
                        Path(base) / "Google/Chrome/Application/chrome.exe",
                        Path(base) / "Microsoft/Edge/Application/msedge.exe",
                        Path(base) / "Chromium/Application/chrome.exe",
                    ]
                )
    else:
        candidates.extend(
            [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            ]
        )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _profile_id(value: Any) -> str:
    result = str(value or "").strip()
    if not _PROFILE_ID.fullmatch(result):
        raise ValueError("Managed browser profile id is invalid")
    return result
