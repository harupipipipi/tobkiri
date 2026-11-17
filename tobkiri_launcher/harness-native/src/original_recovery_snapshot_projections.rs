//! Revalidate selected Profile projections using the retained runtime owner.
use super::*;
use std::path::Component;

const PROJECTION_ERROR: &str =
    "core_runtime.profile_content_projection.ProfileContentProjectionError";

struct SelectedPack<'a> {
    id: &'a Text,
    digest: Digest,
    source: external_catalog::ProjectionPackSource,
}

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
    let source_count = rows
        .iter()
        .filter(|&&row| selections.at(row).get("source_pack_id").is_some())
        .count();
    if source_count == 0 {
        return op.owner.with_original_catalog_source(|source| {
            verify_selected(op, budget, selections, rows, source.runtime, &[])
        })?;
    }
    op.owner.with_original_catalog_source(|source| {
        op.owner.with_original_review_user_data(|user_data| {
            // The original resolves admitted Pack origins before visiting any projection.
            let packs = resolve_sources(
                op,
                budget,
                selections,
                effective_set,
                (source.runtime, user_data),
                source_count,
            )?;
            verify_selected(op, budget, selections, rows, source.runtime, &packs)
        })?
    })?
}

fn resolve_sources<'a, S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    selections: View<'a>,
    effective_set: View<'_>,
    (runtime, user_data): (&Path, &Path),
    source_count: usize,
) -> Result<Funded<Vec<SelectedPack<'a>>>> {
    let capacity = || OriginalAuthorityError::capacity(GRAPH);
    let reservation = op.reserve(
        GRAPH,
        source_count
            .checked_mul(std::mem::size_of::<SelectedPack<'_>>())
            .ok_or_else(capacity)?,
    )?;
    let mut packs: Vec<SelectedPack<'_>> = Vec::new();
    packs
        .try_reserve_exact(source_count)
        .map_err(|_| capacity())?;
    for &row in selections.items()? {
        let Some(pack) = selections.at(row).get("source_pack_id") else {
            continue;
        };
        let mut pin = None;
        // Python's dictionary comprehension retains the last pin for each identity.
        for &effective in effective_set.items()?.iter().rev() {
            let entry = effective_set.at(effective);
            if pack.same(entry.field("identity")?, op, budget)? {
                pin = Some(entry.field("artifact_digest")?);
                break;
            }
        }
        let pin = pin.ok_or_else(|| {
            raw(
                "builtins.ValueError",
                "projection Pack is outside the selected Profile closure",
            )
        })?;
        let pack_text = definitions::render_python_str(op, budget, pack.doc, pack.id)?;
        let pin = definitions::render_python_str(op, budget, pin.doc, pin.id)?;
        let digest =
            Digest::parse(pin.as_str().expect("strict closure artifact pin")).map_err(|_| {
                raw(
                    "builtins.ValueError",
                    "projection Pack artifact pin is invalid",
                )
            })?;
        let source = external_catalog::resolve_projection_pack(
            op,
            runtime,
            user_data,
            pack_text.as_str().expect("strict Pack identity"),
            &digest,
            budget,
        )?;
        let id = pack.text()?;
        let mut previous = None;
        for value in &mut packs {
            if equal_text(id, value.id, op, budget)? {
                previous = Some(value);
                break;
            }
        }
        let value = SelectedPack { id, digest, source };
        match previous {
            Some(previous) => *previous = value,
            None => packs.push(value),
        }
    }
    Ok(Funded {
        value: packs,
        _capacity: reservation,
    })
}

fn projection_error(message: &'static str) -> OriginalAuthorityError {
    raw(PROJECTION_ERROR, message)
}

