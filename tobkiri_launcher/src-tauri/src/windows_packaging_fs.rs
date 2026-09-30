//! Handle-bound Windows packaging filesystem operations.
//! Ancestor guards allow child creation but deny writes to the guarded directory
//! itself and deny deletion/rename. Leaf read
//! guards deny writes and deletion. Deletion uses a separate, identity-checked
//! DELETE handle, never a pathname retry.
use std::os::windows::{
    ffi::{OsStrExt, OsStringExt},
    fs::{MetadataExt, OpenOptionsExt},
    io::{AsRawHandle, FromRawHandle, OwnedHandle},
};
use std::{
    ffi::{OsStr, OsString},
    fs::{self, File, OpenOptions},
    io,
    mem::{size_of, zeroed},
    path::{Component, Path, PathBuf},
    ptr::{null, null_mut},
};
use windows_sys::Win32::{
    Foundation::LocalFree,
    Security::{Authorization::*, *},
    Storage::FileSystem::*,
    System::{
        SystemServices::{ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE},
        Threading::{GetCurrentProcess, OpenProcessToken},
    },
};
macro_rules! invalid { ($($arg:tt)*) => { return Err(io::Error::new(io::ErrorKind::InvalidData, format!($($arg)*))) }; }
trait ErrorContext<T> {
    fn context(self, label: &str) -> io::Result<T>;
}
impl<T> ErrorContext<T> for io::Result<T> {
    fn context(self, label: &str) -> io::Result<T> {
        self.map_err(|e| io::Error::new(e.kind(), format!("{label}: {e}")))
    }
}
const WRITE_ACCESS: u32 = FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_WRITE_ATTRIBUTES
    | FILE_DELETE_CHILD
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
    | 0x4000_0000 // GENERIC_WRITE
    | 0x1000_0000; // GENERIC_ALL

struct LocalAllocation(*mut std::ffi::c_void);
impl Drop for LocalAllocation {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { LocalFree(self.0) };
        }
    }
}

fn wide(value: &OsStr) -> io::Result<Vec<u16>> {
    let mut value = value.encode_wide().collect::<Vec<_>>();
    if value.contains(&0) {
        invalid!("[PYTHON_SEALED_INVALID] NUL in Windows path");
    }
    value.push(0);
    Ok(value)
}

fn sid_string(sid: PSID) -> io::Result<String> {
    let mut output = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut output) } == 0 {
        return Err(io::Error::last_os_error()).context("read Windows security identity");
    }
    let _allocation = LocalAllocation(output.cast());
    let mut len = 0;
    unsafe {
        while *output.add(len) != 0 {
            len += 1;
        }
        String::from_utf16(std::slice::from_raw_parts(output, len))
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }
}

pub fn current_user_sid() -> io::Result<String> {
    let mut token = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(io::Error::last_os_error()).context("open Launcher security token");
    }
    let token = unsafe { OwnedHandle::from_raw_handle(token) };
    let mut needed = 0;
    unsafe { GetTokenInformation(token.as_raw_handle(), TokenUser, null_mut(), 0, &mut needed) };
    if needed == 0 {
        return Err(io::Error::last_os_error()).context("size Launcher security token");
    }
    // Keep TOKEN_USER and its inline SID naturally aligned.
    let mut buffer = vec![0usize; (needed as usize).div_ceil(size_of::<usize>())];
    if unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            TokenUser,
            buffer.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    } == 0
    {
        return Err(io::Error::last_os_error()).context("read Launcher security token");
    }
    sid_string(unsafe { (*(buffer.as_ptr().cast::<TOKEN_USER>())).User.Sid })
}

fn security_descriptor(sid: &str, sealed: bool) -> io::Result<LocalAllocation> {
    let rights = if sealed { "FRFX" } else { "FA" };
    let sddl = wide(OsStr::new(&format!(
        "O:{sid}D:P(A;OICI;{rights};;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    )))?;
    let mut descriptor = null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error()).context("build private snapshot DACL");
    }
    Ok(LocalAllocation(descriptor))
}

pub fn create_private_directory(path: &Path, sid: &str) -> io::Result<()> {
    let descriptor = security_descriptor(sid, false)?;
    let attributes = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.0,
        bInheritHandle: 0,
    };
    let path = wide(path.as_os_str())?;
    if unsafe { CreateDirectoryW(path.as_ptr(), &attributes) } == 0 {
        return Err(io::Error::last_os_error())
            .context("create private Windows snapshot directory");
    }
    Ok(())
}

