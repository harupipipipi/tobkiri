use async_trait::async_trait;
use std::{
    any::Any,
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};
use tobkiri_canon::{ControlHandle, Digest, ids::*};
use tobkiri_gate::*;
use tobkiri_store::*;
struct DenyPrompt;
#[async_trait]
impl TrustedPrompt for DenyPrompt {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Deny
    }
}
// Test-only stand-in for Core's private factory proof. Public metadata alone
// cannot be downcast into this type or satisfy its live one-shot activation.
struct FactoryAuth {
    identity: Digest,
    closing: AtomicBool,
    admission: Mutex<()>,
}
struct ReachedProof {
    factory: ControlHandle<FactoryAuth>,
    metadata: ActiveFrameMetadata,
    context: CallCx,
    retired: AtomicBool,
    claimed: AtomicBool,
    admission: Mutex<()>,
    dispatch: (ShapeRef, OpName, Digest, FrozenPortTarget),
}
impl ReachedProof {
    fn retire(&self) {
        let _factory = self.factory.admission.lock().unwrap();
        let _admission = self.admission.lock().unwrap();
        self.retired.store(true, Ordering::Release);
    }
}
// Trusted fixture controls use the real fallible shared allocator and retain a
// bounded receipt until physical destruction. These tests exercise Gate proof
// semantics, not a production owner-funding or transitive heap inventory claim.
struct TestControlReceipt(u64);
static TEST_CONTROL_BYTES: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
impl Drop for TestControlReceipt {
    fn drop(&mut self) {
        TEST_CONTROL_BYTES.fetch_sub(self.0, Ordering::AcqRel);
    }
}
fn test_receipt(bytes: usize) -> TestControlReceipt {
    let bytes = u64::try_from(bytes).unwrap();
    TEST_CONTROL_BYTES
        .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
            used.checked_add(bytes)
                .filter(|next| *next <= 8 * 1024 * 1024)
        })
        .unwrap();
    TestControlReceipt(bytes)
}
fn test_control<T: Send + Sync + 'static>(value: T) -> ControlHandle<T> {
    let receipt = test_receipt(ControlHandle::<T>::storage_bytes::<TestControlReceipt>());
    ControlHandle::try_new_precharged(value, receipt).unwrap()
}
struct FrameVerifier {
    factory: ControlHandle<FactoryAuth>,
}
impl ActiveFrameVerifier for FrameVerifier {
    fn factory_identity(&self) -> &Digest {
        &self.factory.identity
    }
    fn close_registration(&self) {
        if let Ok(_admission) = self.factory.admission.lock() {
            self.factory.closing.store(true, Ordering::Release);
        } else {
            self.factory.closing.store(true, Ordering::Release);
        }
    }
    fn authenticate<'a>(
        &self,
        _: &CallCx,
        proof: &'a (dyn Any + Send + Sync),
        _: AllocationReason,
    ) -> Result<&'a ActiveFrameMetadata, GateError> {
        let proof = proof
            .downcast_ref::<ReachedProof>()
            .ok_or_else(|| GateError::Denied("foreign private proof".into()))?;
        if self.factory.closing.load(Ordering::Acquire)
            || !ControlHandle::ptr_eq(&self.factory, &proof.factory)
            || proof.retired.load(Ordering::Acquire)
        {
            return Err(GateError::Denied("retired private activation".into()));
        }
        Ok(&proof.metadata)
    }
    fn bound_context<'a>(
        &self,
        proof: &'a (dyn Any + Send + Sync),
    ) -> Result<&'a CallCx, GateError> {
        let proof = proof
            .downcast_ref::<ReachedProof>()
            .ok_or_else(|| GateError::Denied("foreign context proof".into()))?;
        if self.factory.closing.load(Ordering::Acquire)
            || !ControlHandle::ptr_eq(&self.factory, &proof.factory)
        {
            return Err(GateError::Denied(
                "closed or foreign fixture factory".into(),
            ));
        }
        Ok(&proof.context)
    }
    fn retained_control_bytes(&self, proof: &(dyn Any + Send + Sync)) -> Result<u64, GateError> {
        let proof = proof
            .downcast_ref::<ReachedProof>()
            .ok_or_else(|| GateError::Denied("foreign private proof".into()))?;
        // Fixture-local inventory, not a claim about production Core custody.
        Ok((std::mem::size_of_val(proof) + 4096) as u64)
    }
    fn claim_dispatch(
        &self,
        cx: &CallCx,
        proof: &(dyn Any + Send + Sync),
        dispatch: FrameDispatch<'_>,
    ) -> Result<(), GateError> {
        let proof = proof.downcast_ref::<ReachedProof>().unwrap();
        let _factory = self.factory.admission.lock().unwrap();
        let _admission = proof.admission.lock().unwrap();
        self.authenticate(cx, proof, AllocationReason::ArgumentDigest)?;
        if (
            &proof.dispatch.0,
            &proof.dispatch.1,
            &proof.dispatch.2,
            &proof.dispatch.3,
        ) != (dispatch.view, dispatch.op, dispatch.args, dispatch.target)
            || proof
                .claimed
                .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                .is_err()
        {
            return Err(GateError::Denied(
                "private activation already claimed or changed".into(),
            ));
        }
        Ok(())
    }
    fn with_allocation(
        &self,
        cx: &CallCx,
        proof: &(dyn Any + Send + Sync),
        reason: AllocationReason,
        admit: &mut dyn FnMut(&ActiveFrameMetadata) -> Result<(), GateError>,
    ) -> Result<(), GateError> {
        let private = proof
            .downcast_ref::<ReachedProof>()
            .ok_or_else(|| GateError::Denied("foreign private proof".into()))?;
        let _factory = self.factory.admission.lock().unwrap();
        let _admission = private.admission.lock().unwrap();
        let metadata = self.authenticate(cx, proof, reason)?;
        admit(metadata)
    }
}
struct Fixture {
    _dir: tempfile::TempDir,
    root: DataRoot,
    gate: GateEngine,
    approvals: Arc<Approvals>,
    profile: ProfileId,
    revision: Digest,
    cx: CallCx,
    shape: ShapeRef,
    op: OpName,
    target: FrozenPortTarget,
    descriptor: CompiledCallDescriptor,
    factory: ControlHandle<FactoryAuth>,
    frame_registration: FrameVerifierRegistration,
}
impl Fixture {
    fn new(cost: u64) -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = DataRoot::open(dir.path().canonicalize().unwrap().join("authority")).unwrap();
        let profile = ProfileId::parse("prof_admission").unwrap();
        let approvals = Arc::new(Approvals::open(&root, &profile).unwrap());
        let audit = Arc::new(AuditLog::open(&root, &profile, AuditQuota::default()).unwrap());
        let gate = GateEngine::new(
            approvals.clone(),
            audit,
            Arc::new(DenyPrompt),
            MediatorChain::default(),
        );
        let caller = PackId::parse("local.admission-caller").unwrap();
        let caller_hash = PackHash::new(Digest::hash(b"caller"));
        let target = FrozenPortTarget {
            pack: PackId::parse("local.admission-provider").unwrap(),
            hash: PackHash::new(Digest::hash(b"provider")),
            provided: ShapeRef::parse("local.admission.echo@1.0").unwrap(),
        };
        let shape = target.provided.clone();
        let op = OpName::parse("echo").unwrap();
        let limits = ResourceLimits {
            fuel_per_call: 100000,
            memory_mib: 64,
            wall_ms: 30000,
            table_elements: 4096,
            output_bytes: 1024,
        };
        let permissions = PermissionSet {
            ports: BTreeSet::from([shape.to_string()]),
            wasm: limits.clone(),
            ..Default::default()
        };
        for (pack, hash) in [(&caller, &caller_hash), (&target.pack, &target.hash)] {
            gate.register_pack(
                &profile,
                pack,
                hash,
                PackPolicy {
                    declaration: permissions.clone(),
                    ceiling: permissions.clone(),
                    pack_limits: permissions.clone(),
                },
            )
            .unwrap();
            approvals
                .set_permissions(&profile, pack, hash, &permissions)
                .unwrap();
        }
        let revision = Digest::hash(b"compiled revision");
        gate.register_revision(
            &profile,
            &revision,
            BTreeMap::from([
                ((caller.clone(), caller_hash.clone()), limits.clone()),
                ((target.pack.clone(), target.hash.clone()), limits.clone()),
            ]),
        )
        .unwrap();
        gate.register_operation_views(
            &profile,
            &revision,
            BTreeMap::from([(shape.clone(), BTreeSet::from([op.clone()]))]),
        )
        .unwrap();
        gate.register_port_bindings(
            &profile,
            &revision,
            BTreeMap::from([(shape.clone(), BTreeSet::from([target.clone()]))]),
        )
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(30);
        let budget = BudgetContext::new(
            LoopLimits {
                max_iterations: 10,
                max_wall_ms: 30000,
                max_cost_units: cost,
            },
            deadline,
        )
        .unwrap();
        let cx = CallCx::root(
            profile.clone(),
            Principal::Pack {
                pack: caller.clone(),
                hash: caller_hash.clone(),
            },
            RequestId::parse("req_admission").unwrap(),
            CancellationToken::new(),
            deadline,
            limits,
        )
        .with_revision(revision.clone())
        .with_budget(budget)
        .unwrap();
        let descriptor = CompiledCallDescriptor {
            id: Digest::hash(b"sealed compiled descriptor"),
            ast_hash: Digest::hash(b"original base AST"),
            owner: CompiledCodeOwner::Pack {
                pack: caller,
                hash: caller_hash,
            },
            revision: revision.clone(),
            flow: "echo".into(),
            node: "flow/call".into(),
            kind: CompiledNodeKind::StepCall,
            shape: shape.clone(),
            op: op.clone(),
            targets: BTreeSet::from([target.clone()]),
        };
        static NEXT_FACTORY: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
        let ordinal = NEXT_FACTORY
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |next| {
                next.checked_add(1)
            })
            .unwrap();
        let identity = Digest::hash(
            format!(
                "tobkiri-store-admission-test-factory-v1:{}:{ordinal}",
                std::process::id()
            )
            .as_bytes(),
        );
        let factory = test_control(FactoryAuth {
            identity,
            closing: AtomicBool::new(false),
            admission: Mutex::new(()),
        });
        let verifier = test_control(FrameVerifier {
            factory: factory.clone(),
        })
        .project(|value| value as &dyn ActiveFrameVerifier);
        let receipt = test_receipt(
            GateEngine::frame_verifier_registration_storage_bytes::<TestControlReceipt>(&profile)
                .unwrap(),
        );
        let frame_registration = gate
            .register_frame_verifier_precharged(&profile, &revision, verifier, receipt)
            .unwrap();
        Self {
            _dir: dir,
            root,
            gate,
            approvals,
            profile,
            revision,
            cx,
            shape,
            op,
            target,
            descriptor,
            factory,
            frame_registration,
        }
    }
    fn proof(&self, args: &Digest, activation: u64) -> ControlHandle<ReachedProof> {
        self.proof_with(args, activation, |_| {})
    }
    fn proof_with(
        &self,
        args: &Digest,
        activation: u64,
        adjust: impl FnOnce(&mut ReachedProof),
    ) -> ControlHandle<ReachedProof> {
        let mut proof = ReachedProof {
            factory: self.factory.clone(),
            metadata: ActiveFrameMetadata {
                profile: self.profile.clone(),
                revision: self.revision.clone(),
                security_epoch: self.cx.security_epoch(),
                principal: self.cx.principal().clone(),
                request: self.cx.request().clone(),
                frame: Digest::hash(b"private execution frame"),
                node: self.descriptor.id.clone(),
                activation,
                owner: self.descriptor.owner.clone(),
                reason: AllocationReason::ArgumentDigest,
                path: Digest::hash(b"actual admitted entry path"),
                transitive_memory_bytes: None,
            },
            context: self.cx.clone(),
            retired: AtomicBool::new(false),
            claimed: AtomicBool::new(false),
            admission: Mutex::new(()),
            dispatch: (
                self.shape.clone(),
                self.op.clone(),
                args.clone(),
                self.target.clone(),
            ),
        };
        adjust(&mut proof);
        test_control(proof)
    }
    fn scope(&self, proof: ControlHandle<ReachedProof>) -> AllocationFrameScope {
        self.gate
            .mint_allocation_scope(
                &self.cx,
                &self.frame_registration,
                proof.project(|proof| proof as &(dyn Any + Send + Sync)),
                AllocationReason::ArgumentDigest,
            )
            .unwrap()
    }
    async fn ordinary(&self, args: &Digest) -> PortAdmission {
        self.gate
            .admit_port(
                &self.cx,
                self.shape.clone(),
                self.op.clone(),
                args.clone(),
                self.target.clone(),
            )
            .await
            .unwrap()
    }
    fn authority(&self) -> CompiledCallAuthority {
        self.gate
            .register_compiled_calls(
                &self.factory.identity,
                &self.profile,
                &self.revision,
                BTreeMap::from([(self.descriptor.id.clone(), self.descriptor.clone())]),
            )
            .unwrap()
    }
    fn outcome_count(&self) -> i64 {
        let raw = rusqlite::Connection::open(
            self.root
                .path()
                .join(format!("profiles/{}/audit.sqlite", self.profile)),
        )
        .unwrap();
        raw.query_row(
            "SELECT COUNT(*) FROM audit WHERE CAST(body AS TEXT) LIKE '%\"kind\":\"outcome\"%'",
            [],
            |r| r.get(0),
        )
        .unwrap()
    }
}
#[tokio::test]
async fn ui_outer_and_final_dispatch_share_one_grant_one_cost_and_one_outcome() {
    let f = Fixture::new(10);
    let args = Digest::hash(b"immutable args");
    let admission = f.ordinary(&args).await;
    let id = admission.id().clone();
    assert_eq!(
        f.approvals.reservation_state(&id).unwrap().as_deref(),
        Some("dispatched")
    );
    assert_eq!(f.cx.budget().unwrap().cost_units(), 0);
    assert!(admission.requires_dynamic_consumes());
    // Outer registry/shape checks inspect the wrapper and move it unchanged.
    assert_eq!(admission.target(), &f.target);
    let admitted = f
        .gate
        .claim_port(&f.cx, admission, &f.shape, &f.op, &args, &f.target)
        .await
        .unwrap();
    assert_eq!(admitted.permit().id(), &id);
    assert_eq!(f.cx.budget().unwrap().cost_units(), 1);
    f.gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
    assert_eq!(f.outcome_count(), 1);
}
#[tokio::test]
async fn frozen_target_argument_and_principal_changes_fail_before_cost_or_effect() {
    let f = Fixture::new(10);
    let args = Digest::hash(b"immutable args");
    let admission = f.ordinary(&args).await;
    assert!(
        f.gate
            .claim_port(
                &f.cx,
                admission,
                &f.shape,
                &f.op,
                &Digest::hash(b"forged"),
                &f.target
            )
            .await
            .is_err()
    );
    let admission = f.ordinary(&args).await;
    let mut target = f.target.clone();
    target.hash = PackHash::new(Digest::hash(b"replacement"));
    assert!(
        f.gate
            .claim_port(&f.cx, admission, &f.shape, &f.op, &args, &target)
            .await
            .is_err()
    );
    let admission = f.ordinary(&args).await;
    let wrong = f.cx.child(Principal::Pack {
        pack: f.target.pack.clone(),
        hash: f.target.hash.clone(),
    });
    assert!(
        f.gate
            .claim_port(&wrong, admission, &f.shape, &f.op, &args, &f.target)
            .await
            .is_err()
    );
    assert_eq!(f.cx.budget().unwrap().cost_units(), 0);
}
#[tokio::test]
async fn reached_compiled_activation_preserves_original_witness_and_requires_fresh_permissions() {
    let f = Fixture::new(10);
    let authority = f.authority();
    let execution = f.gate.begin_compiled_execution(&f.cx, &authority).unwrap();
    let args = Digest::hash(b"immutable args");
    assert!(
        execution
            .reach(
                &f.cx,
                &Digest::hash(b"payload label"),
                Digest::hash(b"frame"),
                f.target.clone()
            )
            .is_err()
    );
    let reached = f
        .gate
        .reach_compiled(
            &f.cx,
            &execution,
            f.scope(f.proof(&args, 1)),
            &f.descriptor.id,
            f.target.clone(),
        )
        .unwrap();
    let admission = f
        .gate
        .admit_compiled(&f.cx, reached, args.clone())
        .await
        .unwrap();
    assert!(!admission.requires_dynamic_consumes());
    let admitted = f
        .gate
        .claim_port(&f.cx, admission, &f.shape, &f.op, &args, &f.target)
        .await
        .unwrap();
    f.gate
        .record_outcome(admitted.permit().id().clone(), Outcome::Succeeded)
        .await
        .unwrap();
    assert_eq!(f.cx.budget().unwrap().cost_units(), 1);
    let mut relabeled = f.descriptor.clone();
    relabeled.owner = CompiledCodeOwner::User {
        profile_ast: relabeled.ast_hash.clone(),
    };
    // Metadata registration is not execution authority. A relabeled catalog
    // cannot authenticate the actual reached Pack frame as User code.
    let relabeled_authority = f
        .gate
        .register_compiled_calls(
            &f.factory.identity,
            &f.profile,
            &f.revision,
            BTreeMap::from([(relabeled.id.clone(), relabeled)]),
        )
        .unwrap();
    let relabeled_execution = f
        .gate
        .begin_compiled_execution(&f.cx, &relabeled_authority)
        .unwrap();
    assert!(
        f.gate
            .reach_compiled(
                &f.cx,
                &relabeled_execution,
                f.scope(f.proof(&args, 2)),
                &f.descriptor.id,
                f.target.clone(),
            )
            .is_err()
    );
    assert_eq!(f.cx.budget().unwrap().cost_units(), 1);
    let reached = f
        .gate
        .reach_compiled(
            &f.cx,
            &execution,
            f.scope(f.proof(&args, 2)),
            &f.descriptor.id,
            f.target.clone(),
        )
        .unwrap();
    let (caller, hash) = f.cx.principal().pack().unwrap();
    f.approvals
        .set_permissions(&f.profile, caller, hash, &PermissionSet::default())
        .unwrap();
    assert!(f.gate.admit_compiled(&f.cx, reached, args).await.is_err());
}
#[tokio::test]
async fn exhausted_dispatch_budget_records_failure_without_refund_or_second_charge() {
    let f = Fixture::new(1);
    let args = Digest::hash(b"immutable args");
    let first = f.ordinary(&args).await;
    let admitted = f
        .gate
        .claim_port(&f.cx, first, &f.shape, &f.op, &args, &f.target)
        .await
        .unwrap();
    f.gate
        .record_outcome(admitted.permit().id().clone(), Outcome::Succeeded)
        .await
        .unwrap();
    let second = f.ordinary(&args).await;
    let id = second.id().clone();
    assert!(matches!(
        f.gate
            .claim_port(&f.cx, second, &f.shape, &f.op, &args, &f.target)
            .await,
        Err(GateError::Limit(_))
    ));
    assert_eq!(f.cx.budget().unwrap().cost_units(), 1);
    assert_eq!(
        f.approvals
            .terminal_delivery(&id)
            .unwrap()
            .unwrap()
            .decision
            .outcome,
        Outcome::Failed {
            code: "limit-exceeded".into()
        }
    );
    assert_eq!(f.outcome_count(), 2);
}

