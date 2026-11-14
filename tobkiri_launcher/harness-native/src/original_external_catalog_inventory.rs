//! The pinned Host's signed-file inventory read inside an already verified CAS root.
//! Nothing in this comparison projects authority from the package signature.
use super::*;
use sha2::{Digest as _, Sha256};
use std::{borrow::Cow, ffi::CStr};
use tobkiri_canon::{CanonValue, Digest};

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const SIGNED_MANIFEST: &[u8] = b".tobkiri/signed-pack.json";
const EMPTY: &str = "external Normal Pack signed file inventory is empty";
const INVENTORY_CHANGED: &str = "external Normal Pack artifact file inventory changed";
const DIGEST_CHANGED: &str = "external Normal Pack artifact file digest changed";
const SYMLINK: &str = "external Normal Pack artifact contains a symlink";
const FILE_CHANGED: &str = "signed Pack entry changed while reading";
const PATH_BYTES: usize = 8192;
const PATH_DEPTH: usize = 128;
const READ_CHUNK: usize = 64 * 1024;

struct Entry<'a> {
    // Keep the decoder's Python Unicode code points, including any filename
    // bytes represented using Python's surrogateescape.
    path: Cow<'a, [u32]>,
    ordinal: usize,
    _owned_path: Option<NativeSetupReservation>,
    size: Option<&'a Node>,
    digest: Option<&'a Node>,
    seen: bool,
}

pub(super) fn verify<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    root: &TrackedFile,
    manifest: &Document,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let Some(files) = manifest.get(manifest.root, "files") else {
        return Err(model::denied(EMPTY));
    };
    let files = match manifest.node(files) {
        Node::Array(files) => files,
        // Iteration over JSON strings and objects yields non-mapping values.
        Node::String(_) | Node::Object(_) | Node::Scalar(CanonValue::Null) => {
            return Err(model::denied(EMPTY));
        }
        Node::Scalar(value) => {
            let kind = match value {
                CanonValue::Bool(false) | CanonValue::Int(0) => {
                    return Err(model::denied(EMPTY));
                }
                CanonValue::Float(value) if *value == 0.0 => {
                    return Err(model::denied(EMPTY));
                }
                CanonValue::Bool(true) => "bool",
                CanonValue::Int(_) | CanonValue::BigInt(_) => "int",
                CanonValue::Float(_) => "float",
                _ => unreachable!("scalar arena"),
            };
            return Err(raw(
                "builtins.TypeError",
                format!("'{kind}' object is not iterable"),
            ));
        }
    };
    let _index = op.reserve(STAGE, files.len().saturating_mul(size_of::<Entry<'_>>()))?;
    let mut expected = Vec::new();
    expected.try_reserve_exact(files.len()).map_err(capacity)?;
    if expected.capacity() != files.len() {
        return Err(capacity(()));
    }
    let mut longest = 0;
    for (ordinal, &id) in files.iter().enumerate() {
        let Some(fields) = manifest.entries(id) else {
            charge(op, budget, 1)?;
            continue;
        };
        let mut path = None;
        let mut size = None;
        let mut digest = None;
        for (field, id) in fields {
            charge(op, budget, field.0.len() + 1)?;
            match field {
                _ if field.is("path") => path = Some(manifest.node(*id)),
                _ if field.is("size") => size = Some(manifest.node(*id)),
                _ if field.is("sha256") => digest = Some(manifest.node(*id)),
                _ => {}
            }
        }
        let path = path.ok_or_else(|| raw("builtins.KeyError", "'path'"))?;
        let mut owned_path = None;
        let path: Cow<'_, [u32]> = match path {
            Node::String(path) => Cow::Borrowed(path.0.as_slice()),
            Node::Scalar(CanonValue::BigInt(path)) => {
                charge(op, budget, path.len() + 1)?;
                owned_path = Some(op.reserve(STAGE, path.len().saturating_mul(4))?);
                Cow::Owned(ascii_points(path)?)
            }
            Node::Scalar(CanonValue::Int(path)) => {
                owned_path = Some(op.reserve(STAGE, 128)?);
                Cow::Owned(ascii_points(&path.to_string())?)
            }
            Node::Scalar(CanonValue::Bool(path)) => Cow::Borrowed(if *path {
                &[84, 114, 117, 101]
            } else {
                &[70, 97, 108, 115, 101]
            }),
            Node::Scalar(CanonValue::Null) => Cow::Borrowed(&[78, 111, 110, 101]),
            // These forms are outside signer output. Do not approximate a
            // Python container repr with an unmetered recursive conversion.
            _ => {
                return Err(OriginalAuthorityError::unsupported(
                    STAGE,
                    "external Pack signed file inventory has a non-string path",
                ));
            }
        };
        longest = longest.max(path.len());
        expected.push(Entry {
            path,
            ordinal,
            _owned_path: owned_path,
            size,
            digest,
            seen: false,
        });
    }
    if expected.is_empty() {
        return Err(model::denied(EMPTY));
    }
    charge(
        op,
        budget,
        expected
            .len()
            .saturating_mul((expected.len().ilog2() + 1) as usize)
            .saturating_mul(longest + 1),
    )?;
    // An equal path's last entry replaces earlier ones in the original
    // Python dict comprehension, whether or not the original list is sorted.
    expected.sort_unstable_by(|left, right| {
        left.path
            .cmp(&right.path)
            .then_with(|| right.ordinal.cmp(&left.ordinal))
    });
    expected.dedup_by(|later, first| later.path == first.path);

    let _scratch = op.reserve(STAGE, PATH_BYTES + READ_CHUNK)?;
    let mut path = Vec::new();
    let mut chunk = Vec::new();
    path.try_reserve_exact(PATH_BYTES).map_err(capacity)?;
    chunk.try_reserve_exact(READ_CHUNK).map_err(capacity)?;
    if path.capacity() != PATH_BYTES || chunk.capacity() != READ_CHUNK {
        return Err(capacity(()));
    }
    chunk.resize(READ_CHUNK, 0);
    let mut scan = Scan {
        op,
        budget,
        expected,
        path,
        chunk,
    };
    scan.walk(root, 0)?;
    charge(op, budget, scan.expected.len())?;
    if scan.expected.iter().any(|entry| !entry.seen) {
        return Err(model::denied(INVENTORY_CHANGED));
    }
    Ok(())
}

