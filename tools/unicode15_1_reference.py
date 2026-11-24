"""CI-only generation/oracle for pinned Unicode 15.1 Rust primitives.

No product runtime imports, table parser or Python Host are added. The Rust
tables are immutable build data; fresh independent observations stay in CI.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import platform
import struct
import subprocess
import sys
import unicodedata as ud


PINS = (
    ("NormalizationTest.txt", "unicode-org/unicodetools",
     "2e88574243dbb5dd4109f54bd25d43588e78226f", 2625136),
    ("unicodeobject.c", "python/cpython",
     "acb08fa37905fcf726b8f031e3a4c5f1f58e3bd7", 475025),
    ("COPYING.unicode", "unicode-org/unicodetools",
     "d7e7973c2fd6f2586a8999a69dc21e39af26be0f", 2033),
)
FORMAT = "io.tobkiri.unicode15-1-reference.v1"
LIMIT = 0x110000
BLOCK = 128
CONTEXTS = (
    "", "defaults", "a'b", 'a"b', 'a\'"b', "\\\0\b\t\n\v\f\r\x1b\x7f",
    "\u00a0\u0085\u200b\u2028", "\ud800", "\udc00", "\ud800\udc00",
    "\u1100\u1161", "\uac00\u11a8", "\u0340", "\u0344", "\u01fa",
    "a\u0301\u0323", "a\u0305\u0301", "\u212a", "\u00b5\ufb00",
    "\u03a3", "\u039f\u03a3", "\u039f\u03a3\u0391", "\u03a3\u0301",
    "\u0391\u03a3\u0301", "\u0391\u03a3\u0301\u0391",
    "\ud800\u03a3", "\u0391\u03a3\udc00", "\u0130", "\U0002ebf0",
    "\U0001fae9", "\U0010ffff", "\u00e9\U0001f9ed",
)


def require_ci() -> None:
    import os
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("Unicode generation and source fetch are CI-only")


def require_python() -> None:
    require_ci()
    if (platform.python_implementation() != "CPython"
            or sys.version_info[:3] != (3, 13, 16) or ud.unidata_version != "15.1.0"):
        raise ValueError("CPython 3.13.16 / Unicode 15.1.0 required")


def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def fetch(destination: Path) -> None:
    require_ci()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, repo, pin, size in PINS:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/git/blobs/{pin}"], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if len(result.stdout) > 2 * size + 1024 * 1024:
            raise ValueError("pinned source response exceeded bound")
        record = json.loads(result.stdout)
        if record["sha"] != pin or record["size"] != size or record["encoding"] != "base64":
            raise ValueError("pinned primary source metadata differs")
        data = base64.b64decode(record["content"])
        if len(data) != size or git_blob(data) != pin:
            raise ValueError("pinned primary source bytes differ")
        (destination / name).write_bytes(data)
    print(json.dumps({"primary_sources_fetched": len(PINS), "product_execution": False}))


class Ranges:
    def __init__(self):
        self.rows = []

    def add(self, cp: int) -> None:
        if self.rows and self.rows[-1][1] + 1 == cp:
            self.rows[-1][1] = cp
        else:
            self.rows.append([cp, cp])


def codepoints(value: str) -> list[int]:
    return list(map(ord, value))


def sequence_hash(digest, key: int, value: list[int]) -> None:
    digest.update(struct.pack("<II", key, len(value)))
    for cp in value:
        digest.update(struct.pack("<I", cp))


def rust_array(name: str, kind: str, rows, *, tuples=True) -> str:
    entries = []
    for row in rows:
        text = ",".join(str(n) for n in row) if tuples else str(row)
        entries.append(f"({text})," if tuples else f"{text},")
    return (f"#[rustfmt::skip]\npub(crate) static {name}: &[{kind}] = &[\n"
            + "\n".join(entries) + "\n];\n")


def generate(inputs: Path, destination: Path) -> None:
    require_python()
    sources = {}
    for name, repo, pin, size in PINS:
        data = (inputs / name).read_bytes()
        if len(data) != size or git_blob(data) != pin:
            raise ValueError("primary input changed before generation")
        sources[name] = {"repository": repo, "git_blob": pin, "bytes": size,
                         "sha256": hashlib.sha256(data).hexdigest()}
    c_source = (inputs / "unicodeobject.c").read_text()
    if "lower_ucs4" not in c_source or "handle_capital_sigma" not in c_source:
        raise ValueError("pinned lower implementation differs")
    ccc, index, decomposition, composition = [], [], [], []
    lower, printable = Ranges(), Ranges()
    maximum = 1
    nfc_hash, lower_hash, repr_hash = (hashlib.sha256() for _ in range(3))
    block = []
    for cp in range(LIMIT):
        value = chr(cp)
        combining = ud.combining(value)
        if combining:
            ccc.append((cp, combining))
        nfd = codepoints(ud.normalize("NFD", value))
        maximum = max(maximum, len(nfd))
        # Hangul is algorithmic at runtime; it still contributes to maximum.
        if nfd != [cp] and not 0xAC00 <= cp < 0xD7A4:
            if len(nfd) > 255:
                raise ValueError("canonical expansion exceeds pinned index type")
            index.append((cp, len(decomposition), len(nfd)))
            decomposition.extend(nfd)
        raw = ud.decomposition(value)
        if raw and not raw.startswith("<"):
            pair = [int(item, 16) for item in raw.split()]
            if len(pair) == 2 and ud.normalize("NFC", "".join(map(chr, pair))) == value:
                composition.append((*pair, cp))
        lowered = value.lower()
        if len(lowered) < 1:
            raise ValueError("lower equality proof requires no deletion mapping")
        changed = lowered != value
        if changed:
            lower.add(cp)
        if value.isprintable():
            printable.add(cp)
        sequence_hash(nfc_hash, cp, codepoints(ud.normalize("NFC", value)))
        lower_hash.update(struct.pack("<IB", cp, changed))
        block.append(value)
        if len(block) == BLOCK:
            encoded = repr("".join(block)).encode("utf-8")
            repr_hash.update(struct.pack("<II", cp + 1 - BLOCK, len(encoded)))
            repr_hash.update(encoded)
            block.clear()
    if block or chr(0x3A3).lower() == chr(0x3A3):
        raise ValueError("pinned full-domain block/Sigma assumptions differ")
    composition.sort()
    if len({row[:2] for row in composition}) != len(composition):
        raise ValueError("duplicate canonical composition pair")
    table = ("// Generated by tools/unicode15_1_reference.py in pinned CI.\n"
             "// Unicode data: see COPYING.unicode; CPython derivation: COPYING.python.\n"
             '#[cfg(test)]\npub(crate) const UNICODE_VERSION: &str = "15.1.0";\n'
             '#[cfg(test)]\npub(crate) const PYTHON_VERSION: &str = "3.13.16";\n'
             f"pub(crate) const MAX_CANONICAL_EXPANSION: usize = {maximum};\n"
             + rust_array("CCC", "(u32,u8)", ccc)
             + rust_array("CANONICAL_DECOMP_INDEX", "(u32,u32,u8)", index)
             + rust_array("DECOMP_DATA", "u32", decomposition, tuples=False)
             + rust_array("COMPOSITION", "(u32,u32,u32)", composition)
             + rust_array("LOWER_CHANGED_RANGES", "(u32,u32)", lower.rows)
             + rust_array("PRINTABLE_RANGES", "(u32,u32)", printable.rows))

    normalization = {}
    official_rows = 0
    for line in (inputs / "NormalizationTest.txt").read_text().splitlines():
        text = line.split("#", 1)[0].strip()
        if not text or text.startswith("@"):
            continue
        columns = [[int(cp, 16) for cp in part.split()] for part in text.split(";")[:5]]
        if len(columns) != 5:
            raise ValueError("official normalization row shape differs")
        official_rows += 1
        for i, row in enumerate(columns):
            expected = columns[1 if i < 3 else 3]
            if codepoints(ud.normalize("NFC", "".join(map(chr, row)))) != expected:
                raise ValueError("CPython disagrees with official Unicode 15.1 NFC corpus")
            key = tuple(row)
            if key in normalization and normalization[key] != expected:
                raise ValueError("inconsistent normalization corpus")
            normalization[key] = expected
    contexts = [{"input": codepoints(value), "nfc": codepoints(ud.normalize("NFC", value)),
                 "lower_changed": value.lower() != value, "repr": repr(value)}
                for value in CONTEXTS]
    metadata = {"format": FORMAT, "python": "3.13.16", "unicode": "15.1.0",
                "sources": sources, "scalar_count": LIMIT, "repr_block_size": BLOCK,
                "official_rows": official_rows, "normalization_cases": len(normalization),
                "context_cases": len(contexts), "max_canonical_expansion": maximum,
                "scalar_nfc_sha256": nfc_hash.hexdigest(),
                "scalar_lower_changed_sha256": lower_hash.hexdigest(),
                "all_codepoint_repr_blocks_sha256": repr_hash.hexdigest(),
                "table_sha256": hashlib.sha256(table.encode()).hexdigest(),
                "lower_equality_proof": "Pinned lower_ucs4 has only capital-Sigma context logic; "
                    "both outputs differ from Sigma. Every full single lower mapping is nonempty. "
                    "Expansion makes length grow; otherwise equality is positionwise.",
                "native_comparison": False, "qualification": False, "production_enabled": False}
    fixture = {**metadata, "normalization": [{"input": list(key), "nfc": expected}
                for key, expected in sorted(normalization.items())], "contexts": contexts}
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    (destination / "unicode15_1_data.rs").write_text(table)
    (destination / "COPYING.unicode").write_bytes((inputs / "COPYING.unicode").read_bytes())
    for name, value in (("fixtures.json", fixture), ("summary.json", metadata)):
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        if len(encoded.encode()) > 16 * 1024 * 1024:
            raise ValueError("Unicode reference exceeds bounded CI storage")
        (destination / name).write_text(encoded)
    print(json.dumps({"tables_generated": True, "official_rows": official_rows,
                      "normalization_cases": len(normalization), "scalar_count": LIMIT,
                      "table_bytes": len(table.encode()), "max_expansion": maximum,
                      "native_comparison": False, "qualification": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "generate"))
    parser.add_argument("--inputs", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.output.resolve())
    elif args.inputs is None:
        parser.error("generate requires --inputs")
    else:
        generate(args.inputs.resolve(strict=True), args.output.resolve())


if __name__ == "__main__":
    main()
