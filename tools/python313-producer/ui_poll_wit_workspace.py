"""Bound the two derived WIT directories written by unchanged upstream CMake.

These are copied build inputs verified before and after execution, not source
files claimed to remain immutable inside a writable directory mount.
"""

from pathlib import Path
import shutil
import stat

from build_native import fingerprint

VERSIONS = ("p2", "p3")
MAX_ENTRIES = 20_000
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
OUTPUT = "wasi-libc.wit"
TEMPORARY = OUTPUT + ".tmp"


def inventory(root: Path) -> dict:
    """Inventory physical directories and regular bytes without following links."""
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError("physical absolute WIT workspace required")
    entries = {}
    total = 0
    pending = [root]
    while pending:
        path = pending.pop()
        metadata = path.lstat()
        name = path.relative_to(root).as_posix()
        mode = oct(stat.S_IMODE(metadata.st_mode))
        if stat.S_ISDIR(metadata.st_mode):
            entries[name] = {"kind": "directory", "mode": mode}
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            total += metadata.st_size
            if total > MAX_TOTAL:
                raise ValueError("WIT workspace byte bound exceeded")
            entries[name] = {
                "kind": "file",
                "mode": mode,
                **fingerprint(path, MAX_FILE),
            }
        else:
            raise ValueError("WIT links and special entries are forbidden")
        if len(entries) + len(pending) > MAX_ENTRIES:
            raise ValueError("WIT workspace entry bound exceeded")
    return dict(sorted(entries.items()))


def prepare(candidate: Path, work: Path) -> dict:
    """Copy only the verified p2/p3 WIT inputs into a fresh private workspace."""
    for path in (candidate, work):
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            raise ValueError("physical candidate and work directories required")
    parent = work / "derived-wit"
    parent.mkdir(mode=0o700)
    records = {}
    for version in VERSIONS:
        relative = f"wasi/{version}/wit"
        source = candidate / relative
        before = inventory(source)
        if (
            "wasi-libc.wit.in" not in before
            or before["wasi-libc.wit.in"]["kind"] != "file"
            or TEMPORARY in before
        ):
            raise ValueError("original WIT template or clean directory missing")
        copied = parent / version
        shutil.copytree(source, copied, symlinks=False)
        if inventory(copied) != before:
            raise ValueError("copied WIT build inputs differ from verified source")
        records[version] = {
            "source_relative": relative,
            "workspace_relative": f"derived-wit/{version}",
            "before": before,
        }
    return records


def mounts(work: Path) -> list[str]:
    """Return only the two writable overlays, after their source read-only mount."""
    arguments = []
    for version in VERSIONS:
        path = work / "derived-wit" / version
        inventory(path)
        arguments.extend(["--bind", str(path), f"/libc-source/wasi/{version}/wit"])
    return arguments


def validate(before: dict, after: dict, *, successful: bool) -> None:
    """Reject changed dependencies, path/type/mode changes and success temporaries."""
    allowed = {OUTPUT} if successful else {OUTPUT, TEMPORARY}
    if any(name not in before and name not in allowed for name in after):
        raise ValueError("unlisted derived WIT entry")
    for name, entry in before.items():
        if name == OUTPUT:
            if name not in after or after[name]["kind"] != "file":
                raise ValueError("generated WIT changed type or disappeared")
            if entry["mode"] != after[name]["mode"]:
                raise ValueError("generated WIT mode changed")
        elif after.get(name) != entry:
            raise ValueError("copied WIT input changed")
    for name in allowed & after.keys():
        if after[name]["kind"] != "file":
            raise ValueError("generated WIT output is not a regular file")
    if successful and (OUTPUT not in after or TEMPORARY in after):
        raise ValueError("generated WIT missing or temporary remains")


def observe(work: Path, records: dict, *, successful: bool) -> dict:
    """Retain full post-build inventories and validate each derived WIT view."""
    observed = {}
    for version in VERSIONS:
        record = records[version]
        after = inventory(work / record["workspace_relative"])
        validate(record["before"], after, successful=successful)
        observed[version] = {
            **record,
            "after": after,
            "derived_output": after.get(OUTPUT),
            "failure_temporary": after.get(TEMPORARY),
        }
    return {
        "directories": observed,
        "copied_inputs_verified_before_and_after": True,
        "copied_inputs_continuously_immutable": False,
        "generated_wit_is_original_source": False,
        "production_enabled": False,
    }
