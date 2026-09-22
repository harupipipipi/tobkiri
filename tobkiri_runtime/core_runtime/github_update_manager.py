"""GitHub-backed update manager for Rumi runtime and defaultspack.

The updater is intentionally conservative:
- it downloads GitHub Release source archives, so no custom update server is
  required;
- it overlays files instead of delete-syncing directories;
- runtime data directories are protected per target.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional

from .host_contract import host_contract_value


UpdateTarget = Literal["tobkiri", "defaultspack"]
UpdateTargetInput = Literal["tobkiri", "rumiai", "defaultspack"]

DEFAULT_REPO = "harupipipipi/rumiai"
DEFAULT_TIMEOUT = 20
MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
AUTO_UPDATE_INTERVAL_HOURS = 24
UPDATE_TARGETS: tuple[UpdateTarget, ...] = ("tobkiri", "defaultspack")


def normalize_update_target(target: str) -> UpdateTarget:
    if target == "rumiai":
        return "tobkiri"
    if target in UPDATE_TARGETS:
        return target  # type: ignore[return-value]
    raise GitHubUpdateError(f"unsupported update target: {target}")

_COMMON_EXCLUDES = (
    ".git/**",
    "**/.git/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "*.pyc",
    "**/*.pyc",
    ".DS_Store",
    "**/.DS_Store",
)

_PROTECTED_BY_TARGET: dict[UpdateTarget, tuple[str, ...]] = {
    "tobkiri": (
        "user_data",
        "user_data/**",
        "ecosystem",
        "ecosystem/**",
        "bundled",
        "bundled/**",
    ),
    "defaultspack": (
        "user_data",
        "user_data/**",
        "pack_backups",
        "pack_backups/**",
        "pack_staging",
        "pack_staging/**",
    ),
}


@dataclass(frozen=True)
class GitHubRelease:
    tag_name: str
    html_url: str
    zipball_url: str
    name: str = ""
    body: str = ""


@dataclass(frozen=True)
class UpdateCheck:
    target: UpdateTarget
    current_version: str
    latest_version: str
    update_available: bool
    release_url: str
    repo: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "update_available": self.update_available,
            "release_url": self.release_url,
            "repo": self.repo,
        }


@dataclass(frozen=True)
class FilePlan:
    add: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return len(self.add) + len(self.update)

    def to_dict(self) -> dict[str, Any]:
        return {
            "add": self.add,
            "update": self.update,
            "skip": self.skip,
            "changed_count": self.changed_count,
        }


@dataclass(frozen=True)
class UpdateApplyResult:
    target: UpdateTarget
    current_version: str
    latest_version: str
    release_url: str
    backup_dir: str
    applied_files: list[str]
    skipped_files: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "release_url": self.release_url,
            "backup_dir": self.backup_dir,
            "applied_files": self.applied_files,
            "skipped_files": self.skipped_files,
            "applied_count": len(self.applied_files),
            "skipped_count": len(self.skipped_files),
        }


@dataclass(frozen=True)
class AutoUpdateRunResult:
    enabled_targets: list[UpdateTarget]
    due: bool
    checked_at: str | None
    results: list[dict[str, Any]]
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled_targets": self.enabled_targets,
            "due": self.due,
            "checked_at": self.checked_at,
            "results": self.results,
            "skipped_reason": self.skipped_reason,
        }


class GitHubUpdateError(RuntimeError):
    """Raised when a GitHub update operation cannot continue."""


class GitHubUpdateManager:
    def __init__(
        self,
        base_dir: str | Path | None = None,
        repo: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent.parent
        self.repo = repo or os.environ.get("RUMI_UPDATE_REPO", DEFAULT_REPO)
        self.timeout = timeout
        self.user_data_dir = self.base_dir / "user_data"
        self.staging_dir = self.user_data_dir / "update_staging"
        self.backup_dir = self.user_data_dir / "update_backups"
        self.settings_path = self.user_data_dir / "settings" / "update_preferences.json"

    def check(self, target: UpdateTarget) -> UpdateCheck:
        return self.check_many([target])[0]

    def check_many(self, targets: list[UpdateTarget]) -> list[UpdateCheck]:
        release = self.fetch_latest_release()
        with tempfile.TemporaryDirectory(prefix="rumi-update-check-") as tmp:
            tmp_dir = Path(tmp)
            archive_path = tmp_dir / "source.zip"
            extract_dir = tmp_dir / "source"
            self.download_archive(release.zipball_url, archive_path)
            _safe_extract_zip(archive_path, extract_dir)

            checks: list[UpdateCheck] = []
            for target in targets:
                current_version = self.current_version(target)
                source_dir = self._source_dir_for_target(target, extract_dir)
                latest_version = self._source_version(target, source_dir) or _normalize_version(release.tag_name)
                checks.append(
                    UpdateCheck(
                        target=target,
                        current_version=current_version,
                        latest_version=latest_version,
                        update_available=_version_newer(latest_version, current_version),
                        release_url=release.html_url,
                        repo=self.repo,
                    )
                )
            return checks

    def apply(
        self,
        target: UpdateTarget,
        *,
        force: bool = False,
        release: GitHubRelease | None = None,
    ) -> UpdateApplyResult:
        release = release or self.fetch_latest_release()
        current_version = self.current_version(target)
        with tempfile.TemporaryDirectory(prefix=f"rumi-{target}-update-") as tmp:
            tmp_dir = Path(tmp)
            archive_path = tmp_dir / "source.zip"
            extract_dir = tmp_dir / "source"
            self.download_archive(release.zipball_url, archive_path)
            _safe_extract_zip(archive_path, extract_dir)

            source_dir = self._source_dir_for_target(target, extract_dir)
            latest_version = self._source_version(target, source_dir) or _normalize_version(release.tag_name)
            if not force and not _version_newer(latest_version, current_version):
                raise GitHubUpdateError(
                    f"{target} is already up to date ({current_version} >= {latest_version})"
                )
            dest_dir = self._dest_dir_for_target(target)
            if not dest_dir.exists():
                raise GitHubUpdateError(f"Update target is missing: {dest_dir}")

            plan = self.plan_overlay(target, source_dir, dest_dir)
            backup_dir = self._backup_target(target, dest_dir)
            applied = self._apply_overlay(source_dir, dest_dir, plan)

        self._audit(
            "github_update_applied",
            True,
            {
                "target": target,
                "repo": self.repo,
                "current_version": current_version,
                "latest_version": latest_version,
                "release_url": release.html_url,
                "backup_dir": str(backup_dir),
                "applied_count": len(applied),
                "skipped_count": len(plan.skip),
            },
        )
        return UpdateApplyResult(
            target=target,
            current_version=current_version,
            latest_version=latest_version,
            release_url=release.html_url,
            backup_dir=str(backup_dir),
            applied_files=applied,
            skipped_files=plan.skip,
        )

    def read_auto_update_settings(self) -> dict[str, Any]:
        data: Mapping[str, Any] = {}
        if self.settings_path.is_file():
            try:
                loaded = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, Mapping):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        return self._normalize_auto_update_settings(data)

    def set_auto_update_settings(self, auto_update: Mapping[str, Any]) -> dict[str, Any]:
        current = self.read_auto_update_settings()
        next_auto = dict(current["auto_update"])
        for target in UPDATE_TARGETS:
            if target in auto_update:
                next_auto[target] = bool(auto_update[target])

        updated = {
            **current,
            "auto_update": next_auto,
            "updated_at": _utc_now_iso(),
        }
        self._write_auto_update_settings(updated)
        return self.read_auto_update_settings()

    def run_auto_updates_once(self, *, force: bool = False) -> AutoUpdateRunResult:
        settings = self.read_auto_update_settings()
        enabled_targets = [
            target for target in UPDATE_TARGETS
            if settings["auto_update"].get(target) is True
        ]
        if not enabled_targets:
            return AutoUpdateRunResult(
                enabled_targets=[],
                due=False,
                checked_at=settings.get("last_checked_at"),
                results=list(settings.get("last_results") or []),
                skipped_reason="disabled",
            )

        if not force and not self._auto_update_due(settings):
            return AutoUpdateRunResult(
                enabled_targets=enabled_targets,
                due=False,
                checked_at=settings.get("last_checked_at"),
                results=list(settings.get("last_results") or []),
                skipped_reason="interval",
            )

        checked_at = _utc_now_iso()
        results: list[dict[str, Any]] = []
        try:
            checks = self.check_many(enabled_targets)
        except Exception as exc:
            results.append({"target": None, "status": "error", "error": str(exc)})
        else:
            for check in checks:
                if not check.update_available:
                    results.append({
                        "target": check.target,
                        "status": "up_to_date",
                        "current_version": check.current_version,
                        "latest_version": check.latest_version,
                    })
                    continue
                try:
                    applied = self.apply(check.target)
                    results.append({
                        "target": check.target,
                        "status": "applied",
                        "current_version": applied.current_version,
                        "latest_version": applied.latest_version,
                        "backup_dir": applied.backup_dir,
                        "applied_count": len(applied.applied_files),
                        "restart_required": check.target == "tobkiri",
                        "routes_reload_recommended": check.target == "defaultspack",
                    })
                except Exception as exc:
                    results.append({
                        "target": check.target,
                        "status": "error",
                        "current_version": check.current_version,
                        "latest_version": check.latest_version,
                        "error": str(exc),
                    })

        updated = {
            **settings,
            "last_checked_at": checked_at,
            "last_results": results,
        }
        self._write_auto_update_settings(updated)
        return AutoUpdateRunResult(
            enabled_targets=enabled_targets,
            due=True,
            checked_at=checked_at,
            results=results,
        )

    def fetch_latest_release(self) -> GitHubRelease:
        url = f"https://api.github.com/repos/{self.repo}/releases/latest"
        data = self._read_json(url)
        try:
            return GitHubRelease(
                tag_name=str(data["tag_name"]),
                html_url=str(data["html_url"]),
                zipball_url=str(data["zipball_url"]),
                name=str(data.get("name") or ""),
                body=str(data.get("body") or ""),
            )
        except KeyError as exc:
            raise GitHubUpdateError(f"GitHub release response missing {exc}") from exc

    def download_archive(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        request = self._request(url)
        total = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response, open(dest, "wb") as f:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise GitHubUpdateError("GitHub archive is too large")
                    f.write(chunk)
        except urllib.error.URLError as exc:
            raise GitHubUpdateError(f"failed to download GitHub archive: {exc}") from exc

    def current_version(self, target: UpdateTarget) -> str:
        if target == "tobkiri":
            return _read_pyproject_version(self.base_dir / "pyproject.toml") or "0.0.0"
        if target == "defaultspack":
            return _read_ecosystem_version(self.base_dir / "ecosystem" / "defaultspack" / "ecosystem.json") or "0.0.0"
        raise GitHubUpdateError(f"unsupported update target: {target}")

    def plan_overlay(self, target: UpdateTarget, source_dir: Path, dest_dir: Path) -> FilePlan:
        add: list[str] = []
        update: list[str] = []
        skip: list[str] = []
        for file_path in _iter_files(source_dir):
            rel = _rel_posix(file_path, source_dir)
            if self._is_protected(target, rel):
                skip.append(rel)
                continue
            dest_file = dest_dir / rel
            if dest_file.exists():
                update.append(rel)
            else:
                add.append(rel)
        return FilePlan(add=add, update=update, skip=skip)

    def _read_json(self, url: str) -> dict[str, Any]:
        request = self._request(url)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise GitHubUpdateError(f"failed to read GitHub release metadata: {exc}") from exc

    def _request(self, url: str) -> urllib.request.Request:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"tobkiri-updater/{self.current_version('tobkiri')}",
        }
        token = host_contract_value("github_update_token", provider_id="github")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return urllib.request.Request(url, headers=headers)

    def _normalize_auto_update_settings(self, data: Mapping[str, Any]) -> dict[str, Any]:
        raw_auto = data.get("auto_update")
        if not isinstance(raw_auto, Mapping):
            raw_auto = {}
        auto_update = {
            target: bool(raw_auto.get(target, False))
            for target in UPDATE_TARGETS
        }
        last_results = data.get("last_results")
        if not isinstance(last_results, list):
            last_results = []
        return {
            "auto_update": auto_update,
            "check_interval_hours": AUTO_UPDATE_INTERVAL_HOURS,
            "last_checked_at": data.get("last_checked_at") if isinstance(data.get("last_checked_at"), str) else None,
            "last_results": last_results,
            "updated_at": data.get("updated_at") if isinstance(data.get("updated_at"), str) else None,
        }

    def _write_auto_update_settings(self, settings: Mapping[str, Any]) -> None:
        normalized = self._normalize_auto_update_settings(settings)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.settings_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self.settings_path)

    @staticmethod
    def _auto_update_due(settings: Mapping[str, Any]) -> bool:
        last_checked_at = settings.get("last_checked_at")
        if not isinstance(last_checked_at, str) or not last_checked_at:
            return True
        try:
            last_checked = datetime.fromisoformat(last_checked_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        elapsed = datetime.now(timezone.utc) - last_checked
        return elapsed.total_seconds() >= AUTO_UPDATE_INTERVAL_HOURS * 60 * 60

    def _source_dir_for_target(self, target: UpdateTarget, extracted_root: Path) -> Path:
        if target == "tobkiri":
            source = _find_child_with(extracted_root, Path("tobkiri_runtime") / "app.py")
            if source is None:
                raise GitHubUpdateError("source archive does not contain tobkiri_runtime/app.py")
            return source / "tobkiri_runtime"
        if target == "defaultspack":
            marker = Path("tobkiri_runtime") / "ecosystem" / "defaultspack" / "ecosystem.json"
            source = _find_child_with(extracted_root, marker)
            if source is None:
                raise GitHubUpdateError("source archive does not contain defaultspack")
            return source / "tobkiri_runtime" / "ecosystem" / "defaultspack"
        raise GitHubUpdateError(f"unsupported update target: {target}")

    def _dest_dir_for_target(self, target: UpdateTarget) -> Path:
        if target == "tobkiri":
            return self.base_dir
        if target == "defaultspack":
            return self.base_dir / "ecosystem" / "defaultspack"
        raise GitHubUpdateError(f"unsupported update target: {target}")

    @staticmethod
    def _source_version(target: UpdateTarget, source_dir: Path) -> str | None:
        if target == "tobkiri":
            return _read_pyproject_version(source_dir / "pyproject.toml")
        if target == "defaultspack":
            return _read_ecosystem_version(source_dir / "ecosystem.json")
        return None

    def _is_protected(self, target: UpdateTarget, rel: str) -> bool:
        patterns = _COMMON_EXCLUDES + _PROTECTED_BY_TARGET[target]
        return any(fnmatch.fnmatch(rel, pattern) for pattern in patterns)

    def _backup_target(self, target: UpdateTarget, dest_dir: Path) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self.backup_dir / target / ts
        if backup_dir.exists():
            backup_dir = backup_dir.with_name(f"{backup_dir.name}-{os.getpid()}")
        backup_dir.parent.mkdir(parents=True, exist_ok=True)

        root = dest_dir.resolve()

        def ignore(current_dir: str, names: list[str]) -> set[str]:
            ignored: set[str] = set()
            current = Path(current_dir).resolve()
            for name in names:
                try:
                    rel = (current / name).relative_to(root).as_posix()
                except ValueError:
                    rel = name
                if self._is_protected(target, rel):
                    ignored.add(name)
            return ignored

        shutil.copytree(dest_dir, backup_dir, symlinks=False, ignore=ignore)
        return backup_dir

    def _apply_overlay(self, source_dir: Path, dest_dir: Path, plan: FilePlan) -> list[str]:
        applied: list[str] = []
        for rel in plan.add + plan.update:
            src = source_dir / rel
            dst = dest_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            applied.append(rel)
        return applied

    @staticmethod
    def _audit(event_type: str, success: bool, details: dict[str, Any]) -> None:
        try:
            from .audit_logger import get_audit_logger

            get_audit_logger().log_system_event(
                event_type=event_type,
                success=success,
                details=details,
                error=str(details.get("error") or ""),
            )
        except Exception:
            pass


def _iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            yield path


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_extract_zip(archive_path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(archive_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith(("/", "\\")) or "\x00" in name:
                raise GitHubUpdateError(f"unsafe archive entry: {name!r}")
            parts = name.replace("\\", "/").split("/")
            if ".." in parts:
                raise GitHubUpdateError(f"path traversal in archive entry: {name!r}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise GitHubUpdateError(f"symlink in archive rejected: {name!r}")

            target = dest / name
            target_resolved = target.resolve()
            try:
                target_resolved.relative_to(dest_resolved)
            except ValueError as exc:
                raise GitHubUpdateError(f"archive entry escapes destination: {name!r}") from exc

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def _find_child_with(root: Path, marker: Path) -> Optional[Path]:
    for child in root.iterdir():
        if child.is_dir() and (child / marker).exists():
            return child
    return None


def _read_pyproject_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"').strip("'")
    return None


def _read_ecosystem_version(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    version = data.get("version")
    return str(version) if version else None


def _normalize_version(version: str) -> str:
    return version.strip().removeprefix("v")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _version_newer(latest: str, current: str) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        clean = _normalize_version(value).split("-", 1)[0]
        parsed: list[int] = []
        for item in clean.split("."):
            try:
                parsed.append(int(item))
            except ValueError:
                parsed.append(0)
        return tuple(parsed)

    return parts(latest) > parts(current)


_global_update_manager: GitHubUpdateManager | None = None


def get_github_update_manager() -> GitHubUpdateManager:
    global _global_update_manager
    if _global_update_manager is None:
        _global_update_manager = GitHubUpdateManager()
    return _global_update_manager


def reset_github_update_manager(**kwargs: Any) -> GitHubUpdateManager:
    global _global_update_manager
    _global_update_manager = GitHubUpdateManager(**kwargs)
    return _global_update_manager
