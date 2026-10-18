//! Native primitives for the common DATA extent/reservation VFS.
//! Windows lock files deny DELETE sharing for their entire descriptor lifetime,
//! so a mutable filename cannot split an active byte-lock domain.
use crate::StoreError;
use rusqlite::ffi::*;
use std::{ffi::CStr, fs::File, io};

pub(crate) struct Stat {
    pub(crate) st_dev: u64,
    pub(crate) st_ino: u64,
    pub(crate) st_size: i64,
}

#[cfg(unix)]
mod native {
    use super::*;
    use std::os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::MetadataExt,
    };
    pub(crate) fn identity(directory: &File) -> Result<(u64, u64), StoreError> {
        let metadata = directory.metadata()?;
        Ok((metadata.dev(), metadata.ino()))
    }
    pub(crate) fn directory_lock(directory: &File) -> Result<File, StoreError> {
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                c".".as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error().into());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }
    pub(crate) fn lock(file: &File, exclusive: bool) -> io::Result<()> {
        let mode = if exclusive {
            libc::LOCK_EX
        } else {
            libc::LOCK_SH
        };
        if unsafe { libc::flock(file.as_raw_fd(), mode | libc::LOCK_NB) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    pub(crate) fn unlock(file: &File) -> io::Result<()> {
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) } != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    pub(crate) fn stat(directory: &File, name: &CStr) -> Result<Option<Stat>, i32> {
        let mut stat = std::mem::MaybeUninit::<libc::stat>::uninit();
        if unsafe {
            libc::fstatat(
                directory.as_raw_fd(),
                name.as_ptr(),
                stat.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        } != 0
        {
            return if io::Error::last_os_error().kind() == io::ErrorKind::NotFound {
                Ok(None)
            } else {
                Err(SQLITE_IOERR_FSTAT)
            };
        }
        let stat = unsafe { stat.assume_init() };
        if stat.st_mode & libc::S_IFMT != libc::S_IFREG || stat.st_nlink != 1 || stat.st_size < 0 {
            return Err(SQLITE_CANTOPEN);
        }
        Ok(Some(Stat {
            st_dev: stat.st_dev as u64,
            st_ino: stat.st_ino as u64,
            st_size: stat.st_size,
        }))
    }
    pub(crate) fn open_lease(directory: &File, name: &CStr) -> Result<File, StoreError> {
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDWR | libc::O_CREAT | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error().into());
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.nlink() != 1 || metadata.len() != 0 {
            return Err(StoreError::Denied("invalid DATA lease inode".into()));
        }
        Ok(file)
    }
    pub(crate) fn unlink(directory: &File, name: &CStr) -> i32 {
        unsafe { libc::unlinkat(directory.as_raw_fd(), name.as_ptr(), 0) }
    }
}

#[cfg(windows)]
mod native {
    use super::*;
    use crate::secure::windows as secure;
    use std::{mem::zeroed, os::windows::io::AsRawHandle};
    use windows_sys::Win32::{
        Storage::FileSystem::{
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES, FILE_SHARE_READ, FILE_SHARE_WRITE,
            LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, LockFileEx, OPEN_EXISTING,
            READ_CONTROL, UnlockFileEx,
        },
        System::IO::OVERLAPPED,
    };
    fn legacy_identity(identity: (u64, [u8; 16])) -> Result<(u64, u64), StoreError> {
        // The current durable DATA transfer receipt has a 64-bit inode field.
        // Local NTFS uses 64-bit IDs. Refuse, rather than truncate, any future
        // filesystem identity that cannot be represented by this contract.
        if identity.1[8..].iter().any(|byte| *byte != 0) {
            return Err(StoreError::Unavailable(
                "DATA file identity exceeds receipt width".into(),
            ));
        }
        Ok((
            identity.0,
            u64::from_le_bytes(identity.1[..8].try_into().unwrap()),
        ))
    }
    fn text(name: &CStr) -> Result<&str, StoreError> {
        name.to_str()
            .map_err(|_| StoreError::Invalid("non-UTF8 DATA name".into()))
    }
    pub(crate) fn identity(directory: &File) -> Result<(u64, u64), StoreError> {
        secure::private_directory(directory)?;
        legacy_identity(secure::file_identity(directory)?)
    }
    pub(crate) fn directory_lock(directory: &File) -> Result<File, StoreError> {
        secure::open_file_lease(directory, ".tobkiri-quota-directory.lease")
    }
    pub(crate) fn lock(file: &File, exclusive: bool) -> io::Result<()> {
        let mut overlapped: OVERLAPPED = unsafe { zeroed() };
        let flags = LOCKFILE_FAIL_IMMEDIATELY
            | if exclusive {
                LOCKFILE_EXCLUSIVE_LOCK
            } else {
                0
            };
        if unsafe { LockFileEx(file.as_raw_handle(), flags, 0, 1, 0, &mut overlapped) } == 0 {
            let error = io::Error::last_os_error();
            return Err(if error.raw_os_error() == Some(33) {
                io::Error::from(io::ErrorKind::WouldBlock)
            } else {
                error
            });
        }
        Ok(())
    }
    pub(crate) fn unlock(file: &File) -> io::Result<()> {
        let mut overlapped: OVERLAPPED = unsafe { zeroed() };
        if unsafe { UnlockFileEx(file.as_raw_handle(), 0, 1, 0, &mut overlapped) } == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    pub(crate) fn stat(directory: &File, name: &CStr) -> Result<Option<Stat>, i32> {
        let file = match secure::open_file_native(
            directory,
            text(name).map_err(|_| SQLITE_CANTOPEN)?,
            FILE_READ_ATTRIBUTES | READ_CONTROL,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT,
        ) {
            Ok(file) => file,
            Err(StoreError::Io(error)) if error.kind() == io::ErrorKind::NotFound => {
                return Ok(None);
            }
            Err(_) => return Err(SQLITE_CANTOPEN),
        };
        let metadata = secure::regular_metadata(&file).map_err(|_| SQLITE_IOERR_FSTAT)?;
        let (st_dev, st_ino) = legacy_identity(metadata.identity).map_err(|_| SQLITE_CANTOPEN)?;
        let st_size = i64::try_from(metadata.len).map_err(|_| SQLITE_CANTOPEN)?;
        Ok(Some(Stat {
            st_dev,
            st_ino,
            st_size,
        }))
    }
    pub(crate) fn open_lease(directory: &File, name: &CStr) -> Result<File, StoreError> {
        let file = secure::open_file_lease(directory, text(name)?)?;
        if secure::regular_metadata(&file)?.len != 0 {
            return Err(StoreError::Denied("nonempty DATA lease inode".into()));
        }
        Ok(file)
    }
    pub(crate) fn unlink(directory: &File, name: &CStr) -> i32 {
        match text(name).and_then(|name| secure::unlink_file(directory, name)) {
            Ok(()) => 0,
            Err(_) => -1,
        }
    }
}
pub(crate) use native::*;
