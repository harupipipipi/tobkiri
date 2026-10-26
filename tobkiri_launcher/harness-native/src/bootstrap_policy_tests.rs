//! A deliberately hostile test component must never turn metadata into IO.
use super::*;
use crate::{BootstrapModule, BootstrapRoots};
use std::{fs::File, path::PathBuf};
use tobkiri_core::RuntimeOwner;

struct SelectedFixture {
    root: PathBuf,
    _directory: File,
    process: u32,
}
impl BootstrapInputOwner for SelectedFixture {
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
        if self.process == std::process::id() {
            Ok(())
        } else {
            Err(NativeDataIoError::Retired)
        }
    }
}
static INDEX: BootstrapModuleIndex = BootstrapModuleIndex {
    sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    modules: &[BootstrapModule {
        module: "fixture",
        relative_file: "fixture.py",
    }],
};

#[tokio::test(flavor = "current_thread")]
async fn even_an_abi_valid_component_cannot_inspect_during_metadata_bootstrap() -> Result<()> {
    let source = r#"(component
      (type $native (instance
        (export "inventory" (func (result string)))
        (export "inspect" (func (param "payload" string) (result string)))))
      (import "tobkiri-native:harness/bindings@1.0.0" (instance $native (type $native)))
      (alias export $native "inspect" (func $inspect))
      (type $code (enum "not-initialized" "already-initialized" "invalid-frame"
        "invalid-reference" "unsupported-value" "method-raised"))
      (type $error (record (field "code" $code) (field "message" string) (field "details" string)))
      (type $unit (result (error $error)))
      (type $text (result string (error $error)))
      (core module $storage
        (memory (export "memory") 1)
        (global $next (mut i32) (i32.const 1024))
        (data (i32.const 32) "{}")
        (func (export "realloc") (param i32 i32 i32 i32) (result i32)
          local.get 0 local.get 1 i32.or if unreachable end
          local.get 2 i32.const 1 i32.ne if unreachable end
          global.get $next
          global.get $next local.get 3 i32.add global.set $next))
      (core instance $storage (instantiate $storage))
      (alias core export $storage "memory" (core memory $memory))
      (alias core export $storage "realloc" (core func $realloc))
      (core func $inspect-lowered (canon lower (func $inspect) (memory $memory) (realloc $realloc)))
      (core module $run
        (import "native" "inspect" (func $inspect (param i32 i32 i32)))
        (func (export "init") (param i32 i32) (result i32)
          i32.const 32 i32.const 2 i32.const 64 call $inspect
          i32.const 128)
        (func (export "text") (param i32 i32) (result i32) i32.const 128))
      (core instance $run (instantiate $run
        (with "native" (instance (export "inspect" (func $inspect-lowered))))))
      (alias core export $run "init" (core func $init))
      (alias core export $run "text" (core func $text))
      (func $init (param "payload" string) (result $unit)
        (canon lift (core func $init) (memory $memory) (realloc $realloc)))
      (func $text (param "payload" string) (result $text)
        (canon lift (core func $text) (memory $memory) (realloc $realloc)))
      (instance $runtime
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "init" (func $init)) (export "bootstrap-profile-id" (func $text)))
      (instance $transport
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "describe-value" (func $text)))
      (export "tobkiri-ui:harness-native/runtime@1.0.0" (instance $runtime))
      (export "tobkiri-ui:harness-native/transport@1.0.0" (instance $transport)))"#;
    let temporary = tempfile::tempdir()?;
    let root = temporary.path().canonicalize()?;
    let selected = SelectedFixture {
        root: root.clone(),
        _directory: File::open(&root)?,
        process: std::process::id(),
    };
    let owner = RuntimeOwner::open(root.join("runtime-data"))?;
    let invocation = owner.begin_native_setup()?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    let component = Component::new(engine.engine(), wat::parse_str(source)?)?;
    let failure =
        match NativeBootstrap::prepare(&engine, &component, invocation, selected, &INDEX).await {
            Err(failure) => failure,
            Ok(_) => bail!("hostile metadata component did not fail execution"),
        };
    let original = format!("{failure:#}");
    assert!(
        original.contains("native inspection is unavailable in the metadata-only bootstrap phase"),
        "failure must come from actual native inspection rejection: {original}"
    );
    Ok(())
}
