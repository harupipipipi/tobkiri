//! Identity-only deferral of an active provider successor to the Python Host.
//!
//! This covers sandboxed providers and one non-launch Gateway provider slot.
//! It neither admits external bytes nor loads code or produces descriptors.
//! The caller verifies
//! the signed bundled predecessor first; Host capture must authenticate the
//! successor's signature, CAS, compatibility and complete authority closure
//! before routes or Shell execution become available.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

const GATEWAY_PACK_ID: &str = "rumi_ai_gateway_pack";

#[derive(Clone, Copy, PartialEq, Eq)]
enum SuccessorKind {
    Gateway,
    Sandbox,
}

/// Already captured Profile activation identities and their exact documents.
pub(super) struct ActiveSelection<'a> {
    pub profile: &'a Value,
    pub lock: Option<&'a Value>,
    pub plan: Option<&'a Value>,
    pub profile_revision: Option<&'a str>,
    pub activation_id: Option<&'a str>,
    pub plan_digest: Option<&'a str>,
    pub lock_digest: Option<&'a str>,
}

/// Return only a consistent successor pin; this is never execution authority.
///
/// `protected` includes every catalog Base backend, the selected Base, Shell,
/// Application and launch provider. The catalog digest's predecessor hash
/// verification remains the caller's responsibility and precedes this check.
pub(super) fn deferred_identity(
    selected: &ActiveSelection<'_>,
    pack_id: &str,
    expected: &str,
    predecessor: &Value,
    source_manifest_digest: Option<&str>,
    protected: bool,
) -> Option<String> {
    if protected
        || !valid_digest(expected)
        || !valid_digest(source_manifest_digest?)
        || string(predecessor, "/pack/id") != Some(pack_id)
        || string(predecessor, "/pack_api_version") != Some("io.tobkiri.pack.v4")
    {
        return None;
    }
    let successor_kind = match (
        string(predecessor, "/pack/kind"),
        string(predecessor, "/requirements/execution_boundary"),
    ) {
        (Some("host_extension"), Some("host_brokered")) if pack_id == GATEWAY_PACK_ID => {
            SuccessorKind::Gateway
        }
        (Some("normal_sandbox"), Some("sandbox")) => SuccessorKind::Sandbox,
        _ => return None,
    };
    let previous_digest = string(predecessor, "/pack/artifact_digest")?;
    if !valid_digest(previous_digest) || previous_digest == expected {
        return None;
    }
    let revision = selected.profile_revision?;
    let plan_digest = selected.plan_digest?;
    let lock_digest = selected.lock_digest?;
    if !valid_digest(revision)
        || !valid_digest(plan_digest)
        || !valid_digest(lock_digest)
        || selected.activation_id?.is_empty()
    {
        return None;
    }
    let lock = selected.lock?;
    let plan = selected.plan?;
    if string(lock, "/lock_digest") != Some(lock_digest)
        || string(lock, "/plan_digest") != Some(plan_digest)
        || string(plan, "/plan_digest") != Some(plan_digest)
        || string(lock, "/profile_revision") != Some(revision)
        || string(plan, "/profile_revision") != Some(revision)
    {
        return None;
    }
    let packs = selected.profile.get("packs")?.as_array()?;
    let entry = unique(packs, "/pack_id", pack_id)?;
    if string(entry, "/role") != Some("provider")
        || string(entry, "/artifact_digest") != Some(expected)
    {
        return None;
    }
    for graph in [lock, plan] {
        let effective = graph.get("effective_set")?.as_array()?;
        let pin = unique(effective, "/identity", pack_id)?;
        if string(pin, "/role") != Some("pack") || string(pin, "/artifact_digest") != Some(expected)
        {
            return None;
        }
    }

    let mut variants = BTreeMap::new();
    for pin in lock.get("variant_pins")?.as_array()? {
        if string(pin, "/pack_id") != Some(pack_id) {
            continue;
        }
        if !successor_pin(pin, expected, successor_kind)
            || variants.insert(string(pin, "/variant_id")?, pin).is_some()
        {
            return None;
        }
    }
    if variants.is_empty() {
        return None;
    }
    let mut used_variants = BTreeSet::new();
    for binding in plan.get("bindings")?.as_array()? {
        if string(binding, "/pack_id") != Some(pack_id) {
            continue;
        }
        let principal = binding.get("function_principal")?;
        if !successor_pin(binding, expected, successor_kind)
            || string(principal, "/parent_artifact_digest") != Some(expected)
            || !valid_digest(string(principal, "/function_implementation_digest")?)
            || !valid_digest(string(principal, "/contract_revision_digest")?)
        {
            return None;
        }
        let function_id = string(principal, "/function_id")?;
        let operation = string(binding, "/operation_id")?;
        if function_id.is_empty()
            || operation.is_empty()
            || string(principal, "/operation_id") != Some(operation)
        {
            return None;
        }
        if successor_kind == SuccessorKind::Gateway {
            let functions = predecessor.get("functions")?.as_array()?;
            let function = unique(functions, "/id", function_id)?;
            if string(function, "/role") != Some("host_capability_provider")
                || string(function, "/contract_revision_digest")
                    != string(principal, "/contract_revision_digest")
                || !function
                    .get("operations")?
                    .as_array()?
                    .iter()
                    .any(|id| id.as_str() == Some(operation))
            {
                return None;
            }
        }
        let variant_id = string(binding, "/variant_id")?;
        let pin = variants.get(variant_id)?;
        for field in [
            "artifact_digest",
            "executable_catalog_digest",
            "platform",
            "architecture",
            "runtime_abi",
            "backend",
            "execution_kind",
            "domain_kind",
        ] {
            if pin.get(field) != binding.get(field) {
                return None;
            }
        }
        used_variants.insert(variant_id);
    }
    // Neither a vacant slot nor unbound Lock variants establish same-kind
    // successor evidence. The Host still validates every exact principal.
    (used_variants.len() == variants.len()).then(|| expected.to_owned())
}

