//! The original App activation store's first recovery branch on a held workspace.
use super::*;
use std::ops::ControlFlow;
use tobkiri_canon::Variant;

#[path = "original_recovery_pending.rs"]
pub(super) mod pending;

pub(crate) const APP_DENIED: &str =
    "ecosystem.defaultspack.domain.profile_resolution.ProfileResolutionDenied";

pub(super) struct RecoveryWorkspace {
    state: Directory,
    workspace_digest: Digest,
    lock_name: LockName,
}

pub(super) struct ExistingRecoveryWorkspace(RecoveryWorkspace);

impl RecoveryWorkspace {
    pub(super) fn open<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        workspace: &Path,
        lock_name: LockName,
    ) -> Result<Self> {
        Self::open_with_mode(op, workspace, lock_name, false)
    }

    /// Capture an existing activation state directory for read-only inspection.
    pub(super) fn open_existing<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        workspace: &Path,
        lock_name: LockName,
    ) -> Result<ExistingRecoveryWorkspace> {
        Self::open_with_mode(op, workspace, lock_name, true).map(ExistingRecoveryWorkspace)
    }

    fn open_with_mode<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        workspace: &Path,
        lock_name: LockName,
        existing_only: bool,
    ) -> Result<Self> {
        let (state, workspace_digest) = initialize(op, workspace, existing_only)?;
        Ok(Self {
            state,
            workspace_digest,
            lock_name,
        })
    }

    /// Hold the original workspace lock while state recovery awaits Pack work.
    /// `Break` returns to artifact verification only after the lock has closed.
    pub(super) async fn recover_async<S: OriginalAuthorityOwner, B, T>(
        &self,
        op: &Operation<'_, S>,
        on_state: impl AsyncFnOnce(
            &Directory,
            &Digest,
            Option<Funded<Document>>,
        ) -> Result<ControlFlow<B, T>>,
    ) -> Result<ControlFlow<B, T>> {
        let guard = PublicationLock::acquire_named(op, &self.state, self.lock_name)?;
        let result = match self.read_pending(op) {
            Ok(pending) => on_state(&self.state, &self.workspace_digest, pending).await,
            Err(error) => Err(error),
        };
        self.finish_lock(op, guard, result, ControlFlow::is_continue)
    }

    fn read_pending<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
    ) -> Result<Option<Funded<Document>>> {
        self.state
            .exists(op, pending::NAME)?
            .then(|| pending::read(op, &self.state, &self.workspace_digest))
            .transpose()
    }

    pub(super) fn republish<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        publish: impl FnOnce(&Directory, &Digest) -> Result<()>,
    ) -> Result<()> {
        let guard = PublicationLock::acquire_named(op, &self.state, self.lock_name)?;
        let outcome = publish(&self.state, &self.workspace_digest);
        self.finish_lock(op, guard, outcome, |_| true)
    }

    fn finish_lock<S: OriginalAuthorityOwner, T>(
        &self,
        op: &Operation<'_, S>,
        mut guard: PublicationLock,
        outcome: Result<T>,
        validate_success: impl FnOnce(&T) -> bool,
    ) -> Result<T> {
        let result = outcome.and_then(|reached| {
            if validate_success(&reached) {
                guard.validate(op, &self.state, self.lock_name)?;
            }
            Ok(reached)
        });
        finish_scope(result, guard.close())
    }
}

impl ExistingRecoveryWorkspace {
    /// Hold the original workspace lock while inspecting the current activation.
    pub(super) async fn inspect_current<S: OriginalAuthorityOwner, T>(
        &self,
        op: &Operation<'_, S>,
        inspect: impl AsyncFnOnce(&Directory, &Digest) -> Result<T>,
    ) -> Result<T> {
        let workspace = &self.0;
        let guard = PublicationLock::acquire_existing(op, &workspace.state, workspace.lock_name)?;
        let outcome = match workspace.state.exists(op, pending::NAME) {
            Ok(false) => inspect(&workspace.state, &workspace.workspace_digest).await,
            Ok(true) => Err(raw(
                APP_DENIED,
                "activation workspace has a pending journal",
            )),
            Err(error) => Err(error),
        };
        workspace.finish_lock(op, guard, outcome, |_| true)
    }
}

