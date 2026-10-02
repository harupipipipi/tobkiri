//! Windows package authenticity and private, held-handle execution snapshots.
//!
//! The signed Launcher pins all runtime manifests and the release signer's DER
//! certificate hash. WinVerifyTrust authenticates that very executable through
//! a no-write/no-delete handle. Every snapshot component is held without write
//! or delete sharing, and its protected DACL permits only the current user,
//! SYSTEM and Administrators. Sealing removes the user's write rights. As on
//! POSIX, an administrator or intentionally malicious same-user process that
//! changes an owner DACL is outside the threat model.

use super::*;
use std::collections::BTreeSet;
use std::io::Write;
use std::mem::{size_of, zeroed};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::ptr::{null, null_mut};
use windows_sys::Win32::Foundation::{LocalFree, ERROR_LOCK_VIOLATION};
use windows_sys::Win32::Security::Authorization::*;
use windows_sys::Win32::Security::WinTrust::*;
use windows_sys::Win32::Security::*;
use windows_sys::Win32::Storage::FileSystem::*;
use windows_sys::Win32::System::SystemInformation::GetWindowsDirectoryW;
use windows_sys::Win32::System::SystemServices::{ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};
use windows_sys::Win32::System::IO::OVERLAPPED;

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

fn wide(value: &OsStr) -> Result<Vec<u16>> {
    let mut value = value.encode_wide().collect::<Vec<_>>();
    if value.contains(&0) {
        bail!("[PYTHON_SEALED_INVALID] NUL in Windows path");
    }
    value.push(0);
    Ok(value)
}

fn sid_string(sid: PSID) -> Result<String> {
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
        Ok(String::from_utf16(std::slice::from_raw_parts(output, len))?)
    }
}

fn current_user_sid() -> Result<String> {
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

fn security_descriptor(sid: &str, sealed: bool) -> Result<LocalAllocation> {
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

fn create_private_directory(path: &Path, sid: &str) -> Result<()> {
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

fn create_private_file(path: &Path, sid: &str) -> Result<File> {
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

fn set_private_acl(file: &File, sid: &str, sealed: bool) -> Result<()> {
    let descriptor = security_descriptor(sid, sealed)?;
    let mut dacl = null_mut();
    let mut present = 0;
    let mut defaulted = 0;
    if unsafe { GetSecurityDescriptorDacl(descriptor.0, &mut present, &mut dacl, &mut defaulted) }
        == 0
        || present == 0
        || dacl.is_null()
    {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] private DACL is unavailable");
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
    verify_acl(file, sid, sealed, true)
}

/// Inspect the actual handle's owner and ACL, never a pathname's descriptor.
fn verify_acl(file: &File, sid: &str, sealed: bool, private: bool) -> Result<()> {
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
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] missing Windows owner or DACL ({status})");
    }
    let owner = sid_string(owner)?;
    if owner != sid && (private || !matches!(owner.as_str(), "S-1-5-18" | "S-1-5-32-544")) {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] Windows path has an untrusted owner");
    }
    if private {
        let mut control = 0;
        let mut revision = 0;
        if unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision) } == 0
            || control & SE_DACL_PROTECTED == 0
        {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot DACL inherits external authority");
        }
    }
    let count = unsafe { (*dacl).AceCount };
    for index in 0..u32::from(count) {
        let mut ace = null_mut();
        if unsafe { GetAce(dacl, index, &mut ace) } == 0 || ace.is_null() {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] malformed Windows ACL");
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
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] unsupported Windows access grant");
        }
        let allow = unsafe { &*(ace.cast::<ACCESS_ALLOWED_ACE>()) };
        let grantee = sid_string((&allow.SidStart as *const u32).cast_mut().cast())?;
        let privileged = matches!(grantee.as_str(), "S-1-5-18" | "S-1-5-32-544");
        let user = grantee == sid;
        if (private && !privileged && !user)
            || (!privileged && (!user || sealed) && allow.Mask & WRITE_ACCESS != 0)
        {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] Windows ACL permits unauthorized writes");
        }
    }
    Ok(())
}

