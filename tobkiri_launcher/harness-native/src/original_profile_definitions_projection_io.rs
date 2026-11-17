//! Physical neutral projection inventory for migration and activation recovery.
use super::*;
use sha2::{Digest as _, Sha256};
use std::{ffi::CStr, io::Read};
use tobkiri_canon::{CoreArrayBuilder, CoreValue, MAX_BYTES};

pub(super) const PROJECTION_ERROR: &str =
    "core_runtime.profile_content_projection.ProfileContentProjectionError";

pub(super) struct Retirement {
    pub(super) pack: &'static str,
    pub(super) id: &'static str,
    pub(super) kind: &'static str,
    pub(super) directory: &'static str,
    pub(super) artifact_root: &'static str,
}

// Fixed original Pack IDs in the source's sorted unique processing order.
pub(super) const RETIREMENTS: [Retirement; 4] = [
    Retirement {
        pack: "rumi_agent_services_pack",
        id: "tobkiri.profile-content.agent-services.v1",
        kind: "profile_content",
        directory: "agent-services",
        artifact_root: "profile_projections/agent-services",
    },
    Retirement {
        pack: "rumi_local_agent_pack",
        id: "tobkiri.profile-content.local-agent.v1",
        kind: "profile_content",
        directory: "local-agent",
        artifact_root: "profile_projections/local-agent",
    },
    Retirement {
        pack: "rumi_pack_suite_pack",
        id: "tobkiri.profile-content.pack-suite.v1",
        kind: "profile_content",
        directory: "pack-suite",
        artifact_root: "profile_projections/pack-suite",
    },
    Retirement {
        pack: "rumi_reference_ui_pack",
        id: "tobkiri.ui-projection.reference.v1",
        kind: "ui_projection",
        directory: "reference-ui",
        artifact_root: "profile_projections/reference-ui",
    },
];

const ROOT_UNAVAILABLE: &str = "Profile projection root is unavailable";
const UNSAFE_ROOT: &str = "Profile projection path is unsafe";
const CHANGED: &str = "Profile projection changed while being read";
const PATH_BYTES: usize = 8192;
const DEPTH: usize = 128;

enum RootStep<'a> {
    Retirement,
    Persisted(Option<&'a [u8]>),
}

pub(super) fn inventory<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    directory: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(Digest, usize)> {
    let (runtime, _paths) = selected_runtime(op, runtime)?;
    let neutral = runtime.join("profile_projections");
    let selected = neutral.join(directory);
    inventory_at(
        op,
        &runtime,
        &selected,
        budget,
        false,
        |parent, inventory| {
            with_root(
                op,
                parent,
                &neutral,
                c"profile_projections",
                RootStep::Retirement,
                |projection| {
                    let name = CString::new(directory).map_err(capacity)?;
                    with_root(
                        op,
                        projection,
                        &selected,
                        &name,
                        RootStep::Retirement,
                        |directory| inventory.walk(directory, 0),
                    )
                },
            )
        },
    )
}

/// Verify a normalized neutral artifact root selected by a persisted activation.
/// Only the original Host supplies `runtime`; artifact components never resolve links.
pub(in crate::original_authority::io::publication) fn persisted_neutral_inventory<
    S: OriginalAuthorityOwner,
