//! Static edge references in the original Setup resolver. These are retained
//! candidate DATA; only the later original resolution can consume them.
use super::*;
use crate::bootstrap_review::Review;
use crate::{BootstrapReviewFailure, BootstrapReviewOwner, BootstrapReviewReachpoint};
use review_continuation::ReviewCaller;
use tobkiri_canon::{AllocationKind, AllocationRequest, Digest};

const REQUEST: &str = "io.tobkiri.native-resolver-static-edges.v1";
const RESPONSE: &str = "io.tobkiri.native-resolver-static-edges-result.v1";
const SCHEMA: &str = "io.tobkiri.profile-authority-edge.v1";
const REF_PREFIX: &str = "authority-ref:";

pub(super) async fn bind_static<S: BootstrapInputOwner + BootstrapReviewOwner>(
    review: &mut Review,
    pack: &mut ReviewPackBorrow<'_, S>,
) -> Result<BootstrapReviewFailure> {
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
    let request = denied::frame(pack, REQUEST, [("source_profile", source.clone())])?;
    match pack.observe_resolver_static_rows(&request).await? {
        Ok(rows) => review.actual.resolver_static_rows = Some(rows),
        Err(outcome) => {
            return catalog::handle_call_failure(review, pack, caller, outcome, false).await;
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
    if denied::field(&rows, "format")?.as_str() != Some(RESPONSE)
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
    review.actual._resolver_static_bindings = Some(derive_bindings(
        &denied::field(&rows, "rows")?,
        snapshot,
        pack.input_budget,
    )?);
    caller.check(review, pack)?;
    Ok(BootstrapReviewFailure::NotBound(
        BootstrapReviewReachpoint::ResolveDynamicEdgesNotBound,
    ))
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