fn host_extension_pin(pin: &Value, expected: &str) -> bool {
    string(pin, "/artifact_digest") == Some(expected)
        && string(pin, "/execution_kind") == Some("host_extension")
        && string(pin, "/domain_kind") == Some("dedicated_process")
        && string(pin, "/backend") == Some("tobkiri.python-host-v4")
        && string(pin, "/executable_catalog_digest").is_some_and(valid_digest)
        && ["/variant_id", "/platform", "/architecture", "/runtime_abi"]
            .iter()
            .all(|field| string(pin, field).is_some_and(|value| !value.is_empty()))
}

fn successor_pin(pin: &Value, expected: &str, kind: SuccessorKind) -> bool {
    if kind == SuccessorKind::Gateway {
        return host_extension_pin(pin, expected);
    }
    string(pin, "/artifact_digest") == Some(expected)
        && string(pin, "/executable_catalog_digest").is_some_and(valid_digest)
        && matches!(
            (string(pin, "/execution_kind"), string(pin, "/domain_kind")),
            (Some("pack_vm"), Some("pack_vm" | "dedicated_process"))
                | (Some("wasm"), Some("wasm_component"))
        )
        && string(pin, "/backend")
            .is_some_and(|backend| !backend.is_empty() && backend != "tobkiri.python-host-v4")
        && ["/variant_id", "/platform", "/architecture", "/runtime_abi"]
            .iter()
            .all(|field| string(pin, field).is_some_and(|value| !value.is_empty()))
}

fn unique<'a>(items: &'a [Value], field: &str, identity: &str) -> Option<&'a Value> {
    let mut matches = items
        .iter()
        .filter(|item| string(item, field) == Some(identity));
    let item = matches.next()?;
    matches.next().is_none().then_some(item)
}

fn string<'a>(value: &'a Value, pointer: &str) -> Option<&'a str> {
    value.pointer(pointer).and_then(Value::as_str)
}

