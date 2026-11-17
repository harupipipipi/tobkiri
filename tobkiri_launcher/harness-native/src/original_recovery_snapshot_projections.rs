//! Revalidate selected Profile projections using the retained runtime owner.
use super::*;
use std::path::Component;

const PROJECTION_ERROR: &str =
    "core_runtime.profile_content_projection.ProfileContentProjectionError";

pub(super) fn verify<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    selections: View<'_>,
    effective_set: View<'_>,
) -> Result<()> {
    let rows = selections.items()?;
    if rows.is_empty() {
        return Ok(());
    }
    // The original resolves admitted Pack origins before visiting neutral roots.
    for &row in rows {
        if let Some(pack) = selections.at(row).get("source_pack_id") {
            let mut pinned = false;
            for &effective in effective_set.items()? {
                if pack.same(effective_set.at(effective).field("identity")?, op, budget)? {
                    pinned = true;
                    break;
                }
            }
            if !pinned {
                return Err(raw(
                    "builtins.ValueError",
                    "projection Pack is outside the selected Profile closure",
                ));
            }
            return Err(OriginalAuthorityError::unsupported(
                GRAPH,
                "selected admitted Pack projection contents",
            ));
        }
    }
    op.owner.with_original_catalog_source(|source| {
        verify_neutral(op, budget, selections, rows, source.runtime)
    })?
}

fn projection_error(message: &'static str) -> OriginalAuthorityError {
    raw(PROJECTION_ERROR, message)
}

fn verify_neutral<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    selections: View<'_>,
    rows: &[usize],
    runtime: &Path,
) -> Result<()> {
    let capacity = || OriginalAuthorityError::capacity(GRAPH);
    let _indices = op.reserve(
        GRAPH,
        rows.len()
            .checked_mul(std::mem::size_of::<&Text>() + std::mem::size_of::<Funded<PathBuf>>())
            .ok_or_else(capacity)?,
    )?;
    let mut ids: Vec<&Text> = Vec::new();
    let mut paths: Vec<Funded<PathBuf>> = Vec::new();
    ids.try_reserve_exact(rows.len()).map_err(|_| capacity())?;
    paths
        .try_reserve_exact(rows.len())
        .map_err(|_| capacity())?;
    for &row in rows {
        let selection = selections.at(row);
        let id = selection.field("projection_id")?.text()?;
        if id.0.is_empty() {
            return Err(projection_error("Profile projection ID is invalid"));
        }
        for old in &ids {
            if equal_text(id, old, op, budget)? {
                return Err(projection_error("Profile projection ID is invalid"));
            }
        }
        ids.push(id);

        if selection.get("source_artifact_digest").is_some() {
            return Err(projection_error("projection Pack identity is missing"));
        }
        let path = selection.field("artifact_root")?;
        let path_raw = definitions::render_python_str(op, budget, path.doc, path.id)?;
        let path = normalized_neutral(op, path_raw.as_str().expect("strict projection path"))?;
        let (digest, files) = definitions::persisted_neutral_inventory(op, runtime, &path, budget)?;
        if !selection.field("content_digest")?.is(digest.as_str())
            || !matches!(selection.field("file_count")?.node(), Node::Scalar(CanonValue::Int(count))
                if usize::try_from(*count) == Ok(files))
        {
            return Err(projection_error("Profile projection binding is stale"));
        }
        for old in &paths {
            compare_work(
                op,
                budget,
                old.as_os_str().len().min(path.as_os_str().len()) + 1,
            )?;
            if old.as_os_str() == path.as_os_str() {
                return Err(projection_error(
                    "multiple projection IDs alias the same artifact root",
                ));
            }
        }
        paths.push(path);
    }
    Ok(())
}

fn normalized_neutral<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &str,
) -> Result<Funded<PathBuf>> {
    let unsafe_path = || projection_error("Profile projection path is unsafe");
    let mut parts = Path::new(path).components();
    if parts.next() != Some(Component::Normal("profile_projections".as_ref())) {
        return Err(unsafe_path());
    }
    let capacity = op.reserve(GRAPH, path.len())?;
    let mut normalized = PathBuf::new();
    normalized
        .try_reserve_exact(path.len())
        .map_err(|_| OriginalAuthorityError::capacity(GRAPH))?;
    normalized.push("profile_projections");
    for part in parts {
        let Component::Normal(piece) = part else {
            return Err(unsafe_path());
        };
        normalized.push(piece);
    }
    Ok(Funded {
        value: normalized,
        _capacity: capacity,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::BootstrapReviewOwner;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use serde_json::json;

    #[tokio::test(flavor = "current_thread")]
    async fn verifies_nested_neutral_content_and_rejects_aliases_and_symlinks() {
        with_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let runtime = source
                .with_original_catalog_source(|source| source.runtime.to_owned())
                .unwrap();
            let tree = runtime.join("profile_projections/local/nested");
            fs::create_dir_all(&tree).unwrap();
            let member = tree.join("item.txt");
            fs::write(&member, b"resolved profile data").unwrap();
            let entries = json!([{
                "path": "item.txt",
                "digest": Digest::hash(b"resolved profile data").as_str(),
            }]);
            let selection = json!({
                "projection_id": "projection.one",
                "artifact_root": "profile_projections/local/nested",
                "content_digest": Digest::hash(&serde_json::to_vec(&entries).unwrap()).as_str(),
                "file_count": 1,
            });
            let evaluate = |selections| {
                let data = serde_json::to_vec(&json!({
                    "projections": selections,
                    "effective_set": [],
                }))
                .unwrap();
                let doc = json::decode_read_only_strict(&data, &op, budget).unwrap();
                let root = View {
                    doc: &doc,
                    id: doc.root,
                };
                verify(
                    &op,
                    budget,
                    root.field("projections").unwrap(),
                    root.field("effective_set").unwrap(),
                )
            };
            evaluate(vec![selection.clone()]).unwrap();

            let mut alias = selection.clone();
            alias["projection_id"] = json!("projection.two");
            alias["artifact_root"] = json!("profile_projections/local//./nested");
            assert_eq!(
                evaluate(vec![selection.clone(), alias])
                    .unwrap_err()
                    .message,
                "multiple projection IDs alias the same artifact root",
            );
            fs::write(&member, b"changed content").unwrap();
            assert_eq!(
                evaluate(vec![selection.clone()]).unwrap_err().message,
                "Profile projection binding is stale",
            );
            let link = runtime.join("profile_projections/link");
            std::os::unix::fs::symlink(&tree, link).unwrap();
            let mut symlink = selection;
            symlink["artifact_root"] = json!("profile_projections/link");
            assert_eq!(
                evaluate(vec![symlink]).unwrap_err().message,
                "Profile projection path contains a symlink",
            );
        });
    }
}
