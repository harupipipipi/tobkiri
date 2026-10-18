use crate::*;
use std::collections::{BTreeMap, BTreeSet};
use tobkiri_canon::{Variant, ids::ShapeName};
use tobkiri_manifest::{
    Cardinality, InputDeclaration, RuntimeSpec, ShapeDef, ShapeKind, VerifiedPack, WitFunction,
    WitType, compatibility, input_function,
};

// Unresolved source candidates have no frozen normalized AST identity yet.
struct OverlayCandidate {
    owner: Option<PackId>,
    hash: Option<PackHash>,
    document: OverlayDocument,
}

fn conflict(code: &str, message: impl Into<String>, packs: Vec<String>) -> Conflict {
    Conflict {
        code: code.into(),
        message: message.into(),
        packs,
    }
}
fn canonical<T: Serialize>(value: &T) -> Result<CanonValue, String> {
    let bytes = serde_json::to_vec(value).map_err(|e| e.to_string())?;
    tobkiri_canon::parse(&bytes, Variant::StrictV1).map_err(|e| e.to_string())
}
fn valid_deadline(ms: u64) -> bool {
    std::time::Instant::now()
        .checked_add(std::time::Duration::from_millis(ms))
        .is_some()
}
fn shape<'a>(
    reference: &ShapeRef,
    shapes: &'a BTreeMap<ShapeRef, ShapeDef>,
) -> Option<&'a ShapeDef> {
    shapes.get(reference)
}
fn validate_policy_views(
    label: &str,
    permissions: &PermissionSet,
    views: &BTreeMap<ShapeRef, ShapeDef>,
    errors: &mut Vec<Conflict>,
) {
    if let Err(error) = tobkiri_manifest::resolve_permission_views(&permissions.ports, views) {
        errors.push(conflict("policy-view", format!("{label}: {error}"), vec![]));
    }
}
fn original_permission(
    owner: Option<&PackId>,
    reference: &ShapeRef,
    profile: &Profile,
    selected: &BTreeMap<PackId, &VerifiedPack>,
) -> bool {
    let literal = reference.to_string();
    let Some(owner) = owner else {
        return profile
            .user_permissions
            .as_ref()
            .is_some_and(|declaration| declaration.ports.contains(&literal))
            && profile.ceiling.ports.contains(&literal);
    };
    let Some(pack) = selected.get(owner) else {
        return false;
    };
    let Some(limits) = profile.pack_limits.get(owner) else {
        return false;
    };
    pack.manifest.permissions.ports.contains(&literal)
        && profile.ceiling.ports.contains(&literal)
        && limits.ports.contains(&literal)
}
fn check_step_authority(
    body: &[Step],
    owner: Option<&PackId>,
    profile: &Profile,
    selected: &BTreeMap<PackId, &VerifiedPack>,
    views: &BTreeMap<ShapeRef, ShapeDef>,
    errors: &mut Vec<Conflict>,
) {
    let mut flat = Vec::new();
    steps(body, &mut flat);
    for step in flat {
        let mut requestable = Vec::new();
        match &step.kind {
            // This literal belongs to the already verified, pinned Flow AST.
            // It declares the structural request without creating permission.
            StepKind::Call(call) => requestable.push((&call.shape, Some(&call.op))),
            StepKind::Flow(call) => requestable.push((&call.implements, None)),
            _ => (),
        }
        let emit = OpName::parse("emit").expect("fixed operation");
        if let Some(stream) = &step.stream {
            requestable.push((&stream.emit, Some(&emit)));
        }
        for (reference, operation) in requestable {
            if !original_permission(owner, reference, profile, selected) {
                errors.push(conflict("port-permission",format!("{reference} requires the original principal declaration and ceiling, with explicit Pack limits for Pack principals; routing never grants authority"),owner.iter().map(ToString::to_string).collect()));
            }
            if let Some(operation) = operation
                && !views
                    .get(reference)
                    .is_some_and(|view| view.functions.contains_key(operation))
            {
                errors.push(conflict(
                    "caller-view-operation",
                    format!("{reference} does not expose caller operation {operation}"),
                    vec![],
                ));
            }
        }
    }
}
fn reference_strings(value: &CanonValue, out: &mut Vec<String>) {
    match value {
        CanonValue::Str(s) if s.starts_with('$') => out.push(s.clone()),
        CanonValue::Arr(items) => {
            for v in items {
                reference_strings(v, out)
            }
        }
        CanonValue::Obj(items) => {
            for v in items.values() {
                reference_strings(v, out)
            }
        }
        _ => (),
    }
}
fn expression(value: &CanonValue) -> bool {
    match value {
        CanonValue::Obj(values) if values.len() == 1 => {
            let (name, args) = values.iter().next().expect("one entry");
            match name.as_str() {
                "eq" | "ne" => args
                    .as_array()
                    .is_some_and(|a| a.len() == 2 && a.iter().all(expression)),
                "and" | "or" => args
                    .as_array()
                    .is_some_and(|a| !a.is_empty() && a.iter().all(expression)),
                "not" | "exists" => expression(args),
                _ => false,
            }
        }
        CanonValue::Obj(_) => false,
        CanonValue::Arr(v) => v.iter().all(expression),
        CanonValue::Float(_) | CanonValue::BigInt(_) => false,
        _ => true,
    }
}
fn steps<'a>(steps: &'a [Step], out: &mut Vec<&'a Step>) {
    for step in steps {
        out.push(step);
        match &step.kind {
            StepKind::Branch(b) => {
                self::steps(&b.then, out);
                self::steps(&b.otherwise, out)
            }
            StepKind::Parallel(p) => self::steps(&p.body, out),
            StepKind::Loop(l) => self::steps(&l.body, out),
            _ => (),
        }
    }
}
fn policy_keys(document: &FlowDocument, errors: &mut Vec<Conflict>) {
    let mut all = Vec::new();
    steps(&document.steps, &mut all);
    let explicit: BTreeSet<_> = all.iter().filter_map(|s| s.id.as_ref()).collect();
    if document
        .on_error
        .steps
        .keys()
        .any(|id| !explicit.contains(id))
    {
        errors.push(conflict(
            "error-policy-step",
            "per-step error policies must name an explicit existing step ID",
            vec![],
        ));
    }
}
fn step_references(step: &Step, out: &mut Vec<String>) {
    match &step.kind {
        StepKind::Call(c) => reference_strings(&c.args, out),
        StepKind::Flow(c) => reference_strings(&c.args, out),
        StepKind::Branch(b) => reference_strings(&b.condition, out),
        StepKind::Parallel(p) => reference_strings(&p.for_each, out),
        StepKind::Loop(l) => reference_strings(&l.condition, out),
        StepKind::Return(v) => reference_strings(v, out),
        StepKind::Next(n) => reference_strings(&n.args, out),
    }
}
// Alternative branches may each call next once. Repetition and parallel execution
// of next are not allowed because the continuation token is single use.
fn max_next(body: &[Step]) -> Option<usize> {
    let mut count = 0usize;
    for step in body {
        let n = match &step.kind {
            StepKind::Next(_) => 1,
            StepKind::Branch(b) => max_next(&b.then)?.max(max_next(&b.otherwise)?),
            StepKind::Loop(l) => {
                if max_next(&l.body)? > 0 {
                    return None;
                }
                0
            }
            StepKind::Parallel(p) => {
                if max_next(&p.body)? > 0 {
                    return None;
                }
                0
            }
            _ => 0,
        };
        count = count.checked_add(n)?;
    }
    Some(count)
}
fn assign_ids(steps: &mut [Step], prefix: &str) {
    for (index, step) in steps.iter_mut().enumerate() {
        let address = format!("{prefix}{index}");
        if step.id.is_none() {
            step.id = Some(format!("auto-{address}"));
        }
        match &mut step.kind {
            StepKind::Branch(b) => {
                assign_ids(&mut b.then, &format!("{address}-t-"));
                assign_ids(&mut b.otherwise, &format!("{address}-e-"))
            }
            StepKind::Parallel(p) => assign_ids(&mut p.body, &format!("{address}-p-")),
            StepKind::Loop(l) => assign_ids(&mut l.body, &format!("{address}-l-")),
            _ => (),
        }
    }
}
fn check_steps(
    document: &FlowDocument,
    profile: &Profile,
    shapes: &BTreeMap<ShapeRef, ShapeDef>,
    overlay: bool,
    errors: &mut Vec<Conflict>,
) {
    let mut all = Vec::new();
    steps(&document.steps, &mut all);
    let mut ids = BTreeSet::new();
    for step in all {
        if step
            .id
            .as_ref()
            .is_some_and(|id| id.is_empty() || !ids.insert(id.clone()))
        {
            errors.push(conflict(
                "duplicate-step",
                "step IDs must be unique including nested steps",
                vec![],
            ));
        }
        if step.overlay == OverlayAccess::Open && step.role.is_none() {
            errors.push(conflict(
                "missing-role",
                "open overlay targets must declare a role",
                vec![],
            ));
        }
        if let Some(role) = &step.role
            && !shape(role, shapes).is_some_and(|s| s.kind == ShapeKind::Role)
        {
            errors.push(conflict(
                "unknown-role",
                format!("role shape {role} missing"),
                vec![],
            ));
        }
        match &step.kind {
            StepKind::Call(call) => {
                if !shape(&call.shape, shapes).is_some_and(|s| s.functions.contains_key(&call.op)) {
                    errors.push(conflict(
                        "unknown-operation",
                        format!("{} does not define {}", call.shape, call.op),
                        vec![],
                    ));
                }
            }
            StepKind::Branch(branch) => {
                if !expression(&branch.condition) {
                    errors.push(conflict(
                        "invalid-expression",
                        "branch expression outside frozen operators",
                        vec![],
                    ));
                }
            }
            StepKind::Loop(body) => {
                if !expression(&body.condition)
                    || body.limits.as_ref().is_some_and(|l| {
                        !l.validate()
                            || !valid_deadline(l.max_wall_ms)
                            || !l.is_subset_of(&profile.ceiling.loops)
                    })
                {
                    errors.push(conflict(
                        "loop-limit",
                        "loop expression/bounds invalid or exceed Profile defaults",
                        vec![],
                    ));
                }
            }
            StepKind::Parallel(parallel) => {
                if parallel.max == 0 || parallel.max > 32 || parallel.binding.is_empty() {
                    errors.push(conflict(
                        "parallel-limit",
                        "parallel requires a binding and concurrency 1..32",
                        vec![],
                    ));
                }
            }
            StepKind::Next(_) | StepKind::Return(_) if !overlay => errors.push(conflict(
                "overlay-only-step",
                "next/return only allowed in overlays",
                vec![],
            )),
            _ => (),
        }
    }
}
fn target_matches(target: &OverlayTarget, flow: &ResolvedFlow, step: &Step, key: &str) -> bool {
    match target {
        OverlayTarget::Role { role } => step.role.as_ref() == Some(role),
        OverlayTarget::Step {
            flow: target,
            step: id,
        } => (target == &flow.document.id || target == key) && step.id.as_ref() == Some(id),
    }
}
fn overlay_id(overlay: &ResolvedOverlay) -> String {
    overlay.owner.as_ref().map_or_else(
        || format!("user:{}", overlay.document.id),
        |owner| format!("{owner}/{}", overlay.document.id),
    )
}
fn label_matches(overlay: &ResolvedOverlay, label: &str) -> bool {
    overlay_id(overlay) == label || overlay.owner.as_ref().is_some_and(|p| p.as_str() == label)
}
fn ordered(
    candidates: &[ResolvedOverlay],
    explicit: Option<&[String]>,
) -> Result<Vec<ResolvedOverlay>, String> {
    let count = candidates.len();
    let mut edges = vec![BTreeSet::new(); count];
    for (i, overlay) in candidates.iter().enumerate() {
        for label in &overlay.document.order.before {
            for (j, other) in candidates.iter().enumerate() {
                if i != j && label_matches(other, label) {
                    edges[i].insert(j);
                }
            }
        }
        for label in &overlay.document.order.after {
            for (j, other) in candidates.iter().enumerate() {
                if i != j && label_matches(other, label) {
                    edges[j].insert(i);
                }
            }
        }
    }
    if let Some(order) = explicit {
        let mut ranks = vec![None; count];
        for (rank, label) in order.iter().enumerate() {
            let matching: Vec<_> = candidates
                .iter()
                .enumerate()
                .filter(|(_, o)| label_matches(o, label))
                .map(|(i, _)| i)
                .collect();
            if matching.len() != 1 {
                return Err("Profile overlay order label is absent or ambiguous".into());
            }
            let index = matching[0];
            if ranks[index].replace(rank).is_some() {
                return Err("Profile overlay order duplicates overlay".into());
            }
        }
        for i in 0..count {
            for j in 0..count {
                if ranks[i].zip(ranks[j]).is_some_and(|(a, b)| a < b) {
                    edges[i].insert(j);
                }
            }
        }
    }
    let mut remaining: BTreeSet<usize> = (0..count).collect();
    let mut result = Vec::new();
    while !remaining.is_empty() {
        let ready: Vec<_> = remaining
            .iter()
            .copied()
            .filter(|i| !remaining.iter().any(|j| edges[*j].contains(i)))
            .collect();
        if ready.len() != 1 {
            return Err(if ready.is_empty() {
                "overlay order cycle"
            } else {
                "overlay order is not explicit and total"
            }
            .into());
        }
        let index = ready[0];
        remaining.remove(&index);
        result.push(candidates[index].clone());
    }
    Ok(result)
}
/// Compile one immutable candidate without mutating heads, approvals or stores.
pub fn compile(
    profile: &Profile,
    packs: &[VerifiedPack],
) -> Result<CompiledProfile, Vec<Conflict>> {
    let mut errors = Vec::new();
    if profile.format != "tobkiri.profile/1"
        || !profile.ceiling.loops.validate()
        || !valid_deadline(profile.ceiling.loops.max_wall_ms)
        || !valid_deadline(profile.ceiling.wasm.wall_ms)
    {
        errors.push(conflict(
            "profile-limits",
            "Profile requires valid default loop bounds",
            vec![],
        ));
    }
    let mut selected: BTreeMap<PackId, &VerifiedPack> = BTreeMap::new();
    for pin in &profile.packs {
        if selected.contains_key(&pin.id) {
            errors.push(conflict(
                "duplicate-pack",
                format!("duplicate Pack {}", pin.id),
                vec![pin.id.to_string()],
            ));
            continue;
        }
        let Some(pack) = packs.iter().find(|p| {
            p.manifest.id == pin.id && p.hash == pin.sha256 && p.manifest.version == pin.version
        }) else {
            errors.push(conflict(
                "missing-artifact",
                format!("Pack {} not verified at pinned hash/version", pin.id),
                vec![pin.id.to_string()],
            ));
            continue;
        };
        if let Some(key) = &pack.manifest.publisher_key
            && profile.publishers.get(pin.id.publisher()) != Some(key)
        {
            errors.push(conflict(
                "publisher-binding",
                "Pack publisher key is not bound by Profile",
                vec![pin.id.to_string()],
            ));
        }
        if matches!(pack.manifest.runtime, RuntimeSpec::Native { .. }) && !profile.ceiling.native {
            errors.push(conflict(
                "dangerous-native",
                "Profile must explicitly allow native execution",
                vec![pin.id.to_string()],
            ));
        }
        selected.insert(pin.id.clone(), pack);
    }
    for (id, limits) in &profile.pack_limits {
        if !selected.contains_key(id) || !limits.is_subset_of(&profile.ceiling) {
            errors.push(conflict(
                "permission-widening",
                "Pack limits must belong to a selected Pack and narrow Profile ceiling",
                vec![id.to_string()],
            ));
        }
    }
    for id in profile
        .startup
        .inputs
        .iter()
        .map(|entry| &entry.source_pack)
        .chain(profile.startup.ui.iter())
    {
        if !selected.contains_key(id) {
            errors.push(conflict(
                "startup-pack",
                "startup references absent Pack",
                vec![id.to_string()],
            ));
        }
    }
    if profile
        .startup
        .ui
        .as_ref()
        .is_some_and(|id| selected.get(id).is_some_and(|p| p.manifest.ui.is_none()))
    {
        errors.push(conflict(
            "startup-ui",
            "startup UI Pack has no UI entry",
            vec![],
        ));
    }
    let mut shapes: BTreeMap<ShapeRef, ShapeDef> = BTreeMap::new();
    for pack in selected.values() {
        for definition in &pack.shapes {
            if shapes
                .insert(definition.reference.clone(), definition.clone())
                .is_some()
            {
                errors.push(conflict(
                    "duplicate-shape",
                    "duplicate shape name/revision",
                    vec![pack.manifest.id.to_string()],
                ));
            }
        }
    }
    // A formal declaration is checked even if this Profile does not start it.
    // External shape ownership is resolved here, never from provider aliases.
    for pack in selected.values() {
        for declaration in &pack.manifest.inputs {
            let Some(view) = shapes.get(&declaration.exact_input_view) else {
                errors.push(conflict(
                    "input-declaration-view",
                    "formal Input has no exact authoritative view",
                    vec![pack.manifest.id.to_string()],
                ));
                continue;
            };
            match input_function(&pack.manifest, declaration, view) {
                Ok(function) => {
                    if let Err(error) = input_arguments(function) {
                        errors.push(conflict(
                            "input-declaration-signature",
                            error,
                            vec![pack.manifest.id.to_string()],
                        ));
                    }
                }
                Err(error) => errors.push(conflict(
                    "input-declaration-operation",
                    error.to_string(),
                    vec![pack.manifest.id.to_string()],
                )),
            }
        }
    }
    let mut input_functions = Vec::new();
    let mut input_selections = BTreeSet::new();
    for entry in &profile.startup.inputs {
        let owner = entry.source_pack.to_string();
        if !input_selections.insert(entry.clone()) {
            errors.push(conflict(
                "startup-input-duplicate",
                "duplicate exact Input entry",
                vec![owner],
            ));
            continue;
        }
        let Some(pack) = selected.get(&entry.source_pack) else {
            continue; // The absent Pack already has a startup-pack conflict.
        };
        let Some(view) = shapes.get(&entry.exact_input_view) else {
            errors.push(conflict(
                "startup-input-view",
                "Input requires its exact verified WIT view",
                vec![owner],
            ));
            continue;
        };
        let declaration = InputDeclaration {
            exact_input_view: entry.exact_input_view.clone(),
            operation: entry.operation.clone(),
        };
        let function = match input_function(&pack.manifest, &declaration, view) {
            Ok(function) => function,
            Err(error) => {
                errors.push(conflict(
                    "startup-input-declaration",
                    error.to_string(),
                    vec![owner],
                ));
                continue;
            }
        };
        match input_arguments(function) {
            Ok(arguments) => input_functions.push((
                entry.clone(),
                pack.hash.clone(),
                function.clone(),
                arguments,
            )),
            Err(error) => errors.push(conflict("startup-input-signature", error, vec![owner])),
        }
    }
    let definitions: Vec<_> = shapes.values().collect();
    for (i, old) in definitions.iter().enumerate() {
        for new in definitions.iter().skip(i + 1) {
            if old.reference.name == new.reference.name
                && old.reference.version.major == new.reference.version.major
                && compatibility(old, new).is_err()
            {
                errors.push(conflict(
                    "mislabeled-minor",
                    format!("incompatible minor revisions for {}", old.reference.name),
                    vec![],
                ));
            }
        }
    }
    validate_policy_views("Profile ceiling", &profile.ceiling, &shapes, &mut errors);
    if let Some(declaration) = &profile.user_permissions {
        validate_policy_views("User declaration", declaration, &shapes, &mut errors);
    }
    for pack in selected.values() {
        validate_policy_views(
            &format!("Pack {} declaration", pack.manifest.id),
            &pack.manifest.permissions,
            &shapes,
            &mut errors,
        );
    }
    for (owner, limits) in &profile.pack_limits {
        validate_policy_views(
            &format!("Pack {owner} limits"),
            limits,
            &shapes,
            &mut errors,
        );
    }
    for key in profile.bindings.keys() {
        let valid = key.rsplit_once('@').is_some_and(|(name, major)| {
            ShapeName::parse(name).is_ok()
                && major.parse::<u16>().is_ok()
                && !major.is_empty()
                && !(major.len() > 1 && major.starts_with('0'))
        });
        if !valid {
            errors.push(conflict(
                "binding-key",
                "bindings must use shape-name@major",
                vec![],
            ));
        }
    }
    let mut provider_groups: BTreeMap<(ShapeName, u16), Vec<(PackId, ShapeRef)>> = BTreeMap::new();
    for pack in selected.values() {
        for port in &pack.manifest.ports.provides {
            let reference = ShapeRef::new(port.shape.clone(), port.version);
            if shape(&reference, &shapes).is_none() {
                errors.push(conflict(
                    "untyped-port",
                    format!("typed provided port {reference} has no WIT shape"),
                    vec![pack.manifest.id.to_string()],
                ));
            }
            provider_groups
                .entry((port.shape.clone(), port.version.major))
                .or_default()
                .push((pack.manifest.id.clone(), reference));
        }
    }
    let mut bindings = BTreeMap::new();
    for ((name, major), providers) in &provider_groups {
        let reference = providers[0].1.clone();
        let Some(definition) = shape(&reference, &shapes) else {
            continue;
        };
        let ids: BTreeSet<_> = providers.iter().map(|(id, _)| id.clone()).collect();
        let key = format!("{name}@{major}");
        let chosen = if definition.cardinality == Cardinality::Many {
            ids.into_iter().collect::<Vec<_>>()
        } else if let Some(choice) = profile.bindings.get(&key) {
            if !ids.contains(choice) {
                errors.push(conflict(
                    "provider-binding",
                    "explicit binding is not a provider",
                    vec![choice.to_string()],
                ));
                Vec::new()
            } else {
                vec![choice.clone()]
            }
        } else if ids.len() == 1 {
            ids.into_iter().collect()
        } else {
            errors.push(conflict(
                "provider-collision",
                format!("single slot {key} needs explicit provider selection"),
                ids.iter().map(ToString::to_string).collect(),
            ));
            Vec::new()
        };
        for definition in shapes
            .keys()
            .filter(|r| &r.name == name && r.version.major == *major)
        {
            bindings.insert(definition.clone(), chosen.clone());
        }
        for (_, reference) in providers {
            bindings.insert(reference.clone(), chosen.clone());
        }
    }
    let mut dynamic_flows = Vec::new();
    for pack in selected.values() {
        for port in &pack.manifest.ports.consumes {
            let reference = ShapeRef::new(port.shape.clone(), port.version);
            let definition = shape(&reference, &shapes);
            if definition.is_none() {
                errors.push(conflict(
                    "untyped-port",
                    format!("typed consumed port {reference} has no WIT shape"),
                    vec![pack.manifest.id.to_string()],
                ));
            }
            // A consumed Flow shape declares a dynamic guest run-flow
            // target, never a Port call. It is checked against selected
            // Flow endpoints by exact implements identity below.
            if definition.is_some_and(|def| def.kind == ShapeKind::Flow) {
                dynamic_flows.push((pack.manifest.id.clone(), port.optional, reference));
                continue;
            }
            let resolved = bindings
                .iter()
                .find(|(r, _)| {
                    r.name == reference.name && r.version.major == reference.version.major
                })
                .map(|(_, p)| p.clone());
            if !port.optional && resolved.as_ref().is_none_or(Vec::is_empty) {
                errors.push(conflict(
                    "required-port",
                    format!("{} requires {reference}", pack.manifest.id),
                    vec![pack.manifest.id.to_string()],
                ));
            }
            if let Some(resolved) = resolved {
                bindings.insert(reference, resolved);
            }
        }
    }
    for (key, choice) in &profile.bindings {
        if !provider_groups
            .keys()
            .any(|(name, major)| format!("{name}@{major}") == *key)
        {
            errors.push(conflict(
                "unused-binding",
                "explicit binding does not name an available port",
                vec![choice.to_string()],
            ));
        }
    }
    let mut flows: BTreeMap<String, ResolvedFlow> = BTreeMap::new();
    let mut overlay_candidates = Vec::new();
    for pack in selected.values() {
        let mut ids = BTreeSet::new();
        for path in &pack.manifest.flows {
            match pack.files.get(path).map_or_else(
                || Err(ProfileError::Invalid("flow file missing".into())),
                |b| parse_flow(b),
            ) {
                Ok(mut document) => {
                    if !ids.insert(document.id.clone()) {
                        errors.push(conflict(
                            "duplicate-flow",
                            "duplicate Flow ID within Pack",
                            vec![pack.manifest.id.to_string()],
                        ));
                        continue;
                    }
                    policy_keys(&document, &mut errors);
                    assign_ids(&mut document.steps, "");
                    check_steps(&document, profile, &shapes, false, &mut errors);
                    check_step_authority(
                        &document.steps,
                        Some(&pack.manifest.id),
                        profile,
                        &selected,
                        &shapes,
                        &mut errors,
                    );
                    let key = format!("{}/{}", pack.manifest.id, document.id);
                    let placeholder = Digest::hash(b"");
                    flows.insert(
                        key,
                        ResolvedFlow {
                            owner: pack.manifest.id.clone(),
                            hash: pack.hash.clone(),
                            document,
                            ordered_overlays: BTreeMap::new(),
                            plan_digest: placeholder,
                            call_witnesses: BTreeMap::new(),
                        },
                    );
                }
                Err(e) => errors.push(conflict(
                    "flow-document",
                    e.to_string(),
                    vec![pack.manifest.id.to_string()],
                )),
            }
        }
        let mut overlay_ids = BTreeSet::new();
        for path in &pack.manifest.overlays {
            match pack.files.get(path).map_or_else(
                || Err(ProfileError::Invalid("overlay file missing".into())),
                |b| parse_overlay(b),
            ) {
                Ok(document) => {
                    if !overlay_ids.insert(document.id.clone()) {
                        errors.push(conflict(
                            "duplicate-overlay",
                            "duplicate overlay ID within Pack",
                            vec![pack.manifest.id.to_string()],
                        ));
                    }
                    overlay_candidates.push(OverlayCandidate {
                        owner: Some(pack.manifest.id.clone()),
                        hash: Some(pack.hash.clone()),
                        document,
                    });
                }
                Err(e) => errors.push(conflict(
                    "overlay-document",
                    e.to_string(),
                    vec![pack.manifest.id.to_string()],
                )),
            }
        }
    }
    let mut user_ids = BTreeSet::new();
    for document in &profile.user_overlays {
        if document.format != "tobkiri.overlay/1" || !user_ids.insert(document.id.clone()) {
            errors.push(conflict(
                "user-overlay",
                "invalid format or duplicate user overlay ID",
                vec![],
            ));
        }
        overlay_candidates.push(OverlayCandidate {
            owner: None,
            hash: None,
            document: document.clone(),
        });
    }
    for (flow_key, flow) in &mut flows {
        let mut all = Vec::new();
        steps(&flow.document.steps, &mut all);
        for step in &all {
            if let StepKind::Call(call) = &step.kind {
                if bindings.get(&call.shape).is_none_or(Vec::is_empty) {
                    errors.push(conflict(
                        "flow-binding",
                        format!("Flow call {} is not bound", call.shape),
                        vec![flow.owner.to_string()],
                    ));
                }
                if !selected[&flow.owner]
                    .manifest
                    .permissions
                    .ports
                    .contains(&call.shape.to_string())
                {
                    errors.push(conflict(
                        "flow-permission",
                        format!("Flow owner did not declare {}", call.shape),
                        vec![flow.owner.to_string()],
                    ));
                }
            }
            if let StepKind::Flow(sub) = &step.kind
                && !shapes
                    .values()
                    .any(|s| s.reference == sub.implements && s.kind == ShapeKind::Flow)
            {
                errors.push(conflict(
                    "subflow-shape",
                    "subflow implements shape missing",
                    vec![],
                ));
            }
            let mut matches = Vec::new();
            for overlay in &overlay_candidates {
                if !target_matches(&overlay.document.target, flow, step, flow_key) {
                    continue;
                }
                if let Some(owner) = &overlay.owner {
                    if !matches!(overlay.document.target, OverlayTarget::Role { .. })
                        || step.overlay != OverlayAccess::Open
                    {
                        errors.push(conflict(
                            "overlay-closed",
                            "Pack overlay requires an open role-addressed target",
                            vec![owner.to_string()],
                        ));
                        continue;
                    }
                    let role = step.role.as_ref().expect("role target");
                    if !original_permission(Some(owner), role, profile, &selected) {
                        errors.push(conflict(
                            "overlay-permission",
                            "Pack overlay requires Profile permission for the target role",
                            vec![owner.to_string()],
                        ));
                        continue;
                    }
                    if owner != &flow.owner
                        && !flow.document.implements.as_ref().is_some_and(|reference| {
                            shape(reference, &shapes)
                                .is_some_and(|shape| shape.kind == ShapeKind::Flow)
                        })
                    {
                        errors.push(conflict(
                            "overlay-private-flow",
                            "foreign Pack overlay requires a published Flow shape",
                            vec![owner.to_string()],
                        ));
                        continue;
                    }
                }
                let mut body = overlay.document.body.clone();
                assign_ids(&mut body, "overlay-");
                let wrapper = FlowDocument {
                    format: "tobkiri.flow/1".into(),
                    id: "overlay".into(),
                    implements: None,
                    trigger: flow.document.trigger.clone(),
                    loop_limits: "profile-default".into(),
                    steps: body,
                    on_error: ErrorPolicy::default(),
                };
                check_steps(&wrapper, profile, &shapes, true, &mut errors);
                check_step_authority(
                    &wrapper.steps,
                    overlay.owner.as_ref(),
                    profile,
                    &selected,
                    &shapes,
                    &mut errors,
                );
                let mut flat = Vec::new();
                steps(&wrapper.steps, &mut flat);
                let next_count = max_next(&wrapper.steps);
                if next_count.is_none_or(|n| n > 1) {
                    errors.push(conflict(
                        "multiple-next",
                        "next must occur at most once per path, outside loops and parallel bodies",
                        vec![],
                    ));
                }
                let returns = flat.iter().any(|s| matches!(s.kind, StepKind::Return(_)));
                if next_count == Some(0)
                    && !returns
                    && let Some(output) = &step.out
                {
                    let mut refs = Vec::new();
                    for s in &all {
                        step_references(s, &mut refs)
                    }
                    if refs.iter().any(|r| {
                        r == &format!("${output}") || r.starts_with(&format!("${output}."))
                    }) {
                        errors.push(conflict(
                            "delete-used-output",
                            "overlay cannot delete a step whose output is used",
                            vec![],
                        ));
                    }
                }
                let mut document = overlay.document.clone();
                document.body = wrapper.steps;
                // This is the existing witness digest, after the same body ID
                // normalization. Store it even when the overlay has no calls.
                let ast_hash = tobkiri_canon::digest(
                    &canonical(&document)
                        .map_err(|e| vec![conflict("canonical-witness", e, vec![])])?,
                    Variant::StrictV1,
                )
                .map_err(|e| vec![conflict("canonical-witness", e.to_string(), vec![])])?;
                matches.push(ResolvedOverlay {
                    owner: overlay.owner.clone(),
                    hash: overlay.hash.clone(),
                    document,
                    ast_hash,
                });
            }
            if !matches.is_empty() {
                let explicit = profile
                    .overlay_order
                    .iter()
                    .find(|o| target_matches(&o.target, flow, step, flow_key))
                    .map(|o| o.order.as_slice());
                match ordered(&matches, explicit) {
                    Ok(ordered) => {
                        flow.ordered_overlays
                            .insert(step.id.clone().expect("assigned step ID"), ordered);
                    }
                    Err(e) => errors.push(conflict("overlay-order", e, vec![])),
                }
            }
        }
    }
    for overlay in &overlay_candidates {
        if !flows.iter().any(|(key, flow)| {
            let mut all = Vec::new();
            steps(&flow.document.steps, &mut all);
            all.into_iter()
                .any(|step| target_matches(&overlay.document.target, flow, step, key))
        }) {
            errors.push(conflict(
                "overlay-target",
                "overlay target does not exist",
                overlay.owner.iter().map(ToString::to_string).collect(),
            ));
        }
    }
    let mut implementations: BTreeMap<(ShapeName, u16), Vec<&ResolvedFlow>> = BTreeMap::new();
    for flow in flows.values() {
        if let Some(reference) = &flow.document.implements {
            if !shape(reference, &shapes).is_some_and(|s| s.kind == ShapeKind::Flow) {
                errors.push(conflict(
                    "flow-implements",
                    "Flow implements must reference a known flow shape",
                    vec![flow.owner.to_string()],
                ));
            }
            implementations
                .entry((reference.name.clone(), reference.version.major))
                .or_default()
                .push(flow);
        }
    }
    for flow in flows.values() {
        let mut all = Vec::new();
        steps(&flow.document.steps, &mut all);
        for step in all {
            if let StepKind::Flow(sub) = &step.kind
                && implementations
                    .get(&(sub.implements.name.clone(), sub.implements.version.major))
                    .is_none_or(|c| c.len() != 1)
            {
                errors.push(conflict(
                    "subflow-ambiguous",
                    "subflow implements must resolve exactly one selected Flow",
                    vec![flow.owner.to_string()],
                ));
            }
        }
    }
    // NativeInput routes select exact Flow keys, so sharing an implements view
    // is valid. A shape-only subflow call still has to resolve uniquely above.
    // Guest run-flow consumes are the dynamic counterpart: they resolve the
    // exact implements identity the runtime guest Flow entry requires.
    for (owner, optional, reference) in &dynamic_flows {
        let targets = flows
            .values()
            .filter(|flow| flow.document.implements.as_ref() == Some(reference))
            .count();
        if targets == 0 && !optional {
            errors.push(conflict(
                "required-flow",
                format!("{owner} requires {reference}"),
                vec![owner.to_string()],
            ));
        }
        if targets > 1 {
            errors.push(conflict(
                "flow-ambiguous",
                format!("dynamic consume {reference} resolves {targets} selected Flow endpoints"),
                vec![owner.to_string()],
            ));
        }
    }
    for flow in flows.values() {
        if !shapes
            .get(&flow.document.trigger.input)
            .is_some_and(|view| view.kind == ShapeKind::Port)
        {
            errors.push(conflict(
                "flow-trigger-view",
                "Flow trigger requires an exact verified Port view",
                vec![flow.owner.to_string()],
            ));
        }
    }
    if !errors.is_empty() {
        return Err(errors);
    }
    let profile_value =
        canonical(profile).map_err(|e| vec![conflict("canonical-profile", e, vec![])])?;
    let binding_value = CanonValue::Obj(
        bindings
            .iter()
            .map(|(r, p)| {
                (
                    r.to_string(),
                    CanonValue::Arr(p.iter().map(|id| CanonValue::Str(id.to_string())).collect()),
                )
            })
            .collect(),
    );
    for flow in flows.values_mut() {
        let value =
            canonical(&flow.document).map_err(|e| vec![conflict("canonical-flow", e, vec![])])?;
        let overlays = flow
            .ordered_overlays
            .iter()
            .map(|(step, items)| {
                let items = items
                    .iter()
                    .map(|o| {
                        Ok(CanonValue::Obj(BTreeMap::from([
                            ("identity".into(), CanonValue::Str(overlay_id(o))),
                            (
                                "hash".into(),
                                o.hash
                                    .as_ref()
                                    .map_or(CanonValue::Null, |h| CanonValue::Str(h.to_string())),
                            ),
                            ("document".into(), canonical(&o.document)?),
                        ])))
                    })
                    .collect::<Result<Vec<_>, String>>()?;
                Ok((step.clone(), CanonValue::Arr(items)))
            })
            .collect::<Result<BTreeMap<_, _>, String>>()
            .map_err(|e| vec![conflict("canonical-overlay", e, vec![])])?;
        let value = CanonValue::Obj(BTreeMap::from([
            ("profile".into(), profile_value.clone()),
            ("owner".into(), CanonValue::Str(flow.owner.to_string())),
            ("hash".into(), CanonValue::Str(flow.hash.to_string())),
            ("bindings".into(), binding_value.clone()),
            ("flow".into(), value),
            ("overlays".into(), CanonValue::Obj(overlays)),
        ]));
        flow.plan_digest = tobkiri_canon::digest(&value, Variant::StrictV1)
            .map_err(|e| vec![conflict("canonical-plan", e.to_string(), vec![])])?;
    }
    let plan_value = CanonValue::Obj(BTreeMap::from([
        ("profile".into(), profile_value),
        ("bindings".into(), binding_value),
        (
            "flows".into(),
            CanonValue::Obj(
                flows
                    .iter()
                    .map(|(id, f)| (id.clone(), CanonValue::Str(f.plan_digest.to_string())))
                    .collect(),
            ),
        ),
    ]));
    let plan_digest = tobkiri_canon::digest(&plan_value, Variant::StrictV1)
        .map_err(|e| vec![conflict("canonical-plan", e.to_string(), vec![])])?;
    for (flow_key, flow) in &mut flows {
        let ast_hash = tobkiri_canon::digest(
            &canonical(&flow.document)
                .map_err(|e| vec![conflict("canonical-witness", e, vec![])])?,
            Variant::StrictV1,
        )
        .map_err(|e| vec![conflict("canonical-witness", e.to_string(), vec![])])?;
        append_witnesses(
            &mut flow.call_witnesses,
            &flow.document.steps,
            CodeLocation::Flow,
            CodeOwner::Pack {
                id: flow.owner.clone(),
                hash: flow.hash.clone(),
            },
            &ast_hash,
            &plan_digest,
            flow_key,
            &bindings,
            &selected,
        )?;
        let mut seen = BTreeSet::new();
        for overlay in flow.ordered_overlays.values().flatten() {
            let identity = overlay_id(overlay);
            if !seen.insert(identity.clone()) {
                continue;
            }
            let (code, owner) = match (&overlay.owner, &overlay.hash) {
                (Some(id), Some(hash)) => (
                    CodeLocation::PackOverlay { id: identity },
                    CodeOwner::Pack {
                        id: id.clone(),
                        hash: hash.clone(),
                    },
                ),
                (None, None) => (CodeLocation::UserOverlay { id: identity }, CodeOwner::User),
                _ => {
                    return Err(vec![conflict(
                        "witness-owner",
                        "inconsistent overlay code owner",
                        vec![],
                    )]);
                }
            };
            append_witnesses(
                &mut flow.call_witnesses,
                &overlay.document.body,
                code,
                owner,
                overlay.ast_hash(),
                &plan_digest,
                flow_key,
                &bindings,
                &selected,
            )?;
        }
    }
    let native_inputs = input_functions
        .into_iter()
        .map(|(selection, source_hash, function, arguments)| {
            let flow_routes: Vec<_> = flows
                .iter()
                .filter(|(_, flow)| flow.document.trigger.input == selection.exact_input_view)
                .map(|(key, _)| key.clone())
                .collect();
            // The first physical handle-forwarding slice preserves one typed
            // graph and one shared source cursor through exactly one Flow.
            // Scalars retain the existing zero/multiple-route behavior.
            let payload = match function.result.as_ref() {
                Some(WitType::Result { ok, .. }) => ok.as_deref(),
                result => result,
            };
            if payload.is_some_and(contains_input_handle) && flow_routes.len() != 1 {
                return Err(vec![conflict(
                    "input-handle-routes",
                    "physical Input stream/callback delivery requires exactly one frozen Flow route",
                    vec![selection.source_pack.to_string()],
                )]);
            }
            Ok(CompiledInputEntry {
                selection,
                source_hash,
                profile_revision: plan_digest.clone(),
                function,
                arguments,
                flow_routes,
            })
        })
        .collect::<Result<Vec<_>, Vec<Conflict>>>()?;
    Ok(CompiledProfile {
        profile: profile.clone(),
        native_inputs,
        bindings,
        views: shapes,
        flows,
        plan_digest,
    })
}

