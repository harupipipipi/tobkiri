//! Generic trusted native User invocation; no Pack-specific execution path.
use anyhow::{Context, Result, bail};
use std::{io::Write, path::Path};
use tobkiri_core::{
    Core, CoreBuffer, Session, UserPortOutput, Variant,
    ids::{OpName, ProfileId, ShapeRef},
};
use tokio_util::sync::CancellationToken;

pub(super) async fn run(
    core: &Core,
    profile: &str,
    shape: &str,
    operation: &str,
    path: &Path,
) -> Result<()> {
    // This file is explicit native User metadata. Stdin remains exclusively
    // owned by the terminal host and its independently funded guest reads.
    let bytes = super::bounded_read(path, tobkiri_core::MAX_BYTES)?;
    let args = tobkiri_core::parse(&bytes, Variant::StrictV1)?;
    if !matches!(args, tobkiri_core::CanonValue::Obj(_)) {
        bail!("typed User arguments must be a StrictV1 object");
    }
    let shape = ShapeRef::parse(shape)?;
    let operation = OpName::parse(operation)?;
    let session = core.start(&ProfileId::parse(profile)?).await?;
    let outcome = invoke(&session, shape, operation, &args).await;
    super::complete_with_cleanup(outcome, session.close().await)
}

pub(super) async fn invoke(
    session: &Session,
    shape: ShapeRef,
    operation: OpName,
    args: &tobkiri_core::CanonValue,
) -> Result<()> {
    invoke_inner(
        session,
        shape,
        operation,
        args,
        #[cfg(feature = "closed-request-metrics")]
        None,
    )
    .await
}

#[cfg(feature = "closed-request-metrics")]
pub(super) async fn invoke_observed(
    session: &Session,
    shape: ShapeRef,
    operation: OpName,
    args: &tobkiri_core::CanonValue,
    observation: &tobkiri_core::closed_metrics::Observation,
) -> Result<()> {
    invoke_inner(session, shape, operation, args, Some(observation)).await
}

async fn invoke_inner(
    session: &Session,
    shape: ShapeRef,
    operation: OpName,
    args: &tobkiri_core::CanonValue,
    #[cfg(feature = "closed-request-metrics")] observation: Option<
        &tobkiri_core::closed_metrics::Observation,
    >,
) -> Result<()> {
    let cancel = CancellationToken::new();
    let mut call = Box::pin(async {
        #[cfg(feature = "closed-request-metrics")]
        if let Some(observation) = observation {
            return session
                .invoke_user_port_observed(shape, operation, args, cancel.clone(), observation)
                .await;
        }
        session
            .invoke_user_port(shape, operation, args, cancel.clone())
            .await
    });
    let output = tokio::select! {
        result = &mut call => result,
        _ = tokio::signal::ctrl_c() => {
            cancel.cancel();
            // Do not abandon a consumed grant or a pending physical dispatch.
            let result = call.await;
            if let Ok(UserPortOutput::Stream { handle, .. }) = &result { session.user_stream_cancel(handle).await?; }
            result?;
            bail!("User invocation cancelled");
        }
    }?;
    match output {
        UserPortOutput::Value(value) => write_buffer(&mut std::io::stdout().lock(), &value),
        UserPortOutput::Stream { envelope, handle } => {
            let outcome: Result<()> = async {
                write_buffer(&mut std::io::stdout().lock(), &envelope)?;
                loop {
                    let mut pull = Box::pin(session.user_stream_pull_buffer(&handle, 16));
                    let batch = tokio::select! {
                        result = &mut pull => result,
                        _ = tokio::signal::ctrl_c() => {
                            cancel.cancel();
                            // Continue polling this exact pull to acknowledged
                            // cleanup. A cancellation request is not completion.
                            // A filesystem-commit-unknown or cleanup error is
                            // more informative than the requested cancellation.
                            pull.await?;
                            bail!("User stream cancelled");
                        }
                    }?;
                    if batch.as_bytes() == b"[]" {
                        break;
                    }
                    write_buffer(&mut std::io::stdout().lock(), &batch)?;
                }
                Ok(())
            }
            .await;
            let cleanup = session.user_stream_cancel(&handle).await;
            super::complete_with_cleanup(outcome, cleanup)
        }
    }
}

fn write_buffer(output: &mut impl Write, value: &CoreBuffer) -> Result<()> {
    // Borrow producer-owned bytes through the actual native write; never parse
    // and rematerialize guest batches into unaccounted host JSON/Vec values.
    // Core-to-native return is the delivery boundary. A later stdout failure
    // never grants permission to replay a potentially completed User action.
    const FAILURE: &str = "User output transport failed; request may already have completed; do not automatically repeat it";
    output.write_all(value.as_bytes()).context(FAILURE)?;
    output.write_all(b"\n").context(FAILURE)?;
    output.flush().context(FAILURE)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    };
    struct Budget(AtomicU64);
    impl tobkiri_core::AllocationBudget for Budget {
        fn reserve(
            &self,
            request: tobkiri_core::AllocationRequest,
        ) -> Result<(), tobkiri_core::AllocationError> {
            self.0.fetch_add(request.bytes, Ordering::SeqCst);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.0.fetch_sub(bytes, Ordering::SeqCst);
        }
    }
    struct InspectWriter {
        owner: Arc<Budget>,
        output: Vec<u8>,
        fail: bool,
    }
    impl Write for InspectWriter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            assert!(self.owner.0.load(Ordering::SeqCst) > 0);
            if self.fail {
                return Err(std::io::Error::other("injected transport failure"));
            }
            self.output.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    #[test]
    fn native_transport_borrows_the_live_producer_on_success_and_failure() {
        let owner = Arc::new(Budget(AtomicU64::new(0)));
        let budget: Arc<dyn tobkiri_core::AllocationBudget> = owner.clone();
        let buffer = CoreBuffer::copy_from(br#"[{"kind":"progress"}]"#, &budget).unwrap();
        let charged = owner.0.load(Ordering::SeqCst);
        let mut writer = InspectWriter {
            owner: owner.clone(),
            output: vec![],
            fail: false,
        };
        write_buffer(&mut writer, &buffer).unwrap();
        assert_eq!(&writer.output, b"[{\"kind\":\"progress\"}]\n");
        writer.fail = true;
        assert!(write_buffer(&mut writer, &buffer).is_err());
        assert_eq!(owner.0.load(Ordering::SeqCst), charged);
        drop(buffer);
        assert_eq!(owner.0.load(Ordering::SeqCst), 0);
    }
}
