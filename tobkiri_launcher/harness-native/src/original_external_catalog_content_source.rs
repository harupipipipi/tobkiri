//! Exact admitted Pack roots for persisted Profile content projections.
use super::external_catalog_pinned::{bundled_contains, exact_position};
use super::*;
use tobkiri_canon::Digest;

const STAGE: OriginalAuthorityStage = OriginalAuthorityStage::Source;
const STALE: &str = "projection Pack identity is stale or not a Normal Pack";

/// An exact admitted root and its physical identity, funded until the last use.
pub(in crate::original_authority::io::publication) struct ProjectionPackSource {
    path: PathBuf,
    identity: (u64, u64),
    _capacity: NativeSetupReservation,
}

impl ProjectionPackSource {
    /// Borrow the selected physical Pack path.
    pub(in crate::original_authority::io::publication) fn root(&self) -> &Path {
        &self.path
    }

    /// Return the selected device and inode for subsequent descriptor checks.
    pub(in crate::original_authority::io::publication) fn identity(&self) -> (u64, u64) {
        self.identity
    }

    fn from_external(root: PackRoot) -> Self {
        Self {
            path: root.path,
            identity: root.physical_identity,
            _capacity: root._capacity,
        }
    }
}

/// Resolve an exact pin from the verified retained catalog, or the generated
/// catalog's bundled root when no external entry matches.
pub(in crate::original_authority::io::publication) fn resolve_projection_pack<
    S: OriginalAuthorityOwner,
