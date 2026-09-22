"""Viewer-invoked browser authority runner owned by the browser service pack."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
import urllib.parse
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Final, Mapping


_PROFILE_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_COOKIES: Final[int] = 5_000
_MAX_TABS: Final[int] = 256


class BrowserHostRunner:
    """Own browser session/profile/cookie metadata and approved navigation."""

    def __init__(self, user_data_root: Path | None = None) -> None:
        base = user_data_root or Path(
            os.environ.get("RUMI_USER_DATA") or Path.home() / ".rumi"
        )
        self.root = Path(base) / "browser_host"
        self.state_path = self.root / "state.json"

    def run(
        self,
        action: str,
        payload: Mapping[str, Any] | None,
        *,
        viewer_host_approved: bool,
        artifact_root: Path | None = None,
    ) -> dict[str, Any]:
        """Run one allowlisted action after Viewer token validation."""

        if not viewer_host_approved:
            raise PermissionError("Viewer approval is required")
        normalized = str(action or "").strip()
        args = dict(payload or {})
        contract_operation = str(args.pop("_rumi_contract_operation", "")).strip()
        state = self._read_state()
        if normalized == "browser.session":
            if contract_operation == "browser.session.create":
                state["session_id"] = "session_" + uuid.uuid4().hex
                state["tabs"] = []
                state["active_tab_id"] = None
                self._write_state(state)
            elif contract_operation == "browser.session.close":
                closed_session_id = state["session_id"]
                state["session_id"] = "session_" + uuid.uuid4().hex
                state["tabs"] = []
                state["active_tab_id"] = None
                self._write_state(state)
                return {
                    "action": "browser.session.close",
                    "closed_session_id": closed_session_id,
                    "closed": True,
                }
            return self._session(state)
        if normalized == "browser.profiles.list":
            return self._profiles(state)
        if normalized == "browser.profile.create":
            return self._create_profile(state, args)
        if normalized == "browser.profile.set_active":
            return self._set_active_profile(state, args)
        if normalized == "browser.profile.delete":
            return self._delete_profile(state, args)
        if normalized == "browser.profile.clear_cache":
            return self._clear_cache(state, args)
        if normalized == "browser.profile.clear_cookies":
            return self._clear_cookies(state, args)
        if normalized == "browser.cookies.list":
            return self._list_cookies(state, args)
        if normalized == "browser.cookies.import":
            return self._import_cookies(state, args)
        if normalized == "browser.cookies.delete":
            return self._delete_cookies(state, args)
        if normalized == "browser.open_url":
            return self._open_url(state, args)
        if normalized == "browser.tabs":
            return self._tabs(state)
        if normalized == "browser.select_tab":
            return self._select_tab(state, args)
        if normalized == "browser.downloads.list":
            return self._downloads(state)
        if normalized == "browser.download.collect":
            return self._collect_download(args, artifact_root)
        return {
            "action": normalized,
            "is_error": True,
            "error_type": "browser_runner_unavailable",
        }

    def _session(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "browser.session",
            "session_id": state["session_id"],
            "active_profile_id": state["active_profile_id"],
            "active_tab_id": state.get("active_tab_id"),
            "profiles": list(state["profiles"].values()),
            "tabs": list(state["tabs"]),
        }

    def _profiles(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "browser.profiles.list",
            "active_profile_id": state["active_profile_id"],
            "profiles": list(state["profiles"].values()),
        }

    def _create_profile(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile_id = _profile_id(
            payload.get("profile_id") or payload.get("name") or f"profile-{int(time.time())}"
        )
        if profile_id in state["profiles"]:
            raise ValueError("browser profile already exists")
        profile = {
            "profile_id": profile_id,
            "label": str(payload.get("label") or payload.get("name") or profile_id)[:128],
            "created_at": _now(),
            "cache_revision": 0,
        }
        state["profiles"][profile_id] = profile
        state["cookies"][profile_id] = []
        if payload.get("set_active", True) is not False:
            state["active_profile_id"] = profile_id
        self._write_state(state)
        return {"action": "browser.profile.create", "profile": profile}

    def _set_active_profile(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile_id = _profile_id(payload.get("profile_id"))
        if profile_id not in state["profiles"]:
            raise KeyError("browser profile is unavailable")
        state["active_profile_id"] = profile_id
        self._write_state(state)
        return {"action": "browser.profile.set_active", "active_profile_id": profile_id}

    def _delete_profile(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile_id = _profile_id(payload.get("profile_id"))
        if profile_id == "default":
            raise ValueError("the default browser profile cannot be deleted")
        existed = state["profiles"].pop(profile_id, None) is not None
        state["cookies"].pop(profile_id, None)
        state["tabs"] = [
            tab for tab in state["tabs"] if tab.get("profile_id") != profile_id
        ]
        if state["active_profile_id"] == profile_id:
            state["active_profile_id"] = "default"
        self._write_state(state)
        return {"action": "browser.profile.delete", "profile_id": profile_id, "deleted": existed}

    def _clear_cache(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile = self._profile(state, payload)
        profile["cache_revision"] = int(profile.get("cache_revision") or 0) + 1
        self._write_state(state)
        return {
            "action": "browser.profile.clear_cache",
            "profile_id": profile["profile_id"],
            "cache_revision": profile["cache_revision"],
        }

    def _clear_cookies(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile = self._profile(state, payload)
        profile_id = profile["profile_id"]
        removed = len(state["cookies"].get(profile_id, []))
        state["cookies"][profile_id] = []
        self._write_state(state)
        return {
            "action": "browser.profile.clear_cookies",
            "profile_id": profile_id,
            "removed": removed,
        }

    def _list_cookies(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile = self._profile(state, payload)
        profile_id = profile["profile_id"]
        include_values = payload.get("include_values") is True
        cookies = []
        for item in state["cookies"].get(profile_id, []):
            projected = dict(item)
            if not include_values:
                projected.pop("value", None)
            cookies.append(projected)
        return {
            "action": "browser.cookies.list",
            "profile_id": profile_id,
            "cookies": cookies,
            "count": len(cookies),
        }

    def _import_cookies(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile = self._profile(state, payload)
        raw = payload.get("cookies")
        if not isinstance(raw, list) or len(raw) > _MAX_COOKIES:
            raise ValueError("cookies must be a bounded list")
        normalized = [_cookie(item) for item in raw if isinstance(item, Mapping)]
        profile_id = profile["profile_id"]
        current = [] if payload.get("replace") is True else state["cookies"].get(profile_id, [])
        merged = {_cookie_key(item): item for item in current}
        merged.update({_cookie_key(item): item for item in normalized})
        state["cookies"][profile_id] = list(merged.values())[:_MAX_COOKIES]
        self._write_state(state)
        return {
            "action": "browser.cookies.import",
            "profile_id": profile_id,
            "imported": len(normalized),
            "count": len(state["cookies"][profile_id]),
        }

    def _delete_cookies(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile = self._profile(state, payload)
        profile_id = profile["profile_id"]
        before = state["cookies"].get(profile_id, [])
        remaining = [item for item in before if not _cookie_matches(item, payload)]
        state["cookies"][profile_id] = remaining
        self._write_state(state)
        return {
            "action": "browser.cookies.delete",
            "profile_id": profile_id,
            "deleted": len(before) - len(remaining),
            "count": len(remaining),
        }

    def _open_url(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        raw_url = str(payload.get("url") or "").strip()
        parsed = urllib.parse.urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("an HTTP(S) URL is required")
        profile = self._profile(state, payload)
        opened = bool(webbrowser.open(raw_url, new=2, autoraise=True))
        if not opened:
            raise RuntimeError("the host browser did not accept the URL")
        tab = {
            "tab_id": "tab_" + uuid.uuid4().hex,
            "profile_id": profile["profile_id"],
            "url": raw_url,
            "opened_at": _now(),
        }
        state["tabs"] = [*state["tabs"], tab][-_MAX_TABS:]
        state["active_tab_id"] = tab["tab_id"]
        self._write_state(state)
        return {"action": "browser.open_url", "opened": True, **tab}

    def _tabs(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": "browser.tabs",
            "active_tab_id": state.get("active_tab_id"),
            "tabs": list(state["tabs"]),
        }

    def _select_tab(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        tab_id = str(payload.get("tab_id") or "").strip()
        if not any(item.get("tab_id") == tab_id for item in state["tabs"]):
            raise KeyError("browser tab is unavailable")
        state["active_tab_id"] = tab_id
        self._write_state(state)
        return {"action": "browser.select_tab", "active_tab_id": tab_id}

    def _downloads(self, state: dict[str, Any]) -> dict[str, Any]:
        managed = self.root / "downloads"
        downloads = []
        if managed.is_symlink():
            raise PermissionError("managed browser download directory cannot be a symlink")
        if managed.is_dir():
            for candidate in sorted(managed.iterdir(), key=lambda item: item.name):
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                downloads.append(
                    {
                        "download_id": candidate.name,
                        "name": candidate.name,
                        "size": candidate.stat().st_size,
                    }
                )
        return {"action": "browser.downloads.list", "downloads": downloads}

    def _collect_download(
        self,
        payload: Mapping[str, Any],
        artifact_root: Path | None,
    ) -> dict[str, Any]:
        if artifact_root is None:
            raise ValueError("a validated conversation artifact root is required")
        download_id = str(payload.get("download_id") or "").strip()
        if not download_id or Path(download_id).name != download_id:
            raise ValueError("download_id must be one managed filename")
        source = self.root / "downloads" / download_id
        if source.parent.is_symlink():
            raise PermissionError("managed browser download directory cannot be a symlink")
        if source.is_symlink() or not source.is_file():
            raise FileNotFoundError("managed browser download is unavailable")
        destination_root = Path(artifact_root)
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / download_id
        temporary = destination_root / f".{download_id}.{uuid.uuid4().hex}.tmp"
        source_fd = -1
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source, flags)
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise PermissionError("managed download is not a regular file")
            with os.fdopen(source_fd, "rb", closefd=True) as source_handle:
                source_fd = -1
                with temporary.open("xb") as destination_handle:
                    os.chmod(temporary, 0o600)
                    shutil.copyfileobj(source_handle, destination_handle, 1024 * 1024)
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
            os.replace(temporary, destination)
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return {
            "action": "browser.download.collect",
            "download_id": download_id,
            "path": str(destination),
            "size": destination.stat().st_size,
        }

    def _profile(
        self, state: dict[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        profile_id = _profile_id(payload.get("profile_id") or state["active_profile_id"])
        profile = state["profiles"].get(profile_id)
        if not isinstance(profile, dict):
            raise KeyError("browser profile is unavailable")
        return profile

    def _read_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("browser state is unavailable") from exc
        state = raw if isinstance(raw, dict) else {}
        default = {
            "profile_id": "default",
            "label": "Default",
            "created_at": 0,
            "cache_revision": 0,
        }
        profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
        profiles.setdefault("default", default)
        cookies = state.get("cookies") if isinstance(state.get("cookies"), dict) else {}
        cookies.setdefault("default", [])
        return {
            "version": 1,
            "session_id": str(state.get("session_id") or "session_" + uuid.uuid4().hex),
            "active_profile_id": str(state.get("active_profile_id") or "default"),
            "active_tab_id": state.get("active_tab_id"),
            "profiles": profiles,
            "cookies": cookies,
            "tabs": list(state.get("tabs") or [])[-_MAX_TABS:],
            "downloads": list(state.get("downloads") or []),
        }

    def _write_state(self, state: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".state.", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


def run_browser_host_action(
    action: str,
    payload: Mapping[str, Any] | None,
    *,
    viewer_host_approved: bool,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Run one Viewer-authorized browser action."""

    return BrowserHostRunner().run(
        action,
        payload,
        viewer_host_approved=viewer_host_approved,
        artifact_root=artifact_root,
    )


