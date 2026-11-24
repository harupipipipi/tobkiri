//! Real verified WAT Input -> canonical one-route Flow -> typed callee. All
//! frames, child leases and read/terminal receipts come from production paths.
use super::*;
use std::path::Path;
use tobkiri_canon::{CanonValue, Digest};
use tobkiri_gate::{ApprovalStore, EffectRequest, WasmEntryKind};
use tobkiri_manifest::{
    FileEntry, InputDeclaration, PackManifest, PortDeclaration, Ports, RuntimeSpec,
    ShapeDefinition, ShapeKind, VerifiedPack,
};
use tobkiri_profile::{
    ErrorPolicy, FlowDocument, OverlayAccess, PortCall, Step, StepKind, Trigger,
};
use tobkiri_store::{Approvals, DataRoot};

#[derive(Clone, Copy)]
enum Kind {
    Stream,
    Callback,
}
impl Kind {
    fn handle(self) -> &'static str {
        match self {
            Self::Stream => "stream-handle",
            Self::Callback => "callback-handle",
        }
    }
    fn entry(self) -> WasmEntryKind {
        match self {
            Self::Stream => WasmEntryKind::StreamOpen { handle: 1 },
            Self::Callback => WasmEntryKind::CallbackOpen { handle: 1 },
        }
    }
}
fn view(namespace: &str, name: &str) -> ShapeRef {
    ShapeRef::parse(&format!("local.{namespace}.{name}@1.0")).unwrap()
}
fn common_types() -> &'static str {
    r#"(type $code (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
       (type $error (record (field "code" $code) (field "message" string)))
       (type $handle (record (field "id" u64)))
       (type $init-result (result (error $error)))"#
}
fn source_component(kind: Kind) -> Vec<u8> {
    let (result_type, source_exports, source_aliases, source_lifts, source_interface) = match kind {
        Kind::Stream => (
            r#"(type $items (list string)) (type $source-result (result $items (error $error)))"#,
            r#"(global $remaining (mut i32) (i32.const 3))
              (data (i32.const 256) "null")
              (func (export "pull") (param i64) (param $max i32) (result i32)
                (local $count i32) (local $at i32)
                global.get $remaining local.get $max i32.lt_u
                if (result i32) global.get $remaining else local.get $max end local.set $count
                global.get $remaining local.get $count i32.sub global.set $remaining
                i32.const 0 i32.const 0 i32.store
                i32.const 4 i32.const 64 i32.store i32.const 8 local.get $count i32.store
                block $done loop $next
                  local.get $at local.get $count i32.ge_u br_if $done
                  i32.const 64 local.get $at i32.const 8 i32.mul i32.add i32.const 256 i32.store
                  i32.const 68 local.get $at i32.const 8 i32.mul i32.add i32.const 4 i32.store
                  local.get $at i32.const 1 i32.add local.set $at br $next
                end end i32.const 0)
              (func (export "cancel") (param i64) i32.const 0 global.set $remaining)"#,
            r#"(alias core export $i "pull" (core func $pull)) (alias core export $i "cancel" (core func $cancel))"#,
            r#"(func $pull-value (param "local-id" u64) (param "max" u32) (result $source-result) (canon lift (core func $pull) (memory $memory) (realloc $realloc)))
              (func $cancel-value (param "local-id" u64) (canon lift (core func $cancel)))"#,
            r#"(instance $source (export "pull" (func $pull-value)) (export "cancel" (func $cancel-value)))
              (export "tobkiri:core/stream-source@1.0.0" (instance $source))"#,
        ),
        Kind::Callback => (
            r#"(type $source-result (result string (error $error)))"#,
            r#"(data (i32.const 256) "42")
              (func (export "invoke") (param i64 i32 i32) (result i32)
                i32.const 0 i32.const 0 i32.store i32.const 4 i32.const 256 i32.store
                i32.const 8 i32.const 2 i32.store i32.const 0)"#,
            r#"(alias core export $i "invoke" (core func $invoke))"#,
            r#"(func $invoke-value (param "local-id" u64) (param "args" string) (result $source-result) (canon lift (core func $invoke) (memory $memory) (realloc $realloc)))"#,
            r#"(instance $source (export "invoke" (func $invoke-value)))
              (export "tobkiri:core/callback-sink@1.0.0" (instance $source))"#,
        ),
    };
    wat::parse_str(format!(r#"(component {types}
      (type $read-result (result $handle (error $error))) {result_type}
      (core module $m (memory (export "memory") 1 1)
        (func (export "realloc") (param i32 i32 i32 i32) (result i32) i32.const 1024)
        (func (export "init") (param i32 i32) (result i32) i32.const 0)
        (func (export "shutdown"))
        (func (export "read") (result i32) i32.const 0 i32.const 0 i32.store
          i32.const 8 i64.const 1 i64.store i32.const 0) {source_exports})
      (core instance $i (instantiate $m))
      (alias core export $i "memory" (core memory $memory))
      (alias core export $i "realloc" (core func $realloc))
      (alias core export $i "init" (core func $init))
      (alias core export $i "shutdown" (core func $shutdown))
      (alias core export $i "read" (core func $read)) {source_aliases}
      (func $read-value (result $read-result) (canon lift (core func $read) (memory $memory) (realloc $realloc))) {source_lifts}
      (func $init-value (param "config" string) (result $init-result) (canon lift (core func $init) (memory $memory) (realloc $realloc)))
      (func $shutdown-value (canon lift (core func $shutdown)))
      (instance $types (export "error-code" (type $code)) (export "error" (type $error)) (export "{handle}" (type $handle)))
      (export "tobkiri:core/types@1.0.0" (instance $types))
      (instance $lifecycle (export "init" (func $init-value)) (export "shutdown" (func $shutdown-value)))
      (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle)) {source_interface}
      (instance $input (export "read" (func $read-value)))
      (export "local:source/input@1.0.0" (instance $input)))"#, types=common_types(), handle=kind.handle())).unwrap()
}
// Import named core types before importing a function which uses them.
// wasmparser0.252 validates imported function types against imported_types;
// local record/enum definitions alone do not name their external identities.
fn sink_types(kind: Kind, operation_kind: Kind) -> String {
    format!(
        r#"(type $core-types (instance
      (type $code-definition (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
      (export "error-code" (type $named-code (eq $code-definition)))
      (type $error-definition (record (field "code" $named-code) (field "message" string)))
      (export "error" (type (eq $error-definition)))
      (type $stream-definition (record (field "id" u64)))
      (export "stream-handle" (type (eq $stream-definition)))
      (type $callback-definition (record (field "id" u64)))
      (export "callback-handle" (type (eq $callback-definition)))))
      (import "tobkiri:core/types@1.0.0" (instance $types-import (type $core-types)))
      (alias export $types-import "error-code" (type $code))
      (alias export $types-import "error" (type $error))
      (alias export $types-import "{handle}" (type $handle))
      (alias export $types-import "{operation_handle}" (type $operation-handle))
      (type $init-result (result (error $error)))"#,
        handle = kind.handle(),
        operation_handle = operation_kind.handle()
    )
}
fn sink_component(kind: Kind, wrong_kind: bool) -> Vec<u8> {
    let operation_kind = if wrong_kind { Kind::Callback } else { kind };
    let (signature, call, name) = match operation_kind {
        Kind::Stream => (
            r#"(type $items (list string)) (type $result (result $items (error $error)))
              (export "pull" (func (param "s" $operation-handle) (param "max" u32) (result $result)))"#,
            r#"local.get $id i32.const 4 i32.const 128 call $use
              i32.const 128 i32.load if i32.const 901 return end
              i32.const 136 i32.load i32.const 3 i32.ne if i32.const 902 return end
              local.get $id i32.const 1 i32.const 128 call $use
              i32.const 128 i32.load if i32.const 903 return end
              i32.const 136 i32.load if i32.const 904 return end i32.const 42"#,
            "pull",
        ),
        Kind::Callback => (
            r#"(type $result (result string (error $error)))
              (export "invoke-callback" (func (param "c" $operation-handle) (param "args" string) (result $result)))"#,
            r#"local.get $id i32.const 256 i32.const 4 i32.const 128 call $use
              i32.const 128 i32.load if i32.const 911 return end
              i32.const 136 i32.load i32.const 2 i32.ne if i32.const 912 return end
              i32.const 132 i32.load i32.load8_u i32.const 52 i32.ne if i32.const 913 return end
              local.get $id i32.const 256 i32.const 4 i32.const 128 call $use
              i32.const 128 i32.load if i32.const 914 return end i32.const 42"#,
            "invoke-callback",
        ),
    };
    let call = if wrong_kind {
        // Keep the typed callee argument genuine, then use its ID through the
        // other core operation. Resolve must reject the actual source kind.
        "local.get $id i32.const 256 i32.const 4 i32.const 128 call $use i32.const 128 i32.load if (result i32) i32.const 42 else i32.const 999 end"
    } else {
        call
    };
    let core_sig = if matches!(operation_kind, Kind::Stream) {
        "(param i64 i32 i32)"
    } else {
        "(param i64 i32 i32 i32)"
    };
    wat::parse_str(format!(r#"(component {types}
      (type $flow (instance {signature}))
      (import "tobkiri:core/flow@1.0.0" (instance $flow-import (type $flow)))
      (alias export $flow-import "{name}" (func $use))
      (core module $allocator
        (memory (export "memory") 1 1)
        (func (export "realloc") (param i32 i32 i32 i32) (result i32) i32.const 1024))
      (core instance $a (instantiate $allocator))
      (alias core export $a "memory" (core memory $memory))
      (alias core export $a "realloc" (core func $realloc))
      (core func $use-lowered (canon lower (func $use) (memory $memory) (realloc $realloc)))
      (core module $m (import "flow" "use" (func $use {core_sig}))
        (import "allocator" "memory" (memory 1 1)) (data (i32.const 256) "null")
        (func (export "init") (param i32 i32) (result i32) i32.const 0)
        (func (export "shutdown"))
        (func (export "consume") (param $id i64) (result i32) {call}))
      (core instance $i (instantiate $m
        (with "flow" (instance (export "use" (func $use-lowered))))
        (with "allocator" (instance (export "memory" (memory $memory))))))
      (alias core export $i "init" (core func $init)) (alias core export $i "shutdown" (core func $shutdown))
      (alias core export $i "consume" (core func $consume))
      (func $consume-value (param "value" $handle) (result u32) (canon lift (core func $consume)))
      (func $init-value (param "config" string) (result $init-result) (canon lift (core func $init) (memory $memory) (realloc $realloc)))
      (func $shutdown-value (canon lift (core func $shutdown)))
      (instance $types (export "error-code" (type $code)) (export "error" (type $error)) (export "{handle}" (type $handle)))
      (export "tobkiri:core/types@1.0.0" (instance $types))
      (instance $lifecycle (export "init" (func $init-value)) (export "shutdown" (func $shutdown-value)))
      (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
      (instance $input (export "consume" (func $consume-value)))
      (export "local:sink/input@1.0.0" (instance $input)))"#, types=sink_types(kind, operation_kind), handle=kind.handle())).unwrap()
}

// One byte-identical contract is verified by the source Pack and carried as
// the sink's declared foreign-port WIT dependency. It defines no sink shape or
// provider and supplies no source/Flow authority.
fn fixture_wit(namespace: &str, kind: Kind) -> Vec<u8> {
    let source = namespace == "source";
    format!("package local:{namespace}@1.0.0; interface input {{ use tobkiri:core/types@1.0.0.{{json, {}, error}}; {} }} interface turn {{ run: func(); }}", kind.handle(), if source { format!("read: func() -> result<{}, error>;", kind.handle()) } else { format!("consume: func(value: {}) -> u32;", kind.handle()) }).into_bytes()
}
fn write_pack(directory: &Path, namespace: &str, kind: Kind, routes: usize) -> VerifiedPack {
    write_pack_variant(directory, namespace, kind, routes, false)
}
fn write_pack_variant(
    directory: &Path,
    namespace: &str,
    kind: Kind,
    routes: usize,
    wrong_kind: bool,
) -> VerifiedPack {
    let source = namespace == "source";
    let input = view(namespace, "input");
    let permission = crate::test_support::permissions(&["source", "sink"]);
    let mut files = BTreeMap::from([
        (
            "component.wasm".into(),
            if source {
                source_component(kind)
            } else {
                sink_component(kind, wrong_kind)
            },
        ),
        ("wit/fixture.wit".into(), fixture_wit(namespace, kind)),
        (
            "wit/input.shape.json".into(),
            if source && matches!(kind, Kind::Stream) {
                br#"{"cardinality":"one","streams":{"read":"json"},"callbacks":{}}"#.to_vec()
            } else {
                br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec()
            },
        ),
        (
            "wit/turn.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        ),
    ]);
    if !source {
        // inspect_pack_wit seeds every declared consumes/provides interface,
        // including the sink's already-declared local.source.input contract.
        // Include those actual matching WIT bytes before manifest file hashes
        // and build_tpk/verify_tpk, not an unchecked in-memory interface.
        files.insert("wit/deps/source.wit".into(), fixture_wit("source", kind));
    }
    let mut flows = Vec::new();
    if !source {
        for index in 0..routes {
            let path = format!("flows/route{index}.json");
            let document = FlowDocument {
                format: "tobkiri.flow/1".into(),
                id: format!("route{index}"),
                implements: Some(view("sink", "turn")),
                trigger: Trigger {
                    input: view("source", "input"),
                },
                loop_limits: "profile-default".into(),
                steps: vec![Step {
                    id: Some("consume".into()),
                    role: None,
                    overlay: OverlayAccess::Closed,
                    out: None,
                    stream: None,
                    kind: StepKind::Call(PortCall {
                        shape: input.clone(),
                        op: OpName::parse("consume").unwrap(),
                        args: CanonValue::Obj(BTreeMap::from([(
                            "value".into(),
                            CanonValue::Str("$input".into()),
                        )])),
                    }),
                }],
                on_error: ErrorPolicy::default(),
            };
            files.insert(path.clone(), serde_json::to_vec(&document).unwrap());
            flows.push(path);
        }
    }
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: tobkiri_canon::ids::PackId::parse(&format!("local.{namespace}")).unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: "wat-input-lifecycle-fixture/1".into(),
            config: None,
        },
        shapes: [
            (input.clone(), ShapeKind::Port),
            (view(namespace, "turn"), ShapeKind::Flow),
        ]
        .into_iter()
        .map(|(reference, kind)| ShapeDefinition {
            name: reference.name,
            version: reference.version,
            kind,
            wit: "wit/fixture.wit".into(),
        })
        .collect(),
        ports: Ports {
            provides: vec![PortDeclaration {
                shape: input.name.clone(),
                version: input.version,
                optional: false,
            }],
            consumes: if source {
                vec![]
            } else {
                vec![
                    PortDeclaration {
                        shape: input.name.clone(),
                        version: input.version,
                        optional: false,
                    },
                    PortDeclaration {
                        shape: view("source", "input").name,
                        version: input.version,
                        optional: false,
                    },
                ]
            },
            untyped: vec![],
        },
        inputs: if source {
            vec![InputDeclaration {
                exact_input_view: input,
                operation: OpName::parse("read").unwrap(),
            }]
        } else {
            vec![]
        },
        flows,
        overlays: vec![],
        stores: vec![],
        permissions: permission,
        ui: None,
        files: files
            .iter()
            .map(|(path, bytes)| FileEntry {
                path: path.clone(),
                sha256: Digest::hash(bytes),
                size: bytes.len() as u64,
            })
            .collect(),
    };
    let artifact = tobkiri_manifest::build_tpk(&manifest, &files, None).unwrap();
    let pack = tobkiri_manifest::verify_tpk(&artifact, &BTreeMap::new()).unwrap();
    std::fs::write(directory.join(format!("{namespace}.tpk")), artifact).unwrap();
    pack
}

struct Fixture {
    core: crate::Core,
    profile: tobkiri_profile::Profile,
    root: DataRoot,
    source: (tobkiri_canon::ids::PackId, tobkiri_canon::ids::PackHash),
    _directory: tempfile::TempDir,
}
impl Fixture {
    async fn new(kind: Kind) -> Self {
        Self::with_wrong_kind(kind, false).await
    }
    async fn with_wrong_kind(kind: Kind, wrong_kind: bool) -> Self {
        let directory = tempfile::tempdir().unwrap();
        let physical = directory.path().canonicalize().unwrap();
        let packs = vec![
            write_pack(&physical, "source", kind, 0),
            write_pack_variant(&physical, "sink", kind, 1, wrong_kind),
        ];
        assert_eq!(
            packs[0].files["wit/fixture.wit"].read().unwrap(),
            packs[1].files["wit/deps/source.wit"].read().unwrap()
        );
        let views = packs
            .iter()
            .flat_map(|pack| pack.shapes.iter())
            .map(|shape| (shape.reference.clone(), shape.clone()))
            .collect();
        for pack in &packs {
            tobkiri_manifest::inspect_pack_wit(pack, &views)
                .expect("known verified Input fixture ports/types must match their packaged WIT");
        }
        let permission = packs[0].manifest.permissions.clone();
        let profile =
            crate::test_support::runtime_profile(&physical, &packs, permission.clone(), false);
        let core = crate::Core::open(
            crate::CoreConfig::for_test_data_root(physical.join("data")),
            Arc::new(crate::test_support::RuntimeHost),
        )
        .unwrap();
        core.save_profile(profile.clone(), None).await.unwrap();
        let root = DataRoot::open(physical.join("data")).unwrap();
        let approvals = Approvals::open(&root, &profile.id).unwrap();
        for pack in &packs {
            approvals
                .set_permissions(&profile.id, &pack.manifest.id, &pack.hash, &permission)
                .unwrap();
        }
        Self {
            core,
            profile,
            root,
            source: (packs[0].manifest.id.clone(), packs[0].hash.clone()),
            _directory: directory,
        }
    }
    fn events(&self, kind: &str, effect: &EffectRequest) -> Vec<tobkiri_gate::AuditEvent> {
        let path = self
            .root
            .path()
            .join(DataRoot::profile_relative(&self.profile.id, "audit.sqlite").unwrap());
        let db =
            rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                .unwrap();
        let mut statement = db.prepare("SELECT body FROM audit ORDER BY seq").unwrap();
        let digest = CanonValue::Str(effect.digest().unwrap().to_string());
        statement
            .query_map([], |row| row.get::<_, Vec<u8>>(0))
            .unwrap()
            .map(|row| serde_json::from_slice::<tobkiri_gate::AuditEvent>(&row.unwrap()).unwrap())
            .filter(|event| {
                event.kind == kind && event.payload.get("effect_digest") == Some(&digest)
            })
            .collect()
    }
    fn source_effect(&self, kind: Kind) -> EffectRequest {
        EffectRequest::WasmEntry {
            pack: self.source.0.clone(),
            hash: self.source.1.clone(),
            entry: kind.entry(),
        }
    }
}
async fn read(session: &Session) -> NativeInput {
    let entry = &session.input_entries()[0];
    crate::test_support::within_session(
        session,
        "actual typed Input read",
        session.read_input(
            InputSourceId::parse(entry.source_pack().as_str()).unwrap(),
            entry.exact_input_view().clone(),
            entry.operation().clone(),
        ),
    )
    .await
    .unwrap()
}
async fn complete(session: &Session, input: NativeInput) -> NativeInputCompletion {
    let mut run = session.submit_native_input(input).await.unwrap();
    assert_eq!(run.routes().len(), 1);
    let mut failures = Vec::new();
    while let Some(event) = run.events.recv().await {
        crate::test_support::capture_run_failure(&event.event, &mut failures);
    }
    let result = run.result.await.unwrap();
    assert_eq!(
        result.status,
        NativeInputStatus::Succeeded,
        "{}",
        crate::test_support::native_run_diagnostic(&result, &failures)
    );
    assert_eq!(result.buffer.as_bytes(), br#"{"routes":[{"error":null,"index":0,"result":42,"status":"succeeded"}],"status":"succeeded"}"#);
    result
}
#[tokio::test]
async fn physical_input_stream_uses_original_child_after_parent_ack_and_finishes_once_at_eof() {
    let fixture = Fixture::new(Kind::Stream).await;
    let session = fixture.core.start(&fixture.profile.id).await.unwrap();
    let input = read(&session).await;
    assert!(input.custody.is_some());
    assert_eq!(
        input
            .payload
            .digest_guest(&crate::test_support::value_budget())
            .unwrap(),
        input.receipt.metadata().payload
    );
    let child = fixture.source_effect(Kind::Stream);
    assert_eq!(fixture.events("dispatch", &child).len(), 1);
    assert!(fixture.events("outcome", &child).is_empty());
    let parent = EffectRequest::PortCall {
        shape: view("source", "input"),
        op: OpName::parse("read").unwrap(),
    };
    assert_eq!(fixture.events("outcome", &parent).len(), 1);
    complete(&session, input).await;
    let outcomes = fixture.events("outcome", &child);
    assert_eq!(outcomes.len(), 1);
    assert_eq!(
        outcomes[0]
            .payload
            .get("outcome")
            .and_then(|value| value.get("status"))
            .and_then(CanonValue::as_str),
        Some("succeeded")
    );
    session.close().await.unwrap();
    assert_eq!(fixture.events("outcome", &child).len(), 1);
}
#[tokio::test]
async fn physical_input_callback_remains_same_live_child_for_two_uses_then_session_close() {
    let fixture = Fixture::new(Kind::Callback).await;
    let session = fixture.core.start(&fixture.profile.id).await.unwrap();
    let input = read(&session).await;
    assert!(input.custody.is_some());
    let child = fixture.source_effect(Kind::Callback);
    assert_eq!(fixture.events("dispatch", &child).len(), 1);
    complete(&session, input).await;
    assert!(fixture.events("outcome", &child).is_empty());
    session.close().await.unwrap();
    assert_eq!(fixture.events("outcome", &child).len(), 1);
}
#[tokio::test]
async fn physical_input_forwarding_cannot_cross_actual_session_factory() {
    let fixture = Fixture::new(Kind::Stream).await;
    let first = fixture.core.start(&fixture.profile.id).await.unwrap();
    let second = fixture.core.start(&fixture.profile.id).await.unwrap();
    let input = read(&first).await;
    assert!(second.submit_native_input(input).await.is_err());
    first.close().await.unwrap();
    second.close().await.unwrap();
    assert_eq!(
        fixture
            .events("dispatch", &fixture.source_effect(Kind::Stream))
            .len(),
        1
    );
    assert_eq!(
        fixture
            .events("outcome", &fixture.source_effect(Kind::Stream))
            .len(),
        1
    );
}
#[test]
fn physical_input_handles_reject_zero_and_multiple_frozen_routes_before_runtime() {
    for kind in [Kind::Stream, Kind::Callback] {
        for routes in [0, 2] {
            let directory = tempfile::tempdir().unwrap();
            let physical = directory.path().canonicalize().unwrap();
            let packs = vec![
                write_pack(&physical, "source", kind, 0),
                write_pack(&physical, "sink", kind, routes),
            ];
            let profile = crate::test_support::runtime_profile(
                &physical,
                &packs,
                packs[0].manifest.permissions.clone(),
                false,
            );
            let errors = tobkiri_profile::compile(&profile, &packs)
                .expect_err("handle fanout must be rejected");
            assert!(
                errors
                    .iter()
                    .any(|error| error.code == "input-handle-routes"),
                "{errors:?}"
            );
        }
    }
}

#[tokio::test]
async fn physical_input_stream_id_cannot_be_used_as_callback_in_actual_callee() {
    let fixture = Fixture::with_wrong_kind(Kind::Stream, true).await;
    let session = fixture.core.start(&fixture.profile.id).await.unwrap();
    let input = read(&session).await;
    // The real stream alias is delivered, then an ordinary core callback use
    // rejects its kind. A local fallback ID or kind confusion would yield 999.
    complete(&session, input).await;
    let child = fixture.source_effect(Kind::Stream);
    assert_eq!(fixture.events("dispatch", &child).len(), 1);
    assert!(fixture.events("outcome", &child).is_empty());
    session.close().await.unwrap();
    assert_eq!(fixture.events("outcome", &child).len(), 1);
}
#[tokio::test]
async fn physical_input_cancel_before_transfer_keeps_parent_ack_and_closes_original_child() {
    let fixture = Fixture::new(Kind::Stream).await;
    let session = fixture.core.start(&fixture.profile.id).await.unwrap();
    let input = read(&session).await;
    input.context.cancel().cancel();
    assert!(session.submit_native_input(input).await.is_err());
    session.close().await.unwrap();
    let child = fixture.source_effect(Kind::Stream);
    assert_eq!(fixture.events("dispatch", &child).len(), 1);
    assert_eq!(fixture.events("outcome", &child).len(), 1);
    let parent = EffectRequest::PortCall {
        shape: view("source", "input"),
        op: OpName::parse("read").unwrap(),
    };
    let outcomes = fixture.events("outcome", &parent);
    assert_eq!(outcomes.len(), 1);
    assert_eq!(
        outcomes[0]
            .payload
            .get("outcome")
            .and_then(|value| value.get("status"))
            .and_then(CanonValue::as_str),
        Some("succeeded")
    );
}

#[tokio::test]
async fn physical_input_saturated_tasks_keep_original_child_terminal_delivery() {
    let fixture = Fixture::new(Kind::Stream).await;
    let session = fixture.core.start(&fixture.profile.id).await.unwrap();
    let input = read(&session).await;
    let control = input.context.control().clone();
    let limit = tobkiri_gate::CoreControlLimits::default().max_tasks;
    let mut occupied = Vec::new();
    for _ in 0..limit {
        match control.reserve(tobkiri_gate::ControlKind::Task, 1) {
            Ok(reservation) => occupied.push(reservation),
            Err(_) => break,
        }
    }
    assert_eq!(
        control.usage().count(tobkiri_gate::ControlKind::Task),
        limit
    );
    input.context.cancel().cancel();
    assert!(session.submit_native_input(input).await.is_err());
    // Task saturation can independently make physical disposal unavailable.
    // That must remain an error/unknown in the original outcome; it must not
    // prevent delivery of the already-admitted child's terminal audit record.
    let closed = tokio::time::timeout(std::time::Duration::from_secs(5), session.close())
        .await
        .expect("terminal delivery must not wait for a new Task reservation");
    let child = fixture.source_effect(Kind::Stream);
    assert_eq!(fixture.events("dispatch", &child).len(), 1);
    assert_eq!(fixture.events("outcome", &child).len(), 1, "{closed:?}");
    drop(occupied);
}
