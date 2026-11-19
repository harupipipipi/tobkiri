//! Verify legacy Registry routes against the accepted v4 Bundle.

use super::super::{BundleEntryKind, VerifiedBundleLock, MAX_CANONICAL_JSON_BYTES};
use super::NativeApplicationSources;
use anyhow::{bail, ensure, Context, Result};
use std::{ffi::OsStr, os::unix::ffi::OsStrExt, path::Path, sync::Arc};
use tobkiri_core::{
    read_relative_with, AllocationBudget, AllocationError, CanonError, CoreBuffer, CoreValue,
    Digest,
};
use tobkiri_harness_native::{
    OriginalConversationReadSelection, OriginalModelResolveRole, OriginalModelResolveSelection,
    OriginalModelSaveSelection, OriginalModelSettingsSelection, OriginalProviderPrepareSelection,
    OriginalRoutePin,
};

/// The exact Bundle entries to reread, captured while the resolver source is borrowed.
pub(crate) struct LockedRegistryBundle<'pin> {
    root: CoreBuffer,
    provider_prepare: Option<[LockedRegistryRoute<'pin>; 2]>,
    model_list: Option<LockedRegistryRoute<'pin>>,
    model_save: Option<[LockedRegistryRoute<'pin>; 2]>,
    model_resolve: [Option<LockedRegistryRoute<'pin>>; 4],
    conversation_read: [Option<LockedRegistryRoute<'pin>>; 3],
    budget: Arc<dyn AllocationBudget>,
}

impl NativeApplicationSources {
    /// Capture Bundle-relative file identities from the original verified lock.
    /// File reads happen after the Core source borrow has ended.
    pub(crate) fn registry_bundle<'pin>(
        &self,
        provider: Option<&'pin OriginalProviderPrepareSelection<'_>>,
        model: Option<&'pin OriginalModelSettingsSelection<'_>>,
        model_save: Option<&'pin OriginalModelSaveSelection<'_>>,
        model_resolve: Option<&'pin OriginalModelResolveSelection<'_>>,
        conversation_read: Option<&'pin OriginalConversationReadSelection<'_>>,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<LockedRegistryBundle<'pin>> {
        let provider_prepare = provider
            .map(|selection| {
                ensure!(
                    selection.coordinator().caller_function_id()
                        == self.authority.shell_provider_id,
                    "old Provider prepare coordinator differs from the selected Shell"
                );
                Ok::<_, anyhow::Error>([
                    LockedRegistryRoute::from_lock(
                        &self.bundle_lock,
                        selection.coordinator(),
                        budget,
                    )?,
                    LockedRegistryRoute::from_lock(
                        &self.bundle_lock,
                        selection.provider(),
                        budget,
                    )?,
                ])
            })
            .transpose()?;
        let model_list = model
            .map(|selection| {
                ensure!(
                    selection.model_list().caller_function_id() == self.authority.shell_provider_id,
                    "old Model list caller differs from the selected Shell"
                );
                LockedRegistryRoute::from_lock(&self.bundle_lock, selection.model_list(), budget)
            })
            .transpose()?;
        let model_save = model_save
            .map(|selection| {
                let model_manage = selection.model_manage();
                let provider_read = selection.provider_read();
                ensure!(
                    model_manage.caller_function_id() == self.authority.shell_provider_id,
                    "old Model manage caller differs from the selected Shell"
                );
                ensure!(
                    provider_read.caller_function_id() == model_manage.function_id(),
                    "old Model save Provider read caller differs from Model manage"
                );
                Ok::<_, anyhow::Error>([
                    LockedRegistryRoute::from_lock(&self.bundle_lock, model_manage, budget)?,
                    LockedRegistryRoute::from_lock(&self.bundle_lock, provider_read, budget)?,
                ])
            })
            .transpose()?;
        let mut model_resolve_routes = [None, None, None, None];
        if let Some(selection) = model_resolve {
            use OriginalModelResolveRole::{Generate, QuoteGenerate, QuoteStream, Stream};
            for (locked, role) in
                model_resolve_routes
                    .iter_mut()
                    .zip([QuoteGenerate, QuoteStream, Generate, Stream])
            {
                *locked = selection
                    .pin(role)
                    .map(|pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget))
                    .transpose()?;
            }
        }
        let mut conversation_routes = [None, None, None];
        if let Some(selection) = conversation_read {
            let shell = selection.shell();
            if let Some(shell) = shell {
                ensure!(
                    shell.caller_function_id() == self.authority.shell_provider_id,
                    "old Conversation read caller differs from the selected Shell"
                );
            }
            for (locked, pin) in conversation_routes.iter_mut().zip([
                shell,
                selection.saved_turn(),
                selection.saved_orchestration(),
            ]) {
                *locked = pin
                    .map(|pin| LockedRegistryRoute::from_lock(&self.bundle_lock, pin, budget))
                    .transpose()?;
            }
        }
        Ok(LockedRegistryBundle {
            root: CoreBuffer::copy_from(self.bundle_root.as_os_str().as_bytes(), budget)?,
            provider_prepare,
            model_list,
            model_save,
            model_resolve: model_resolve_routes,
            conversation_read: conversation_routes,
            budget: Arc::clone(budget),
        })
    }
}

impl LockedRegistryBundle<'_> {
    /// Reread and confirm the locked Pack and executable catalog for each pin.
    pub(crate) fn verify(&self, mut checkpoint: impl FnMut() -> Result<()>) -> Result<()> {
        let root = Path::new(OsStr::from_bytes(self.root.as_bytes()));
        if let Some([coordinator, provider]) = &self.provider_prepare {
            coordinator.verify(root, &self.budget, &mut checkpoint)?;
            provider.verify(root, &self.budget, &mut checkpoint)?;
        }
        if let Some(model_list) = &self.model_list {
            model_list.verify(root, &self.budget, &mut checkpoint)?;
        }
        if let Some([model_manage, provider_read]) = &self.model_save {
            model_manage.verify(root, &self.budget, &mut checkpoint)?;
            provider_read.verify(root, &self.budget, &mut checkpoint)?;
        }
        let mut model_resolve = self.model_resolve.iter().flatten();
        if let Some(first) = model_resolve.next() {
            // Every role targets the same locked Model Pack. Check each signed pin
            // against that Pack and catalog, including the role's own operation.
            let pack = first.pack.read(root, &self.budget, &mut checkpoint)?;
            let catalog = first.catalog.read(root, &self.budget, &mut checkpoint)?;
            for route in std::iter::once(first).chain(model_resolve) {
                checkpoint()?;
                route.verify_documents(&pack, &catalog, Some("read"))?;
            }
        }
        for conversation_read in self.conversation_read.iter().flatten() {
            conversation_read.verify(root, &self.budget, &mut checkpoint)?;
        }
        checkpoint()
    }
}

