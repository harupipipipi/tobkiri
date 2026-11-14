//! Fixed raw-getter observations, before canonical validation or recovery.
//! Actual guest errors remain opaque objects; Host raise edges borrow the same
//! funded carrier and never reconstruct an exception from diagnostic strings.
use super::denied::{
    exact, field, frame, ownership, reference, result, same_reference, same_scope,
};
use super::*;
use tobkiri_canon::CoreRef;

/// Exact builtin type observed by the trusted bridge. Record contents remain
/// opaque; an unknown class is an ABI refusal, never a replacement None.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BootstrapBuiltinKind {
    None,
    Bool,
    Int,
    Float,
    Str,
    List,
    Tuple,
    Dict,
    Set,
    Frozenset,
    Record,
}

/// The original recovery catch test, derived by the bridge from the actual
/// registered exception class, separately from its portable kind tag.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BootstrapRecoveryCatch {
    None,
    ValueError,
    OsError,
}

/// The raw getter completed. This is not canonical validation or a Profile ID.
pub struct BootstrapIdentityValue {
    result: CoreBuffer,
    view: CoreBuffer,
    observation: CoreValue,
    builtin: BootstrapBuiltinKind,
}
impl std::fmt::Debug for BootstrapIdentityValue {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.debug_struct("BootstrapIdentityValue")
            .field("builtin", &self.builtin)
            .finish_non_exhaustive()
    }
}
impl BootstrapIdentityValue {
    pub fn result(&self) -> &CoreBuffer {
        &self.result
    }
    pub fn view(&self) -> &CoreBuffer {
        &self.view
    }
    pub fn observation(&self) -> &CoreValue {
        &self.observation
    }
    pub fn builtin_kind(&self) -> BootstrapBuiltinKind {
        self.builtin
    }
    /// 129 is the original too-long branch; its contents were not disclosed.
    pub fn string_length(&self) -> Option<usize> {
        self.observation
            .get_key("string")?
            .get_key("length")?
            .as_i64()
            .and_then(|value| usize::try_from(value).ok())
    }
    /// Funded numeric Python codepoints, including lone surrogates, only when
    /// the raw string has at most 128 codepoints. No UTF-8 conversion occurs.
    pub fn codepoints(&self) -> Option<CoreValue> {
        (self.string_length()? <= 128)
            .then(|| self.observation.get_key("string")?.get_key("codepoints"))?
    }
}

/// One actual MethodRaised exception and its shallow, provenance-limited view.
/// This adds no Source/IO/AppHandle/invocation/account clone. Its existing
/// Store graph budget identity is also held by the original CoreBuffers, with
/// the same funded DATA lifetime and original allocation-owner reap contract.
pub struct BootstrapGetterError {
    error: NativeDataError,
    view: CoreBuffer,
    observation: CoreValue,
    budget: Arc<dyn AllocationBudget>,
    recovery_catch: BootstrapRecoveryCatch,
    origin: &'static str,
}
impl BootstrapGetterError {
    pub fn error(&self) -> &NativeDataError {
        &self.error
    }
    pub fn view(&self) -> &CoreBuffer {
        &self.view
    }
    pub fn observation(&self) -> &CoreValue {
        &self.observation
    }
    pub fn recovery_catch(&self) -> BootstrapRecoveryCatch {
        self.recovery_catch
    }
    /// Fixed actual caller provenance, independently of any exception tag.
    pub fn origin(&self) -> &'static str {
        self.origin
    }

    pub(super) fn check_store(
        &self,
        budget: &Arc<dyn AllocationBudget>,
        scope: &CoreValue,
    ) -> Result<()> {
        // Scope alone is DATA. This private carrier additionally has the
        // identical actual Store allocation owner; a foreign ledger with the
        // same counters cannot substitute its object or fabricate this value.
        if !Arc::ptr_eq(&self.budget, budget)
            || !same_scope(&field(&self.observation, "scope")?, scope)
        {
            bail!("raw getter cause belongs to another actual Store");
        }
        Ok(())
    }
}
impl std::fmt::Debug for BootstrapGetterError {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.debug_struct("BootstrapGetterError")
            .field("recovery_catch", &self.recovery_catch)
            .finish_non_exhaustive()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BootstrapIdentityStage {
    Getter,
    Describe,
}

/// An ABI/transport refusal is distinct from an original getter exception.
/// Retain any prior actual ref until the caller's original Store finishes.
/// The current shallow exception qualification is at most eight builtin scalar
/// args, strings of 4096 codepoints and integers of 4096 bits. Beyond this is
/// explicitly unsupported DATA, not a new original runtime/operation limit.
pub struct BootstrapIdentityFailure {
    stage: BootstrapIdentityStage,
    origin: &'static str,
    error: NativeDataError,
    getter: Option<NativeDataError>,
    _retained: [Option<CoreBuffer>; 2],
}
impl BootstrapIdentityFailure {
    pub fn stage(&self) -> BootstrapIdentityStage {
        self.stage
    }
    pub fn origin(&self) -> &'static str {
        self.origin
    }
    pub fn error(&self) -> &NativeDataError {
        &self.error
    }
    pub fn getter_error(&self) -> Option<&NativeDataError> {
        self.getter.as_ref()
    }
}
impl std::fmt::Debug for BootstrapIdentityFailure {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.debug_struct("BootstrapIdentityFailure")
            .field("stage", &self.stage)
            .finish_non_exhaustive()
    }
}

