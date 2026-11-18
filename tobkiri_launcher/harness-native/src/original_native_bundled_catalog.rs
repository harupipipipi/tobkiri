//! Native ownership of the selected bundle's verified documents.
use super::*;
use crate::original_authority::bundled_schema::{CatalogDocumentKind, CatalogSchemas};
use bundled_catalog_checks::{ExecutableCatalogIssue, definition_revision_matches, executable_pin};
use member_index::{CatalogMemberIndex, document_identity};
use native_bundle_lock::{LockMember, lock_entries, member_at};
use std::fmt;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;

pub(super) enum NativeBundleError {
    Authority(OriginalAuthorityError),
    Integrity(String),
    InvalidLockEntry {
        path: CoreBuffer,
        field: &'static str,
    },
    TypeError(&'static str),
    UnrepresentedDocument,
    NestedLockJson {
        bytes: Funded<Zeroizing<Vec<u8>>>,
        detail: CoreBuffer,
    },
    Lock {
        cause: Box<OriginalAuthorityError>,
        detail: CoreBuffer,
        _capacity: NativeSetupReservation,
    },
}

impl From<OriginalAuthorityError> for NativeBundleError {
    fn from(error: OriginalAuthorityError) -> Self {
        Self::Authority(error)
    }
}

impl fmt::Display for NativeBundleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Authority(error) => error.fmt(f),
            Self::Integrity(message) => f.write_str(message),
            Self::InvalidLockEntry { path, field } => write!(
                f,
                "bundle entry {} has an invalid {field}",
                path.as_str().map_err(|_| fmt::Error)?
            ),
            Self::TypeError(message) => f.write_str(message),
            Self::UnrepresentedDocument => {
                f.write_str("the native reader cannot represent this document's validation outcome")
            }
            Self::Lock { detail, .. } | Self::NestedLockJson { detail, .. } => {
                write!(
                    f,
                    "cannot read bundle lock: {}",
                    detail.as_str().map_err(|_| fmt::Error)?
                )
            }
        }
    }
}

impl std::error::Error for NativeBundleError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Authority(error) => Some(error),
            Self::Lock { cause, .. } => Some(cause.as_ref()),
            _ => None,
        }
    }
}

impl fmt::Debug for NativeBundleError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("NativeBundleError")
            .field("message", &format_args!("{self}"))
            .field("cause", &std::error::Error::source(self))
            .finish()
    }
}

pub(super) type NativeBundleResult<T> = std::result::Result<T, NativeBundleError>;

struct OwnedMember {
    row: LockMember,
    bytes: Funded<Zeroizing<Vec<u8>>>,
}

pub(super) struct NativeOwnedBundle {
    pub(super) root: PathBuf,
    pub(super) artifact_root: Option<PathBuf>,
    pub(super) pins: usize,
    pub(super) artifact_parent: Option<Identity>,
    lock: Funded<Document>,
    identities: CatalogMemberIndex,
    members: Funded<Vec<OwnedMember>>,
    _paths: NativeSetupReservation,
}

impl NativeOwnedBundle {
    pub(super) fn entry_count(&self) -> usize {
        self.members.len()
    }

    pub(super) fn entries(&self) -> impl Iterator<Item = (CatalogDocumentKind, &str, &[u8])> {
        self.identities
            .entries()
            .zip(self.members.iter())
            .map(|((kind, id), member)| (kind, id, member.bytes.value.as_slice()))
    }
}

