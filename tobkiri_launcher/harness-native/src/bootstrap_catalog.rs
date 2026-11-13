//! Fixed catalog loading shared by recovery and fresh Setup candidate selection.
//! Root selection and resource verification are Host operations. Pack catalog
//! policy stays in the same original Runtime receiver and its actual loader.
use super::*;
use crate::bootstrap_review::{Review, original_failure};
use crate::original_authority::catalog::{BindingFailure, CatalogIo};
use crate::original_authority::definitions::{self, DefinitionRecovery};
use crate::{BootstrapReviewFailure, BootstrapReviewReachpoint};
use review_continuation::ReviewCaller;
use std::path::PathBuf;

#[path = "bootstrap_recovery_error.rs"]
mod recovery_cause;

pub(super) struct RootResolution {
    pub resolved: CoreBuffer,
    pub binding: CoreBuffer,
}
pub(super) type Dispatch<S> = fn(
    &S,
    &NativeSetupInvocation,
    &mut CatalogIo,
    &CoreValue,
    &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer>;

pub(super) fn inspect<S: BootstrapInputOwner>(
    context: NativeImportContext<'_, State<S>>,
    work: ControlHandle<Work<S>>,
) -> Result<NativeImportOperation> {
    let dispatch = *work
        .get()
        .catalog_dispatch
        .get()
        .context("catalog source caller is not bound")?;
    let request = context
        .input()
        .context("catalog request is missing")?
        .clone();
    let request = CoreValue::parse_guest(request.as_bytes(), context.allocation_owners().graph())?;
    let custody = work.project(|value| value as &(dyn std::any::Any + Send + Sync));
    context.inspect_retaining(custody, move |output| {
        let state = work.get();
        if state.phase.load(Ordering::Acquire) != CATALOG_LOAD
            || !state.invocation.same_invocation(output.invocation())
        {
            return Err(NativeDataIoError::Retired);
        }
        state.source.check_live()?;
        output.charge_work(1)?;
        let mut current = state
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let io = current.as_mut().ok_or(NativeDataIoError::Unavailable)?;
        let response = dispatch(
            &state.source,
            &state.invocation,
            io,
            &request,
            output.allocation_owners().graph(),
        )
        .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
        output
            .charge_work(u64::try_from(response.len()).map_err(|_| NativeDataIoError::Capacity)?)?;
        output.append(response.as_bytes())
    })
}

fn dispatch<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    owner: &S,
    invocation: &NativeSetupInvocation,
    io: &mut CatalogIo,
    request: &CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreBuffer> {
    match io.inspect(owner, invocation, request, budget) {
        Ok(value) => Ok(value),
        Err(error) => {
            let Some(original) = error.downcast_ref::<crate::OriginalAuthorityError>() else {
                return Err(error);
            };
            if !matches!(&original.cause, Some(crate::OriginalAuthorityCause::Io(_))) {
                return Err(error);
            }
            let graph = io.error_graph(original, budget)?;
            let scope = request.get_key("scope").context("catalog scope missing")?;
            let mut out = CoreBuffer::builder(budget, MAX_BYTES);
            out.append(b"{\"format\":\"io.tobkiri.native-inspection-response.v1\",\"scope\":")?;
            out.append(scope.encode_guest(budget)?.as_bytes())?;
            out.append(b",\"error\":{\"code\":")?;
            bootstrap_metadata::string_parts(&mut out, [graph.code().as_str()?])?;
            out.append(b",\"message\":")?;
            bootstrap_metadata::string_parts(&mut out, [graph.message().as_str()?])?;
            out.append(b",\"details\":")?;
            bootstrap_metadata::string_parts(&mut out, [graph.details().as_str()?])?;
            out.append(b"}}")?;
            Ok(out.finish()?)
        }
    }
}

pub(super) async fn load<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
) -> Result<std::result::Result<(), BootstrapReviewFailure>> {
    caller.check(review, pack)?;
    pack.require_same_runtime()?;
    if pack
        .work
        .get()
        .source
        .with_original_catalog_source(|_| ())
        .is_err()
    {
        return Ok(Err(BootstrapReviewFailure::NotBound(
            BootstrapReviewReachpoint::CatalogSourceNotBound,
        )));
    }
    let call = pack.call.clone();
    review.actual.host_root = Some(pack.observe_fixed_call(3, &call, "host-root").await?);
    caller.check(review, pack)?;
    if let Some(failure) = take_failure(&mut review.actual.host_root) {
        return Ok(Err(handle_call_failure(
            review, pack, caller, failure, false,
        )
        .await?));
    }
    let resolution = pack
        .work
        .get()
        .review_root
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .take()
        .context("actual current Host root metadata missing")?;
    let _path_scratch = pack.work.get().invocation.reserve(
        CapacityKind::VfsBuffer,
        u64::try_from(
            resolution
                .resolved
                .len()
                .checked_mul(2)
                .and_then(|n| n.checked_add(1024))
                .context("root path scratch overflow")?,
        )?,
    )?;
    let runtime = parent(&resolution.resolved, 4)?;
    let io = CatalogIo::new(
        &pack.work.get().source,
        &pack.work.get().invocation,
        pack.cancellation,
        review.read.catalog_cleanup(),
        &runtime,
        pack.input_budget,
    );
    let io = match io {
        Ok(io) => io,
        Err(error) => return Ok(Err(original_failure(error))),
    };
    *pack
        .work
        .get()
        .catalog
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(io);
    pack.work
        .get()
        .catalog_dispatch
        .set(dispatch::<S>)
        .map_err(|_| anyhow::anyhow!("catalog caller already bound"))?;
    let development = {
        let mut current = pack
            .work
            .get()
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        current
            .as_mut()
            .expect("admitted source")
            .select_development(&pack.work.get().source, &pack.work.get().invocation)
    };
    let development = match development {
        Ok(value) => value,
        Err(error) => {
            return Ok(Err(binding_failure(
                review,
                pack,
                caller,
                BindingFailure::Original(error),
                false,
            )
            .await?));
        }
    };
    let (root, binding) = if development {
        (
            returned(review.actual.host_root.as_ref().expect("host root"))?.result(),
            resolution.binding,
        )
    } else {
        review.actual.bundle_root = Some(pack.observe_fixed_call(4, &call, "bundle-root").await?);
        caller.check(review, pack)?;
        if let Some(failure) = take_failure(&mut review.actual.bundle_root) {
            return Ok(Err(handle_call_failure(
                review, pack, caller, failure, false,
            )
            .await?));
        }
        let resolution = pack
            .work
            .get()
            .review_root
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
            .context("actual current bundle root metadata missing")?;
        let bundle = parent(&resolution.resolved, 2)?.join("v4");
        let selected = pack
            .work
            .get()
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_mut()
            .expect("catalog source")
            .select_packaged(&bundle);
        if let Err(error) = selected {
            return Ok(Err(original_failure(error)));
        }
        (
            returned(review.actual.bundle_root.as_ref().expect("bundle root"))?.result(),
            resolution.binding,
        )
    };
    // Borrow/copy only the opaque root reference before any other await. The
    // real returned buffers remain in Review.actual through finish.
    review.actual.catalog_input = Some(loader_call(pack, root)?);
    let manifest = {
        let mut current = pack
            .work
            .get()
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        current
            .as_mut()
            .expect("catalog source")
            .prepare_manifest(&pack.work.get().source, &pack.work.get().invocation)
    };
    let manifest = match manifest {
        Ok(value) => value,
        Err(error) => {
            return Ok(Err(
                binding_failure(review, pack, caller, error, true).await?
            ));
        }
    };
    if manifest {
        let input = {
            let current = pack
                .work
                .get()
                .catalog
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            manifest_call(
                pack,
                current
                    .as_ref()
                    .expect("catalog source")
                    .manifest_slice()
                    .context("reached manifest missing")?,
            )?
        };
        review.actual.manifest_input = Some(input.clone());
        review.actual.manifest = Some(pack.observe_fixed_call(2, &input, "manifest").await?);
        caller.check(review, pack)?;
        if let Some(failure) = take_failure(&mut review.actual.manifest) {
            return Ok(Err(handle_call_failure(
                review, pack, caller, failure, true,
            )
            .await?));
        }
        let verification = {
            let current = pack
                .work
                .get()
                .catalog
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            current.as_ref().expect("catalog source").verify_prepared(
                &pack.work.get().source,
                &pack.work.get().invocation,
                returned(
                    review
                        .actual
                        .manifest
                        .as_ref()
                        .expect("actual manifest parse"),
                )?,
            )
        };
        if let Err(error) = verification {
            return Ok(Err(
                binding_failure(review, pack, caller, error, false).await?
            ));
        }
        pack.work
            .get()
            .catalog
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .as_mut()
            .expect("catalog source")
            .clear_manifest();
    }
    pack.work
        .get()
        .catalog
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .as_mut()
        .expect("catalog source")
        .bind_loader(CoreValue::string(binding.as_str()?, pack.input_budget)?)?;
    let input = review
        .actual
        .catalog_input
        .as_ref()
        .expect("fixed loader input")
        .clone();
    review.actual.catalog = Some(pack.observe_fixed_call(7, &input, "catalog").await?);
    caller.check(review, pack)?;
    if let Some(failure) = take_failure(&mut review.actual.catalog) {
        return Ok(Err(handle_call_failure(
            review, pack, caller, failure, false,
        )
        .await?));
    }
    if returned(review.actual.catalog.as_ref().expect("actual catalog"))?
        .observation()
        .get_key("kind")
        .and_then(|v| v.as_str().map(|s| s == "bundled-catalog"))
        != Some(true)
    {
        bail!("actual load_catalog returned an unrepresented concrete catalog");
    }
    if !pack
        .work
        .get()
        .catalog
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .as_ref()
        .is_some_and(CatalogIo::completed)
    {
        bail!("actual loader returned before its reached resource occurrences completed");
    }
    caller.check(review, pack)?;
    Ok(Ok(()))
}