struct LockedRegistryRoute<'pin> {
    pin: &'pin OriginalRoutePin,
    pack: LockedDocument,
    catalog: LockedDocument,
}

impl<'pin> LockedRegistryRoute<'pin> {
    fn from_lock(
        lock: &VerifiedBundleLock,
        pin: &'pin OriginalRoutePin,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Self> {
        let pack_id = pin.pack_id();
        let path = lock
            .pack_paths
            .get(pack_id)
            .filter(|path| lock.authority_roles.get(*path) == Some(&BundleEntryKind::Pack))
            .context("legacy Registry Pack is absent from the accepted Bundle authority")?;
        let pack_digest = lock
            .authority_digests
            .get(path)
            .context("legacy Registry Pack is missing its accepted Bundle digest")?;
        let (catalog_path, catalog_digest) = lock
            .sidecar_digests
            .iter()
            .find(|(path, _)| {
                // The Bundle verifier already enforced each catalog's path and Pack identity.
                path.strip_prefix("packs/")
                    .and_then(|name| name.strip_suffix(".executables.v4.json"))
                    == Some(pack_id)
            })
            .context("legacy Registry executable catalog is absent from the accepted Bundle")?;
        Ok(Self {
            pin,
            pack: LockedDocument::new(path, pack_digest, budget)?,
            catalog: LockedDocument::new(catalog_path, catalog_digest, budget)?,
        })
    }

    fn verify(
        &self,
        root: &Path,
        budget: &Arc<dyn AllocationBudget>,
        checkpoint: &mut impl FnMut() -> Result<()>,
    ) -> Result<()> {
        let pack = self.pack.read(root, budget, checkpoint)?;
        let catalog = self.catalog.read(root, budget, checkpoint)?;
        self.verify_documents(&pack, &catalog, None)
    }

