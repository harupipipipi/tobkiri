//! One existing Pack factory call and its finite returned-exception view.
//! Host raise provenance stays here; it is never sent back into the guest.
use super::*;
use crate::{NativeErrorGraph, NativeErrorKind};

const FACTORY_SLOT: usize = 5;
const IMPORT_SLOT: usize = 6;

// Fixed runner-held shares, charged by the original CoreBuffer allocations. In
// particular a codec rejection cannot destroy a real factory output before
// retire_failure has retired the Store and awaited its physical IO owner.
pub(super) struct DeniedCallCustody {
    message: CoreBuffer,
    input: CoreBuffer,
    host_raise: BootstrapDeniedRaise,
    imported: Option<CoreBuffer>,
    result: Option<CoreBuffer>,
    view: Option<CoreBuffer>,
    _error: [Option<CoreBuffer>; 3],
    #[cfg(debug_assertions)]
    view_rejected: bool,
}
impl DeniedCallCustody {
    #[cfg(debug_assertions)]
    pub(super) fn view_rejected(&self) -> bool {
        self.view_rejected
    }
    #[cfg(debug_assertions)]
    pub(super) fn assert_rejected_outputs_live(&self) {
        assert!(
            self.result.is_some() && self.view.is_some(),
            "rejected factory outputs died before Store/IO retirement"
        );
    }
    fn hold_error(&mut self, error: &NativeDataError) {
        self._error = [
            Some(error.code().clone()),
            Some(error.message().clone()),
            error.details().cloned(),
        ];
    }
}

/// The Host's separate raise site, not a request to mutate a guest exception.
pub enum BootstrapDeniedRaise {
    Plain,
    ExplicitFrom(NativeErrorGraph),
}
impl std::fmt::Debug for BootstrapDeniedRaise {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.write_str(match self {
            Self::Plain => "Plain",
            Self::ExplicitFrom(_) => "ExplicitFrom",
        })
    }
}

/// A real fresh factory value, retained by its original Store registry. The
/// funded envelopes remain distinct from Review's owned string diagnostics.
/// A reference is meaningful only until that Store retires; the view is DATA.
pub struct BootstrapDenied {
    result: CoreBuffer,
    view: CoreBuffer,
    message: CoreBuffer,
    host_raise: BootstrapDeniedRaise,
}
impl BootstrapDenied {
    pub fn result(&self) -> &CoreBuffer {
        &self.result
    }
    pub fn view(&self) -> &CoreBuffer {
        &self.view
    }
    pub fn message(&self) -> &CoreBuffer {
        &self.message
    }
    pub fn kind(&self) -> NativeErrorKind {
        NativeErrorKind::ProfileResolutionDenied
    }
    pub fn cause(&self) -> Option<&NativeErrorGraph> {
        match &self.host_raise {
            BootstrapDeniedRaise::Plain => None,
            BootstrapDeniedRaise::ExplicitFrom(error) => Some(error),
        }
    }
    pub fn context(&self) -> Option<&NativeErrorGraph> {
        self.cause()
    }
    pub fn suppress_context(&self) -> bool {
        self.cause().is_some()
    }
}
impl std::fmt::Debug for BootstrapDenied {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.debug_struct("BootstrapDenied")
            .field("host_raise", &self.host_raise)
            .finish_non_exhaustive()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BootstrapDeniedStage {
    Input,
    Factory,
    Describe,
}

/// The original method error/ref is preserved; a thrown factory is never a
/// returned denied value. Native traps/accounting failures stay outer errors.
pub struct BootstrapDeniedFailure {
    stage: BootstrapDeniedStage,
    error: NativeDataError,
    pending_raise: BootstrapDeniedRaise,
    // A failed description must not discard its prior actual factory value.
    _retained: [Option<CoreBuffer>; 3],
}
impl BootstrapDeniedFailure {
    pub fn stage(&self) -> BootstrapDeniedStage {
        self.stage
    }
    pub fn error(&self) -> &NativeDataError {
        &self.error
    }
    pub fn factory_raised(&self) -> bool {
        self.stage == BootstrapDeniedStage::Factory
            && self.error.code().as_bytes() == b"method-raised"
    }
    /// Ambient Host except context at a factory throw, before `raise ... from`
    /// could execute. Guest intrinsic cause/context/suppression remain opaque
    /// in the actual error ref; none is overwritten or guessed here.
    pub fn host_except_context(&self) -> Option<&NativeErrorGraph> {
        if !self.factory_raised() {
            return None;
        }
        match &self.pending_raise {
            BootstrapDeniedRaise::Plain => None,
            BootstrapDeniedRaise::ExplicitFrom(error) => Some(error),
        }
    }
}
impl std::fmt::Debug for BootstrapDeniedFailure {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        out.debug_struct("BootstrapDeniedFailure")
            .field("stage", &self.stage)
            .field("pending_raise", &self.pending_raise)
            .finish_non_exhaustive()
    }
}

