//! Edge references in the original fresh bundled Setup resolver. These are retained
//! candidate DATA; only the later original resolution can consume them.
use super::*;
use crate::BootstrapReviewOwner;
use crate::bootstrap_review::Review;
use review_continuation::ReviewCaller;
use tobkiri_canon::{AllocationKind, AllocationRequest, Digest};

pub(super) const STATIC_REQUEST: &str = "io.tobkiri.native-resolver-static-edges.v1";
pub(super) const DYNAMIC_REQUEST: &str = "io.tobkiri.native-resolver-dynamic-edges.v1";
pub(super) const VERIFIED_ARTIFACTS_REQUEST: &str =
    "io.tobkiri.native-resolver-verified-artifacts.v1";
const STATIC_RESPONSE: &str = "io.tobkiri.native-resolver-static-edges-result.v1";
const DYNAMIC_RESPONSE: &str = "io.tobkiri.native-resolver-dynamic-edges-result.v1";
const RESOLVE_REQUEST: &str = "io.tobkiri.native-resolver-profile.v1";
const RESOLVE_RESPONSE: &str = "io.tobkiri.native-resolver-profile-result.v1";
const RESOLVED_FIELDS: [&str; 4] = ["resolved", "profile", "lock", "plan"];
const SCHEMA: &str = "io.tobkiri.profile-authority-edge.v1";
const REF_PREFIX: &str = "authority-ref:";

pub(super) async fn bind_bundled_edges<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    profile_id_text: &str,
) -> listing::Outcome {
    let caller = ReviewCaller::Resolver;
    caller.check(review, pack)?;
    pack.require_same_runtime()?;
    let scope = catalog::actual_call_scope(pack)?;
    let source = catalog::returned(
        review
            .actual
            .resolver_source_profile
            .as_ref()
            .context("actual source Profile missing")?,
    )?;
    let source = catalog::result_root(pack, source, &scope)?;
    let request = denied::frame(pack, STATIC_REQUEST, [("source_profile", source.clone())])?;
    match pack
        .observe_fixed_projection(&request, "resolver-static-edges")
        .await?
    {
        Ok(rows) => review.actual.resolver_static_rows = Some(rows),
        Err(outcome) => {
            return Ok(Err(catalog::handle_call_failure(
                review, pack, caller, outcome, false,
            )
            .await?));
        }
    }
    caller.check(review, pack)?;
    let rows = CoreValue::parse_guest(
        review
            .actual
            .resolver_static_rows
            .as_ref()
            .expect("actual edge rows")
            .as_bytes(),
        pack.input_budget,
    )?;
    denied::exact(&rows, &["format", "scope", "source_profile", "rows"])?;
    if denied::field(&rows, "format")?.as_str() != Some(STATIC_RESPONSE)
        || !denied::same_scope(&denied::field(&rows, "scope")?, &scope)
        || !denied::same_reference(&denied::field(&rows, "source_profile")?, &source)?
    {
        bail!("requested-edge rows differ from their original Profile or scope");
    }
    let snapshot = review
        .actual
        .resolver_authority
        .as_ref()
        .context("actual resolver Authority snapshot missing")?
        .snapshot_digest();
    review.actual.resolver_bindings = Some(derive_bindings(
        &denied::field(&rows, "rows")?,
        snapshot,
        pack.input_budget,
    )?);
    drop((rows, request));
    observe_bundled_dynamic_edges(review, pack, &scope, source, profile_id_text).await
}

