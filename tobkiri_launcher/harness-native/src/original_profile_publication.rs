//! The reached prefix of the OLD publication recovery, under its real owner.
//! No intent/key/path/SQL/callback is supplied by a guest. A verified intent is
//! a continuation boundary, never authority to publish or a successful review.
use super::*;
use crate::original_authority::json::{self, Document, Node, Text};
use tobkiri_canon::verify_seal_hex;
use zeroize::Zeroizing;

#[cfg(unix)]
#[path = "original_profile_publication_io.rs"]
mod publication_io;
#[cfg(unix)]
use publication_io::{Directory, PublicationLock, read_stream};
#[path = "original_profile_publication_model.rs"]
mod model;
use model::{Pointer, validate_pointer};
#[cfg(unix)]
#[path = "original_active_profile.rs"]
mod active_profile;
#[path = "original_setup_candidate_selection.rs"]
mod candidate_selection;
#[path = "original_profile_catalog_io.rs"]
pub(crate) mod catalog;
#[path = "original_profile_definitions.rs"]
pub(crate) mod definitions;
#[cfg(unix)]
#[path = "original_development_handover.rs"]
mod development_handover;
#[cfg(unix)]
#[path = "original_external_catalog_io.rs"]
pub(crate) mod external_catalog;
pub(crate) use candidate_selection::{CandidateSelection, ResolverAuthority};

const JOURNAL: &str = "bootstrap-publication.json";
const LOCK: &str = ".bootstrap-publication.lock";
const KEY: &str = ".publication-key";
const RAW_IO: &str = "tobkiri.native.PublicationRawIo";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Prefix {
    NoJournal,
    NoIntent,
    RecoveredNone,
    BootstrapProfileIdNotBound,
}