#[test]
fn allocation_scope_requires_private_live_exact_reason_and_current_epoch() {
    let f = Fixture::new(10);
    let args = Digest::hash(b"immutable args");
    assert!(
        f.gate
            .mint_allocation_scope(
                &f.cx,
                &f.frame_registration,
                test_control(f.proof(&args, 1).metadata.clone())
                    .project(|value| value as &(dyn Any + Send + Sync)),
                AllocationReason::ArgumentDigest,
            )
            .is_err()
    );
    assert!(
        f.gate
            .mint_allocation_scope(
                &f.cx,
                &f.frame_registration,
                f.proof(&args, 2)
                    .project(|value| value as &(dyn Any + Send + Sync)),
                AllocationReason::OutputBuffer,
            )
            .is_err()
    );
    let proof = f.proof(&args, 3);
    let scope = f.scope(proof.clone());
    assert_eq!(
        f.gate.frame_capacity_limit(&f.cx, &scope).unwrap(),
        64 * 1024 * 1024
    );
    proof.retire();
    assert!(f.gate.frame_capacity_limit(&f.cx, &scope).is_err());
    let scope = f.scope(f.proof(&args, 4));
    f.gate.revoke_epoch(&f.profile).unwrap();
    assert!(matches!(
        f.gate.frame_capacity_limit(&f.cx, &scope),
        Err(GateError::StaleEpoch)
    ));
}