struct Scan<'a, 'op, S> {
    op: &'a Operation<'op, S>,
    budget: &'a Arc<dyn AllocationBudget>,
    expected: Vec<Entry<'a>>,
    path: Vec<u8>,
    chunk: Vec<u8>,
}

impl<S: OriginalAuthorityOwner> Scan<'_, '_, S> {
    fn walk(&mut self, directory: &TrackedFile, depth: usize) -> Result<()> {
        if depth > PATH_DEPTH {
            return Err(capacity(()));
        }
        let _frame = self.op.reserve(STAGE, 4096)?;
        let before = directory.metadata().map_err(io)?;
        if !before.is_dir() {
            return Err(model::denied(INVENTORY_CHANGED));
        }
        let names = self.names(directory)?;
        // os.walk checks directory links, then visits this directory's files,
        // then descends into the remaining directories in enumeration order.
        for is_directory in [false, true] {
            for item in names.iter() {
                self.op.check(STAGE)?;
                if item.directory != is_directory {
                    continue;
                }
                let old_len = self.path.len();
                let name = item.name.as_c_str();
                let length = old_len + usize::from(old_len != 0) + name.to_bytes().len();
                if length > PATH_BYTES {
                    return Err(capacity(()));
                }
                charge(self.op, self.budget, length)?;
                if old_len != 0 {
                    self.path.push(b'/');
                }
                self.path.extend_from_slice(name.to_bytes());
                let result = self.visit(directory, name, &item.listed, depth);
                self.path.truncate(old_len);
                result?;
            }
        }
        self.op.check(STAGE)?;
        let after = directory.metadata().map_err(io)?;
        if metadata_identity(&before) != metadata_identity(&after)
            || (before.ctime(), before.ctime_nsec()) != (after.ctime(), after.ctime_nsec())
        {
            return Err(model::denied(INVENTORY_CHANGED));
        }
        Ok(())
    }

