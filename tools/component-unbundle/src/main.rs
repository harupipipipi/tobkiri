// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//! Build-time transformation only; no runtime admission or authority is granted.
mod upstream;

use anyhow::{Result, bail};
use std::io::{self, Read, Write};

const OUTPUT_LIMIT: usize = 64 * 1024 * 1024;
const THRESHOLD: usize = 10 * 1024;

fn write_bundle(mut writer: impl Write, bundle: &upstream::Bundle<'_>) -> Result<()> {
    let size = bundle
        .modules
        .iter()
        .try_fold(16usize + bundle.component.len(), |total, module| {
            total.checked_add(4)?.checked_add(module.len())
        });
    if size.is_none_or(|size| size > OUTPUT_LIMIT) {
        bail!("transformed closure exceeds output bound");
    }
    writer.write_all(b"TBUNB001")?;
    writer.write_all(&(bundle.modules.len() as u32).to_le_bytes())?;
    writer.write_all(&(bundle.component.len() as u32).to_le_bytes())?;
    writer.write_all(&bundle.component)?;
    for module in &bundle.modules {
        writer.write_all(&(module.len() as u32).to_le_bytes())?;
        writer.write_all(module)?;
    }
    writer.flush()?;
    Ok(())
}

fn main() -> Result<()> {
    if std::env::args_os().count() != 1 {
        bail!("expected bounded component bytes on stdin; no arguments");
    }
    let mut input = Vec::new();
    io::stdin()
        .lock()
        .take(upstream::INPUT_LIMIT as u64 + 1)
        .read_to_end(&mut input)?;
    let bundle = upstream::unbundle(&input, THRESHOLD)?;
    write_bundle(io::stdout().lock(), &bundle)
}

#[cfg(test)]
mod tests {
    use super::*;
    use wasm_encoder::{
        CodeSection, Component, ComponentExportKind, ComponentExportSection, CoreTypeSection,
        ExportKind, ExportSection, Function, FunctionSection, Instruction, Module, ModuleSection,
        TypeSection, ValType,
    };
    use wasmparser::{Encoding, Parser, Payload};

    fn module(value: i32) -> Vec<u8> {
        let mut module = Module::new();
        let mut types = TypeSection::new();
        types.ty().function([], [ValType::I32]);
        module.section(&types);
        let mut functions = FunctionSection::new();
        functions.function(0);
        module.section(&functions);
        let mut exports = ExportSection::new();
        exports.export("read", ExportKind::Func, 0);
        module.section(&exports);
        let mut code = CodeSection::new();
        let mut function = Function::new([]);
        function.instruction(&Instruction::I32Const(value));
        function.instruction(&Instruction::End);
        code.function(&function);
        module.section(&code);
        module.finish()
    }

    fn component(modules: &[Vec<u8>]) -> Vec<u8> {
        let mut component = Component::new();
        for module in modules {
            component.section(&wasm_encoder::RawSection {
                id: 1,
                data: module,
            });
        }
        let mut exports = ComponentExportSection::new();
        for index in 0..modules.len() as u32 {
            exports.export(
                &format!("module{index}"),
                ComponentExportKind::Module,
                index,
                None,
            );
        }
        component.section(&exports);
        component.finish()
    }

    #[test]
    fn extracted_modules_keep_exact_original_bytes_and_export_indices() {
        let originals = [module(7), module(9), module(7)];
        let input = component(&originals);
        let bundle = upstream::unbundle(&input, 0).unwrap();
        assert_eq!(
            bundle.modules,
            originals.iter().map(Vec::as_slice).collect::<Vec<_>>()
        );
        let mut names = Vec::new();
        let mut exports = Vec::new();
        for payload in Parser::new(0).parse_all(&bundle.component) {
            match payload.unwrap() {
                Payload::ComponentImportSection(section) => {
                    names.extend(section.into_iter().map(|i| i.unwrap().name.0.to_owned()));
                }
                Payload::ComponentExportSection(section) => {
                    exports.extend(section.into_iter().map(|e| e.unwrap().index));
                }
                Payload::ModuleSection { .. } => panic!("extracted body retained"),
                _ => {}
            }
        }
        assert_eq!(
            names,
            [
                "unbundled-module0",
                "unbundled-module1",
                "unbundled-module2"
            ]
        );
        assert_eq!(exports, [3, 4, 5]);
        // The old module definitions become aliases after the three new imports.
        let mut wire = Vec::new();
        write_bundle(&mut wire, &bundle).unwrap();
        assert_eq!(&wire[..8], b"TBUNB001");
        assert_eq!(u32::from_le_bytes(wire[8..12].try_into().unwrap()), 3);
    }

