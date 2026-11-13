//! Fixed catalog resources of the actual selected AppHandle, under Review.
//! This is not a Path service: the private caller selects one source, and the
//! loader can only consume its lock, each lock occurrence, pins, then artifacts.
use super::*;
use base64::Engine as _;
use sha2::{Digest as _, Sha256};
use std::path::Path;
use tobkiri_canon::{AllocationBudget, CoreBuffer, CoreBufferBuilder, CoreValue, MAX_BYTES};
#[cfg(all(test, unix))]
#[path = "original_profile_catalog_tests.rs"]
mod tests;

pub(crate) struct CatalogIo {
    source: PathBuf,
    runtime: PathBuf,
    selected: PathBuf,
    cancellation: CoreCancellation,
    cleanup: Arc<CleanupState>,
    _capacity: NativeSetupReservation,
    development: bool,
    loader: Loader,
    binding: Option<CoreValue>,
    lock_bytes: Option<Funded<Zeroizing<Vec<u8>>>>,
    lock: Option<Funded<Document>>,
    entry: usize,
    walk: usize,
    pins: usize,
    candidate: Option<PathBuf>,
    error_id: u32,
    initial_bundle: bool,
    manifest_bytes: Option<Funded<Zeroizing<Vec<u8>>>>,
    budget: Arc<dyn AllocationBudget>,
    development_environment: bool,
    configured_app: Option<PathBuf>,
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum Loader {
    Root,
    RootSymlink,
    RootResolve,
    LockSymlink,
    LockRead,
    Entries,
    Walk,
    EntryRead,
    ArtifactResolve,
    Done,
    Rejected,
}

#[derive(Debug)]
pub(crate) enum BindingFailure {
    Denied(&'static str),
    Original(OriginalAuthorityError),
}
impl From<OriginalAuthorityError> for BindingFailure {
    fn from(value: OriginalAuthorityError) -> Self {
        Self::Original(value)
    }
}

impl CatalogIo {
    /// CI-only observation of the actual finite occurrence. Copy-only DATA;
    /// callers use try_lock and this neither joins IO nor authorizes a read.
    #[cfg(all(
        test,
        feature = "bootstrap-ci",
        target_os = "macos",
        target_arch = "aarch64"
    ))]
    pub(crate) fn ci_progress(&self) -> (&'static str, usize, usize, usize) {
        let stage = match self.loader {
            Loader::Root => "root",
            Loader::RootSymlink => "root-symlink",
            Loader::RootResolve => "root-resolve",
            Loader::LockSymlink => "lock-symlink",
            Loader::LockRead => "lock-read",
            Loader::Entries => "entries",
            Loader::Walk => "walk",
            Loader::EntryRead => "entry-read",
            Loader::ArtifactResolve => "artifact-resolve",
            Loader::Done => "done",
            Loader::Rejected => "rejected",
        };
        (stage, self.entry, self.walk, self.pins)
    }

    pub(crate) fn new<S: crate::BootstrapReviewOwner>(
        owner: &S,
        invocation: &NativeSetupInvocation,
        cancellation: &CoreCancellation,
        cleanup: Arc<CleanupState>,
        runtime: &Path,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self> {
        // Prepay the bounded private state/Path scratch before copying any
        // selected spelling. No source object or NativeAccount is cloned.
        let capacity = invocation
            .reserve(CapacityKind::VfsBuffer, 128 * 1024)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        let (source, development_environment, configured_app) = owner
            .with_original_catalog_source(|input| {
                if input.runtime.as_os_str().len() > 8192
                    || input
                        .configured_app
                        .is_some_and(|path| path.as_os_str().len() > 8192)
                {
                    return Err(OriginalAuthorityError::capacity(
                        OriginalAuthorityStage::Path,
                    ));
                }
                let development = input.environment == "development";
                Ok((
                    input.runtime.to_path_buf(),
                    development,
                    if development {
                        input.configured_app.map(Path::to_path_buf)
                    } else {
                        None
                    },
                ))
            })??;
        if runtime.as_os_str().len() > 8192 {
            return Err(OriginalAuthorityError::capacity(
                OriginalAuthorityStage::Path,
            ));
        }
        owner.check_original_owner()?;
        Ok(Self {
            source,
            runtime: runtime.to_path_buf(),
            selected: PathBuf::new(),
            cancellation: cancellation.clone(),
            cleanup,
            _capacity: capacity,
            development: false,
            loader: Loader::Root,
            binding: None,
            lock_bytes: None,
            lock: None,
            entry: 0,
            walk: 0,
            pins: 0,
            candidate: None,
            error_id: 16,
            initial_bundle: false,
            manifest_bytes: None,
            budget: Arc::clone(budget),
            development_environment,
            configured_app,
        })
    }
    fn operation<'a, S: crate::BootstrapReviewOwner>(
        &self,
        owner: &'a S,
        invocation: &'a NativeSetupInvocation,
        cancellation: &'a CoreCancellation,
    ) -> Result<Operation<'a, S>> {
        let op = Operation {
            owner,
            invocation,
            cancellation,
            deadline: invocation
                .deadline()
                .map_err(|_| OriginalAuthorityError::unavailable())?,
            pid: std::process::id(),
            cleanup: self.cleanup.clone(),
            #[cfg(test)]
            fixture: None,
        };
        op.check(OriginalAuthorityStage::Source)?;
        // Reborrow the actual selected origin under the owner's immutable
        // source contract. This path equality is only a consistency refusal;
        // neither it nor resolved metadata creates a resource permission.
        owner.with_original_catalog_source(|input| {
            if input.runtime == self.source {
                Ok(())
            } else {
                Err(OriginalAuthorityError::unavailable())
            }
        })??;
        Ok(op)
    }
    pub(crate) fn select_development<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        invocation: &NativeSetupInvocation,
    ) -> Result<bool> {
        let cancellation = self.cancellation.clone();
        let op = self.operation(owner, invocation, &cancellation)?;
        if !self.development_environment {
            return Ok(false);
        }
        let configured = match self
            .configured_app
            .as_ref()
            .filter(|v| !v.as_os_str().is_empty())
        {
            Some(value) => match fs::canonicalize(value) {
                Ok(path) => Some(path),
                Err(_) => return Ok(false),
            },
            None => None,
        };
        // Both comparisons repeat runtime_root.resolve(), at the original
        // short-circuit positions. The second is not a cached first proof.
        for sibling in [false, true] {
            op.check(OriginalAuthorityStage::Source)?;
            let root = if sibling {
                self.runtime
                    .parent()
                    .unwrap_or(&self.runtime)
                    .join("tobkiri_launcher/src-tauri/target/dev-defaults")
            } else {
                self.runtime.join("bundled/dev-defaults")
            };
            let bundle = root.join("v4");
            let artifacts = root.join("platform-artifacts");
            let canonical =
                fs::canonicalize(&self.runtime).map_err(|e| raw_io(e, self.runtime.to_str()))?;
            if configured.as_ref() == Some(&canonical)
                && bundle.is_dir()
                && !bundle.is_symlink()
                && artifacts.is_dir()
                && !artifacts.is_symlink()
            {
                self.selected = bundle;
                self.development = true;
                return Ok(true);
            }
        }
        Ok(false)
    }
    pub(crate) fn select_packaged(&mut self, root: &Path) -> Result<()> {
        if self.development || root.as_os_str().len() > 8192 {
            return Err(OriginalAuthorityError::unavailable());
        }
        self.selected = root.to_path_buf();
        self.initial_bundle = true;
        Ok(())
    }
    pub(crate) fn bind_loader(&mut self, binding: CoreValue) -> Result<()> {
        if self.binding.is_some()
            || binding.as_str().is_none()
            || self.selected.as_os_str().is_empty()
        {
            return Err(OriginalAuthorityError::unavailable());
        }
        self.binding = Some(binding);
        Ok(())
    }

    /// The resolver hashes a separate physical read after its bundle selection.
    /// The previous loader's bytes and the manifest's inventory are unrelated.
    pub(crate) fn resolver_lock_digest<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        invocation: &NativeSetupInvocation,
    ) -> Result<Digest> {
        let op = self.operation(owner, invocation, &self.cancellation)?;
        if self.selected.as_os_str().is_empty() || self.binding.is_some() {
            return Err(OriginalAuthorityError::unavailable());
        }
        let path = self.selected.join("bundle.lock.json");
        let bytes = read_file(&op, &path, &self.budget)?;
        charge_work(&self.budget, bytes.value.len() as u64)?;
        let digest = Digest::hash(&bytes.value);
        op.check(OriginalAuthorityStage::Source)?;
        Ok(digest)
    }
    pub(crate) fn inspect<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        invocation: &NativeSetupInvocation,
        request: &CoreValue,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<CoreBuffer> {
        if !Arc::ptr_eq(&self.budget, budget) {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let cancellation = self.cancellation.clone();
        let op = self.operation(owner, invocation, &cancellation)?;
        crate::source_metadata::exact(request, &["format", "scope", "target", "operation", "args"])
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if request.get_key("format").and_then(|v| {
            v.as_str()
                .map(|s| s == "io.tobkiri.native-catalog-request.v1")
        }) != Some(true)
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let scope = request
            .get_key("scope")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let expected =
            crate::bootstrap_metadata::Scope::original(invocation).frame(budget, false)?;
        let expected = CoreValue::parse_guest(expected.as_bytes(), budget)?
            .get_key("scope")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if scope.encode_guest(budget)?.as_bytes() != expected.encode_guest(budget)?.as_bytes() {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let target = request
            .get_key("target")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        crate::source_metadata::exact(&target, &["binding", "steps"])
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if target
            .get_key("binding")
            .and_then(|v| v.as_str().map(str::to_owned))
            != self
                .binding
                .as_ref()
                .and_then(|v| v.as_str().map(str::to_owned))
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let steps = target
            .get_key("steps")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let steps_array = steps
            .array()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if steps_array.len() > 256 {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let operation = request
            .get_key("operation")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let operation = operation
            .as_str()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let args = request
            .get_key("args")
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let args = args
            .array()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let mut path = self.selected.clone();
        if self.loader != Loader::Root {
            for step in steps_array.iter() {
                let pair = step
                    .array()
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                if pair.len() != 2 {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let kind = pair
                    .get(0)
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                let value = pair
                    .get(1)
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                let value = value
                    .as_str()
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                if value.len() > 8192 {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                op.check(OriginalAuthorityStage::Source)?;
                charge_work(&self.budget, value.len() as u64 + 1)?;
                match kind.as_str() {
                    Some("join") => {
                        // Bound the owned intermediate BEFORE PathBuf growth,
                        // even for a request the occurrence grammar will deny.
                        // One small step must not amplify many prior joins
                        // beyond the existing prepaid private Path scratch.
                        path.as_os_str()
                            .len()
                            .checked_add(value.len())
                            .and_then(|n| n.checked_add(1))
                            .filter(|n| *n <= 8192)
                            .ok_or_else(|| {
                                OriginalAuthorityError::capacity(OriginalAuthorityStage::Path)
                            })?;
                        path.push(value);
                    }
                    Some("parent") if value.is_empty() => {
                        path.pop();
                    }
                    _ => return Err(OriginalAuthorityError::unavailable().into()),
                }
            }
        }
        let mut output = CoreBuffer::builder(budget, MAX_BYTES);
        output.append(b"{\"format\":\"io.tobkiri.native-inspection-response.v1\",\"scope\":")?;
        output.append(scope.encode_guest(budget)?.as_bytes())?;
        output.append(b",\"result\":")?;
        let valid = match self.loader {
            Loader::Root if operation == "root" && args.is_empty() => {
                let parents = if self.initial_bundle { 2 } else { 4 };
                let expected_len = parents + usize::from(self.initial_bundle);
                if steps_array.len() != expected_len {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                for index in 0..parents {
                    let step = steps_array
                        .get(index)
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    let pair = step
                        .array()
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    if pair.get(0).and_then(|v| v.as_str().map(|v| v == "parent")) != Some(true)
                        || pair.get(1).and_then(|v| v.as_str().map(str::is_empty)) != Some(true)
                    {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                }
                if self.initial_bundle {
                    let step = steps_array
                        .get(parents)
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    let pair = step
                        .array()
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    if pair.get(0).and_then(|v| v.as_str().map(|v| v == "join")) != Some(true)
                        || pair.get(1).and_then(|v| v.as_str().map(|v| v == "v4")) != Some(true)
                    {
                        return Err(OriginalAuthorityError::unavailable().into());
                    }
                }
                write_path(
                    &mut output,
                    self.binding.as_ref().expect("bound loader"),
                    &self.selected,
                    None,
                    budget,
                )?;
                self.loader = Loader::RootSymlink;
                true
            }
            Loader::RootSymlink
                if operation == "is-symlink" && args.is_empty() && steps_array.is_empty() =>
            {
                let symlink = self.selected.is_symlink();
                write_bool(&mut output, symlink)?;
                self.loader = if symlink {
                    Loader::Rejected
                } else {
                    Loader::RootResolve
                };
                true
            }
            Loader::RootResolve
                if operation == "resolve" && strict(&args) && steps_array.is_empty() =>
            {
                self.selected = fs::canonicalize(&self.selected)
                    .map_err(|e| raw_io(e, self.selected.to_str()))?;
                write_path(
                    &mut output,
                    self.binding.as_ref().expect("bound loader"),
                    &self.selected,
                    Some(&steps),
                    budget,
                )?;
                self.loader = Loader::LockSymlink;
                true
            }
            Loader::LockSymlink
                if operation == "is-symlink"
                    && args.is_empty()
                    && path == self.selected.join("bundle.lock.json") =>
            {
                let symlink = path.is_symlink();
                write_bool(&mut output, symlink)?;
                self.loader = if symlink {
                    Loader::Rejected
                } else {
                    Loader::LockRead
                };
                true
            }
            Loader::LockRead
                if operation == "read-bytes"
                    && args.is_empty()
                    && path == self.selected.join("bundle.lock.json") =>
            {
                let bytes = read_file(&op, &path, &self.budget)?;
                write_bytes(&mut output, &bytes.value)?;
                self.lock_bytes = Some(bytes);
                self.loader = Loader::Entries;
                true
            }
            Loader::Entries if operation == "resolve" && strict(&args) => {
                self.ensure_lock(&op)?;
                let relative = self.entry_path(self.entry)?;
                if path != self.selected.join(&relative) {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let candidate = fs::canonicalize(&path).map_err(|e| raw_io(e, path.to_str()))?;
                write_path(
                    &mut output,
                    self.binding.as_ref().expect("bound loader"),
                    &candidate,
                    Some(&steps),
                    budget,
                )?;
                self.candidate = Some(candidate);
                self.walk = 0;
                self.loader = Loader::Walk;
                true
            }
            Loader::Walk if operation == "is-symlink" && args.is_empty() => {
                if self.candidate.as_ref().is_none_or(|candidate| {
                    candidate == &self.selected || !candidate.starts_with(&self.selected)
                }) {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let relative = self.entry_path(self.entry)?;
                let parts: Vec<_> = Path::new(&relative)
                    .components()
                    .filter(|part| *part != Component::CurDir)
                    .collect();
                if self.walk >= parts.len() {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let mut expected = self.selected.clone();
                for part in &parts[..=self.walk] {
                    expected.push(part);
                }
                if path != expected {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let symlink = path.is_symlink();
                write_bool(&mut output, symlink)?;
                self.walk += 1;
                if symlink {
                    self.loader = Loader::Rejected;
                } else if self.walk == parts.len() {
                    self.loader = Loader::EntryRead;
                }
                true
            }
            Loader::EntryRead if operation == "read-bytes" && args.is_empty() => {
                if path != self.selected.join(self.entry_path(self.entry)?) {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let bytes = read_file(
                    &op,
                    self.candidate.as_ref().expect("resolved entry"),
                    &self.budget,
                )?;
                write_bytes(&mut output, &bytes.value)?;
                self.candidate = None;
                self.entry += 1;
                self.loader = Loader::Entries;
                true
            }
            Loader::Entries if operation == "read-bytes" && args.is_empty() => {
                self.ensure_lock(&op)?;
                if self.entry != self.entry_count()? {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let index = self
                    .pin_index(self.pins)
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                if path != self.selected.join(self.entry_path(index)?) {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                // The actual second read deliberately does not repeat resolve
                // or a symlink walk; never replay the first bytes from DATA.
                let bytes = read_file(&op, &path, &self.budget)?;
                write_bytes(&mut output, &bytes.value)?;
                self.pins += 1;
                true
            }
            Loader::Entries
                if operation == "is-dir"
                    && args.is_empty()
                    && path
                        == self
                            .selected
                            .parent()
                            .ok_or_else(OriginalAuthorityError::unavailable)?
                            .join("platform-artifacts") =>
            {
                self.ensure_lock(&op)?;
                if self.entry != self.entry_count()? || self.pin_index(self.pins).is_some() {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                let is_dir = path.is_dir();
                write_bool(&mut output, is_dir)?;
                self.loader = if is_dir {
                    Loader::ArtifactResolve
                } else {
                    Loader::Done
                };
                true
            }
            Loader::ArtifactResolve
                if operation == "resolve"
                    && strict(&args)
                    && path
                        == self
                            .selected
                            .parent()
                            .ok_or_else(OriginalAuthorityError::unavailable)?
                            .join("platform-artifacts") =>
            {
                let resolved = fs::canonicalize(&path).map_err(|e| raw_io(e, path.to_str()))?;
                write_path(
                    &mut output,
                    self.binding.as_ref().expect("bound loader"),
                    &resolved,
                    Some(&steps),
                    budget,
                )?;
                self.loader = Loader::Done;
                true
            }
            _ => false,
        };
        if !valid {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        op.check(OriginalAuthorityStage::Source)?;
        output.append(b"}")?;
        Ok(output.finish()?)
    }
    fn ensure_lock<S: crate::BootstrapReviewOwner>(&mut self, op: &Operation<'_, S>) -> Result<()> {
        if self.lock.is_none() {
            let bytes = self
                .lock_bytes
                .take()
                .ok_or_else(OriginalAuthorityError::unavailable)?;
            // The original guest already parsed/validated the lock before it
            // can request an entry. This copy is a resource occurrence index;
            // it is not a catalog, hash proof, grant, or returned file cache.
            self.lock = Some(json::decode_catalog_lock(&bytes.value, op, &self.budget)?);
        }
        Ok(())
    }
    fn entries(&self) -> Result<(&Document, &[usize])> {
        let doc = &self
            .lock
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?
            .value;
        let entries = doc
            .get(doc.root, "entries")
            .and_then(|id| match doc.node(id) {
                Node::Array(items) => Some(items.as_slice()),
                _ => None,
            })
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        Ok((doc, entries))
    }
    fn entry_count(&self) -> Result<usize> {
        Ok(self.entries()?.1.len())
    }
    // Lock occurrence roles only. This is not Pack validation, executable
    // admission, an artifact judgment, or authority derived from kind text.
    fn pin_index(&self, pin: usize) -> Option<usize> {
        let (doc, entries) = self.entries().ok()?;
        entries
            .iter()
            .enumerate()
            .filter(|(_, entry)| {
                doc.get(**entry, "kind")
                    .is_some_and(|kind| doc.string_is(kind, "executable_catalog"))
            })
            .nth(pin)
            .map(|(index, _)| index)
    }
    pub(crate) fn completed(&self) -> bool {
        self.loader == Loader::Done
            && self.entry_count().is_ok_and(|count| count == self.entry)
            && self.pin_index(self.pins).is_none()
    }
    fn entry_path(&self, index: usize) -> Result<String> {
        let (doc, entries) = self.entries()?;
        let entry = *entries
            .get(index)
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let path = doc
            .get(entry, "path")
            .and_then(|id| match doc.node(id) {
                Node::String(text) => Some(text),
                _ => None,
            })
            .ok_or_else(OriginalAuthorityError::unavailable)?
            .utf8()?;
        if path.len() > 8192 {
            return Err(OriginalAuthorityError::capacity(
                OriginalAuthorityStage::Path,
            ));
        }
        Ok(path)
    }
    fn manifest<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        invocation: &NativeSetupInvocation,
    ) -> std::result::Result<Option<Funded<Zeroizing<Vec<u8>>>>, BindingFailure> {
        let cancellation = self.cancellation.clone();
        let op = self.operation(owner, invocation, &cancellation)?;
        if self.development {
            return Ok(None);
        }
        let artifacts = self
            .selected
            .parent()
            .ok_or_else(OriginalAuthorityError::unavailable)?
            .join("platform-artifacts");
        if !artifacts.exists() {
            return Ok(None);
        }
        let manifest = self.runtime.join("runtime-resource-manifest.v1.json");
        if manifest.is_symlink() || !manifest.is_file() {
            return Err(BindingFailure::Denied(
                "packaged runtime resource manifest is unavailable",
            ));
        }
        Ok(Some(read_file(&op, &manifest, &self.budget)?))
    }
    pub(crate) fn prepare_manifest<S: crate::BootstrapReviewOwner>(
        &mut self,
        owner: &S,
        invocation: &NativeSetupInvocation,
    ) -> std::result::Result<bool, BindingFailure> {
        self.manifest_bytes = self.manifest(owner, invocation)?;
        Ok(self.manifest_bytes.is_some())
    }
    pub(crate) fn manifest_slice(&self) -> Option<&[u8]> {
        self.manifest_bytes
            .as_ref()
            .map(|bytes| bytes.value.as_slice())
    }
    pub(crate) fn verify_prepared<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        invocation: &NativeSetupInvocation,
        actual_parse: &crate::BootstrapIdentityValue,
    ) -> std::result::Result<(), BindingFailure> {
        if actual_parse
            .observation()
            .get_key("origin")
            .and_then(|v| v.as_str().map(|v| v == "manifest-returned"))
            != Some(true)
        {
            return Err(BindingFailure::Original(
                OriginalAuthorityError::unavailable(),
            ));
        }
        self.verify_binding(
            owner,
            invocation,
            self.manifest_slice()
                .ok_or_else(OriginalAuthorityError::unavailable)?,
        )
    }
    pub(crate) fn clear_manifest(&mut self) {
        drop(self.manifest_bytes.take());
    }
    #[cfg(unix)]
    pub(crate) fn error_graph(
        &mut self,
        error: &OriginalAuthorityError,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<crate::NativeErrorGraph> {
        let Some(OriginalAuthorityCause::Io(io)) = &error.cause else {
            return Err(OriginalAuthorityError::unavailable().into());
        };
        if error.context.is_some() || io.context.is_some() {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let errno = io.errno.ok_or_else(OriginalAuthorityError::unavailable)?;
        let kind = match errno {
            libc::ENOENT => "file-not-found-error",
            libc::EACCES | libc::EPERM => "permission-error",
            libc::ENOTDIR => "not-a-directory-error",
            libc::EISDIR => "is-a-directory-error",
            libc::EEXIST => "file-exists-error",
            libc::EINTR => "interrupted-error",
            libc::EAGAIN => "blocking-io-error",
            _ => "os-error",
        };
        // libc's actual errno display is the same source used by CPython's
        // OSError constructors. No native Error Display suffix is parsed.
        let strerror = unsafe { std::ffi::CStr::from_ptr(libc::strerror(errno)) }.to_str()?;
        let mut message = format!("[Errno {errno}] {strerror}");
        if let Some(filename) = &io.filename {
            if !filename.is_ascii() {
                return Err(OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Source,
                    "non-ASCII catalog OSError filename display",
                )
                .into());
            }
            message.push_str(": ");
            message.push_str(&json::repr_text(&Text::from_str(filename)));
        }
        self.error_id = self
            .error_id
            .checked_add(1)
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let mut out = CoreBuffer::builder(budget, MAX_BYTES);
        out.append(format!("{{\"format\":\"tobkiri.native-exception/1\",\"graph_id\":{},\"root\":0,\"nodes\":[{{\"kind\":", self.error_id).as_bytes())?;
        crate::bootstrap_metadata::string_parts(&mut out, [kind])?;
        out.append(format!(",\"args\":[{errno},").as_bytes())?;
        crate::bootstrap_metadata::string_parts(&mut out, [strerror])?;
        out.append(format!("],\"fields\":{{\"errno\":{errno},\"strerror\":").as_bytes())?;
        crate::bootstrap_metadata::string_parts(&mut out, [strerror])?;
        out.append(b",\"filename\":")?;
        if let Some(filename) = &io.filename {
            crate::bootstrap_metadata::string_parts(&mut out, [filename.as_str()])?;
        } else {
            out.append(b"null")?;
        }
        out.append(
            b",\"filename2\":null},\"cause\":null,\"context\":null,\"suppress_context\":false}]}",
        )?;
        Ok(crate::NativeErrorGraph::decode_catalog_io(
            CoreBuffer::copy_from(kind.as_bytes(), budget)?,
            CoreBuffer::copy_from(message.as_bytes(), budget)?,
            out.finish()?,
            budget,
        )?)
    }
    #[cfg(not(unix))]
    pub(crate) fn error_graph(
        &mut self,
        _: &OriginalAuthorityError,
        _: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<crate::NativeErrorGraph> {
        Err(OriginalAuthorityError::unavailable().into())
    }
    /// This runs only after the actual same-Store read_text/json.loads accepted
    /// these very bytes. The native decoder supplies bounded structural DATA,
    /// never an invented parser exception or a Pack approval.
    fn verify_binding<S: crate::BootstrapReviewOwner>(
        &self,
        owner: &S,
        invocation: &NativeSetupInvocation,
        bytes: &[u8],
    ) -> std::result::Result<(), BindingFailure> {
        let cancellation = self.cancellation.clone();
        let op = self.operation(owner, invocation, &cancellation)?;
        let doc = json::decode_catalog_manifest(bytes, &op, &self.budget)?;
        let root = doc.value.root;
        if doc.value.entries(root).is_none() {
            return Err(BindingFailure::Original(
                OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Source,
                    "non-object manifest attribute error",
                ),
            ));
        }
        let schema = doc.value.get(root, "schema");
        if schema.is_none_or(|id| {
            !doc.value
                .string_is(id, "io.tobkiri.runtime-resource-manifest.v1")
        }) {
            return Err(BindingFailure::Denied(
                "packaged runtime resource manifest is unsupported",
            ));
        }
        let entries = doc
            .value
            .get(root, "entries")
            .and_then(|id| match doc.value.node(id) {
                Node::Array(items) => Some(items),
                _ => None,
            });
        let Some(entries) = entries else {
            return Err(BindingFailure::Denied(
                "packaged runtime resource inventory is invalid",
            ));
        };
        let mut map_bytes = 4096_usize;
        let map_capacity = op.reserve(OriginalAuthorityStage::Source, map_bytes)?;
        let mut expected = BTreeMap::new();
        for &entry in entries {
            op.check(OriginalAuthorityStage::Source)?;
            if doc.value.entries(entry).is_none() {
                return Err(BindingFailure::Denied(
                    "packaged runtime resource entry is invalid",
                ));
            }
            let text = |name| {
                doc.value
                    .get(entry, name)
                    .and_then(|id| match doc.value.node(id) {
                        Node::String(text) => Some(text),
                        _ => None,
                    })
            };
            // Preserve Python codepoints, including ignored surrogate keys;
            // no UTF-8/canonicalization rejection precedes the original guard.
            let path = text("path");
            let digest = text("sha256");
            let size = doc
                .value
                .get(entry, "size")
                .and_then(|id| match doc.value.node(id) {
                    Node::Scalar(CanonValue::Int(value)) if *value >= 0 => {
                        u64::try_from(*value).ok()
                    }
                    Node::Scalar(CanonValue::BigInt(value)) if !value.starts_with('-') => {
                        Some(value.parse::<u64>().unwrap_or(u64::MAX))
                    }
                    _ => None,
                });
            let (Some(path), Some(digest), Some(size)) = (path, digest, size) else {
                return Err(BindingFailure::Denied(
                    "packaged runtime resource entry is unsafe",
                ));
            };
            if path.0.first() == Some(&u32::from(b'/'))
                || path
                    .0
                    .split(|&point| point == u32::from(b'/'))
                    .any(|part| part == [46, 46])
                || digest.0.len() != 64
                || !digest
                    .0
                    .iter()
                    .all(|&point| matches!(point, 48..=57 | 97..=102))
                || expected.contains_key(path)
            {
                return Err(BindingFailure::Denied(
                    "packaged runtime resource entry is unsafe",
                ));
            }
            map_bytes = map_bytes
                .checked_add(
                    path.0
                        .len()
                        .checked_mul(16)
                        .and_then(|n| n.checked_add(1024))
                        .ok_or_else(|| {
                            OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
                        })?,
                )
                .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            map_capacity
                .resize(map_bytes as u64)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            expected.insert(path.clone(), (size, digest.utf8()?));
        }
        let artifacts = self
            .selected
            .parent()
            .ok_or_else(OriginalAuthorityError::unavailable)?
            .join("platform-artifacts");
        let mut actual = std::collections::BTreeSet::new();
        let mut pending = Vec::new();
        // Traversal state is bounded under the existing invocation, including
        // its live paths/sets and all input/hash buffers.
        let traversal = op.reserve(OriginalAuthorityStage::Source, 32768)?;
        for root in [&self.selected, &artifacts] {
            let prefix = root.strip_prefix(&self.runtime).map_err(|_| {
                OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Source,
                    "catalog resource-root relative_to error",
                )
            })?;
            if root.is_symlink() || !root.is_dir() {
                return Err(BindingFailure::Denied(
                    "packaged Profile resource root is unsafe",
                ));
            }
            pending.push(root.to_path_buf());
            while let Some(current) = pending.pop() {
                op.check(OriginalAuthorityStage::Source)?;
                // os.walk(onerror=None) skips a directory it cannot enumerate.
                let Some((dirs, files)) =
                    scan_directory(&op, &current, pending.len(), &traversal, &self.budget)?
                else {
                    continue;
                };
                if current.is_symlink() || dirs.iter().chain(&files).any(|path| path.is_symlink()) {
                    return Err(BindingFailure::Denied(
                        "packaged Profile resource contains a symlink",
                    ));
                }
                for path in files {
                    if !path.is_file() {
                        return Err(BindingFailure::Denied(
                            "packaged Profile resource contains a non-file entry",
                        ));
                    }
                    let relative = path
                        .strip_prefix(&self.runtime)
                        .map_err(|_| {
                            OriginalAuthorityError::unsupported(
                                OriginalAuthorityStage::Source,
                                "catalog resource relative_to error",
                            )
                        })?
                        .to_str()
                        .ok_or_else(OriginalAuthorityError::unavailable)?;
                    let key = Text::from_str(relative);
                    actual.insert(key.clone());
                    let Some((size, digest)) = expected.get(&key) else {
                        return Err(BindingFailure::Original(raw(
                            crate::bootstrap_review::DENIED,
                            format!("packaged Profile resource is unlisted: {relative}"),
                        )));
                    };
                    let bytes = read_file(&op, &path, &self.budget)?;
                    charge_work(
                        &self.budget,
                        u64::try_from(bytes.value.len()).map_err(|_| {
                            OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
                        })?,
                    )?;
                    if *size != bytes.value.len() as u64 || format_digest(&bytes.value) != *digest {
                        return Err(BindingFailure::Original(raw(
                            crate::bootstrap_review::DENIED,
                            format!("packaged Profile resource is not launcher-bound: {relative}"),
                        )));
                    }
                }
                // LIFO reversed preserves os.walk's native directory order.
                pending.extend(dirs.into_iter().rev());
            }
            let _ = prefix;
        }
        let prefixes = [&self.selected, &artifacts].map(|path| {
            path.strip_prefix(&self.runtime)
                .ok()
                .and_then(Path::to_str)
                .map(|s| Text::from_str(&format!("{s}/")))
        });
        let expected_paths: std::collections::BTreeSet<_> = expected
            .keys()
            .filter(|path| {
                prefixes
                    .iter()
                    .flatten()
                    .any(|prefix| path.0.starts_with(&prefix.0))
            })
            .cloned()
            .collect();
        if actual != expected_paths {
            return Err(BindingFailure::Denied(
                "packaged Profile resource inventory is incomplete",
            ));
        }
        Ok(())
    }
}

fn strict(args: &tobkiri_canon::CoreArray<'_>) -> bool {
    args.len() == 1 && args.get(0).and_then(|v| v.as_bool()) == Some(true)
}
fn write_bool(
    out: &mut CoreBufferBuilder,
    value: bool,
) -> std::result::Result<(), tobkiri_canon::AllocationError> {
    out.append(if value {
        b"{\"kind\":\"bool\",\"value\":true}"
    } else {
        b"{\"kind\":\"bool\",\"value\":false}"
    })
}
fn write_path(
    out: &mut CoreBufferBuilder,
    binding: &CoreValue,
    path: &Path,
    steps: Option<&CoreValue>,
    budget: &Arc<dyn AllocationBudget>,
) -> anyhow::Result<()> {
    out.append(b"{\"kind\":\"path\",\"binding\":")?;
    out.append(binding.encode_guest(budget)?.as_bytes())?;
    out.append(b",\"value\":")?;
    crate::bootstrap_metadata::string_parts(
        out,
        [path
            .to_str()
            .ok_or_else(OriginalAuthorityError::unavailable)?],
    )?;
    out.append(b",\"steps\":")?;
    if let Some(steps) = steps {
        out.append(steps.encode_guest(budget)?.as_bytes())?;
    } else {
        out.append(b"[]")?;
    }
    out.append(b"}")?;
    Ok(())
}
pub(crate) fn write_bytes(
    out: &mut CoreBufferBuilder,
    bytes: &[u8],
) -> std::result::Result<(), tobkiri_canon::AllocationError> {
    out.append(b"{\"kind\":\"bytes\",\"value\":\"")?;
    append_base64(out, bytes)?;
    out.append(b"\"}")
}
pub(crate) fn append_base64(
    out: &mut CoreBufferBuilder,
    bytes: &[u8],
) -> std::result::Result<(), tobkiri_canon::AllocationError> {
    let mut encoded = [0_u8; 4096];
    for chunk in bytes.chunks(3072) {
        let count = base64::engine::general_purpose::STANDARD
            .encode_slice(chunk, &mut encoded)
            .map_err(|_| tobkiri_canon::AllocationError::Limit)?;
        out.append(&encoded[..count])?;
    }
    Ok(())
}

fn format_digest(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut result = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        let _ = write!(result, "{byte:02x}");
    }
    result
}
#[cfg(unix)]
fn scan_directory<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    current: &Path,
    pending: usize,
    capacity: &NativeSetupReservation,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<(Vec<PathBuf>, Vec<PathBuf>)>> {
    use std::os::unix::ffi::OsStrExt as _;
    op.check(OriginalAuthorityStage::Source)?;
    let name = CString::new(current.as_os_str().as_bytes())
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    // Reuse the already-proved DIR close/uncertainty ledger, rather than an
    // unobserved std::ReadDir Drop. No descriptor reaches the guest.
    let Some(pointer) = std::ptr::NonNull::new(unsafe { libc::opendir(name.as_ptr()) }) else {
        return Ok(None);
    };
    op.cleanup.files.fetch_add(1, Ordering::AcqRel);
    let directory = TrackedDirectory {
        pointer,
        state: op.cleanup.clone(),
    };
    let before_uncertain = op.cleanup.uncertain.load(Ordering::Acquire);
    let read = (|| {
        let mut dirs = Vec::new();
        let mut files = Vec::new();
        loop {
            op.check(OriginalAuthorityStage::Source)?;
            set_errno_zero();
            let entry = unsafe { libc::readdir(directory.pointer.as_ptr()) };
            if entry.is_null() {
                if std::io::Error::last_os_error().raw_os_error() != Some(0) {
                    return Ok(None);
                }
                break;
            }
            let name = unsafe { std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
            if matches!(name, b"." | b"..") {
                continue;
            }
            charge_work(budget, name.len() as u64 + 1)?;
            let count = pending
                .checked_add(dirs.len())
                .and_then(|n| n.checked_add(files.len()))
                .and_then(|n| n.checked_add(2))
                .and_then(|n| n.checked_mul(32768))
                .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            capacity
                .resize(count as u64)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            let path = current.join(std::ffi::OsStr::from_bytes(name));
            if path.is_dir() {
                dirs.push(path);
            } else {
                files.push(path);
            }
        }
        Ok(Some((dirs, files)))
    })();
    // os.walk closes scandir BEFORE yielding names/starting the file loop.
    drop(directory);
    if !before_uncertain && op.cleanup.uncertain.load(Ordering::Acquire) {
        return Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Close,
            "catalog directory close was not confirmed",
        ));
    }
    read
}
#[cfg(not(unix))]
fn scan_directory<S: crate::BootstrapReviewOwner>(
    _: &Operation<'_, S>,
    _: &Path,
    _: usize,
    _: &NativeSetupReservation,
    _: &Arc<dyn AllocationBudget>,
) -> Result<Option<(Vec<PathBuf>, Vec<PathBuf>)>> {
    Err(OriginalAuthorityError::unavailable())
}

#[cfg(unix)]
fn read_file<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    path: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    op.check(OriginalAuthorityStage::Source)?;
    use std::os::unix::fs::OpenOptionsExt as _;
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .map_err(|error| raw_io(error, path.to_str()))?;
    let mut file = TrackedFile::new(file, op.cleanup.clone());
    let read = (|| {
        let metadata = file.metadata().map_err(|error| raw_io(error, None))?;
        // CPython FileIO rejects an opened directory at its constructor's
        // fstat point and supplies the original filename. This is its semantic
        // EISDIR construction from the actual FD type, not a read errno.
        if metadata.is_dir() {
            return Err(raw_io(
                std::io::Error::from_raw_os_error(libc::EISDIR),
                path.to_str(),
            ));
        }
        if !metadata.is_file() && !metadata.is_dir() {
            return Err(OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Source,
                "non-regular catalog resource",
            ));
        }
        read_resource(op, &mut file, metadata.len(), budget)
    })();
    let close = file.close().map_err(|error| raw_io(error, None));
    finish_scope(read, close)
}
#[cfg(unix)]
fn read_resource<S: crate::BootstrapReviewOwner>(
    op: &Operation<'_, S>,
    file: &mut TrackedFile,
    size: u64,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    const LIMIT: usize = 32 * 1024 * 1024;
    let size = usize::try_from(size)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    if size > LIMIT {
        return Err(OriginalAuthorityError::capacity(
            OriginalAuthorityStage::Source,
        ));
    }
    let initial = size.saturating_add(1).clamp(1, LIMIT);
    let capacity = op.reserve(OriginalAuthorityStage::Source, initial)?;
    let mut bytes = Zeroizing::new(Vec::new());
    bytes
        .try_reserve_exact(initial)
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
    if bytes.capacity() != initial {
        return Err(OriginalAuthorityError::capacity(
            OriginalAuthorityStage::Source,
        ));
    }
    let mut scratch = [0_u8; 8192];
    loop {
        op.check(OriginalAuthorityStage::Source)?;
        let count = loop {
            match file.read(&mut scratch) {
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {
                    op.check(OriginalAuthorityStage::Source)?
                }
                result => break result.map_err(|error| raw_io(error, None))?,
            }
        };
        if count == 0 {
            break;
        }
        let needed = bytes
            .len()
            .checked_add(count)
            .filter(|n| *n <= LIMIT)
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        if needed > bytes.capacity() {
            let next = bytes
                .capacity()
                .checked_mul(2)
                .map(|n| n.max(needed).min(LIMIT))
                .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            capacity
                .resize(bytes.capacity().checked_add(next).ok_or_else(|| {
                    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
                })? as u64)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            charge_work(budget, bytes.len() as u64 * 3 + count as u64)?;
            let mut replacement = Zeroizing::new(Vec::new());
            replacement
                .try_reserve_exact(next)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
            if replacement.capacity() != next {
                return Err(OriginalAuthorityError::capacity(
                    OriginalAuthorityStage::Source,
                ));
            }
            replacement.extend_from_slice(&bytes);
            drop(std::mem::replace(&mut bytes, replacement));
            capacity
                .resize(bytes.capacity() as u64)
                .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))?;
        } else {
            charge_work(budget, count as u64 * 3)?;
        }
        bytes.extend_from_slice(&scratch[..count]);
    }
    Ok(Funded {
        value: bytes,
        _capacity: capacity,
    })
}
#[cfg(not(unix))]
fn read_file<S: crate::BootstrapReviewOwner>(
    _: &Operation<'_, S>,
    _: &Path,
    _: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    Err(OriginalAuthorityError::unavailable())
}
fn charge_work(budget: &Arc<dyn AllocationBudget>, work: u64) -> Result<()> {
    budget
        .reserve(tobkiri_canon::AllocationRequest {
            kind: tobkiri_canon::AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work,
        })
        .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Source))
}
