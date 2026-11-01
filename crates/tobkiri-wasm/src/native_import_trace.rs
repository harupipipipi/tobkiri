//! TEST-ONLY diagnostic DATA custody. Never compiled into production admission.
use super::{NativeState, ensure_active};
use anyhow::{Context, Result, bail, ensure};
use serde::ser::{Serialize, SerializeSeq, SerializeStruct, Serializer};
use std::{
    io::Write,
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_canon::{AllocationKind, AllocationRequest, CoreBuffer, LeasedShared};
use tobkiri_gate::{ControlKind, ControlReservation, NativeSetupInvocation};
use wasmtime::component::types::{ComponentItem, Type};
use wasmtime::component::{BudgetedFuture, HostAllocationOwner, Linker, Val};
use wasmtime::{Engine, StoreContextMut};

pub(super) const INTERFACE: &str = "tobkiri-ui:diagnostics/import-trace@1.0.0";
const MAX_RECORDS: usize = 128;
const MAX_NAME_BYTES: usize = 128;

struct Record {
    sequence: u64,
    name: CoreBuffer,
}
struct State {
    rows: [Option<Record>; MAX_RECORDS],
    recorded: usize,
    seen: u64,
    overflow: bool,
    malformed: bool,
}
struct Inner {
    // Buffers are destroyed before the ownership handles and control refund.
    state: Mutex<State>,
    failed: AtomicBool,
    invocation: NativeSetupInvocation,
    owners: crate::AllocationOwners,
}
type Held = LeasedShared<Inner, ControlReservation>;

/// A bounded trace stays funded independently through actual Store destruction.
#[derive(Clone)]
pub(in crate::native_setup_store) struct NativeImportTrace(Held);
impl NativeImportTrace {
    pub(in crate::native_setup_store) fn new(
        invocation: NativeSetupInvocation,
        owners: crate::AllocationOwners,
    ) -> Result<Self> {
        invocation.allocation_domain()?;
        // Prepay bounded snapshot header/flag serialization before retirement.
        owners.graph().reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            work: 1024,
        })?;
        // Same pinned Darwin PAL mutex allowance as the existing funded kernel.
        // This is requested storage, not an exact allocator/RSS claim.
        let bytes = (Held::storage_bytes() as u64)
            .checked_add((size_of::<std::os::raw::c_long>() + 56) as u64)
            .context("diagnostic trace metadata overflow")?;
        let control = invocation
            .control()
            .reserve(ControlKind::Registration, bytes)?;
        let state = Mutex::new(State {
            rows: std::array::from_fn(|_| None),
            recorded: 0,
            seen: 0,
            overflow: false,
            malformed: false,
        });
        drop(
            state
                .try_lock()
                .map_err(|_| anyhow::anyhow!("diagnostic trace mutex unavailable"))?,
        );
        Ok(Self(Held::try_new_precharged(
            Inner {
                state,
                failed: AtomicBool::new(false),
                invocation,
                owners,
            },
            control,
        )?))
    }
    fn record(&self, name: &str) -> Result<()> {
        self.0.get().invocation.allocation_domain()?;
        let result = self.record_inner(name);
        if result.is_err() {
            self.mark_failure();
        }
        result
    }
    fn mark_failure(&self) {
        if self.0.get().invocation.allocation_domain().is_ok() {
            self.0.get().failed.store(true, Ordering::Release);
        }
    }
    fn record_inner(&self, name: &str) -> Result<()> {
        let inner = self.0.get();
        inner.invocation.allocation_domain()?; // validate before any mutex
        inner.owners.graph().reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: 0,
            depth: 0,
            // Include name checks/copy and later borrowed JSON serialization.
            // No fresh work budget is acquired after Store retirement.
            work: (name.len() as u64).saturating_mul(8).saturating_add(256),
        })?;
        let mut state = inner
            .state
            .try_lock()
            .map_err(|_| anyhow::anyhow!("diagnostic trace mutex unavailable"))?;
        let sequence = state.seen;
        let Some(next) = sequence.checked_add(1) else {
            state.overflow = true;
            return Ok(());
        };
        state.seen = next;
        if name.is_empty()
            || name.len() > MAX_NAME_BYTES
            || !name.split('.').all(|part| {
                let mut bytes = part.bytes();
                bytes
                    .next()
                    .is_some_and(|b| b.is_ascii_alphabetic() || b == b'_')
                    && bytes.all(|b| b.is_ascii_alphanumeric() || b == b'_')
            })
        {
            state.malformed = true;
            return Ok(()); // preserve the original import outcome, but inconclusive
        }
        if state.recorded == MAX_RECORDS {
            state.overflow = true;
            return Ok(());
        }
        let record = Record {
            sequence,
            name: CoreBuffer::copy_from(name.as_bytes(), inner.owners.graph())?,
        };
        let index = state.recorded;
        state.rows[index] = Some(record);
        state.recorded += 1;
        Ok(())
    }
    /// Borrow/stream already funded DATA after retirement. Never allocate a new
    /// guest String/Vec/graph using an expired owner or export a raw descriptor.
    pub(super) fn write_snapshot(&self, writer: impl Write) -> Result<()> {
        self.0.get().invocation.allocation_domain()?;
        let state = self
            .0
            .get()
            .state
            .try_lock()
            .map_err(|_| anyhow::anyhow!("diagnostic trace mutex unavailable"))?;
        serde_json::to_writer(
            writer,
            &Snapshot(&state, self.0.get().failed.load(Ordering::Acquire)),
        )?;
        Ok(())
    }
}