#[derive(Debug)]
pub enum BootstrapIdentityOutcome {
    Returned(BootstrapIdentityValue),
    GetterRaised(BootstrapGetterError),
    Refused(BootstrapIdentityFailure),
}

// Runner-held, including before any fallible decode/await. Dropping a child
// future cannot drop the actual getter/ref/view before Store and IO retirement.
pub(super) struct IdentityCallCustody {
    result: Option<CoreBuffer>,
    getter: Option<NativeDataError>,
    view: Option<CoreBuffer>,
    describe_error: Option<NativeDataError>,
    #[cfg(debug_assertions)]
    view_rejected: bool,
}

enum FixedCallOutput {
    Identity(BootstrapIdentityOutcome),
    Projection(CoreBuffer),
}
impl IdentityCallCustody {
    #[cfg(debug_assertions)]
    pub(super) fn view_rejected(&self) -> bool {
        self.view_rejected
    }
    #[cfg(debug_assertions)]
    pub(super) fn assert_rejected_outputs_live(&self) {
        assert!(
            self.view.is_some() && (self.result.is_some() || self.getter.is_some()),
            "rejected getter outputs died before Store/IO retirement"
        );
    }
}

impl<S: BootstrapInputOwner> ReviewPackBorrow<'_, S> {
    /// One same-Store raw getter followed by its finite top-level observation.
    /// This standalone stage does not enter the journal/canonical/listing path.
    pub(super) async fn observe_bootstrap_profile_id(
        &mut self,
    ) -> Result<BootstrapIdentityOutcome> {
        let input = self.call.clone();
        self.observe_fixed_call(1, &input, "getter").await
    }

    pub(super) async fn observe_fixed_call(
        &mut self,
        slot: usize,
        input: &CoreBuffer,
        origin: &'static str,
    ) -> Result<BootstrapIdentityOutcome> {
        match self.fixed_call(slot, input, origin).await? {
            FixedCallOutput::Identity(outcome) => Ok(outcome),
            FixedCallOutput::Projection(_) => bail!("unexpected fixed call projection"),
        }
    }

    pub(super) async fn observe_fixed_projection(
        &mut self,
        input: &CoreBuffer,
        origin: &'static str,
    ) -> Result<std::result::Result<CoreBuffer, BootstrapIdentityOutcome>> {
        let slot = match origin {
            "bootstrap-preview" | "bootstrap-recovery-catalog" => BOOTSTRAP_PREVIEW_SLOT,
            "bootstrap-listing" => BOOTSTRAP_LISTING_SLOT,
            "bootstrap-activation-preflight" | "bootstrap-activation-match" => {
                BOOTSTRAP_ACTIVATION_DECISION_SLOT
            }
            _ => 2,
        };
        match self.fixed_call(slot, input, origin).await? {
            FixedCallOutput::Projection(rows) => Ok(Ok(rows)),
            FixedCallOutput::Identity(outcome) => Ok(Err(outcome)),
        }
    }

    pub(super) async fn observe_resolver_profile(
        &mut self,
        input: &CoreBuffer,
    ) -> Result<std::result::Result<CoreBuffer, BootstrapIdentityOutcome>> {
        match self
            .fixed_call(RESOLVE_PROFILE_SLOT, input, "resolver-profile")
            .await?
        {
            FixedCallOutput::Projection(boundary) => Ok(Ok(boundary)),
            FixedCallOutput::Identity(outcome) => Ok(Err(outcome)),
        }
    }

    pub(super) async fn observe_bootstrap_confirmation(
        &mut self,
        input: &CoreBuffer,
    ) -> Result<std::result::Result<CoreBuffer, BootstrapIdentityOutcome>> {
        match self
            .fixed_call(BOOTSTRAP_CONFIRMATION_SLOT, input, "bootstrap-confirmation")
            .await?
        {
            FixedCallOutput::Projection(value) => Ok(Ok(value)),
            FixedCallOutput::Identity(outcome) => Ok(Err(outcome)),
        }
    }

    async fn fixed_call(
        &mut self,
        slot: usize,
        input: &CoreBuffer,
        origin: &'static str,
    ) -> Result<FixedCallOutput> {
        if !matches!(
            (slot, origin),
            (1, "getter")
                | (3, "host-root")
                | (4, "bundle-root")
                | (7, "catalog")
                | (2, "manifest")
                | (2, "resolver-source-lookup")
                | (2, "bootstrap-template-lookup")
                | (2, "resolver-static-edges")
                | (2, "resolver-dynamic-edges")
                | (2, "resolver-verified-artifacts")
                | (2, "bootstrap-predecessor-source")
                | (2, "bootstrap-inactive-source")
                | (2, "bootstrap-inactive-equality")
                | (BOOTSTRAP_CONFIRMATION_SLOT, "bootstrap-confirmation")
                | (BOOTSTRAP_PREVIEW_SLOT, "bootstrap-preview")
                | (BOOTSTRAP_PREVIEW_SLOT, "bootstrap-recovery-catalog")
                | (BOOTSTRAP_LISTING_SLOT, "bootstrap-listing")
                | (
                    BOOTSTRAP_ACTIVATION_DECISION_SLOT,
                    "bootstrap-activation-preflight" | "bootstrap-activation-match"
                )
                | (RESOLVE_PROFILE_SLOT, "resolver-profile")
        ) {
            bail!("fixed original caller is unavailable");
        }
        let entry = self.work.get().phase.load(Ordering::Acquire);
        if !matches!(entry, READY | REVIEW) || self.identity_pending.is_some() {
            bail!("raw getter observation is unavailable");
        }
        let phase = match (entry, origin) {
            (REVIEW, "host-root") => REVIEW_HOST_ROOT,
            (REVIEW, "bundle-root") => REVIEW_BUNDLE_ROOT,
            (REVIEW, "catalog") => CATALOG_LOAD,
            (REVIEW, "manifest") => CATALOG_PARSE,
            (REVIEW, "resolver-source-lookup") => RESOLVER_SOURCE_LOOKUP,
            (REVIEW, "bootstrap-template-lookup") => BOOTSTRAP_TEMPLATE_LOOKUP,
            (REVIEW, "resolver-static-edges") => RESOLVER_STATIC_EDGES,
            (REVIEW, "resolver-dynamic-edges") => RESOLVER_DYNAMIC_EDGES,
            (REVIEW, "resolver-verified-artifacts") => RESOLVER_VERIFIED_ARTIFACTS,
            (REVIEW, "resolver-profile") => RESOLVE_PROFILE,
            (REVIEW, "bootstrap-predecessor-source") => BOOTSTRAP_PREDECESSOR_SOURCE,
            (REVIEW, "bootstrap-inactive-source") => BOOTSTRAP_INACTIVE_SOURCE,
            (REVIEW, "bootstrap-inactive-equality") => BOOTSTRAP_INACTIVE_EQUALITY,
            (REVIEW, "bootstrap-confirmation") => BOOTSTRAP_CONFIRMATION,
            (REVIEW, "bootstrap-preview") => BOOTSTRAP_PREVIEW,
            (REVIEW, "bootstrap-recovery-catalog") => BOOTSTRAP_RECOVERY_CATALOG,
            (REVIEW, "bootstrap-listing") => BOOTSTRAP_LISTING,
            (REVIEW, "bootstrap-activation-preflight") => BOOTSTRAP_ACTIVATION_PREFLIGHT,
            (REVIEW, "bootstrap-activation-match") => BOOTSTRAP_ACTIVATION_MATCH,
            (READY, "getter") => IDENTITY,
            (REVIEW, "getter") => REVIEW,
            _ => bail!("fixed original caller is outside review"),
        };
        self.work.get().source.check_live()?;
        if matches!(slot, 3 | 4) || phase == RESOLVE_PROFILE {
            self.work
                .get()
                .source_occurrence
                .store(SOURCE_UNUSED, Ordering::Release);
        }
        *self.identity_pending = Some(IdentityCallCustody {
            result: None,
            getter: None,
            view: None,
            describe_error: None,
            #[cfg(debug_assertions)]
            view_rejected: false,
        });
        let mut guard = RootCallGuard {
            work: (*self.work).clone(),
            io: (*self.io).clone(),
            completed: false,
        };
        self.work.get().phase.store(phase, Ordering::Release);
        let outcome = match self.identity_in_phase(phase, slot, input, origin).await {
            Err(original) => Err(self.retire_failure(original).await),
            result => result,
        };
        if self.work.get().phase.load(Ordering::Acquire) == phase {
            self.work.get().phase.store(entry, Ordering::Release);
        }
        guard.completed = true;
        outcome
    }

    async fn identity_in_phase(
        &mut self,
        phase: u8,
        slot: usize,
        input: &CoreBuffer,
        origin: &'static str,
    ) -> Result<FixedCallOutput> {
        match self.string_call_in_phase(slot, input, phase).await? {
            Ok(value) => {
                self.identity_pending
                    .as_mut()
                    .expect("admitted getter")
                    .result = Some(value)
            }
            Err(error) => {
                self.identity_pending
                    .as_mut()
                    .expect("admitted getter")
                    .getter = Some(error)
            }
        }
        let pending = self.identity_pending.as_ref().expect("admitted getter");
        if matches!(
            phase,
            RESOLVER_STATIC_EDGES
                | RESOLVER_DYNAMIC_EDGES
                | RESOLVE_PROFILE
                | BOOTSTRAP_PREDECESSOR_SOURCE
                | BOOTSTRAP_INACTIVE_SOURCE
                | BOOTSTRAP_INACTIVE_EQUALITY
                | BOOTSTRAP_CONFIRMATION
                | BOOTSTRAP_PREVIEW
                | BOOTSTRAP_RECOVERY_CATALOG
                | BOOTSTRAP_LISTING
                | BOOTSTRAP_TEMPLATE_LOOKUP
                | BOOTSTRAP_ACTIVATION_PREFLIGHT
                | BOOTSTRAP_ACTIVATION_MATCH
        ) && pending.result.is_some()
        {
            let pending = self.identity_pending.take().expect("admitted edge call");
            return Ok(FixedCallOutput::Projection(
                pending.result.expect("completed edge call"),
            ));
        }
        if pending
            .getter
            .as_ref()
            .is_some_and(|error| error.code().as_bytes() != b"method-raised")
        {
            return Ok(FixedCallOutput::Identity(
                self.identity_refused(BootstrapIdentityStage::Getter, origin),
            ));
        }
        let raised = pending.getter.is_some();
        let bytes = match &pending.getter {
            Some(error) => {
                if !error.message().as_bytes().is_empty() {
                    bail!("raw getter MethodRaised message differs from the fixed bridge");
                }
                error
                    .details()
                    .context("raw getter MethodRaised ref is missing")?
            }
            None => pending.result.as_ref().expect("actual getter result"),
        };
        let scope = CoreValue::parse_guest(self.call.as_bytes(), self.input_budget)?
            .get_key("scope")
            .context("original call scope missing")?;
        let packet = result(
            bytes,
            "io.tobkiri.native-value-result.v1",
            &scope,
            self.input_budget,
        )?;
        let root = field(&packet, "root")?;
        ownership(&packet, &root, false)?;
        let request = frame(
            self,
            if origin == "getter" {
                "io.tobkiri.native-bootstrap-identity-describe.v1"
            } else {
                "io.tobkiri.native-bootstrap-call-describe.v1"
            },
            [("root", root.clone())],
        )?;
        match self.string_call_in_phase(2, &request, phase).await? {
            Ok(value) => {
                self.identity_pending
                    .as_mut()
                    .expect("admitted getter")
                    .view = Some(value)
            }
            Err(error) => {
                self.identity_pending
                    .as_mut()
                    .expect("admitted getter")
                    .describe_error = Some(error);
                return Ok(FixedCallOutput::Identity(
                    self.identity_refused(BootstrapIdentityStage::Describe, origin),
                ));
            }
        }
        let pending = self.identity_pending.as_ref().expect("admitted getter");
        let observation = CoreValue::parse_guest(
            pending
                .view
                .as_ref()
                .expect("actual identity view")
                .as_bytes(),
            self.input_budget,
        )?;
        let validated = validate_view(&observation, &root, &scope, raised, origin);
        let (builtin, recovery_catch) = match validated {
            Ok(value) => value,
            Err(error) => {
                #[cfg(debug_assertions)]
                {
                    let pending = self.identity_pending.as_mut().expect("admitted getter");
                    pending.view_rejected = true;
                    pending.assert_rejected_outputs_live();
                }
                return Err(error);
            }
        };
        let pending = self.identity_pending.take().expect("observed getter");
        let view = pending.view.expect("actual identity view");
        Ok(FixedCallOutput::Identity(if raised {
            BootstrapIdentityOutcome::GetterRaised(BootstrapGetterError {
                error: pending.getter.expect("actual getter exception"),
                view,
                observation,
                budget: Arc::clone(self.input_budget),
                recovery_catch,
                origin,
            })
        } else {
            BootstrapIdentityOutcome::Returned(BootstrapIdentityValue {
                result: pending.result.expect("actual getter result"),
                view,
                observation,
                builtin,
            })
        }))
    }

    fn identity_refused(
        &mut self,
        stage: BootstrapIdentityStage,
        origin: &'static str,
    ) -> BootstrapIdentityOutcome {
        let mut pending = self.identity_pending.take().expect("admitted getter");
        let error = match stage {
            BootstrapIdentityStage::Getter => pending.getter.take().expect("actual getter refusal"),
            BootstrapIdentityStage::Describe => pending
                .describe_error
                .take()
                .expect("actual describe refusal"),
        };
        BootstrapIdentityOutcome::Refused(BootstrapIdentityFailure {
            stage,
            origin,
            error,
            getter: pending.getter,
            _retained: [pending.result, pending.view],
        })
    }
}