>(
    op: &Operation<'_, S>,
    runtime: &Path,
    user_data: &Path,
    id: &str,
    digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<ProjectionPackSource> {
    let CatalogInputs {
        _bundled: bundled,
        registry,
        _paths,
    } = read_inputs(op, runtime, user_data, budget)?;
    let in_bundled = bundled_contains(op, budget, &bundled, id)?;
    drop(bundled);

    if let Some((registry, mut inventory)) = authenticate_registry(op, registry, budget)? {
        if inventory.host_extension_pending {
            return Err(OriginalAuthorityError::unsupported(
                STAGE,
                "external Host Extension compatibility projection",
            ));
        }
        let (selected, retained_for_id) =
            exact_position(op, budget, &registry, &inventory, id, digest)?;
        if selected.is_none() && retained_for_id && !in_bundled {
            return Err(model::denied("selected Pack revision is not admitted"));
        }
        inventory.finish_revision(&registry, op, budget)?;
        if let Some(position) = selected {
            let pack = inventory._entries.value.swap_remove(position);
            return Ok(ProjectionPackSource::from_external(pack._root));
        }
    }
    if !in_bundled {
        return Err(raw(
            BOUNDARY_ERROR,
            format!("Pack is absent from the Host v4 catalog: {id}"),
        ));
    }
    bundled_source(op, runtime, id, digest, budget)
}

fn bundled_source<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    runtime: &Path,
    id: &str,
    digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<ProjectionPackSource> {
    op.check(STAGE)?;
    let _capacity = op.reserve(STAGE, 8 * (runtime.as_os_str().len() + id.len() + 512))?;
    // The original takes the runtime from the resolved module filename.
    // Below that owner-selected root, open only the generated catalog's ID.
    let runtime = fs::canonicalize(runtime).map_err(|error| raw_io(error, runtime.to_str()))?;
    let ecosystem = runtime.join("ecosystem");
    let path = ecosystem.join(id);
    let is_symlink = |path: &Path| path.symlink_metadata().is_ok_and(|meta| meta.is_symlink());
    if is_symlink(&ecosystem) {
        return Err(raw(
            BOUNDARY_ERROR,
            "ecosystem boundary must not be a symlink",
        ));
    }
    if is_symlink(&path) {
        return Err(raw(
            BOUNDARY_ERROR,
            format!("cataloged Pack root must not be a symlink: {id}"),
        ));
    }
    let directory = Parent::walk(&path, &op.cleanup).map_err(|_| {
        raw(
            BOUNDARY_ERROR,
            format!("cataloged Pack root is missing: {id}"),
        )
    })?;
    let admission = (|| {
        let meta = directory
            .metadata()
            .map_err(|error| raw_io(error, path.to_str()))?;
        let identity = (meta.dev(), meta.ino());
        let (_, actual, _) =
            compile_artifact(op, budget, &directory, &path).map_err(direct_compile_error)?;
        if &actual != digest {
            return Err(raw(
                BOUNDARY_ERROR,
                "selected bundled Pack revision is unavailable",
            ));
        }
        let source = ProjectionPackSource {
            path,
            identity,
            _capacity,
        };
        check_current_root(op, &source)?;
        Ok(source)
    })();
    finish_scope(
        admission,
        directory.close().map_err(|error| raw_io(error, None)),
    )
}

/// Recompile the admitted artifact, then reopen and validate its manifest
/// before passing it to the descriptor-based subtree verifier.
pub(in crate::original_authority::io::publication) fn with_projection_manifest<
    S: OriginalAuthorityOwner,
    T,
>(
    op: &Operation<'_, S>,
    source: &ProjectionPackSource,
    id: &str,
    digest: &Digest,
    budget: &Arc<dyn AllocationBudget>,
    visit: impl FnOnce(&Document) -> Result<T>,
) -> Result<T> {
    let directory = open_current_root(op, source)?;
    let result = (|| {
        let (compiled, actual_digest, variants) =
            compile_artifact(op, budget, &directory, source.root())
                .map_err(direct_compile_error)?;
        let compiled_matches =
            &actual_digest == digest && matches_manifest(&compiled.manifest, id, digest);
        drop((compiled, variants));
        let bytes =
            read_artifact_definition(op, &directory, source.root(), c"pack.v4.json", budget)
                .map_err(direct_compile_error)?;
        let manifest = compiler::read_manifest(&bytes, op, budget).map_err(direct_compile_error)?;
        drop(bytes);
        if !compiled_matches || !matches_manifest(manifest.document(), id, digest) {
            return Err(value(STALE));
        }
        let result = visit(manifest.document())?;
        drop(manifest);
        check_current_root(op, source)?;
        Ok(result)
    })();
    finish_scope(
        result,
        directory.close().map_err(|error| raw_io(error, None)),
    )
}

fn matches_manifest(doc: &Document, id: &str, digest: &Digest) -> bool {
    let pack = doc.get(doc.root, "pack").expect("validated Pack manifest");
    registry_text(doc, pack, "id").is(id)
        && registry_text(doc, pack, "kind").is("normal_sandbox")
        && registry_text(doc, pack, "artifact_digest").is(digest.as_str())
}

fn open_current_root<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    source: &ProjectionPackSource,
) -> Result<TrackedFile> {
    op.check(STAGE)?;
    let path = source.root();
    let directory = Parent::walk(path, &op.cleanup).map_err(|error| {
        if path
            .ancestors()
            .any(|part| part.symlink_metadata().is_ok_and(|meta| meta.is_symlink()))
        {
            value("projection Pack root contains a symlink")
        } else if let Some(OriginalAuthorityCause::Io(io)) = error.cause {
            raw_root_io(io)
        } else {
            error
        }
    })?;
    let identity = directory
        .metadata()
        .map(|meta| (meta.dev(), meta.ino()))
        .map_err(|error| raw_io(error, path.to_str()));
    match identity {
        Ok(identity) if identity == source.identity() => Ok(directory),
        result => {
            let result = result.and_then(|_| Err(value("projection Pack root identity changed")));
            finish_scope(
                result,
                directory.close().map_err(|error| raw_io(error, None)),
            )
        }
    }
}

fn check_current_root<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    source: &ProjectionPackSource,
) -> Result<()> {
    open_current_root(op, source)?
        .close()
        .map_err(|error| raw_io(error, None))
}