>(
    op: &Operation<'_, S>,
    runtime: &Path,
    normalized_relative: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<(Digest, usize)> {
    let relative = normalized_relative.as_os_str().as_bytes();
    if relative.len() > PATH_BYTES {
        return Err(capacity(()));
    }
    let components = relative.split(|byte| *byte == b'/');
    if components.clone().next() != Some(b"profile_projections".as_slice())
        || components
            .clone()
            .any(|part| part.is_empty() || part == b"." || part == b".." || part.contains(&0))
    {
        return Err(projection_error(UNSAFE_ROOT));
    }
    if components.clone().count() > DEPTH {
        return Err(capacity(()));
    }
    let (runtime, _paths) = selected_runtime(op, runtime)?;
    let selected = runtime.join(normalized_relative);
    if selected.as_os_str().len() > PATH_BYTES {
        return Err(capacity(()));
    }
    inventory_at(
        op,
        &runtime,
        &selected,
        budget,
        true,
        |parent, inventory| {
            walk_roots(parent, &runtime, components.peekable(), inventory)?;
            // Re-observe the canonical Host root as well as the relative roots
            // checked while walk_roots unwinds its anchored descriptors.
            op.check(OriginalAuthorityStage::Source)?;
            let current = Parent::walk(&runtime, &op.cleanup)?;
            let compare = (|| {
                let observed = parent.metadata().map_err(|error| raw_io(error, None))?;
                let current = current.metadata().map_err(|error| raw_io(error, None))?;
                if identity(&observed) != identity(&current) {
                    return Err(projection_error(CHANGED));
                }
                Ok(())
            })();
            finish_scope(
                compare,
                current.close().map_err(|error| raw_io(error, None)),
            )
        },
    )
}

fn selected_runtime<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
) -> Result<(PathBuf, NativeSetupReservation)> {
    if runtime.as_os_str().len() > PATH_BYTES {
        return Err(capacity(()));
    }
    let paths = op.reserve(OriginalAuthorityStage::Source, 12 * PATH_BYTES)?;
    // As in the original module's RUNTIME_ROOT, resolve the Host path once
    // before walking any projection component with O_NOFOLLOW.
    let runtime = fs::canonicalize(runtime).map_err(|error| raw_io(error, runtime.to_str()))?;
    if runtime.as_os_str().len() > PATH_BYTES {
        return Err(capacity(()));
    }
    Ok((runtime, paths))
}

fn inventory_at<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    selected: &Path,
    budget: &Arc<dyn AllocationBudget>,
    skip_unreadable: bool,
    visit: impl FnOnce(&TrackedFile, &mut Inventory<'_, '_, S>) -> Result<()>,
) -> Result<(Digest, usize)> {
    let mut scratch = String::new();
    scratch.try_reserve_exact(PATH_BYTES).map_err(capacity)?;
    if scratch.capacity() != PATH_BYTES {
        return Err(capacity(()));
    }
    let mut inventory = Inventory {
        op,
        budget,
        files: CoreValue::array_builder_incremental(budget, MAX_BYTES).map_err(capacity)?,
        relative: scratch,
        root: selected,
        skip_unreadable,
    };
    let runtime = Parent::walk(runtime, &op.cleanup)?;
    let result = visit(&runtime, &mut inventory);
    finish_scope(result, runtime.close().map_err(|error| raw_io(error, None)))?;
    let count = inventory.files.len();
    if count == 0 {
        return Err(projection_error("Profile projection is empty"));
    }
    let files = inventory.files.finish().map_err(capacity)?;
    let digest = files.digest_guest(budget).map_err(capacity)?;
    Ok((digest, count))
}

fn walk_roots<'a, S: OriginalAuthorityOwner>(
    parent: &TrackedFile,
    path: &Path,
    mut parts: std::iter::Peekable<impl Iterator<Item = &'a [u8]>>,
    inventory: &mut Inventory<'_, '_, S>,
) -> Result<()> {
    let Some(part) = parts.next() else {
        return inventory.walk(parent, 0);
    };
    let op = inventory.op;
    let _stack = op.reserve(OriginalAuthorityStage::Source, 4096)?;
    let length = path.as_os_str().len().saturating_add(part.len() + 1);
    let _path = op.reserve(OriginalAuthorityStage::Source, length.saturating_mul(2))?;
    let path = path.join(std::ffi::OsStr::from_bytes(part));
    let name = CString::new(part).map_err(capacity)?;
    let next = parts.peek().copied();
    with_root(
        op,
        parent,
        &path,
        &name,
        RootStep::Persisted(next),
        |directory| walk_roots(directory, &path, parts, inventory),
    )
}

