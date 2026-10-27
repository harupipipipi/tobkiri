"""Stage, but never build or activate, the ordered original-Python bridge."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from prepare_ordered_tool_bridge import prepare, verify

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packs/local.ai-tool-bridge-ordered-python"


def bounded_read(path: Path, limit: int) -> bytes:
    """Read a bounded regular file, rejecting links and overlarge evidence."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"invalid or oversized evidence file: {path.name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("evidence file exceeds limit")
    return data


def strict_json(data: bytes) -> object:
    """Reject duplicate evidence keys rather than silently taking the last."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate evidence key")
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(
                          ValueError("nonfinite evidence number")))


def reviewed_inputs() -> tuple[bytes, bytes]:
    """Validate the real frozen oracle; return data and hash-only guest binding."""
    from freeze_ordered_tool_bridge import cases
    import sys
    sys.path.insert(0, str(PACK))
    from ordered_json import dumps
    verify(PACK / "python")
    raw = bounded_read(PACK / "oracle.json", 16 * 1024 * 1024)
    binding_raw = bounded_read(PACK / "oracle.freeze.json", 64 * 1024)
    oracle = strict_json(raw)
    binding = strict_json(binding_raw)
    inputs = [{"id": item["id"], "operation": item["operation"],
               "input_json": dumps(item["input"])} for item in cases()]
    actual_inputs = [{key: item[key] for key in ("id", "operation", "input_json")}
                     for item in oracle.get("cases", [])]
    expected = {
        "format": "tobkiri.ordered-tool-bridge-frozen-inputs/1",
        "protocol": "ordered-finite-json-v1",
        "original_reference_runtime": {"python": "3.13.5", "unicode": "15.1.0"},
        "oracle_sha256": hashlib.sha256(raw).hexdigest(),
        "source_provenance_sha256": hashlib.sha256(bounded_read(
            PACK / "python/provenance.json", 1024 * 1024)).hexdigest(),
        "freeze_script_sha256": hashlib.sha256(bounded_read(
            Path(__file__).with_name("freeze_ordered_tool_bridge.py"),
            1024 * 1024)).hexdigest(),
        "case_inputs_sha256": hashlib.sha256(json.dumps(
            inputs, ensure_ascii=True, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        "case_count": len(inputs),
    }
    if binding != expected or actual_inputs != inputs:
        raise ValueError("frozen oracle or complete case set differs from reviewed inputs")
    if (oracle.get("format") != "tobkiri.ordered-python-tool-bridge-oracle/1"
            or oracle.get("protocol") != expected["protocol"]
            or oracle.get("python") != "3.13.5" or oracle.get("unicode") != "15.1.0"
            or any(oracle.get(key) != expected[key] for key in
                   ("source_provenance_sha256", "freeze_script_sha256"))):
        raise ValueError("oracle identity does not match reviewed freeze")
    return raw, binding_raw


def stage(output: Path) -> dict[str, object]:
    """Create an immutable-hash SDK plan in a new staging directory."""
    if output.exists():
        raise ValueError("stage output must not already exist")
    _, binding = reviewed_inputs()
    expected = strict_json(binding)["source_provenance_sha256"]
    output.mkdir(parents=True)
    prepare(output)
    if hashlib.sha256((output / "provenance.json").read_bytes()).hexdigest() != expected:
        raise ValueError("source changed during staging")
    (output / "reviewed-inputs.json").write_bytes(binding)
    for name in ("caller_adapter.py", "app.py"):
        (output / name).write_bytes(bounded_read(PACK / "python" / name, 1024 * 1024))
    (output / "ordered_json.py").write_bytes(bounded_read(
        PACK / "ordered_json.py", 1024 * 1024))
    (output / "wit/deps").mkdir(parents=True)
    (output / "wit/pack.wit").write_bytes(bounded_read(PACK / "wit/pack.wit", 1024 * 1024))
    # Pack wit/deps is ignored in git. Always capture the tracked canonical file,
    # never an optional local dependency copy that can disappear on checkout.
    core = ROOT / "crates/tobkiri-wit/wit/core/core.wit"
    (output / "wit/deps/core.wit").write_bytes(bounded_read(core, 1024 * 1024))
    sources = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            relative = path.relative_to(output).as_posix()
            sources.append({"source": relative, "destination": relative,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    plan = {
        "format": "tobkiri.python-build/3", "stub_wasi": True,
        "wit": "wit/pack.wit", "world": "pack", "entrypoint": "app",
        "sources": sources,
        "outer_exports": ["tobkiri:core/lifecycle@1.0.0",
                          "local:ai-tool-bridge-ordered-python/bridge@1.0.0"],
        "allowed_imports": [f"tobkiri:core/{name}@1.0.0" for name in
                            ("types", "flow", "clock", "random", "audit")],
    }
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    stage(arguments.output)