#[cfg(test)]
pub(super) fn ready_test<T>(future: impl Future<Output = T>) -> T {
    use std::task::{Context, Poll, Waker};
    let mut future = std::pin::pin!(future);
    match future
        .as_mut()
        .poll(&mut Context::from_waker(Waker::noop()))
    {
        Poll::Ready(result) => result,
        Poll::Pending => panic!("fixture has no suspending recovery operation"),
    }
}

#[cfg(test)]
pub(super) fn recover<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    workspace: &Path,
    lock_name: LockName,
    on_state: impl FnOnce(&Directory, Option<&Document>) -> Result<Option<T>>,
) -> Result<Option<T>> {
    let recovery = RecoveryWorkspace::open(op, workspace, lock_name)?;
    ready_test(recovery.recover_async(op, async |state, _, journal| {
        Ok(match on_state(state, journal.as_deref())? {
            Some(value) => ControlFlow::Break(value),
            None => ControlFlow::Continue(()),
        })
    }))
    .map(ControlFlow::break_value)
}

fn initialize<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    workspace: &Path,
    existing_only: bool,
) -> Result<(Directory, Digest)> {
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
    let state = if existing_only {
        Directory::capture_existing_private(op, &state_root)
    } else {
        Directory::capture(op, &state_root, true)
    };
    let state = state.map_err(|error| {
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
    Ok((state, workspace_digest))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{lock_free, operation, with_source};
    use std::{
        cell::Cell,
        future::{Future, pending, poll_fn},
        os::unix::fs::{PermissionsExt, symlink},
        task::{Context, Poll, Waker},
    };

    fn inspect<S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        workspace: &Path,
        name: LockName,
    ) -> Result<Option<()>> {
        recover(op, workspace, name, |_, _| Ok(Some(())))
    }

    #[tokio::test(flavor = "current_thread")]
    async fn async_recovery_keeps_the_workspace_lock_until_return_or_drop() {
        struct ObserveCallbackDrop<'a>(&'a Path, &'a Cell<bool>);
        impl Drop for ObserveCallbackDrop<'_> {
            fn drop(&mut self) {
                self.1.set(!lock_free(self.0));
            }
        }

        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            fs::create_dir_all(&workspace).unwrap();
            let name = LockName::workspace("profile");
            let lock = workspace.join("activation").join(name.text());
            let op = operation(read, source, cancellation);
            let recovery = RecoveryWorkspace::open(&op, &workspace, name).unwrap();
            let before = read.receipt();
            let mut cx = Context::from_waker(Waker::noop());
            let resume = Cell::new(false);

            let mut completed =
                std::pin::pin!(recovery.recover_async(&op, async |state, _, journal| {
                    assert!(journal.is_none());
                    poll_fn(|_| {
                        if resume.get() {
                            Poll::Ready(())
                        } else {
                            Poll::Pending
                        }
                    })
                    .await;
                    assert!(!lock_free(&lock));
                    assert!(!state.exists(&op, pending::NAME)?);
                    Ok(ControlFlow::<(), ()>::Break(()))
                }));
            assert!(completed.as_mut().poll(&mut cx).is_pending());
            assert!(!lock_free(&lock));
            assert_eq!(read.receipt().open_files, before.open_files + 1);
            resume.set(true);
            assert!(matches!(
                completed.as_mut().poll(&mut cx),
                Poll::Ready(Ok(ControlFlow::Break(())))
            ));
            assert!(lock_free(&lock));
            assert_eq!(read.receipt(), before);

            let locked_at_callback_drop = Cell::new(false);
            {
                let mut suspended = std::pin::pin!(recovery.recover_async(&op, async |_, _, _| {
                    let _observe = ObserveCallbackDrop(&lock, &locked_at_callback_drop);
                    pending::<Result<ControlFlow<()>>>().await
                }));
                assert!(suspended.as_mut().poll(&mut cx).is_pending());
                assert!(!lock_free(&lock));
            }
            assert!(locked_at_callback_drop.get());
            assert!(lock_free(&lock));
            assert_eq!(read.receipt(), before);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_constructor_creates_private_state_and_preserves_existing_records() {
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

            assert_eq!(
                initialize(&op, &workspace, false).unwrap().1,
                expected_digest
            );
            assert_eq!(fs::read_dir(&state_root).unwrap().count(), 0);
            assert_eq!(
                fs::metadata(&state_root).unwrap().permissions().mode() & 0o777,
                0o700
            );

            let pending = state_root.join("pending.json");
            fs::write(&pending, b"existing bytes").unwrap();
            fs::set_permissions(&state_root, fs::Permissions::from_mode(0o755)).unwrap();
            initialize(&op, &workspace, false).unwrap();
            assert_eq!(fs::read(&pending).unwrap(), b"existing bytes");
            assert_eq!(fs::read_dir(&state_root).unwrap().count(), 1);
            assert_eq!(
                fs::metadata(&state_root).unwrap().permissions().mode() & 0o777,
                0o700
            );
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_constructor_checks_state_symlink_before_workspace_symlink() {
        with_source(|source, read, cancellation, _| {
            let target = source.root.join("workspace");
            let alias = source.root.join("alias");
            fs::create_dir_all(&target).unwrap();
            symlink(&target, &alias).unwrap();
            let state_alias = target.join("activation");
            symlink("missing", &state_alias).unwrap();
            let op = operation(read, source, cancellation);
            let name = LockName::workspace("profile");

            let error = inspect(&op, &alias, name).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "state_root must not be a symlink");
            assert!(error.cause.is_none());

            fs::remove_file(&state_alias).unwrap();
            let error = inspect(&op, &alias, name).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "workspace_root must not be a symlink");
            assert!(error.cause.is_none());
            assert_eq!(fs::read_dir(&target).unwrap().count(), 0);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_constructor_refuses_workspace_files_and_redirected_state_ancestors() {
        with_source(|source, read, cancellation, _| {
            let actual = source.root.join("actual");
            fs::create_dir_all(actual.join("profile")).unwrap();
            let workspace_file = source.root.join("workspace-file");
            fs::write(&workspace_file, b"workspace file").unwrap();
            let alias = source.root.join("alias");
            symlink(&actual, &alias).unwrap();
            let op = operation(read, source, cancellation);
            let name = LockName::workspace("profile");

            let missing = source.root.join("missing-workspace");
            let error = inspect(&op, &missing, name).unwrap_err();
            assert_eq!(error.class, RAW_IO);
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Io(error))
                    if error.kind == std::io::ErrorKind::NotFound
            ));
            assert!(!missing.exists());

            let error = inspect(&op, &workspace_file, name).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "workspace_root must be a directory");
            assert!(error.cause.is_none());

            let error = inspect(&op, &alias.join("profile"), name).unwrap_err();
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

    #[tokio::test(flavor = "current_thread")]
    async fn existing_workspace_requires_the_state_directory_and_original_lock() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state_root = workspace.join("activation");
            let op = operation(read, source, cancellation);
            let name = LockName::workspace("profile");
            let before = read.receipt();

            let error = RecoveryWorkspace::open_existing(&op, &workspace, name)
                .err()
                .expect("missing workspace");
            assert_eq!(error.class, RAW_IO);
            assert!(!workspace.exists());

            fs::create_dir_all(&workspace).unwrap();
            let error = RecoveryWorkspace::open_existing(&op, &workspace, name)
                .err()
                .expect("missing activation state");
            assert_eq!(error.message, "activation state root is unsafe");
            assert!(!state_root.exists());

            fs::create_dir(&state_root).unwrap();
            fs::set_permissions(&state_root, fs::Permissions::from_mode(0o700)).unwrap();
            let existing = RecoveryWorkspace::open_existing(&op, &workspace, name).unwrap();
            let result: Result<()> = ready_test(
                existing.inspect_current(&op, async |_, _| panic!("lock must already exist")),
            );
            assert_eq!(
                result.unwrap_err().message,
                "activation process lock is unavailable"
            );
            assert_eq!(fs::read_dir(&state_root).unwrap().count(), 0);
            assert_eq!(read.receipt(), before);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn existing_workspace_refuses_pending_without_reading_or_rewriting_it() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state_root = workspace.join("activation");
            let op = operation(read, source, cancellation);
            let name = LockName::workspace("profile");
            let before = read.receipt();
            fs::create_dir_all(&workspace).unwrap();
            recover(&op, &workspace, name, |_, _| Ok(Some(()))).unwrap();
            let journal = state_root.join(pending::NAME);
            let lock = state_root.join(name.text());
            fs::write(&journal, b"incomplete JSON").unwrap();
            fs::set_permissions(&journal, fs::Permissions::from_mode(0o000)).unwrap();
            let existing = RecoveryWorkspace::open_existing(&op, &workspace, name).unwrap();
            let result: Result<()> = ready_test(
                existing.inspect_current(&op, async |_, _| panic!("pending journal must stop us")),
            );
            assert_eq!(
                result.unwrap_err().message,
                "activation workspace has a pending journal"
            );
            assert_eq!(
                fs::metadata(&journal).unwrap().permissions().mode() & 0o777,
                0o000
            );
            fs::set_permissions(&journal, fs::Permissions::from_mode(0o600)).unwrap();
            assert_eq!(fs::read(&journal).unwrap(), b"incomplete JSON");
            assert!(lock_free(&lock));
            assert_eq!(read.receipt(), before);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn existing_workspace_preserves_state_and_lock_metadata_while_inspecting() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state_root = workspace.join("activation");
            let op = operation(read, source, cancellation);
            let name = LockName::workspace("profile");
            let before = read.receipt();
            fs::create_dir_all(&workspace).unwrap();
            recover(&op, &workspace, name, |_, _| Ok(Some(()))).unwrap();
            let record = state_root.join("active.json");
            let lock = state_root.join(name.text());
            fs::write(&record, b"existing activation").unwrap();
            fs::write(&lock, b"existing lock").unwrap();
            fs::set_permissions(&lock, fs::Permissions::from_mode(0o400)).unwrap();
            fs::set_permissions(&state_root, fs::Permissions::from_mode(0o500)).unwrap();
            let workspace_digest = Digest::hash(
                &serde_json::to_vec(&serde_json::json!({
                    "workspace_root": workspace.canonicalize().unwrap().to_str().unwrap(),
                }))
                .unwrap(),
            );
            let metadata = |path: &Path| {
                let value = fs::metadata(path).unwrap();
                (
                    value.ino(),
                    value.mode(),
                    value.len(),
                    value.mtime(),
                    value.mtime_nsec(),
                    value.ctime(),
                    value.ctime_nsec(),
                )
            };
            let tracked_paths = [&state_root, &record, &lock];
            let before_metadata = tracked_paths.map(|path| metadata(path));
            let observer = fs::File::open(&lock).unwrap();
            let existing = RecoveryWorkspace::open_existing(&op, &workspace, name).unwrap();
            ready_test(existing.inspect_current(&op, async |state, digest| {
                assert_eq!(
                    unsafe { libc::flock(observer.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
                    -1
                );
                assert_eq!(
                    state.read(&op, "active.json")?.as_slice(),
                    b"existing activation"
                );
                assert_eq!(digest, &workspace_digest);
                Ok(())
            }))
            .unwrap();
            assert_eq!(
                unsafe { libc::flock(observer.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
                0
            );
            assert_eq!(
                unsafe { libc::flock(observer.as_raw_fd(), libc::LOCK_UN) },
                0
            );
            assert_eq!(fs::read(&lock).unwrap(), b"existing lock");
            assert_eq!(tracked_paths.map(|path| metadata(path)), before_metadata);
            assert_eq!(read.receipt(), before);
            fs::set_permissions(&state_root, fs::Permissions::from_mode(0o700)).unwrap();
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn existing_workspace_refuses_unsafe_permissions_without_repairing_them() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state_root = workspace.join("activation");
            let op = operation(read, source, cancellation);
            let name = LockName::workspace("profile");
            fs::create_dir_all(&workspace).unwrap();
            recover(&op, &workspace, name, |_, _| Ok(Some(()))).unwrap();
            let lock = state_root.join(name.text());

            fs::set_permissions(&state_root, fs::Permissions::from_mode(0o755)).unwrap();
            let error = RecoveryWorkspace::open_existing(&op, &workspace, name)
                .err()
                .expect("unsafe activation state permissions");
            assert_eq!(error.message, "activation state root is unsafe");
            assert_eq!(fs::metadata(&state_root).unwrap().mode() & 0o777, 0o755);
            fs::set_permissions(&state_root, fs::Permissions::from_mode(0o700)).unwrap();

            let existing = RecoveryWorkspace::open_existing(&op, &workspace, name).unwrap();
            fs::set_permissions(&lock, fs::Permissions::from_mode(0o666)).unwrap();
            let result: Result<()> = ready_test(
                existing.inspect_current(&op, async |_, _| panic!("unsafe lock permissions")),
            );
            assert_eq!(
                result.unwrap_err().message,
                "activation process lock is unavailable"
            );
            assert_eq!(fs::metadata(&lock).unwrap().mode() & 0o777, 0o666);
            assert!(lock_free(&lock));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn existing_workspace_refuses_a_replaced_lock_and_releases_the_original() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state_root = workspace.join("activation");
            let op = operation(read, source, cancellation);
            let name = LockName::workspace("profile");
            let before = read.receipt();
            fs::create_dir_all(&workspace).unwrap();
            recover(&op, &workspace, name, |_, _| Ok(Some(()))).unwrap();
            let lock = state_root.join(name.text());
            let moved = state_root.join("moved-lock");
            let existing = RecoveryWorkspace::open_existing(&op, &workspace, name).unwrap();

            let result = ready_test(existing.inspect_current(&op, async |_, _| {
                assert!(!lock_free(&lock));
                fs::rename(&lock, &moved).unwrap();
                fs::write(&lock, b"replacement").unwrap();
                fs::set_permissions(&lock, fs::Permissions::from_mode(0o600)).unwrap();
                Ok(())
            }));
            assert_eq!(
                result.unwrap_err().message,
                "persistence open file identity changed"
            );
            assert_eq!(fs::read(&lock).unwrap(), b"replacement");
            assert!(lock_free(&moved));
            assert_eq!(read.receipt(), before);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_pending_read_validates_only_original_shape_and_does_not_mutate() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            fs::create_dir_all(&workspace).unwrap();
            let state = workspace.join("activation");
            let pending = state.join("pending.json");
            let name = LockName::workspace("準備-profile");
            let lock = state.join(".activation-e9d4221cd9afd87c40881ca3.lock");
            let op = operation(read, source, cancellation);

            assert_eq!(
                recover(&op, &workspace, name, |_, _| {
                    assert!(!lock_free(&lock));
                    Ok(None::<()>)
                })
                .unwrap(),
                None
            );
            assert_eq!(
                fs::metadata(&lock).unwrap().permissions().mode() & 0o777,
                0o600
            );
            assert_eq!(fs::read_dir(&state).unwrap().count(), 1);
            assert!(lock_free(&lock));

            let resolved = workspace.canonicalize().unwrap();
            let digest = Digest::hash(
                &serde_json::to_vec(
                    &serde_json::json!({"workspace_root": resolved.to_str().unwrap()}),
                )
                .unwrap(),
            );
            let valid = serde_json::json!({
                "schema": "io.tobkiri.defaultspack-pending-activation.v1",
                "reservation_id": null,
                "activation_id": ["not", "yet", "interpreted"],
                "envelope_path": "../deferred",
                "envelope_digest": true,
                "workspace_digest": digest.as_str(),
                "fencing_token": {},
            });
            let mut invalid = vec![serde_json::json!([])];
            let mut missing = valid.clone();
            missing.as_object_mut().unwrap().remove("fencing_token");
            invalid.push(missing);
            for (key, value) in [
                ("extra", serde_json::json!(null)),
                ("schema", serde_json::json!("wrong")),
                (
                    "workspace_digest",
                    serde_json::json!(Digest::hash(b"another workspace").as_str()),
                ),
            ] {
                let mut changed = valid.clone();
                changed[key] = value;
                invalid.push(changed);
            }
            for value in invalid {
                let data = serde_json::to_vec(&value).unwrap();
                fs::write(&pending, &data).unwrap();
                let error =
                    recover::<_, ()>(&op, &workspace, name, |_, _| panic!("journal exists"))
                        .unwrap_err();
                assert_eq!(error.class, APP_DENIED);
                assert_eq!(error.message, "pending activation journal is invalid");
                assert!(error.cause.is_none());
                assert_eq!(fs::read(&pending).unwrap(), data);
                assert!(lock_free(&lock));
            }

            let data = serde_json::to_vec(&valid).unwrap();
            fs::write(&pending, &data).unwrap();
            assert_eq!(
                recover(&op, &workspace, name, |_, journal| {
                    assert!(journal.is_some());
                    Ok(Some(()))
                })
                .unwrap(),
                Some(())
            );
            assert_eq!(fs::read(&pending).unwrap(), data);
            assert_eq!(fs::read_dir(&state).unwrap().count(), 2);
            assert!(lock_free(&lock));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_pending_read_preserves_strict_decoder_cause_and_unwinds_its_lock() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state = workspace.join("activation");
            let pending = state.join("pending.json");
            let name = LockName::workspace("profile");
            let op = operation(read, source, cancellation);
            fs::create_dir_all(&state).unwrap();
            for (data, message, nested) in [
                (
                    b"{".as_slice(),
                    "invalid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
                    Some("json.decoder.JSONDecodeError"),
                ),
                (
                    b"\xff".as_slice(),
                    "json input is not valid UTF-8",
                    Some("builtins.UnicodeDecodeError"),
                ),
                (
                    br#"{"schema":null,"schema":null}"#.as_slice(),
                    "duplicate JSON object key: 'schema'",
                    None,
                ),
            ] {
                fs::write(&pending, data).unwrap();
                let error =
                    recover::<_, ()>(&op, &workspace, name, |_, _| panic!("journal exists"))
                        .unwrap_err();
                assert_eq!(error.class, APP_DENIED);
                assert_eq!(
                    error.message,
                    format!("pending activation journal is unavailable: {message}")
                );
                assert!(error.suppressed);
                let Some(OriginalAuthorityCause::Python {
                    class,
                    message: inner,
                    cause,
                    suppressed,
                    ..
                }) = error.cause
                else {
                    panic!("missing strict decoder cause");
                };
                assert_eq!(class, "tobkiri_protocol.errors.CanonicalizationError");
                assert_eq!(inner, message);
                assert_eq!(suppressed, nested.is_some());
                assert_eq!(
                    cause.as_deref().map(|cause| match cause {
                        OriginalAuthorityCause::Python { class, .. } => *class,
                        _ => panic!("unexpected strict decoder cause type"),
                    }),
                    nested
                );
                assert_eq!(fs::read(&pending).unwrap(), data);
                assert!(lock_free(&state.join(name.text())));
            }
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_pending_read_preserves_os_error_from_an_unreadable_record() {
        // A privileged runner can open the fixture despite its mode.
        if unsafe { libc::geteuid() } == 0 {
            return;
        }
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state = workspace.join("activation");
            let pending = state.join("pending.json");
            let name = LockName::workspace("profile");
            let op = operation(read, source, cancellation);
            fs::create_dir_all(&state).unwrap();
            fs::write(&pending, b"{}").unwrap();
            fs::set_permissions(&pending, fs::Permissions::from_mode(0o000)).unwrap();

            let result = recover::<_, ()>(&op, &workspace, name, |_, _| panic!("journal exists"));
            fs::set_permissions(&pending, fs::Permissions::from_mode(0o600)).unwrap();
            let error = result.unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(
                error.message,
                "pending activation journal is unavailable: [Errno 13] Permission denied: 'pending.json'"
            );
            assert!(error.suppressed);
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Io(io))
                    if io.errno == Some(libc::EACCES)
                        && io.filename.as_deref() == Some("pending.json")
            ));
            assert_eq!(fs::read(&pending).unwrap(), b"{}");
            assert!(lock_free(&state.join(name.text())));
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn recovery_pending_presence_refuses_a_directory_and_releases_its_lock() {
        with_source(|source, read, cancellation, _| {
            let workspace = source.root.join("workspace");
            let state = workspace.join("activation");
            let pending = state.join("pending.json");
            fs::create_dir_all(&pending).unwrap();
            let name = LockName::workspace("profile");
            let op = operation(read, source, cancellation);

            let error = inspect(&op, &workspace, name).unwrap_err();
            assert_eq!(error.class, SECURE_PERSISTENCE_ERROR);
            assert_eq!(error.message, "persistence entry identity is unsafe");
            assert!(pending.is_dir());
            let lock = state.join(name.text());
            assert!(lock_free(&lock));

            fs::remove_file(&lock).unwrap();
            fs::create_dir(&lock).unwrap();
            let error = inspect(&op, &workspace, name).unwrap_err();
            assert_eq!(error.class, APP_DENIED);
            assert_eq!(error.message, "activation process lock is unavailable");
            assert!(matches!(
                error.cause,
                Some(OriginalAuthorityCause::Python {
                    class: SECURE_PERSISTENCE_ERROR,
                    ..
                })
            ));
        });
    }
}
