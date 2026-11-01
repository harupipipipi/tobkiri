//! Verified component -> real Core User entry -> picker -> registered FS operations.
//! The trusted UI chooses an opaque candidate; no fixture creates a Gate proof.
use super::*;
use std::{fs, path::PathBuf, sync::Mutex};
use tobkiri_canon::{
    AllocationBudget, CanonValue, CoreBuffer,
    ids::{OpName, ShapeRef, UiSessionId},
};
use tobkiri_gate::{EffectRequest, FsPermission, HostError, UiEmbed};
use tobkiri_manifest::{
    FileEntry, PackManifest, PortDeclaration, Ports, RuntimeSpec, ShapeDefinition, ShapeKind,
};

#[derive(Default)]
struct Picker(Mutex<Option<String>>);
#[async_trait::async_trait]
impl TrustedPrompt for Picker {
    async fn prompt_approval(&self, _: ApprovalPrompt) -> ApprovalAnswer {
        ApprovalAnswer::Approve
    }
}
#[async_trait::async_trait]
impl HostIo for Picker {
    async fn pick_directory_budgeted(
        &self,
        _: &str,
        max: usize,
        budget: Arc<dyn AllocationBudget>,
    ) -> Result<Option<CoreBuffer>, HostError> {
        let selected = self.0.lock().unwrap();
        selected
            .as_ref()
            .map(|id| {
                if id.len() > max {
                    return Err(HostError("candidate bound".into()));
                }
                CoreBuffer::copy_from(id.as_bytes(), &budget)
                    .map_err(|_| HostError("candidate allocation".into()))
            })
            .transpose()
    }
    fn display(&self, _: &str, _: &CanonValue) {}
    async fn terminal_read_line(&self, _: &str) -> Result<String, HostError> {
        Err(HostError("unavailable".into()))
    }
    fn terminal_write(&self, _: &str) {}
    fn embed_ui(&self, _: UiEmbed) -> Result<UiSessionId, HostError> {
        Err(HostError("unavailable".into()))
    }
    async fn pick_directory(&self, _: &str) -> Option<PathBuf> {
        panic!("guest must use budgeted candidate picker")
    }
}

