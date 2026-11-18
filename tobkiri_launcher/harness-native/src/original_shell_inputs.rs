//! The original catalog documents and Pack pins used to select a Shell variant.
use super::*;
use tobkiri_canon::AllocationError;

#[cfg(test)]
#[path = "original_recovery_shell_selection_tests.rs"]
mod recovery_tests;

#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    Profile,
    Shell,
}

struct Entry {
    kind: Kind,
    bytes: Funded<Zeroizing<Vec<u8>>>,
}

pub(super) struct ShellInputs(Funded<Vec<Entry>>);

#[derive(Default)]
pub(super) struct PackPins(Option<Funded<Vec<Option<PackPin>>>>);

struct PackPin {
    id: CoreValue,
    digest: Digest,
    shell: bool,
}

pub(super) struct SelectedShell {
    pub(super) artifact: CoreValue,
    pub(super) entrypoint: Option<CoreValue>,
    pub(super) macos: bool,
}

/// Recovery identity selection from the physically retained Shell definition.
/// The variant and path share the allocation owner of that parsed definition.
pub(super) struct RecoveryShellVariant {
    committed_shell: CoreValue,
    definition: CoreValue,
    variant: CoreValue,
    relative: CoreValue,
}

impl RecoveryShellVariant {
    pub(super) fn relative(&self) -> &str {
        self.relative.as_str().expect("checked Shell artifact path")
    }

    pub(super) fn variant(&self) -> &CoreValue {
        &self.variant
    }
}

pub(super) const MAX_NAME: usize = 255;

pub(super) fn single_leaf(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME
        && name != "."
        && name != ".."
        && !name.contains(['/', '\0'])
}

