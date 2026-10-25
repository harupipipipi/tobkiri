// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//! Read-only metadata for every embedded core module, including tiny shims.
//! This is not provider, table-at-call-time, TLS, ABI or admission proof.
use anyhow::{Context, Result, bail, ensure};
use wasmparser::{
    CanonicalFunction, ComponentAlias, ComponentExternalKind, ComponentOuterAliasKind,
    ComponentTypeRef, ElementItems, ElementKind, Encoding, ExternalKind, Instance, Parser, Payload,
    TypeRef, WasmFeatures,
};

const INPUT_LIMIT: usize = 32 * 1024 * 1024;
const OUTPUT_LIMIT: usize = 64 * 1024 * 1024;
const ROW_LIMIT: usize = 1_000_000;

#[derive(Default)]
struct Rows {
    bytes: Vec<u8>,
    count: usize,
}

impl Rows {
    fn row(&mut self, fields: &[String]) -> Result<()> {
        self.count += 1;
        ensure!(self.count <= ROW_LIMIT, "graph row bound exceeded");
        self.bytes.push(b'[');
        for (i, field) in fields.iter().enumerate() {
            ensure!(field.len() <= 1024 * 1024, "graph field bound exceeded");
            if i > 0 {
                self.bytes.push(b',');
            }
            self.bytes.push(b'"');
            for c in field.chars() {
                match c {
                    '"' => self.bytes.extend_from_slice(b"\\\""),
                    '\\' => self.bytes.extend_from_slice(b"\\\\"),
                    c if c < ' ' => self
                        .bytes
                        .extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes()),
                    c => {
                        let mut encoded = [0; 4];
                        self.bytes
                            .extend_from_slice(c.encode_utf8(&mut encoded).as_bytes());
                    }
                }
            }
            self.bytes.push(b'"');
            ensure!(
                self.bytes.len() <= OUTPUT_LIMIT,
                "graph output bound exceeded"
            );
        }
        self.bytes.extend_from_slice(b"]\n");
        ensure!(
            self.bytes.len() <= OUTPUT_LIMIT,
            "graph output bound exceeded"
        );
        Ok(())
    }
}

macro_rules! emit {
    ($out:expr; $($value:expr),+ $(,)?) => {
        $out.row(&[$($value.to_string()),+])?
    };
}

#[derive(Default)]
struct Scope {
    id: usize,
    modules: u32,
    instances: u32,
    components: u32,
    component_instances: u32,
    core: [u32; 5],
}

#[derive(Default)]
struct Core {
    id: usize,
    full_operators: bool,
    types: u32,
    entities: [u32; 5],
    imported_functions: u32,
    bodies: u32,
}

enum Frame {
    Component(Scope),
    Module(Core),
}

fn kind_index(kind: ExternalKind) -> usize {
    match kind {
        ExternalKind::Func | ExternalKind::FuncExact => 0,
        ExternalKind::Table => 1,
        ExternalKind::Memory => 2,
        ExternalKind::Global => 3,
        ExternalKind::Tag => 4,
    }
}

fn scope(stack: &mut [Frame]) -> Result<&mut Scope> {
    match stack.last_mut() {
        Some(Frame::Component(s)) => Ok(s),
        _ => bail!("expected enclosing component scope"),
    }
}

fn core(stack: &mut [Frame]) -> Result<&mut Core> {
    match stack.last_mut() {
        Some(Frame::Module(m)) => Ok(m),
        _ => bail!("expected enclosing core module"),
    }
}

fn expression(expr: wasmparser::ConstExpr<'_>) -> Result<String> {
    let mut reader = expr.get_operators_reader();
    let mut ops = Vec::new();
    while !reader.eof() {
        ensure!(ops.len() < 4096, "constant expression bound exceeded");
        ops.push(format!("{:?}", reader.read()?));
    }
    Ok(ops.join("; "))
}

/// Fully validate the component, then emit bounded source-index metadata.
pub fn observe(input: &[u8]) -> Result<Vec<u8>> {
    observe_detailed(input, &[])
}