fn with_root<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    path: &Path,
    name: &CStr,
    step: RootStep<'_>,
    visit: impl FnOnce(&TrackedFile) -> Result<()>,
) -> Result<()> {
    op.check(OriginalAuthorityStage::Source)?;
    let stat = stat_at(parent, name).map_err(|error| {
        caused(
            projection_error(ROOT_UNAVAILABLE),
            raw_io(error, path.to_str()),
        )
    })?;
    if stat.st_mode & libc::S_IFMT == libc::S_IFLNK {
        return Err(projection_error(
            "Profile projection path contains a symlink",
        ));
    }
    if stat.st_mode & libc::S_IFMT != libc::S_IFDIR {
        let error = projection_error(ROOT_UNAVAILABLE);
        return Err(match step {
            // Python lstat accepts this leaf, then gets ENOTDIR at the next
            // component. Preserve that inner error without opening the leaf.
            RootStep::Persisted(Some(next)) => caused(
                error,
                raw_io(
                    std::io::Error::from_raw_os_error(libc::ENOTDIR),
                    path.join(std::ffi::OsStr::from_bytes(next)).to_str(),
                ),
            ),
            _ => error,
        });
    }
    let flags = if matches!(step, RootStep::Persisted(Some(_))) {
        SEARCH_DIR
    } else {
        libc::O_DIRECTORY
    };
    let dir = match open_at(op, parent, name, flags) {
        Ok(dir) => dir,
        // The original rglob skips unreadable directories. Repeat lstat to
        // distinguish an unchanged unreadable root from a replaced one.
        Err(error)
            if matches!(step, RootStep::Persisted(None))
                && error.kind() == std::io::ErrorKind::PermissionDenied =>
        {
            let current = stat_at(parent, name)
                .map_err(|error| caused(projection_error(CHANGED), raw_io(error, path.to_str())))?;
            return unchanged_unreadable(&stat, &current);
        }
        Err(error) => {
            let next_path = match step {
                RootStep::Persisted(Some(next))
                    if error.kind() == std::io::ErrorKind::PermissionDenied =>
                {
                    Some(path.join(std::ffi::OsStr::from_bytes(next)))
                }
                _ => None,
            };
            return Err(caused(
                projection_error(ROOT_UNAVAILABLE),
                raw_io(error, next_path.as_deref().unwrap_or(path).to_str()),
            ));
        }
    };
    let outcome = (|| {
        let observed = dir
            .metadata()
            .map_err(|error| caused(projection_error(ROOT_UNAVAILABLE), raw_io(error, None)))?;
        check_directory_identity(&observed, &stat)?;
        visit(&dir)?;
        let current = stat_at(parent, name).map_err(|error| {
            caused(
                projection_error(ROOT_UNAVAILABLE),
                raw_io(error, path.to_str()),
            )
        })?;
        check_directory_identity(&observed, &current)
    })();
    finish_scope(outcome, dir.close().map_err(|error| raw_io(error, None)))
}

// lstat of a deeper path requires search permission, while inventory requires
// opening the final directory for listing. Keep those permissions separate.
#[cfg(any(target_os = "linux", target_os = "android"))]
const SEARCH_DIR: i32 = libc::O_PATH | libc::O_DIRECTORY;
#[cfg(target_vendor = "apple")]
const SEARCH_DIR: i32 = libc::O_SEARCH;
#[cfg(not(any(target_os = "linux", target_os = "android", target_vendor = "apple")))]
const SEARCH_DIR: i32 = libc::O_DIRECTORY;

struct Inventory<'a, 'op, S> {
    op: &'a Operation<'op, S>,
    budget: &'a Arc<dyn AllocationBudget>,
    files: CoreArrayBuilder,
    relative: String,
    root: &'a Path,
    skip_unreadable: bool,
}