    fn names(&self, directory: &TrackedFile) -> Result<Funded<Vec<Member>>> {
        // Account for the libc directory stream buffer. Close that stream
        // before recursion so descendants don't retain its physical resources.
        let _buffer = self.op.reserve(STAGE, 1024 * 1024)?;
        let capacity = self.op.reserve(STAGE, 1)?;
        let mut members = Vec::new();
        let mut name_bytes = 0usize;
        // A fresh open file description keeps the borrowed directory's own
        // offset intact across the signed-manifest and compiler reads.
        let mut listing = open_child(self.op, directory, c".", libc::O_DIRECTORY)
            .and_then(TrackedFile::into_directory)
            .map_err(io)?;
        let outcome = (|| {
            loop {
                charge(self.op, self.budget, 1)?;
                let Some(name) = listing.next_name().map_err(io)? else {
                    break;
                };
                let length = name.to_bytes_with_nul().len();
                charge(self.op, self.budget, length)?;
                let listed = stat_child(directory, name, libc::AT_SYMLINK_NOFOLLOW).map_err(io)?;
                let kind = listed.st_mode & libc::S_IFMT;
                if kind == libc::S_IFLNK
                    // Follow for the original diagnosis only. Never open or
                    // descend through the link.
                    && stat_child(directory, name, 0)
                        .is_ok_and(|target| target.st_mode & libc::S_IFMT == libc::S_IFDIR)
                {
                    return Err(model::denied(SYMLINK));
                }
                name_bytes += length;
                push_member(
                    &mut members,
                    &capacity,
                    name_bytes,
                    name,
                    listed,
                    kind == libc::S_IFDIR,
                )?;
            }
            Ok(Funded {
                value: members,
                _capacity: capacity,
            })
        })();
        drop(listing);
        if self.op.cleanup.uncertain.load(Ordering::Acquire) {
            return finish_scope(
                outcome,
                Err(OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Close,
                    "external Pack inventory directory close was not confirmed",
                )),
            );
        }
        outcome
    }

    fn visit(
        &mut self,
        directory: &TrackedFile,
        name: &CStr,
        listed: &libc::stat,
        depth: usize,
    ) -> Result<()> {
        let kind = listed.st_mode & libc::S_IFMT;
        if kind == libc::S_IFDIR {
            let child = open_child(self.op, directory, name, libc::O_DIRECTORY).map_err(io)?;
            let outcome = (|| {
                let opened = child.metadata().map_err(io)?;
                if !opened.is_dir() || metadata_identity(&opened) != stat_identity(listed) {
                    return Err(model::denied(INVENTORY_CHANGED));
                }
                self.walk(&child, depth + 1)?;
                let current = stat_child(directory, name, libc::AT_SYMLINK_NOFOLLOW).map_err(io)?;
                if current.st_mode & libc::S_IFMT != libc::S_IFDIR
                    || metadata_identity(&opened) != stat_identity(&current)
                {
                    return Err(model::denied(INVENTORY_CHANGED));
                }
                Ok(())
            })();
            return finish_scope(outcome, child.close().map_err(io));
        }
        if kind == libc::S_IFLNK {
            return Err(model::denied(INVENTORY_CHANGED));
        }
        // The caller separately opens, verifies, and decodes the manifest.
        if self.path == SIGNED_MANIFEST {
            return if kind == libc::S_IFREG {
                Ok(())
            } else {
                Err(model::denied(INVENTORY_CHANGED))
            };
        }
        let comparisons = (self.expected.len().ilog2() + 1) as usize;
        charge(
            self.op,
            self.budget,
            comparisons.saturating_mul(self.path.len() + 1),
        )?;
        let index = self
            .expected
            .binary_search_by(|entry| entry.path.iter().copied().cmp(path_points(&self.path)))
            .map_err(|_| model::denied(INVENTORY_CHANGED))?;
        if self.path.contains(&b'\\') {
            return Err(model::denied(
                "external Normal Pack artifact path is unsafe",
            ));
        }
        let entry = &mut self.expected[index];
        if entry.seen {
            return Err(model::denied(INVENTORY_CHANGED));
        }
        if kind != libc::S_IFREG {
            return Err(model::denied("signed Pack entry is not a regular file"));
        }
        let mut file = open_child(self.op, directory, name, 0).map_err(io)?;
        let outcome = (|| {
            let before = file.metadata().map_err(io)?;
            if !before.is_file() {
                return Err(model::denied("signed Pack entry is not a regular file"));
            }
            if metadata_identity(&before) != stat_identity(listed) {
                return Err(model::denied(FILE_CHANGED));
            }
            if !size_matches(before.len(), entry.size) {
                return Err(model::denied(DIGEST_CHANGED));
            }

            let mut hash = Sha256::new();
            let mut total = 0u64;
            loop {
                self.op.check(STAGE)?;
                let count = match file.read(&mut self.chunk) {
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    result => result.map_err(io)?,
                };
                if count == 0 {
                    break;
                }
                charge(self.op, self.budget, count)?;
                total += count as u64;
                if total > before.len() {
                    return Err(model::denied(FILE_CHANGED));
                }
                hash.update(&self.chunk[..count]);
            }
            let after = file.metadata().map_err(io)?;
            let current = stat_child(directory, name, libc::AT_SYMLINK_NOFOLLOW).map_err(io)?;
            if current.st_mode & libc::S_IFMT != libc::S_IFREG
                || metadata_identity(&before) != metadata_identity(&after)
                || metadata_identity(&before) != stat_identity(&current)
            {
                return Err(model::denied(FILE_CHANGED));
            }
            let digest = Digest::from_sha256(hash.finalize().into());
            if !size_matches(total, entry.size)
                || !matches!(entry.digest, Some(Node::String(expected)) if expected.is(digest.hex()))
            {
                return Err(model::denied(DIGEST_CHANGED));
            }
            entry.seen = true;
            self.op.check(STAGE)
        })();
        finish_scope(outcome, file.close().map_err(io))
    }
}

