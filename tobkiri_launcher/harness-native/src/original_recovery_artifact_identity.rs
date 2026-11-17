//! Metadata fence for the committed Profile's selected packaged artifact.
use super::shell_file::open_shell_file;
use super::shell_inputs::{MAX_NAME, single_leaf};
use super::shell_root::{open_parent, open_shell_directory, stat_at, stat_identity, stat_mode};
use super::shell_tree::MAX_TREE_DEPTH;
use super::*;

#[path = "original_recovery_artifact_member.rs"]
mod member;
pub(super) use member::SelectedPackage;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const TIMEOUT: Duration = Duration::from_secs(5);
const CHANGED: &str = "active Profile Shell artifact changed after verification";
const UNAVAILABLE: &str = "active Profile Shell artifact identity is unavailable";
const SYMLINK: &str = "active Profile Shell artifact identity contains a symlink";
const ROOT_UNAVAILABLE: &str = "active Profile Shell artifact root is unavailable";
const PARENT_UNAVAILABLE: &str = "active Profile Shell artifact parent is unavailable";
const MEMBER_UNAVAILABLE: &str = "active Profile Shell artifact member is unavailable";
const STREAM_CHUNK: usize = 1024 * 1024;

/// The selected spelling and exact ordered lstat evidence. Owns no descriptors.
pub(in crate::original_authority::io::publication) struct ArtifactTreeIdentity {
    root_end: usize,
    path_end: usize,
    rows: Buffer<Entry>,
    names: Buffer<u8>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Role {
    Root,
    Prefix,
    Tree(usize),
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Entry {
    role: Role,
    name_end: usize,
    metadata: Metadata,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
struct Metadata {
    device: u64,
    inode: u64,
    mode: u32,
    size: i64,
    links: u64,
    mtime: (libc::time_t, libc::c_long),
    ctime: (libc::time_t, libc::c_long),
}

impl Metadata {
    fn from_stat(stat: &libc::stat) -> Self {
        fn link_count<T: Into<u64>>(count: T) -> u64 {
            count.into()
        }
        Self {
            device: device_bits(stat.st_dev),
            inode: stat.st_ino,
            mode: mode_bits(stat.st_mode),
            size: stat.st_size,
            links: link_count(stat.st_nlink),
            mtime: (stat.st_mtime, stat.st_mtime_nsec),
            ctime: (stat.st_ctime, stat.st_ctime_nsec),
        }
    }
}

/// Funding stays with the allocated vector and includes both buffers on growth.
struct Buffer<T> {
    values: Vec<T>,
    capacity: NativeSetupReservation,
}

impl<T> Buffer<T> {
    fn new<S: OriginalAuthorityOwner>(op: &Operation<'_, S>) -> Result<Self> {
        Ok(Self {
            values: Vec::new(),
            capacity: op.reserve(STAGE, 1)?,
        })
    }

    fn reserve(&mut self, additional: usize, budget: &Arc<dyn AllocationBudget>) -> Result<()> {
        let current = self.values.capacity();
        let needed = self
            .values
            .len()
            .checked_add(additional)
            .ok_or_else(exhausted)?;
        if needed <= current {
            return Ok(());
        }
        let next = needed
            .max(4)
            .max(current.checked_mul(2).ok_or_else(exhausted)?);
        let bytes = |count: usize| count.checked_mul(size_of::<T>()).ok_or_else(exhausted);
        let peak = bytes(current.checked_add(next).ok_or_else(exhausted)?)?;
        charge_work(budget, bytes(current)? as u64)?;
        self.capacity.resize(peak as u64).map_err(|_| exhausted())?;
        self.values
            .try_reserve_exact(next - self.values.len())
            .map_err(|_| exhausted())?;
        if self.values.capacity() != next {
            return Err(exhausted());
        }
        self.capacity
            .resize(bytes(next)? as u64)
            .map_err(|_| exhausted())
    }

    fn push(&mut self, value: T, budget: &Arc<dyn AllocationBudget>) -> Result<()> {
        self.reserve(1, budget)?;
        self.values.push(value);
        Ok(())
    }
}

impl Buffer<u8> {
    fn append(&mut self, bytes: &[u8], budget: &Arc<dyn AllocationBudget>) -> Result<()> {
        self.reserve(bytes.len(), budget)?;
        self.values.extend_from_slice(bytes);
        Ok(())
    }
}

impl ArtifactTreeIdentity {
    fn record(
        &mut self,
        role: Role,
        name: &str,
        metadata: Metadata,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<()> {
        self.names.append(name.as_bytes(), budget)?;
        self.rows.push(
            Entry {
                role,
                name_end: self.names.values.len(),
                metadata,
            },
            budget,
        )
    }
}

/// Capture a variant admitted by the loaded catalog, relative to its pinned
/// artifact parent. A prior physical Shell walk can also pin the two leaves.
pub(in crate::original_authority::io::publication) fn capture<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &Path,
    relative: &str,
    expected_parent: Identity,
    expected_shell: Option<(Identity, Identity)>,
) -> Result<ArtifactTreeIdentity> {
    capture_for(
        op,
        budget,
        root,
        relative,
        expected_parent,
        expected_shell,
        None,
    )
}

fn capture_for<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &Path,
    relative: &str,
    expected_parent: Identity,
    expected_shell: Option<(Identity, Identity)>,
    active_digest: Option<&str>,
) -> Result<ArtifactTreeIdentity> {
    bounded(op, identity_timeout, |op| {
        let mut snapshot = ArtifactTreeIdentity {
            root_end: 0,
            path_end: 0,
            rows: Buffer::new(op)?,
            names: Buffer::new(op)?,
        };
        let result = walk_tree(
            op,
            budget,
            root,
            relative,
            expected_parent,
            expected_shell,
            &mut Evidence::Capture(&mut snapshot, active_digest),
        );
        if op.cleanup.uncertain.load(Ordering::Acquire) {
            return Err(OriginalAuthorityError::unavailable());
        }
        result?;
        op.check(STAGE)?;
        Ok(snapshot)
    })
}

/// Verify a package outside the publication lock using the same rooted reads
/// and member checks as committed recovery. The caller sets a shared deadline.
pub(super) fn verify_active_package<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &Path,
    parent: Identity,
    variant: &CoreValue,
) -> Result<()> {
    let relative = variant
        .get_key("relative_path")
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let relative = relative
        .as_str()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    charge_work(budget, (relative.len() as u64).saturating_mul(2))?;
    if relative.starts_with('/') || relative_parts(relative).any(|part| part == "..") {
        return Err(artifact_rejected("packaged artifact path escapes its root"));
    }
    let digest = variant
        .get_key("artifact_digest")
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let digest = digest
        .as_str()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    let identity = capture_for(op, budget, root, relative, parent, None, Some(digest))?;
    SelectedPackage::new(root, relative, parent, &identity).verify(op, budget, variant)
}

/// Hash only the regular files named by the original rooted metadata pass.
/// Each read opens the exact member and rechecks its full evidence afterward.
pub(super) fn verify_tree_digest<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &Path,
    relative: &str,
    expected_parent: Identity,
    expected: &ArtifactTreeIdentity,
    digest: &str,
) -> Result<()> {
    bounded(
        op,
        || artifact_rejected("packaged artifact verification deadline exceeded"),
        |op| {
            op.check(STAGE)?;
            let hex = require_real_digest(digest)?;
            let mut scratch = Buffer::new(op)?;
            scratch.reserve(STREAM_CHUNK, budget)?;
            scratch.values.resize(STREAM_CHUNK, 0);
            let mut verification = Verification {
                expected,
                row: 0,
                name_start: expected.path_end,
                digest: Sha256::new(),
                scratch,
            };
            let result = walk_tree(
                op,
                budget,
                root,
                relative,
                expected_parent,
                None,
                &mut Evidence::Verify(&mut verification),
            );
            if op.cleanup.uncertain.load(Ordering::Acquire) {
                return Err(OriginalAuthorityError::unavailable());
            }
            result?;
            op.check(STAGE)?;
            if verification.row != expected.rows.values.len() {
                return Err(denied(CHANGED));
            }
            let actual = verification.digest.finalize();
            let alphabet = b"0123456789abcdef";
            let encoded = actual.iter().flat_map(|byte| {
                [
                    alphabet[(byte >> 4) as usize],
                    alphabet[(byte & 15) as usize],
                ]
            });
            if !digest.starts_with("sha256:") || !encoded.eq(hex.iter().copied()) {
                return Err(artifact_rejected(
                    "packaged artifact digest does not match selected bytes",
                ));
            }
            Ok(())
        },
    )
}

