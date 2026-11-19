"""Stage replay tooling with verified immutable Profile and Gateway source inputs."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = "42dfc6e02d7477ccbbf0e3692256d6c6e781c9a8"
GATEWAY_CAPTURE = "0e87a8e1a6dfc13e86482af9db48c6011494020b"
PROFILE_DIR = "tobkiri_runtime/ecosystem/defaultspack/v4"
FROZEN = {
    f"{PROFILE_DIR}/defaults.profile.v5.json": (
        ORIGINAL,
        "4c66d34286551d93df2778321a989c81e007c13f03fdec53bf7ad65c4faafdb2",
    ),
    f"{PROFILE_DIR}/defaults.profile.intent.v1.json": (
        ORIGINAL,
        "a011d0559a5bf485393ed738830e03ab09e54761539ec3c3961feeefc5b9ed29",
    ),
    "tobkiri_runtime/core_runtime/global_contract_dispatch.py": (
        GATEWAY_CAPTURE,
        "f1cc60d7361b0716430c715c529fc36d41d1db5a130e47c2ae835962f6f7d893",
    ),
    "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/gateway.py": (
        GATEWAY_CAPTURE,
        "9df95525a3c467ba3313d8b3ea9bc1ddc7de7f8d1d4a14a9cf8d9f3fa849b208",
    ),
    "tobkiri_runtime/tobkiri_host/broker.py": (
        ORIGINAL,
        "5a1e76765a525058268cd62b23bf6bdd460b53eac2198715ecbb44f03244eed9",
    ),
}
CURRENT_PATHS = (
    "scripts/wasm",
    "packs/local.ai-tool-bridge-ordered-python",
    "packs/local.ai-gateway-tool-caller-python",
    "packs/local.ai-gateway-finalizer-python",
    "packs/local.ai-stream-normalizer-python",
    "packs/local.ai-usage-cost-python",
    "crates/tobkiri-canon/src/codec.rs",
    "crates/tobkiri-wit/wit/core/core.wit",
    "crates/tobkiri-e2e/fixtures/ordered-python",
    "crates/tobkiri-e2e/fixtures/gateway-tool-caller",
    "sdk/python",
    "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/messages_generate.py",
    "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/text_generate.py",
    "tobkiri_runtime/ecosystem/rumi_ai_stream_pack",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/contracts.v4.json",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/executables.v4.json",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/pack.v4.json",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/runtime/bridge.py",
    "tobkiri_runtime/ecosystem/rumi_ai_usage_pack",
    "tobkiri_runtime/tobkiri_protocol/conversation_lifecycle.py",
    "tools/python313-producer",
    "tools/quality/python_finalizer_normalize_compat",
    "tools/quality/python_provider_compat",
    "tools/quality/python_tool_bridge_compat",
    "tools/quality/test_python313_sharing.py",
    "tests/fixtures/issue1520/gateway-tool-caller.current-source.json",
    "tests/test_preserved_gateway_tool_caller.py",
    "tests/test_stage_preserved_ordered_python.py",
)
HISTORICAL_TESTS = (
    "tests/test_gateway_finalization_providers.py",
    "tests/test_gateway_finalization_providers_stage.py",
    "tests/test_gateway_finalizer.py",
    "tests/test_gateway_finalizer_replay.py",
    "tests/test_gateway_finalizer_stage.py",
    "tests/test_gateway_tool_caller_current_replay.py",
    "tests/test_gateway_tool_caller_extraction.py",
    "tests/test_gateway_tool_caller_graph_materialization.py",
    "tests/test_gateway_tool_caller_replay.py",
    "tests/test_gateway_tool_caller_stage.py",
    "tests/test_preserved_gateway_tool_caller.py",
    "tests/test_python_tool_bridge_ordered_adapter.py",
    "tests/test_python_tool_bridge_ordered_evidence.py",
    "tests/test_python_tool_bridge_ordered_extraction.py",
    "tests/test_python_tool_bridge_ordered_integer_v2.py",
    "tests/test_python_tool_bridge_ordered_materialization.py",
    "tests/test_python_tool_bridge_ordered_materialization_v2.py",
    "tests/test_python_tool_bridge_ordered_nonfinite_v2.py",
    "tests/test_python_tool_bridge_ordered_pack_v2.py",
    "tests/test_python_tool_bridge_ordered_replay_v2.py",
    "tests/test_stage_preserved_ordered_python.py",
)
HISTORICAL_FIXTURES = (
    "tests/fixtures/issue1520/gateway-tool-caller.source.json",
    "tests/fixtures/issue1520/gateway-tool-caller.typed-surface.json",
)


def stage(destination: Path, *, root_tests: bool = False) -> None:
    """Copy committed CI inputs and verify the historical blobs before writing."""
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination.resolve() != destination or destination.is_relative_to(ROOT):
        raise ValueError("preserved replay requires a fresh physical runner directory")
    if destination.exists():
        raise ValueError("preserved replay destination already exists")
    frozen = {}
    for relative, (commit, expected) in FROZEN.items():
        content = subprocess.check_output(
            ["git", "--no-replace-objects", "cat-file", "blob", f"{commit}:{relative}"],
            cwd=ROOT,
        )
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"frozen replay source differs: {relative}")
        frozen[relative] = content
    destination.mkdir(mode=0o700)
    # The replay verifiers still check the entire source closure and every
    # byte of the preserved artifacts against their original receipts.
    tracked_paths = dict.fromkeys(CURRENT_PATHS)
    if root_tests:
        tracked_paths.update(dict.fromkeys((*HISTORICAL_TESTS, *HISTORICAL_FIXTURES)))
    with subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD", "--", *tracked_paths],
        cwd=ROOT,
        stdout=subprocess.PIPE,
    ) as archive:
        subprocess.run(
            ["tar", "-xf", "-", "-C", str(destination)],
            stdin=archive.stdout,
            check=True,
        )
        if archive.wait():
            raise ValueError("could not stage the current replay tooling")
    for relative, content in frozen.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(content)


def main() -> None:
    """Accept an unused directory on the CI runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root-tests", action="store_true")
    arguments = parser.parse_args()
    stage(arguments.output, root_tests=arguments.root_tests)


if __name__ == "__main__":
    main()
