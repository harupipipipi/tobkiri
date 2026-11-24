//! One original invocation for Engine creation, source reading and compilation.
//! Source handles/digests prove no authorization; the caller must supply its
//! actually admitted source and retain that original input owner's custody.
use crate::{BootstrapInputOwner, BootstrapModuleIndex, NativeBootstrap};
use anyhow::{Result, ensure};
use std::{fs::File, io::Read, time::Instant};
use tobkiri_canon::{CoreBuffer, Digest};
use tobkiri_gate::{CapacityKind, ControlKind, CoreCancellation, NativeSetupInvocation};
use tobkiri_wasm::{
    NativeDataBinding, NativeDataError, NativeDataIoError, NativeDataObservation,
    NativeDataProgram, NativeDataValue, WasmEngine,
};
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
pub struct NativeComponentPreparation<S: NativeComponentOwner> {
    source: S,
    invocation: NativeSetupInvocation,
    engine: WasmEngine,
    deadline: Instant,
    cancellation: CoreCancellation,
}

/// The same Engine, component, source owner and original operation after compile.
pub struct PreparedNativeComponent<S: NativeComponentOwner> {
    preparation: NativeComponentPreparation<S>,
    component: Component,
    observation: NativePreparationObservation,
}

/// Retain an actual native caller's liveness through preparation and cleanup.
/// This is neither artifact admission nor permission to read any path.
pub trait NativeComponentOwner: Send + Sync + 'static {
    /// Validate the same retained native owner without selecting roots or IO.
    fn check_component_live(&self) -> std::result::Result<(), NativeDataIoError>;
}

impl<S: BootstrapInputOwner> NativeComponentOwner for S {
    fn check_component_live(&self) -> std::result::Result<(), NativeDataIoError> {
        BootstrapInputOwner::check_live(self)
    }
}

impl<S: NativeComponentOwner> NativeComponentPreparation<S> {
    /// The caller must begin the real operation BEFORE entering this function.
    /// Both the actual app adapter and CI use this same composition choice.
    pub fn new(invocation: NativeSetupInvocation, source: S) -> Result<Self> {
        Self::new_inner(invocation, source, None)
    }

    /// Preserve an actual native owner's cancellation without changing accounts.
    /// The funded child rejects foreign/inert parents and cannot cancel them.
    pub fn new_with_cancellation_parent(
        invocation: NativeSetupInvocation,
        source: S,
        parent: &CoreCancellation,
    ) -> Result<Self> {
        Self::new_inner(invocation, source, Some(parent))
    }

    fn new_inner(
        invocation: NativeSetupInvocation,
        source: S,
        parent: Option<&CoreCancellation>,
    ) -> Result<Self> {
        let deadline = invocation.deadline()?;
        source.check_component_live()?;
        ensure!(
            Instant::now() < deadline,
            "original native preparation deadline expired"
        );
        let cancellation = match parent {
            Some(parent) => parent.child_with_control(invocation.control())?,
            None => CoreCancellation::new_with_control(invocation.control())?,
        };
        ensure!(
            !cancellation.is_cancelled(),
            "native preparation cancelled: {:?}",
            cancellation.cause()
        );
        let engine =
            WasmEngine::new_with_control_and_optimization(invocation.control(), OptLevel::None)?;
        let preparation = Self {
            source,
            invocation,
            engine,
            deadline,
            cancellation,
        };
        preparation.check_live()?;
        Ok(preparation)
    }

    /// Reduction-only handle retained across compile and the eventual Store.
    /// Signalling it does not prove worker completion or physical release.
    pub fn cancellation(&self) -> &CoreCancellation {
        &self.cancellation
    }