fn verify_selected<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    selections: View<'_>,
    rows: &[usize],
    runtime: &Path,
    packs: &[SelectedPack<'_>],
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

        let ((digest, files), path) = match selection.get("source_pack_id") {
            Some(pack) => verify_pack(op, budget, selection, pack, packs)?,
            None => verify_neutral(op, budget, selection, runtime, !packs.is_empty())?,
        };
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

fn verify_neutral<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    selection: View<'_>,
    runtime: &Path,
    absolute_alias_key: bool,
) -> Result<((Digest, usize), Funded<PathBuf>)> {
    if selection.get("source_artifact_digest").is_some() {
        return Err(projection_error("projection Pack identity is missing"));
    }
    let path = selection.field("artifact_root")?;
    let path = definitions::render_python_str(op, budget, path.doc, path.id)?;
    let path = normalized_neutral(op, path.as_str().expect("strict projection path"))?;
    let inventory = definitions::persisted_neutral_inventory(op, runtime, &path, budget)?;
    let path = if absolute_alias_key {
        // Match neutral roots against exact Pack roots as well as other neutral roots.
        let _scratch = op.reserve(GRAPH, 8192 * 12)?;
        let canonical =
            fs::canonicalize(runtime).map_err(|error| raw_io(error, runtime.to_str()))?;
        funded_join(op, &canonical, &path)?
    } else {
        path
    };
    Ok((inventory, path))
}

fn verify_pack<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    selection: View<'_>,
    pack: View<'_>,
    packs: &[SelectedPack<'_>],
) -> Result<((Digest, usize), Funded<PathBuf>)> {
    if selection.get("source_legacy_pack_id").is_some() {
        return Err(projection_error(
            "projection cannot mix Pack and legacy origins",
        ));
    }
    let pin = selection
        .get("source_artifact_digest")
        .ok_or_else(|| projection_error("resolved projection Pack artifact pin is missing"))?;
    if pin.is("") {
        return Err(projection_error(
            "resolved projection Pack artifact pin is missing",
        ));
    }
    let id = pack.text()?;
    let mut selected = None;
    for value in packs {
        if equal_text(id, value.id, op, budget)? {
            selected = Some(value);
            break;
        }
    }
    let stale = || {
        let message = "projection Pack is unselected or its artifact pin is stale";
        caused(
            projection_error(message),
            raw("builtins.ValueError", message),
        )
    };
    let selected = selected.ok_or_else(stale)?;
    if !pin.is(selected.digest.as_str()) {
        return Err(stale());
    }
    let relative = selection.field("artifact_root")?;
    let relative = definitions::render_python_str(op, budget, relative.doc, relative.id)?;
    let relative = relative.as_str().expect("strict projection path");
    if relative.contains('\\')
        || relative
            .split('/')
            .any(|part| matches!(part, "" | "." | ".."))
    {
        let message = "projection Pack content path is unsafe";
        return Err(caused(
            projection_error(message),
            raw("builtins.ValueError", message),
        ));
    }
    let pack = definitions::render_python_str(op, budget, pack.doc, pack.id)?;
    let relative = Path::new(relative);
    let inventory = external_catalog::with_projection_manifest(
        op,
        &selected.source,
        pack.as_str().expect("strict Pack identity"),
        &selected.digest,
        budget,
        |manifest| {
            definitions::persisted_pack_inventory(
                op,
                selected.source.root(),
                selected.source.identity(),
                relative,
                manifest,
                budget,
            )
        },
    )
    .map_err(|error| recompile_error(op, error))?;
    let path = funded_join(op, selected.source.root(), relative)?;
    Ok((inventory, path))
}

fn recompile_error<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    error: OriginalAuthorityError,
) -> OriginalAuthorityError {
    if error.class == RAW_IO {
        return definitions::pack_projection_io_error(op, error);
    }
    if matches!(
        error.class,
        "builtins.ValueError"
            | "builtins.FileNotFoundError"
            | "builtins.UnicodeDecodeError"
            | "json.decoder.JSONDecodeError"
            | "tobkiri_protocol.errors.CanonicalizationError"
            | "tobkiri_protocol.errors.SchemaValidationError"
    ) {
        let _message = match op.reserve(GRAPH, error.message.len()) {
            Ok(reservation) => reservation,
            Err(error) => return error,
        };
        return caused(raw(PROJECTION_ERROR, error.message.clone()), error);
    }
    error
}