fn pack(directory: &std::path::Path, permissions: PermissionSet) -> VerifiedPack {
    let component = wat::parse_str(r#"(component
      (type $core-types (instance
        (type $code-definition (enum "denied" "approval-required" "cancelled" "not-found" "invalid" "limit-exceeded" "conflict" "unavailable" "internal"))
        (export "error-code" (type $named-code (eq $code-definition)))
        (type $error-definition (record (field "code" $named-code) (field "message" string)))
        (export "error" (type (eq $error-definition)))))
      (import "tobkiri:core/types@1.0.0" (instance $types-import (type $core-types)))
      (alias export $types-import "error-code" (type $error-code))
      (alias export $types-import "error" (type $error))
      (type $init-result (result (error $error)))
      (type $host-io (instance
        (type $pick-result (result string (error $error)))
        (export "pick-directory" (func (param "purpose" string) (result $pick-result)))))
      (type $fs-imports (instance
        (export "dir" (type $dir (sub resource)))
        (type $own-dir (own $dir))
        (type $borrow-dir (borrow $dir))
        (type $open-result (result $own-dir (error $error)))
        (type $bytes (list u8))
        (type $read-result (result $bytes (error $error)))
        (type $entry-definition (record (field "name" string) (field "is-dir" bool) (field "size" u64)))
        (export "entry" (type $entry (eq $entry-definition)))
        (type $entries (list $entry))
        (type $list-result (result $entries (error $error)))
        (type $stat-result (result $entry (error $error)))
        (type $unit-result (result (error $error)))
        (export "open-grant" (func (param "grant" string) (result $open-result)))
        (export "[method]dir.read" (func (param "self" $borrow-dir) (param "path" string) (param "offset" u64) (param "len" u32) (result $read-result)))
        (export "[method]dir.list" (func (param "self" $borrow-dir) (param "path" string) (result $list-result)))
        (export "[method]dir.stat" (func (param "self" $borrow-dir) (param "path" string) (result $stat-result)))
        (export "[method]dir.write" (func (param "self" $borrow-dir) (param "path" string) (param "data" $bytes) (param "append" bool) (result $unit-result)))
        (export "[method]dir.rename" (func (param "self" $borrow-dir) (param "src" string) (param "dst" string) (result $unit-result)))
        (export "[method]dir.remove" (func (param "self" $borrow-dir) (param "path" string) (result $unit-result)))
        (export "[method]dir.mkdir" (func (param "self" $borrow-dir) (param "path" string) (result $unit-result)))))
      (import "tobkiri:core/host-io@1.0.0" (instance $io (type $host-io)))
      (import "tobkiri:core/fs@1.0.0" (instance $fs (type $fs-imports)))
      (alias export $io "pick-directory" (func $pick))
      (alias export $fs "dir" (type $dir))
      (alias export $fs "open-grant" (func $open))
      (alias export $fs "[method]dir.read" (func $read))
      (alias export $fs "[method]dir.list" (func $list))
      (alias export $fs "[method]dir.stat" (func $stat))
      (alias export $fs "[method]dir.write" (func $write))
      (alias export $fs "[method]dir.rename" (func $rename))
      (alias export $fs "[method]dir.remove" (func $remove))
      (alias export $fs "[method]dir.mkdir" (func $mkdir))
      (core module $allocator
        (memory (export "memory") 1 1)
        (global $next (mut i32) (i32.const 1024))
        (func (export "realloc") (param i32 i32) (param $align i32) (param $size i32) (result i32)
          (local $at i32)
          global.get $next local.get $align i32.const 1 i32.sub i32.add
          local.get $align i32.const 1 i32.sub i32.const -1 i32.xor i32.and local.tee $at
          local.get $size i32.add global.set $next local.get $at))
      (core instance $a (instantiate $allocator))
      (alias core export $a "memory" (core memory $memory))
      (alias core export $a "realloc" (core func $realloc))
      (core func $pick-lowered (canon lower (func $pick) (memory $memory) (realloc $realloc)))
      (core func $open-lowered (canon lower (func $open) (memory $memory) (realloc $realloc)))
      (core func $read-lowered (canon lower (func $read) (memory $memory) (realloc $realloc)))
      (core func $list-lowered (canon lower (func $list) (memory $memory) (realloc $realloc)))
      (core func $stat-lowered (canon lower (func $stat) (memory $memory) (realloc $realloc)))
      (core func $write-lowered (canon lower (func $write) (memory $memory) (realloc $realloc)))
      (core func $rename-lowered (canon lower (func $rename) (memory $memory) (realloc $realloc)))
      (core func $remove-lowered (canon lower (func $remove) (memory $memory) (realloc $realloc)))
      (core func $mkdir-lowered (canon lower (func $mkdir) (memory $memory) (realloc $realloc)))
      (core func $drop-dir (canon resource.drop $dir))
      (core module $body
        (import "memory" "memory" (memory 1 1))
        (import "io" "pick" (func $pick (param i32 i32 i32)))
        (import "fs" "open" (func $open (param i32 i32 i32)))
        (import "fs" "read" (func $read (param i32 i32 i32 i64 i32 i32)))
        (import "fs" "list" (func $list (param i32 i32 i32 i32)))
        (import "fs" "stat" (func $stat (param i32 i32 i32 i32)))
        (import "fs" "write" (func $write (param i32 i32 i32 i32 i32 i32 i32)))
        (import "fs" "rename" (func $rename (param i32 i32 i32 i32 i32 i32)))
        (import "fs" "remove" (func $remove (param i32 i32 i32 i32)))
        (import "fs" "mkdir" (func $mkdir (param i32 i32 i32 i32)))
        (import "fs" "drop" (func $drop (param i32)))
        (data (i32.const 256) "workspace")
        (data (i32.const 272) "data.txt")
        (data (i32.const 288) "world")
        (func (export "init") (param i32 i32) (result i32)
          i32.const 64 i32.const 0 i32.store i32.const 64)
        (func (export "shutdown"))
        (func $open-workspace (result i32)
          i32.const 256 i32.const 9 i32.const 96 call $pick
          i32.const 96 i32.load if i32.const -1 return end
          i32.const 100 i32.load i32.const 104 i32.load i32.const 128 call $open
          i32.const 128 i32.load if i32.const -1 return end
          i32.const 132 i32.load)
        (func $unit-code (result i32)
          i32.const 160 i32.load
          if (result i32) i32.const 600 i32.const 164 i32.load8_u i32.add
          else i32.const 0 end)
        (func (export "write-probe") (param $path i32) (param $len i32) (param $append i32) (result i32)
          (local $dir i32) (local $result i32)
          call $open-workspace local.tee $dir i32.const -1 i32.eq if i32.const 900 return end
          local.get $dir local.get $path local.get $len i32.const 288 i32.const 5 local.get $append i32.const 160 call $write
          call $unit-code local.tee $result i32.eqz if
            local.get $dir local.get $path local.get $len i64.const 0 i32.const 16 i32.const 160 call $read
            i32.const 160 i32.load
            if i32.const 300 i32.const 164 i32.load8_u i32.add local.set $result
            else i32.const 168 i32.load i32.const 8 i32.shl i32.const 164 i32.load i32.load8_u i32.or local.set $result end
          end
          local.get $dir call $drop local.get $result)
        (func (export "rename-probe") (param $src i32) (param $src-len i32) (param $dst i32) (param $dst-len i32) (result i32)
          (local $dir i32) (local $result i32)
          call $open-workspace local.tee $dir i32.const -1 i32.eq if i32.const 900 return end
          local.get $dir local.get $src local.get $src-len local.get $dst local.get $dst-len i32.const 160 call $rename
          call $unit-code local.set $result local.get $dir call $drop local.get $result)
        (func (export "remove-probe") (param $path i32) (param $len i32) (result i32)
          (local $dir i32) (local $result i32)
          call $open-workspace local.tee $dir i32.const -1 i32.eq if i32.const 900 return end
          local.get $dir local.get $path local.get $len i32.const 160 call $remove
          call $unit-code local.set $result local.get $dir call $drop local.get $result)
        (func (export "mkdir-probe") (param $path i32) (param $len i32) (result i32)
          (local $dir i32) (local $result i32)
          call $open-workspace local.tee $dir i32.const -1 i32.eq if i32.const 900 return end
          local.get $dir local.get $path local.get $len i32.const 160 call $mkdir
          call $unit-code local.set $result local.get $dir call $drop local.get $result)
        (func (export "list-probe") (param $path i32) (param $len i32) (result i32)
          (local $dir i32) (local $entry i32) (local $count i32) (local $index i32)
          (local $directories i32) (local $sizes i32) (local $names i32) (local $result i32)
          call $open-workspace local.tee $dir i32.const -1 i32.eq if i32.const 900 return end
          local.get $dir local.get $path local.get $len i32.const 160 call $list
          i32.const 160 i32.load
          if i32.const 400 i32.const 164 i32.load8_u i32.add local.set $result
          else
            i32.const 168 i32.load local.set $count
            block $done loop $next
              local.get $index local.get $count i32.ge_u br_if $done
              i32.const 164 i32.load local.get $index i32.const 24 i32.mul i32.add local.set $entry
              local.get $directories local.get $entry i32.load8_u offset=8 i32.add local.set $directories
              local.get $names local.get $entry i32.load offset=4 i32.add
              local.get $entry i32.load i32.load8_u i32.add local.set $names
              local.get $entry i32.load8_u offset=8 i32.eqz if
                local.get $sizes local.get $entry i64.load offset=16 i32.wrap_i64 i32.add local.set $sizes
              end
              local.get $index i32.const 1 i32.add local.set $index br $next
            end end
            local.get $count i32.const 10000 i32.mul
            local.get $directories i32.const 1000 i32.mul i32.add
            local.get $sizes i32.const 10 i32.mul i32.add local.get $names i32.add local.set $result
          end
          local.get $dir call $drop local.get $result)
        (func (export "stat-probe") (param $path i32) (param $len i32) (result i32)
          (local $dir i32) (local $result i32)
          call $open-workspace local.tee $dir i32.const -1 i32.eq if i32.const 900 return end
          local.get $dir local.get $path local.get $len i32.const 160 call $stat
          i32.const 160 i32.load
          if i32.const 500 i32.const 168 i32.load8_u i32.add local.set $result
          else
            i32.const 172 i32.load local.set $result
            i32.const 176 i32.load8_u
            if local.get $result i32.const 16777216 i32.or local.set $result
            else local.get $result i32.const 184 i64.load i32.wrap_i64 i32.const 8 i32.shl i32.or local.set $result end
          end
          local.get $dir call $drop local.get $result)
        (func (export "probe") (result i32) (local $dir i32) (local $result i32)
          i32.const 256 i32.const 9 i32.const 96 call $pick
          i32.const 96 i32.load if i32.const 100 i32.const 100 i32.load i32.add return end
          i32.const 100 i32.load i32.const 104 i32.load i32.const 128 call $open
          i32.const 128 i32.load if i32.const 200 i32.const 132 i32.load i32.add return end
          i32.const 132 i32.load local.set $dir
          local.get $dir i32.const 272 i32.const 8 i64.const 0 i32.const 5 i32.const 160 call $read
          i32.const 160 i32.load
          if (result i32) i32.const 300 i32.const 164 i32.load i32.add
          else i32.const 168 i32.load i32.const 8 i32.shl i32.const 164 i32.load i32.load8_u i32.or end
          local.set $result local.get $dir call $drop local.get $result))
      (core instance $b (instantiate $body
        (with "memory" (instance (export "memory" (memory $memory))))
        (with "io" (instance (export "pick" (func $pick-lowered))))
        (with "fs" (instance (export "open" (func $open-lowered)) (export "read" (func $read-lowered)) (export "list" (func $list-lowered)) (export "stat" (func $stat-lowered)) (export "write" (func $write-lowered)) (export "rename" (func $rename-lowered)) (export "remove" (func $remove-lowered)) (export "mkdir" (func $mkdir-lowered)) (export "drop" (func $drop-dir))))))
      (alias core export $b "init" (core func $init))
      (alias core export $b "shutdown" (core func $shutdown))
      (alias core export $b "probe" (core func $probe))
      (alias core export $b "list-probe" (core func $list-probe))
      (alias core export $b "stat-probe" (core func $stat-probe))
      (alias core export $b "write-probe" (core func $write-probe))
      (alias core export $b "rename-probe" (core func $rename-probe))
      (alias core export $b "remove-probe" (core func $remove-probe))
      (alias core export $b "mkdir-probe" (core func $mkdir-probe))
      (func $init-value (param "config" string) (result $init-result) (canon lift (core func $init) (memory $memory) (realloc $realloc)))
      (func $shutdown-value (canon lift (core func $shutdown)))
      (func $probe-value (result u32) (canon lift (core func $probe)))
      (func $list-value (param "path" string) (result u32) (canon lift (core func $list-probe) (memory $memory) (realloc $realloc)))
      (func $stat-value (param "path" string) (result u32) (canon lift (core func $stat-probe) (memory $memory) (realloc $realloc)))
      (func $write-value (param "path" string) (param "append" bool) (result u32) (canon lift (core func $write-probe) (memory $memory) (realloc $realloc)))
      (func $rename-value (param "src" string) (param "dst" string) (result u32) (canon lift (core func $rename-probe) (memory $memory) (realloc $realloc)))
      (func $remove-value (param "path" string) (result u32) (canon lift (core func $remove-probe) (memory $memory) (realloc $realloc)))
      (func $mkdir-value (param "path" string) (result u32) (canon lift (core func $mkdir-probe) (memory $memory) (realloc $realloc)))
      (instance $types (export "error-code" (type $error-code)) (export "error" (type $error)))
      (export "tobkiri:core/types@1.0.0" (instance $types))
      (instance $lifecycle (export "init" (func $init-value)) (export "shutdown" (func $shutdown-value)))
      (export "tobkiri:core/lifecycle@1.0.0" (instance $lifecycle))
      (instance $port (export "probe" (func $probe-value)) (export "list-probe" (func $list-value)) (export "stat-probe" (func $stat-value)) (export "write-probe" (func $write-value)) (export "rename-probe" (func $rename-value)) (export "remove-probe" (func $remove-value)) (export "mkdir-probe" (func $mkdir-value)))
      (export "local:fs-import-fixture/port@1.0.0" (instance $port)))"#).unwrap();
    let shape = ShapeRef::parse("local.fs-import-fixture.port@1.0").unwrap();
    let files = BTreeMap::from([
        ("component.wasm".into(), component),
        (
            "wit/port.wit".into(),
            b"package local:fs-import-fixture@1.0.0; interface port { probe: func() -> u32; list-probe: func(path: string) -> u32; stat-probe: func(path: string) -> u32; write-probe: func(path: string, append: bool) -> u32; rename-probe: func(src: string, dst: string) -> u32; remove-probe: func(path: string) -> u32; mkdir-probe: func(path: string) -> u32; }"
                .to_vec(),
        ),
        (
            "wit/port.shape.json".into(),
            br#"{"cardinality":"one","streams":{},"callbacks":{}}"#.to_vec(),
        ),
    ]);
    let manifest = PackManifest {
        format: "tobkiri.pack/1".into(),
        id: PackId::parse("local.fs-import-fixture").unwrap(),
        version: "1.0.0".into(),
        publisher_key: None,
        runtime: RuntimeSpec::Wasm {
            core_modules: Default::default(),
            component: "component.wasm".into(),
            toolchain: "wat-fs-regression/1".into(),
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
    fs::write(directory.join("fs-import-fixture.tpk"), artifact).unwrap();
    verified
}

async fn probe(allow: bool) -> CoreBuffer {
    let temporary = tempfile::tempdir().unwrap();
    let base = temporary.path().canonicalize().unwrap();
    fs::create_dir(base.join("workspace")).unwrap();
    fs::write(base.join("workspace/data.txt"), b"hello").unwrap();
    let picker = Arc::new(Picker::default());
    let core = Core::open(
        CoreConfig::for_test_data_root(base.join("data")),
        picker.clone(),
    )
    .unwrap();
    let root = core
        .register_filesystem_root(
            fs::File::open(base.join("workspace")).unwrap(),
            crate::FilesystemRights {
                read: true,
                write: false,
            },
        )
        .unwrap();
    *picker.0.lock().unwrap() = Some(root.id().into());
    let mut permissions = crate::test_support::permissions(&[]);
    permissions
        .ports
        .insert("local.fs-import-fixture.port@1.0".into());
    permissions.host_io.insert("pick-directory".into());
    permissions.fs.push(FsPermission {
        root: root.id().into(),
        read: true,
        write: false,
    });
    let verified = pack(&base, permissions.clone());
    let mut profile = crate::test_support::runtime_profile(
        &base,
        std::slice::from_ref(&verified),
        permissions.clone(),
        false,
    );
    profile.user_permissions = Some(permissions.clone());
    if !allow {
        profile
            .pack_limits
            .get_mut(&verified.manifest.id)
            .unwrap()
            .fs
            .clear();
    }
    core.save_profile(profile.clone(), None).await.unwrap();
    let session = core.start(&profile.id).await.unwrap();
    session
        .call_user_port(
            ShapeRef::parse("local.fs-import-fixture.port@1.0").unwrap(),
            OpName::parse("probe").unwrap(),
            &CanonValue::Obj(BTreeMap::new()),
        )
        .await
        .unwrap()
}

#[tokio::test]
async fn verified_guest_reads_registered_root_through_actual_core() {
    let value = probe(true).await;
    assert_eq!(value.as_str().unwrap(), "1384"); // five bytes and first byte 'h'.
}

#[tokio::test]
async fn verified_guest_cannot_use_fs_outside_its_pack_limit() {
    let value = probe(false).await;
    assert_eq!(value.as_str().unwrap(), "100"); // actual picker result error: denied.
}

struct ReadOnlyFixture {
    _temporary: tempfile::TempDir,
    core: Core,
    profile: Profile,
    session: Session,
    workspace: PathBuf,
    root: String,
}
impl ReadOnlyFixture {
    async fn new(protected_inside: bool) -> Self {
        Self::with_output_limit(protected_inside, 1024 * 1024).await
    }

    async fn with_output_limit(protected_inside: bool, output_bytes: u64) -> Self {
        Self::with_access(protected_inside, output_bytes, false, false).await
    }

    async fn writable(protected_inside: bool, pack_write: bool) -> Self {
        Self::with_access(protected_inside, 1024 * 1024, true, pack_write).await
    }

    async fn with_access(
        protected_inside: bool,
        output_bytes: u64,
        root_write: bool,
        pack_write: bool,
    ) -> Self {
        let temporary = tempfile::tempdir().unwrap();
        let base = temporary.path().canonicalize().unwrap();
        let workspace = base.join("workspace");
        fs::create_dir(&workspace).unwrap();
        fs::create_dir(workspace.join("nested")).unwrap();
        fs::write(workspace.join("data.txt"), b"hello").unwrap();
        let picker = Arc::new(Picker::default());
        let data = if protected_inside {
            workspace.join("authority")
        } else {
            base.join("data")
        };
        let core = Core::open(CoreConfig::for_test_data_root(data), picker.clone()).unwrap();
        let root = core
            .register_filesystem_root(
                fs::File::open(&workspace).unwrap(),
                crate::FilesystemRights {
                    read: true,
                    write: root_write,
                },
            )
            .unwrap();
        *picker.0.lock().unwrap() = Some(root.id().into());
        let mut permissions = crate::test_support::permissions(&[]);
        permissions
            .ports
            .insert("local.fs-import-fixture.port@1.0".into());
        permissions.host_io.insert("pick-directory".into());
        permissions.fs.push(FsPermission {
            root: root.id().into(),
            read: true,
            write: root_write,
        });
        // A finite explicit fixture budget also covers the real 1025-entry
        // scan reaching its count bound; original read-test limits are intact.
        permissions.wasm.wall_ms = 5_000;
        permissions.wasm.output_bytes = output_bytes;
        permissions.loops.max_wall_ms = 5_000;
        let verified = pack(&base, permissions.clone());
        let mut profile = crate::test_support::runtime_profile(
            &base,
            std::slice::from_ref(&verified),
            permissions.clone(),
            false,
        );
        // Root custody remains writable in the negative fixture; only the
        // actual verified provider's approved limit removes mutation rights.
        profile
            .pack_limits
            .get_mut(&verified.manifest.id)
            .unwrap()
            .fs[0]
            .write = pack_write;
        profile.ceiling.audit_read = true;
        permissions.audit_read = true;
        profile.user_permissions = Some(permissions);
        core.save_profile(profile.clone(), None).await.unwrap();
        let session = core.start(&profile.id).await.unwrap();
        Self {
            _temporary: temporary,
            core,
            profile,
            session,
            workspace,
            root: root.id().into(),
        }
    }

    async fn invoke(&self, operation: &str, path: &str) -> CoreBuffer {
        self.invoke_args(
            operation,
            BTreeMap::from([("path".into(), CanonValue::Str(path.into()))]),
        )
        .await
    }

    async fn write(&self, path: &str, append: bool) -> CoreBuffer {
        self.invoke_args(
            "write-probe",
            BTreeMap::from([
                ("path".into(), CanonValue::Str(path.into())),
                ("append".into(), CanonValue::Bool(append)),
            ]),
        )
        .await
    }

    async fn rename(&self, src: &str, dst: &str) -> CoreBuffer {
        self.invoke_args(
            "rename-probe",
            BTreeMap::from([
                ("src".into(), CanonValue::Str(src.into())),
                ("dst".into(), CanonValue::Str(dst.into())),
            ]),
        )
        .await
    }

    async fn invoke_args(&self, operation: &str, args: BTreeMap<String, CanonValue>) -> CoreBuffer {
        self.session
            .call_user_port(
                ShapeRef::parse("local.fs-import-fixture.port@1.0").unwrap(),
                OpName::parse(operation).unwrap(),
                &CanonValue::Obj(args),
            )
            .await
            .unwrap_or_else(|error| {
                panic!("filesystem guest operation {operation} failed: {error}")
            })
    }

    async fn audit(&self) -> Vec<tobkiri_store::AuditRow> {
        use tobkiri_gate::{Gate, Outcome};
        let gate = &self.session.gate;
        let cx = self
            .core
            .user_context(&self.profile, gate)
            .unwrap()
            .with_revision(self.session.plan_digest().clone());
        let audit = AuditLog::open(
            &self.core.profiles.root,
            &self.profile.id,
            AuditQuota::default(),
        )
        .unwrap();
        let grant = gate
            .authorize(
                &cx,
                EffectRequest::AuditRead,
                AuditLog::read_digest(0, 200).unwrap(),
            )
            .await
            .unwrap();
        let id = grant.id().clone();
        let rows = audit.read(&cx, 0, 200, grant).unwrap();
        gate.record_outcome(id, Outcome::Succeeded).await.unwrap();
        rows
    }

    fn outcomes<'a>(
        &self,
        rows: &'a [tobkiri_store::AuditRow],
        operation: tobkiri_gate::FsOperation,
        path: &str,
    ) -> Vec<&'a tobkiri_store::AuditRow> {
        let effect = EffectRequest::Fs {
            root: self.root.clone(),
            path: path.into(),
            operation,
        };
        let digest = CanonValue::Str(effect.digest().unwrap().to_string());
        rows.iter()
            .filter(|row| {
                row.event.kind == "outcome"
                    && row.event.payload.get("effect_digest") == Some(&digest)
            })
            .collect()
    }
}

