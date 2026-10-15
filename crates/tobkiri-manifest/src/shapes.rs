use crate::*;
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use std::collections::{BTreeMap, BTreeSet};
use tobkiri_canon::{CanonValue, MAX_SAFE_INTEGER, Variant};
use wit_parser::{Resolve, Type, TypeDefKind, TypeOwner};

fn invalid(message: impl Into<String>) -> ManifestError {
    ManifestError::Invalid(message.into())
}

/// A nominal declaration belongs to an actual import or export binding.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum WitBindingDirection {
    Import,
    Export,
}

/// Frozen evidence extracted from this exact verified runtime artifact. The
/// absence of a nominal proof never changes structural ABI or effect rights.
#[derive(Debug)]
pub struct PackWitInspection {
    allowed_imports: BTreeSet<String>,
    core_errors: PackCoreErrorBindings,
}
#[derive(Debug)]
pub struct PackCoreErrorBindings {
    hash: PackHash,
    core_error_bindings: BTreeMap<WitBindingDirection, BTreeMap<ShapeRef, BTreeSet<OpName>>>,
}
impl PackWitInspection {
    pub fn allowed_imports(&self) -> &BTreeSet<String> {
        &self.allowed_imports
    }
    pub fn core_errors(&self) -> &PackCoreErrorBindings {
        &self.core_errors
    }
    pub fn into_core_errors(self) -> PackCoreErrorBindings {
        self.core_errors
    }
}
impl PackCoreErrorBindings {
    pub fn hash(&self) -> &PackHash {
        &self.hash
    }
    pub fn has_core_error_result(
        &self,
        direction: WitBindingDirection,
        view: &ShapeRef,
        op: &OpName,
    ) -> bool {
        self.core_error_bindings
            .get(&direction)
            .and_then(|views| views.get(view))
            .is_some_and(|operations| operations.contains(op))
    }
}

fn resolved_core_error_result(resolve: &Resolve, result: Option<Type>) -> bool {
    fn unalias(resolve: &Resolve, mut ty: Type) -> Option<Type> {
        for _ in 0..=64 {
            let Type::Id(id) = ty else {
                return Some(ty);
            };
            match &resolve.types[id].kind {
                TypeDefKind::Type(next) => ty = *next,
                _ => return Some(ty),
            }
        }
        None
    }
    let Some(Type::Id(id)) = result.and_then(|ty| unalias(resolve, ty)) else {
        return false;
    };
    let TypeDefKind::Result(result) = &resolve.types[id].kind else {
        return false;
    };
    let Some(Type::Id(id)) = result.err.and_then(|ty| unalias(resolve, ty)) else {
        return false;
    };
    let definition = &resolve.types[id];
    let TypeOwner::Interface(owner) = definition.owner else {
        return false;
    };
    let interface = &resolve.interfaces[owner];
    definition.name.as_deref() == Some("error")
        && interface.name.as_deref() == Some("types")
        && interface.package.is_some_and(|package| {
            matches!(
                resolve.packages[package].name.to_string().as_str(),
                "tobkiri:core@1.0.0" | "tobkiri:core@1.1.0"
            )
        })
}
fn resolve_known_core() -> Result<Resolve, ManifestError> {
    let (mut resolve, _) = tobkiri_wit::resolve_core().map_err(|e| invalid(e.to_string()))?;
    let (newer, _) =
        tobkiri_wit::resolve_core_version("1.1.0").map_err(|e| invalid(e.to_string()))?;
    resolve.merge(newer).map_err(|e| invalid(e.to_string()))?;
    Ok(resolve)
}
/// The only forbidden mapping is onto the closed core WIT package.
pub fn wit_interface_name(reference: &ShapeRef) -> Result<String, ManifestError> {
    let owner = reference.name.owner();
    if owner.publisher() == "tobkiri" && owner.name() == "core" {
        return Err(invalid(
            "Pack-defined shape maps onto reserved WIT package tobkiri:core",
        ));
    }
    Ok(format!(
        "{}:{}/{}@{}.{}.0",
        owner.publisher(),
        owner.name(),
        reference.name.interface(),
        reference.version.major,
        reference.version.minor
    ))
}
/// Import names allowed by a declaration; type dependencies add no effect authority.
pub fn allowed_imports(manifest: &PackManifest) -> Result<BTreeSet<String>, ManifestError> {
    let mut interfaces: BTreeSet<&str> = tobkiri_wit::MINIMAL_IMPORTS.iter().copied().collect();
    let p = &manifest.permissions;
    if !p.network.origins.is_empty() || p.network.user_origins.is_some() {
        interfaces.extend(["network", "secrets"]);
    }
    if !p.secrets.is_empty() {
        interfaces.insert("secrets");
    }
    if !p.fs.is_empty() || !p.proc.is_empty() || p.pty {
        interfaces.insert("fs");
    }
    if !p.proc.is_empty() {
        interfaces.insert("proc");
    }
    if p.pty {
        interfaces.insert("pty");
    }
    if !manifest.stores.is_empty()
        || p.storage.private
        || p.storage.public_read
        || p.storage.public_write
        || p.cross_profile_storage
    {
        interfaces.insert("storage");
    }
    if !p.host_io.is_empty() {
        interfaces.insert("host-io");
    }
    if p.mediator {
        interfaces.insert("approvals");
    }
    let mut result: BTreeSet<_> = interfaces
        .into_iter()
        .flat_map(|i| {
            [
                format!("tobkiri:core/{i}@1.0.0"),
                format!("tobkiri:core/{i}@1.1.0"),
            ]
        })
        .collect();
    for port in &manifest.ports.consumes {
        result.insert(wit_interface_name(&ShapeRef::new(
            port.shape.clone(),
            port.version,
        ))?);
    }
    Ok(result)
}
fn convert(
    resolve: &Resolve,
    ty: Type,
    stack: &mut BTreeSet<usize>,
) -> Result<WitType, ManifestError> {
    Ok(match ty {
        Type::Bool => WitType::Bool,
        Type::U8 => WitType::U8,
        Type::U16 => WitType::U16,
        Type::U32 => WitType::U32,
        Type::U64 => WitType::U64,
        Type::S8 => WitType::S8,
        Type::S16 => WitType::S16,
        Type::S32 => WitType::S32,
        Type::S64 => WitType::S64,
        Type::F32 => WitType::F32,
        Type::F64 => WitType::F64,
        Type::Char => WitType::Char,
        Type::String => WitType::String,
        Type::ErrorContext => return Err(invalid("error-context is not a v1 shape type")),
        Type::Id(id) => {
            if !stack.insert(id.index()) {
                return Err(invalid("recursive WIT shape type"));
            }
            let definition = &resolve.types[id];
            if let TypeOwner::Interface(interface) = definition.owner {
                let interface = &resolve.interfaces[interface];
                if interface.name.as_deref() == Some("types")
                    && interface.package.is_some_and(|p| {
                        resolve.packages[p].name.namespace == "tobkiri"
                            && resolve.packages[p].name.name == "core"
                            && resolve.packages[p].name.version.as_ref().is_some_and(|v| {
                                v.major == 1
                                    && matches!(v.minor, 0 | 1)
                                    && v.patch == 0
                                    && v.pre.is_empty()
                                    && v.build.is_empty()
                            })
                    })
                {
                    let core = match definition.name.as_deref() {
                        Some("json") => Some(WitType::Json),
                        Some("stream-handle") => Some(WitType::StreamHandle),
                        Some("callback-handle") => Some(WitType::CallbackHandle),
                        _ => None,
                    };
                    if let Some(core) = core {
                        stack.remove(&id.index());
                        return Ok(core);
                    }
                }
            }
            let converted = match &definition.kind {
                TypeDefKind::Type(ty) => convert(resolve, *ty, stack)?,
                TypeDefKind::List(ty) => WitType::List(Box::new(convert(resolve, *ty, stack)?)),
                TypeDefKind::Option(ty) => WitType::Option(Box::new(convert(resolve, *ty, stack)?)),
                TypeDefKind::Tuple(tuple) => WitType::Tuple(
                    tuple
                        .types
                        .iter()
                        .map(|t| convert(resolve, *t, stack))
                        .collect::<Result<_, _>>()?,
                ),
                TypeDefKind::Record(record) => WitType::Record(
                    record
                        .fields
                        .iter()
                        .map(|f| {
                            Ok(WitField {
                                name: f.name.clone(),
                                ty: convert(resolve, f.ty, stack)?,
                            })
                        })
                        .collect::<Result<_, ManifestError>>()?,
                ),
                TypeDefKind::Variant(variant) => WitType::Variant(
                    variant
                        .cases
                        .iter()
                        .map(|c| {
                            Ok(WitCase {
                                name: c.name.clone(),
                                ty: c.ty.map(|t| convert(resolve, t, stack)).transpose()?,
                            })
                        })
                        .collect::<Result<_, ManifestError>>()?,
                ),
                TypeDefKind::Enum(enumeration) => {
                    WitType::Enum(enumeration.cases.iter().map(|c| c.name.clone()).collect())
                }
                TypeDefKind::Flags(flags) => {
                    WitType::Flags(flags.flags.iter().map(|f| f.name.clone()).collect())
                }
                TypeDefKind::Result(result) => WitType::Result {
                    ok: result
                        .ok
                        .map(|t| convert(resolve, t, stack).map(Box::new))
                        .transpose()?,
                    err: result
                        .err
                        .map(|t| convert(resolve, t, stack).map(Box::new))
                        .transpose()?,
                },
                TypeDefKind::Resource | TypeDefKind::Handle(_) => {
                    return Err(invalid("resources are forbidden in Pack shapes"));
                }
                _ => return Err(invalid("unsupported v1 WIT shape type")),
            };
            stack.remove(&id.index());
            converted
        }
    })
}
/// Include only type-only interfaces reached from declared port pins in the
/// hash-verified WIT files. Namespace membership never grants an import.
pub fn allowed_imports_for_pack(pack: &VerifiedPack) -> Result<BTreeSet<String>, ManifestError> {
    Ok(inspect_pack_wit_impl(pack, None)?.allowed_imports)
}