    fn check_live(&self) -> Result<()> {
        self.source.check_component_live()?;
        ensure!(
            self.invocation.deadline()? == self.deadline && Instant::now() < self.deadline,
            "original native deadline exhausted during component preparation"
        );
        ensure!(
            !self.cancellation.is_cancelled(),
            "native preparation cancelled: {:?}",
            self.cancellation.cause()
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

impl<S: NativeComponentOwner> PreparedNativeComponent<S> {
    #[cfg(all(
        test,
        feature = "thread-capabilities-ci",
        target_os = "macos",
        target_arch = "aarch64"
    ))]
    pub(crate) fn into_diagnostic_parts(
        self,
    ) -> Result<(
        WasmEngine,
        Component,
        NativeSetupInvocation,
        S,
        CoreCancellation,
    )> {
        self.preparation.check_live()?;
        Ok((
            self.preparation.engine,
            self.component,
            self.preparation.invocation,
            self.preparation.source,
            self.preparation.cancellation,
        ))
    }

    /// Actual observations only; not an initialization success or permission.
    pub fn observation(&self) -> NativePreparationObservation {
        self.observation
    }

    /// Consume this preparation into one pure DATA program with no IO imports.
    /// The caller supplies fixed typed exports; no App operation enters Core's
    /// bootstrap binding table. Artifact admission remains the caller's duty.
    pub async fn prepare_data_program(
        self,
        bindings: &[NativeDataBinding<'_>],
    ) -> Result<PreparedNativeDataProgram<S>> {
        self.preparation.check_live()?;
        let preparation = self.preparation;
        let store = preparation
            .engine
            .native_setup_store_with_cancellation_parent(
                preparation.invocation,
                (),
                &preparation.cancellation,
            )?;
        let program = store
            .prepare_data_program(&self.component, bindings)
            .await?;
        preparation.source.check_component_live()?;
        ensure!(
            !preparation.cancellation.is_cancelled(),
            "native preparation cancelled: {:?}",
            preparation.cancellation.cause()
        );
        ensure!(
            Instant::now() < program.original_deadline(),
            "original preparation deadline expired"
        );
        Ok(PreparedNativeDataProgram {
            program,
            source: preparation.source,
        })
    }

    /// Initialize using the SAME original operation and input owner. Store
    /// ownership retains the actual Engine/ticker through its physical lifetime.
    pub async fn initialize(
        self,
        index: &'static BootstrapModuleIndex,
    ) -> Result<std::result::Result<NativeBootstrap<S>, NativeDataError>>
    where
        S: BootstrapInputOwner,
    {
        self.preparation.check_live()?;
        let cancellation = self.preparation.cancellation;
        let result = NativeBootstrap::prepare_with_cancellation_parent(
            &self.preparation.engine,
            &self.component,
            self.preparation.invocation,
            self.preparation.source,
            index,
            &cancellation,
        )
        .await;
        // Never replace an actual initialization/refusal failure with a later
        // cancellation observation. A successful object is not published late.
        match result {
            Ok(Ok(runner)) if cancellation.is_cancelled() => {
                let original =
                    anyhow::anyhow!("native preparation cancelled: {:?}", cancellation.cause());
                match runner.finish().await {
                    Ok(()) => Err(original),
                    Err(cleanup) => {
                        Err(crate::BootstrapCleanupFailure { original, cleanup }.into())
                    }
                }
            }
            other => other,
        }
    }
}

/// One original Store and its actual source owner; no metadata roots required.
/// Store retirement precedes source-owner destruction, including cancellation.
pub struct PreparedNativeDataProgram<S: NativeComponentOwner> {
    program: NativeDataProgram<()>,
    source: S,
}

impl<S: NativeComponentOwner> PreparedNativeDataProgram<S> {
    /// Invoke a previously bound slot without resetting the original account.
    pub async fn invoke(
        &mut self,
        slot: usize,
        input: Option<&CoreBuffer>,
    ) -> Result<std::result::Result<NativeDataValue, NativeDataError>> {
        if let Err(error) = self.source.check_component_live() {
            self.program.retire();
            return Err(error.into());
        }
        let result = self.program.invoke(slot, input).await;
        if let Err(error) = self.source.check_component_live() {
            drop(result);
            self.program.retire();
            return Err(error.into());
        }
        result
    }

