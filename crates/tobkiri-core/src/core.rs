use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tobkiri_canon::{
    Digest,
    ids::{PackId, ProfileId, RequestId, ShapeRef},
};
use tobkiri_gate::{
    ApprovalAnswer, ApprovalPrompt, ApprovalStore, CallCx, CoreControl, FrozenPortTarget,
    GateEngine, HostIo, MediatorChain, PackPolicy, PermissionSet, PortBindings, Principal,
    TrustedPrompt,
};
use tobkiri_manifest::VerifiedPack;
use tobkiri_profile::{CompiledProfile, Profile, compile};
use tobkiri_store::{Approvals, AuditLog, AuditQuota, DataRoot};

use crate::{
    AuditRunJournal, CoreError, Session,
    profiles::{Profiles, load_packs, old_hashes, published},
};

/// Generic Core configuration. Pack initialization is bound to verified artifact bytes.
#[derive(Default)]
pub struct CoreConfig {
    test_data_root: Option<PathBuf>,
}
impl CoreConfig {
    /// A test-only data root override is available in debug/test builds.
    #[cfg(debug_assertions)]
    pub fn for_test_data_root(path: PathBuf) -> Self {
        Self {
            test_data_root: Some(path),
        }
    }
}

/// Core owns runtime and Profile data; hosts only render trusted surfaces.
pub struct Core {
    host: Arc<dyn HostIo>,
    profiles: Profiles,
    admissions: crate::admission::Admissions,
    capacity: tobkiri_gate::CapacityLedger,
    /// One bounded native-control account shared by all Profile Gates,
    /// contexts, retained catalogs and execution records owned by this Core.
    control: CoreControl,
    filesystem: Option<Arc<crate::filesystem_registry::CoreFilesystem>>,
    /// Per-Core trusted process descriptor catalog: one bounded owner holding
    /// the append-only entry slots, the move-only launch key and every
    /// retained descriptor. Absent when initialization or the platform fails
    /// closed; its absence never weakens other Core services.
    processes: Option<crate::process_catalog::ProcessCatalog>,
    recovery: BTreeMap<ProfileId, ProfileRecoveryStatus>,
    #[cfg(test)]
    audit_timing: Option<Arc<crate::test_support::AuditTiming>>,
}

/// Read-only evidence from synchronous recovery. This never authorizes work;
/// startup still requires the asynchronous barrier and current durable status.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ProfileRecoveryStatus {
    Dormant,
    IntegrityFault {
        code: tobkiri_gate::IntegrityFaultCode,
    },
    RecoveryUnavailable {
        code: &'static str,
    },
}
fn recovery_status(error: &CoreError) -> ProfileRecoveryStatus {
    match error {
        CoreError::Gate(tobkiri_gate::GateError::Integrity(code))
        | CoreError::Store(tobkiri_store::StoreError::Integrity(code)) => {
            ProfileRecoveryStatus::IntegrityFault { code: *code }
        }
        CoreError::Gate(error) => ProfileRecoveryStatus::RecoveryUnavailable { code: error.code() },
        _ => ProfileRecoveryStatus::RecoveryUnavailable {
            code: "persistence",
        },
    }
}

struct Prompt(Arc<dyn HostIo>);
#[async_trait::async_trait]
impl TrustedPrompt for Prompt {
    async fn prompt_approval(&self, prompt: ApprovalPrompt) -> ApprovalAnswer {
        self.0.prompt_approval(prompt).await
    }
}

impl Core {
    /// Borrow the original account for sibling source-admission paths.
    pub(crate) fn source_control(&self) -> &CoreControl {
        &self.control
    }