fn assert_status(row: &tobkiri_store::AuditRow, status: &str) {
    assert_eq!(
        row.event
            .payload
            .get("outcome")
            .and_then(|outcome| outcome.get("status")),
        Some(&CanonValue::Str(status.into()))
    );
}

#[tokio::test]
async fn verified_guest_lists_exact_entry_records_and_repeated_scans_restart() {
    let fixture = ReadOnlyFixture::new(false).await;
    // The guest inspects canonical list/record layout and all three fields:
    // 2 entries*10000 + 1 directory*1000 + 5 file bytes*10 +
    // (8+'d') + (6+'n'). No ordering assumption is made about readdir.
    for _ in 0..2 {
        assert_eq!(
            fixture.invoke("list-probe", "").await.as_str().unwrap(),
            "21274"
        );
    }
    let rows = fixture.audit().await;
    let outcomes = fixture.outcomes(&rows, tobkiri_gate::FsOperation::List, "");
    assert_eq!(
        outcomes.len(),
        2,
        "each actual list has one immutable terminal"
    );
    assert_ne!(
        outcomes[0].event.payload.get("grant"),
        outcomes[1].event.payload.get("grant")
    );
    for row in outcomes {
        assert_status(row, "succeeded");
    }
}

#[tokio::test]
async fn verified_guest_stats_files_zero_sizes_directories_and_root() {
    let fixture = ReadOnlyFixture::new(false).await;
    fs::write(fixture.workspace.join("nested/value.txt"), b"123456").unwrap();
    fs::write(fixture.workspace.join("nested/empty.bin"), []).unwrap();
    for (path, expected) in [
        ("data.txt", "1288"),
        ("nested/value.txt", "1545"),
        ("nested/empty.bin", "9"),
        ("nested", "16777222"),
        ("", "16777216"),
    ] {
        assert_eq!(
            fixture.invoke("stat-probe", path).await.as_str().unwrap(),
            expected,
            "stat path {path}"
        );
    }
    let rows = fixture.audit().await;
    for path in [
        "data.txt",
        "nested/value.txt",
        "nested/empty.bin",
        "nested",
        "",
    ] {
        let outcomes = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, path);
        assert_eq!(outcomes.len(), 1);
        assert_status(outcomes[0], "succeeded");
    }
}

