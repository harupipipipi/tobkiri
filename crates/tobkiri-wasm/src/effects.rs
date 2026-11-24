//! Generic core effects. Guest values never pass through an unowned CanonValue
//! or a legacy HostIo String callback. Resource versions remain nominal.
use crate::{
    InvocationState,
    core_imports::host_from_store,
    effect_values::{EffectValues, field, list, text, uint},
    host::{PackHost, PackState},
    owned_values::ValueOwners,
};
use anyhow::{Context, Result, bail};
use std::{
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};
use tobkiri_canon::{
    AllocationError, CoreBuffer, CoreValue,
    ids::{OpName, PackId, ShapeRef},
};
use tobkiri_flow::{StepError, StepOutput};
use tobkiri_gate::{
    CallCx, ControlKind, ControlReservation, EffectRequest, Gate, GrantId, Outcome,
};
use tobkiri_store::{
    FixedDataRequest, NativeSqlTransaction, SecretRef, SecretWorkResult, StoreHandle,
    UserGlobalStore, Vault,
};
use wasmtime::component::{LinkerInstance, Resource, ResourceAny, ResourceType, Val};
use wasmtime::{AsContextMut, StoreContextMut};

#[path = "audit_imports.rs"]
mod audit_imports;
#[path = "filesystem_imports.rs"]
pub mod filesystem_imports;
#[path = "host_io_imports.rs"]
mod host_io_imports;
#[path = "secrets_imports.rs"]
mod secrets_imports;

struct HostSecret {
    name: String,
    _secret: SecretRef,
}
struct HostResponse {
    response: crate::network::NetResponse,
    request: EffectRequest,
}
#[derive(Clone)]
enum HostStore {
    Profile(Arc<StoreHandle>),
    Global(Arc<UserGlobalStore>),
}

/// Native delivery bookkeeping, admitted before a storage operation can write.
/// This is deliberately move-only. Keeping the actual handle here also keeps
/// its connection custody alive through conversion and terminal delivery.
struct WriteDeliveryReceipt<H> {
    id: Option<GrantId>,
    writes: bool,
    sql_completed: bool,
    terminal_done: bool,
    _hold: H,
    // The held resource drops before its native control reservation.
    _reservation: ControlReservation,
}
impl<H> WriteDeliveryReceipt<H> {
    fn prepare(cx: &CallCx, id: Option<&GrantId>, writes: bool, hold: H) -> Result<Self> {
        let bytes = std::mem::size_of::<Self>()
            .checked_add(id.map_or(0, GrantId::clone_storage_bytes))
            .context("storage delivery receipt size overflow")?;
        let reservation = cx
            .control()
            .reserve(ControlKind::Frame, u64::try_from(bytes)?)?;
        Ok(Self {
            id: id.map(GrantId::try_clone_precharged).transpose()?,
            writes,
            sql_completed: false,
            terminal_done: false,
            _hold: hold,
            _reservation: reservation,
        })
    }
    fn completed(&mut self, writes: bool, terminal_done: bool) {
        self.writes = writes;
        self.sql_completed = true;
        self.terminal_done = terminal_done;
    }
    fn delivery<T>(&self, result: Result<T>) -> Result<T> {
        result.map_err(|error| {
            if self.writes && self.sql_completed {
                tobkiri_store::StoreError::CommitUnknown.into()
            } else {
                error
            }
        })
    }
    fn converted<T>(&self, cx: &CallCx, result: Result<T>) -> Result<T> {
        self.delivery(result.and_then(|value| {
            cx.check_active()?;
            Ok(value)
        }))
    }
    async fn finish_fixed<T>(
        &mut self,
        gate: &dyn Gate,
        cx: &CallCx,
        result: Result<T>,
    ) -> Result<T> {
        if self.terminal_done {
            bail!("storage terminal already delivered")
        }
        let result = self.converted(cx, result);
        // Move the exact ID once. Drop never submits another proposal, retries
        // the effect, or changes an immutable winner after cancellation.
        let id = self
            .id
            .take()
            .context("storage terminal already attempted")?;
        if let Err(error) = gate.record_outcome(id, effect_outcome(&result, cx)).await {
            // A backend's sealed no-replay state must survive even if delivery
            // of that failure also fails. Never make it an ordinary retry.
            if result
                .as_ref()
                .err()
                .and_then(crate::core_imports::storage_failure_metadata)
                .is_some()
            {
                return result;
            }
            return self.delivery(Err(error.into()));
        }
        self.terminal_done = true;
        self.converted(cx, result)
    }
}
// There is intentionally no asynchronous Drop action. If this future is
// abandoned, the consumed durable grant remains non-reusable; its supervisor
// owns deadline settlement. Actual uncertain SQL/close cleanup belongs to
// Store, and delivery uncertainty never physically quarantines a healthy DB.

