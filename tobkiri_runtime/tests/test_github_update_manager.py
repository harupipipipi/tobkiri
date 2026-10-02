from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from core_runtime.github_update_manager import (
    GitHubRelease,
    GitHubUpdateError,
    GitHubUpdateManager,
)


PACK_TARGET = "contribution_pack"


def _update_target_descriptors() -> list[dict[str, object]]:
    return [
        {
            "schema": "io.tobkiri.update-target.v1",
            "target": "tobkiri",
            "source_root": "tobkiri_runtime",
            "destination_root": ".",
            "version_path": "pyproject.toml",
            "version_format": "pyproject",
            "protected_paths": [
                "user_data",
                "user_data/**",
                "ecosystem",
                "ecosystem/**",
                "bundled",
                "bundled/**",
            ],
            "restart_required": True,
        },
        {
            "schema": "io.tobkiri.update-target.v1",
            "target": PACK_TARGET,
            "source_root": f"tobkiri_runtime/ecosystem/{PACK_TARGET}",
            "destination_root": f"ecosystem/{PACK_TARGET}",
            "version_path": "ecosystem.json",
            "version_format": "json",
            "protected_paths": [
                "user_data",
                "user_data/**",
                "pack_backups",
                "pack_backups/**",
                "pack_staging",
                "pack_staging/**",
            ],
            "runtime_reload_recommended": True,
        },
    ]


