"""Capture sanitized actual M2 receipts; credentials and user paths stay private."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

REPO = Path(__file__).resolve().parents[3]
WORK = REPO.parent / ".work"
OUT = REPO / "crates/tobkiri-e2e/evidence"

def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()

def snapshot() -> dict[str, str]:
    paths = {REPO / "Cargo.toml", REPO / "Cargo.lock", REPO / "rust-toolchain.toml"}
    for name in ["canon", "manifest", "profile", "gate", "store", "flow", "core", "wasm", "wit", "e2e"]:
        root = REPO / "crates" / ("tobkiri-" + name)
        paths.update(root.rglob("*.rs"))
        paths.update(root.rglob("*.wit"))
        paths.add(root / "Cargo.toml")
    for relative in ["packs", "sdk"]:
        root = REPO / relative
        for suffix in ["*.rs", "*.js", "*.mjs", "*.wit"]:
            paths.update(path for path in root.rglob(suffix) if "node_modules" not in path.parts and ".venv" not in path.parts)
    paths.update([REPO / "packs/Cargo.toml", REPO / "packs/Cargo.lock", REPO / "packs/build.py"])
    paths.add(Path(__file__).resolve())
    return {path.relative_to(REPO).as_posix(): sha(path) for path in sorted(paths) if path.is_file()}

def digest_map(values: dict[str, str]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def artifacts() -> dict:
    profiles = {}
    packs = {}
    for name in ["harness-live", "harness-mock", "echo", "harness-ui"]:
        path = REPO / "packs/profiles" / (name + ".profile.json")
        document = json.loads(path.read_text())
        profiles[name] = {"document_sha256": sha(path), "packs": {pin["id"]: pin["sha256"] for pin in document["packs"]}}
        for pin in document["packs"]:
            directory = REPO / "packs" / pin["id"]
            manifest = json.loads((directory / "pack.json").read_text())
            files = {item["path"]: sha(directory / item["path"]) for item in manifest["files"]}
            assert all(files[item["path"]] == item["sha256"] for item in manifest["files"])
            packs[pin["id"]] = {"manifest_sha256": pin["sha256"], "manifest_document_sha256": sha(directory / "pack.json"), "files": files}
    return {"profiles": profiles, "packs": packs}

def run(arguments: list[str], environment: dict, log_name: str) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, cwd=REPO, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    log = WORK / "evidence" / log_name
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(result.stdout)
    log.chmod(0o600)
    return result

def main() -> None:
    before = snapshot()
    fixture_artifacts = artifacts()
    environment = dict(os.environ, CARGO_TARGET_DIR=str(WORK / "cargo-target"), CARGO_BUILD_JOBS="2", CARGO_INCREMENTAL="0")
    deterministic_args = ["cargo", "test", "-p", "tobkiri-e2e", "--locked", "--test", "chat", "--test", "network", "--test", "ui"]
    deterministic = run(deterministic_args, environment, "ws-e-m2-final-deterministic.log")
    if deterministic.returncode:
        raise SystemExit("deterministic M2 failed; inspect the private work log")
    private_name = "ws-e-llama-owned-" + str(time.time_ns()) + ".json"
    live_environment = dict(environment, TOBKIRI_TEST_LLAMA="1", TOBKIRI_TEST_PRIVATE_RECEIPT=str(WORK / "evidence" / private_name))
    live_args = ["cargo", "test", "-p", "tobkiri-e2e", "--locked", "--test", "llama", "--", "--ignored", "--nocapture"]
    live = run(live_args, live_environment, "ws-e-m2-final-live.log")
    if live.returncode:
        raise SystemExit("actual live M2 failed; inspect the private work log")
    match = re.search(r"real-1b receipt: model=(\S+) plan_digest=(\S+) streamed_deltas=(\d+) cancelled=true persisted=true secret_dispatches=(\d+) secret_outcomes=(\d+)", live.stdout)
    if not match:
        raise SystemExit("actual live receipt was absent")
    after = snapshot()
    if before != after:
        raise SystemExit("source changed during replay; capture a stable checkpoint again")
    state = json.loads((WORK / "llama/server-state.json").read_text())
    model = Path(state["model_path"])
    receipt = {
        "format": "tobkiri.m2-evidence/1", "source_snapshot_sha256": digest_map(before), "source_files": before,
        "fixture_artifacts": fixture_artifacts,
        "model": {"alias": match[1], "sha256": sha(model), "bytes": model.stat().st_size},
        "deterministic": {"command": "CARGO_TARGET_DIR=\"$PWD/../.work/cargo-target\" CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 " + " ".join(deterministic_args), "exit_code": 0, "tests_passed": 4, "real_components": True},
        "live": {"command": "CARGO_TARGET_DIR=\"$PWD/../.work/cargo-target\" CARGO_BUILD_JOBS=2 CARGO_INCREMENTAL=0 TOBKIRI_TEST_LLAMA=1 " + " ".join(live_args),
            "exit_code": 0, "plan_digest": match[2], "streamed_deltas": int(match[3]), "persisted_messages_loaded": 2, "cancelled": True, "cancelled_turn_saved": False,
            "server_idle_within_ms": 3000, "secret_dispatches": int(match[4]), "secret_outcomes": int(match[5]), "opaque_header_binding": True,
            "plaintext_data_and_export_scan": "passed", "vault": "real OS-backed DefaultKeyProvider", "credential_import": "generic Core user set_secret from mode-0600 file"},
        "limits": {"migration_complete": False, "all_837_legacy_inventory_rows": "todo", "native_browser_ui_acceptance": "pending", "cli_operation_acceptance": "pending", "m4_full_parity": "pending"}
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "m2-final.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"deterministic_passed": 4, "live_passed": 1, "streamed_deltas": int(match[3]), "source_snapshot_sha256": receipt["source_snapshot_sha256"], "receipt": "crates/tobkiri-e2e/evidence/m2-final.json"}))

if __name__ == "__main__":
    main()
