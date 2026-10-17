//! Windows DATA capabilities. Only a local NTFS namespace is admitted.
//!
//! Descendants are opened as single components with NtCreateFile's
//! RootDirectory, never by concatenating a diagnostic path. Every open inspects
//! the returned handle for reparse points, private ACLs and hardlink aliases.
//! See Microsoft's NtCreateFile and GetFileInformationByHandleEx contracts.
use crate::StoreError;
use std::{
    fs::File,
    io::{Read, Write},
    mem::{size_of, zeroed},
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    path::{Component, Path, Prefix},
    ptr::{null, null_mut},
};
use windows_sys::{
    Wdk::{Foundation::OBJECT_ATTRIBUTES, Storage::FileSystem::*},
    Win32::{
        Foundation::{
            INVALID_HANDLE_VALUE, LocalFree, OBJ_CASE_INSENSITIVE, RtlNtStatusToDosError,
            UNICODE_STRING,
        },
        Security::{ACE_HEADER, Authorization::*, *},
        Storage::FileSystem::*,
        System::{
            IO::{IO_STATUS_BLOCK, OVERLAPPED},
            SystemServices::{
                ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE, FILE_PERSISTENT_ACLS,
            },
            Threading::{GetCurrentProcess, OpenProcessToken},
        },
    },
};

type Result<T> = std::result::Result<T, StoreError>;
const DIRECTORY_ACCESS: u32 =
    FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE;
const READ_ACCESS: u32 = FILE_READ_DATA | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE;
const OBJ_DONT_REPARSE: u32 = 0x1000;
const FILE_REMOTE_DEVICE: u32 = 0x10;
const STATUS_NO_MORE_FILES: i32 = 0x80000006u32 as i32;

fn denied(message: &str) -> StoreError {
    StoreError::Denied(message.into())
}
fn stage(error: StoreError, label: &str) -> StoreError {
    match error {
        StoreError::Io(error) => StoreError::Io(std::io::Error::new(
            error.kind(),
            format!("{label}: {error}"),
        )),
        other => other,
    }
}
fn nt_result(status: i32, block: &IO_STATUS_BLOCK) -> Result<()> {
    if status < 0 {
        return Err(std::io::Error::from_raw_os_error(
            unsafe { RtlNtStatusToDosError(status) } as i32
        )
        .into());
    }
    if status != 0 || unsafe { block.Anonymous.Status } != 0 {
        return Err(StoreError::Unavailable(
            "native storage operation did not complete synchronously".into(),
        ));
    }
    Ok(())
}

