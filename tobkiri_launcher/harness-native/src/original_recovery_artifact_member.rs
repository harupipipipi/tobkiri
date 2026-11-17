//! Independent reads of the selected entrypoint and bundle metadata.
use super::*;
use std::io::{Seek, SeekFrom};

const ENTRY_OUTSIDE: &str = "packaged artifact entrypoint is missing or outside artifact";
const ENTRY_NOT_REGULAR: &str = "packaged artifact entrypoint is not a regular file";
const INFO_NOT_REGULAR: &str = "macOS packaged artifact has no safe Info.plist";
const INFO_INVALID: &str = "macOS packaged artifact Info.plist is invalid";

pub(in crate::original_authority::io::publication::catalog) struct SelectedPackage<'a> {
    root: &'a Path,
    relative: &'a str,
    parent: Identity,
    snapshot: &'a ArtifactTreeIdentity,
}

#[derive(Clone, Copy)]
enum ReadKind {
    Entry,
    Header,
    InfoPlist,
}

impl ReadKind {
    fn missing(self) -> OriginalAuthorityError {
        artifact_rejected(match self {
            Self::InfoPlist => INFO_NOT_REGULAR,
            _ => ENTRY_OUTSIDE,
        })
    }

    fn not_regular(self) -> OriginalAuthorityError {
        artifact_rejected(match self {
            Self::InfoPlist => INFO_NOT_REGULAR,
            _ => ENTRY_NOT_REGULAR,
        })
    }

    fn read_error(self, error: std::io::Error) -> OriginalAuthorityError {
        let mut rejected = artifact_rejected(match self {
            Self::Entry => "packaged artifact member could not be read",
            Self::Header => "packaged artifact entrypoint header could not be read",
            Self::InfoPlist => INFO_INVALID,
        });
        if let Some(OriginalAuthorityCause::Python {
            cause, suppressed, ..
        }) = &mut rejected.cause
        {
            *cause = Some(Box::new(into_cause(raw_io(error, None))));
            *suppressed = true;
        }
        rejected
    }
}

impl<'a> SelectedPackage<'a> {
    pub(in crate::original_authority::io::publication::catalog) fn new(
        root: &'a Path,
        relative: &'a str,
        parent: Identity,
        snapshot: &'a ArtifactTreeIdentity,
    ) -> Self {
        Self {
            root,
            relative,
            parent,
            snapshot,
        }
    }

    pub(in crate::original_authority::io::publication::catalog) fn verify<
        S: crate::BootstrapReviewOwner,
    >(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        variant: &CoreValue,
    ) -> Result<()> {
        // The tree, entrypoint hash, bundle metadata and header share the
        // original verification interval. The earlier lstat pass is separate.
        bounded(
            op,
            || artifact_rejected("packaged artifact verification deadline exceeded"),
            |op| {
                let field = |name| {
                    variant
                        .get_key(name)
                        .ok_or_else(OriginalAuthorityError::unavailable)
                };
                let digest = field("artifact_digest")?;
                verify_tree_digest(
                    op,
                    budget,
                    self.root,
                    self.relative,
                    self.parent,
                    self.snapshot,
                    text(&digest)?,
                )?;
                let entry = field("entrypoint")?;
                let entry = text(&entry)?;
                let expected_digest = field("entrypoint_digest")?;
                self.read_file(
                    op,
                    budget,
                    entry,
                    ReadKind::Entry,
                    |walk, file, expected| {
                        let mut scratch = Buffer::new(op)?;
                        scratch.reserve(STREAM_CHUNK, budget)?;
                        scratch.values.resize(STREAM_CHUNK, 0);
                        let mut digest = Sha256::new();
                        walk.hash_contents(file, expected, &mut scratch.values, &mut digest)?;
                        let digest = Digest::from_sha256(digest.finalize().into());
                        if digest.as_str() != text(&expected_digest)? {
                            return Err(artifact_rejected(
                                "packaged artifact entrypoint digest does not match",
                            ));
                        }
                        Ok(())
                    },
                )?;
                if text(&field("platform")?)? == "macos" {
                    let mut parts = Self::parts(op, self.relative, ReadKind::InfoPlist, 2)?;
                    parts.values.extend(["Contents", "Info.plist"]);
                    let bundle = field("bundle_identity")?;
                    self.with_file(
                        op,
                        budget,
                        &parts.values,
                        ReadKind::InfoPlist,
                        |_, file, size| {
                            let length = usize::try_from(size.size)
                                .map_err(|_| artifact_rejected(INFO_INVALID))?;
                            let mut raw = Buffer::new(op)?;
                            raw.reserve(length, budget)?;
                            raw.values.resize(length, 0);
                            if read_up_to(op, file, &mut raw.values, ReadKind::InfoPlist)? != length
                            {
                                return Err(artifact_rejected(INFO_INVALID));
                            }
                            bundle_metadata::check_bundle_identity(
                                op,
                                budget,
                                &raw.values,
                                text(&bundle)?,
                            )
                        },
                    )?;
                }
                let architecture = field("architecture")?;
                self.read_file(op, budget, entry, ReadKind::Header, |_, file, expected| {
                    let actual = architecture_of(op, file, expected.size)?;
                    if actual != Some(text(&architecture)?) {
                        return Err(artifact_rejected(
                            "packaged artifact architecture does not match selection",
                        ));
                    }
                    Ok(())
                })
            },
        )
    }

