//! Validate signatures before linking effectful dynamic callbacks.
#[cfg(test)]
#[path = "deepthink_ledger_abi_tests.rs"]
mod deepthink_ledger_tests;
#[cfg(test)]
#[path = "generation_reader_abi_tests.rs"]
mod generation_reader_tests;

use anyhow::{Context, Result, bail};
use std::collections::BTreeMap;
use tobkiri_manifest::{ShapeDef, VerifiedPack, WitFunction, WitType};
use tobkiri_wit::{AbiFunction, AbiType};
use wasmtime::{
    Engine,
    component::{
        Component, ResourceType,
        types::{ComponentFunc, ComponentItem, Type},
    },
};

pub(crate) fn validate_abi(
    engine: &Engine,
    component: &Component,
    pack: &VerifiedPack,
    shapes: &[ShapeDef],
) -> Result<()> {
    let packages = BTreeMap::from([
        ("1.0.0", tobkiri_wit::core_abi_version("1.0.0")?),
        ("1.1.0", tobkiri_wit::core_abi_version("1.1.0")?),
    ]);
    let mut resources: BTreeMap<&str, BTreeMap<usize, ResourceType>> = BTreeMap::new();
    let ty = component.component_type();
    for (name, item) in ty.imports(engine).chain(ty.exports(engine)) {
        let Some((interface, version)) = core_interface(name) else {
            continue;
        };
        let expected = packages
            .get(version)
            .context("unknown exact core package")?
            .get(interface)
            .context("unknown core interface")?;
        let ComponentItem::ComponentInstance(instance) = item.ty else {
            bail!("core ABI is not an interface")
        };
        for (name, item) in instance.exports(engine) {
            if let ComponentItem::Resource(resource) = item.ty {
                let id = expected
                    .resources
                    .get(name)
                    .context("unknown core resource")?;
                if resources.iter().any(|(other, types)| {
                    *other != version && types.values().any(|other| *other == resource)
                }) {
                    bail!(
                        "core resources from different package versions cannot share nominal identity"
                    )
                }
                if resources
                    .entry(version)
                    .or_default()
                    .insert(*id, resource)
                    .is_some_and(|old| old != resource)
                {
                    bail!("core resource identity mismatch")
                }
            }
        }
    }
    for (name, item) in ty.imports(engine).chain(ty.exports(engine)) {
        if name.starts_with("wasi:") {
            continue;
        }
        let ComponentItem::ComponentInstance(instance) = item.ty else {
            bail!("ABI must be an interface")
        };
        let core_name = core_interface(name);
        let empty_resources = BTreeMap::new();
        let resource_scope = core_name
            .and_then(|(_, version)| resources.get(version))
            .unwrap_or(&empty_resources);
        let declared = pack
            .manifest
            .ports
            .consumes
            .iter()
            .chain(&pack.manifest.ports.provides)
            .find(|port| {
                crate::host::interface_name(&tobkiri_canon::ids::ShapeRef::new(
                    port.shape.clone(),
                    port.version,
                )) == name
            });
        let shape = declared
            .map(|port| {
                shapes
                    .iter()
                    .find(|s| s.reference.name == port.shape && s.reference.version == port.version)
                    .context("declared ABI shape missing")
            })
            .transpose()?;
        for (member, item) in instance.exports(engine) {
            match item.ty {
                ComponentItem::ComponentFunc(actual) => {
                    let expected = if let Some((name, version)) = core_name {
                        packages
                            .get(version)
                            .context("unknown exact core package")?
                            .get(name)
                            .context("unknown core interface")?
                            .functions
                            .get(member)
                            .context("unknown core function")?
                            .clone()
                    } else {
                        from_function(
                            shape
                                .context("undeclared imported operation")?
                                .functions
                                .get(&tobkiri_canon::ids::OpName::parse(member)?)
                                .context("undeclared shape operation")?,
                        )
                    };
                    check_function(&actual, &expected, resource_scope)?;
                }
                ComponentItem::Type(actual) => {
                    if let Some((name, version)) = core_name {
                        let expected = packages
                            .get(version)
                            .context("unknown exact core package")?
                            .get(name)
                            .context("unknown core interface")?
                            .types
                            .get(member)
                            .context("unknown core type")?;
                        if !matches_type(&actual, expected, resource_scope) {
                            bail!("core named type ABI mismatch")
                        }
                    }
                }
                ComponentItem::Resource(_) => {
                    if core_name.is_none() {
                        bail!("shape resource forbidden")
                    }
                }
                _ => bail!("unsupported component ABI item"),
            }
        }
    }
    Ok(())
}
fn core_interface(name: &str) -> Option<(&str, &str)> {
    let rest = name.strip_prefix("tobkiri:core/")?;
    let (interface, version) = rest.rsplit_once('@')?;
    matches!(version, "1.0.0" | "1.1.0").then_some((interface, version))
}
fn check_function(
    actual: &ComponentFunc,
    expected: &AbiFunction,
    resources: &BTreeMap<usize, ResourceType>,
) -> Result<()> {
    let params: Vec<_> = actual.params().collect();
    let results: Vec<_> = actual.results().collect();
    if params.len() != expected.params.len()
        || results.len() != usize::from(expected.result.is_some())
    {
        bail!("function ABI arity mismatch")
    }
    for ((name, ty), (expected_name, expected_ty)) in params.iter().zip(&expected.params) {
        if name != expected_name || !matches_type(ty, expected_ty, resources) {
            bail!("function ABI parameter mismatch")
        }
    }
    if let Some(expected) = &expected.result
        && !matches_type(&results[0], expected, resources)
    {
        bail!("function ABI result mismatch")
    }
    Ok(())
}
fn matches_type(
    actual: &Type,
    expected: &AbiType,
    resources: &BTreeMap<usize, ResourceType>,
) -> bool {
    use AbiType as A;
    use Type as T;
    match (actual, expected) {
        (T::Bool, A::Primitive("bool"))
        | (T::U8, A::Primitive("u8"))
        | (T::U16, A::Primitive("u16"))
        | (T::U32, A::Primitive("u32"))
        | (T::U64, A::Primitive("u64"))
        | (T::S8, A::Primitive("s8"))
        | (T::S16, A::Primitive("s16"))
        | (T::S32, A::Primitive("s32"))
        | (T::S64, A::Primitive("s64"))
        | (T::Float32, A::Primitive("f32"))
        | (T::Float64, A::Primitive("f64"))
        | (T::Char, A::Primitive("char"))
        | (T::String, A::Primitive("string")) => true,
        (T::List(a), A::List(e)) => matches_type(&a.ty(), e, resources),
        (T::Tuple(a), A::Tuple(e)) => {
            a.types().len() == e.len()
                && a.types()
                    .zip(e)
                    .all(|(a, e)| matches_type(&a, e, resources))
        }
        (T::Record(a), A::Record(e)) => {
            a.fields().len() == e.len()
                && a.fields()
                    .zip(e)
                    .all(|(a, (name, e))| a.name == name && matches_type(&a.ty, e, resources))
        }
        (T::Variant(a), A::Variant(e)) => {
            a.cases().len() == e.len()
                && a.cases().zip(e).all(|(a, (name, e))| {
                    a.name == name && optional_type(a.ty.as_ref(), e.as_ref(), resources)
                })
        }
        (T::Enum(a), A::Enum(e)) => a.names().eq(e.iter().map(String::as_str)),
        (T::Flags(a), A::Flags(e)) => a.names().eq(e.iter().map(String::as_str)),
        (T::Option(a), A::Option(e)) => matches_type(&a.ty(), e, resources),
        (T::Result(a), A::Result(ok, err)) => {
            optional_type(a.ok().as_ref(), ok.as_deref(), resources)
                && optional_type(a.err().as_ref(), err.as_deref(), resources)
        }
        (T::Own(a), A::Own(e)) | (T::Borrow(a), A::Borrow(e)) => resources.get(e) == Some(a),
        _ => false,
    }
}
fn optional_type(a: Option<&Type>, e: Option<&AbiType>, r: &BTreeMap<usize, ResourceType>) -> bool {
    match (a, e) {
        (None, None) => true,
        (Some(a), Some(e)) => matches_type(a, e, r),
        _ => false,
    }
}
fn from_function(function: &WitFunction) -> AbiFunction {
    AbiFunction {
        params: function
            .params
            .iter()
            .map(|(n, t)| (n.clone(), from_type(t)))
            .collect(),
        result: function.result.as_ref().map(from_type),
    }
}
fn from_type(ty: &WitType) -> AbiType {
    use AbiType as A;
    use WitType as W;
    match ty {
        W::Bool => A::Primitive("bool"),
        W::U8 => A::Primitive("u8"),
        W::U16 => A::Primitive("u16"),
        W::U32 => A::Primitive("u32"),
        W::U64 => A::Primitive("u64"),
        W::S8 => A::Primitive("s8"),
        W::S16 => A::Primitive("s16"),
        W::S32 => A::Primitive("s32"),
        W::S64 => A::Primitive("s64"),
        W::F32 => A::Primitive("f32"),
        W::F64 => A::Primitive("f64"),
        W::Char => A::Primitive("char"),
        W::String | W::Json => A::Primitive("string"),
        W::List(t) => A::List(Box::new(from_type(t))),
        W::Tuple(t) => A::Tuple(t.iter().map(from_type).collect()),
        W::Record(t) => A::Record(
            t.iter()
                .map(|f| (f.name.clone(), from_type(&f.ty)))
                .collect(),
        ),
        W::Variant(t) => A::Variant(
            t.iter()
                .map(|c| (c.name.clone(), c.ty.as_ref().map(from_type)))
                .collect(),
        ),
        W::Enum(t) => A::Enum(t.clone()),
        W::Flags(t) => A::Flags(t.clone()),
        W::Option(t) => A::Option(Box::new(from_type(t))),
        W::Result { ok, err } => A::Result(
            ok.as_deref().map(from_type).map(Box::new),
            err.as_deref().map(from_type).map(Box::new),
        ),
        W::StreamHandle | W::CallbackHandle => A::Record(vec![("id".into(), A::Primitive("u64"))]),
    }
}

