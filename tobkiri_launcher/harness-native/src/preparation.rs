//! One original invocation for Engine creation, source reading and compilation.
//! Source handles/digests prove no authorization; the caller must supply its
//! actually admitted source and retain that original input owner's custody.
use crate::{BootstrapInputOwner, BootstrapModuleIndex, NativeBootstrap};
use anyhow::{Result, ensure};
use std::{fs::File, io::Read, time::Instant};
use tobkiri_canon::Digest;
use tobkiri_gate::{CapacityKind, ControlKind, NativeSetupInvocation};
use tobkiri_wasm::{NativeDataError, WasmEngine};
use wasmtime::{OptLevel, component::Component};

/// Actual cold preparation values. These do not claim compiler heap/RSS or
/// compiled metadata is completely measured by the source buffer reservation.
#[derive(Clone, Copy, Debug)]
pub struct NativePreparationObservation {
    /// Time from the caller's original operation start, including Engine creation.
    pub elapsed_micros: u128,
    /// Actual peak payload charge on that same original account.
    pub peak_charged_bytes: u64,
    /// Bytes actually read from the caller's selected file.
    pub source_bytes: u64,
    /// The original absolute deadline, never a new execution interval.
    pub deadline: Instant,
    /// The actual compiler choice used by this preparation path.
    pub optimization: OptLevel,
}

/// Actual source owner and operation, before the single cold compile attempt.
/// There is no begin/retry/reset API and no caller-selected policy override.
pub struct NativeComponentPreparation<S: BootstrapInputOwner> {
    source: S,
    invocation: NativeSetupInvocation,
    engine: WasmEngine,
    deadline: Instant,
}

/// The same Engine, component, source owner and original operation after compile.
pub struct PreparedNativeComponent<S: BootstrapInputOwner> {
    preparation: NativeComponentPreparation<S>,
    component: Component,
    observation: NativePreparationObservation,
}

impl<S: BootstrapInputOwner> NativeComponentPreparation<S> {
    /// The caller must begin the real operation BEFORE entering this function.
    /// Both the actual app adapter and CI use this same composition choice.
    pub fn new(invocation: NativeSetupInvocation, source: S) -> Result<Self> {
        let deadline = invocation.deadline()?;
        source.check_live()?;
        ensure!(
            Instant::now() < deadline,
            "original native preparation deadline expired"
        );
        let engine =
            WasmEngine::new_with_control_and_optimization(invocation.control(), OptLevel::None)?;
        let preparation = Self {
            source,
            invocation,
            engine,
            deadline,
        };
        preparation.check_live()?;
        Ok(preparation)
    }

    fn check_live(&self) -> Result<()> {
        self.source.check_live()?;
        ensure!(
            self.invocation.deadline()? == self.deadline && Instant::now() < self.deadline,
            "original native deadline exhausted during component preparation"
        );
        Ok(())
    }

    /// Consume the single attempt. The expected digest checks byte integrity,
    /// not source rights, signatures, permissions or native effect authority.
    pub fn compile(self, mut input: File, expected: &Digest) -> Result<PreparedNativeComponent<S>> {
        self.check_live()?;
        let metadata = input.metadata()?;
        let size = usize::try_from(metadata.len())?;
        ensure!(
            metadata.is_file() && size > 0 && size <= 32 * 1024 * 1024,
            "component exceeds source bound or is not a regular file"
        );
        let payload = self
            .invocation
            .reserve(CapacityKind::VfsBuffer, u64::try_from(size)?)?;
        let permit = self
            .invocation
            .control()
            .reserve(ControlKind::Frame, u64::try_from(size)?)?;
        let mut bytes = Vec::new();
        bytes.try_reserve_exact(size)?;
        ensure!(
            bytes.capacity() == size,
            "unsupported source allocator capacity"
        );
        bytes.resize(size, 0);
        input.read_exact(&mut bytes)?;
        self.check_live()?;
        ensure!(
            input.metadata()?.len() == metadata.len() && &Digest::hash(&bytes) == expected,
            "component changed"
        );
        let compiled = Component::new(self.engine.engine(), &bytes);
        // The actual source allocation dies before either prepaid reservation.
        drop(bytes);
        drop(payload);
        drop(permit);
        drop(input);
        let elapsed_micros = self.invocation.started_at().elapsed().as_micros();
        let peak_charged_bytes = self.invocation.capacity_snapshot()?.peak_charged_bytes;
        let component = compiled.map_err(|original| {
            original.context(format!("native component compile failed after {elapsed_micros}us; peak_charged_bytes={peak_charged_bytes}"))
        })?;
        self.check_live().map_err(|original| {
            original.context(format!("native preparation elapsed_us={elapsed_micros}; peak_charged_bytes={peak_charged_bytes}"))
        })?;
        let observation = NativePreparationObservation {
            elapsed_micros,
            peak_charged_bytes,
            source_bytes: u64::try_from(size)?,
            deadline: self.deadline,
            optimization: OptLevel::None,
        };
        Ok(PreparedNativeComponent {
            preparation: self,
            component,
            observation,
        })
    }
}

