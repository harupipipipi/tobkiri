//! Exact revisions used to extend Setup's authenticated registered catalog.
use super::external_catalog_pinned::{bundled_contains, exact_position_text};
use super::*;
use tobkiri_canon::{CoreArray, Digest};

/// Re-observe admitted records using the ordered, unique `[id, digest]` rows
/// from the Profile, then resolve the chosen root using only this Pack's pin
/// before rereading its manifest.
pub(in crate::original_authority::io::publication) fn read_registered_closure_manifest<
    S: crate::BootstrapReviewOwner,
>(
    op: &Operation<'_, S>,
    runtime: &Path,
    user_data: &Path,
    budget: &Arc<dyn AllocationBudget>,
    id: &str,
    pins: &CoreValue,
) -> Result<ClosureManifest> {
    let pins = pins.array().expect("authenticated Profile Pack pin rows");
    let catalog = read_catalog(op, runtime, user_data, budget, |registry, packs| {
        for row in pins.iter() {
            let id = pin_id(&row);
            let id = id.as_str().expect("authenticated Profile Pack ID");
            charge(op, budget, id.len().saturating_add(72))?;
            let digest = pin_value(&row);
            check_retained_pin(op, runtime, budget, registry, packs, id, pin_text(&digest))?;
        }
        Ok(())
    })?;
    if catalog.host_extension_pending() {
        return Ok(ClosureManifest::HostExtension);
    }
    reject_registered_collisions(op, budget, &catalog, &pins)?;
    let pin_value = find_pin(op, budget, &pins, id)?;
    let pin = pin_value.as_ref().map(pin_text);
    let record = match selected(op, budget, &catalog, id, pin)? {
        Some(pack) => {
            let registry = catalog._registry.as_ref().expect("authenticated registry");
            let record = registry.get(pack._root.entry, "catalog_record").unwrap();
            match registry.get(record, "authority") {
                Some(authority)
                    if registry.string_is(authority, "host-signed-external-normal-v4") =>
                {
                    let expected = registry.get(record, "artifact_digest").and_then(|field| {
                        match registry.node(field) {
                            Node::String(text) => parse_record_digest(text),
                            _ => None,
                        }
                    });
                    Record::Normal(expected)
                }
                Some(authority)
                    if registry.string_is(authority, "host-signed-external-host-extension-v4") =>
                {
                    Record::HostExtension
                }
                _ => Record::Bundled,
            }
        }
        None if bundled_contains(op, budget, &catalog._bundled, id)? => Record::Bundled,
        None => {
            return Err(raw(
                "core_runtime.pack_control_v4.PackControlInvalidRequest",
                "Pack is absent from the canonical v4 catalog",
            ));
        }
    };
    drop(catalog);

    let roots = read_catalog(op, runtime, user_data, budget, |registry, packs| {
        if let Some(pin) = pin {
            check_retained_pin(op, runtime, budget, registry, packs, id, pin)?;
        }
        Ok(())
    })?;
    if roots.host_extension_pending() {
        return Ok(ClosureManifest::HostExtension);
    }
    let root = match selected(op, budget, &roots, id, pin)? {
        Some(pack) => &pack._root,
        None if bundled_contains(op, budget, &roots._bundled, id)? => {
            return Ok(ClosureManifest::BundledSource);
        }
        None => {
            let _diagnostic =
                op.reserve(OriginalAuthorityStage::Source, id.len().saturating_add(128))?;
            return Err(raw(
                BOUNDARY_ERROR,
                format!("Pack is absent from the Host v4 catalog: {id}"),
            ));
        }
    };
    match record {
        Record::Normal(expected) => {
            read_normal_closure_manifest(op, budget, root, id, expected.as_ref())
        }
        Record::Bundled => Ok(ClosureManifest::BundledSource),
        Record::HostExtension => Ok(ClosureManifest::HostExtension),
    }
}

enum Record {
    Normal(Option<Digest>),
    Bundled,
    HostExtension,
}

fn pin_id(row: &CoreValue) -> CoreValue {
    row.get_index(0).expect("authenticated Profile Pack pin ID")
}

fn pin_value(row: &CoreValue) -> CoreValue {
    row.get_index(1).expect("authenticated Profile Pack pin")
}

