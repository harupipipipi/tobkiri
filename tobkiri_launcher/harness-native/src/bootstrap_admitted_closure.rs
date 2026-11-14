//! Traverse the original recovery catalog without materializing execution authority.
use super::*;
use crate::original_authority::external_catalog::{self, ClosureManifest};

pub(super) async fn run<S: BootstrapInputOwner + crate::BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    caller: ReviewCaller,
    seed: CoreValue,
) -> RecoveryOutcome {
    caller.check(review, pack)?;
    pack.require_same_runtime()?;
    let scope = actual_call_scope(pack)?;
    let catalog = result_root(
        pack,
        returned(
            review
                .actual
                .catalog
                .as_ref()
                .context("original recovery catalog required")?,
        )?,
        &scope,
    )?;
    let invocation = &pack.work.get().invocation;
    let lease = invocation.reserve(CapacityKind::VfsBuffer, 1)?;
    let mut queue = Vec::new();
    for id in seed
        .array()
        .context("admitted normal Pack IDs required")?
        .iter()
    {
        enqueue(&mut queue, &lease, id, pack.input_budget)?;
    }
    let mut external = CoreValue::array_builder_incremental(pack.input_budget, MAX_BYTES)?;
    let mut position = 0;
    while position < queue.len() {
        caller.check(review, pack)?;
        let id = queue[position].share();
        position += 1;
        let mut observation = match observe(
            pack,
            &scope,
            &catalog,
            &id,
            CoreValue::null(pack.input_budget)?,
            CoreValue::null(pack.input_budget)?,
        )
        .await?
        {
            Ok(observation) => observation,
            Err(problem) => {
                return Ok(Err(handle_call_failure(
                    review, pack, caller, problem, false,
                )
                .await?));
            }
        };
        if kind(&observation)? == "external-required" {
            let manifest = match external_catalog::read_closure_manifest(
                &review.read,
                &pack.work.get().source,
                pack.cancellation,
                pack.input_budget,
                id.as_str().expect("checked closure Pack ID"),
            ) {
                Ok(value) => value,
                Err(error) => return Ok(Err(registry_failure(review, pack, caller, error).await?)),
            };
            let (manifest, digest) = match manifest {
                ClosureManifest::External {
                    manifest,
                    artifact_digest,
                } => (manifest, artifact_digest),
                ClosureManifest::BundledSource => {
                    return Ok(Err(BootstrapReviewFailure::NotBound(
                        BootstrapReviewReachpoint::ClosureBundledPackRootNotBound,
                    )));
                }
                ClosureManifest::HostExtension => {
                    return Ok(Err(BootstrapReviewFailure::NotBound(
                        BootstrapReviewReachpoint::AdmittedHostExtensionProjectionNotBound,
                    )));
                }
            };
            observation = match observe(
                pack,
                &scope,
                &catalog,
                &id,
                manifest.share(),
                digest.share(),
            )
            .await?
            {
                Ok(observation) => observation,
                Err(problem) => {
                    return Ok(Err(handle_call_failure(
                        review, pack, caller, problem, false,
                    )
                    .await?));
                }
            };
            if kind(&observation)? != "external-read" {
                bail!("original closure did not consume the admitted manifest");
            }
            external.push(CoreValue::array_shared(
                [Ok(id), Ok(manifest), Ok(digest)].into_iter(),
                pack.input_budget,
            )?)?;
        } else if kind(&observation)? != "bundled" {
            bail!("original closure returned an unexpected membership result");
        }
        let dependencies = denied::field(&observation, "dependencies")?;
        let dependencies = dependencies
            .array()
            .context("ordered closure dependencies required")?;
        for dependency in dependencies.iter() {
            enqueue(&mut queue, &lease, dependency, pack.input_budget)?;
        }
    }
    review.actual._admitted_closure = Some(external.finish()?);
    Ok(Err(BootstrapReviewFailure::NotBound(
        BootstrapReviewReachpoint::AdmittedPackExecutableCatalogNotBound,
    )))
}

