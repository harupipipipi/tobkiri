//! Private artifact snapshots and funded, temporary source reads.
//!
//! These objects are storage, not verified Packs or Gate authority. A loader
//! must still check the original manifest, signature, inventory and Profile pin.
//! A catalog shares its caller's CoreControl account; accepted file sizes never
//! create a separate resident-memory allowance.

use crate::ManifestError;
use sha2::{Digest as _, Sha256};
use std::{
    fs::{File, Metadata},
    io::{self, Read, Seek, SeekFrom, Write},
    ops::Deref,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::SystemTime,
};
use tobkiri_canon::{
    Digest,
    ownership::{ControlHandle, LeasedShared},
};
use tobkiri_gate::{ControlKind, ControlReservation, CoreControl};

const MAX_STAGED_BYTES: u64 = 512 * 1024 * 1024;
const READ_SCRATCH: u64 = 64 * 1024;
static NEXT_SOURCE: AtomicU64 = AtomicU64::new(1);

fn invalid(message: impl Into<String>) -> ManifestError {
    ManifestError::Invalid(message.into())
}
fn capacity(error: impl std::fmt::Display) -> ManifestError {
    invalid(format!("source capacity: {error}"))
}

struct CatalogUsage {
    retained: ControlReservation,
    staged_bytes: u64,
}
struct CatalogState {
    control: CoreControl,
    usage: Mutex<CatalogUsage>,
}

/// One source-loading lifetime, including all retained snapshots and indexes.
/// Cloning retains the same prepaid block and account, with no new allocation.
#[derive(Clone, Debug)]
pub struct SourceCatalog(LeasedShared<CatalogState, ControlReservation>);
impl SourceCatalog {
    /// Prepay both the shared block and its growable retained-storage record.
    /// Both category slots and all bytes belong to the supplied account.
    pub fn new(control: &CoreControl) -> Result<Self, ManifestError> {
        let block = control
            .reserve(
                ControlKind::Catalog,
                LeasedShared::<CatalogState, ControlReservation>::storage_bytes() as u64,
            )
            .map_err(capacity)?;
        let retained = control.reserve(ControlKind::Catalog, 1).map_err(capacity)?;
        Ok(Self(
            LeasedShared::try_new_precharged(
                CatalogState {
                    control: control.clone(),
                    usage: Mutex::new(CatalogUsage {
                        retained,
                        staged_bytes: 0,
                    }),
                },
                block,
            )
            .map_err(capacity)?,
        ))
    }

    /// Charge before constructing retained index/path/manifest metadata.
    /// Such storage must retain this catalog until its physical destruction.
    /// No partial refund or account handover is exposed.
    pub fn prepay_metadata(&self, bytes: u64) -> Result<(), ManifestError> {
        self.0
            .get()
            .usage
            .lock()
            .map_err(|_| invalid("source accounting poisoned"))?
            .retained
            .try_grow(bytes)
            .map_err(capacity)
    }

    fn claim_storage(&self, bytes: u64) -> Result<(), ManifestError> {
        let mut usage = self
            .0
            .get()
            .usage
            .lock()
            .map_err(|_| invalid("source accounting poisoned"))?;
        let next = usage
            .staged_bytes
            .checked_add(bytes)
            .filter(|next| *next <= MAX_STAGED_BYTES)
            .ok_or_else(|| invalid("private source staging exceeds 512 MiB"))?;
        // Conservative lifetime accounting includes failed/partial writes.
        // It cannot be refunded while an uncertain file write still exists.
        usage.staged_bytes = next;
        Ok(())
    }

    fn reserve_read(&self, bytes: u64) -> Result<ControlReservation, ManifestError> {
        self.0
            .get()
            .control
            .reserve(ControlKind::Load, bytes)
            .map_err(capacity)
    }