    /// Open the in-process Core with no listener and no provider-specific route.
    pub fn open(config: CoreConfig, host: Arc<dyn HostIo>) -> Result<Self, CoreError> {
        let default = dirs::data_local_dir()
            .ok_or_else(|| CoreError::Invalid("platform data directory is unavailable".into()))?
            .join("Tobkiri");
        let root = Arc::new(DataRoot::open(
            config.test_data_root.as_ref().unwrap_or(&default),
        )?);
        let capacity = crate::capacity::shared(&root)?;
        let control = CoreControl::default();
        let filesystem =
            crate::filesystem_registry::CoreFilesystem::new(&root, control.clone()).ok();
        // The process owner shares THIS SAME CoreControl account; entropy or
        // platform failure leaves the component absent without weakening Core.
        let processes = crate::process_catalog::ProcessCatalog::new(control.clone()).ok();
        let mut core = Self {
            capacity,
            control,
            filesystem,
            processes,
            host,
            profiles: Profiles { root },
            admissions: crate::admission::Admissions::default(),
            recovery: BTreeMap::new(),
            #[cfg(test)]
            audit_timing: None,
        };
        let profiles = core.profile_ids()?;
        for profile in profiles {
            // Expire only fully verified persisted deadlines before opening the
            // audit sink. A corrupt record is never guessed or repaired here.
            let recovered = (|| {
                let approvals = Approvals::open(&core.profiles.root, &profile)?;
                let now = u64::try_from(
                    SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .map_err(|_| CoreError::Gate(tobkiri_gate::GateError::Unavailable))?
                        .as_millis(),
                )
                .map_err(|_| CoreError::Gate(tobkiri_gate::GateError::Unavailable))?;
                approvals.expire_terminal(&profile, now)?;
                if let tobkiri_gate::AdmissionStatus::IntegrityFault { code } =
                    approvals.admission_status(&profile)?
                {
                    return Err(tobkiri_gate::GateError::Integrity(code).into());
                }
                let (gate, _) = core.authority_for_id(&profile)?;
                core.admissions.install(profile.clone(), gate)?;
                Ok::<(), CoreError>(())
            })();
            let status = match recovered {
                Ok(()) => ProfileRecoveryStatus::Dormant,
                Err(error) => recovery_status(&error),
            };
            // Faulted profiles have no Gate/barrier here. Hosts may inspect
            // immutable profile documents while all effect routes stay denied.
            core.recovery.insert(profile, status);
        }
        Ok(core)
    }

    /// Register a trusted already-open directory for explicit configuration.
    /// This creates only an identity and rights ceiling, never an approval.
    /// Configure and approve that exact ID in the Profile before guest use.
    /// Native directory-selection/configuration UI is a separate host concern.
    pub fn register_filesystem_root(
        &self,
        directory: std::fs::File,
        rights: crate::FilesystemRights,
    ) -> Result<crate::FilesystemRoot, CoreError> {
        self.filesystem
            .as_ref()
            .ok_or(tobkiri_gate::GateError::Unavailable)?
            .register(directory, rights)
            .map_err(|error| CoreError::Runtime(error.to_string()))
    }

    /// Register a trusted already-open executable descriptor and a separate
    /// already-open working-directory descriptor under a bounded program id.
    /// Registration retains the original `File` objects, derives stat facts
    /// and a bounded streamed SHA-256 checked against the required expected
    /// digest, then re-observes both descriptors for drift.
    ///
    /// This records catalog facts only - never a launch, approval, jail, or
    /// exec-readiness proof. Descriptor custody, digest equality and a
    /// before/after sample do not prove immutable bytes, freedom from
    /// concurrent writers, effective access, caller liveness, current policy,
    /// or format/interpreter support. An absent cwd is not representable and
    /// is never substituted; `proc`/`pty` remain `Unavailable`.
    pub fn register_process_catalog_entry(
        &self,
        program_id: &str,
        executable: std::fs::File,
        expected_executable_digest: &Digest,
        catalog_label: &str,
        cwd: std::fs::File,
        cwd_label: &str,
    ) -> Result<(), crate::ProcessRegistrationError> {
        self.processes
            .as_ref()
            .ok_or(crate::ProcessRegistrationError::Unavailable)?
            .register(
                program_id,
                executable,
                expected_executable_digest,
                catalog_label,
                cwd,
                cwd_label,
            )
    }