/// This carrier stays in Bootstrap, outside the caught execution future. Thus
/// panic/drop cannot hide its actual FD receipt or release uncertain custody.
pub(crate) struct PublicationRead {
    invocation: NativeSetupInvocation,
    cleanup: Arc<CleanupState>,
    calls: u8,
    fresh: candidate_selection::FreshSetup,
    _capacity: NativeSetupReservation,
    #[cfg(unix)]
    pending: Option<PendingRecoveryScope>,
    #[cfg(unix)]
    confirmed: Option<ConfirmedScope>,
    #[cfg(test)]
    remove_intent_after_lock: bool,
}
impl PublicationRead {
    pub(crate) fn catalog_cleanup(&self) -> Arc<CleanupState> {
        self.cleanup.clone()
    }
    pub(crate) fn new(
        invocation: &NativeSetupInvocation,
    ) -> std::result::Result<Self, OriginalAuthorityAdmission> {
        let control = invocation
            .control()
            .reserve(ControlKind::Context, 4096)
            .map_err(|_| OriginalAuthorityAdmission::Capacity {
                code: "native-control",
            })?;
        let capacity = invocation
            .reserve(CapacityKind::VfsBuffer, 2 * 1024 * 1024 + 16384)
            .map_err(|error| OriginalAuthorityAdmission::Capacity { code: error.code() })?;
        Ok(Self {
            invocation: invocation.clone(),
            cleanup: Arc::new(CleanupState::new(control)),
            calls: 0,
            fresh: candidate_selection::FreshSetup::default(),
            _capacity: capacity,
            #[cfg(unix)]
            pending: None,
            #[cfg(unix)]
            confirmed: None,
            #[cfg(test)]
            remove_intent_after_lock: false,
        })
    }
    #[cfg(test)]
    pub(crate) fn run<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        discard_superseded: bool,
    ) -> Result<Prefix> {
        self.run_inner(owner, cancellation, discard_superseded, false)
    }
    pub(crate) fn enter_recovery<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        discard_superseded: bool,
    ) -> Result<Prefix> {
        self.run_inner(owner, cancellation, discard_superseded, true)
    }

    /// The confirmed capture takes its outer lock before recovering or resolving.
    pub(crate) fn begin_confirmed<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        budget: &Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<()> {
        #[cfg(unix)]
        {
            if self.calls != 0 {
                return Err(OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Guard,
                    "confirmed publication has already entered recovery",
                ));
            }
            // Both recovery occurrences borrow this one lock. Listing recovery
            // can discard a stale intent, which a confirmed capture must refuse.
            self.calls = 2;
            let op = Self::operation(&self.invocation, &self.cleanup, owner, cancellation)?;
            op.check(OriginalAuthorityStage::Source)?;
            self.confirmed = Some(
                owner
                    .with_original_review_user_data(|root| confirmed_scope(&op, root, budget))??,
            );
            self.fresh.record_confirmed_recovery(cancellation);
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = (owner, cancellation, budget);
            Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "Windows confirmed publication persistence/ACL",
            ))
        }
    }

    /// Validate the same directory and lock after the confirmed review returns.
    pub(crate) fn verify_confirmed<S: OriginalAuthorityOwner>(
        &self,
        owner: &S,
        cancellation: &CoreCancellation,
    ) -> Result<()> {
        #[cfg(unix)]
        if let Some(confirmed) = &self.confirmed {
            let op = self.candidate_operation(owner, cancellation)?;
            op.check(OriginalAuthorityStage::Guard)?;
            confirmed.scope.validate(&op)?;
            if self.candidate_io_verified() {
                return Ok(());
            }
        }
        #[cfg(not(unix))]
        let _ = (owner, cancellation);
        Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Guard,
            "confirmed publication cleanup is uncertain or the lock is absent",
        ))
    }

    fn candidate_io_verified(&self) -> bool {
        // The confirmed branch accounts for its own held lock only. Any other
        // outstanding descriptor, connection, or uncertain close still fails.
        #[cfg(unix)]
        if let Some(confirmed) = &self.confirmed {
            let receipt = self.receipt();
            return confirmed.scope.guard.file.is_some()
                && receipt.open_files == 1
                && receipt.open_connections == 0
                && !receipt.uncertain;
        }
        self.cleanup.verified()
    }

    fn operation<'a, S: OriginalAuthorityOwner>(
        invocation: &'a NativeSetupInvocation,
        cleanup: &Arc<CleanupState>,
        owner: &'a S,
        cancellation: &'a CoreCancellation,
    ) -> Result<Operation<'a, S>> {
        Ok(Operation {
            owner,
            invocation,
            cancellation,
            deadline: invocation
                .deadline()
                .map_err(|_| OriginalAuthorityError::unavailable())?,
            pid: std::process::id(),
            cleanup: cleanup.clone(),
            #[cfg(test)]
            fixture: None,
        })
    }
    fn run_inner<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        discard_superseded: bool,
        retain_continuation: bool,
    ) -> Result<Prefix> {
        #[cfg(unix)]
        if self.pending.is_some() {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Guard,
                "publication continuation is already held",
            ));
        }
        if self.calls > 1 || discard_superseded != (self.calls == 0) {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "publication recovery occurrence",
            ));
        }
        self.calls += 1;
        #[cfg(test)]
        let remove_intent_after_lock = std::mem::take(&mut self.remove_intent_after_lock);
        let op = Self::operation(&self.invocation, &self.cleanup, owner, cancellation)?;
        op.check(OriginalAuthorityStage::Source)?;
        // Retirement is applied after the actual body/close by the owning job.
        // Do not replace a prior original failure with a later shutdown flag.
        let result = owner
            .with_original_review_user_data(|root| {
                prefix(
                    &op,
                    root,
                    discard_superseded,
                    retain_continuation,
                    #[cfg(unix)]
                    &mut self.pending,
                    #[cfg(test)]
                    remove_intent_after_lock,
                )
            })?
            .map_err(recovery_error);
        if retain_continuation
            && matches!(
                result,
                Ok(Prefix::NoJournal | Prefix::NoIntent | Prefix::RecoveredNone)
            )
        {
            self.fresh.record_recovery(cancellation);
        }
        result
    }
    #[cfg(test)]
    pub(crate) fn remove_intent_after_lock_for_fixture(&mut self) {
        self.remove_intent_after_lock = true;
    }
    pub(crate) fn receipt(&self) -> OriginalAuthorityCloseReceipt {
        self.cleanup.receipt()
    }
    pub(crate) fn uncertain_cleanup(&self) -> Option<OriginalAuthorityCleanup> {
        (!self.cleanup.verified()).then(|| OriginalAuthorityCleanup(self.cleanup.clone()))
    }

    /// No guard, path, intent or resumable token is exposed to a Pack/guest.
    pub(crate) fn check_continuation_thread(&self) -> Result<()> {
        #[cfg(unix)]
        if let Some(pending) = &self.pending {
            return pending.check_thread();
        }
        Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Guard,
            "publication continuation is not held",
        ))
    }
    pub(crate) fn candidate_matches_identity(&self, identity: &[u32]) -> Result<bool> {
        self.check_continuation_thread()?;
        #[cfg(unix)]
        {
            let pending = self
                .pending
                .as_ref()
                .expect("held publication continuation");
            Ok(
                matches!(pending.intent.doc.node(pending.profile_id), Node::String(text) if text.0.as_slice() == identity),
            )
        }
        #[cfg(not(unix))]
        Err(OriginalAuthorityError::unavailable())
    }
    pub(crate) fn construct_workspace<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        identity: &str,
    ) -> Result<()> {
        self.check_continuation_thread()?;
        #[cfg(unix)]
        {
            let op = Operation {
                owner,
                invocation: &self.invocation,
                cancellation,
                deadline: self
                    .invocation
                    .deadline()
                    .map_err(|_| OriginalAuthorityError::unavailable())?,
                pid: std::process::id(),
                cleanup: self.cleanup.clone(),
                #[cfg(test)]
                fixture: None,
            };
            op.check(OriginalAuthorityStage::Source)?;
            let pending = self
                .pending
                .as_mut()
                .expect("held publication continuation");
            pending._workspace = Some(workspace_path(&op, &pending.root, identity)?);
            Ok(())
        }
        #[cfg(not(unix))]
        {
            let _ = (owner, cancellation, identity);
            Err(OriginalAuthorityError::unavailable())
        }
    }
    /// Called after actual Store retirement and native IO Join. Exceptional
    /// exits skip Python's post-yield validation; actual unlock/close always run.
    pub(crate) fn close_continuation(&mut self) -> Result<()> {
        self.fresh = candidate_selection::FreshSetup::default();
        #[cfg(unix)]
        if let Some(mut confirmed) = self.confirmed.take() {
            let thread = check_scope_thread(confirmed.thread);
            let closed = confirmed.scope.guard.close();
            return finish_scope(thread, closed);
        }
        #[cfg(unix)]
        if let Some(mut pending) = self.pending.take() {
            let thread = pending.check_thread();
            let closed = pending.scope.guard.close();
            return finish_scope(thread, closed);
        }
        Ok(())
    }
}