#[cfg(test)]
mod staged_component_tests {
    use super::*;
    use std::collections::BTreeSet;
    use std::path::Path;
    use tobkiri_manifest::{
        RuntimeSpec, build_tpk, inspect_pack_wit, load_shapes, parse_manifest, verify_tpk,
    };

    const READER_ID: &str = "local.openrouter-generation-reader";
    const READER_VIEW: &str = "local.openrouter-generation-reader.generation-reader@1.0";
    const INPUT_ID: &str = "local.deepthink-input";
    const INPUT_VIEW: &str = "local.deepthink.continuation-input@1.0";
    const BASE_PACKS: &[&str] = &[
        "harness-chat",
        "echo-chat",
        "harness-agent",
        "harness-ui",
        "openai-compat-provider",
        "mock-llm",
        "harness-conversation-store",
        "harness-prompt",
        "terminal-input",
        "terminal-output",
        "echo-llm",
        "echo-store",
        "echo-prompt",
        "echo-input",
        "echo-output",
        "pi-adapter",
        "harness-tools",
        "deepthink",
        "deepthink-input",
        "openrouter-mimo-provider",
        "mimo-prompt",
        "deepthink-ui",
        "openrouter-generation-reader",
    ];
    const PRESET_STYLES: &[&str] = &[
        "chatgpt-agent-deep-research-service",
        "claude-code-service",
        "cline-pair-programmer-service",
        "gemini-cli-service",
        "genspark-report-service",
        "manus-project-service",
        "openclaw-hermes-local-service",
    ];

