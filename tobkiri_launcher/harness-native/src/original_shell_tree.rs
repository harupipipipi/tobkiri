//! Host-owned cursor for the original Shell digest's sorted depth-first walk.
use super::shell_inputs::{MAX_NAME, single_leaf};
use super::*;

// The guest ordered JSON codec permits 100,000 nodes in the whole frame.
const MAX_LISTING_NAMES: usize = 100_000 - 32;
pub(super) const MAX_TREE_DEPTH: usize = 128;

#[derive(Default)]
pub(super) struct ShellTree {
    listing: Option<Box<Listing>>,
}

// Only the currently open lexical walk is retained. No descendant's name is
// admitted before its parent was enumerated, and a list is visited only once.
struct Listing {
    identity: Identity,
    names: Vec<CoreValue>,
    next: usize,
    parent: Option<Box<Listing>>,
    _capacity: NativeSetupReservation,
}

impl Drop for ShellTree {
    fn drop(&mut self) {
        while let Some(mut listing) = self.listing.take() {
            self.listing = listing.parent.take();
        }
    }
}

impl ShellTree {
    pub(super) fn matches(
        &self,
        selected: &str,
        steps: &tobkiri_canon::CoreArray<'_>,
        descriptor: bool,
    ) -> bool {
        let offset = usize::from(descriptor);
        if (descriptor && !step_matches(steps, 0, "shell-file", ""))
            || !step_matches(steps, offset, "shell-resolved-root", "")
            || !step_matches(steps, offset + 1, "join", selected)
        {
            return false;
        }
        let prefix = offset + 2;
        let mut index = steps.len();
        let mut frame = self.listing.as_deref();
        while let Some(listing) = frame {
            if index <= prefix {
                return false;
            }
            index -= 1;
            let current = listing.names[listing.next]
                .as_str()
                .expect("physical directory name");
            if !step_matches(steps, index, "join", current) {
                return false;
            }
            frame = listing.parent.as_deref();
        }
        index == prefix
    }

    pub(super) fn advance(&mut self) -> PackResolve {
        while let Some(mut listing) = self.listing.take() {
            listing.next += 1;
            if listing.next < listing.names.len() {
                self.listing = Some(listing);
                return PackResolve::ShellTreeSymlink;
            }
            self.listing = listing.parent.take();
        }
        PackResolve::ShellReady
    }

    // Fill a bounded stack with the verified directory identities from leaf
    // to root. The caller then walks it in reverse, closing parents promptly.
    pub(super) fn ancestors(&self) -> impl Iterator<Item = Identity> + '_ {
        std::iter::successors(self.listing.as_deref(), |frame| frame.parent.as_deref())
            .map(|frame| frame.identity)
    }

    pub(super) fn list<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        leaf: TrackedFile,
        path: &Path,
        identity: Identity,
        response: &mut CoreBufferBuilder,
        budget: &Arc<dyn AllocationBudget>,
    ) -> anyhow::Result<PackResolve> {
        let exhausted = || OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
        let capacity = op.reserve(OriginalAuthorityStage::Source, size_of::<Listing>())?;
        let mut directory = match leaf.into_directory() {
            Ok(directory) => directory,
            Err(_) if op.cleanup.uncertain.load(Ordering::Acquire) => {
                return Err(OriginalAuthorityError::unavailable().into());
            }
            Err(error) => return Err(raw_io(error, path.to_str()).into()),
        };
        let mut names = Vec::new();
        let listing = (|| -> anyhow::Result<()> {
            response.append(b"{\"kind\":\"shell-directory\",\"value\":[")?;
            loop {
                op.check(OriginalAuthorityStage::Source)?;
                let Some(name) = directory
                    .next_name()
                    .map_err(|error| raw_io(error, path.to_str()))?
                else {
                    break;
                };
                let name = name.to_str().map_err(|_| {
                    OriginalAuthorityError::unsupported(
                        OriginalAuthorityStage::Source,
                        "non-UTF-8 Shell directory member",
                    )
                })?;
                if !single_leaf(name) {
                    return Err(OriginalAuthorityError::unavailable().into());
                }
                // The returned array and fixed frame share the canonical
                // guest node budget. Leave room for the envelope.
                if names.len() == MAX_LISTING_NAMES {
                    return Err(exhausted().into());
                }
                charge_work(budget, name.len() as u64 + 1)?;
                if names.len() == names.capacity() {
                    let current = names.capacity();
                    let next = current.max(4).checked_mul(2).ok_or_else(exhausted)?;
                    let bytes = |count: usize| {
                        count
                            .checked_mul(size_of::<CoreValue>())
                            .and_then(|bytes| bytes.checked_add(size_of::<Listing>()))
                            .ok_or_else(exhausted)
                    };
                    capacity
                        .resize(bytes(current.checked_add(next).ok_or_else(exhausted)?)? as u64)
                        .map_err(|_| exhausted())?;
                    charge_work(budget, current as u64)?;
                    names
                        .try_reserve_exact(next - current)
                        .map_err(|_| exhausted())?;
                    if names.capacity() != next {
                        return Err(exhausted().into());
                    }
                    capacity
                        .resize(bytes(next)? as u64)
                        .map_err(|_| exhausted())?;
                }
                if !names.is_empty() {
                    response.append(b",")?;
                }
                crate::bootstrap_metadata::string_parts(response, [name])?;
                names.push(CoreValue::string(name, budget)?);
            }
            response.append(b"]}")?;
            Ok(())
        })();
        drop(directory);
        if op.cleanup.uncertain.load(Ordering::Acquire) {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        listing?;
        if names.is_empty() {
            return Ok(self.advance());
        }
        // UTF-8 preserves code point order for the accepted scalar names.
        let names_count = names.len() as u64;
        let sort_work =
            names_count * u64::from(names_count.ilog2() + 1) * (MAX_NAME as u64 + 1) * 4;
        charge_work(budget, sort_work)?;
        op.check(OriginalAuthorityStage::Source)?;
        names.sort_unstable_by(|left, right| left.as_str().cmp(&right.as_str()));
        op.check(OriginalAuthorityStage::Source)?;
        if names
            .windows(2)
            .any(|pair| pair[0].as_str() == pair[1].as_str())
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        self.listing = Some(Box::new(Listing {
            identity,
            names,
            next: 0,
            parent: self.listing.take(),
            _capacity: capacity,
        }));
        Ok(PackResolve::ShellTreeSymlink)
    }
}

pub(super) fn step_name(steps: &tobkiri_canon::CoreArray<'_>, index: usize) -> Option<CoreValue> {
    let step = steps.get(index)?;
    let parts = step.array()?;
    if parts.len() != 2 || parts.get(0)?.as_str() != Some("join") {
        return None;
    }
    parts.get(1)
}

fn step_matches(
    steps: &tobkiri_canon::CoreArray<'_>,
    index: usize,
    kind: &str,
    name: &str,
) -> bool {
    steps.get(index).and_then(|step| {
        let parts = step.array()?;
        Some(
            parts.len() == 2
                && parts.get(0)?.as_str() == Some(kind)
                && parts.get(1)?.as_str() == Some(name),
        )
    }) == Some(true)
}
