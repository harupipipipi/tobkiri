use std::collections::{BTreeMap, BTreeSet};
use tobkiri_canon::{
    Digest,
    ids::{InputSourceId, OpName, ProfileId, ShapeRef},
};
use tobkiri_gate::{
    CallCx, CoreIngressPolicy, FrozenPortTarget, GateEngine, IngressPolicyRegistration,
    IngressRoute, InputSourceBinding, PermissionSet, Principal, SourceReadMetadata,
};
use tobkiri_manifest::VerifiedPack;
use tobkiri_profile::{CompiledInputEntry, CompiledProfile};

use crate::CoreError;

pub(crate) type NativeIngressKey = (InputSourceId, ShapeRef, OpName);
pub(crate) type NativeIngresses = BTreeMap<NativeIngressKey, IngressPolicyRegistration>;

/// Resolve one Gate route back to exactly one Flow selected by this frozen
/// Input record. The registered full endpoint identity and its exact key are
/// checked; no route key or target is inferred from a caller-supplied label.
pub(crate) fn route_flow_key<'a>(
    compiled: &'a CompiledProfile,
    entry: &CompiledInputEntry,
    route: &IngressRoute,
) -> Result<&'a str, CoreError> {
    if entry.profile_revision() != &compiled.plan_digest
        || !compiled
            .input_entries()
            .iter()
            .any(|candidate| std::ptr::eq(candidate, entry))
    {
        return Err(CoreError::Invalid(
            "native Input route is outside this frozen revision".into(),
        ));
    }
    let target = &route.target;
    let key = target.flow_key.as_str();
    if !entry.flow_routes().iter().any(|candidate| candidate == key) {
        return Err(CoreError::Invalid(
            "native Input route key is absent from the frozen trigger plan".into(),
        ));
    }
    let (canonical_key, flow) = compiled
        .flows
        .get_key_value(key)
        .ok_or_else(|| CoreError::Invalid("native Input route Flow is not frozen".into()))?;
    let implements = flow.document.implements.as_ref().ok_or_else(|| {
        CoreError::Invalid("native Input route Flow has no implements endpoint".into())
    })?;
    if target.profile != compiled.profile.id
        || target.revision != compiled.plan_digest
        || target.owner != flow.owner
        || target.owner_hash != flow.hash
        || target.flow_key.as_str() != key
        || target.plan_digest != flow.plan_digest
        || target.implements != *implements
        || route.implements != *implements
        || target.input_view != *entry.exact_input_view()
        || flow.document.trigger.input != *entry.exact_input_view()
    {
        return Err(CoreError::Invalid(
            "native Input route differs from the frozen trigger plan".into(),
        ));
    }
    // Return the canonical key owned by the frozen CompiledProfile. The
    // route's key is only a lookup input and must not escape its registration.
    Ok(canonical_key.as_str())
}

/// Produce a stable invocation identifier from the actual one-shot source
/// receipt using fixed stack storage. It is an identity label only; Gate still
/// requires the nonclone receipt and exact registered route policy.
pub(crate) fn native_invocation_identity(
    receipt: &SourceReadMetadata,
) -> Result<Digest, CoreError> {
    let mut bytes = [0u8; 1024];
    let mut at = 0usize;
    let mut push = |part: &[u8]| -> Result<(), CoreError> {
        let end = at
            .checked_add(8)
            .and_then(|next| next.checked_add(part.len()))
            .filter(|end| *end <= bytes.len())
            .ok_or_else(|| CoreError::Invalid("native invocation identity overflow".into()))?;
        bytes[at..at + 8].copy_from_slice(&(part.len() as u64).to_le_bytes());
        bytes[at + 8..end].copy_from_slice(part);
        at = end;
        Ok(())
    };
    push(b"tobkiri.native-input-invocation/1")?;
    push(receipt.source.as_str().as_bytes())?;
    push(receipt.view.name.as_str().as_bytes())?;
    push(&receipt.view.version.major.to_le_bytes())?;
    push(&receipt.view.version.minor.to_le_bytes())?;
    push(receipt.op.as_str().as_bytes())?;
    push(receipt.read_grant.as_str().as_bytes())?;
    push(receipt.frame.as_str().as_bytes())?;
    push(&receipt.activation.to_le_bytes())?;
    push(receipt.path.as_str().as_bytes())?;
    push(receipt.payload.as_str().as_bytes())?;
    Ok(Digest::hash(&bytes[..at]))
}