    fn expected_artifacts() -> BTreeMap<String, (usize, usize)> {
        // (distinct pinned artifacts, Wasm artifacts). Only harness-chat has
        // both the unchanged baseline and the explicit DeepThink Flow variant.
        let mut expected = BASE_PACKS
            .iter()
            .map(|name| {
                let data = [
                    "harness-chat",
                    "echo-chat",
                    "harness-agent",
                    "harness-ui",
                    "deepthink-ui",
                ]
                .contains(name);
                (format!("local.{name}"), (1, usize::from(!data)))
            })
            .collect::<BTreeMap<_, _>>();
        expected.insert("local.harness-chat".into(), (2, 0));
        for style in PRESET_STYLES {
            expected.insert(format!("local.preset-{style}"), (1, 1));
            expected.insert(format!("local.deepthink-preset-{style}"), (1, 1));
        }
        expected
    }

    fn check_reader_declaration(reader: &VerifiedPack, pack_root: &Path) -> Result<()> {
        let reference = tobkiri_manifest::ShapeRef::parse(READER_VIEW)?;
        let shapes = load_shapes(&reader.manifest, &reader.files)?;
        assert_eq!(shapes.len(), 1, "one authoritative Reader shape");
        assert_eq!(shapes[0].reference, reference);
        assert_eq!(
            shapes[0]
                .functions
                .keys()
                .map(|op| op.as_str())
                .collect::<BTreeSet<_>>(),
            BTreeSet::from(["lookup", "lookup-requested"]),
        );
        assert_eq!(reader.manifest.shapes.len(), 1);
        assert_eq!(reader.manifest.ports.provides.len(), 1);
        let provided = &reader.manifest.ports.provides[0];
        assert_eq!(provided.shape, reference.name);
        assert_eq!(provided.version, reference.version);
        assert!(!provided.optional);
        assert!(reader.manifest.ports.consumes.is_empty());
        assert!(reader.manifest.ports.untyped.is_empty());
        assert!(reader.manifest.inputs.is_empty());
        assert!(reader.manifest.flows.is_empty());
        assert!(reader.manifest.overlays.is_empty());
        assert!(reader.manifest.stores.is_empty());
        assert!(reader.manifest.ui.is_none());
        let source_wit = std::fs::read(
            pack_root.join("recovery/local.openrouter-generation-reader/wit/deps/reader.wit"),
        )?;
        assert_eq!(
            reader.files.get("wit/deps/reader.wit"),
            Some(&source_wit),
            "staged Reader WIT must be the exact current defining source",
        );
        let permissions = &reader.manifest.permissions;
        assert_eq!(
            permissions.network.origins,
            BTreeSet::from(["https://openrouter.ai".to_owned()]),
        );
        assert!(permissions.network.user_origins.is_none());
        assert_eq!(permissions.secrets.len(), 1);
        assert_eq!(permissions.secrets[0].name, "api-key");
        assert!(!permissions.secrets[0].reveal && !permissions.secrets[0].optional);
        assert!(
            permissions.fs.is_empty()
                && permissions.proc.is_empty()
                && !permissions.pty
                && !permissions.native
                && !permissions.storage.private
                && !permissions.storage.public_read
                && !permissions.storage.public_write
                && !permissions.cross_profile_storage
                && !permissions.audit_read
                && permissions.host_io.is_empty()
                && permissions.ports.is_empty()
                && !permissions.mediator,
            "Reader retains only its declared OpenRouter/opaque-key authority",
        );
        Ok(())
    }

