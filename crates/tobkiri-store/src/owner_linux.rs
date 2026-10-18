//! Narrow Linux owner admission. The retained root fd supplies both the native
//! filesystem identity and mount ID; mountinfo is evidence only when read from
//! verified procfs for this process. A mountpoint pathname never grants authority.
//!
//! The ext-family magic is shared by ext2/ext3/ext4. Admission therefore also
//! requires an unambiguous `ext4` record for that fd's mount ID and device, then
//! repeats the native fd checks. Unsupported targets/APIs and bounded-evidence
//! failures remain unavailable. The common owner broker still acquires `flock`.

use std::io::Read;

const MAX_MOUNTINFO_BYTES: usize = 1024 * 1024;
const MAX_MOUNTINFO_LINES: usize = 4096;
const MAX_MOUNTINFO_LINE_BYTES: usize = 8192;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct MountIdentity {
    id: u64,
    major: u32,
    minor: u32,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct EvidenceUnavailable;
type EvidenceResult<T> = Result<T, EvidenceUnavailable>;

fn decimal(bytes: &[u8]) -> EvidenceResult<u64> {
    if bytes.is_empty() || (bytes.len() > 1 && bytes[0] == b'0') {
        return Err(EvidenceUnavailable);
    }
    bytes.iter().try_fold(0_u64, |value, byte| {
        if !byte.is_ascii_digit() {
            return Err(EvidenceUnavailable);
        }
        value
            .checked_mul(10)
            .and_then(|value| value.checked_add(u64::from(*byte - b'0')))
            .ok_or(EvidenceUnavailable)
    })
}

fn device(bytes: &[u8]) -> EvidenceResult<(u32, u32)> {
    let mut parts = bytes.split(|byte| *byte == b':');
    let major = decimal(parts.next().ok_or(EvidenceUnavailable)?)?;
    let minor = decimal(parts.next().ok_or(EvidenceUnavailable)?)?;
    if parts.next().is_some() {
        return Err(EvidenceUnavailable);
    }
    Ok((
        u32::try_from(major).map_err(|_| EvidenceUnavailable)?,
        u32::try_from(minor).map_err(|_| EvidenceUnavailable)?,
    ))
}

/// Parse one bounded kernel record without decoding or following its paths.
fn check_record(line: &[u8], expected: MountIdentity) -> EvidenceResult<bool> {
    if line.is_empty()
        || line
            .iter()
            .any(|byte| *byte < b' ' || *byte == 0x7f || !byte.is_ascii())
    {
        return Err(EvidenceUnavailable);
    }
    let mut fields = line.split(|byte| *byte == b' ');
    let id = decimal(fields.next().ok_or(EvidenceUnavailable)?)?;
    if id == 0 {
        return Err(EvidenceUnavailable);
    }
    decimal(fields.next().ok_or(EvidenceUnavailable)?)?; // Parent may be outside this namespace.
    let (major, minor) = device(fields.next().ok_or(EvidenceUnavailable)?)?;
    for _ in 0..2 {
        if !fields.next().ok_or(EvidenceUnavailable)?.starts_with(b"/") {
            return Err(EvidenceUnavailable);
        }
    }
    if fields.next().ok_or(EvidenceUnavailable)?.is_empty() {
        return Err(EvidenceUnavailable);
    }
    loop {
        match fields.next() {
            Some(b"-") => break,
            Some(field) if !field.is_empty() => (),
            _ => return Err(EvidenceUnavailable),
        }
    }
    let filesystem = fields.next().ok_or(EvidenceUnavailable)?;
    let source = fields.next().ok_or(EvidenceUnavailable)?;
    let options = fields.next().ok_or(EvidenceUnavailable)?;
    if filesystem.is_empty() || source.is_empty() || options.is_empty() || fields.next().is_some() {
        return Err(EvidenceUnavailable);
    }
    if id != expected.id {
        return Ok(false);
    }
    if (major, minor) != (expected.major, expected.minor) || filesystem != b"ext4" {
        return Err(EvidenceUnavailable);
    }
    Ok(true)
}

/// Finite work and stack storage even for an oversized/malformed procfs view.
/// A missing final newline, duplicate mount ID or incomplete read is no proof.
fn check_mountinfo(mut input: impl Read, expected: MountIdentity) -> EvidenceResult<()> {
    let mut chunk = [0_u8; 8192];
    let mut line = [0_u8; MAX_MOUNTINFO_LINE_BYTES];
    let mut line_len = 0;
    let mut bytes = 0_usize;
    let mut lines = 0_usize;
    let mut found = false;
    loop {
        let count = input.read(&mut chunk).map_err(|_| EvidenceUnavailable)?;
        if count == 0 {
            break;
        }
        bytes = bytes.checked_add(count).ok_or(EvidenceUnavailable)?;
        if bytes > MAX_MOUNTINFO_BYTES {
            return Err(EvidenceUnavailable);
        }
        for byte in &chunk[..count] {
            if *byte == b'\n' {
                lines += 1;
                if lines > MAX_MOUNTINFO_LINES {
                    return Err(EvidenceUnavailable);
                }
                if check_record(&line[..line_len], expected)? {
                    if found {
                        return Err(EvidenceUnavailable);
                    }
                    found = true;
                }
                line_len = 0;
            } else {
                if line_len == line.len() {
                    return Err(EvidenceUnavailable);
                }
                line[line_len] = *byte;
                line_len += 1;
            }
        }
    }
    if !found || line_len != 0 {
        return Err(EvidenceUnavailable);
    }
    Ok(())
}

#[cfg(all(target_os = "linux", target_arch = "x86_64", target_env = "gnu"))]
mod native {
    use super::*;
    use std::{
        ffi::CStr,
        fs::File,
        mem::MaybeUninit,
        os::{
            fd::{AsRawFd, FromRawFd, RawFd},
            unix::fs::MetadataExt,
        },
    };

    // This value alone never distinguishes ext4 from ext2/ext3.
    const EXT_FAMILY_MAGIC: libc::c_long = 0xef53;
    const PROC_MAGIC: libc::c_long = 0x9fa0;
    const REQUIRED_STATX: u32 = libc::STATX_MNT_ID
        | libc::STATX_INO
        | libc::STATX_TYPE
        | libc::STATX_MODE
        | libc::STATX_UID;

    #[derive(Debug, PartialEq, Eq)]
    struct RootSnapshot {
        mount: MountIdentity,
        device: u64,
        inode: u64,
        uid: u32,
        mode: u32,
    }

    fn filesystem(file: &File) -> EvidenceResult<libc::c_long> {
        let mut value = MaybeUninit::<libc::statfs>::uninit();
        if unsafe { libc::fstatfs(file.as_raw_fd(), value.as_mut_ptr()) } != 0 {
            return Err(EvidenceUnavailable);
        }
        Ok(unsafe { value.assume_init() }.f_type)
    }

    fn snapshot(file: &File) -> EvidenceResult<RootSnapshot> {
        let metadata = file.metadata().map_err(|_| EvidenceUnavailable)?;
        if !metadata.is_dir()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o077 != 0
            || filesystem(file)? != EXT_FAMILY_MAGIC
        {
            return Err(EvidenceUnavailable);
        }
        let mut value = MaybeUninit::<libc::statx>::uninit();
        if unsafe {
            libc::statx(
                file.as_raw_fd(),
                c"".as_ptr(),
                libc::AT_EMPTY_PATH | libc::AT_STATX_SYNC_AS_STAT,
                REQUIRED_STATX,
                value.as_mut_ptr(),
            )
        } != 0
        {
            return Err(EvidenceUnavailable);
        }
        let value = unsafe { value.assume_init() };
        if value.stx_mask & REQUIRED_STATX != REQUIRED_STATX
            || value.stx_mnt_id == 0
            || value.stx_ino != metadata.ino()
            || value.stx_uid != metadata.uid()
            || u32::from(value.stx_mode) != metadata.mode()
            || value.stx_dev_major != libc::major(metadata.dev())
            || value.stx_dev_minor != libc::minor(metadata.dev())
        {
            return Err(EvidenceUnavailable);
        }
        Ok(RootSnapshot {
            mount: MountIdentity {
                id: value.stx_mnt_id,
                major: value.stx_dev_major,
                minor: value.stx_dev_minor,
            },
            device: metadata.dev(),
            inode: metadata.ino(),
            uid: metadata.uid(),
            mode: metadata.mode(),
        })
    }

    fn open_at(directory: RawFd, name: &CStr, is_directory: bool) -> EvidenceResult<File> {
        let flags = libc::O_RDONLY
            | libc::O_NOFOLLOW
            | libc::O_CLOEXEC
            | if is_directory { libc::O_DIRECTORY } else { 0 };
        let fd = unsafe { libc::openat(directory, name.as_ptr(), flags) };
        if fd < 0 {
            return Err(EvidenceUnavailable);
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }

    fn mountinfo() -> EvidenceResult<File> {
        // The only absolute lookup is a kernel evidence source, never the data
        // root. Verify its real filesystem before trusting even `self`.
        let proc = open_at(libc::AT_FDCWD, c"/proc", true)?;
        if filesystem(&proc)? != PROC_MAGIC {
            return Err(EvidenceUnavailable);
        }
        let mut self_link = [0_u8; 32];
        let length = unsafe {
            libc::readlinkat(
                proc.as_raw_fd(),
                c"self".as_ptr(),
                self_link.as_mut_ptr().cast(),
                self_link.len(),
            )
        };
        if length <= 0 || length as usize >= self_link.len() {
            return Err(EvidenceUnavailable);
        }
        if decimal(&self_link[..length as usize])? != u64::from(std::process::id()) {
            return Err(EvidenceUnavailable);
        }
        self_link[length as usize] = 0;
        let pid = CStr::from_bytes_with_nul(&self_link[..=length as usize])
            .map_err(|_| EvidenceUnavailable)?;
        let process = open_at(proc.as_raw_fd(), pid, true)?;
        let info = open_at(process.as_raw_fd(), c"mountinfo", false)?;
        let proc_device = proc.metadata().map_err(|_| EvidenceUnavailable)?.dev();
        if filesystem(&process)? != PROC_MAGIC
            || filesystem(&info)? != PROC_MAGIC
            || process.metadata().map_err(|_| EvidenceUnavailable)?.dev() != proc_device
            || info.metadata().map_err(|_| EvidenceUnavailable)?.dev() != proc_device
            || !info.metadata().map_err(|_| EvidenceUnavailable)?.is_file()
        {
            return Err(EvidenceUnavailable);
        }
        Ok(info)
    }

    pub(super) fn validate(file: &File) -> EvidenceResult<()> {
        let before = snapshot(file)?;
        check_mountinfo(mountinfo()?, before.mount)?;
        if snapshot(file)? != before {
            return Err(EvidenceUnavailable);
        }
        Ok(())
    }
}

#[cfg(all(target_os = "linux", target_arch = "x86_64", target_env = "gnu"))]
pub(super) fn validate(file: &std::fs::File) -> Result<(), crate::StoreError> {
    native::validate(file).map_err(|_| {
        crate::StoreError::Unavailable("allocation owner Linux ext4 evidence unavailable".into())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    const TARGET: MountIdentity = MountIdentity {
        id: 27,
        major: 8,
        minor: 1,
    };
    const RECORD: &[u8] = b"27 1 8:1 / / rw,relatime shared:1 - ext4 /dev/root rw\n";

    fn check(bytes: &[u8]) -> EvidenceResult<()> {
        check_mountinfo(Cursor::new(bytes), TARGET)
    }

    #[test]
    fn ext4_evidence_matches_mount_id_and_device_without_following_paths() {
        let records = b"9 1 0:5 / /proc rw - proc proc rw\n27 1 8:1 /escaped\\040root /renamed\\040path rw shared:1 master:2 - ext4 /dev/root rw\n";
        assert_eq!(check(records), Ok(()));
        assert_eq!(check(RECORD), Ok(()));
    }

    #[test]
    fn ext_family_magic_cannot_admit_ext2_ext3_or_other_mount_types() {
        for filesystem in [
            "ext2",
            "ext3",
            "xfs",
            "overlay",
            "tmpfs",
            "nfs",
            "fuse.ext4",
        ] {
            let record = format!("27 1 8:1 / / rw - {filesystem} none rw\n");
            assert!(check(record.as_bytes()).is_err(), "{filesystem}");
        }
    }

    #[test]
    fn missing_duplicate_or_wrong_device_is_not_mount_evidence() {
        assert!(check(b"28 1 8:1 / / rw - ext4 /dev/root rw\n").is_err());
        assert!(check(b"27 1 8:2 / / rw - ext4 /dev/root rw\n").is_err());
        assert!(check(b"27 1 9:1 / / rw - ext4 /dev/root rw\n").is_err());
        let mut twice = RECORD.to_vec();
        twice.extend_from_slice(RECORD);
        assert!(check(&twice).is_err());
    }

    #[test]
    fn malformed_numeric_fields_and_incomplete_records_are_unavailable() {
        for record in [
            "027 1 8:1 / / rw - ext4 none rw\n",
            "27 1 8:1:0 / / rw - ext4 none rw\n",
            "27 1 4294967296:1 / / rw - ext4 none rw\n",
            "18446744073709551616 1 8:1 / / rw - ext4 none rw\n",
            "27 1 8:1 / / rw ext4 none rw\n",
            "27 1 8:1 / / rw - ext4 none\n",
            "27 1 8:1 / / rw - ext4 none rw extra\n",
            "27 1 8:1 / / rw - ext4 none rw",
            "27 1 8:1 / / rw - ext4 none rw\r\n",
        ] {
            assert!(check(record.as_bytes()).is_err(), "{record:?}");
        }
        let mut malformed_after_match = RECORD.to_vec();
        malformed_after_match.extend_from_slice(b"invalid\n");
        assert!(check(&malformed_after_match).is_err());
    }

    #[test]
    fn mountinfo_line_count_total_bytes_and_line_bytes_are_bounded() {
        let mut too_long = RECORD[..RECORD.len() - 1].to_vec();
        too_long.resize(MAX_MOUNTINFO_LINE_BYTES + 1, b'x');
        too_long.push(b'\n');
        assert!(check(&too_long).is_err());

        let mut too_many = RECORD.to_vec();
        let unrelated = b"28 1 8:2 / /other rw - ext4 none rw\n";
        for _ in 0..MAX_MOUNTINFO_LINES {
            too_many.extend_from_slice(unrelated);
        }
        assert!(check(&too_many).is_err());

        let mut large_line = b"28 1 8:2 / /other rw ".to_vec();
        large_line.resize(1000, b'x');
        large_line.extend_from_slice(b" - ext4 none rw\n");
        let mut too_large = RECORD.to_vec();
        while too_large.len() <= MAX_MOUNTINFO_BYTES {
            too_large.extend_from_slice(&large_line);
        }
        assert!(check(&too_large).is_err());
    }

    #[test]
    fn failed_read_after_matching_record_cannot_mint_evidence() {
        struct FailAfterRecord(bool);
        impl Read for FailAfterRecord {
            fn read(&mut self, output: &mut [u8]) -> std::io::Result<usize> {
                if self.0 {
                    return Err(std::io::Error::other("injected read failure"));
                }
                self.0 = true;
                output[..RECORD.len()].copy_from_slice(RECORD);
                Ok(RECORD.len())
            }
        }
        assert!(check_mountinfo(FailAfterRecord(false), TARGET).is_err());
    }
}
