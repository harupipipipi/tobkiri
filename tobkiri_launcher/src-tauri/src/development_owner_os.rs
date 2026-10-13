//! Current-session OS evidence for explicitly confirmed developer continuation.
//!
//! This module verifies the running Native against bounded, stable executable
//! bytes and retains actual Native picker URLs. It confers no consent by itself
//! and persists no authorization or folder grant. The caller must establish
//! local-development mode, obtain explicit Native consent, capture paths with
//! no-follow handles, and retain the returned selection for the Host lifetime.

fn canonical_sha256(value: &str) -> Result<&str, String> {
    let value = value.strip_prefix("sha256:").unwrap_or(value);
    if value.len() != 64
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err("Developer Native executable digest is invalid".into());
    }
    Ok(value)
}

#[cfg(target_os = "macos")]
pub(crate) use macos::{
    retain_picker_url_for_session, verify_current_executable, PickerSessionAccess,
};

#[cfg(not(target_os = "macos"))]
pub(crate) fn verify_current_executable(_expected_sha256: &str) -> Result<(), String> {
    Err("Developer Native code verification is available only on macOS".into())
}

#[cfg(target_os = "macos")]
mod macos {
    use super::canonical_sha256;
    use objc2::rc::Retained;
    use objc2::Message;
    use objc2_foundation::{NSString, NSURL};
    use sha2::{Digest, Sha256};
    use std::ffi::c_void;
    use std::fs::OpenOptions;
    use std::io::Read;
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    use std::path::{Path, PathBuf};
    use std::ptr::{self, NonNull};