impl<S: BootstrapInputOwner> NativeBootstrap<S> {
    /// Call the original factory in this initialized Store, then observe only
    /// its fresh returned Exception. This unit does not enter Review/recovery.
    /// Keep its funded value/failure until the original Store finish, exactly
    /// as for existing getter/CoreBuffer results; detach DATA separately.
    pub async fn denied(
        &mut self,
        message: &str,
        host_raise: BootstrapDeniedRaise,
    ) -> Result<std::result::Result<BootstrapDenied, BootstrapDeniedFailure>> {
        if self.work.get().phase.load(Ordering::Acquire) != READY
            || self.denied_pending.is_some()
            || message.len() > MAX_BYTES
        {
            bail!("native denied factory is unavailable or exceeds the envelope");
        }
        self.work.get().source.check_live()?;
        // Incoming cause is DATA. Rebind its finite buffers to this existing
        // codec account before the factory call; never retain a foreign budget
        // or introduce another account/deadline. Graph IDs/edges stay intact.
        let host_raise = match host_raise {
            BootstrapDeniedRaise::Plain => BootstrapDeniedRaise::Plain,
            BootstrapDeniedRaise::ExplicitFrom(error) => {
                let code = error.code().try_copy_to(&self.input_budget)?;
                let message = error.message().try_copy_to(&self.input_budget)?;
                let details = error.details().try_copy_to(&self.input_budget)?;
                let bound = NativeErrorGraph::decode(code, message, details, &self.input_budget)?;
                drop(error);
                BootstrapDeniedRaise::ExplicitFrom(bound)
            }
        };
        let message = CoreBuffer::copy_from(message.as_bytes(), &self.input_budget)?;
        let input = import_message(self, message.as_str()?)?;
        self.denied_pending = Some(DeniedCallCustody {
            message,
            input,
            host_raise,
            imported: None,
            result: None,
            view: None,
            _error: [None, None, None],
            #[cfg(debug_assertions)]
            view_rejected: false,
        });
        let mut guard = RootCallGuard {
            work: self.work.clone(),
            io: self.io.clone(),
            completed: false,
        };
        self.work.get().phase.store(DENIED, Ordering::Release);
        let result = match self.denied_in_phase().await {
            Err(original) => Err(self.retire_failure(original).await),
            result => result,
        };
        if self.work.get().phase.load(Ordering::Acquire) == DENIED {
            self.work.get().phase.store(READY, Ordering::Release);
        }
        guard.completed = true;
        result
    }