#[tokio::test]
async fn verified_guest_list_omits_protected_data_and_stat_denies_it_and_escape() {
    let fixture = ReadOnlyFixture::new(true).await;
    assert_eq!(
        fixture.invoke("list-probe", "").await.as_str().unwrap(),
        "21274"
    );
    assert_eq!(
        fixture
            .invoke("stat-probe", "authority")
            .await
            .as_str()
            .unwrap(),
        "500"
    ); // denied
    assert_eq!(
        fixture
            .invoke("stat-probe", "../data.txt")
            .await
            .as_str()
            .unwrap(),
        "504"
    ); // invalid before effect
    let rows = fixture.audit().await;
    let list = fixture.outcomes(&rows, tobkiri_gate::FsOperation::List, "");
    assert_eq!(list.len(), 1);
    assert_status(list[0], "succeeded");
    let protected = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, "authority");
    assert_eq!(protected.len(), 1);
    assert_status(protected[0], "failed");
    assert!(
        fixture
            .outcomes(&rows, tobkiri_gate::FsOperation::Stat, "../data.txt")
            .is_empty(),
        "invalid traversal must not consume a filesystem effect grant"
    );
}

#[cfg(unix)]
#[tokio::test]
async fn verified_guest_stat_does_not_follow_a_leaf_symlink_outside_registered_root() {
    let fixture = ReadOnlyFixture::new(false).await;
    let outside = fixture
        .workspace
        .parent()
        .unwrap()
        .join("outside-private.txt");
    fs::write(&outside, b"outside data must not become stat metadata").unwrap();
    std::os::unix::fs::symlink(&outside, fixture.workspace.join("outside-link")).unwrap();
    assert_eq!(
        fixture
            .invoke("stat-probe", "data.txt")
            .await
            .as_str()
            .unwrap(),
        "1288",
        "the same registered root must allow an ordinary file"
    );
    assert_eq!(
        fixture
            .invoke("stat-probe", "outside-link")
            .await
            .as_str()
            .unwrap(),
        "500"
    ); // fstatat(AT_SYMLINK_NOFOLLOW) rejects S_IFLNK as denied before openat.
    let rows = fixture.audit().await;
    let outcomes = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, "outside-link");
    assert_eq!(outcomes.len(), 1);
    assert_status(outcomes[0], "failed");
}