impl CatalogIo {
    pub(super) fn retain_pack_pin<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        bytes: &[u8],
    ) -> Result<()> {
        let Some(pins) = self.recovery_pack_pins.as_ref() else {
            return Ok(());
        };
        let (lock, entries) = self.entries()?;
        let entry = *entries
            .get(self.entry)
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if !pack_entry(lock, entry) {
            return Ok(());
        }
        let allocation = if pins.0.is_none() {
            charge_work(&self.budget, (entries.len() as u64).saturating_mul(16))?;
            let count = entries.iter().filter(|&&id| pack_entry(lock, id)).count();
            Some(funded_entries(op, count)?)
        } else {
            None
        };
        let pin = pack_pin(bytes, &self.budget)?;
        let pins = &mut self
            .recovery_pack_pins
            .as_mut()
            .expect("retained Pack pins")
            .0;
        if let Some(allocation) = allocation {
            *pins = Some(allocation);
        }
        push_entry(pins.as_mut().expect("funded Pack pins"), pin)
    }

    pub(super) fn retain_shell_input<S: crate::BootstrapReviewOwner>(
        &mut self,
        op: &Operation<'_, S>,
        bytes: Funded<Zeroizing<Vec<u8>>>,
    ) -> Result<()> {
        let (lock, entries) = self.entries()?;
        let entry = *entries
            .get(self.entry)
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let Some(kind) = shell_input_kind(lock, entry) else {
            return Ok(());
        };
        if self.shell_inputs.is_none() {
            charge_work(&self.budget, (entries.len() as u64).saturating_mul(16))?;
            let maximum = entries
                .iter()
                .filter(|&&entry| shell_input_kind(lock, entry).is_some())
                .count();
            self.shell_inputs = Some(ShellInputs(funded_entries(op, maximum)?));
        }
        let inputs = &mut self.shell_inputs.as_mut().expect("retained Shell inputs").0;
        push_entry(inputs, Entry { kind, bytes })
    }

    pub(super) fn selected_shell(&self) -> anyhow::Result<SelectedShell> {
        let inputs = self
            .shell_inputs
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let request = match &self.pack_resolve_source {
            Some(PackResolveSource::Bundled(profile_id)) => {
                let profile = inputs
                    .find(Kind::Profile, "profile_id", profile_id, &self.budget)?
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                field(&profile, "shell")?
            }
            Some(PackResolveSource::Recovery(Some(request))) => request.share(),
            _ => return Err(OriginalAuthorityError::unavailable().into()),
        };
        let provider = field(&request, "provider_id")?;
        let shell = inputs
            .find(
                Kind::Shell,
                "provider_id",
                provider
                    .as_str()
                    .ok_or_else(OriginalAuthorityError::unavailable)?,
                &self.budget,
            )?
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if !text_is(&shell, "availability", "verified")
            || !same_text(&shell, &request, "pack_id")
            || !same_text(&shell, &request, "contract_id")
        {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let launch = field(&shell, "launch")?;
        let variants = field(&launch, "variants")?;
        let variants = variants
            .array()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let mut selected = variants.iter().filter(|variant| {
            same_text(variant, &request, "platform") && same_text(variant, &request, "architecture")
        });
        let variant = selected
            .next()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        if selected.next().is_some() {
            return Err(OriginalAuthorityError::unavailable().into());
        }
        let name = field(&variant, "relative_path")?;
        let name = name
            .as_str()
            .filter(|name| single_leaf(name))
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        // Retain only the two selected names from the authenticated catalog.
        // The root selection still handles a missing entrypoint as before;
        // the later physical resolve requires an admitted value.
        let entrypoint = variant.get_key("entrypoint");
        let entrypoint = entrypoint
            .as_ref()
            .and_then(CoreValue::as_str)
            .filter(|entry| entrypoint_path(entry))
            .map(|entry| CoreValue::string(entry, &self.budget))
            .transpose()?;
        Ok(SelectedShell {
            artifact: CoreValue::string(name, &self.budget)?,
            entrypoint,
            macos: text_is(&variant, "platform", "macos"),
        })
    }

    /// Match the original identity-only predicate before inspecting the tree.
    pub(super) fn recovery_identity_variant<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
    ) -> Result<RecoveryShellVariant> {
        let committed_shell = self.recovery_profile_shell(op, profile)?;
        let definition = self.recovery_shell_definition(&committed_shell)?;
        let unavailable = || shell_denied("active Profile Shell artifact is unavailable");
        let variants = field(&field(&definition, "launch")?, "variants")?;
        let variants = variants
            .array()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        charge_work(&self.budget, (variants.len() as u64).saturating_mul(128))?;
        let mut matching = variants.iter().filter(|variant| {
            same_text(variant, &committed_shell, "platform")
                && same_text(variant, &committed_shell, "architecture")
                && text_matches(
                    variant,
                    "entrypoint_digest",
                    &committed_shell,
                    "executable_artifact_digest",
                )
        });
        let variant = matching.next().ok_or_else(unavailable)?;
        if matching.next().is_some() || self.artifact_root.is_none() {
            return Err(unavailable());
        }
        let relative = field(&variant, "relative_path")?;
        let path = relative.as_str().ok_or_else(unavailable)?;
        charge_work(&self.budget, (path.len() as u64).saturating_mul(2))?;
        if path.contains('\0')
            || Path::new(path).is_absolute()
            || Path::new(path)
                .components()
                .any(|part| part == Component::ParentDir)
        {
            return Err(unavailable());
        }
        Ok(RecoveryShellVariant {
            committed_shell,
            definition,
            variant,
            relative,
        })
    }

    /// Classify the binding before reading any files, as in active snapshot recovery.
    pub(super) fn active_artifact_variant<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
        profiles: &CoreValue,
        admitted_closure: Option<&CoreValue>,
    ) -> Result<(CoreValue, ActiveArtifactVerification)> {
        self.select_active_variant(op, profile, admitted_closure, || {
            registered_shell(profiles, profile_id, &self.budget)
        })
    }

    pub(super) fn initial_active_variant<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        profile_id: &str,
        source_profile: &CoreValue,
    ) -> Result<(CoreValue, ActiveArtifactVerification)> {
        self.select_active_variant(op, profile, None, || {
            Ok(initial_source_shell(source_profile, profile_id))
        })
    }

    fn select_active_variant<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
        admitted_closure: Option<&CoreValue>,
        source_shell: impl FnOnce() -> Result<Option<CoreValue>>,
    ) -> Result<(CoreValue, ActiveArtifactVerification)> {
        let shell = self.recovery_profile_shell(op, profile)?;
        let definition = self.recovery_shell_definition(&shell)?;
        if !text_is(&definition, "availability", "verified") {
            return Err(shell_denied(
                "active Profile Shell definition is unavailable",
            ));
        }
        let variants = field(&field(&definition, "launch")?, "variants")?;
        let variants = variants
            .array()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let platform_matches = |variant: &CoreValue| {
            same_text(variant, &shell, "platform") && same_text(variant, &shell, "architecture")
        };
        charge_work(&self.budget, (variants.len() as u64).saturating_mul(128))?;
        let exact = unique_variant(variants.iter().filter(|variant| {
            platform_matches(variant)
                && text_matches(
                    variant,
                    "entrypoint_digest",
                    &shell,
                    "executable_artifact_digest",
                )
        }));
        let unavailable = || shell_denied("active Profile Shell artifact is unavailable");
        let registered = source_shell()?.ok_or_else(unavailable)?;
        if ![
            "provider_id",
            "pack_id",
            "contract_id",
            "platform",
            "architecture",
        ]
        .into_iter()
        .all(|key| same_text(&shell, &registered, key))
        {
            return Err(unavailable());
        }
        let pack = field(&shell, "pack_id")?;
        let pack = pack.as_str().ok_or_else(unavailable)?;
        let current_revision = same_text(&shell, &definition, "definition_revision");
        if let Some(variant) = exact
            && same_text(&shell, &registered, "executable_artifact_digest")
            && current_revision
            && self.recovery_pack_digest_matches(
                pack,
                field(&shell, "artifact_digest")?
                    .as_str()
                    .ok_or_else(unavailable)?,
                admitted_closure,
            )?
        {
            return if self.artifact_root.is_some() {
                Ok((variant, ActiveArtifactVerification::Current))
            } else {
                Err(unavailable())
            };
        }
        if self.artifact_root.is_none()
            || current_revision
            || !["provider_id", "pack_id", "contract_id"]
                .into_iter()
                .all(|key| same_text(&definition, &shell, key))
        {
            return Err(unavailable());
        }
        charge_work(&self.budget, (variants.len() as u64).saturating_mul(128))?;
        let successor =
            unique_variant(variants.iter().filter(platform_matches)).ok_or_else(unavailable)?;
        // Only a bundled Shell can satisfy this check: the verified external
        // closure permits normal_sandbox and application Pack manifests.
        if !self.bundled_pack_pin(pack)?.is_some_and(|pin| pin.shell)
            || !same_text(&registered, &successor, "artifact_digest")
            || !text_matches(
                &registered,
                "executable_artifact_digest",
                &successor,
                "entrypoint_digest",
            )
            || !same_text(&registered, &definition, "definition_revision")
            || !same_text(&definition, &successor, "artifact_digest")
        {
            return Err(unavailable());
        }
        Ok((
            successor,
            ActiveArtifactVerification::SuccessorNeedsReconfirmation,
        ))
    }

    fn recovery_profile_shell<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        profile: &CoreValue,
    ) -> Result<CoreValue> {
        self.require_recovery_catalog(op)?;
        profile
            .get_key("shell")
            .filter(|shell| shell.object().is_some())
            .ok_or_else(|| shell_denied("active Profile Shell binding is unavailable"))
    }

    pub(super) fn require_recovery_catalog<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
    ) -> Result<()> {
        op.check(OriginalAuthorityStage::Source)?;
        if !self.completed()
            || self.binding.is_none()
            || !self.invocation.same_invocation(op.invocation)
            || !self.cancellation.same_node(op.cancellation)
            || !Arc::ptr_eq(&self.cleanup, &op.cleanup)
        {
            return Err(OriginalAuthorityError::unavailable());
        }
        Ok(())
    }

    fn recovery_shell_definition(&self, committed_shell: &CoreValue) -> Result<CoreValue> {
        let unavailable = || shell_denied("active Profile Shell definition is unavailable");
        let provider = committed_shell
            .get_key("provider_id")
            .ok_or_else(unavailable)?;
        let provider = provider.as_str().ok_or_else(unavailable)?;
        self.shell_inputs
            .as_ref()
            .ok_or_else(unavailable)?
            .find(Kind::Shell, "provider_id", provider, &self.budget)?
            .ok_or_else(unavailable)
    }

    /// Require the original full binding against the profiles and verified
    /// external closure actually supplied to the replacement catalog call.
    pub(super) fn verify_recovery_shell_binding<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        selected: &RecoveryShellVariant,
        profile_id: &str,
        profiles: &CoreValue,
        admitted_closure: Option<&CoreValue>,
    ) -> Result<()> {
        self.verify_current_shell_binding(op, selected, admitted_closure, || {
            registered_shell(profiles, profile_id, &self.budget)
        })
    }

    /// The independent publication loader supplies both the source Profile
    /// and the bundled Pack pin for the selected Shell.
    pub(super) fn verify_initial_shell_binding<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        selected: &RecoveryShellVariant,
        profile_id: &str,
        source_profile: &CoreValue,
    ) -> Result<()> {
        self.verify_current_shell_binding(op, selected, None, || {
            Ok(initial_source_shell(source_profile, profile_id))
        })
    }

    fn verify_current_shell_binding<S: OriginalAuthorityOwner>(
        &self,
        op: &Operation<'_, S>,
        selected: &RecoveryShellVariant,
        admitted_closure: Option<&CoreValue>,
        source_shell: impl FnOnce() -> Result<Option<CoreValue>>,
    ) -> Result<()> {
        op.check(OriginalAuthorityStage::Source)?;
        if !text_is(&selected.definition, "availability", "verified") {
            return Err(shell_denied(
                "active Profile Shell definition is unavailable",
            ));
        }
        let unavailable = || shell_denied("active Profile Shell artifact is unavailable");
        let source_shell = source_shell()?.ok_or_else(unavailable)?;
        let shell = &selected.committed_shell;
        if ![
            "provider_id",
            "pack_id",
            "contract_id",
            "platform",
            "architecture",
            "executable_artifact_digest",
        ]
        .into_iter()
        .all(|key| same_text(shell, &source_shell, key))
            || !same_text(shell, &selected.definition, "definition_revision")
        {
            return Err(unavailable());
        }
        let pack = field(shell, "pack_id")?;
        let digest = shell.get_key("artifact_digest").ok_or_else(unavailable)?;
        if !self.recovery_pack_digest_matches(
            pack.as_str().ok_or_else(unavailable)?,
            digest.as_str().ok_or_else(unavailable)?,
            admitted_closure,
        )? {
            return Err(unavailable());
        }
        Ok(())
    }

    fn recovery_pack_digest_matches(
        &self,
        id: &str,
        digest: &str,
        admitted_closure: Option<&CoreValue>,
    ) -> Result<bool> {
        if let Some(pin) = self.bundled_pack_pin(id)? {
            return Ok(pin.digest.as_str() == digest);
        }
        let Some(admitted_closure) = admitted_closure else {
            return Ok(false);
        };
        let Some(row) = matching_row(admitted_closure, id, &self.budget)? else {
            return Ok(false);
        };
        let header = row
            .get_index(1)
            .and_then(|manifest| manifest.get_key("pack"))
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        Ok(text_is(&header, "id", id)
            && (text_is(&header, "kind", "normal_sandbox")
                || text_is(&header, "kind", "application"))
            && text_is(&header, "artifact_digest", digest))
    }

    fn bundled_pack_pin(&self, id: &str) -> Result<Option<&PackPin>> {
        let pins = &self
            .recovery_pack_pins
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?
            .0;
        if let Some(pins) = pins {
            charge_work(
                &self.budget,
                (pins.len() as u64).saturating_mul(id.len() as u64 + 1),
            )?;
            for pin in pins.iter() {
                let pin = pin
                    .as_ref()
                    .ok_or_else(OriginalAuthorityError::unavailable)?;
                if pin.id.as_str() == Some(id) {
                    return Ok(Some(pin));
                }
            }
        }
        Ok(None)
    }
}