impl<S: OriginalAuthorityOwner> Inventory<'_, '_, S> {
    fn walk(&mut self, directory: &TrackedFile, depth: usize) -> Result<()> {
        if depth > DEPTH {
            return Err(capacity(()));
        }
        // Pathlib orders path components, so sorted siblings and a depth-first
        // traversal produce the same sequence even for "a/" and "a.txt".
        let _stack = self.op.reserve(OriginalAuthorityStage::Source, 4096)?;
        let mut listing = directory
            .try_clone()
            .and_then(TrackedFile::into_directory)
            .map_err(|error| self.changed(error, false))?;
        let funding = self.op.reserve(OriginalAuthorityStage::Source, 1)?;
        let mut names = Vec::new();
        let scan: Result<()> = (|| {
            loop {
                self.op.check(OriginalAuthorityStage::Source)?;
                let Some(name) = listing
                    .next_name()
                    .map_err(|error| self.changed(error, false))?
                else {
                    break;
                };
                let name = name.to_str().map_err(|_| {
                    OriginalAuthorityError::unsupported(
                        OriginalAuthorityStage::Source,
                        "non-UTF-8 Profile projection directory member",
                    )
                })?;
                self.work(name.len() as u64 + 1)?;
                push_name(
                    &funding,
                    &mut names,
                    CoreValue::string(name, self.budget).map_err(capacity)?,
                )?;
            }
            Ok(())
        })();
        drop(listing);
        if self.op.cleanup.uncertain.load(Ordering::Acquire) {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Close,
                "Profile projection directory close was not confirmed",
            ));
        }
        scan?;
        let count = names.len();
        if count > 1 {
            let longest = names
                .iter()
                .map(|name| name.as_str().expect("path member").len())
                .max()
                .unwrap_or(0);
            self.work(
                (count as u64)
                    .saturating_mul(u64::from(count.ilog2() + 1))
                    .saturating_mul(longest as u64 + 1),
            )?;
            names.sort_unstable_by(|left, right| left.as_str().cmp(&right.as_str()));
        }
        for item in names {
            self.op.check(OriginalAuthorityStage::Source)?;
            let item = item.as_str().expect("path member");
            let length = self.relative.len();
            if length + usize::from(length != 0) + item.len() > PATH_BYTES {
                return Err(capacity(()));
            }
            if length != 0 {
                self.relative.push('/');
            }
            self.relative.push_str(item);
            let name = CString::new(item).map_err(capacity)?;
            self.visit(directory, &name, depth)?;
            self.relative.truncate(length);
        }
        Ok(())
    }

    fn visit(&mut self, directory: &TrackedFile, name: &CStr, depth: usize) -> Result<()> {
        let stat = match stat_at(directory, name) {
            Ok(stat) => stat,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(raw_io(error, self.root.join(&self.relative).to_str())),
        };
        match stat.st_mode & libc::S_IFMT {
            libc::S_IFLNK => Err(projection_error("Profile projection contains a symlink")),
            libc::S_IFDIR => {
                let child = match open_at(self.op, directory, name, libc::O_DIRECTORY) {
                    Ok(child) => child,
                    Err(error)
                        if self.skip_unreadable
                            && error.kind() == std::io::ErrorKind::PermissionDenied =>
                    {
                        let current =
                            stat_at(directory, name).map_err(|error| self.changed(error, true))?;
                        return unchanged_unreadable(&stat, &current);
                    }
                    Err(error) => return Err(self.changed(error, true)),
                };
                let outcome = (|| {
                    let identity = child
                        .metadata()
                        .map_err(|error| self.changed(error, false))?;
                    check_directory_identity(&identity, &stat)?;
                    self.walk(&child, depth + 1)?;
                    let current =
                        stat_at(directory, name).map_err(|error| self.changed(error, true))?;
                    check_directory_identity(&identity, &current)
                })();
                finish_scope(
                    outcome,
                    child.close().map_err(|error| self.changed(error, false)),
                )
            }
            libc::S_IFREG => self.hash_file(directory, name),
            // Python's is_file skips nonregular entries.
            _ => Ok(()),
        }
    }

    fn hash_file(&mut self, directory: &TrackedFile, name: &CStr) -> Result<()> {
        const CHUNK: usize = 1024 * 1024;
        let _scratch = self.op.reserve(OriginalAuthorityStage::Source, CHUNK)?;
        let mut scratch = Vec::new();
        scratch.try_reserve_exact(CHUNK).map_err(capacity)?;
        if scratch.capacity() != CHUNK {
            return Err(capacity(()));
        }
        scratch.resize(CHUNK, 0);
        let mut file =
            open_at(self.op, directory, name, 0).map_err(|error| self.changed(error, true))?;
        let result = (|| {
            let before = file
                .metadata()
                .map_err(|error| self.changed(error, false))?;
            if !before.is_file() {
                return Err(projection_error(
                    "Profile projection entry is not a regular file",
                ));
            }
            let mut digest = Sha256::new();
            loop {
                self.op.check(OriginalAuthorityStage::Source)?;
                let count = match file.read(&mut scratch) {
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    result => result.map_err(|error| self.changed(error, false))?,
                };
                if count == 0 {
                    break;
                }
                self.work(count as u64)?;
                digest.update(&scratch[..count]);
            }
            let after = file
                .metadata()
                .map_err(|error| self.changed(error, false))?;
            let current = stat_at(directory, name).map_err(|error| self.changed(error, true))?;
            if !same_file(&before, &after, &current) {
                return Err(projection_error(CHANGED));
            }
            Ok(Digest::from_sha256(digest.finalize().into()))
        })();
        let digest = finish_scope(
            result,
            file.close().map_err(|error| self.changed(error, false)),
        )?;
        let value = CoreValue::object_shared(
            [
                (
                    "path",
                    CoreValue::string(&self.relative, self.budget).map_err(capacity)?,
                ),
                (
                    "digest",
                    CoreValue::string(digest.as_str(), self.budget).map_err(capacity)?,
                ),
            ]
            .into_iter()
            .map(Ok),
            self.budget,
        )
        .map_err(capacity)?;
        self.files.push(value).map_err(capacity)
    }

    fn changed(&self, error: std::io::Error, filename: bool) -> OriginalAuthorityError {
        let path = filename.then(|| self.root.join(&self.relative));
        caused(
            projection_error(CHANGED),
            raw_io(error, path.as_deref().and_then(Path::to_str)),
        )
    }

    fn work(&self, work: u64) -> Result<()> {
        self.budget
            .reserve(tobkiri_canon::AllocationRequest {
                kind: tobkiri_canon::AllocationKind::Temporary,
                bytes: 0,
                depth: 0,
                elements: 0,
                work,
            })
            .map_err(capacity)
    }
}