    #[test]
    fn below_threshold_keeps_original_module_body() {
        let original = module(2);
        let input = component(std::slice::from_ref(&original));
        let bundle = upstream::unbundle(&input, usize::MAX).unwrap();
        assert!(bundle.modules.is_empty());
        let found: Vec<_> = Parser::new(0)
            .parse_all(&bundle.component)
            .filter_map(|p| match p.unwrap() {
                Payload::ModuleSection {
                    unchecked_range, ..
                } => Some(unchecked_range),
                _ => None,
            })
            .collect();
        assert_eq!(found.len(), 1);
        assert_eq!(&bundle.component[found[0].clone()], original.as_slice());
    }

    #[test]
    fn nested_component_and_outer_module_aliases_remain_valid() {
        let mut child = Component::new();
        child.section(&ModuleSection(&{
            let mut module = Module::new();
            let mut types = TypeSection::new();
            types.ty().function([], []);
            module.section(&types);
            module
        }));
        let mut root = Component::new();
        root.section(&wasm_encoder::NestedComponentSection(&child));
        let input = root.finish();
        let bundle = upstream::unbundle(&input, 0).unwrap();
        assert_eq!(bundle.modules.len(), 1);
        assert!(
            Parser::new(0)
                .parse_all(&bundle.component)
                .any(|p| matches!(
                    p.unwrap(),
                    Payload::Version {
                        encoding: Encoding::Component,
                        ..
                    }
                ))
        );
    }

    #[test]
    fn preexisting_core_types_and_mixed_module_indices_survive_translation() {
        let small = module(1);
        let mut large = module(2);
        let mut original = Module::new();
        // Different body length selects just the second module.
        original.section(&wasm_encoder::RawSection {
            id: 0,
            data: &[0, 1, 2, 3, 4],
        });
        large.extend_from_slice(&original.finish()[8..]);
        let mut root = Component::new();
        let mut types = CoreTypeSection::new();
        types.ty().core().function([], []);
        root.section(&types);
        root.section(&wasm_encoder::RawSection {
            id: 1,
            data: &small,
        });
        root.section(&wasm_encoder::RawSection {
            id: 1,
            data: &large,
        });
        let mut exports = ComponentExportSection::new();
        exports.export("small", ComponentExportKind::Module, 0, None);
        exports.export("large", ComponentExportKind::Module, 1, None);
        root.section(&exports);
        let input = root.finish();
        let bundle = upstream::unbundle(&input, small.len()).unwrap();
        assert_eq!(bundle.modules, [&large[..]]);
    }

    #[test]
    fn malformed_core_only_and_oversized_inputs_are_rejected() {
        for input in [b"garbage".as_slice(), module(0).as_slice()] {
            assert!(upstream::unbundle(input, 0).is_err());
        }
        assert!(upstream::unbundle(&vec![0; upstream::INPUT_LIMIT + 1], 0).is_err());
    }

    #[test]
    fn reserved_import_names_and_excessive_nesting_are_rejected() {
        let mut root = Component::new();
        let mut types = CoreTypeSection::new();
        types.ty().module(&wasm_encoder::ModuleType::new());
        root.section(&types);
        let mut imports = wasm_encoder::ComponentImportSection::new();
        imports.import(
            "unbundled-module0",
            wasm_encoder::ComponentTypeRef::Module(0),
        );
        root.section(&imports);
        assert!(upstream::unbundle(&root.finish(), 0).is_err());

        let mut nested = Component::new();
        for _ in 0..17 {
            let mut outer = Component::new();
            outer.section(&wasm_encoder::NestedComponentSection(&nested));
            nested = outer;
        }
        assert!(upstream::unbundle(&nested.finish(), 0).is_err());
    }
}