fn valid_digest(value: &str) -> bool {
    value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct Fixture {
        profile: Value,
        lock: Value,
        plan: Value,
        predecessor: Value,
        artifact: String,
        revision: String,
        plan_digest: String,
        lock_digest: String,
        source_digest: String,
    }

    fn digest(byte: char) -> String {
        format!("sha256:{}", byte.to_string().repeat(64))
    }

    impl Fixture {
        fn new() -> Self {
            let artifact = digest('a');
            let revision = digest('b');
            let plan_digest = digest('c');
            let lock_digest = digest('d');
            let source_digest = digest('e');
            let pin = json!({
                "pack_id": GATEWAY_PACK_ID, "artifact_digest": artifact,
                "variant_id": "gateway.generate.python", "platform": "any",
                "architecture": "any", "runtime_abi": "python3.13",
                "backend": "tobkiri.python-host-v4", "execution_kind": "host_extension",
                "domain_kind": "dedicated_process", "executable_catalog_digest": digest('f')
            });
            let mut binding = pin.clone();
            binding["operation_id"] = json!("gateway.generate");
            binding["function_principal"] = json!({
                "parent_artifact_digest": artifact,
                "function_implementation_digest": digest('1'),
                "function_id": "gateway.generate", "operation_id": "gateway.generate",
                "contract_revision_digest": digest('2')
            });
            let effective = json!([{
                "identity": GATEWAY_PACK_ID, "role": "pack", "artifact_digest": artifact
            }]);
            Self {
                profile: json!({"packs": [{
                    "pack_id": GATEWAY_PACK_ID, "role": "provider", "artifact_digest": artifact
                }]}),
                lock: json!({
                    "profile_revision": revision, "lock_digest": lock_digest,
                    "plan_digest": plan_digest, "effective_set": effective,
                    "variant_pins": [pin]
                }),
                plan: json!({
                    "profile_revision": revision, "plan_digest": plan_digest,
                    "effective_set": effective, "bindings": [binding]
                }),
                predecessor: json!({
                    "pack_api_version": "io.tobkiri.pack.v4",
                    "pack": {"id": GATEWAY_PACK_ID, "kind": "host_extension", "artifact_digest": digest('3')},
                    "requirements": {"execution_boundary": "host_brokered"},
                    "functions": [{"id": "gateway.generate", "role": "host_capability_provider",
                        "contract_revision_digest": digest('2'), "operations": ["gateway.generate"]}]
                }),
                artifact,
                revision,
                plan_digest,
                lock_digest,
                source_digest,
            }
        }

        fn sandbox() -> Self {
            let mut fixture = Self::new();
            let pack_id = "normal.provider";
            fixture.predecessor["pack"]["id"] = json!(pack_id);
            fixture.predecessor["pack"]["kind"] = json!("normal_sandbox");
            fixture.predecessor["requirements"]["execution_boundary"] = json!("sandbox");
            fixture.predecessor["functions"][0]["role"] = json!("pure");
            fixture.profile["packs"][0]["pack_id"] = json!(pack_id);
            for graph in [&mut fixture.lock, &mut fixture.plan] {
                graph["effective_set"][0]["identity"] = json!(pack_id);
            }
            for pin in [
                &mut fixture.lock["variant_pins"][0],
                &mut fixture.plan["bindings"][0],
            ] {
                pin["pack_id"] = json!(pack_id);
                pin["execution_kind"] = json!("pack_vm");
                pin["backend"] = json!("tobkiri.python-pack-v4");
                pin["variant_id"] = json!("normal.changed.python");
            }
            let binding = &mut fixture.plan["bindings"][0];
            binding["operation_id"] = json!("normal.changed-operation");
            binding["function_principal"]["function_id"] = json!("normal.changed-function");
            binding["function_principal"]["operation_id"] = json!("normal.changed-operation");
            binding["function_principal"]["contract_revision_digest"] = json!(digest('4'));
            fixture
        }

        fn selection(&self) -> ActiveSelection<'_> {
            ActiveSelection {
                profile: &self.profile,
                lock: Some(&self.lock),
                plan: Some(&self.plan),
                profile_revision: Some(&self.revision),
                activation_id: Some("activation:gateway"),
                plan_digest: Some(&self.plan_digest),
                lock_digest: Some(&self.lock_digest),
            }
        }

        fn deferred(&self) -> Option<String> {
            deferred_identity(
                &self.selection(),
                self.predecessor["pack"]["id"].as_str().unwrap(),
                &self.artifact,
                &self.predecessor,
                Some(&self.source_digest),
                false,
            )
        }
    }

    #[test]
    fn exact_active_gateway_successor_is_identity_only_deferred() {
        let fixture = Fixture::new();
        assert_eq!(fixture.deferred(), Some(fixture.artifact.clone()));
        let mut unchanged = Fixture::new();
        unchanged.predecessor["pack"]["artifact_digest"] = json!(unchanged.artifact);
        assert_eq!(unchanged.deferred(), None);
    }

    #[test]
    fn sandbox_successor_preserves_reviewed_new_function_and_variant_identities() {
        for (execution, domain, backend, abi) in [
            ("pack_vm", "pack_vm", "tobkiri.python-pack-v4", "python3.13"),
            (
                "pack_vm",
                "dedicated_process",
                "tobkiri.python-pack-v4",
                "python3.14",
            ),
            (
                "wasm",
                "wasm_component",
                "tobkiri.wasmtime-pulley-v1",
                "component-v1",
            ),
        ] {
            let mut fixture = Fixture::sandbox();
            for pin in [
                &mut fixture.lock["variant_pins"][0],
                &mut fixture.plan["bindings"][0],
            ] {
                pin["execution_kind"] = json!(execution);
                pin["domain_kind"] = json!(domain);
                pin["backend"] = json!(backend);
                pin["runtime_abi"] = json!(abi);
                pin["executable_catalog_digest"] = json!(digest('5'));
            }
            assert_eq!(fixture.deferred(), Some(fixture.artifact.clone()));
        }
        let mut unchanged = Fixture::sandbox();
        unchanged.predecessor["pack"]["artifact_digest"] = json!(unchanged.artifact);
        assert_eq!(unchanged.deferred(), None);
    }

    #[test]
    fn sandbox_successor_cannot_defer_protected_or_host_provider_slots() {
        let fixture = Fixture::sandbox();
        assert_eq!(
            deferred_identity(
                &fixture.selection(),
                "normal.provider",
                &fixture.artifact,
                &fixture.predecessor,
                Some(&fixture.source_digest),
                true,
            ),
            None
        );
        for (kind, boundary) in [
            ("host_extension", "host_brokered"),
            ("application", "declarative_only"),
            ("normal_sandbox", "host_brokered"),
            ("normal_sandbox", "declarative_only"),
        ] {
            let mut changed = Fixture::sandbox();
            changed.predecessor["pack"]["kind"] = json!(kind);
            changed.predecessor["requirements"]["execution_boundary"] = json!(boundary);
            assert_eq!(changed.deferred(), None, "{kind}:{boundary}");
        }
    }

    #[test]
    fn sandbox_pins_reject_host_remote_and_inconsistent_execution_boundaries() {
        for (execution, domain, backend) in [
            (
                "host_extension",
                "dedicated_process",
                "tobkiri.python-host-v4",
            ),
            ("remote", "remote", "tobkiri.remote-pack-v4"),
            ("pack_vm", "wasm_component", "tobkiri.python-pack-v4"),
            ("wasm", "dedicated_process", "tobkiri.wasmtime-pulley-v1"),
            ("pack_vm", "dedicated_process", "tobkiri.python-host-v4"),
            ("pack_vm", "dedicated_process", ""),
        ] {
            let mut fixture = Fixture::sandbox();
            for pin in [
                &mut fixture.lock["variant_pins"][0],
                &mut fixture.plan["bindings"][0],
            ] {
                pin["execution_kind"] = json!(execution);
                pin["domain_kind"] = json!(domain);
                pin["backend"] = json!(backend);
            }
            assert_eq!(fixture.deferred(), None, "{execution}:{domain}:{backend}");
        }
    }

    #[test]
    fn sandbox_successor_requires_exact_lock_plan_pins_and_principal() {
        for field in [
            "artifact_digest",
            "executable_catalog_digest",
            "variant_id",
            "backend",
            "platform",
            "architecture",
            "runtime_abi",
            "execution_kind",
            "domain_kind",
        ] {
            let mut fixture = Fixture::sandbox();
            fixture.plan["bindings"][0][field] = json!("different");
            assert_eq!(fixture.deferred(), None, "{field}");
        }
        for (field, value) in [
            ("parent_artifact_digest", json!(digest('9'))),
            ("function_implementation_digest", json!("invalid")),
            ("contract_revision_digest", json!("invalid")),
            ("function_id", json!("")),
            ("operation_id", json!("normal.other-operation")),
        ] {
            let mut fixture = Fixture::sandbox();
            fixture.plan["bindings"][0]["function_principal"][field] = value;
            assert_eq!(fixture.deferred(), None, "{field}");
        }
        let mut fixture = Fixture::sandbox();
        fixture.plan["bindings"][0]["operation_id"] = json!("");
        fixture.plan["bindings"][0]["function_principal"]["operation_id"] = json!("");
        assert_eq!(fixture.deferred(), None);
    }

    #[test]
    fn sandbox_successor_rejects_stale_duplicate_missing_and_unbound_evidence() {
        for mutation in 0..9 {
            let mut fixture = Fixture::sandbox();
            match mutation {
                0 => fixture.profile["packs"][0]["artifact_digest"] = json!(digest('9')),
                1 => fixture.profile["packs"][0]["role"] = json!("application"),
                2 => {
                    let row = fixture.profile["packs"][0].clone();
                    fixture.profile["packs"].as_array_mut().unwrap().push(row);
                }
                3 => fixture.lock["effective_set"][0]["artifact_digest"] = json!(digest('9')),
                4 => fixture.plan["effective_set"] = json!([]),
                5 => fixture.plan["bindings"] = json!([]),
                6 => fixture.lock["variant_pins"] = json!([]),
                7 => {
                    let row = fixture.lock["variant_pins"][0].clone();
                    fixture.lock["variant_pins"]
                        .as_array_mut()
                        .unwrap()
                        .push(row);
                }
                _ => {
                    let mut row = fixture.lock["variant_pins"][0].clone();
                    row["variant_id"] = json!("normal.unbound");
                    fixture.lock["variant_pins"]
                        .as_array_mut()
                        .unwrap()
                        .push(row);
                }
            }
            assert_eq!(fixture.deferred(), None, "{mutation}");
        }
    }

    #[test]
    fn only_catalogued_non_launch_gateway_slot_can_be_deferred() {
        let fixture = Fixture::new();
        let selected = fixture.selection();
        assert_eq!(
            deferred_identity(
                &selected,
                GATEWAY_PACK_ID,
                &fixture.artifact,
                &fixture.predecessor,
                Some(&fixture.source_digest),
                true
            ),
            None
        );
        for other in [
            "defaultspack",
            "tobkiri_host_pack_control",
            "authority.bridge",
            "runtime.tauri.application.default",
            "shell.tauri.default",
            "other.extension",
        ] {
            assert_eq!(
                deferred_identity(
                    &selected,
                    other,
                    &fixture.artifact,
                    &fixture.predecessor,
                    Some(&fixture.source_digest),
                    false
                ),
                None,
                "{other}"
            );
        }
        for source in [None, Some("not-a-digest")] {
            assert_eq!(
                deferred_identity(
                    &selected,
                    GATEWAY_PACK_ID,
                    &fixture.artifact,
                    &fixture.predecessor,
                    source,
                    false
                ),
                None
            );
        }
    }

    #[test]
    fn bootstrap_or_incomplete_activation_has_no_deferral() {
        let fixture = Fixture::new();
        for missing in 0..6 {
            let mut selected = fixture.selection();
            match missing {
                0 => selected.profile_revision = None,
                1 => selected.activation_id = None,
                2 => selected.plan_digest = None,
                3 => selected.lock_digest = None,
                4 => selected.lock = None,
                _ => selected.plan = None,
            }
            assert_eq!(
                deferred_identity(
                    &selected,
                    GATEWAY_PACK_ID,
                    &fixture.artifact,
                    &fixture.predecessor,
                    Some(&fixture.source_digest),
                    false
                ),
                None,
                "{missing}"
            );
        }
        let mut selected = fixture.selection();
        selected.activation_id = Some("");
        assert_eq!(
            deferred_identity(
                &selected,
                GATEWAY_PACK_ID,
                &fixture.artifact,
                &fixture.predecessor,
                Some(&fixture.source_digest),
                false
            ),
            None
        );
    }

    #[test]
    fn predecessor_must_be_the_same_host_extension_slot() {
        for (pointer, replacement) in [
            ("/pack/id", json!("other.extension")),
            ("/pack/kind", json!("normal_sandbox")),
            ("/pack_api_version", json!("io.tobkiri.pack.v3")),
            ("/pack/artifact_digest", json!("invalid")),
            ("/requirements/execution_boundary", json!("sandbox")),
        ] {
            let mut fixture = Fixture::new();
            *fixture.predecessor.pointer_mut(pointer).unwrap() = replacement;
            assert_eq!(fixture.deferred(), None, "{pointer}");
        }
    }

    #[test]
    fn profile_and_both_graphs_require_unique_exact_role_and_digest() {
        for document in 0..3 {
            for mutation in 0..4 {
                let mut fixture = Fixture::new();
                let (array, role) = match document {
                    0 => (fixture.profile["packs"].as_array_mut().unwrap(), "provider"),
                    1 => (
                        fixture.lock["effective_set"].as_array_mut().unwrap(),
                        "pack",
                    ),
                    _ => (
                        fixture.plan["effective_set"].as_array_mut().unwrap(),
                        "pack",
                    ),
                };
                assert_eq!(array[0]["role"], role);
                match mutation {
                    0 => array[0]["artifact_digest"] = json!(digest('9')),
                    1 => array[0]["role"] = json!("application"),
                    2 => array.push(array[0].clone()),
                    _ => array.clear(),
                }
                assert_eq!(fixture.deferred(), None, "{document}:{mutation}");
            }
        }
    }

    #[test]
    fn active_document_identities_must_match_the_capture() {
        for (document, field) in [
            (0, "lock_digest"),
            (0, "plan_digest"),
            (0, "profile_revision"),
            (1, "plan_digest"),
            (1, "profile_revision"),
        ] {
            let mut fixture = Fixture::new();
            let target = if document == 0 {
                &mut fixture.lock
            } else {
                &mut fixture.plan
            };
            target[field] = json!(digest('9'));
            assert_eq!(fixture.deferred(), None, "{document}:{field}");
        }
    }

    #[test]
    fn both_binding_and_variant_require_exact_host_extension_execution() {
        for document in 0..2 {
            for (field, replacement) in [
                ("artifact_digest", json!(digest('9'))),
                ("execution_kind", json!("pack_vm")),
                ("domain_kind", json!("pack_vm")),
                ("backend", json!("tobkiri.remote-pack-v4")),
                ("executable_catalog_digest", json!("invalid")),
                ("variant_id", json!("")),
                ("platform", json!("")),
                ("architecture", json!("")),
                ("runtime_abi", json!("")),
            ] {
                let mut fixture = Fixture::new();
                let pin = if document == 0 {
                    &mut fixture.lock["variant_pins"][0]
                } else {
                    &mut fixture.plan["bindings"][0]
                };
                pin[field] = replacement;
                assert_eq!(fixture.deferred(), None, "{document}:{field}");
            }
        }
        for field in [
            "executable_catalog_digest",
            "variant_id",
            "platform",
            "architecture",
            "runtime_abi",
        ] {
            let mut fixture = Fixture::new();
            fixture.plan["bindings"][0][field] = json!("different");
            assert_eq!(fixture.deferred(), None, "{field}");
        }
    }

    #[test]
    fn bindings_and_variants_must_supply_nonempty_matching_evidence() {
        for mutation in 0..5 {
            let mut fixture = Fixture::new();
            match mutation {
                0 => fixture.plan["bindings"] = json!([]),
                1 => fixture.lock["variant_pins"] = json!([]),
                2 => {
                    let pin = fixture.lock["variant_pins"][0].clone();
                    fixture.lock["variant_pins"]
                        .as_array_mut()
                        .unwrap()
                        .push(pin);
                }
                3 => {
                    let mut pin = fixture.lock["variant_pins"][0].clone();
                    pin["variant_id"] = json!("gateway.unbound.python");
                    fixture.lock["variant_pins"]
                        .as_array_mut()
                        .unwrap()
                        .push(pin);
                }
                _ => fixture.plan["bindings"][0]["pack_id"] = json!("other.extension"),
            }
            assert_eq!(fixture.deferred(), None, "{mutation}");
        }
    }

    #[test]
    fn exact_function_principal_stays_bound_to_predecessor_contract_and_operation() {
        for (pointer, replacement) in [
            ("/parent_artifact_digest", json!(digest('9'))),
            ("/function_implementation_digest", json!("invalid")),
            ("/function_id", json!("authority.bridge")),
            ("/contract_revision_digest", json!(digest('9'))),
            ("/operation_id", json!("gateway.other")),
        ] {
            let mut fixture = Fixture::new();
            *fixture.plan["bindings"][0]["function_principal"]
                .pointer_mut(pointer)
                .unwrap() = replacement;
            assert_eq!(fixture.deferred(), None, "{pointer}");
        }
        for field in ["function_principal", "operation_id"] {
            let mut fixture = Fixture::new();
            fixture.plan["bindings"][0]
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert_eq!(fixture.deferred(), None, "{field}");
        }
        for mutation in 0..3 {
            let mut fixture = Fixture::new();
            match mutation {
                0 => fixture.predecessor["functions"][0]["role"] = json!("brokered"),
                1 => fixture.predecessor["functions"][0]["operations"] = json!([]),
                _ => {
                    let function = fixture.predecessor["functions"][0].clone();
                    fixture.predecessor["functions"]
                        .as_array_mut()
                        .unwrap()
                        .push(function);
                }
            }
            assert_eq!(fixture.deferred(), None, "{mutation}");
        }
    }
}
