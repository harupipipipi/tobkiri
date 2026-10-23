//! Bounded structural review of an exact, uncommitted Profile candidate.
//!
//! This is data for a trusted caller to render. It does not save a revision,
//! approve a permission, register a Session, invoke a Pack, or establish
//! readiness. Target statuses describe only whether bounded metadata can be
//! shown safely; they do not prove that a referenced capability is installed
//! or available.

use std::{
    io::Write,
    mem::size_of,
    net::{Ipv4Addr, Ipv6Addr},
};

use serde::Serialize;
use tobkiri_canon::{Digest, ids::ShapeRef};
use tobkiri_gate::PermissionSet;
use tobkiri_manifest::VerifiedPack;
use tobkiri_profile::Profile;

use crate::{
    Core, CoreError,
    profiles::{load_packs, strict_bytes},
};

/// The maximum owned review representation, aligned with the current trusted
/// host response body bound. This limits only this projection, not Profile
/// validity or the existing compiler contract.
const MAX_REVIEW_BYTES: usize = 1024 * 1024;
const MAX_REVIEW_TEXT_BYTES: usize = MAX_REVIEW_BYTES;
const MAX_NETWORK_ORIGIN_BYTES: usize = 2048;
const MAX_PROCESS_ID_BYTES: usize = 128;
const MAX_SECRET_NAME_BYTES: usize = 240;
const MAX_HOST_IO_NAME_BYTES: usize = 128;
const MAX_FILESYSTEM_ROOT_ID_BYTES: usize = 72;
const MAX_SHAPE_REF_BYTES: usize = 128;

/// Why a bounded review projection could not be produced.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProfileReviewCapacity {
    /// The complete pack review would exceed the bounded response shape.
    PackCount,
    /// The compiler produced more conflict records than fit in the response.
    ConflictCount,
    /// The bounded DTO or its copied text would exceed the response budget.
    OutputBytes,
    /// A required bounded allocation could not be reserved.
    Allocation,
}

/// Errors from candidate loading, existing Profile canonicalization, or the
/// review-only capacity checks. Compiler conflicts are returned as data.
#[derive(Debug, thiserror::Error)]
pub enum ProfileReviewError {
    #[error(transparent)]
    Core(#[from] CoreError),
    #[error("candidate review exceeds bounded capacity: {0:?}")]
    Capacity(ProfileReviewCapacity),
}

/// Canonical candidate identity using the same bytes and digest recipe as a
/// persisted Profile revision. It is not a commit or an approval token.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProfileReviewIdentity {
    pub profile_id: String,
    pub revision: String,
}

/// Exact verified Pack metadata bound to the candidate's pin.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProfileReviewPack {
    pub id: String,
    pub version: String,
    pub artifact_hash: String,
    pub manifest_format: &'static str,
    /// Verified public signing key, when the Pack is publisher-signed.
    pub publisher_key: Option<String>,
    pub declared_permissions: PermissionReview,
    /// The candidate's Pack-specific authority limit, if present. Missing
    /// limits remain visible as `None`; compiler conflicts are reported
    /// separately.
    pub profile_limit: Option<PermissionReview>,
}

/// A Profile permission limit whose Pack is not among the verified candidate
/// pins. It remains visible alongside the compiler's structural conflict.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProfileReviewPackLimit {
    pub pack_id: String,
    pub permissions: PermissionReview,
}

/// Structural conflicts from the existing Profile compiler. The compiler's
/// prose messages are intentionally omitted because they may include source
/// document text. Pack IDs are included only when they match verified Packs.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProfileReviewConflict {
    pub code: String,
    pub affected_pack_ids: Vec<String>,
}