fn require_real_digest(digest: &str) -> Result<&[u8]> {
    let hex = digest.strip_prefix("sha256:").unwrap_or(digest).as_bytes();
    if hex
        .first()
        .is_none_or(|first| hex.iter().all(|byte| byte == first))
    {
        return Err(artifact_rejected(
            "packaged artifact uses a sentinel digest",
        ));
    }
    Ok(hex)
}

/// Compare exact evidence, with bounded work and no content reads.
pub(in crate::original_authority::io::publication) fn require_same<
    S: crate::BootstrapReviewOwner,
>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    expected: &ArtifactTreeIdentity,
    actual: &ArtifactTreeIdentity,
) -> Result<()> {
    bounded(op, identity_timeout, |op| {
        op.check(STAGE)?;
        if expected.root_end != actual.root_end || expected.path_end != actual.path_end {
            return Err(denied(CHANGED));
        }
        compare(op, budget, &expected.rows.values, &actual.rows.values)?;
        compare(op, budget, &expected.names.values, &actual.names.values)?;
        op.check(STAGE)
    })
}

fn compare<S: OriginalAuthorityOwner, T: PartialEq>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    expected: &[T],
    actual: &[T],
) -> Result<()> {
    if expected.len() != actual.len() {
        return Err(denied(CHANGED));
    }
    let chunk = 16 * 1024 / size_of::<T>();
    for (expected, actual) in expected.chunks(chunk).zip(actual.chunks(chunk)) {
        op.check(STAGE)?;
        charge_work(budget, std::mem::size_of_val(expected) as u64)?;
        if expected != actual {
            return Err(denied(CHANGED));
        }
    }
    Ok(())
}