    fn authority(&self, profile: &Profile) -> Result<(Arc<GateEngine>, Arc<AuditLog>), CoreError> {
        self.authority_for_id(&profile.id)
    }
    #[cfg(test)]
    pub(crate) fn set_audit_timing(&mut self, timing: Arc<crate::test_support::AuditTiming>) {
        self.audit_timing = Some(timing);
    }
    fn authority_for_id(
        &self,
        id: &ProfileId,
    ) -> Result<(Arc<GateEngine>, Arc<AuditLog>), CoreError> {
        let approvals = Arc::new(Approvals::open(&self.profiles.root, id)?);
        if let tobkiri_gate::AdmissionStatus::IntegrityFault { code } =
            approvals.admission_status(id)?
        {
            return Err(tobkiri_gate::GateError::Integrity(code).into());
        }
        let audit = match AuditLog::open(&self.profiles.root, id, AuditQuota::default()) {
            Ok(audit) => Arc::new(audit),
            Err(tobkiri_store::StoreError::Integrity(code)) => {
                // The domain latch precedes Gate construction so another Core
                // cannot admit work after audit-chain verification failed here.
                approvals.latch_integrity(id, code)?;
                return Err(tobkiri_store::StoreError::Integrity(code).into());
            }
            Err(error) => return Err(error.into()),
        };
        // The diagnostic wrapper exists only in unit tests and delegates to
        // this exact real sink. The journal still receives the concrete log.
        #[cfg(test)]
        let gate_audit: Arc<dyn tobkiri_gate::AuditSink> = match &self.audit_timing {
            Some(timing) => crate::test_support::timed_audit_sink(audit.clone(), timing.clone()),
            None => audit.clone(),
        };
        #[cfg(not(test))]
        let gate_audit = audit.clone();
        let gate = Arc::new(GateEngine::new_with_control(
            approvals,
            gate_audit,
            Arc::new(Prompt(self.host.clone())),
            MediatorChain::default(),
            self.control.clone(),
        ));
        Ok((gate, audit))
    }

    async fn ensure_admission(&self, id: &ProfileId) -> Result<(), CoreError> {
        if tokio::runtime::Handle::try_current().is_err() {
            return Err(CoreError::Gate(tobkiri_gate::GateError::Unavailable));
        }
        let (gate, _) = self.authority_for_id(id)?;
        self.admissions
            .install(id.clone(), gate)?
            .ensure_ready()
            .await
    }

    /// Inspect recovery without creating a Gate, Session, or guest authority.
    pub fn profile_recovery_status(
        &self,
        id: &ProfileId,
    ) -> Result<ProfileRecoveryStatus, CoreError> {
        let approvals = match Approvals::open(&self.profiles.root, id) {
            Ok(approvals) => approvals,
            Err(error) => {
                return self
                    .recovery
                    .get(id)
                    .copied()
                    .map_or_else(|| Err(error.into()), Ok);
            }
        };
        if let tobkiri_gate::AdmissionStatus::IntegrityFault { code } =
            approvals.admission_status(id)?
        {
            return Ok(ProfileRecoveryStatus::IntegrityFault { code });
        }
        Ok(self
            .recovery
            .get(id)
            .copied()
            .unwrap_or(ProfileRecoveryStatus::Dormant))
    }

    fn user_context(&self, profile: &Profile, gate: &GateEngine) -> Result<CallCx, CoreError> {
        let control = gate.control().clone();
        let cancellation = tobkiri_gate::CancellationToken::new_with_control(&control)
            .map_err(tobkiri_gate::GateError::from)?;
        Ok(CallCx::try_root_with_control(
            profile.id.clone(),
            Principal::User,
            RequestId::new(format!("req_{}", ulid::Ulid::new()))?,
            cancellation,
            Instant::now()
                .checked_add(Duration::from_millis(profile.ceiling.wasm.wall_ms))
                .ok_or_else(|| {
                    CoreError::Invalid("wall ceiling exceeds the supported clock range".into())
                })?,
            profile.ceiling.wasm.clone(),
            control,
        )?
        .with_security_epoch(gate.epoch(&profile.id)?))
    }