/// Register only explicit compiled Input records and their exact frozen
/// trigger routes. Bindings describe topology; the Gate independently checks
/// source and caller permissions at admission time.
pub(crate) fn register_inputs(
    gate: &GateEngine,
    compiled: &CompiledProfile,
    packs: &[VerifiedPack],
    cx: &CallCx,
) -> Result<NativeIngresses, CoreError> {
    if cx.profile() != &compiled.profile.id || cx.revision() != Some(&compiled.plan_digest) {
        return Err(CoreError::Invalid(
            "input policy revision does not match the Session".into(),
        ));
    }

    let mut ingress = NativeIngresses::new();
    for source_pack in compiled
        .input_entries()
        .iter()
        .map(|entry| entry.source_pack())
        .collect::<BTreeSet<_>>()
    {
        let pack = packs
            .iter()
            .find(|pack| &pack.manifest.id == source_pack)
            .ok_or_else(|| CoreError::NotFound("startup input Pack is missing".into()))?;
        if !compiled.profile.packs.iter().any(|pin| {
            pin.id == pack.manifest.id
                && pin.sha256 == pack.hash
                && pin.version == pack.manifest.version
        }) {
            return Err(CoreError::Invalid(
                "input Pack does not match its frozen Profile pin".into(),
            ));
        }
        let entries = compiled
            .input_entries()
            .iter()
            .filter(|entry| entry.source_pack() == source_pack)
            .collect::<Vec<_>>();
        if entries.is_empty() || entries.len() > 256 {
            return Err(CoreError::Invalid(
                "frozen Input source entry count is invalid".into(),
            ));
        }

        let pack_cx = cx.try_child(Principal::Pack {
            pack: pack.manifest.id.clone(),
            hash: pack.hash.clone(),
        })?;
        let mut allowed = gate
            .effective_permissions(&pack_cx)?
            .intersect(&pack.manifest.permissions)
            .intersect(&compiled.profile.ceiling);
        if let Some(explicit_limits) = compiled.profile.pack_limits.get(source_pack) {
            allowed = allowed.intersect(explicit_limits);
        } else {
            return Err(CoreError::Invalid(
                "Input Pack has no explicit resource limit".into(),
            ));
        }

        let mut entry_views = BTreeSet::new();
        let mut all_routes = Vec::with_capacity(entries.len());
        let mut source_bindings = Vec::with_capacity(entries.len());
        for entry in entries {
            if entry.profile_revision() != &compiled.plan_digest
                || entry.source_hash() != &pack.hash
                || !compiled
                    .bindings
                    .get(entry.exact_input_view())
                    .is_some_and(|providers| providers.contains(source_pack))
            {
                return Err(CoreError::Invalid(
                    "Input record differs from its exact compiled source binding".into(),
                ));
            }
            let source_target = target_for(pack, entry.exact_input_view())?;
            entry_views.insert(entry.exact_input_view().clone());
            let routes = exact_routes(gate, cx, compiled, packs, entry)?;
            all_routes.push((entry, source_target.clone(), routes));
            source_bindings.push(InputSourceBinding {
                revision: compiled.plan_digest.clone(),
                pack: pack.manifest.id.clone(),
                hash: pack.hash.clone(),
                entry_view: entry.exact_input_view().clone(),
                entry_op: entry.operation().clone(),
                target: source_target,
                declaration: pack.manifest.permissions.clone(),
                // Replaced below after the source-wide union is complete.
                projection: PermissionSet::default(),
            });
        }

        let mut projected_ports = entry_views
            .iter()
            .map(ToString::to_string)
            .collect::<BTreeSet<_>>();
        for (_, _, routes) in &all_routes {
            for route in routes {
                let literal = route.implements.to_string();
                if allowed.ports.contains(&literal) {
                    projected_ports.insert(literal);
                }
            }
        }
        let projection = PermissionSet {
            ports: projected_ports,
            wasm: allowed.wasm.clone(),
            ..PermissionSet::default()
        };
        for binding in &mut source_bindings {
            binding.projection = projection.clone();
        }
        let source = InputSourceId::parse(source_pack.as_str())?;
        gate.register_input_sources(&compiled.profile.id, &source, source_bindings)?;

        for (entry, target, routes) in all_routes {
            let policy = CoreIngressPolicy {
                id: ingress_identity(
                    &compiled.plan_digest,
                    &compiled.profile.id,
                    &source,
                    entry.exact_input_view(),
                    entry.operation(),
                )?,
                source: source.clone(),
                view: entry.exact_input_view().clone(),
                op: entry.operation().clone(),
                target,
                routes,
            };
            let registration =
                gate.register_native_ingress(&compiled.profile.id, &compiled.plan_digest, policy)?;
            ingress.insert(
                (
                    source.clone(),
                    entry.exact_input_view().clone(),
                    entry.operation().clone(),
                ),
                registration,
            );
        }
    }
    Ok(ingress)
}

