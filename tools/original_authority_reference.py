"""Fresh original-Authority fixtures for CI; never a production state adapter.

Run only in CI. This executes the pinned original Python store, prepares a
separate identical input for the native comparison, and retains bounded JSON.
Database/key fixtures remain in the runner's temporary directory, not artifacts.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import closing
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import tomllib


ORIGINAL_HEAD = "1cf025f1eeab31a3daa2861b1f26a025c6954b6e"
CLOCK = 1000.0
LIMIT = 2 * 1024 * 1024
FORMAT = "io.tobkiri.original-authority-fixtures.v1"
CASE_COUNT = 38
REQUIREMENTS = (
    "cryptography", "cffi", "pycparser", "pytest", "iniconfig", "packaging",
    "pluggy", "pygments",
)
SOURCES = (
    "core_runtime/authority/v4_store.py", "core_runtime/authority/v4_models.py",
    "core_runtime/authority/store_contracts.py", "core_runtime/process_identity.py",
    "core_runtime/secure_sqlite_path.py", "core_runtime/bootstrap/profile_capture.py",
    "tobkiri_protocol/canonical.py", "tests/test_authority_v4_schema_migration.py",
    "tests/test_authority_v4_lifecycle.py",
)
EPOCH_TEXTS = (
    ("plus-leading-zero", " +0001 "), ("underscore", "1_0"),
    ("unicode-decimal", "\u0661\u0662"), ("unicode-whitespace", "\u20031\u2003"),
    ("i-json-boundary", "9007199254740991"),
    ("above-i-json", "9007199254740992"),
    ("above-u64", "18446744073709551616"), ("hundred-digits", "9" * 100),
    ("zero", "0"), ("negative", "-1"), ("bad-underscore", "1__0"),
    ("empty", ""), ("over-int-digit-limit", "9" * 4301),
)


def write_json(path: Path, value: object) -> None:
    """Write only a bounded JSON observation, with no key or database bytes."""
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    if len(data.encode()) > LIMIT:
        raise ValueError("original Authority observation exceeds bound")
    path.write_text(data, encoding="utf-8")


def requirements(root: Path, output: Path) -> None:
    """Select exact versions and wheel hashes from the original checked-in lock."""
    lock = tomllib.loads((root / "tobkiri_runtime/uv.lock").read_text())
    rows = []
    for name in REQUIREMENTS:
        matches = [p for p in lock["package"] if p["name"] == name]
        if len(matches) != 1 or not matches[0].get("wheels"):
            raise ValueError("one locked wheel inventory per dependency required")
        item = matches[0]
        hashes = sorted({wheel["hash"] for wheel in item["wheels"]})
        if any(not h.startswith("sha256:") or len(h) != 71 for h in hashes):
            raise ValueError("locked SHA-256 wheel identity required")
        rows.append(item["name"] + "==" + item["version"] + " "
                    + " ".join("--hash=" + h for h in hashes))
    output.write_text("\n".join(rows) + "\n", encoding="utf-8")


def original(root: Path):
    """Load the actual original module from the authenticated source checkout."""
    actual = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if actual != ORIGINAL_HEAD or platform.python_version() != "3.13.16":
        raise ValueError("pinned original checkout and CPython required")
    if sys.get_int_max_str_digits() != 4300:
        raise ValueError("original default integer conversion limit required")
    runtime = root / "tobkiri_runtime"
    sys.path.insert(0, str(runtime))
    from core_runtime.authority import v4_store
    from core_runtime.authority.v4_models import authority_digest
    from cryptography.fernet import Fernet
    from tobkiri_protocol.canonical import canonical_digest

    if Path(v4_store.__file__).resolve() != (runtime / SOURCES[0]).resolve():
        raise ValueError("original Authority module was substituted")
    # Execute the exact original helper bodies, without importing unrelated
    # services or replacing either canonical codec with a test implementation.
    source_path = runtime / "core_runtime/bootstrap/profile_capture.py"
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    names = {"_authority_snapshot_digest", "_genesis_authority_snapshot_digest"}
    functions = [n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in functions} != names:
        raise ValueError("complete original snapshot helpers required")
    future = ast.ImportFrom(module="__future__",
                            names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *functions], type_ignores=[]))
    namespace = {"canonical_digest": canonical_digest, "authority_digest": authority_digest}
    exec(compile(module, str(source_path), "exec"), namespace)
    return v4_store.AuthorityStore, authority_digest, Fernet, namespace, runtime


def error_graph(error: BaseException, root: Path) -> dict:
    """Retain actual typed cause/context topology without process addresses."""
    seen: dict[int, int] = {}

    def visit(value):
        if value is None:
            return None
        identity = id(value)
        if identity in seen:
            return {"ref": seen[identity]}
        if len(seen) >= 32:
            raise ValueError("original exception graph exceeds bound")
        seen[identity] = len(seen)
        message = str(value).replace(str(root), "$USER_DATA")
        if len(message.encode()) > 16384:
            raise ValueError("original exception message exceeds bound")
        result = {"id": seen[identity],
                  "type": type(value).__module__ + "." + type(value).__qualname__,
                  "message": message, "suppressed": value.__suppress_context__,
                  "cause": visit(value.__cause__), "context": visit(value.__context__)}
        if isinstance(value, OSError):
            result["errno"] = value.errno
            result["filename"] = (str(value.filename).replace(str(root), "$USER_DATA")
                                  if value.filename is not None else None)
        return result

    return visit(error)


def mutate(path: Path, changes: dict[str, str | None]) -> None:
    """Test-only external state change between the two original reads."""
    allowed = {"security_epoch", "security_epoch_advanced_at", "security_epoch_reason_digest"}
    if not changes or not set(changes) <= allowed:
        raise ValueError("fixed fixture metadata mutation required")
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            for key, value in changes.items():
                if value is None:
                    connection.execute("DELETE FROM authority_meta WHERE key=?", (key,))
                else:
                    connection.execute("UPDATE authority_meta SET value=? WHERE key=?", (value, key))


def material(root: Path, store_type, helpers: dict, changes: dict | None) -> dict:
    """Execute actual context open/read/read/close; keep later digest separate."""
    path = root / "authority/v4.sqlite3"
    if not path.is_file():
        if path.exists():
            # This is the original caller's path decision, before its App-owned
            # denied exception factory. It is not a Python exception substitute.
            return {"outcome": "non-regular-path"}
        return {"outcome": "genesis", "security_epoch": "1",
                "epoch_record": None,
                "protocol_snapshot": helpers["_genesis_authority_snapshot_digest"](
                    "sha256:" + "a" * 64)}
    try:
        with store_type(path, clock=lambda: CLOCK) as store:
            first = store.security_epoch
            if changes:
                mutate(path, changes)
            record = store.security_epoch_record
        result = {"outcome": "material", "security_epoch": str(first),
                  "epoch_record": {"value": str(record.value),
                                   "advanced_at": record.advanced_at,
                                   "reason_digest": record.reason_digest}}
    except Exception as error:
        return {"outcome": "error", "error": error_graph(error, root)}
    # The finite native operation returns materials, not Core authority or a
    # digest bypassing the original protocol's safe-integer refusal.
    class ActualRecordView:
        security_epoch_record = record
    try:
        result["protocol_snapshot"] = helpers["_authority_snapshot_digest"](
            ActualRecordView(), "sha256:" + "a" * 64)
    except Exception as error:
        result["protocol_snapshot_error"] = error_graph(error, root)
    return result


def database_state(root: Path, inspection: Path, fernet_type) -> dict:
    """Observe a separate copy, including committed decrypted data and schema."""
    files = []
    authority = root / "authority"
    paths = ([authority, *sorted(authority.rglob("*"))] if authority.exists() else [])
    for path in paths:
        relative = str(path.relative_to(root))
        meta = path.lstat()
        row = {"path": relative, "mode": stat.S_IMODE(meta.st_mode)}
        if path.is_symlink():
            row["symlink"] = path.readlink().as_posix()
        elif path.is_dir():
            row["directory"] = True
        else:
            row["bytes"] = meta.st_size
        files.append(row)
    # Key bytes and database bytes never enter the JSON/artifact. The copy is
    # inspected so SQLite observer sidecars cannot alter the tested root.
    inspection.mkdir(mode=0o700)
    captured = inspection / "authority"
    if authority.is_symlink():
        captured.symlink_to(authority.readlink())
    elif authority.is_dir():
        shutil.copytree(authority, captured, symlinks=True)
    elif authority.exists():
        shutil.copy2(authority, captured)
    path = inspection / "authority/v4.sqlite3"
    result = {"scope": "original-authority-subtree", "files": files}
    if not path.is_file() or path.is_symlink():
        return result
    try:
        key = (inspection / "authority/v4.key").read_bytes()
        cipher = fernet_type(key)
    except (OSError, ValueError):
        cipher = None
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            tables = connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
            observed = {}
            for table, ddl in tables:
                # Names originate from SQLite; quote identifiers, never interpolate
                # caller SQL or treat a malformed database name as a command.
                quoted = '"' + table.replace('"', '""') + '"'
                cursor = connection.execute("SELECT * FROM " + quoted)
                names = [item[0] for item in cursor.description]
                rows = []
                for values in cursor:
                    row = {}
                    for name, value in zip(names, values):
                        if isinstance(value, bytes):
                            if name == "encrypted_payload" and cipher is not None:
                                try:
                                    value = {"decrypted": json.loads(cipher.decrypt(value))}
                                except Exception:
                                    value = {"encrypted_sha256": hashlib.sha256(value).hexdigest()}
                            else:
                                value = {"bytes_sha256": hashlib.sha256(value).hexdigest()}
                        row[name] = value
                    rows.append(row)
                rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
                # SQL whitespace is not the persisted-schema contract. Retain
                # actual columns/constraints and index structure instead.
                columns = connection.execute("PRAGMA table_info(" + quoted + ")").fetchall()
                indexes = []
                for index in connection.execute("PRAGMA index_list(" + quoted + ")"):
                    index_name = '"' + index[1].replace('"', '""') + '"'
                    indexes.append({"name": index[1], "unique": bool(index[2]),
                                    "origin": index[3], "partial": bool(index[4]),
                                    "columns": connection.execute(
                                        "PRAGMA index_info(" + index_name + ")").fetchall()})
                indexes.sort(key=lambda row: row["name"])
                observed[table] = {"columns": columns, "strict": ddl.rstrip().endswith("STRICT"),
                                   "indexes": indexes, "rows": rows}
            result["tables"] = observed
    except sqlite3.Error as error:
        result["inspection_error"] = error_graph(error, inspection)
    return result


def prepare(original_root: Path, destination: Path) -> None:
    """Prepare real original fixtures and fresh reference observations."""
    if destination.exists():
        raise ValueError("new CI fixture destination required")
    store_type, digest, fernet_type, helpers, runtime = original(original_root)
    destination.mkdir(mode=0o700, parents=True)
    specifications = [
        ("absent", "absent", None), ("non-file", "directory", None),
        ("dangling-db-symlink", "dangling", None), ("empty-db", "empty", None),
        ("normal-v4", "normal", None), ("historical-v1", "v1", None),
        ("historical-v2", "v2", None), ("historical-v3", "v3", None),
        ("unknown-table", "unknown-table", None), ("future-version", "future", None),
        ("missing-epoch-repaired", "missing-epoch", None),
        ("bad-key", "bad-key", None), ("wrong-key", "wrong-key", None),
        ("key-outer-whitespace", "key-whitespace", None),
        ("key-ignored-base64-bytes", "key-base64-noise", None),
        ("key-broad-mode", "key-broad", None),
        ("guard-broad-mode", "guard-broad", None),
        ("guard-directory", "guard-directory", None),
        ("guard-symlink", "guard-symlink", None),
        ("bad-audit-token", "bad-audit", None),
        ("reason-untagged", "reason-untagged", None),
        ("reason-invalid", "reason-invalid", None),
        ("record-nonfinite-time", "time-nan", None),
        ("between-read-change", "normal", {
            "security_epoch": "7", "security_epoch_advanced_at": "1001.25",
            "security_epoch_reason_digest": digest({"reason": "between-reads"})}),
        ("between-read-missing-record", "normal", {"security_epoch_reason_digest": None}),
    ]
    specifications.extend(("epoch-" + name, "epoch:" + text, None)
                          for name, text in EPOCH_TEXTS)
    reports = []
    for name, kind, changes in specifications:
        case = destination / name
        seed = case / "seed"
        seed.mkdir(mode=0o700, parents=True)
        authority = seed / "authority"
        path = authority / "v4.sqlite3"
        if kind not in {"absent", "directory", "dangling"}:
            authority.mkdir(mode=0o700)
            if kind == "empty":
                path.touch(mode=0o600)
            elif kind == "v1":
                from tests.test_authority_v4_schema_migration import historical_authority_v1
                historical = case / "historical"
                historical.mkdir(mode=0o700)
                fixture = historical_authority_v1.__wrapped__(historical)
                fixture.harness.store.close()
                # The source fixture's raw sqlite3 context commits but does
                # not itself close its connection. Copying only the main file
                # can omit committed WAL pages. SQLite backup captures the
                # actual committed v1 state, without a recovery/initialization.
                with closing(sqlite3.connect(fixture.path)) as live:
                    with closing(sqlite3.connect(path)) as captured:
                        live.backup(captured)
                path.chmod(0o600)
                for source, target in (
                    (fixture.key_path, path.with_suffix(".key")),
                    (fixture.path.with_name("." + fixture.path.name + ".lifecycle.lock"),
                     path.with_name("." + path.name + ".lifecycle.lock")),
                ):
                    shutil.copy2(source, target)
            else:
                with store_type(path, clock=lambda: CLOCK) as store:
                    store.advance_security_epoch("reference-fixture")
                if kind.startswith("epoch:"):
                    mutate(path, {"security_epoch": kind.removeprefix("epoch:")})
                elif kind == "missing-epoch":
                    mutate(path, {"security_epoch": None})
                elif kind in {"reason-untagged", "reason-invalid", "time-nan"}:
                    mutate(path, {"security_epoch_reason_digest": "b" * 64}
                           if kind == "reason-untagged" else
                           {"security_epoch_reason_digest": "invalid"}
                           if kind == "reason-invalid" else
                           {"security_epoch_advanced_at": "nan"})
                elif kind in {"future", "unknown-table", "bad-audit", "v2", "v3"}:
                    with closing(sqlite3.connect(path)) as connection:
                        with connection:
                            if kind in {"v2", "v3"}:
                                # These versions already have request-bound
                                # lease columns; their registration table is
                                # introduced by the original v4 initializer.
                                connection.execute("DROP TABLE host_extension_registrations")
                                connection.execute("UPDATE authority_meta SET value=?"
                                                   " WHERE key='schema_version'", (kind[1:],))
                            elif kind == "future":
                                connection.execute("UPDATE authority_meta SET value='999'"
                                                   " WHERE key='schema_version'")
                            elif kind == "unknown-table":
                                connection.execute("CREATE TABLE unexpected(value TEXT)")
                            else:
                                connection.execute("UPDATE authority_audit SET encrypted_payload=?",
                                                   (b"not-a-fernet-token",))
                elif kind in {"bad-key", "wrong-key"}:
                    path.with_suffix(".key").write_bytes(
                        b"invalid-key" if kind == "bad-key" else fernet_type.generate_key())
                elif kind in {"key-whitespace", "key-base64-noise"}:
                    key_path = path.with_suffix(".key")
                    key = key_path.read_bytes()
                    key_path.write_bytes(b" \t" + key + b"\r\n" if kind == "key-whitespace"
                                         else key[:5] + b"!\xff" + key[5:])
                elif kind == "key-broad":
                    path.with_suffix(".key").chmod(0o644)
                elif kind.startswith("guard-"):
                    guard = path.with_name("." + path.name + ".lifecycle.lock")
                    if kind == "guard-broad":
                        guard.chmod(0o644)
                    else:
                        guard.unlink()
                        if kind == "guard-directory":
                            guard.mkdir(mode=0o700)
                        else:
                            target = authority / "guard-target"
                            target.write_bytes(b"\0")
                            target.chmod(0o600)
                            guard.symlink_to(target.name)
        elif kind != "absent":
            authority.mkdir(mode=0o700)
            if kind == "directory":
                path.mkdir(mode=0o700)
            else:
                path.symlink_to("absent.sqlite3")
        reference, native = case / "reference", case / "native"
        shutil.copytree(seed, reference, symlinks=True)
        shutil.copytree(seed, native, symlinks=True)
        report = {"name": name, "user_data_dir": str(native), "clock": CLOCK,
                  "after_epoch_mutation": changes,
                  "expected": material(reference, store_type, helpers, changes),
                  "reference_state": database_state(reference, case / "inspection", fernet_type)}
        reports.append(report)
    metadata = {"format": FORMAT, "original_head": ORIGINAL_HEAD,
                "python": platform.python_version(), "native_comparison": False,
                "qualification": False, "production_enabled": False,
                "sources": {name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
                            for name in SOURCES}, "cases": reports}
    write_json(destination / "fixtures.json", metadata)
    summary = {key: value for key, value in metadata.items() if key != "cases"}
    summary["cases"] = [{"name": r["name"], "outcome": r["expected"]["outcome"],
                         "error_type": r["expected"].get("error", {}).get("type"),
                         "observation": r["expected"],
                         "late_protocol_refusal": "protocol_snapshot_error" in r["expected"]}
                        for r in reports]
    migrated = next(r for r in reports if r["name"] == "historical-v1")
    tables = migrated["reference_state"].get("tables", {})
    summary["historical_lease_states"] = sorted(
        row["state"] for row in tables.get("invocation_leases", {}).get("rows", []))
    summary["reference_checks_passed"] = False
    write_json(destination / "summary.json", summary)
    by_name = {item["name"]: item for item in reports}
    if len(reports) != CASE_COUNT:
        raise AssertionError("closed original Authority case inventory differs")
    successful = {"empty-db", "normal-v4", "historical-v1", "historical-v2", "historical-v3",
                  "missing-epoch-repaired",
                  "key-outer-whitespace", "key-ignored-base64-bytes", "reason-untagged",
                  "between-read-change", "epoch-plus-leading-zero", "epoch-underscore",
                  "epoch-unicode-decimal", "epoch-unicode-whitespace", "epoch-i-json-boundary",
                  "epoch-above-i-json", "epoch-above-u64", "epoch-hundred-digits"}
    for name, report in by_name.items():
        expected = ("genesis" if name in {"absent", "dangling-db-symlink"} else
                    "non-regular-path" if name == "non-file" else
                    "material" if name in successful else "error")
        if report["expected"]["outcome"] != expected:
            raise AssertionError((name, expected, report["expected"]))
    for name in ("absent", "dangling-db-symlink"):
        if "tables" in by_name[name]["reference_state"]:
            raise AssertionError("absent branch initialized an Authority database")
    changed = by_name["between-read-change"]["expected"]
    if (changed["security_epoch"], changed["epoch_record"]["value"]) != ("2", "7"):
        raise AssertionError("the separate original reads were collapsed")
    if any("protocol_snapshot_error" not in by_name[name]["expected"] for name in
           ("epoch-above-i-json", "epoch-above-u64", "epoch-hundred-digits")):
        raise AssertionError("late original protocol safe-integer refusal was bypassed")
    migrated = by_name["historical-v1"]["reference_state"]["tables"]
    if {row["state"] for row in migrated["invocation_leases"]["rows"]} != {"revoked", "ambiguous"}:
        raise AssertionError(("actual historical lease migration did not run",
                              summary["historical_lease_states"]))
    for name in ("historical-v1", "historical-v2", "historical-v3"):
        tables = by_name[name]["reference_state"]["tables"]
        meta = {row["key"]: row["value"] for row in tables["authority_meta"]["rows"]}
        if meta["schema_version"] != "4" or "host_extension_registrations" not in tables:
            raise AssertionError("actual supported schema upgrade did not finish")
    summary["reference_checks_passed"] = True
    write_json(destination / "summary.json", summary)
    print(json.dumps({"format": FORMAT, "cases": len(reports),
                      "original_head": ORIGINAL_HEAD, "native_comparison": False,
                      "qualification": False, "production_enabled": False}))


def load_json(path: Path):
    """Reject duplicate fields, nonfinite values and oversized observation files."""
    if path.stat().st_size > LIMIT:
        raise ValueError("native Authority observation exceeds bound")

    def pairs(items):
        output = {}
        for name, value in items:
            if name in output:
                raise ValueError("duplicate Authority observation field")
            output[name] = value
        return output

    def nonfinite(value):
        raise ValueError("nonfinite Authority observation")

    return json.loads(path.read_bytes(), object_pairs_hook=pairs, parse_constant=nonfinite)


def first_difference(expected, actual, path="$"):
    """Compare types as well as values; JSON true must not equal integer one."""
    if type(expected) is not type(actual):
        return path + ": type differs"
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return path + ": fields differ"
        for name in expected:
            difference = first_difference(expected[name], actual[name], path + "." + name)
            if difference:
                return difference
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return path + ": length differs"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = first_difference(left, right, path + "[" + str(index) + "]")
            if difference:
                return difference
    elif expected != actual:
        return path + ": value differs"
    return None


def logical_state(value: dict) -> dict:
    """Keep fixed Authority effects; SQLite physical page layout is separate."""
    value = dict(value)
    value["files"] = [dict(row) for row in value["files"]]
    for row in value["files"]:
        if Path(row["path"]).name in {"v4.sqlite3", "v4.sqlite3-wal", "v4.sqlite3-shm"}:
            row.pop("bytes", None)
    return value


def compare(original_root: Path, fixture_path: Path, observations: Path, output: Path) -> None:
    """Compare actual native results and independently inspect their real DB effects."""
    _, _, fernet_type, _, _ = original(original_root)
    fixtures, native = load_json(fixture_path), load_json(observations)
    if (fixtures["format"] != FORMAT or fixtures["original_head"] != ORIGINAL_HEAD
            or native["format"] != "io.tobkiri.original-authority-native-observations.v1"):
        raise ValueError("fresh original/native fixture formats required")
    expected_names = {row["name"] for row in fixtures["cases"]}
    actual = {row["name"]: row for row in native["cases"]}
    if (len(expected_names) != CASE_COUNT or len(actual) != CASE_COUNT
            or len(native["cases"]) != CASE_COUNT):
        raise ValueError("exact closed original/native observation inventory required")
    if set(actual) != expected_names:
        raise ValueError("native Authority case inventory differs")
    report = {"format": "io.tobkiri.original-authority-comparison.v1",
              "original_head": ORIGINAL_HEAD, "cases": [], "matched": False,
              "production_enabled": False, "qualification": False,
              "scope": "finite old-state materials, typed failures and Authority effects only"}
    destination = fixture_path.parent
    for case in fixtures["cases"]:
        name = case["name"]
        expected = {key: value for key, value in case["expected"].items()
                    if key not in {"protocol_snapshot", "protocol_snapshot_error"}}
        observed = {key: value for key, value in actual[name].items() if key != "name"}
        difference = first_difference(expected, observed)
        state = database_state(Path(case["user_data_dir"]),
                               destination / name / "native-inspection", fernet_type)
        state_difference = first_difference(logical_state(case["reference_state"]),
                                            logical_state(state))
        # Existing key bytes are immutable input, including noncanonical but
        # valid encoded forms. Newly generated test keys need not equal random
        # bytes generated by the independent original run.
        seed_key = destination / name / "seed/authority/v4.key"
        key_difference = False
        if seed_key.is_file():
            for target in ("reference", "native"):
                key = destination / name / target / "authority/v4.key"
                key_difference |= not key.is_file() or key.read_bytes() != seed_key.read_bytes()
        elif observed.get("outcome") == "material":
            for target in ("reference", "native"):
                fernet_type((destination / name / target / "authority/v4.key").read_bytes().strip())
        report["cases"].append({"name": name, "material_difference": difference,
                                "state_difference": state_difference,
                                "existing_key_changed": key_difference})
    report["matched"] = all(not (case["material_difference"] or case["state_difference"]
                                  or case["existing_key_changed"]) for case in report["cases"])
    write_json(output, report)
    if not report["matched"]:
        failures = [row for row in report["cases"] if row["material_difference"]
                    or row["state_difference"] or row["existing_key_changed"]]
        raise AssertionError(failures)
    print(json.dumps({"native_comparison": True, "matched": True, "cases": CASE_COUNT,
                      "qualification": False, "production_enabled": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("requirements", "prepare", "compare"))
    parser.add_argument("--original-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fixtures", type=Path)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    root = args.original_root.resolve(strict=True)
    if args.command == "requirements":
        requirements(root, args.output)
    elif args.command == "prepare":
        prepare(root, args.output)
    elif args.fixtures is None or args.observations is None:
        parser.error("compare requires fixtures and native observations")
    else:
        compare(root, args.fixtures, args.observations, args.output)


if __name__ == "__main__":
    main()