impl HostStore {
    async fn sql_exec_typed(
        &self,
        cx: &CallCx,
        statement: tobkiri_store::OwnedSqlStatement,
        policy: tobkiri_store::SqlResultPolicy,
        gate: Arc<dyn tobkiri_gate::Gate>,
        budget: Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<tobkiri_store::OwnedSqlResult, tobkiri_store::StoreError> {
        match self {
            Self::Profile(handle) => {
                handle
                    .sql_exec_typed(cx, statement, policy, gate, budget)
                    .await
            }
            Self::Global(handle) => {
                handle
                    .sql_exec_typed(cx, statement, policy, gate, budget)
                    .await
            }
        }
    }
    async fn sql_query_typed(
        &self,
        cx: &CallCx,
        statement: tobkiri_store::OwnedSqlStatement,
        gate: Arc<dyn tobkiri_gate::Gate>,
        budget: Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<tobkiri_store::OwnedSqlResult, tobkiri_store::StoreError> {
        match self {
            Self::Profile(handle) => handle.sql_query_typed(cx, statement, gate, budget).await,
            Self::Global(handle) => handle.sql_query_typed(cx, statement, gate, budget).await,
        }
    }
    async fn sql_begin(
        &self,
        cx: &CallCx,
        gate: Arc<dyn tobkiri_gate::Gate>,
        budget: Arc<dyn tobkiri_canon::AllocationBudget>,
    ) -> Result<Arc<NativeSqlTransaction>, tobkiri_store::StoreError> {
        match self {
            Self::Profile(handle) => handle.sql_begin(cx, gate, budget).await,
            Self::Global(handle) => handle.sql_begin(cx, gate, budget).await,
        }
    }
}
type HostDirectory = crate::filesystem::FsDirectory;
struct UnavailableProcess;
struct UnavailableTerminal;
/// Same-package aliases share this exact Rust identity. Different package
/// versions use different const instantiations and cannot exchange resources.
struct Nominal<T, const VERSION: u8>(T);

pub(crate) fn link_resource(
    instance: &mut LinkerInstance<'_, InvocationState<PackState>>,
    interface: &str,
    version: &str,
    name: &str,
) -> Result<()> {
    macro_rules! link {
        ($ty:ty) => {{
            match version {
                "1.0.0" => instance.resource(
                    name,
                    ResourceType::host::<Nominal<$ty, 0>>(),
                    |mut store, rep| {
                        store
                            .data_mut()
                            .data
                            .wasi
                            .table
                            .delete(Resource::<Nominal<$ty, 0>>::new_own(rep))?;
                        Ok(())
                    },
                )?,
                "1.1.0" => instance.resource(
                    name,
                    ResourceType::host::<Nominal<$ty, 1>>(),
                    |mut store, rep| {
                        store
                            .data_mut()
                            .data
                            .wasi
                            .table
                            .delete(Resource::<Nominal<$ty, 1>>::new_own(rep))?;
                        Ok(())
                    },
                )?,
                "1.2.0" => instance.resource(
                    name,
                    ResourceType::host::<Nominal<$ty, 2>>(),
                    |mut store, rep| {
                        store
                            .data_mut()
                            .data
                            .wasi
                            .table
                            .delete(Resource::<Nominal<$ty, 2>>::new_own(rep))?;
                        Ok(())
                    },
                )?,
                _ => bail!("unknown exact core resource package"),
            }
        }};
    }
    match (interface, name) {
        ("secrets" | "network", "secret") => link!(HostSecret),
        ("network", "response") => link!(HostResponse),
        ("storage", "store") => link!(HostStore),
        ("fs" | "proc" | "pty", "dir") => link!(HostDirectory),
        ("proc", "process") => link!(UnavailableProcess),
        ("pty", "terminal") => link!(UnavailableTerminal),
        ("storage", "sql-transaction") if matches!(version, "1.1.0" | "1.2.0") => {
            link!(Arc<NativeSqlTransaction>)
        }
        _ => bail!("unknown core resource"),
    }
    Ok(())
}
fn push<T: Send + 'static>(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    version: &str,
    value: T,
) -> Result<Val> {
    Ok(match version {
        "1.0.0" => {
            let value = store
                .data_mut()
                .data
                .wasi
                .table
                .push(Nominal::<T, 0>(value))?;
            Val::Resource(ResourceAny::try_from_resource(
                value,
                store.as_context_mut(),
            )?)
        }
        "1.1.0" => {
            let value = store
                .data_mut()
                .data
                .wasi
                .table
                .push(Nominal::<T, 1>(value))?;
            Val::Resource(ResourceAny::try_from_resource(
                value,
                store.as_context_mut(),
            )?)
        }
        "1.2.0" => {
            let value = store
                .data_mut()
                .data
                .wasi
                .table
                .push(Nominal::<T, 2>(value))?;
            Val::Resource(ResourceAny::try_from_resource(
                value,
                store.as_context_mut(),
            )?)
        }
        _ => bail!("unknown exact core resource package"),
    })
}
fn resource_rep<T: Send + 'static>(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    version: &str,
    value: &Val,
) -> Result<u32> {
    let Val::Resource(value) = value else {
        bail!("expected core resource")
    };
    Ok(match version {
        "1.0.0" => value
            .try_into_resource::<Nominal<T, 0>>(store.as_context_mut())?
            .rep(),
        "1.1.0" => value
            .try_into_resource::<Nominal<T, 1>>(store.as_context_mut())?
            .rep(),
        "1.2.0" => value
            .try_into_resource::<Nominal<T, 2>>(store.as_context_mut())?
            .rep(),
        _ => bail!("unknown exact core resource package"),
    })
}
fn get_resource<'a, T: Send + 'static>(
    store: &'a StoreContextMut<'_, InvocationState<PackState>>,
    version: &str,
    rep: u32,
) -> Result<&'a T> {
    Ok(match version {
        "1.0.0" => {
            &store
                .data()
                .data
                .wasi
                .table
                .get(&Resource::<Nominal<T, 0>>::new_borrow(rep))?
                .0
        }
        "1.1.0" => {
            &store
                .data()
                .data
                .wasi
                .table
                .get(&Resource::<Nominal<T, 1>>::new_borrow(rep))?
                .0
        }
        "1.2.0" => {
            &store
                .data()
                .data
                .wasi
                .table
                .get(&Resource::<Nominal<T, 2>>::new_borrow(rep))?
                .0
        }
        _ => bail!("unknown exact core resource package"),
    })
}
fn get_resource_mut<'a, T: Send + 'static>(
    store: &'a mut StoreContextMut<'_, InvocationState<PackState>>,
    version: &str,
    rep: u32,
) -> Result<&'a mut T> {
    Ok(match version {
        "1.0.0" => {
            &mut store
                .data_mut()
                .data
                .wasi
                .table
                .get_mut(&Resource::<Nominal<T, 0>>::new_borrow(rep))?
                .0
        }
        "1.1.0" => {
            &mut store
                .data_mut()
                .data
                .wasi
                .table
                .get_mut(&Resource::<Nominal<T, 1>>::new_borrow(rep))?
                .0
        }
        "1.2.0" => {
            &mut store
                .data_mut()
                .data
                .wasi
                .table
                .get_mut(&Resource::<Nominal<T, 2>>::new_borrow(rep))?
                .0
        }
        _ => bail!("unknown exact core resource package"),
    })
}
async fn outcome<T>(host: &PackHost, id: GrantId, result: &Result<T>, cx: &CallCx) -> Result<()> {
    host.shared
        .gate
        .record_outcome(id, effect_outcome(result, cx))
        .await?;
    Ok(())
}
fn effect_outcome<T>(result: &Result<T>, cx: &CallCx) -> Outcome {
    if result.is_ok() {
        Outcome::Succeeded
    } else if let Some((code, _)) = result
        .as_ref()
        .err()
        .and_then(crate::core_imports::storage_failure_metadata)
    {
        Outcome::Failed { code: code.into() }
    } else if std::time::Instant::now() >= cx.deadline() {
        Outcome::Failed {
            code: "deadline-failure-unknown".into(),
        }
    } else if cx.cancel().is_cancelled() {
        Outcome::Cancelled
    } else {
        Outcome::Failed {
            code: "host-effect".into(),
        }
    }
}
#[cfg(test)]
fn commit_unknown(error: &anyhow::Error) -> bool {
    matches!(
        error.downcast_ref::<tobkiri_store::StoreError>(),
        Some(tobkiri_store::StoreError::CommitUnknown)
    )
}
fn bounded_name(value: &Val) -> Result<&str> {
    let value = text(value)?;
    if value.is_empty() || value.len() > 128 || value.contains('\0') {
        bail!("core name exceeds its bounded metadata format")
    }
    Ok(value)
}
fn buffer_value(value: &Val, codec: &EffectValues<'_>) -> Result<Option<CoreBuffer>> {
    match value {
        Val::Option(None) => Ok(None),
        Val::Option(Some(value)) => Ok(Some(codec.copy_bytes(value)?)),
        _ => bail!("expected optional bytes"),
    }
}
fn scalar_json(value: u64, budget: &Arc<dyn tobkiri_canon::AllocationBudget>) -> Result<CoreValue> {
    if value <= tobkiri_canon::MAX_SAFE_INTEGER {
        Ok(CoreValue::int(value as i64, budget)?)
    } else {
        let mut bytes = [0u8; 20];
        let mut at = bytes.len();
        let mut n = value;
        loop {
            at -= 1;
            bytes[at] = b'0' + (n % 10) as u8;
            n /= 10;
            if n == 0 {
                break;
            }
        }
        Ok(CoreValue::string(
            std::str::from_utf8(&bytes[at..])?,
            budget,
        )?)
    }
}