struct Snapshot<'a>(&'a State, bool);
impl Serialize for Snapshot<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let state = self.0;
        let mut out = serializer.serialize_struct("NativeImportDiagnostic", 10)?;
        out.serialize_field("format", "tobkiri.native-import-diagnostic/1")?;
        out.serialize_field("diagnostic_only", &true)?;
        out.serialize_field("production_enabled", &false)?;
        out.serialize_field("seen", &state.seen)?;
        out.serialize_field("recorded", &state.recorded)?;
        out.serialize_field("overflow", &state.overflow)?;
        out.serialize_field("malformed", &state.malformed)?;
        out.serialize_field("recording_failed", &self.1)?;
        out.serialize_field(
            "trace_complete",
            &(state.recorded > 0
                && state.recorded as u64 == state.seen
                && !state.overflow
                && !state.malformed
                && !self.1),
        )?;
        out.serialize_field("records", &Rows(&state.rows[..state.recorded]))?;
        out.end()
    }
}
struct Rows<'a>(&'a [Option<Record>]);
impl Serialize for Rows<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let mut out = serializer.serialize_seq(Some(self.0.len()))?;
        for record in self.0 {
            out.serialize_element(record.as_ref().expect("contiguous funded trace"))?;
        }
        out.end()
    }
}
impl Serialize for Record {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let mut out = serializer.serialize_struct("ImportRecord", 2)?;
        out.serialize_field("sequence", &self.sequence)?;
        out.serialize_field(
            "module",
            self.name.as_str().expect("validated ASCII module"),
        )?;
        out.end()
    }
}

pub(super) fn validate(engine: &Engine, item: &ComponentItem) -> Result<()> {
    let ComponentItem::ComponentInstance(instance) = item else {
        bail!("diagnostic trace import must be an instance");
    };
    let mut exports = instance.exports(engine);
    let Some(("record-import", export)) = exports.next() else {
        bail!("diagnostic trace import differs");
    };
    ensure!(
        exports.next().is_none(),
        "diagnostic trace has extra exports"
    );
    let ComponentItem::ComponentFunc(func) = export.ty else {
        bail!("diagnostic trace must be a function");
    };
    ensure!(
        !func.async_() && func.params().eq([("name", Type::String)]) && func.results().len() == 0,
        "diagnostic trace signature differs"
    );
    Ok(())
}
pub(super) fn define<T: Send + 'static>(
    linker: &mut Linker<NativeState<T>>,
    owner: &HostAllocationOwner,
) -> Result<()> {
    linker.instance(INTERFACE)?.func_new_async_owned(
        "record-import",
        owner,
        |mut store, _ty, params, results| {
            let owner = store.data().owners.component().clone();
            BudgetedFuture::new(
                async move {
                    let result = record_callback(&mut store, params, results);
                    if result.is_err()
                        && let Some(trace) = store.data().import_trace.as_ref()
                    {
                        trace.mark_failure();
                    }
                    result.map_err(wasmtime::Error::from_anyhow)
                },
                &owner,
                0,
            )
        },
    )?;
    Ok(())
}

fn record_callback<T: Send + 'static>(
    store: &mut StoreContextMut<'_, NativeState<T>>,
    params: &[Val],
    results: &mut [Val],
) -> Result<()> {
    let [Val::String(name)] = params else {
        bail!("diagnostic trace arguments differ");
    };
    ensure!(results.is_empty(), "diagnostic trace result differs");
    ensure_active(store.data_mut())?;
    store
        .data()
        .import_trace
        .as_ref()
        .context("diagnostic trace is unbound")?
        .record(name.as_str())?;
    ensure_active(store.data_mut())?;
    Ok(())
}

#[cfg(test)]
#[path = "native_import_trace_tests.rs"]
mod tests;