pub(super) async fn run_recovery<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> Result<std::result::Result<CoreBuffer, BootstrapReviewFailure>> {
    let caller = ReviewCaller::Recovery;
    if let Err(error) = load(review, pack, caller).await? {
        return Ok(Err(error));
    }
    // Repeat the actual selected user-data lookup at its original post-load
    // reachpoint. No constructor, exists, migration or Authority is advanced.
    if review.catalog_prefix {
        pack.work
            .get()
            .source
            .with_original_review_user_data(|_| ())?;
        return Ok(Err(BootstrapReviewFailure::NotBound(
            BootstrapReviewReachpoint::ProfileDefinitionStoreNotBound,
        )));
    }
    let definitions = definitions::recover(
        &review.read,
        &pack.work.get().source,
        pack.cancellation,
        pack.input_budget,
    );
    match definitions {
        Ok(outcome) => Ok(Err(BootstrapReviewFailure::NotBound(match outcome {
            DefinitionRecovery::ImportLegacy => {
                BootstrapReviewReachpoint::ImportLegacyCollectionNotBound
            }
            DefinitionRecovery::SuccessorBuilder(candidate) => {
                review.actual._successor_candidate = Some(candidate);
                BootstrapReviewReachpoint::LegacySuccessorBuilderNotBound
            }
            DefinitionRecovery::RepairLegacyDisplayNames => {
                BootstrapReviewReachpoint::RepairLegacyDisplayNamesNotBound
            }
        }))),
        Err(error) => {
            match recovery_cause::project(&error, &pack.work.get().invocation, pack.input_budget)? {
                Some(cause) => {
                    // No borrowed native String diagnostic may survive its
                    // prepaid Read through retirement/factory awaits. The
                    // fixed source raise text was checked by this projector.
                    drop(error);
                    Ok(Err(review_continuation::denied(
                        review,
                        pack,
                        caller,
                        recovery_cause::MESSAGE,
                        BootstrapDeniedRaise::ExplicitFrom(cause),
                    )
                    .await?))
                }
                // Still an explicitly UNQUALIFIED typed factory request (or
                // an uncaught raw registry error), never a fallback factory.
                None => Ok(Err(original_failure(error))),
            }
        }
    }
}