// These callers use the same compiler and descriptor reader outside the CAS
// admission wrapper. Preserve the original exception at that read boundary.
fn direct_compile_error(mut error: OriginalAuthorityError) -> OriginalAuthorityError {
    if error.class == EXTERNAL_ERROR
        && error.message == "external Pack artifact digest verification failed"
        && error.context.is_none()
    {
        match error.cause.take() {
            Some(OriginalAuthorityCause::Io(io)) => return raw_root_io(io),
            Some(OriginalAuthorityCause::Python {
                class,
                message,
                cause,
                context: None,
                suppressed,
            }) => {
                return OriginalAuthorityError {
                    stage: STAGE,
                    class,
                    message,
                    cause: cause.map(|cause| *cause),
                    context: None,
                    suppressed,
                };
            }
            cause => error.cause = cause,
        }
    }
    error
}

fn raw_root_io(io: OriginalAuthorityIoError) -> OriginalAuthorityError {
    let mut error = raw(RAW_IO, "");
    error.cause = Some(OriginalAuthorityCause::Io(io));
    error
}

#[cfg(test)]
mod tests {
    use super::super::super::definitions::tests::{operation, with_source};
    use super::*;
    use serde_json::{Value, json};
    use std::os::unix::fs::symlink;

    fn generated_catalog(runtime: &Path, id: &str) {
        let path = runtime.join(SOURCE_CATALOG);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(
            path,
            serde_json::to_vec(&json!({
                "catalog_api_version": SOURCE_VERSION,
                "pack_ids": [id],
                "packs": [{"pack_id": id, "dependencies": {}}],
            }))
            .unwrap(),
        )
        .unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn bundled_projection_origin_requires_catalog_pin_and_physical_continuity() {
        with_source(|owner, publication, cancellation, budget| {
            let op = operation(publication, owner, cancellation);
            let runtime = owner.root.join("projection-runtime");
            let fixture = Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../tobkiri_runtime/ecosystem/rumi_mcp_gateway_pack");
            let manifest: Value =
                serde_json::from_slice(&fs::read(fixture.join("pack.v4.json")).unwrap()).unwrap();
            let id = manifest["pack"]["id"].as_str().unwrap();
            let digest =
                Digest::parse(manifest["pack"]["artifact_digest"].as_str().unwrap()).unwrap();
            let root = runtime.join("ecosystem").join(id);
            fs::create_dir_all(root.join("runtime")).unwrap();
            for name in [
                "pack.v4.json",
                "contracts.v4.json",
                "artifact-index.v4.json",
                "executables.v4.json",
                "runtime/gateway.py",
            ] {
                fs::copy(fixture.join(name), root.join(name)).unwrap();
            }
            let resolve = |digest: &Digest| {
                resolve_projection_pack(&op, &runtime, &owner.root, id, digest, budget)
            };
            generated_catalog(&runtime, "qa_unrelated");
            let error = resolve(&digest).err().unwrap();
            assert_eq!(error.class, BOUNDARY_ERROR);
            assert_eq!(
                error.message,
                format!("Pack is absent from the Host v4 catalog: {id}")
            );

            generated_catalog(&runtime, id);
            let error = resolve(&Digest::hash(b"other revision")).err().unwrap();
            assert_eq!(error.class, BOUNDARY_ERROR);
            assert_eq!(
                error.message,
                "selected bundled Pack revision is unavailable"
            );
            let source = resolve(&digest).unwrap();
            assert_eq!(source.root(), root.canonicalize().unwrap());
            with_projection_manifest(&op, &source, id, &digest, budget, |manifest| {
                assert!(matches_manifest(manifest, id, &digest));
                Ok(())
            })
            .unwrap();

            let implementation = root.join("runtime/gateway.py");
            fs::write(&implementation, b"changed implementation").unwrap();
            let error =
                with_projection_manifest::<_, ()>(&op, &source, id, &digest, budget, |_| {
                    panic!("a changed executable must not deliver a manifest")
                })
                .unwrap_err();
            assert_eq!(error.class, "tobkiri_host.errors.InvalidArtifactError");
            assert_eq!(error.message, "executable implementation digest mismatch");
            fs::copy(fixture.join("runtime/gateway.py"), implementation).unwrap();

            let old_root = root.with_file_name("original-pack");
            fs::rename(&root, &old_root).unwrap();
            fs::create_dir(&root).unwrap();
            let error =
                with_projection_manifest::<_, ()>(&op, &source, id, &digest, budget, |_| {
                    panic!("a replaced root must not deliver a manifest")
                })
                .unwrap_err();
            assert_eq!(error.message, "projection Pack root identity changed");
            fs::remove_dir(&root).unwrap();
            symlink(&old_root, &root).unwrap();
            let error =
                with_projection_manifest::<_, ()>(&op, &source, id, &digest, budget, |_| {
                    panic!("a symlinked root must not deliver a manifest")
                })
                .unwrap_err();
            assert_eq!(error.message, "projection Pack root contains a symlink");
            assert_eq!(publication.receipt().open_files, 0);
        });
    }

    #[tokio::test(flavor = "current_thread")]
    async fn projection_origin_uses_exact_signed_revision_and_never_bypasses_catalog_checks() {
        let Some(fixture) = std::env::var_os("TOBKIRI_EXTERNAL_CATALOG_FIXTURE") else {
            return;
        };
        let bytes = fs::read(fixture).unwrap();
        assert!(bytes.len() <= 2 * 1024 * 1024);
        let fixture: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(
            fixture["format"],
            "io.tobkiri.original-external-pack-catalog-reference.v1"
        );
        let cases = fixture["cases"].as_array().unwrap();
        let case = |name| cases.iter().find(|case| case["name"] == name).unwrap();
        let retained = case("retained-version");
        let owner = Path::new(retained["native_user_data"].as_str().unwrap());
        let revisions = retained["retained_probe"]["ordered"].as_array().unwrap();
        assert_eq!(revisions.len(), 2);
        let id = revisions[0]["key"].as_str().unwrap();
        with_source(|source, publication, cancellation, budget| {
            let op = operation(publication, source, cancellation);
            let runtime = source.root.join("projection-runtime");
            let resolve = |owner: &Path, digest: &Digest| {
                resolve_projection_pack(&op, &runtime, owner, id, digest, budget)
            };
            let original_digest =
                Digest::parse(revisions[0]["artifact_digest"].as_str().unwrap()).unwrap();
            let error = resolve(owner, &original_digest).err().unwrap();
            assert_eq!(error.class, BOUNDARY_ERROR);
            assert!(
                error
                    .message
                    .starts_with("canonical Pack v4 catalog is unavailable:")
            );

            let bundled_id = "rumi_mcp_gateway_pack";
            generated_catalog(&runtime, bundled_id);
            for revision in revisions {
                let digest = Digest::parse(revision["artifact_digest"].as_str().unwrap()).unwrap();
                let key = revision["key"].as_str().unwrap();
                let token = retained["expected"]["value"]["entries"][key]["store_token"]
                    .as_str()
                    .unwrap();
                let expected = owner.join("pack_control").join(CAS).join(token);
                let source = resolve(owner, &digest).unwrap();
                assert_eq!(source.root(), expected.canonicalize().unwrap());
                with_projection_manifest(&op, &source, id, &digest, budget, |manifest| {
                    assert!(matches_manifest(manifest, id, &digest));
                    Ok(())
                })
                .unwrap();
            }
            let bad_owner = Path::new(case("malformed-hmac")["native_user_data"].as_str().unwrap());
            // Even an explicitly cataloged bundled ID must not bypass a bad HMAC.
            let error = resolve_projection_pack(
                &op,
                &runtime,
                bad_owner,
                bundled_id,
                &original_digest,
                budget,
            )
            .err()
            .unwrap();
            assert_eq!(error.class, EXTERNAL_ERROR);
            assert_eq!(
                error.message,
                "external Normal Pack catalog authentication failed"
            );
            assert_eq!(publication.receipt().open_files, 0);
        });
    }
}