#[cfg(unix)]
fn workspace_path<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    profile_id: &str,
) -> Result<Funded<PathBuf>> {
    let bytes = root
        .as_os_str()
        .len()
        .checked_add(profile_id.len())
        .and_then(|bytes| bytes.checked_add(32))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Path))?;
    let capacity = op.reserve(OriginalAuthorityStage::Path, bytes.saturating_add(8192))?;
    // Exactly the original lexical joins, prepaid in one owned buffer.
    let mut workspace = PathBuf::new();
    workspace
        .try_reserve_exact(bytes)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Path))?;
    if workspace.capacity() != bytes {
        return Err(OriginalAuthorityError::capacity(
            OriginalAuthorityStage::Path,
        ));
    }
    workspace.push(root);
    workspace.push("workspaces");
    workspace.push(profile_id);
    Ok(Funded {
        value: workspace,
        _capacity: capacity,
    })
}

fn raw(class: &'static str, message: impl Into<String>) -> OriginalAuthorityError {
    let mut error = json::raw_error(class, message);
    error.stage = OriginalAuthorityStage::Source;
    error
}
fn value(message: &'static str) -> OriginalAuthorityError {
    raw("builtins.ValueError", message)
}
fn secure_error(message: &'static str) -> OriginalAuthorityError {
    raw(
        "tobkiri_protocol.secure_persistence.SecurePersistenceError",
        message,
    )
}
fn into_cause(mut error: OriginalAuthorityError) -> OriginalAuthorityCause {
    if error.class == RAW_IO
        && let Some(OriginalAuthorityCause::Io(mut io)) = error.cause.take()
    {
        io.context = error.context.map(|error| Box::new(into_cause(*error)));
        return OriginalAuthorityCause::Io(io);
    }
    OriginalAuthorityCause::Python {
        class: error.class,
        message: error.message,
        cause: error.cause.map(Box::new),
        context: error.context.map(|error| Box::new(into_cause(*error))),
        suppressed: error.suppressed,
    }
}
fn caused(
    mut error: OriginalAuthorityError,
    cause: OriginalAuthorityError,
) -> OriginalAuthorityError {
    error.cause = Some(into_cause(cause));
    error.suppressed = true;
    error
}
fn raw_io(error: std::io::Error, filename: Option<&str>) -> OriginalAuthorityError {
    OriginalAuthorityError {
        stage: OriginalAuthorityStage::Source,
        class: RAW_IO,
        message: String::new(),
        cause: Some(OriginalAuthorityCause::Io(OriginalAuthorityIoError {
            kind: error.kind(),
            errno: error.raw_os_error(),
            filename: filename.map(str::to_owned),
            context: None,
        })),
        context: None,
        suppressed: false,
    }
}
fn recovery_error(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == RAW_IO
        || matches!(
            error.class,
            "builtins.ValueError"
                | "builtins.FileNotFoundError"
                | "core_runtime.profile_content_projection.ProfileContentProjectionError"
                | "tobkiri_protocol.errors.CanonicalizationError"
                | "tobkiri_protocol.secure_persistence.SecurePersistenceError"
                | "core_runtime.hmac_key_manager.SigningKeyError"
                | "core_runtime.active_profile_store_v4.ActiveProfileStoreError"
                | "core_runtime.active_profile_store_v4.ActiveProfileStoreIntegrityError"
                | "core_runtime.active_profile_store_v4.ActiveProfileStoreLockTimeout"
        )
    {
        caused(
            raw(
                crate::bootstrap_review::DENIED,
                "Profile publication recovery failed closed",
            ),
            error,
        )
    } else {
        error
    }
}