async fn observe_bundled_dynamic_edges<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    source: CoreValue,
    profile_id_text: &str,
) -> listing::Outcome {
    let caller = ReviewCaller::Resolver;
    caller.check(review, pack)?;
    let actual = &review.actual;
    let catalog = catalog::returned(actual.catalog.as_ref().context("actual catalog missing")?)?;
    let profile_id = catalog::returned(
        actual
            .resolver_getter
            .as_ref()
            .context("actual second Profile ID missing")?,
    )?;
    let references = [
        ("catalog", catalog::result_root(pack, catalog, scope)?),
        ("profile_id", catalog::result_root(pack, profile_id, scope)?),
        ("source_profile", source),
    ];
    let request = denied::frame(pack, DYNAMIC_REQUEST, references.clone())?;
    match pack
        .observe_fixed_projection(&request, "resolver-dynamic-edges")
        .await?
    {
        Ok(rows) => review.actual.resolver_dynamic_rows = Some(rows),
        Err(outcome) => {
            return Ok(Err(catalog::handle_call_failure(
                review, pack, caller, outcome, false,
            )
            .await?));
        }
    }
    caller.check(review, pack)?;
    let rows = CoreValue::parse_guest(
        review
            .actual
            .resolver_dynamic_rows
            .as_ref()
            .expect("actual dynamic edge rows")
            .as_bytes(),
        pack.input_budget,
    )?;
    validate_bundled_dynamic_rows(&rows, scope, &references)?;
    // The actual Pack returned an empty tuple for the fresh bundled candidate.
    // The original loop makes no updates to the retained authority bindings.
    drop((rows, request));
    let request = denied::frame(pack, VERIFIED_ARTIFACTS_REQUEST, references.clone())?;
    review.actual.resolver_verified_artifacts = Some(
        pack.observe_fixed_call(2, &request, "resolver-verified-artifacts")
            .await?,
    );
    caller.check(review, pack)?;
    if let Some(failure) = catalog::take_failure(&mut review.actual.resolver_verified_artifacts) {
        return Ok(Err(catalog::handle_call_failure(
            review, pack, caller, failure, false,
        )
        .await?));
    }
    let artifacts = catalog::returned(
        review
            .actual
            .resolver_verified_artifacts
            .as_ref()
            .expect("actual verified artifact set"),
    )?;
    if artifacts.builtin_kind() != BootstrapBuiltinKind::Set {
        bail!("original verified Profile artifacts did not return a set");
    }
    let artifacts = catalog::result_root(pack, artifacts, scope)?;
    let [
        (catalog_name, catalog),
        (profile_name, profile_id),
        (source_name, source),
    ] = references;
    let authority = review
        .actual
        .resolver_authority
        .as_ref()
        .context("actual resolver Authority snapshot missing")?;
    let bindings = review
        .actual
        .resolver_bindings
        .as_ref()
        .context("actual resolver bindings missing")?;
    let request = denied::frame(
        pack,
        RESOLVE_REQUEST,
        [
            (catalog_name, catalog),
            (profile_name, profile_id),
            (source_name, source),
            ("verified_artifacts", artifacts),
            (
                "snapshot",
                CoreValue::string(authority.snapshot_digest().as_str(), pack.input_budget)?,
            ),
            (
                "epoch",
                CoreValue::string(authority.security_epoch().decimal(), pack.input_budget)?,
            ),
            ("bindings", bindings.clone()),
        ],
    )?;
    resolve_profile(review, pack, scope, &request, profile_id_text).await
}

async fn resolve_profile<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
    scope: &CoreValue,
    request: &CoreBuffer,
    profile_id: &str,
) -> listing::Outcome {
    let caller = ReviewCaller::Resolver;
    caller.check(review, pack)?;
    pack.work
        .get()
        .catalog
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .as_mut()
        .context("completed original catalog controller missing")?
        .begin_pack_resolve(profile_id)?;
    match pack.observe_resolver_profile(request).await? {
        Ok(result) => review.actual.resolver_profile_result = Some(result),
        Err(failure) => {
            caller.check(review, pack)?;
            return Ok(Err(catalog::handle_call_failure(
                review, pack, caller, failure, false,
            )
            .await?));
        }
    }
    caller.check(review, pack)?;
    let shell_completed = pack
        .work
        .get()
        .catalog
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .as_ref()
        .is_some_and(
            crate::original_authority::catalog::CatalogIo::pack_resolve_shell_artifact_completed,
        );
    let result = CoreValue::parse_guest(
        review
            .actual
            .resolver_profile_result
            .as_ref()
            .expect("actual resolved Profile result")
            .as_bytes(),
        pack.input_budget,
    )?;
    validate_resolved_profile(&result, scope, shell_completed)?;
    if pack.work.get().source_occurrence.load(Ordering::Acquire) != SOURCE_COMPLETED {
        bail!("original resolver did not complete projection source resolution");
    }
    drop(result);
    predecessor::run(review, pack, scope, profile_id).await
}

fn validate_resolved_profile(
    result: &CoreValue,
    scope: &CoreValue,
    shell_completed: bool,
) -> Result<()> {
    if !shell_completed {
        bail!("original resolver did not finish verifying the selected Shell");
    }
    denied::exact(
        result,
        &[
            "format", "scope", "resolved", "profile", "lock", "plan", "owned",
        ],
    )?;
    if denied::field(result, "format")?.as_str() != Some(RESOLVE_RESPONSE)
        || !denied::same_scope(&denied::field(result, "scope")?, scope)
    {
        bail!("resolved Profile result scope or format differs");
    }
    let mut object_ids: [Option<CoreValue>; RESOLVED_FIELDS.len()] =
        [const { None }; RESOLVED_FIELDS.len()];
    for (index, name) in RESOLVED_FIELDS.into_iter().enumerate() {
        let reference = denied::field(result, name)?;
        denied::reference(&reference, scope)?;
        let id = denied::field(&reference, "object")?;
        if object_ids[..index]
            .iter()
            .flatten()
            .any(|existing| existing.as_str() == id.as_str())
        {
            bail!("resolved Profile fields alias each other");
        }
        object_ids[index] = Some(id);
    }
    let owned = denied::field(result, "owned")?;
    let owned = owned
        .array()
        .context("resolved Profile ownership is not an array")?;
    let mut remaining = object_ids.iter().flatten();
    for id in owned.iter() {
        if !remaining.any(|issued| issued.as_str() == id.as_str()) {
            bail!("resolved Profile ownership differs from the issued references");
        }
    }
    Ok(())
}