    /// Validate conflicts and obtain all required native approvals before
    /// installing any of this save's permission effects. A refusal during
    /// preparation leaves earlier approvals unapplied. Permission-effect commit
    /// and head/epoch commit still need a coordinated durable transaction;
    /// an error after mutation does not establish rollback.
    pub async fn save_profile(
        &self,
        profile: Profile,
        expected: Option<Digest>,
    ) -> Result<Digest, CoreError> {
        self.ensure_admission(&profile.id).await?;
        let current = self.profiles.head(&profile.id)?;
        if current != expected {
            return Err(CoreError::Invalid(
                "Profile head changed; reload before applying the edit".into(),
            ));
        }
        let previous = if current.is_some() {
            Some(self.profiles.load(&profile.id)?.0)
        } else {
            None
        };
        let packs = load_packs(&profile, &self.control)?;
        let compiled = compile(&profile, &packs).map_err(CoreError::Conflicts)?;
        let (gate, _) = self.authority(&profile)?;
        register_policies(&gate, &profile, &packs)?;
        register_views(&gate, &compiled)?;
        let cx = self
            .user_context(&profile, &gate)?
            .with_revision(compiled.plan_digest.clone());
        let old = old_hashes(previous.as_ref());
        // Admit the complete retention array before constructing it. Each
        // preparation separately retains its original reservation and the
        // actual owned policy capacities under this SAME CoreControl account.
        let approval_count = packs
            .len()
            .checked_add(1)
            .ok_or(tobkiri_gate::GateError::Unavailable)?;
        let preparation_bytes = approval_count
            .checked_mul(std::mem::size_of::<
                tobkiri_gate::PreparedPermissionApproval<'_>,
            >())
            .and_then(|bytes| {
                bytes.checked_add(std::mem::size_of::<
                    Vec<tobkiri_gate::PreparedPermissionApproval<'_>>,
                >())
            })
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(tobkiri_gate::GateError::Unavailable)?;
        let mut preparation_array = self
            .control
            .reserve(tobkiri_gate::ControlKind::Queue, preparation_bytes)
            .map_err(tobkiri_gate::GateError::from)?;
        let mut pack_approvals = Vec::new();
        pack_approvals
            .try_reserve_exact(approval_count)
            .map_err(|_| tobkiri_gate::GateError::Unavailable)?;
        let retained_array_bytes = pack_approvals
            .capacity()
            .checked_mul(std::mem::size_of::<
                tobkiri_gate::PreparedPermissionApproval<'_>,
            >())
            .and_then(|bytes| {
                bytes.checked_add(std::mem::size_of::<
                    Vec<tobkiri_gate::PreparedPermissionApproval<'_>>,
                >())
            })
            .and_then(|bytes| u64::try_from(bytes).ok())
            .ok_or(tobkiri_gate::GateError::Unavailable)?;
        preparation_array
            .try_grow(retained_array_bytes.saturating_sub(preparation_bytes))
            .map_err(tobkiri_gate::GateError::from)?;
        for pack in &packs {
            pack_approvals.push(
                gate.prepare_pack_permissions(
                    &cx,
                    &pack.manifest.id,
                    old.get(&pack.manifest.id),
                    &pack.hash,
                )
                .await?,
            );
        }
        let digest = self.profiles.write_revision(&profile)?;
        let target_epoch = cx
            .security_epoch()
            .checked_add(1)
            .ok_or_else(|| CoreError::Invalid("security epoch exhausted".into()))?;
        let user_approval = gate
            .prepare_user_permissions(
                &cx,
                &digest,
                &compiled.plan_digest,
                target_epoch,
                &profile
                    .user_permissions
                    .clone()
                    .unwrap_or_default()
                    .intersect(&profile.ceiling),
            )
            .await?;
        pack_approvals.push(user_approval);
        // Every prompt succeeds before policy installation. Pack and User
        // effects now share one transaction; the later head/epoch CAS still
        // requires a durable whole-save decision and recovery protocol.
        tobkiri_gate::PreparedPermissionApproval::commit_all(pack_approvals)?;
        self.profiles.root.compare_and_swap_profile_head_at_epoch(
            &profile.id,
            expected.as_ref(),
            cx.security_epoch(),
            &digest,
        )?;
        Ok(digest)
    }