fn prefix<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    discard_superseded: bool,
    retain_continuation: bool,
    #[cfg(unix)] pending: &mut Option<PendingRecoveryScope>,
    #[cfg(test)] remove_intent_after_lock: bool,
) -> Result<Prefix> {
    let mut paths_capacity = Some(
        op.reserve(
            OriginalAuthorityStage::Source,
            root.as_os_str()
                .len()
                .saturating_mul(16)
                .saturating_add(8192),
        )?,
    );
    let profiles = root.join("profiles");
    let path = profiles.join(JOURNAL);
    // Original exists() suppresses inaccessible/non-directory branches; a
    // dangling symlink is included by the separate is_symlink() operation.
    op.check(OriginalAuthorityStage::Source)?;
    let exists = path.exists() || path.is_symlink();
    op.check(OriginalAuthorityStage::Source)?;
    if !exists {
        return Ok(Prefix::NoJournal);
    }
    #[cfg(unix)]
    {
        let mut scope = Some(RecoveryScope::acquire(op, &profiles)?);
        // This private lexical body is the only continuation. Neither an
        // authenticated arena nor a guard escapes for a later reacquisition.
        let result = (|| {
            #[cfg(test)]
            if remove_intent_after_lock {
                fs::remove_file(&path).map_err(|error| raw_io(error, path.to_str()))?;
            }
            let Some(mut intent) = read_intent(op, &profiles)? else {
                return Ok(Prefix::NoIntent);
            };
            let expected = intent.predecessor(op)?;
            let current = active_profile::load(op, root)?;
            if !intent.is_current(current.as_ref(), expected.as_ref())? {
                // prepare_bootstrap_profile_review supplies confirmation=None.
                // Its outer occurrence discards; the inner occurrence keeps.
                if discard_superseded {
                    Directory::capture(op, &profiles, true)?.clear_journal(op)?;
                }
                return Ok(Prefix::RecoveredNone);
            }
            let candidate = intent
                .doc
                .get(intent.payload, "candidate")
                .expect("intent payload");
            let profile_id = subscript(&intent.doc, candidate, "profile_id")?;
            if retain_continuation {
                *pending = Some(PendingRecoveryScope {
                    scope: scope.take().expect("live publication scope"),
                    root: Funded {
                        value: root.to_path_buf(),
                        _capacity: paths_capacity.take().expect("funded publication paths"),
                    },
                    intent,
                    profile_id,
                    _current: current,
                    _expected: expected,
                    _workspace: None,
                    thread: std::thread::current().id(),
                });
            }
            // The next actual dependency is _bootstrap_profile_id(), before
            // runtime/catalog/Authority. Reaching it is no publication grant.
            Ok(Prefix::BootstrapProfileIdNotBound)
        })();
        match scope {
            Some(scope) => scope.finish(op, result),
            None => result,
        }
    }
    #[cfg(not(unix))]
    {
        let _ = (retain_continuation, paths_capacity);
        Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Source,
            "Windows publication persistence/ACL",
        ))
    }
}