    type Ref = *const c_void;

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(value: Ref);
        fn CFGetTypeID(value: Ref) -> usize;
        fn CFDataGetTypeID() -> usize;
        fn CFDataGetLength(data: Ref) -> isize;
        fn CFDataGetBytePtr(data: Ref) -> *const u8;
        fn CFDictionaryGetValue(dictionary: Ref, key: Ref) -> Ref;
    }

    #[link(name = "Security", kind = "framework")]
    extern "C" {
        fn SecCodeCopySelf(flags: u32, out: *mut Ref) -> i32;
        fn SecCodeCheckValidity(code: Ref, flags: u32, requirement: Ref) -> i32;
        fn SecStaticCodeCreateWithPath(path: Ref, flags: u32, out: *mut Ref) -> i32;
        fn SecStaticCodeCheckValidity(code: Ref, flags: u32, requirement: Ref) -> i32;
        fn SecCodeCopySigningInformation(code: Ref, flags: u32, out: *mut Ref) -> i32;
        static kSecCodeInfoUnique: Ref;
    }

    /// A retained CF reference; every owned API result is released on every path.
    struct Owned(NonNull<c_void>);

    impl Owned {
        fn new(value: Ref) -> Result<Self, String> {
            NonNull::new(value.cast_mut())
                .map(Self)
                .ok_or_else(|| "macOS returned no developer continuation object".into())
        }

        fn raw(&self) -> Ref {
            self.0.as_ptr()
        }
    }

    impl Drop for Owned {
        fn drop(&mut self) {
            // SAFETY: Owned contains one non-null +1 CF reference.
            unsafe { CFRelease(self.raw()) }
        }
    }

    fn status(code: i32, operation: &str) -> Result<(), String> {
        if code != 0 {
            // Keep code-verification diagnostics independent of private file paths.
            return Err(format!(
                "Developer continuation {operation} failed ({code})"
            ));
        }
        Ok(())
    }

    fn copied(operation: &str, call: impl FnOnce(*mut Ref) -> i32) -> Result<Owned, String> {
        let mut out = ptr::null();
        let code = call(&mut out);
        // Be conservative even when a failing API returns a partial result.
        let owned = (!out.is_null()).then(|| Owned::new(out)).transpose()?;
        status(code, operation)?;
        owned.ok_or_else(|| "macOS returned no developer continuation object".into())
    }

    fn read_data(value: Ref, maximum: usize) -> Result<Vec<u8>, String> {
        // SAFETY: Type and length are checked before reading the retained CFData.
        unsafe {
            if value.is_null() || CFGetTypeID(value) != CFDataGetTypeID() {
                return Err("Developer Native signing data is invalid".into());
            }
            let size = CFDataGetLength(value);
            if size <= 0 || size as usize > maximum {
                return Err("Developer Native signing data has an invalid size".into());
            }
            let bytes = CFDataGetBytePtr(value);
            if bytes.is_null() {
                return Err("Developer Native signing data is unavailable".into());
            }
            Ok(std::slice::from_raw_parts(bytes, size as usize).to_vec())
        }
    }

    fn executable_sha256(path: &Path) -> Result<String, String> {
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "Developer Native executable cannot be read".to_string())?;
        let metadata = file
            .metadata()
            .map_err(|_| "Developer Native executable is unavailable")?;
        if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 1024 * 1024 * 1024 {
            return Err("Developer Native executable is invalid".into());
        }
        let mut digest = Sha256::new();
        let mut buffer = [0u8; 64 * 1024];
        let mut total = 0u64;
        loop {
            let size = file
                .read(&mut buffer)
                .map_err(|_| "Developer Native executable cannot be read")?;
            if size == 0 {
                break;
            }
            total += size as u64;
            if total > metadata.len() {
                return Err("Developer Native executable changed while reading".into());
            }
            digest.update(&buffer[..size]);
        }
        if total != metadata.len() {
            return Err("Developer Native executable changed while reading".into());
        }
        let after = file
            .metadata()
            .map_err(|_| "Developer Native executable is unavailable")?;
        let named = std::fs::symlink_metadata(path)
            .map_err(|_| "Developer Native executable is unavailable")?;
        let identity = |m: &std::fs::Metadata| {
            (
                m.dev(),
                m.ino(),
                m.uid(),
                m.mode(),
                m.len(),
                m.mtime(),
                m.mtime_nsec(),
                m.ctime(),
                m.ctime_nsec(),
            )
        };
        if identity(&metadata) != identity(&after) || identity(&metadata) != identity(&named) {
            return Err("Developer Native executable changed while reading".into());
        }
        Ok(format!("{:x}", digest.finalize()))
    }

    fn current_executable_path() -> Result<PathBuf, String> {
        let path =
            std::env::current_exe().map_err(|_| "Developer Native executable is unavailable")?;
        let resolved = std::fs::canonicalize(&path)
            .map_err(|_| "Developer Native executable is unavailable")?;
        if resolved != path {
            return Err("Developer Native executable path is redirected".into());
        }
        Ok(path)
    }

    /// Verify running code against the exact stable on-disk executable bytes.
    /// This works for ad-hoc Native; it confers no durable storage permission.
    pub(crate) fn verify_current_executable(expected_sha256: &str) -> Result<(), String> {
        let digest = canonical_sha256(expected_sha256)?;
        let path = current_executable_path()?;
        if executable_sha256(&path)? != digest {
            return Err("Developer Native executable identity changed".into());
        }
        let code = copied("current code lookup", |out| unsafe {
            SecCodeCopySelf(0, out)
        })?;
        // Apple's dynamic check compares the kernel's running cdhash against
        // the on-disk CodeDirectory (Code.cpp: SecCode::checkValidity). Signing
        // information alone would only describe disk bytes and is insufficient.
        status(
            unsafe { SecCodeCheckValidity(code.raw(), 0, ptr::null()) },
            "current code validation",
        )?;
        let path_string =
            NSString::from_str(path.to_str().ok_or("Developer Native path is invalid")?);
        let url = NSURL::fileURLWithPath(&path_string);
        let static_code = copied("executable code lookup", |out| unsafe {
            SecStaticCodeCreateWithPath((&*url as *const NSURL).cast(), 0, out)
        })?;
        // Check all architectures and strict signed resources. Do not request
        // network permission or skip executable/resource validation.
        status(
            unsafe { SecStaticCodeCheckValidity(static_code.raw(), 1 | 16, ptr::null()) },
            "executable signature validation",
        )?;
        let dynamic_info = copied("current code identity lookup", |out| unsafe {
            SecCodeCopySigningInformation(code.raw(), 2, out)
        })?;
        let static_info = copied("executable code identity lookup", |out| unsafe {
            SecCodeCopySigningInformation(static_code.raw(), 2, out)
        })?;
        let code_hash = |info: &Owned| unsafe {
            read_data(CFDictionaryGetValue(info.raw(), kSecCodeInfoUnique), 32)
        };
        if code_hash(&dynamic_info)? != code_hash(&static_info)?
            || executable_sha256(&path)? != digest
            || current_executable_path()? != path
        {
            return Err("Running developer Native does not match its executable image".into());
        }
        Ok(())
    }

    /// Retains only the OS picker's current-session folder selection provenance.
    /// This neither resumes a saved grant nor creates persistent authorization.
    pub(crate) struct PickerSessionAccess {
        _url: Retained<NSURL>,
        path: PathBuf,
    }

    impl PickerSessionAccess {
        /// The selected path; the Native adapter must capture it with no-follow FDs.
        pub(crate) fn path(&self) -> &Path {
            &self.path
        }
    }

    /// Retain an actual NSOpenPanel URL for an unsandboxed current-session flow.
    /// The caller must never synthesize this URL from a renderer/path string.
    pub(crate) fn retain_picker_url_for_session(
        url: &NSURL,
    ) -> Result<PickerSessionAccess, String> {
        if !cfg!(debug_assertions) || !url.isFileURL() {
            return Err(
                "Developer continuation requires a Native development folder selection".into(),
            );
        }
        let path = url
            .path()
            .ok_or("Developer continuation folder URL is invalid")?;
        let path = PathBuf::from(path.to_string());
        if !path.is_absolute() || !path.is_dir() {
            return Err("Developer continuation selection is not a local folder".into());
        }
        Ok(PickerSessionAccess {
            _url: url.retain(),
            path,
        })
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn executable_hash_rejects_symlinks_empty_and_oversized_files() {
            let unique = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir().join(format!(
                "tobkiri-native-hash-{}-{unique}",
                std::process::id()
            ));
            std::fs::create_dir(&root).unwrap();
            let file = root.join("image");
            std::fs::write(&file, b"Native image fixture").unwrap();
            assert_eq!(
                executable_sha256(&file).unwrap(),
                format!("{:x}", Sha256::digest(b"Native image fixture"))
            );
            let link = root.join("redirected-image");
            std::os::unix::fs::symlink(&file, &link).unwrap();
            assert!(executable_sha256(&link).is_err());
            std::fs::write(&file, b"").unwrap();
            assert!(executable_sha256(&file).is_err());
            // Sparse fixture: no large allocation or read is performed.
            std::fs::File::create(&file)
                .unwrap()
                .set_len(1024 * 1024 * 1024 + 1)
                .unwrap();
            assert!(executable_sha256(&file).is_err());
            std::fs::remove_dir_all(root).unwrap();
        }

        #[test]
        fn running_image_check_accepts_exact_test_image_and_rejects_wrong_sha() {
            // Only this process's own code and file bytes; no Keychain access.
            let path = std::env::current_exe().unwrap();
            let digest = executable_sha256(&path).unwrap();
            if std::fs::canonicalize(&path).unwrap() == path {
                verify_current_executable(&digest).unwrap();
            } else {
                // For example /tmp -> /private/tmp is a redirected ancestry.
                assert!(verify_current_executable(&digest).is_err());
            }
            assert!(verify_current_executable(&"0".repeat(64)).is_err());
        }

        #[test]
        fn session_picker_access_does_not_accept_non_file_urls() {
            let url = NSURL::URLWithString(&NSString::from_str("https://example.invalid")).unwrap();
            assert!(retain_picker_url_for_session(&url).is_err());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn executable_verification_requires_canonical_sha256() {
        let digest = "a".repeat(64);
        assert_eq!(canonical_sha256(&digest).unwrap(), digest);
        assert_eq!(
            canonical_sha256(&format!("sha256:{digest}")).unwrap(),
            digest
        );
        for bad in [
            "",
            "account",
            "sha256:abc",
            &"A".repeat(64),
            &"a".repeat(65),
        ] {
            assert!(canonical_sha256(bad).is_err());
        }
    }
}