impl CatalogIo {
    pub(super) fn read_owned_bundle<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        invocation: &NativeSetupInvocation,
    ) -> NativeBundleResult<NativeOwnedBundle> {
        let cancellation = self.cancellation.clone();
        let op = self.operation(owner, invocation, &cancellation)?;
        if self.loader != Loader::Root
            || self.binding.is_some()
            || self.selected.as_os_str().is_empty()
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        self.compose_owned_bundle(&op)
    }

    /// Build the native snapshot for the already selected catalog scope.
    pub(super) fn compose_owned_bundle<S: crate::BootstrapReviewOwner>(
        &self,
        op: &Operation<'_, S>,
    ) -> NativeBundleResult<NativeOwnedBundle> {
        if !self.invocation.same_invocation(op.invocation)
            || !Arc::ptr_eq(&self.cleanup, &op.cleanup)
            || self.selected.as_os_str().is_empty()
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let budget = &self.budget;
        if metadata(op, budget, &self.selected, false)?.is_some_and(|value| value.is_symlink()) {
            return Err(integrity("bundle root must not be a symlink"));
        }
        let root = member_path::resolve_member(op, budget, &self.selected)?;
        let lock_path = root.join("bundle.lock.json");
        if metadata(op, budget, &lock_path, false)?.is_some_and(|value| value.is_symlink()) {
            return Err(integrity("bundle lock must not be a symlink"));
        }
        let lock = read_lock(op, budget, &lock_path)?;
        let rows = lock_entries(&lock)?;
        let mut identities = CatalogMemberIndex::new(op, rows.len())?;
        let mut members = member_storage(op, rows.len())?;
        let mut schemas = CatalogSchemas::default();
        for ordinal in 0..rows.len() {
            let row = member_at(&lock, rows, ordinal, op, budget)?;
            let lexical = lexical(&root, &row.relative)?;
            let candidate = member_path::resolve_member(op, budget, &lexical)?;
            if candidate == root || !candidate.starts_with(&root) {
                return Err(integrity(format!(
                    "bundle entry escapes root: {}",
                    row.relative
                )));
            }
            if member_path::member_has_symlink(op, budget, &root, &row.relative)? {
                return Err(integrity(format!(
                    "bundle entry contains a symlink: {}",
                    row.relative
                )));
            }
            let bytes = read_file(op, &candidate, budget)?;
            charge_work(budget, bytes.len() as u64)?;
            let actual = Digest::hash(&bytes);
            if actual != row.expected {
                return Err(integrity(format!(
                    "bundle artifact digest changed: {} ({} != {})",
                    row.relative,
                    actual.as_str(),
                    row.expected.as_str()
                )));
            }
            let document = schemas
                .prove_document(op, budget, row.kind, &bytes)?
                .ok_or(NativeBundleError::UnrepresentedDocument)?;
            if matches!(
                row.kind,
                CatalogDocumentKind::Base | CatalogDocumentKind::Shell
            ) && !definition_revision_matches(&document, op, budget)?
            {
                return Err(integrity(format!(
                    "{} definition revision is stale or tampered: {}",
                    label(row.kind),
                    row.relative
                )));
            }
            if !identities.insert(row.kind, ordinal, &document, op, budget)? {
                let displayed = document_identity(&document, row.kind)
                    .map_or_else(|| "None".to_owned(), json::repr_text);
                return Err(integrity(format!(
                    "duplicate or missing {} identity: {displayed}",
                    label(row.kind)
                )));
            }
            members.value.push(OwnedMember { row, bytes });
        }
        drop(schemas);
        let pins = verify_pins(op, budget, &root, &identities, &members)?;
        let artifact_path = root.parent().unwrap_or(&root).join("platform-artifacts");
        let (artifact_root, artifact_parent) =
            if metadata(op, budget, &artifact_path, true)?.is_some_and(|value| value.is_dir()) {
                let path = member_path::resolve_member(op, budget, &artifact_path)?;
                let parent = shell_root::artifact_parent_identity(op, &root, &path, budget)?;
                (Some(path), parent)
            } else {
                (None, None)
            };
        let paths = root.capacity() + artifact_root.as_ref().map_or(0, PathBuf::capacity);
        Ok(NativeOwnedBundle {
            root,
            artifact_root,
            pins,
            artifact_parent,
            lock,
            identities,
            members,
            _paths: op.reserve(STAGE, paths)?,
        })
    }

    /// Retain the original loader inputs after the same-scope snapshot is consumed.
    pub(super) fn adopt_owned_bundle<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        bundle: NativeOwnedBundle,
    ) -> Result<()> {
        op.check(STAGE)?;
        if !self.invocation.same_invocation(op.invocation)
            || !Arc::ptr_eq(&self.cleanup, &op.cleanup)
            || self.binding.is_none()
            || self.selected.as_os_str().is_empty()
            || self.lock_bytes.is_some()
            || self.lock.is_some()
            || self.entry != 0
            || self.walk != 0
            || self.pins != 0
            || self.candidate.is_some()
            || self.selected_before_resolve.is_some()
            || self.artifact_root.is_some()
            || self.artifact_parent_identity.is_some()
            || self.shell_inputs.is_some()
            || self.pack_resolve != PackResolve::Inactive
        {
            return Err(OriginalAuthorityError::unavailable());
        }

        let NativeOwnedBundle {
            root,
            artifact_root,
            pins,
            artifact_parent,
            lock,
            identities,
            members,
            _paths,
        } = bundle;
        drop(identities);
        self.lock = Some(lock);
        let Funded {
            value: members,
            _capacity: _member_capacity,
        } = members;
        for OwnedMember { bytes, .. } in members {
            self.retain_pack_pin(op, &bytes)?;
            self.retain_shell_input(op, bytes)?;
            self.entry += 1;
        }
        op.check(STAGE)?;
        self.pins = pins;
        self.artifact_parent_identity = artifact_parent;
        self.artifact_root = artifact_root;
        // The existing CatalogIo path reservation covers the adopted paths.
        if self.retain_publication_root {
            self.selected_before_resolve = Some(std::mem::replace(&mut self.selected, root));
        } else {
            self.selected = root;
        }
        Ok(())
    }
}