    /// Load a Profile and its current revision digest.
    pub fn profile(&self, id: &ProfileId) -> Result<(Profile, Digest), CoreError> {
        self.profiles.load(id)
    }

    /// Enumerate sorted Profile directory identities within the existing
    /// 4096-entry recovery bound. This read-only inventory does not load or
    /// approve a revision, create a Session, select an active Profile or prove
    /// readiness. Call `profile` separately to inspect each current revision.
    pub fn profile_ids(&self) -> Result<Vec<ProfileId>, CoreError> {
        self.profiles
            .root
            .list_profiles_bounded(4096)
            .map_err(|error| match error {
                tobkiri_store::StoreError::Quota => {
                    CoreError::Invalid("Profile recovery registry is full".into())
                }
                error => error.into(),
            })
    }

    /// Create a fresh Profile from a structural definition, with no inherited
    /// authority or data even if the definition contains a previous identity.
    pub async fn create_profile(&self, bytes: &[u8], name: String) -> Result<ProfileId, CoreError> {
        let id = ProfileId::new(format!("prof_{}", ulid::Ulid::new()))?;
        let mut profile = match tobkiri_profile::parse_profile(bytes) {
            Ok(mut profile) => {
                profile.id = id.clone();
                profile.name = name;
                profile
            }
            Err(_) => published(bytes)?.to_profile(id.clone(), name),
        };
        profile.id = id.clone();
        self.save_profile(profile, None).await?;
        Ok(id)
    }

    /// Add or update an independently verified artifact pin. Conflict checking
    /// happens before permissions or the current head can change.
    pub async fn add_pack(
        &self,
        id: &ProfileId,
        source: String,
        update: bool,
    ) -> Result<Digest, CoreError> {
        let (mut profile, head) = self.profile(id)?;
        let path = std::path::Path::new(source.strip_prefix("file:").unwrap_or(&source));
        let source_catalog = tobkiri_manifest::source::SourceCatalog::new(&self.control)?;
        let pack = if path.is_dir() {
            tobkiri_manifest::verify_directory_in_catalog(
                path,
                &profile.publishers,
                &source_catalog,
            )?
        } else {
            tobkiri_manifest::verify_tpk_file_in_catalog(
                path,
                &profile.publishers,
                &source_catalog,
            )?
        };
        let existing = profile
            .packs
            .iter()
            .position(|pin| pin.id == pack.manifest.id);
        if existing.is_some() != update {
            return Err(CoreError::Invalid(
                if update {
                    "Pack is not installed; use profile add"
                } else {
                    "Pack is already installed; use profile update"
                }
                .into(),
            ));
        }
        let pin = tobkiri_profile::PackPin {
            id: pack.manifest.id.clone(),
            version: pack.manifest.version.clone(),
            sha256: pack.hash.clone(),
            source,
        };
        if let Some(index) = existing {
            profile.packs[index] = pin;
        } else {
            profile.packs.push(pin);
        }
        self.save_profile(profile, Some(head)).await
    }

    /// Remove a pin only when the resulting required ports remain satisfied.
    pub async fn remove_pack(&self, id: &ProfileId, pack: &PackId) -> Result<Digest, CoreError> {
        let (mut profile, head) = self.profile(id)?;
        let before = profile.packs.len();
        profile.packs.retain(|pin| &pin.id != pack);
        if before == profile.packs.len() {
            return Err(CoreError::NotFound("Pack is not installed".into()));
        }
        profile.bindings.retain(|_, provider| provider != pack);
        self.save_profile(profile, Some(head)).await
    }