    /// Create an anonymous private file, never a path that later gets reopened.
    /// One builder can hold many artifact ranges in the same descriptor.
    pub fn begin_snapshot(&self) -> Result<SourceStaging, ManifestError> {
        self.prepay_metadata(
            LeasedShared::<Snapshot, SourceCatalog>::storage_bytes() as u64 + 4096,
        )?;
        let scratch = self.reserve_read(READ_SCRATCH)?;
        let id = NEXT_SOURCE
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |id| id.checked_add(1))
            .map_err(|_| invalid("source identity exhausted"))?;
        let file = tempfile::tempfile()?;
        Ok(SourceStaging {
            file,
            catalog: self.clone(),
            id,
            length: 0,
            poisoned: false,
            _scratch: scratch,
        })
    }
}

/// A descriptor-bound range captured from the actual input, not a claimed hash.
/// It cannot be constructed by a caller or attached to another snapshot.
#[derive(Clone, Debug)]
pub struct SourceRange {
    source: u64,
    offset: u64,
    length: u64,
    digest: Digest,
    _catalog: SourceCatalog,
}

/// Append-only private staging. An error poisons the complete snapshot.
/// It exposes neither a path nor a writable descriptor to subsequent readers.
pub struct SourceStaging {
    file: File,
    catalog: SourceCatalog,
    id: u64,
    length: u64,
    poisoned: bool,
    _scratch: ControlReservation,
}
impl SourceStaging {
    /// Capture one forward-only input with a fixed-size copy/hash buffer.
    /// Interrupted reads retry; any other read/write failure prevents sealing.
    pub fn append(
        &mut self,
        input: &mut impl Read,
        maximum: u64,
    ) -> Result<SourceRange, ManifestError> {
        if self.poisoned || maximum > MAX_STAGED_BYTES {
            self.poisoned = true;
            return Err(invalid("invalid or failed source staging"));
        }
        let result = self.append_inner(input, maximum);
        if result.is_err() {
            self.poisoned = true;
        }
        result
    }

    fn append_inner(
        &mut self,
        input: &mut impl Read,
        maximum: u64,
    ) -> Result<SourceRange, ManifestError> {
        self.catalog.prepay_metadata(512)?;
        let offset = self.length;
        let mut length = 0;
        let mut hash = Sha256::new();
        let mut buffer = [0u8; 8192];
        loop {
            let available = (maximum - length)
                .saturating_add(1)
                .min(buffer.len() as u64);
            let count = match input.read(&mut buffer[..available as usize]) {
                Ok(count) => count,
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error.into()),
            };
            if count == 0 {
                return Ok(SourceRange {
                    source: self.id,
                    offset,
                    length,
                    digest: Digest::from_sha256(hash.finalize().into()),
                    _catalog: self.catalog.clone(),
                });
            }
            if count as u64 > maximum - length {
                return Err(invalid("source exceeds its declared read bound"));
            }
            self.catalog.claim_storage(count as u64)?;
            self.file.write_all(&buffer[..count])?;
            hash.update(&buffer[..count]);
            length += count as u64;
            self.length += count as u64;
        }
    }

    /// Close the writable API and record the actual completed file identity.
    pub fn finish(self) -> Result<SourceSnapshot, ManifestError> {
        if self.poisoned {
            return Err(invalid("failed source staging cannot be sealed"));
        }
        let identity = FileIdentity::read(&self.file.metadata()?)?;
        if identity.length != self.length {
            return Err(invalid("staged source length changed"));
        }
        Ok(SourceSnapshot(
            LeasedShared::try_new_precharged(
                Snapshot {
                    file: Mutex::new(self.file),
                    identity,
                    id: self.id,
                    catalog: self.catalog.clone(),
                },
                self.catalog,
            )
            .map_err(capacity)?,
        ))
    }
}