fn push_name(
    funding: &NativeSetupReservation,
    names: &mut Vec<CoreValue>,
    name: CoreValue,
) -> Result<()> {
    if names.len() == names.capacity() {
        let current = names.len();
        let next = current.max(4).checked_mul(2).ok_or_else(|| capacity(()))?;
        funding
            .resize((current + next).saturating_mul(size_of::<CoreValue>()) as u64)
            .map_err(capacity)?;
        names.try_reserve_exact(next - current).map_err(capacity)?;
        if names.capacity() != next {
            return Err(capacity(()));
        }
        funding
            .resize(next.saturating_mul(size_of::<CoreValue>()) as u64)
            .map_err(capacity)?;
    }
    names.push(name);
    Ok(())
}

fn stat_at(parent: &TrackedFile, name: &CStr) -> std::io::Result<libc::stat> {
    let mut stat = std::mem::MaybeUninit::uninit();
    if unsafe {
        libc::fstatat(
            parent.as_raw_fd(),
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } != 0
    {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { stat.assume_init() })
}

fn open_at<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    name: &CStr,
    flags: i32,
) -> std::io::Result<TrackedFile> {
    let descriptor = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC | flags,
        )
    };
    if descriptor < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let file = unsafe { File::from_raw_fd(descriptor) };
    Ok(TrackedFile::new(file, op.cleanup.clone()))
}

fn check_directory_identity(opened: &fs::Metadata, current: &libc::stat) -> Result<()> {
    if opened.dev() != device_bits(current.st_dev)
        || opened.ino() != current.st_ino
        || current.st_mode & libc::S_IFMT != libc::S_IFDIR
    {
        return Err(projection_error(CHANGED));
    }
    Ok(())
}

fn unchanged_unreadable(before: &libc::stat, current: &libc::stat) -> Result<()> {
    let identity = |stat: &libc::stat| {
        (
            stat.st_dev,
            stat.st_ino,
            stat.st_mode,
            stat.st_ctime,
            stat.st_ctime_nsec,
        )
    };
    if identity(before) != identity(current) {
        return Err(projection_error(CHANGED));
    }
    Ok(())
}

fn same_file(before: &fs::Metadata, after: &fs::Metadata, current: &libc::stat) -> bool {
    let identity = |value: &fs::Metadata| {
        (
            value.dev(),
            value.ino(),
            value.len(),
            value.mtime(),
            value.mtime_nsec(),
        )
    };
    identity(before) == identity(after)
        && identity(after)
            == (
                device_bits(current.st_dev),
                current.st_ino,
                current.st_size as u64,
                current.st_mtime,
                current.st_mtime_nsec,
            )
}

fn projection_error(message: &'static str) -> OriginalAuthorityError {
    raw(PROJECTION_ERROR, message)
}

fn capacity(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}
