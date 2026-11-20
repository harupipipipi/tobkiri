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
    pub(crate) const SETTLEMENT_RECEIPT_FORMAT: &str = "tobkiri.registry-packs/2";
    pub(crate) const ORCHESTRATION_RECEIPT_FORMAT: &str = "tobkiri.registry-packs/3";
    pub(crate) const PROVIDER_ADAPTER_RECEIPT_FORMAT: &str = "tobkiri.registry-packs/4";
    pub(crate) const AI_ROUTING_RECEIPT_FORMAT: &str = "tobkiri.registry-packs/5";
    pub(crate) const APPLICATION_PRESENTATION_RECEIPT_FORMAT: &str = "tobkiri.registry-packs/6";
    pub(crate) const CONVERSATION_COMPLETE_RECEIPT_FORMAT: &str = "tobkiri.registry-packs/7";
    pub(crate) const WORKFLOW_MESSAGES_RECEIPT_FORMAT: &str = "tobkiri.registry-packs/8";
    const RECEIPT_LIMIT: u64 = 16 * 1024;
    // The Pack authoring CLI's .tpk size ceiling.
    pub(crate) const ARCHIVE_LIMIT: u64 = 512 * 1024 * 1024;
    const PROVIDER_REGISTRY_PACK_ID: &str = "local.provider-registry";
    pub(crate) const PACK_IDS: [&str; 3] = [
        PROVIDER_REGISTRY_PACK_ID,
        "local.model-registry",
        "local.saved-conversation",
    ];
    pub(crate) const SETTLEMENT_PACK_ID: &str = "local.saved-turn-runtime";
    pub(crate) const ORCHESTRATION_PACK_ID: &str = "local.saved-orchestration";
    pub(crate) const PROVIDER_ADAPTER_PACK_ID: &str = "local.provider-adapter";
    pub(crate) const AI_ROUTING_PACK_ID: &str = "local.ai-routing";
    pub(crate) const APPLICATION_PRESENTATION_PACK_ID: &str = "local.application-presentation";
    pub(crate) const CONVERSATION_COMPLETE_PACK_ID: &str = "local.conversation-complete";
    pub(crate) const WORKFLOW_MESSAGES_PACK_ID: &str = "local.workflow-messages";
    const ORCHESTRATION_PACK_VERSION: &str = "0.1.0";
    const PROVIDER_ADAPTER_PACK_VERSIONS: [&str; 3] = ["0.1.0", "0.2.0", "0.3.0"];
    const AI_ROUTING_PACK_VERSIONS: [&str; 7] = [
        "0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0", "0.6.0", "0.7.0",
    ];
    const ADDITIONAL_PACK_IDS: [&str; 7] = [
        SETTLEMENT_PACK_ID,
        ORCHESTRATION_PACK_ID,
        PROVIDER_ADAPTER_PACK_ID,
        AI_ROUTING_PACK_ID,
        APPLICATION_PRESENTATION_PACK_ID,
        CONVERSATION_COMPLETE_PACK_ID,
        WORKFLOW_MESSAGES_PACK_ID,
    ];

    #[derive(Deserialize)]
    #[serde(deny_unknown_fields)]
    pub(crate) struct Receipt {
        format: String,
        packs: Vec<Pack>,
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

        pub(crate) fn archive_sha256(&self) -> &str {
            &self.archive_sha256
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
            || format!("sha256:{:x}", reader.sha256.finalize()) != pack.archive_sha256()
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
        let expected_count = match receipt.format.as_str() {
            RECEIPT_FORMAT => PACK_IDS.len(),
            SETTLEMENT_RECEIPT_FORMAT => PACK_IDS.len() + 1,
            ORCHESTRATION_RECEIPT_FORMAT => PACK_IDS.len() + 2,
            PROVIDER_ADAPTER_RECEIPT_FORMAT => PACK_IDS.len() + 3,
            AI_ROUTING_RECEIPT_FORMAT => PACK_IDS.len() + 4,
            APPLICATION_PRESENTATION_RECEIPT_FORMAT => PACK_IDS.len() + 5,
            CONVERSATION_COMPLETE_RECEIPT_FORMAT => PACK_IDS.len() + 6,
            WORKFLOW_MESSAGES_RECEIPT_FORMAT => PACK_IDS.len() + ADDITIONAL_PACK_IDS.len(),
            _ => return Err(invalid("Registry Pack receipt format is invalid")),
        };
        if receipt.packs.len() != expected_count {
            return Err(invalid(
                "Registry Pack receipt has the wrong number of records",
            ));
        }
        let ids = PACK_IDS.into_iter().chain(ADDITIONAL_PACK_IDS);
        for (id, pack) in ids.zip(&receipt.packs) {
            if pack.id != id
                || pack.archive != format!("{id}.tpk")
                || !valid_version(&pack.version)
                || (id == ORCHESTRATION_PACK_ID && pack.version != ORCHESTRATION_PACK_VERSION)
                || (id == PROVIDER_ADAPTER_PACK_ID
                    && !PROVIDER_ADAPTER_PACK_VERSIONS.contains(&pack.version.as_str()))
                || (id == AI_ROUTING_PACK_ID
                    && !AI_ROUTING_PACK_VERSIONS.contains(&pack.version.as_str()))
                || (id == APPLICATION_PRESENTATION_PACK_ID && pack.version != "0.1.0")
                || (id == CONVERSATION_COMPLETE_PACK_ID && pack.version != "0.1.0")
                || (id == WORKFLOW_MESSAGES_PACK_ID && pack.version != "0.1.0")
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
        let independent = open_child_directory(directory, c".")?;
        let stream = unsafe { libc::fdopendir(independent.as_raw_fd()) };
        if stream.is_null() {
            return Err(io::Error::last_os_error());
        }
        // fdopendir owns this descriptor on success.
        let _ = independent.into_raw_fd();
        let result = (|| {
            let mut found = [false; PACK_IDS.len() + ADDITIONAL_PACK_IDS.len() + 1];
            let names = std::iter::once(RECEIPT.as_bytes())
                .chain(receipt.packs.iter().map(|pack| pack.archive.as_bytes()));
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
                let index = names.clone().position(|expected| expected == name);
                let Some(index) = index else {
                    return Err(invalid("Registry Pack directory has an unexpected entry"));
                };
                if found[index] {
                    return Err(invalid("Registry Pack directory has a duplicate entry"));
                }
                found[index] = true;
            }
            if found[..=receipt.packs.len()].contains(&false) {
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
        mut checkpoint: impl FnMut() -> io::Result<()>,
    ) -> io::Result<()> {
        let directory = open_installed_pack_directory(app_root, &mut checkpoint)?;
        let (receipt, _) = read_verified_receipt(&directory, expected_sha256, &mut checkpoint)?;
        receipt.verify_archives(&directory, |_| Ok(io::sink()), checkpoint)
    }

    /// Inspect the exact Provider archive stream and publish its result only
    /// after all installed archives have matched their receipt entries.
    pub(crate) fn verify_installed_with_provider<T>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<T>,
    ) -> io::Result<T> {
        let (provider, ()) = verify_installed_with_provider_and_model(
            app_root,
            expected_sha256,
            checkpoint,
            inspect_provider,
            discard_archive,
        )?;
        Ok(provider)
    }

    /// Inspect the exact Model archive stream and publish its result only
    /// after all installed archives have matched their receipt entries.
    pub(crate) fn verify_installed_with_model<T>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_model: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<T>,
    ) -> io::Result<T> {
        let ((), model) = verify_installed_with_provider_and_model(
            app_root,
            expected_sha256,
            checkpoint,
            discard_archive,
            inspect_model,
        )?;
        Ok(model)
    }

    /// Inspect the Conversation archive and verify every installed archive's digest.
    pub(crate) fn verify_installed_with_conversation<T>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_conversation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<T>,
    ) -> io::Result<T> {
        let ((), (), conversation) = verify_installed_with_archives(
            app_root,
            expected_sha256,
            checkpoint,
            discard_archive,
            discard_archive,
            inspect_conversation,
        )?;
        Ok(conversation)
    }

    /// Inspect the Provider and Model archives in a single pass over the
    /// installed packs and return both results only after every digest matches.
    pub(crate) fn verify_installed_with_provider_and_model<P, M>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<P>,
        inspect_model: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<M>,
    ) -> io::Result<(P, M)> {
        let (provider, model, ()) = verify_installed_with_archives(
            app_root,
            expected_sha256,
            checkpoint,
            inspect_provider,
            inspect_model,
            discard_archive,
        )?;
        Ok((provider, model))
    }

    /// Inspect the original archives and verify every installed archive's digest.
    pub(crate) fn verify_installed_with_archives<P, M, C>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<P>,
        inspect_model: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<M>,
        inspect_conversation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<C>,
    ) -> io::Result<(P, M, C)> {
        let (provider, model, conversation, _) = verify_installed_with_settlement_archives(
            app_root,
            expected_sha256,
            checkpoint,
            inspect_provider,
            inspect_model,
            inspect_conversation,
            discard_archive,
        )?;
        Ok((provider, model, conversation))
    }

    /// Inspect the settlement archive if admitted by the exact installed receipt.
    pub(crate) fn verify_installed_with_settlement<S>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_settlement: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<S>,
    ) -> io::Result<Option<S>> {
        let ((), (), (), settlement) = verify_installed_with_settlement_archives(
            app_root,
            expected_sha256,
            checkpoint,
            discard_archive,
            discard_archive,
            discard_archive,
            inspect_settlement,
        )?;
        Ok(settlement)
    }

    /// Verify the exact receipt and each physical archive before returning any result.
    /// An absent settlement is allowed only in the original three-Pack format.
    pub(crate) fn verify_installed_with_settlement_archives<P, M, C, S>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<P>,
        inspect_model: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<M>,
        inspect_conversation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<C>,
        inspect_settlement: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<S>,
    ) -> io::Result<(P, M, C, Option<S>)> {
        let (provider, model, conversation, settlement, _) =
            verify_installed_with_orchestration_archives(
                app_root,
                expected_sha256,
                checkpoint,
                inspect_provider,
                inspect_model,
                inspect_conversation,
                inspect_settlement,
                discard_archive,
            )?;
        Ok((provider, model, conversation, settlement))
    }

    /// Inspect the orchestration archive when the installed receipt includes it.
    pub(crate) fn verify_installed_with_orchestration<O>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_orchestration: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<O>,
    ) -> io::Result<Option<O>> {
        let (_, _, _, _, orchestration) = verify_installed_with_orchestration_archives(
            app_root,
            expected_sha256,
            checkpoint,
            discard_archive,
            discard_archive,
            discard_archive,
            discard_archive,
            inspect_orchestration,
        )?;
        Ok(orchestration)
    }

    /// Inspect the original, settlement, and orchestration archives and verify all digests.
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    pub(crate) fn verify_installed_with_orchestration_archives<P, M, C, S, O>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<P>,
        inspect_model: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<M>,
        inspect_conversation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<C>,
        inspect_settlement: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<S>,
        inspect_orchestration: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<O>,
    ) -> io::Result<(P, M, C, Option<S>, Option<O>)> {
        let (provider, model, conversation, settlement, orchestration, _) =
            verify_installed_with_provider_adapter_archives(
                app_root,
                expected_sha256,
                checkpoint,
                inspect_provider,
                inspect_model,
                inspect_conversation,
                inspect_settlement,
                inspect_orchestration,
                discard_archive,
            )?;
        Ok((provider, model, conversation, settlement, orchestration))
    }

    /// Inspect the provider-adapter archive when the installed receipt includes it.
    pub(crate) fn verify_installed_with_provider_adapter<A>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider_adapter: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<A>,
    ) -> io::Result<Option<A>> {
        let (_, _, _, _, _, provider_adapter) = verify_installed_with_provider_adapter_archives(
            app_root,
            expected_sha256,
            checkpoint,
            discard_archive,
            discard_archive,
            discard_archive,
            discard_archive,
            discard_archive,
            inspect_provider_adapter,
        )?;
        Ok(provider_adapter)
    }

    /// Inspect each archive in one pass and return the results after every digest matches.
    /// Optional archives are present only in receipt versions requiring them.
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    pub(crate) fn verify_installed_with_provider_adapter_archives<P, M, C, S, O, A>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<P>,
        inspect_model: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<M>,
        inspect_conversation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<C>,
        inspect_settlement: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<S>,
        inspect_orchestration: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<O>,
        inspect_provider_adapter: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<A>,
    ) -> io::Result<(P, M, C, Option<S>, Option<O>, Option<A>)> {
        let (provider, model, conversation, settlement, orchestration, provider_adapter, _) =
            verify_installed_with_ai_routing_archives(
                app_root,
                expected_sha256,
                checkpoint,
                inspect_provider,
                inspect_model,
                inspect_conversation,
                inspect_settlement,
                inspect_orchestration,
                inspect_provider_adapter,
                discard_archive,
            )?;
        Ok((
            provider,
            model,
            conversation,
            settlement,
            orchestration,
            provider_adapter,
        ))
    }

    /// Inspect the AI Routing archive when the installed receipt includes it.
    pub(crate) fn verify_installed_with_ai_routing<R>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_ai_routing: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<R>,
    ) -> io::Result<Option<R>> {
        let (_, _, _, _, _, _, ai_routing) = verify_installed_with_ai_routing_archives(
            app_root,
            expected_sha256,
            checkpoint,
            discard_archive,
            discard_archive,
            discard_archive,
            discard_archive,
            discard_archive,
            discard_archive,
            inspect_ai_routing,
        )?;
        Ok(ai_routing)
    }

    /// Inspect the archives through AI Routing in receipt order before returning any results.
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    pub(crate) fn verify_installed_with_ai_routing_archives<P, M, C, S, O, A, R>(
        app_root: &Path,
        expected_sha256: &str,
        mut checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<P>,
        inspect_model: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<M>,
        inspect_conversation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<C>,
        inspect_settlement: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<S>,
        inspect_orchestration: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<O>,
        inspect_provider_adapter: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<A>,
        inspect_ai_routing: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<R>,
    ) -> io::Result<(P, M, C, Option<S>, Option<O>, Option<A>, Option<R>)> {
        let directory = open_installed_pack_directory(app_root, &mut checkpoint)?;
        let (receipt, _) = read_verified_receipt(&directory, expected_sha256, &mut checkpoint)?;
        let ([provider, model, conversation], newest) = receipt
            .packs
            .split_first_chunk::<3>()
            .expect("the receipt contains the three original Packs");
        let provider = inspect_archive(&directory, provider, &mut checkpoint, inspect_provider)?;
        let model = inspect_archive(&directory, model, &mut checkpoint, inspect_model)?;
        let conversation = inspect_archive(
            &directory,
            conversation,
            &mut checkpoint,
            inspect_conversation,
        )?;
        let settlement = newest
            .first()
            .map(|pack| inspect_archive(&directory, pack, &mut checkpoint, inspect_settlement))
            .transpose()?;
        let orchestration = newest
            .get(1)
            .map(|pack| inspect_archive(&directory, pack, &mut checkpoint, inspect_orchestration))
            .transpose()?;
        let provider_adapter = newest
            .get(2)
            .map(|pack| {
                inspect_archive(&directory, pack, &mut checkpoint, inspect_provider_adapter)
            })
            .transpose()?;
        let ai_routing = newest
            .get(3)
            .map(|pack| inspect_archive(&directory, pack, &mut checkpoint, inspect_ai_routing))
            .transpose()?;
        Ok((
            provider,
            model,
            conversation,
            settlement,
            orchestration,
            provider_adapter,
            ai_routing,
        ))
    }

    /// Reopen the Provider Registry archive under the exact installed receipt.
    #[allow(dead_code)]
    pub(crate) fn verify_installed_with_provider_snapshot<R>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_provider: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<R>,
    ) -> io::Result<R> {
        inspect_installed_pack(
            app_root,
            expected_sha256,
            PROVIDER_REGISTRY_PACK_ID,
            checkpoint,
            inspect_provider,
        )?
        .ok_or_else(|| {
            io::Error::other("bundled Registry receipt has no Provider Registry archive")
        })
    }

    /// Reopen the presentation archive under the exact installed receipt.
    /// The reader authenticates its bytes before releasing the caller's result.
    #[allow(dead_code)]
    pub(crate) fn verify_installed_with_application_presentation<R>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_presentation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<R>,
    ) -> io::Result<Option<R>> {
        inspect_installed_pack(
            app_root,
            expected_sha256,
            APPLICATION_PRESENTATION_PACK_ID,
            checkpoint,
            inspect_presentation,
        )
    }

    /// Reopen the Conversation completion archive under the exact installed receipt.
    #[allow(dead_code)]
    pub(crate) fn verify_installed_with_conversation_complete<R>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_conversation: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<R>,
    ) -> io::Result<Option<R>> {
        inspect_installed_pack(
            app_root,
            expected_sha256,
            CONVERSATION_COMPLETE_PACK_ID,
            checkpoint,
            inspect_conversation,
        )
    }

    /// Reopen the Workflow messages archive under the exact installed receipt.
    #[allow(dead_code)]
    pub(crate) fn verify_installed_with_workflow_messages<R>(
        app_root: &Path,
        expected_sha256: &str,
        checkpoint: impl FnMut() -> io::Result<()>,
        inspect_workflow_messages: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<R>,
    ) -> io::Result<Option<R>> {
        inspect_installed_pack(
            app_root,
            expected_sha256,
            WORKFLOW_MESSAGES_PACK_ID,
            checkpoint,
            inspect_workflow_messages,
        )
    }

    fn inspect_installed_pack<R>(
        app_root: &Path,
        expected_sha256: &str,
        pack_id: &str,
        mut checkpoint: impl FnMut() -> io::Result<()>,
        inspect: impl FnOnce(&Pack, &mut dyn Read) -> io::Result<R>,
    ) -> io::Result<Option<R>> {
        let directory = open_installed_pack_directory(app_root, &mut checkpoint)?;
        let (receipt, _) = read_verified_receipt(&directory, expected_sha256, &mut checkpoint)?;
        receipt
            .packs
            .iter()
            .find(|pack| pack.id == pack_id)
            .map(|pack| inspect_archive(&directory, pack, &mut checkpoint, inspect))
            .transpose()
    }

    fn open_installed_pack_directory(
        app_root: &Path,
        checkpoint: &mut impl FnMut() -> io::Result<()>,
    ) -> io::Result<File> {
        checkpoint()?;
        let app = open_directory(app_root)?;
        let name = CString::new(DIRECTORY).expect("Registry Pack directory is a fixed name");
        open_child_directory(&app, &name)
    }

    fn discard_archive(_: &Pack, input: &mut dyn Read) -> io::Result<()> {
        io::copy(input, &mut io::sink()).map(|_| ())
    }
}
