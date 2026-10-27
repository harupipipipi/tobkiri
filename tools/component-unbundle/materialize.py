"""Publish exact extracted code for a later, separately verified Pack build.

This trusted development tool performs no guest execution or Profile activation.
The destination parent must be a stable trusted local directory. Publication is
atomic/no-replace; failed private partials never become an admitted artifact.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from measure import INPUT_LIMIT, MAX_MODULES, OUTPUT_LIMIT, _worker_digest, extract

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "wasm"))
from materialize_ordered_tool_bridge_v2 import _publish_no_replace  # noqa: E402
from materialize_ordered_tool_bridge import no_links  # noqa: E402


def materialize(worker: Path, sources: list[Path], destination: Path) -> dict:
    """Publish a bounded, exact code closure without running or approving it."""
    if not destination.is_absolute() or destination.exists():
        raise ValueError("destination must be fresh and absolute")
    no_links(destination)
    if not destination.parent.is_dir() or not 1 <= len(sources) <= 64:
        raise ValueError("trusted parent and bounded component inputs required")
    no_links(worker)
    worker = worker.resolve(strict=True)
    worker_sha = _worker_digest(worker)
    for source in sources:
        if not source.is_absolute():
            raise ValueError("component input must be absolute")
        no_links(source)
    if len({source.resolve(strict=True) for source in sources}) != len(sources):
        raise ValueError("duplicate component input")
    with tempfile.TemporaryDirectory(prefix=".unbundle-", dir=destination.parent) as temporary:
        partial = Path(temporary) / "bundle"
        partial.mkdir(mode=0o700)
        records, unique = [], {}
        source_modules = 0
        for index, source in enumerate(sources):
            target = partial / str(index)
            target.mkdir(mode=0o700)
            record = extract(worker, source, target)
            source_modules += record["source_module_count"]
            if source_modules > MAX_MODULES:
                raise ValueError("total source module count exceeds inventory bound")
            modules = record["component"]["modules"]
            if not modules or len(modules) > 256:
                raise ValueError("shared Pack requires 1..256 actual module imports")
            own = {}
            for module in modules:
                digest, size = module["sha256"], module["bytes"]
                if unique.setdefault(digest, size) != size:
                    raise ValueError("inconsistent shared module identity")
                own[digest] = size
            if record["component"]["bytes"] + sum(own.values()) > INPUT_LIMIT:
                raise ValueError("Pack dependency closure exceeds original input bound")
            records.append(record)
            total = sum(r["component"]["bytes"] for r in records) + sum(unique.values())
            if total > OUTPUT_LIMIT:
                raise ValueError("catalog input bound exceeded")
        if _worker_digest(worker) != worker_sha:
            raise ValueError("trusted worker changed")
        receipt = {
            "format": "tobkiri.shared-core-module-materialization/1",
            "worker_sha256": worker_sha, "components": records,
            "catalog_source_bytes": total, "unique_modules": len(unique),
            "guest_execution_performed": False, "native_core_acceptance": False,
            "production_enabled": False,
        }
        (partial / "extraction.json").write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        _publish_no_replace(partial, destination)
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("components", nargs="+", type=Path)
    args = parser.parse_args()
    receipt = materialize(args.worker, args.components, args.output)
    print(json.dumps({"output": str(args.output),
                      "catalog_source_bytes": receipt["catalog_source_bytes"]}))


if __name__ == "__main__":
    main()