fn enqueue(
    queue: &mut Vec<CoreValue>,
    funding: &NativeSetupReservation,
    value: CoreValue,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<()> {
    let id = value
        .as_str()
        .context("admitted closure needs a string Pack ID")?;
    if !id.is_ascii() || id.is_empty() {
        bail!("admitted closure returned an invalid Pack ID");
    }
    budget.reserve(tobkiri_canon::AllocationRequest {
        kind: tobkiri_canon::AllocationKind::Temporary,
        bytes: 0,
        depth: 0,
        elements: 0,
        work: (id.len() + 1).saturating_mul(queue.len() + 1) as u64,
    })?;
    if queue.iter().any(|seen| seen.as_str() == Some(id)) {
        return Ok(());
    }
    if queue.len() == queue.capacity() {
        let next = queue
            .capacity()
            .max(4)
            .checked_mul(2)
            .context("Pack closure capacity")?;
        let old = queue.capacity();
        let bytes = std::mem::size_of::<CoreValue>();
        funding.resize(u64::try_from(
            (old + next)
                .checked_mul(bytes)
                .context("Pack closure capacity")?,
        )?)?;
        queue.try_reserve_exact(next - old)?;
        if queue.capacity() != next {
            bail!("Pack closure capacity differs");
        }
        funding.resize(u64::try_from(next * bytes)?)?;
    }
    queue.push(value);
    Ok(())
}

fn kind(value: &CoreValue) -> Result<&'static str> {
    match denied::field(value, "kind")?.as_str() {
        Some("bundled") => Ok("bundled"),
        Some("external-required") => Ok("external-required"),
        Some("external-read") => Ok("external-read"),
        _ => bail!("invalid original Pack closure result"),
    }
}

async fn observe<S: BootstrapInputOwner>(
    pack: &mut ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    catalog: &CoreValue,
    id: &CoreValue,
    manifest: CoreValue,
    artifact_digest: CoreValue,
) -> Result<std::result::Result<CoreValue, BootstrapIdentityOutcome>> {
    let request = denied::frame(
        pack,
        ADMITTED_CLOSURE_REQUEST,
        [
            ("catalog", catalog.share()),
            ("pack_id", id.share()),
            ("manifest", manifest),
            ("artifact_digest", artifact_digest),
        ],
    )?;
    let response = match pack
        .observe_fixed_projection(&request, "bootstrap-admitted-closure-step")
        .await?
    {
        Ok(response) => response,
        Err(error) => return Ok(Err(error)),
    };
    let result = CoreValue::parse_guest(response.as_bytes(), pack.input_budget)?;
    denied::exact(
        &result,
        &[
            "format",
            "scope",
            "catalog",
            "pack_id",
            "kind",
            "dependencies",
        ],
    )?;
    if denied::field(&result, "format")?.as_str()
        != Some("io.tobkiri.native-bootstrap-admitted-closure-step-result.v1")
        || !denied::same_scope(&denied::field(&result, "scope")?, scope)
        || !denied::same_reference(&denied::field(&result, "catalog")?, catalog)?
        || denied::field(&result, "pack_id")?.as_str() != id.as_str()
    {
        bail!("original Pack closure result has another origin");
    }
    let values = denied::field(&result, "dependencies")?;
    let values = values
        .array()
        .context("original Pack dependency array required")?;
    let mut previous: Option<CoreValue> = None;
    for value in values.iter() {
        let current = value
            .as_str()
            .context("original Pack dependency must be a string")?;
        if previous
            .as_ref()
            .and_then(CoreValue::as_str)
            .is_some_and(|prior| prior >= current)
        {
            bail!("original Pack dependencies must be unique and ordered");
        }
        previous = Some(value);
    }
    if kind(&result)? == "external-required" && !values.is_empty() {
        bail!("missing Pack already has a dependency result");
    }
    Ok(Ok(result))
}