pub(super) fn bounded<S, T>(
    op: &Operation<'_, S>,
    timeout: impl FnOnce() -> OriginalAuthorityError,
    run: impl FnOnce(&Operation<'_, S>) -> Result<T>,
) -> Result<T> {
    let deadline = Instant::now() + TIMEOUT;
    let scoped = Operation {
        owner: op.owner,
        invocation: op.invocation,
        cancellation: op.cancellation,
        deadline: op.deadline.min(deadline),
        pid: op.pid,
        cleanup: op.cleanup.clone(),
        #[cfg(test)]
        fixture: op.fixture,
    };
    run(&scoped).map_err(|error| {
        if scoped.deadline == deadline
            && matches!(error.cause, Some(OriginalAuthorityCause::Deadline))
        {
            timeout()
        } else {
            error
        }
    })
}

fn walk_tree<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    root: &Path,
    relative: &str,
    expected_parent: Identity,
    expected_shell: Option<(Identity, Identity)>,
    evidence: &mut Evidence<'_, '_>,
) -> Result<()> {
    let path_limit = libc::PATH_MAX as usize;
    if root.as_os_str().len().saturating_add(relative.len()) >= path_limit - 1 {
        return Err(OriginalAuthorityError::capacity(
            OriginalAuthorityStage::Path,
        ));
    }
    let parent_path = root
        .parent()
        .filter(|path| path.is_absolute())
        .ok_or_else(|| denied(ROOT_UNAVAILABLE))?;
    let root_name = root
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| single_leaf(name))
        .ok_or_else(|| denied(ROOT_UNAVAILABLE))?;
    if relative.starts_with('/') || !relative_parts(relative).all(single_leaf) {
        return Err(denied("active Profile Shell artifact is unavailable"));
    }

    // Current diagnostic path, its error copy, and temporary leaf names.
    let _scratch = op.reserve(OriginalAuthorityStage::Path, path_limit * 4)?;
    let mut path = PathBuf::new();
    path.try_reserve_exact(path_limit)
        .map_err(|_| exhausted())?;
    if path.capacity() > path_limit {
        return Err(exhausted());
    }
    path.push(root);
    for part in relative_parts(relative) {
        path.push(part);
    }
    let artifact_end = path.as_os_str().len();
    evidence.path(root, &path, budget)?;
    path.clear();
    path.push(root);
    let mut walk = Walk {
        op,
        budget,
        path,
        artifact_end,
    };

    let anchor = walk.open_anchor(parent_path, expected_parent)?;
    let stat = evidence.stat(walk.raw_stat(&anchor, root_name), PackageLocation::Root)?;
    let metadata = Metadata::from_stat(&stat);
    evidence.record(Role::Root, "", metadata, budget)?;
    if stat_mode(&stat) != mode_bits(libc::S_IFDIR) {
        return Err(evidence.unavailable(PackageLocation::Root));
    }
    if expected_shell.is_some_and(|(root, _)| root != stat_identity(&stat)) {
        return Err(denied(CHANGED));
    }
    let mut parents = Buffer::new(op)?;
    parents.push(
        walk.open_directory(&anchor, root_name, &stat, false)?,
        budget,
    )?;
    let mut parts = relative_parts(relative).peekable();
    let expected_artifact = expected_shell.map(|(_, artifact)| artifact);
    if parts.peek().is_none() {
        walk.path.pop();
        walk.tree(&anchor, root_name, expected_artifact, 0, evidence)?;
        walk.push_path(root_name)?;
    }
    while let Some(name) = parts.next() {
        let parent = &parents.values.last().expect("artifact root").file;
        if parts.peek().is_none() {
            walk.tree(
                parent,
                name,
                expected_artifact,
                parents.values.len(),
                evidence,
            )?;
            break;
        }
        if parents.values.len() == MAX_TREE_DEPTH {
            return Err(exhausted());
        }
        walk.push_path(name)?;
        let stat = evidence.stat(walk.raw_stat(parent, name), PackageLocation::Path)?;
        evidence.record(Role::Prefix, name, Metadata::from_stat(&stat), budget)?;
        if stat_mode(&stat) != mode_bits(libc::S_IFDIR) {
            return Err(evidence.unavailable(PackageLocation::Path));
        }
        parents.push(walk.open_directory(parent, name, &stat, false)?, budget)?;
    }
    while let Some(directory) = parents.values.pop() {
        walk.check_directory(&directory)?;
        walk.path.pop();
    }
    // The retained ancestors pinned each read. Confirm the externally named
    // parent still reaches that same anchor after the traversal.
    drop(walk.open_anchor(parent_path, expected_parent)?);
    Ok(())
}

