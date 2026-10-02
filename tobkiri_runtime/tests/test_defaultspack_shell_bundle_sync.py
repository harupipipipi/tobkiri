from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_MODULE = (
    ROOT
    / "tobkiri_runtime"
    / "ecosystem"
    / "defaultspack"
    / "webapp"
    / "scripts"
    / "shell-bundle-manifest.mjs"
)


def _run_node(expression: str, *, webapp_root: Path, ui_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            expression,
            str(webapp_root),
            str(ui_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _minimal_bundle(tmp_path: Path) -> tuple[Path, Path]:
    webapp_root = tmp_path / "webapp"
    ui_dir = tmp_path / "ui"
    (webapp_root / "src").mkdir(parents=True)
    (webapp_root / "public").mkdir()
    (webapp_root / "scripts").mkdir()
    ui_dir.mkdir()
    (webapp_root / "src/main.tsx").write_text("export const current = 'v1';\n")
    (ui_dir / "shell.html").write_text("<script src='/static/shell-app.js'></script>\n")
    for name in (
        "shell-app.css",
        "shell-app.js",
        "shell-defaultspack-app.js",
        "shell-rolldown-runtime.js",
        "shell-vendor.js",
    ):
        (ui_dir / name).write_text(f"{name}\n")
    return webapp_root, ui_dir


def test_shell_manifest_rejects_source_and_bundle_drift(tmp_path: Path) -> None:
    webapp_root, ui_dir = _minimal_bundle(tmp_path)
    module = json.dumps(str(MANIFEST_MODULE))
    write_expression = (
        f"import {{ writeShellBundleManifest }} from {module}; "
        "writeShellBundleManifest({webappRoot: process.argv[1], uiDir: process.argv[2]});"
    )
    verify_expression = (
        f"import {{ verifyShellBundleManifest }} from {module}; "
        "verifyShellBundleManifest({webappRoot: process.argv[1], uiDir: process.argv[2]});"
    )

    written = _run_node(write_expression, webapp_root=webapp_root, ui_dir=ui_dir)
    assert written.returncode == 0, written.stderr
    verified = _run_node(verify_expression, webapp_root=webapp_root, ui_dir=ui_dir)
    assert verified.returncode == 0, verified.stderr

    (webapp_root / "src/main.tsx").write_text("export const current = 'v2';\n")
    source_drift = _run_node(verify_expression, webapp_root=webapp_root, ui_dir=ui_dir)
    assert source_drift.returncode != 0
    assert "source/bundle drift" in source_drift.stderr

    (webapp_root / "src/main.tsx").write_text("export const current = 'v1';\n")
    (ui_dir / "shell-app.js").write_text("tampered\n")
    bundle_drift = _run_node(verify_expression, webapp_root=webapp_root, ui_dir=ui_dir)
    assert bundle_drift.returncode != 0
    assert "source/bundle drift" in bundle_drift.stderr
