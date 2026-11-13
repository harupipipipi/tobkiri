"""Stage current replay tooling with the two immutable Profile source inputs."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
ORIGINAL = "42dfc6e02d7477ccbbf0e3692256d6c6e781c9a8"
PROFILE_DIR = "tobkiri_runtime/ecosystem/defaultspack/v4"
FROZEN = {
    "defaults.profile.v5.json": (
        "4c66d34286551d93df2778321a989c81e007c13f03fdec53bf7ad65c4faafdb2"
    ),
    "defaults.profile.intent.v1.json": (
        "a011d0559a5bf485393ed738830e03ab09e54761539ec3c3961feeefc5b9ed29"
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
    "tobkiri_runtime/core_runtime/global_contract_dispatch.py",
    "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/gateway.py",
    "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/messages_generate.py",
    "tobkiri_runtime/ecosystem/rumi_ai_gateway_pack/runtime/text_generate.py",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/contracts.v4.json",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/executables.v4.json",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/pack.v4.json",
    "tobkiri_runtime/ecosystem/rumi_ai_tool_bridge_pack/runtime/bridge.py",
    "tobkiri_runtime/tobkiri_host/broker.py",
    "tools/python313-producer",
    "tools/quality/test_python313_sharing.py",
    "tests/fixtures/issue1520/gateway-tool-caller.current-source.json",
    "tests/test_preserved_gateway_tool_caller.py",
    "tests/test_stage_preserved_ordered_python.py",
)


def stage(destination: Path) -> None:
    """Copy committed CI inputs and verify the historical blobs before writing."""
    destination = destination.parent.resolve(strict=True) / destination.name
    if destination.resolve() != destination or destination.is_relative_to(ROOT):
        raise ValueError("preserved replay requires a fresh physical runner directory")
    if destination.exists():
        raise ValueError("preserved replay destination already exists")
    frozen = {}
    for name, expected in FROZEN.items():
        relative = f"{PROFILE_DIR}/{name}"
        content = subprocess.check_output(
            ["git", "cat-file", "blob", f"{ORIGINAL}:{relative}"], cwd=ROOT
        )
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"frozen Profile source differs: {relative}")
        frozen[relative] = content
    destination.mkdir(mode=0o700)
    # Use tracked HEAD tooling and fixtures. Only the two archived source
    # inputs come from the older commit. The original verifiers still check
    # the entire source closure and every byte of the preserved artifacts.
    with subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD", "--", *CURRENT_PATHS],
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
    (destination / PROFILE_DIR).mkdir(parents=True)
    for relative, content in frozen.items():
        with (destination / relative).open("xb") as target:
            target.write(content)


def main() -> None:
    """Accept an unused directory on the CI runner."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    stage(parser.parse_args().output)


if __name__ == "__main__":
    main()