#[test]
fn producer_funding_uses_own_cap_and_explicit_transitive_constraint() {
    let f = Fixture::new(10);
    let args = Digest::hash(b"immutable args");
    let mut narrow_limits = f.cx.limits().clone();
    narrow_limits.memory_mib = 1;
    let unrelated_caller_limits = f.cx.clone().narrow_limits(&narrow_limits);
    let proof = f.proof_with(&args, 1, |proof| {
        proof.metadata.owner = CompiledCodeOwner::Pack {
            pack: f.target.pack.clone(),
            hash: f.target.hash.clone(),
        };
    });
    let scope = f
        .gate
        .mint_allocation_scope(
            &unrelated_caller_limits,
            &f.frame_registration,
            proof.project(|proof| proof as &(dyn Any + Send + Sync)),
            AllocationReason::ArgumentDigest,
        )
        .unwrap();
    assert_eq!(
        f.gate
            .frame_capacity_limit(&unrelated_caller_limits, &scope)
            .unwrap(),
        64 * 1024 * 1024
    );
    let constrained = f.proof_with(&args, 2, |proof| {
        proof.metadata.transitive_memory_bytes = Some(2 * 1024 * 1024);
    });
    let scope = f.scope(constrained);
    assert_eq!(
        f.gate.frame_capacity_limit(&f.cx, &scope).unwrap(),
        2 * 1024 * 1024
    );
}