fn read_lock<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    path: &Path,
) -> NativeBundleResult<Funded<Document>> {
    let bytes = match read_file(op, path, budget) {
        Ok(bytes) => bytes,
        Err(error) => return Err(lock_error(error, op, budget)?),
    };
    match json::decode_read_only_strict(&bytes, op, budget) {
        Ok(lock) => Ok(lock),
        Err(error)
            if error.class == "tobkiri_protocol.errors.CanonicalizationError"
                && matches!(
                    error.cause,
                    Some(OriginalAuthorityCause::Python {
                        class: "json.decoder.JSONDecodeError" | "builtins.UnicodeDecodeError",
                        ..
                    })
                ) =>
        {
            let detail = CoreBuffer::copy_from(error.message.as_bytes(), budget)
                .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
            Err(NativeBundleError::NestedLockJson { bytes, detail })
        }
        Err(error) => Err(lock_error(error, op, budget)?),
    }
}

fn lock_error<S: OriginalAuthorityOwner>(
    cause: OriginalAuthorityError,
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
) -> NativeBundleResult<NativeBundleError> {
    use crate::original_authority::{ERROR_TEXT_BYTES, os_text};
    let capacity = |_| OriginalAuthorityError::capacity(STAGE);
    let detail = if let Some(OriginalAuthorityCause::Io(io)) = &cause.cause {
        let errno = io.errno.ok_or_else(OriginalAuthorityError::unavailable)?;
        charge_work(budget, ERROR_TEXT_BYTES as u64)?;
        let mut buffer = [0; ERROR_TEXT_BYTES];
        let message =
            os_text(errno, &mut buffer).map_err(|_| OriginalAuthorityError::unavailable())?;
        let mut out = CoreBuffer::builder(budget, MAX_BYTES);
        out.append(format!("[Errno {errno}] ").as_bytes())
            .map_err(capacity)?;
        out.append(message.as_bytes()).map_err(capacity)?;
        if let Some(path) = &io.filename {
            out.append(b": ").map_err(capacity)?;
            let repr = crate::unicode15_1::repr_utf8(path, budget).map_err(capacity)?;
            out.append(repr.as_bytes()).map_err(capacity)?;
        }
        out.finish().map_err(capacity)?
    } else if cause.class == "tobkiri_protocol.errors.CanonicalizationError" {
        CoreBuffer::copy_from(cause.message.as_bytes(), budget).map_err(capacity)?
    } else {
        return Ok(NativeBundleError::Authority(cause));
    };
    let _capacity = op.reserve(STAGE, size_of::<OriginalAuthorityError>())?;
    Ok(NativeBundleError::Lock {
        cause: Box::new(cause),
        detail,
        _capacity,
    })
}

