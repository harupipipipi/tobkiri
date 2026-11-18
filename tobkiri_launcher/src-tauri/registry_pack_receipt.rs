//! Descriptor-relative verification of the pinned Registry Pack receipt and archives.

pub(crate) const DIRECTORY: &str = "native-registry-packs";

pub(crate) fn valid_raw_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(unix)]
pub(crate) use unix::*;

#[cfg(unix)]
mod unix {
    use super::{valid_raw_sha256, DIRECTORY};
    use serde::Deserialize;
    use sha2::{Digest, Sha256};
    use std::ffi::{CStr, CString};
    use std::fs::{File, OpenOptions};
    use std::io::{self, Read, Write};
    use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd};
    use std::os::unix::{
        ffi::OsStrExt,
        fs::{MetadataExt, OpenOptionsExt},
    };
    use std::path::{Component, Path};

    pub(crate) const RECEIPT: &str = "receipt.json";
    pub(crate) const RECEIPT_FORMAT: &str = "tobkiri.registry-packs/1";
    const RECEIPT_LIMIT: u64 = 16 * 1024;
    // The Pack authoring CLI's .tpk size ceiling.
    pub(crate) const ARCHIVE_LIMIT: u64 = 512 * 1024 * 1024;
    pub(crate) const PACK_IDS: [&str; 3] = [
        "local.provider-registry",
        "local.model-registry",
        "local.saved-conversation",
    ];

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    pub(crate) struct Receipt {
        format: String,
        packs: [Pack; PACK_IDS.len()],
    }

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    pub(crate) struct Pack {
        id: String,
        version: String,
        archive: String,
        archive_bytes: u64,
        archive_sha256: String,
        manifest_sha256: String,
    }

    impl Pack {
        pub(crate) fn id(&self) -> &str {
            &self.id
        }

        pub(crate) fn version(&self) -> &str {
            &self.version
        }

        pub(crate) fn archive_bytes(&self) -> u64 {
            self.archive_bytes
        }

        pub(crate) fn manifest_sha256(&self) -> &str {
            &self.manifest_sha256
        }
    }

    struct ArchiveReader<'a, C> {
        input: io::Take<File>,
        receipt: &'a Pack,
        checkpoint: &'a mut C,
        checkpoint_error: Option<io::Error>,
        sha256: Sha256,
        bytes_read: u64,
    }

    impl<C: FnMut() -> io::Result<()>> Read for ArchiveReader<'_, C> {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            if self.checkpoint_error.is_none() {
                self.checkpoint_error = (self.checkpoint)().err();
            }
            if self.checkpoint_error.is_some() {
                // `io::copy` and Pack parsing may retry an interrupted read.
                return Err(io::Error::other(
                    "Registry Pack inspection checkpoint failed",
                ));
            }
            let count = self.input.read(output)?;
            self.bytes_read += count as u64;
            if self.bytes_read > self.receipt.archive_bytes {
                return Err(archive_mismatch(self.receipt));
            }
            self.sha256.update(&output[..count]);
            Ok(count)
        }
    }

    fn archive_mismatch(pack: &Pack) -> io::Error {
        invalid(format!(
            "Registry Pack archive differs from receipt: {}",
            pack.id
        ))
    }

    fn inspect_archive<T>(
        directory: &File,
        pack: &Pack,
        checkpoint: &mut impl FnMut() -> io::Result<()>,
        inspect: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<T>,
    ) -> io::Result<T> {
        checkpoint()?;
        let file = open_input(
            directory,
            &pack.archive,
            pack.archive_bytes..=pack.archive_bytes,
        )?;
        let mut reader = ArchiveReader {
            input: file.take(pack.archive_bytes + 1),
            receipt: pack,
            checkpoint,
            checkpoint_error: None,
            sha256: Sha256::new(),
            bytes_read: 0,
        };
        // The visitor may stop at the end of a format member. Include the
        // entire physical archive in the receipt hash before accepting its data.
        let result = inspect(pack, &mut reader).and_then(|result| {
            io::copy(&mut reader, &mut io::sink())?;
            Ok(result)
        });
        let result = reader.checkpoint_error.take().map_or(result, Err)?;
        if reader.bytes_read != pack.archive_bytes
            || format!("sha256:{:x}", reader.sha256.finalize()) != pack.archive_sha256
        {
            return Err(archive_mismatch(pack));
        }
        checkpoint()?;
        Ok(result)
    }

    fn tagged_sha256(value: &str) -> bool {
        value.strip_prefix("sha256:").is_some_and(valid_raw_sha256)
    }

    fn valid_version(version: &str) -> bool {
        // Match tobkiri-manifest's three-part numeric Pack version grammar.
        let mut parts = version.split('.');
        (0..3).all(|_| {
            parts.next().is_some_and(|part| {
                part == "0"
                    || (!part.is_empty()
                        && !part.starts_with('0')
                        && part.bytes().all(|byte| byte.is_ascii_digit()))
            })
        }) && parts.next().is_none()
    }

    fn parse_receipt(raw: &[u8]) -> io::Result<Receipt> {
        let document: serde_json::Value = serde_json::from_slice(raw)?;
        // Derived structs also accept positional arrays. Require named objects.
        if !document
            .get("packs")
            .and_then(serde_json::Value::as_array)
            .is_some_and(|packs| packs.iter().all(serde_json::Value::is_object))
        {
            return Err(invalid(
                "Registry Pack receipt must describe named Pack records",
            ));
        }
        let receipt: Receipt = serde_json::from_slice(raw)?;
        if receipt.format != RECEIPT_FORMAT {
            return Err(invalid("Registry Pack receipt format is invalid"));
        }
        for (id, pack) in PACK_IDS.into_iter().zip(&receipt.packs) {
            if pack.id != id
                || pack.archive != format!("{id}.tpk")
                || !valid_version(&pack.version)
                || !(1..=ARCHIVE_LIMIT).contains(&pack.archive_bytes)
                || !tagged_sha256(&pack.archive_sha256)
                || !tagged_sha256(&pack.manifest_sha256)
            {
                return Err(invalid(format!(
                    "Registry Pack receipt record for {id} is invalid"
                )));
            }
        }
        Ok(receipt)
    }

    fn open_input(
        directory: &File,
        name: &str,
        sizes: std::ops::RangeInclusive<u64>,
    ) -> io::Result<File> {
        let name =
            CString::new(name).map_err(|_| invalid("Registry Pack filename contains NUL"))?;
        let fd = unsafe {
            libc::openat(
                directory.as_raw_fd(),
                name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd == -1 {
            return Err(io::Error::last_os_error());
        }
        let file = unsafe { File::from_raw_fd(fd) };
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.nlink() != 1 || !sizes.contains(&metadata.len()) {
            return Err(invalid(
                "Registry Pack input must be a bounded, singly linked regular file",
            ));
        }
        Ok(file)
    }

    fn invalid(message: impl Into<String>) -> io::Error {
        io::Error::new(io::ErrorKind::InvalidData, message.into())
    }

    const DIRECTORY_FLAGS: libc::c_int =
        libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC;

    fn open_child_directory(directory: &File, name: &CStr) -> io::Result<File> {
        let fd = unsafe { libc::openat(directory.as_raw_fd(), name.as_ptr(), DIRECTORY_FLAGS) };
        if fd == -1 {
            return Err(io::Error::last_os_error());
        }
        Ok(unsafe { File::from_raw_fd(fd) })
    }

    /// Resolve an absolute directory with each path component opened without following links.
    pub(crate) fn open_directory(path: &Path) -> io::Result<File> {
        if !path.is_absolute() {
            return Err(invalid("Registry Pack directory path must be absolute"));
        }
        let mut directory = OpenOptions::new()
            .read(true)
            .custom_flags(DIRECTORY_FLAGS)
            .open("/")?;
        for component in path.components().skip(1) {
            let Component::Normal(name) = component else {
                return Err(invalid(
                    "Registry Pack directory path has an invalid component",
                ));
            };
            let name = CString::new(name.as_bytes())
                .map_err(|_| invalid("Registry Pack directory path contains NUL"))?;
            directory = open_child_directory(&directory, &name)?;
        }
        Ok(directory)
    }

    fn verify_inventory(directory: &File, receipt: &Receipt) -> io::Result<()> {
        let names = [
            RECEIPT.as_bytes(),
            receipt.packs[0].archive.as_bytes(),
            receipt.packs[1].archive.as_bytes(),
            receipt.packs[2].archive.as_bytes(),
        ];
        let independent = open_child_directory(directory, c".")?;
        let stream = unsafe { libc::fdopendir(independent.as_raw_fd()) };
        if stream.is_null() {
            return Err(io::Error::last_os_error());
        }
        // fdopendir owns this descriptor on success.
        let _ = independent.into_raw_fd();
        let result = (|| {
            let mut found = [false; PACK_IDS.len() + 1];
            loop {
                #[cfg(any(target_os = "macos", target_os = "ios"))]
                unsafe {
                    *libc::__error() = 0;
                }
                #[cfg(any(target_os = "linux", target_os = "android"))]
                unsafe {
                    *libc::__errno_location() = 0;
                }
                let entry = unsafe { libc::readdir(stream) };
                if entry.is_null() {
                    let error = io::Error::last_os_error();
                    if error.raw_os_error() != Some(0) {
                        return Err(error);
                    }
                    break;
                }
                let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
                if name == b"." || name == b".." {
                    continue;
                }
                let index = names.iter().position(|expected| *expected == name);
                let Some(index) = index else {
                    return Err(invalid("Registry Pack directory has an unexpected entry"));
                };
                if found[index] {
                    return Err(invalid("Registry Pack directory has a duplicate entry"));
                }
                found[index] = true;
            }
            if found.contains(&false) {
                return Err(invalid(
                    "Registry Pack directory is missing a receipt or archive",
                ));
            }
            Ok(())
        })();
        unsafe { libc::closedir(stream) };
        result
    }

    /// Read and authenticate the bounded receipt and match the directory inventory against it.
    pub(crate) fn read_verified_receipt(
        directory: &File,
        expected_sha256: &str,
        mut checkpoint: impl FnMut() -> io::Result<()>,
    ) -> io::Result<(Receipt, Vec<u8>)> {
        checkpoint()?;
        if !valid_raw_sha256(expected_sha256) {
            return Err(invalid(
                "Registry Pack receipt pin must be a lowercase SHA-256",
            ));
        }
        let mut raw = Vec::new();
        open_input(directory, RECEIPT, 1..=RECEIPT_LIMIT)?
            .take(RECEIPT_LIMIT + 1)
            .read_to_end(&mut raw)?;
        if raw.len() as u64 > RECEIPT_LIMIT
            || format!("{:x}", Sha256::digest(&raw)) != expected_sha256
        {
            return Err(invalid(
                "Registry Pack receipt differs from the expected digest",
            ));
        }
        let receipt = parse_receipt(&raw)?;
        verify_inventory(directory, &receipt)?;
        Ok((receipt, raw))
    }

    impl Receipt {
        /// Stream each archive under the receipt's length and digest. Stage copies through the
        /// same reader and installed verification discards the bytes.
        pub(crate) fn verify_archives<W: Write>(
            &self,
            directory: &File,
            mut open_output: impl FnMut(&str) -> io::Result<W>,
            mut checkpoint: impl FnMut() -> io::Result<()>,
        ) -> io::Result<()> {
            for pack in &self.packs {
                inspect_archive(directory, pack, &mut checkpoint, |pack, input| {
                    io::copy(input, &mut open_output(&pack.archive)?).map(|_| ())
                })?;
            }
            checkpoint()
        }
    }

    /// Verify the installed Registry Packs under the exact application resource root.
    pub(crate) fn verify_installed(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
    ) -> io::Result<()> {
        verify_installed_with_provider(app_root, expected_sha256, checkpoint, |_, input| {
            io::copy(input, &mut io::sink()).map(|_| ())
        })
    }

    /// Inspect the exact Provider archive stream and publish its result only
    /// after all installed archives have matched their receipt entries.
    pub(crate) fn verify_installed_with_provider<T>(
        app_root: &Path,
        expected_sha256: &str,
        mut checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<T>,
    ) -> io::Result<T> {
        checkpoint()?;
        let app = open_directory(app_root)?;
        let name = CString::new(DIRECTORY).expect("Registry Pack directory is a fixed name");
        let directory = open_child_directory(&app, &name)?;
        let (receipt, _) = read_verified_receipt(&directory, expected_sha256, &mut checkpoint)?;
        let [provider, model, conversation] = &receipt.packs;
        let result = inspect_archive(&directory, provider, &mut checkpoint, inspect_provider)?;
        for pack in [model, conversation] {
            inspect_archive(&directory, pack, &mut checkpoint, |_, input| {
                io::copy(input, &mut io::sink()).map(|_| ())
            })?;
        }
        Ok(result)
    }
}
