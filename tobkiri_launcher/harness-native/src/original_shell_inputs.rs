//! The two original catalog document roles used to select a Shell variant.
use super::*;

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

pub(super) const MAX_NAME: usize = 255;

pub(super) fn single_leaf(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= MAX_NAME
        && name != "."
        && name != ".."
        && !name.contains(['/', '\0'])
}

impl CatalogIo {
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
            let exhausted = || OriginalAuthorityError::capacity(OriginalAuthorityStage::Source);
            let allocation = maximum
                .checked_mul(std::mem::size_of::<Entry>())
                .ok_or_else(exhausted)?;
            let capacity = op.reserve(OriginalAuthorityStage::Source, allocation)?;
            let mut values = Vec::new();
            values.try_reserve_exact(maximum).map_err(|_| exhausted())?;
            if values.capacity() != maximum {
                return Err(exhausted());
            }
            self.shell_inputs = Some(ShellInputs(Funded {
                value: values,
                _capacity: capacity,
            }));
        }
        let inputs = &mut self.shell_inputs.as_mut().expect("retained Shell inputs").0;
        if inputs.len() == inputs.capacity() {
            return Err(OriginalAuthorityError::capacity(
                OriginalAuthorityStage::Source,
            ));
        }
        inputs.push(Entry { kind, bytes });
        Ok(())
    }

    pub(super) fn selected_shell_leaf(&self) -> anyhow::Result<CoreValue> {
        let inputs = self
            .shell_inputs
            .as_ref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let profile_id = self
            .shell_profile_id
            .as_deref()
            .ok_or_else(OriginalAuthorityError::unavailable)?;
        let profile = inputs.find(Kind::Profile, "profile_id", profile_id, &self.budget)?;
        let request = field(&profile, "shell")?;
        let provider = field(&request, "provider_id")?;
        let shell = inputs.find(
            Kind::Shell,
            "provider_id",
            provider
                .as_str()
                .ok_or_else(OriginalAuthorityError::unavailable)?,
            &self.budget,
        )?;
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
        // Keep only the selected name beyond this physical observation.
        Ok(CoreValue::string(name, &self.budget)?)
    }
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
    ) -> anyhow::Result<CoreValue> {
        let mut result = None;
        for entry in self.0.iter().filter(|entry| entry.kind == kind) {
            // Read only the already funded bytes handed to the actual
            // successful catalog loader, never an extra disk read. The Pack
            // still performs its own validation and selection in its order.
            let document = CoreValue::parse_guest(&entry.bytes.value, budget)?;
            if text_is(&document, key, expected) && result.replace(document).is_some() {
                return Err(OriginalAuthorityError::unavailable().into());
            }
        }
        result.ok_or_else(|| OriginalAuthorityError::unavailable().into())
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
    other
        .get_key(key)
        .is_some_and(|other| other.as_str().is_some_and(|text| text_is(value, key, text)))
}