#[allow(clippy::too_many_arguments)]
fn append_witnesses(
    registry: &mut BTreeMap<CallNodeKey, CompiledCallWitness>,
    body: &[Step],
    code: CodeLocation,
    owner: CodeOwner,
    ast_hash: &Digest,
    revision: &Digest,
    flow: &str,
    bindings: &BTreeMap<ShapeRef, Vec<PackId>>,
    selected: &BTreeMap<PackId, &VerifiedPack>,
) -> Result<(), Vec<Conflict>> {
    let mut all = Vec::new();
    steps(body, &mut all);
    for step in all {
        let mut requests = Vec::new();
        if let StepKind::Call(call) = &step.kind {
            requests.push((CallNodeKind::Call, call.shape.clone(), call.op.clone()));
        }
        if let Some(route) = &step.stream {
            requests.push((
                CallNodeKind::StreamEmit,
                route.emit.clone(),
                OpName::parse("emit").expect("fixed operation"),
            ));
        }
        for (kind, shape, op) in requests {
            let key = CallNodeKey {
                code: code.clone(),
                node: step.id.clone().ok_or_else(|| {
                    vec![conflict("witness-node", "compiler node ID missing", vec![])]
                })?,
                kind,
            };
            let providers = bindings
                .get(&shape)
                .filter(|p| !p.is_empty())
                .ok_or_else(|| {
                    vec![conflict(
                        "witness-target",
                        format!("no frozen target for {shape}"),
                        vec![],
                    )]
                })?;
            let mut targets = Vec::new();
            for id in providers {
                let pack = selected.get(id).ok_or_else(|| {
                    vec![conflict("witness-target", "target Pack absent", vec![])]
                })?;
                let refs: Vec<_> = pack
                    .manifest
                    .ports
                    .provides
                    .iter()
                    .filter(|p| p.shape == shape.name && p.version.major == shape.version.major)
                    .map(|p| ShapeRef::new(p.shape.clone(), p.version))
                    .collect();
                let provided = if refs.contains(&shape) {
                    shape.clone()
                } else if refs.len() == 1 {
                    refs[0].clone()
                } else {
                    return Err(vec![conflict(
                        "witness-target",
                        "ambiguous provided view within Pack",
                        vec![id.to_string()],
                    )]);
                };
                targets.push(FrozenTarget {
                    pack: id.clone(),
                    hash: pack.hash.clone(),
                    provided,
                });
            }
            let descriptor_id = tobkiri_canon::digest(
                &canonical(&(
                    "tobkiri.compiled-call/1",
                    ast_hash,
                    &owner,
                    revision,
                    flow,
                    &key,
                    &shape,
                    &op,
                    &targets,
                ))
                .map_err(|e| vec![conflict("canonical-witness", e, vec![])])?,
                Variant::StrictV1,
            )
            .map_err(|e| vec![conflict("canonical-witness", e.to_string(), vec![])])?;
            let descriptor = CompiledCallWitness {
                id: descriptor_id,
                ast_hash: ast_hash.clone(),
                owner: owner.clone(),
                profile_revision: revision.clone(),
                flow: flow.into(),
                key: key.clone(),
                shape,
                op,
                targets,
            };
            if registry.insert(key, descriptor).is_some() {
                return Err(vec![conflict(
                    "witness-node",
                    "duplicate compiled call node identity",
                    vec![],
                )]);
            }
        }
    }
    Ok(())
}

