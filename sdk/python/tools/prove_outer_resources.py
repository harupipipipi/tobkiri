"""Actual typed-composition resource proof; not a Core/Gate acceptance substitute."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from composition import compose


def prove(composer: Path) -> dict:
    """Execute own, borrow, alias, consume and drop across the typed outer ABI."""
    from wasmtime import Engine, Store, WasmtimeError, wat2wasm
    from wasmtime.component import Component, Linker

    fixture = Path(__file__).resolve().parents[1] / "tests/fixtures/outer-resources"
    exports = ["local:outer-resource-proof/objects@1.0.0",
               "local:outer-resource-proof/observer@1.0.0"]
    with tempfile.TemporaryDirectory(prefix="tobkiri-outer-resources-") as temp:
        stage = Path(temp)
        inner = wat2wasm((fixture / "inner.wat").read_text())
        (stage / "inner.wasm").write_bytes(inner)
        composition = compose(composer.absolute(), stage / "inner.wasm", stage,
                              fixture, "proof", exports, {"PATH": os.defpath})
        outer = (stage / "outer.wasm").read_bytes()
        engine = Engine()
        store = Store(engine)
        component = Component(engine, outer)
        assert set(component.type.imports(engine)) == set()
        assert set(component.type.exports(engine)) == set(exports)
        instance = Linker(engine).instantiate(store, component)
        assert instance.get_export_index(store, "exports") is None

        def function(target, shape, operation):
            index = target.get_export_index(store, shape)
            item = target.get_export_index(store, operation, index)
            call = target.get_func(store, item)

            def invoke(*args):
                value = call(store, *args)
                call.post_return(store)
                return value
            return invoke

        make = function(instance, exports[0], "make")
        inspect = function(instance, exports[0], "inspect")
        observe = function(instance, exports[1], "inspect")
        consume = function(instance, exports[0], "consume")
        drops = function(instance, exports[0], "drops")
        one = make(42)
        assert one.owned
        assert inspect(one) == 42 and observe(one) == 42 and inspect(one) == 42
        assert drops() == 0
        one.drop(store)
        assert drops() == 1
        two = make(99)
        assert consume(two) == 99
        assert drops() == 2
        # Another component instance has a distinct nominal resource, even
        # though it was compiled from the same WIT and has the same structure.
        other_instance = Linker(engine).instantiate(store, component)
        other = function(other_instance, exports[0], "make")(7)
        try:
            observe(other)
        except WasmtimeError as error:
            if "resource" not in str(error).lower():
                raise AssertionError("foreign resource failed for an unrelated reason") from error
        else:
            raise AssertionError("composition accepted a foreign nominal resource")
        other.drop(store)
        return {"format": "tobkiri.outer-resource-proof/1",
                "imports": [], "exports": exports,
                "source_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (fixture / "inner.wat", fixture / "pack.wit")},
                "inner_sha256": hashlib.sha256(inner).hexdigest(),
                "outer_sha256": hashlib.sha256(outer).hexdigest(),
                "composition": composition,
                "checks": {"own": True, "borrow_preserves_owner": True,
                           "cross_interface_nominal_alias": True,
                           "foreign_nominal_resource_denied": True,
                           "drop_exactly_once": True, "consume_own": True,
                           "private_initializer_inaccessible": True},
                "drop_count": 2, "core_runtime_acceptance": False}


def main() -> None:
    """Use only the explicitly selected, pinned build-time tool."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--composer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = prove(args.composer)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