fn open_held(
    path: &Path,
    directory: bool,
    owner_control: bool,
    allow_delete: bool,
) -> Result<File> {
    let access = FILE_GENERIC_READ | if owner_control { WRITE_DAC } else { 0 };
    let file = OpenOptions::new()
        .access_mode(access)
        .share_mode(FILE_SHARE_READ | if allow_delete { FILE_SHARE_DELETE } else { 0 })
        .custom_flags(
            FILE_FLAG_OPEN_REPARSE_POINT
                | if directory {
                    FILE_FLAG_BACKUP_SEMANTICS
                } else {
                    0
                },
        )
        .open(path)?;
    let metadata = file.metadata()?;
    if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
        || metadata.is_dir() != directory
        || (!directory
            && (!metadata.is_file() || windows_file_identity(&file)?.number_of_links != 1))
    {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] linked or special Windows path");
    }
    Ok(file)
}

/// Lock each ancestor before using a full Win32 path. Holding without delete
/// sharing prevents renaming a component between subsequent opens. The final
/// no-follow open rejects junctions and every other reparse tag, not just links.
fn hold_ancestors(path: &Path, held: &mut BTreeMap<PathBuf, File>) -> Result<()> {
    if !path.is_absolute() {
        bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] Windows path must be absolute");
    }
    match path.components().next() {
        Some(Component::Prefix(prefix))
            if matches!(
                prefix.kind(),
                std::path::Prefix::Disk(_) | std::path::Prefix::VerbatimDisk(_)
            ) => {}
        _ => bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] Windows snapshot requires a local disk"),
    }
    let mut ancestors = path.ancestors().collect::<Vec<_>>();
    ancestors.reverse();
    for directory in ancestors {
        if !held.contains_key(directory) {
            held.insert(
                directory.to_path_buf(),
                open_held(directory, true, false, false)?,
            );
        }
    }
    Ok(())
}

fn verify_windows_relative_path(value: &str) -> Result<()> {
    validate_relative_path(value)?;
    if value.is_empty() || value.contains('\0') {
        bail!("[PYTHON_SEALED_INVALID] empty Windows inventory path");
    }
    for part in value.split('/') {
        let stem = part
            .split('.')
            .next()
            .unwrap_or_default()
            .to_ascii_uppercase();
        if part.is_empty()
            || part.contains([':', '<', '>', '"', '|', '?', '*'])
            || part.ends_with(['.', ' '])
            || part.chars().any(|c| c.is_control())
            || matches!(
                stem.as_str(),
                "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
            )
            || ((stem.starts_with("COM") || stem.starts_with("LPT"))
                && stem.len() == 4
                && matches!(stem.as_bytes()[3], b'1'..=b'9'))
        {
            bail!("[PYTHON_SEALED_INVALID] aliased or unsafe Windows inventory path");
        }
    }
    Ok(())
}

