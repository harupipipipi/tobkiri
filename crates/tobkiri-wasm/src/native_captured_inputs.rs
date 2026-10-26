//! Closed ordinal DATA over already frozen native backing, never OS paths.
use anyhow::{Result, bail};
use tobkiri_canon::{AllocationError, AllocationKind, AllocationRequest, CoreBuffer, LeasedShared};
use tobkiri_gate::{ControlKind, ControlReservation, NativeSetupInvocation};
use tobkiri_store::NativeFileSnapshot;

const MAX_FILES: usize = 4096;
const MAX_METADATA: usize = 1024 * 1024;
const MAX_READ: usize = 64 * 1024;
const BASE_METADATA: usize = 4096;

/// Native-selected virtual name for one same-invocation frozen file. The name
/// is presentation DATA, never a path that the callback opens on the host.
pub struct NativeCapturedFile<'a> {
    pub path: &'a str,
    pub snapshot: &'a NativeFileSnapshot,
}
struct File {
    path: String,
    snapshot: NativeFileSnapshot,
}
struct Inputs {
    files: Vec<File>,
    owners: crate::AllocationOwners,
    invocation: NativeSetupInvocation,
}
/// Immutable native-only closed inventory. No API replaces entries or chooses
/// another capture/descriptor. Clones retain metadata, FD and original funding.
#[derive(Clone)]
pub struct NativeCapturedInputs(LeasedShared<Inputs, ControlReservation>);

fn charge(owners: &crate::AllocationOwners, work: u64) -> Result<()> {
    owners.graph().reserve(AllocationRequest {
        kind: AllocationKind::Temporary,
        bytes: 0,
        elements: 0,
        depth: 0,
        work,
    })?;
    Ok(())
}

// Preserve the original Python _namespace_retention calculation and rejection
// rules, including UTF-8 byte counts and conservative repeated ancestors.
fn namespace_cost(path: &str) -> Result<usize> {
    if path.is_empty() || path.contains(['\\', '\0']) {
        bail!("invalid captured input name");
    }
    let mut retained = path
        .len()
        .checked_add(256)
        .filter(|n| *n <= MAX_METADATA)
        .ok_or_else(|| anyhow::anyhow!("captured input metadata exceeds its original bound"))?;
    let mut prefix = 0usize;
    for (index, part) in path.split('/').enumerate() {
        if matches!(part, "" | "." | "..") {
            bail!("captured input name escapes its namespace");
        }
        prefix = prefix
            .checked_add(part.len())
            .and_then(|n| n.checked_add(1))
            .ok_or_else(|| anyhow::anyhow!("captured input metadata overflow"))?;
        retained = (index + 1)
            .checked_mul(8)
            .and_then(|n| n.checked_add(prefix))
            .and_then(|n| n.checked_add(256))
            .and_then(|n| n.checked_add(retained))
            .filter(|n| *n <= MAX_METADATA)
            .ok_or_else(|| anyhow::anyhow!("captured input metadata exceeds its original bound"))?;
    }
    Ok(retained)
}

fn no_collisions<'a>(order: &[usize], name: impl Fn(usize) -> &'a str) -> Result<()> {
    if order.windows(2).any(|pair| name(pair[0]) == name(pair[1])) {
        bail!("captured input names collide");
    }
    for &index in order {
        let path = name(index);
        for (offset, _) in path.match_indices('/') {
            let parent = &path[..offset];
            if order
                .binary_search_by(|other| name(*other).cmp(parent))
                .is_ok()
            {
                bail!("captured input file collides with a directory");
            }
        }
    }
    Ok(())
}

