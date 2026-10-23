// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
// Derived from Bytecode Alliance wasm-tools v1.245.1 component.rs.
// Helper bodies below are unchanged; the bounded driver replaces upstream file IO.
use anyhow::{Context, Result, bail};
use std::collections::HashMap;
use wasm_encoder::ModuleType;
use wasm_encoder::reencode::{Error, Reencode, ReencodeComponent, RoundtripReencoder};
use wasmparser::types::{CoreTypeId, EntityType, Types};
use wasmparser::{Payload, ValidPayload, WasmFeatures};

pub const INPUT_LIMIT: usize = 32 * 1024 * 1024;
const MAX_MODULES: usize = 4096;
const MAX_DEPTH: usize = 16;
const MAX_SECTIONS: usize = 16_384;

pub struct Bundle<'a> {
    pub component: Vec<u8>,
    pub modules: Vec<&'a [u8]>,
}

fn check_input(input: &[u8]) -> Result<()> {
    if input.len() > INPUT_LIMIT || !wasmparser::Parser::is_component(input) {
        bail!("expected a component within the original 32 MiB input bound");
    }
    let mut stack = Vec::new();
    let mut sections = 0;
    let mut modules = 0;
    for payload in wasmparser::Parser::new(0).parse_all(input) {
        let payload = payload?;
        if stack.last() == Some(&wasmparser::Encoding::Component) && payload.as_section().is_some()
        {
            sections += 1;
            if sections > MAX_SECTIONS {
                bail!("component section bound exceeded");
            }
        }
        match payload {
            Payload::Version { encoding, .. } => {
                stack.push(encoding);
                if stack
                    .iter()
                    .filter(|e| **e == wasmparser::Encoding::Component)
                    .count()
                    > MAX_DEPTH + 1
                {
                    bail!("component nesting bound exceeded");
                }
            }
            Payload::ModuleSection { .. } => {
                modules += 1;
                if modules > MAX_MODULES {
                    bail!("embedded module bound exceeded");
                }
            }
            Payload::ComponentImportSection(imports) if stack.len() == 1 => {
                for import in imports {
                    if import?.name.0.starts_with("unbundled-module") {
                        bail!("input uses the reserved extraction import namespace");
                    }
                }
            }
            Payload::End(_) => {
                stack.pop();
            }
            _ => {}
        }
    }
    // Unlike the upstream type-discovery pass, validate all function bodies.
    wasmparser::Validator::new_with_features(WasmFeatures::all())
        .validate_all(input)
        .context("invalid input component")?;
    Ok(())
}

/// Preserve core module bytes and replace embedded definitions by typed imports.
/// This is a build-time transformation, not Host admission or guest execution.
pub fn unbundle(input: &[u8], threshold: usize) -> Result<Bundle<'_>> {
    check_input(input)?;
    let mut modules_to_extract = Vec::new();
    for payload in wasmparser::Parser::new(0).parse_all(input) {
        if let Payload::ModuleSection {
            unchecked_range, ..
        } = payload?
        {
            modules_to_extract.push(if unchecked_range.len() > threshold {
                Some(&input[unchecked_range])
            } else {
                None
            });
        }
    }
    let mut core_types = CoreTypeInterner::default();
    let mut imports = wasm_encoder::ComponentImportSection::new();
    let mut validator = wasmparser::Validator::new_with_features(WasmFeatures::all());
    let mut modules = Vec::new();
    for module in modules_to_extract.iter().filter_map(|m| *m) {
        let mut types = None;
        for payload in wasmparser::Parser::new(0).parse_all(module) {
            match validator.payload(&payload?)? {
                ValidPayload::Ok | ValidPayload::Parser(_) | ValidPayload::Func(..) => {}
                ValidPayload::End(t) => types = Some(t),
            }
        }
        let types = types.context("module type discovery did not terminate")?;
        validator.reset();
        let mut module_ty = ModuleTypeCreator::new(&mut core_types, &types);
        let view = types.as_ref();
        for (module, name, ty) in view.core_imports().context("missing core imports")? {
            let ty = module_ty.convert_entity_type(ty)?;
            module_ty.module.import(module, name, ty);
        }
        for (name, ty) in view.core_exports().context("missing core exports")? {
            let ty = module_ty.convert_entity_type(ty)?;
            module_ty.module.export(name, ty);
        }
        let module_type_idx = module_ty.component.section.len();
        module_ty.component.section.ty().module(&module_ty.module);
        let name = format!("unbundled-module{}", imports.len());
        imports.import(
            &name,
            wasm_encoder::ComponentTypeRef::Module(module_type_idx),
        );
        modules.push(module);
    }
    let mut component = wasm_encoder::Component::new();
    component.section(&core_types.section);
    component.section(&imports);
    ModuleExtractor {
        extra_core_types: core_types.section.len(),
        extra_core_modules: imports.len(),
        depth: 0,
        modules: modules_to_extract.iter(),
        next_module: 0,
    }
    .parse_component(&mut component, wasmparser::Parser::new(0), input)?;
    let component = component.finish();
    if component.len() > INPUT_LIMIT {
        bail!("transformed component exceeds original 32 MiB input bound");
    }
    wasmparser::Validator::new_with_features(WasmFeatures::all())
        .validate_all(&component)
        .context("invalid transformed component")?;
    Ok(Bundle { component, modules })
}

/// Top-level conversion of core wasm function types to a component core types
/// section.
///
/// This is a builder for a core type section in a component from a list of
/// function types. Functions are dedup'd and translated only once.
#[derive(Default)]
struct CoreTypeInterner {
    section: wasm_encoder::CoreTypeSection,
    intern: HashMap<CoreTypeId, u32>,
}

