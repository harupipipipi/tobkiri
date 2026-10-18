//! Root-only ordinary WasmEngine validation of fixed known generated WAT.
//! No Store invocation, key access, authority fixture, payload input or mock.
use tobkiri_wasm::WasmEngine;
use wasmtime::component::Component;
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

fn original_common_types() -> &'static str {
    r#"(type $code (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
       (type $error (record (field "code" $code) (field "message" string)))
       (type $handle (record (field "id" u64)))
       (type $init-result (result (error $error)))"#
}
fn original_sink_component(kind: Kind, wrong_kind: bool) -> Vec<u8> {
    let operation_kind = if wrong_kind { Kind::Callback } else { kind };
    let (signature, call, name) = match operation_kind {
        Kind::Stream => (
            r#"(type $items (list string)) (type $result (result $items (error $error)))
              (export "pull" (func (param "s" $handle) (param "max" u32) (result $result)))"#,
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
              (export "invoke-callback" (func (param "c" $handle) (param "args" string) (result $result)))"#,
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
      (export "local:sink/input@1.0.0" (instance $input)))"#, types=original_common_types(), handle=kind.handle())).unwrap()
}

#[tokio::test]
async fn known_generated_input_components_report_concrete_validation_and_accept_named_imports() {
    let engine = WasmEngine::new().unwrap();
    // The original sink is known generated fixture bytes. Its concrete error
    // proves this correction addresses an actual validator boundary.
    let Err(original) = Component::new(
        engine.engine(),
        original_sink_component(Kind::Stream, false),
    ) else {
        panic!("the original unnamed imported record fixture must be rejected");
    };
    let concrete: String = format!("{original:#}").chars().take(4096).collect();
    eprintln!("known-generated-original-sink-validation: {concrete}");
    assert!(
        concrete.contains("not valid to be used as import"),
        "{concrete}"
    );
    for (name, bytes) in [
        ("source-stream", source_component(Kind::Stream)),
        ("source-callback", source_component(Kind::Callback)),
        ("sink-stream", sink_component(Kind::Stream, false)),
        ("sink-callback", sink_component(Kind::Callback, false)),
        (
            "sink-stream-wrong-callback-use",
            sink_component(Kind::Stream, true),
        ),
    ] {
        if let Err(error) = Component::new(engine.engine(), bytes) {
            let concrete: String = format!("{error:#}").chars().take(4096).collect();
            panic!("known-generated-{name}-validation: {concrete}");
        }
    }
}
