//! Actual domain DATA from a small component, not an original-App startup proof.
use super::*;
use crate::{BootstrapModule, BootstrapRoots};
use std::{
    fs::File,
    path::{Path, PathBuf},
};
use tobkiri_core::RuntimeOwner;

struct Source {
    root: PathBuf,
    _directory: File,
}
impl BootstrapInputOwner for Source {
    fn with_roots<R>(
        &self,
        operation: impl for<'a> FnOnce(BootstrapRoots<'a>) -> R,
    ) -> std::result::Result<R, NativeDataIoError> {
        Ok(operation(BootstrapRoots {
            runtime: &self.root,
            working_directory: &self.root,
            development_workspace: None,
        }))
    }
    fn check_live(&self) -> std::result::Result<(), NativeDataIoError> {
        Ok(())
    }
}
static INDEX: BootstrapModuleIndex = BootstrapModuleIndex {
    sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    modules: &[BootstrapModule {
        module: "fixture",
        relative_file: "fixture.py",
    }],
};
const MESSAGE: &str = "actual refusal λ";
const DETAILS: &str = " { \"number\": 1.0, \"items\": [2, 1] }\n";

async fn domain(root: &Path) -> Result<NativeDataError> {
    let owner = RuntimeOwner::open(root.join("data"))?;
    let invocation = owner.begin_native_setup()?;
    let engine = WasmEngine::new_with_control(invocation.control())?;
    let escape = |text: &str| {
        text.bytes()
            .map(|byte| format!("\\{byte:02x}"))
            .collect::<String>()
    };
    let source = r#"(component
      (type $code (enum "not-initialized" "already-initialized" "invalid-frame"
        "invalid-reference" "unsupported-value" "method-raised"))
      (type $error (record (field "code" $code) (field "message" string) (field "details" string)))
      (type $unit (result (error $error)))
      (type $text (result string (error $error)))
      (core module $m
        (memory (export "memory") 1)
        (global $next (mut i32) (i32.const 8192))
        (data (i32.const 512) "@MESSAGE@")
        (data (i32.const 2048) "@DETAILS@")
        (func (export "realloc") (param i32 i32 i32 i32) (result i32)
          global.get $next global.get $next local.get 3 i32.add global.set $next)
        (func (export "refuse") (param i32 i32) (result i32)
          i32.const 128 i32.const 1 i32.store
          i32.const 132 i32.const 5 i32.store
          i32.const 136 i32.const 512 i32.store
          i32.const 140 i32.const @MESSAGE_LEN@ i32.store
          i32.const 144 i32.const 2048 i32.store
          i32.const 148 i32.const @DETAILS_LEN@ i32.store
          i32.const 128))
      (core instance $m (instantiate $m))
      (alias core export $m "memory" (core memory $memory))
      (alias core export $m "realloc" (core func $realloc))
      (alias core export $m "refuse" (core func $refuse))
      (func $init (param "payload" string) (result $unit)
        (canon lift (core func $refuse) (memory $memory) (realloc $realloc)))
      (func $text (param "payload" string) (result $text)
        (canon lift (core func $refuse) (memory $memory) (realloc $realloc)))
      (instance $runtime
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "init" (func $init)) (export "bootstrap-profile-id" (func $text))
        (export "host-resource-root" (func $text)) (export "bundled-profile-root" (func $text)))
      (instance $transport
        (export "error-code" (type $code)) (export "error" (type $error))
        (export "describe-value" (func $text)))
      (export "tobkiri-ui:harness-native/runtime@1.0.0" (instance $runtime))
      (export "tobkiri-ui:harness-native/transport@1.0.0" (instance $transport)))"#
        .replace("@MESSAGE@", &escape(MESSAGE))
        .replace("@DETAILS@", &escape(DETAILS))
        .replace("@MESSAGE_LEN@", &MESSAGE.len().to_string())
        .replace("@DETAILS_LEN@", &DETAILS.len().to_string());
    let component = Component::new(engine.engine(), wat::parse_str(source)?)?;
    let source = Source {
        root: root.to_owned(),
        _directory: File::open(root)?,
    };
    match NativeBootstrap::prepare(&engine, &component, invocation, source, &INDEX).await? {
        Err(domain) => Ok(domain),
        Ok(runner) => {
            runner.finish().await?;
            bail!("domain fixture unexpectedly initialized");
        }
    }
}

#[tokio::test(flavor = "current_thread")]
async fn successful_cleanup_preserves_the_actual_domain_buffers() -> Result<()> {
    let root = tempfile::tempdir()?;
    let domain = domain(root.path()).await?;
    let pointer = domain.message().as_bytes().as_ptr();
    let domain = initialization_domain_after_cleanup(domain, Ok(()))?;
    assert_eq!(domain.code().as_bytes(), b"method-raised");
    assert_eq!(domain.message().as_bytes(), MESSAGE.as_bytes());
    assert_eq!(domain.details().unwrap().as_bytes(), DETAILS.as_bytes());
    assert_eq!(domain.message().as_bytes().as_ptr(), pointer);
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn cleanup_failure_retains_domain_bytes_and_the_actual_cleanup_error() -> Result<()> {
    let root = tempfile::tempdir()?;
    let domain = domain(root.path()).await?;
    let cleanup = anyhow::Error::new(NativeDataIoError::CompletionUnknown);
    let original: &(dyn std::error::Error + 'static) = cleanup.as_ref();
    let pointer = original as *const dyn std::error::Error;
    let failure = initialization_domain_after_cleanup(domain, Err(cleanup))
        .err()
        .expect("cleanup failure was lost");
    let outer: &(dyn std::error::Error + 'static) = failure.as_ref();
    let failure = outer
        .downcast_ref::<BootstrapDomainCleanupFailure>()
        .expect("typed domain/cleanup branches were lost");
    assert_eq!(failure.domain.code().as_bytes(), b"method-raised");
    assert_eq!(failure.domain.message().as_bytes(), MESSAGE.as_bytes());
    assert_eq!(
        failure.domain.details().unwrap().as_bytes(),
        DETAILS.as_bytes()
    );
    let retained: &(dyn std::error::Error + 'static) = failure.cleanup.as_ref();
    assert!(std::ptr::eq(retained, pointer));
    assert!(retained.is::<NativeDataIoError>());
    Ok(())
}