struct LocalAllocation(*mut std::ffi::c_void);
impl Drop for LocalAllocation {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { LocalFree(self.0) };
        }
    }
}
fn sid_string(sid: PSID) -> Result<String> {
    let mut text = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let _allocation = LocalAllocation(text.cast());
    let mut length = 0;
    unsafe {
        while *text.add(length) != 0 {
            length += 1;
        }
    }
    String::from_utf16(unsafe { std::slice::from_raw_parts(text, length) })
        .map_err(|_| denied("invalid owner SID"))
}
fn user_sid() -> Result<String> {
    let mut raw = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let token = unsafe { OwnedHandle::from_raw_handle(raw) };
    let mut needed = 0;
    unsafe { GetTokenInformation(token.as_raw_handle(), TokenUser, null_mut(), 0, &mut needed) };
    if needed == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let mut storage = vec![0usize; (needed as usize).div_ceil(size_of::<usize>())];
    if unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            TokenUser,
            storage.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    sid_string(unsafe { (*(storage.as_ptr().cast::<TOKEN_USER>())).User.Sid })
}
fn descriptor(sddl: &str) -> Result<LocalAllocation> {
    let text = sddl.encode_utf16().chain([0]).collect::<Vec<_>>();
    let mut raw = null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            text.as_ptr(),
            SDDL_REVISION_1,
            &mut raw,
            null_mut(),
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(LocalAllocation(raw))
}
fn private_descriptor() -> Result<LocalAllocation> {
    let sid = user_sid()?;
    descriptor(&format!(
        "O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    ))
}
fn private_acl(file: &File) -> Result<()> {
    let mut owner = null_mut();
    let mut dacl = null_mut();
    let mut raw = null_mut();
    let status = unsafe {
        GetSecurityInfo(
            file.as_raw_handle(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut dacl,
            null_mut(),
            &mut raw,
        )
    };
    let _allocation = LocalAllocation(raw);
    if status != 0 {
        return Err(std::io::Error::from_raw_os_error(status as i32).into());
    }
    if owner.is_null() || dacl.is_null() || unsafe { IsValidAcl(dacl) } == 0 {
        return Err(denied("DATA requires a valid owner and non-null DACL"));
    }
    let sid = user_sid()?;
    if sid_string(owner)? != sid {
        return Err(denied("DATA owner is not the current user"));
    }
    let mut control = 0;
    let mut revision = 0;
    if unsafe { GetSecurityDescriptorControl(raw, &mut control, &mut revision) } == 0
        || control & SE_DACL_PROTECTED == 0
    {
        return Err(denied("DATA DACL must be protected from inheritance"));
    }
    for index in 0..u32::from(unsafe { (*dacl).AceCount }) {
        let mut ace = null_mut();
        if unsafe { GetAce(dacl, index, &mut ace) } == 0 || ace.is_null() {
            return Err(denied("invalid DATA ACE"));
        }
        let header = unsafe { &*ace.cast::<ACE_HEADER>() };
        if header.AceType == ACCESS_DENIED_ACE_TYPE as u8 {
            continue;
        }
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE as u8
            || usize::from(header.AceSize) < size_of::<ACCESS_ALLOWED_ACE>()
        {
            return Err(denied("unsupported DATA grant ACE"));
        }
        let allow = unsafe { &*ace.cast::<ACCESS_ALLOWED_ACE>() };
        let grantee = sid_string((&allow.SidStart as *const u32).cast_mut().cast())?;
        // Include inherit-only ACEs: a private namespace must not delegate a
        // broad grant to future children, even if it does not apply here.
        if grantee != sid && !matches!(grantee.as_str(), "S-1-5-18" | "S-1-5-32-544") {
            return Err(denied(
                "DATA ACL grants access outside its trusted owner set",
            ));
        }
    }
    Ok(())
}

/// Stable, untruncated identity of the held native object.
pub(crate) fn file_identity(file: &File) -> Result<(u64, [u8; 16])> {
    let _ = checked_information(file)?;
    let mut info: FILE_ID_INFO = unsafe { zeroed() };
    if unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle(),
            FileIdInfo,
            (&mut info as *mut FILE_ID_INFO).cast(),
            size_of::<FILE_ID_INFO>() as u32,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok((info.VolumeSerialNumber, info.FileId.Identifier))
}
fn checked_information(file: &File) -> Result<BY_HANDLE_FILE_INFORMATION> {
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0 && info.nNumberOfLinks != 1)
    {
        return Err(denied(
            "DATA object must not be a reparse point or multiply-linked file",
        ));
    }
    if unsafe { GetFileType(file.as_raw_handle()) } != FILE_TYPE_DISK {
        return Err(denied("DATA object is not a disk file"));
    }
    Ok(info)
}
fn local_ntfs(file: &File) -> Result<()> {
    #[repr(C)]
    struct Device {
        kind: u32,
        characteristics: u32,
    }
    let mut device = Device {
        kind: 0,
        characteristics: 0,
    };
    let mut block = unsafe { zeroed() };
    let status = unsafe {
        NtQueryVolumeInformationFile(
            file.as_raw_handle(),
            &mut block,
            (&mut device as *mut Device).cast(),
            size_of::<Device>() as u32,
            FileFsDeviceInformation,
        )
    };
    nt_result(status, &block)?;
    if device.characteristics & FILE_REMOTE_DEVICE != 0 {
        return Err(denied("remote DATA volumes are not supported"));
    }
    let mut name = [0u16; 32];
    let mut flags = 0;
    if unsafe {
        GetVolumeInformationByHandleW(
            file.as_raw_handle(),
            null_mut(),
            0,
            null_mut(),
            null_mut(),
            &mut flags,
            name.as_mut_ptr(),
            name.len() as u32,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    let end = name
        .iter()
        .position(|x| *x == 0)
        .ok_or_else(|| denied("invalid filesystem name"))?;
    if String::from_utf16_lossy(&name[..end]) != "NTFS" || flags & FILE_PERSISTENT_ACLS == 0 {
        return Err(StoreError::Unavailable(
            "Windows DATA currently requires local NTFS with persistent ACLs".into(),
        ));
    }
    Ok(())
}
/// Revalidate the native directory's type, ACL, volume and no-reparse policy.
pub(crate) fn private_directory(file: &File) -> Result<()> {
    if checked_information(file)?.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
        return Err(denied("DATA capability is not a directory"));
    }
    local_ntfs(file)?;
    private_acl(file)
}
pub(crate) fn validate_component(value: &str) -> Result<()> {
    let stem = value
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_uppercase();
    if value.is_empty()
        || value == "."
        || value == ".."
        || value.contains(['/', '\\', ':', '<', '>', '"', '|', '?', '*'])
        || value.ends_with(['.', ' '])
        || value.chars().any(char::is_control)
        || matches!(
            stem.as_str(),
            "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
        )
        || ((stem.starts_with("COM") || stem.starts_with("LPT"))
            && stem.chars().count() == 4
            && matches!(stem.chars().nth(3), Some('1'..='9' | '¹' | '²' | '³')))
    {
        return Err(StoreError::Invalid("unsafe Windows DATA component".into()));
    }
    Ok(())
}
/// Managed storage IDs are case-sensitive logical keys. Windows admission
/// restricts their physical spelling instead of silently merging distinct
/// Profile/Pack/store IDs through NTFS case or short-name aliases. Absolute
/// host ancestors and encrypted logical map keys are not storage components.
pub(crate) fn validate_storage_component(value: &str) -> Result<()> {
    if !value
        .bytes()
        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte))
    {
        return Err(StoreError::Invalid(
            "Windows storage IDs require lowercase ASCII components".into(),
        ));
    }
    validate_component(value)
}
fn open_relative(
    parent: &File,
    value: &str,
    access: u32,
    share: u32,
    disposition: u32,
    options: u32,
    security: bool,
) -> Result<File> {
    let mut encoded = value.encode_utf16().collect::<Vec<_>>();
    let bytes = u16::try_from(
        encoded
            .len()
            .checked_mul(2)
            .ok_or_else(|| denied("name too long"))?,
    )
    .map_err(|_| denied("name too long"))?;
    let name = UNICODE_STRING {
        Length: bytes,
        MaximumLength: bytes,
        Buffer: encoded.as_mut_ptr(),
    };
    let descriptor = if security {
        Some(private_descriptor()?)
    } else {
        None
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.as_raw_handle(),
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
        SecurityDescriptor: descriptor.as_ref().map_or(null(), |d| d.0.cast()),
        SecurityQualityOfService: null(),
    };
    let mut raw = null_mut();
    let mut block = unsafe { zeroed() };
    let status = unsafe {
        NtCreateFile(
            &mut raw,
            access,
            &attributes,
            &mut block,
            null(),
            FILE_ATTRIBUTE_NORMAL,
            share,
            disposition,
            options | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            null(),
            0,
        )
    };
    if status < 0 {
        return Err(std::io::Error::from_raw_os_error(
            unsafe { RtlNtStatusToDosError(status) } as i32
        )
        .into());
    }
    if raw.is_null() || raw == INVALID_HANDLE_VALUE {
        return Err(denied("native DATA open returned no handle"));
    }
    let file = unsafe { File::from_raw_handle(raw) };
    nt_result(status, &block)?;
    Ok(file)
}
fn open_directory_relative(parent: &File, value: &str, create: bool) -> Result<File> {
    let open = || {
        open_relative(
            parent,
            value,
            DIRECTORY_ACCESS,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            FILE_OPEN,
            FILE_DIRECTORY_FILE,
            false,
        )
    };
    match open() {
        Ok(file) => Ok(file),
        Err(StoreError::Io(error)) if create && error.kind() == std::io::ErrorKind::NotFound => {
            // OPEN_IF requests create authority even for an existing ancestor
            // on Windows. First open read-only; only an absent name needs the
            // caller's child-creation authority and the protected descriptor.
            match open_relative(
                parent,
                value,
                DIRECTORY_ACCESS,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                FILE_CREATE,
                FILE_DIRECTORY_FILE,
                true,
            ) {
                Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    open()
                }
                result => result,
            }
        }
        Err(error) => Err(error),
    }
}
pub(crate) fn open_root(path: &Path) -> Result<File> {
    let mut parts = path.components();
    let disk = match parts.next() {
        Some(Component::Prefix(p)) => match p.kind() {
            Prefix::Disk(d) | Prefix::VerbatimDisk(d) => d,
            _ => return Err(denied("DATA requires a local disk path")),
        },
        _ => return Err(denied("DATA requires a local disk path")),
    };
    if !matches!(parts.next(), Some(Component::RootDir)) {
        return Err(denied("DATA requires an absolute root"));
    }
    let components = parts
        .map(|part| match part {
            Component::Normal(s) => s
                .to_str()
                .ok_or_else(|| denied("non-Unicode DATA path"))
                .and_then(|s| {
                    validate_component(s)?;
                    Ok(s.to_owned())
                }),
            _ => Err(denied("unsafe DATA root path")),
        })
        .collect::<Result<Vec<_>>>()?;
    if components.is_empty() {
        return Err(denied("a volume root cannot be a private DATA root"));
    }
    let volume = format!("\\\\?\\{}:\\", char::from(disk))
        .encode_utf16()
        .chain([0])
        .collect::<Vec<_>>();
    let raw = unsafe {
        CreateFileW(
            volume.as_ptr(),
            DIRECTORY_ACCESS,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            null(),
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            null_mut(),
        )
    };
    if raw == INVALID_HANDLE_VALUE {
        return Err(std::io::Error::last_os_error().into());
    }
    let mut dir = unsafe { File::from_raw_handle(raw) };
    local_ntfs(&dir).map_err(|error| stage(error, "inspect DATA volume"))?;
    for (index, component) in components.iter().enumerate() {
        // Ancestors are namespace anchors, not authority for the final root's
        // ACL. New directories are private from their first native handle.
        let child = open_directory_relative(&dir, component, true)
            .map_err(|error| stage(error, &format!("open DATA ancestor {index}")))?;
        if checked_information(&child)?.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY == 0 {
            return Err(denied("DATA ancestor is not a directory"));
        }
        if index + 1 == components.len() {
            private_directory(&child)?;
        }
        dir = child;
    }
    Ok(dir)
}
pub(crate) fn open_dir(parent: &File, value: &str, create: bool) -> Result<File> {
    validate_storage_component(value)?;
    private_directory(parent)?;
    let file = open_directory_relative(parent, value, create)?;
    private_directory(&file)?;
    Ok(file)
}
pub(crate) fn open_file(parent: &File, value: &str, create: bool, exclusive: bool) -> Result<File> {
    validate_storage_component(value)?;
    private_directory(parent)?;
    let access = READ_ACCESS
        | if create {
            FILE_WRITE_DATA | FILE_APPEND_DATA
        } else {
            0
        };
    let share = if exclusive {
        0
    } else if create {
        FILE_SHARE_READ | FILE_SHARE_WRITE
    } else {
        FILE_SHARE_READ
    };
    let file = open_relative(
        parent,
        value,
        access,
        share,
        if exclusive {
            FILE_CREATE
        } else if create {
            FILE_OPEN_IF
        } else {
            FILE_OPEN
        },
        FILE_NON_DIRECTORY_FILE,
        create,
    )?;
    let _ = checked_information(&file)?;
    private_acl(&file)?;
    Ok(file)
}
pub(crate) fn open_file_lease(parent: &File, value: &str) -> Result<File> {
    open_file(parent, value, true, false)
}

