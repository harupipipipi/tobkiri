//! Verified ordinary component -> actual Core User/provider frames -> nested
//! Audit grants -> real last canonical Value hook. No execution/ACK fixture.
use super::*;
use std::{fs, time::Duration};
use tobkiri_canon::{
    CanonValue, CoreBuffer,
    ids::{OpName, RequestId},
};
use tobkiri_gate::{EffectRequest, Gate, GrantId, Outcome, Principal, TerminalDisposition};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
};
use tobkiri_store::AuditRow;

const KIND: &str = "fixture-marker";
const PAYLOAD: &[u8] = br#"{"needle":"owned-audit","control":"\u0001\n","profile":"forged","principal":"forged","request":"forged"}"#;
const SHAPE: &str = "local.audit-import-fixture.port@1.0";
const COMPONENT: &str = r###"(component
  (type $core-types (instance
    (type $code-definition (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
    (export "error-code" (type $named-code (eq $code-definition)))
    (type $error-definition (record (field "code" $named-code) (field "message" string)))
    (export "error" (type (eq $error-definition)))))
  (import "tobkiri:core/types@1.0.0" (instance $types-import (type $core-types)))
  (alias export $types-import "error-code" (type $error-code))
  (alias export $types-import "error" (type $error))
  (type $unit-result (result (error $error)))
  (type $audit-imports (instance
    (type $rows (list string))
    (type $read-result (result $rows (error $error)))
    (export "append" (func (param "kind" string) (param "payload" string) (result $unit-result)))
    (export "read" (func (param "after" u64) (param "max" u32) (result $read-result)))))
  (import "tobkiri:core/audit@1.0.0" (instance $audit (type $audit-imports)))
  (alias export $audit "append" (func $append))
  (alias export $audit "read" (func $read))
  (core module $allocator
    (memory (export "memory") 1 1)
    (global $next (mut i32) (i32.const 4096))
    (func (export "realloc") (param i32 i32) (param $align i32) (param $size i32) (result i32)
      (local $at i32)
      global.get $next local.get $align i32.const 1 i32.sub i32.add
      local.get $align i32.const 1 i32.sub i32.const -1 i32.xor i32.and local.tee $at
      local.get $size i32.add global.set $next local.get $at))
  (core instance $a (instantiate $allocator))
  (alias core export $a "memory" (core memory $memory))
  (alias core export $a "realloc" (core func $realloc))
  (core func $append-lowered (canon lower (func $append) (memory $memory) (realloc $realloc)))
  (core func $read-lowered (canon lower (func $read) (memory $memory) (realloc $realloc)))
  (core module $body
    (import "memory" "memory" (memory 1 1))
    (import "audit" "append" (func $append (param i32 i32 i32 i32 i32)))
    (import "audit" "read" (func $read (param i64 i32 i32)))
    (data (i32.const 256) "\66\69\78\74\75\72\65\2d\6d\61\72\6b\65\72")
    (data (i32.const 320) "\7b\22\6e\65\65\64\6c\65\22\3a\22\6f\77\6e\65\64\2d\61\75\64\69\74\22\2c\22\63\6f\6e\74\72\6f\6c\22\3a\22\5c\75\30\30\30\31\5c\6e\22\2c\22\70\72\6f\66\69\6c\65\22\3a\22\66\6f\72\67\65\64\22\2c\22\70\72\69\6e\63\69\70\61\6c\22\3a\22\66\6f\72\67\65\64\22\2c\22\72\65\71\75\65\73\74\22\3a\22\66\6f\72\67\65\64\22\7d")
    (data (i32.const 512) "\22\6b\69\6e\64\22\3a\22\67\75\65\73\74\2d\65\76\65\6e\74\22")
    (func (export "init") (param i32 i32) (result i32)
      i32.const 64 i32.const 0 i32.store i32.const 64)
    (func (export "shutdown"))
    (func $append-one (param $result i32)
      i32.const 256 i32.const 14 i32.const 320 i32.const 104 local.get $result call $append)
    (func $contains-event (param $at i32) (param $length i32) (result i32)
      (local $offset i32) (local $index i32)
      block $absent loop $candidate
        local.get $offset i32.const 20 i32.add local.get $length i32.gt_u br_if $absent
        i32.const 0 local.set $index
        block $mismatch loop $byte
          local.get $index i32.const 20 i32.eq if i32.const 1 return end
          local.get $at local.get $offset i32.add local.get $index i32.add i32.load8_u
          i32.const 512 local.get $index i32.add i32.load8_u i32.ne br_if $mismatch
          local.get $index i32.const 1 i32.add local.set $index br $byte
        end end
        local.get $offset i32.const 1 i32.add local.set $offset br $candidate
      end end i32.const 0)
    (func (export "append-only") (result i32)
      i32.const 96 call $append-one i32.const 96 i32.load)
    (func (export "late-append") (result i32)
      ;; Indirect result bounds are checked AFTER the real host callback in
      ;; HostFn::call_sync_lower. SQL commits before this invalid retptr traps.
      i32.const 65532 call $append-one i32.const 99)
    (func (export "round-trip") (result i32)
      (local $count i32) (local $index i32) (local $entry i32) (local $found i32)
      i32.const 96 call $append-one
      i32.const 96 i32.load if i32.const 100 i32.const 100 i32.load8_u i32.add return end
      i64.const 0 i32.const 1000 i32.const 128 call $read
      i32.const 128 i32.load if i32.const 200 i32.const 132 i32.load8_u i32.add return end
      i32.const 136 i32.load local.set $count
      block $done loop $row
        local.get $index local.get $count i32.ge_u br_if $done
        i32.const 132 i32.load local.get $index i32.const 8 i32.mul i32.add local.set $entry
        local.get $found local.get $entry i32.load local.get $entry i32.load offset=4 call $contains-event i32.add local.set $found
        local.get $index i32.const 1 i32.add local.set $index br $row
      end end
      local.get $found i32.const 1000000 i32.mul local.get $count i32.add))
  (core instance $b (instantiate $body
    (with "memory" (instance (export "memory" (memory $memory))))
    (with "audit" (instance (export "append" (func $append-lowered)) (export "read" (func $read-lowered))))))
  (alias core export $b "init" (core func $init))
  (alias core export $b "shutdown" (core func $shutdown))
  (alias core export $b "round-trip" (core func $round-trip))
  (alias core export $b "append-only" (core func $append-only))
  (alias core export $b "late-append" (core func $late-append))
  (func $init-value (param "config" string) (result $unit-result) (canon lift (core func $init) (memory $memory) (realloc $realloc)))
  (func $shutdown-value (canon lift (core func $shutdown)))
  (func $round-trip-value (result u32) (canon lift (core func $round-trip)))
  (func $append-only-value (result u32) (canon lift (core func $append-only)))
  (func $late-append-value (result u32) (canon lift (core func $late-append)))
  (instance $types (export "error-code" (type $error-code)) (export "error" (type $error)))
  (export "tobkiri:core/types@1.0.0" (instance $types))
  (instance $lifecycle (export "init" (func $init-value)) (export "shutdown" (func $shutdown-value)))
  (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
  (instance $port (export "round-trip" (func $round-trip-value)) (export "append-only" (func $append-only-value)) (export "late-append" (func $late-append-value)))
  (export "local:audit-import-fixture/port@1.0.0" (instance $port)))"###;

fn pack(directory: &std::path::Path, permissions: PermissionSet) -> VerifiedPack {
    let component = wat::parse_str(COMPONENT).unwrap();
    let shape = ShapeRef::parse(SHAPE).unwrap();
    let files = BTreeMap::from([
        ("component.wasm".into(), component),
        ("wit/port.wit".into(), b"package local:audit-import-fixture@1.0.0; interface port { round-trip: func() -> u32; append-only: func() -> u32; late-append: func() -> u32; }".to_vec()),
        ("wit/port.shape.json".into(), br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec()),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.audit-import-fixture").unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            component: "component.wasm".into(),
            toolchain: "wat-audit-ABI-regression/1".into(),
            config: None,
        },
        shapes: vec![ShapeDefinition {
            name: shape.name.clone(),
            version: shape.version,
            kind: ShapeKind::Port,
            wit: "wit/port.wit".into(),
        }],
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: shape.name,
                version: shape.version,
                optional: false,
            }],
            ..Default::default()
        },
        inputs: vec![],
        flows: vec![],
        overlays: vec![],
        stores: vec![],
        permissions,
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes): (&String, &Vec<u8>)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let artifact = tobkiri_manifest::build_tpk(&manifest, &files, None).unwrap();
    let verified = tobkiri_manifest::verify_tpk(&artifact, &BTreeMap::new()).unwrap();
    fs::write(directory.join("audit-import-fixture.tpk"), artifact).unwrap();
    verified
}