    /// Original preparation deadline, including all prior calls.
    pub fn original_deadline(&self) -> Instant {
        self.program.original_deadline()
    }
    /// Actual remaining fuel in that same Store.
    pub fn fuel_remaining(&self) -> Result<u64> {
        self.program.fuel_remaining()
    }
    /// Actual last-call observations, never approval or readiness evidence.
    pub fn observation(&self) -> NativeDataObservation {
        self.program.observation()
    }
    /// Physically retire the Store; there are no imported IO workers to await.
    pub fn retire(&mut self) {
        self.program.retire();
    }
}

impl<S: NativeComponentOwner> Drop for PreparedNativeDataProgram<S> {
    fn drop(&mut self) {
        self.program.retire();
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

    #[tokio::test(flavor = "current_thread")]
    async fn cancellation_keeps_source_custody_and_refuses_late_compile_or_handoff() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().canonicalize()?;
        let owner = RuntimeOwner::open(root.join("data"))?;
        let retired = Arc::new(AtomicBool::new(false));
        let bytes = data_component()?;
        let path = root.join("cancelled.wasm");
        File::create(&path)?.write_all(&bytes)?;
        for after_compile in [false, true] {
            let invocation = owner.begin_native_setup()?;
            let deadline = invocation.deadline()?;
            let parent = CoreCancellation::new_with_control(invocation.control())?;
            let preparation = NativeComponentPreparation::new_with_cancellation_parent(
                invocation.clone(),
                source(&root, &retired)?,
                &parent,
            )?;
            assert!(preparation.cancellation().is_direct_child_of(&parent));
            let failure = if after_compile {
                let prepared = preparation.compile(File::open(&path)?, &Digest::hash(&bytes))?;
                parent.cancel();
                prepared.prepare_data_program(&[]).await.err()
            } else {
                parent.cancel();
                preparation
                    .compile(File::open(&path)?, &Digest::hash(&bytes))
                    .err()
            }
            .ok_or_else(|| anyhow::anyhow!("cancelled preparation published"))?;
            assert!(failure.to_string().contains("native preparation cancelled"));
            assert_eq!(invocation.deadline()?, deadline);
            assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 0);
            assert_eq!(Arc::strong_count(&retired), 1);
        }
        Ok(())
    }