/// Identity and logical length captured from one checked regular-file handle.
pub(crate) struct RegularMetadata {
    pub(crate) identity: (u64, [u8; 16]),
    pub(crate) len: u64,
}
pub(crate) fn regular_metadata(file: &File) -> Result<RegularMetadata> {
    if checked_information(file)?.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0 {
        return Err(denied("DATA requires a regular file"));
    }
    private_acl(file)?;
    Ok(RegularMetadata {
        identity: file_identity(file)?,
        len: file.metadata()?.len(),
    })
}

/// Native Win32-shaped open for the SQLite adapter. Existing objects are
/// inspected before any truncation/delete-on-close side effect. The diagnostic
/// path never participates in opening the object.
pub(crate) fn open_file_native(
    parent: &File,
    value: &str,
    access: u32,
    share: u32,
    disposition: u32,
    flags: u32,
) -> Result<File> {
    validate_storage_component(value)?;
    private_directory(parent)?;
    let supported = FILE_ATTRIBUTE_NORMAL
        | FILE_FLAG_RANDOM_ACCESS
        | FILE_FLAG_SEQUENTIAL_SCAN
        | FILE_FLAG_WRITE_THROUGH
        | FILE_FLAG_OPEN_REPARSE_POINT
        | FILE_FLAG_DELETE_ON_CLOSE;
    if flags & !supported != 0 {
        return Err(StoreError::Unavailable(
            "unsupported native DATA file-open flags".into(),
        ));
    }
    let access = access
        | FILE_READ_ATTRIBUTES
        | READ_CONTROL
        | SYNCHRONIZE
        | if flags & FILE_FLAG_DELETE_ON_CLOSE != 0 {
            DELETE
        } else {
            0
        };
    let options = FILE_NON_DIRECTORY_FILE
        | if flags & FILE_FLAG_WRITE_THROUGH != 0 {
            FILE_WRITE_THROUGH
        } else {
            0
        };
    let share = share & !FILE_SHARE_DELETE;
    let create = |mode| open_relative(parent, value, access, share, mode, options, true);
    let file = match disposition {
        CREATE_NEW => create(FILE_CREATE)?,
        OPEN_EXISTING | TRUNCATE_EXISTING => {
            open_relative(parent, value, access, share, FILE_OPEN, options, false)?
        }
        OPEN_ALWAYS => create(FILE_OPEN_IF)?,
        CREATE_ALWAYS => match create(FILE_CREATE) {
            Ok(file) => file,
            Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                open_relative(parent, value, access, share, FILE_OPEN, options, false)?
            }
            Err(error) => return Err(error),
        },
        _ => {
            return Err(StoreError::Invalid(
                "unknown native DATA create disposition".into(),
            ));
        }
    };
    regular_metadata(&file)?;
    if matches!(disposition, CREATE_ALWAYS | TRUNCATE_EXISTING) {
        file.set_len(0)?;
    }
    if flags & FILE_FLAG_DELETE_ON_CLOSE != 0 {
        let info = FILE_DISPOSITION_INFO { DeleteFile: true };
        if unsafe {
            SetFileInformationByHandle(
                file.as_raw_handle(),
                FileDispositionInfo,
                (&info as *const FILE_DISPOSITION_INFO).cast(),
                size_of::<FILE_DISPOSITION_INFO>() as u32,
            )
        } == 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
    }
    Ok(file)
}
pub(crate) fn reopen_directory_owner(directory: &File) -> Result<File> {
    private_directory(directory)?;
    let file = open_relative(
        directory,
        "",
        DIRECTORY_ACCESS | FILE_ADD_FILE,
        FILE_SHARE_READ | FILE_SHARE_DELETE,
        FILE_OPEN,
        FILE_DIRECTORY_FILE,
        false,
    )?;
    private_directory(&file)?;
    if file_identity(directory)? != file_identity(&file)? {
        return Err(denied("owner directory identity changed"));
    }
    Ok(file)
}
pub(crate) fn lock_exclusive(file: &File) -> Result<()> {
    let mut overlap: OVERLAPPED = unsafe { zeroed() };
    if unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK,
            0,
            1,
            0,
            &mut overlap,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(())
}
pub(crate) fn read(parent: &File, value: &str, max: usize) -> Result<Vec<u8>> {
    let file = open_file(parent, value, false, false)?;
    if file.metadata()?.len() > max as u64 {
        return Err(StoreError::Quota);
    }
    let mut bytes = Vec::new();
    file.take((max as u64).saturating_add(1))
        .read_to_end(&mut bytes)?;
    if bytes.len() > max {
        return Err(StoreError::Quota);
    }
    Ok(bytes)
}
pub(crate) fn file_len(parent: &File, value: &str) -> Result<Option<u64>> {
    match open_file_native(
        parent,
        value,
        FILE_READ_ATTRIBUTES | READ_CONTROL,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        OPEN_EXISTING,
        FILE_FLAG_OPEN_REPARSE_POINT,
    ) {
        Ok(file) => Ok(Some(regular_metadata(&file)?.len)),
        Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}
pub(crate) fn path_identity(parent: &File, value: &str) -> Result<Option<(u64, [u8; 16])>> {
    validate_storage_component(value)?;
    private_directory(parent)?;
    let file = match open_relative(
        parent,
        value,
        FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        FILE_OPEN,
        0,
        false,
    ) {
        Ok(file) => file,
        Err(StoreError::Io(error)) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(None);
        }
        Err(error) => return Err(error),
    };
    private_acl(&file)?;
    Ok(Some(file_identity(&file)?))
}
pub(crate) fn sync_directory(directory: &File) -> Result<()> {
    private_directory(directory)?;
    if crate::owner::flush_windows_owner_directory(directory)? {
        return Ok(());
    }
    // FlushFileBuffers/NtFlushBuffersFile require write-data authority. Obtain
    // it from this exact object, not a pathname, for the duration of the flush.
    // A separately held allocation-owner lease may deliberately deny this
    // open; that conflict remains an error, never an ignored durability check.
    let flush = open_relative(
        directory,
        "",
        DIRECTORY_ACCESS | FILE_ADD_FILE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        FILE_OPEN,
        FILE_DIRECTORY_FILE,
        false,
    )?;
    if file_identity(&flush)? != file_identity(directory)? {
        return Err(denied("directory changed before flush"));
    }
    let mut block = unsafe { zeroed() };
    let status = unsafe { NtFlushBuffersFile(flush.as_raw_handle(), &mut block) };
    nt_result(status, &block)
}
fn rename_handle(
    file: &File,
    destination: &File,
    value: &str,
    replace: bool,
    same_directory: bool,
) -> Result<()> {
    validate_storage_component(value)?;
    private_directory(destination)?;
    let name = value.encode_utf16().collect::<Vec<_>>();
    let length = size_of::<FILE_RENAME_INFORMATION>() + name.len() * 2;
    let mut storage = vec![0usize; length.div_ceil(size_of::<usize>())];
    let info = storage.as_mut_ptr().cast::<FILE_RENAME_INFORMATION>();
    let mut block = unsafe { zeroed() };
    let status = unsafe {
        (*info).Anonymous.ReplaceIfExists = replace;
        // A simple name with a NULL root uses the opened source's current
        // parent. For a proven same-directory operation this avoids Windows
        // reopening the directory for write access, which must stay excluded
        // while its physical allocation-owner handle is alive. The source
        // inode and directory are continuously pinned without DELETE sharing.
        (*info).RootDirectory = if same_directory {
            null_mut()
        } else {
            destination.as_raw_handle()
        };
        (*info).FileNameLength = (name.len() * 2) as u32;
        std::ptr::copy_nonoverlapping(name.as_ptr(), (*info).FileName.as_mut_ptr(), name.len());
        NtSetInformationFile(
            file.as_raw_handle(),
            &mut block,
            info.cast(),
            length as u32,
            FileRenameInformation,
        )
    };
    nt_result(status, &block)
}
pub(crate) fn write_atomic(parent: &File, value: &str, bytes: &[u8]) -> Result<()> {
    publish_file(parent, value, bytes, true).map(|_| ())
}
pub(crate) fn publish_new(parent: &File, value: &str, bytes: &[u8]) -> Result<bool> {
    publish_file(parent, value, bytes, false)
}
fn publish_file(parent: &File, value: &str, bytes: &[u8], replace: bool) -> Result<bool> {
    validate_storage_component(value)?;
    private_directory(parent)?;
    // Reject unsafe existing targets before creating any temporary object.
    let exists = file_len(parent, value)?.is_some();
    if exists && !replace {
        return Ok(false);
    }
    let temporary = super::temporary_name()?;
    let mut file = open_relative(
        parent,
        &temporary,
        READ_ACCESS | FILE_WRITE_DATA | DELETE,
        0,
        FILE_CREATE,
        FILE_NON_DIRECTORY_FILE | FILE_WRITE_THROUGH,
        true,
    )
    .map_err(|error| stage(error, "create atomic DATA temporary"))?;
    let identity = file_identity(&file)?;
    private_acl(&file)?;
    file.write_all(bytes)
        .map_err(|error| stage(error.into(), "write atomic DATA temporary"))?;
    file.sync_all()
        .map_err(|error| stage(error.into(), "flush atomic DATA temporary"))?;
    if file_identity(&file)? != identity {
        return Err(denied("temporary DATA identity changed"));
    }
    if let Err(error) = rename_handle(&file, parent, value, replace, true) {
        if !replace
            && matches!(&error, StoreError::Io(error) if error.kind() == std::io::ErrorKind::AlreadyExists)
        {
            let info = FILE_DISPOSITION_INFO { DeleteFile: true };
            if unsafe {
                SetFileInformationByHandle(
                    file.as_raw_handle(),
                    FileDispositionInfo,
                    (&info as *const FILE_DISPOSITION_INFO).cast(),
                    size_of::<FILE_DISPOSITION_INFO>() as u32,
                )
            } == 0
            {
                return Err(std::io::Error::last_os_error().into());
            }
            return Ok(false);
        }
        // Keep a private residue if native publication has an unknown outcome;
        // deleting the handle could otherwise delete a successfully moved file.
        return Err(stage(error, "publish atomic DATA temporary"));
    }
    sync_directory(parent).map_err(|error| stage(error, "flush published DATA directory"))?;
    Ok(true)
}
pub(crate) fn rename_no_replace(
    source: &File,
    from: &str,
    destination: &File,
    to: &str,
) -> Result<()> {
    validate_storage_component(from)?;
    private_directory(source)?;
    private_directory(destination)?;
    let file = open_relative(
        source,
        from,
        FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE | DELETE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        FILE_OPEN,
        0,
        false,
    )?;
    let identity = file_identity(&file)?;
    private_acl(&file)?;
    rename_handle(
        &file,
        destination,
        to,
        false,
        file_identity(source)? == file_identity(destination)?,
    )?;
    if file_identity(&file)? != identity {
        return Err(denied("renamed DATA identity changed"));
    }
    sync_directory(source)?;
    sync_directory(destination)
}
pub(crate) fn unlink_file(parent: &File, value: &str) -> Result<()> {
    validate_storage_component(value)?;
    private_directory(parent)?;
    let file = open_relative(
        parent,
        value,
        FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE | DELETE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        FILE_OPEN,
        FILE_NON_DIRECTORY_FILE,
        false,
    )?;
    let _ = checked_information(&file)?;
    private_acl(&file)?;
    let info = FILE_DISPOSITION_INFO { DeleteFile: true };
    if unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileDispositionInfo,
            (&info as *const FILE_DISPOSITION_INFO).cast(),
            size_of::<FILE_DISPOSITION_INFO>() as u32,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    drop(file);
    sync_directory(parent)
}
pub(crate) fn list_names(directory: &File) -> Result<Vec<String>> {
    private_directory(directory)?;
    let mut buffer = vec![0u64; 512];
    let mut names = Vec::new();
    let mut restart = true;
    loop {
        let mut block: IO_STATUS_BLOCK = unsafe { zeroed() };
        let status = unsafe {
            NtQueryDirectoryFile(
                directory.as_raw_handle(),
                null_mut(),
                None,
                null(),
                &mut block,
                buffer.as_mut_ptr().cast(),
                (buffer.len() * 8) as u32,
                FileNamesInformation,
                true,
                null(),
                restart,
            )
        };
        restart = false;
        if status == STATUS_NO_MORE_FILES {
            break;
        }
        nt_result(status, &block)?;
        let used = block.Information;
        if used < 12 || used > buffer.len() * 8 {
            return Err(denied("invalid directory inventory length"));
        }
        let bytes = unsafe { std::slice::from_raw_parts(buffer.as_ptr().cast::<u8>(), used) };
        let length = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
        if length % 2 != 0 || length > used - 12 {
            return Err(denied("invalid directory entry length"));
        }
        let text = bytes[12..12 + length]
            .chunks_exact(2)
            .map(|c| u16::from_le_bytes([c[0], c[1]]))
            .collect::<Vec<_>>();
        let name = String::from_utf16(&text).map_err(|_| denied("non-Unicode Profile name"))?;
        if name == "." || name == ".." {
            continue;
        }
        validate_storage_component(&name)?;
        names.push(name);
    }
    Ok(names)
}

#[cfg(test)]
#[path = "secure_windows_tests.rs"]
mod tests;
