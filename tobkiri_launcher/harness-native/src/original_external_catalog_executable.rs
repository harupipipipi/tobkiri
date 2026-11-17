//! Independent reads in the original admitted executable catalog loader.
use super::external_catalog_pinned::{self, PinnedPackSelection};
use super::external_catalog_projection::{self, NormalProjection};
use super::*;
use std::ffi::CStr;
use tobkiri_canon::Digest;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const VERIFICATION_FAILED: &str = "external Pack executable catalog verification failed";
const MANIFEST_MISMATCH: &str =
    "external Pack executable catalog does not match the admitted manifest";

pub(super) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    user_data: &Path,
    budget: &Arc<dyn AllocationBudget>,
    id: &str,
    expected_manifest: &CoreValue,
    digest: &str,
) -> Result<ClosureExecutable> {
    if expected_manifest
        .get_key("pack")
        .and_then(|pack| pack.get_key("artifact_digest"))
        .is_none_or(|artifact_digest| artifact_digest.as_str() != Some(digest))
    {
        return Err(model::denied(MANIFEST_MISMATCH));
    }
    let digest = Digest::parse(digest).map_err(|_| model::denied(MANIFEST_MISMATCH))?;
    let PinnedPackSelection::Normal { registry, pack } =
        external_catalog_pinned::read(op, runtime, user_data, budget, id, &digest)?
    else {
        return Ok(ClosureExecutable::HostExtension);
    };
    let root = &pack._root;
    let (manifest, executable, compiled_id_matches, compiled_digest) =
        read_and_project(op, budget, &registry, root, id).map_err(executable_failure)?;

    // Open the admitted path again and verify a new set of definitions,
    // implementations, signed inventory and receipt against the pinned entry.
    let reverified = read_artifact_definitions(op, root, &registry, budget)?;
    if reverified.receipt.host_extension {
        return Ok(ClosureExecutable::HostExtension);
    }
    drop(reverified);

    let manifest = manifest.document();
    let executable = executable.document();
    let field =
        |doc: &Document, parent, key| doc.get(parent, key).expect("checked executable document");
    let pack_row = field(manifest, manifest.root, "pack");
    let integrity = field(manifest, manifest.root, "integrity");
    let canonical_manifest = canonical_digest(manifest, manifest.root, None, op, budget)?;
    let expected_digest = expected_manifest.digest_guest(budget).map_err(capacity)?;
    let admission_digest = &pack._artifact_digest;
    let manifest_source = registry_text(manifest, integrity, "source_identity");
    let executable_source = registry_text(executable, executable.root, "source_identity");
    charge(
        op,
        budget,
        manifest_source
            .0
            .len()
            .min(executable_source.0.len())
            .saturating_add(1),
    )?;
    if canonical_manifest != expected_digest
        || !manifest.string_is(field(manifest, pack_row, "id"), id)
        || !manifest.string_is(
            field(manifest, pack_row, "artifact_digest"),
            admission_digest.as_str(),
        )
        || !compiled_id_matches
        || &compiled_digest != admission_digest
        || !executable.string_is(field(executable, executable.root, "pack_id"), id)
        || executable_source != manifest_source
    {
        return Err(model::denied(MANIFEST_MISMATCH));
    }
    let bytes = json::canonical_subtree(executable, executable.root, None, op, budget)?;
    let executable = CoreValue::parse_guest(&bytes, budget).map_err(capacity)?;
    Ok(ClosureExecutable::External(executable))
}

fn read_and_project<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    registry: &Document,
    root: &PackRoot,
    id: &str,
) -> Result<(
    compiler::PackManifest,
    compiler::ExecutableCatalog,
    bool,
    Digest,
)> {
    op.check(STAGE)?;
    let directory = Parent::walk(&root.path, &op.cleanup)?;
    let result = (|| {
        let opened = directory.metadata().map_err(|error| raw_io(error, None))?;
        if (opened.dev(), opened.ino()) != root.physical_identity {
            return Err(model::denied("external Pack root identity changed"));
        }
        let read = |name| read_artifact_definition(op, &directory, &root.path, name, budget);
        let manifest = compiler::read_manifest(&read(c"pack.v4.json")?, op, budget)?;
        let executable = compiler::read_executable(&read(c"executables.v4.json")?, op, budget)?;
        compiler::check_external_catalog_identity(&executable, &manifest, op, budget)?;

        let (documents, compiled_digest, verified) =
            compile_artifact(op, budget, &directory, &root.path)?;
        let compiled = &documents.manifest;
        let pack = compiled.get(compiled.root, "pack").expect("validated Pack");
        charge(op, budget, id.len().saturating_add(1))?;
        let compiled_id_matches = registry_text(compiled, pack, "id").is(id);
        drop((documents, verified));

        let signed = {
            let raw = read_signed_manifest(op, budget, &directory, &root.path)?;
            model::decode_host_json(&raw, op, budget)?
        };
        project(op, budget, read, &signed, registry, root.entry)?;
        Ok((manifest, executable, compiled_id_matches, compiled_digest))
    })();
    finish_scope(
        result,
        directory.close().map_err(|error| raw_io(error, None)),
    )
}

fn project<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    budget: &Arc<dyn AllocationBudget>,
    read: impl Fn(&'static CStr) -> Result<Funded<Zeroizing<Vec<u8>>>>,
    signed: &Document,
    registry: &Document,
    entry: usize,
) -> Result<()> {
    let manifest = compiler::read_manifest(&read(c"pack.v4.json")?, op, budget)?;
    let contracts = compiler::read_contracts(&read(c"contracts.v4.json")?, op, budget)?;
    let physical_digest = {
        let raw = read(c"contracts.v4.json")?;
        charge(op, budget, raw.len())?;
        Digest::hash(&raw)
    };
    external_catalog_projection::check_contracts(
        op,
        budget,
        manifest.document(),
        &contracts,
        &physical_digest,
    )?;
    drop(contracts);
    let executable = compiler::read_executable(&read(c"executables.v4.json")?, op, budget)?;
    external_catalog_projection::verify(
        op,
        budget,
        NormalProjection {
            manifest: manifest.document(),
            executable: executable.document(),
            signed,
        },
        registry,
        entry,
    )
}

fn executable_failure(mut error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == "tobkiri.native.OriginalAuthorityIntegrationFailure"
        || matches!(
            error.cause.as_ref(),
            Some(OriginalAuthorityCause::Cancelled | OriginalAuthorityCause::Deadline)
        )
    {
        return error;
    }
    // The shared compiler already records the original Python cause for a
    // CAS verification. Here the same exception has the executable wrapper.
    if error.class == EXTERNAL_ERROR
        && error.message == "external Pack artifact digest verification failed"
        && error.cause.is_some()
    {
        error.message = VERIFICATION_FAILED.into();
        return error;
    }
    caused(model::denied(VERIFICATION_FAILED), error)
}