fn input_arguments(function: &WitFunction) -> Result<CanonValue, String> {
    let mut arguments = BTreeMap::new();
    for (name, ty) in &function.params {
        // Manifest WIT conversion resolves aliases before this check. Only an
        // actual field-zero record fixes its complete value without user data.
        if !matches!(ty, WitType::Record(fields) if fields.is_empty()) {
            return Err(
                "physical Input entry has required arguments; no implicit defaults are allowed"
                    .into(),
            );
        }
        let value = CanonValue::Obj(BTreeMap::new());
        tobkiri_manifest::validate_json(ty, &value).map_err(|error| error.to_string())?;
        if arguments.insert(name.clone(), value).is_some() {
            return Err("physical Input signature contains duplicate parameter names".into());
        }
    }
    // None, empty records, and result<_, E> remain distinct in function.result.
    // This compiler never creates an ingress payload or normalizes those cases.
    Ok(CanonValue::Obj(arguments))
}
fn contains_input_handle(ty: &WitType) -> bool {
    match ty {
        WitType::StreamHandle | WitType::CallbackHandle => true,
        WitType::List(ty) | WitType::Option(ty) => contains_input_handle(ty),
        WitType::Tuple(types) => types.iter().any(contains_input_handle),
        WitType::Record(fields) => fields.iter().any(|field| contains_input_handle(&field.ty)),
        WitType::Variant(cases) => cases
            .iter()
            .filter_map(|case| case.ty.as_ref())
            .any(contains_input_handle),
        WitType::Result { ok, err } => ok
            .iter()
            .chain(err.iter())
            .any(|ty| contains_input_handle(ty)),
        _ => false,
    }
}