fn validate_bundled_dynamic_rows(
    value: &CoreValue,
    scope: &CoreValue,
    references: &[(&str, CoreValue); 3],
) -> Result<()> {
    denied::exact(
        value,
        &[
            "format",
            "scope",
            "catalog",
            "profile_id",
            "source_profile",
            "rows",
        ],
    )?;
    if denied::field(value, "format")?.as_str() != Some(DYNAMIC_RESPONSE)
        || !denied::same_scope(&denied::field(value, "scope")?, scope)
    {
        bail!("dynamic edge result scope or format differs");
    }
    for (name, reference) in references {
        if !denied::same_reference(&denied::field(value, name)?, reference)? {
            bail!("dynamic edges differ from their original resolver inputs");
        }
    }
    if denied::field(value, "rows")?
        .array()
        .is_none_or(|rows| !rows.is_empty())
    {
        bail!("fresh bundled Setup returned unbound dynamic edges");
    }
    Ok(())
}

// The ordered rows retain their key/value data through the same graph budget.
// These two temporary vectors own only slots and an index, also funded before
// allocation. A sorted index never changes the dictionary's insertion order.
struct Scratch<'a> {
    budget: &'a Arc<dyn AllocationBudget>,
    bytes: u64,
}
impl Drop for Scratch<'_> {
    fn drop(&mut self) {
        self.budget.release(self.bytes);
    }
}

fn charge(budget: &Arc<dyn AllocationBudget>, bytes: usize, work: usize) -> Result<()> {
    budget.reserve(AllocationRequest {
        kind: AllocationKind::Temporary,
        bytes: u64::try_from(bytes)?,
        elements: 0,
        depth: 0,
        work: u64::try_from(work)?,
    })?;
    Ok(())
}

fn derive_bindings(
    rows: &CoreValue,
    snapshot: &Digest,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let rows = rows
        .array()
        .context("requested-edge rows are not an array")?;
    let slots = rows.len();
    let bytes = slots
        .checked_mul(std::mem::size_of::<(CoreValue, Digest)>() + std::mem::size_of::<usize>())
        .context("requested-edge scratch overflow")?;
    charge(budget, bytes, slots)?;
    let _scratch = Scratch {
        budget,
        bytes: u64::try_from(bytes)?,
    };
    let mut ordered: Vec<(CoreValue, Digest)> = Vec::new();
    let mut sorted: Vec<usize> = Vec::new();
    ordered.try_reserve_exact(slots)?;
    sorted.try_reserve_exact(slots)?;
    if ordered.capacity() != slots || sorted.capacity() != slots {
        bail!("requested-edge scratch capacity differs");
    }

    let schema = CoreValue::string(SCHEMA, budget)?;
    let snapshot = CoreValue::string(snapshot.as_str(), budget)?;
    for row in rows.iter() {
        let fields = row
            .array()
            .filter(|fields| fields.len() == 4)
            .context("requested-edge row must have four fields")?;
        let mut key = CoreBuffer::builder(budget, MAX_BYTES);
        for (index, field) in fields.iter().enumerate() {
            if index > 0 {
                key.append(b"|")?;
            }
            key.append(
                field
                    .as_str()
                    .context("requested-edge field is not exact text")?
                    .as_bytes(),
            )?;
        }
        let key = CoreValue::string_from_utf8(&key.finish()?, budget)?;
        let preimage = CoreValue::object_shared(
            [
                Ok(("edge", key.clone())),
                Ok(("profile_authority_snapshot_digest", snapshot.clone())),
                Ok(("schema", schema.clone())),
            ]
            .into_iter(),
            budget,
        )?;
        let digest = preimage.digest_guest(budget)?;
        let name = key.as_str().expect("constructed edge key");
        let mut low = 0;
        let mut high = sorted.len();
        while low < high {
            let mid = low + (high - low) / 2;
            let compared = ordered[sorted[mid]].0.as_str().expect("stored edge key");
            charge(budget, 0, name.len().min(compared.len()) + 1)?;
            if compared < name {
                low = mid + 1;
            } else {
                high = mid;
            }
        }
        if let Some(&position) = sorted.get(low) {
            let compared = ordered[position].0.as_str().expect("stored edge key");
            charge(budget, 0, name.len().min(compared.len()) + 1)?;
            if compared == name {
                ordered[position].1 = digest;
                continue;
            }
        }
        charge(budget, 0, sorted.len() - low + 1)?;
        sorted.insert(low, ordered.len());
        ordered.push((key, digest));
    }

    let mut output = CoreValue::array_builder(budget, ordered.len())?;
    for (key, digest) in ordered {
        let mut value = [0; REF_PREFIX.len() + 64];
        let (prefix, hex) = value.split_at_mut(REF_PREFIX.len());
        prefix.copy_from_slice(REF_PREFIX.as_bytes());
        hex.copy_from_slice(&digest.as_str().as_bytes()[7..]);
        let value = CoreValue::string(std::str::from_utf8(&value)?, budget)?;
        output.push(CoreValue::array_shared(
            [Ok(key), Ok(value)].into_iter(),
            budget,
        )?)?;
    }
    Ok(output.finish()?)
}

#[cfg(test)]
#[path = "bootstrap_resolver_edges_tests.rs"]
mod tests;