fn pin_text(value: &CoreValue) -> &str {
    value.as_str().expect("retained Profile Pack pin text")
}

fn find_pin<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    pins: &CoreArray<'_>,
    id: &str,
) -> Result<Option<CoreValue>> {
    for row in pins.iter() {
        charge(op, budget, id.len().saturating_add(1))?;
        if pin_id(&row).as_str() == Some(id) {
            return Ok(Some(pin_value(&row)));
        }
    }
    Ok(None)
}

fn parse_record_digest(text: &Text) -> Option<Digest> {
    let mut bytes = [0; 71];
    if text.0.len() != bytes.len() {
        return None;
    }
    for (slot, &point) in bytes.iter_mut().zip(&text.0) {
        *slot = u8::try_from(point).ok()?;
    }
    Digest::parse(std::str::from_utf8(&bytes).ok()?).ok()
}

impl ExternalPackRead {
    fn host_extension_pending(&self) -> bool {
        self.inventory
            .as_ref()
            .is_some_and(|inventory| inventory.host_extension_pending)
    }
}

fn read_catalog<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    user_data: &Path,
    budget: &Arc<dyn AllocationBudget>,
    check_pins: impl FnOnce(&Document, &VerifiedInventory) -> Result<()>,
) -> Result<ExternalPackRead> {
    let input = read_inputs(op, runtime, user_data, budget)?;
    authenticate_external_with(op, input, budget, check_pins)
}

fn check_retained_pin<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    budget: &Arc<dyn AllocationBudget>,
    registry: &Document,
    packs: &VerifiedInventory,
    id: &str,
    pin: &str,
) -> Result<()> {
    let (selected, retained) = exact_position_text(op, budget, registry, packs, id, pin)?;
    if retained && selected.is_none() {
        let bundled = read_bundled(op, &runtime.join(SOURCE_CATALOG), budget)?;
        if !bundled_contains(op, budget, &bundled, id)? {
            return Err(model::denied("selected Pack revision is not admitted"));
        }
    }
    Ok(())
}

fn selected<'catalog, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    catalog: &'catalog ExternalPackRead,
    id: &str,
    pin: Option<&str>,
) -> Result<Option<&'catalog RetainedPack>> {
    let Some(pin) = pin else {
        return catalog.selected(op, budget, id);
    };
    let (Some(registry), Some(packs)) = (&catalog._registry, &catalog.inventory) else {
        return Ok(None);
    };
    let (position, _) = exact_position_text(op, budget, registry, packs, id, pin)?;
    Ok(position.map(|position| &packs._entries[position]))
}

fn reject_registered_collisions<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    catalog: &ExternalPackRead,
    pins: &CoreArray<'_>,
) -> Result<()> {
    let (Some(registry), Some(packs)) = (&catalog._registry, &catalog.inventory) else {
        return Ok(());
    };
    let bundled = &catalog._bundled;
    let ids = bundled._ids.array().expect("verified bundled inventory");
    let length = ids.len().min(packs._entries.len());
    let _funding = op.reserve(
        OriginalAuthorityStage::Source,
        length.saturating_mul(size_of::<&Text>()),
    )?;
    let mut collisions = Vec::new();
    collisions.try_reserve_exact(length).map_err(capacity)?;
    if collisions.capacity() != length {
        return Err(capacity(()));
    }
    for id in ids.iter() {
        let id = id.as_str().expect("verified bundled Pack ID");
        let pin_value = find_pin(op, budget, pins, id)?;
        let pin = pin_value.as_ref().map(pin_text);
        let Some(pack) = selected(op, budget, catalog, id, pin)? else {
            continue;
        };
        let record = registry.get(pack._root.entry, "catalog_record").unwrap();
        let artifact = registry
            .get(record, "artifact_digest")
            .ok_or_else(|| raw("builtins.KeyError", "'artifact_digest'"))?;
        let explicitly_matches = pin.is_some_and(|pin| registry.string_is(artifact, pin));
        if !explicitly_matches || !bundled_allows_normal_shadow(op, budget, bundled, id)? {
            collisions.push(registry_text(registry, pack._root.entry, "pack_id"));
        }
    }
    reject_collisions(op, collisions)
}