fn relative_parts(relative: &str) -> impl Iterator<Item = &str> {
    relative
        .split('/')
        .filter(|name| !matches!(*name, "" | "."))
}

enum Evidence<'a, 'snapshot> {
    Capture(&'a mut ArtifactTreeIdentity, Option<&'snapshot str>),
    Verify(&'a mut Verification<'snapshot>),
}

#[derive(Clone, Copy)]
enum PackageLocation {
    Root,
    Path,
    Selected,
    Descendant,
}

impl Evidence<'_, '_> {
    fn active_digest(&self) -> Option<&str> {
        match self {
            Self::Capture(_, digest) => *digest,
            Self::Verify(_) => None,
        }
    }

    fn stat(&self, result: Result<libc::stat>, location: PackageLocation) -> Result<libc::stat> {
        let Some(digest) = self.active_digest() else {
            return require_no_symlink(result.map_err(artifact_io)?);
        };
        match result {
            Ok(stat) if is_symlink(&stat) => Err(artifact_rejected(match location {
                PackageLocation::Root => "packaged artifact root must be a real directory",
                PackageLocation::Path | PackageLocation::Selected => {
                    "packaged artifact path contains a symlink"
                }
                PackageLocation::Descendant => "packaged artifact tree contains a symlink",
            })),
            Err(error)
                if error.class == RAW_IO
                    && !matches!(location, PackageLocation::Root)
                    && matches!(&error.cause, Some(OriginalAuthorityCause::Io(io))
                        if matches!(io.kind, std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory)) =>
            {
                Err(self.unavailable(location))
            }
            result => {
                if matches!(location, PackageLocation::Selected) {
                    require_real_digest(digest)?;
                }
                result
            }
        }
    }

    fn unavailable(&self, location: PackageLocation) -> OriginalAuthorityError {
        if let Some(digest) = self.active_digest() {
            if matches!(location, PackageLocation::Root) {
                return artifact_rejected("packaged artifact root must be a real directory");
            }
            return require_real_digest(digest)
                .err()
                .unwrap_or_else(|| artifact_rejected("packaged artifact entry is unavailable"));
        }
        denied(match location {
            PackageLocation::Root => ROOT_UNAVAILABLE,
            PackageLocation::Path => PARENT_UNAVAILABLE,
            PackageLocation::Selected | PackageLocation::Descendant => MEMBER_UNAVAILABLE,
        })
    }