fn exact_routes(
    gate: &GateEngine,
    cx: &CallCx,
    compiled: &CompiledProfile,
    packs: &[VerifiedPack],
    entry: &tobkiri_profile::CompiledInputEntry,
) -> Result<Vec<IngressRoute>, CoreError> {
    if entry.flow_routes().is_empty() {
        return Err(CoreError::Invalid(
            "frozen Input entry has no compiled Flow trigger".into(),
        ));
    }
    let mut routes = Vec::with_capacity(entry.flow_routes().len());
    for key in entry.flow_routes() {
        let flow = compiled
            .flows
            .get(key)
            .ok_or_else(|| CoreError::Invalid("Input trigger Flow is not frozen".into()))?;
        if flow.document.trigger.input != *entry.exact_input_view() {
            return Err(CoreError::Invalid(
                "Input route differs from its frozen trigger view".into(),
            ));
        }
        let implements = flow.document.implements.clone().ok_or_else(|| {
            CoreError::Invalid("Input trigger Flow has no exact implements binding".into())
        })?;
        let owner = packs
            .iter()
            .find(|pack| pack.manifest.id == flow.owner && pack.hash == flow.hash)
            .ok_or_else(|| CoreError::Invalid("Input trigger Flow owner is missing".into()))?;
        let target = gate.resolve_frozen_flow_target(cx, &implements, Some(key))?;
        if target.profile != compiled.profile.id
            || target.revision != compiled.plan_digest
            || target.owner != flow.owner
            || target.owner_hash != flow.hash
            || target.flow_key.as_str() != key
            || target.plan_digest != flow.plan_digest
            || target.implements != implements
            || target.input_view != *entry.exact_input_view()
        {
            return Err(CoreError::Invalid(
                "Input trigger Flow differs from the frozen endpoint catalog".into(),
            ));
        }
        // `owner` is retained as evidence that the target resolves to the
        // exact verified Pack artifact, not an ordinary port binding.
        if owner.manifest.id != target.owner || owner.hash != target.owner_hash {
            return Err(CoreError::Invalid(
                "Input trigger Flow owner differs from the verified Pack".into(),
            ));
        }
        routes.push(IngressRoute { implements, target });
    }
    Ok(routes)
}

fn target_for(pack: &VerifiedPack, requested: &ShapeRef) -> Result<FrozenPortTarget, CoreError> {
    let provided = pack
        .manifest
        .ports
        .provides
        .iter()
        .filter(|port| {
            port.shape == requested.name
                && port.version.major == requested.version.major
                && port.version.minor >= requested.version.minor
        })
        .max_by_key(|port| port.version.minor)
        .ok_or_else(|| CoreError::Invalid("exact frozen Input endpoint is missing".into()))?;
    Ok(FrozenPortTarget {
        pack: pack.manifest.id.clone(),
        hash: pack.hash.clone(),
        provided: ShapeRef::new(provided.shape.clone(), provided.version),
    })
}

fn ingress_identity(
    revision: &Digest,
    profile: &ProfileId,
    source: &InputSourceId,
    view: &ShapeRef,
    op: &OpName,
) -> Result<Digest, CoreError> {
    let mut bytes = [0u8; 1024];
    let mut at = 0usize;
    let mut push = |part: &[u8]| -> Result<(), CoreError> {
        let end = at
            .checked_add(8)
            .and_then(|n| n.checked_add(part.len()))
            .filter(|end| *end <= bytes.len())
            .ok_or_else(|| CoreError::Invalid("Input identity exceeds fixed bound".into()))?;
        bytes[at..at + 8].copy_from_slice(&(part.len() as u64).to_le_bytes());
        bytes[at + 8..end].copy_from_slice(part);
        at = end;
        Ok(())
    };
    push(b"tobkiri.native-ingress/1")?;
    push(revision.as_str().as_bytes())?;
    push(profile.as_str().as_bytes())?;
    push(source.as_str().as_bytes())?;
    push(view.name.as_str().as_bytes())?;
    push(&view.version.major.to_le_bytes())?;
    push(&view.version.minor.to_le_bytes())?;
    push(op.as_str().as_bytes())?;
    Ok(Digest::hash(&bytes[..at]))
}

#[cfg(test)]
#[path = "input_tests.rs"]
mod tests;