    fn verify_documents(
        &self,
        pack: &CoreValue,
        catalog: &CoreValue,
        effect_class: Option<&str>,
    ) -> Result<()> {
        let pin = self.pin;
        let pack_identity = pack
            .get_key("pack")
            .context("missing locked Pack identity")?;
        let integrity = pack
            .get_key("integrity")
            .context("missing locked Pack integrity")?;
        let source_identity = integrity.get_key("source_identity");
        // The original Bundle verifier checked the catalog's closed schema and digest,
        // and only allowed a materialization alias on source-bound projections.
        let canonical_catalog_digest = catalog
            .get_key("materialization_catalog_digest")
            .filter(|value| value.as_str().is_some())
            .or_else(|| catalog.get_key("catalog_digest"));
        ensure!(
            text_matches(&pack_identity, "id", pin.pack_id())
                && text_matches(&pack_identity, "artifact_digest", pin.artifact_digest())
                && text_matches(catalog, "pack_id", pin.pack_id())
                && source_identity
                    .as_ref()
                    .and_then(CoreValue::as_str)
                    .is_some_and(|source| { text_matches(catalog, "source_identity", source) })
                && canonical_catalog_digest
                    .as_ref()
                    .and_then(CoreValue::as_str)
                    == Some(pin.executable_catalog_digest()),
            "legacy Registry Pack or canonical catalog identity differs from the selected pin"
        );
        let sidecar = unique_row(pack, "artifacts", "path", "executables.v4.json")?;
        ensure!(
            text_matches(&sidecar, "kind", "sidecar")
                && text_matches(&sidecar, "digest", self.catalog.digest.as_str()),
            "legacy Registry Pack does not commit the selected locked catalog bytes"
        );

        let function = unique_row(pack, "functions", "id", pin.function_id())?;
        let provider = unique_row(pack, "provider_catalog", "provider_id", pin.function_id())?;
        let operation = unique_row(
            pack,
            "operation_catalog",
            "operation_id",
            pin.operation_id(),
        )?;
        let contract = unique_row(pack, "contracts", "contract_id", pin.contract_id())?;
        let variant = unique_row(catalog, "variants", "variant_id", pin.variant_id())?;
        let variant_operation =
            unique_row(&variant, "operations", "operation_id", pin.operation_id())?;
        if let Some(effect_class) = effect_class {
            ensure!(
                text_matches(&variant_operation, "effect_class", effect_class),
                "legacy Registry executable operation effect differs from the selected route"
            );
        }
        let implementation_path = variant.get_key("implementation_path");
        let implementation = unique_row(
            pack,
            "artifacts",
            "path",
            implementation_path
                .as_ref()
                .and_then(CoreValue::as_str)
                .context("missing locked executable path")?,
        )?;

        let matches = |row: &CoreValue, fields: &[(&str, &str)]| {
            fields
                .iter()
                .all(|(key, expected)| text_matches(row, key, expected))
        };
        let operation_id = pin.operation_id();
        let contract_id = pin.contract_id();
        let revision = pin.contract_revision_digest();
        let implementation_digest = pin.function_implementation_digest();
        ensure!(
            matches(
                &function,
                &[
                    ("implementation_digest", implementation_digest),
                    ("contract_revision_digest", revision),
                    ("isolation", pin.domain_kind()),
                ]
            ) && matches(
                &provider,
                &[
                    ("owner", pin.pack_id()),
                    ("contract_reference", contract_id)
                ]
            ) && matches(
                &operation,
                &[
                    ("owner", pin.pack_id()),
                    ("contract_reference", contract_id),
                    ("provider_id", pin.function_id()),
                ]
            ) && matches(
                &variant,
                &[
                    ("function_id", pin.function_id()),
                    ("implementation_digest", implementation_digest),
                    ("platform", pin.platform()),
                    ("architecture", pin.architecture()),
                    ("runtime_abi", pin.runtime_abi()),
                    ("backend", pin.backend()),
                    ("execution_kind", pin.execution_kind()),
                ]
            ) && matches(
                &implementation,
                &[("kind", "executable"), ("digest", implementation_digest)]
            ) && matches(
                &variant_operation,
                &[("contract_id", contract_id), ("revision_digest", revision)]
            ) && text_matches(&contract, "revision_digest", revision)
                && [&function, &provider, &contract].into_iter().all(|row| {
                    row.get_key("operations").and_then(|value| {
                        Some(
                            value
                                .array()?
                                .iter()
                                .filter(|item| item.as_str() == Some(operation_id))
                                .count(),
                        )
                    }) == Some(1)
                }),
            "legacy Registry Pack or variant does not match the selected function and operation"
        );
        Ok(())
    }
}

