"""Translate authenticated comparison DATA into exact compiled source members.

This changes representation only. No original catalog/schema validator is
imported, no validation result is generated, and no runtime cache is populated.
"""

from __future__ import annotations

import base64
import hashlib
import json

MAX_BYTES = 4 * 1024 * 1024
ROOT = "bundled-defaults/"
INDEX = ROOT + "index.json"


def identity(raw: bytes) -> dict:
    """Describe bytes, never a domain result or a source capability."""
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate bundle source key")
        value[key] = item
    return value


def _decode(value):
    if type(value) is not str:
        raise ValueError("bundle source must be canonical base64")
    raw = base64.b64decode(value.encode("ascii"), validate=True)
    if base64.b64encode(raw).decode("ascii") != value:
        raise ValueError("bundle source must be canonical base64")
    return raw


def _text(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def resources(cases: dict, *, diagnostic_reread: bool = False) -> tuple[dict, dict]:
    """Retain all original occurrences, optionally changing one diagnostic read.

    A diagnostic artifact has its own source identity and receipt. It is never
    accepted as the ordinary Pack or described as the original component.
    """
    if type(diagnostic_reread) is not bool:
        raise ValueError("explicit diagnostic source mode required")
    payload = cases["payload"]
    if type(payload) is not str or len(payload.encode()) > MAX_BYTES:
        raise ValueError("bundle carrier exceeds original bound")
    value = json.loads(payload, object_pairs_hook=_object)
    if (
        type(value) is not dict
        or set(value) != {"format", "lock_base64", "blobs", "reads"}
        or value["format"] != "tobkiri.bundle-defaults/2"
        or type(value["blobs"]) is not list
        or type(value["reads"]) is not list
    ):
        raise ValueError("exact original source carrier required")
    lock = _decode(value["lock_base64"])
    blobs = [_decode(item) for item in value["blobs"]]
    if len(set(blobs)) != len(blobs):
        raise ValueError("duplicate source blobs")
    reads = value["reads"]
    referenced = set()
    for row in reads:
        if (
            type(row) is not dict
            or set(row) != {"index", "path", "reread", "blob"}
            or type(row["index"]) is not int
            or row["index"] < 0
            or type(row["path"]) is not str
            or type(row["reread"]) is not bool
            or type(row["blob"]) is not int
            or not 0 <= row["blob"] < len(blobs)
        ):
            raise ValueError("exact original read occurrence required")
        referenced.add(row["blob"])
    if len(referenced) != len(blobs):
        raise ValueError("unreferenced source blob")

    def source_identity():
        hashes = [identity(raw) for raw in blobs]
        return {
            "format": "tobkiri.bundle-source-bytes/1",
            "lock": identity(lock),
            "reads": [
                {
                    "index": row["index"],
                    "path": row["path"],
                    "reread": row["reread"],
                    **hashes[row["blob"]],
                }
                for row in reads
            ],
        }

    if source_identity() != cases["source_identity"]:
        raise ValueError("raw source differs from the independent original")
    mutation = None
    if diagnostic_reread:
        change = cases["reread_mutation"]
        occurrence = change["occurrence"]
        if (
            type(occurrence) is not int
            or not 0 <= occurrence < len(reads)
            or reads[occurrence]["reread"] is not True
        ):
            raise ValueError("original executable reread diagnostic required")
        changed = _decode(change["bytes_base64"])
        old = blobs[reads[occurrence]["blob"]]
        if old == changed:
            raise ValueError("diagnostic must change actual source bytes")
        if changed not in blobs:
            blobs.append(changed)
        reads[occurrence]["blob"] = blobs.index(changed)
        mutation = {
            "occurrence": occurrence,
            "before": identity(old),
            "after": identity(changed),
            "expected_error": change["expected_error"],
        }
    owned = len(lock) + sum(map(len, blobs))
    logical = len(lock) + sum(len(blobs[row["blob"]]) for row in reads)
    if owned > MAX_BYTES or logical > MAX_BYTES:
        raise ValueError("raw source exceeds original owned/logical bounds")
    members = {ROOT + "lock.json": lock}
    for ordinal, raw in enumerate(blobs):
        members[ROOT + f"blobs/{ordinal}.json"] = raw
    index = {
        "format": "tobkiri.embedded-bundle/2",
        "lock": identity(lock),
        "blobs": [identity(raw) for raw in blobs],
        "reads": reads,
        "source_identity": source_identity(),
    }
    members[INDEX] = (_text(index) + "\n").encode()
    if any(len(raw) > MAX_BYTES for raw in members.values()):
        raise ValueError("raw source member exceeds original resource bound")
    receipt = {
        "format": "tobkiri.embedded-bundle/2",
        "members": {name: identity(raw) for name, raw in sorted(members.items())},
        "source_identity_sha256": hashlib.sha256(
            _text(index["source_identity"]).encode()
        ).hexdigest(),
        "owned_source_bytes": owned,
        "logical_source_bytes": logical,
        "read_occurrences": len(reads),
        "diagnostic_mutation": mutation,
    }
    return members, receipt


def verify_members(proof: dict, source: dict, cases: dict, *, diagnostic_reread=False):
    """Recompute every compiled resource identity from independent original DATA."""
    _members, expected = resources(cases, diagnostic_reread=diagnostic_reread)
    if source["embedded_resources"] != expected:
        raise ValueError("compiled raw sources differ from the original DATA")
    captured = {row["destination"]: row for row in proof["source_manifest"]}
    if {name for name in captured if name.startswith(ROOT)} != set(expected["members"]):
        raise ValueError("compiled resource inventory differs")
    for name, record in expected["members"].items():
        if {key: captured[name][key] for key in ("bytes", "sha256")} != record:
            raise ValueError("actual compiled raw source differs: " + name)