    /// Revisit the trusted approval surface for the current, validated Pack set.
    pub async fn approve_profile(&self, id: &ProfileId) -> Result<(), CoreError> {
        self.ensure_admission(id).await?;
        let (profile, profile_hash) = self.profile(id)?;
        let packs = load_packs(&profile, &self.control)?;
        let compiled = compile(&profile, &packs).map_err(CoreError::Conflicts)?;
        let (gate, _) = self.authority(&profile)?;
        register_policies(&gate, &profile, &packs)?;
        register_views(&gate, &compiled)?;
        let cx = self
            .user_context(&profile, &gate)?
            .with_revision(compiled.plan_digest.clone());
        for pack in &packs {
            gate.approve_pack_permissions(&cx, &pack.manifest.id, Some(&pack.hash), &pack.hash)
                .await?;
        }
        gate.approve_user_permissions(
            &cx,
            &profile_hash,
            &compiled.plan_digest,
            cx.security_epoch(),
            &profile
                .user_permissions
                .clone()
                .unwrap_or_default()
                .intersect(&profile.ceiling),
        )
        .await?;
        Ok(())
    }

    /// Set a per-Profile secret through the Core user API. Plaintext is absent
    /// from Profile revisions, exports, approval prompts and run journals.
    pub async fn set_secret(
        &self,
        id: &ProfileId,
        name: &str,
        bytes: &[u8],
    ) -> Result<(), CoreError> {
        self.ensure_admission(id).await?;
        self.profile(id)?;
        tobkiri_store::Vault::open_default(&self.profiles.root, id)?.set(name, bytes)?;
        Ok(())
    }

    /// Produce a bounded, structural conflict report for a proposed revision.
    pub fn check_profile(
        &self,
        profile: &Profile,
    ) -> Result<tobkiri_profile::ConflictReport, CoreError> {
        let packs = load_packs(profile, &self.control)?;
        let conflicts = compile(profile, &packs).err().unwrap_or_default();
        Ok(tobkiri_profile::ConflictReport {
            repair_pack_available: !conflicts.is_empty(),
            conflicts,
        })
    }
    /// Publish only the explicit structural Profile fields.
    pub fn export_profile(&self, id: &ProfileId) -> Result<Vec<u8>, CoreError> {
        self.profiles.export(id)
    }
    /// Import with a fresh identity and fresh authority. The chosen ceiling must
    /// be a subset of the published ceiling before any approval can occur.
    pub async fn import_profile(
        &self,
        bytes: &[u8],
        name: String,
        narrowing: Option<PermissionSet>,
    ) -> Result<ProfileId, CoreError> {
        let document = published(bytes)?;
        let id = ProfileId::new(format!("prof_{}", ulid::Ulid::new()))?;
        let mut profile = document.to_profile(id.clone(), name);
        if let Some(ceiling) = narrowing {
            if !ceiling.is_subset_of(&profile.ceiling) {
                return Err(CoreError::Invalid(
                    "import cannot widen the published permission ceiling".into(),
                ));
            }
            profile.ceiling = ceiling;
        }
        self.save_profile(profile, None).await?;
        Ok(id)
    }