#[tokio::test]
async fn compiled_allocation_and_dispatch_share_one_private_activation() {
    let f = Fixture::new(10);
    let authority = f.authority();
    let execution = f.gate.begin_compiled_execution(&f.cx, &authority).unwrap();
    let args = Digest::hash(b"immutable args");
    let proof = f.proof(&args, 1);
    let first = f
        .gate
        .reach_compiled(
            &f.cx,
            &execution,
            f.scope(proof.clone()),
            &f.descriptor.id,
            f.target.clone(),
        )
        .unwrap();
    let duplicate = f
        .gate
        .reach_compiled(
            &f.cx,
            &execution,
            f.scope(proof.clone()),
            &f.descriptor.id,
            f.target.clone(),
        )
        .unwrap();
    let first = f
        .gate
        .admit_compiled(&f.cx, first, args.clone())
        .await
        .unwrap();
    let duplicate = f
        .gate
        .admit_compiled(&f.cx, duplicate, args.clone())
        .await
        .unwrap();
    let admitted = f
        .gate
        .claim_port(&f.cx, first, &f.shape, &f.op, &args, &f.target)
        .await
        .unwrap();
    assert!(
        f.gate
            .claim_port(&f.cx, duplicate, &f.shape, &f.op, &args, &f.target)
            .await
            .is_err()
    );
    assert_eq!(f.cx.budget().unwrap().cost_units(), 1);
    let (permit, target) = admitted.into_parts();
    assert_eq!(target, f.target);
    assert_eq!(permit.args_digest(), &args);
    assert_eq!(permit.revision(), Some(&f.revision));
    f.gate
        .record_outcome(permit.id().clone(), Outcome::Succeeded)
        .await
        .unwrap();
    // The rejected second grant is abandoned separately; it never dispatches.
    assert_eq!(f.outcome_count(), 2);
}

