//! Shared immutable artifact indexes. Runtime consumers read one funded
//! artifact at a time instead of cloning every Pack payload into memory.
use crate::{
    ManifestError,
    source::{SourceCatalog, StagedArtifact},
};
use std::{collections::BTreeMap, ops::Deref};
use tobkiri_canon::ownership::LeasedShared;

struct Index {
    entries: BTreeMap<String, StagedArtifact>,
    catalog: SourceCatalog,
}

/// Immutable verified-file index; clones retain the same physical storage.
#[derive(Clone, Debug)]
pub struct ArtifactFiles(LeasedShared<Index, SourceCatalog>);
impl ArtifactFiles {
    /// Snapshot caller-owned developer inputs before manifest verification.
    /// This grants no signature, inventory, Profile pin or execution authority.
    pub fn capture_for_verification(
        files: &BTreeMap<String, Vec<u8>>,
        catalog: &SourceCatalog,
    ) -> Result<Self, ManifestError> {
        let mut staging = catalog.begin_snapshot()?;
        let mut ranges = BTreeMap::new();
        for (path, bytes) in files {
            catalog.prepay_metadata(1024 + path.len() as u64)?;
            ranges.insert(
                path.clone(),
                staging.append(&mut bytes.as_slice(), bytes.len() as u64)?,
            );
        }
        let snapshot = staging.finish()?;
        let entries = ranges
            .into_iter()
            .map(|(path, range)| Ok((path, snapshot.artifact(&range)?)))
            .collect::<Result<_, ManifestError>>()?;
        Self::new(entries, catalog)
    }
    /// Empty native metadata index; this carries no admitted payload.
    pub fn empty(catalog: &SourceCatalog) -> Result<Self, ManifestError> {
        Self::new(BTreeMap::new(), catalog)
    }
    pub(crate) fn new(
        entries: BTreeMap<String, StagedArtifact>,
        catalog: &SourceCatalog,
    ) -> Result<Self, ManifestError> {
        catalog.prepay_metadata(LeasedShared::<Index, SourceCatalog>::storage_bytes() as u64)?;
        Ok(Self(
            LeasedShared::try_new_precharged(
                Index {
                    entries,
                    catalog: catalog.clone(),
                },
                catalog.clone(),
            )
            .map_err(|error| ManifestError::Invalid(error.to_string()))?,
        ))
    }

    pub fn catalog(&self) -> &SourceCatalog {
        &self.0.get().catalog
    }

    /// Trusted developer packaging helper, never a Core runtime read. The
    /// returned builder input is caller-owned and is not a funded source cache.
    pub fn materialize_for_pack_build(&self) -> Result<BTreeMap<String, Vec<u8>>, ManifestError> {
        self.iter()
            .map(|(path, artifact)| Ok((path.clone(), artifact.read()?.to_vec())))
            .collect()
    }
}
impl Deref for ArtifactFiles {
    type Target = BTreeMap<String, StagedArtifact>;
    fn deref(&self) -> &Self::Target {
        &self.0.get().entries
    }
}
