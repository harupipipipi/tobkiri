(component
  (type $core-types (instance
    (type $code-definition (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
    (export "error-code" (type $named-code (eq $code-definition)))
    (type $error-definition (record (field "code" $named-code) (field "message" string)))
    (export "error" (type (eq $error-definition)))))
  (import "tobkiri:core/types@1.2.0" (instance $types-import (type $core-types)))
  (alias export $types-import "error-code" (type $code))
  (alias export $types-import "error" (type $error))
  (type $unit-result (result (error $error)))
  (type $storage (instance
    (export "store" (type $store (sub resource)))
    (type $own-store (own $store))
    (type $borrow-store (borrow $store))
    (type $open-result (result $own-store (error $error)))
    (type $bytes (list u8))
    (type $maybe-bytes (option $bytes))
    (type $get-result (result $maybe-bytes (error $error)))
    (type $put-result (result (error $error)))
    (export "open" (func (param "name" string) (result $open-result)))
    (export "open-foreign" (func (param "owner-pack" string) (param "name" string) (result $open-result)))
    (export "open-user-global" (func (param "name" string) (result $open-result)))
    (export "[method]store.kv-get" (func (param "self" $borrow-store) (param "key" string) (result $get-result)))
    (export "[method]store.kv-put" (func (param "self" $borrow-store) (param "key" string) (param "value" $bytes) (result $put-result)))))
  (import "tobkiri:core/storage@1.2.0" (instance $storage-import (type $storage)))
  (alias export $storage-import "store" (type $store))
  (alias export $storage-import "open" (func $open))
  (alias export $storage-import "open-foreign" (func $open-foreign))
  (alias export $storage-import "open-user-global" (func $open-user-global))
  (alias export $storage-import "[method]store.kv-get" (func $get))
  (alias export $storage-import "[method]store.kv-put" (func $put))
  (core module $allocator
    (memory (export "memory") 1 1)
    (global $next (mut i32) (i32.const 4096))
    (func (export "realloc") (param $old i32) (param $old-size i32) (param $align i32) (param $size i32) (result i32)
      (local $at i32)
      global.get $next local.get $align i32.const 1 i32.sub i32.add
      i32.const 0 local.get $align i32.sub i32.and local.set $at
      local.get $size i32.const 65536 local.get $at i32.sub i32.gt_u if unreachable end
      local.get $at local.get $size i32.add global.set $next
      local.get $old-size local.get $size i32.gt_u if local.get $size local.set $old-size end
      local.get $at local.get $old local.get $old-size memory.copy
      local.get $at))
  (core instance $mem (instantiate $allocator))
  (alias core export $mem "memory" (core memory $memory))
  (alias core export $mem "realloc" (core func $realloc))
  (core func $lower-open (canon lower (func $open) (memory $memory) (realloc $realloc)))
  (core func $lower-foreign (canon lower (func $open-foreign) (memory $memory) (realloc $realloc)))
  (core func $lower-user-global (canon lower (func $open-user-global) (memory $memory) (realloc $realloc)))
  (core func $lower-get (canon lower (func $get) (memory $memory) (realloc $realloc)))
  (core func $lower-put (canon lower (func $put) (memory $memory) (realloc $realloc)))
  (core func $drop-store (canon resource.drop $store))
  (core module $probe
    (import "host" "memory" (memory 1 1))
    (import "host" "open" (func $open (param i32 i32 i32)))
    (import "host" "open-foreign" (func $open-foreign (param i32 i32 i32 i32 i32)))
    (import "host" "open-user-global" (func $open-user-global (param i32 i32 i32)))
    (import "host" "get" (func $get (param i32 i32 i32 i32)))
    (import "host" "put" (func $put (param i32 i32 i32 i32 i32 i32)))
    (import "host" "drop" (func $drop (param i32)))
    (data (i32.const 128) "@OWNER@")
    (data (i32.const 256) "marker")
    (data (i32.const 300) "shared-notes")
    (func (export "init") (param i32 i32) (result i32)
      i32.const 16 i32.const 0 i32.store8 i32.const 16)
    (func (export "shutdown"))
    ;; The host's canonical open result lives at 32. The guest's fixed owner
    ;; and key are lookup arguments; the real Pack principal comes from Core.
    (func $open-selected (param $name i32) (param $len i32)
      i32.const @FOREIGN@
      if
        i32.const 128 i32.const @OWNER_LEN@ local.get $name local.get $len i32.const 32 call $open-foreign
      else
        local.get $name local.get $len i32.const 32 call $open
      end)
    ;; Success returns the actual one-byte marker (0 for an absent key), or
    ;; 0 for a successful put. A nonzero ABI error keeps its stage and enum
    ;; ordinal, even when the error itself is the ordinal-zero `denied`.
    (func (export "read-marker") (param $name i32) (param $len i32) (result i32)
      (local $handle i32)
      local.get $name local.get $len call $open-selected
      i32.const 32 i32.load8_u if i32.const 256 i32.const 36 i32.load8_u i32.add return end
      i32.const 36 i32.load local.tee $handle i32.const 256 i32.const 6 i32.const 64 call $get
      local.get $handle call $drop
      i32.const 64 i32.load8_u if i32.const 512 i32.const 68 i32.load8_u i32.add return end
      i32.const 68 i32.load8_u i32.eqz if i32.const 0 return end
      i32.const 76 i32.load i32.const 1 i32.ne if i32.const 768 return end
      i32.const 72 i32.load i32.load8_u)
    (func (export "write-marker") (param $name i32) (param $len i32) (param $value i32) (result i32)
      (local $handle i32)
      local.get $name local.get $len call $open-selected
      i32.const 32 i32.load8_u if i32.const 256 i32.const 36 i32.load8_u i32.add return end
      i32.const 272 local.get $value i32.store8
      i32.const 36 i32.load local.tee $handle i32.const 256 i32.const 6 i32.const 272 i32.const 1 i32.const 64 call $put
      local.get $handle call $drop
      i32.const 64 i32.load8_u if i32.const 512 i32.const 68 i32.load8_u i32.add return end
      i32.const 0))
    (func (export "read-global-marker") (result i32)
      (local $handle i32)
      i32.const 300 i32.const 12 i32.const 32 call $open-user-global
      i32.const 32 i32.load8_u if i32.const 256 i32.const 36 i32.load8_u i32.add return end
      i32.const 36 i32.load local.tee $handle i32.const 256 i32.const 6 i32.const 64 call $get
      local.get $handle call $drop
      i32.const 64 i32.load8_u if i32.const 512 i32.const 68 i32.load8_u i32.add return end
      i32.const 68 i32.load8_u i32.eqz if i32.const 0 return end
      i32.const 76 i32.load i32.const 1 i32.ne if i32.const 768 return end
      i32.const 72 i32.load i32.load8_u)
    (func (export "write-global-marker") (param $value i32) (result i32)
      (local $handle i32)
      i32.const 300 i32.const 12 i32.const 32 call $open-user-global
      i32.const 32 i32.load8_u if i32.const 256 i32.const 36 i32.load8_u i32.add return end
      i32.const 272 local.get $value i32.store8
      i32.const 36 i32.load local.tee $handle i32.const 256 i32.const 6 i32.const 272 i32.const 1 i32.const 64 call $put
      local.get $handle call $drop
      i32.const 64 i32.load8_u if i32.const 512 i32.const 68 i32.load8_u i32.add return end
      i32.const 0))
  (core instance $body (instantiate $probe
    (with "host" (instance
      (export "memory" (memory $memory))
      (export "open" (func $lower-open))
      (export "open-foreign" (func $lower-foreign))
      (export "open-user-global" (func $lower-user-global))
      (export "get" (func $lower-get))
      (export "put" (func $lower-put))
      (export "drop" (func $drop-store))))))
  (alias core export $body "init" (core func $init))
  (alias core export $body "shutdown" (core func $shutdown))
  (alias core export $body "read-marker" (core func $read))
  (alias core export $body "write-marker" (core func $write))
  (alias core export $body "read-global-marker" (core func $read-global))
  (alias core export $body "write-global-marker" (core func $write-global))
  (func $init-value (param "config" string) (result $unit-result)
    (canon lift (core func $init) (memory $memory) (realloc $realloc)))
  (func $shutdown-value (canon lift (core func $shutdown)))
  (func $read-value (param "store" string) (result u32)
    (canon lift (core func $read) (memory $memory) (realloc $realloc)))
  (func $write-value (param "store" string) (param "value" u8) (result u32)
    (canon lift (core func $write) (memory $memory) (realloc $realloc)))
  (func $read-global-value (result u32)
    (canon lift (core func $read-global) (memory $memory) (realloc $realloc)))
  (func $write-global-value (param "value" u8) (result u32)
    (canon lift (core func $write-global) (memory $memory) (realloc $realloc)))
  (instance $types (export "error-code" (type $code)) (export "error" (type $error)))
  (export "tobkiri:core/types@1.2.0" (instance $types))
  (instance $lifecycle (export "init" (func $init-value)) (export "shutdown" (func $shutdown-value)))
  (export "tobkiri:core/lifecycle@1.2.0" (instance $lifecycle))
  (instance $api
    (export "read-marker" (func $read-value))
    (export "write-marker" (func $write-value))
    (export "read-global-marker" (func $read-global-value))
    (export "write-global-marker" (func $write-global-value)))
  (export "local:@PACK@/probe@1.0.0" (instance $api)))