fn verify_authenticode(path: &Path, file: &File, expected_signer: &str) -> Result<()> {
    require_sha256(expected_signer)
        .context("[PYTHON_SEALED_PROVENANCE_UNAVAILABLE] build has no Windows signer identity")?;
    let path_wide = wide(path.as_os_str())?;
    let mut info = WINTRUST_FILE_INFO {
        cbStruct: size_of::<WINTRUST_FILE_INFO>() as u32,
        pcwszFilePath: path_wide.as_ptr(),
        hFile: file.as_raw_handle(),
        ..Default::default()
    };
    let mut trust = WINTRUST_DATA {
        cbStruct: size_of::<WINTRUST_DATA>() as u32,
        dwUIChoice: WTD_UI_NONE,
        fdwRevocationChecks: WTD_REVOKE_NONE,
        dwUnionChoice: WTD_CHOICE_FILE,
        Anonymous: WINTRUST_DATA_0 { pFile: &mut info },
        dwStateAction: WTD_STATEACTION_VERIFY,
        // Launch remains local-first. This authenticates the complete PE and
        // pinned certificate using installed trust; it makes no online
        // revocation-freshness claim and never fetches a replacement runtime.
        dwProvFlags: WTD_CACHE_ONLY_URL_RETRIEVAL | WTD_REVOCATION_CHECK_NONE | WTD_DISABLE_MD2_MD4,
        ..Default::default()
    };
    let mut action = WINTRUST_ACTION_GENERIC_VERIFY_V2;
    let status = unsafe {
        WinVerifyTrust(
            (-1isize) as _,
            &mut action,
            (&mut trust as *mut WINTRUST_DATA).cast(),
        )
    };
    let result = (|| {
        if status != 0 {
            bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Windows Launcher Authenticode verification failed ({status:#x})");
        }
        let provider = unsafe { WTHelperProvDataFromStateData(trust.hWVTStateData) };
        if provider.is_null() {
            bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Windows signature state unavailable");
        }
        let signer = unsafe { WTHelperGetProvSignerFromChain(provider, 0, 0, 0) };
        if signer.is_null()
            || unsafe {
                (*signer).csCertChain == 0
                    || (*signer).pasCertChain.is_null()
                    || (*signer).dwError != 0
            }
        {
            bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Windows signer chain unavailable");
        }
        let certificate = unsafe { (*(*signer).pasCertChain).pCert };
        if certificate.is_null()
            || unsafe {
                (*certificate).pbCertEncoded.is_null() || (*certificate).cbCertEncoded == 0
            }
        {
            bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Windows signer certificate unavailable");
        }
        let der = unsafe {
            std::slice::from_raw_parts(
                (*certificate).pbCertEncoded,
                (*certificate).cbCertEncoded as usize,
            )
        };
        if sha256_bytes(der) != expected_signer {
            bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Windows Launcher signer differs from release identity");
        }
        Ok(())
    })();
    trust.dwStateAction = WTD_STATEACTION_CLOSE;
    unsafe {
        WinVerifyTrust(
            (-1isize) as _,
            &mut action,
            (&mut trust as *mut WINTRUST_DATA).cast(),
        )
    };
    result
}

fn authenticated_caller(config: &AppConfig, held: &mut BTreeMap<PathBuf, File>) -> Result<File> {
    let executable = std::env::current_exe()?;
    let parent = executable
        .parent()
        .context("Windows Launcher parent missing")?;
    hold_ancestors(parent, held)?;
    hold_ancestors(&config.app_dir, held)?;
    // Windows Tauri resources are installed beside the running Launcher.
    // Canonicalization is only for spelling equality after every component
    // has been inspected and locked; it cannot hide a junction here.
    if fs::canonicalize(&config.app_dir)? != fs::canonicalize(parent.join("app"))? {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] Windows resources are outside the Launcher installation");
    }
    let file = open_held(&executable, false, false, false)?;
    verify_acl(&file, &current_user_sid()?, false, false)?;
    verify_authenticode(
        &executable,
        &file,
        option_env!("TOBKIRI_WINDOWS_SIGNER_CERT_SHA256").unwrap_or_default(),
    )?;
    Ok(file)
}

pub(super) fn verify_package_provenance(
    config: &AppConfig,
    provenance: &PackageProvenance,
) -> Result<()> {
    if provenance.kind != required_package_provenance_kind() {
        bail!("[PYTHON_SEALED_PROVENANCE_INVALID] packaged Python provenance kind mismatch");
    }
    authenticated_caller(config, &mut BTreeMap::new())?;
    Ok(())
}

pub(super) struct Snapshot {
    root: PathBuf,
    sid: String,
    // Ancestor and caller handles also outlive the child. The installed source
    // tree itself need not remain present once all bytes are authenticated.
    ancestors: BTreeMap<PathBuf, File>,
    caller: Option<File>,
    directories: BTreeMap<PathBuf, File>,
    files: BTreeMap<String, File>,
    identities: BTreeMap<String, WindowsFileIdentity>,
    manifest_sha256: String,
    cleanup_on_drop: bool,
}

impl Snapshot {
    fn add_directory(&mut self, relative: &Path) -> Result<()> {
        if self.directories.contains_key(relative) {
            return Ok(());
        }
        if let Some(parent) = relative.parent() {
            self.add_directory(parent)?;
        }
        let path = self.root.join(relative);
        create_private_directory(&path, &self.sid)?;
        let file = open_held(&path, true, true, false)?;
        verify_acl(&file, &self.sid, false, true)?;
        self.directories.insert(relative.to_path_buf(), file);
        Ok(())
    }

    fn add_bytes(&mut self, relative: &str, bytes: &[u8]) -> Result<()> {
        verify_windows_relative_path(relative)?;
        let relative_path = Path::new(relative);
        self.add_directory(
            relative_path
                .parent()
                .context("snapshot file parent missing")?,
        )?;
        let path = self.root.join(relative_path);
        let mut destination = create_private_file(&path, &self.sid)?;
        destination.write_all(bytes)?;
        destination.sync_all()?;
        drop(destination);
        let held = open_held(&path, false, true, false)?;
        set_private_acl(&held, &self.sid, true)?;
        self.identities
            .insert(relative.to_owned(), windows_file_identity(&held)?);
        self.files.insert(relative.to_owned(), held);
        Ok(())
    }

    fn copy_file(
        &mut self,
        source: &Path,
        entry: &SealedFile,
        source_directories: &mut BTreeMap<PathBuf, File>,
    ) -> Result<()> {
        let path = source.join(&entry.path);
        hold_ancestors(
            path.parent().context("source parent missing")?,
            source_directories,
        )?;
        let mut source_file = open_held(&path, false, false, false)?;
        if source_file.metadata()?.len() != entry.size {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] source file size changed");
        }
        self.add_directory(
            Path::new(&entry.path)
                .parent()
                .context("snapshot parent missing")?,
        )?;
        let target = self.root.join(&entry.path);
        let mut output = create_private_file(&target, &self.sid)?;
        let mut hash = Sha256::new();
        let mut total = 0u64;
        let mut buffer = [0u8; 128 * 1024];
        loop {
            let count = source_file.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            total = total
                .checked_add(count as u64)
                .context("snapshot file size overflow")?;
            if total > entry.size {
                bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] source file grew");
            }
            hash.update(&buffer[..count]);
            output.write_all(&buffer[..count])?;
        }
        if total != entry.size
            || hex::encode(hash.finalize()) != entry.sha256
            || windows_file_identity(&source_file)?.number_of_links != 1
        {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] source bytes or link count changed");
        }
        output.sync_all()?;
        drop(output);
        let held = open_held(&target, false, true, false)?;
        set_private_acl(&held, &self.sid, true)?;
        self.identities
            .insert(entry.path.clone(), windows_file_identity(&held)?);
        self.files.insert(entry.path.clone(), held);
        Ok(())
    }

    pub(super) fn revalidate(
        &self,
        manifest: &SealedEnvironmentManifest,
        overlay: &VerifiedRuntimeOverlay,
    ) -> Result<()> {
        for file in self.directories.values() {
            verify_acl(file, &self.sid, true, true)?;
            if file.metadata()?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot directory became a reparse point");
            }
        }
        let expected = manifest
            .files
            .iter()
            .map(|entry| entry.path.clone())
            .chain([
                MANIFEST_FILENAME.to_owned(),
                SNAPSHOT_RUNTIME_MANIFEST.to_owned(),
            ])
            .collect::<BTreeSet<_>>();
        if self.files.keys().cloned().collect::<BTreeSet<_>>() != expected {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot closure changed");
        }
        for (path, file) in &self.files {
            verify_acl(file, &self.sid, true, true)?;
            if self.identities.get(path) != Some(&windows_file_identity(file)?) {
                bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot handle identity changed");
            }
            let reopened = open_held(&self.root.join(path), false, false, false)?;
            if windows_file_identity(&reopened)? != windows_file_identity(file)? {
                bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot path identity changed");
            }
        }
        // Held no-write handles make repeated full hashing unnecessary. These
        // two generated files bind the source manifest and runtime overlay.
        for (path, digest) in [
            (MANIFEST_FILENAME, self.manifest_sha256.as_str()),
            (SNAPSHOT_RUNTIME_MANIFEST, overlay.sha256.as_str()),
        ] {
            if sha256_bytes(&read_bounded_regular(
                &self.root.join(path),
                4 * 1024 * 1024,
            )?) != digest
            {
                bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot authority changed");
            }
        }
        let mut actual_files = Vec::new();
        let mut actual_directories = Vec::new();
        collect_files(
            &self.root,
            &self.root,
            &mut actual_files,
            &mut actual_directories,
        )?;
        let expected_inventory = expected
            .into_iter()
            .filter(|p| p != MANIFEST_FILENAME)
            .collect::<BTreeSet<_>>();
        if actual_files.into_iter().collect::<BTreeSet<_>>() != expected_inventory
            || actual_directories.into_iter().collect::<BTreeSet<_>>()
                != self
                    .directories
                    .keys()
                    .filter(|p| !p.as_os_str().is_empty())
                    .map(|p| p.to_string_lossy().replace('\\', "/"))
                    .collect()
        {
            bail!("[PYTHON_SEALED_SNAPSHOT_INVALID] snapshot inventory changed");
        }
        Ok(())
    }

    pub(super) fn cleanup(&mut self) {
        self.cleanup_on_drop = false;
        // Restore deletion rights through the authenticated handles, before
        // releasing no-delete sharing. Never recursively walk an unknown tree.
        for file in self.directories.values().chain(self.files.values()) {
            if set_private_acl(file, &self.sid, false).is_err() {
                return;
            }
        }
        let mut files = self.files.keys().cloned().collect::<Vec<_>>();
        files.sort();
        self.files.clear();
        for relative in files {
            let _ = fs::remove_file(self.root.join(relative));
        }
        let mut directories = self.directories.keys().cloned().collect::<Vec<_>>();
        directories.sort_by_key(|path| std::cmp::Reverse(path.components().count()));
        for relative in directories {
            self.directories.remove(&relative);
            let _ = fs::remove_dir(self.root.join(relative));
        }
        self.caller.take();
        self.ancestors.clear();
    }
}