fn initial_source_shell(source: &CoreValue, profile_id: &str) -> Option<CoreValue> {
    source
        .get_key("shell")
        .filter(|shell| text_is(source, "profile_id", profile_id) && shell.object().is_some())
}

fn unique_variant(mut matching: impl Iterator<Item = CoreValue>) -> Option<CoreValue> {
    let first = matching.next()?;
    matching.next().is_none().then_some(first)
}

pub(super) fn registered_shell(
    profiles: &CoreValue,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreValue>> {
    Ok(registered_profile(profiles, profile_id, budget)?
        .and_then(|profile| profile.get_key("shell"))
        .filter(|shell| shell.object().is_some()))
}

pub(super) fn registered_profile(
    profiles: &CoreValue,
    profile_id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreValue>> {
    Ok(matching_row(profiles, profile_id, budget)?.and_then(|row| row.get_index(1)))
}

fn funded_entries<S: OriginalAuthorityOwner, T>(
    op: &Operation<'_, S>,
    count: usize,
) -> Result<Funded<Vec<T>>> {
    let allocation = count
        .checked_mul(std::mem::size_of::<T>())
        .ok_or_else(source_capacity)?;
    let capacity = op.reserve(OriginalAuthorityStage::Source, allocation)?;
    let mut value = Vec::new();
    value
        .try_reserve_exact(count)
        .map_err(|_| source_capacity())?;
    if value.capacity() != count {
        return Err(source_capacity());
    }
    Ok(Funded {
        value,
        _capacity: capacity,
    })
}

fn push_entry<T>(entries: &mut Funded<Vec<T>>, entry: T) -> Result<()> {
    if entries.len() == entries.capacity() {
        return Err(source_capacity());
    }
    entries.push(entry);
    Ok(())
}

fn pack_entry(lock: &Document, entry: usize) -> bool {
    lock.get(entry, "kind")
        .is_some_and(|kind| lock.string_is(kind, "pack"))
}

fn pack_pin(bytes: &[u8], budget: &Arc<dyn AllocationBudget>) -> Result<Option<PackPin>> {
    let manifest = match CoreValue::parse_guest(bytes, budget) {
        Ok(manifest) => manifest,
        // Leave invalid bytes to the actual loader's validation. A missing
        // capture also prevents adopting an external Pack on this basis.
        Err(AllocationError::Invalid | AllocationError::Depth) => return Ok(None),
        Err(_) => return Err(source_capacity()),
    };
    let pin = (|| {
        let header = manifest.get_key("pack")?;
        let id = header.get_key("id")?;
        let digest = header.get_key("artifact_digest")?;
        let digest = Digest::parse(digest.as_str()?).ok()?;
        let shell = text_is(&header, "kind", "shell");
        Some((id, digest, shell))
    })();
    let Some((id, digest, shell)) = pin else {
        return Ok(None);
    };
    let Some(id) = id.as_str() else {
        return Ok(None);
    };
    // Copy only the Pack ID. Sharing the original field would retain the
    // entire manifest's graph through the recovery continuation.
    let id = CoreValue::string(id, budget).map_err(|_| source_capacity())?;
    Ok(Some(PackPin { id, digest, shell }))
}

fn matching_row(
    rows: &CoreValue,
    id: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<Option<CoreValue>> {
    let rows = rows
        .array()
        .ok_or_else(OriginalAuthorityError::unavailable)?;
    charge_work(
        budget,
        (rows.len() as u64).saturating_mul(id.len() as u64 + 1),
    )?;
    let mut matching = rows
        .iter()
        .filter(|row| row.get_index(0).is_some_and(|key| key.as_str() == Some(id)));
    let first = matching.next();
    if matching.next().is_some() {
        return Err(OriginalAuthorityError::unavailable());
    }
    Ok(first)
}

fn shell_denied(message: &'static str) -> OriginalAuthorityError {
    raw(recovery_workspace::APP_DENIED, message)
}

fn source_capacity() -> OriginalAuthorityError {
    OriginalAuthorityError::capacity(OriginalAuthorityStage::Source)
}

fn entrypoint_path(name: &str) -> bool {
    name.len() < libc::PATH_MAX as usize && name.split('/').all(single_leaf)
}

fn shell_input_kind(lock: &Document, entry: usize) -> Option<Kind> {
    let kind = lock.get(entry, "kind")?;
    if lock.string_is(kind, "profile") {
        Some(Kind::Profile)
    } else if lock.string_is(kind, "shell") {
        Some(Kind::Shell)
    } else {
        None
    }
}

impl ShellInputs {
    fn find(
        &self,
        kind: Kind,
        key: &str,
        expected: &str,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<CoreValue>> {
        let mut result = None;
        for entry in self.0.iter().filter(|entry| entry.kind == kind) {
            // Read only the already funded bytes handed to the actual
            // successful catalog loader, never an extra disk read. The Pack
            // still performs its own validation and selection in its order.
            let document = CoreValue::parse_guest(&entry.bytes.value, budget)
                .map_err(|_| source_capacity())?;
            if text_is(&document, key, expected) && result.replace(document).is_some() {
                return Err(OriginalAuthorityError::unavailable());
            }
        }
        Ok(result)
    }
}

fn field(value: &CoreValue, key: &str) -> Result<CoreValue> {
    value
        .get_key(key)
        .ok_or_else(OriginalAuthorityError::unavailable)
}

fn text_is(value: &CoreValue, key: &str, text: &str) -> bool {
    value
        .get_key(key)
        .is_some_and(|value| value.as_str() == Some(text))
}

fn same_text(value: &CoreValue, other: &CoreValue, key: &str) -> bool {
    text_matches(value, key, other, key)
}

fn text_matches(value: &CoreValue, key: &str, other: &CoreValue, other_key: &str) -> bool {
    other
        .get_key(other_key)
        .is_some_and(|other| other.as_str().is_some_and(|text| text_is(value, key, text)))
}