struct Member {
    name: CString,
    listed: libc::stat,
    directory: bool,
}

fn push_member(
    members: &mut Vec<Member>,
    funding: &NativeSetupReservation,
    names_bytes: usize,
    name: &CStr,
    listed: libc::stat,
    directory: bool,
) -> Result<()> {
    let total = |slots: usize| {
        names_bytes
            .saturating_add(slots.saturating_mul(size_of::<Member>()))
            .max(1) as u64
    };
    let current = members.capacity();
    if members.len() == current {
        let next = current.max(4).checked_mul(2).ok_or_else(|| capacity(()))?;
        funding.resize(total(current + next)).map_err(capacity)?;
        members
            .try_reserve_exact(next - current)
            .map_err(capacity)?;
        if members.capacity() != next {
            return Err(capacity(()));
        }
        funding.resize(total(next)).map_err(capacity)?;
    } else {
        funding.resize(total(current)).map_err(capacity)?;
    }
    members.push(Member {
        name: name.to_owned(),
        listed,
        directory,
    });
    Ok(())
}

fn path_points(bytes: &[u8]) -> impl Iterator<Item = u32> + '_ {
    bytes.utf8_chunks().flat_map(|part| {
        part.valid()
            .chars()
            .map(u32::from)
            .chain(part.invalid().iter().map(|byte| 0xdc00 + u32::from(*byte)))
    })
}

fn size_matches(actual: u64, size: Option<&Node>) -> bool {
    match size {
        Some(Node::Scalar(CanonValue::Int(size))) => u64::try_from(*size) == Ok(actual),
        Some(Node::Scalar(CanonValue::BigInt(size))) => {
            size.len() <= 20 && size.parse::<u64>() == Ok(actual)
        }
        Some(Node::Scalar(CanonValue::Bool(size))) => u64::from(*size) == actual,
        Some(Node::Scalar(CanonValue::Float(size))) => {
            *size >= 0.0 && *size < u64::MAX as f64 && size.fract() == 0.0 && *size as u64 == actual
        }
        _ => false,
    }
}