#[tokio::test]
async fn verified_guest_list_count_limit_errors_without_publishing_truncated_entries() {
    let fixture = ReadOnlyFixture::new(false).await;
    fs::create_dir(fixture.workspace.join("many")).unwrap();
    for index in 0..1025 {
        fs::write(fixture.workspace.join(format!("many/item-{index:04}")), []).unwrap();
    }
    // The existing native backend reports its closed count-bound denial;
    // an adapter that returned a truncated success could not return 400.
    assert_eq!(
        fixture.invoke("list-probe", "many").await.as_str().unwrap(),
        "400"
    );
    assert_eq!(
        fixture
            .invoke("stat-probe", "data.txt")
            .await
            .as_str()
            .unwrap(),
        "1288"
    );
    let rows = fixture.audit().await;
    let list = fixture.outcomes(&rows, tobkiri_gate::FsOperation::List, "many");
    assert_eq!(list.len(), 1);
    assert_status(list[0], "failed");
    let stat = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, "data.txt");
    assert_eq!(stat.len(), 1);
    assert_status(stat[0], "succeeded");
}

#[tokio::test]
async fn verified_guest_stat_conversion_limit_records_failure_without_metadata_publication() {
    // The opaque picker token fits. The 64-byte path also fits its actual
    // 75-byte caller JSON, while the entry's bounded name+metadata exceeds 80.
    let fixture = ReadOnlyFixture::with_output_limit(false, 80).await;
    let name = "n".repeat(64);
    fs::write(fixture.workspace.join(&name), b"hello").unwrap();
    assert_eq!(
        fixture.invoke("stat-probe", &name).await.as_str().unwrap(),
        "505"
    );
    assert_eq!(
        fixture
            .invoke("stat-probe", "data.txt")
            .await
            .as_str()
            .unwrap(),
        "1288"
    );
    let rows = fixture.audit().await;
    let limited = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, &name);
    assert_eq!(limited.len(), 1);
    assert_status(limited[0], "failed");
    let healthy = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, "data.txt");
    assert_eq!(healthy.len(), 1);
    assert_status(healthy[0], "succeeded");
}

