//! Actual APFS-native spool reservation. No sparse/truncate fallback is allowed.
use super::*;
use sha2::{Digest as _, Sha256};
use std::{
    ffi::CString,
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::MetadataExt,
    },
};
use tobkiri_gate::{CapacityKind, ControlKind};

struct PendingSpool {
    // The name is already gone before disk reservation/copy. All failure paths
    // close the writer and then the final reader before outer funding drops.
    writer: Option<File>,
    reader: Option<File>,
}
struct PendingName<'a> {
    root: &'a NativeSetupOwnerLease,
    name: CString,
    linked: bool,
}
impl PendingName<'_> {
    fn unlink(&mut self) -> Result<(), StoreError> {
        if self.linked {
            // SAFETY: both the pinned directory FD and exclusive name are live.
            if unsafe { libc::unlinkat(self.root.directory().as_raw_fd(), self.name.as_ptr(), 0) }
                != 0
            {
                return Err(std::io::Error::last_os_error().into());
            }
            self.linked = false;
        }
        Ok(())
    }
}
impl Drop for PendingName<'_> {
    fn drop(&mut self) {
        let _ = self.unlink();
    }
}

fn new_spool(root: &NativeSetupOwnerLease) -> Result<PendingSpool, StoreError> {
    let mut random = [0; 16];
    getrandom::fill(&mut random)
        .map_err(|_| StoreError::Unavailable("native spool name unavailable".into()))?;
    let name = CString::new(format!(".native-capture-{}", hex::encode(random)))
        .map_err(|_| StoreError::Invalid("native spool name".into()))?;
    // SAFETY: a valid pinned directory and NUL-terminated private leaf. O_EXCL
    // creates a distinct inode after the source FD has already been opened.
    let fd = unsafe {
        libc::openat(
            root.directory().as_raw_fd(),
            name.as_ptr(),
            libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    // SAFETY: exactly one owner is made for the fresh descriptor.
    let writer = unsafe { File::from_raw_fd(fd) };
    let mut name = PendingName {
        name,
        root,
        linked: true,
    };
    // Open the only readonly alias while this empty inode is still private.
    // SAFETY: the pinned root and private exclusive leaf are live.
    let fd = unsafe {
        libc::openat(
            root.directory().as_raw_fd(),
            name.name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    // SAFETY: exactly one owner for the fresh readonly descriptor.
    let reader = unsafe { File::from_raw_fd(fd) };
    let a = writer.metadata()?;
    let b = reader.metadata()?;
    if !a.is_file() || (a.dev(), a.ino(), a.len(), a.nlink()) != (b.dev(), b.ino(), 0, 1) {
        return Err(StoreError::Invalid("native spool identity changed".into()));
    }
    name.unlink()?;
    if reader.metadata()?.nlink() != 0 {
        return Err(StoreError::Invalid(
            "native snapshot still has a public alias".into(),
        ));
    }
    for file in [&writer, &reader] {
        // SAFETY: this flag applies only to our owned private spool descriptors.
        if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_NOCACHE, 1) } != 0 {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    Ok(PendingSpool {
        writer: Some(writer),
        reader: Some(reader),
    })
}

fn reserve_disk(file: &File, length: u64) -> Result<(), StoreError> {
    let length = i64::try_from(length)
        .map_err(|_| StoreError::Invalid("captured size is not an off_t".into()))?;
    if length == 0 {
        return Ok(());
    }
    let mut request = libc::fstore_t {
        fst_flags: libc::F_ALLOCATEALL,
        fst_posmode: libc::F_PEOFPOSMODE,
        fst_offset: 0,
        fst_length: length,
        fst_bytesalloc: 0,
    };
    // SAFETY: request has the exact platform fstore_t ABI. This reserves real
    // backing; changing only the file's logical length is never a substitute.
    if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_PREALLOCATE, &mut request) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    if request.fst_bytesalloc < length {
        return Err(StoreError::Unavailable(
            "native snapshot disk reservation was partial".into(),
        ));
    }
    Ok(())
}

pub(super) fn capture(
    root: &NativeSetupOwnerLease,
    invocation: &NativeSetupInvocation,
    source: NativeSnapshotSource<'_>,
    cancel: &CoreCancellation,
    check_work: &mut dyn FnMut(u64) -> Result<(), GateError>,
) -> Result<NativeFileSnapshot, StoreError> {
    if source.length >= 1 << 63 || source.relative.len() > 1024 * 1024 {
        return Err(StoreError::Invalid(
            "native capture source exceeds its original contract".into(),
        ));
    }
    let control = invocation
        .control()
        .reserve(ControlKind::Frame, SNAPSHOT_METADATA_BYTES)?;
    if LeasedShared::<HeldSnapshot, ControlReservation>::storage_bytes() as u64
        > SNAPSHOT_METADATA_BYTES
    {
        return Err(StoreError::Unavailable(
            "native snapshot metadata layout".into(),
        ));
    }
    // The original reader temporarily collects relative components/CStrings.
    // Fund that native metadata before it allocates, separately from DATA bytes.
    let parts = source.relative.bytes().filter(|b| *b == b'/').count() + 1;
    let path_bytes = parts
        .checked_mul(2)
        .and_then(|n| n.checked_mul(size_of::<&str>()))
        .and_then(|n| n.checked_add(source.relative.len()))
        .and_then(|n| n.checked_add(8192))
        .ok_or_else(|| StoreError::Invalid("native capture metadata overflow".into()))?;
    let _transient = invocation
        .control()
        .reserve(ControlKind::Frame, path_bytes as u64)?;
    // One streaming buffer plus bounded hash/reader scratch. File length is a
    // disk resource, not a newly invented 64 MiB file-size ceiling.
    let _memory = invocation.reserve(CapacityKind::AbiMarshal, (READ_CHUNK + 16 * 1024) as u64)?;
    let mut scratch = Vec::new();
    scratch
        .try_reserve_exact(READ_CHUNK)
        .map_err(|_| StoreError::Quota)?;
    if scratch.capacity() != READ_CHUNK {
        return Err(StoreError::Unavailable(
            "native capture allocator capacity".into(),
        ));
    }
    scratch.resize(READ_CHUNK, 0);
    let mut failure = None;
    let pending = tobkiri_canon::files::read_relative_with(
        source.root,
        source.relative,
        source.length,
        |reader, length| {
            let copy = (|| -> Result<PendingSpool, StoreError> {
                if length != source.length {
                    return Err(StoreError::Invalid("captured source size changed".into()));
                }
                check_active(invocation, cancel)?;
                check_work(1)?;
                let mut pending = new_spool(root)?;
                let writer = pending.writer.as_mut().expect("live spool writer");
                reserve_disk(writer, length)?;
                let mut hash = Sha256::new();
                let mut remaining = length;
                while remaining != 0 {
                    check_active(invocation, cancel)?;
                    let count =
                        usize::try_from(remaining.min(READ_CHUNK as u64)).expect("bounded chunk");
                    check_work(count as u64)?;
                    Read::read_exact(reader, &mut scratch[..count])?;
                    writer.write_all(&scratch[..count])?;
                    hash.update(&scratch[..count]);
                    remaining -= count as u64;
                }
                check_active(invocation, cancel)?;
                check_work(1)?;
                if Digest::from_sha256(hash.finalize().into()) != *source.sha256 {
                    return Err(StoreError::Invalid("captured source hash changed".into()));
                }
                writer.sync_all()?;
                Ok(pending)
            })();
            copy.map_err(|error| {
                failure = Some(error);
                tobkiri_canon::CanonError::Invalid("native capture failed".into())
            })
        },
    );
    if let Some(error) = failure {
        return Err(error);
    }
    let mut pending = pending.map_err(|error| StoreError::Canon(error.to_string()))?;
    // The original source reader has now completed its exact-length/EOF and
    // dev/inode/mode/size/mtime/ctime recheck. No result escaped its callback.
    check_active(invocation, cancel)?;
    check_work(1)?;
    let reader = pending.reader.take().expect("live readonly spool");
    let writer = pending.writer.as_ref().expect("live spool writer");
    let before = writer.metadata()?;
    let readonly = reader.metadata()?;
    if (before.dev(), before.ino(), before.len())
        != (readonly.dev(), readonly.ino(), readonly.len())
        || readonly.nlink() != 0
        || readonly.len() != source.length
    {
        return Err(StoreError::Invalid("native spool identity changed".into()));
    }
    drop(pending.writer.take());
    if reader.metadata()?.nlink() != 0 {
        return Err(StoreError::Invalid(
            "native snapshot still has a public alias".into(),
        ));
    }
    // Bind the bytes returned by callbacks to the actual closed-writer backing,
    // not merely to the source bytes supplied to write_all during the copy.
    use std::os::unix::fs::FileExt;
    let mut hash = Sha256::new();
    let mut offset = 0;
    while offset < source.length {
        check_active(invocation, cancel)?;
        let count = usize::try_from((source.length - offset).min(READ_CHUNK as u64))
            .expect("bounded chunk");
        check_work(count as u64)?;
        let mut read = 0;
        while read < count {
            check_active(invocation, cancel)?;
            match reader.read_at(&mut scratch[read..count], offset + read as u64) {
                Ok(0) => return Err(StoreError::Invalid("native snapshot ended early".into())),
                Ok(n) => read += n,
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(e) => return Err(e.into()),
            }
        }
        hash.update(&scratch[..count]);
        offset += count as u64;
    }
    if Digest::from_sha256(hash.finalize().into()) != *source.sha256 {
        return Err(StoreError::Invalid(
            "sealed native snapshot hash mismatch".into(),
        ));
    }
    check_active(invocation, cancel)?;
    check_work(1)?;
    let held = HeldSnapshot {
        _file: reader,
        length: source.length,
        sha256: source.sha256.clone(),
        invocation: invocation.clone(),
        cancel: cancel.clone(),
        _root: root.clone(),
    };
    LeasedShared::try_new_precharged(held, control)
        .map(NativeFileSnapshot)
        .map_err(|_| StoreError::Quota)
}