/// Atomically create a protected directory relative to a pinned parent and
/// return its creation handle. Unlike CreateDirectoryW followed by reopening,
/// this has no name-replacement window even under a shared writable TEMP.
/// The parent is a namespace anchor, not an ACL authority; only the newly
/// created object's protected current-user DACL is trusted.
pub fn create_private_directory_pinned(path: &Path, sid: &str) -> io::Result<PinnedPath> {
    use windows_sys::Wdk::{
        Foundation::OBJECT_ATTRIBUTES,
        Storage::FileSystem::{
            NtCreateFile, FILE_CREATE, FILE_DIRECTORY_FILE, FILE_OPEN_REPARSE_POINT,
            FILE_SYNCHRONOUS_IO_NONALERT,
        },
    };
    use windows_sys::Win32::{
        Foundation::{RtlNtStatusToDosError, OBJ_CASE_INSENSITIVE, UNICODE_STRING},
        System::IO::IO_STATUS_BLOCK,
    };
    validate_absolute(path)?;
    let parent_path = path.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "private directory has no parent",
        )
    })?;
    let name = path.file_name().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "private directory has no name")
    })?;
    validate_component(name)?;
    let parent = open_pinned(parent_path, true)?;
    let mut encoded = name.encode_wide().collect::<Vec<_>>();
    let bytes = encoded
        .len()
        .checked_mul(2)
        .and_then(|n| u16::try_from(n).ok())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "private directory name too long",
            )
        })?;
    let name = UNICODE_STRING {
        Length: bytes,
        MaximumLength: bytes,
        Buffer: encoded.as_mut_ptr(),
    };
    let descriptor = security_descriptor(sid, false)?;
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.file.as_raw_handle(),
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE,
        SecurityDescriptor: descriptor.0.cast(),
        SecurityQualityOfService: null(),
    };
    let mut status_block: IO_STATUS_BLOCK = unsafe { zeroed() };
    let mut raw = null_mut();
    let status = unsafe {
        NtCreateFile(
            &mut raw,
            FILE_GENERIC_READ | WRITE_DAC,
            &attributes,
            &mut status_block,
            null(),
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ,
            FILE_CREATE,
            FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT | FILE_OPEN_REPARSE_POINT,
            null(),
            0,
        )
    };
    if status < 0 {
        return Err(io::Error::from_raw_os_error(
            unsafe { RtlNtStatusToDosError(status) } as i32,
        ));
    }
    if raw.is_null() || raw == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
        invalid!("private directory creation returned no handle; residue retained");
    }
    let file = unsafe { File::from_raw_handle(raw) };
    // FILE_CREATED is the native create-information result (WinNT value 2),
    // distinct from the FILE_CREATE disposition requested above.
    const FILE_CREATED_INFORMATION: usize = 2;
    if status != 0 || status_block.Information != FILE_CREATED_INFORMATION {
        invalid!("private directory creation did not confirm a new object; residue retained");
    }
    if !identity(&file)?.directory {
        invalid!("private directory creation returned wrong object type");
    }
    verify_acl(&file, sid, false, true)?;
    let mut ancestors = parent._ancestors;
    ancestors.push(parent.file);
    Ok(PinnedPath {
        file,
        path: path.to_owned(),
        _ancestors: ancestors,
    })
}

pub fn create_private_file(path: &Path, sid: &str) -> io::Result<File> {
    let descriptor = security_descriptor(sid, false)?;
    let attributes = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.0,
        bInheritHandle: 0,
    };
    let path = wide(path.as_os_str())?;
    let handle = unsafe {
        CreateFileW(
            path.as_ptr(),
            FILE_GENERIC_WRITE,
            0,
            &attributes,
            CREATE_NEW,
            FILE_FLAG_OPEN_REPARSE_POINT,
            null_mut(),
        )
    };
    if handle == windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE {
        return Err(io::Error::last_os_error()).context("create private Windows snapshot file");
    }
    Ok(unsafe { File::from_raw_handle(handle) })
}