#[tokio::test]
async fn verified_guest_task_admission_denial_records_no_fs_terminal() {
    let fixture = ReadOnlyFixture::new(false).await;
    // Saturate the actual CoreControl Task category on the Core account the
    // guest import charges. The denial lands at whichever Task-funded
    // admission reaches the bound first; it is always before any consumed
    // work reaches the native mailbox, never inside it.
    let gate = &fixture.session.gate;
    let cx = fixture
        .core
        .user_context(&fixture.profile, gate)
        .unwrap()
        .with_revision(fixture.session.plan_digest().clone());
    let mut held = Vec::new();
    let mut saturated = false;
    for _ in 0..4096 {
        match cx
            .control()
            .reserve(tobkiri_gate::ControlKind::Task, 16 * 1024)
        {
            Ok(reservation) => held.push(reservation),
            Err(_) => {
                saturated = true;
                break;
            }
        }
    }
    assert!(saturated, "fixture must reach the actual task bound");
    let denied = fixture
        .session
        .call_user_port(
            ShapeRef::parse("local.fs-import-fixture.port@1.0").unwrap(),
            OpName::parse("stat-probe").unwrap(),
            &CanonValue::Obj(BTreeMap::from([(
                "path".into(),
                CanonValue::Str("data.txt".into()),
            )])),
        )
        .await;
    // The denial may surface as a call error or a guest error code; it must
    // never return the actual stat success for the existing file.
    if let Ok(value) = &denied {
        assert_ne!(
            value.as_str().unwrap(),
            "1288",
            "control admission denial must not return a stat success"
        );
    }
    drop(held);
    // Refunded reservations leave the account healthy: the identical effect
    // succeeds and records exactly one succeeded immutable terminal.
    assert_eq!(
        fixture
            .invoke("stat-probe", "data.txt")
            .await
            .as_str()
            .unwrap(),
        "1288"
    );
    let rows = fixture.audit().await;
    let outcomes = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, "data.txt");
    assert_eq!(
        outcomes.len(),
        1,
        "denied admission must not publish an extra filesystem terminal"
    );
    assert_status(outcomes[0], "succeeded");
}

