"""Restore the recorded Rust version as verified inputs, without running it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tomllib
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from build_native import fingerprint
from fetch_registry import capture
from fetch_sdk import extract, inventory

VERSION = "1.94.0"
DATE = "2026-03-05"
HOST = "x86_64-unknown-linux-gnu"
TARGET = "wasm32-wasip2"
MANIFEST_URL = f"https://static.rust-lang.org/dist/channel-rust-{VERSION}.toml"
MANIFEST_SHA256 = "aaa177def36e01d539bee6bde95295230b9ce378f81057845db8d0ebe97898ee"
INPUTS = (
    ("rustc", HOST, "rustc",
     "31a0d3ac9383dfdeb4fce86eeed5ade3230131c635264c0eab7252dbf235f28e"),
    ("cargo", HOST, "cargo",
     "8e17624f3de39e079845bfb25ed15a042f4b50ceca78e37c56c4b9b15949b9f7"),
    ("rust-std", HOST, f"rust-std-{HOST}",
     "dd33653107c36e040082050d9e547e64dac5b456ba74069430d838c00c189a05"),
    ("rust-std", TARGET, f"rust-std-{TARGET}",
     "e947e730da3fea212453b2835765802d9b0c80e19405cab188a6854834196e6c"),
)
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_DOWNLOADS = 512 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 ** 3
DISK_FLOOR = 8 * 1024 ** 3


def download(url: str, path: Path, digest: str, maximum: int) -> int:
    """Download only bounded hash-bound bytes; never invoke an install script."""
    request = Request(url, headers={"User-Agent": "Tobkiri-source-replay"})
    with urlopen(request, timeout=60) as response, path.open("xb") as output:
        if urlsplit(response.geturl()).scheme != "https":
            raise ValueError("Rust input redirect must remain HTTPS")
        return capture(response, output, digest, maximum)


def selected_inputs(manifest: bytes) -> list[dict]:
    """Require each selected archive to agree with the pinned release manifest."""
    if len(manifest) > 1024 * 1024 or hashlib.sha256(manifest).hexdigest() != MANIFEST_SHA256:
        raise ValueError("Rust release manifest identity mismatch")
    data = tomllib.loads(manifest.decode("utf-8"))
    if data["date"] != DATE:
        raise ValueError("Rust release date mismatch")
    inputs = []
    for package, target, component, digest in INPUTS:
        prefix = f"{package}-{VERSION}-{target}"
        url = f"https://static.rust-lang.org/dist/{DATE}/{prefix}.tar.xz"
        recorded = data["pkg"][package]["target"][target]
        if (recorded["available"] is not True or recorded["xz_url"] != url
                or recorded["xz_hash"] != digest):
            raise ValueError("Rust component differs from recorded release")
        inputs.append({"prefix": prefix, "url": url, "component": component,
                       "sha256": digest, "package": package, "target": target})
    return inputs


def merge_component(component: Path, destination: Path) -> None:
    """Assemble verified component files without executing upstream installers."""
    if not component.is_dir() or component.resolve() != component:
        raise ValueError("physical Rust component directory required")
    if not destination.is_dir() or destination.resolve() != destination:
        raise ValueError("physical Rust destination required")
    entries = inventory(component)
    if "manifest.in" not in entries or "link" in entries["manifest.in"]:
        raise ValueError("Rust component has no regular installation inventory")
    links = []
    for name, entry in entries.items():
        if name == "manifest.in":
            continue
        original, output = component / name, destination / name
        # Never traverse an earlier component's symlink when merging inputs.
        parent = destination
        for part in Path(name).parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise ValueError("Rust component merge traverses a link")
            parent.mkdir(exist_ok=True)
        if "link" in entry:
            original.resolve(strict=True).relative_to(component)
            links.append((output, entry["link"]))
        elif output.exists() or output.is_symlink():
            if (not stat.S_ISREG(output.lstat().st_mode)
                    or fingerprint(output) != {k: entry[k] for k in ("bytes", "sha256")}
                    or oct(stat.S_IMODE(output.stat().st_mode)) != entry["mode"]):
                raise ValueError("Rust component output collision")
        else:
            # These remain immutable inputs. Hardlinking avoids a second large
            # copy; later sandbox stages mount the assembled tree read-only.
            os.link(original, output, follow_symlinks=False)
    for output, target in links:
        if output.exists() or output.is_symlink():
            if not output.is_symlink() or os.readlink(output) != target:
                raise ValueError("Rust component link collision")
        else:
            output.symlink_to(target)
        output.resolve(strict=True).relative_to(destination)


def acquire(destination: Path) -> None:
    """Build the fresh toolchain tree only on a resource-checked remote worker."""
    if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("Rust input restoration is restricted to Linux CI")
    if (not destination.is_absolute() or not destination.parent.is_dir()
            or destination.parent.resolve() != destination.parent):
        raise ValueError("new absolute destination under physical parent required")
    if shutil.disk_usage(destination.parent).free < DISK_FLOOR + MAX_EXPANDED + MAX_DOWNLOADS:
        raise ValueError("Rust input acquisition disk floor not met")
    destination.mkdir(mode=0o700)
    manifest = destination / "release.toml"
    download(MANIFEST_URL, manifest, MANIFEST_SHA256, 1024 * 1024)
    inputs = selected_inputs(manifest.read_bytes())
    toolchain = destination / "toolchain"
    toolchain.mkdir(mode=0o700)
    downloaded, expanded = 0, 0
    receipt = {"format": "tobkiri.python313-rust-inputs/1", "outcome": "pending",
               "version": VERSION, "host": HOST, "target": TARGET,
               "release_manifest": fingerprint(manifest), "inputs": [],
               "tool_execution": False, "installer_execution": False,
               "production_enabled": False}
    try:
        for entry in inputs:
            archive = destination / (entry["prefix"] + ".tar.xz")
            downloaded += download(entry["url"], archive, entry["sha256"],
                                   min(MAX_ARCHIVE, MAX_DOWNLOADS - downloaded))
            root = destination / entry["prefix"]
            entries = extract(archive, root, prefix=entry["prefix"],
                              max_expanded=MAX_EXPANDED - expanded)
            expanded += sum(item.get("bytes", 0) for item in entries.values())
            if expanded > MAX_EXPANDED:
                raise ValueError("combined Rust input expansion bound exceeded")
            merge_component(root / entry["component"], toolchain)
            receipt["inputs"].append({**entry, "archive": fingerprint(archive, MAX_ARCHIVE),
                                      "extracted_files": len(entries)})
        files = inventory(toolchain)
        for required in ("bin/cargo", "bin/rustc", "bin/rustdoc"):
            if required not in files or "link" in files[required]:
                raise ValueError("missing Rust executable input")
        for target in (HOST, TARGET):
            if not any(name.startswith(f"lib/rustlib/{target}/lib/") for name in files):
                raise ValueError("missing Rust standard-library target")
        receipt.update(files=files, outcome="success")
    except BaseException:
        receipt["outcome"] = "failed"
        raise
    finally:
        receipt.update(downloaded_bytes=downloaded, expanded_bytes=expanded)
        (destination / "rust-manifest.json").write_text(
            json.dumps(receipt, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    acquire(parser.parse_args().destination)