struct LockedDocument {
    path: CoreBuffer,
    digest: Digest,
}

impl LockedDocument {
    fn new(path: &str, digest: &str, budget: &Arc<dyn AllocationBudget>) -> Result<Self> {
        Ok(Self {
            path: CoreBuffer::copy_from(path.as_bytes(), budget)?,
            digest: Digest::parse(digest)?,
        })
    }

    fn read(
        &self,
        root: &Path,
        budget: &Arc<dyn AllocationBudget>,
        checkpoint: &mut impl FnMut() -> Result<()>,
    ) -> Result<CoreValue> {
        checkpoint()?;
        let path = self.path.as_str()?;
        let mut failure = None;
        let bytes = read_relative_with(
            root,
            path,
            MAX_CANONICAL_JSON_BYTES as u64,
            |reader, size| {
                let bytes = CoreBuffer::write_exact(size as usize, budget, |target| {
                    for chunk in target.chunks_mut(8192) {
                        if let Err(error) =
                            checkpoint().and_then(|()| reader.read_exact(chunk).map_err(Into::into))
                        {
                            failure = Some(error);
                            return Err(AllocationError::Invalid);
                        }
                    }
                    Ok(())
                })
                .map_err(|_| CanonError::Limit("legacy Registry Bundle input"))?;
                Ok(bytes)
            },
        );
        if let Some(error) = failure {
            return Err(error);
        }
        let bytes = bytes.with_context(|| format!("could not read locked Bundle entry {path}"))?;
        checkpoint()?;
        ensure!(
            Digest::hash(bytes.as_bytes()) == self.digest,
            "locked Bundle digest changed: {path}"
        );
        CoreValue::parse_guest(bytes.as_bytes(), budget)
            .with_context(|| format!("could not decode locked Bundle entry {path}"))
    }
}

fn text_matches(value: &CoreValue, key: &str, expected: &str) -> bool {
    value.get_key(key).as_ref().and_then(CoreValue::as_str) == Some(expected)
}

fn unique_row(value: &CoreValue, list: &str, key: &str, id: &str) -> Result<CoreValue> {
    let rows = value
        .get_key(list)
        .with_context(|| format!("missing locked {list}"))?;
    let rows = rows
        .array()
        .with_context(|| format!("invalid locked {list}"))?;
    let mut rows = rows.iter().filter(|row| text_matches(row, key, id));
    let row = rows
        .next()
        .with_context(|| format!("missing selected row in locked {list}"))?;
    if rows.next().is_some() {
        bail!("ambiguous selected row in locked {list}");
    }
    Ok(row)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::symlink, time::SystemTime};
    use tobkiri_core::AllocationRequest;

    struct TestBudget;

    impl AllocationBudget for TestBudget {
        fn reserve(&self, _: AllocationRequest) -> Result<(), AllocationError> {
            Ok(())
        }

        fn release(&self, _: u64) {}
    }

    #[test]
    fn locked_document_rejects_replacement_and_preserves_checkpoint_failure() {
        let unique = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "tobkiri-locked-provider-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir(&root).unwrap();
        let path = root.join("selected.json");
        let original = br#"{"pin":"original"}"#;
        fs::write(&path, original).unwrap();
        let budget: Arc<dyn AllocationBudget> = Arc::new(TestBudget);
        let document =
            LockedDocument::new("selected.json", Digest::hash(original).as_str(), &budget).unwrap();

        let value = document.read(&root, &budget, &mut || Ok(())).unwrap();
        assert!(text_matches(&value, "pin", "original"));
        let mut checks = 0;
        let cancelled = document.read(&root, &budget, &mut || {
            checks += 1;
            ensure!(checks < 2, "inspection cancelled");
            Ok(())
        });
        assert_eq!(cancelled.unwrap_err().to_string(), "inspection cancelled");

        fs::write(&path, br#"{"pin":"modified"}"#).unwrap();
        assert!(document
            .read(&root, &budget, &mut || Ok(()))
            .unwrap_err()
            .to_string()
            .contains("digest changed"));
        let other = root.join("other.json");
        fs::write(&other, original).unwrap();
        fs::remove_file(&path).unwrap();
        symlink(&other, &path).unwrap();
        let symlink = document.read(&root, &budget, &mut || Ok(()));
        fs::remove_dir_all(&root).unwrap();
        assert!(symlink.is_err());
    }
}