/// Bounded structural facts for one exact candidate. An empty conflict list
/// means only that `tobkiri_profile::compile` accepted this structure; it does
/// not imply preparation, readiness, approval, or successful startup.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ProfileReview {
    pub candidate: ProfileReviewIdentity,
    pub profile_ceiling: PermissionReview,
    pub user_permissions: Option<PermissionReview>,
    pub packs: Vec<ProfileReviewPack>,
    pub unresolved_pack_limits: Vec<ProfileReviewPackLimit>,
    pub conflicts: Vec<ProfileReviewConflict>,
}

/// Permission targets and bounds suitable for review, without secret values,
/// source paths, executable descriptors, or raw Pack file contents.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct PermissionReview {
    pub network_origins: Vec<ReviewedTarget>,
    pub user_origins: Option<UserOriginReview>,
    pub filesystem: Vec<FilesystemPermissionReview>,
    pub process_ids: Vec<ReviewedTarget>,
    pub pty: bool,
    pub secrets: Vec<SecretPermissionReview>,
    pub storage: StoragePermissionReview,
    pub native: bool,
    pub cross_profile_storage: bool,
    pub audit_read: bool,
    pub audit_append: bool,
    pub host_io: Vec<ReviewedTarget>,
    pub port_shapes: Vec<ReviewedTarget>,
    pub mediator: bool,
    pub wasm: tobkiri_gate::ResourceLimits,
    pub loops: tobkiri_gate::LoopLimits,
}

/// A bounded permission target. `Exact` means the value passed the local
/// display-safety grammar; it does not establish that the target is installed.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ReviewedTarget {
    pub value: Option<String>,
    pub status: TargetReviewStatus,
}