pub fn set_private_acl(file: &File, sid: &str, sealed: bool) -> io::Result<()> {
    let descriptor = security_descriptor(sid, sealed)?;
    let mut dacl = null_mut();
    let mut present = 0;
    let mut defaulted = 0;
    if unsafe { GetSecurityDescriptorDacl(descriptor.0, &mut present, &mut dacl, &mut defaulted) }
        == 0
        || present == 0
        || dacl.is_null()
    {
        invalid!("[PYTHON_SEALED_SNAPSHOT_INVALID] private DACL is unavailable");
    }
    let status = unsafe {
        SetSecurityInfo(
            file.as_raw_handle(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            null_mut(),
            null_mut(),
            dacl,
            null(),
        )
    };
    if status != 0 {
        return Err(io::Error::from_raw_os_error(status as i32))
            .context("seal Windows snapshot DACL");
    }
    verify_acl(file, sid, sealed, true).map_err(|error| {
        io::Error::new(error.kind(), format!("after applying private ACL: {error}"))
    })
}

/// Inspect the actual handle's owner and ACL, never a pathname's descriptor.
pub fn verify_acl(file: &File, sid: &str, sealed: bool, private: bool) -> io::Result<()> {
    let mut owner = null_mut();
    let mut dacl = null_mut();
    let mut descriptor = null_mut();
    let status = unsafe {
        GetSecurityInfo(
            file.as_raw_handle(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut dacl,
            null_mut(),
            &mut descriptor,
        )
    };
    let _allocation = LocalAllocation(descriptor);
    if status != 0 || owner.is_null() || dacl.is_null() {
        invalid!("[PYTHON_SEALED_SNAPSHOT_INVALID] missing Windows owner or DACL ({status})");
    }
    let owner = sid_string(owner)?;
    if owner != sid && (private || !matches!(owner.as_str(), "S-1-5-18" | "S-1-5-32-544")) {
        invalid!("[PYTHON_SEALED_SNAPSHOT_INVALID] Windows path has an untrusted owner");
    }
    if private {
        let mut control = 0;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0
            || control & SE_DACL_PROTECTED == 0
        {
            invalid!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot DACL inherits external authority");
        }
    }
    let count = unsafe { (*dacl).AceCount };
    for index in 0..u32::from(count) {
        let mut ace = null_mut();
        if unsafe { GetAce(dacl, index, &mut ace) } == 0 || ace.is_null() {
            invalid!("[PYTHON_SEALED_SNAPSHOT_INVALID] malformed Windows ACL");
        }
        let header = unsafe { &*(ace.cast::<ACE_HEADER>()) };
        if header.AceFlags & INHERIT_ONLY_ACE as u8 != 0 {
            continue;
        }
        // A deny ACE can only restrict access. Complex/callback grants are not
        // accepted because they cannot establish this small trust boundary.
        if header.AceType == ACCESS_DENIED_ACE_TYPE as u8 {
            continue;
        }
        if header.AceType != ACCESS_ALLOWED_ACE_TYPE as u8
            || usize::from(header.AceSize) < size_of::<ACCESS_ALLOWED_ACE>()
        {
            invalid!("[PYTHON_SEALED_SNAPSHOT_INVALID] unsupported Windows access grant");
        }
        let allow = unsafe { &*(ace.cast::<ACCESS_ALLOWED_ACE>()) };
        let grantee = sid_string((&allow.SidStart as *const u32).cast_mut().cast())?;
        let privileged = matches!(grantee.as_str(), "S-1-5-18" | "S-1-5-32-544");
        let user = grantee == sid;
        if (private && !privileged && !user)
            || (!privileged && (!user || sealed) && allow.Mask & WRITE_ACCESS != 0)
        {
            let grantee_class = if user {
                "current-user"
            } else if privileged {
                "system-or-administrators"
            } else {
                "other"
            };
            invalid!("[PYTHON_SEALED_SNAPSHOT_INVALID] Windows ACL permits unauthorized writes (sealed={sealed}, private={private}, ace={index}, mask={:#010x}, flags={:#04x}, grantee={grantee_class})", allow.Mask, header.AceFlags);
        }
    }
    Ok(())
}

/// Stable local-volume identity obtained from the open object, not its path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Identity {
    pub volume: u64,
    pub file: u64,
    pub directory: bool,
}

pub fn identity(file: &File) -> io::Result<Identity> {
    let mut info: BY_HANDLE_FILE_INFORMATION = unsafe { zeroed() };
    if unsafe { GetFileInformationByHandle(file.as_raw_handle(), &mut info) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let directory = info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || (!directory && info.nNumberOfLinks != 1)
    {
        invalid!("packaging object is a reparse point or hardlinked file");
    }
    Ok(Identity {
        volume: u64::from(info.dwVolumeSerialNumber),
        file: (u64::from(info.nFileIndexHigh) << 32) | u64::from(info.nFileIndexLow),
        directory,
    })
}

fn open_leaf(path: &Path, directory: bool, mutation: bool) -> io::Result<File> {
    let access = FILE_GENERIC_READ
        | if mutation {
            DELETE | FILE_WRITE_ATTRIBUTES
        } else {
            0
        };
    let file = OpenOptions::new()
        .access_mode(access)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(
            FILE_FLAG_OPEN_REPARSE_POINT
                | if directory {
                    FILE_FLAG_BACKUP_SEMANTICS
                } else {
                    0
                },
        )
        .open(path)?;
    if identity(&file)?.directory != directory {
        invalid!("packaging object type changed");
    }
    Ok(file)
}

/// All ancestors remain held without delete sharing for the entire operation.
#[derive(Debug)]
pub struct PinnedPath {
    pub file: File,
    pub path: PathBuf,
    _ancestors: Vec<File>,
}

pub fn open_pinned(path: &Path, directory: bool) -> io::Result<PinnedPath> {
    validate_absolute(path)?;
    let mut ancestors = path.ancestors().skip(1).collect::<Vec<_>>();
    ancestors.reverse();
    let mut held = Vec::new();
    for ancestor in ancestors {
        held.push(open_leaf(ancestor, true, false)?);
    }
    let file = open_leaf(path, directory, false)?;
    Ok(PinnedPath {
        file,
        path: path.to_owned(),
        _ancestors: held,
    })
}

fn validate_absolute(path: &Path) -> io::Result<()> {
    if !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, Component::ParentDir | Component::CurDir))
    {
        invalid!("packaging path must be absolute and normalized");
    }
    if !matches!(path.components().next(), Some(Component::Prefix(p)) if matches!(p.kind(), std::path::Prefix::Disk(_) | std::path::Prefix::VerbatimDisk(_)))
    {
        invalid!("packaging path must use a local disk");
    }
    for component in path.components() {
        if let Component::Normal(name) = component {
            validate_component(name)?;
        }
    }
    Ok(())
}