#[cfg(unix)]
struct PendingRecoveryScope {
    scope: RecoveryScope,
    root: Funded<PathBuf>,
    intent: Intent,
    profile_id: usize,
    _current: Option<active_profile::LoadedPointer>,
    _expected: Option<Pointer>,
    _workspace: Option<Funded<PathBuf>>,
    thread: std::thread::ThreadId,
}
#[cfg(unix)]
impl PendingRecoveryScope {
    fn check_thread(&self) -> Result<()> {
        check_scope_thread(self.thread)
    }
}

#[cfg(unix)]
fn check_scope_thread(thread: std::thread::ThreadId) -> Result<()> {
    if thread != std::thread::current().id() {
        return Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Guard,
            "publication continuation moved from its logical worker thread",
        ));
    }
    Ok(())
}

#[cfg(unix)]
struct RecoveryScope {
    directory: Directory,
    guard: PublicationLock,
}
#[cfg(unix)]
impl RecoveryScope {
    fn acquire<S: OriginalAuthorityOwner>(op: &Operation<'_, S>, profiles: &Path) -> Result<Self> {
        let directory = Directory::capture(op, profiles, true)?;
        let guard = PublicationLock::acquire(op, &directory)?;
        Ok(Self { directory, guard })
    }

    fn validate<S: OriginalAuthorityOwner>(&self, op: &Operation<'_, S>) -> Result<()> {
        self.directory.validate_file(
            op,
            LOCK,
            self.guard.file.as_ref().expect("publication lock"),
        )
    }

    fn finish<S: OriginalAuthorityOwner>(
        mut self,
        op: &Operation<'_, S>,
        result: Result<Prefix>,
    ) -> Result<Prefix> {
        // Python validates after a normal return from the with body. Errors
        // and the not-yet-bound exceptional continuation skip that check.
        let result = if matches!(result, Ok(Prefix::NoIntent | Prefix::RecoveredNone)) {
            finish_scope(result, self.validate(op))
        } else {
            result
        };
        let closed = self.guard.close();
        finish_scope(result, closed)
    }
}

#[cfg(unix)]
struct Intent {
    doc: Funded<Document>,
    payload: usize,
}