fn ascii_points(ascii: &str) -> Result<Vec<u32>> {
    let mut points = Vec::new();
    points.try_reserve_exact(ascii.len()).map_err(capacity)?;
    if points.capacity() != ascii.len() {
        return Err(capacity(()));
    }
    points.extend(ascii.bytes().map(u32::from));
    Ok(points)
}

fn stat_child(
    parent: &TrackedFile,
    name: &CStr,
    flags: libc::c_int,
) -> std::io::Result<libc::stat> {
    let mut stat = std::mem::MaybeUninit::uninit();
    if unsafe { libc::fstatat(parent.as_raw_fd(), name.as_ptr(), stat.as_mut_ptr(), flags) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(unsafe { stat.assume_init() })
}

fn open_child<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    parent: &TrackedFile,
    name: &CStr,
    flags: libc::c_int,
) -> std::io::Result<TrackedFile> {
    let fd = unsafe {
        libc::openat(
            parent.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC | flags,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(TrackedFile::new(
        unsafe { File::from_raw_fd(fd) },
        op.cleanup.clone(),
    ))
}

fn metadata_identity(stat: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    (
        stat.dev(),
        stat.ino(),
        stat.len(),
        stat.mtime(),
        stat.mtime_nsec(),
    )
}

fn stat_identity(stat: &libc::stat) -> (u64, u64, u64, i64, i64) {
    (
        device_bits(stat.st_dev),
        stat.st_ino,
        stat.st_size as u64,
        stat.st_mtime,
        stat.st_mtime_nsec,
    )
}

fn io(error: std::io::Error) -> OriginalAuthorityError {
    raw_io(error, None)
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::*;
    use serde_json::json;
    use std::os::unix::fs::symlink;

    #[tokio::test(flavor = "current_thread")]
    async fn matches_the_physical_inventory_and_closes_files_on_denials() {
        with_source(|source, read, cancellation, budget| {
            let op = operation(read, source, cancellation);
            let path = source.root.join("test-cas");
            let nested = path.join("nested");
            let file = nested.join("content.bin");
            fs::create_dir_all(path.join(".tobkiri")).unwrap();
            fs::create_dir(&nested).unwrap();
            fs::create_dir(path.join("empty")).unwrap();
            fs::write(&file, b"abc").unwrap();
            fs::write(path.join(".tobkiri/signed-pack.json"), b"excluded").unwrap();
            let root = Parent::walk(&path, &op.cleanup).unwrap();
            let sample = json!({
                "files": [
                    {"path": "nested/content.bin", "size": 1, "sha256": "replaced"},
                    {"path": "nested/content.bin", "size": 3, "sha256": Digest::hash(b"abc").hex()}
                ]
            });
            let manifest =
                json::decode_catalog_manifest(&serde_json::to_vec(&sample).unwrap(), &op, budget)
                    .unwrap();
            let check = || verify(&op, &root, &manifest, budget);
            check().unwrap();
            assert_eq!(read.receipt().open_files, 1);

            fs::write(&file, b"ABC").unwrap();
            assert_eq!(check().unwrap_err().message, DIGEST_CHANGED);
            fs::write(&file, b"abcd").unwrap();
            assert_eq!(check().unwrap_err().message, DIGEST_CHANGED);
            fs::write(&file, b"abc").unwrap();

            let extra = path.join("untracked");
            fs::write(&extra, b"x").unwrap();
            assert_eq!(check().unwrap_err().message, INVENTORY_CHANGED);
            fs::remove_file(&extra).unwrap();
            fs::remove_file(&file).unwrap();
            assert_eq!(check().unwrap_err().message, INVENTORY_CHANGED);
            symlink(&nested, &file).unwrap();
            assert_eq!(check().unwrap_err().message, SYMLINK);
            assert_eq!(read.receipt().open_files, 1);
            root.close().unwrap();
            assert!(read.receipt().verified());
        });
    }
}