pub fn validate_component(name: &OsStr) -> io::Result<()> {
    let text = name
        .to_str()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "non-Unicode packaging name"))?;
    let stem = text
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_uppercase();
    if text.is_empty()
        || text == "."
        || text == ".."
        || text.contains(['/', '\\', ':', '<', '>', '"', '|', '?', '*'])
        || text.ends_with(['.', ' '])
        || text.chars().any(char::is_control)
        || matches!(
            stem.as_str(),
            "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
        )
        || ((stem.starts_with("COM") || stem.starts_with("LPT"))
            && stem.chars().count() == 4
            && matches!(stem.chars().nth(3), Some('1'..='9' | '¹' | '²' | '³')))
    {
        invalid!("unsafe Windows packaging component");
    }
    Ok(())
}

pub type Inventory = std::collections::BTreeMap<String, (u64, u64, bool)>;

pub fn inventory(root: &Path) -> io::Result<Inventory> {
    fn visit(
        path: &Path,
        relative: &str,
        volume: u64,
        output: &mut Inventory,
        sid: &str,
    ) -> io::Result<()> {
        let held = open_pinned(path, true)?;
        verify_acl(&held.file, sid, false, false).map_err(|error| {
            io::Error::new(
                error.kind(),
                format!("inventory directory {relative:?}: {error}"),
            )
        })?;
        let id = identity(&held.file)?;
        if id.volume != volume {
            invalid!("packaging tree crosses volumes");
        }
        output.insert(relative.to_owned(), (id.volume, id.file, true));
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            validate_component(&entry.file_name())?;
            let name = entry.file_name().into_string().map_err(|_| {
                io::Error::new(io::ErrorKind::InvalidData, "non-Unicode packaging name")
            })?;
            let rel = if relative.is_empty() {
                name
            } else {
                format!("{relative}/{name}")
            };
            if entry.file_type()?.is_dir() {
                visit(&entry.path(), &rel, volume, output, sid)?;
            } else {
                let file = open_pinned(&entry.path(), false)?;
                verify_acl(&file.file, sid, false, false).map_err(|error| {
                    io::Error::new(error.kind(), format!("inventory file {rel:?}: {error}"))
                })?;
                let id = identity(&file.file)?;
                if id.volume != volume {
                    invalid!("packaging tree crosses volumes");
                }
                output.insert(rel, (id.volume, id.file, false));
            }
        }
        Ok(())
    }
    let root_pin = open_pinned(root, true)?;
    let mut output = Inventory::new();
    visit(
        root,
        "",
        identity(&root_pin.file)?.volume,
        &mut output,
        &current_user_sid()?,
    )?;
    Ok(output)
}