#[tokio::test]
async fn verified_guest_open_grants_record_one_terminal_per_supervised_call() {
    let fixture = ReadOnlyFixture::new(false).await;
    // Every probe drives the real picker token -> supervised open consume ->
    // native stat. Each accepted open consumption must leave exactly one
    // immutable terminal for its own grant; none may strand mid-consume.
    for _ in 0..2 {
        assert_eq!(
            fixture
                .invoke("stat-probe", "data.txt")
                .await
                .as_str()
                .unwrap(),
            "1288"
        );
    }
    let rows = fixture.audit().await;
    let opens = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Open, "");
    assert_eq!(
        opens.len(),
        2,
        "each supervised open consumption records exactly one terminal"
    );
    assert_ne!(
        opens[0].event.payload.get("grant"),
        opens[1].event.payload.get("grant")
    );
    for row in opens {
        assert_status(row, "succeeded");
    }
    let stats = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, "data.txt");
    assert_eq!(stats.len(), 2, "one stat terminal per supervised call");
}

#[tokio::test]
async fn verified_guest_list_limit_failure_after_consume_keeps_one_terminal() {
    let fixture = ReadOnlyFixture::with_output_limit(false, 80).await;
    // A bounded entry name pushes the real narrowed output lease over its
    // 80-byte bound inside the consumed list; the bounded failure must
    // surface with exactly one immutable terminal, never a truncation.
    fs::write(fixture.workspace.join("a-very-long-file-name-padding"), []).unwrap();
    let denied_output = fixture.invoke("list-probe", "").await;
    let denied = denied_output.as_str().unwrap();
    assert!(
        denied.starts_with('4'),
        "post-consume bounded failure must be an error, got {denied}"
    );
    assert_eq!(
        fixture
            .invoke("stat-probe", "data.txt")
            .await
            .as_str()
            .unwrap(),
        "1288"
    );
    let rows = fixture.audit().await;
    let list = fixture.outcomes(&rows, tobkiri_gate::FsOperation::List, "");
    assert_eq!(
        list.len(),
        1,
        "the consumed list records exactly one terminal"
    );
    assert_status(list[0], "failed");
    let stat = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Stat, "data.txt");
    assert_eq!(stat.len(), 1);
    assert_status(stat[0], "succeeded");
}

#[tokio::test]
async fn verified_guest_mutates_registered_root_and_reads_back_actual_bytes() {
    use tobkiri_gate::FsOperation;
    let fixture = ReadOnlyFixture::writable(false, true).await;
    assert_eq!(
        fixture.write("data.txt", false).await.as_str().unwrap(),
        "1399"
    );
    assert_eq!(
        fs::read(fixture.workspace.join("data.txt")).unwrap(),
        b"world"
    );
    assert_eq!(
        fixture.write("data.txt", true).await.as_str().unwrap(),
        "2679"
    );
    assert_eq!(
        fs::read(fixture.workspace.join("data.txt")).unwrap(),
        b"worldworld"
    );
    assert_eq!(
        fixture
            .rename("data.txt", "moved.txt")
            .await
            .as_str()
            .unwrap(),
        "0"
    );
    assert!(!fixture.workspace.join("data.txt").exists());
    assert_eq!(
        fs::read(fixture.workspace.join("moved.txt")).unwrap(),
        b"worldworld"
    );
    assert_eq!(
        fixture
            .invoke("mkdir-probe", "created")
            .await
            .as_str()
            .unwrap(),
        "0"
    );
    assert!(fixture.workspace.join("created").is_dir());
    assert_eq!(
        fixture
            .invoke("remove-probe", "moved.txt")
            .await
            .as_str()
            .unwrap(),
        "0"
    );
    assert_eq!(
        fixture
            .invoke("remove-probe", "created")
            .await
            .as_str()
            .unwrap(),
        "0"
    );
    assert!(!fixture.workspace.join("moved.txt").exists());
    assert!(!fixture.workspace.join("created").exists());
    let rows = fixture.audit().await;
    for (operation, path, count) in [
        (FsOperation::Write, "data.txt", 2),
        (FsOperation::Read, "data.txt", 2),
        (FsOperation::Rename, "data.txt", 1),
        (FsOperation::Mkdir, "created", 1),
        (FsOperation::Remove, "moved.txt", 1),
        (FsOperation::Remove, "created", 1),
    ] {
        let outcomes = fixture.outcomes(&rows, operation, path);
        assert_eq!(
            outcomes.len(),
            count,
            "one terminal per actual {operation:?}"
        );
        for row in outcomes {
            assert_status(row, "succeeded");
        }
    }
}

