from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


SCRIPTS = Path(__file__).resolve().parents[1]
FIXTURES = SCRIPTS / "tests" / "fixtures" / "artifact_integrity_vectors.json"


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INTEGRITY = _load("artifact_integrity")


def _materialize(root: Path, fixture: dict[str, object]) -> Path:
    artifact = root / str(fixture["root_name"])
    files = fixture["files"]
    assert isinstance(files, dict)
    for relative, contents in files.items():
        path = artifact if not relative else artifact / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(contents), encoding="utf-8", newline="")
    return artifact


def _catalog(
    *, platform: str, architecture: str, artifact_id: str, entrypoint: str
) -> dict[str, object]:
    provider = artifact_id.rsplit(".", 1)[0]
    return {
        "schema": "io.tobkiri.launcher.presentation-catalog.v1",
        "default_selection": {
            "base_pack_id": "fixture-base",
            "shell_provider_id": provider,
        },
        "shell_providers": [
            {
                "provider_id": provider,
                "artifact_variants": [
                    {
                        "artifact_id": artifact_id,
                        "variant": f"{platform}-{architecture}",
                        "platform": platform,
                        "architecture": architecture,
                        "entrypoint": entrypoint,
                        "prebuilt": True,
                        "production": True,
                        "development_command": None,
                    }
                ],
            }
        ],
    }


def test_python_vectors_match_the_canonical_contract() -> None:
    vectors = json.loads(FIXTURES.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="tobkiri-artifact-vectors-") as temp:
        root = Path(temp)
        for name, fixture in vectors.items():
            artifact = _materialize(root / name, fixture)
            assert INTEGRITY.artifact_digest_and_size(artifact) == (
                fixture["sha256"],
                fixture["size"],
            )


@pytest.mark.parametrize(
    ("fixture_name", "platform", "architecture", "artifact_id"),
    [
        (
            "linux-file",
            "linux",
            "x86_64",
            "shell.tauri.default.linux-x86_64",
        ),
        (
            "windows-exe",
            "windows",
            "x86_64",
            "shell.tauri.default.windows-x86_64",
        ),
    ],
)
def test_python_created_file_digest_and_size_match_cross_language_vector(
    fixture_name: str, platform: str, architecture: str, artifact_id: str
) -> None:
    vectors = json.loads(FIXTURES.read_text(encoding="utf-8"))
    fixture = vectors[fixture_name]
    with TemporaryDirectory(prefix=f"tobkiri-{fixture_name}-release-") as temp:
        root = Path(temp)
        source = _materialize(root / "source", fixture)
        source.chmod(0o755)
        catalog_path = root / "presentation_catalog.json"
        catalog_path.write_text(
            json.dumps(
                _catalog(
                    platform=platform,
                    architecture=architecture,
                    artifact_id=artifact_id,
                    entrypoint=str(fixture["root_name"]),
                )
            ),
            encoding="utf-8",
        )
        manifest_path = root / "shell-build-output.v4.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "io.tobkiri.shell.build-output.v4",
                    "artifact_id": artifact_id,
                    "artifact_path": os.fspath(source),
                    "platform": platform,
                    "architecture": architecture,
                    "build_profile": "release",
                    "source_identity": "test:artifact-integrity",
                    "source_revision": "a" * 40,
                }
            ),
            encoding="utf-8",
        )
        signing_key = root / "signing-key.raw"
        signing_key.write_bytes(bytes(range(32)))

        assert INTEGRITY.artifact_digest_and_size(source) == (
            fixture["sha256"],
            fixture["size"],
        )


def test_mac_app_directory_vector_remains_recursive_and_symlink_safe() -> None:
    vectors = json.loads(FIXTURES.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="tobkiri-macos-app-release-") as temp:
        artifact = _materialize(Path(temp), vectors["mac-app"])
        assert INTEGRITY.artifact_digest_and_size(artifact) == (
            vectors["mac-app"]["sha256"],
            vectors["mac-app"]["size"],
        )

        outside = Path(temp) / "outside"
        outside.write_bytes(b"outside")
        artifact.joinpath("Contents", "link").symlink_to(outside)
        with pytest.raises(RuntimeError, match="symlink"):
            INTEGRITY.artifact_digest_and_size(artifact)