pub(crate) async fn invoke(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    interface: &str,
    version: &str,
    operation: &str,
    params: &[Val],
) -> Result<Vec<Val>> {
    let host = host_from_store(store)?;
    let cx = store.data().cx.clone();
    let owners = store.data().data.owners.clone();
    let codec = EffectValues(&owners);
    if !matches!(version, "1.0.0" | "1.1.0" | "1.2.0") {
        bail!("unknown exact core package")
    }
    if interface == "flow" && operation == "is-cancelled" {
        return Ok(vec![Val::Bool(cx.check_active().is_err())]);
    }
    if interface == "flow" && operation == "cancel-stream" {
        crate::core_imports::cancel_stream(&host, &cx, uint(field(&params[0], "id")?)?).await?;
        return Ok(vec![]);
    }
    host.shared.gate.check_context(&cx)?;
    match interface {
        "flow" => match operation {
            "current-profile" if version == "1.2.0" => {
                if !params.is_empty() {
                    bail!("current-profile takes no arguments")
                }
                let frame = store
                    .data()
                    .data
                    .frame
                    .as_ref()
                    .context("current-profile requires an actual current provider frame")?;
                let current = frame.context();
                if current.profile() != cx.profile()
                    || current.principal() != cx.principal()
                    || current.request() != cx.request()
                    || current.revision() != cx.revision()
                    || current.security_epoch() != cx.security_epoch()
                {
                    bail!("current-profile frame differs from the current invocation")
                }
                host.shared.gate.check_context(current)?;
                // Core derives the native identity in a fixed 2048-byte
                // scratch area. Fund it before calling, as the existing
                // provider-operation preparation does, including on errors.
                let _identity_control = current.control().reserve(ControlKind::Frame, 2048)?;
                frame.native_execution_id().map_err(anyhow::Error::new)?;
                codec.ok(Some(codec.string(cx.profile().as_str())?))
            }
            "call-port" | "call-port-stream" => {
                let shape = ShapeRef::parse(text(&params[0])?)?;
                let op = OpName::parse(text(&params[1])?)?;
                let args = codec.parse(&params[2])?;
                let frame = store
                    .data()
                    .data
                    .frame
                    .as_ref()
                    .context("dynamic call lacks actual provider frame")?
                    .clone();
                let result = host
                    .call_guest_owned(&cx, &shape, &op, args, &frame)
                    .await?;
                match result {
                    StepOutput::Value(value) if operation == "call-port" => {
                        codec.ok(Some(codec.json(&value)?))
                    }
                    StepOutput::Stream(stream) if operation == "call-port-stream" => {
                        let id = host
                            .register_core_stream(&cx, shape, stream, &frame)
                            .await?;
                        codec.ok(Some(codec.record([("id", Val::U64(id))].into_iter())?))
                    }
                    StepOutput::Stream(mut stream) => {
                        stream.close().await.map_err(anyhow::Error::new)?;
                        bail!("value call returned a stream")
                    }
                    _ => bail!("stream call returned a value"),
                }
            }
            "pull" => {
                let values = crate::core_imports::pull_stream(
                    &host,
                    &cx,
                    uint(field(&params[0], "id")?)?,
                    u32::try_from(uint(&params[1])?)?,
                    &owners,
                )
                .await?;
                let array = values.array().context("stream did not return array")?;
                let mut out = wasmtime::component::BudgetedVecBuilder::new(
                    array.len(),
                    owners.component(),
                    0,
                )?;
                for value in array.iter() {
                    out.push(codec.json(&value)?)?;
                }
                codec.ok(Some(Val::List(out.finish()?)))
            }
            "invoke-callback" => {
                let value = crate::core_imports::invoke_callback(
                    &host,
                    &cx,
                    uint(field(&params[0], "id")?)?,
                    codec.parse(&params[1])?,
                    &owners,
                )
                .await?;
                codec.ok(Some(codec.json(&value)?))
            }
            "emit" => {
                let shape = ShapeRef::parse(text(&params[0])?)?;
                let event = codec.parse(&params[1])?;
                let args =
                    CoreValue::object_shared([Ok(("event", event))].into_iter(), owners.graph())?;
                let op = OpName::parse("emit")?;
                let frame = store
                    .data()
                    .data
                    .frame
                    .as_ref()
                    .context("dynamic emit lacks actual provider frame")?
                    .clone();
                match host
                    .call_guest_owned(&cx, &shape, &op, args, &frame)
                    .await?
                {
                    StepOutput::Value(_) => codec.ok(None),
                    StepOutput::Stream(mut stream) => {
                        stream.close().await.map_err(anyhow::Error::new)?;
                        bail!("output call returned a stream")
                    }
                }
            }
            "run-flow" => {
                let shape = ShapeRef::parse(text(&params[0])?)?;
                let input = codec.parse(&params[1])?;
                let frame = store
                    .data()
                    .data
                    .frame
                    .as_ref()
                    .context("guest Flow call lacks actual provider frame")?
                    .clone();
                let tobkiri_gate::Principal::Pack { pack, hash } = cx.principal() else {
                    bail!("guest Flow call is not an actual component Pack")
                };
                let verified = host
                    .shared
                    .verified
                    .get(pack)
                    .filter(|verified| &verified.hash == hash)
                    .context("guest Flow source does not match verified component")?;
                if !verified
                    .manifest
                    .ports
                    .consumes
                    .iter()
                    .any(|port| port.shape == shape.name && port.version == shape.version)
                {
                    host.shared
                        .gate
                        .record_denial(&cx, "guest-flow", "undeclared-view")
                        .await?;
                    bail!("guest Flow view is not declared by the component")
                }
                // The Core runner recognizes only its actual private provider
                // frame and host-call context. An AST witness or Input receipt
                // cannot authorize this dynamic entry. It owns the one fresh
                // nested FlowRun terminal; the caller's PortCall stays here.
                let digest = input.digest_guest(owners.graph())?;
                let runner = host
                    .shared
                    .flow_runner
                    .get()
                    .and_then(std::sync::Weak::upgrade)
                    .context("Core guest Flow runner is not installed")?;
                // Actual Store-installed frame, before the Core runner may
                // start its interpreter task. Closing registration fails shut.
                let native = host.native_work_spawner(&store.data().data).await?;
                let stream = runner
                    .run_guest_flow_with_spawner(
                        &cx,
                        frame.as_ref(),
                        &shape,
                        &digest,
                        input,
                        Arc::new(native.clone()),
                    )
                    .await?;
                let id = host
                    .register_core_stream_with_native(&cx, shape, stream, Some(&native))
                    .await?;
                codec.ok(Some(codec.record([("id", Val::U64(id))].into_iter())?))
            }
            _ => bail!("unknown generic Flow operation"),
        },
        "clock" => {
            let request = EffectRequest::Clock;
            let args = codec.args_hash(interface, version, operation, params)?;
            let grant = host
                .shared
                .gate
                .authorize(&cx, request.clone(), args.clone())
                .await?;
            let id = grant.id().clone();
            let permit = grant.consume(&cx, &request, &args)?;
            let cx = cx.narrow_limits(permit.limits());
            let result=async{match operation{"now-ms"=>Ok(vec![Val::U64(u64::try_from(SystemTime::now().duration_since(UNIX_EPOCH)?.as_millis())?)]),"monotonic-ns"=>{static START:std::sync::OnceLock<std::time::Instant>=std::sync::OnceLock::new();Ok(vec![Val::U64(u64::try_from(START.get_or_init(std::time::Instant::now).elapsed().as_nanos())?)])},"sleep-ms"=>{tokio::select!{_=cx.cancel().cancelled()=>bail!("clock sleep cancelled"),_=tokio::time::sleep_until(cx.deadline().into())=>bail!("original sleep deadline elapsed"),_=tokio::time::sleep(std::time::Duration::from_millis(uint(&params[0])?))=>{}}codec.ok(None)},_=>bail!("unknown clock operation")}}.await;
            outcome(&host, id, &result, &cx).await?;
            result
        }
        "random" => {
            if operation != "bytes" {
                bail!("unknown random operation")
            };
            let n = usize::try_from(uint(&params[0])?)?;
            if n as u64 > cx.limits().output_bytes || n > 1024 * 1024 {
                bail!("random output limit exceeded")
            }
            let request = EffectRequest::Random;
            let args = codec.args_hash(interface, version, operation, params)?;
            let grant = host
                .shared
                .gate
                .authorize(&cx, request.clone(), args.clone())
                .await?;
            let id = grant.id().clone();
            let permit = grant.consume(&cx, &request, &args)?;
            let cx = cx.narrow_limits(permit.limits());
            let result = (|| {
                if n as u64 > cx.limits().output_bytes {
                    bail!("random output exceeds grant budget")
                };
                let bytes = CoreBuffer::write_exact(n, owners.graph(), |out| {
                    getrandom::fill(out).map_err(|_| AllocationError::Unavailable)
                })?;
                Ok(vec![codec.bytes(bytes.as_bytes())?])
            })();
            outcome(&host, id, &result, &cx).await?;
            result
        }
        "host-io" => {
            let request = EffectRequest::HostIo {
                operation: operation.into(),
            };
            let args = codec.args_hash(interface, version, operation, params)?;
            let grant = host
                .shared
                .gate
                .authorize(&cx, request.clone(), args.clone())
                .await?;
            if operation == "terminal-read-line" {
                // The actual generation admits the owner before consumption.
                // Dropping this import cannot drop its consumed terminal.
                let spawner = host.native_work_spawner(&store.data().data).await?;
                return host_io_imports::read_line(
                    &host,
                    &cx,
                    spawner,
                    grant,
                    text(&params[0])?,
                    &owners,
                )
                .await;
            }
            let id = grant.id().clone();
            let permit = grant.consume(&cx, &request, &args)?;
            let cx = cx.narrow_limits(permit.limits());
            let producer_frame = store.data().data.frame.clone();
            let result = async {
                match operation {
                    "display" => {
                        let value = codec.parse(&params[1])?;
                        let frame = producer_frame
                            .as_ref()
                            .context("display lacks live producer frame")?;
                        let output = host.allocation_owners_for_frame(
                            &cx,
                            frame
                                .allocation_scope(tobkiri_gate::AllocationReason::OutputBuffer)
                                .map_err(anyhow::Error::new)?,
                        )?;
                        let buffer = value.encode_guest(output.graph())?;
                        if buffer.len() as u64 > cx.limits().output_bytes {
                            bail!("display output limit exceeded")
                        };
                        host.shared
                            .io
                            .display_budgeted(text(&params[0])?, &buffer)?;
                        codec.ok(None)
                    }
                    "terminal-write" => {
                        let value = text(&params[0])?;
                        if value.len() as u64 > cx.limits().output_bytes {
                            bail!("terminal output limit exceeded")
                        };
                        let buffer = CoreBuffer::copy_from(value.as_bytes(), owners.graph())?;
                        host.shared.io.terminal_write_budgeted(&buffer)?;
                        codec.ok(None)
                    }
                    "request-ui-embed" => Err(StepError::new(
                        "unavailable",
                        "guest UI embedding requires the Core Session registration bridge",
                    )
                    .into()),
                    "pick-directory" => {
                        filesystem_imports::pick_directory(&host, &cx, text(&params[0])?, &owners)
                            .await
                    }
                    _ => bail!("unknown host I/O operation"),
                }
            }
            .await;
            outcome(&host, id, &result, &cx).await?;
            result
        }
        "secrets" => {
            if !matches!(operation, "get" | "reveal") {
                bail!("unknown secret operation")
            };
            let name = bounded_name(&params[0])?;
            let request = EffectRequest::Secret {
                name: name.into(),
                reveal: operation == "reveal",
            };
            let grant = host
                .shared
                .gate
                .authorize(&cx, request, Vault::secret_digest(name)?)
                .await?;
            let spawner = host.native_work_spawner(&store.data().data).await?;
            secrets_imports::call(&host, &cx, spawner, grant, |secret, output_bytes| {
                match (operation, secret) {
                    ("get", SecretWorkResult::Opaque(secret)) => codec.ok(Some(push(
                        store,
                        version,
                        HostSecret {
                            name: name.into(),
                            _secret: secret,
                        },
                    )?)),
                    ("reveal", SecretWorkResult::Revealed(bytes)) => {
                        if bytes.bytes().len() as u64 > output_bytes {
                            bail!("secret output exceeds its bound")
                        }
                        // The Stage A owner zeroizes on every conversion exit.
                        codec.ok(Some(codec.string(std::str::from_utf8(bytes.bytes())?)?))
                    }
                    _ => Err(StepError::new("invalid", "secret result mode mismatch").into()),
                }
            })
            .await
        }
        "network" => network(store, &host, &cx, version, operation, params, &owners).await,
        "storage" => storage(store, &host, &cx, version, operation, params, &owners).await,
        "audit" => audit_imports::invoke(store, &host, &cx, operation, params, &owners).await,
        "fs" => {
            filesystem_imports::invoke(store, &host, &cx, version, operation, params, &owners).await
        }
        "proc" | "pty" | "approvals" => Err(StepError::new(
            "unavailable",
            "generic effect backend unavailable for this operation",
        )
        .into()),
        _ => bail!("unknown core interface"),
    }
}

