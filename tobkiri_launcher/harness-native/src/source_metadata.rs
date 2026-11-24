//! The original module's narrowly selected metadata operation, not a root grant.
use crate::{BootstrapModule, BootstrapModuleIndex};
use tobkiri_canon::{CoreBuffer, CoreValue};
use tobkiri_gate::NativeSetupInvocation;

/// A trusted native composition's explicit original-operation policy.
///
/// Neither a source index, an AppConfig path nor successful open issues this
/// policy. The original application adapter must select it deliberately and
/// retain its actual input/shutdown owner. It permits only the named module's
/// unchanged non-strict source resolution at one of its two original getters.
/// It grants no content read, enumeration, derived path or general-root access.
pub struct ModuleMetadataPolicy {
    module: &'static str,
    relative_file: &'static str,
}

/// Domain-owned source rule for the original ProfileRuntime's two root getters.
/// SDK/module knowledge stays here, outside generic Core and its filesystem API.
pub static ORIGINAL_PROFILE_SOURCE_METADATA: ModuleMetadataPolicy = ModuleMetadataPolicy {
    module: "ecosystem.defaultspack.defaultspack.profile_runtime_composition",
    relative_file: "ecosystem/defaultspack/defaultspack/profile_runtime_composition.py",
};

pub(super) fn selected(
    frame: &CoreValue,
    invocation: &NativeSetupInvocation,
    index: &'static BootstrapModuleIndex,
    policy: &'static ModuleMetadataPolicy,
) -> Option<&'static BootstrapModule> {
    exact(frame, &["format", "scope", "target", "operation", "args"])?;
    if frame.get_key("format")?.as_str() != Some("io.tobkiri.native-inspection-request.v1")
        || frame.get_key("operation")?.as_str() != Some("resolve")
    {
        return None;
    }
    let scope = frame.get_key("scope")?;
    let scope = scope.array()?;
    let p = invocation.provenance();
    let expected = [p.account_generation, p.invocation, 1, 1];
    if scope.len() != expected.len() {
        return None;
    }
    for (actual, expected) in scope.iter().zip(expected) {
        if canonical_hex(actual.as_str()?)? != expected {
            return None;
        }
    }
    let target = frame.get_key("target")?;
    exact(&target, &["binding", "steps"])?;
    if !target.get_key("steps")?.array()?.is_empty() {
        return None;
    }
    let binding = target.get_key("binding")?;
    let binding = binding.as_str()?.strip_prefix("module_")?;
    if binding.is_empty()
        || binding.len() > 3
        || (binding.len() > 1 && binding.starts_with('0'))
        || !binding.bytes().all(|c| c.is_ascii_digit())
    {
        return None;
    }
    let module = index.modules.get(binding.parse::<usize>().ok()?)?;
    if module.module != policy.module || module.relative_file != policy.relative_file {
        return None;
    }
    let args = frame.get_key("args")?;
    let args = args.array()?;
    (args.len() == 1 && args.get(0)?.as_bool() == Some(false)).then_some(module)
}

pub(super) fn exact(value: &CoreValue, names: &[&str]) -> Option<()> {
    let object = value.object()?;
    (object.len() == names.len() && names.iter().all(|name| object.get(name).is_some()))
        .then_some(())
}

pub(super) fn canonical_hex(text: &str) -> Option<u64> {
    if text.is_empty()
        || text.len() > 16
        || text.starts_with('0')
        || !text
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    {
        return None;
    }
    u64::from_str_radix(text, 16).ok().filter(|&n| n != 0)
}

pub(super) fn resolve(
    output: &mut tobkiri_wasm::NativeInspectionOutput<'_>,
    root: &std::path::Path,
    module: &BootstrapModule,
) -> Result<CoreBuffer, tobkiri_wasm::NativeDataIoError> {
    #[cfg(target_os = "macos")]
    {
        crate::source_resolve::resolve(output, root, module.relative_file)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (root, module);
        Err(output.reject_execution(tobkiri_wasm::NativeDataIoError::Unavailable))
    }
}
