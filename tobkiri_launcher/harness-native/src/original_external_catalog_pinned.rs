//! Exact revision selection for the independent executable-catalog read.
use super::*;
use tobkiri_canon::Digest;

pub(super) enum PinnedPackSelection {
    Normal {
        registry: Funded<Document>,
        pack: RetainedPack,
    },
    HostExtensionPending,
}

/// Verify a fresh external-only snapshot and return its exact admitted revision.
/// The Pack ID and digest come from the validated closure manifest. The
/// returned receipt selects a root for further reads; it authenticates no
/// separately read executable catalog.
pub(super) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    user_data: &Path,
    budget: &Arc<dyn AllocationBudget>,
    id: &str,
    digest: &Digest,
) -> Result<PinnedPackSelection> {
    if id.is_empty() {
        return Err(model::denied("external Pack executable request is invalid"));
    }
    let _paths = op.reserve(
        OriginalAuthorityStage::Source,
        12 * (runtime.as_os_str().len() + user_data.as_os_str().len() + 512),
    )?;
    let user_data = resolve_existing_prefix(op, user_data)?;
    let input = read_registry_input(op, &user_data, budget)?;
    let Some((registry, mut inventory)) = authenticate_registry(op, input, budget)? else {
        return Err(model::denied("external Pack is not admitted"));
    };
    if inventory.host_extension_pending {
        return Ok(PinnedPackSelection::HostExtensionPending);
    }

    let mut retained_for_id = false;
    let mut selected = None;
    for (position, pack) in inventory._entries.iter().enumerate() {
        charge(op, budget, id.len().saturating_add(1))?;
        if registry_text(&registry, pack._root.entry, "pack_id").is(id) {
            retained_for_id = true;
            if &pack._artifact_digest == digest {
                // Identical duplicate entries can survive the authenticated
                // scan. Python's assignment keeps the last matching key.
                selected = Some(position);
            }
        }
    }
    if selected.is_none() && retained_for_id {
        // Only an unmatched pin for a Pack with retained revisions consults
        // the generated catalog. A bundled baseline isn't an external receipt.
        let bundled = read_bundled(op, &runtime.join(SOURCE_CATALOG), budget)?;
        let ids = bundled._ids.array().expect("verified bundled inventory");
        let mut bundled_id = false;
        for candidate in ids.iter() {
            charge(op, budget, id.len().saturating_add(1))?;
            if candidate.as_str() == Some(id) {
                bundled_id = true;
                break;
            }
        }
        if !bundled_id {
            return Err(model::denied("selected Pack revision is not admitted"));
        }
    }
    inventory.finish_revision(&registry, op, budget)?;
    let Some(position) = selected else {
        return Err(model::denied("external Pack is not admitted"));
    };
    Ok(PinnedPackSelection::Normal {
        registry,
        pack: inventory._entries.value.swap_remove(position),
    })
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::*;
    use serde_json::Value;

    #[tokio::test(flavor = "current_thread")]
    async fn pinned_registry_selects_the_requested_signed_revision_before_source_fallback() {
        let Some(fixture) = std::env::var_os("TOBKIRI_EXTERNAL_CATALOG_FIXTURE") else {
            return;
        };
        let bytes = fs::read(fixture).unwrap();
        assert!(bytes.len() <= 2 * 1024 * 1024);
        let fixture: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            fixture["format"],
            "io.tobkiri.original-external-pack-catalog-reference.v1"
        );
        let cases = fixture["cases"].as_array().unwrap();
        let retained = cases
            .iter()
            .find(|case| case["name"] == "retained-version")
            .unwrap();
        let runtime = Path::new(retained["runtime_dir"].as_str().unwrap());
        let owner = Path::new(retained["native_user_data"].as_str().unwrap());
        let revisions = retained["retained_probe"]["ordered"].as_array().unwrap();
        assert_eq!(revisions.len(), 2);
        let id = revisions[0]["key"].as_str().unwrap();
        with_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let absent_source = owner.join("absent-runtime");
            assert!(!absent_source.exists());
            for row in revisions {
                let key = row["key"].as_str().unwrap();
                let digest = Digest::parse(row["artifact_digest"].as_str().unwrap()).unwrap();
                let PinnedPackSelection::Normal { registry, pack } =
                    read(&op, &absent_source, owner, budget, id, &digest).unwrap()
                else {
                    panic!("the signed fixture must be a Normal Pack");
                };
                let entries = registry.get(registry.root, "entries").unwrap();
                assert_eq!(registry.get(entries, key), Some(pack._root.entry));
                assert_eq!(pack._artifact_digest, digest);
                assert_eq!(pack._content_digest.as_str(), row["content_digest"]);
                assert_eq!(publication.receipt().open_files, 0);
            }

            let absent_digest = Digest::from_sha256([0; 32]);
            for (runtime, id, class, message) in [
                (
                    runtime,
                    id,
                    EXTERNAL_ERROR,
                    "selected Pack revision is not admitted",
                ),
                (
                    absent_source.as_path(),
                    id,
                    BOUNDARY_ERROR,
                    "canonical Pack v4 catalog is unavailable:",
                ),
                (
                    absent_source.as_path(),
                    "qa.absent-pack",
                    EXTERNAL_ERROR,
                    "external Pack is not admitted",
                ),
            ] {
                let error = read(&op, runtime, owner, budget, id, &absent_digest)
                    .err()
                    .expect("the requested pin is absent");
                assert_eq!(error.class, class);
                assert!(error.message.starts_with(message));
                assert_eq!(publication.receipt().open_files, 0);
            }

            for (name, message) in [
                ("absent", "external Pack is not admitted"),
                (
                    "malformed-hmac",
                    "external Normal Pack catalog authentication failed",
                ),
            ] {
                let case = cases.iter().find(|case| case["name"] == name).unwrap();
                let case_owner = Path::new(case["native_user_data"].as_str().unwrap());
                let error = read(&op, &absent_source, case_owner, budget, id, &absent_digest)
                    .err()
                    .expect("the pinned registry did not authenticate a selection");
                assert_eq!(error.class, EXTERNAL_ERROR);
                assert_eq!(error.message, message);
                assert_eq!(publication.receipt().open_files, 0);
            }
        });
    }
}
