"""Count encoded core instantiations only, without compiling/executing Wasm.

This deliberately narrow binary reader follows wasmparser 0.252.0 component
instances.rs, imports.rs and aliases.rs. It rejects imported/aliased components
or modules and requires each nested component definition to be instantiated
exactly once. It does not count compiler-generated fused adapter modules, so
its result is NOT an exact Wasmtime Store.instances requirement.
"""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

INPUTS = {
    "caller": ("issue1520-gateway-trial/component-work/component.wasm", "1a438458ba804cf9b62c7125602b1499f159263387f56f2470c51ce4191e521c"),
    "provider": ("issue1520-v2-trial/component-work/component.wasm", "0f28f4a87158b18a24fe045a9be1c24e8d9405549a0af1b56546b2017ac92a46"),
    "composition": ("issue1520-gateway-trial/composition-probe-work/composed.wasm", "2530b6a090f6584cfc6ffeef131b070af429c0b120381be463c767f47bb2fe67"),
}
ROOT = Path(__file__).resolve().parent.parent

class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def byte(self):
        assert self.pos < len(self.data), "truncated byte"
        value = self.data[self.pos]
        self.pos += 1
        return value

    def u32(self):
        value = 0
        for shift in range(0, 35, 7):
            byte = self.byte()
            value |= (byte & 127) << shift
            if not byte & 128:
                assert value <= 0xffffffff, "u32 overflow"
                return value
        raise ValueError("invalid u32")

    def name(self):
        length = self.u32()
        end = self.pos + length
        assert end <= len(self.data), "truncated name"
        value = self.data[self.pos:end].decode("utf-8")
        self.pos = end
        return value

    def component_name(self):
        assert self.byte() in (0, 1), "unsupported component name format"
        return self.name()

    def external_kind(self):
        kind = self.byte()
        if kind == 0:
            assert self.byte() == 0x11, "invalid core module kind"
        else:
            assert kind in (1, 2, 3, 4, 5), "invalid component external kind"
        return kind

    def complete(self):
        assert self.pos == len(self.data), "unread section bytes"


def scan(data, label, nodes):
    assert data[:8] == b"\x00asm\x0d\x00\x01\x00", "not a v1 component"
    reader = Reader(data)
    reader.pos = 8
    children, modules, instantiations, from_exports = [], 0, [], 0
    component_instantiations, import_kinds, alias_count = [], [], 0
    while reader.pos < len(data):
        section_id, size = reader.byte(), reader.u32()
        end = reader.pos + size
        assert end <= len(data), "truncated section"
        section = Reader(data[reader.pos:end])
        reader.pos = end
        if section_id == 1:
            assert section.data[:8] == b"\x00asm\x01\x00\x00\x00"
            modules += 1
        elif section_id == 4:
            children.append(section.data)
        elif section_id == 2:
            for _ in range(section.u32()):
                kind = section.byte()
                if kind == 0:
                    instantiations.append(section.u32())
                    for _ in range(section.u32()):
                        section.name()
                        assert section.byte() == 0x12
                        section.u32()
                elif kind == 1:
                    from_exports += 1
                    for _ in range(section.u32()):
                        section.name()
                        assert section.byte() in (0, 1, 2, 3, 4)
                        section.u32()
                else:
                    raise ValueError("invalid core instance form")
            section.complete()
        elif section_id == 5:
            for _ in range(section.u32()):
                kind = section.byte()
                if kind == 0:
                    component_instantiations.append(section.u32())
                    for _ in range(section.u32()):
                        section.name()
                        section.external_kind()
                        section.u32()
                elif kind == 1:
                    for _ in range(section.u32()):
                        section.component_name()
                        section.external_kind()
                        section.u32()
                else:
                    raise ValueError("invalid component instance form")
            section.complete()
        elif section_id == 6:
            for _ in range(section.u32()):
                first = section.byte()
                second = section.byte() if first == 0 else None
                form = section.byte()
                # Reject aliases that add component/module index-space entries.
                assert first != 4 and (first, second) != (0, 0x11)
                alias_count += 1
                if form in (0, 1):
                    section.u32()
                    section.name()
                elif form == 2:
                    section.u32()
                    section.u32()
                else:
                    raise ValueError("invalid alias form")
            section.complete()
        elif section_id == 10:
            for _ in range(section.u32()):
                section.component_name()
                kind = section.external_kind()
                assert kind not in (0, 4), "imported module/component unsupported"
                import_kinds.append(kind)
                if kind in (1, 5):
                    section.u32()
                elif kind == 3:
                    bound = section.byte()
                    if bound == 0:
                        section.u32()
                    else:
                        assert bound == 1
                else:
                    raise ValueError("value imports unsupported")
            section.complete()
    reader.complete()
    assert all(index < modules for index in instantiations)
    assert Counter(component_instantiations) == Counter(range(len(children))), (
        "nested component definitions not instantiated exactly once", label,
        component_instantiations, len(children),
    )
    node = {
        "component_path": label,
        "defined_core_modules": modules,
        "encoded_core_instantiate_entries": len(instantiations),
        "core_module_indices": instantiations,
        "from_exports_entries_excluded": from_exports,
        "nested_component_definitions": len(children),
        "component_instantiate_indices": component_instantiations,
        "each_nested_definition_instantiated_once": True,
        "no_module_or_component_imports_or_aliases": True,
        "other_import_kinds": import_kinds,
        "aliases_checked": alias_count,
    }
    nodes.append(node)
    total = len(instantiations)
    for index, child in enumerate(children):
        total += scan(child, f"{label}/{index}", nodes)
    node["encoded_core_instantiate_subtree_total"] = total
    return total


def main():
    records = []
    for label, (relative, expected_sha256) in INPUTS.items():
        path = ROOT / relative
        data = path.read_bytes()
        assert len(data) <= 64 * 1024 * 1024
        sha256 = hashlib.sha256(data).hexdigest()
        assert sha256 == expected_sha256, f"changed {label} identity"
        nodes = []
        total = scan(data, label, nodes)
        records.append({"label": label, "path": str(path), "sha256": sha256,
                        "bytes": len(data), "encoded_core_instantiations": total,
                        "nodes": nodes})
    print(json.dumps({
        "format": "development-static-encoded-core-instance-count/1",
        "python_version": sys.version.split()[0],
        "reader_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "wasm_compiled": False, "wasm_instantiated": False,
        "guest_executed": False, "runtime_instance_count_measured": False,
        "scope": "Encoded core Instantiate entries, excluding FromExports; nested definition multiplicity verified.",
        "caveat": "Wasmtime may synthesize fused adapter modules during compilation. Encoded count is not an exact Store.instances requirement or sufficient-cap guarantee.",
        "artifacts": records,
    }, indent=2))

if __name__ == "__main__":
    main()