def _profile_id(value: Any) -> str:
    profile_id = str(value or "").strip()
    if not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError("browser profile id is invalid")
    return profile_id


def _cookie(value: Mapping[str, Any]) -> dict[str, Any]:
    name = str(value.get("name") or "").strip()
    domain = str(value.get("domain") or "").strip().lower()
    if not name or not domain or len(name) > 256 or len(domain) > 253:
        raise ValueError("cookie name and domain are required")
    return {
        "name": name,
        "value": str(value.get("value") or "")[:16_384],
        "domain": domain,
        "path": str(value.get("path") or "/")[:1024],
        "secure": bool(value.get("secure", True)),
        "http_only": bool(value.get("http_only", True)),
        "same_site": str(value.get("same_site") or "Lax")[:16],
        "expires_at": value.get("expires_at"),
    }


def _cookie_key(value: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(value.get("name") or ""),
        str(value.get("domain") or ""),
        str(value.get("path") or "/"),
    )


def _cookie_matches(cookie: Mapping[str, Any], query: Mapping[str, Any]) -> bool:
    for key in ("name", "domain", "path"):
        expected = str(query.get(key) or "").strip()
        if expected and str(cookie.get(key) or "") != expected:
            return False
    return True


def _now() -> int:
    return int(time.time())

