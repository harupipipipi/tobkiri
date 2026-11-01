"""Capture verified inputs, build deterministic tars, run one pinned local worker.

No shell, Cargo, downloader, or upstream build script is invoked. This driver
DOES execute its reviewed compressor worker after verifying the supplied hash.
Run only with a reviewed local compressor in a trusted filesystem.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import subprocess
import tarfile
import tempfile
import time

MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_OUTPUT = 128 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024
MAX_RECORDS = 10000
STDERR_LIMIT = 8192
WORKER_TIMEOUT = 240
BINARY_ASSETS = {
    "libcomponentize_py_runtime_sync.so.zst", "libcomponentize_py_runtime_async.so.zst",
    "libpython3.13.so.zst", "libc.so.zst", "libwasi-emulated-mman.so.zst",
    "libwasi-emulated-process-clocks.so.zst", "libwasi-emulated-getpid.so.zst",
    "libwasi-emulated-signal.so.zst", "libc++.so.zst", "libc++abi.so.zst",
    "wasi_snapshot_preview1.reactor.wasm.zst",
}
TAR_ASSETS = {"python-lib.tar.zst", "bundled.tar.zst"}
ASSETS = BINARY_ASSETS | TAR_ASSETS


def digest(data: bytes) -> str:
    """Return SHA-256 of captured bytes."""
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> None:
    """Reject absolute, ambiguous, or escaping manifest paths."""
    if (not isinstance(name, str) or not name or "\\" in name
            or "\x00" in name or PurePosixPath(name).is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise ValueError("unsafe manifest path")


def read_file(path: Path, limit: int) -> bytes:
    """Read bounded regular data, rejecting all symlink path components."""
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"symlink input: {path}")
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"missing, nonregular, or oversized input: {path}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("input limit exceeded")
    return data


def capture(root: Path, manifest: bytes) -> dict[str, list[dict]]:
    """Capture every hash-verified input once, before creating any output."""
    document = json.loads(manifest)
    if document.get("schema_version") != 1:
        raise ValueError("unknown input manifest schema")
    records = document.get("files")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("invalid input record count")
    groups = {name: [] for name in ASSETS}
    seen_paths: set[str] = set()
    members: set[tuple[str, str | None]] = set()
    total = 0
    for record in records:
        asset, name = record["asset"], record["path"]
        if asset not in ASSETS:
            raise ValueError("unknown asset")
        safe_name(name)
        if name in seen_paths:
            raise ValueError("duplicate input path")
        seen_paths.add(name)
        member = record.get("member")
        if asset in TAR_ASSETS:
            safe_name(member)
            if (PurePosixPath(member).suffix in (".a", ".pyc", ".whl")
                    or PurePosixPath(member).name in (
                        "Makefile", "Changelog", "NEWS.txt"
                    )):
                raise ValueError("excluded upstream tar member")
        elif member is not None:
            raise ValueError("binary asset cannot have tar member")
        key = (asset, member)
        if key in members:
            raise ValueError("duplicate asset member")
        members.add(key)
        if record.get("mode", 0o644) not in (0o644, 0o755):
            raise ValueError("unsupported normalized mode")
        data = read_file(root / name, MAX_FILE)
        if digest(data) != record["sha256"]:
            raise ValueError(f"input digest mismatch: {name}")
        total += len(data)
        if total > MAX_TOTAL:
            raise ValueError("aggregate input limit exceeded")
        groups[asset].append({**record, "data": data})
    if any(not entries for entries in groups.values()):
        raise ValueError("all 13 real assets must be provided")
    sync = groups["libcomponentize_py_runtime_sync.so.zst"][0]["data"]
    async_ = groups["libcomponentize_py_runtime_async.so.zst"][0]["data"]
    if not sync or not async_ or sync == async_:
        raise ValueError("sync/async runtimes must be nonempty and distinct")
    for asset, entries in groups.items():
        if asset in BINARY_ASSETS and not entries[0]["data"].startswith(b"\0asm"):
            raise ValueError("binary asset is not Wasm")
        if asset in TAR_ASSETS:
            paths = {entry["member"] for entry in entries}
            for name in paths:
                if any(str(parent) in paths for parent in PurePosixPath(name).parents):
                    raise ValueError("tar file/directory collision")
    return groups


def tar_bytes(entries: list[dict]) -> bytes:
    """Create deterministic regular-file tar, preserving content and include rules."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for entry in sorted(entries, key=lambda item: item["member"]):
            info = tarfile.TarInfo(entry["member"])
            info.mode = entry.get("mode", 0o644)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            info.size = len(entry["data"])
            if output.tell() + info.size + 4096 > MAX_OUTPUT:
                raise ValueError("tar input exceeds worker bound")
            tar.addfile(info, io.BytesIO(entry["data"]))
    data = output.getvalue()
    if len(data) > MAX_OUTPUT:
        raise ValueError("tar input exceeds worker bound")
    return data