/// Record every instruction only in explicitly selected definition occurrences.
/// Input, output, validation and execution-authority bounds remain unchanged.
pub fn observe_detailed(input: &[u8], full_modules: &[usize]) -> Result<Vec<u8>> {
    ensure!(
        full_modules.len() <= 16 && full_modules.windows(2).all(|p| p[0] < p[1]),
        "full module selectors must be unique, ascending and bounded"
    );
    ensure!(input.len() <= INPUT_LIMIT, "component input bound exceeded");
    ensure!(Parser::is_component(input), "expected component bytes");
    // Apply framing/count/depth bounds before the full body validator.
    let mut depth = 0usize;
    let mut sections = 0usize;
    let mut modules = 0usize;
    for payload in Parser::new(0).parse_all(input) {
        let payload = payload?;
        if payload.as_section().is_some() {
            sections += 1;
            ensure!(sections <= 16384, "section bound exceeded");
        }
        match payload {
            Payload::Version { .. } => {
                depth += 1;
                ensure!(depth <= 18, "nesting bound exceeded");
            }
            Payload::ModuleSection { .. } => {
                modules += 1;
                ensure!(modules <= 4096, "module bound exceeded");
            }
            Payload::End(_) => depth = depth.checked_sub(1).context("unbalanced parser")?,
            _ => {}
        }
    }
    ensure!(depth == 0, "incomplete component");
    ensure!(
        full_modules.iter().all(|id| *id < modules),
        "full module selector is absent"
    );
    wasmparser::Validator::new_with_features(WasmFeatures::all())
        .validate_all(input)
        .context("invalid component graph input")?;

    let mut out = Rows::default();
    emit!(out; "format", "tobkiri.core-graph-observation/1");
    emit!(out; "input", input.len());
    emit!(out; "validation", "wasmparser-0.245.1-all-function-bodies");
    emit!(out; "full-operator-modules", full_modules.iter().map(usize::to_string).collect::<Vec<_>>().join(","));
    emit!(out; "scope", "source index graph; no runtime provider or storage proof");
    let mut stack = Vec::new();
    let mut next_scope = 1usize;
    let mut next_module = 0usize;
    let mut pending_scope = None;
    let mut pending_module = None;
    for payload in Parser::new(0).parse_all(input) {
        match payload? {
            Payload::Version { encoding, .. } => match encoding {
                Encoding::Component => {
                    let id = if stack.is_empty() {
                        0
                    } else {
                        pending_scope.take().context("missing nested scope")?
                    };
                    stack.push(Frame::Component(Scope {
                        id,
                        ..Scope::default()
                    }));
                }
                Encoding::Module => {
                    let id = pending_module.take().context("missing embedded module")?;
                    stack.push(Frame::Module(Core {
                        id,
                        full_operators: full_modules.contains(&id),
                        ..Core::default()
                    }));
                }
            },
            Payload::ComponentSection {
                unchecked_range, ..
            } => {
                let s = scope(&mut stack)?;
                emit!(out; "component", s.id, s.components, next_scope, unchecked_range.start, unchecked_range.len());
                pending_scope = Some(next_scope);
                s.components += 1;
                next_scope += 1;
            }
            Payload::ModuleSection {
                unchecked_range, ..
            } => {
                let s = scope(&mut stack)?;
                emit!(out; "module", s.id, s.modules, next_module, unchecked_range.start, unchecked_range.len());
                pending_module = Some(next_module);
                s.modules += 1;
                next_module += 1;
            }
            Payload::InstanceSection(reader) => {
                for instance in reader {
                    let s = scope(&mut stack)?;
                    let index = s.instances;
                    s.instances += 1;
                    match instance? {
                        Instance::Instantiate { module_index, args } => {
                            emit!(out; "instantiate", s.id, index, module_index);
                            for arg in args {
                                emit!(out; "argument", s.id, index, arg.name, format!("{:?}", arg.kind), arg.index);
                            }
                        }
                        Instance::FromExports(exports) => {
                            emit!(out; "instance-exports", s.id, index);
                            for export in exports {
                                emit!(out; "instance-export", s.id, index, export.name, format!("{:?}", export.kind), export.index);
                            }
                        }
                    }
                }
            }
            Payload::ComponentAliasSection(reader) => {
                for alias in reader {
                    let s = scope(&mut stack)?;
                    match alias? {
                        ComponentAlias::CoreInstanceExport {
                            kind,
                            instance_index,
                            name,
                        } => {
                            let index = &mut s.core[kind_index(kind)];
                            emit!(out; "core-alias", s.id, format!("{kind:?}"), *index, instance_index, name);
                            *index += 1;
                        }
                        ComponentAlias::Outer { kind, count, index } => {
                            let target = match kind {
                                ComponentOuterAliasKind::CoreModule => {
                                    let n = s.modules;
                                    s.modules += 1;
                                    n.to_string()
                                }
                                ComponentOuterAliasKind::Component => {
                                    let n = s.components;
                                    s.components += 1;
                                    n.to_string()
                                }
                                _ => "untracked-type-index".into(),
                            };
                            emit!(out; "outer-alias", s.id, format!("{kind:?}"), target, count, index);
                        }
                        ComponentAlias::InstanceExport {
                            kind,
                            instance_index,
                            name,
                        } => {
                            let target = match kind {
                                ComponentExternalKind::Module => {
                                    let n = s.modules;
                                    s.modules += 1;
                                    n.to_string()
                                }
                                ComponentExternalKind::Component => {
                                    let n = s.components;
                                    s.components += 1;
                                    n.to_string()
                                }
                                ComponentExternalKind::Instance => {
                                    let n = s.component_instances;
                                    s.component_instances += 1;
                                    n.to_string()
                                }
                                _ => "untracked-component-value-index".into(),
                            };
                            emit!(out; "component-alias", s.id, format!("{kind:?}"), target, instance_index, name);
                        }
                    }
                }
            }
            Payload::ComponentCanonicalSection(reader) => {
                for canonical in reader {
                    let s = scope(&mut stack)?;
                    let canonical = canonical?;
                    if matches!(canonical, CanonicalFunction::Lift { .. }) {
                        emit!(out; "canonical-lift", s.id, format!("{canonical:?}"));
                    } else {
                        emit!(out; "canonical-core-function", s.id, s.core[0], format!("{canonical:?}"));
                        s.core[0] += 1;
                    }
                }
            }
            Payload::ComponentImportSection(reader) => {
                for import in reader {
                    let import = import?;
                    let s = scope(&mut stack)?;
                    let target = match import.ty {
                        ComponentTypeRef::Module(_) => {
                            let n = s.modules;
                            s.modules += 1;
                            n.to_string()
                        }
                        ComponentTypeRef::Component(_) => {
                            let n = s.components;
                            s.components += 1;
                            n.to_string()
                        }
                        ComponentTypeRef::Instance(_) => {
                            let n = s.component_instances;
                            s.component_instances += 1;
                            n.to_string()
                        }
                        _ => "untracked-component-value-index".into(),
                    };
                    emit!(out; "component-import", s.id, import.name.0, target, format!("{:?}", import.ty));
                }
            }
            Payload::ComponentInstanceSection(reader) => {
                for instance in reader {
                    let s = scope(&mut stack)?;
                    emit!(out; "component-instance", s.id, s.component_instances, format!("{:?}", instance?));
                    s.component_instances += 1;
                }
            }
            Payload::ComponentExportSection(reader) => {
                for export in reader {
                    let export = export?;
                    let s = scope(&mut stack)?;
                    // Exporting also appends an alias in the component namespace.
                    let target = match export.kind {
                        ComponentExternalKind::Module => {
                            let n = s.modules;
                            s.modules += 1;
                            n.to_string()
                        }
                        ComponentExternalKind::Component => {
                            let n = s.components;
                            s.components += 1;
                            n.to_string()
                        }
                        ComponentExternalKind::Instance => {
                            let n = s.component_instances;
                            s.component_instances += 1;
                            n.to_string()
                        }
                        _ => "untracked-component-value-index".into(),
                    };
                    emit!(out; "component-export", s.id, export.name.0, format!("{:?}", export.kind), target, export.index);
                }
            }
            Payload::ImportSection(reader) => {
                for import in reader.into_imports() {
                    let import = import?;
                    let m = core(&mut stack)?;
                    let kind = match import.ty {
                        TypeRef::Func(_) | TypeRef::FuncExact(_) => ExternalKind::Func,
                        TypeRef::Table(_) => ExternalKind::Table,
                        TypeRef::Memory(_) => ExternalKind::Memory,
                        TypeRef::Global(_) => ExternalKind::Global,
                        TypeRef::Tag(_) => ExternalKind::Tag,
                    };
                    let index = m.entities[kind_index(kind)];
                    emit!(out; "import", m.id, import.module, import.name, format!("{kind:?}"), index, format!("{:?}", import.ty));
                    m.entities[kind_index(kind)] += 1;
                    if kind == ExternalKind::Func {
                        m.imported_functions += 1;
                    }
                }
            }
            Payload::TypeSection(reader) => {
                let m = core(&mut stack)?;
                for group in reader {
                    for ty in group?.types() {
                        emit!(out; "type", m.id, m.types, format!("{ty:?}"));
                        m.types += 1;
                    }
                }
            }
            Payload::ExportSection(reader) => {
                let m = core(&mut stack)?;
                for export in reader {
                    let export = export?;
                    emit!(out; "export", m.id, export.name, format!("{:?}", export.kind), export.index);
                }
            }
            Payload::FunctionSection(reader) => {
                let m = core(&mut stack)?;
                for ty in reader {
                    emit!(out; "function-type", m.id, m.entities[0], ty?);
                    m.entities[0] += 1;
                }
            }
            Payload::GlobalSection(reader) => {
                let m = core(&mut stack)?;
                for global in reader {
                    let global = global?;
                    emit!(out; "global", m.id, m.entities[3], format!("{:?}", global.ty), expression(global.init_expr)?);
                    m.entities[3] += 1;
                }
            }
            Payload::MemorySection(reader) => {
                let m = core(&mut stack)?;
                for memory in reader {
                    emit!(out; "memory", m.id, m.entities[2], format!("{:?}", memory?));
                    m.entities[2] += 1;
                }
            }
            Payload::TableSection(reader) => {
                let m = core(&mut stack)?;
                for table in reader {
                    emit!(out; "table", m.id, m.entities[1], format!("{:?}", table?));
                    m.entities[1] += 1;
                }
            }
            Payload::ElementSection(reader) => {
                let m = core(&mut stack)?;
                for (index, element) in reader.into_iter().enumerate() {
                    let element = element?;
                    match element.kind {
                        ElementKind::Active {
                            table_index,
                            offset_expr,
                        } => {
                            emit!(out; "element", m.id, index, "active", table_index.unwrap_or(0), expression(offset_expr)?);
                        }
                        ElementKind::Passive => {
                            emit!(out; "element", m.id, index, "passive");
                        }
                        ElementKind::Declared => {
                            emit!(out; "element", m.id, index, "declared");
                        }
                    }
                    match element.items {
                        ElementItems::Functions(items) => {
                            for (slot, func) in items.into_iter().enumerate() {
                                emit!(out; "element-function", m.id, index, slot, func?);
                            }
                        }
                        ElementItems::Expressions(ty, items) => {
                            for (slot, expr) in items.into_iter().enumerate() {
                                emit!(out; "element-expression", m.id, index, slot, format!("{ty:?}"), expression(expr?)?);
                            }
                        }
                    }
                }
            }
            Payload::StartSection { func, .. } => {
                emit!(out; "start", core(&mut stack)?.id, func);
            }
            Payload::CodeSectionEntry(body) => {
                let m = core(&mut stack)?;
                let index = m.imported_functions + m.bodies;
                m.bodies += 1;
                let range = body.range();
                emit!(out; "body", m.id, index, range.start, range.len());
                for local in body.get_locals_reader()? {
                    let (count, ty) = local?;
                    emit!(out; "locals", m.id, index, count, format!("{ty:?}"));
                }
                let full = m.full_operators || range.len() <= 256;
                let mut ops = body.get_operators_reader()?;
                while !ops.eof() {
                    let offset = ops.original_position();
                    let op = format!("{:?}", ops.read()?);
                    let important = [
                        "Call",
                        "ReturnCall",
                        "GlobalSet",
                        "TableSet",
                        "TableInit",
                        "TableCopy",
                        "TableFill",
                        "TableGrow",
                        "MemoryInit",
                        "MemoryCopy",
                        "MemoryFill",
                    ]
                    .iter()
                    .any(|prefix| op.starts_with(prefix));
                    if full || important {
                        emit!(out; "operator", m.id, index, offset, op);
                    }
                }
                emit!(out; "body-coverage", m.id, index, if full { "all-operators" } else { "selected-call-and-mutation-operators" });
            }
            Payload::End(offset) => match stack.pop().context("unbalanced observation stack")? {
                Frame::Module(m) => {
                    emit!(out; "module-end", m.id, offset, m.bodies, m.imported_functions);
                }
                Frame::Component(s) => {
                    emit!(out; "component-end", s.id, offset);
                }
            },
            _ => {}
        }
    }
    ensure!(stack.is_empty(), "observation ended inside a nested scope");
    emit!(out; "complete", next_scope, next_module);
    emit!(out; "final_provider_resolution_verified", "false");
    emit!(out; "errno_storage_identity_verified", "false");
    emit!(out; "production_enabled", "false");
    Ok(out.bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use wasm_encoder::{
        CodeSection, Component, ComponentExportKind, ComponentExportSection, Function,
        FunctionSection, Instruction, Module, TypeSection, ValType,
    };

    fn module(valid: bool) -> Vec<u8> {
        let mut m = Module::new();
        let mut types = TypeSection::new();
        types.ty().function([], [ValType::I32]);
        m.section(&types);
        let mut functions = FunctionSection::new();
        functions.function(0);
        m.section(&functions);
        let mut code = CodeSection::new();
        let mut f = Function::new([]);
        if valid {
            f.instruction(&Instruction::I32Const(7));
        }
        f.instruction(&Instruction::End);
        code.function(&f);
        m.section(&code);
        m.finish()
    }

    #[test]
    fn tiny_identical_modules_keep_separate_occurrences_and_all_bodies() {
        let raw = module(true);
        assert!(raw.len() < 10240);
        let mut c = Component::new();
        c.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        c.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        let rows = String::from_utf8(observe(&c.finish()).unwrap()).unwrap();
        assert!(rows.contains("[\"module\",\"0\",\"0\",\"0\","));
        assert!(rows.contains("[\"module\",\"0\",\"1\",\"1\","));
        assert_eq!(rows.matches("[\"body\",").count(), 2);
        assert!(rows.contains("[\"complete\",\"1\",\"2\"]"));
        assert!(rows.ends_with("[\"production_enabled\",\"false\"]\n"));
    }

    #[test]
    fn graph_validates_function_bodies_even_when_never_instantiated() {
        let raw = module(false);
        let mut c = Component::new();
        c.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        assert!(observe(&c.finish()).is_err());
        assert!(observe(&module(true)).is_err());
    }

    #[test]
    fn nested_scope_does_not_merge_module_index_zero() {
        let raw = module(true);
        let mut inner = Component::new();
        inner.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        let mut outer = Component::new();
        outer.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        outer.section(&wasm_encoder::NestedComponentSection(&inner));
        let rows = String::from_utf8(observe(&outer.finish()).unwrap()).unwrap();
        assert!(rows.contains("[\"module\",\"1\",\"0\",\"1\","));
    }

    #[test]
    fn component_export_alias_occupies_its_module_index() {
        let raw = module(true);
        let mut c = Component::new();
        c.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        let mut exports = ComponentExportSection::new();
        exports.export("aliased-module", ComponentExportKind::Module, 0, None);
        c.section(&exports);
        c.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        let rows = String::from_utf8(observe(&c.finish()).unwrap()).unwrap();
        assert!(rows.contains("[\"module\",\"0\",\"2\",\"1\","));
    }

    #[test]
    fn strings_escape_controls_quotes_and_backslashes() {
        let mut out = Rows::default();
        out.row(&["a\n\"\\é".into()]).unwrap();
        assert_eq!(
            String::from_utf8(out.bytes).unwrap(),
            "[\"a\\u000a\\\"\\\\é\"]\n"
        );
    }

    #[test]
    fn selected_module_captures_large_function_instructions_without_raising_bounds() {
        let mut module = Module::new();
        let mut types = TypeSection::new();
        types.ty().function([], []);
        module.section(&types);
        let mut functions = FunctionSection::new();
        functions.function(0);
        module.section(&functions);
        let mut code = CodeSection::new();
        let mut f = Function::new([]);
        for _ in 0..300 {
            f.instruction(&Instruction::Nop);
        }
        f.instruction(&Instruction::End);
        code.function(&f);
        module.section(&code);
        let raw = module.finish();
        let mut c = Component::new();
        c.section(&wasm_encoder::RawSection { id: 1, data: &raw });
        let input = c.finish();
        let normal = String::from_utf8(observe(&input).unwrap()).unwrap();
        let full = String::from_utf8(observe_detailed(&input, &[0]).unwrap()).unwrap();
        assert!(!normal.contains(",\"Nop\"]"));
        assert_eq!(full.matches(",\"Nop\"]").count(), 300);
        assert!(observe_detailed(&input, &[1]).is_err());
        assert!(observe_detailed(&input, &[0, 0]).is_err());
    }
}
