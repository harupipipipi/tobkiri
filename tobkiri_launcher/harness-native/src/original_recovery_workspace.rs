//! The original App activation store's constructor on a held workspace.
use super::*;
use tobkiri_canon::Variant;

const APP_DENIED: &str = "ecosystem.defaultspack.domain.profile_resolution.ProfileResolutionDenied";
const SECURE_PERSISTENCE_ERROR: &str = "tobkiri_protocol.secure_persistence.SecurePersistenceError";

/// Initialize the original activation directory while both the Authority and
/// publication guard are live. The constructor ends before the activation
/// process lock or the first pending journal read.
pub(super) fn construct<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    workspace: &Path,
) -> Result<()> {
    initialize(op, workspace).map(|_| ())
}

fn initialize<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    workspace: &Path,
) -> Result<Digest> {
    let stage = OriginalAuthorityStage::Path;
    let lexical_bytes = workspace
        .as_os_str()
        .len()
        .checked_add("/activation".len())
        .ok_or_else(|| OriginalAuthorityError::capacity(stage))?;
    // Unix realpath is bounded by PATH_MAX. The funded scratch covers the
    // resolved path, its UTF-8 copy, and the canonical encoding's escapes.
    let resolved_bytes = libc::PATH_MAX as usize;
    let _paths = op.reserve(
        stage,
        lexical_bytes.saturating_add(resolved_bytes * 16 + 8192),
    )?;
    let mut state_root = PathBuf::new();
    state_root
        .try_reserve_exact(lexical_bytes)
        .map_err(|_| OriginalAuthorityError::capacity(stage))?;
    state_root.push(workspace);
    state_root.push("activation");
    if state_root.capacity() > lexical_bytes {
        return Err(OriginalAuthorityError::capacity(stage));
    }

    // The source checks the lexical state root before the workspace itself.
    for (path, message) in [
        (state_root.as_path(), "state_root must not be a symlink"),
        (workspace, "workspace_root must not be a symlink"),
    ] {
        if candidate_selection::predecessor_stat(op, path, |path| fs::symlink_metadata(path))?
            .is_some_and(|metadata| metadata.is_symlink())
        {
            return Err(raw(APP_DENIED, message));
        }
    }
    op.check(stage)?;
    let resolved = workspace
        .canonicalize()
        .map_err(|error| raw_io(error, workspace.to_str()))?;
    if resolved.capacity() > resolved_bytes {
        return Err(OriginalAuthorityError::capacity(stage));
    }
    if !candidate_selection::predecessor_stat(op, &resolved, |path| fs::metadata(path))?
        .is_some_and(|metadata| metadata.is_dir())
    {
        return Err(raw(APP_DENIED, "workspace_root must be a directory"));
    }
    // SecureDirectory captures the lexical activation path, not the resolved
    // workspace path. Redirected persistence ancestors must still be refused.
    let _state = Directory::capture(op, &state_root, true).map_err(|error| {
        if error.class == SECURE_PERSISTENCE_ERROR {
            caused(raw(APP_DENIED, "activation state root is unsafe"), error)
        } else {
            error
        }
    })?;
    let workspace_text = resolved.to_str().ok_or_else(|| {
        OriginalAuthorityError::unsupported(stage, "workspace path Unicode encoding exception")
    })?;
    let workspace_digest = tobkiri_canon::digest(
        &object([("workspace_root", CanonValue::Str(workspace_text.to_owned()))]),
        Variant::StrictV1,
    )
    .map_err(|_| OriginalAuthorityError::unavailable())?;
    op.check(stage)?;
    Ok(workspace_digest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};
    use std::os::unix::fs::{PermissionsExt, symlink};

    #[test]
    fn recovery_constructor_creates_private_state_and_preserves_existing_records() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspaces/準備/an-activated-profile");
            let state_root = workspace.join("activation");
            fs::create_dir_all(&workspace).unwrap();
            let op = operation(read, source, cancellation);
            let resolved = workspace.canonicalize().unwrap();
            let expected_bytes = serde_json::to_vec(
                &serde_json::json!({"workspace_root": resolved.to_str().unwrap()}),
            )
            .unwrap();
            let expected_digest = Digest::hash(&expected_bytes);

            assert_eq!(initialize(&op, &workspace).unwrap(), expected_digest);
            assert_eq!(fs::read_dir(&state_root).unwrap().count(), 0);
            assert_eq!(
                fs::metadata(&state_root).unwrap().permissions().mode() & 0o777,
                0o700
            );

            let pending = state_root.join("pending.json");
            fs::write(&pending, b"existing bytes").unwrap();
            fs::set_permissions(&state_root, fs::Permissions::from_mode(0o755)).unwrap();
            construct(&op, &workspace).unwrap();
            assert_eq!(fs::read(&pending).unwrap(), b"existing bytes");
            assert_eq!(fs::read_dir(&state_root).unwrap().count(), 1);
            assert_eq!(
                fs::metadata(&state_root).unwrap().permissions().mode() & 0o777,
                0o700
            );
        });
    }

    #[test]
    fn recovery_constructor_checks_state_symlink_before_workspace_symlink() {
        with_source(|source, read, cancellation, _| {
            let target = source.root.join("workspace");
            let alias = source.root.join("alias");
            fs::create_dir_all(&target).unwrap();
            symlink(&target, &alias).unwrap();
            let state_alias = target.join("activation");
            symlink("missing", &state_alias).unwrap();
            let op = operation(read, source, cancellation);

            let error = construct(&op, &alias).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "state_root must not be a symlink");
            assert!(error.cause.is_none());

            fs::remove_file(&state_alias).unwrap();
            let error = construct(&op, &alias).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "workspace_root must not be a symlink");
            assert!(error.cause.is_none());
            assert_eq!(fs::read_dir(&target).unwrap().count(), 0);
        });
    }

    #[test]
    fn recovery_constructor_refuses_workspace_files_and_redirected_state_ancestors() {
        with_source(|source, read, cancellation, _| {
            let actual = source.root.join("actual");
            fs::create_dir_all(actual.join("profile")).unwrap();
            let workspace_file = source.root.join("workspace-file");
            fs::write(&workspace_file, b"workspace file").unwrap();
            let alias = source.root.join("alias");
            symlink(&actual, &alias).unwrap();
            let op = operation(read, source, cancellation);

            let missing = source.root.join("missing-workspace");
            let error = construct(&op, &missing).unwrap_err();
            assert_eq!(error.class, RAW_IO);
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Io(error))
                    if error.kind == std::io::ErrorKind::NotFound
            ));
            assert!(!missing.exists());

            let error = construct(&op, &workspace_file).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "workspace_root must be a directory");
            assert!(error.cause.is_none());

            let error = construct(&op, &alias.join("profile")).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "activation state root is unsafe");
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Python {
                    class: SECURE_PERSISTENCE_ERROR,
                    ..
                })
            ));
            assert_eq!(fs::read(&workspace_file).unwrap(), b"workspace file");
            assert_eq!(fs::read_dir(actual.join("profile")).unwrap().count(), 0);
        });
    }
}
