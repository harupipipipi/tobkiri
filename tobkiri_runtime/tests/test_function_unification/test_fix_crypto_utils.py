"""Digest helpers remain owned by the v4 pack policy boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

from core_runtime.crypto_utils import compute_file_sha256
from tests.legacy_authority_contracts import assert_retired_module_absent


def test_compute_file_sha256_is_exact_for_known_content(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"hello world")
    assert compute_file_sha256(path) == hashlib.sha256(b"hello world").hexdigest()


def test_compute_file_sha256_handles_multi_chunk_content(tmp_path: Path) -> None:
    payload = b"A" * (65536 * 2 + 1234)
    path = tmp_path / "large.bin"
    path.write_bytes(payload)
    assert compute_file_sha256(path) == hashlib.sha256(payload).hexdigest()


def test_pack_policy_uses_canonical_digest_helper() -> None:
    from core_runtime import pack_function_policy

    assert pack_function_policy.compute_file_sha256 is compute_file_sha256


def test_digest_helper_is_not_reexported_by_retired_executor() -> None:
    assert_retired_module_absent("core_runtime.capability_executor")