async fn network(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    host: &PackHost,
    cx: &CallCx,
    version: &str,
    operation: &str,
    params: &[Val],
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    let codec = EffectValues(owners);
    if operation != "fetch" {
        let rep = resource_rep::<HostResponse>(store, version, &params[0])?;
        let request = get_resource::<HostResponse>(store, version, rep)?
            .request
            .clone();
        let args = codec.args_hash("network", version, operation, &params[1..])?;
        let grant = host
            .shared
            .gate
            .authorize(cx, request.clone(), args.clone())
            .await?;
        let id = grant.id().clone();
        let permit = grant.consume(cx, &request, &args)?;
        let narrowed = cx.clone().narrow_limits(permit.limits());
        let result = async {
            let response = &mut get_resource_mut::<HostResponse>(store, version, rep)?.response;
            response.check_active()?;
            match operation {
                "[method]response.status" => Ok(vec![Val::U16(response.status)]),
                "[method]response.headers" => {
                    let encoded = response.headers.encode_guest(owners.graph())?;
                    if encoded.len() as u64 > narrowed.limits().output_bytes {
                        bail!("response headers exceed grant output budget")
                    }
                    let array = response
                        .headers
                        .array()
                        .context("invalid response headers")?;
                    let mut out = wasmtime::component::BudgetedVecBuilder::new(
                        array.len(),
                        owners.component(),
                        0,
                    )?;
                    for pair in array.iter() {
                        let pair = pair.array().context("invalid response header pair")?;
                        let name = pair.get(0).context("header name missing")?;
                        let value = pair.get(1).context("header value missing")?;
                        out.push(
                            codec.tuple(
                                [
                                    codec.string(name.as_str().context("header name type")?)?,
                                    codec.string(value.as_str().context("header value type")?)?,
                                ]
                                .into_iter(),
                            )?,
                        )?;
                    }
                    Ok(vec![Val::List(out.finish()?)])
                }
                "[method]response.read" => {
                    let max = u32::try_from(uint(&params[1])?)?;
                    if u64::from(max) > narrowed.limits().output_bytes {
                        bail!("response read exceeds grant output budget")
                    }
                    let value = response.read_owned(max, owners.graph()).await?;
                    codec.ok(Some(match value {
                        None => Val::Option(None),
                        Some(value) => codec.some(codec.bytes(value.as_bytes())?)?,
                    }))
                }
                _ => bail!("unknown response operation"),
            }
        }
        .await;
        outcome(host, id, &result, &narrowed).await?;
        return result;
    }
    let req = &params[0];
    let url = text(field(req, "url")?)?;
    let method = text(field(req, "method")?)?;
    if url.len() > 8192 || method.len() > 32 {
        bail!("network target exceeds metadata bound")
    }
    let origin = url::Url::parse(url)?.origin().ascii_serialization();
    let request = EffectRequest::Network {
        origin,
        method: method.into(),
        url: url.into(),
    };
    let body = buffer_value(field(req, "body")?, &codec)?;
    if body
        .as_ref()
        .is_some_and(|body| body.len() as u64 > cx.limits().output_bytes)
    {
        bail!("network body exceeds its bound")
    }
    let raw_headers = list(field(req, "headers")?)?;
    if raw_headers.len() > 128 {
        bail!("network header count exceeded")
    };
    let descriptors = list(field(req, "secret-headers")?)?;
    let header_count = raw_headers
        .len()
        .checked_add(descriptors.len())
        .context("header count overflow")?;
    if header_count > 128 {
        bail!("network header count exceeded")
    }
    let mut headers =
        wasmtime::component::BudgetedVecBuilder::new(header_count, owners.component(), 0)?;
    let mut header_bytes = 0usize;
    for header in raw_headers {
        let Val::Tuple(pair) = header else {
            bail!("invalid header tuple")
        };
        if pair.len() != 2 {
            bail!("invalid header tuple arity")
        }
        let name = text(&pair[0])?;
        let value = text(&pair[1])?;
        header_bytes = header_bytes
            .checked_add(name.len())
            .and_then(|n| n.checked_add(value.len()))
            .context("header size overflow")?;
        if name.len() > 256 || value.len() > 16384 || header_bytes > 65536 {
            bail!("network headers exceed their bound")
        }
        // Original immutable tuple/string allocations keep their old leases.
        headers.push(header.clone())?;
    }
    let resources = list(&params[1])?;
    if resources.len() > 32 {
        bail!("secret reference count exceeded")
    };
    // Reject out-of-bounds metadata BEFORE network/secret Grant consumption, DNS or
    // keychain work. A huge prefix must never reach the native join buffer.
    for descriptor in descriptors {
        let index = usize::try_from(uint(field(descriptor, "index")?)?)?;
        if index >= resources.len() {
            bail!("secret header index outside resource list")
        }
        let name = text(field(descriptor, "name")?)?;
        let prefix = text(field(descriptor, "prefix")?)?;
        if name.len() > 256 || prefix.len() > SecretRef::MAX_NETWORK_HEADER_BYTES {
            bail!("network headers exceed their bound")
        }
        if prefix.contains(['\r', '\n']) {
            bail!("secret header contains newline")
        }
    }
    let mut names = Vec::with_capacity(resources.len());
    for value in resources {
        let rep = resource_rep::<HostSecret>(store, version, value)?;
        names.push(
            get_resource::<HostSecret>(store, version, rep)?
                .name
                .clone(),
        );
    }
    let names_graph = CoreValue::array_from(
        names
            .iter()
            .map(|name| CoreValue::string(name, owners.graph())),
        owners.graph(),
    )?;
    let request_graph = codec.wire(req)?;
    let hash = CoreValue::object_shared(
        [
            Ok(("request", request_graph)),
            Ok(("secret-names", names_graph)),
        ]
        .into_iter(),
        owners.graph(),
    )?
    .digest_guest(owners.graph())?;
    let grant = host
        .shared
        .gate
        .authorize(cx, request.clone(), hash.clone())
        .await?;
    let id = grant.id().clone();
    let result = async {
        let timeout = match field(req, "timeout-ms")? {
            Val::Option(None) => None,
            Val::Option(Some(value)) => Some(u32::try_from(uint(value)?)?),
            _ => bail!("invalid timeout"),
        };
        let client =
            crate::network::NetClient::from_grant(grant, cx, &request, &hash, timeout).await?;
        for descriptor in descriptors {
            let index = usize::try_from(uint(field(descriptor, "index")?)?)?;
            let name = names
                .get(index)
                .context("secret header index outside resource list")?;
            let secret_request = EffectRequest::Secret {
                name: name.clone(),
                reveal: false,
            };
            let secret_grant = host
                .shared
                .gate
                .authorize(cx, secret_request, Vault::secret_digest(name)?)
                .await?;
            let spawner = host.native_work_spawner(&store.data().data).await?;
            secrets_imports::call(host, cx, spawner, secret_grant, |secret, _| {
                let SecretWorkResult::Opaque(secret) = secret else {
                    return Err(StepError::new("invalid", "secret result mode mismatch").into());
                };
                let value = secret.with_network_value(
                    cx,
                    client.permit(),
                    text(field(descriptor, "prefix")?)?,
                    owners.graph(),
                    |value| -> Result<Val> { codec.string(std::str::from_utf8(value)?) },
                )??;
                let name = text(field(descriptor, "name")?)?;
                let value_len = text(&value)?.len();
                header_bytes = header_bytes
                    .checked_add(name.len())
                    .and_then(|n| n.checked_add(value_len))
                    .context("secret header size overflow")?;
                if name.len() > 256 || value_len > 16384 || header_bytes > 65536 {
                    bail!("network headers exceed their bound")
                }
                headers.push(codec.tuple([codec.string(name)?, value].into_iter())?)?;
                Ok(())
            })
            .await?;
        }
        // HTTP adapter still owns bounded native transport buffers; request body
        // is copied only by its owner-retaining bounded body bridge below.
        let headers = codec.wire(&Val::List(headers.finish()?))?;
        let response = client
            .send_owned(headers, body, owners.graph().clone())
            .await?;
        codec.ok(Some(push(
            store,
            version,
            HostResponse {
                response,
                request: request.clone(),
            },
        )?))
    }
    .await;
    outcome(host, id, &result, cx).await?;
    result
}

