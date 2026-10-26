//! Bounded failed-init observations, never a domain outcome or authority.
use std::fmt;
use tobkiri_canon::{CoreBuffer, CoreValue};
use tobkiri_gate::{NativeSetupInvocation, NativeSetupSnapshot};
use tobkiri_wasm::NativeDataObservation;

const KINDS: &[&str] = &[
    "value-error",
    "type-error",
    "runtime-error",
    "name-error",
    "import-error",
    "module-not-found-error",
    "key-error",
    "attribute-error",
    "assertion-error",
    "memory-error",
    "recursion-error",
    "os-error",
    "file-not-found-error",
    "permission-error",
    "unrepresented-exception",
];

/// The original guest execution failure plus data recorded BEFORE its runtime
/// attempted exception printing. The diagnostic is not a reconstructed error.
/// Fuel/deadline/memory failures remain outer execution failures.
pub struct OriginalInitializationFailure {
    pub original: anyhow::Error,
    pub diagnostic: CoreBuffer,
    pub observation: Option<NativeDataObservation>,
    pub capacity: Option<NativeSetupSnapshot>,
    pub elapsed_micros: u128,
}
impl fmt::Debug for OriginalInitializationFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(self, f)
    }
}
impl fmt::Display for OriginalInitializationFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("original native initialization failed; bounded pre-print observation: ")?;
        match self.diagnostic.as_str() {
            Ok(text) => f.write_str(text)?,
            Err(_) => f.write_str("unavailable UTF-8 observation")?,
        }
        write!(
            f,
            "; native={:?}; capacity={:?}; elapsed_us={}",
            self.observation, self.capacity, self.elapsed_micros
        )
    }
}
impl std::error::Error for OriginalInitializationFailure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(self.original.as_ref())
    }
}

/// Accept only this native init's closed observation. Even a valid record is
/// untrusted DATA; it cannot prove success, source rights or resource recovery.
pub(super) fn valid(frame: &CoreValue, invocation: &NativeSetupInvocation) -> bool {
    check(frame, invocation).is_some()
}
fn check(frame: &CoreValue, invocation: &NativeSetupInvocation) -> Option<()> {
    crate::source_metadata::exact(
        frame,
        &[
            "format",
            "scope",
            "phase",
            "kind",
            "args",
            "fields",
            "locations",
            "omitted",
        ],
    )?;
    if frame.get_key("format")?.as_str() != Some("io.tobkiri.native-init-diagnostic.v1") {
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
        if crate::source_metadata::canonical_hex(actual.as_str()?)? != expected {
            return None;
        }
    }
    if !matches!(
        frame.get_key("phase")?.as_str()?,
        "original-factory" | "transport-registry"
    ) {
        return None;
    }
    let kind = frame.get_key("kind")?;
    let kind = kind.as_str()?;
    if !KINDS.contains(&kind) {
        return None;
    }
    let args = frame.get_key("args")?;
    let args = args.array()?;
    if args.len() > 4 || args.iter().any(|arg| !scalar(&arg)) {
        return None;
    }
    let fields = frame.get_key("fields")?;
    let fields = fields.object()?;
    let allowed: &[&str] = match kind {
        "import-error" | "module-not-found-error" => &["name", "path"],
        "name-error" | "attribute-error" => &["name"],
        "os-error" | "file-not-found-error" | "permission-error" => {
            &["errno", "strerror", "filename", "filename2"]
        }
        _ => &[],
    };
    if fields.len() > allowed.len() {
        return None;
    }
    for (name, value) in fields.iter() {
        if !allowed.contains(&name) {
            return None;
        }
        if !scalar(&value) {
            return None;
        }
    }
    let locations = frame.get_key("locations")?;
    let locations = locations.array()?;
    if locations.len() > 4 {
        return None;
    }
    for location in locations.iter() {
        let parts = location.array()?;
        if parts.len() != 3
            || parts.get(0)?.as_str()?.len() > 256
            || parts.get(1)?.as_str()?.len() > 64
            || parts.get(2)?.as_i64()? <= 0
            || parts.get(2)?.as_i64()? > 9_007_199_254_740_991
        {
            return None;
        }
    }
    frame.get_key("omitted")?.as_bool()?;
    if kind == "unrepresented-exception"
        && (!args.is_empty()
            || !fields.is_empty()
            || !locations.is_empty()
            || frame.get_key("omitted")?.as_bool() != Some(true))
    {
        return None;
    }
    Some(())
}
fn scalar(value: &CoreValue) -> bool {
    null(value)
        || value.as_bool().is_some()
        || value
            .as_i64()
            .is_some_and(|n| (-9_007_199_254_740_991..=9_007_199_254_740_991).contains(&n))
        || value.as_str().is_some_and(|s| s.len() <= 512)
}
fn null(value: &CoreValue) -> bool {
    matches!(value.inspect(), tobkiri_canon::CoreRef::Null)
}