impl CoreTypeInterner {
    fn convert_func(&mut self, id: CoreTypeId, types: &Types) -> Result<u32> {
        if let Some(ret) = self.intern.get(&id) {
            return Ok(*ret);
        }
        let ty = &types[id];
        if !ty.is_final || ty.supertype_idx.is_some() || ty.composite_type.shared {
            bail!("unsupported core type to translate")
        }
        let f = match &ty.composite_type.inner {
            wasmparser::CompositeInnerType::Func(f) => f,
            _ => bail!("unsupported core type to translate"),
        };

        let ret = self.section.len();
        self.section.ty().core().function(
            f.params()
                .iter()
                .map(|p| RoundtripReencoder.val_type(*p))
                .collect::<Result<Vec<_>, _>>()?,
            f.results()
                .iter()
                .map(|p| RoundtripReencoder.val_type(*p))
                .collect::<Result<Vec<_>, _>>()?,
        );
        self.intern.insert(id, ret);
        Ok(ret)
    }
}

/// Second-level conversion of a module into a component module type.
///
/// This uses a [`CoreTypeInterner`] to create function types and aliases them
/// into the module type being created to cut down on space used to encode
/// function types.
///
/// This also itself has an `intern` cache to avoid creating multiple aliases
/// for the same type.
struct ModuleTypeCreator<'a> {
    component: &'a mut CoreTypeInterner,
    types: &'a Types,
    intern: HashMap<CoreTypeId, u32>,
    module: ModuleType,
}

impl<'a> ModuleTypeCreator<'a> {
    fn new(component: &'a mut CoreTypeInterner, types: &'a Types) -> Self {
        ModuleTypeCreator {
            component,
            types,
            intern: HashMap::new(),
            module: ModuleType::new(),
        }
    }

    fn convert_entity_type(
        &mut self,
        ty: wasmparser::types::EntityType,
    ) -> Result<wasm_encoder::EntityType> {
        Ok(match ty {
            EntityType::Func(id) => wasm_encoder::EntityType::Function(self.convert_func(id)?),
            EntityType::FuncExact(id) => {
                wasm_encoder::EntityType::FunctionExact(self.convert_func(id)?)
            }
            EntityType::Table(t) => wasm_encoder::EntityType::Table(wasm_encoder::TableType {
                shared: t.shared,
                maximum: t.maximum,
                minimum: t.initial,
                table64: t.table64,
                element_type: RoundtripReencoder.ref_type(t.element_type)?,
            }),
            EntityType::Memory(m) => wasm_encoder::EntityType::Memory(wasm_encoder::MemoryType {
                shared: m.shared,
                maximum: m.maximum,
                minimum: m.initial,
                memory64: m.memory64,
                page_size_log2: m.page_size_log2,
            }),
            EntityType::Global(g) => wasm_encoder::EntityType::Global(wasm_encoder::GlobalType {
                mutable: g.mutable,
                shared: g.shared,
                val_type: RoundtripReencoder.val_type(g.content_type)?,
            }),
            EntityType::Tag(id) => wasm_encoder::EntityType::Tag(wasm_encoder::TagType {
                kind: wasm_encoder::TagKind::Exception,
                func_type_idx: self.convert_func(id)?,
            }),
        })
    }

    fn convert_func(&mut self, id: CoreTypeId) -> Result<u32> {
        if let Some(ret) = self.intern.get(&id) {
            return Ok(*ret);
        }
        let outer_index = self.component.convert_func(id, self.types)?;
        let ret = self.module.type_count();
        self.module.alias_outer_core_type(1, outer_index);
        self.intern.insert(id, ret);
        Ok(ret)
    }
}

struct ModuleExtractor<'a> {
    extra_core_types: u32,
    extra_core_modules: u32,
    depth: u32,
    modules: std::slice::Iter<'a, Option<&'a [u8]>>,
    next_module: u32,
}

impl Reencode for ModuleExtractor<'_> {
    type Error = std::convert::Infallible;

    fn type_index(&mut self, index: u32) -> Result<u32, Error<Self::Error>> {
        Ok(if self.depth == 0 {
            index + self.extra_core_types
        } else {
            index
        })
    }
}

impl ReencodeComponent for ModuleExtractor<'_> {
    fn module_index(&mut self, index: u32) -> u32 {
        if self.depth == 0 {
            index + self.extra_core_modules
        } else {
            index
        }
    }

    fn outer_type_index(&mut self, count: u32, index: u32) -> Result<u32, Error<Self::Error>> {
        Ok(if self.depth == count {
            index + self.extra_core_types
        } else {
            index
        })
    }

    fn outer_module_index(&mut self, count: u32, index: u32) -> u32 {
        if self.depth == count {
            index + self.extra_core_modules
        } else {
            index
        }
    }

    fn push_depth(&mut self) {
        self.depth += 1;
    }

    fn pop_depth(&mut self) {
        self.depth -= 1;
    }

    fn parse_component_submodule(
        &mut self,
        component: &mut wasm_encoder::Component,
        _parser: wasmparser::Parser,
        module: &[u8],
    ) -> Result<(), Error<Self::Error>> {
        let extracted = self.modules.next().unwrap().is_some();
        if extracted {
            let mut aliases = wasm_encoder::ComponentAliasSection::new();
            aliases.alias(wasm_encoder::Alias::Outer {
                kind: wasm_encoder::ComponentOuterAliasKind::CoreModule,
                count: self.depth,
                index: self.next_module,
            });
            self.next_module += 1;
            component.section(&aliases);
        } else {
            component.section(&wasm_encoder::RawSection {
                id: wasm_encoder::ComponentSectionId::CoreModule as u8,
                data: module,
            });
        }
        Ok(())
    }
}
