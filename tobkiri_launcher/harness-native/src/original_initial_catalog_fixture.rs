//! Bind the original publication oracle's selected Bundle documents to its physical Shell.
use super::*;

const FILES: [&str; 3] = [
    "reread-source.json",
    "reread-shell.json",
    "reread-pack.json",
];
const INVENTORY: &[u8] = br#"{"entries":[{"kind":"profile"},{"kind":"shell"},{"kind":"pack"}]}"#;

/// Recreate the consumed loader state from the oracle's three relevant rows.
/// The lifecycle test supplies the verification scope and real artifact tree.
pub(in crate::original_authority::io::publication) fn load<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    witness: &Path,
    artifact_release: &Path,
) -> Result<CatalogIo> {
    load_catalog(op, budget, witness, artifact_release, false)
}

pub(in crate::original_authority::io::publication) fn resolved_review<
    S: crate::BootstrapReviewOwner,
>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    witness: &Path,
    artifact_release: &Path,
    candidate: &CoreValue,
) -> Result<CatalogIo> {
    let mut io = load_catalog(op, budget, witness, artifact_release, true)?;
    io.begin_registered_pack_resolve("defaults", candidate)?;
    // The fixture supplies the earlier resolver occurrence. The caller under
    // test still reopens and verifies the original physical Shell separately.
    io.pack_resolve = PackResolve::ShellArtifactReady;
    Ok(io)
}

fn load_catalog<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    witness: &Path,
    artifact_release: &Path,
    resolved_review: bool,
) -> Result<CatalogIo> {
    const PATH: OriginalAuthorityStage = OriginalAuthorityStage::Path;
    let witness_len = witness.as_os_str().len();
    let release_len = artifact_release.as_os_str().len();
    if witness_len > 8192 || release_len > 8192 {
        return Err(OriginalAuthorityError::capacity(PATH));
    }
    let _paths = op.reserve(
        PATH,
        witness_len + release_len + libc::PATH_MAX as usize * 4,
    )?;
    op.check(PATH)?;
    let artifact_root = fs::canonicalize(artifact_release)
        .map_err(|error| raw_io(error, artifact_release.to_str()))?;
    if !artifact_root.is_dir() || artifact_root.parent().is_none() {
        return Err(OriginalAuthorityError::unavailable());
    }
    op.check(PATH)?;

    // The oracle exports the three selected documents. Use their fixed order
    // for the private retention machinery. Selected and artifact roots are
    // siblings, as in the original loader. Verification uses the physical
    // artifact root and the retained documents.
    let selected = artifact_root.with_file_name("oracle-bundle");
    let mut io = CatalogIo::new(
        op.owner,
        op.invocation,
        op.cancellation,
        op.cleanup.clone(),
        witness,
        budget,
    )?;
    io.select_packaged(&selected)?;
    if resolved_review {
        io.retain_review_pack_dependencies();
    } else {
        io.retain_publication_inputs();
    }
    io.bind_loader(
        CoreValue::string("initial-publication-oracle", budget)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?,
    )?;
    io.lock = Some(json::decode_catalog_lock(INVENTORY, op, budget)?);
    for name in FILES {
        let path = witness.join(name);
        if path.is_symlink() {
            return Err(OriginalAuthorityError::unavailable());
        }
        let occurrence = read_file(op, &path, budget)?;
        io.retain_pack_pin(op, &occurrence.value)?;
        io.retain_shell_input(op, occurrence)?;
        io.entry += 1;
    }
    io.artifact_parent_identity =
        shell_root::artifact_parent_identity(op, &selected, &artifact_root, budget)?;
    if io.artifact_parent_identity.is_none() {
        return Err(OriginalAuthorityError::unavailable());
    }
    io.artifact_root = Some(artifact_root);
    io.loader = Loader::Done;
    if !io.completed() {
        return Err(OriginalAuthorityError::unavailable());
    }
    Ok(io)
}