    async fn denied_in_phase(
        &mut self,
    ) -> Result<std::result::Result<BootstrapDenied, BootstrapDeniedFailure>> {
        let input = self
            .denied_pending
            .as_ref()
            .expect("admitted factory")
            .input
            .clone();
        let imported = match self
            .string_call_in_phase(IMPORT_SLOT, &input, DENIED)
            .await?
        {
            Ok(value) => value,
            Err(error) => {
                return Ok(Err(self.denied_failure(BootstrapDeniedStage::Input, error)));
            }
        };
        self.denied_pending
            .as_mut()
            .expect("admitted factory")
            .imported = Some(imported.clone());
        let scope = CoreValue::parse_guest(self.call.as_bytes(), &self.input_budget)?
            .get_key("scope")
            .context("original call scope missing")?;
        let imported_value = result(
            &imported,
            "io.tobkiri.native-values-imported.v1",
            &scope,
            &self.input_budget,
        )?;
        let argument = field(&imported_value, "root")?;
        ownership(&imported_value, &argument, false)?;
        let args = CoreValue::array_from([Ok(argument)].into_iter(), &self.input_budget)?;
        let call = frame(
            self,
            "io.tobkiri.native-call.v1",
            [
                ("args", args),
                (
                    "kwargs",
                    CoreValue::array_from(std::iter::empty(), &self.input_budget)?,
                ),
            ],
        )?;
        let returned = match self
            .string_call_in_phase(FACTORY_SLOT, &call, DENIED)
            .await?
        {
            Ok(value) => value,
            Err(error) => {
                self.denied_pending
                    .as_mut()
                    .expect("admitted factory")
                    .hold_error(&error);
                if error.code().as_bytes() == b"method-raised" {
                    let raised = result(
                        error
                            .details()
                            .context("factory method error reference is missing")?,
                        "io.tobkiri.native-value-result.v1",
                        &scope,
                        &self.input_budget,
                    )?;
                    ownership(&raised, &field(&raised, "root")?, false)?;
                }
                return Ok(Err(
                    self.denied_failure(BootstrapDeniedStage::Factory, error)
                ));
            }
        };
        self.denied_pending
            .as_mut()
            .expect("admitted factory")
            .result = Some(returned.clone());
        let returned_value = result(
            &returned,
            "io.tobkiri.native-value-result.v1",
            &scope,
            &self.input_budget,
        )?;
        let root = field(&returned_value, "root")?;
        ownership(&returned_value, &root, true)?;
        let request = frame(
            self,
            "io.tobkiri.native-value-describe.v1",
            [("root", root.clone())],
        )?;
        let view = match self.string_call_in_phase(2, &request, DENIED).await? {
            Ok(value) => value,
            Err(error) => {
                return Ok(Err(
                    self.denied_failure(BootstrapDeniedStage::Describe, error)
                ));
            }
        };
        self.denied_pending.as_mut().expect("admitted factory").view = Some(view.clone());
        let message = self
            .denied_pending
            .as_ref()
            .expect("admitted factory")
            .message
            .clone();
        if let Err(error) =
            validate_view(&view, &root, &scope, message.as_str()?, &self.input_budget)
        {
            #[cfg(debug_assertions)]
            {
                let pending = self.denied_pending.as_mut().expect("admitted factory");
                pending.view_rejected = true;
                pending.assert_rejected_outputs_live();
            }
            return Err(error);
        }
        let custody = self.denied_pending.take().expect("admitted factory");
        Ok(Ok(BootstrapDenied {
            result: custody.result.expect("observed factory result"),
            view: custody.view.expect("observed factory view"),
            message: custody.message,
            host_raise: custody.host_raise,
        }))
    }

    fn denied_failure(
        &mut self,
        stage: BootstrapDeniedStage,
        error: NativeDataError,
    ) -> BootstrapDeniedFailure {
        let custody = self.denied_pending.take().expect("admitted factory");
        BootstrapDeniedFailure {
            stage,
            error,
            pending_raise: custody.host_raise,
            _retained: [custody.imported, custody.result, custody.view],
        }
    }
}