/// Inspect one immutable runtime artifact, keeping import/export evidence
/// separate from the Profile's unique canonical ShapeDef catalog.
pub fn inspect_pack_wit(
    pack: &VerifiedPack,
    views: &BTreeMap<ShapeRef, ShapeDef>,
) -> Result<PackWitInspection, ManifestError> {
    inspect_pack_wit_impl(pack, Some(views))
}
fn resolve_pack_wit(pack: &VerifiedPack) -> Result<Resolve, ManifestError> {
    let mut documents = 0usize;
    let mut input_bytes = 0usize;
    for (_, bytes) in pack.files.iter().filter(|(path, _)| path.ends_with(".wit")) {
        documents += 1;
        input_bytes = input_bytes
            .checked_add(bytes.len())
            .ok_or_else(|| invalid("verified WIT input size overflow"))?;
    }
    if documents > 32 || input_bytes > 4 * 1024 * 1024 {
        return Err(invalid("bounded verified WIT dependency input exceeded"));
    }
    let mut resolve = resolve_known_core()?;
    let mut package_documents: BTreeMap<
        wit_parser::PackageName,
        Vec<wit_parser::UnresolvedPackageGroup>,
    > = BTreeMap::new();
    let mut package_count = 0usize;
    for (path, bytes) in pack.files.iter().filter(|(path, _)| path.ends_with(".wit")) {
        let text = std::str::from_utf8(bytes).map_err(|_| invalid("WIT is not UTF-8"))?;
        if [
            tobkiri_wit::CORE_WIT_SOURCE,
            tobkiri_wit::CORE_V1_1_WIT_SOURCE,
        ]
        .iter()
        .any(|source| text.trim() == source.trim())
        {
            continue;
        }
        let group = crate::wit_preflight::parse_guest_wit(path, text)?;
        if std::iter::once(&group.main)
            .chain(&group.nested)
            .any(|p| p.name.namespace == "tobkiri" && p.name.name == "core")
        {
            return Err(invalid("Pack WIT cannot redefine the closed core package"));
        }
        package_count = package_count
            .checked_add(1 + group.nested.len())
            .ok_or_else(|| invalid("verified WIT package count overflow"))?;
        if package_count > 128 {
            return Err(invalid("bounded verified WIT package count exceeded"));
        }
        package_documents
            .entry(group.main.name.clone())
            .or_default()
            .push(group);
    }
    let mut groups = Vec::new();
    for mut documents in package_documents.into_values() {
        let group = if documents.len() == 1 {
            documents.pop().expect("one admitted WIT document")
        } else {
            // Multiple verified files may contribute distinct interfaces to
            // one package. Use the parser's file-union semantics (including
            // file-local `use` namespaces), rather than duplicate packages or
            // a structural merge of independently resolved definitions.
            let mut sources = wit_parser::SourceMap::default();
            for document in documents {
                // These exact bytes already passed the allocation-free syntax
                // preflight above. Each is reparsed at most once when its
                // package spans files; no retry/partial-graph cloning occurs.
                sources.append(document.source_map);
            }
            sources
                .parse()
                .map_err(|(sources, error)| invalid(error.highlight(&sources)))?
        };
        groups.push(group);
    }
    // The pinned parser sorts the bounded group set itself. Do not repeatedly
    // clone partially resolved graphs while guessing dependency order.
    if let Some(main) = groups.pop() {
        resolve
            .push_groups(main, groups)
            .map_err(|e| invalid(format!("unresolved verified WIT dependency: {e}")))?;
    }

    Ok(resolve)
}
fn inspect_pack_wit_impl(
    pack: &VerifiedPack,
    views: Option<&BTreeMap<ShapeRef, ShapeDef>>,
) -> Result<PackWitInspection, ManifestError> {
    let mut allowed = allowed_imports(&pack.manifest)?;
    let resolve = resolve_pack_wit(pack)?;
    let mut interfaces = BTreeMap::new();
    for (id, iface) in resolve.interfaces.iter() {
        if let (Some(package), Some(name)) = (iface.package, &iface.name) {
            let p = &resolve.packages[package];
            let version = p
                .name
                .version
                .as_ref()
                .ok_or_else(|| invalid("WIT package needs a version"))?;
            interfaces.insert(
                format!("{}:{}/{}@{}", p.name.namespace, p.name.name, name, version),
                id,
            );
        }
    }
    let seeds: Vec<_> = pack
        .manifest
        .ports
        .consumes
        .iter()
        .chain(&pack.manifest.ports.provides)
        .map(|p| wit_interface_name(&ShapeRef::new(p.shape.clone(), p.version)))
        .collect::<Result<_, _>>()?;
    let mut visited = BTreeSet::new();
    let mut dependencies = BTreeSet::new();
    fn walk(
        resolve: &Resolve,
        ty: Type,
        visited: &mut BTreeSet<usize>,
        deps: &mut BTreeSet<wit_parser::InterfaceId>,
    ) {
        if let Type::Id(id) = ty {
            if !visited.insert(id.index()) {
                return;
            }
            let d = &resolve.types[id];
            if let TypeOwner::Interface(owner) = d.owner {
                deps.insert(owner);
            }
            match &d.kind {
                TypeDefKind::Type(t) | TypeDefKind::List(t) | TypeDefKind::Option(t) => {
                    walk(resolve, *t, visited, deps)
                }
                TypeDefKind::Record(r) => {
                    for f in &r.fields {
                        walk(resolve, f.ty, visited, deps)
                    }
                }
                TypeDefKind::Tuple(t) => {
                    for ty in &t.types {
                        walk(resolve, *ty, visited, deps)
                    }
                }
                TypeDefKind::Variant(v) => {
                    for c in &v.cases {
                        if let Some(t) = c.ty {
                            walk(resolve, t, &mut *visited, deps)
                        }
                    }
                }
                TypeDefKind::Result(r) => {
                    for t in r.ok.iter().chain(&r.err) {
                        walk(resolve, *t, visited, deps)
                    }
                }
                _ => (),
            }
        }
    }
    let mut seeded = BTreeSet::new();
    let mut traversal = TypeExpansion::new();
    for name in seeds {
        let id = *interfaces
            .get(&name)
            .ok_or_else(|| invalid(format!("declared port has no verified WIT: {name}")))?;
        seeded.insert(id);
        let iface = &resolve.interfaces[id];
        for f in iface.functions.values() {
            for p in &f.params {
                traversal.check(&resolve, p.ty, 0)?;
                walk(&resolve, p.ty, &mut visited, &mut dependencies);
            }
            if let Some(t) = f.result {
                traversal.check(&resolve, t, 0)?;
                walk(&resolve, t, &mut visited, &mut dependencies)
            }
        }
        for id in iface.types.values() {
            traversal.check(&resolve, Type::Id(*id), 0)?;
            walk(&resolve, Type::Id(*id), &mut visited, &mut dependencies);
        }
    }
    let mut expansion = TypeExpansion::new();
    for id in dependencies {
        let (name, _) = interfaces
            .iter()
            .find(|(_, i)| **i == id)
            .ok_or_else(|| invalid("type interface lacks qualified identity"))?;
        if allowed.contains(name) || seeded.contains(&id) {
            continue;
        }
        let iface = &resolve.interfaces[id];
        if !iface.functions.is_empty() {
            return Err(invalid(
                "undeclared type dependency interface has operations",
            ));
        }
        for ty in iface.types.values() {
            expansion.convert(&resolve, Type::Id(*ty))?;
        }
        allowed.insert(name.clone());
    }
    let mut core_error_bindings = BTreeMap::new();
    if let Some(views) = views {
        for (direction, ports) in [
            (WitBindingDirection::Import, &pack.manifest.ports.consumes),
            (WitBindingDirection::Export, &pack.manifest.ports.provides),
        ] {
            for port in ports {
                let reference = ShapeRef::new(port.shape.clone(), port.version);
                let Some(expected) = views.get(&reference) else {
                    continue;
                };
                let Some(interface) = interfaces.get(&wit_interface_name(&reference)?) else {
                    continue;
                };
                let interface = &resolve.interfaces[*interface];
                let mut operations = BTreeSet::new();
                for op in expected.functions.keys() {
                    if interface
                        .functions
                        .get(op.as_str())
                        .is_some_and(|function| {
                            function.kind == wit_parser::FunctionKind::Freestanding
                                && resolved_core_error_result(&resolve, function.result)
                        })
                    {
                        operations.insert(op.clone());
                    }
                }
                if !operations.is_empty() {
                    core_error_bindings
                        .entry(direction)
                        .or_insert_with(BTreeMap::new)
                        .insert(reference, operations);
                }
            }
        }
    }
    Ok(PackWitInspection {
        allowed_imports: allowed,
        core_errors: PackCoreErrorBindings {
            hash: pack.hash.clone(),
            core_error_bindings,
        },
    })
}
/// Resolve declared interfaces with authoritative wit-parser and verified sidecars.
pub fn load_shapes(
    manifest: &PackManifest,
    files: &BTreeMap<String, Vec<u8>>,
) -> Result<Vec<ShapeDef>, ManifestError> {
    let mut cache: BTreeMap<String, (Resolve, wit_parser::PackageId)> = BTreeMap::new();
    let mut result = Vec::new();
    let mut expansion = TypeExpansion::new();
    for definition in &manifest.shapes {
        if !cache.contains_key(&definition.wit) {
            let mut resolve = resolve_known_core()?;
            let wit = std::str::from_utf8(
                files
                    .get(&definition.wit)
                    .ok_or_else(|| invalid("missing WIT"))?,
            )
            .map_err(|_| invalid("WIT is not UTF-8"))?;
            let group = crate::wit_preflight::parse_guest_wit(&definition.wit, wit)?;
            if std::iter::once(&group.main)
                .chain(&group.nested)
                .any(|p| p.name.namespace == "tobkiri" && p.name.name == "core")
            {
                return Err(invalid("Pack WIT cannot redefine the closed core package"));
            }
            let package = resolve
                .push_group(group)
                .map_err(|e| invalid(e.to_string()))?;
            cache.insert(definition.wit.clone(), (resolve, package));
        }
        let (resolve, package_id) = &cache[&definition.wit];
        let package = &resolve.packages[*package_id];
        let owner = definition.name.owner();
        let version = package
            .name
            .version
            .as_ref()
            .ok_or_else(|| invalid("WIT package needs version"))?;
        if package.name.namespace != owner.publisher()
            || package.name.name != owner.name()
            || version.major != u64::from(definition.version.major)
            || version.minor != u64::from(definition.version.minor)
            || version.patch != 0
            || !version.pre.is_empty()
            || !version.build.is_empty()
        {
            return Err(invalid("WIT package name/version does not match shape"));
        }
        let interface = &resolve.interfaces[*package
            .interfaces
            .get(&definition.name.interface())
            .ok_or_else(|| invalid("declared WIT interface absent"))?];
        let sidecar_path = format!("wit/{}.shape.json", definition.name.interface());
        let value = tobkiri_canon::parse(
            files
                .get(&sidecar_path)
                .ok_or_else(|| invalid("missing shape sidecar"))?,
            Variant::StrictV1,
        )?;
        let sidecar: ShapeSidecar =
            serde_json::from_slice(&tobkiri_canon::encode(&value, Variant::StrictV1)?)?;
        let mut functions = BTreeMap::new();
        for (name, function) in &interface.functions {
            if function.kind != wit_parser::FunctionKind::Freestanding {
                return Err(invalid("resource methods forbidden in shapes"));
            }
            let params = function
                .params
                .iter()
                .map(|p| Ok((p.name.clone(), expansion.convert(resolve, p.ty)?)))
                .collect::<Result<_, ManifestError>>()?;
            functions.insert(
                OpName::parse(name)?,
                WitFunction {
                    params,
                    result: function
                        .result
                        .map(|t| expansion.convert(resolve, t))
                        .transpose()?,
                },
            );
        }
        let mut types = BTreeMap::new();
        for (name, id) in &interface.types {
            types.insert(name.clone(), expansion.convert(resolve, Type::Id(*id))?);
        }
        for type_name in sidecar.streams.values().chain(sidecar.callbacks.values()) {
            for id in package.interfaces.values() {
                if let Some(type_id) = resolve.interfaces[*id].types.get(type_name) {
                    let ty = expansion.convert(resolve, Type::Id(*type_id))?;
                    if types.get(type_name).is_some_and(|known| known != &ty) {
                        return Err(invalid("ambiguous stream/callback event type"));
                    }
                    types.insert(type_name.clone(), ty);
                }
            }
            if !types.contains_key(type_name) {
                return Err(invalid("stream/callback refers to unknown WIT type"));
            }
        }
        for operation in sidecar.streams.keys().chain(sidecar.callbacks.keys()) {
            if !functions.contains_key(&OpName::parse(operation)?) {
                return Err(invalid("stream/callback operation missing"));
            }
        }
        result.push(ShapeDef {
            reference: ShapeRef::new(definition.name.clone(), definition.version),
            kind: definition.kind,
            cardinality: sidecar.cardinality,
            functions,
            types,
            streams: sidecar.streams,
            callbacks: sidecar.callbacks,
            wit: String::from_utf8(files[&definition.wit].clone())
                .map_err(|_| invalid("WIT is not UTF-8"))?,
        });
    }
    Ok(result)
}