fn null(value: &CoreValue) -> bool {
    matches!(value.inspect(), CoreRef::Null)
}

fn string(value: &CoreValue, limit: usize, truncated: bool) -> Result<()> {
    exact(value, &["length", "codepoints"])?;
    let length = field(value, "length")?
        .as_i64()
        .and_then(|v| usize::try_from(v).ok())
        .context("raw getter codepoint length is invalid")?;
    let points = field(value, "codepoints")?;
    let points = points
        .array()
        .context("raw getter codepoints are invalid")?;
    if truncated && length == limit + 1 && points.is_empty() {
        return Ok(());
    }
    if length > limit || points.len() != length {
        bail!("raw getter codepoint count differs");
    }
    for index in 0..points.len() {
        if points
            .get(index)
            .and_then(|v| v.as_i64())
            .is_none_or(|v| !(0..=0x10ffff).contains(&v))
        {
            bail!("raw getter codepoint is invalid");
        }
    }
    Ok(())
}

fn scalar(value: &CoreValue) -> Result<()> {
    exact(value, &["kind", "value"])?;
    let kind = field(value, "kind")?;
    let value = field(value, "value")?;
    match kind.as_str() {
        Some("none") if null(&value) => Ok(()),
        Some("bool") if value.as_bool().is_some() => Ok(()),
        Some("str") => string(&value, 4096, false),
        Some("int") => {
            let text = value
                .as_str()
                .context("raw getter integer is not hex text")?;
            let magnitude = text.strip_prefix('-').unwrap_or(text);
            if magnitude.is_empty()
                || magnitude.len() > 1024
                || (magnitude.len() > 1 && magnitude.starts_with('0'))
                || text == "-0"
                || !magnitude.bytes().all(hex)
            {
                bail!("raw getter integer is invalid");
            }
            Ok(())
        }
        Some("float")
            if value
                .as_str()
                .is_some_and(|v| v.len() == 16 && v.bytes().all(hex)) =>
        {
            Ok(())
        }
        _ => bail!("raw getter exception scalar is unsupported"),
    }
}
fn hex(value: u8) -> bool {
    value.is_ascii_digit() || (b'a'..=b'f').contains(&value)
}
fn finite_bytes(value: &CoreValue) -> Result<()> {
    exact(value, &["kind", "value"])?;
    if field(value, "kind")?.as_str() != Some("bytes") {
        bail!("manifest decoder object is not bytes");
    }
    let text = field(value, "value")?;
    let text = text
        .as_str()
        .context("manifest decoder bytes are not base64")?;
    if text.len() > MAX_BYTES || !text.len().is_multiple_of(4) {
        bail!("manifest decoder byte envelope is invalid");
    }
    let sextet = |byte| match byte {
        b'A'..=b'Z' => Some(byte - b'A'),
        b'a'..=b'z' => Some(byte - b'a' + 26),
        b'0'..=b'9' => Some(byte - b'0' + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    };
    let padding = text.bytes().rev().take_while(|&b| b == b'=').count();
    if padding > 2
        || text.as_bytes()[..text.len() - padding]
            .iter()
            .any(|&byte| sextet(byte).is_none())
    {
        bail!("manifest decoder bytes are not canonical base64");
    }
    if padding != 0 {
        let last = text
            .as_bytes()
            .get(text.len() - padding - 1)
            .and_then(|&byte| sextet(byte))
            .context("manifest decoder base64 is empty")?;
        if (padding == 1 && last & 3 != 0) || (padding == 2 && last & 15 != 0) {
            bail!("manifest decoder base64 tail is not canonical");
        }
    }
    Ok(())
}

fn error_catch(kind: &str) -> Result<BootstrapRecoveryCatch> {
    use BootstrapRecoveryCatch::*;
    Ok(match kind {
        "value-error"
        | "protocol-error"
        | "canonicalization-error"
        | "schema-validation-error"
        | "json-decode-error" => ValueError,
        "unicode-decode-error" => ValueError,
        "os-error"
        | "file-not-found-error"
        | "permission-error"
        | "not-a-directory-error"
        | "is-a-directory-error"
        | "file-exists-error"
        | "interrupted-error"
        | "blocking-io-error"
        | "timeout-error"
        | "connection-error"
        | "broken-pipe-error"
        | "connection-aborted-error"
        | "connection-refused-error"
        | "connection-reset-error"
        | "process-lookup-error"
        | "child-process-error" => OsError,
        "type-error"
        | "key-error"
        | "index-error"
        | "attribute-error"
        | "runtime-error"
        | "default-profile-v4-error"
        | "bundle-integrity-error"
        | "profile-resolution-denied" => None,
        _ => bail!("raw getter exception class is outside the registered ABI"),
    })
}

fn origin_matches(actual: Option<&str>, origin: &str, raised: bool) -> bool {
    matches!(
        (actual, origin, raised),
        (Some("getter-returned"), "getter", false)
            | (Some("getter-raised"), "getter", true)
            | (Some("host-root-returned"), "host-root", false)
            | (Some("host-root-raised"), "host-root", true)
            | (Some("bundle-root-returned"), "bundle-root", false)
            | (Some("bundle-root-raised"), "bundle-root", true)
            | (Some("catalog-returned"), "catalog", false)
            | (Some("catalog-raised"), "catalog", true)
            | (Some("manifest-returned"), "manifest", false)
            | (Some("manifest-raised"), "manifest", true)
            | (
                Some("resolver-source-lookup-returned"),
                "resolver-source-lookup",
                false
            )
            | (
                Some("resolver-source-lookup-raised"),
                "resolver-source-lookup",
                true
            )
            | (
                Some("bootstrap-template-lookup-raised"),
                "bootstrap-template-lookup",
                true
            )
            | (
                Some("resolver-static-edges-raised"),
                "resolver-static-edges",
                true
            )
            | (
                Some("resolver-dynamic-edges-raised"),
                "resolver-dynamic-edges",
                true
            )
            | (
                Some("resolver-verified-artifacts-returned"),
                "resolver-verified-artifacts",
                false
            )
            | (
                Some("resolver-verified-artifacts-raised"),
                "resolver-verified-artifacts",
                true
            )
            | (Some("resolver-profile-raised"), "resolver-profile", true)
            | (
                Some("bootstrap-predecessor-source-raised"),
                "bootstrap-predecessor-source",
                true
            )
            | (
                Some("bootstrap-inactive-source-raised"),
                "bootstrap-inactive-source",
                true
            )
            | (
                Some("bootstrap-inactive-equality-raised"),
                "bootstrap-inactive-equality",
                true
            )
            | (
                Some("bootstrap-confirmation-raised"),
                "bootstrap-confirmation",
                true
            )
            | (Some("bootstrap-preview-raised"), "bootstrap-preview", true)
            | (
                Some("bootstrap-recovery-catalog-raised"),
                "bootstrap-recovery-catalog",
                true
            )
            | (Some("bootstrap-listing-raised"), "bootstrap-listing", true)
            | (
                Some("bootstrap-activation-preflight-raised"),
                "bootstrap-activation-preflight",
                true
            )
            | (
                Some("bootstrap-activation-match-raised"),
                "bootstrap-activation-match",
                true
            )
    )
}

fn validate_view(
    value: &CoreValue,
    root: &CoreValue,
    scope: &CoreValue,
    raised: bool,
    origin: &str,
) -> Result<(BootstrapBuiltinKind, BootstrapRecoveryCatch)> {
    exact(
        value,
        &[
            "format",
            "scope",
            "root",
            "origin",
            "kind",
            "registered_builtin_kind",
            "string",
            "exception",
            "owned",
        ],
    )?;
    if field(value, "format")?.as_str() != Some("io.tobkiri.native-bootstrap-identity-view.v1")
        || !same_scope(&field(value, "scope")?, scope)
        || !same_reference(&field(value, "root")?, root)?
        || !origin_matches(field(value, "origin")?.as_str(), origin, raised)
    {
        bail!("raw getter view has different actual origin/scope/ref");
    }
    let registered = field(value, "registered_builtin_kind")?;
    let kind = field(value, "kind")?;
    let builtin = match registered.as_str() {
        Some("none") => BootstrapBuiltinKind::None,
        Some("bool") => BootstrapBuiltinKind::Bool,
        Some("int") => BootstrapBuiltinKind::Int,
        Some("float") => BootstrapBuiltinKind::Float,
        Some("str") => BootstrapBuiltinKind::Str,
        Some("list") => BootstrapBuiltinKind::List,
        Some("tuple") => BootstrapBuiltinKind::Tuple,
        Some("dict") => BootstrapBuiltinKind::Dict,
        Some("set") => BootstrapBuiltinKind::Set,
        Some("frozenset") => BootstrapBuiltinKind::Frozenset,
        Some("record") => BootstrapBuiltinKind::Record,
        _ => bail!("raw getter builtin type is outside the registered ABI"),
    };
    let kind_text = kind.as_str().context("raw getter kind is not text")?;
    if builtin == BootstrapBuiltinKind::Record {
        if kind_text.is_empty()
            || kind_text.len() > 64
            || !kind_text.as_bytes()[0].is_ascii_lowercase()
            || !kind_text
                .bytes()
                .all(|v| v.is_ascii_lowercase() || v.is_ascii_digit() || v == b'-')
            || matches!(
                kind_text,
                "none"
                    | "bool"
                    | "int"
                    | "float"
                    | "str"
                    | "list"
                    | "tuple"
                    | "dict"
                    | "set"
                    | "frozenset"
            )
        {
            bail!("raw getter record tag is invalid");
        }
    } else if registered.as_str() != kind.as_str() {
        bail!("raw getter actual builtin type differs");
    }
    let text = field(value, "string")?;
    if builtin == BootstrapBuiltinKind::Str {
        string(&text, 128, true)?;
    } else if !null(&text) {
        bail!("raw getter non-string disclosed contents");
    }
    let exception = field(value, "exception")?;
    let owned = field(value, "owned")?;
    let owned = owned
        .array()
        .context("raw getter ownership list is invalid")?;
    if !raised {
        if !null(&exception) || !owned.is_empty() {
            bail!("returned raw getter disclosed exception contents/holds");
        }
        return Ok((builtin, BootstrapRecoveryCatch::None));
    }
    if builtin != BootstrapBuiltinKind::Record {
        bail!("raw getter throw is not a registered exception");
    }
    let json_fields = origin == "manifest" && kind_text == "json-decode-error";
    let unicode_fields = origin == "manifest" && kind_text == "unicode-decode-error";
    let keys: &[&str] = if unicode_fields {
        &[
            "recovery_catch",
            "args",
            "cause",
            "context",
            "suppress_context",
            "os_fields",
            "unicode_fields",
        ]
    } else if json_fields {
        &[
            "recovery_catch",
            "args",
            "cause",
            "context",
            "suppress_context",
            "os_fields",
            "json_fields",
        ]
    } else {
        &[
            "recovery_catch",
            "args",
            "cause",
            "context",
            "suppress_context",
            "os_fields",
        ]
    };
    exact(&exception, keys)?;
    if json_fields {
        let fields = field(&exception, "json_fields")?;
        exact(&fields, &["msg", "doc", "pos", "lineno", "colno"])?;
        string(&field(&fields, "msg")?, 4096, false)?;
        string(&field(&fields, "doc")?, MAX_BYTES, false)?;
        for name in ["pos", "lineno", "colno"] {
            if field(&fields, name)?.as_i64().is_none_or(|v| v < 0) {
                bail!("actual manifest parser position is invalid");
            }
        }
    }
    if unicode_fields {
        let fields = field(&exception, "unicode_fields")?;
        exact(
            &fields,
            &[
                "encoding",
                "object",
                "start",
                "end",
                "reason",
                "formatted_message",
            ],
        )?;
        string(&field(&fields, "encoding")?, 4096, false)?;
        string(&field(&fields, "reason")?, 4096, false)?;
        string(&field(&fields, "formatted_message")?, 4096, false)?;
        finite_bytes(&field(&fields, "object")?)?;
        let start = field(&fields, "start")?
            .as_i64()
            .context("actual UnicodeDecodeError start missing")?;
        let end = field(&fields, "end")?
            .as_i64()
            .context("actual UnicodeDecodeError end missing")?;
        if start < 0 || end < start {
            bail!("actual UnicodeDecodeError positions invalid");
        }
    }
    let recovery_catch = match field(&exception, "recovery_catch")?.as_str() {
        Some("none") => BootstrapRecoveryCatch::None,
        Some("value-error") => BootstrapRecoveryCatch::ValueError,
        Some("os-error") => BootstrapRecoveryCatch::OsError,
        _ => bail!("raw getter actual exception classification is invalid"),
    };
    // Check both actual class classification and the trusted inventory tag.
    // A frame's arbitrary kind text alone cannot select a recovery catch.
    if recovery_catch != error_catch(kind_text)? {
        bail!("raw getter class classification and inventory differ");
    }
    let args = field(&exception, "args")?;
    let args = args
        .array()
        .context("raw getter exception args are invalid")?;
    if args.len() > 8 {
        bail!("raw getter exception args exceed the finite ABI");
    }
    for index in 0..args.len() {
        let argument = args.get(index).context("raw getter argument missing")?;
        if unicode_fields && field(&argument, "kind")?.as_str() == Some("bytes") {
            finite_bytes(&argument)?;
        } else {
            scalar(&argument)?;
        }
    }
    if field(&exception, "suppress_context")?.as_bool().is_none() {
        bail!("raw getter intrinsic suppression is invalid");
    }
    let os = field(&exception, "os_fields")?;
    if recovery_catch == BootstrapRecoveryCatch::OsError {
        exact(&os, &["errno", "strerror", "filename", "filename2"])?;
        for name in ["errno", "strerror", "filename", "filename2"] {
            scalar(&field(&os, name)?)?;
        }
    } else if !null(&os) {
        bail!("raw getter non-OS exception disclosed OS fields");
    }
    let cause = field(&exception, "cause")?;
    let context = field(&exception, "context")?;
    for edge in [&cause, &context] {
        if !null(edge) {
            reference(edge, scope)?;
        }
    }
    if owned.len() > 2 {
        bail!("raw getter shallow view issued too many holds");
    }
    for index in 0..owned.len() {
        let id = owned.get(index).context("raw getter owned hold missing")?;
        let text = id.as_str().context("raw getter owned hold is not text")?;
        if field(root, "object")?.as_str() == Some(text)
            || ![&cause, &context].into_iter().any(|edge| {
                !null(edge)
                    && field(edge, "object")
                        .ok()
                        .is_some_and(|v| v.as_str() == Some(text))
            })
            || (index == 1
                && owned
                    .get(0)
                    .is_some_and(|first| first.as_str() == Some(text)))
        {
            bail!("raw getter owned hold is not its unique intrinsic edge");
        }
    }
    Ok((builtin, recovery_catch))
}

impl<S: BootstrapInputOwner> NativeBootstrap<S> {
    /// Standalone same-Store raw observation, with unchanged READY admission.
    pub async fn observe_bootstrap_profile_id(&mut self) -> Result<BootstrapIdentityOutcome> {
        if self.work.get().phase.load(Ordering::Acquire) != READY {
            bail!("raw getter observation is unavailable");
        }
        self.pack_borrow().observe_bootstrap_profile_id().await
    }
}