#[tokio::test]
async fn verified_guest_pack_write_denial_precedes_all_mutating_io() {
    use tobkiri_gate::FsOperation;
    let fixture = ReadOnlyFixture::writable(false, false).await;
    assert_eq!(
        fixture.write("data.txt", true).await.as_str().unwrap(),
        "600"
    );
    assert_eq!(
        fixture
            .rename("data.txt", "moved.txt")
            .await
            .as_str()
            .unwrap(),
        "600"
    );
    assert_eq!(
        fixture
            .invoke("remove-probe", "data.txt")
            .await
            .as_str()
            .unwrap(),
        "600"
    );
    assert_eq!(
        fixture
            .invoke("mkdir-probe", "created")
            .await
            .as_str()
            .unwrap(),
        "600"
    );
    assert_eq!(
        fs::read(fixture.workspace.join("data.txt")).unwrap(),
        b"hello"
    );
    assert!(!fixture.workspace.join("moved.txt").exists());
    assert!(!fixture.workspace.join("created").exists());
    // Read rights and the actual root capability still work in this same
    // provider; only the approved pack write ceiling rejects these calls.
    assert_eq!(
        fixture
            .invoke("stat-probe", "data.txt")
            .await
            .as_str()
            .unwrap(),
        "1288"
    );
    let rows = fixture.audit().await;
    for operation in [
        FsOperation::Write,
        FsOperation::Rename,
        FsOperation::Remove,
        FsOperation::Mkdir,
    ] {
        assert!(
            fixture
                .outcomes(
                    &rows,
                    operation,
                    if operation == FsOperation::Mkdir {
                        "created"
                    } else {
                        "data.txt"
                    }
                )
                .is_empty(),
            "denied preparation must not consume a mutating filesystem grant"
        );
    }
}

#[tokio::test]
async fn verified_guest_mutation_rejects_escape_and_protected_data_without_changes() {
    use tobkiri_gate::FsOperation;
    let fixture = ReadOnlyFixture::writable(true, true).await;
    let outside = fixture.workspace.parent().unwrap().join("outside.txt");
    let protected = fixture.workspace.join("authority/sentinel");
    fs::write(&outside, b"outside").unwrap();
    fs::write(&protected, b"protected").unwrap();
    assert_eq!(
        fixture
            .write("../outside.txt", false)
            .await
            .as_str()
            .unwrap(),
        "604"
    );
    assert_eq!(
        fixture
            .write("authority/sentinel", false)
            .await
            .as_str()
            .unwrap(),
        "600"
    );
    assert_eq!(
        fixture
            .rename("data.txt", "authority/replaced")
            .await
            .as_str()
            .unwrap(),
        "600"
    );
    assert_eq!(fs::read(&outside).unwrap(), b"outside");
    assert_eq!(fs::read(&protected).unwrap(), b"protected");
    assert_eq!(
        fs::read(fixture.workspace.join("data.txt")).unwrap(),
        b"hello"
    );
    assert!(!fixture.workspace.join("authority/replaced").exists());
    let rows = fixture.audit().await;
    assert!(
        fixture
            .outcomes(&rows, FsOperation::Write, "../outside.txt")
            .is_empty()
    );
    for (operation, path) in [
        (FsOperation::Write, "authority/sentinel"),
        (FsOperation::Rename, "data.txt"),
    ] {
        let outcomes = fixture.outcomes(&rows, operation, path);
        assert_eq!(outcomes.len(), 1);
        assert_status(outcomes[0], "failed");
    }
}

#[tokio::test]
async fn verified_guest_failed_mutation_surfaces_commit_unknown_without_replay() {
    let fixture = ReadOnlyFixture::writable(false, true).await;
    fs::write(fixture.workspace.join("nested/child"), b"retain me").unwrap();
    // The real unlinkat(AT_REMOVEDIR) reaches the backend's mutation boundary
    // and fails for a nonempty directory. Its actual PartialUnknown receipt
    // must survive guest conversion as internal/no-replay, never success.
    assert_eq!(
        fixture
            .invoke("remove-probe", "nested")
            .await
            .as_str()
            .unwrap(),
        "608"
    );
    assert_eq!(
        fs::read(fixture.workspace.join("nested/child")).unwrap(),
        b"retain me"
    );
    let rows = fixture.audit().await;
    let outcomes = fixture.outcomes(&rows, tobkiri_gate::FsOperation::Remove, "nested");
    assert_eq!(outcomes.len(), 1, "failed mutation is never replayed");
    assert_status(outcomes[0], "failed");
    assert_eq!(
        outcomes[0]
            .event
            .payload
            .get("outcome")
            .and_then(|outcome| outcome.get("code")),
        Some(&CanonValue::Str("fs-commit-unknown".into()))
    );
}