fn member_storage<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    count: usize,
) -> Result<Funded<Vec<OwnedMember>>> {
    let capacity = count
        .checked_mul(size_of::<OwnedMember>())
        .ok_or_else(|| OriginalAuthorityError::capacity(STAGE))?;
    let capacity = op.reserve(STAGE, capacity)?;
    let mut value = Vec::new();
    value
        .try_reserve_exact(count)
        .map_err(|_| OriginalAuthorityError::capacity(STAGE))?;
    if value.capacity() != count {
        return Err(OriginalAuthorityError::capacity(STAGE));
    }
    Ok(Funded {
        value,
        _capacity: capacity,
    })
}

fn verify_pins<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &Path,
    identities: &CatalogMemberIndex,
    records: &[OwnedMember],
) -> NativeBundleResult<usize> {
    let mut pins = 0;
    for ((kind, id), record) in identities.entries().zip(records) {
        if kind != CatalogDocumentKind::ExecutableCatalog {
            continue;
        }
        op.check(STAGE)?;
        charge_work(
            budget,
            (records.len() as u64).saturating_mul(id.len() as u64 + 1),
        )?;
        let manifest = identities
            .entries()
            .zip(records)
            .find_map(|((kind, pack), record)| {
                (kind == CatalogDocumentKind::Pack && pack == id).then_some(record)
            });
        let Some(manifest) = manifest else {
            return Err(pin_issue(ExecutableCatalogIssue::MissingPackManifest, id));
        };
        let manifest = json::decode_read_only_strict(&manifest.bytes, op, budget)?;
        let executable = json::decode_read_only_strict(&record.bytes, op, budget)?;
        let expected_pin = executable_pin(&executable, Some(&manifest), op, budget)?
            .map_err(|issue| pin_issue(issue, id))?;
        // Independent reopening from the original lexical target. The
        // original pin check does not repeat resolve or the symlink walk.
        let path = lexical(root, &record.row.relative)?;
        let fresh = read_file(op, &path, budget)?;
        charge_work(budget, fresh.len() as u64)?;
        let actual = Digest::hash(&fresh);
        if actual != record.row.expected || !expected_pin.is(actual.as_str()) {
            return Err(integrity(format!(
                "Pack executable catalog artifact pin is stale: {id}"
            )));
        }
        pins += 1;
    }
    Ok(pins)
}

fn lexical(root: &Path, relative: &str) -> Result<PathBuf> {
    if root.as_os_str().len() + relative.len() + 1 > 8192 {
        return Err(OriginalAuthorityError::capacity(
            OriginalAuthorityStage::Path,
        ));
    }
    Ok(member_path::lexical_member(root, relative))
}

fn metadata<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    path: &Path,
    follow: bool,
) -> Result<Option<fs::Metadata>> {
    op.check(STAGE)?;
    charge_work(budget, path.as_os_str().len() as u64 + 1)?;
    let result = if follow {
        fs::metadata(path)
    } else {
        fs::symlink_metadata(path)
    };
    op.check(STAGE)?;
    match result {
        Ok(metadata) => Ok(Some(metadata)),
        Err(error)
            if error
                .raw_os_error()
                .is_some_and(pathlib_suppressed_stat_errno) =>
        {
            Ok(None)
        }
        Err(error) => Err(raw_io(error, path.to_str())),
    }
}

fn integrity(message: impl Into<String>) -> NativeBundleError {
    NativeBundleError::Integrity(message.into())
}

pub(super) fn label(kind: CatalogDocumentKind) -> &'static str {
    match kind {
        CatalogDocumentKind::Pack => "pack",
        CatalogDocumentKind::Base => "base",
        CatalogDocumentKind::Shell => "shell",
        CatalogDocumentKind::Profile => "profile",
        CatalogDocumentKind::ExecutableCatalog => "executable_catalog",
    }
}

fn pin_issue(issue: ExecutableCatalogIssue, id: &str) -> NativeBundleError {
    use ExecutableCatalogIssue::*;
    let message = match issue {
        MissingPackManifest => "executable catalog has no bundled Pack manifest",
        StaleSourceIdentity => "executable catalog source identity is stale",
        StaleCatalogDigest => "executable catalog digest is stale",
        MissingOrDuplicateArtifactPin => "Pack manifest does not pin executable catalog",
        InvalidArtifactPinDigest => return NativeBundleError::UnrepresentedDocument,
    };
    integrity(format!("{message}: {id}"))
}
