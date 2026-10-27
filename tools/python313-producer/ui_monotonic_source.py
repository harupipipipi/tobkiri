"""Derive a separate clock candidate with a partial monotonic WASI adapter.

Only now/resolution cross the new typed interface. Every other original
monotonic export retains its real signature and unreachable implementation.
"""

from pathlib import Path
import tomllib

from build_native import fingerprint
from fetch_sdk import inventory
import ui_clock_source

FEATURE = "ui-monotonic-clock"
IMPORT_INTERFACE = "tobkiri-ui:clock/monotonic@1.0.0"
INTERFACE_PREFIX = "wasi:clocks/monotonic-clock@"
ANCHOR = b"        linker = linker.adapter(module, &make_stub_adapter(module, imports))?;\n"
REPLACEMENT = b"""        if cfg!(feature = "ui-monotonic-clock")
            && module.starts_with("wasi:clocks/monotonic-clock@")
        {
            linker = linker.adapter(module, &make_ui_monotonic_adapter(module, imports)?)?;
        } else {
            linker = linker.adapter(module, &make_stub_adapter(module, imports))?;
        }
"""
ADAPTER = b'''
// Tobkiri's separate UI-local candidate. This does not enable WASI subscriptions
// or polling, nor change the original STUB_WASI initialization policy.
fn make_ui_monotonic_adapter(
    module_name: &str,
    stubs: &HashMap<&str, FuncType>,
) -> Result<Vec<u8>, Error> {
    use wasm_encoder::{EntityType, ImportSection};
    use wasm_encoder::reencode::{Reencode, RoundtripReencoder as R};

    let version = semver::Version::parse(
        module_name.rsplit_once('@').unwrap().1,
    )?;
    if version.major != 0 || version.minor != 2
        || !version.pre.is_empty() || !version.build.is_empty()
    {
        bail!("unsupported UI monotonic-clock interface");
    }
    for name in ["now", "resolution"] {
        let Some(ty) = stubs.get(name) else {
            bail!("UI monotonic-clock function is missing");
        };
        if !ty.params().is_empty() || ty.results() != [wasmparser::ValType::I64] {
            bail!("unsupported UI monotonic-clock function ABI");
        }
    }

    let mut entries = stubs.iter().collect::<Vec<_>>();
    entries.sort_unstable_by_key(|(name, _)| **name);
    let mut types = TypeSection::new();
    for (_, ty) in &entries {
        types.ty().function(
            ty.params().iter().map(|&v| R.val_type(v).unwrap()),
            ty.results().iter().map(|&v| R.val_type(v).unwrap()),
        );
    }
    let mut imports = ImportSection::new();
    for name in ["now", "resolution"] {
        let type_index = u32::try_from(
            entries.iter().position(|(entry, _)| **entry == name).unwrap(),
        )?;
        imports.import(
            "tobkiri-ui:clock/monotonic@1.0.0",
            name,
            EntityType::Function(type_index),
        );
    }
    let mut functions = FunctionSection::new();
    let mut exports = ExportSection::new();
    let mut code = CodeSection::new();
    let mut defined_functions = 0;
    for (type_index, (name, _)) in entries.iter().enumerate() {
        let function_index = match **name {
            "now" => 0,
            "resolution" => 1,
            _ => {
                functions.function(u32::try_from(type_index)?);
                let mut function = Function::new([]);
                function.instruction(&Ins::Unreachable);
                function.instruction(&Ins::End);
                code.function(&function);
                let index = 2 + defined_functions;
                defined_functions += 1;
                index
            }
        };
        exports.export(name, ExportKind::Func, function_index);
    }
    let mut module = Module::new();
    module.section(&types);
    module.section(&imports);
    module.section(&functions);
    module.section(&exports);
    module.section(&code);
    let mut bytes = module.finish();

    // This metadata describes only the two genuine imported observations.
    // Other raw exports stay available to satisfy the original library's ABI.
    let mut resolve = wit_parser::Resolve::default();
    let package = resolve.push_source(
        "ui-monotonic-adapter.wit",
        "package tobkiri-ui:clock@1.0.0;\\n\\
         interface monotonic {\\n\\
           now: func() -> u64;\\n\\
           resolution: func() -> u64;\\n\\
         }\\n\\
         world adapter { import monotonic; }\\n",
    )?;
    let world = resolve.select_world(&[package], Some("adapter"))?;
    wit_component::embed_component_metadata(
        &mut bytes, &resolve, world, wit_component::StringEncoding::UTF8,
    )?;
    eprintln!("tobkiri-ui-monotonic-clock {module_name}: now,resolution only");
    Ok(bytes)
}
'''