    fn path(&mut self, root: &Path, path: &Path, budget: &Arc<dyn AllocationBudget>) -> Result<()> {
        let bytes = path.as_os_str().as_bytes();
        let root_end = root.as_os_str().len();
        charge_work(budget, bytes.len() as u64)?;
        match self {
            Self::Capture(snapshot, _) => {
                snapshot.root_end = root_end;
                snapshot.path_end = bytes.len();
                snapshot.names.append(bytes, budget)
            }
            Self::Verify(verification) => {
                let expected = verification.expected;
                if expected.root_end != root_end
                    || expected.names.values[..expected.path_end] != *bytes
                {
                    return Err(denied(CHANGED));
                }
                Ok(())
            }
        }
    }

    fn record(
        &mut self,
        role: Role,
        name: &str,
        metadata: Metadata,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<()> {
        match self {
            Self::Capture(snapshot, _) => snapshot.record(role, name, metadata, budget),
            Self::Verify(verification) => {
                let expected = verification.expected;
                let row = expected
                    .rows
                    .values
                    .get(verification.row)
                    .ok_or_else(|| denied(CHANGED))?;
                if row.role != role
                    || row.metadata != metadata
                    || expected.names.values[verification.name_start..row.name_end]
                        != *name.as_bytes()
                {
                    return Err(denied(CHANGED));
                }
                verification.row += 1;
                verification.name_start = row.name_end;
                Ok(())
            }
        }
    }
}

struct Verification<'a> {
    expected: &'a ArtifactTreeIdentity,
    row: usize,
    name_start: usize,
    digest: Sha256,
    scratch: Buffer<u8>,
}

struct Walk<'a, 'op, S> {
    op: &'a Operation<'op, S>,
    budget: &'a Arc<dyn AllocationBudget>,
    path: PathBuf,
    artifact_end: usize,
}

struct DirectoryEvidence {
    file: TrackedFile,
    metadata: Metadata,
}

struct Frame {
    directory: DirectoryEvidence,
    listing: Listing,
}

struct Listing {
    names: Buffer<std::ops::Range<usize>>,
    bytes: Buffer<u8>,
    next: usize,
}

impl Listing {
    fn next(&mut self) -> Option<&str> {
        let range = self.names.values.get(self.next)?.clone();
        self.next += 1;
        Some(std::str::from_utf8(&self.bytes.values[range]).expect("admitted member name"))
    }
}

impl<S: crate::BootstrapReviewOwner> Walk<'_, '_, S> {
    fn push_path(&mut self, name: &str) -> Result<()> {
        if self.path.as_os_str().len() + 1 + name.len() >= libc::PATH_MAX as usize {
            return Err(OriginalAuthorityError::capacity(
                OriginalAuthorityStage::Path,
            ));
        }
        self.path.push(name);
        Ok(())
    }

    fn open_anchor(&self, path: &Path, expected: Identity) -> Result<TrackedFile> {
        charge_work(self.budget, path.as_os_str().len() as u64)?;
        let parent = open_parent(self.op, path)?;
        self.op.check(STAGE)?;
        if !parent
            .metadata()
            .is_ok_and(|metadata| identity(&metadata) == expected)
        {
            return Err(OriginalAuthorityError::unavailable());
        }
        Ok(parent)
    }

    fn stat(&self, parent: &TrackedFile, name: &str) -> Result<libc::stat> {
        require_no_symlink(self.raw_stat(parent, name).map_err(artifact_io)?)
    }

    fn raw_stat(&self, parent: &TrackedFile, name: &str) -> Result<libc::stat> {
        charge_work(self.budget, (name.len() + size_of::<Metadata>()) as u64)?;
        let name = shell_root::checked_name(name)?;
        let stat = stat_at(self.op, parent, &name, || self.path.clone())?;
        self.op.check(STAGE)?;
        Ok(stat)
    }

    fn open_directory(
        &self,
        parent: &TrackedFile,
        name: &str,
        stat: &libc::stat,
        enumerate: bool,
    ) -> Result<DirectoryEvidence> {
        let directory = DirectoryEvidence {
            file: open_shell_directory(
                self.op,
                parent,
                &self.path,
                name,
                stat_identity(stat),
                enumerate,
            )
            .map_err(artifact_io)?,
            metadata: Metadata::from_stat(stat),
        };
        self.check_directory(&directory)?;
        Ok(directory)
    }