struct Fixture {
    session: Session,
    core: Core,
    profile: Profile,
    verified: VerifiedPack,
    _temporary: tempfile::TempDir,
}
impl Fixture {
    async fn new() -> Self {
        let temporary = tempfile::tempdir().unwrap();
        let physical = temporary.path().canonicalize().unwrap();
        let core = Core::open(
            CoreConfig::for_test_data_root(physical.join("data")),
            Arc::new(crate::test_support::RuntimeHost),
        )
        .unwrap();
        let mut permissions = crate::test_support::permissions(&[]);
        permissions.ports.insert(SHAPE.into());
        permissions.audit_append = true;
        permissions.audit_read = true;
        // Explicit finite NEW fixture limits cover ordinary compiler/runtime
        // work plus whole-history owned verification. No original cap changes.
        permissions.wasm.wall_ms = 5000;
        permissions.loops.max_wall_ms = 5000;
        let verified = pack(&physical, permissions.clone());
        let mut profile = crate::test_support::runtime_profile(
            &physical,
            std::slice::from_ref(&verified),
            permissions.clone(),
            false,
        );
        profile.user_permissions = Some(permissions);
        core.save_profile(profile.clone(), None).await.unwrap();
        let session = core.start(&profile.id).await.unwrap();
        Self {
            session,
            core,
            profile,
            verified,
            _temporary: temporary,
        }
    }
    async fn invoke(&self, operation: &str) -> Result<CoreBuffer, CoreError> {
        let started = std::time::Instant::now();
        let result = tokio::time::timeout(Duration::from_secs(10), self.session.call_user_port(
            ShapeRef::parse(SHAPE).unwrap(), OpName::parse(operation).unwrap(), &CanonValue::Obj(BTreeMap::new()),
        )).await.expect("actual Audit import must finish or report failure within its original finite fixture deadline");
        eprintln!(
            "AUDIT_GUEST_INVOCATION elapsed_us={} succeeded={}",
            started.elapsed().as_micros(),
            result.is_ok()
        );
        result
    }
    fn approvals(&self) -> Approvals {
        Approvals::open(&self.core.profiles.root, &self.profile.id).unwrap()
    }
    async fn rows(&self) -> Vec<AuditRow> {
        let gate = &self.session.gate;
        let cx = self
            .core
            .user_context(&self.profile, gate)
            .unwrap()
            .with_revision(self.session.plan_digest().clone());
        let log = AuditLog::open(
            &self.core.profiles.root,
            &self.profile.id,
            AuditQuota::default(),
        )
        .unwrap();
        let grant = gate
            .authorize(
                &cx,
                EffectRequest::AuditRead,
                AuditLog::read_digest(0, 1000).unwrap(),
            )
            .await
            .unwrap();
        let id = grant.id().clone();
        let rows = log.read(&cx, 0, 1000, grant).unwrap();
        gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
        rows
    }
    /// A late canonical trap closes the callback adapter and wakes its SAME
    /// accepted supervisor; it does not join that asynchronous job. Observe
    /// its actual persisted outcome before inspecting the terminal snapshot.
    /// Fresh native AuditRead grants authorize observation only; they never
    /// consume, settle, renew or replace the original nested AuditAppend.
    async fn rows_after_original_outcome(
        &self,
        request: &RequestId,
        effect: EffectRequest,
    ) -> Vec<AuditRow> {
        let effect = CanonValue::Str(effect.digest().unwrap().to_string());
        let principal = self.principal();
        tokio::time::timeout(Duration::from_secs(5), async {
            // Keep observation's independent user audit rows below the real
            // 600/minute quota even if the completion job is lost. Success
            // depends on persisted identity, never a chosen sleep/poll count.
            for _ in 0..512 {
                tokio::task::yield_now().await;
                let rows = self.rows().await;
                if rows.iter().any(|row| {
                    row.event.kind == "outcome"
                        && row.event.request == *request
                        && row.event.principal == principal
                        && row.event.payload.get("effect_digest") == Some(&effect)
                }) {
                    return rows;
                }
            }
            panic!("original Audit supervisor outcome was not observed within bounded cooperative reads");
        }).await.expect("original Audit completion must persist within the bounded observation window")
    }