async fn storage(
    store: &mut StoreContextMut<'_, InvocationState<PackState>>,
    host: &PackHost,
    cx: &CallCx,
    version: &str,
    operation: &str,
    params: &[Val],
    owners: &ValueOwners,
) -> Result<Vec<Val>> {
    let codec = EffectValues(owners);
    if matches!(operation, "open" | "open-foreign") {
        let (owner, name) = if operation == "open" {
            (
                cx.principal()
                    .pack()
                    .context("store open requires Pack")?
                    .0
                    .clone(),
                bounded_name(&params[0])?,
            )
        } else {
            (PackId::parse(text(&params[0])?)?, bounded_name(&params[1])?)
        };
        let (request, args) = host.shared.stores.open_request(&owner, name)?;
        let grant = host.shared.gate.authorize(cx, request, args).await?;
        let id = grant.id().clone();
        let result = host
            .shared
            .stores
            .open_store(cx, &owner, name, grant)
            .map_err(anyhow::Error::new)
            .and_then(|handle| {
                codec.ok(Some(push(
                    store,
                    version,
                    HostStore::Profile(Arc::new(handle)),
                )?))
            });
        outcome(host, id, &result, cx).await?;
        return result;
    }
    if operation == "open-user-global" {
        let name = bounded_name(&params[0])?;
        let (request, args) = UserGlobalStore::open_request(name)?;
        let grant = host.shared.gate.authorize(cx, request, args).await?;
        let id = grant.id().clone();
        let result = UserGlobalStore::open(&host.shared.root, cx, name, grant)
            .map_err(anyhow::Error::new)
            .and_then(|handle| {
                codec.ok(Some(push(
                    store,
                    version,
                    HostStore::Global(Arc::new(handle)),
                )?))
            });
        outcome(host, id, &result, cx).await?;
        return result;
    }
    if operation.starts_with("[method]sql-transaction.") {
        if !matches!(version, "1.1.0" | "1.2.0") {
            bail!("typed SQL transaction is not a storage@1.0 resource")
        }
        let rep = resource_rep::<Arc<NativeSqlTransaction>>(store, version, &params[0])?;
        let transaction = get_resource::<Arc<NativeSqlTransaction>>(store, version, rep)?.clone();
        let mut delivery = WriteDeliveryReceipt::prepare(cx, None, false, transaction)?;
        return match operation {
            "[method]sql-transaction.sql-exec-typed" => {
                let statement = crate::storage_values::statement(params, owners)?;
                let policy = crate::storage_values::policy(&params[4])?;
                let result = delivery
                    ._hold
                    .exec_typed(
                        cx,
                        statement,
                        policy,
                        host.shared.gate.clone(),
                        owners.graph().clone(),
                    )
                    .await?;
                // A statement in an explicit transaction remains provisional.
                delivery.completed(result.confirmed_write_commit(), true);
                delivery.converted(cx, crate::storage_values::result(&result, owners))
            }
            "[method]sql-transaction.sql-query-typed" => {
                let statement = crate::storage_values::statement(params, owners)?;
                let result = delivery
                    ._hold
                    .query_typed(
                        cx,
                        statement,
                        host.shared.gate.clone(),
                        owners.graph().clone(),
                    )
                    .await?;
                delivery.completed(result.confirmed_write_commit(), true);
                delivery.converted(cx, crate::storage_values::result(&result, owners))
            }
            "[method]sql-transaction.commit" => {
                delivery._hold.commit(cx, host.shared.gate.clone()).await?;
                delivery.completed(true, true);
                delivery.converted(cx, codec.ok(None))
            }
            "[method]sql-transaction.rollback" => {
                delivery._hold.rollback(cx).await?;
                delivery.completed(false, true);
                delivery.converted(cx, codec.ok(None))
            }
            _ => bail!("unknown SQL transaction operation"),
        };
    }
    let rep = resource_rep::<HostStore>(store, version, &params[0])?;
    let handle = get_resource::<HostStore>(store, version, rep)?.clone();
    let budget = owners.graph();
    if matches!(
        operation,
        "[method]store.sql-exec" | "[method]store.sql-query"
    ) {
        let query = operation.ends_with("sql-query");
        let statement = crate::storage_values::legacy_statement(params, query, owners)?;
        let mut delivery = WriteDeliveryReceipt::prepare(cx, None, false, handle)?;
        let value = if query {
            delivery
                ._hold
                .sql_query_typed(cx, statement, host.shared.gate.clone(), budget.clone())
                .await?
        } else {
            delivery
                ._hold
                .sql_exec_typed(
                    cx,
                    statement,
                    tobkiri_store::SqlResultPolicy::ForbidRows,
                    host.shared.gate.clone(),
                    budget.clone(),
                )
                .await?
        };
        delivery.completed(value.confirmed_write_commit(), true);
        return delivery.converted(
            cx,
            crate::storage_values::legacy_result(&value, query, owners),
        );
    }
    if matches!(
        operation,
        "[method]store.sql-exec-typed"
            | "[method]store.sql-query-typed"
            | "[method]store.sql-begin"
    ) {
        if !matches!(version, "1.1.0" | "1.2.0") {
            bail!("typed SQL methods are not storage@1.0 operations")
        }
        let mut delivery = WriteDeliveryReceipt::prepare(cx, None, false, handle)?;
        return match operation {
            "[method]store.sql-exec-typed" => {
                let statement = crate::storage_values::statement(params, owners)?;
                let policy = crate::storage_values::policy(&params[4])?;
                let result = delivery
                    ._hold
                    .sql_exec_typed(
                        cx,
                        statement,
                        policy,
                        host.shared.gate.clone(),
                        budget.clone(),
                    )
                    .await?;
                delivery.completed(result.confirmed_write_commit(), true);
                delivery.converted(cx, crate::storage_values::result(&result, owners))
            }
            "[method]store.sql-query-typed" => {
                let statement = crate::storage_values::statement(params, owners)?;
                let result = delivery
                    ._hold
                    .sql_query_typed(cx, statement, host.shared.gate.clone(), budget.clone())
                    .await?;
                delivery.completed(result.confirmed_write_commit(), true);
                delivery.converted(cx, crate::storage_values::result(&result, owners))
            }
            "[method]store.sql-begin" => {
                let transaction = delivery
                    ._hold
                    .sql_begin(cx, host.shared.gate.clone(), budget.clone())
                    .await?;
                delivery.completed(false, true);
                delivery.converted(cx, codec.ok(Some(push(store, version, transaction)?)))
            }
            _ => unreachable!(),
        };
    }
    let value = match operation {
        "[method]store.kv-get" | "[method]store.kv-delete" => CoreValue::object_shared(
            [
                Ok((
                    "op",
                    CoreValue::string(
                        if operation.ends_with("kv-get") {
                            "kv-get"
                        } else {
                            "kv-delete"
                        },
                        budget,
                    )?,
                )),
                Ok(("key", CoreValue::string(text(&params[1])?, budget)?)),
            ]
            .into_iter(),
            budget,
        )?,
        "[method]store.kv-put" => CoreValue::object_shared(
            [
                Ok(("op", CoreValue::string("kv-put", budget)?)),
                Ok(("key", CoreValue::string(text(&params[1])?, budget)?)),
                Ok(("value", codec.wire(&params[2])?)),
            ]
            .into_iter(),
            budget,
        )?,
        "[method]store.kv-list" => CoreValue::object_shared(
            [
                Ok(("op", CoreValue::string("kv-list", budget)?)),
                Ok(("prefix", CoreValue::string(text(&params[1])?, budget)?)),
                Ok(("after", codec.wire(&params[2])?)),
                Ok(("limit", scalar_json(uint(&params[3])?, budget)?)),
            ]
            .into_iter(),
            budget,
        )?,
        _ => {
            return Err(StepError::new(
                "unavailable",
                "owned native typed SQL resource adapter is being connected",
            )
            .into());
        }
    };
    let fixed = FixedDataRequest::from_value(value, budget)?;
    let (request, args) = match &handle {
        HostStore::Profile(handle) => handle.fixed_request(&fixed)?,
        HostStore::Global(handle) => handle.fixed_request(&fixed)?,
    };
    let grant = host.shared.gate.authorize(cx, request, args).await?;
    let writes = fixed.operation().storage_operation().is_write();
    let mut delivery = WriteDeliveryReceipt::prepare(cx, Some(grant.id()), writes, handle)?;
    let executed = match &delivery._hold {
        HostStore::Profile(handle) => handle.execute_fixed(cx, fixed, grant, budget.clone()),
        HostStore::Global(handle) => handle.execute_fixed(cx, fixed, grant, budget.clone()),
    };
    if executed.is_ok() {
        // Store has completed its actual savepoint RELEASE and all internal
        // output/activity checks. Subsequent errors concern delivery only.
        delivery.completed(writes, false);
    }
    let result = executed
        .map_err(anyhow::Error::new)
        .and_then(|value| match operation {
            "[method]store.kv-get" => codec.ok(Some(match value.inspect() {
                tobkiri_canon::CoreRef::Null => Val::Option(None),
                tobkiri_canon::CoreRef::Arr(bytes) => {
                    let mut out = wasmtime::component::BudgetedVecBuilder::new(
                        bytes.len(),
                        owners.component(),
                        0,
                    )?;
                    for byte in bytes.iter() {
                        let tobkiri_canon::CoreRef::Int(byte) = byte.inspect() else {
                            bail!("store byte type mismatch")
                        };
                        out.push(Val::U8(u8::try_from(byte)?))?;
                    }
                    codec.some(Val::List(out.finish()?))?
                }
                _ => bail!("store returned invalid byte option"),
            })),
            "[method]store.kv-list" => {
                let values = value.array().context("store returned invalid key list")?;
                let mut out = wasmtime::component::BudgetedVecBuilder::new(
                    values.len(),
                    owners.component(),
                    0,
                )?;
                for value in values.iter() {
                    out.push(codec.string(value.as_str().context("store key is not text")?)?)?;
                }
                codec.ok(Some(Val::List(out.finish()?)))
            }
            _ => codec.ok(None),
        });
    delivery
        .finish_fixed(host.shared.gate.as_ref(), cx, result)
        .await
}

