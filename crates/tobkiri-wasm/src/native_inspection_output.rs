//! Native-selected work over the original IO slot and borrowed DATA writer.
use super::{NativeDataIo, NativeDataIoError, Result, Scope, Value};
use crate::AllocationOwners;
use std::cell::Cell;
use tobkiri_canon::{AllocationError, CoreBuffer, CoreBufferBuilder, MAX_BYTES};
use tobkiri_gate::NativeSetupInvocation;

/// Output of one nativeKit inspection at its original reachpoint.
///
/// Native code selects the callback and retains its actual target, phase and
/// authority bindings through worker execution. This writer grants no effects
/// and is not a guest-selected callback or an ordinary Pack capability.
/// Its private construction fixes the original owner and total envelope bound.
pub struct NativeInspectionOutput<'scope> {
    scope: &'scope Scope,
    bytes: CoreBufferBuilder,
    failed: Option<NativeDataIoError>,
}

/// A producer-local activity/work check on the original IO scope.
///
/// It cannot escape its borrowed callback, clone custody, issue an owner or
/// expose a Store/Gate/IO API. Failures are latched privately: ignoring one or
/// returning a replacement domain error cannot authorize publication.
pub struct NativeInspectionCheckpoint<'scope> {
    scope: &'scope Scope,
    failed: &'scope Cell<Option<NativeDataIoError>>,
}

impl NativeInspectionCheckpoint<'_> {
    /// Check original retirement/cancellation/deadline and charge original work.
    /// This does not interrupt an already blocking OS syscall or reset budgets.
    pub fn charge_work(&self, work: u64) -> Result<()> {
        if let Some(error) = self.failed.get() {
            return Err(error);
        }
        self.scope.work(work).inspect_err(|&error| {
            self.failed.set(Some(error));
        })
    }
}

impl NativeInspectionOutput<'_> {
    fn fail(&mut self, error: NativeDataIoError) -> NativeDataIoError {
        *self.failed.get_or_insert(error)
    }

    /// Charge original host work and check the original owner/retirement clocks.
    /// A failed check is sticky, even if native callback code ignores its error.
    pub fn charge_work(&mut self, work: u64) -> Result<()> {
        if let Some(error) = self.failed {
            return Err(error);
        }
        self.scope.work(work).map_err(|error| self.fail(error))
    }

    /// Borrow the actual original invocation; no substitute identity is made.
    pub fn invocation(&self) -> &NativeSetupInvocation {
        &self.scope.invocation
    }

    /// Borrow only the original allocation owners for native temporary DATA.
    pub fn allocation_owners(&self) -> &AllocationOwners {
        &self.scope.owners
    }

    /// Number of bytes staged privately, including framing and error DATA.
    pub fn len(&self) -> usize {
        self.bytes.len()
    }

    /// Whether the private output contains no staged bytes.
    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }

    /// Append borrowed DATA using only the original 4 MiB envelope/64 MiB owner.
    pub fn append(&mut self, bytes: &[u8]) -> Result<()> {
        self.charge_work(1)?;
        self.bytes
            .append(bytes)
            .map_err(|_| self.fail(NativeDataIoError::Capacity))?;
        self.charge_work(0)
    }

    /// Prepay a bounded borrowed tail BEFORE a native producer performs IO.
    ///
    /// A producer's original typed error is returned unchanged in the inner
    /// result, with the unused tail rewound. NativeKit may encode a supported
    /// domain error graph; execution/accounting failures remain outer errors.
    /// Failed accounting or invalid producer lengths permanently prevent
    /// publication, while native error fields still require their own original
    /// disclosure/authority contract. No error is automatically made portable.
    pub fn write_chunk<E, F>(
        &mut self,
        maximum: usize,
        producer: F,
    ) -> Result<std::result::Result<usize, E>>
    where
        F: FnOnce(&mut [u8]) -> std::result::Result<usize, E>,
    {
        self.write_chunk_checked(maximum, |tail, _checkpoint| producer(tail))
    }

    /// Prepay one producer tail with a nonescaping original activity checkpoint.
    ///
    /// The kernel checks activity before execution and successful tail commit
    /// even if the producer never uses the checkpoint. Its first failure is
    /// sticky and takes priority over producer success/domain errors. Physical
    /// work/FD custody still remains held by the original worker and Join.
    pub fn write_chunk_checked<E, F>(
        &mut self,
        maximum: usize,
        producer: F,
    ) -> Result<std::result::Result<usize, E>>
    where
        F: FnOnce(&mut [u8], NativeInspectionCheckpoint<'_>) -> std::result::Result<usize, E>,
    {
        self.charge_work(1)?;
        let scope = self.scope;
        let failed = Cell::new(None);
        let checkpoint = NativeInspectionCheckpoint {
            scope,
            failed: &failed,
        };
        let mut producer_error = None;
        let mut invalid_length = false;
        let result = self
            .bytes
            .write_chunk(maximum, |tail| match producer(tail, checkpoint) {
                Ok(count) if count <= tail.len() => {
                    if failed.get().is_some() {
                        return Err(AllocationError::Invalid);
                    }
                    if let Err(error) = scope.work(0) {
                        failed.set(Some(error));
                        return Err(AllocationError::Invalid);
                    }
                    Ok(count)
                }
                Ok(_) => {
                    invalid_length = true;
                    Err(AllocationError::Invalid)
                }
                Err(error) => {
                    producer_error = Some(error);
                    Err(AllocationError::Invalid)
                }
            });
        if let Some(error) = failed.get() {
            return Err(self.fail(error));
        }
        self.charge_work(0)?;
        match (result, producer_error) {
            (Ok(count), None) => Ok(Ok(count)),
            (Err(_), Some(error)) => Ok(Err(error)),
            _ => Err(self.fail(if invalid_length {
                NativeDataIoError::CompletionUnknown
            } else {
                NativeDataIoError::Capacity
            })),
        }
    }

    fn finish(mut self) -> Result<CoreBuffer> {
        self.charge_work(1)?;
        self.bytes.finish().map_err(|_| NativeDataIoError::Capacity)
    }
}

impl NativeDataIo {
    /// Execute native-selected inspection at the original live IO reachpoint.
    ///
    /// Uses the existing one-inflight slot, physical Joins, original cancellation,
    /// deadline and owners. It never snapshots/caches an inspection result or
    /// interprets a domain operation, path, Pack ID, permission or error graph.
    /// NativeKit must retain and validate the actual target/phase/authority in
    /// its callback, not merely pass a previously checked approval boolean.
    /// Partial output cannot escape callback, accounting or retirement failure.
    /// There is deliberately no guest import that selects this callback.
    pub async fn inspect<F>(&self, work: F) -> Result<CoreBuffer>
    where
        F: for<'scope> FnOnce(&mut NativeInspectionOutput<'scope>) -> Result<()> + Send + 'static,
    {
        let held = self.clone();
        let value = self
            .execute(move || {
                let scope = &held.0.get().scope;
                let mut output = NativeInspectionOutput {
                    scope,
                    bytes: CoreBuffer::builder(scope.owners.graph(), MAX_BYTES),
                    failed: None,
                };
                let completed = work(&mut output);
                if let Some(error) = output.failed {
                    return Err(error);
                }
                completed?;
                output.finish().map(Value::Bytes)
            })
            .await?;
        match value {
            Value::Bytes(bytes) => Ok(bytes),
            _ => Err(NativeDataIoError::CompletionUnknown),
        }
    }
}
