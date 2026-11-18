"""Source staging and guest routing tests without any native extension."""

import ast
import base64
import hashlib
import itertools
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import component_inputs  # noqa: E402
import build_components  # noqa: E402
import app_schema_extension_inputs as app_schema  # noqa: E402
import harness_native_dependencies as dependencies  # noqa: E402
from fetch_sdk import inventory  # noqa: E402
from schema_probe.captured_loader import Tree  # noqa: E402


def identity(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


class CatalogWireDecoder(unittest.TestCase):
    def setUp(self):
        pack = HERE.parents[1] / "packs/local.ai-tool-bridge-ordered-python"
        self.v1 = ModuleType("ordered_json")
        exec(compile((pack / "ordered_json.py").read_bytes(), "<pinned-ordered-json>", "exec"),
             self.v1.__dict__)
        source = (pack / "ordered_json_v2.py").read_bytes()
        derived = app_schema.derive_catalog_wire_decoder(source)
        self.original = ModuleType("ordered_json_v2")
        self.codec = ModuleType("ordered_json_v2_app_catalog")
        with patch.dict(sys.modules, ordered_json=self.v1):
            exec(compile(source, "<pinned-ordered-json-v2>", "exec"), self.original.__dict__)
            exec(compile(derived, "<app-catalog-wire>", "exec"), self.codec.__dict__)

    def assert_matches_error(self, frame):
        with self.assertRaises(self.v1.OrderedJSONError) as original:
            self.original.loads(frame)
        with self.assertRaises(self.v1.OrderedJSONError) as derived:
            self.codec.loads(frame)
        self.assertEqual(derived.exception.args, original.exception.args)
        expected = original.exception.__cause__
        observed = derived.exception.__cause__
        self.assertIs(type(observed), type(expected))
        if expected is not None:
            self.assertEqual(observed.args, expected.args)

    def test_staged_string_scan_preserves_json_rules_and_chunk_boundaries(self):
        alphabet = ('"', "\\", "A", "\n", "é", "😀")
        for length in range(5):
            for characters in itertools.product(alphabet, repeat=length):
                text = "".join(characters)
                self.assertEqual(
                    self.codec._CATALOG_STRING_RUN.match(text).end(),
                    self.v1._STRING_RUN.match(text).end(),
                )
        self.v1.STRING_CHUNK_SIZE = 7
        for offset in range(7):
            payload = "A" * (31 + offset) + '\\"line\n😀' + "x" * 33
            frame = json.dumps({"payload": payload}, ensure_ascii=False)
            self.assertEqual(self.codec.loads(frame), self.original.loads(frame))
        for frame in ['{"payload":"' + "x" * 21 + '\n' + "x" * 21 + '"}',
                      '{"payload":"\\ud800"}', '{"a":1,"a":2}']:
            self.assert_matches_error(frame)

    def test_decoded_validation_preserves_numeric_and_unicode_errors(self):
        self.v1.STRING_CHUNK_SIZE = 7
        frame = r'{"é":[{"\ud83d\ude00":"\u2603"},-0.0,true,null]}'
        self.assertEqual(self.codec.loads(frame), self.original.loads(frame))
        for frame in (r'{"data":[{"abcdefg\udfff":"value"}]}',
                      r'{"data":[{"key":"abcdefg\ud800"}]}',
                      '{"data":[1e999]}', '{"data":[NaN]}',
                      '{"data":[' + '1' * 4301 + ']}'):
            with self.subTest(frame=frame[:100]):
                self.assert_matches_error(frame)

    def test_decoded_validation_counts_container_keys_and_values(self):
        frame = '{"data":[{"a":"b","c":"d"},{"e":"f"}]}'
        self.original.MAX_NODES = self.codec.MAX_NODES = 11
        self.assertEqual(self.codec.loads(frame), self.original.loads(frame))
        self.original.MAX_NODES = self.codec.MAX_NODES = 10
        self.assert_matches_error(frame)
        # Isolate the second node check from the token preflight.
        with patch.object(self.original, "_preflight"), patch.object(self.codec, "_preflight"):
            self.assert_matches_error(frame)

    def test_catalog_page_decode_does_not_reencode_base64(self):
        encoded = base64.b64encode(b"catalog document bytes" * 12000).decode("ascii")
        frame = json.dumps({"kind": "snapshot-byte-rows", "rows": [["pack", "test", encoded]]})
        expected = self.original.loads(frame)
        with patch.object(self.v1, "encode_basestring", side_effect=AssertionError):
            self.assertEqual(self.codec.loads(frame), expected)


def fixture(root):
    """Tiny synthetic dependency files used only as inert source DATA."""
    schema, stdlib, rpds = (root / name for name in ("schema", "stdlib", "rpds"))
    pins = []
    for name in (
        "attrs",
        "jsonschema",
        "jsonschema-specifications",
        "referencing",
        "packaging",
        "rpds-py",
    ):
        native = name == "rpds-py"
        package = "rpds" if native else name.replace("-", "_")
        files = {package + "/__init__.py": b"# inert dependency fixture\n"}
        if not native:
            files[name + "-1.dist-info/METADATA"] = (
                "Name: " + name + "\nVersion: 1\n\n"
            ).encode()
        else:
            files[package + "/native.so"] = b"excluded native platform extension"
        base = schema / "packages" / ("rpds-py-native" if native else name)
        for relative, raw in files.items():
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        pins.append(
            {
                "distribution": name,
                "role": "native-oracle" if native else "pure",
                "files": {path: identity(raw) for path, raw in files.items()},
            }
        )
    original_stdlib = b"# inert original stdlib source fixture\n"
    target = stdlib / "source/subprocess.py"
    target.parent.mkdir(parents=True)
    target.write_bytes(original_stdlib)
    extension = rpds / "work/stage/rpds.abi3.so"
    extension.parent.mkdir(parents=True)
    extension.write_bytes(b"inert synthetic Wasm extension; never imported")
    verified = {
        "inputs": pins,
        "stdlib": {"files": {"subprocess.py": identity(original_stdlib)}},
        "extension": identity(extension.read_bytes()),
        "predecessors": {},
        "rpds_head": dependencies.build_schema.RPDS_HEAD,
        "ui_clock_variant": False,
    }
    return schema, stdlib, rpds, verified


class NativeHarnessDependencyTests(unittest.TestCase):
    def test_app_backend_stages_live_provider_and_derives_only_declared_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stage = root / "native"
            original = component_inputs.stage_harness_native(HERE.parents[1], stage)
            before = {path: (stage / path).read_bytes() for path in original["files"]}
            schema, stdlib, rpds, verified = fixture(root)
            kernel = root / "kernel"
            artifact = kernel / app_schema.ARTIFACT
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"\x00asm\x01\x00\x00\x00")
            source_head = "source selected by CI"
            extension_build = {"format": "tobkiri.app-schema-kernel-extension/1",
                               "outcome": "success", "guest_executed": False,
                               "production_enabled": False, "source_head": source_head,
                               "features": ["app-schema-backend"],
                               "compiled_features": ["app-schema-backend", "default"],
                               "source_files": inventory(app_schema.SOURCE),
                               "artifact_path": app_schema.ARTIFACT,
                               "artifact": identity(artifact.read_bytes())}
            (kernel / "extension-build.json").write_text(json.dumps(extension_build))
            with patch.dict(app_schema.os.environ, {"GITHUB_ACTIONS": "true",
                                                     "GITHUB_SHA": source_head}):
                selected = app_schema.verify_extension(kernel, ["app-schema-backend"])
                with self.assertRaisesRegex(ValueError, "source-built kernel derivation"):
                    app_schema.verify_extension(kernel, ["app-schema-backend", "app-schema-phases"])
                result = dependencies.stage(stage, original, verified, schema, stdlib, rpds,
                                            app_extension=selected)
                from harness_native_ci_inputs import verify_app_source_bindings

                metadata = json.loads((stage / "python" / dependencies.METADATA).read_bytes())
                source = {row["source"]: row for row in result["source_files"]}
                verify_app_source_bindings({"harness_native_dependencies": metadata}, source)
                bad_kernel = "python/" + app_schema.EXTENSION
                with self.assertRaisesRegex(ValueError, "App dependency differs"):
                    verify_app_source_bindings({"harness_native_dependencies": metadata},
                                               {**source, bad_kernel: {**source[bad_kernel], "sha256": "wrong"}})
            changed = {path for path, raw in before.items() if (stage / path).read_bytes() != raw}
            self.assertEqual(changed, {"python/tobkiri_protocol/validation.py",
                                       "python/tobkiri_protocol/bundle_catalog.py",
                                       "python/ordered_json_v2.py",
                                       "python/harness_native_source_index.py", "plan.json"})
            self.assertIn(b"from app_schema_provider import AppSchemaProvider as Draft202012Validator",
                          (stage / "python/tobkiri_protocol/validation.py").read_bytes())
            catalog = (stage / "python/tobkiri_protocol/bundle_catalog.py").read_bytes()
            self.assertIn(b"return cls._load_native_snapshot(root)", catalog)
            self.assertIn(b'snapshot-byte-rows', catalog)
            self.assertIn(b"kind, value = path._read_catalog_entry(index)", catalog)
            self.assertIn(b"return path._read_catalog_pin_digest(index)", catalog)
            self.assertIn(b"document = json.loads(raw)", catalog)
            self.assertNotIn(b"_semantic_diagnostics", catalog)
            compile(catalog, "bundle_catalog.py", "exec")
            self.assertIn(b"_CATALOG_STRING_RUN.match",
                          (stage / "python/ordered_json_v2.py").read_bytes())
            self.assertFalse(metadata["original_source_files_unchanged"])
            self.assertFalse(metadata["app_schema_backend"]["validation_results_prepared"])

    def test_derived_catalog_accepts_typed_reads_only_from_the_native_path(self):
        repo = HERE.parents[1]
        runtime = repo / "tobkiri_runtime"
        source = runtime / "tobkiri_protocol/bundle_catalog.py"
        module = ModuleType("tobkiri_protocol._catalog_routing_test")
        module.__package__ = "tobkiri_protocol"
        packs = runtime / "ecosystem/defaultspack/v4/packs"
        pack_id = "rumi_discord_connector_pack"
        raw = (packs / f"{pack_id}.pack.v4.json").read_bytes()
        executable = (packs / f"{pack_id}.executables.v4.json").read_bytes()
        text = raw.decode()
        document = json.loads(text)
        relative = "./pack.json"
        executable_relative = "packs//catalog.json"

        def digest(content: bytes) -> str:
            return "sha256:" + hashlib.sha256(content).hexdigest()

        def make_lock(
            content: bytes,
            executable_content: bytes | None = None,
            *,
            kind: str = "pack",
        ) -> bytes:
            entries = [{"path": relative, "kind": kind, "digest": digest(content)}]
            if executable_content is not None:
                entries.append({
                    "path": executable_relative,
                    "kind": "executable_catalog",
                    "digest": digest(executable_content),
                })
            return json.dumps({
                "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
                "entries": entries,
            }).encode()

        lock = make_lock(raw)
        reply = {"kind": "document", "value": text}
        executable_text = executable.decode()
        pin_digest = digest(executable)
        reads = []

        def inspect(payload: str) -> str:
            request = json.loads(payload)
            operation = request["operation"]
            target = request["target"]
            steps = target["steps"]
            if operation in {"root", "resolve"}:
                self.assertEqual(steps, [])
                result = {"kind": "path", "binding": "root", "value": "/bundle"}
            elif operation == "is-symlink":
                self.assertIn(steps, [[], [["join", "bundle.lock.json"]]])
                result = {"kind": "bool", "value": False}
            elif operation == "is-dir":
                self.assertEqual(steps, [["parent", ""], ["join", "platform-artifacts"]])
                result = {"kind": "bool", "value": False}
            elif operation == "read-bytes":
                self.assertEqual(steps, [["join", "bundle.lock.json"]])
                result = {"kind": "bytes", "value": base64.b64encode(lock).decode()}
            else:
                index, = request["args"]
                reads.append((operation, index))
                if operation == "read-entry" and index == 0:
                    self.assertEqual(steps, [["join", relative]])
                    result = reply
                else:
                    self.assertEqual(index, 1)
                    self.assertEqual(steps, [["join", executable_relative]])
                    if operation == "read-entry":
                        result = {"kind": "document", "value": executable_text}
                    else:
                        self.assertEqual(operation, "read-pin-digest")
                        result = {"kind": "catalog-pin-digest", "value": pin_digest}
            return json.dumps({"format": "io.tobkiri.native-inspection-response.v1",
                               "scope": request["scope"], "result": result})

        paths = [runtime, repo / "packs/local.ai-tool-bridge-ordered-python"]
        with (
            patch.object(sys, "path", [*map(str, paths), *sys.path]),
            patch.dict(sys.modules, {module.__name__: module}),
        ):
            from harness_native.native_path import PathBridge
            from harness_native.value_graph import Scope
            from tobkiri_protocol import validation

            bridge = PathBridge(Scope(1, 2, 3, 4), "posix", inspect,
                                os_flags={"O_RDONLY": 0})
            root = bridge.bind("root", "/bundle")
            with patch.object(validation, "_semantic_diagnostics",
                              wraps=validation._semantic_diagnostics) as semantic:
                exec(compile(app_schema.derive_catalog_validation(source.read_bytes()),
                             str(source), "exec"), module.__dict__)
                module.Path = bridge.module_path(str(source), root)
                catalog = module.BundledCatalog
                with (
                    patch.object(module, "strict_loads",
                                 wraps=module.strict_loads) as strict,
                    patch.object(module, "validate_document",
                                 wraps=module.validate_document) as validate,
                ):
                    loaded = bridge._load_catalog(catalog.load, root)
                    self.assertEqual(loaded.packs[pack_id], document)
                    strict.assert_called_once_with(lock)
                    validate.assert_not_called()
                    semantic.assert_not_called()

                    for status, error_type, message in (
                        ("escape", module.BundleIntegrityError,
                         "bundle entry escapes root: ./pack.json"),
                        ("symlink", module.BundleIntegrityError,
                         "bundle entry contains a symlink: ./pack.json"),
                        ("null-byte", ValueError, "lstat: embedded null character in path"),
                    ):
                        with self.subTest(status=status):
                            reply = {"kind": "catalog-path-rejection", "value": status}
                            reads.clear()
                            with self.assertRaises(error_type) as error:
                                bridge._load_catalog(catalog.load, root)
                            self.assertEqual(str(error.exception), message)
                            self.assertEqual(reads, [("read-entry", 0)])

                    bad_raw = b'{"pack": {}, "pack": {}}'
                    reply = {
                        "kind": "bytes", "value": base64.b64encode(bad_raw).decode(),
                    }
                    with self.assertRaisesRegex(
                        module.BundleIntegrityError,
                        r"bundle artifact digest changed: \./pack\.json",
                    ):
                        bridge._load_catalog(catalog.load, root)
                    validate.assert_not_called()

                    lock = make_lock(bad_raw)
                    with self.assertRaisesRegex(
                        module.BundleIntegrityError, r"invalid pack document \./pack\.json",
                    ) as error:
                        bridge._load_catalog(catalog.load, root)
                    validate.assert_called_once_with(bad_raw, "pack")
                    self.assertEqual(error.exception.__cause__.diagnostics,
                                     ("duplicate JSON object key: 'pack'",))

                    for value in (text, document):
                        with (
                            self.subTest(kind=type(value)),
                            self.assertRaises(TypeError),
                        ):
                            bridge._load_catalog(
                                lambda selected: catalog.load_from_reader(
                                    lock, entry_reader=lambda *_: value, root=selected,
                                    artifact_root_resolver=lambda: None,
                                ),
                                root,
                            )

                    reply = {"kind": "document", "value": text}
                    lock = make_lock(raw, executable)
                    reads.clear()
                    loaded = bridge._load_catalog(catalog.load, root)
                    self.assertEqual(loaded.executable_catalogs[pack_id],
                                     json.loads(executable_text))
                    self.assertEqual(reads, [
                        ("read-entry", 0), ("read-entry", 1), ("read-pin-digest", 1),
                    ])

                    different_bytes = executable + b"\n"
                    for first, reread in ((different_bytes, executable),
                                          (different_bytes, different_bytes)):
                        with self.subTest(pin_matches=(reread == executable)):
                            lock = make_lock(raw, first)
                            executable_text = first.decode()
                            pin_digest = digest(reread)
                            with self.assertRaisesRegex(
                                module.BundleIntegrityError,
                                f"Pack executable catalog artifact pin is stale: {pack_id}",
                            ):
                                bridge._load_catalog(catalog.load, root)

                    lock = make_lock(raw, executable)
                    validate.side_effect = lambda content, _kind: json.loads(content)
                    reads.clear()
                    bridge._load_catalog(
                        lambda selected: catalog.load_from_reader(
                            lock,
                            entry_reader=lambda i, _path, _reread: (raw, executable)[i],
                            root=selected,
                            artifact_root_resolver=lambda: None,
                        ),
                        root,
                    )
                    self.assertEqual(reads, [])

                    relative = "defaults-basepack.base.v1.json"
                    base_raw = (packs.parent / relative).read_bytes()
                    base = json.loads(base_raw)
                    stale_base = json.dumps({
                        **base, "definition_revision": "sha256:" + "0" * 64,
                    }).encode()
                    validate.reset_mock()
                    with patch.object(module, "canonical_digest",
                                      wraps=module.canonical_digest) as revision_digest:
                        lock = make_lock(base_raw, kind="base")
                        reply = {"kind": "document", "value": base_raw.decode()}
                        loaded = bridge._load_catalog(catalog.load, root)
                        self.assertEqual(loaded.bases[base["pack_id"]], base)
                        validate.assert_not_called()
                        revision_digest.assert_not_called()

                        lock = make_lock(stale_base, kind="base")
                        reply = {
                            "kind": "bytes",
                            "value": base64.b64encode(stale_base).decode(),
                        }
                        with self.assertRaises(module.BundleIntegrityError) as error:
                            bridge._load_catalog(catalog.load, root)
                        self.assertEqual(
                            str(error.exception),
                            "base definition revision is stale or tampered: "
                            f"{relative}",
                        )
                        validate.assert_called_once_with(stale_base, "base")
                        revision_digest.assert_called_once_with({
                            key: value
                            for key, value in base.items()
                            if key != "definition_revision"
                        })

                    def load_pack_entries(*responses: bytes | str) -> list[str]:
                        entries = [{
                            "path": f"pack-{index}.json",
                            "kind": "pack",
                            "digest": digest(
                                response.encode() if type(response) is str else response
                            ),
                        } for index, response in enumerate(responses)]
                        identity_lock = json.dumps({
                            "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
                            "entries": entries,
                        }).encode()
                        loaded = catalog._load_from_reader(
                            identity_lock,
                            entry_reader=lambda i, _path, _reread: responses[i],
                            root=root,
                            artifact_root_resolver=lambda: None,
                            native_reads=True,
                        )
                        return list(loaded.packs)

                    other_pack = (packs / "defaults-basepack.pack.v4.json").read_bytes()
                    self.assertEqual(
                        load_pack_entries(text, other_pack.decode()),
                        [pack_id, "defaults-basepack"],
                    )
                    self.assertEqual(
                        load_pack_entries(text, other_pack),
                        [pack_id, "defaults-basepack"],
                    )
                    for responses in ((text, raw), (text, other_pack, text)):
                        with (
                            self.subTest(occurrences=len(responses)),
                            self.assertRaises(module.BundleIntegrityError) as error,
                        ):
                            load_pack_entries(*responses)
                        self.assertEqual(
                            str(error.exception),
                            f"duplicate or missing pack identity: {pack_id!r}",
                        )

    def test_derived_catalog_materializes_snapshot_byte_rows_in_lock_order(self):
        repo = HERE.parents[1]
        runtime = repo / "tobkiri_runtime"
        source = runtime / "tobkiri_protocol/bundle_catalog.py"
        module = ModuleType("tobkiri_protocol._catalog_snapshot_test")
        module.__package__ = "tobkiri_protocol"
        with (
            patch.object(sys, "path", [str(runtime),
                         str(repo / "packs/local.ai-tool-bridge-ordered-python"), *sys.path]),
            patch.dict(sys.modules, {module.__name__: module}),
        ):
            from harness_native.exception_graph import ExceptionGraphStore
            from harness_native.native_path import PathBridge
            from harness_native.value_graph import Scope, UnsupportedValue
            from tobkiri_protocol import bundle_catalog as original_catalog
            from tobkiri_protocol.errors import (
                CanonicalizationError,
                SchemaValidationError,
            )

            exec(compile(app_schema.derive_catalog_validation(source.read_bytes()),
                         str(source), "exec"), module.__dict__)
            scope = Scope(1, 2, 3, 4)
            opened = {
                "kind": "snapshot-open",
                "root": "/resolved/bundle",
                "artifact_root": "/resolved/platform-artifacts",
                "count": 6,
            }
            def byte_frame(raw: bytes) -> dict[str, str]:
                return {"kind": "bytes", "value": base64.b64encode(raw).decode("ascii")}

            def page(*rows: tuple[str, str, bytes]) -> dict:
                return {
                    "kind": "snapshot-byte-rows",
                    "rows": [
                        [kind, identity, byte_frame(raw)["value"]]
                        for kind, identity, raw in rows
                    ],
                }

            first = page(
                ("pack", "z", '{"nested":{"z":-0,"a":"é"},"pack":{"id":"z"}}'.encode()),
                ("base", "base", b'{"pack_id":"base"}'),
                ("shell", "shell", b'{"provider_id":"shell"}'),
            )
            last = page(
                ("profile", "profile", b'{"profile_id":"profile"}'),
                ("executable_catalog", "a", b'{"pack_id":"a"}'),
                ("pack", "a", b'{"pack":{"id":"a"}}'),
            )

            def load(rows, *, decode_error=None):
                scripted = [
                    ("root", [], {"kind": "snapshot-root", "binding": "root", "value": "/bundle"}),
                    *rows,
                ]

                def inspect(payload: str) -> str:
                    request = json.loads(payload)
                    operation, args, result = scripted.pop(0)
                    self.assertEqual(request, {
                        "format": "io.tobkiri.native-catalog-request.v1",
                        "scope": scope.wire(),
                        "target": {"binding": "root", "steps": []},
                        "operation": operation,
                        "args": args,
                    })
                    return json.dumps({"format": "io.tobkiri.native-inspection-response.v1",
                                       "scope": scope.wire(), "result": result})

                bridge = PathBridge(scope, "posix", inspect, os_flags={"O_RDONLY": 0},
                                    decode_error=decode_error)
                root = bridge.bind("root", "/bundle")
                module.Path = bridge.module_path(str(source), root)
                try:
                    return bridge._load_catalog(module.BundledCatalog.load, root)
                finally:
                    self.assertEqual(scripted, [])
                    self.assertIsNone(bridge._catalog_snapshot_root)
                    self.assertIsNone(bridge._catalog_snapshot_result)

            catalog = load([
                ("snapshot-open", [], opened),
                ("snapshot-byte-rows", [0], first),
                ("snapshot-byte-rows", [3], last),
                ("snapshot-close", [6], {"kind": "unit"}),
            ])
            self.assertIs(type(catalog), module.BundledCatalog)
            self.assertEqual(list(catalog.packs), ["z", "a"])
            self.assertEqual(list(catalog.packs["z"]), ["nested", "pack"])
            self.assertEqual(list(catalog.packs["z"]["nested"]), ["z", "a"])
            self.assertEqual(catalog.packs["z"]["nested"], {"z": 0, "a": "é"})
            self.assertEqual(catalog.packs["a"], {"pack": {"id": "a"}})
            self.assertEqual(catalog.bases, {"base": {"pack_id": "base"}})
            self.assertEqual(catalog.shells, {"shell": {"provider_id": "shell"}})
            self.assertEqual(catalog.profiles, {"profile": {"profile_id": "profile"}})
            self.assertEqual(catalog.executable_catalogs, {"a": {"pack_id": "a"}})
            self.assertEqual(str(catalog.root), "/resolved/bundle")
            self.assertEqual(catalog.root._target(), {"binding": "root", "steps": []})
            self.assertEqual(str(catalog.artifact_root), "/resolved/platform-artifacts")
            self.assertEqual(catalog.artifact_root._target(), {
                "binding": "root", "steps": [["parent", ""], ["join", "platform-artifacts"]],
            })

            original_pack = runtime / (
                "ecosystem/defaultspack/v4/packs/defaults-basepack.pack.v4.json"
            )
            oversized = json.loads(original_pack.read_bytes())
            oversized["pack"]["description"] = "é" + '\\"\n' * 450_000
            source_text = json.dumps(oversized, ensure_ascii=False)
            raw_frame = byte_frame(source_text.encode())
            wire_max = 4 * 1024 * 1024
            self.assertGreater(
                len(json.dumps(source_text, ensure_ascii=False).encode()), wire_max,
            )
            self.assertLess(len(raw_frame["value"]), wire_max)
            with_bytes = load([
                ("snapshot-open", [], {**opened, "count": 7}),
                ("snapshot-byte-rows", [0], first),
                ("snapshot-byte-rows", [3], raw_frame),
                ("snapshot-byte-rows", [4], last),
                ("snapshot-close", [7], {"kind": "unit"}),
            ])
            self.assertEqual(list(with_bytes.packs), ["z", "defaults-basepack", "a"])
            decoded = with_bytes.packs["defaults-basepack"]
            self.assertEqual(list(decoded), list(oversized))
            self.assertEqual(list(decoded["pack"]), list(oversized["pack"]))
            self.assertTrue(decoded == oversized)

            for raw in (
                b'{"pack_api_version":"io.tobkiri.pack.v5","pack":{"id":"z"}}',
                b'{"pack_api_version":"io.tobkiri.pack.v4",'
                b'"base_api_version":"io.tobkiri.base.v4"}',
                b'{"pack_api_version":"io.tobkiri.pack.v4","pack":{}}',
                b'{"pack_api_version":"io.tobkiri.pack.v4","pack":{"id":"z"}}',
            ):
                with self.subTest(raw=raw), self.assertRaises(UnsupportedValue):
                    load([
                        ("snapshot-open", [], {**opened, "count": 4}),
                        ("snapshot-byte-rows", [0], first),
                        ("snapshot-byte-rows", [3], byte_frame(raw)),
                    ])

            for invalid in (
                page(),
                first,
                {"kind": "snapshot-rows", "rows": first["rows"][:1]},
                page(("pack", "other", b'{"pack":{"id":"z"}}')),
                page(("pack", "z", b"{")),
                page(("pack", "z", b"[]")),
                page(("pack", "z", b'{"pack":{"id":"z"},"invalid":"\xff"}')),
                page(("pack", "z", '{"pack":{"id":"z"}}'.encode("utf-16"))),
            ):
                with (
                    self.subTest(invalid=invalid),
                    self.assertRaises(UnsupportedValue),
                ):
                    load([
                        ("snapshot-open", [], {**opened, "count": 1}),
                        ("snapshot-byte-rows", [0], invalid),
                    ])
            for encoded in ("AA$=", "Y Q==", "YQ=", "YQ==YQ==", "é"):
                with (
                    self.subTest(encoded=encoded),
                    self.assertRaisesRegex(UnsupportedValue, "invalid native byte result"),
                ):
                    load([
                        ("snapshot-open", [], {**opened, "count": 2}),
                        ("snapshot-byte-rows", [0], {
                            "kind": "snapshot-byte-rows",
                            "rows": [first["rows"][0], ["pack", "next", encoded]],
                        }),
                    ])
            with self.assertRaisesRegex(UnsupportedValue, "snapshot identity"):
                load([
                    ("snapshot-open", [], {**opened, "count": 4}),
                    ("snapshot-byte-rows", [0], first),
                    ("snapshot-byte-rows", [3], {
                        "kind": "snapshot-byte-rows", "rows": first["rows"][:1],
                    }),
                ])
            with self.assertRaises(UnsupportedValue):
                load([
                    ("snapshot-open", [], {**opened, "count": 3}),
                    ("snapshot-byte-rows", [0], first),
                    ("snapshot-close", [3], {"kind": "unit", "unexpected": True}),
                ])

            for tag, error_type in (
                ("integrity", module.BundleIntegrityError),
                ("type-error", TypeError),
                ("value-error", ValueError),
            ):
                with self.subTest(tag=tag), self.assertRaises(error_type) as exc:
                    load([("snapshot-open", [], {
                        "kind": "snapshot-error", "error": tag, "message": "original message",
                    })])
                self.assertEqual(str(exc.exception), "original message")
                self.assertIsNone(exc.exception.__cause__)

            invalid_json = "duplicate JSON object key: 'entries'"

            def immediate_cause(error: BaseException) -> BaseException:
                cause = error.__cause__
                self.assertIsNotNone(cause)
                self.assertIs(error.__context__, cause)
                self.assertTrue(error.__suppress_context__)
                self.assertIsNone(cause.__cause__)
                self.assertIsNone(cause.__context__)
                self.assertFalse(cause.__suppress_context__)
                return cause

            with self.assertRaises(module.BundleIntegrityError) as exc:
                load([("snapshot-open", [], {
                    "kind": "snapshot-error", "error": "lock-json", "cause": invalid_json,
                    "message": f"cannot read bundle lock: {invalid_json}",
                })])
            cause = immediate_cause(exc.exception)
            self.assertIs(type(cause), CanonicalizationError)
            self.assertEqual(str(cause), invalid_json)
            self.assertEqual(str(exc.exception), f"cannot read bundle lock: {invalid_json}")

            original = FileNotFoundError(2, "missing", "/resolved/bundle/bundle.lock.json")
            graph = {
                "code": "file-not-found-error",
                "message": str(original),
                "details": json.dumps({
                    "format": "tobkiri.native-exception/1",
                    "graph_id": 1,
                    "root": 0,
                    "nodes": [{
                        "kind": "file-not-found-error", "args": list(original.args),
                        "fields": {"errno": original.errno, "strerror": original.strerror,
                                   "filename": original.filename, "filename2": None},
                        "cause": None, "context": None, "suppress_context": False,
                    }],
                }),
            }
            store = ExceptionGraphStore(_catalog_paths=True)
            with self.assertRaises(module.BundleIntegrityError) as exc:
                load([("snapshot-open", [], {
                    "kind": "snapshot-error", "error": "lock-io", "cause": graph,
                    "message": f"cannot read bundle lock: {original}",
                })], decode_error=lambda row: store.materialize(**row))
            cause = immediate_cause(exc.exception)
            self.assertIs(cause, store.materialize(**graph))
            self.assertIs(type(cause), FileNotFoundError)
            self.assertEqual((cause.args, cause.filename), (original.args, original.filename))
            self.assertEqual(str(exc.exception), f"cannot read bundle lock: {original}")

            def copied_lock(raw: bytes) -> list[tuple[str, list, dict]]:
                return [("snapshot-open", [], {
                    "kind": "snapshot-error",
                    "error": "lock-json-source",
                    "message": "native parser diagnostic",
                    "cause": byte_frame(raw),
                })]

            for raw, nested_type, attributes in (
                (b'{\n"entries": [}', json.JSONDecodeError,
                 ("msg", "doc", "pos", "lineno", "colno")),
                (b'{"entries":"\xff"}', UnicodeDecodeError,
                 ("encoding", "object", "start", "end", "reason")),
            ):
                with self.subTest(nested=nested_type):
                    with self.assertRaises(CanonicalizationError) as original:
                        module.strict_loads(raw)
                    with self.assertRaises(module.BundleIntegrityError) as result:
                        load(copied_lock(raw))
                    error = result.exception
                    cause = error.__cause__
                    nested = cause.__cause__
                    expected = original.exception.__cause__
                    self.assertIs(type(cause), CanonicalizationError)
                    self.assertIs(type(nested), nested_type)
                    self.assertIs(error.__context__, cause)
                    self.assertIs(cause.__context__, nested)
                    self.assertTrue(error.__suppress_context__)
                    self.assertTrue(cause.__suppress_context__)
                    self.assertIs(nested.__cause__, expected.__cause__)
                    self.assertEqual(nested.__suppress_context__,
                                     expected.__suppress_context__)
                    context = nested.__context__
                    expected_context = expected.__context__
                    self.assertIs(type(context), type(expected_context))
                    if expected_context is not None:
                        self.assertEqual(context.args, expected_context.args)
                        self.assertIs(context.__cause__, expected_context.__cause__)
                        self.assertIs(context.__context__, expected_context.__context__)
                        self.assertEqual(context.__suppress_context__,
                                         expected_context.__suppress_context__)
                    self.assertEqual(cause.args, original.exception.args)
                    self.assertEqual(nested.args, expected.args)
                    self.assertEqual(str(error), f"cannot read bundle lock: {cause}")
                    for name in attributes:
                        self.assertEqual(getattr(nested, name), getattr(expected, name))

            for raw in (b"{}", b'{"entries": [], "entries": []}'):
                with self.subTest(raw=raw), self.assertRaises(UnsupportedValue):
                    load(copied_lock(raw))

            relative = "./packs/rejected-π.json"
            rejected = {
                "kind": "snapshot-invalid-document",
                "document_kind": "pack",
                "path": relative,
            }
            valid_pack = original_pack.read_bytes()
            invalid_pack = json.loads(valid_pack)
            invalid_pack["pack"]["id"] = "BAD ID"
            invalid_pack["provider_authority"] = "forbidden"

            def compare_exception_graph(
                actual: BaseException,
                original: BaseException,
                observed: dict[int, BaseException],
            ) -> None:
                self.assertEqual(vars(actual), vars(original))
                self.assertEqual(actual.args, original.args)
                self.assertEqual(actual.__suppress_context__,
                                 original.__suppress_context__)
                for link in ("__cause__", "__context__"):
                    node = getattr(actual, link)
                    expected_node = getattr(original, link)
                    self.assertIs(type(node), type(expected_node))
                    if expected_node is None:
                        continue
                    if id(expected_node) in observed:
                        self.assertIs(node, observed[id(expected_node)])
                        continue
                    observed[id(expected_node)] = node
                    compare_exception_graph(node, expected_node, observed)

            for label, raw, nested_type in (
                ("json", b'{\n"pack": [}', json.JSONDecodeError),
                ("utf8", b'{"pack":"\xff"}', UnicodeDecodeError),
                ("schema and semantics", json.dumps(invalid_pack).encode(), None),
            ):
                with self.subTest(rejected_document=label):
                    lock = json.dumps({
                        "schema": "io.tobkiri.defaultspack-bundle-lock.v1",
                        "entries": [{
                            "path": relative,
                            "kind": "pack",
                            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        }],
                    }).encode()

                    def read_entry(index: int, path: str, reread: bool) -> bytes:
                        self.assertEqual((index, path, reread), (0, relative, False))
                        return raw

                    with self.assertRaises(
                        original_catalog.BundleIntegrityError
                    ) as expected:
                        original_catalog.BundledCatalog.load_from_reader(
                            lock, entry_reader=read_entry, root="/bundle",
                            artifact_root_resolver=lambda: self.fail(
                                "resolved artifact root"
                            ),
                        )
                    with (
                        patch.object(module, "validate_document",
                                     wraps=module.validate_document) as validate,
                        self.assertRaises(module.BundleIntegrityError) as result,
                    ):
                        load([
                            ("snapshot-open", [], rejected),
                            ("snapshot-invalid-document-bytes", [], byte_frame(raw)),
                        ])
                    validate.assert_called_once_with(raw, "pack")
                    error, reference = result.exception, expected.exception
                    self.assertIs(type(error), module.BundleIntegrityError)
                    self.assertIn(relative, str(error))
                    compare_exception_graph(error, reference, {id(reference): error})
                    cause = error.__cause__
                    self.assertIs(type(cause), SchemaValidationError)
                    if nested_type is None:
                        self.assertIsNone(cause.__cause__)
                        self.assertTrue(any(
                            "does not match" in entry for entry in cause.diagnostics
                        ))
                        self.assertTrue(any(
                            "forbidden authority-bearing field" in entry
                            for entry in cause.diagnostics
                        ))
                    else:
                        self.assertIs(type(cause.__cause__.__cause__), nested_type)

            for invalid in (
                {**rejected, "document_kind": "distribution"},
                {**rejected, "document_kind": ["pack"]},
                {**rejected, "path": 1},
                {**rejected, "path": ""},
                {**rejected, "message": "forged error"},
            ):
                with (
                    self.subTest(rejection=invalid), self.assertRaises(UnsupportedValue)
                ):
                    load([("snapshot-open", [], invalid)])

            for invalid in (
                {"kind": "unit"},
                {"kind": "bytes", "value": "not base64"},
                {**byte_frame(b"{"), "extra": True},
                byte_frame(valid_pack),
            ):
                with self.subTest(rejection_bytes=invalid["kind"]), self.assertRaises(
                    UnsupportedValue,
                ):
                    load([
                        ("snapshot-open", [], rejected),
                        ("snapshot-invalid-document-bytes", [], invalid),
                    ])

    def test_optional_trio_is_atomic_and_native_only(self):
        value = Path("/input")
        for enabled in (True, False):
            self.assertFalse(dependencies.selected(enabled, None, None, None))
        self.assertTrue(dependencies.selected(True, value, value, value))
        for enabled, paths in (
            (False, (value, value, value)),
            (True, (value, None, None)),
            (True, (value, value, None)),
            (True, (None, None, value)),
        ):
            with (
                self.subTest(enabled=enabled, paths=paths),
                self.assertRaises(ValueError),
            ):
                dependencies.selected(enabled, *paths)
        with (
            patch.object(build_components.sys, "platform", "linux"),
            patch.dict(
                build_components.os.environ,
                {"GITHUB_ACTIONS": "true"},
            ),
            patch.object(Path, "mkdir", side_effect=AssertionError("unexpected IO")),
        ):
            with self.assertRaisesRegex(ValueError, "--harness-native"):
                build_components.run(
                    None,
                    value,
                    value,
                    value,
                    all_five=True,
                    schema_inputs=value,
                    stdlib=value,
                    rpds=value,
                )

    def test_existing_verifier_is_reused_without_clock_variant_opt_in(self):
        roots = [
            Path("/") / name
            for name in ("schema", "producer", "rpds", "validation", "stdlib")
        ]
        with (
            patch.object(
                dependencies.build_schema, "verify_inputs", return_value={"pinned": {}}
            ) as verify,
            patch.object(
                dependencies.runtime_inputs,
                "report",
                side_effect=[{"inputs": ["wheel pins"]}, {"files": {}}],
            ),
            patch.object(
                dependencies, "fingerprint", return_value={"sha256": "actual"}
            ),
        ):
            result = dependencies.verify(*roots)
        verify.assert_called_once_with(*roots)
        self.assertFalse(result["ui_clock_variant"])
        self.assertEqual(
            result["rpds_head"], "53163ed815c62c4853bbed3d8a082c0764e3084f"
        )

    def test_source_plan_preserves_all_originals_and_captures_every_late_module(self):
        original_plans = component_inputs.plans(True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            stage = root / "native"
            original = component_inputs.stage_harness_native(HERE.parents[1], stage)
            before = {name: (stage / name).read_bytes() for name in original["files"]}
            schema, stdlib, rpds, verified = fixture(root)
            result = dependencies.stage(stage, original, verified, schema, stdlib, rpds)
            plan = json.loads((stage / "plan.json").read_bytes())
            previous = sys.path[:]
            try:
                sys.path.insert(0, str(HERE.parents[1] / "sdk/python"))
                import build_component as sdk

                checked, _raw, plan_hash, captured = sdk._verify_plan(
                    stage / "plan.json"
                )
            finally:
                sys.path[:] = previous
            self.assertEqual(checked, plan)
            self.assertEqual(plan_hash, result["plan_sha256"])
            self.assertEqual(len(captured), len(plan["sources"]))
            self.assertEqual(plan["entrypoint"], "harness_native_entry")
            self.assertEqual(plan["allowed_imports"], dependencies.IMPORTS)
            self.assertEqual(plan["outer_exports"], dependencies.EXPORTS)
            for name, raw in before.items():
                if name != "plan.json":
                    self.assertEqual((stage / name).read_bytes(), raw, name)
            self.assertEqual(result["files"], inventory(stage))
            self.assertEqual(
                result["plan_sha256"],
                identity(
                    (stage / "plan.json").read_bytes(),
                )["sha256"],
            )
            data = ast.parse(
                (stage / "python/harness_native_dependency_data.py").read_bytes()
            )
            self.assertTrue(all(isinstance(node, ast.Assign) for node in data.body))
            values = {
                node.targets[0].id: ast.literal_eval(node.value) for node in data.body
            }
            self.assertIs(type(values["FILES"]), tuple)
            tree = Tree(values["FILES"], compressed=True)
            files = tree.files
            self.assertEqual(
                values["NAMESPACES"],
                ("ecosystem", "ecosystem.defaultspack", "harness_native"),
            )
            for name in (
                "ecosystem/__init__.py",
                "ecosystem/defaultspack/__init__.py",
                "harness_native/__init__.py",
            ):
                self.assertNotIn(name, files)
                self.assertFalse((stage / "python" / name).exists())
            for name, raw in before.items():
                if name.startswith("python/"):
                    self.assertEqual(files[name.removeprefix("python/")], raw)
            self.assertIn("plistlib", values["STDLIB_PREIMPORTS"])
            self.assertIn("subprocess", values["STDLIB_PREIMPORTS"])
            self.assertNotIn("ctypes", values["STDLIB_PREIMPORTS"])
            self.assertNotIn("rpds/native.so", files)
            self.assertNotIn("rpds/rpds.abi3.so", files)
            self.assertEqual(
                (stage / "python/rpds/rpds.abi3.so").read_bytes(),
                (rpds / "work/stage/rpds.abi3.so").read_bytes(),
            )
            proof = json.loads((stage / "python" / dependencies.METADATA).read_bytes())
            storage = proof["source_storage"]
            self.assertFalse(storage["expanded_member_cache"])
            self.assertFalse(storage["code_cache"])
            self.assertFalse(storage["decompression_after_restore_proven"])
            self.assertFalse(storage["artifact_size_within_limit_proven"])
            self.assertLess(
                storage["stored_payload_bytes"], proof["captured_source_bytes"]
            )
            self.assertGreater(
                tree.accounted_bytes, proof["captured_raw_admission_bytes"]
            )
            for name, codec, payload, length, digest in values["FILES"]:
                self.assertIn(codec, ("identity", "zlib"))
                self.assertEqual(
                    identity(files[name]), {"bytes": length, "sha256": digest}
                )
            self.assertEqual(proof["original_module_count"], 68)
            self.assertEqual(proof["original_schema_count"], 42)
            for flag in (
                "dependency_closure_complete",
                "all_seventeen_methods_compatible",
                "component_init_performed",
                "native_imports_connected",
                "production_enabled",
            ):
                self.assertFalse(proof[flag])
            self.assertEqual(
                proof["unprovided_external_dependencies"], ["cryptography"]
            )
            for item in plan["sources"]:
                self.assertEqual(
                    identity((stage / item["source"]).read_bytes())["sha256"],
                    item["sha256"],
                )
            manifest = [
                {
                    "source": item["source"],
                    "destination": item["destination"],
                    "sha256": item["sha256"],
                    "bytes": result["files"][item["source"]]["bytes"],
                }
                for item in plan["sources"]
            ]
            receipt = {
                "source_manifest": manifest,
                "source_manifest_sha256": hashlib.sha256(
                    json.dumps(
                        manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest(),
                "build_plan_sha256": result["plan_sha256"],
                "world": "harness-native",
                "imports": sorted(dependencies.IMPORTS),
                "exports": sorted(dependencies.EXPORTS),
                "inner": {
                    "imports": sorted(dependencies.IMPORTS),
                    "exports": sorted([*dependencies.EXPORTS, "exports"]),
                },
                "harness_native_dependencies": proof,
            }
            dependencies.verify_component_receipt(receipt, result, stage)
            for field, value in (
                ("source_manifest_sha256", "forged"),
                ("imports", dependencies.IMPORTS + ["unexpected:io/host@1.0.0"]),
                ("exports", dependencies.EXPORTS[:1]),
            ):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    dependencies.verify_component_receipt(
                        {**receipt, field: value},
                        result,
                        stage,
                    )
        self.assertEqual(component_inputs.plans(True), original_plans)

    def test_tampered_dependency_and_non_native_plan_fail_before_extension_staging(
        self,
    ):
        for changed in ("wheel", "boundary"):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                stage = root / "native"
                original = component_inputs.stage_harness_native(HERE.parents[1], stage)
                schema, stdlib, rpds, verified = fixture(root)
                if changed == "wheel":
                    (schema / "packages/attrs/attrs/__init__.py").write_bytes(b"forged")
                else:
                    plan = json.loads((stage / "plan.json").read_bytes())
                    plan["world"] = "other"
                    (stage / "plan.json").write_text(json.dumps(plan))
                    original["files"] = inventory(stage)
                    original["plan_sha256"] = identity(
                        (stage / "plan.json").read_bytes()
                    )["sha256"]
                with self.assertRaises(ValueError):
                    dependencies.stage(stage, original, verified, schema, stdlib, rpds)
                self.assertFalse((stage / "python/rpds/rpds.abi3.so").exists())


if __name__ == "__main__":
    unittest.main()