    fn principal(&self) -> Principal {
        Principal::Pack {
            pack: self.verified.manifest.id.clone(),
            hash: self.verified.hash.clone(),
        }
    }
    fn guest_rows<'a>(&self, rows: &'a [AuditRow]) -> Vec<&'a AuditRow> {
        rows.iter()
            .filter(|row| {
                row.event.kind == "guest-event"
                    && row.event.payload.get("kind") == Some(&CanonValue::Str(KIND.into()))
            })
            .collect()
    }
    fn outcome<'a>(&self, rows: &'a [AuditRow], effect: EffectRequest) -> &'a AuditRow {
        let digest = CanonValue::Str(effect.digest().unwrap().to_string());
        let matching = rows
            .iter()
            .filter(|row| {
                row.event.kind == "outcome"
                    && row.event.principal == self.principal()
                    && row.event.payload.get("effect_digest") == Some(&digest)
            })
            .collect::<Vec<_>>();
        assert_eq!(
            matching.len(),
            1,
            "one immutable terminal for the original nested Audit effect"
        );
        matching[0]
    }
    fn acknowledged(
        &self,
        row: &AuditRow,
        effect: EffectRequest,
        args: &Digest,
    ) -> tobkiri_gate::TerminalDelivery {
        let CanonValue::Str(id) = row.event.payload.get("grant").unwrap() else {
            panic!("actual terminal must contain its original GrantId")
        };
        // Reconstruct this descriptive ID from the actual persisted outcome;
        // serde creates no Grant, DispatchPermit or terminal authority.
        let id: GrantId = serde_json::from_value(serde_json::Value::String(id.clone())).unwrap();
        let delivery = self.approvals().terminal_delivery(&id).unwrap().unwrap();
        assert!(delivery.acknowledged);
        assert_eq!(delivery.decision.reservation.id, id);
        assert_eq!(
            delivery.decision.reservation.effect,
            effect.digest().unwrap()
        );
        assert_eq!(&delivery.decision.reservation.args, args);
        assert_eq!(delivery.decision.reservation.principal, self.principal());
        assert_eq!(
            delivery.decision.disposition,
            TerminalDisposition::Completed
        );
        assert!(
            delivery
                .decision
                .reservation
                .original_expires_unix_ms
                .is_some()
        );
        assert_eq!(row.event_id.as_ref(), Some(&delivery.decision.event_id));
        delivery
    }
}