    // Controlled owner of the selected test artifact, deliberately no roots API.
    // This fixture proves DATA custody, not product artifact admission or GUI IO.
    struct DataSource {
        retired: Arc<AtomicBool>,
        _directory: File,
    }
    impl NativeComponentOwner for DataSource {
        fn check_component_live(&self) -> std::result::Result<(), NativeDataIoError> {
            if self.retired.load(Ordering::Acquire) {
                Err(NativeDataIoError::Retired)
            } else {
                Ok(())
            }
        }
    }
    fn data_component() -> Result<Vec<u8>> {
        Ok(wat::parse_str(
            r#"(component
            (core module $m
                (memory (export "memory") 1)
                (data (i32.const 32) "x")
                (func (export "realloc") (param i32 i32 i32 i32) (result i32) i32.const 64)
                (func (export "view") (result i32)
                    i32.const 0 i32.const 32 i32.store
                    i32.const 4 i32.const 1 i32.store i32.const 0)
                (func (export "post") (param i32)))
            (core instance $i (instantiate $m))
            (alias core export $i "memory" (core memory $memory))
            (alias core export $i "realloc" (core func $realloc))
            (alias core export $i "view" (core func $view))
            (alias core export $i "post" (core func $post))
            (func (export "view") (result string)
                (canon lift (core func $view) (memory $memory)
                    (realloc $realloc) (post-return $post))))"#,
        )?)
    }

    #[tokio::test(flavor = "current_thread")]
    async fn pure_data_handoff_keeps_one_account_deadline_fuel_and_owner() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().canonicalize()?;
        let owner = RuntimeOwner::open(root.join("data"))?;
        let retired = Arc::new(AtomicBool::new(false));
        let bytes = data_component()?;
        let path = root.join("data.wasm");
        File::create(&path)?.write_all(&bytes)?;
        let invocation = owner.begin_native_setup()?;
        let deadline = invocation.deadline()?;
        let parent = CoreCancellation::new_with_control(invocation.control())?;
        let prepared = NativeComponentPreparation::new_with_cancellation_parent(
            invocation.clone(),
            DataSource {
                retired: retired.clone(),
                _directory: File::open(&root)?,
            },
            &parent,
        )?
        .compile(File::open(path)?, &Digest::hash(&bytes))?;
        let binding = [NativeDataBinding {
            export: tobkiri_wasm::NativeDataExport::new(None, "view"),
            result: None,
        }];
        let mut program = prepared.prepare_data_program(&binding).await?;
        let mut fuel = program.fuel_remaining()?;
        assert!(fuel > 0 && fuel <= 2_000_000_000);
        assert!(invocation.capacity_snapshot()?.charged_bytes > 0);
        for _ in 0..2 {
            let reply = program
                .invoke(0, None)
                .await?
                .map_err(|_| anyhow::anyhow!("unexpected domain error"))?;
            let NativeDataValue::String(text) = reply else {
                anyhow::bail!("expected DATA string");
            };
            assert_eq!(text.as_str()?, "x");
            assert!(program.fuel_remaining()? < fuel);
            fuel = program.fuel_remaining()?;
            assert_eq!(program.original_deadline(), deadline);
        }
        assert_eq!(Arc::strong_count(&retired), 2);
        parent.cancel();
        drop(parent);
        // The consumed preparation's retained child still sees cancellation.
        // Epoch execution keeps its existing grace, but new result storage is
        // refused immediately by the original allocation owner's cancel check.
        let error = program
            .invoke(0, None)
            .await
            .err()
            .ok_or_else(|| anyhow::anyhow!("consumed preparation lost cancellation"))?;
        let failure = format!("{error:#}");
        assert!(failure.contains("result-storage"), "{failure}");
        assert!(
            failure.contains("canonical host allocation unavailable"),
            "{failure}"
        );
        assert!(program.observation().store_retired);
        drop(program);
        tobkiri_wasm::reap_allocation_owners()?;
        assert_eq!(Arc::strong_count(&retired), 1);
        assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 0);
        Ok(())
    }

    #[tokio::test(flavor = "current_thread")]
    async fn pure_data_owner_revocation_retires_the_same_store_permanently() -> Result<()> {
        let temporary = tempfile::tempdir()?;
        let root = temporary.path().canonicalize()?;
        let owner = RuntimeOwner::open(root.join("data"))?;
        let retired = Arc::new(AtomicBool::new(false));
        let bytes = data_component()?;
        let path = root.join("revoked.wasm");
        File::create(&path)?.write_all(&bytes)?;
        let invocation = owner.begin_native_setup()?;
        let prepared = NativeComponentPreparation::new(
            invocation.clone(),
            DataSource {
                retired: retired.clone(),
                _directory: File::open(&root)?,
            },
        )?
        .compile(File::open(path)?, &Digest::hash(&bytes))?;
        let binding = [NativeDataBinding {
            export: tobkiri_wasm::NativeDataExport::new(None, "view"),
            result: None,
        }];
        let mut program = prepared.prepare_data_program(&binding).await?;
        retired.store(true, Ordering::Release);
        assert!(program.invoke(0, None).await.is_err());
        assert!(program.observation().store_retired);
        retired.store(false, Ordering::Release);
        assert!(program.invoke(0, None).await.is_err());
        drop(program);
        tobkiri_wasm::reap_allocation_owners()?;
        assert_eq!(invocation.capacity_snapshot()?.charged_bytes, 0);
        Ok(())
    }
}