    fn staged_pack(directory: &Path) -> Result<VerifiedPack> {
        let manifest = parse_manifest(&std::fs::read(directory.join("pack.json"))?)?;
        let mut files = BTreeMap::new();
        for entry in &manifest.files {
            files.insert(
                entry.path.clone(),
                std::fs::read(directory.join(&entry.path))?,
            );
        }
        let archive = build_tpk(&manifest, &files, None)?;
        Ok(verify_tpk(&archive, &BTreeMap::new())?)
    }

    fn check_input_declaration(input: &VerifiedPack, pack_root: &Path) -> Result<()> {
        assert!(
            input.manifest.shapes.is_empty(),
            "the worker owns this shape"
        );
        assert!(input.manifest.stores.is_empty());
        assert!(input.manifest.flows.is_empty());
        assert!(input.manifest.overlays.is_empty());
        assert!(input.manifest.inputs.is_empty());
        assert!(input.manifest.ports.untyped.is_empty());
        assert!(input.manifest.ui.is_none());
        let reference = tobkiri_manifest::ShapeRef::parse(INPUT_VIEW)?;
        assert_eq!(input.manifest.ports.provides.len(), 1);
        assert_eq!(input.manifest.ports.provides[0].shape, reference.name);
        assert_eq!(input.manifest.ports.provides[0].version, reference.version);
        assert!(!input.manifest.ports.provides[0].optional);
        let flow_views = BTreeSet::from([
            "local.deepthink.control-flow@1.0".to_owned(),
            "local.deepthink.continuation-flow@1.0".to_owned(),
            "local.deepthink.regeneration-flow@1.0".to_owned(),
        ]);
        let mut consumed_views = flow_views.clone();
        consumed_views.insert("local.deepthink.workflow@1.0".to_owned());
        assert_eq!(
            input
                .manifest
                .ports
                .consumes
                .iter()
                .map(|port| {
                    assert!(!port.optional);
                    format!("{}@{}", port.shape, port.version)
                })
                .collect::<BTreeSet<_>>(),
            consumed_views,
        );
        let mut views = flow_views;
        views.insert(INPUT_VIEW.to_owned());
        let permissions = &input.manifest.permissions;
        assert_eq!(permissions.ports, views);
        assert!(
            !permissions.ports.contains("local.deepthink.workflow@1.0"),
            "a WIT type dependency is not direct workflow call authority"
        );
        assert!(permissions.network.origins.is_empty());
        assert!(permissions.network.user_origins.is_none());
        assert!(permissions.secrets.is_empty());
        assert!(permissions.fs.is_empty() && permissions.proc.is_empty());
        assert!(!permissions.pty && !permissions.native && !permissions.mediator);
        assert!(!permissions.cross_profile_storage && !permissions.audit_read);
        assert!(permissions.host_io.is_empty());
        assert!(
            !permissions.storage.private
                && !permissions.storage.public_read
                && !permissions.storage.public_write
        );
        let source_wit = std::fs::read(pack_root.join("shapes/deepthink.wit"))?;
        assert_eq!(input.files.get("wit/deps/deepthink.wit"), Some(&source_wit));
        Ok(())
    }