#[cfg(unix)]
impl Intent {
    fn predecessor<S: OriginalAuthorityOwner>(
        &mut self,
        op: &Operation<'_, S>,
    ) -> Result<Option<Pointer>> {
        // The caller parses the predecessor again after journal.read.
        let id = self
            .doc
            .get(self.payload, "predecessor")
            .expect("intent payload");
        if matches!(self.doc.node(id), Node::Scalar(CanonValue::Null)) {
            Ok(None)
        } else {
            validate_pointer(op, &mut self.doc, id).map(Some)
        }
    }

    fn is_current(
        &self,
        current: Option<&active_profile::LoadedPointer>,
        expected: Option<&Pointer>,
    ) -> Result<bool> {
        let same = match (current, expected) {
            (None, None) => true,
            (Some(current), Some(expected)) => {
                current.pointer.matches(&current.doc, expected, &self.doc)
            }
            _ => false,
        };
        if same {
            Ok(true)
        } else {
            published_candidate(current, self, expected)
        }
    }
}

#[cfg(unix)]
struct ConfirmedScope {
    scope: RecoveryScope,
    _predecessor: Option<active_profile::LoadedPointer>,
    thread: std::thread::ThreadId,
}

#[cfg(unix)]
fn confirmed_scope<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &Path,
    budget: &Arc<dyn tobkiri_canon::AllocationBudget>,
) -> Result<ConfirmedScope> {
    if let Some(handover) = development_handover::read_optional(op, root, budget)?
        && !handover
            .payload
            .get(handover.payload.root, "stage")
            .is_some_and(|stage| handover.payload.string_is(stage, "completed"))
    {
        return Err(value(
            "Development Host handover requires recovery before activation",
        ));
    }
    let _paths = op.reserve(
        OriginalAuthorityStage::Path,
        root.as_os_str()
            .len()
            .saturating_mul(16)
            .saturating_add(8192),
    )?;
    let profiles = root.join("profiles");
    let mut scope = RecoveryScope::acquire(op, &profiles)?;
    let result = (|| {
        let path = profiles.join(JOURNAL);
        op.check(OriginalAuthorityStage::Source)?;
        let exists = path.exists() || path.is_symlink();
        op.check(OriginalAuthorityStage::Source)?;
        if exists {
            // The original nested recovery borrows the outer lock. Its
            // confirmed caller refuses stale evidence without clearing it.
            let intent = read_intent(op, &profiles).map_err(recovery_error)?;
            if let Some(mut intent) = intent {
                let expected = intent.predecessor(op).map_err(recovery_error)?;
                let current = active_profile::load(op, root).map_err(recovery_error)?;
                if !intent
                    .is_current(current.as_ref(), expected.as_ref())
                    .map_err(recovery_error)?
                {
                    return Err(raw(
                        crate::bootstrap_review::DENIED,
                        "Profile publication predecessor is stale",
                    ));
                }
                return Err(OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Source,
                    "confirmed publication recovery still requires activation store",
                ));
            }
        }
        active_profile::load(op, root)
    })();
    match result {
        Ok(predecessor) => Ok(ConfirmedScope {
            scope,
            _predecessor: predecessor,
            thread: std::thread::current().id(),
        }),
        Err(error) => finish_scope(Err(error), scope.guard.close()),
    }
}

#[cfg(all(test, unix))]
fn with_confirmed_predecessor<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    root: &Path,
    budget: &Arc<dyn tobkiri_canon::AllocationBudget>,
    inspect: impl FnOnce(&RecoveryScope, Option<&active_profile::LoadedPointer>) -> Result<T>,
) -> Result<T> {
    let ConfirmedScope {
        mut scope,
        _predecessor: predecessor,
        ..
    } = confirmed_scope(op, root, budget)?;
    let result = inspect(&scope, predecessor.as_ref());
    let result = if result.is_ok() {
        finish_scope(result, scope.validate(op))
    } else {
        result
    };
    finish_scope(result, scope.guard.close())
}