def patch_stub_source(original: bytes) -> bytes:
    """Keep the original generator and wall exception, replacing one dispatch."""
    if (
        original.count(ANCHOR) != 1
        or original.count(ui_clock_source.REPLACEMENT) != 1
        or b"make_ui_monotonic_adapter" in original
    ):
        raise ValueError("authenticated wall-clock candidate source required")
    return original.replace(ANCHOR, REPLACEMENT, 1) + ADAPTER


def feature_manifest(original: bytes) -> bytes:
    """Add only a feature that includes the already explicit wall-clock mode."""
    document = tomllib.loads(original.decode())
    if (
        document.get("package", {}).get("name") != "componentize-py"
        or document.get("features") != {ui_clock_source.FEATURE: []}
        or not original.endswith(b"ui-wall-clock = []\n")
    ):
        raise ValueError("original wall-clock feature manifest required")
    updated = original + b'ui-monotonic-clock = ["ui-wall-clock"]\n'
    expected = {
        **document,
        "features": {**document["features"], FEATURE: [ui_clock_source.FEATURE]},
    }
    if tomllib.loads(updated.decode()) != expected:
        raise ValueError("monotonic feature changed unrelated manifest fields")
    return updated


def build_recipe(original: bytes) -> bytes:
    """Use the unchanged recipe with the separately identified clock feature."""
    anchor = ui_clock_source.BUILD.rstrip(b"\n") + b" --features ui-wall-clock\n"
    if original.count(anchor) != 1:
        raise ValueError("original explicit wall-clock recipe required")
    return original.replace(
        anchor,
        ui_clock_source.BUILD.rstrip(b"\n") + b" --features ui-monotonic-clock\n",
        1,
    )


def enable(stage: Path, receipt: dict) -> dict:
    """Derive from the checked wall candidate without replacing its identity."""
    before = inventory(stage)
    if (
        before != receipt["files"]
        or any("link" in row for row in before.values())
        or receipt.get("ui_wall_clock", {}).get("feature") != ui_clock_source.FEATURE
        or "ui_monotonic_clock" in receipt
    ):
        raise ValueError("authenticated private wall-clock candidate required")
    changes = {
        "Cargo.toml": feature_manifest((stage / "Cargo.toml").read_bytes()),
        "src/stubwasi.rs": patch_stub_source((stage / "src/stubwasi.rs").read_bytes()),
    }
    expected = dict(before)
    for name, raw in changes.items():
        (stage / name).write_bytes(raw)
        expected[name] = {**before[name], **fingerprint(stage / name)}
    after = inventory(stage)
    if after != expected:
        raise ValueError("monotonic candidate changed unrelated source or assets")
    return {
        **receipt,
        "files": after,
        "delta": receipt["delta"]
        + "; explicit ui-monotonic-clock partial adapter forwarding only now/resolution",
        "ui_monotonic_clock": {
            "feature": FEATURE,
            "feature_dependencies": [ui_clock_source.FEATURE],
            "interface_prefix": INTERFACE_PREFIX,
            "import_interface": IMPORT_INTERFACE,
            "functions": ["now", "resolution"],
            "result": "u64 nanoseconds",
            "other_exports": "original signatures and unreachable bodies",
            "stub_wasi_retained": True,
            "changed_files": {
                name: {"wall_clock_candidate": before[name], "candidate": after[name]}
                for name in changes
            },
        },
    }