    fn check_directory(&self, directory: &DirectoryEvidence) -> Result<()> {
        if self.descriptor_metadata(&directory.file)? != directory.metadata {
            return Err(denied(CHANGED));
        }
        Ok(())
    }

    fn descriptor_metadata(&self, file: &TrackedFile) -> Result<Metadata> {
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        loop {
            self.op.check(STAGE)?;
            if unsafe { libc::fstat(file.as_raw_fd(), stat.as_mut_ptr()) } == 0 {
                break;
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(artifact_io(raw_io(error, self.path.to_str())));
            }
        }
        self.op.check(STAGE)?;
        Ok(Metadata::from_stat(&unsafe { stat.assume_init() }))
    }

    fn tree(
        &mut self,
        parent: &TrackedFile,
        selected: &str,
        expected: Option<Identity>,
        prefix_depth: usize,
        evidence: &mut Evidence<'_, '_>,
    ) -> Result<()> {
        self.push_path(selected)?;
        let Some(frame) = self.visit(parent, selected, 0, expected, evidence)? else {
            self.path.pop();
            return Ok(());
        };
        let mut stack = Buffer::new(self.op)?;
        stack.push(frame, self.budget)?;
        while !stack.values.is_empty() {
            let depth = stack.values.len();
            let frame = stack.values.last_mut().expect("directory walk frame");
            match frame.listing.next() {
                Some(name) => {
                    if prefix_depth + depth > MAX_TREE_DEPTH {
                        return Err(exhausted());
                    }
                    self.push_path(name)?;
                    match self.visit(&frame.directory.file, name, depth, None, evidence)? {
                        Some(child) => stack.push(child, self.budget)?,
                        None => {
                            self.path.pop();
                        }
                    }
                }
                None => {
                    self.check_directory(&frame.directory)?;
                    stack.values.pop();
                    self.path.pop();
                }
            }
        }
        Ok(())
    }

    fn visit(
        &self,
        parent: &TrackedFile,
        name: &str,
        depth: usize,
        expected: Option<Identity>,
        evidence: &mut Evidence<'_, '_>,
    ) -> Result<Option<Frame>> {
        let location = if depth == 0 {
            PackageLocation::Selected
        } else {
            PackageLocation::Descendant
        };
        let stat = evidence.stat(self.raw_stat(parent, name), location)?;
        if expected.is_some_and(|expected| expected != stat_identity(&stat)) {
            return Err(denied(CHANGED));
        }
        // Python records the selected artifact itself as ".", then sorted
        // descendant paths. Here depth and component name encode those paths.
        let metadata = Metadata::from_stat(&stat);
        evidence.record(
            Role::Tree(depth),
            if depth == 0 { "." } else { name },
            metadata,
            self.budget,
        )?;
        let kind = stat_mode(&stat);
        if kind == mode_bits(libc::S_IFREG) {
            if let Evidence::Verify(verification) = evidence {
                self.hash_file(parent, name, &stat, metadata, verification)?;
            }
            return Ok(None);
        }
        if kind != mode_bits(libc::S_IFDIR) {
            return Err(evidence.unavailable(location));
        }
        let directory = self.open_directory(parent, name, &stat, true)?;
        let listing = self.list(&directory.file)?;
        Ok(Some(Frame { directory, listing }))
    }

    fn hash_file(
        &self,
        directory: &TrackedFile,
        name: &str,
        stat: &libc::stat,
        expected: Metadata,
        verification: &mut Verification<'_>,
    ) -> Result<()> {
        let relative = self
            .path
            .as_os_str()
            .as_bytes()
            .get(self.artifact_end + 1..)
            .unwrap_or_default();
        charge_work(self.budget, relative.len() as u64 + 1)?;
        let name = shell_root::checked_name(name)?;
        let mut file = open_shell_file(
            self.op,
            directory,
            self.path.parent().expect("member parent"),
            &name,
            stat_identity(stat),
        )
        .map_err(artifact_io)?;
        verification.digest.update(relative);
        verification.digest.update(b"\0");
        self.hash_contents(
            &mut file,
            expected,
            &mut verification.scratch.values,
            &mut verification.digest,
        )?;
        file.close()
            .map_err(|_| OriginalAuthorityError::unavailable())
    }