    fn parts<'p, S: OriginalAuthorityOwner>(
        op: &Operation<'_, S>,
        relative: &'p str,
        kind: ReadKind,
        extra: usize,
    ) -> Result<Buffer<&'p str>> {
        if relative.len() >= libc::PATH_MAX as usize {
            return Err(exhausted());
        }
        if relative.starts_with('/') || !relative_parts(relative).all(single_leaf) {
            return Err(kind.missing());
        }
        let _path = op.reserve(STAGE, relative.len())?;
        let count = relative_parts(relative).count().saturating_add(extra);
        let capacity = op.reserve(STAGE, count.saturating_mul(size_of::<&str>()))?;
        let mut values = Vec::new();
        values.try_reserve_exact(count).map_err(|_| exhausted())?;
        if values.capacity() != count {
            return Err(exhausted());
        }
        values.extend(relative_parts(relative));
        Ok(Buffer { values, capacity })
    }

    fn read_file<S: crate::BootstrapReviewOwner, T>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        relative: &str,
        kind: ReadKind,
        read: impl FnOnce(&Walk<'_, '_, S>, &mut TrackedFile, Metadata) -> Result<T>,
    ) -> Result<T> {
        let parts = Self::parts(op, relative, kind, 0)?;
        self.with_file(op, budget, &parts.values, kind, read)
    }

    fn with_file<S: crate::BootstrapReviewOwner, T>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        parts: &[&str],
        kind: ReadKind,
        read: impl FnOnce(&Walk<'_, '_, S>, &mut TrackedFile, Metadata) -> Result<T>,
    ) -> Result<T> {
        let evidence = self.chain(op, budget, parts, kind)?;
        let path_limit = libc::PATH_MAX as usize;
        let _path_bytes = op.reserve(OriginalAuthorityStage::Path, path_limit * 4)?;
        let mut path = PathBuf::new();
        path.try_reserve_exact(path_limit)
            .map_err(|_| exhausted())?;
        if path.capacity() != path_limit || self.root.as_os_str().len() >= path_limit {
            return Err(exhausted());
        }
        let mut walk = Walk {
            op,
            budget,
            path,
            artifact_end: self.snapshot.path_end,
        };
        let root_parent = self.root.parent().ok_or_else(|| denied(CHANGED))?;
        walk.path.push(root_parent);
        let root_name = self
            .root
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| denied(CHANGED))?;
        let anchor = walk.open_anchor(root_parent, self.parent)?;
        let mut parents = Buffer::new(op)?;
        let mut rows = std::iter::once(root_name)
            .chain(parts.iter().copied())
            .zip(evidence.values)
            .peekable();
        while let Some((name, expected)) = rows.next() {
            walk.push_path(name)?;
            let parent = parents
                .values
                .last()
                .map_or(&anchor, |item: &DirectoryEvidence| &item.file);
            let stat = walk.stat(parent, name)?;
            if Metadata::from_stat(&stat) != expected {
                return Err(denied(CHANGED));
            }
            if rows.peek().is_some() {
                parents.push(walk.open_directory(parent, name, &stat, false)?, budget)?;
                continue;
            }
            let name = shell_root::checked_name(name)?;
            let mut file = open_shell_file(
                op,
                parent,
                walk.path.parent().expect("member parent"),
                &name,
                stat_identity(&stat),
            )?;
            if walk.descriptor_metadata(&file)? != expected {
                return Err(denied(CHANGED));
            }
            let outcome = (|| {
                let value = read(&walk, &mut file, expected)?;
                if walk.descriptor_metadata(&file)? != expected {
                    return Err(denied(CHANGED));
                }
                Ok(value)
            })();
            let closed = file
                .close()
                .map_err(|_| OriginalAuthorityError::unavailable());
            let value = finish_scope(outcome, closed)?;
            for directory in &parents.values {
                walk.check_directory(directory)?;
            }
            drop((parents, anchor));
            drop(walk.open_anchor(root_parent, self.parent)?);
            if op.cleanup.uncertain.load(Ordering::Acquire) {
                return Err(OriginalAuthorityError::unavailable());
            }
            return Ok(value);
        }
        Err(kind.missing())
    }

    fn chain<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        budget: &Arc<dyn AllocationBudget>,
        parts: &[&str],
        kind: ReadKind,
    ) -> Result<Buffer<Metadata>> {
        let mut suffix = parts.iter();
        let mut artifact_depth = 0;
        for selected in relative_parts(self.relative) {
            if suffix.next().copied() != Some(selected) {
                return Err(kind.missing());
            }
            artifact_depth += 1;
        }
        let inside = suffix.as_slice();
        let mut chain = Buffer::new(op)?;
        let mut name_start = self.snapshot.path_end;
        let mut matched = 0;
        for row in &self.snapshot.rows.values {
            op.check(STAGE)?;
            let name = &self.snapshot.names.values[name_start..row.name_end];
            name_start = row.name_end;
            charge_work(budget, (name.len() + size_of::<Entry>()) as u64)?;
            match row.role {
                Role::Root | Role::Prefix => chain.push(row.metadata, budget)?,
                Role::Tree(0) => {
                    if artifact_depth > 0 {
                        chain.push(row.metadata, budget)?;
                    }
                    if inside.is_empty() {
                        break;
                    }
                }
                Role::Tree(depth) if depth <= matched + 1 => {
                    matched = depth - 1;
                    chain.values.truncate(artifact_depth + 1 + matched);
                    if name == inside[matched].as_bytes() {
                        chain.push(row.metadata, budget)?;
                        matched += 1;
                        if matched == inside.len() {
                            break;
                        }
                    }
                }
                Role::Tree(_) => {}
            }
        }
        if chain.values.len() != parts.len() + 1 {
            return Err(kind.missing());
        }
        let (file, dirs) = chain.values.split_last().ok_or_else(|| kind.missing())?;
        let is =
            |metadata: &Metadata, mode| metadata.mode & mode_bits(libc::S_IFMT) == mode_bits(mode);
        if !dirs.iter().all(|metadata| is(metadata, libc::S_IFDIR)) {
            return Err(kind.missing());
        }
        if !is(file, libc::S_IFREG) {
            return Err(kind.not_regular());
        }
        Ok(chain)
    }
}

