//! The regular Shell leaf's original digest reads under the fixed catalog call.
use super::*;
use crate::fd_stat::{MAX_STAT_JSON_BYTES, encode_stat};
use std::io::{Seek as _, SeekFrom};

const CHUNK_BYTES: usize = 1024 * 1024;

pub(super) enum ShellFile {
    Tree(TrackedFile),
    Entrypoint(TrackedFile),
    Header {
        file: TrackedFile,
        size: u64,
        pe_offset: u64,
    },
}

impl ShellFile {
    pub(super) fn header(file: TrackedFile) -> Self {
        Self::Header {
            file,
            size: 0,
            pe_offset: 0,
        }
    }

    fn into_file(self) -> TrackedFile {
        match self {
            Self::Tree(file) | Self::Entrypoint(file) | Self::Header { file, .. } => file,
        }
    }

    fn file_mut(&mut self) -> &mut TrackedFile {
        match self {
            Self::Tree(file) | Self::Entrypoint(file) | Self::Header { file, .. } => file,
        }
    }
}

pub(super) fn write_descriptor(
    out: &mut CoreBufferBuilder,
    binding: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<()> {
    out.append(b"{\"kind\":\"fd\",\"binding\":")?;
    out.append(binding.encode_guest(budget)?.as_bytes())?;
    out.append(b"}")?;
    Ok(())
}

pub(super) fn open_shell_file<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    root: &TrackedFile,
    root_path: &Path,
    name: &std::ffi::CStr,
    expected: Identity,
) -> Result<TrackedFile> {
    let descriptor = loop {
        op.check(OriginalAuthorityStage::Source)?;
        let descriptor = unsafe {
            libc::openat(
                root.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY
                    | libc::O_NOFOLLOW
                    | libc::O_CLOEXEC
                    | libc::O_NONBLOCK
                    | libc::O_NOCTTY,
            )
        };
        if descriptor >= 0 {
            break descriptor;
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            let path = root_path.join(std::ffi::OsStr::from_bytes(name.to_bytes()));
            return Err(raw_io(error, path.to_str()));
        }
    };
    let file = TrackedFile::new(unsafe { File::from_raw_fd(descriptor) }, op.cleanup.clone());
    if !file
        .metadata()
        .is_ok_and(|meta| identity(&meta) == expected)
    {
        file.close()
            .map_err(|_| OriginalAuthorityError::unavailable())?;
        return Err(OriginalAuthorityError::unavailable());
    }
    Ok(file)
}