fn parent(resolved: &CoreBuffer, count: usize) -> Result<PathBuf> {
    if resolved.len() > 8192 {
        bail!("actual root exceeds the finite source path ABI");
    }
    let mut result = PathBuf::from(resolved.as_str()?);
    for _ in 0..count {
        if !result.pop() {
            bail!("actual module parent missing");
        }
    }
    Ok(result)
}
fn returned(value: &BootstrapIdentityOutcome) -> Result<&BootstrapIdentityValue> {
    match value {
        BootstrapIdentityOutcome::Returned(value) => Ok(value),
        _ => bail!("actual fixed call did not return"),
    }
}
// Move the actual opaque carrier, never reconstruct it from a kind/message.
fn take_failure(value: &mut Option<BootstrapIdentityOutcome>) -> Option<BootstrapIdentityOutcome> {
    if matches!(value, Some(BootstrapIdentityOutcome::Returned(_))) {
        None
    } else {
        value.take()
    }
}
async fn handle_call_failure<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
    value: BootstrapIdentityOutcome,
    manifest: bool,
) -> Result<BootstrapReviewFailure> {
    match value {
        BootstrapIdentityOutcome::GetterRaised(error) => {
            let json = error
                .observation()
                .get_key("kind")
                .and_then(|v| v.as_str().map(|s| s == "json-decode-error"))
                == Some(true);
            if manifest && json {
                review_continuation::denied(
                    review,
                    pack,
                    caller,
                    "packaged runtime resource manifest is invalid",
                    BootstrapDeniedRaise::OpaqueGetter(error),
                )
                .await
            } else if matches!(caller, ReviewCaller::Recovery)
                && error.recovery_catch() != BootstrapRecoveryCatch::None
            {
                review_continuation::denied(
                    review,
                    pack,
                    caller,
                    recovery_cause::MESSAGE,
                    BootstrapDeniedRaise::OpaqueGetter(error),
                )
                .await
            } else {
                Ok(BootstrapReviewFailure::GetterRaised(error))
            }
        }
        BootstrapIdentityOutcome::Refused(error) => {
            Ok(BootstrapReviewFailure::ObservationRefused(error))
        }
        BootstrapIdentityOutcome::Returned(_) => {
            bail!("returned fixed call was classified as a failure")
        }
    }
}
fn loader_call<S: BootstrapInputOwner>(
    pack: &ReviewPackBorrow<'_, S>,
    root: &CoreBuffer,
) -> Result<CoreBuffer> {
    let scope = CoreValue::parse_guest(pack.call.as_bytes(), pack.input_budget)?
        .get_key("scope")
        .context("call scope missing")?;
    let packet = denied::result(
        root,
        "io.tobkiri.native-value-result.v1",
        &scope,
        pack.input_budget,
    )?;
    let root = denied::field(&packet, "root")?;
    denied::ownership(&packet, &root, false)?;
    denied::frame(
        pack,
        "io.tobkiri.native-call.v1",
        [
            (
                "args",
                CoreValue::array_from([Ok(root)].into_iter(), pack.input_budget)?,
            ),
            (
                "kwargs",
                CoreValue::array_from(std::iter::empty(), pack.input_budget)?,
            ),
        ],
    )
}
fn manifest_call<S: BootstrapInputOwner>(
    pack: &ReviewPackBorrow<'_, S>,
    bytes: &[u8],
) -> Result<CoreBuffer> {
    let scope = CoreValue::parse_guest(pack.call.as_bytes(), pack.input_budget)?
        .get_key("scope")
        .context("call scope missing")?;
    let mut out = CoreBuffer::builder(pack.input_budget, MAX_BYTES);
    out.append(b"{\"format\":\"io.tobkiri.native-catalog-manifest-parse.v1\",\"scope\":")?;
    out.append(scope.encode_guest(pack.input_budget)?.as_bytes())?;
    out.append(b",\"bytes\":\"")?;
    crate::original_authority::catalog::append_base64(&mut out, bytes)?;
    out.append(b"\"}")?;
    Ok(out.finish()?)
}
async fn binding_failure<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
    error: BindingFailure,
    manifest_read: bool,
) -> Result<BootstrapReviewFailure> {
    match error {
        BindingFailure::Denied(message) => {
            review_continuation::denied(review, pack, caller, message, BootstrapDeniedRaise::Plain)
                .await
        }
        BindingFailure::Original(error) if error.class == crate::bootstrap_review::DENIED => {
            review_continuation::denied(
                review,
                pack,
                caller,
                &error.message,
                BootstrapDeniedRaise::Plain,
            )
            .await
        }
        BindingFailure::Original(error)
            if (manifest_read || matches!(caller, ReviewCaller::Recovery))
                && matches!(&error.cause, Some(crate::OriginalAuthorityCause::Io(_))) =>
        {
            let message = if manifest_read {
                "packaged runtime resource manifest is invalid"
            } else {
                recovery_cause::MESSAGE
            };
            let graph = pack
                .work
                .get()
                .catalog
                .lock()
                .unwrap_or_else(|error| error.into_inner())
                .as_mut()
                .context("actual catalog source missing")?
                .error_graph(&error, pack.input_budget)?;
            review_continuation::denied(
                review,
                pack,
                caller,
                message,
                BootstrapDeniedRaise::ExplicitFrom(graph),
            )
            .await
        }
        BindingFailure::Original(error) => Ok(original_failure(error)),
    }
}