fn text(value: &CoreValue) -> Result<&str> {
    value
        .as_str()
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn read_up_to<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    file: &mut TrackedFile,
    mut buffer: &mut [u8],
    kind: ReadKind,
) -> Result<usize> {
    let size = buffer.len();
    while !buffer.is_empty() {
        op.check(STAGE)?;
        match file.read(buffer) {
            Ok(0) => break,
            Ok(count) => buffer = &mut buffer[count..],
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(kind.read_error(error)),
        }
    }
    Ok(size - buffer.len())
}

fn architecture_of<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    file: &mut TrackedFile,
    size: i64,
) -> Result<Option<&'static str>> {
    let kind = ReadKind::Header;
    let mut head = [0; 64];
    let read = read_up_to(op, file, &mut head, kind)?;
    let head = &head[..read];
    if head.starts_with(b"MZ") {
        if head.len() < 64 {
            return Err(artifact_rejected("PE entrypoint DOS header is truncated"));
        }
        let offset = u32::from_le_bytes(head[60..64].try_into().unwrap());
        let header_size = 24;
        if offset < 64 || i64::from(offset) > size - header_size {
            return Err(artifact_rejected("PE entrypoint header is out of bounds"));
        }
        let mut tail = [0; 24];
        op.check(STAGE)?;
        file.seek(SeekFrom::Start(u64::from(offset)))
            .map_err(|error| kind.read_error(error))?;
        if read_up_to(op, file, &mut tail, kind)? != tail.len() || &tail[..4] != b"PE\0\0" {
            return Err(artifact_rejected(
                "PE entrypoint signature is invalid or truncated",
            ));
        }
        return Ok(architecture(
            u16::from_le_bytes(tail[4..6].try_into().unwrap()).into(),
            0x8664,
            0xAA64,
        ));
    }
    if let [a, b, c, d, m1, m2, m3, m4, ..] = head {
        let machine = [*m1, *m2, *m3, *m4];
        match [*a, *b, *c, *d] {
            [0xcf, 0xfa, 0xed, 0xfe] => {
                return Ok(architecture(
                    u32::from_le_bytes(machine),
                    0x0100_0007,
                    0x0100_000c,
                ));
            }
            [0xfe, 0xed, 0xfa, 0xcf] => {
                return Ok(architecture(
                    u32::from_be_bytes(machine),
                    0x0100_0007,
                    0x0100_000c,
                ));
            }
            _ => {}
        }
    }
    if head.starts_with(b"\x7fELF") && head.len() >= 20 {
        let field = [head[18], head[19]];
        let machine = if head[5] == 1 {
            u16::from_le_bytes(field)
        } else {
            u16::from_be_bytes(field)
        };
        return Ok(architecture(machine.into(), 62, 183));
    }
    Ok(None)
}