/// Operations whose result error aliases the exact committed Core error type.
/// This is provenance metadata, not structural compatibility or authority.
/// Resolve the verified document against the closed Core definitions so a
/// Pack-local record with the same fields cannot opt into public error codes.
pub fn core_error_result_operations(shape: &ShapeDef) -> Result<BTreeSet<OpName>, ManifestError> {
    if shape.wit.len() > 1024 * 1024 || shape.functions.len() > 16_384 {
        return Err(invalid("bounded error provenance document exceeded"));
    }
    let mut resolve = resolve_known_core()?;
    let group = crate::wit_preflight::parse_guest_wit("verified-shape.wit", &shape.wit)?;
    if std::iter::once(&group.main)
        .chain(&group.nested)
        .any(|p| p.name.namespace == "tobkiri" && p.name.name == "core")
    {
        return Err(invalid("Pack WIT cannot redefine the closed core package"));
    }
    let package_id = resolve
        .push_group(group)
        .map_err(|e| invalid(e.to_string()))?;
    let package = &resolve.packages[package_id];
    let expected = wit_interface_name(&shape.reference)?;
    let actual = format!(
        "{}:{}/{}@{}",
        package.name.namespace,
        package.name.name,
        shape.reference.name.interface(),
        package
            .name
            .version
            .as_ref()
            .ok_or_else(|| invalid("WIT package needs version"))?,
    );
    if actual != expected {
        return Err(invalid("error provenance WIT differs from exact shape"));
    }
    let interface = &resolve.interfaces[*package
        .interfaces
        .get(&shape.reference.name.interface())
        .ok_or_else(|| invalid("declared WIT interface absent"))?];

    // Follow only aliases. In particular, an equivalent record/enum is never
    // the same declared type, including one reusing Core's error-code enum.
    fn unalias(resolve: &Resolve, mut ty: Type) -> Type {
        while let Type::Id(id) = ty {
            let TypeDefKind::Type(next) = &resolve.types[id].kind else {
                break;
            };
            ty = *next;
        }
        ty
    }
    let core_errors: BTreeSet<_> = resolve
        .packages
        .iter()
        .filter(|(_, p)| {
            matches!(
                p.name.to_string().as_str(),
                "tobkiri:core@1.0.0" | "tobkiri:core@1.1.0"
            )
        })
        .map(|(_, package)| {
            let interface = &resolve.interfaces[package.interfaces["types"]];
            interface.types["error"]
        })
        .collect();
    let mut operations = BTreeSet::new();
    let mut expansion = TypeExpansion::new();
    for (name, function) in &interface.functions {
        let op = OpName::parse(name)?;
        let declared = shape
            .functions
            .get(&op)
            .ok_or_else(|| invalid("error provenance operation differs from exact shape"))?;
        if function.kind != wit_parser::FunctionKind::Freestanding
            || function
                .result
                .map(|ty| expansion.convert(&resolve, ty))
                .transpose()?
                != declared.result
        {
            return Err(invalid("error provenance result differs from exact shape"));
        }
        let Some(Type::Id(result)) = function.result.map(|ty| unalias(&resolve, ty)) else {
            continue;
        };
        let TypeDefKind::Result(result) = &resolve.types[result].kind else {
            continue;
        };
        if let Some(Type::Id(error)) = result.err.map(|ty| unalias(&resolve, ty))
            && core_errors.contains(&error)
        {
            operations.insert(op);
        }
    }
    if interface.functions.len() != shape.functions.len() {
        return Err(invalid(
            "error provenance operations differ from exact shape",
        ));
    }
    Ok(operations)
}