/// Whether a permission target can be shown without echoing unsafe text.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TargetReviewStatus {
    Exact,
    RedactedInvalid,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct UserOriginReview {
    pub max: u32,
    pub allow_loopback: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct FilesystemPermissionReview {
    /// Opaque `fs-root:` identity only; never a filesystem pathname.
    pub root_id: ReviewedTarget,
    pub read: bool,
    pub write: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct SecretPermissionReview {
    /// Secret slot name only; the vault value is never opened or copied.
    pub name: ReviewedTarget,
    pub reveal: bool,
    pub optional: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct StoragePermissionReview {
    pub private: bool,
    pub public_read: bool,
    pub public_write: bool,
}

#[derive(Default)]
struct ReviewBudget {
    bytes: usize,
}

impl ReviewBudget {
    fn add(&mut self, bytes: usize) -> Result<(), ProfileReviewError> {
        self.bytes = self
            .bytes
            .checked_add(bytes)
            .filter(|total| *total <= MAX_REVIEW_BYTES)
            .ok_or(ProfileReviewError::Capacity(
                ProfileReviewCapacity::OutputBytes,
            ))?;
        Ok(())
    }

    fn add_vec<T>(&mut self, count: usize) -> Result<(), ProfileReviewError> {
        self.add(
            count
                .checked_mul(size_of::<T>())
                .ok_or(ProfileReviewError::Capacity(
                    ProfileReviewCapacity::OutputBytes,
                ))?,
        )
    }

    fn add_text(&mut self, value: &str) -> Result<(), ProfileReviewError> {
        if value.len() > MAX_REVIEW_TEXT_BYTES {
            return Err(ProfileReviewError::Capacity(
                ProfileReviewCapacity::OutputBytes,
            ));
        }
        // Include the string header as a conservative per-allocation allowance.
        self.add(value.len().checked_add(size_of::<String>()).ok_or(
            ProfileReviewError::Capacity(ProfileReviewCapacity::OutputBytes),
        )?)
    }
}

impl Core {
    /// Inspect one Profile candidate without changing persistent or runtime
    /// state. Pack bytes are verified through the shared loader; compilation
    /// uses the same compiler as save/start. This synchronous in-process API
    /// does not consume CoreControl budget, matching the existing structural
    /// `check_profile` path; callers must keep it off an unbounded IPC surface.
    pub fn review_profile(&self, candidate: &Profile) -> Result<ProfileReview, ProfileReviewError> {
        // Even before canonical encoding, refuse a pack list that cannot fit
        // in the bounded DTO. This is a review capacity result, not a Profile
        // validity rule, and it prevents loading thousands of unnecessary
        // artifacts for a response that cannot represent them.
        if candidate.packs.len() > MAX_REVIEW_BYTES / size_of::<ProfileReviewPack>() {
            return Err(ProfileReviewError::Capacity(
                ProfileReviewCapacity::PackCount,
            ));
        }

        let canonical = strict_bytes(candidate)?;
        // `strict_bytes` is exactly the persisted revision encoding used by
        // Profiles::write_revision; do not substitute the compiler plan hash.
        let revision = Digest::hash(&canonical);
        drop(canonical);

        let packs = load_packs(candidate, &self.control)?;
        let conflicts = tobkiri_profile::compile(candidate, &packs)
            .err()
            .unwrap_or_default();

        let mut budget = ReviewBudget::default();
        preflight_review(candidate, &packs, &conflicts, &revision, &mut budget)?;
        let review = build_review(candidate, &packs, &conflicts, revision)?;
        check_wire_size(&review)?;
        Ok(review)
    }
}

fn preflight_review(
    candidate: &Profile,
    packs: &[VerifiedPack],
    conflicts: &[tobkiri_profile::Conflict],
    revision: &Digest,
    budget: &mut ReviewBudget,
) -> Result<(), ProfileReviewError> {
    if conflicts.len() > MAX_REVIEW_BYTES / size_of::<ProfileReviewConflict>() {
        return Err(ProfileReviewError::Capacity(
            ProfileReviewCapacity::ConflictCount,
        ));
    }
    budget.add(size_of::<ProfileReview>())?;
    budget.add_vec::<ProfileReviewPack>(packs.len())?;
    budget.add_vec::<ProfileReviewConflict>(conflicts.len())?;
    budget.add_text(candidate.id.as_str())?;
    budget.add_text(revision.as_str())?;
    add_permission_targets(budget, &candidate.ceiling)?;
    if let Some(permissions) = &candidate.user_permissions {
        add_permission_targets(budget, permissions)?;
    }
    for pack in packs {
        budget.add_text(pack.manifest.id.as_str())?;
        budget.add_text(&pack.manifest.version)?;
        budget.add_text(pack.hash.as_str())?;
        if let Some(publisher_key) = &pack.manifest.publisher_key {
            budget.add_text(publisher_key)?;
        }
        add_permission_targets(budget, &pack.manifest.permissions)?;
        if let Some(limit) = candidate.pack_limits.get(&pack.manifest.id) {
            add_permission_targets(budget, limit)?;
        }
    }
    let unresolved_pack_limits = candidate
        .pack_limits
        .keys()
        .filter(|id| !candidate.packs.iter().any(|pin| &pin.id == *id))
        .count();
    budget.add_vec::<ProfileReviewPackLimit>(unresolved_pack_limits)?;
    for (id, limit) in &candidate.pack_limits {
        if !candidate.packs.iter().any(|pin| &pin.id == id) {
            budget.add_text(id.as_str())?;
            add_permission_targets(budget, limit)?;
        }
    }
    for conflict in conflicts {
        budget.add_vec::<String>(verified_conflict_pack_count(conflict, packs))?;
        budget.add_text(&conflict.code)?;
        for source_id in &conflict.packs {
            if let Some(pack) = packs
                .iter()
                .find(|pack| pack.manifest.id.as_str() == source_id.as_str())
            {
                budget.add_text(pack.manifest.id.as_str())?;
            }
        }
    }
    Ok(())
}

fn add_permission_targets(
    budget: &mut ReviewBudget,
    permissions: &PermissionSet,
) -> Result<(), ProfileReviewError> {
    budget.add_vec::<ReviewedTarget>(permissions.network.origins.len())?;
    for origin in &permissions.network.origins {
        if safe_network_origin(origin) {
            budget.add_text(origin)?;
        }
    }
    budget.add_vec::<FilesystemPermissionReview>(permissions.fs.len())?;
    for filesystem in &permissions.fs {
        if safe_filesystem_root_id(&filesystem.root) {
            budget.add_text(&filesystem.root)?;
        }
    }
    budget.add_vec::<ReviewedTarget>(permissions.proc.len())?;
    for program_id in &permissions.proc {
        if safe_bounded_identifier(program_id, MAX_PROCESS_ID_BYTES) {
            budget.add_text(program_id)?;
        }
    }
    budget.add_vec::<SecretPermissionReview>(permissions.secrets.len())?;
    for secret in &permissions.secrets {
        if safe_secret_name(&secret.name) {
            budget.add_text(&secret.name)?;
        }
    }
    budget.add_vec::<ReviewedTarget>(permissions.host_io.len())?;
    for operation in &permissions.host_io {
        if safe_bounded_identifier(operation, MAX_HOST_IO_NAME_BYTES) {
            budget.add_text(operation)?;
        }
    }
    budget.add_vec::<ReviewedTarget>(permissions.ports.len())?;
    for shape in &permissions.ports {
        if safe_shape_reference(shape) {
            budget.add_text(shape)?;
        }
    }
    Ok(())
}

fn verified_conflict_pack_count(
    conflict: &tobkiri_profile::Conflict,
    packs: &[VerifiedPack],
) -> usize {
    conflict
        .packs
        .iter()
        .filter(|source_id| {
            packs
                .iter()
                .any(|pack| pack.manifest.id.as_str() == source_id.as_str())
        })
        .count()
}

fn build_review(
    candidate: &Profile,
    packs: &[VerifiedPack],
    conflicts: &[tobkiri_profile::Conflict],
    revision: Digest,
) -> Result<ProfileReview, ProfileReviewError> {
    let mut reviewed_packs = bounded_vec::<ProfileReviewPack>(packs.len())?;
    for pack in packs {
        reviewed_packs.push(ProfileReviewPack {
            id: copy_text(pack.manifest.id.as_str())?,
            version: copy_text(&pack.manifest.version)?,
            artifact_hash: copy_text(pack.hash.as_str())?,
            manifest_format: "tobkiri.pack/1",
            publisher_key: pack
                .manifest
                .publisher_key
                .as_deref()
                .map(copy_text)
                .transpose()?,
            declared_permissions: review_permissions(&pack.manifest.permissions)?,
            profile_limit: candidate
                .pack_limits
                .get(&pack.manifest.id)
                .map(review_permissions)
                .transpose()?,
        });
    }

    let unresolved_count = candidate
        .pack_limits
        .keys()
        .filter(|id| !candidate.packs.iter().any(|pin| &pin.id == *id))
        .count();
    let mut unresolved_pack_limits = bounded_vec::<ProfileReviewPackLimit>(unresolved_count)?;
    for (id, permissions) in &candidate.pack_limits {
        if !candidate.packs.iter().any(|pin| &pin.id == id) {
            unresolved_pack_limits.push(ProfileReviewPackLimit {
                pack_id: copy_text(id.as_str())?,
                permissions: review_permissions(permissions)?,
            });
        }
    }

    let mut reviewed_conflicts = bounded_vec::<ProfileReviewConflict>(conflicts.len())?;
    for conflict in conflicts {
        let affected_count = verified_conflict_pack_count(conflict, packs);
        let mut affected_pack_ids = bounded_vec::<String>(affected_count)?;
        for source_id in &conflict.packs {
            if let Some(pack) = packs
                .iter()
                .find(|pack| pack.manifest.id.as_str() == source_id.as_str())
            {
                affected_pack_ids.push(copy_text(pack.manifest.id.as_str())?);
            }
        }
        reviewed_conflicts.push(ProfileReviewConflict {
            code: copy_text(&conflict.code)?,
            affected_pack_ids,
        });
    }

    Ok(ProfileReview {
        candidate: ProfileReviewIdentity {
            profile_id: copy_text(candidate.id.as_str())?,
            revision: copy_text(revision.as_str())?,
        },
        profile_ceiling: review_permissions(&candidate.ceiling)?,
        user_permissions: candidate
            .user_permissions
            .as_ref()
            .map(review_permissions)
            .transpose()?,
        packs: reviewed_packs,
        unresolved_pack_limits,
        conflicts: reviewed_conflicts,
    })
}

fn review_permissions(permissions: &PermissionSet) -> Result<PermissionReview, ProfileReviewError> {
    let mut network_origins = bounded_vec::<ReviewedTarget>(permissions.network.origins.len())?;
    for origin in &permissions.network.origins {
        network_origins.push(reviewed_target(origin, safe_network_origin(origin))?);
    }
    let mut filesystem = bounded_vec::<FilesystemPermissionReview>(permissions.fs.len())?;
    for permission in &permissions.fs {
        filesystem.push(FilesystemPermissionReview {
            root_id: reviewed_target(&permission.root, safe_filesystem_root_id(&permission.root))?,
            read: permission.read,
            write: permission.write,
        });
    }
    let mut process_ids = bounded_vec::<ReviewedTarget>(permissions.proc.len())?;
    for program_id in &permissions.proc {
        process_ids.push(reviewed_target(
            program_id,
            safe_bounded_identifier(program_id, MAX_PROCESS_ID_BYTES),
        )?);
    }
    let mut secrets = bounded_vec::<SecretPermissionReview>(permissions.secrets.len())?;
    for permission in &permissions.secrets {
        secrets.push(SecretPermissionReview {
            name: reviewed_target(&permission.name, safe_secret_name(&permission.name))?,
            reveal: permission.reveal,
            optional: permission.optional,
        });
    }
    let mut host_io = bounded_vec::<ReviewedTarget>(permissions.host_io.len())?;
    for operation in &permissions.host_io {
        host_io.push(reviewed_target(
            operation,
            safe_bounded_identifier(operation, MAX_HOST_IO_NAME_BYTES),
        )?);
    }
    let mut port_shapes = bounded_vec::<ReviewedTarget>(permissions.ports.len())?;
    for shape in &permissions.ports {
        port_shapes.push(reviewed_target(shape, safe_shape_reference(shape))?);
    }

    Ok(PermissionReview {
        network_origins,
        user_origins: permissions.network.user_origins.as_ref().map(|permission| {
            UserOriginReview {
                max: permission.max,
                allow_loopback: permission.allow_loopback,
            }
        }),
        filesystem,
        process_ids,
        pty: permissions.pty,
        secrets,
        storage: StoragePermissionReview {
            private: permissions.storage.private,
            public_read: permissions.storage.public_read,
            public_write: permissions.storage.public_write,
        },
        native: permissions.native,
        cross_profile_storage: permissions.cross_profile_storage,
        audit_read: permissions.audit_read,
        audit_append: permissions.audit_append,
        host_io,
        port_shapes,
        mediator: permissions.mediator,
        wasm: permissions.wasm.clone(),
        loops: permissions.loops.clone(),
    })
}

fn reviewed_target(value: &str, safe: bool) -> Result<ReviewedTarget, ProfileReviewError> {
    if safe {
        Ok(ReviewedTarget {
            value: Some(copy_text(value)?),
            status: TargetReviewStatus::Exact,
        })
    } else {
        Ok(ReviewedTarget {
            value: None,
            status: TargetReviewStatus::RedactedInvalid,
        })
    }
}

fn bounded_vec<T>(count: usize) -> Result<Vec<T>, ProfileReviewError> {
    if count
        .checked_mul(size_of::<T>())
        .is_none_or(|bytes| bytes > MAX_REVIEW_BYTES)
    {
        return Err(ProfileReviewError::Capacity(
            ProfileReviewCapacity::OutputBytes,
        ));
    }
    let mut result = Vec::new();
    result
        .try_reserve_exact(count)
        .map_err(|_| ProfileReviewError::Capacity(ProfileReviewCapacity::Allocation))?;
    if result.capacity() > MAX_REVIEW_BYTES / size_of::<T>().max(1) {
        return Err(ProfileReviewError::Capacity(
            ProfileReviewCapacity::Allocation,
        ));
    }
    if result.capacity() != count {
        return Err(ProfileReviewError::Capacity(
            ProfileReviewCapacity::Allocation,
        ));
    }
    Ok(result)
}

fn copy_text(value: &str) -> Result<String, ProfileReviewError> {
    if value.len() > MAX_REVIEW_TEXT_BYTES {
        return Err(ProfileReviewError::Capacity(
            ProfileReviewCapacity::OutputBytes,
        ));
    }
    let mut result = String::new();
    result
        .try_reserve_exact(value.len())
        .map_err(|_| ProfileReviewError::Capacity(ProfileReviewCapacity::Allocation))?;
    if result.capacity() != value.len() {
        return Err(ProfileReviewError::Capacity(
            ProfileReviewCapacity::Allocation,
        ));
    }
    result.push_str(value);
    Ok(result)
}

struct CountWriter {
    bytes: usize,
}

impl Write for CountWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.bytes = self
            .bytes
            .checked_add(bytes.len())
            .filter(|total| *total <= MAX_REVIEW_BYTES)
            .ok_or_else(|| std::io::Error::other("review output bound"))?;
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn check_wire_size(review: &ProfileReview) -> Result<(), ProfileReviewError> {
    let mut writer = CountWriter { bytes: 0 };
    serde_json::to_writer(&mut writer, review)
        .map_err(|_| ProfileReviewError::Capacity(ProfileReviewCapacity::OutputBytes))
}

fn safe_bounded_identifier(value: &str, max_bytes: usize) -> bool {
    !value.is_empty() && value.len() <= max_bytes && !value.chars().any(char::is_control)
}

fn safe_secret_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= MAX_SECRET_NAME_BYTES
        && value != "."
        && value != ".."
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

fn safe_filesystem_root_id(value: &str) -> bool {
    value.len() == MAX_FILESYSTEM_ROOT_ID_BYTES
        && value.starts_with("fs-root:")
        && value.as_bytes()[8..]
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
}

fn safe_shape_reference(value: &str) -> bool {
    value.len() <= MAX_SHAPE_REF_BYTES && ShapeRef::parse(value).is_ok()
}

/// A conservative, display-safe subset of canonical network origins. Values
/// containing userinfo, query, fragment, path, escapes, or non-canonical host
/// syntax are redacted rather than echoed. This does not validate network
/// readiness or grant authority.
fn safe_network_origin(value: &str) -> bool {
    if value.len() > MAX_NETWORK_ORIGIN_BYTES
        || !value.is_ascii()
        || value
            .bytes()
            .any(|byte| matches!(byte, b'@' | b'?' | b'#' | b'%' | b'\\'))
    {
        return false;
    }
    let (scheme, authority) = if let Some(authority) = value.strip_prefix("https://") {
        ("https", authority)
    } else if let Some(authority) = value.strip_prefix("http://") {
        ("http", authority)
    } else {
        return false;
    };
    if authority.is_empty() || authority.contains('/') {
        return false;
    }

    let port = if let Some(bracketed) = authority.strip_prefix('[') {
        let Some((address, suffix)) = bracketed.split_once(']') else {
            return false;
        };
        if address.parse::<Ipv6Addr>().is_err() {
            return false;
        }
        if suffix.is_empty() {
            None
        } else if let Some(port) = suffix.strip_prefix(':') {
            Some(port)
        } else {
            return false;
        }
    } else {
        let mut split = authority.split(':');
        let host = split.next().unwrap_or_default();
        let port = split.next();
        if split.next().is_some() || !safe_dns_or_ipv4_host(host) {
            return false;
        }
        port
    };
    if let Some(port) = port {
        if port.is_empty()
            || !port.bytes().all(|byte| byte.is_ascii_digit())
            || port.parse::<u16>().is_err()
            || port.len() > 1 && port.starts_with('0')
        {
            return false;
        }
        let default_port = if scheme == "https" { "443" } else { "80" };
        if port == default_port {
            return false;
        }
    }
    true
}

fn safe_dns_or_ipv4_host(host: &str) -> bool {
    if host
        .bytes()
        .all(|byte| byte.is_ascii_digit() || byte == b'.')
    {
        return host.parse::<Ipv4Addr>().is_ok();
    }
    if host.is_empty()
        || host.starts_with('.')
        || host.ends_with('.')
        || host.contains("..")
        || !host.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'.' || byte == b'-'
        })
    {
        return false;
    }
    host.split('.').all(|label| {
        !label.is_empty() && !label.starts_with('-') && !label.ends_with('-') && label.len() <= 63
    })
}

#[cfg(test)]
mod tests {
    use std::{
        collections::BTreeSet,
        sync::{
            Arc,
            atomic::{AtomicUsize, Ordering},
        },
    };

    use async_trait::async_trait;
    use tobkiri_canon::{
        CanonValue, Digest,
        ids::{PackHash, UiSessionId},
    };
    use tobkiri_gate::{
        ApprovalAnswer, ApprovalPrompt, FsPermission, HostError, HostIo, SecretPermission,
        TrustedPrompt, UiEmbed,
    };

    use super::*;

    #[derive(Default)]
    struct ReviewHost {
        prompts: AtomicUsize,
        embeds: AtomicUsize,
    }

    #[async_trait]
    impl TrustedPrompt for ReviewHost {
        async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
            self.prompts.fetch_add(1, Ordering::SeqCst);
            ApprovalAnswer::Deny
        }
    }

    #[async_trait]
    impl HostIo for ReviewHost {
        fn display(&self, _: &str, _: &CanonValue) {}
        async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
            Err(HostError("unavailable".into()))
        }
        fn terminal_write(&self, _: &str) {}
        fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
            self.embeds.fetch_add(1, Ordering::SeqCst);
            Err(HostError("unavailable".into()))
        }
        async fn pick_directory(&self, _: &str) -> Option<std::path::PathBuf> {
            None
        }
    }

    fn fixture() -> (
        Core,
        Arc<ReviewHost>,
        Profile,
        tempfile::TempDir,
        std::path::PathBuf,
    ) {
        let directory = tempfile::tempdir().unwrap();
        let physical = std::fs::canonicalize(directory.path()).unwrap();
        let permissions = crate::test_support::permissions(&["review"]);
        let pack =
            crate::test_support::runtime_pack(&physical, "review", permissions.clone(), false);
        let profile = crate::test_support::runtime_profile(&physical, &[pack], permissions, false);
        let data_root = physical.join("data");
        let host = Arc::new(ReviewHost::default());
        let core = Core::open(
            crate::CoreConfig::for_test_data_root(data_root.clone()),
            host.clone(),
        )
        .unwrap();
        (core, host, profile, directory, data_root)
    }

    #[test]
    fn review_binds_verified_pins_permissions_and_persisted_revision_recipe() {
        let (core, _, profile, _directory, _) = fixture();
        let review = core.review_profile(&profile).unwrap();
        let persisted_recipe = crate::profiles::strict_bytes(&profile).unwrap();
        let expected = Digest::hash(&persisted_recipe);

        assert_eq!(review.candidate.profile_id, profile.id.as_str());
        assert_eq!(review.candidate.revision, expected.as_str());
        assert_eq!(review.packs.len(), 1);
        assert_eq!(review.packs[0].id, profile.packs[0].id.as_str());
        assert_eq!(review.packs[0].version, profile.packs[0].version);
        assert_eq!(
            review.packs[0].artifact_hash,
            profile.packs[0].sha256.as_str()
        );
        assert_eq!(review.packs[0].manifest_format, "tobkiri.pack/1");
        assert!(review.packs[0].publisher_key.is_none());
        assert_eq!(review.packs[0].declared_permissions.port_shapes.len(), 2);
        assert!(
            review.packs[0]
                .declared_permissions
                .port_shapes
                .iter()
                .all(|target| target.status == TargetReviewStatus::Exact)
        );
        assert!(review.conflicts.is_empty());
    }

    #[test]
    fn altered_pin_is_refused_before_a_review_is_returned() {
        let (core, _, mut profile, _directory, _) = fixture();
        profile.packs[0].sha256 = PackHash::new(Digest::hash(b"altered pin"));

        assert!(matches!(
            core.review_profile(&profile),
            Err(ProfileReviewError::Core(CoreError::Invalid(_)))
        ));
    }

    #[test]
    fn structural_conflict_review_is_read_only_and_never_prompts_or_embeds() {
        let (core, host, mut profile, _directory, data_root) = fixture();
        profile
            .pack_limits
            .get_mut(&profile.packs[0].id)
            .unwrap()
            .native = true;
        let profile_directory = data_root.join("profiles").join(profile.id.as_str());
        assert!(!profile_directory.exists());

        let review = core.review_profile(&profile).unwrap();

        assert!(
            review
                .conflicts
                .iter()
                .any(|conflict| conflict.code == "permission-widening")
        );
        assert!(!profile_directory.exists());
        assert_eq!(host.prompts.load(Ordering::SeqCst), 0);
        assert_eq!(host.embeds.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn permission_review_preserves_targets_but_redacts_unsafe_network_scopes() {
        let mut permissions = PermissionSet::default();
        permissions.network.origins = BTreeSet::from([
            "https://example.test".into(),
            "https://user:secret@example.test".into(),
            "https://example.test?token=value".into(),
            "https://example.test#fragment".into(),
            "https://example.test/private/path".into(),
        ]);
        permissions.fs.push(FsPermission {
            root: format!("fs-root:{}", "a".repeat(64)),
            read: true,
            write: false,
        });
        permissions.proc.insert("search-indexer".into());
        permissions.secrets.push(SecretPermission {
            name: "provider-token".into(),
            reveal: false,
            optional: true,
        });
        permissions.ports.insert("local.review.input@1.0".into());

        let review = review_permissions(&permissions).unwrap();

        let origins = &review.network_origins;
        assert_eq!(origins.len(), 5);
        assert!(origins.iter().any(|origin| {
            origin.value.as_deref() == Some("https://example.test")
                && origin.status == TargetReviewStatus::Exact
        }));
        assert_eq!(
            origins
                .iter()
                .filter(|origin| origin.status == TargetReviewStatus::RedactedInvalid)
                .count(),
            4
        );
        let expected_root = format!("fs-root:{}", "a".repeat(64));
        assert_eq!(
            review.filesystem[0].root_id.value.as_deref(),
            Some(expected_root.as_str())
        );
        assert_eq!(
            review.process_ids[0].value.as_deref(),
            Some("search-indexer")
        );
        assert_eq!(
            review.secrets[0].name.value.as_deref(),
            Some("provider-token")
        );
        assert!(!review.secrets[0].reveal);
        assert_eq!(
            review.port_shapes[0].value.as_deref(),
            Some("local.review.input@1.0")
        );
    }

    #[test]
    fn oversized_pack_review_fails_as_capacity_before_loading_artifacts() {
        let (core, _, mut profile, _directory, _) = fixture();
        let limit = MAX_REVIEW_BYTES / size_of::<ProfileReviewPack>() + 1;
        let template = profile.packs[0].clone();
        profile.packs.resize(limit, template);

        assert!(matches!(
            core.review_profile(&profile),
            Err(ProfileReviewError::Capacity(
                ProfileReviewCapacity::PackCount
            ))
        ));
    }
}