impl Drop for Snapshot {
    fn drop(&mut self) {
        if self.cleanup_on_drop {
            self.cleanup();
        }
    }
}

pub(super) fn create(
    config: &AppConfig,
    source_root: &Path,
    manifest: &SealedEnvironmentManifest,
    manifest_bytes: &[u8],
    runtime_resource_manifest: &crate::runtime_resource_integrity::VerifiedResourceManifest,
    runtime_overlay: &VerifiedRuntimeOverlay,
) -> Result<VerifiedEnvironment> {
    let sid = current_user_sid()?;
    let mut ancestors = BTreeMap::new();
    let caller = authenticated_caller(config, &mut ancestors)?;
    let temp = std::env::temp_dir();
    hold_ancestors(&temp, &mut ancestors)?;
    verify_acl(
        ancestors
            .get(&temp)
            .context("private temp handle missing")?,
        &sid,
        false,
        false,
    )?;
    let root = temp.join(format!(
        ".tobkiri-sealed-python-{}-{}",
        std::process::id(),
        random_nonce()
    ));
    create_private_directory(&root, &sid)?;
    let root_handle = open_held(&root, true, true, false)?;
    let mut snapshot = Snapshot {
        root: root.clone(),
        sid,
        ancestors,
        caller: Some(caller),
        directories: [(PathBuf::new(), root_handle)].into_iter().collect(),
        files: BTreeMap::new(),
        identities: BTreeMap::new(),
        manifest_sha256: sha256_bytes(manifest_bytes),
        cleanup_on_drop: true,
    };
    let mut names = BTreeSet::new();
    let mut source_directories = BTreeMap::new();
    hold_ancestors(source_root, &mut source_directories)?;
    for entry in &manifest.files {
        verify_windows_relative_path(&entry.path)?;
        // Conservative Unicode lowercasing also rejects ordinary NTFS aliases.
        // create_new below is authoritative for any additional OS equivalence.
        if !names.insert(entry.path.to_lowercase()) {
            bail!("[PYTHON_SEALED_INVALID] case-aliased Windows inventory");
        }
        snapshot.copy_file(source_root, entry, &mut source_directories)?;
    }
    snapshot.add_bytes(MANIFEST_FILENAME, manifest_bytes)?;
    snapshot.add_bytes(SNAPSHOT_RUNTIME_MANIFEST, &runtime_overlay.bytes)?;
    for directory in snapshot.directories.values() {
        set_private_acl(directory, &snapshot.sid, true)?;
    }
    if &crate::runtime_resource_integrity::verify(&config.app_dir)? != runtime_resource_manifest {
        bail!(
            "[PYTHON_SEALED_SNAPSHOT_INVALID] runtime resource authority changed during snapshot"
        );
    }
    verify_package_provenance(config, &manifest.package_provenance)?;
    snapshot.revalidate(manifest, runtime_overlay)?;
    // These auxiliary handles permit cleanup after the primary Snapshot
    // drops its no-delete holds; they still never permit concurrent writes.
    let root_lease = open_held(&root, true, false, true)?;
    let interpreter_lease = open_held(&fixed_interpreter(&root), false, false, true)?;
    let environment_lease = acquire_environment_lease(&root.join(LIFETIME_LEASE))?;
    snapshot.cleanup_on_drop = false;
    Ok(VerifiedEnvironment {
        manifest_path: root.join(MANIFEST_FILENAME),
        root: root.clone(),
        manifest: manifest.clone(),
        _root_lease: root_lease,
        _interpreter_lease: interpreter_lease,
        environment_lease: Some(environment_lease),
        snapshot_path: Some(root),
        windows_snapshot: Some(snapshot),
        runtime_overlay: runtime_overlay.clone(),
        cleanup_authority: CleanupAuthority::BeforeChildSpawn,
    })
}