impl<S: BootstrapInputOwner> PreparedNativeComponent<S> {
    #[cfg(all(
        test,
        feature = "bootstrap-ci",
        target_os = "macos",
        target_arch = "aarch64"
    ))]
    pub(crate) fn into_diagnostic_parts(
        self,
    ) -> Result<(WasmEngine, Component, NativeSetupInvocation, S)> {
        self.preparation.check_live()?;
        Ok((
            self.preparation.engine,
            self.component,
            self.preparation.invocation,
            self.preparation.source,
        ))
    }

    /// Actual observations only; not an initialization success or permission.
    pub fn observation(&self) -> NativePreparationObservation {
        self.observation
    }

    /// Initialize using the SAME original operation and input owner. Store
    /// ownership retains the actual Engine/ticker through its physical lifetime.
    pub async fn initialize(
        self,
        index: &'static BootstrapModuleIndex,
    ) -> Result<std::result::Result<NativeBootstrap<S>, NativeDataError>> {
        self.preparation.check_live()?;
        NativeBootstrap::prepare(
            &self.preparation.engine,
            &self.component,
            self.preparation.invocation,
            self.preparation.source,
            index,
        )
        .await
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use crate::BootstrapRoots;
    use std::{
        io::Write,
        path::PathBuf,
        sync::{
            Arc,
            atomic::{AtomicBool, Ordering},
        },
    };
    use tobkiri_core::RuntimeOwner;
    use tobkiri_wasm::NativeDataIoError;

    // Controlled source custody only. This fixture grants no inspection,
    // Profile, approval or effect authority.
    struct Source {
        root: PathBuf,
        _directory: File,
        retired: Arc<AtomicBool>,
    }
    impl BootstrapInputOwner for Source {
        fn with_roots<R>(
            &self,
            use_roots: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
        ) -> std::result::Result<R, NativeDataIoError> {
            self.check_live()?;
            Ok(use_roots(BootstrapRoots {
                runtime: &self.root,
                working_directory: &self.root,
                development_workspace: None,
            }))
        }
        fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
            if self.retired.load(Ordering::Acquire) {
                Err(NativeDataIoError::Retired)
            } else {
                Ok(())
            }
        }
    }
    fn source(root: &std::path::Path, retired: &Arc<AtomicBool>) -> Result<Source> {
        Ok(Source {
            root: root.to_path_buf(),
            _directory: File::open(root)?,
            retired: retired.clone(),
        })
    }

    #[tokio::test(flavor = "current_thread")]
    async fn rejected_sources_release_payload_without_restarting_the_clock() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().canonicalize()?;
        let owner = RuntimeOwner::open(root.join("data"))?;
        let retired = Arc::new(AtomicBool::new(false));
        let valid = wat::parse_str("(component)")?;
        for (name, length) in [
            ("empty", 0),
            ("oversize", 32 * 1024 * 1024 + 1),
            ("digest", 8),
        ] {
            let path = root.join(name);
            File::create(&path)?.set_len(length)?;
            if name == "digest" {
                File::create(&path)?.write_all(&valid)?;
            }
            let invocation = owner.begin_native_setup()?;
            let deadline = invocation.deadline()?;
            let preparation =
                NativeComponentPreparation::new(invocation.clone(), source(&root, &retired)?)?;
            let result = preparation.compile(File::open(path)?, &Digest::hash(b"wrong source"));
            let error = result
                .err()
                .ok_or_else(|| anyhow::anyhow!("invalid source admitted: {name}"))?;
            assert!(
                error.to_string().contains(if name == "digest" {
                    "component changed"
                } else {
                    "component exceeds source bound"
                }),
                "source failed for an unrelated reason: {name}: {error:#}"
            );
            assert_eq!(invocation.deadline()?, deadline);
            assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 0);
            tokio::task::yield_now().await;
        }
        let path = root.join("retired");
        File::create(&path)?.write_all(&valid)?;
        let invocation = owner.begin_native_setup()?;
        let preparation = NativeComponentPreparation::new(invocation, source(&root, &retired)?)?;
        retired.store(true, Ordering::Release);
        let result = preparation.compile(File::open(path)?, &Digest::hash(&valid));
        let failure = result
            .err()
            .ok_or_else(|| anyhow::anyhow!("revoked owner ignored"))?;
        assert!(
            matches!(
                failure.downcast_ref::<NativeDataIoError>(),
                Some(NativeDataIoError::Retired)
            ),
            "lost original source revocation: {failure:#}"
        );
        Ok(())
    }

    #[tokio::test(flavor = "current_thread")]
    async fn prepared_component_retains_source_and_original_deadline() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().canonicalize()?;
        let owner = RuntimeOwner::open(root.join("data"))?;
        let retired = Arc::new(AtomicBool::new(false));
        let bytes = wat::parse_str("(component)")?;
        let path = root.join("component.wasm");
        File::create(&path)?.write_all(&bytes)?;
        let invocation = owner.begin_native_setup()?;
        let deadline = invocation.deadline()?;
        let prepared =
            NativeComponentPreparation::new(invocation.clone(), source(&root, &retired)?)?
                .compile(File::open(path)?, &Digest::hash(&bytes))?;
        let observation = prepared.observation();
        assert_eq!(observation.optimization, OptLevel::None);
        assert_eq!(observation.deadline, deadline);
        assert_eq!(invocation.deadline()?, deadline);
        assert_eq!(observation.source_bytes, u64::try_from(bytes.len())?);
        assert!(observation.peak_charged_bytes >= observation.source_bytes);
        assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 0);
        assert_eq!(Arc::strong_count(&retired), 2);
        drop(prepared);
        assert_eq!(Arc::strong_count(&retired), 1);
        Ok(())
    }
}