impl CatalogIo {
    pub(super) fn inspect_shell_file<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        scope: &CoreValue,
        steps: &tobkiri_canon::CoreArray<'_>,
        operation: &CoreValue,
        args: &tobkiri_canon::CoreArray<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<CoreBuffer> {
        let entrypoint = matches!(self.shell_file, Some(ShellFile::Entrypoint(_)));
        let header = matches!(self.shell_file, Some(ShellFile::Header { .. }));
        let valid = if entrypoint || header {
            let tag = if header {
                "shell-header-file"
            } else {
                "shell-entrypoint-file"
            };
            self.shell_entrypoint
                .as_ref()
                .and_then(CoreValue::as_str)
                .is_some_and(|path| {
                    exact_steps(
                        steps,
                        &[(tag, ""), ("shell-resolved-root", ""), ("join", path)],
                    )
                })
        } else {
            self.shell_leaf
                .as_ref()
                .and_then(CoreValue::as_str)
                .is_some_and(|leaf| self.shell_tree.matches(leaf, steps, true))
        };
        if !valid {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let mut out = inspection_result(scope, budget)?;
        op.check(OriginalAuthorityStage::Source)?;
        charge_work(budget, 1)?;
        let stage = self.pack_resolve;
        if operation.as_str() == Some("close") && args.is_empty() {
            self.pack_resolve = PackResolve::Spent;
            self.shell_file
                .take()
                .ok_or_else(OriginalAuthorityError::unavailable)?
                .into_file()
                .close()
                .map_err(|_| OriginalAuthorityError::unavailable())?;
            out.append(b"{\"kind\":\"unit\"}}")?;
            op.check(OriginalAuthorityStage::Source)?;
            let response = out.finish()?;
            if stage == PackResolve::ShellFileClose {
                self.pack_resolve = if header {
                    PackResolve::ShellArtifactReady
                } else if entrypoint {
                    if self.shell_macos {
                        PackResolve::ShellPlistSymlink
                    } else {
                        PackResolve::ShellHeaderOpen
                    }
                } else {
                    self.shell_tree.advance()
                };
            }
            return Ok(response);
        }
        let retained = self
            .shell_file
            .as_mut()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if let ShellFile::Header {
            file,
            size,
            pe_offset,
        } = retained
        {
            let next = inspect_header(
                op,
                (&mut self.pack_resolve, file, size, pe_offset),
                operation.as_str(),
                args,
                &mut out,
                budget,
            )?;
            op.check(OriginalAuthorityStage::Source)?;
            out.append(b"}")?;
            let response = out.finish()?;
            self.pack_resolve = next;
            return Ok(response);
        }
        let file = retained.file_mut();
        let next = match (stage, operation.as_str(), args.len()) {
            (PackResolve::ShellFileTell, Some("seek"), 2)
                if args.get(0).and_then(|value| value.as_i64()) == Some(0)
                    && args.get(1).and_then(|value| value.as_i64())
                        == Some(i64::from(libc::SEEK_CUR)) =>
            {
                self.pack_resolve = PackResolve::Spent;
                let offset = file
                    .stream_position()
                    .map_err(|error| raw_io(error, None))?;
                if offset != 0 {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                out.append(b"{\"kind\":\"int\",\"value\":0}")?;
                PackResolve::ShellFileStat
            }
            (PackResolve::ShellFileStat | PackResolve::ShellFileEnd, Some("fstat"), 0) => {
                self.pack_resolve = PackResolve::Spent;
                let meta = file.metadata().map_err(|error| raw_io(error, None))?;
                let mut stat = [0_u8; MAX_STAT_JSON_BYTES];
                let length = encode_stat(&meta, &mut stat).ok_or_else(|| {
                    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
                })?;
                out.append(&stat[..length])?;
                if stage == PackResolve::ShellFileStat {
                    PackResolve::ShellFileRead
                } else {
                    PackResolve::ShellFileClose
                }
            }
            (PackResolve::ShellFileRead | PackResolve::ShellFileEnd, Some("read"), 1) => {
                let size = args
                    .get(0)
                    .and_then(|value| value.as_i64())
                    .and_then(|value| usize::try_from(value).ok())
                    .filter(|size| (1..=CHUNK_BYTES).contains(size))
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                self.pack_resolve = PackResolve::Spent;
                let _capacity = op.reserve(OriginalAuthorityStage::Source, size)?;
                let mut bytes = Zeroizing::new(Vec::new());
                bytes.try_reserve_exact(size).map_err(|_| {
                    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
                })?;
                if bytes.capacity() != size {
                    return Err(
                        OriginalAuthorityError::capacity(OriginalAuthorityStage::Source).into(),
                    );
                }
                bytes.resize(size, 0);
                let count = loop {
                    op.check(OriginalAuthorityStage::Source)?;
                    match file.read(&mut bytes) {
                        Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                        result => break result.map_err(|error| raw_io(error, None))?,
                    }
                };
                charge_work(budget, count as u64)?;
                write_bytes(&mut out, &bytes[..count])?;
                if count == 0 {
                    PackResolve::ShellFileEnd
                } else {
                    PackResolve::ShellFileRead
                }
            }
            _ => return Err(OriginalAuthorityError::unavailable().into()),
        };
        op.check(OriginalAuthorityStage::Source)?;
        out.append(b"}")?;
        let response = out.finish()?;
        self.pack_resolve = next;
        Ok(response)
    }
}

fn inspect_header<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    state: (&mut PackResolve, &mut TrackedFile, &mut u64, &mut u64),
    operation: Option<&str>,
    args: &tobkiri_canon::CoreArray<'_>,
    out: &mut CoreBufferBuilder,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<PackResolve> {
    let (stage, file, size, pe_offset) = state;
    const PREFIX: usize = 64;
    const PE_HEADER: usize = 24;
    Ok(match (*stage, operation, args.len()) {
        (PackResolve::ShellFileStat | PackResolve::ShellFileEnd, Some("fstat"), 0) => {
            let first = *stage == PackResolve::ShellFileStat;
            *stage = PackResolve::Spent;
            let metadata = file.metadata().map_err(|error| raw_io(error, None))?;
            let mut stat = [0_u8; MAX_STAT_JSON_BYTES];
            let length = encode_stat(&metadata, &mut stat)
                .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            out.append(&stat[..length])?;
            if first {
                *size = metadata.len();
                PackResolve::ShellFileRead
            } else {
                PackResolve::ShellFileClose
            }
        }
        (PackResolve::ShellHeaderSeek, Some("seek"), 2)
            if args
                .get(0)
                .and_then(|v| v.as_i64())
                .and_then(|n| u64::try_from(n).ok())
                == Some(*pe_offset)
                && args.get(1).and_then(|v| v.as_i64()) == Some(i64::from(libc::SEEK_SET)) =>
        {
            *stage = PackResolve::Spent;
            let offset = file
                .seek(SeekFrom::Start(*pe_offset))
                .map_err(|error| raw_io(error, None))?;
            if offset != *pe_offset {
                return Err(OriginalAuthorityError::unavailable().into());
            }
            out.append(b"{\"kind\":\"int\",\"value\":")?;
            out.append(offset.to_string().as_bytes())?;
            out.append(b"}")?;
            PackResolve::ShellHeaderTail
        }
        (PackResolve::ShellFileRead | PackResolve::ShellHeaderTail, Some("read"), 1) => {
            let prefix = *stage == PackResolve::ShellFileRead;
            let required = if prefix { PREFIX } else { PE_HEADER };
            if args.get(0).and_then(|v| v.as_i64()) != Some(required as i64) {
                return Err(OriginalAuthorityError::unavailable().into());
            }
            *stage = PackResolve::Spent;
            let mut bytes = [0_u8; PREFIX];
            let mut count = 0;
            while count < required {
                op.check(OriginalAuthorityStage::Source)?;
                charge_work(budget, 1)?;
                match file.read(&mut bytes[count..required]) {
                    Ok(0) => break,
                    Ok(n) => count += n,
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(error) => return Err(raw_io(error, None).into()),
                }
            }
            write_bytes(out, &bytes[..count])?;
            if prefix && bytes[..count].starts_with(b"MZ") {
                if count != PREFIX {
                    return Ok(PackResolve::Spent);
                }
                let offset = u64::from(u32::from_le_bytes(
                    bytes[60..64].try_into().expect("four offset bytes"),
                ));
                if offset < PREFIX as u64 || offset + PE_HEADER as u64 > *size {
                    return Ok(PackResolve::Spent);
                }
                *pe_offset = offset;
                PackResolve::ShellHeaderSeek
            } else if !prefix && (count != PE_HEADER || !bytes.starts_with(b"PE\0\0")) {
                PackResolve::Spent
            } else {
                PackResolve::ShellFileEnd
            }
        }
        _ => return Err(OriginalAuthorityError::unavailable().into()),
    })
}