fn append_args() -> Digest {
    let payload = tobkiri_canon::parse(PAYLOAD, tobkiri_canon::Variant::StrictV1).unwrap();
    let args = CanonValue::Arr(vec![CanonValue::Str(KIND.into()), payload]);
    Digest::hash(&tobkiri_canon::encode(&args, tobkiri_canon::Variant::StrictV1).unwrap())
}

#[tokio::test]
async fn verified_guest_audit_round_trip_has_original_nested_ack_and_provenance() {
    let fixture = Fixture::new().await;
    let output = fixture.invoke("round-trip").await.unwrap();
    let value: u32 = output.as_str().unwrap().parse().unwrap();
    assert_eq!(
        value / 1_000_000,
        1,
        "guest read must receive exactly the one appended guest event through the real list<string> ABI"
    );
    assert!(!value.is_multiple_of(1_000_000));
    let rows = fixture.rows().await;
    let guest = fixture.guest_rows(&rows);
    assert_eq!(guest.len(), 1);
    assert_eq!(guest[0].event.profile, fixture.profile.id);
    assert_eq!(guest[0].event.principal, fixture.principal());
    assert_ne!(guest[0].event.request.as_str(), "forged");
    assert_eq!(
        guest[0].event.payload.get("payload"),
        Some(&tobkiri_canon::parse(PAYLOAD, tobkiri_canon::Variant::StrictV1).unwrap())
    );
    assert_eq!(
        guest[0]
            .event
            .payload
            .get("payload")
            .and_then(|value| value.get("control")),
        Some(&CanonValue::Str("\u{0001}\n".into()))
    );
    let append = fixture.acknowledged(
        fixture.outcome(&rows, EffectRequest::AuditAppend),
        EffectRequest::AuditAppend,
        &append_args(),
    );
    let read = fixture.acknowledged(
        fixture.outcome(&rows, EffectRequest::AuditRead),
        EffectRequest::AuditRead,
        &AuditLog::read_digest(0, 1000).unwrap(),
    );
    assert_eq!(append.decision.outcome, Outcome::Succeeded);
    assert_eq!(read.decision.outcome, Outcome::Succeeded);
    assert_ne!(append.decision.reservation.id, read.decision.reservation.id);
    assert_eq!(append.decision.reservation.request, guest[0].event.request);
    assert_eq!(read.decision.reservation.request, guest[0].event.request);
    fixture.session.close().await.unwrap();
}