fn bundled_allows_normal_shadow<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    bundled: &BundledRecords,
    id: &str,
) -> Result<bool> {
    let doc = &bundled._catalog;
    let Node::Array(records) = doc.node(doc.get(doc.root, "packs").unwrap()) else {
        unreachable!("verified bundled records");
    };
    for &record in records {
        let pack = registry_text(doc, record, "pack_id");
        charge(op, budget, pack.0.len().saturating_add(1))?;
        if trim_id(pack).iter().copied().eq(id.bytes().map(u32::from)) {
            return Ok(doc.get(record, "kind").is_some_and(|kind| {
                doc.string_is(kind, "normal_sandbox") || doc.string_is(kind, "application")
            }));
        }
    }
    unreachable!("verified bundled Pack ID")
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::*;
    use serde_json::{Value, json};

    #[tokio::test(flavor = "current_thread")]
    async fn registered_closure_rereads_exact_retained_manifest_and_checks_unrelated_pins() {
        let Some(path) = std::env::var_os("TOBKIRI_EXTERNAL_CATALOG_FIXTURE") else {
            return;
        };
        let bytes = fs::read(path).unwrap();
        assert!(bytes.len() <= 2 * 1024 * 1024);
        let fixture: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            fixture["format"],
            "io.tobkiri.original-external-pack-catalog-reference.v1"
        );
        let case = fixture["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["name"] == "retained-version")
            .unwrap();
        let runtime = Path::new(case["runtime_dir"].as_str().unwrap());
        let owner = Path::new(case["native_user_data"].as_str().unwrap());
        let entries = case["expected"]["value"]["entries"].as_object().unwrap();
        assert_eq!(entries.len(), 2);
        for entry in entries.values() {
            with_source(|source, publication, cancellation, budget| {
                let op = operation(publication, source, cancellation);
                let id = entry["pack_id"].as_str().unwrap();
                let digest = entry["artifact_digest"].as_str().unwrap();
                let pins = CoreValue::parse_guest(
                    &serde_json::to_vec(&json!([["qa.unlisted", digest], [id, digest]])).unwrap(),
                    budget,
                )
                .unwrap();
                let ClosureManifest::External {
                    manifest,
                    artifact_digest,
                } = read_registered_closure_manifest(&op, runtime, owner, budget, id, &pins)
                    .unwrap()
                else {
                    panic!("the exact signed Normal Pack revision was not admitted");
                };
                assert_eq!(artifact_digest.as_str(), Some(digest));
                let path = owner
                    .join("pack_control")
                    .join(CAS)
                    .join(entry["store_token"].as_str().unwrap())
                    .join("pack.v4.json");
                let expected: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
                let bytes = manifest.encode_guest(budget).unwrap();
                assert_eq!(
                    serde_json::from_slice::<Value>(bytes.as_bytes()).unwrap(),
                    expected
                );
                assert_eq!(publication.receipt().open_files, 0);
            });
        }

        with_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let id = entries.values().next().unwrap()["pack_id"]
                .as_str()
                .unwrap();
            let bundled_id = case["bundled"]["ids"][0].as_str().unwrap();
            let unavailable = Digest::from_sha256([0; 32]);
            for unmatched in [unavailable.as_str(), "unrecognized legacy pin"] {
                let pins = CoreValue::parse_guest(
                    &serde_json::to_vec(&json!([[id, unmatched]])).unwrap(),
                    budget,
                )
                .unwrap();
                for requested in [id, bundled_id] {
                    let error = read_registered_closure_manifest(
                        &op, runtime, owner, budget, requested, &pins,
                    )
                    .err()
                    .expect("the full pin set must reject an unmatched external revision");
                    assert_eq!(error.class, EXTERNAL_ERROR);
                    assert_eq!(error.message, "selected Pack revision is not admitted");
                }
            }
            let empty = CoreValue::parse_guest(b"[]", budget).unwrap();
            assert!(matches!(
                read_registered_closure_manifest(&op, runtime, owner, budget, bundled_id, &empty)
                    .unwrap(),
                ClosureManifest::BundledSource
            ));
            assert_eq!(publication.receipt().open_files, 0);
        });
    }
}