#[cfg(unix)]
fn published_candidate(
    current: Option<&active_profile::LoadedPointer>,
    intent: &Intent,
    expected: Option<&Pointer>,
) -> Result<bool> {
    let candidate = intent
        .doc
        .get(intent.payload, "candidate")
        .expect("intent payload");
    let Some(current) = current else {
        return Ok(false);
    };
    // tuple(generator) eagerly obtains all five keys before comparison.
    let fields = [
        "profile_id",
        "profile_revision",
        "activation_id",
        "plan_digest",
        "lock_digest",
    ];
    let candidate_fields = [
        subscript(&intent.doc, candidate, fields[0])?,
        subscript(&intent.doc, candidate, fields[1])?,
        subscript(&intent.doc, candidate, fields[2])?,
        subscript(&intent.doc, candidate, fields[3])?,
        subscript(&intent.doc, candidate, fields[4])?,
    ];
    let same = fields
        .into_iter()
        .zip(candidate_fields)
        .all(|(name, candidate)| {
            model::equal_atom(
                current.doc.node(current.pointer.field(name)),
                intent.doc.node(candidate),
            )
        });
    Ok(same
        && current.pointer.integer(&current.doc, "generation")
            == expected.map_or(1, |pointer| pointer.integer(&intent.doc, "generation") + 1))
}

fn subscript(doc: &Document, id: usize, key: &'static str) -> Result<usize> {
    if let Node::Object(_) = doc.node(id) {
        return doc
            .get(id, key)
            .ok_or_else(|| raw("builtins.KeyError", format!("'{key}'")));
    }
    let message = match doc.node(id) {
        Node::Array(_) => "list indices must be integers or slices, not str".to_owned(),
        Node::String(_) => "string indices must be integers, not 'str'".to_owned(),
        node => format!("'{}' object is not subscriptable", model::kind(node)),
    };
    Err(raw("builtins.TypeError", message))
}

#[cfg(unix)]
fn read_intent<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    profiles: &Path,
) -> Result<Option<Intent>> {
    // read() independently constructs/captures its SecureDirectory, under lock.
    let directory = Directory::capture(op, profiles, true)?;
    if !directory.exists(op, JOURNAL)? {
        return Ok(None);
    }
    let bytes = directory.read(op, JOURNAL)?;
    let mut doc = json::decode_strict(&bytes, op)?;
    let key_path = profiles.join(KEY);
    op.check(OriginalAuthorityStage::Key)?;
    let key_exists = key_path.exists();
    op.check(OriginalAuthorityStage::Key)?;
    if !key_exists {
        return Err(value("Profile publication signing key is unavailable"));
    }
    if !exact(&doc, doc.root, &["payload", "hmac"]) {
        return Err(value("Profile publication intent is invalid"));
    }
    let payload = doc.get(doc.root, "payload").expect("exact envelope");
    let hmac = doc.get(doc.root, "hmac").expect("exact envelope");
    if !exact(
        &doc,
        payload,
        &["schema", "predecessor", "candidate", "confirmation_digest"],
    ) || !doc
        .get(payload, "schema")
        .is_some_and(|id| doc.string_is(id, "io.tobkiri.profile-publication-intent.v1"))
        || !matches!(doc.node(hmac), Node::String(_))
    {
        return Err(value("Profile publication intent authentication failed"));
    }
    let key = signing_key(op, &key_path)?;
    let old_root = doc.root;
    doc.root = payload;
    let canonical = json::canonical(&doc, None, op)?;
    doc.root = old_root;
    let preimage = python_default_spacing(op, &canonical)?;
    let Node::String(signature) = doc.node(hmac) else {
        unreachable!("checked HMAC string")
    };
    let ascii = signature.0.iter().all(|&point| point <= 127);
    let signature_text = signature.utf8()?;
    let lowercase_hex = ascii
        && signature_text.len() == 64
        && signature_text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte));
    // compute_data_hmac precedes compare_digest even for malformed/non-ASCII
    // expected text. A fixed well-formed mismatch lets the existing primitive
    // do that same HMAC once; invalid expected text can never become accepted.
    let expected = if lowercase_hex {
        &signature_text
    } else {
        "0000000000000000000000000000000000000000000000000000000000000000"
    };
    let matched = verify_seal_hex(&preimage, expected, &key).is_ok();
    if !ascii {
        return Err(raw(
            "builtins.TypeError",
            "comparing strings with non-ASCII characters is not supported",
        ));
    }
    // The existing verifier accepts exactly lowercase hex, like comparison
    // against compute_data_hmac's lowercase text, and uses constant-time HMAC.
    if !lowercase_hex || !matched {
        return Err(value("Profile publication intent authentication failed"));
    }
    let predecessor = doc.get(payload, "predecessor").expect("exact payload");
    if !matches!(doc.node(predecessor), Node::Scalar(CanonValue::Null)) {
        validate_pointer(op, &mut doc, predecessor)?;
    }
    // candidate and confirmation_digest are deliberately not inspected here.
    Ok(Some(Intent { doc, payload }))
}