#[derive(PartialEq, Eq)]
struct FileIdentity {
    length: u64,
    modified: SystemTime,
    readonly: bool,
    #[cfg(unix)]
    physical: (u64, u64, u32, i64, i64, i64, i64),
}
impl FileIdentity {
    fn read(metadata: &Metadata) -> io::Result<Self> {
        #[cfg(unix)]
        use std::os::unix::fs::MetadataExt;
        Ok(Self {
            length: metadata.len(),
            modified: metadata.modified()?,
            readonly: metadata.permissions().readonly(),
            #[cfg(unix)]
            physical: (
                metadata.dev(),
                metadata.ino(),
                metadata.mode(),
                metadata.mtime(),
                metadata.mtime_nsec(),
                metadata.ctime(),
                metadata.ctime_nsec(),
            ),
        })
    }
}
struct Snapshot {
    file: Mutex<File>,
    identity: FileIdentity,
    id: u64,
    catalog: SourceCatalog,
}
impl Snapshot {
    fn stable(&self) -> Result<(), ManifestError> {
        let file = self
            .file
            .lock()
            .map_err(|_| invalid("private source descriptor poisoned"))?;
        if FileIdentity::read(&file.metadata()?)? != self.identity {
            return Err(invalid("private source changed after staging"));
        }
        Ok(())
    }
}

/// Immutable private snapshot. Every alias retains the original funded block.
#[derive(Clone, Debug)]
pub struct SourceSnapshot(LeasedShared<Snapshot, SourceCatalog>);
impl SourceSnapshot {
    /// Bind only a range captured by this exact descriptor's builder.
    pub fn artifact(&self, range: &SourceRange) -> Result<StagedArtifact, ManifestError> {
        if self.0.get().id != range.source
            || range
                .offset
                .checked_add(range.length)
                .is_none_or(|end| end > self.0.get().identity.length)
        {
            return Err(invalid("range belongs to another source snapshot"));
        }
        self.0.get().stable()?;
        Ok(StagedArtifact {
            snapshot: self.clone(),
            range: range.clone(),
        })
    }
}

/// Bytes are read only from private immutable staging, never the original path.
/// This still does not establish manifest/signature/Profile admission.
#[derive(Clone, Debug)]
pub struct StagedArtifact {
    snapshot: SourceSnapshot,
    range: SourceRange,
}
impl StagedArtifact {
    pub fn len(&self) -> u64 {
        self.range.length
    }
    pub fn is_empty(&self) -> bool {
        self.range.length == 0
    }
    pub fn digest(&self) -> &Digest {
        &self.range.digest
    }

    /// Inspect without materializing the payload. The callback must separately
    /// prepay any allocations it retains. Its result is usable only on success.
    pub fn inspect<T>(
        &self,
        inspect: impl FnOnce(&mut dyn Read) -> Result<T, ManifestError>,
    ) -> Result<T, ManifestError> {
        let _scratch = self.catalog().reserve_read(READ_SCRATCH)?;
        self.inspect_inner(inspect)
    }

    fn catalog(&self) -> &SourceCatalog {
        &self.snapshot.0.get().catalog
    }

    fn inspect_inner<T>(
        &self,
        inspect: impl FnOnce(&mut dyn Read) -> Result<T, ManifestError>,
    ) -> Result<T, ManifestError> {
        let snapshot = self.snapshot.0.get();
        snapshot.stable()?;
        let mut reader = RangeReader {
            snapshot,
            range: &self.range,
            position: 0,
            hash: Sha256::new(),
        };
        let result = inspect(&mut reader)?;
        io::copy(&mut reader, &mut io::sink())?;
        if reader.position != self.range.length
            || Digest::from_sha256(reader.hash.finalize().into()) != self.range.digest
        {
            return Err(invalid("private source digest/length changed"));
        }
        snapshot.stable()?;
        Ok(result)
    }

