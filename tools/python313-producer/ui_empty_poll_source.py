"""Derive an inactive, exact-source WASIp2 empty-poll libc candidate.

This does not rebuild libc, select producer assets, or establish runtime parity.
The independent C ABI oracle and cold restricted schema replay are required
before this source change can be used by the UI calculation candidate.
"""

import hashlib

LIBC_COMMIT = "161b3195fc2558d2b1ba3eb9ffae3b2b47407623"
POLL_PATH = "libc-bottom-half/cloudlibc/src/libc/poll/poll.c"
SOURCE_URL = (
    f"https://raw.githubusercontent.com/WebAssembly/wasi-libc/{LIBC_COMMIT}/{POLL_PATH}"
)
ORIGINAL_SHA256 = "22b6fa1ef5462b5a6d3b91b2c26e62bb857c6aa964f5f1047d2c45ffd3c4c0c9"
SDK_ARCHIVE_SHA256 = "0ba8b5bfaeb2adf3f29bab5841d76cf5318ab8e1642ea195f88baba1abd47bce"
ANCHOR = b"""  if (state.event_count > 0 && timeout != 0) {
    return state.event_count;
  }

  poll_own_pollable_t timeout_pollable;
"""
ADDITION = b"""  // No descriptors and no waiting require no pollable resources.
  if (nfds == 0 && timeout == 0) {
    return 0;
  }

"""


def derive(original: bytes) -> tuple[bytes, dict]:
    """Insert only the exact empty-FD/nonblocking path after original checks."""
    if type(original) is not bytes:
        raise ValueError("exact original libc source bytes required")
    if hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("original WASIp2 poll source differs from its pinned bytes")
    if original.count(ANCHOR) != 1:
        raise ValueError("original WASIp2 poll insertion point is not unique")
    anchor = original.index(ANCHOR)
    offset = anchor + ANCHOR.index(b"  poll_own_pollable_t timeout_pollable;")
    candidate = original[:offset] + ADDITION + original[offset:]
    return candidate, {
        "format": "tobkiri.ui-empty-poll-source/1",
        "upstream_commit": LIBC_COMMIT,
        "upstream_path": POLL_PATH,
        "upstream_url": SOURCE_URL,
        "original": {"bytes": len(original), "sha256": ORIGINAL_SHA256},
        "candidate": {
            "bytes": len(candidate),
            "sha256": hashlib.sha256(candidate).hexdigest(),
        },
        "insertion_offset": offset,
        "inserted_bytes": len(ADDITION),
        "condition": "nfds == 0 && timeout == 0",
        "original_prefix_and_suffix_unchanged": True,
        "sdk_archive_sha256": SDK_ARCHIVE_SHA256,
        "sdk_archive_version_checked": False,
        "distributed_libc_binary_correspondence_verified": False,
        "c_abi_and_errno_runtime_verified": False,
        "cold_schema_parity_verified": False,
        "production_enabled": False,
    }