#[tokio::test]
async fn retired_or_different_reached_node_cannot_claim_physical_dispatch() {
    let f = Fixture::new(10);
    let authority = f.authority();
    let execution = f.gate.begin_compiled_execution(&f.cx, &authority).unwrap();
    let args = Digest::hash(b"immutable args");
    let wrong_node = f.proof_with(&args, 1, |proof| {
        proof.metadata.node = Digest::hash(b"another reached node");
    });
    assert!(
        f.gate
            .reach_compiled(
                &f.cx,
                &execution,
                f.scope(wrong_node),
                &f.descriptor.id,
                f.target.clone(),
            )
            .is_err()
    );
    let proof = f.proof(&args, 2);
    let reached = f
        .gate
        .reach_compiled(
            &f.cx,
            &execution,
            f.scope(proof.clone()),
            &f.descriptor.id,
            f.target.clone(),
        )
        .unwrap();
    let admission = f
        .gate
        .admit_compiled(&f.cx, reached, args.clone())
        .await
        .unwrap();
    proof.retire();
    assert!(
        f.gate
            .claim_port(&f.cx, admission, &f.shape, &f.op, &args, &f.target)
            .await
            .is_err()
    );
    assert_eq!(f.cx.budget().unwrap().cost_units(), 0);
}

#[test]
fn node_retirement_and_new_allocation_admission_share_one_critical_section() {
    let f = Fixture::new(10);
    let args = Digest::hash(b"immutable args");
    let proof = f.proof(&args, 1);
    let scope = f.scope(proof.clone());
    let (start, wait) = std::sync::mpsc::channel();
    let mut count = 0;
    std::thread::scope(|threads| {
        let retire_proof = proof.clone();
        let retirement = threads.spawn(move || {
            wait.recv().unwrap();
            retire_proof.retire();
        });
        f.gate
            .with_frame_allocation(&f.cx, &scope, &mut |cap| {
                assert_eq!(cap, 64 * 1024 * 1024);
                assert!(proof.admission.try_lock().is_err());
                start.send(()).unwrap();
                count += 1;
                Ok(())
            })
            .unwrap();
        retirement.join().unwrap();
    });
    assert!(
        f.gate
            .with_frame_allocation(&f.cx, &scope, &mut |_| {
                count += 1;
                Ok(())
            })
            .is_err()
    );
    assert_eq!(count, 1);
}

#[test]
fn private_factory_identity_cannot_be_replaced_by_identical_public_metadata() {
    let first = Fixture::new(10);
    let second = Fixture::new(10);
    assert_ne!(first.factory.identity, second.factory.identity);
    let args = Digest::hash(b"same public binding");
    let foreign = second.proof(&args, 1);
    assert!(
        first
            .gate
            .mint_allocation_scope(
                &first.cx,
                &first.frame_registration,
                foreign.project(|proof| proof as &(dyn Any + Send + Sync)),
                AllocationReason::ArgumentDigest,
            )
            .is_err()
    );
}

#[test]
fn dropping_actual_factory_registration_closes_retained_proof_admission() {
    let f = Fixture::new(10);
    let args = Digest::hash(b"retained proof");
    let scope = f.scope(f.proof(&args, 1));
    // This drops the real noncloneable lifetime token. The retained scope keeps
    // its physical control receipt, but may not admit any new work.
    drop(f.frame_registration);
    assert!(f.factory.closing.load(Ordering::Acquire));
    let mut called = false;
    assert!(
        f.gate
            .with_frame_allocation(&f.cx, &scope, &mut |_| {
                called = true;
                Ok(())
            })
            .is_err()
    );
    assert!(!called);
}