    /// Instantiate all verified components, freeze bindings and start a Session.
    pub async fn start(&self, id: &ProfileId) -> Result<Session, CoreError> {
        self.ensure_admission(id).await?;
        let (head, epoch) = self.profiles.root.profile_head_snapshot(id)?;
        let profile = self.profiles.load_revision(id, &head)?;
        let packs = load_packs(&profile, &self.control)?;
        let compiled = compile(&profile, &packs).map_err(CoreError::Conflicts)?;
        let (gate, audit) = self.authority(&profile)?;
        register_policies(&gate, &profile, &packs)?;
        register_views(&gate, &compiled)?;
        register_port_bindings(&gate, &compiled, &packs)?;
        gate.register_user_policy_for_revision(
            &profile.id,
            &head,
            &compiled.plan_digest,
            epoch,
            profile.user_permissions.clone().unwrap_or_default(),
            profile.ceiling.clone(),
        )?;
        let cx = self
            .user_context(&profile, &gate)?
            .with_security_epoch(epoch)
            .with_revision(compiled.plan_digest.clone());
        let effective_limits = packs
            .iter()
            .map(|pack| {
                let pack_cx = cx.try_child(Principal::Pack {
                    pack: pack.manifest.id.clone(),
                    hash: pack.hash.clone(),
                })?;
                Ok((
                    (pack.manifest.id.clone(), pack.hash.clone()),
                    gate.effective_permissions(&pack_cx)?.wasm,
                ))
            })
            .collect::<Result<BTreeMap<_, _>, CoreError>>()?;
        gate.register_revision(&profile.id, &compiled.plan_digest, effective_limits.clone())?;
        // Flow endpoints are a separate immutable catalog from physical port
        // bindings. Register only after the revision's verified owner budgets
        // and exact operation views exist, and keep the handle alive while
        // NativeInput ingress policies are derived from it.
        let _flow_endpoint_registration =
            crate::execution_frames::register_flow_endpoints(&compiled, &gate)?;
        let native_ingress = crate::inputs::register_inputs(&gate, &compiled, &packs, &cx)?;
        let startup_ui = profile
            .startup
            .ui
            .as_ref()
            .map(|id| {
                let pack = packs
                    .iter()
                    .find(|pack| &pack.manifest.id == id)
                    .ok_or_else(|| CoreError::NotFound("startup UI Pack is missing".into()))?;
                let ui = pack.manifest.ui.as_ref().ok_or_else(|| {
                    CoreError::Invalid("startup UI Pack does not declare a UI".into())
                })?;
                Ok::<_, CoreError>((
                    pack.manifest.id.clone(),
                    pack.hash.clone(),
                    ui.ports_used.clone(),
                ))
            })
            .transpose()?;
        let materials: Vec<_> = packs
            .iter()
            .filter_map(|pack| {
                pack.manifest.ui.as_ref().map(|ui| {
                    (
                        pack.manifest.id.clone(),
                        pack.hash.clone(),
                        crate::session::UiMaterial {
                            entry: ui.entry.clone(),
                            files: pack.files.clone(),
                        },
                    )
                })
            })
            .collect();
        // One canonical compiled-profile Arc backs both the Core factory and
        // FlowEngine. The two layers borrow exact Flow keys; neither clones
        // Flow ASTs into an independently owned catalog.
        let compiled = Arc::new(compiled);
        let pack_host = Arc::new(
            tobkiri_wasm::PackHost::new(
                gate.clone(),
                self.capacity.clone(),
                self.host.clone(),
                self.profiles.root.clone(),
                compiled.bindings.clone(),
                compiled.views.clone(),
                packs,
                effective_limits,
                &cx,
            )
            .await
            .map_err(|error| CoreError::Runtime(error.to_string()))?,
        );
        if let Some(filesystem) = &self.filesystem {
            pack_host
                .set_filesystem_access(
                    filesystem
                        .for_gate(gate.clone())
                        .map_err(|error| CoreError::Runtime(error.to_string()))?,
                )
                .map_err(|error| CoreError::Runtime(error.to_string()))?;
        }
        let invoker: Arc<dyn tobkiri_flow::PortInvoker> = pack_host.clone();
        let execution_factory = crate::execution_frames::ExecutionFactory::new_with_context(
            compiled.clone(),
            gate.clone(),
            invoker.clone(),
            &cx,
        )
        .await?;
        let provider_bridge =
            crate::session::provider_entry::CoreProviderEntryBridge::allocate_shared(
                execution_factory.provider_authority(),
                cx.control(),
            )?;
        pack_host
            .set_provider_entry_bridge(provider_bridge.clone())
            .map_err(|error| CoreError::Runtime(error.to_string()))?;
        let session = Session::new_with_ingress(
            crate::session::SessionServices {
                compiled,
                gate,
                invoker: invoker.clone(),
                journal: Arc::new(AuditRunJournal(audit)),
                host: self.host.clone(),
            },
            epoch,
            head.clone(),
            cx.cancel().clone(),
            native_ingress,
            execution_factory,
            provider_bridge,
        );
        session.ensure_started().await?;
        pack_host.set_flow_runner(session.weak_runner())?;
        for (pack, hash, material) in materials {
            session.install_ui_material(pack, hash, material)?;
        }
        if let Some((pack, hash, ports)) = startup_ui {
            let request = tobkiri_gate::UiEmbed {
                profile: profile.id.clone(),
                pack: pack.clone(),
                hash: hash.clone(),
                slot: "startup".into(),
            };
            match session.host.embed_ui(request) {
                Ok(host_session) => {
                    let token =
                        tobkiri_canon::ids::UiSessionId::new(format!("ui_{}", ulid::Ulid::new()))?;
                    session.register_ui(
                        token,
                        host_session,
                        pack,
                        hash,
                        ports.into_iter().collect(),
                    )?;
                }
                Err(_) => session.host.terminal_write(
                    "Tobkiri: this host cannot embed the selected UI; input Packs are running.\n",
                ),
            }
        }
        Ok(session)
    }
}

