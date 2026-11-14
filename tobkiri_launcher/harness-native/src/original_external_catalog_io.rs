//! Original Host admission catalog read, rooted in an authenticated review.
use super::*;
use std::os::unix::fs::OpenOptionsExt;
use tobkiri_canon::{AllocationBudget, AllocationKind, AllocationRequest, CoreValue, MAX_BYTES};

// The generated catalog describes packaged Pack IDs. It is distinct from the
// selected bundle catalog and from user-installed Pack manifests.
const SOURCE_CATALOG: &str = "schemas/pack_v4_catalog.v1.json";
const SOURCE_VERSION: &str = "io.tobkiri.pack-source-catalog.v1";
const REGISTRY: &str = "external_normal_pack_catalog.v4.json";
const REGISTRY_MAX_BYTES: usize = 16 * 1024 * 1024;
const BOUNDARY_ERROR: &str = "core_runtime.pack_boundary.PackBoundaryError";
const EXTERNAL_ERROR: &str = "core_runtime.external_pack_catalog_v4.ExternalPackCatalogDenied";

/// Funded records for one actual bundled inventory read.
pub(crate) struct BundledRecords {
    _catalog: Funded<Document>,
    _ids: CoreValue,
}

/// The physical registry input, or the absence that the original read uses to
/// return an empty catalog. This result does not confer Pack authority.
pub(crate) struct RegistryInput {
    _bundled: BundledRecords,
    raw: Option<Funded<Zeroizing<Vec<u8>>>>,
}

impl RegistryInput {
    pub(crate) fn has_saved_registry(&self) -> bool {
        self.raw.is_some()
    }
}

#[cfg(test)]
mod tests {
    use super::super::definitions::tests::{operation, with_source};
    use super::*;
    use serde_json::{Value, json};
    use std::os::unix::fs::symlink;