    /// Admission-only proof for the real built components. This never creates
    /// a DataRoot, grants permission, substitutes HostIo or executes a guest.
    #[tokio::test]
    #[ignore = "requires packs/build.py and build_deepthink_profile.py staging"]
    async fn real_staged_components_match_verified_wit_and_closed_core_abi() -> Result<()> {
        let pack_root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../packs");
        let mut packs = BASE_PACKS
            .iter()
            .map(|name| {
                staged_pack(&pack_root.join(format!("local.{name}")))
                    .with_context(|| format!("verify staged local.{name}"))
            })
            .collect::<Result<Vec<_>>>()?;
        let mut profiles = Vec::new();
        for name in [
            "harness-live",
            "harness-mock",
            "harness-ui",
            "echo",
            "deepthink-mimo",
            "deepthink-mimo-ui",
        ] {
            let path = pack_root.join(format!("profiles/{name}.profile.json"));
            let profile = tobkiri_profile::parse_profile(&std::fs::read(&path)?)?;
            profiles.push((name.to_owned(), profile, path));
        }
        for (directory, suffix, published) in [
            ("presets", ".published.json", true),
            ("deepthink-presets", ".profile.json", false),
        ] {
            let mut paths = std::fs::read_dir(pack_root.join("profiles").join(directory))?
                .map(|entry| entry.map(|entry| entry.path()))
                .collect::<std::io::Result<Vec<_>>>()?;
            paths.retain(|path| path.to_string_lossy().ends_with(suffix));
            paths.sort();
            assert_eq!(paths.len(), 7, "seven staged {directory} Profiles");
            for (index, path) in paths.iter().enumerate() {
                let label = format!(
                    "{directory}/{}",
                    path.file_name().unwrap().to_string_lossy()
                );
                let bytes = std::fs::read(path)?;
                let profile = if published {
                    tobkiri_profile::parse_published(&bytes)?.to_profile(
                        tobkiri_canon::ids::ProfileId::parse(&format!(
                            "prof_staged_preset_{index}"
                        ))?,
                        &label,
                    )
                } else {
                    tobkiri_profile::parse_profile(&bytes)?
                };
                profiles.push((label, profile, path.clone()));
            }
        }
        assert_eq!(profiles.len(), 20);
        for (_, profile, path) in &profiles {
            for pin in &profile.packs {
                if !packs
                    .iter()
                    .any(|pack| pack.manifest.id == pin.id && pack.hash == pin.sha256)
                {
                    let locator = pin
                        .source
                        .strip_prefix("file:")
                        .context("staged Profile must select a local source")?;
                    let directory = path
                        .parent()
                        .context("staged Profile source directory is absent")?
                        .join(locator);
                    let pack = staged_pack(&directory)?;
                    if pack.manifest.id != pin.id || pack.hash != pin.sha256 {
                        bail!("staged Profile source differs from its exact pin")
                    }
                    packs.push(pack);
                }
            }
        }
        let mut actual_artifacts = BTreeMap::new();
        let mut exact_pins = BTreeSet::new();
        for pack in &packs {
            assert!(
                exact_pins.insert((pack.manifest.id.clone(), pack.hash.clone())),
                "a staged artifact must not be counted twice",
            );
            let wasm = match &pack.manifest.runtime {
                RuntimeSpec::Wasm { .. } => true,
                RuntimeSpec::Data => false,
                RuntimeSpec::Native { .. } => bail!("native runtime entered staged Wasm fixtures"),
            };
            let counts = actual_artifacts
                .entry(pack.manifest.id.to_string())
                .or_insert((0usize, 0usize));
            counts.0 += 1;
            counts.1 += usize::from(wasm);
        }
        assert_eq!(
            actual_artifacts,
            expected_artifacts(),
            "the exact baseline, style variants, Flow variant and Reader set must match",
        );
        assert_eq!(
            packs.len(),
            expected_artifacts()
                .values()
                .map(|(count, _)| count)
                .sum::<usize>(),
            "distinct selected artifact inventory",
        );
        let reader = packs
            .iter()
            .find(|pack| pack.manifest.id.as_str() == READER_ID)
            .context("the staged Reader is absent")?;
        check_reader_declaration(reader, &pack_root)?;
        let input = packs
            .iter()
            .find(|pack| pack.manifest.id.as_str() == INPUT_ID)
            .context("the staged input adapter is absent")?;
        check_input_declaration(input, &pack_root)?;
        let mut views = BTreeMap::new();
        for pack in &packs {
            for shape in load_shapes(&pack.manifest, &pack.files)? {
                let reference = shape.reference.clone();
                // Alternative artifacts can retain the same defining Pack ID
                // and identical WIT. Each Profile still selects one exact hash;
                // conflicting definitions must never be merged for ABI checks.
                if let Some(previous) = views.get(&reference) {
                    if previous != &shape {
                        bail!("conflicting authoritative shape in staged fixtures")
                    }
                } else {
                    views.insert(reference, shape);
                }
            }
        }
        let engine = crate::WasmEngine::new()?;
        let shapes = views.values().cloned().collect::<Vec<_>>();
        let mut components = BTreeMap::<tobkiri_canon::Digest, Component>::new();
        let mut wasm_count = 0;
        for pack in &packs {
            let RuntimeSpec::Wasm { component, .. } = &pack.manifest.runtime else {
                continue;
            };
            let inspection = inspect_pack_wit(pack, &views)
                .with_context(|| format!("inspect {}", pack.manifest.id))?;
            if pack.manifest.id.as_str() == READER_ID {
                let expected_imports = tobkiri_wit::MINIMAL_IMPORTS
                    .iter()
                    .copied()
                    .chain(["network", "secrets"])
                    .flat_map(|interface| {
                        ["1.0.0", "1.1.0"]
                            .map(|version| format!("tobkiri:core/{interface}@{version}"))
                    })
                    .collect::<BTreeSet<_>>();
                assert_eq!(inspection.allowed_imports(), &expected_imports);
                let reference = tobkiri_manifest::ShapeRef::parse(READER_VIEW)?;
                for name in ["lookup", "lookup-requested"] {
                    assert!(inspection.core_errors().has_core_error_result(
                        tobkiri_manifest::WitBindingDirection::Export,
                        &reference,
                        &tobkiri_manifest::OpName::parse(name)?,
                    ));
                }
            }
            let bytes = &pack.files[component];
            let digest = tobkiri_canon::Digest::hash(bytes);
            let component = if let Some(component) = components.get(&digest) {
                component.clone()
            } else {
                let component = Component::new(engine.engine(), bytes)?;
                components.insert(digest, component.clone());
                component
            };
            let imports =
                crate::validate_imports(engine.engine(), &component, inspection.allowed_imports())
                    .with_context(|| format!("imports {}", pack.manifest.id))?;
            if pack.manifest.id.as_str() == INPUT_ID {
                println!("input actual import names: {imports:?}");
                super::deepthink_ledger_tests::check_input_type_dependency_imports(
                    engine.engine(),
                    &component,
                    pack,
                    &shapes,
                )?;
                assert!(imports.iter().any(|name| name == "tobkiri:core/flow@1.0.0"));
            }
            if pack.manifest.id.as_str() == READER_ID {
                for interface in ["network", "secrets"] {
                    let name = format!("tobkiri:core/{interface}@1.0.0");
                    assert!(imports.contains(&name), "real Reader imports {name}");
                    let mut missing_authority = inspection.allowed_imports().clone();
                    missing_authority
                        .retain(|name| !name.starts_with(&format!("tobkiri:core/{interface}@")));
                    assert!(
                        crate::validate_imports(engine.engine(), &component, &missing_authority)
                            .is_err(),
                        "Reader cannot be admitted without {interface}",
                    );
                }
            }
            validate_abi(engine.engine(), &component, pack, &shapes)
                .with_context(|| format!("ABI {}", pack.manifest.id))?;
            assert!(
                crate::validate_imports(engine.engine(), &component, &Default::default()).is_err()
            );
            println!("verified component ABI {} {}", pack.manifest.id, pack.hash);
            wasm_count += 1;
        }
        assert_eq!(
            wasm_count,
            expected_artifacts()
                .values()
                .map(|(_, count)| count)
                .sum::<usize>(),
            "selected Wasm artifact inventory"
        );
        for (name, profile, _) in profiles {
            let deepthink = profile
                .packs
                .iter()
                .any(|pin| pin.id.as_str() == "local.deepthink");
            let input_pins = profile
                .packs
                .iter()
                .filter(|pin| pin.id.as_str() == INPUT_ID)
                .collect::<Vec<_>>();
            assert_eq!(
                input_pins.len(),
                usize::from(deepthink),
                "input adapter pin in {name}"
            );
            assert_eq!(
                profile
                    .bindings
                    .get("local.deepthink.continuation-input@1")
                    .map(|id| id.as_str()),
                deepthink.then_some(INPUT_ID),
                "whole input binding in {name}"
            );
            assert_eq!(
                profile
                    .bindings
                    .get("local.deepthink.input-context@1")
                    .map(|id| id.as_str()),
                deepthink.then_some("local.deepthink"),
                "worker preparation binding in {name}"
            );
            if deepthink {
                assert_eq!(
                    input_pins[0].sha256, input.hash,
                    "exact input adapter pin in {name}"
                );
            }
            let reader_pins = profile
                .packs
                .iter()
                .filter(|pin| pin.id.as_str() == READER_ID)
                .collect::<Vec<_>>();
            assert_eq!(
                reader_pins.len(),
                usize::from(deepthink),
                "Reader pin in {name}"
            );
            let binding = profile
                .bindings
                .get("local.openrouter-generation-reader.generation-reader@1")
                .map(|id| id.as_str());
            assert_eq!(
                binding,
                deepthink.then_some(READER_ID),
                "Reader binding in {name}"
            );
            if deepthink {
                assert_eq!(
                    reader_pins[0].sha256, reader.hash,
                    "exact Reader pin in {name}"
                );
            }
            let selected = profile
                .packs
                .iter()
                .map(|pin| {
                    packs
                        .iter()
                        .find(|pack| pack.manifest.id == pin.id && pack.hash == pin.sha256)
                        .cloned()
                        .with_context(|| format!("verified Profile pin {}", pin.id))
                })
                .collect::<Result<Vec<_>>>()?;
            let compiled = tobkiri_profile::compile(&profile, &selected)
                .map_err(|conflicts| anyhow::anyhow!("Profile {name}: {conflicts:?}"))?;
            println!(
                "verified Profile {name}: {} Packs, {} Flows, {} views, {} overlay orders, {}",
                selected.len(),
                compiled.flows.len(),
                compiled.views.len(),
                profile.overlay_order.len(),
                compiled.plan_digest,
            );
            let mut tampered = profile.clone();
            tampered.packs[0].sha256 = tobkiri_canon::Digest::hash(b"wrong-artifact").into();
            assert!(tobkiri_profile::compile(&tampered, &selected).is_err());
        }
        Ok(())
    }
}