    fn hash_contents(
        &self,
        file: &mut TrackedFile,
        expected: Metadata,
        buffer: &mut [u8],
        digest: &mut Sha256,
    ) -> Result<()> {
        let changed = || artifact_rejected("packaged artifact changed while it was verified");
        let expected_size = u64::try_from(expected.size).map_err(|_| changed())?;
        if self.descriptor_metadata(file)? != expected {
            return Err(changed());
        }
        let mut size = 0;
        loop {
            self.op.check(STAGE)?;
            let count = match file.read(buffer) {
                Ok(0) => break,
                Ok(count) => count,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(artifact_io(raw_io(error, self.path.to_str()))),
            };
            size += count as u64;
            if size > expected_size {
                return Err(changed());
            }
            charge_work(self.budget, count as u64)?;
            digest.update(&buffer[..count]);
        }
        if size != expected_size || self.descriptor_metadata(file)? != expected {
            return Err(changed());
        }
        Ok(())
    }

    fn list(&self, file: &TrackedFile) -> Result<Listing> {
        let mut listing = Listing {
            names: Buffer::new(self.op)?,
            bytes: Buffer::new(self.op)?,
            next: 0,
        };
        {
            // One libc directory stream is live at a time. Keep its buffer
            // funded separately from the already enumerated directory names.
            let _directory_stream = self.op.reserve(STAGE, 1024 * 1024)?;
            let io = |error| artifact_io(raw_io(error, self.path.to_str()));
            let mut directory = file
                .try_clone()
                .and_then(|file| file.into_directory())
                .map_err(io)?;
            loop {
                self.op.check(STAGE)?;
                let Some(name) = directory.next_name().map_err(io)? else {
                    break;
                };
                let name = name.to_str().map_err(|_| {
                    OriginalAuthorityError::unsupported(STAGE, "non-UTF-8 Shell directory member")
                })?;
                if !single_leaf(name) {
                    return Err(denied(MEMBER_UNAVAILABLE));
                }
                charge_work(self.budget, name.len() as u64 + 1)?;
                let start = listing.bytes.values.len();
                listing.bytes.append(name.as_bytes(), self.budget)?;
                listing
                    .names
                    .push(start..listing.bytes.values.len(), self.budget)?;
            }
        }
        let names = &mut listing.names.values;
        let bytes = &listing.bytes.values;
        if !names.is_empty() {
            let count = names.len() as u64;
            charge_work(
                self.budget,
                count * u64::from(count.ilog2() + 1) * (MAX_NAME as u64 + 1),
            )?;
            self.op.check(STAGE)?;
            // UTF-8 bytes preserve Unicode scalar order.
            names.sort_unstable_by(|left, right| bytes[left.clone()].cmp(&bytes[right.clone()]));
            self.op.check(STAGE)?;
            if names
                .windows(2)
                .any(|pair| bytes[pair[0].clone()] == bytes[pair[1].clone()])
            {
                return Err(denied(CHANGED));
            }
        }
        Ok(listing)
    }
}

fn is_symlink(stat: &libc::stat) -> bool {
    stat_mode(stat) == mode_bits(libc::S_IFLNK)
}

fn require_no_symlink(stat: libc::stat) -> Result<libc::stat> {
    if is_symlink(&stat) {
        return Err(denied(SYMLINK));
    }
    Ok(stat)
}

fn denied(message: &'static str) -> OriginalAuthorityError {
    raw(RECOVERY_APP_DENIED, message)
}

fn identity_timeout() -> OriginalAuthorityError {
    raw(
        "ecosystem.defaultspack.domain._profile_runtime_records.ActivationLockTimeout",
        "artifact identity verification deadline exceeded",
    )
}

pub(super) fn artifact_rejected(message: &'static str) -> OriginalAuthorityError {
    caused(
        raw(
            RECOVERY_APP_DENIED,
            format!("active Profile Shell artifact rejected: {message}"),
        ),
        raw("tobkiri_protocol.errors.ProtocolError", message),
    )
}

fn artifact_io(error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == RAW_IO {
        caused(denied(UNAVAILABLE), error)
    } else {
        error
    }
}

fn exhausted() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(STAGE)
}

#[cfg(test)]
#[path = "original_recovery_artifact_identity_tests.rs"]
mod tests;