#[tokio::test]
async fn verified_guest_audit_late_canonical_bounds_failure_keeps_committed_append_once() {
    let fixture = Fixture::new().await;
    assert!(
        fixture.invoke("late-append").await.is_err(),
        "a postcallback bounds trap cannot become normal ABI return"
    );
    let rows = fixture.rows().await;
    assert_eq!(
        fixture.guest_rows(&rows).len(),
        1,
        "the real SQL append committed before the late canonical failure; no rollback or replay"
    );
    let request = fixture.guest_rows(&rows)[0].event.request.clone();
    let rows = fixture
        .rows_after_original_outcome(&request, EffectRequest::AuditAppend)
        .await;
    assert_eq!(
        fixture.guest_rows(&rows).len(),
        1,
        "waiting for original completion cannot reappend the committed guest event"
    );
    let append = fixture.acknowledged(
        fixture.outcome(&rows, EffectRequest::AuditAppend),
        EffectRequest::AuditAppend,
        &append_args(),
    );
    assert_eq!(
        append.decision.outcome,
        Outcome::Failed {
            code: "unavailable".into()
        }
    );
    let same = fixture
        .approvals()
        .terminal_delivery(&append.decision.reservation.id)
        .unwrap()
        .unwrap();
    assert_eq!(same.decision.event_id, append.decision.event_id);
    assert_eq!(same.decision.outcome, append.decision.outcome);
    // Native/Value Unknown is not turned into a new Grant or a second append.
    // This test observes the committed row + immutable original failed ACK;
    // it does not fabricate access to the private runtime delivery arena.
}

struct AckFault {
    connection: rusqlite::Connection,
    active: bool,
}
impl AckFault {
    fn install(fixture: &Fixture) -> Self {
        let path = fixture.core.profiles.root.path().join(
            tobkiri_store::DataRoot::profile_relative(&fixture.profile.id, "audit.sqlite").unwrap(),
        );
        let connection = rusqlite::Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        )
        .unwrap();
        // Only this genuine nested AuditAppend terminal delivery fails. Guest
        // append bytes and all unrelated permission/entry/PortCall rows remain.
        let digest = EffectRequest::AuditAppend.digest().unwrap();
        connection.execute_batch(&format!(r#"CREATE TRIGGER audit_guest_ack_fault BEFORE INSERT ON audit WHEN instr(CAST(NEW.body AS TEXT), '"kind":"outcome"') > 0 AND instr(CAST(NEW.body AS TEXT), '"effect_digest":"{}"') > 0 BEGIN SELECT RAISE(FAIL, 'actual Audit ACK fixture fault'); END;"#, digest.as_str())).unwrap();
        Self {
            connection,
            active: true,
        }
    }
    fn clear(&mut self) {
        self.connection
            .execute_batch("DROP TRIGGER audit_guest_ack_fault;")
            .unwrap();
        self.active = false;
    }
}
impl Drop for AckFault {
    fn drop(&mut self) {
        if self.active {
            let _ = self
                .connection
                .execute_batch("DROP TRIGGER IF EXISTS audit_guest_ack_fault;");
        }
    }
}

#[tokio::test]
async fn verified_guest_audit_original_ack_failure_repairs_delivery_without_reappend() {
    let fixture = Fixture::new().await;
    let mut fault = AckFault::install(&fixture);
    assert!(
        fixture.invoke("append-only").await.is_err(),
        "canonical Ready without the original terminal ACK cannot normal-return"
    );
    let approvals = fixture.approvals();
    let pending = approvals
        .pending_terminal(&fixture.profile.id, 1000)
        .unwrap()
        .into_iter()
        .filter(|terminal| {
            terminal.reservation.effect == EffectRequest::AuditAppend.digest().unwrap()
                && terminal.reservation.principal == fixture.principal()
        })
        .collect::<Vec<_>>();
    assert_eq!(
        pending.len(),
        1,
        "the same original consumed append retains its pending immutable terminal"
    );
    let original = &pending[0];
    assert_eq!(original.reservation.args, append_args());
    assert!(original.reservation.original_expires_unix_ms.is_some());
    assert_eq!(
        original.outcome,
        Outcome::Succeeded,
        "last-ABI Ready proposed this immutable winner; delivery failure cannot relabel it"
    );
    let before = approvals
        .terminal_delivery(&original.reservation.id)
        .unwrap()
        .unwrap();
    assert!(!before.acknowledged);
    fault.clear();
    fixture
        .session
        .gate
        .flush_outbox(&fixture.profile.id)
        .await
        .unwrap();
    let after = approvals
        .terminal_delivery(&original.reservation.id)
        .unwrap()
        .unwrap();
    assert!(after.acknowledged);
    assert_eq!(after.decision.event_id, original.event_id);
    assert_eq!(after.decision.reservation.id, original.reservation.id);
    assert_eq!(after.decision.outcome, original.outcome);
    let rows = fixture.rows().await;
    assert_eq!(
        fixture.guest_rows(&rows).len(),
        1,
        "repair only delivers the original terminal; it never re-enters append SQL"
    );
    assert_eq!(
        fixture
            .outcome(&rows, EffectRequest::AuditAppend)
            .event_id
            .as_ref(),
        Some(&original.event_id)
    );
}