    fn stage(runtime: &Path, manifest: &Value) -> PathBuf {
        let path = runtime.join(SOURCE_CATALOG);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, serde_json::to_vec(manifest).unwrap()).unwrap();
        path
    }

    fn manifest() -> Value {
        json!({
            "catalog_api_version": SOURCE_VERSION,
            "pack_ids": ["first", "second"],
            "packs": [
                {"pack_id": "second", "dependencies": {"first": {}}},
                {"pack_id": "first", "dependencies": {}},
            ],
        })
    }

    #[tokio::test(flavor = "current_thread")]
    async fn external_catalog_reads_bundled_first_and_leaves_absent_owner_unchanged() {
        with_source(|source, read, cancellation, budget| {
            let runtime = source.root.join("test-runtime");
            let op = operation(read, source, cancellation);
            let path = stage(&runtime, &manifest());
            let catalog = read_inputs(&op, &runtime, &source.root, budget).unwrap();
            assert!(!catalog.has_saved_registry());
            assert_eq!(
                catalog
                    ._bundled
                    ._ids
                    .get_index(0)
                    .as_ref()
                    .and_then(CoreValue::as_str),
                Some("first")
            );
            assert_eq!(
                catalog
                    ._bundled
                    ._ids
                    .get_index(1)
                    .as_ref()
                    .and_then(CoreValue::as_str),
                Some("second")
            );
            assert!(!source.root.join("pack_control").exists());
            assert_eq!(read.receipt().open_files, 0);
            fs::remove_file(&path).unwrap();
            let error = read_inputs(&op, &runtime, &source.root, budget)
                .err()
                .unwrap();
            assert_eq!(error.class, BOUNDARY_ERROR);
            assert!(
                error
                    .message
                    .starts_with("canonical Pack v4 catalog is unavailable")
            );
            assert!(!source.root.join("pack_control").exists());
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn external_catalog_reads_one_regular_file_and_rejects_aliases() {
        with_source(|source, read, cancellation, budget| {
            let runtime = source.root.join("test-runtime");
            let op = operation(read, source, cancellation);
            stage(&runtime, &manifest());
            let parent = source.root.join("pack_control");
            fs::create_dir(&parent).unwrap();
            let path = parent.join(REGISTRY);
            let sample = br#"{"signature":"not-yet-validated"}"#;
            fs::write(&path, sample).unwrap();
            let catalog = read_inputs(&op, &runtime, &source.root, budget).unwrap();
            assert_eq!(catalog.raw.as_ref().unwrap().as_slice(), sample);
            assert_eq!(fs::read(&path).unwrap(), sample);
            assert!(!parent.join(".external_normal_pack_catalog_key").exists());
            let second_name = parent.join("second-file");
            fs::hard_link(&path, &second_name).unwrap();
            let error = read_inputs(&op, &runtime, &source.root, budget)
                .err()
                .unwrap();
            assert_eq!(error.class, EXTERNAL_ERROR);
            assert_eq!(error.message, "Host JSON file identity is invalid");
            fs::remove_file(&path).unwrap();
            symlink(&second_name, &path).unwrap();
            let error = read_inputs(&op, &runtime, &source.root, budget)
                .err()
                .unwrap();
            assert_eq!(error.class, EXTERNAL_ERROR);
            assert_eq!(
                error.message,
                "Host JSON file is unreadable: external_normal_pack_catalog.v4.json"
            );
            assert_eq!(read.receipt().open_files, 0);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn external_catalog_matches_the_pinned_bundled_ids_when_supplied() {
        let Some(path) = std::env::var_os("TOBKIRI_EXTERNAL_CATALOG_FIXTURE") else {
            return;
        };
        let bytes = fs::read(path).unwrap();
        assert!(bytes.len() <= 2 * 1024 * 1024);
        let fixture: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            fixture["format"],
            "io.tobkiri.original-external-pack-catalog-reference.v1"
        );
        let cases = fixture["cases"].as_array().unwrap();
        assert_eq!(cases.len(), 3);
        for case in cases {
            with_source(|source, read, cancellation, budget| {
                let op = operation(read, source, cancellation);
                let runtime = Path::new(case["runtime_dir"].as_str().unwrap());
                let owner = Path::new(case["native_user_data"].as_str().unwrap());
                let catalog = read_inputs(&op, runtime, owner, budget).unwrap();
                let expected = case["bundled"]["ids"].as_array().unwrap();
                let actual = catalog._bundled._ids.array().unwrap();
                assert_eq!(actual.len(), expected.len());
                for (actual, expected) in actual.iter().zip(expected) {
                    assert_eq!(actual.as_str(), expected.as_str());
                }
                assert_eq!(
                    catalog.has_saved_registry(),
                    case["name"].as_str() != Some("absent")
                );
                assert_eq!(read.receipt().open_files, 0);
            });
        }
    }
}

pub(crate) fn read<S: crate::BootstrapReviewOwner>(
    publication: &PublicationRead,
    owner: &S,
    cancellation: &CoreCancellation,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<RegistryInput> {
    publication.check_continuation_thread()?;
    let pending = publication.pending.as_ref().expect("held recovery");
    let op = PublicationRead::operation(
        &publication.invocation,
        &publication.cleanup,
        owner,
        cancellation,
    )?;
    owner
        .with_original_catalog_source(|source| {
            read_inputs(&op, source.runtime, &pending.root, budget)
        })?
        .map_err(recovery_error)
}

fn read_inputs<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    user_data: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<RegistryInput> {
    // Original runtime_user_data_scope resolves this path before reading the
    // generated Pack catalog. Keep using the original selected Host owner.
    let _paths = op.reserve(
        OriginalAuthorityStage::Source,
        12 * (runtime.as_os_str().len() + user_data.as_os_str().len() + 512),
    )?;
    op.check(OriginalAuthorityStage::Source)?;
    let user_data = resolve_existing_prefix(op, user_data)?;
    let source_path = runtime.join(SOURCE_CATALOG);
    let bundled = read_bundled(op, &source_path, budget)?;
    let registry = user_data.join("pack_control").join(REGISTRY);
    op.check(OriginalAuthorityStage::Source)?;
    let raw = registry
        .exists()
        .then(|| read_registry(op, &registry, budget))
        .transpose()?;
    Ok(RegistryInput {
        _bundled: bundled,
        raw,
    })
}

fn resolve_existing_prefix<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &Path,
) -> Result<PathBuf> {
    let mut prefix = path;
    loop {
        op.check(OriginalAuthorityStage::Source)?;
        match fs::canonicalize(prefix) {
            Ok(resolved) => {
                return Ok(resolved.join(path.strip_prefix(prefix).expect("root prefix")));
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) =>
            {
                prefix = prefix
                    .parent()
                    .ok_or_else(|| raw_io(error, prefix.to_str()))?;
            }
            Err(error) => return Err(raw_io(error, prefix.to_str())),
        }
    }
}

fn read_bundled<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<BundledRecords> {
    let failed = |error| {
        caused(
            raw(
                BOUNDARY_ERROR,
                format!(
                    "canonical Pack v4 catalog is unavailable: {}",
                    path.display()
                ),
            ),
            error,
        )
    };
    op.check(OriginalAuthorityStage::Source)?;
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .map_err(|error| failed(raw_io(error, path.to_str())))?;
    let mut file = TrackedFile::new(file, op.cleanup.clone());
    let input = (|| {
        let length = file.metadata().map_err(|error| raw_io(error, None))?;
        if length.is_dir() {
            return Err(raw_io(
                std::io::Error::from_raw_os_error(libc::EISDIR),
                path.to_str(),
            ));
        }
        if !length.is_file() || length.len() > MAX_BYTES as u64 {
            return Err(OriginalAuthorityError::capacity(
                OriginalAuthorityStage::Source,
            ));
        }
        publication_io::read_stream_limited(op, &mut file, 8192, Some(MAX_BYTES))
    })();
    let close = file.close().map_err(|error| raw_io(error, None));
    let input = finish_scope(input, close).map_err(|error| {
        if error.class == RAW_IO {
            failed(error)
        } else {
            error
        }
    })?;
    // Path.read_text uses universal newlines. JSON whitespace outside a
    // string is equivalent; the byte codec retains its own bounded errors.
    let catalog = json::decode_catalog_manifest(&input, op, budget).map_err(|error| {
        if matches!(
            error.cause.as_ref(),
            Some(OriginalAuthorityCause::Python {
                class: "json.decoder.JSONDecodeError" | "builtins.UnicodeDecodeError",
                ..
            })
        ) {
            failed(error)
        } else {
            error
        }
    })?;
    drop(input);
    let ids = bundled_ids(op, &catalog, budget)?;
    Ok(BundledRecords {
        _catalog: catalog,
        _ids: ids,
    })
}

fn bundled_ids<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    catalog: &Document,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue> {
    let fail = |message: &'static str| raw(BOUNDARY_ERROR, message);
    if catalog.entries(catalog.root).is_none() {
        return Err(fail("canonical Pack v4 catalog is not an object"));
    }
    if !catalog
        .get(catalog.root, "catalog_api_version")
        .is_some_and(|id| catalog.string_is(id, SOURCE_VERSION))
    {
        return Err(fail("canonical Pack v4 catalog version is invalid"));
    }
    let Some(Node::Array(records)) = catalog
        .get(catalog.root, "packs")
        .map(|id| catalog.node(id))
    else {
        return Err(fail("canonical Pack v4 catalog has no packs list"));
    };
    let _record_lease = op.reserve(
        OriginalAuthorityStage::Source,
        records
            .len()
            .saturating_mul(size_of::<(CoreValue, usize)>() * 2),
    )?;
    let mut packed = Vec::new();
    packed.try_reserve_exact(records.len()).map_err(capacity)?;
    if packed.capacity() != records.len() {
        return Err(capacity(()));
    }
    for &record in records {
        let Some(fields) = catalog.entries(record) else {
            return Err(fail(
                "canonical Pack v4 catalog contains a malformed record",
            ));
        };
        charge(op, budget, fields.len().saturating_mul(8))?;
        let id = catalog.get(record, "pack_id").map(|id| catalog.node(id));
        let id = match id {
            Some(Node::String(text)) => trim_id(text),
            None | Some(Node::Scalar(tobkiri_canon::CanonValue::Null)) => &[],
            _ => {
                return Err(OriginalAuthorityError::unsupported(
                    OriginalAuthorityStage::Source,
                    "legacy bundled Pack ID string conversion",
                ));
            }
        };
        charge(op, budget, id.len())?;
        let id = bounded_id(id).ok_or_else(|| raw(BOUNDARY_ERROR, invalid_pack_id_message(id)))?;
        let id_value = CoreValue::string(id.as_str(), budget).map_err(capacity)?;
        charge(op, budget, packed.len().saturating_mul(64))?;
        if packed
            .iter()
            .any(|(existing, _): &(CoreValue, usize)| existing.as_str() == Some(id.as_str()))
        {
            return Err(raw(
                BOUNDARY_ERROR,
                format!(
                    "canonical Pack v4 catalog has invalid Pack ID: '{}'",
                    id.as_str()
                ),
            ));
        }
        packed.push((id_value, record));
    }
    let declared = match catalog
        .get(catalog.root, "pack_ids")
        .map(|id| catalog.node(id))
    {
        Some(Node::Array(ids)) if ids.len() == packed.len() => ids,
        _ => return Err(fail("canonical Pack v4 catalog Pack IDs are inconsistent")),
    };
    charge(
        op,
        budget,
        packed.len().saturating_mul(packed.len()).saturating_mul(64),
    )?;
    let mut ids = CoreValue::array_builder(budget, packed.len()).map_err(capacity)?;
    let mut sorted = Vec::new();
    sorted.try_reserve_exact(packed.len()).map_err(capacity)?;
    if sorted.capacity() != packed.len() {
        return Err(capacity(()));
    }
    sorted.extend(packed.iter().map(|(id, _)| id));
    sorted.sort_unstable_by(|left, right| left.as_str().cmp(&right.as_str()));
    for (&declared, id) in declared.iter().zip(&sorted) {
        if !catalog.string_is(declared, id.as_str().expect("bounded Pack ID")) {
            return Err(fail("canonical Pack v4 catalog Pack IDs are inconsistent"));
        }
        ids.push(id.share()).map_err(capacity)?;
    }
    for (id, record) in &packed {
        charge(
            op,
            budget,
            catalog
                .entries(*record)
                .expect("record")
                .len()
                .saturating_mul(16),
        )?;
        let dependencies = catalog
            .get(*record, "dependencies")
            .and_then(|id| catalog.entries(id))
            .ok_or_else(|| {
                raw(
                    BOUNDARY_ERROR,
                    format!(
                        "Pack {} has malformed v4 dependencies",
                        id.as_str().expect("ID")
                    ),
                )
            })?;
        for (dependency, _) in dependencies {
            charge(
                op,
                budget,
                dependency.0.len() + packed.len().saturating_mul(64),
            )?;
            let valid = bounded_id(trim_id(dependency)).is_some_and(|dep| {
                sorted
                    .binary_search_by(|id| id.as_str().expect("Pack ID").cmp(dep.as_str()))
                    .is_ok()
            });
            if !valid {
                return Err(raw(
                    BOUNDARY_ERROR,
                    format!(
                        "Pack {} declares an unknown v4 dependency",
                        id.as_str().expect("ID")
                    ),
                ));
            }
        }
    }
    ids.finish().map_err(capacity)
}

fn trim_id(text: &Text) -> &[u32] {
    let is_space = |point| {
        char::from_u32(point)
            .is_some_and(|ch| python_space(ch) || matches!(ch, '\u{1c}'..='\u{1f}'))
    };
    let points = &text.0;
    let Some(first) = points.iter().position(|point| !is_space(*point)) else {
        return &[];
    };
    let end = points
        .iter()
        .rposition(|point| !is_space(*point))
        .expect("nonempty");
    &points[first..=end]
}

struct PackId {
    bytes: [u8; 64],
    len: usize,
}
impl PackId {
    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[..self.len]).expect("Pack ID was validated")
    }
}
fn bounded_id(id: &[u32]) -> Option<PackId> {
    let mut out = PackId {
        bytes: [0; 64],
        len: id.len(),
    };
    if !(1..=out.bytes.len()).contains(&id.len())
        || id.iter().any(|point| {
            u8::try_from(*point)
                .ok()
                .is_none_or(|byte| !byte.is_ascii_alphanumeric() && byte != b'_' && byte != b'-')
        })
    {
        return None;
    }
    for (slot, point) in out.bytes.iter_mut().zip(id) {
        *slot = *point as u8;
    }
    Some(out)
}

fn invalid_pack_id_message(id: &[u32]) -> String {
    if id.len() > 128 {
        return "canonical Pack v4 catalog has an overlength Pack ID".into();
    }
    let repr = json::repr_text(&Text(id.to_vec()));
    format!("canonical Pack v4 catalog has invalid Pack ID: {repr}")
}

fn read_registry<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    path: &Path,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Funded<Zeroizing<Vec<u8>>>> {
    let external_error = |message| raw(EXTERNAL_ERROR, message);
    op.check(OriginalAuthorityStage::Source)?;
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|error| {
            caused(
                external_error(
                    "Host JSON file is unreadable: external_normal_pack_catalog.v4.json",
                ),
                raw_io(error, path.to_str()),
            )
        })?;
    let mut file = TrackedFile::new(file, op.cleanup.clone());
    let input = (|| {
        let before = file.metadata().map_err(|error| raw_io(error, None))?;
        if !before.is_file() || before.nlink() != 1 || before.len() > REGISTRY_MAX_BYTES as u64 {
            return Err(external_error("Host JSON file identity is invalid"));
        }
        // Preserve the original bounded single os.read occurrence.
        let max = REGISTRY_MAX_BYTES + 1;
        let funding = op.reserve(OriginalAuthorityStage::Source, max)?;
        let mut raw = Zeroizing::new(Vec::new());
        raw.try_reserve_exact(max).map_err(capacity)?;
        if raw.capacity() != max {
            return Err(capacity(()));
        }
        charge(op, budget, max)?;
        raw.resize(max, 0);
        let size = file.read(&mut raw).map_err(|error| raw_io(error, None))?;
        let after = file.metadata().map_err(|error| raw_io(error, None))?;
        if size > REGISTRY_MAX_BYTES || after.nlink() != 1 || identity(&before) != identity(&after)
        {
            return Err(external_error("Host JSON file changed while reading"));
        }
        raw.truncate(size);
        // The original retains only the returned bytes.
        let funding = shrink_raw(op, &mut raw, funding)?;
        Ok(Funded {
            value: raw,
            _capacity: funding,
        })
    })();
    let close = file.close().map_err(|error| raw_io(error, None));
    finish_scope(input, close)
}

fn shrink_raw<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    raw: &mut Zeroizing<Vec<u8>>,
    funding: NativeSetupReservation,
) -> Result<NativeSetupReservation> {
    let length = raw.len();
    let held = op.reserve(OriginalAuthorityStage::Source, length.max(1))?;
    let mut smaller = Zeroizing::new(Vec::new());
    smaller.try_reserve_exact(length).map_err(capacity)?;
    if smaller.capacity() != length {
        return Err(capacity(()));
    }
    smaller.extend_from_slice(raw);
    drop(std::mem::replace(raw, smaller));
    drop(funding);
    Ok(held)
}

fn identity(metadata: &fs::Metadata) -> (u64, u64, u64, i64, i64) {
    (
        metadata.dev(),
        metadata.ino(),
        metadata.len(),
        metadata.mtime(),
        metadata.mtime_nsec(),
    )
}

fn charge<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    work: usize,
) -> Result<()> {
    op.check(OriginalAuthorityStage::Source)?;
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            depth: 0,
            elements: 0,
            work: work as u64,
        })
        .map_err(capacity)
}

fn capacity(_: impl Sized) -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}
