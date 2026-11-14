//! Fixed catalog loading shared by recovery and fresh Setup candidate selection.
//! Root selection and resource verification are Host operations. Pack catalog
//! policy stays in the same original Runtime receiver and its actual loader.
use super::*;
use crate::bootstrap_review::{BundleCalls, Review, original_failure};
use crate::original_authority::catalog::{BindingFailure, CatalogIo};
use crate::original_authority::definitions::{self, DefinitionRecovery};
use crate::{BootstrapReviewFailure, BootstrapReviewReachpoint};
use review_continuation::ReviewCaller;
use std::path::PathBuf;

#[path = "bootstrap_recovery_error.rs"]
mod recovery_cause;

pub(super) const BOOTSTRAP_TEMPLATE_REQUEST: &str =
    "io.tobkiri.native-bootstrap-template-lookup.v1";

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
    u8,
) -> Result<CoreBuffer>;

pub(super) fn inspect<S: BootstrapInputOwner>(
    context: NativeImportContext<'_, State<S>>,
    work: ControlHandle<Work<S>>,
    phase: u8,
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
    if phase == RESOLVE_PROFILE
        && request.get_key("format").and_then(|value| {
            value
                .as_str()
                .map(|format| format == crate::source_metadata::REQUEST)
        }) == Some(true)
    {
        let state = work.get();
        state
            .metadata_policy
            .context("original module metadata policy is unavailable")?;
        let module = crate::source_metadata::selected(
            &request,
            &state.invocation,
            state.index,
            &crate::source_metadata::ORIGINAL_PROJECTION_SOURCE_METADATA,
        )
        .context("metadata request is outside the original projection source operation")?;
        return inspect_source_metadata(context, work, phase, module);
    }
    let custody = work.project(|value| value as &(dyn std::any::Any + Send + Sync));
    context.inspect_retaining(custody, move |output| {
        let state = work.get();
        if state.phase.load(Ordering::Acquire) != phase
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
            phase,
        )
        .map_err(|_| output.reject_execution(NativeDataIoError::Unavailable))?;
        state.source.check_live()?;
        if state.phase.load(Ordering::Acquire) != phase {
            return Err(NativeDataIoError::Retired);
        }
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
    phase: u8,
) -> Result<CoreBuffer> {
    let result = match phase {
        CATALOG_LOAD => io.inspect(owner, invocation, request, budget),
        RESOLVE_PROFILE => io.inspect_pack_resolve(owner, invocation, request, budget),
        _ => bail!("catalog call phase is unavailable"),
    };
    match result {
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
    let (mut io, binding) = match select_bundle(review, pack, caller).await? {
        Ok(selected) => selected,
        Err(error) => return Ok(Err(error)),
    };
    io.bind_loader(CoreValue::string(binding.as_str()?, pack.input_budget)?)?;
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
        .and_then(|value| value.as_str().map(|kind| kind == "bundled-catalog"))
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

pub(super) async fn resolver_source_profile<
    S: BootstrapInputOwner + crate::BootstrapReviewOwner,
>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> Result<std::result::Result<(), BootstrapReviewFailure>> {
    let caller = ReviewCaller::Resolver;
    caller.check(review, pack)?;
    pack.require_same_runtime()?;
    let catalog = returned(
        review
            .actual
            .catalog
            .as_ref()
            .context("actual catalog missing")?,
    )?;
    let profile_id = returned(
        review
            .actual
            .resolver_getter
            .as_ref()
            .context("actual second Profile ID missing")?,
    )?;
    let input = resolver_source_lookup_call(pack, catalog, profile_id)?;
    review.actual.resolver_source_profile = Some(
        pack.observe_fixed_call(2, &input, "resolver-source-lookup")
            .await?,
    );
    caller.check(review, pack)?;
    if let Some(failure) = take_failure(&mut review.actual.resolver_source_profile) {
        return Ok(Err(handle_call_failure(
            review, pack, caller, failure, false,
        )
        .await?));
    }
    match returned(
        review
            .actual
            .resolver_source_profile
            .as_ref()
            .expect("actual lookup"),
    )?
    .builtin_kind()
    {
        BootstrapBuiltinKind::None => Ok(Err(review_continuation::denied(
            review,
            pack,
            caller,
            "bundled setup Profile is missing",
            BootstrapDeniedRaise::Plain,
        )
        .await?)),
        BootstrapBuiltinKind::Dict => Ok(Ok(())),
        _ => bail!("actual bundled Profile has an unrepresented concrete type"),
    }
}

fn resolver_source_lookup_call<S: BootstrapInputOwner>(
    pack: &ReviewPackBorrow<'_, S>,
    catalog: &BootstrapIdentityValue,
    profile_id: &BootstrapIdentityValue,
) -> Result<CoreBuffer> {
    let scope = actual_call_scope(pack)?;
    denied::frame(
        pack,
        "io.tobkiri.native-resolver-source-lookup.v1",
        [
            ("catalog", result_root(pack, catalog, &scope)?),
            ("profile_id", result_root(pack, profile_id, &scope)?),
        ],
    )
}

pub(super) fn actual_call_scope<S: BootstrapInputOwner>(
    pack: &ReviewPackBorrow<'_, S>,
) -> Result<CoreValue> {
    CoreValue::parse_guest(pack.call.as_bytes(), pack.input_budget)?
        .get_key("scope")
        .context("call scope missing")
}

pub(super) fn result_root<S: BootstrapInputOwner>(
    pack: &ReviewPackBorrow<'_, S>,
    value: &BootstrapIdentityValue,
    scope: &CoreValue,
) -> Result<CoreValue> {
    let packet = denied::result(
        value.result(),
        "io.tobkiri.native-value-result.v1",
        scope,
        pack.input_budget,
    )?;
    let root = denied::field(&packet, "root")?;
    denied::ownership(&packet, &root, false)?;
    Ok(root)
}

pub(super) async fn resolver_lock_digest<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> Result<std::result::Result<tobkiri_canon::Digest, BootstrapReviewFailure>> {
    let (io, _) = match select_bundle(review, pack, ReviewCaller::Resolver).await? {
        Ok(selected) => selected,
        Err(error) => return Ok(Err(error)),
    };
    pack.require_same_runtime()?;
    Ok(io
        .resolver_lock_digest(&pack.work.get().source, &pack.work.get().invocation)
        .map_err(original_failure))
}

fn bundle_calls(review: &mut Review, caller: ReviewCaller) -> &mut BundleCalls {
    match caller {
        ReviewCaller::Recovery | ReviewCaller::RecoveryTemplate | ReviewCaller::Candidate => {
            &mut review.actual.catalog_roots
        }
        ReviewCaller::Resolver => &mut review.actual.resolver_roots,
    }
}

async fn select_bundle<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
) -> Result<std::result::Result<(CatalogIo, CoreBuffer), BootstrapReviewFailure>> {
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
    bundle_calls(review, caller).host = Some(pack.observe_fixed_call(3, &call, "host-root").await?);
    caller.check(review, pack)?;
    if let Some(failure) = take_failure(&mut bundle_calls(review, caller).host) {
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
    let mut io = match io {
        Ok(io) => io,
        Err(error) => return Ok(Err(original_failure(error))),
    };
    let development = io.select_development(&pack.work.get().source, &pack.work.get().invocation);
    let development = match development {
        Ok(value) => value,
        Err(error) => {
            return Ok(Err(binding_failure(
                review,
                pack,
                caller,
                &mut io,
                BindingFailure::Original(error),
                false,
            )
            .await?));
        }
    };
    let binding = if development {
        resolution.binding
    } else {
        bundle_calls(review, caller).packaged =
            Some(pack.observe_fixed_call(4, &call, "bundle-root").await?);
        caller.check(review, pack)?;
        if let Some(failure) = take_failure(&mut bundle_calls(review, caller).packaged) {
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
        if let Err(error) = io.select_packaged(&bundle) {
            return Ok(Err(original_failure(error)));
        }
        resolution.binding
    };
    // The first loader prepares its argument at this boundary. The resolver
    // reaches the same root check without a second catalog load.
    if !matches!(caller, ReviewCaller::Resolver) {
        let roots = bundle_calls(review, caller);
        let root = roots
            .packaged
            .as_ref()
            .or(roots.host.as_ref())
            .expect("selected root");
        review.actual.catalog_input = Some(loader_call(pack, returned(root)?.result())?);
    }
    let manifest = io.prepare_manifest(&pack.work.get().source, &pack.work.get().invocation);
    let manifest = match manifest {
        Ok(value) => value,
        Err(error) => {
            return Ok(Err(binding_failure(
                review, pack, caller, &mut io, error, true,
            )
            .await?));
        }
    };
    if manifest {
        let input = manifest_call(
            pack,
            io.manifest_slice().context("reached manifest missing")?,
        )?;
        bundle_calls(review, caller)._manifest_input = Some(input.clone());
        bundle_calls(review, caller).manifest =
            Some(pack.observe_fixed_call(2, &input, "manifest").await?);
        caller.check(review, pack)?;
        if let Some(failure) = take_failure(&mut bundle_calls(review, caller).manifest) {
            return Ok(Err(handle_call_failure(
                review, pack, caller, failure, true,
            )
            .await?));
        }
        let verification = io.verify_prepared(
            &pack.work.get().source,
            &pack.work.get().invocation,
            returned(
                bundle_calls(review, caller)
                    .manifest
                    .as_ref()
                    .expect("actual manifest parse"),
            )?,
        );
        if let Err(error) = verification {
            return Ok(Err(binding_failure(
                review, pack, caller, &mut io, error, false,
            )
            .await?));
        }
        io.clear_manifest();
    }
    Ok(Ok((io, binding)))
}

pub(super) async fn run_recovery<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> super::listing::Outcome {
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
    let reachpoint = match definitions {
        Ok(DefinitionRecovery::ImportLegacy) => {
            BootstrapReviewReachpoint::ImportLegacyCollectionNotBound
        }
        Ok(DefinitionRecovery::SuccessorBuilder(candidate)) => {
            review.actual._successor_candidate = Some(candidate);
            BootstrapReviewReachpoint::LegacySuccessorBuilderNotBound
        }
        Ok(DefinitionRecovery::RepairLegacyDisplayNames) => {
            BootstrapReviewReachpoint::RepairLegacyDisplayNamesNotBound
        }
        #[cfg(unix)]
        Ok(DefinitionRecovery::ProfileDefinitions(profiles)) => {
            return read_admitted_catalog(review, pack, caller, profiles).await;
        }
        #[cfg(unix)]
        Ok(DefinitionRecovery::BootstrapTemplate(store)) => {
            return bootstrap_template(review, pack, store).await;
        }
        Err(error) => return Ok(Err(registry_failure(review, pack, caller, error).await?)),
    };
    Ok(Err(BootstrapReviewFailure::NotBound(reachpoint)))
}

#[cfg(unix)]
async fn bootstrap_template<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    store: definitions::BootstrapTemplateStore,
) -> super::listing::Outcome {
    let caller = ReviewCaller::RecoveryTemplate;
    pack.require_same_runtime()?;
    let identity = match review_continuation::validated_identity(review, pack, caller).await? {
        Ok(identity) => identity,
        Err(error) => return Ok(Err(error)),
    };
    let mut ascii = [0; 128];
    let key = identity.ascii_identity(&mut ascii)?;
    drop(identity);
    let scope = actual_call_scope(pack)?;
    let catalog = result_root(
        pack,
        returned(
            review
                .actual
                .catalog
                .as_ref()
                .context("actual recovery catalog missing")?,
        )?,
        &scope,
    )?;
    let profile_id = result_root(
        pack,
        returned(
            caller
                .getter(review)
                .as_ref()
                .context("actual recovery template key missing")?,
        )?,
        &scope,
    )?;
    let input = denied::frame(
        pack,
        BOOTSTRAP_TEMPLATE_REQUEST,
        [
            ("catalog", catalog.share()),
            ("profile_id", profile_id.share()),
        ],
    )?;
    let lookup = pack
        .observe_fixed_projection(&input, "bootstrap-template-lookup")
        .await?;
    caller.check(review, pack)?;
    let data = match lookup {
        Ok(data) => data,
        Err(error) => {
            return Ok(Err(
                handle_call_failure(review, pack, caller, error, false).await?
            ));
        }
    };
    let template =
        bootstrap_template_data(&data, &scope, &catalog, &profile_id, key, pack.input_budget)?;
    drop((data, input, scope, catalog, profile_id));
    let profiles = definitions::complete_bootstrap_template(
        &review.read,
        &pack.work.get().source,
        pack.cancellation,
        pack.input_budget,
        store,
        &template,
    );
    drop(template);
    let profiles = match profiles {
        Ok(profiles) => profiles,
        Err(error) => return Ok(Err(registry_failure(review, pack, caller, error).await?)),
    };
    read_admitted_catalog(review, pack, caller, profiles).await
}

#[cfg(unix)]
async fn read_admitted_catalog<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
    profiles: CoreValue,
) -> super::listing::Outcome {
    review.actual._recovery_profile_definitions = Some(profiles);
    let catalog = match crate::original_authority::external_catalog::read(
        &review.read,
        &pack.work.get().source,
        pack.cancellation,
        pack.input_budget,
    ) {
        Ok(catalog) => catalog,
        Err(error) => return Ok(Err(registry_failure(review, pack, caller, error).await?)),
    };
    let boundary = if catalog.has_saved_registry() {
        BootstrapReviewReachpoint::AdmittedPackCatalogAuthenticationNotBound
    } else {
        BootstrapReviewReachpoint::CatalogWithProfilesNotBound
    };
    review.actual._recovery_external_catalog = Some(catalog);
    Ok(Err(BootstrapReviewFailure::NotBound(boundary)))
}

#[cfg(unix)]
fn bootstrap_template_data(
    data: &CoreBuffer,
    scope: &CoreValue,
    catalog: &CoreValue,
    profile_id: &CoreValue,
    key: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let packet = CoreValue::parse_guest(data.as_bytes(), budget)?;
    denied::exact(
        &packet,
        &["format", "scope", "catalog", "profile_id", "profile"],
    )?;
    if denied::field(&packet, "format")?.as_str()
        != Some("io.tobkiri.native-bootstrap-template-data.v1")
        || !denied::same_scope(&denied::field(&packet, "scope")?, scope)
        || !denied::same_reference(&denied::field(&packet, "catalog")?, catalog)?
        || !denied::same_reference(&denied::field(&packet, "profile_id")?, profile_id)?
    {
        bail!("bootstrap template DATA differs from the actual catalog or getter");
    }
    let profile = denied::field(&packet, "profile")?;
    if profile
        .get_key("profile_id")
        .as_ref()
        .and_then(CoreValue::as_str)
        != Some(key)
    {
        bail!("bootstrap template Profile ID differs from the selected key");
    }
    Ok(profile)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use serde_json::{Value, json};
    use tobkiri_canon::{AllocationError, AllocationRequest};

    struct TestBudget;

    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    #[test]
    fn bootstrap_template_data_requires_same_catalog_getter_and_profile_key() -> Result<()> {
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let parse = |value: &Value| -> Result<CoreValue> {
            Ok(CoreValue::parse_guest(
                &serde_json::to_vec(value)?,
                &budget,
            )?)
        };
        let scope = json!(["1", "2", "3", "4"]);
        let catalog = json!({"scope": scope, "object": "1"});
        let profile_id = json!({"scope": scope, "object": "2"});
        let packet = json!({
            "format": "io.tobkiri.native-bootstrap-template-data.v1",
            "scope": scope,
            "catalog": catalog,
            "profile_id": profile_id,
            "profile": {"profile_id": "defaults", "display_name": "Tobkiri Harness"},
        });
        let scope = parse(&scope)?;
        let catalog = parse(&catalog)?;
        let profile_id = parse(&profile_id)?;
        let selected = |packet: &Value| -> Result<CoreValue> {
            let bytes = CoreBuffer::copy_from(&serde_json::to_vec(packet)?, &budget)?;
            bootstrap_template_data(&bytes, &scope, &catalog, &profile_id, "defaults", &budget)
        };
        assert_eq!(
            selected(&packet)?
                .get_key("display_name")
                .as_ref()
                .and_then(CoreValue::as_str),
            Some("Tobkiri Harness")
        );
        for (field, value) in [
            ("/scope/0", json!("5")),
            ("/catalog/object", json!("2")),
            ("/profile_id/object", json!("1")),
            ("/profile/profile_id", json!("alternate")),
        ] {
            let mut changed = packet.clone();
            *changed.pointer_mut(field).context("fixture field")? = value;
            assert!(selected(&changed).is_err(), "{field}");
        }
        Ok(())
    }
}

async fn registry_failure<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
    error: crate::OriginalAuthorityError,
) -> Result<BootstrapReviewFailure> {
    let Some(cause) =
        recovery_cause::project(&error, &pack.work.get().invocation, pack.input_budget)?
    else {
        return Ok(original_failure(error));
    };
    // The error diagnostic is funded by Read. Release it before awaiting the factory.
    drop(error);
    review_continuation::denied(
        review,
        pack,
        caller,
        recovery_cause::MESSAGE,
        BootstrapDeniedRaise::ExplicitFrom(cause),
    )
    .await
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
pub(super) fn returned(value: &BootstrapIdentityOutcome) -> Result<&BootstrapIdentityValue> {
    match value {
        BootstrapIdentityOutcome::Returned(value) => Ok(value),
        _ => bail!("actual fixed call did not return"),
    }
}
// Move the actual opaque carrier, never reconstruct it from a kind/message.
pub(super) fn take_failure(
    value: &mut Option<BootstrapIdentityOutcome>,
) -> Option<BootstrapIdentityOutcome> {
    if matches!(value, Some(BootstrapIdentityOutcome::Returned(_))) {
        None
    } else {
        value.take()
    }
}
pub(super) async fn handle_call_failure<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
    value: BootstrapIdentityOutcome,
    manifest: bool,
) -> Result<BootstrapReviewFailure> {
    match value {
        BootstrapIdentityOutcome::GetterRaised(error) => {
            let kind = error.observation().get_key("kind");
            let kind = kind.as_ref().and_then(CoreValue::as_str);
            if matches!(caller, ReviewCaller::RecoveryTemplate) && kind == Some("key-error") {
                review_continuation::denied(
                    review,
                    pack,
                    caller,
                    "application bootstrap Profile is unavailable from its catalog",
                    BootstrapDeniedRaise::OpaqueGetter(error),
                )
                .await
            } else if manifest && kind == Some("json-decode-error") {
                review_continuation::denied(
                    review,
                    pack,
                    caller,
                    "packaged runtime resource manifest is invalid",
                    BootstrapDeniedRaise::OpaqueGetter(error),
                )
                .await
            } else if caller.is_recovery() && error.recovery_catch() != BootstrapRecoveryCatch::None
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
    io: &mut CatalogIo,
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
            let graph = io.error_graph(&error, pack.input_budget)?;
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