pub(super) fn acquire_lease(file: &File) -> Result<()> {
    let mut overlapped: OVERLAPPED = unsafe { zeroed() };
    if unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut overlapped,
        )
    } == 0
    {
        return Err(io::Error::last_os_error())
            .context("[PYTHON_SEALED_LEASE_BUSY] Windows lease is held exclusively");
    }
    Ok(())
}

pub(super) fn release_lease(file: &File) -> Result<()> {
    let mut overlapped: OVERLAPPED = unsafe { zeroed() };
    if unsafe { UnlockFileEx(file.as_raw_handle(), 0, 1, 0, &mut overlapped) } == 0 {
        return Err(io::Error::last_os_error()).context("release Windows parent lifetime lease");
    }
    Ok(())
}

pub(super) fn prove_child_lease(path: &Path) -> Result<()> {
    let file = open_held(path, false, false, false)?;
    let mut overlapped: OVERLAPPED = unsafe { zeroed() };
    if unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_FAIL_IMMEDIATELY | LOCKFILE_EXCLUSIVE_LOCK,
            0,
            1,
            0,
            &mut overlapped,
        )
    } != 0
    {
        release_lease(&file)?;
        bail!("[PYTHON_SEALED_LEASE_MISSING] bootstrap did not retain the Windows lifetime lease");
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() != Some(ERROR_LOCK_VIOLATION as i32) {
        return Err(error).context("[PYTHON_SEALED_LEASE_INVALID] test Windows child lease");
    }
    Ok(())
}

