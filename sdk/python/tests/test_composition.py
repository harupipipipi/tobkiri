"""Producer selection checks; runtime resource forwarding has separate evidence."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from composition import (
    _composition_commands,
    composition_source,
    world_export_names,
    world_export_names_from_path,
    world_export_names_from_sources,
)


class CompositionTests(unittest.TestCase):
    def test_selected_interfaces_leave_private_initializer_unexported(self):
        source = composition_source([
            "tobkiri:core/lifecycle@1.0.0",
            "local:shell-policy-python/policy@1.0.0",
        ])
        self.assertEqual(source.count("export implementation["), 2)
        self.assertNotIn('implementation["exports"]', source)
        self.assertNotIn("export implementation...", source)
        self.assertIn("new local:python-inner { ... }", source)

    def test_export_names_cannot_inject_composition_or_select_initializer(self):
        for exports in [[], ["exports"], ["x:y/z@1.0.0"]*2,
                        ['x:y/z@1.0.0\"]; export implementation...; //'],
                        ["wasi:cli/run@0.2"], [1]]:
            with self.assertRaises(ValueError):
                composition_source(exports)

    def test_world_export_names_cover_functions_and_versioned_interfaces(self):
        wit = """
            package local:fixture@1.2.3;
            interface api { ping: func() -> string; }
            world pack {
                export api;
                export status: func() -> string;
            }
        """
        self.assertEqual(world_export_names(wit, "pack"), [
            "local:fixture/api@1.2.3", "status",
        ])

    def test_world_export_parser_rejects_unknown_and_extra_export_forms(self):
        wit = """
            package local:fixture@1.2.3;
            interface api { ping: func() -> string; }
            world pack { export api; export hidden: type; }
        """
        with self.assertRaises(ValueError):
            world_export_names(wit, "pack")

        wit = """
            package local:fixture@1.2.3;
            world pack { export missing; }
        """
        with self.assertRaises(ValueError):
            world_export_names(wit, "pack")

        wit = """
            package local:fixture@1.2.3;
            world pack { export exports: func(); }
        """
        with self.assertRaisesRegex(ValueError, "initializer"):
            world_export_names(wit, "pack")

    def test_world_export_parser_ignores_comment_and_string_declaration_text(self):
        wit = '''
            /* package fake:package@9.9.9; world pack { export fake; } */
            package local:fixture@1.2.3;
            @doc("package fake:package@9.9.9; world pack { export fake; }")
            interface api { ping: func(label: string) -> string; }
            world pack {
                // export forged: func();
                export api;
            }
        '''
        self.assertEqual(world_export_names(wit, "pack"), ["local:fixture/api@1.2.3"])

    def test_world_export_parser_resolves_included_versioned_worlds_exactly(self):
        sources = [
            ("wit/pack.wit", """
                package local:fixture@1.2.3;
                interface api { ping: func() -> string; }
                world pack {
                    include tobkiri:core/pack-minimal@1.0.0;
                    export api;
                    export status: func() -> string;
                }
            """),
            ("wit/deps/core.wit", """
                package tobkiri:core@1.0.0;
                interface lifecycle { init: func(); }
                world pack-minimal {
                    import types;
                    export lifecycle;
                }
            """),
        ]
        self.assertEqual(
            world_export_names_from_sources(sources, "pack", root_source="wit/pack.wit"),
            ["local:fixture/api@1.2.3", "status", "tobkiri:core/lifecycle@1.0.0"],
        )
        missing_dependency = [(sources[0][0], sources[0][1])]
        with self.assertRaisesRegex(ValueError, "not among the captured"):
            world_export_names_from_sources(missing_dependency, "pack",
                                            root_source="wit/pack.wit")

    def test_world_export_parser_rejects_ambiguous_include_identity(self):
        wit = """
            package local:fixture@1.2.3;
            world pack { include tobkiri:core/pack-minimal; }
        """
        with self.assertRaisesRegex(ValueError, "fully qualified"):
            world_export_names(wit, "pack")

    def test_world_export_parser_rejects_annotated_and_unknown_world_items(self):
        for item in [
            "@since(version = 1.0.0) export extra: func();",
            "@unstable(feature = extra) include local:dep/other@1.0.0;",
            "@since(version = 1.0.0) import local:dep/api@1.0.0;",
            "unsupported extra: func();",
        ]:
            with self.subTest(item=item), self.assertRaisesRegex(
                ValueError, "annotated|unsupported"
            ):
                world_export_names(
                    "package local:fixture@1.0.0; world pack {"
                    f"export ping: func(); {item} }}",
                    "pack",
                )

    def test_wit_directory_merges_siblings_and_one_level_dependency_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pack.wit").write_text(
                "package local:fixture@1.0.0; world pack {"
                "include local:dep/other@1.0.0; export api; }",
                encoding="utf-8",
            )
            (root / "api.wit").write_text(
                "package local:fixture@1.0.0; interface api { ping: func(); }",
                encoding="utf-8",
            )
            dependency = root / "deps/dep"
            dependency.mkdir(parents=True)
            (dependency / "dep.wit").write_text(
                "package local:dep@1.0.0; world other { export status: func(); }",
                encoding="utf-8",
            )
            # The tool's directory closure does not recurse into arbitrary dirs.
            (root / "extra").mkdir()
            (root / "extra/ignored.wit").write_text("invalid", encoding="utf-8")
            (dependency / "nested").mkdir()
            (dependency / "nested/ignored.wit").write_text(
                "invalid", encoding="utf-8"
            )
            names, _ = world_export_names_from_path(root, "pack")
            self.assertEqual(names, ["local:fixture/api@1.0.0", "status"])

    def test_wit_dependency_paths_reject_symlinked_directories_and_files(self):
        for link_kind in ("deps", "package", "file"):
            with (
                self.subTest(link_kind=link_kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                base = Path(directory)
                root = base / "wit"
                root.mkdir()
                (root / "pack.wit").write_text(
                    "package local:fixture@1.0.0; world pack { export ping: func(); }",
                    encoding="utf-8",
                )
                outside = base / "outside"
                outside.mkdir()
                (outside / "dep.wit").write_text(
                    "package local:dep@1.0.0; interface api { ping: func(); }",
                    encoding="utf-8",
                )
                if link_kind == "deps":
                    (root / "deps").symlink_to(outside, target_is_directory=True)
                else:
                    (root / "deps").mkdir()
                    if link_kind == "package":
                        (root / "deps/dep").symlink_to(
                            outside, target_is_directory=True
                        )
                    else:
                        (root / "deps/dep.wit").symlink_to(outside / "dep.wit")
                with self.assertRaisesRegex(ValueError, "symlink"):
                    world_export_names_from_path(root, "pack")

    def test_existing_resource_proof_exports_are_resolved_exactly(self):
        fixture = Path(__file__).resolve().parent / "fixtures/outer-resources"
        names, digest = world_export_names_from_path(fixture, "proof")
        self.assertEqual(names, [
            "local:outer-resource-proof/objects@1.0.0",
            "local:outer-resource-proof/observer@1.0.0",
        ])
        self.assertEqual(len(digest), 64)

    def test_official_python_pack_world_includes_lifecycle_export(self):
        fixture = Path(__file__).resolve().parent / "fixtures/build/shell-policy"
        names, digest = world_export_names_from_path(fixture / "wit", "pack")
        self.assertEqual(names, [
            "local:shell-policy-python/policy@1.0.0",
            "tobkiri:core/lifecycle@1.0.0",
        ])
        self.assertEqual(len(digest), 64)

    def test_wac_target_receives_the_staged_wit_directory(self):
        commands = _composition_commands(
            Path("wac-cli"), Path("stage/component.wasm"), Path("stage"),
            Path("stage/wit"), "pack")
        self.assertEqual(commands[0][commands[0].index("--deps-dir") + 1],
                         "stage/no-ambient-dependencies")
        self.assertEqual(commands[1][commands[1].index("--wit") + 1], "stage/wit")
        self.assertEqual(commands[1][-1], "pack")


if __name__ == "__main__":
    unittest.main()