class LocalArchiveUpdateManager(GitHubUpdateManager):
    def __init__(self, *, base_dir: Path, archive_path: Path) -> None:
        super().__init__(
            base_dir=base_dir,
            repo="local/rumiai",
            timeout=1,
            update_target_descriptors=_update_target_descriptors(),
        )
        self.archive_path = archive_path

    def fetch_latest_release(self) -> GitHubRelease:
        return GitHubRelease(
            tag_name="v0.0.1",
            html_url="https://github.example/releases/v0.0.1",
            zipball_url="local://source.zip",
        )

    def download_archive(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.archive_path, dest)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_local_install(root: Path) -> Path:
    base = root / "tobkiri_runtime"
    base.mkdir()
    (base / "app.py").write_text("old app", encoding="utf-8")
    (base / "pyproject.toml").write_text(
        '[project]\nname = "rumi-ai"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    (base / "user_data").mkdir()
    (base / "user_data" / "secret.txt").write_text("keep me", encoding="utf-8")
    _write_json(
        base / "ecosystem" / PACK_TARGET / "ecosystem.json",
        {"pack_id": PACK_TARGET, "version": "2.0.0", "metadata": {"name": "Contribution"}},
    )
    (base / "ecosystem" / PACK_TARGET / "local_only.py").write_text("local", encoding="utf-8")
    (base / "ecosystem" / PACK_TARGET / "user_data").mkdir()
    (base / "ecosystem" / PACK_TARGET / "user_data" / "chat.json").write_text(
        "keep pack data",
        encoding="utf-8",
    )
    (base / "ecosystem" / "custompack").mkdir()
    (base / "ecosystem" / "custompack" / "keep.txt").write_text("custom", encoding="utf-8")
    return base


def _make_source_archive(tmp_path: Path) -> Path:
    src = tmp_path / "archive_src" / "repo-main" / "tobkiri_runtime"
    src.mkdir(parents=True)
    (src / "app.py").write_text("new app", encoding="utf-8")
    (src / "pyproject.toml").write_text(
        '[project]\nname = "rumi-ai"\nversion = "1.1.0"\n',
        encoding="utf-8",
    )
    (src / "user_data").mkdir()
    (src / "user_data" / "secret.txt").write_text("do not copy", encoding="utf-8")
    _write_json(
        src / "ecosystem" / PACK_TARGET / "ecosystem.json",
        {"pack_id": PACK_TARGET, "version": "2.1.0", "metadata": {"name": "Contribution"}},
    )
    (src / "ecosystem" / PACK_TARGET / "new_module.py").write_text("new", encoding="utf-8")
    (src / "ecosystem" / PACK_TARGET / "user_data").mkdir()
    (src / "ecosystem" / PACK_TARGET / "user_data" / "chat.json").write_text(
        "do not copy pack data",
        encoding="utf-8",
    )

    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        root = tmp_path / "archive_src"
        for path in root.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
    return archive


def test_check_many_reads_target_versions_from_source_archive(tmp_path: Path) -> None:
    base = _make_local_install(tmp_path)
    archive = _make_source_archive(tmp_path)
    manager = LocalArchiveUpdateManager(base_dir=base, archive_path=archive)

    checks = {item.target: item for item in manager.check_many(["tobkiri", PACK_TARGET])}

    assert checks["tobkiri"].current_version == "1.0.0"
    assert checks["tobkiri"].latest_version == "1.1.0"
    assert checks["tobkiri"].update_available is True
    assert checks[PACK_TARGET].current_version == "2.0.0"
    assert checks[PACK_TARGET].latest_version == "2.1.0"
    assert checks[PACK_TARGET].update_available is True


def test_rumiai_update_protects_user_data_and_ecosystem(tmp_path: Path) -> None:
    base = _make_local_install(tmp_path)
    archive = _make_source_archive(tmp_path)
    manager = LocalArchiveUpdateManager(base_dir=base, archive_path=archive)

    result = manager.apply("tobkiri")

    assert (base / "app.py").read_text(encoding="utf-8") == "new app"
    assert "version = \"1.1.0\"" in (base / "pyproject.toml").read_text(encoding="utf-8")
    assert (base / "user_data" / "secret.txt").read_text(encoding="utf-8") == "keep me"
    assert (base / "ecosystem" / "custompack" / "keep.txt").read_text(encoding="utf-8") == "custom"
    assert not (base / "ecosystem" / PACK_TARGET / "new_module.py").exists()
    assert set(result.applied_files) == {"app.py", "pyproject.toml"}
    assert f"ecosystem/{PACK_TARGET}/ecosystem.json" in result.skipped_files


def test_contributed_pack_update_is_separate_and_preserves_pack_user_data(tmp_path: Path) -> None:
    base = _make_local_install(tmp_path)
    archive = _make_source_archive(tmp_path)
    manager = LocalArchiveUpdateManager(base_dir=base, archive_path=archive)

    result = manager.apply(PACK_TARGET)
    pack_dir = base / "ecosystem" / PACK_TARGET

    updated_manifest = json.loads((pack_dir / "ecosystem.json").read_text(encoding="utf-8"))
    assert updated_manifest["version"] == "2.1.0"
    assert (pack_dir / "new_module.py").read_text(encoding="utf-8") == "new"
    assert (pack_dir / "local_only.py").read_text(encoding="utf-8") == "local"
    assert (pack_dir / "user_data" / "chat.json").read_text(encoding="utf-8") == "keep pack data"
    assert not (Path(result.backup_dir) / "user_data").exists()
    assert "user_data/chat.json" in result.skipped_files


def test_auto_update_settings_are_off_by_default_and_persist(tmp_path: Path) -> None:
    base = _make_local_install(tmp_path)
    archive = _make_source_archive(tmp_path)
    manager = LocalArchiveUpdateManager(base_dir=base, archive_path=archive)

    settings = manager.read_auto_update_settings()
    assert settings["auto_update"] == {"tobkiri": False, PACK_TARGET: False}

    updated = manager.set_auto_update_settings({PACK_TARGET: True})
    assert updated["auto_update"] == {"tobkiri": False, PACK_TARGET: True}

    manager2 = LocalArchiveUpdateManager(base_dir=base, archive_path=archive)
    assert manager2.read_auto_update_settings()["auto_update"][PACK_TARGET] is True


def test_auto_update_applies_only_enabled_target(tmp_path: Path) -> None:
    base = _make_local_install(tmp_path)
    archive = _make_source_archive(tmp_path)
    manager = LocalArchiveUpdateManager(base_dir=base, archive_path=archive)
    manager.set_auto_update_settings({PACK_TARGET: True, "tobkiri": False})

    result = manager.run_auto_updates_once(force=True)

    assert result.due is True
    assert result.enabled_targets == [PACK_TARGET]
    assert result.results[0]["target"] == PACK_TARGET
    assert result.results[0]["status"] == "applied"
    assert (base / "app.py").read_text(encoding="utf-8") == "old app"
    updated_manifest = json.loads(
        (base / "ecosystem" / PACK_TARGET / "ecosystem.json").read_text(encoding="utf-8")
    )
    assert updated_manifest["version"] == "2.1.0"
    assert (base / "user_data" / "secret.txt").read_text(encoding="utf-8") == "keep me"


def test_update_manager_rejects_an_uncontributed_target(tmp_path: Path) -> None:
    base = _make_local_install(tmp_path)
    archive = _make_source_archive(tmp_path)
    manager = LocalArchiveUpdateManager(base_dir=base, archive_path=archive)

    with pytest.raises(GitHubUpdateError, match="unsupported update target"):
        manager.current_version("uncontributed_pack")