fn funded_join<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    relative: &Path,
) -> Result<Funded<PathBuf>> {
    let len = root
        .as_os_str()
        .len()
        .saturating_add(relative.as_os_str().len())
        .saturating_add(1);
    let capacity = op.reserve(GRAPH, len)?;
    let mut path = PathBuf::new();
    path.try_reserve_exact(len)
        .map_err(|_| OriginalAuthorityError::capacity(GRAPH))?;
    path.push(root);
    path.push(relative);
    Ok(Funded {
        value: path,
        _capacity: capacity,
    })
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
    async fn rejects_unselected_pack_before_checking_neutral_projection_paths() {
        with_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let data = br#"{"projections":[{"projection_id":"","artifact_root":"outside"},
                {"projection_id":"pack","source_pack_id":"qa.unselected"}],"effective_set":[]}"#;
            let doc = json::decode_read_only_strict(data, &op, budget).unwrap();
            let root = View {
                doc: &doc,
                id: doc.root,
            };
            let error = verify(
                &op,
                budget,
                root.field("projections").unwrap(),
                root.field("effective_set").unwrap(),
            )
            .unwrap_err();
            assert_eq!(error.class, "builtins.ValueError");
            assert_eq!(
                error.message,
                "projection Pack is outside the selected Profile closure"
            );
            assert_eq!(publication.receipt().open_files, 0);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn verifies_the_original_signed_pack_projection_and_rejects_a_stale_binding() {
        let Some(fixture) = std::env::var_os("TOBKIRI_EXTERNAL_CATALOG_FIXTURE") else {
            return;
        };
        let bytes = fs::read(fixture).unwrap();
        assert!(bytes.len() <= 2 * 1024 * 1024);
        let fixture: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            fixture["format"],
            "io.tobkiri.original-external-pack-catalog-reference.v1"
        );
        let case = fixture["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|case| case["name"] == "admitted")
            .unwrap();
        let runtime = Path::new(case["runtime_dir"].as_str().unwrap());
        let user_data = Path::new(case["native_user_data"].as_str().unwrap());
        let projection = &case["projection_probe"];
        assert_eq!(projection["source_pack_id"], "conformance.minimal.echo");
        for (field, message) in [
            (None, None),
            (
                Some("source_artifact_digest"),
                Some("projection Pack is unselected or its artifact pin is stale"),
            ),
            (
                Some("content_digest"),
                Some("Profile projection binding is stale"),
            ),
        ] {
            with_source(|source, publication, cancellation, budget| {
                let op = operation(publication, source, cancellation);
                let mut selection = projection.clone();
                if let Some(field) = field {
                    selection[field] = json!(Digest::from_sha256([0; 32]).as_str());
                }
                let data = serde_json::to_vec(&json!({
                    "projections": [selection],
                    "effective_set": [{
                        "identity": projection["source_pack_id"],
                        "artifact_digest": projection["source_artifact_digest"],
                    }]
                }))
                .unwrap();
                let doc = json::decode_read_only_strict(&data, &op, budget).unwrap();
                let root = View {
                    doc: &doc,
                    id: doc.root,
                };
                let selections = root.field("projections").unwrap();
                let sources = resolve_sources(
                    &op,
                    budget,
                    selections,
                    root.field("effective_set").unwrap(),
                    (runtime, user_data),
                    1,
                )
                .unwrap();
                let result = verify_selected(
                    &op,
                    budget,
                    selections,
                    selections.items().unwrap(),
                    runtime,
                    &sources,
                );
                if let Some(message) = message {
                    let error = result.unwrap_err();
                    assert_eq!(error.class, PROJECTION_ERROR);
                    assert_eq!(error.message, message);
                    if field == Some("source_artifact_digest") {
                        assert!(matches!(
                            error.cause,
                            Some(OriginalAuthorityCause::Python {
                                class: "builtins.ValueError",
                                ..
                            })
                        ));
                    }
                } else {
                    result.unwrap();
                }
                assert_eq!(publication.receipt().open_files, 0);
            });
        }
    }

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