    /// Prepay the owned buffer and its control block BEFORE allocation. Last
    /// clone physically frees both before returning the same Load reservation.
    pub fn read(&self) -> Result<SourceBytes, ManifestError> {
        let length = usize::try_from(self.len()).map_err(capacity)?;
        let charge = self
            .len()
            .checked_add(READ_SCRATCH)
            .and_then(|bytes| {
                bytes.checked_add(
                    ControlHandle::<Vec<u8>>::storage_bytes::<ControlReservation>() as u64,
                )
            })
            .ok_or_else(|| invalid("source read allocation overflow"))?;
        let reservation = self.catalog().reserve_read(charge)?;
        let bytes = self.inspect_inner(|reader| {
            let mut bytes = Vec::new();
            bytes.try_reserve_exact(length).map_err(capacity)?;
            let mut buffer = [0u8; 8192];
            loop {
                let count = match reader.read(&mut buffer) {
                    Ok(count) => count,
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    Err(error) => return Err(error.into()),
                };
                if count == 0 {
                    return Ok(bytes);
                }
                bytes.extend_from_slice(&buffer[..count]);
            }
        })?;
        Ok(SourceBytes(
            ControlHandle::try_new_precharged(bytes, reservation).map_err(capacity)?,
        ))
    }
}

struct RangeReader<'a> {
    snapshot: &'a Snapshot,
    range: &'a SourceRange,
    position: u64,
    hash: Sha256,
}
impl Read for RangeReader<'_> {
    fn read(&mut self, target: &mut [u8]) -> io::Result<usize> {
        let limit = (self.range.length - self.position).min(target.len() as u64) as usize;
        if limit == 0 {
            return Ok(0);
        }
        let count = {
            let mut file = self
                .snapshot
                .file
                .lock()
                .map_err(|_| io::Error::other("private source descriptor poisoned"))?;
            file.seek(SeekFrom::Start(self.range.offset + self.position))?;
            file.read(&mut target[..limit])?
        };
        self.position += count as u64;
        self.hash.update(&target[..count]);
        Ok(count)
    }
}

