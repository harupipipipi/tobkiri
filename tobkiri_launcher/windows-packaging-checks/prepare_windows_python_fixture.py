"""Create a test-only Windows lease fixture from an existing uv CPython install.

This deliberately does not attest release provenance or publish a release. The
caller selects an already-installed official uv binary and managed CPython;
UV_OFFLINE prevents the fixture preparation from fetching another interpreter.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


def checked_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RuntimeError(f"Linked or special input: {path}")


def copy_tree(source: Path, destination: Path) -> None:
    info = source.lstat()
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RuntimeError(f"Linked or special directory: {source}")
    destination.mkdir()
    for child in sorted(source.iterdir()):
        info = child.lstat()
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError(f"Reparse input: {child}")
        target = destination / child.name
        if stat.S_ISDIR(info.st_mode):
            copy_tree(child, target)
        else:
            checked_file(child)
            with child.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--runtime-python", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("This fixture must run on native Windows")
    uv = args.uv.resolve(strict=True)
    python = args.runtime_python.resolve(strict=True)
    checked_file(uv)
    checked_file(python)
    if python.name.lower() != "python.exe":
        raise RuntimeError("Select the managed CPython python.exe")
    version = subprocess.check_output(
        [str(python), "-I", "-B", "-c", "import sys;print('.'.join(map(str,sys.version_info[:3])))"],
        text=True,
        timeout=30,
    ).strip()
    if version != args.expected_version:
        raise RuntimeError(f"Expected CPython {args.expected_version}, got {version}")
    output = args.output.absolute()
    output.mkdir(exist_ok=False)
    copy_tree(python.parent, output / "runtime")
    environment = dict(os.environ, UV_OFFLINE="1", UV_NO_PROGRESS="1")
    subprocess.run(
        [str(uv), "venv", str(output / "venv"), "--python", str(output / "runtime/python.exe"),
         "--relocatable", "--link-mode", "copy", "--no-project"],
        env=environment,
        check=True,
        timeout=120,
    )
    cfg = output / "venv/pyvenv.cfg"
    lines = []
    home = site = False
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key = line.partition("=")[0].strip().lower()
        if key == "home":
            line, home = "home = runtime", True
        elif key == "include-system-site-packages":
            line, site = "include-system-site-packages = false", True
        lines.append(line)
    if not home or not site:
        raise RuntimeError("uv did not create the expected venv configuration")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
    probe = output / "venv/Lib/site-packages/tobkiri_lease_smoke"
    probe.mkdir(parents=True, exist_ok=False)
    (probe / "__init__.py").write_text('"""Static test-only site-package probe."""\nVALUE = "leased"\n', encoding="utf-8")
    records = []
    for path in sorted(output.rglob("*")):
        info = path.lstat()
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError(f"uv created a reparse output: {path}")
        if path.is_dir():
            continue
        checked_file(path)
        if info.st_nlink != 1:
            raise RuntimeError(f"uv created a hardlinked output: {path}")
        data = path.read_bytes()
        records.append({"path": path.relative_to(output).as_posix(), "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "executable": path.suffix.lower() in {".exe", ".dll", ".pyd"}})
    records.sort(key=lambda item: item["path"])
    manifest = json.dumps({"schema": "io.tobkiri.sealed-python-environment.v1", "platform": "windows", "files": records}, separators=(",", ":")).encode()
    (output / "sealed-environment.v1.json").write_bytes(manifest)
    executable = output / "venv/Scripts/python.exe"
    report = {"test_only": True, "python_version": version,
              "TOBKIRI_PACKAGING_PYTHON": str(executable),
              "TOBKIRI_PACKAGING_PYTHON_SHA256": hashlib.sha256(executable.read_bytes()).hexdigest(),
              "TOBKIRI_PACKAGING_PYTHON_SNAPSHOT": str(output),
              "TOBKIRI_PACKAGING_PYTHON_INVENTORY_SHA256": hashlib.sha256(manifest).hexdigest()}
    print(json.dumps(report))


if __name__ == "__main__":
    main()
