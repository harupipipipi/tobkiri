//! The regular Shell leaf's original digest reads under the fixed catalog call.
use super::*;
use crate::fd_stat::{MAX_STAT_JSON_BYTES, encode_stat};
use std::io::Seek as _;

const FILE_STEPS: &[(&str, &str)] = &[("shell-file", "")];
const CHUNK_BYTES: usize = 1024 * 1024;

impl CatalogIo {
    pub(super) fn open_shell_file<S: crate::BootstrapReviewOwner>(
        &self,
        op: &Operation<'_, S>,
        root: &TrackedFile,
        root_path: &Path,
        name: &std::ffi::CStr,
    ) -> Result<TrackedFile> {
        let expected = self
            .shell_tree_identity
            .ok_or_else(OriginalAuthorityError::unavailable)?;
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

    pub(super) fn inspect_shell_file<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        scope: &CoreValue,
        steps: &tobkiri_canon::CoreArray<'_>,
        operation: &CoreValue,
        args: &tobkiri_canon::CoreArray<'_>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<CoreBuffer> {
        if !exact_steps(steps, FILE_STEPS) {
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
                .close()
                .map_err(|_| OriginalAuthorityError::unavailable())?;
            out.append(b"{\"kind\":\"unit\"}}")?;
            op.check(OriginalAuthorityStage::Source)?;
            let response = out.finish()?;
            if stage == PackResolve::ShellFileClose {
                self.pack_resolve = PackResolve::ShellReady;
            }
            return Ok(response);
        }
        let file = self
            .shell_file
            .as_mut()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
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
