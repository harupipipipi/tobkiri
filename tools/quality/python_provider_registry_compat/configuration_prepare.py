"""Observe finite prepare results from unchanged original Provider setup code.

No credentials or network are used. The original Registry preflights an absent
temporary store; this does not exercise execute, persistence or GUI approval.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any

from generate import (
    ROOT,
    RUNTIME_ROOT,
    _NetworkFence,
    _capture_sources,
    _verify_sources_unchanged,
)


def cases() -> list[dict[str, Any]]:
    """Return synthetic requests, including exact digest and validation probes."""
    base = {
        "connection_name": "fixture",
        "protocol": "openai-compatible",
        "endpoint": "https://provider.example/v1",
        "key_value": "fixture-secret-123",
    }
    patches = [
        ("minimal", {}),
        ("empty-port", {"endpoint": "https://provider.example:/v1"}),
        ("port-zero", {"endpoint": "https://provider.example:0/v1"}),
        ("port-plus", {"endpoint": "https://provider.example:+123/v1"}),
        ("port-underscore", {"endpoint": "https://provider.example:1_23/v1"}),
        ("port-unicode", {"endpoint": "https://provider.example:١٢٣/v1"}),
        ("port-overflow", {"endpoint": "https://provider.example:65536/v1"}),
        ("userinfo", {"endpoint": "https://user@provider.example/v1"}),
        ("query", {"endpoint": "https://provider.example/v1?x=1"}),
        ("key-leak", {"display_name": "fixture-secret-123"}),
        ("bad-name", {"connection_name": "bad/name"}),
        ("null-optional", {"display_name": None}),
        ("unknown-field", {"approved": True}),
        ("empty-scopes", {"capabilities": []}),
        ("duplicate-scopes", {"capabilities": ["ai.stream", "ai.stream"]}),
        ("audio-scopes", {"capabilities": ["ai.stream", "ai.audio.speech"]}),
        ("anthropic-audio", {
            "protocol": "anthropic", "capabilities": ["ai.audio.speech"],
        }),
        ("unicode-digest", {
            "display_name": '日本語 🚀 "quoted" \\ path',
            "catalog_provider_id": "catalog.fixture",
            "capabilities": ["ai.stream", "ai.generate"],
        }),
        ("local", {
            "protocol": "local-openai-compatible", "key_value": "",
            "endpoint": "http://127.0.0.1:1234/v1",
        }),
        ("local-slash", {
            "protocol": "local-openai-compatible", "key_value": "",
            "endpoint": "http://127.0.0.1:1234/v1/",
        }),
        ("local-ipv6", {
            "protocol": "local-openai-compatible", "key_value": "",
            "endpoint": "http://[::1]:1234/v1",
        }),
        ("local-nonloopback", {
            "protocol": "local-openai-compatible", "key_value": "",
            "endpoint": "http://example.com:1234/v1",
        }),
        ("local-with-key", {
            "protocol": "local-openai-compatible",
            "endpoint": "http://127.0.0.1:1234/v1",
        }),
    ]
    return [{"id": name, "request": {**base, **patch}} for name, patch in patches]


def main() -> None:
    """Authenticate source bytes and record real prepare returns/errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    contents, sources = _capture_sources()
    this_source = Path(__file__).resolve()
    producer_bytes = this_source.read_bytes()
    sys.path.insert(0, str(RUNTIME_ROOT))
    rows = cases()
    with _NetworkFence() as network:
        configuration = importlib.import_module(
            "ecosystem.rumi_provider_registry_pack.runtime.configuration"
        )
        registry = importlib.import_module(
            "ecosystem.rumi_provider_registry_pack.runtime.registry"
        )
        with tempfile.TemporaryDirectory(prefix="provider-prepare-original-") as scratch:
            for row in rows:
                owner = registry.ProviderRegistry(
                    "defaults", user_data_root=Path(scratch) / row["id"],
                )
                try:
                    result = configuration.prepare_configuration(owner, row["request"])
                except (ValueError, PermissionError) as error:
                    row["error"] = {"class": type(error).__name__, "message": str(error)}
                else:
                    row["result"] = result
                if owner.path.exists():
                    raise ValueError("preparation unexpectedly wrote a Registry store")
        loaded = []
        for module in tuple(sys.modules.values()):
            name = getattr(module, "__file__", None)
            if not isinstance(name, str):
                continue
            path = Path(name).resolve()
            if not path.is_relative_to(RUNTIME_ROOT):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if relative not in contents:
                raise ValueError(f"uncaptured source import: {relative}")
            loaded.append(relative)
        if network.attempts:
            raise ValueError("unexpected source network attempt")
    _verify_sources_unchanged(contents)
    if this_source.read_bytes() != producer_bytes:
        raise ValueError("producer changed during observation")
    document = {
        "schema": "io.tobkiri.original-provider-configuration-prepare.v1",
        "scope": "finite prepare only; no execute, credential, persistent or GUI parity",
        "interpreter": {
            "version": sys.version,
            "implementation": sys.implementation.name,
            "executable_sha256": hashlib.sha256(
                Path(sys.executable).read_bytes(),
            ).hexdigest(),
        },
        "producer": {
            "path": this_source.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(producer_bytes).hexdigest(),
        },
        "sources": sources,
        "loaded_sources": sorted(set(loaded)),
        "network_attempts": [],
        "cases": rows,
    }
    args.output.write_text(json.dumps(document, ensure_ascii=True, indent=2) + "\n")
    print(json.dumps({"cases": len(rows), "sources": len(sources), "writes": 0}))


if __name__ == "__main__":
    main()