/// Immutable owned bytes whose original Load charge survives every clone.
#[derive(Clone, Debug)]
pub struct SourceBytes(ControlHandle<Vec<u8>>);
impl Deref for SourceBytes {
    type Target = [u8];
    fn deref(&self) -> &Self::Target {
        self.0.as_ref()
    }
}
impl AsRef<[u8]> for SourceBytes {
    fn as_ref(&self) -> &[u8] {
        self
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use tobkiri_gate::CoreControlLimits;

    fn captured(control: &CoreControl, bytes: &[u8]) -> StagedArtifact {
        let catalog = SourceCatalog::new(control).unwrap();
        let mut staging = catalog.begin_snapshot().unwrap();
        let mut input = bytes;
        let range = staging.append(&mut input, bytes.len() as u64).unwrap();
        staging.finish().unwrap().artifact(&range).unwrap()
    }

    #[test]
    fn exact_bytes_and_hash_survive_original_source_and_catalog_drop() {
        let control = CoreControl::default();
        let mut original = b"original artifact".to_vec();
        let artifact = captured(&control, &original);
        original.fill(b'x');
        drop(original);
        assert_eq!(artifact.digest(), &Digest::hash(b"original artifact"));
        assert_eq!(artifact.read().unwrap().as_ref(), b"original artifact");
        drop(artifact);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Catalog), 0);
    }

    #[test]
    fn ranges_share_one_descriptor_without_sharing_reader_position() {
        let control = CoreControl::default();
        let catalog = SourceCatalog::new(&control).unwrap();
        let mut staging = catalog.begin_snapshot().unwrap();
        let first = staging.append(&mut &b"first"[..], 5).unwrap();
        let second = staging.append(&mut &b"second"[..], 6).unwrap();
        let snapshot = staging.finish().unwrap();
        let first = snapshot.artifact(&first).unwrap();
        let second = snapshot.artifact(&second).unwrap();
        first
            .inspect(|reader| {
                let mut prefix = [0u8; 2];
                reader.read_exact(&mut prefix)?;
                assert_eq!(&prefix, b"fi");
                assert_eq!(second.read()?.as_ref(), b"second");
                let mut suffix = [0u8; 3];
                reader.read_exact(&mut suffix)?;
                assert_eq!(&suffix, b"rst");
                Ok(())
            })
            .unwrap();
    }

    #[test]
    fn a_range_cannot_be_substituted_into_another_snapshot() {
        let control = CoreControl::default();
        let catalog = SourceCatalog::new(&control).unwrap();
        let mut first = catalog.begin_snapshot().unwrap();
        let range = first.append(&mut &b"same"[..], 4).unwrap();
        let _first = first.finish().unwrap();
        let mut second = catalog.begin_snapshot().unwrap();
        second.append(&mut &b"same"[..], 4).unwrap();
        assert!(second.finish().unwrap().artifact(&range).is_err());
    }

    #[test]
    fn excess_or_failed_input_poison_the_complete_snapshot() {
        let control = CoreControl::default();
        let catalog = SourceCatalog::new(&control).unwrap();
        let mut staging = catalog.begin_snapshot().unwrap();
        assert!(staging.append(&mut &b"too long"[..], 3).is_err());
        assert!(staging.finish().is_err());
        struct Failed;
        impl Read for Failed {
            fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
                Err(io::Error::other("physical source failure"))
            }
        }
        let mut staging = catalog.begin_snapshot().unwrap();
        assert!(staging.append(&mut Failed, 10).is_err());
        assert!(staging.finish().is_err());
    }

    #[test]
    fn owned_bytes_hold_the_original_charge_through_the_final_clone() {
        let control = CoreControl::default();
        let artifact = captured(&control, b"retained");
        let bytes = artifact.read().unwrap();
        let copy = bytes.clone();
        let charged = control.usage().bytes;
        drop(bytes);
        assert_eq!(control.usage().bytes, charged);
        drop(artifact);
        assert_eq!(control.usage().count(ControlKind::Catalog), 0);
        assert_eq!(control.usage().count(ControlKind::Load), 1);
        assert_eq!(copy.as_ref(), b"retained");
        drop(copy);
        assert_eq!(control.usage().bytes, 0);
        assert_eq!(control.usage().count(ControlKind::Load), 0);
    }

    #[test]
    fn failed_read_admission_never_runs_the_consumer() {
        let control = CoreControl::new(CoreControlLimits {
            total_bytes: 128 * 1024,
            ..CoreControlLimits::default()
        })
        .unwrap();
        let artifact = captured(&control, b"data");
        let usage = control.usage().bytes;
        let held = control
            .reserve(ControlKind::Task, 128 * 1024 - usage - READ_SCRATCH + 1)
            .unwrap();
        let called = Cell::new(false);
        assert!(
            artifact
                .inspect(|_| {
                    called.set(true);
                    Ok(())
                })
                .is_err()
        );
        assert!(!called.get());
        assert!(artifact.read().is_err());
        assert_eq!(control.usage().count(ControlKind::Load), 0);
        drop(held);
        assert_eq!(artifact.read().unwrap().as_ref(), b"data");
    }

    #[test]
    fn same_inode_modification_is_rejected_even_after_an_early_consumer() {
        let control = CoreControl::default();
        let artifact = captured(&control, b"original");
        assert!(
            artifact
                .inspect(|reader| {
                    let mut first = [0u8; 1];
                    reader.read_exact(&mut first)?;
                    let mut file = artifact.snapshot.0.get().file.lock().unwrap();
                    file.seek(SeekFrom::Start(0))?;
                    file.write_all(b"modified")?;
                    Ok(first)
                })
                .is_err()
        );
    }

    #[test]
    fn a_range_retains_its_metadata_account_after_the_snapshot_is_gone() {
        let control = CoreControl::default();
        let catalog = SourceCatalog::new(&control).unwrap();
        let mut staging = catalog.begin_snapshot().unwrap();
        let range = staging.append(&mut &b"x"[..], 1).unwrap();
        drop(staging.finish().unwrap());
        drop(catalog);
        assert!(control.usage().bytes > 0);
        assert_eq!(control.usage().count(ControlKind::Catalog), 2);
        drop(range);
        assert_eq!(control.usage().bytes, 0);
    }
}
