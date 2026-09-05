import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "tobkiri_launcher/src-tauri/tauri.conf.json"
SOURCE_SUFFIXES = set(
    ".rs .swift .ts .tsx .json .py .md .yml .yaml .toml .sh .mjs".split()
)
GENERATED_PANEL = "tobkiri_runtime/core_runtime/core_pack/core_control_panel/web/"
GENERATED_FILES = {
    "tobkiri_runtime/schemas/pack_v4_catalog.v1.json",
}
ALLOWED_DERIVED_IDENTIFIERS = {
    "dev.tobkiri.launcher.ci-e2e": frozenset(
        {
            ".github/schemas/macos-artifact-policy.v1.schema.json",
            ".github/schemas/macos-ci-e2e-attestation.v1.schema.json",
            ".github/scripts/macos_ci_artifact.py",
            ".github/workflows/desktop-installers.yml",
            "tobkiri_launcher/docs/MACOS_ARTIFACT_POLICY.md",
            "tobkiri_launcher/scripts/package_macos_dmg.sh",
            "tobkiri_launcher/scripts/verify_macos_launcher_cold_boot.py",
            "tobkiri_launcher/src-tauri/ci-e2e/ci-e2e-artifact-policy.v1.json",
            "tobkiri_launcher/src-tauri/src/ci_e2e_app_data.rs",
            "tobkiri_launcher/src-tauri/src/sealed_python.rs",
            "tobkiri_launcher/src-tauri/src/shell_handoff.rs",
            "tobkiri_launcher/src-tauri/tauri.macos.ci-e2e.conf.json",
        }
    ),
    "dev.tobkiri.local-launcher": frozenset(
        {
            "tobkiri_launcher/src-tauri/build.rs",
            "tobkiri_launcher/src-tauri/tauri.macos.dev.conf.json",
        }
    ),
    "dev.tobkiri.launcher.packvm-vz-helper": frozenset(
        {
            ".github/scripts/macos_ci_artifact.py",
            ".github/workflows/desktop-installers.yml",
            ".github/workflows/release.yml",
            "tobkiri_launcher/packvm-vz-helper/Sources/PackVMVZCore/LaunchAssets.swift",
            "tobkiri_launcher/packvm-vz-helper/Sources/PackVMVZCore/VZSupervisor.swift",
            "tobkiri_launcher/scripts/package_macos_dmg.sh",
            "tobkiri_runtime/ecosystem/defaultspack/backend/sandbox/isolation/"
            "macos_vz_provisioner.py",
        }
    ),
}


def _production_sources() -> dict[str, str]:
    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=ROOT, text=True
    ).splitlines()
    sources = {}
    for name in tracked:
        relative = Path(name)
        if (
            relative.suffix not in SOURCE_SUFFIXES
            or any(part.lower() == "tests" for part in relative.parts)
            or relative.name.startswith("test_")
            or ".test." in relative.name
            or "generated" in relative.parts
            or "evidence" in relative.parts
            or name.startswith(GENERATED_PANEL)
            or name in GENERATED_FILES
        ):
            continue
        payload = (ROOT / relative).read_bytes()
        assert len(payload) <= 512 * 1024, f"unbounded source: {name}"
        sources[name] = payload.decode("utf-8")
    # Hashed panel output is excluded; release builds regenerate it from scanned TS.
    return sources


def _collapsed(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "", text).lower()


def test_launcher_compatibility_identity_boundary() -> None:
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert config["productName"] == "Tobkiri Launcher"
    assert config["identifier"] == "dev.rumiai.app"
    fixtures = (
        "dev.tobkiri.launcher",
        '"dev.tobkiri." + "launcher"',
        '`dev.tobkiri.${"launcher"}`',
        'format!("dev.tobkiri.{}", "launcher")',
        *ALLOWED_DERIVED_IDENTIFIERS,
    )
    assert all("dev.tobkiri." in _collapsed(item) for item in fixtures)
    sources = _production_sources()
    # The published application keeps the compatibility identifier. CI and the
    # nested VZ helper use exact, non-published derivative identifiers.
    for name, text in sources.items():
        collapsed_text = _collapsed(text)
        for allowed, allowed_paths in ALLOWED_DERIVED_IDENTIFIERS.items():
            if name in allowed_paths:
                collapsed_text = collapsed_text.replace(_collapsed(allowed), "")
        assert _collapsed("dev.tobkiri.launcher") not in collapsed_text, name
        assert _collapsed("dev.tobikiri.") not in collapsed_text, name
    forbidden_migration = "|".join((
        "app_data_migration|.tobkiri-app-data-migration|.tobkiri-migration-complete",
        "migrate_legacy_app_data|copied legacy Rumi Viewer application data",
        "Tobkiri Launcher app identity migration",
        "legacy Rumi Viewer permissions are not copied",
    )).split("|")
    collapsed = {name: _collapsed(text) for name, text in sources.items()}
    assert not any(_collapsed(marker) in text for text in collapsed.values()
                   for marker in forbidden_migration)
    assert not (ROOT / "tobkiri_launcher/src-tauri/src/app_data_migration.rs").exists()
    assert not (ROOT / "docs/tobkiri-app-identity-migration.md").exists()