fn architecture(machine: u32, x86: u32, arm: u32) -> Option<&'static str> {
    if machine == x86 {
        Some("x86_64")
    } else if machine == arm {
        Some("arm64")
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::original_authority::definitions::tests::{operation, with_source};

    #[tokio::test(flavor = "current_thread")]
    async fn binary_header_checks_match_elf_pe_and_mach_o_architectures() {
        with_source(|source, read, cancellation, _| {
            let op = operation(read, source, cancellation);
            fs::create_dir_all(&source.root).unwrap();
            let binary = source.root.join("entry");
            let mut elf = [0_u8; 20];
            elf[..4].copy_from_slice(b"\x7fELF");
            elf[5] = 1;
            elf[18..20].copy_from_slice(&62_u16.to_le_bytes());
            let mut pe = [0_u8; 88];
            pe[..2].copy_from_slice(b"MZ");
            pe[60..64].copy_from_slice(&64_u32.to_le_bytes());
            pe[64..68].copy_from_slice(b"PE\0\0");
            pe[68..70].copy_from_slice(&0xAA64_u16.to_le_bytes());
            let header = |bytes: &[u8]| {
                fs::write(&binary, bytes).unwrap();
                let mut file = TrackedFile::new(File::open(&binary).unwrap(), op.cleanup.clone());
                let result = architecture_of(&op, &mut file, bytes.len() as i64);
                file.close().unwrap();
                result
            };
            assert_eq!(header(&elf).unwrap(), Some("x86_64"));
            assert_eq!(header(&pe).unwrap(), Some("arm64"));
            assert_eq!(
                header(b"\xfe\xed\xfa\xcf\x01\x00\x00\x07").unwrap(),
                Some("x86_64"),
            );
            assert_eq!(header(&[0; 8]).unwrap(), None);
            for (bytes, message) in [
                (&pe[..12], "PE entrypoint DOS header is truncated"),
                (&pe[..80], "PE entrypoint header is out of bounds"),
            ] {
                let error = header(bytes).unwrap_err();
                assert_eq!(
                    error.message,
                    format!("active Profile Shell artifact rejected: {message}")
                );
            }
            pe[65] = 0;
            assert_eq!(
                header(&pe).unwrap_err().message,
                "active Profile Shell artifact rejected: PE entrypoint signature is invalid or truncated",
            );
        });
    }
}