/// Count each physical expansion of an alias, including repeated references.
/// Every independent conversion starts with this bounded prewalk. One budget
/// spans a complete shape-load/provenance/import-validation operation, so
/// repeated parameters, aliases and exposed type copies cannot reset it.
struct TypeExpansion {
    nodes: usize,
    work: usize,
}
impl TypeExpansion {
    fn new() -> Self {
        Self {
            nodes: 65_536,
            work: 4 * 1024 * 1024,
        }
    }
    fn convert(&mut self, resolve: &Resolve, ty: Type) -> Result<WitType, ManifestError> {
        self.check(resolve, ty, 0)?;
        convert(resolve, ty, &mut BTreeSet::new())
    }
    fn charge(&mut self, bytes: usize) -> Result<(), ManifestError> {
        self.nodes = self
            .nodes
            .checked_sub(1)
            .ok_or_else(|| invalid("bounded WIT type expansion exceeded"))?;
        self.work = self
            .work
            .checked_sub(bytes.max(1))
            .ok_or_else(|| invalid("bounded WIT type work exceeded"))?;
        Ok(())
    }
    fn check(&mut self, resolve: &Resolve, ty: Type, depth: usize) -> Result<(), ManifestError> {
        if depth > 64 {
            return Err(invalid("bounded WIT type depth exceeded"));
        }
        self.charge(1)?;
        let Type::Id(id) = ty else {
            return Ok(());
        };
        match &resolve.types[id].kind {
            TypeDefKind::Type(ty) | TypeDefKind::List(ty) | TypeDefKind::Option(ty) => {
                self.check(resolve, *ty, depth + 1)?;
            }
            TypeDefKind::Record(record) => {
                for field in &record.fields {
                    self.charge(field.name.len())?;
                    self.check(resolve, field.ty, depth + 1)?;
                }
            }
            TypeDefKind::Tuple(tuple) => {
                for ty in &tuple.types {
                    self.check(resolve, *ty, depth + 1)?;
                }
            }
            TypeDefKind::Variant(variant) => {
                for case in &variant.cases {
                    self.charge(case.name.len())?;
                    if let Some(ty) = case.ty {
                        self.check(resolve, ty, depth + 1)?;
                    }
                }
            }
            TypeDefKind::Enum(enumeration) => {
                for case in &enumeration.cases {
                    self.charge(case.name.len())?;
                }
            }
            TypeDefKind::Flags(flags) => {
                for flag in &flags.flags {
                    self.charge(flag.name.len())?;
                }
            }
            TypeDefKind::Result(result) => {
                for ty in result.ok.iter().chain(&result.err) {
                    self.check(resolve, *ty, depth + 1)?;
                }
            }
            _ => return Err(invalid("unsupported WIT shape type")),
        }
        Ok(())
    }
}