pub(super) fn configure_process(command: &mut Command) -> Result<()> {
    let mut directory = vec![0u16; 32_768];
    let len = unsafe { GetWindowsDirectoryW(directory.as_mut_ptr(), directory.len() as u32) };
    if len == 0 || len as usize >= directory.len() {
        return Err(io::Error::last_os_error()).context("resolve trusted Windows directory");
    }
    command.env(
        "SystemRoot",
        OsString::from_wide(&directory[..len as usize]),
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn windows_inventory_rejects_aliases_streams_devices_and_traversal() {
        for value in [
            "",
            "../escape",
            "a/../b",
            "a\\b",
            "a:stream",
            "app/NUL.py",
            "app/COM1.txt",
            "app/file.",
            "app/file ",
            "a//b",
            "app/?",
            "app/\0",
        ] {
            assert!(
                verify_windows_relative_path(value).is_err(),
                "accepted {value:?}"
            );
        }
        for value in [
            "venv/Scripts/python.exe",
            "runtime/DLLs/_ssl.pyd",
            "app/core_runtime/main.py",
        ] {
            verify_windows_relative_path(value).unwrap();
        }
    }

    fn private_test_root() -> (PathBuf, String, File) {
        let sid = current_user_sid().unwrap();
        let root =
            std::env::temp_dir().join(format!("tobkiri-windows-trust-test-{}", random_nonce()));
        create_private_directory(&root, &sid).unwrap();
        let held = open_held(&root, true, true, false).unwrap();
        (root, sid, held)
    }

    #[test]
    fn private_acl_seals_and_held_files_reject_writes_and_replacement() {
        let (root, sid, directory) = private_test_root();
        let path = root.join("payload.bin");
        let mut output = create_private_file(&path, &sid).unwrap();
        output.write_all(b"trusted").unwrap();
        drop(output);
        let file = open_held(&path, false, true, false).unwrap();
        set_private_acl(&file, &sid, true).unwrap();
        verify_acl(&file, &sid, true, true).unwrap();
        assert!(OpenOptions::new().write(true).open(&path).is_err());
        assert!(fs::rename(&path, root.join("moved.bin")).is_err());
        set_private_acl(&file, &sid, false).unwrap();
        drop(file);
        fs::remove_file(path).unwrap();
        drop(directory);
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn hardlinked_files_are_rejected() {
        let (root, _sid, directory) = private_test_root();
        let path = root.join("payload.bin");
        fs::write(&path, b"trusted").unwrap();
        fs::hard_link(&path, root.join("alias.bin")).unwrap();
        assert!(open_held(&path, false, false, false).is_err());
        fs::remove_file(path).unwrap();
        fs::remove_file(root.join("alias.bin")).unwrap();
        drop(directory);
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn lease_proof_requires_a_lock_not_merely_an_open_handle() {
        let (root, _sid, directory) = private_test_root();
        let path = root.join("lease.v1");
        fs::write(&path, b"lease").unwrap();
        let held = open_held(&path, false, false, false).unwrap();
        assert!(prove_child_lease(&path).is_err());
        acquire_lease(&held).unwrap();
        prove_child_lease(&path).unwrap();
        release_lease(&held).unwrap();
        assert!(prove_child_lease(&path).is_err());
        drop(held);
        fs::remove_file(path).unwrap();
        drop(directory);
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn authenticode_rejects_unsigned_payload_even_with_pinned_identity() {
        let (root, _sid, directory) = private_test_root();
        let path = root.join("unsigned.exe");
        fs::write(&path, b"not a signed executable").unwrap();
        let file = open_held(&path, false, false, false).unwrap();
        assert!(verify_authenticode(&path, &file, &"a".repeat(64)).is_err());
        assert!(verify_authenticode(&path, &file, "").is_err());
        drop(file);
        fs::remove_file(path).unwrap();
        drop(directory);
        fs::remove_dir(root).unwrap();
    }
}
