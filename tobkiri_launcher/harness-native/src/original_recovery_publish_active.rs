//! The original Host-global publication after workspace verification.
use super::*;
use workspace_active::ActiveSnapshot;

/// Publish only after the caller has verified the workspace twice, checked the
/// selected package, and matched the confirmed candidate and registered Profile.
pub(super) fn publish<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    scope: &RecoveryScope,
    budget: &Arc<dyn AllocationBudget>,
    active: &ActiveSnapshot,
) -> Result<()> {
    let request = snapshot_request(op, root, budget, active)?;
    scope.validate(op)?;
    let _paths = op.reserve(
        OriginalAuthorityStage::Path,
        root.as_os_str().len().saturating_mul(2).saturating_add(64),
    )?;
    let profiles = root.join("profiles");
    let journal = profiles.join(JOURNAL);
    op.check(OriginalAuthorityStage::Source)?;
    let exists = journal.exists() || journal.is_symlink();
    op.check(OriginalAuthorityStage::Source)?;
    if exists {
        // Python re-enters the journal's logical lock. Here the same thread
        // retains the verified outer guard through this entire operation.
        let changed = || {
            raw(
                recovery_workspace::APP_DENIED,
                "Profile publication candidate changed",
            )
        };
        let mut intent = read_intent(op, &profiles)?.ok_or_else(changed)?;
        if !active.matches_candidate(&intent)? {
            return Err(changed());
        }
        let expected = intent.predecessor(op)?;
        let current = active_profile::load_verified(op, root, budget)?;
        if !published_candidate(current.as_ref(), &intent, expected.as_ref())? {
            let matches = match (current.as_ref(), expected.as_ref()) {
                (None, None) => true,
                (Some(current), Some(expected)) => {
                    current.pointer.matches(&current.doc, expected, &intent.doc)
                }
                _ => false,
            };
            if !matches {
                return Err(raw(
                    recovery_workspace::APP_DENIED,
                    "Profile publication predecessor is stale",
                ));
            }
            scope.validate(op)?;
            active_profile::commit_verified(
                op,
                root,
                &request,
                expected.as_ref().map(|pointer| (&*intent.doc, pointer)),
                publication_time,
                budget,
            )?;
        }
        scope.validate(op)?;
        return scope.directory.clear_journal(op);
    }

    // The original replace_existing=True branch compares with the fresh Host
    // pointer if the already-verified publication has no remaining intent file.
    let current = active_profile::load_verified(op, root, budget)?;
    scope.validate(op)?;
    active_profile::commit_verified(
        op,
        root,
        &request,
        current
            .as_ref()
            .map(|current| (&*current.doc, &current.pointer)),
        publication_time,
        budget,
    )
}

fn snapshot_request<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    budget: &Arc<dyn AllocationBudget>,
    active: &ActiveSnapshot,
) -> Result<CoreValue> {
    let candidate_profile = active.candidate_field("profile_id");
    let profile_id = candidate_profile
        .as_str()
        .expect("validated workspace Profile ID");
    let activation_id = active.candidate_field("activation_id");
    let activation = activation_id.as_str().expect("validated activation ID");
    let suffix = activation.strip_prefix("activation:").unwrap_or(activation);
    let _path = op.reserve(
        OriginalAuthorityStage::Path,
        profile_id
            .len()
            .saturating_add(suffix.len())
            .saturating_add(64),
    )?;
    let relative = format!("workspaces/{profile_id}/activation/activations/{suffix}.json");
    let directory = Directory::capture(op, root, false)?;
    let envelope = (|| {
        let raw = directory.read_relative(op, Path::new(&relative))?;
        json::decode_read_only_strict(&raw, op, budget)
    })()
    .map_err(snapshot_read_error)?;
    if envelope.entries(envelope.root).is_none() {
        return Err(raw(
            recovery_workspace::APP_DENIED,
            "active Profile activation envelope is invalid",
        ));
    }
    let canonical = json::canonical_subtree(&envelope, envelope.root, None, op, budget)?;
    let digest = Digest::hash(&canonical);
    drop((canonical, envelope));
    let capacity = |_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
    let path = CoreValue::string(&relative, budget).map_err(capacity)?;
    let digest = CoreValue::string(digest.as_str(), budget).map_err(capacity)?;
    CoreValue::object_shared(
        [
            Ok(("profile_id", active.activation_profile_id.clone())),
            Ok((
                "profile_revision",
                active.candidate_field("profile_revision"),
            )),
            Ok(("activation_id", activation_id)),
            Ok(("plan_digest", active.candidate_field("plan_digest"))),
            Ok(("lock_digest", active.candidate_field("lock_digest"))),
            Ok(("activation_snapshot_path", path)),
            Ok(("activation_snapshot_digest", digest)),
            Ok(("catalog_revision", active.catalog_revision.clone())),
        ]
        .into_iter(),
        budget,
    )
    .map_err(capacity)
}

fn snapshot_read_error(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == RAW_IO
        || matches!(
            error.class,
            "builtins.FileNotFoundError"
                | "builtins.ValueError"
                | "tobkiri_protocol.errors.CanonicalizationError"
                | SECURE_PERSISTENCE_ERROR
        )
    {
        return caused(
            raw(
                recovery_workspace::APP_DENIED,
                "active Profile activation envelope is unavailable",
            ),
            error,
        );
    }
    error
}

fn publication_time() -> Result<i64> {
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |elapsed| elapsed.as_secs());
    i64::try_from(seconds)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))
}