fn import_message<S: BootstrapInputOwner>(
    runner: &NativeBootstrap<S>,
    message: &str,
) -> Result<CoreBuffer> {
    let mut out = CoreBuffer::builder(&runner.input_budget, MAX_BYTES);
    out.append(b"{\"format\":\"io.tobkiri.native-value-input.v1\",\"scope\":")?;
    Scope::original(&runner.work.get().invocation).write(&mut out)?;
    out.append(b",\"nodes\":[{\"kind\":\"str\",\"value\":")?;
    bootstrap_metadata::string_parts(&mut out, [message])?;
    out.append(b"}],\"root\":{\"node\":0}}")?;
    Ok(out.finish()?)
}
fn frame<S: BootstrapInputOwner, const N: usize>(
    runner: &NativeBootstrap<S>,
    format: &str,
    values: [(&str, CoreValue); N],
) -> Result<CoreBuffer> {
    let mut out = CoreBuffer::builder(&runner.input_budget, MAX_BYTES);
    out.append(b"{\"format\":")?;
    bootstrap_metadata::string_parts(&mut out, [format])?;
    out.append(b",\"scope\":")?;
    Scope::original(&runner.work.get().invocation).write(&mut out)?;
    for (name, value) in values {
        out.append(b",")?;
        bootstrap_metadata::string_parts(&mut out, [name])?;
        out.append(b":")?;
        let encoded = value.encode_guest(&runner.input_budget)?;
        out.append(encoded.as_bytes())?;
    }
    out.append(b"}")?;
    Ok(out.finish()?)
}
fn field(value: &CoreValue, name: &str) -> Result<CoreValue> {
    value
        .get_key(name)
        .context("denied factory DATA field missing")
}
fn exact(value: &CoreValue, names: &[&str]) -> Result<()> {
    if value
        .object()
        .is_none_or(|object| object.len() != names.len())
        || names.iter().any(|name| value.get_key(name).is_none())
    {
        bail!("denied factory DATA shape is invalid");
    }
    Ok(())
}
fn same_scope(left: &CoreValue, right: &CoreValue) -> bool {
    let (Some(left), Some(right)) = (left.array(), right.array()) else {
        return false;
    };
    left.len() == 4
        && right.len() == 4
        && (0..4).all(|index| {
            let (Some(left), Some(right)) = (left.get(index), right.get(index)) else {
                return false;
            };
            left.as_str().is_some() && left.as_str() == right.as_str()
        })
}
fn reference(value: &CoreValue, scope: &CoreValue) -> Result<()> {
    exact(value, &["scope", "object"])?;
    if !same_scope(&field(value, "scope")?, scope) {
        bail!("denied factory reference scope differs");
    }
    let object = field(value, "object")?;
    let text = object
        .as_str()
        .context("denied factory reference is not text")?;
    if text.is_empty()
        || text.len() > 8
        || text.starts_with('0')
        || !text
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || u32::from_str_radix(text, 16).is_err()
    {
        bail!("denied factory reference is invalid");
    }
    Ok(())
}
fn same_reference(left: &CoreValue, right: &CoreValue) -> Result<bool> {
    exact(left, &["scope", "object"])?;
    exact(right, &["scope", "object"])?;
    Ok(
        field(left, "object")?.as_str() == field(right, "object")?.as_str()
            && same_scope(&field(left, "scope")?, &field(right, "scope")?),
    )
}
fn result(
    bytes: &CoreBuffer,
    format: &str,
    scope: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let value = CoreValue::parse_guest(bytes.as_bytes(), budget)?;
    exact(&value, &["format", "scope", "root", "owned"])?;
    if field(&value, "format")?.as_str() != Some(format)
        || !same_scope(&field(&value, "scope")?, scope)
    {
        bail!("denied factory result scope/format differs");
    }
    reference(&field(&value, "root")?, scope)?;
    Ok(value)
}
fn ownership(value: &CoreValue, root: &CoreValue, require_fresh: bool) -> Result<()> {
    let owned = field(value, "owned")?;
    let owned = owned
        .array()
        .context("denied factory ownership is invalid")?;
    if !require_fresh && owned.is_empty() {
        return Ok(());
    }
    let first = owned.get(0).context("denied factory ownership is empty")?;
    if owned.len() != 1 || first.as_str() != field(root, "object")?.as_str() {
        bail!("denied factory value was reused or not freshly issued");
    }
    Ok(())
}
fn validate_view(
    bytes: &CoreBuffer,
    root: &CoreValue,
    scope: &CoreValue,
    message: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let value = CoreValue::parse_guest(bytes.as_bytes(), budget)?;
    exact(&value, &["format", "scope", "root", "nodes", "owned"])?;
    if field(&value, "format")?.as_str() != Some("io.tobkiri.native-value-view.v1")
        || !same_scope(&field(&value, "scope")?, scope)
        || !same_reference(&field(&value, "root")?, root)?
    {
        bail!("denied factory view scope/reference differs");
    }
    let owned = field(&value, "owned")?;
    if owned.array().is_none_or(|values| !values.is_empty()) {
        bail!("denied factory view issued unexpected holds");
    }
    let nodes = field(&value, "nodes")?;
    let nodes = nodes
        .array()
        .context("denied factory view nodes are invalid")?;
    if nodes.len() != 1 {
        bail!("denied factory view is not one exception");
    }
    let node = nodes
        .get(0)
        .context("denied factory exception is missing")?;
    exact(
        &node,
        &[
            "ref",
            "kind",
            "opaque_native_binding",
            "fields",
            "factory_denied",
        ],
    )?;
    if !same_reference(&field(&node, "ref")?, root)?
        || field(&node, "kind")?.as_str() != Some(NativeErrorKind::ProfileResolutionDenied.tag())
        || field(&node, "opaque_native_binding")?.as_bool() != Some(false)
        || field(&node, "fields")?
            .array()
            .is_none_or(|fields| !fields.is_empty())
    {
        bail!("denied factory actual exception kind differs");
    }
    let factory = field(&node, "factory_denied")?;
    exact(&factory, &["args", "cause", "context", "suppress_context"])?;
    let args = field(&factory, "args")?;
    let args = args.array().context("denied factory args are invalid")?;
    let first = args.get(0).context("denied factory args are empty")?;
    if args.len() != 1
        || first.as_str() != Some(message)
        || !matches!(
            field(&factory, "cause")?.inspect(),
            tobkiri_canon::CoreRef::Null
        )
        || !matches!(
            field(&factory, "context")?.inspect(),
            tobkiri_canon::CoreRef::Null
        )
        || field(&factory, "suppress_context")?.as_bool() != Some(false)
    {
        bail!("denied factory actual exception fields differ");
    }
    Ok(())
}