/// Freeze the exact verified provider endpoints before PackHost initialization
/// or any guest entry. This installs topology only; Gate derives rights from
/// the independently registered declarations and approvals.
fn register_port_bindings(
    gate: &GateEngine,
    compiled: &CompiledProfile,
    packs: &[VerifiedPack],
) -> Result<(), CoreError> {
    let mut bindings = PortBindings::new();
    for (requested, providers) in &compiled.bindings {
        let targets = bindings.entry(requested.clone()).or_default();
        for provider in providers {
            let pack = packs
                .iter()
                .find(|pack| &pack.manifest.id == provider)
                .ok_or_else(|| {
                    CoreError::Invalid("compiled port provider is not verified".into())
                })?;
            let provided = pack
                .manifest
                .ports
                .provides
                .iter()
                .filter(|port| {
                    port.shape == requested.name
                        && port.version.major == requested.version.major
                        && port.version.minor >= requested.version.minor
                })
                .max_by_key(|port| port.version.minor)
                .ok_or_else(|| {
                    CoreError::Invalid("compiled provider has no compatible frozen view".into())
                })?;
            targets.insert(FrozenPortTarget {
                pack: pack.manifest.id.clone(),
                hash: pack.hash.clone(),
                provided: ShapeRef::new(provided.shape.clone(), provided.version),
            });
        }
    }
    gate.register_port_bindings(&compiled.profile.id, &compiled.plan_digest, bindings)?;
    Ok(())
}

pub(crate) fn register_policies(
    gate: &GateEngine,
    profile: &Profile,
    packs: &[tobkiri_manifest::VerifiedPack],
) -> Result<(), CoreError> {
    for pack in packs {
        // Each original policy is independent. Bindings and port declarations
        // describe compatibility; neither can manufacture effect authority.
        gate.register_pack(
            &profile.id,
            &pack.manifest.id,
            &pack.hash,
            PackPolicy {
                declaration: pack.manifest.permissions.clone(),
                ceiling: profile.ceiling.clone(),
                pack_limits: profile
                    .pack_limits
                    .get(&pack.manifest.id)
                    .cloned()
                    .unwrap_or_default(),
            },
        )?;
    }
    // Explicit revision-bound User policy is registered separately after its
    // committed document is known. A ceiling-only default carries no authority.
    Ok(())
}

fn register_views(
    gate: &GateEngine,
    compiled: &tobkiri_profile::CompiledProfile,
) -> Result<(), CoreError> {
    gate.register_operation_views(
        &compiled.profile.id,
        &compiled.plan_digest,
        compiled
            .views
            .iter()
            .map(|(reference, view)| (reference.clone(), view.functions.keys().cloned().collect()))
            .collect(),
    )?;
    Ok(())
}

#[cfg(test)]
#[path = "core_tests.rs"]
mod tests;

#[cfg(all(test, unix))]
#[path = "filesystem_registry_tests.rs"]
mod filesystem_registry_tests;

#[cfg(all(test, unix))]
#[path = "filesystem_guest_tests.rs"]
mod filesystem_guest_tests;

#[cfg(test)]
#[path = "audit_guest_tests.rs"]
mod audit_guest_tests;