/// Exact declared-width float mapping for NEW StrictV1 port values.
pub fn encode_f32(value: f32) -> Result<CanonValue, ManifestError> {
    if !value.is_finite() {
        return Err(invalid("nonfinite f32"));
    }
    Ok(CanonValue::Str(format!("{value:e}")))
}
pub fn encode_f64(value: f64) -> Result<CanonValue, ManifestError> {
    if !value.is_finite() {
        return Err(invalid("nonfinite f64"));
    }
    Ok(CanonValue::Str(format!("{value:e}")))
}
pub fn parse_f32(value: &CanonValue) -> Result<f32, ManifestError> {
    let text = value
        .as_str()
        .ok_or_else(|| invalid("f32 must be a canonical string"))?;
    let result: f32 = text.parse().map_err(|_| invalid("invalid f32"))?;
    if encode_f32(result)? != *value {
        return Err(invalid("noncanonical f32 spelling"));
    }
    Ok(result)
}
pub fn parse_f64(value: &CanonValue) -> Result<f64, ManifestError> {
    let text = value
        .as_str()
        .ok_or_else(|| invalid("f64 must be a canonical string"))?;
    let result: f64 = text.parse().map_err(|_| invalid("invalid f64"))?;
    if encode_f64(result)? != *value {
        return Err(invalid("noncanonical f64 spelling"));
    }
    Ok(result)
}
fn integer(value: &CanonValue, min: i128, max: i128, wide: bool) -> Result<(), ManifestError> {
    let (number, string) = match value {
        CanonValue::Int(n) => (i128::from(*n), false),
        CanonValue::Str(text) if wide => {
            let n: i128 = text
                .parse()
                .map_err(|_| invalid("invalid decimal integer"))?;
            if n.to_string() != *text {
                return Err(invalid("noncanonical decimal integer"));
            }
            (n, true)
        }
        _ => return Err(invalid("integer pin requires exact integer")),
    };
    if number < min
        || number > max
        || string != (number.unsigned_abs() > u128::from(MAX_SAFE_INTEGER))
    {
        return Err(invalid(
            "integer pin is out of range or incorrectly represented",
        ));
    }
    Ok(())
}
/// WIT validation is authoritative; generated JSON Schema is only editor metadata.
pub fn validate_json(ty: &WitType, value: &CanonValue) -> Result<(), ManifestError> {
    match ty {
        WitType::Bool if matches!(value, CanonValue::Bool(_)) => Ok(()),
        WitType::String if matches!(value, CanonValue::Str(_)) => Ok(()),
        WitType::Char if value.as_str().is_some_and(|s| s.chars().count() == 1) => Ok(()),
        WitType::U8 => integer(value, 0, u8::MAX as i128, false),
        WitType::U16 => integer(value, 0, u16::MAX as i128, false),
        WitType::U32 => integer(value, 0, u32::MAX as i128, false),
        WitType::U64 => integer(value, 0, u64::MAX as i128, true),
        WitType::S8 => integer(value, i8::MIN as i128, i8::MAX as i128, false),
        WitType::S16 => integer(value, i16::MIN as i128, i16::MAX as i128, false),
        WitType::S32 => integer(value, i32::MIN as i128, i32::MAX as i128, false),
        WitType::S64 => integer(value, i64::MIN as i128, i64::MAX as i128, true),
        WitType::F32 => parse_f32(value).map(|_| ()),
        WitType::F64 => parse_f64(value).map(|_| ()),
        WitType::Json => tobkiri_canon::encode(value, Variant::StrictV1)
            .map(|_| ())
            .map_err(Into::into),
        WitType::Option(inner) if !matches!(value, CanonValue::Null) => validate_json(inner, value),
        WitType::Option(_) => Ok(()),
        WitType::List(inner) if **inner == WitType::U8 => {
            let text = value
                .as_str()
                .ok_or_else(|| invalid("list<u8> requires base64url"))?;
            let bytes = URL_SAFE_NO_PAD
                .decode(text)
                .map_err(|_| invalid("invalid byte list"))?;
            if URL_SAFE_NO_PAD.encode(bytes) != text {
                return Err(invalid("noncanonical byte list"));
            }
            Ok(())
        }
        WitType::List(inner) => {
            for item in value
                .as_array()
                .ok_or_else(|| invalid("list requires array"))?
            {
                validate_json(inner, item)?;
            }
            Ok(())
        }
        WitType::Tuple(types) => {
            let items = value
                .as_array()
                .ok_or_else(|| invalid("tuple requires array"))?;
            if items.len() != types.len() {
                return Err(invalid("tuple length"));
            }
            for (ty, v) in types.iter().zip(items) {
                validate_json(ty, v)?;
            }
            Ok(())
        }
        WitType::Record(fields) => {
            let object = value
                .as_object()
                .ok_or_else(|| invalid("record requires object"))?;
            if object
                .keys()
                .any(|key| !fields.iter().any(|f| &f.name == key))
            {
                return Err(invalid("unknown record field"));
            }
            for field in fields {
                if let Some(value) = object.get(&field.name) {
                    validate_json(&field.ty, value)?;
                } else if !matches!(field.ty, WitType::Option(_)) {
                    return Err(invalid("missing record field"));
                }
            }
            Ok(())
        }
        WitType::Variant(cases) => variant(cases, value),
        WitType::Result { ok, err } => variant(
            &[
                WitCase {
                    name: "ok".into(),
                    ty: ok.as_deref().cloned(),
                },
                WitCase {
                    name: "err".into(),
                    ty: err.as_deref().cloned(),
                },
            ],
            value,
        ),
        WitType::Enum(cases) if value.as_str().is_some_and(|s| cases.iter().any(|c| c == s)) => {
            Ok(())
        }
        WitType::Flags(cases) => {
            let mut seen = BTreeSet::new();
            for v in value
                .as_array()
                .ok_or_else(|| invalid("flags requires array"))?
            {
                let s = v.as_str().ok_or_else(|| invalid("flag requires string"))?;
                if !cases.iter().any(|c| c == s) || !seen.insert(s) {
                    return Err(invalid("unknown or duplicate flag"));
                }
            }
            Ok(())
        }
        WitType::StreamHandle | WitType::CallbackHandle => {
            let obj = value
                .as_object()
                .ok_or_else(|| invalid("handle requires core-owned id record"))?;
            if obj.len() != 1 {
                return Err(invalid("handle fields"));
            }
            integer(
                obj.get("id").ok_or_else(|| invalid("handle id missing"))?,
                0,
                u64::MAX as i128,
                true,
            )
        }
        _ => Err(invalid("JSON value does not match WIT pin")),
    }
}
fn variant(cases: &[WitCase], value: &CanonValue) -> Result<(), ManifestError> {
    let obj = value
        .as_object()
        .ok_or_else(|| invalid("variant requires tag/val object"))?;
    if obj.keys().any(|k| k != "tag" && k != "val") {
        return Err(invalid("unknown variant field"));
    }
    let tag = obj
        .get("tag")
        .and_then(CanonValue::as_str)
        .ok_or_else(|| invalid("variant tag missing"))?;
    let case = cases
        .iter()
        .find(|c| c.name == tag)
        .ok_or_else(|| invalid("unknown variant case"))?;
    match (&case.ty, obj.get("val")) {
        (Some(ty), Some(value)) => validate_json(ty, value),
        (None, None) | (None, Some(CanonValue::Null)) => Ok(()),
        _ => Err(invalid("variant payload mismatch")),
    }
}
/// Generated editor schema. The declared-width float roundtrip remains mandatory.
pub fn json_schema(ty: &WitType) -> serde_json::Value {
    use serde_json::json;
    match ty {
        WitType::Bool => json!({"type":"boolean"}),
        WitType::String => json!({"type":"string"}),
        WitType::Char => json!({"type":"string","minLength":1,"maxLength":1}),
        WitType::F32 | WitType::F64 => {
            json!({"type":"string","pattern":"^-?(0e0|[1-9](\\.[0-9]*[1-9])?e(0|-?[1-9][0-9]*))$","x-tobkiri-wit":if *ty==WitType::F32 {"f32"} else {"f64"}})
        }
        WitType::U8 | WitType::U16 | WitType::U32 | WitType::S8 | WitType::S16 | WitType::S32 => {
            let (min, max) = match ty {
                WitType::U8 => (0, 255),
                WitType::U16 => (0, 65535),
                WitType::U32 => (0, u32::MAX as i64),
                WitType::S8 => (-128, 127),
                WitType::S16 => (-32768, 32767),
                _ => (i32::MIN as i64, i32::MAX as i64),
            };
            json!({"type":"integer","minimum":min,"maximum":max})
        }
        WitType::U64 | WitType::S64 => {
            json!({"oneOf":[{"type":"integer","minimum":if *ty==WitType::U64 {0} else {-(MAX_SAFE_INTEGER as i64)},"maximum":MAX_SAFE_INTEGER},{"type":"string","pattern":"^-?(0|[1-9][0-9]*)$"}],"x-tobkiri-wit":if *ty==WitType::U64 {"u64"} else {"s64"}})
        }
        WitType::Json => json!({"x-tobkiri-wit":"tobkiri:core/types.json"}),
        WitType::Option(inner) => json!({"anyOf":[{"type":"null"},json_schema(inner)]}),
        WitType::List(inner) if **inner == WitType::U8 => {
            json!({"type":"string","pattern":"^[A-Za-z0-9_-]*$","contentEncoding":"base64url"})
        }
        WitType::List(inner) => json!({"type":"array","items":json_schema(inner)}),
        WitType::Tuple(items) => {
            json!({"type":"array","prefixItems":items.iter().map(json_schema).collect::<Vec<_>>(),"minItems":items.len(),"maxItems":items.len()})
        }
        WitType::Record(fields) => {
            json!({"type":"object","properties":fields.iter().map(|f|(f.name.clone(),json_schema(&f.ty))).collect::<BTreeMap<_,_>>(),"required":fields.iter().filter(|f|!matches!(f.ty,WitType::Option(_))).map(|f|&f.name).collect::<Vec<_>>(),"additionalProperties":false})
        }
        WitType::Enum(cases) => json!({"type":"string","enum":cases}),
        WitType::Flags(cases) => {
            json!({"type":"array","uniqueItems":true,"items":{"type":"string","enum":cases}})
        }
        WitType::Variant(cases) => {
            json!({"oneOf":cases.iter().map(|c| { let mut props=BTreeMap::from([("tag",json!({"const":c.name}))]); let mut required=vec!["tag"]; if let Some(ty)=&c.ty {props.insert("val",json_schema(ty));required.push("val");} else {props.insert("val",json!({"type":"null"}));} json!({"type":"object","properties":props,"required":required,"additionalProperties":false}) }).collect::<Vec<_>>()})
        }
        WitType::Result { ok, err } => json_schema(&WitType::Variant(vec![
            WitCase {
                name: "ok".into(),
                ty: ok.as_deref().cloned(),
            },
            WitCase {
                name: "err".into(),
                ty: err.as_deref().cloned(),
            },
        ])),
        WitType::StreamHandle | WitType::CallbackHandle => {
            json_schema(&WitType::Record(vec![WitField {
                name: "id".into(),
                ty: WitType::U64,
            }]))
        }
    }
}
fn evolves(old: &WitType, new: &WitType, input_only: bool) -> bool {
    if old == new {
        return true;
    }
    match (old, new) {
        (WitType::Record(a), WitType::Record(b)) => {
            b.len() >= a.len()
                && a.iter()
                    .zip(b)
                    .all(|(a, b)| a.name == b.name && evolves(&a.ty, &b.ty, input_only))
                && b[a.len()..]
                    .iter()
                    .all(|f| matches!(f.ty, WitType::Option(_)))
        }
        (WitType::Variant(a), WitType::Variant(b)) if input_only => {
            b.len() >= a.len() && a.iter().zip(b).all(|(a, b)| a == b)
        }
        (WitType::Enum(a), WitType::Enum(b)) if input_only => b.starts_with(a),
        (WitType::Option(a), WitType::Option(b)) | (WitType::List(a), WitType::List(b)) => {
            evolves(a, b, input_only)
        }
        (WitType::Tuple(a), WitType::Tuple(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| evolves(a, b, input_only))
        }
        (WitType::Result { ok: a, err: b }, WitType::Result { ok: c, err: d }) => {
            let optional = |a: &Option<Box<WitType>>, b: &Option<Box<WitType>>| match (a, b) {
                (None, None) => true,
                (Some(a), Some(b)) => evolves(a, b, input_only),
                _ => false,
            };
            optional(a, c) && optional(b, d)
        }
        _ => false,
    }
}
/// Reject mislabeled minor changes; different majors require an adapter Pack.
pub fn compatibility(old: &ShapeDef, new: &ShapeDef) -> Result<Compat, ManifestError> {
    if old.reference.name != new.reference.name {
        return Err(invalid("shape names differ"));
    }
    if old.reference.version.major != new.reference.version.major {
        return Ok(Compat::NeedsAdapter);
    }
    if new.reference.version.minor < old.reference.version.minor {
        return compatibility(new, old);
    }
    if old.cardinality != new.cardinality || old.kind != new.kind {
        return Err(invalid(
            "shape cardinality/kind changed within major version",
        ));
    }
    let changed = old.functions != new.functions
        || old.types != new.types
        || old.streams != new.streams
        || old.callbacks != new.callbacks;
    for (name, function) in &old.functions {
        let next = new
            .functions
            .get(name)
            .ok_or_else(|| invalid("minor version removed operation"))?;
        if function.params.len() != next.params.len()
            || !function
                .params
                .iter()
                .zip(&next.params)
                .all(|((n, t), (m, u))| n == m && evolves(t, u, true))
            || match (&function.result, &next.result) {
                (None, None) => false,
                (Some(a), Some(b)) => !evolves(a, b, false),
                _ => true,
            }
        {
            return Err(invalid("minor version changed incompatible operation pins"));
        }
    }
    for (name, ty) in &old.types {
        let next = new
            .types
            .get(name)
            .ok_or_else(|| invalid("minor version removed named type"))?;
        let input_only = old.kind != ShapeKind::Data
            && old
                .functions
                .values()
                .any(|f| f.params.iter().any(|(_, t)| contains_type(t, ty)))
            && !old
                .functions
                .values()
                .any(|f| f.result.as_ref().is_some_and(|t| contains_type(t, ty)));
        if !evolves(ty, next, input_only) {
            return Err(invalid("minor version changed incompatible named type"));
        }
    }
    if old.streams != new.streams || old.callbacks != new.callbacks {
        return Err(invalid("minor version changed stream/callback meanings"));
    }
    if changed && old.reference.version == new.reference.version {
        return Err(invalid("shape pins changed without a version bump"));
    }
    if !changed {
        return Ok(Compat::Exact);
    }
    Ok(Compat::MinorCompatible)
}
fn contains_type(tree: &WitType, target: &WitType) -> bool {
    tree == target
        || match tree {
            WitType::Record(fields) => fields.iter().any(|f| contains_type(&f.ty, target)),
            WitType::Option(t) | WitType::List(t) => contains_type(t, target),
            WitType::Tuple(types) => types.iter().any(|t| contains_type(t, target)),
            WitType::Variant(cases) => cases
                .iter()
                .any(|c| c.ty.as_ref().is_some_and(|t| contains_type(t, target))),
            WitType::Result { ok, err } => {
                ok.as_ref().is_some_and(|t| contains_type(t, target))
                    || err.as_ref().is_some_and(|t| contains_type(t, target))
            }
            _ => false,
        }
}
/// Project compatible record pins, dropping trailing options or filling nulls.
pub fn project_json(
    from: &WitType,
    to: &WitType,
    value: &CanonValue,
) -> Result<CanonValue, ManifestError> {
    validate_json(from, value)?;
    if !evolves(from, to, true) && !evolves(to, from, true) {
        return Err(invalid(
            "projection requires compatible pins; width changes require an adapter",
        ));
    }
    let projected = match (from, to, value) {
        (WitType::Record(old), WitType::Record(new), CanonValue::Obj(values)) => {
            let mut out = BTreeMap::new();
            for field in new {
                if let Some(prior) = old.iter().find(|f| f.name == field.name) {
                    let v = values.get(&field.name).unwrap_or(&CanonValue::Null);
                    out.insert(field.name.clone(), project_json(&prior.ty, &field.ty, v)?);
                } else if matches!(field.ty, WitType::Option(_)) {
                    out.insert(field.name.clone(), CanonValue::Null);
                } else {
                    return Err(invalid("projection missing required field"));
                }
            }
            CanonValue::Obj(out)
        }
        (WitType::Option(a), WitType::Option(b), v) if !matches!(v, CanonValue::Null) => {
            project_json(a, b, v)?
        }
        (WitType::List(a), WitType::List(b), CanonValue::Arr(values)) => CanonValue::Arr(
            values
                .iter()
                .map(|v| project_json(a, b, v))
                .collect::<Result<_, _>>()?,
        ),
        (WitType::Tuple(a), WitType::Tuple(b), CanonValue::Arr(values)) => CanonValue::Arr(
            a.iter()
                .zip(b)
                .zip(values)
                .map(|((a, b), v)| project_json(a, b, v))
                .collect::<Result<_, _>>()?,
        ),
        (WitType::Variant(a), WitType::Variant(b), CanonValue::Obj(values)) => {
            let mut out = values.clone();
            let tag = values["tag"].as_str().expect("validated variant");
            let before = a.iter().find(|c| c.name == tag).expect("validated variant");
            let after = b
                .iter()
                .find(|c| c.name == tag)
                .ok_or_else(|| invalid("projection cannot drop a live variant case"))?;
            if let (Some(a), Some(b), Some(v)) = (&before.ty, &after.ty, values.get("val")) {
                out.insert("val".into(), project_json(a, b, v)?);
            }
            CanonValue::Obj(out)
        }
        (
            WitType::Result { ok: a, err: b },
            WitType::Result { ok: c, err: d },
            CanonValue::Obj(values),
        ) => {
            let mut out = values.clone();
            let (from, to) = if values["tag"].as_str() == Some("ok") {
                (a, c)
            } else {
                (b, d)
            };
            if let (Some(a), Some(b), Some(v)) = (from, to, values.get("val")) {
                out.insert("val".into(), project_json(a, b, v)?);
            }
            CanonValue::Obj(out)
        }
        _ => value.clone(),
    };
    validate_json(to, &projected)?;
    Ok(projected)
}