/// Preserve an unauthenticated generated directory without traversing, deleting,
/// or changing permissions. The target must be a fresh sibling; replacement is
/// forbidden. Identity is rechecked on the exclusive mutation handle.
pub fn preserve_directory_by_identity(
    source: &Path,
    destination: &Path,
    expected: Identity,
) -> io::Result<()> {
    if source.parent() != destination.parent() || !source.is_absolute() {
        invalid!("preserved stage must be an absolute sibling");
    }
    validate_component(
        destination
            .file_name()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing preserved name"))?,
    )?;
    let _parent = open_pinned(source.parent().unwrap(), true)?;
    let file = open_leaf(source, true, true)?;
    if identity(&file)? != expected {
        invalid!("legacy stage changed before preservation");
    }
    let name = wide(destination.as_os_str())?;
    let name_bytes = (name.len() - 1) * 2;
    let offset = std::mem::offset_of!(FILE_RENAME_INFO, FileName);
    // Word allocation preserves FILE_RENAME_INFO alignment.
    let bytes = offset + name_bytes;
    let mut buffer = vec![0usize; (bytes + size_of::<usize>() - 1) / size_of::<usize>()];
    let info = buffer.as_mut_ptr().cast::<FILE_RENAME_INFO>();
    unsafe {
        (*info).Anonymous.ReplaceIfExists = false;
        (*info).RootDirectory = null_mut();
        (*info).FileNameLength = name_bytes as u32;
        std::ptr::copy_nonoverlapping(name.as_ptr(), (*info).FileName.as_mut_ptr(), name.len() - 1);
        if SetFileInformationByHandle(
            file.as_raw_handle(),
            FileRenameInfo,
            info.cast(),
            bytes as u32,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    if identity(&file)? != expected {
        invalid!("preserved stage identity changed");
    }
    Ok(())
}

/// Remove only the inventoried objects. Each mutation targets an opened handle;
/// a replacement, extra, ACL change, or reparse point leaves residue fail-closed.
pub fn remove_owned_tree(root: &Path, expected: &Inventory) -> io::Result<()> {
    let parent = open_pinned(
        root.parent()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing parent"))?,
        true,
    )?;
    if inventory(root)? != *expected {
        invalid!("packaging cleanup inventory changed; residue retained");
    }
    fn remove(path: &Path, relative: &str, expected: &Inventory, sid: &str) -> io::Result<()> {
        let wanted = expected
            .get(relative)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "unowned packaging entry"))?;
        let file = open_leaf(path, wanted.2, true)?;
        let id = identity(&file)?;
        if (id.volume, id.file, id.directory) != *wanted {
            invalid!("packaging cleanup identity changed; residue retained");
        }
        verify_acl(&file, sid, false, false)?;
        if id.directory {
            for entry in fs::read_dir(path)? {
                let entry = entry?;
                validate_component(&entry.file_name())?;
                let name = entry.file_name().into_string().map_err(|_| {
                    io::Error::new(io::ErrorKind::InvalidData, "non-Unicode packaging name")
                })?;
                let child = if relative.is_empty() {
                    name
                } else {
                    format!("{relative}/{name}")
                };
                remove(&entry.path(), &child, expected, sid)?;
            }
        }
        let mut basic: FILE_BASIC_INFO = unsafe { zeroed() };
        if unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle(),
                FileBasicInfo,
                (&mut basic as *mut FILE_BASIC_INFO).cast(),
                size_of::<FILE_BASIC_INFO>() as u32,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        if basic.FileAttributes & FILE_ATTRIBUTE_READONLY != 0 {
            basic.FileAttributes &= !FILE_ATTRIBUTE_READONLY;
            if basic.FileAttributes == 0 {
                basic.FileAttributes = FILE_ATTRIBUTE_NORMAL;
            }
            if unsafe {
                SetFileInformationByHandle(
                    file.as_raw_handle(),
                    FileBasicInfo,
                    (&basic as *const FILE_BASIC_INFO).cast(),
                    size_of::<FILE_BASIC_INFO>() as u32,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
        }
        let disposition = FILE_DISPOSITION_INFO { DeleteFile: true };
        if unsafe {
            SetFileInformationByHandle(
                file.as_raw_handle(),
                FileDispositionInfo,
                (&disposition as *const FILE_DISPOSITION_INFO).cast(),
                size_of::<FILE_DISPOSITION_INFO>() as u32,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
    let result = remove(root, "", expected, &current_user_sid()?);
    drop(parent);
    result
}

pub fn path_from_handle(file: &File) -> io::Result<PathBuf> {
    let needed = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            null_mut(),
            0,
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
        )
    };
    if needed == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut buffer = vec![0u16; needed as usize + 1];
    let length = unsafe {
        GetFinalPathNameByHandleW(
            file.as_raw_handle(),
            buffer.as_mut_ptr(),
            buffer.len() as u32,
            FILE_NAME_NORMALIZED | VOLUME_NAME_DOS,
        )
    };
    if length == 0 || length as usize >= buffer.len() {
        return Err(io::Error::last_os_error());
    }
    Ok(PathBuf::from(OsString::from_wide(
        &buffer[..length as usize],
    )))
}

pub fn open_child(parent: &File, name: &OsStr, directory: bool) -> io::Result<File> {
    validate_component(name)?;
    open_leaf(&path_from_handle(parent)?.join(name), directory, false)
}

/// Owner-control guard for a newly created private object. WRITE_DAC is acquired
/// before sealing, allowing explicit rollback without path-based ACL mutation.
pub fn open_private_control(path: &Path, directory: bool) -> io::Result<PinnedPath> {
    let mut pinned = open_pinned(path, directory)?;
    let file = OpenOptions::new()
        .access_mode(FILE_GENERIC_READ | WRITE_DAC)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(
            FILE_FLAG_OPEN_REPARSE_POINT
                | if directory {
                    FILE_FLAG_BACKUP_SEMANTICS
                } else {
                    0
                },
        )
        .open(path)?;
    if identity(&file)? != identity(&pinned.file)? {
        invalid!("private control identity changed");
    }
    pinned.file = file;
    Ok(pinned)
}

/// Copy a tree through held, non-reparse source handles into private new files.
pub fn copy_private_tree(source: &Path, destination: &Path) -> io::Result<()> {
    let source_pin = open_pinned(source, true)?;
    let parent = open_pinned(
        destination.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "missing destination parent")
        })?,
        true,
    )?;
    let sid = current_user_sid()?;
    verify_acl(&parent.file, &sid, false, false)?;
    create_private_directory(destination, &sid)?;
    let target = open_pinned(destination, true)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        validate_component(&entry.file_name())?;
        let dst = destination.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_private_tree(&entry.path(), &dst)?;
        } else {
            let input = open_pinned(&entry.path(), false)?;
            let mut output = create_private_file(&dst, &sid)?;
            io::copy(&mut &input.file, &mut output)?;
            output.sync_all()?;
        }
    }
    drop(target);
    drop(parent);
    drop(source_pin);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn tree(label: &str) -> PathBuf {
        let sid = current_user_sid().unwrap();
        let path = std::env::temp_dir().join(format!(
            "tobkiri-winfs-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        create_private_directory(&path, &sid).unwrap();
        path
    }
    #[test]
    fn windows_pinned_parent_allows_creation_but_blocks_rename() {
        let root = tree("parent");
        let pin = open_pinned(&root, true).unwrap();
        fs::create_dir(root.join("child")).unwrap();
        assert!(fs::rename(&root, root.with_extension("moved")).is_err());
        drop(pin);
        let inv = inventory(&root).unwrap();
        remove_owned_tree(&root, &inv).unwrap();
        assert!(!root.exists());
    }
    #[test]
    fn windows_read_guard_blocks_writes_and_cleanup_removes_readonly_files() {
        let root = tree("read");
        let path = root.join("a");
        fs::write(&path, b"trusted").unwrap();
        let pin = open_pinned(&path, false).unwrap();
        assert!(fs::write(&path, b"changed").is_err());
        assert!(fs::remove_file(&path).is_err());
        drop(pin);
        let mut permissions = fs::metadata(&path).unwrap().permissions();
        permissions.set_readonly(true);
        fs::set_permissions(&path, permissions).unwrap();
        let inv = inventory(&root).unwrap();
        remove_owned_tree(&root, &inv).unwrap();
        assert!(!root.exists());
    }
    #[test]
    fn windows_cleanup_refuses_same_size_replacement_and_extras() {
        let root = tree("replace");
        let path = root.join("a");
        fs::write(&path, b"one").unwrap();
        let inv = inventory(&root).unwrap();
        fs::rename(&path, root.join("original")).unwrap();
        fs::write(&path, b"two").unwrap();
        assert!(remove_owned_tree(&root, &inv).is_err());
        assert_eq!(fs::read(&path).unwrap(), b"two");
        remove_owned_tree(&root, &inventory(&root).unwrap()).unwrap();
    }
    #[test]
    fn windows_inventory_rejects_hardlinks_and_unsafe_names() {
        for name in ["x:stream", "CON", "NUL.txt", "a.", "a ", "COM1.txt", "../x"] {
            assert!(validate_component(OsStr::new(name)).is_err(), "{name}");
        }
        let root = tree("hardlink");
        fs::write(root.join("a"), b"safe").unwrap();
        fs::hard_link(root.join("a"), root.join("b")).unwrap();
        assert!(inventory(&root).is_err());
        fs::remove_file(root.join("b")).unwrap();
        remove_owned_tree(&root, &inventory(&root).unwrap()).unwrap();
    }
    #[test]
    fn windows_sealed_acl_rejects_write_until_explicit_unseal() {
        let root = tree("acl");
        let sid = current_user_sid().unwrap();
        let pin = open_private_control(&root, true).unwrap();
        set_private_acl(&pin.file, &sid, true).unwrap();
        assert!(fs::write(root.join("blocked"), b"no").is_err());
        verify_acl(&pin.file, &sid, true, true).unwrap();
        set_private_acl(&pin.file, &sid, false).unwrap();
        fs::write(root.join("allowed"), b"yes").unwrap();
        drop(pin);
        remove_owned_tree(&root, &inventory(&root).unwrap()).unwrap();
    }
    #[test]
    fn windows_legacy_preservation_rejects_identity_change_and_existing_target() {
        let root = tree("preserve");
        fs::create_dir(root.join("legacy")).unwrap();
        fs::write(root.join("legacy/sentinel"), b"unchanged").unwrap();
        fs::create_dir(root.join("occupied")).unwrap();
        let pin = open_pinned(&root.join("legacy"), true).unwrap();
        let id = identity(&pin.file).unwrap();
        drop(pin);
        let mut wrong = id;
        wrong.file ^= 1;
        assert!(
            preserve_directory_by_identity(&root.join("legacy"), &root.join("fresh"), wrong)
                .is_err()
        );
        assert!(
            preserve_directory_by_identity(&root.join("legacy"), &root.join("occupied"), id)
                .is_err()
        );
        assert_eq!(
            fs::read(root.join("legacy/sentinel")).unwrap(),
            b"unchanged"
        );
        preserve_directory_by_identity(&root.join("legacy"), &root.join("fresh"), id).unwrap();
        assert_eq!(fs::read(root.join("fresh/sentinel")).unwrap(), b"unchanged");
        remove_owned_tree(&root, &inventory(&root).unwrap()).unwrap();
    }
    #[test]
    fn windows_atomic_private_child_accepts_shared_parent_without_changing_acl() {
        let sid = current_user_sid().unwrap();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-atomic-shared-parent-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        // The broad ACL is applied only through our atomic creation handle.
        // FILE_CREATE prevents touching any pre-existing path, even on collision.
        let parent = create_private_directory_pinned(&root, &sid).unwrap();
        let sddl = wide(OsStr::new(&format!(
            "O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;WD)"
        )))
        .unwrap();
        let mut raw = null_mut();
        assert_ne!(
            unsafe {
                ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    sddl.as_ptr(),
                    SDDL_REVISION_1,
                    &mut raw,
                    null_mut(),
                )
            },
            0
        );
        let descriptor = LocalAllocation(raw);
        let mut dacl = null_mut();
        let mut present = 0;
        let mut defaulted = 0;
        assert_ne!(
            unsafe {
                GetSecurityDescriptorDacl(descriptor.0, &mut present, &mut dacl, &mut defaulted)
            },
            0
        );
        assert_eq!(
            unsafe {
                SetSecurityInfo(
                    parent.file.as_raw_handle(),
                    SE_FILE_OBJECT,
                    DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                    null_mut(),
                    null_mut(),
                    dacl,
                    null(),
                )
            },
            0
        );
        assert!(
            verify_acl(&parent.file, &sid, false, false).is_err(),
            "fixture must actually have a broad parent grant"
        );
        fn acl_bytes(file: &File) -> Vec<u8> {
            let mut dacl = null_mut();
            let mut descriptor = null_mut();
            assert_eq!(
                unsafe {
                    GetSecurityInfo(
                        file.as_raw_handle(),
                        SE_FILE_OBJECT,
                        DACL_SECURITY_INFORMATION,
                        null_mut(),
                        null_mut(),
                        &mut dacl,
                        null_mut(),
                        &mut descriptor,
                    )
                },
                0
            );
            let _allocation = LocalAllocation(descriptor);
            unsafe {
                std::slice::from_raw_parts(dacl.cast::<u8>(), (*dacl).AclSize as usize).to_vec()
            }
        }
        let before = acl_bytes(&parent.file);
        let path = root.join("private-child");
        let child = create_private_directory_pinned(&path, &sid).unwrap();
        verify_acl(&child.file, &sid, false, true).unwrap();
        assert!(
            before == acl_bytes(&parent.file),
            "private child creation changed the shared parent ACL"
        );
        assert!(fs::rename(&path, root.join("moved")).is_err());
        assert!(fs::remove_dir(&path).is_err());
        assert_eq!(
            create_private_directory_pinned(&path, &sid)
                .unwrap_err()
                .kind(),
            io::ErrorKind::AlreadyExists
        );
        let reopened = open_pinned(&path, true).unwrap();
        assert_eq!(
            identity(&child.file).unwrap(),
            identity(&reopened.file).unwrap()
        );
        drop(reopened);
        fs::write(path.join("payload"), b"owned").unwrap();
        drop(child);
        set_private_acl(&parent.file, &sid, false).unwrap();
        drop(parent);
        remove_owned_tree(&root, &inventory(&root).unwrap()).unwrap();
    }
}