fn exact(doc: &Document, id: usize, names: &[&str]) -> bool {
    doc.entries(id).is_some_and(|pairs| {
        pairs.len() == names.len()
            && names
                .iter()
                .all(|name| pairs.iter().any(|(key, _)| key.is(name)))
    })
}
fn python_default_spacing<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    compact: &[u8],
) -> Result<Funded<Vec<u8>>> {
    let bound = compact
        .len()
        .checked_mul(2)
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
    let capacity = op.reserve(OriginalAuthorityStage::Key, bound)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(bound)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
    let mut string = false;
    let mut escaped = false;
    for &byte in compact {
        output.push(byte);
        if string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == b'"' {
                string = false;
            }
        } else if byte == b'"' {
            string = true;
        } else if matches!(byte, b':' | b',') {
            output.push(b' ');
        }
    }
    Ok(Funded {
        value: output,
        _capacity: capacity,
    })
}

#[cfg(unix)]
fn signing_key<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &Path,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    let fail = |message: &str| raw("core_runtime.hmac_key_manager.SigningKeyError", message);
    if path.is_symlink() {
        return Err(fail(&format!(
            "signing-key path is symlinked: {}",
            path.display()
        )));
    }
    let name = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    let descriptor = unsafe {
        libc::open(
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if descriptor < 0 {
        return Err(caused(
            fail("signing-key file is unreadable"),
            raw_io(std::io::Error::last_os_error(), path.to_str()),
        ));
    }
    let mut file = TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
    let result = (|| {
        let metadata = file.metadata().map_err(|error| raw_io(error, None))?;
        if !metadata.is_file() {
            return Err(fail("signing-key path is not a regular file"));
        }
        if metadata.uid() != unsafe { libc::getuid() } {
            return Err(fail("signing-key file is not owned by the Host user"));
        }
        if metadata.mode() & 0o077 != 0 {
            return Err(fail("signing-key file must have mode 0600"));
        }
        let data = read_stream(op, &mut file, 4096)?;
        let text = std::str::from_utf8(&data).map_err(|error| {
            let cause = json::decode(&data, op)
                .err()
                .and_then(|mut error| error.cause.take());
            let mut outer = fail("signing-key file is unreadable");
            outer.cause = cause.or(Some(OriginalAuthorityCause::Unsupported(
                "signing key Unicode projection",
            )));
            outer.suppressed = true;
            let _ = error;
            outer
        })?;
        let text = text.trim_matches(python_whitespace);
        if text.chars().count() < 32 {
            return Err(fail("signing-key material is missing or weak"));
        }
        let capacity = op.reserve(OriginalAuthorityStage::Key, text.len())?;
        let mut key = Zeroizing::new(Vec::new());
        key.try_reserve_exact(text.len())
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Key))?;
        key.extend_from_slice(text.as_bytes());
        Ok(Funded {
            value: key,
            _capacity: capacity,
        })
    })();
    finish_scope(result, file.close().map_err(|error| raw_io(error, None)))
}
fn python_whitespace(ch: char) -> bool {
    python_space(ch) || matches!(ch, '\u{001c}'..='\u{001f}')
}