impl NativeCapturedInputs {
    pub(crate) fn seal(
        invocation: &NativeSetupInvocation,
        owners: &crate::AllocationOwners,
        source: &[NativeCapturedFile<'_>],
    ) -> Result<Self> {
        charge(owners, 1)?;
        if source.len() > MAX_FILES {
            bail!("captured input file count exceeds its original bound");
        }
        let mut metadata = 0usize;
        for file in source {
            charge(owners, file.path.len() as u64 + 1)?;
            if !file.snapshot.belongs_to(invocation)? {
                bail!("captured file belongs to another invocation");
            }
            metadata = metadata
                .checked_add(namespace_cost(file.path)?)
                .filter(|n| *n <= MAX_METADATA)
                .ok_or_else(|| {
                    anyhow::anyhow!("captured input metadata exceeds its original bound")
                })?;
        }
        // Covers bounded comparison/prefix work without an unmetered sort
        // comparator, including long shared prefixes in the original namespace.
        charge(
            owners,
            (metadata as u64)
                .checked_mul(64)
                .ok_or(AllocationError::Overflow)?,
        )?;
        let retained = metadata
            .checked_add(BASE_METADATA)
            .ok_or(AllocationError::Overflow)?;
        if LeasedShared::<Inputs, ControlReservation>::storage_bytes() > BASE_METADATA {
            bail!("captured inventory metadata layout is unsupported");
        }
        let control = invocation
            .control()
            .reserve(ControlKind::Frame, retained as u64)?;
        let mut files = Vec::new();
        let mut order = Vec::new();
        files.try_reserve_exact(source.len())?;
        order.try_reserve_exact(source.len())?;
        if files.capacity() != source.len() || order.capacity() != source.len() {
            bail!("captured inventory allocator capacity is unsupported");
        }
        for (index, file) in source.iter().enumerate() {
            charge(owners, 1)?;
            let mut path = String::new();
            path.try_reserve_exact(file.path.len())?;
            if path.capacity() != file.path.len() {
                bail!("captured name allocator capacity is unsupported");
            }
            path.push_str(file.path);
            files.push(File {
                path,
                snapshot: file.snapshot.clone(),
            });
            order.push(index);
        }
        order.sort_unstable_by(|a, b| files[*a].path.cmp(&files[*b].path));
        no_collisions(&order, |index| files[index].path.as_str())?;
        drop(order);
        charge(owners, 1)?;
        LeasedShared::try_new_precharged(
            Inputs {
                files,
                owners: owners.clone(),
                invocation: invocation.clone(),
            },
            control,
        )
        .map(Self)
        .map_err(Into::into)
    }

    /// Original order/size DATA for constructing the unchanged guest inventory.
    pub fn descriptors(&self) -> impl ExactSizeIterator<Item = (&str, u64)> {
        self.0
            .get()
            .files
            .iter()
            .map(|file| (file.path.as_str(), file.snapshot.len()))
    }
    pub fn len(&self) -> usize {
        self.0.get().files.len()
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub(crate) fn belongs_to(&self, invocation: &NativeSetupInvocation) -> Result<bool> {
        charge(&self.0.get().owners, 1)?;
        Ok(self.0.get().invocation.same_invocation(invocation))
    }

    /// A separately prepaid immutable copy from the fixed backing. The caller
    /// runs synchronous native IO in a funded managed worker, retaining this
    /// capture/output until actual completion even when its receiver cancels.
    pub fn read_range(&self, ordinal: u32, offset: u64, length: u32) -> Result<CoreBuffer> {
        let inputs = self.0.get();
        charge(&inputs.owners, 1)?;
        let file = inputs
            .files
            .get(ordinal as usize)
            .ok_or_else(|| anyhow::anyhow!("captured input ordinal is outside its inventory"))?;
        let length = length as usize;
        if length > MAX_READ
            || offset
                .checked_add(length as u64)
                .is_none_or(|end| end > file.snapshot.len())
        {
            bail!("captured input read exceeds its fixed range");
        }
        let mut read_error = None;
        let output = CoreBuffer::write_exact(length, inputs.owners.graph(), |out| {
            file.snapshot.read_exact_at(offset, out).map_err(|error| {
                read_error = Some(error);
                AllocationError::Unavailable
            })
        });
        if let Some(error) = read_error {
            return Err(error.into());
        }
        Ok(output?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn original_namespace_retention_counts_utf8_and_repeated_ancestors() {
        assert_eq!(namespace_cost("a").unwrap(), 523);
        assert_eq!(namespace_cost("a/b").unwrap(), 801);
        assert_eq!(namespace_cost("é/x").unwrap(), 804);
        assert!(namespace_cost("urn:asset").is_ok());
    }
    #[test]
    fn original_namespace_refuses_escape_and_excess_retention() {
        for name in ["", "/a", "a/", "a//b", "a/./b", "a/../b", "a\\b", "a\0b"] {
            assert!(namespace_cost(name).is_err(), "{name:?}");
        }
        assert!(namespace_cost(&"a".repeat(MAX_METADATA)).is_err());
    }
    #[test]
    fn file_directory_collisions_include_nonadjacent_lexical_prefixes() {
        let names = ["a", "a-else", "a/child"];
        assert!(no_collisions(&[0, 1, 2], |n| names[n]).is_err());
        assert!(no_collisions(&[0, 1], |_| "duplicate").is_err());
        let names = ["dir/a", "dir/b", "directory"];
        assert!(no_collisions(&[0, 1, 2], |n| names[n]).is_ok());
    }
}