def compress(worker: Path, mode: str, data: bytes) -> bytes:
    """Use bounded nonblocking transport; kill the process group on any failure."""
    process = subprocess.Popen(
        [str(worker), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, shell=False, start_new_session=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )
    assert process.stdin and process.stdout and process.stderr
    streams = (process.stdin, process.stdout, process.stderr)
    output, errors = bytearray(), bytearray()
    offset = 0
    deadline = time.monotonic() + WORKER_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            for stream, events in ((process.stdin, selectors.EVENT_WRITE),
                                   (process.stdout, selectors.EVENT_READ),
                                   (process.stderr, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("compression worker timeout")
                for key, _ in selector.select(min(remaining, 0.2)):
                    stream = key.fileobj
                    if stream is process.stdin:
                        if offset == len(data):
                            selector.unregister(stream)
                            stream.close()
                        else:
                            try:
                                offset += os.write(
                                    stream.fileno(), data[offset:offset + 65536]
                                )
                            except BlockingIOError:
                                continue
                    else:
                        try:
                            chunk = os.read(stream.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        target = output if stream is process.stdout else errors
                        limit = MAX_OUTPUT if stream is process.stdout else STDERR_LIMIT
                        if len(target) + len(chunk) > limit:
                            raise ValueError("compression worker output limit exceeded")
                        target.extend(chunk)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if code != 0:
                raise ValueError(f"compression worker failed ({code}): "
                                 f"{errors.decode(errors='replace')}")
            if not output:
                raise ValueError("compression worker returned empty output")
            return bytes(output)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise
    finally:
        for stream in streams:
            stream.close()


def package(root: Path, manifest_path: Path, manifest_sha256: str,
            worker: Path, worker_sha256: str, destination: Path) -> dict:
    """Publish all assets only after successful verified input/output processing."""
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination must be new")
    if (not destination.parent.is_dir()
            or any(p.is_symlink() for p in destination.parents)):
        raise ValueError("trusted destination parent must exist")
    manifest = read_file(manifest_path, MAX_MANIFEST)
    if digest(manifest) != manifest_sha256:
        raise ValueError("input manifest digest mismatch")
    binary = read_file(worker, MAX_FILE)
    if digest(binary) != worker_sha256:
        raise ValueError("worker digest mismatch")
    # Trusted local filesystem contract: no concurrent worker replacement.
    groups = capture(root, manifest)
    partial = Path(tempfile.mkdtemp(prefix=f".{destination.name}.partial-",
                                    dir=destination.parent))
    receipts = {}
    output_total = 0
    try:
        for asset in sorted(ASSETS):
            if asset in TAR_ASSETS:
                payload, mode = tar_bytes(groups[asset]), "tar"
            else:
                payload = groups[asset][0]["data"]
                mode = "wasm-raw" if asset.endswith(".wasm.zst") else "wasm-strip"
            output = compress(worker.absolute(), mode, payload)
            if len(output) > MAX_OUTPUT:
                raise ValueError("compressed asset exceeds bound")
            if not output.startswith(b"\x28\xb5\x2f\xfd"):
                raise ValueError("worker output is not a zstd frame")
            output_total += len(output)
            if output_total > MAX_TOTAL:
                raise ValueError("aggregate compressed output limit exceeded")
            with (partial / asset).open("xb") as stream:
                stream.write(output)
            if read_file(partial / asset, MAX_OUTPUT) != output:
                raise ValueError("written artifact differs from captured output")
            receipts[asset] = {"sha256": digest(output), "bytes": len(output),
                               "mode": mode, "worker_input_sha256": digest(payload)}
        sync_output = receipts["libcomponentize_py_runtime_sync.so.zst"]["sha256"]
        async_output = receipts["libcomponentize_py_runtime_async.so.zst"]["sha256"]
        if sync_output == async_output:
            raise ValueError("sync/async packaged runtimes must remain distinct")
        receipt = {
            "schema_version": 1, "status": "packaged; not runtime validated",
            "input_manifest_sha256": manifest_sha256,
            "worker_sha256": worker_sha256,
            "tar_policy": "sorted regular members; uid/gid/mtime=0; mode0644/0755",
            "artifacts": receipts,
        }
        encoded = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
        (partial / "package-receipt.json").write_bytes(encoded)
        if destination.exists() or destination.is_symlink():
            raise ValueError("destination appeared during packaging")
        os.rename(partial, destination)
        return {"destination": str(destination), "receipt_sha256": digest(encoded),
                "assets": len(receipts)}
    except BaseException:
        failure = {"status": "failed; partial outputs are not published",
                   "artifacts_completed": receipts}
        try:
            (partial / "package-receipt.json").write_text(
                json.dumps(failure, indent=2) + "\n"
            )
            (partial / "FAILED.txt").write_text("Packaging failed.\n")
        except OSError:
            pass
        raise


def main() -> None:
    """Run only an explicitly pinned local worker with verified input manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--worker-sha256", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.input_root, args.manifest, args.manifest_sha256,
                             args.worker, args.worker_sha256, args.destination)))


if __name__ == "__main__":
    main()