#[cfg(test)]
mod delivery_tests {
    use super::*;
    use std::sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    };
    use std::time::{Duration, Instant};
    use tobkiri_canon::{
        AllocationBudget, AllocationRequest, Digest,
        ids::{ProfileId, RequestId},
    };
    use tobkiri_gate::{CoreCancellation, CoreControl, GateError, Principal, ResourceLimits};
    use wasmtime::component::{HostAllocationBudget, HostAllocationError, HostAllocationRequest};

    struct DeniedPayload;
    impl AllocationBudget for DeniedPayload {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Err(AllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            assert_eq!(bytes, 0, "no payload allocation was admitted")
        }
    }
    impl HostAllocationBudget for DeniedPayload {
        fn reserve(&self, _: HostAllocationRequest) -> Result<(), HostAllocationError> {
            Err(HostAllocationError::Limit)
        }
        fn release(&self, bytes: u64) {
            // A failed first charge still destroys its initially empty lease.
            assert_eq!(bytes, 0, "no component allocation was admitted")
        }
    }

    #[derive(Default)]
    struct DeliveryGate {
        proposals: Mutex<Vec<Outcome>>,
        fail: bool,
    }
    #[async_trait::async_trait]
    impl Gate for DeliveryGate {
        fn check_context(&self, cx: &CallCx) -> Result<(), GateError> {
            cx.check_active()
        }
        async fn authorize(
            &self,
            _: &CallCx,
            _: EffectRequest,
            _: Digest,
        ) -> Result<tobkiri_gate::Grant, GateError> {
            panic!("delivery must not mint a replacement grant")
        }
        async fn record_outcome(&self, _: GrantId, outcome: Outcome) -> Result<(), GateError> {
            self.proposals.lock().unwrap().push(outcome);
            if self.fail {
                Err(GateError::Persistence("test delivery failure".into()))
            } else {
                Ok(())
            }
        }
        async fn record_denial(
            &self,
            _: &CallCx,
            _: &'static str,
            _: &'static str,
        ) -> Result<(), GateError> {
            panic!("delivery must not replace the original terminal")
        }
    }
    struct HeldResource {
        control: CoreControl,
        drops: Arc<AtomicU64>,
    }
    impl Drop for HeldResource {
        fn drop(&mut self) {
            assert_eq!(
                self.control.usage().count(ControlKind::Frame),
                1,
                "resource custody must drop before its control reservation"
            );
            self.drops.fetch_add(1, Ordering::AcqRel);
        }
    }
    fn context() -> CallCx {
        let control = CoreControl::default();
        let cancel = CoreCancellation::new_with_control(&control).unwrap();
        CallCx::try_root_with_control(
            ProfileId::parse("delivery-test").unwrap(),
            Principal::User,
            RequestId::parse("delivery-test").unwrap(),
            cancel,
            Instant::now() + Duration::from_secs(10),
            ResourceLimits::unlimited(),
            control,
        )
        .unwrap()
    }
    fn id() -> GrantId {
        serde_json::from_str("\"test-delivery-grant\"").unwrap()
    }

    #[tokio::test]
    async fn completed_write_codec_failure_is_unknown_even_after_cancel() {
        let cx = context();
        let gate = DeliveryGate::default();
        let drops = Arc::new(AtomicU64::new(0));
        let mut receipt = WriteDeliveryReceipt::prepare(
            &cx,
            Some(&id()),
            true,
            HeldResource {
                control: cx.control().clone(),
                drops: drops.clone(),
            },
        )
        .unwrap();
        // This latch represents Store's successful actual RELEASE, not fields
        // inferred from guest JSON or the spelling of a SQL statement.
        receipt.completed(true, false);
        let owners = ValueOwners::from_budgets(Arc::new(DeniedPayload), Arc::new(DeniedPayload));
        let codec = EffectValues(&owners);
        let encoded = codec
            .string("committed result")
            .and_then(|value| codec.ok(Some(value)));
        assert!(
            encoded.is_err(),
            "exercise the real component payload allocator"
        );
        cx.cancel().cancel();
        let error = receipt.finish_fixed(&gate, &cx, encoded).await.unwrap_err();
        assert!(commit_unknown(&error));
        assert!(receipt.terminal_done);
        assert!(matches!(gate.proposals.lock().unwrap().as_slice(),
            [Outcome::Failed { code }] if code == "storage-sql-commit-unknown"));
        assert_eq!(drops.load(Ordering::Acquire), 0);
        drop(receipt);
        assert_eq!(drops.load(Ordering::Acquire), 1);
        assert_eq!(cx.control().usage().count(ControlKind::Frame), 0);
    }

    #[tokio::test]
    async fn completed_write_outcome_failure_is_unknown_and_drop_does_not_retry() {
        let cx = context();
        let gate = DeliveryGate {
            fail: true,
            ..Default::default()
        };
        let mut receipt = WriteDeliveryReceipt::prepare(&cx, Some(&id()), true, ()).unwrap();
        receipt.completed(true, false);
        let error = receipt.finish_fixed(&gate, &cx, Ok(())).await.unwrap_err();
        assert!(commit_unknown(&error));
        assert!(!receipt.terminal_done);
        assert!(matches!(
            gate.proposals.lock().unwrap().as_slice(),
            [Outcome::Succeeded]
        ));
        drop(receipt);
        assert_eq!(gate.proposals.lock().unwrap().len(), 1);
        assert_eq!(cx.control().usage().count(ControlKind::Frame), 0);
    }

    #[tokio::test]
    async fn readonly_failures_stay_ordinary_and_backend_unknown_survives_delivery_failure() {
        let cx = context();
        let mut read = WriteDeliveryReceipt::prepare(&cx, Some(&id()), false, ()).unwrap();
        read.completed(false, false);
        let gate = DeliveryGate::default();
        let error = read
            .finish_fixed::<()>(&gate, &cx, Err(AllocationError::Limit.into()))
            .await
            .unwrap_err();
        assert!(error.downcast_ref::<AllocationError>().is_some());
        assert!(!commit_unknown(&error));
        assert!(matches!(gate.proposals.lock().unwrap().as_slice(),
            [Outcome::Failed { code }] if code == "host-effect"));
        drop(read);

        let gate = DeliveryGate {
            fail: true,
            ..Default::default()
        };
        let mut unknown = WriteDeliveryReceipt::prepare(&cx, Some(&id()), true, ()).unwrap();
        let error = unknown
            .finish_fixed::<()>(
                &gate,
                &cx,
                Err(tobkiri_store::StoreError::CommitUnknown.into()),
            )
            .await
            .unwrap_err();
        assert!(commit_unknown(&error));
        assert!(
            !unknown.sql_completed,
            "preserve the backend's distinct uncertainty proof"
        );
        drop(unknown);
        assert_eq!(gate.proposals.lock().unwrap().len(), 1);
        assert_eq!(cx.control().usage().count(ControlKind::Frame), 0);
    }

    #[tokio::test]
    async fn pending_move_keeps_its_repair_marker_and_one_terminal_attempt() {
        for fail in [false, true] {
            let cx = context();
            let gate = DeliveryGate {
                fail,
                ..Default::default()
            };
            let mut pending = WriteDeliveryReceipt::prepare(&cx, Some(&id()), true, ()).unwrap();
            let error = pending
                .finish_fixed::<()>(
                    &gate,
                    &cx,
                    Err(tobkiri_store::StoreError::PendingDataMove.into()),
                )
                .await
                .unwrap_err();
            assert!(matches!(
                error.downcast_ref::<tobkiri_store::StoreError>(),
                Some(tobkiri_store::StoreError::PendingDataMove)
            ));
            assert!(!pending.sql_completed);
            assert!(!commit_unknown(&error));
            assert!(matches!(gate.proposals.lock().unwrap().as_slice(),
                [Outcome::Failed { code }] if code == "storage-pending-data-move"));
            drop(pending);
            assert_eq!(gate.proposals.lock().unwrap().len(), 1);
            assert_eq!(cx.control().usage().count(ControlKind::Frame), 0);
        }
    }

    #[test]
    fn exhausted_receipt_capacity_rejects_before_work_and_provisional_results_stay_ordinary() {
        let cx = context();
        let occupied = cx
            .control()
            .reserve(
                ControlKind::Frame,
                64 * 1024 * 1024 - cx.control().usage().bytes,
            )
            .unwrap();
        assert!(WriteDeliveryReceipt::prepare(&cx, Some(&id()), true, ()).is_err());
        drop(occupied);
        let mut provisional = WriteDeliveryReceipt::prepare(&cx, None, false, ()).unwrap();
        provisional.completed(false, true);
        let error = provisional
            .converted::<()>(&cx, Err(AllocationError::Limit.into()))
            .unwrap_err();
        assert!(!commit_unknown(&error));
        let mut committed = WriteDeliveryReceipt::prepare(&cx, None, false, ()).unwrap();
        committed.completed(true, true);
        let error = committed
            .converted::<()>(&cx, Err(AllocationError::Limit.into()))
            .unwrap_err();
        assert!(commit_unknown(&error));
        drop((provisional, committed));
        assert_eq!(cx.control().usage().count(ControlKind::Frame), 0);
    }
}
