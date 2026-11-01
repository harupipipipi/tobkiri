//! Physical Input custody captured from the actual typed result. Only the
//! registered Core bridge can attach this inventory to a reached Flow caller.

use crate::{
    HandleEntry, HandleKind, PackHost, ProviderFrame,
    owned_values::{ValueOwners, core_to_val, rewrite_handles_owned},
};
use anyhow::{Context, Result, bail};
use std::sync::Arc;
use tobkiri_canon::{ControlHandle, CoreValue, Digest};
use tobkiri_gate::{CallCx, ControlKind, ControlReservation};
use tobkiri_manifest::{WitFunction, WitType};

struct CapturedHandle {
    id: u64,
    entry: HandleEntry,
}

/// Read-only lifetime inventory of actual scoped Input source entries. Its
/// constructor is private; holding it grants no resolve/dispatch authority.
/// The data and retained source entries drop before the external reservation.
pub struct NativeInputHandles {
    entries: Vec<CapturedHandle>,
    recipient: CallCx,
    payload: Digest,
}

impl NativeInputHandles {
    /// Original authenticated recipient; Core compares its complete binding.
    pub fn recipient(&self) -> &CallCx {
        &self.recipient
    }

    /// Digest of the original immutable typed read payload, without new IDs.
    pub fn payload_digest(&self) -> &Digest {
        &self.payload
    }

    /// Inventory membership is descriptive only. Live Core provenance and
    /// the same actual table-entry identity are required before aliasing.
    pub fn contains(&self, kind: HandleKind, id: u64) -> bool {
        self.entries
            .iter()
            .any(|item| item.id == id && item.entry.kind == kind)
    }

    fn authenticates(&self, id: u64, entry: &HandleEntry) -> bool {
        self.entries.iter().any(|item| {
            item.id == id
                && item.entry.kind == entry.kind
                && Arc::ptr_eq(&item.entry.lease, &entry.lease)
                && item.entry.generation == entry.generation
                && item.entry.local_id == entry.local_id
                && match (&item.entry.source_frame, &entry.source_frame) {
                    (Some(a), Some(b)) => Arc::ptr_eq(a, b),
                    (None, None) => true,
                    _ => false,
                }
        })
    }
}

impl PackHost {
    pub(crate) fn capture_native_input_handles(
        &self,
        recipient: &CallCx,
        value: &CoreValue,
        function: &WitFunction,
        owners: &ValueOwners,
    ) -> Result<Option<ControlHandle<NativeInputHandles>>> {
        let payload_type = match function.result.as_ref() {
            Some(WitType::Result { ok, .. }) => ok.as_deref(),
            value => value,
        };
        let Some(ty) = payload_type.filter(|ty| crate::dispatch::has_handle(ty)) else {
            return Ok(None);
        };
        const MAX: usize = 1024;
        let temporary = self
            .shared
            .control
            .reserve(ControlKind::Frame, (MAX * 32) as u64)?;
        let mut ids = Vec::new();
        ids.try_reserve_exact(MAX)?;
        let typed = core_to_val(value, ty, owners)?;
        let probe = rewrite_handles_owned(&typed, ty, owners, &mut |kind, id| {
            if !ids.contains(&(kind, id)) {
                if ids.len() == MAX {
                    bail!("physical Input handle bound exceeded")
                }
                ids.push((kind, id));
            }
            Ok(id)
        })?;
        drop(probe);
        drop(typed);
        if ids.is_empty() {
            return Ok(None);
        }
        // Prepay the actual vector and all copied shape identities before the
        // first owning table-entry clone. CallCx/lease/source clones share their
        // existing funded storage and retain the original physical custodians.
        let dynamic = {
            let table = self
                .shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?;
            table.native_input_capture_bytes(&ids, recipient)?
        };
        let bytes = ControlHandle::<NativeInputHandles>::storage_bytes::<ControlReservation>()
            .checked_add(
                ids.len()
                    .checked_mul(std::mem::size_of::<CapturedHandle>())
                    .context("Input inventory overflow")?,
            )
            .and_then(|bytes| bytes.checked_add(dynamic))
            .context("Input inventory overflow")?;
        let reservation = self
            .shared
            .control
            .reserve(ControlKind::Frame, u64::try_from(bytes)?)?;
        let mut entries = Vec::new();
        entries.try_reserve_exact(ids.len())?;
        for (kind, id) in &ids {
            let entry = {
                self.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .resolve(*id, *kind, recipient)?
            };
            self.shared.gate.check_context(&entry.owner)?;
            self.check_handle_generation(&entry)?;
            entries.push(CapturedHandle { id: *id, entry });
        }
        let inventory = NativeInputHandles {
            entries,
            recipient: recipient.clone(),
            payload: value.digest_guest(owners.graph())?,
        };
        let result = ControlHandle::try_new_precharged(inventory, reservation)
            .map_err(|_| anyhow::anyhow!("physical Input inventory allocation failed"))?;
        drop(ids);
        drop(temporary);
        Ok(Some(result))
    }

    pub(crate) fn native_input_argument(
        &self,
        provider: &Arc<dyn ProviderFrame>,
        kind: HandleKind,
        id: u64,
    ) -> Result<Option<ControlHandle<NativeInputHandles>>> {
        self.shared
            .provider_entry
            .get()
            .context("Core provider bridge missing")?
            .native_input_argument(provider, kind, id)
            .map_err(anyhow::Error::new)
    }

    pub(crate) fn alias_native_input_argument(
        &self,
        inventory: &NativeInputHandles,
        kind: HandleKind,
        id: u64,
        recipient: &CallCx,
    ) -> Result<u64> {
        recipient.check_active()?;
        self.shared.gate.check_context(inventory.recipient())?;
        let captured = inventory
            .entries
            .iter()
            .find(|item| item.id == id && item.entry.kind == kind)
            .context("Input inventory lacks this typed entry")?;
        // Both temporary table-entry clones are prepaid before copying shape
        // identity. Original inventory/table owners remain strong throughout.
        let bytes = std::mem::size_of::<HandleEntry>()
            .checked_add(captured.entry.authority.clone_storage_bytes())
            .and_then(|bytes| bytes.checked_mul(2))
            .context("Input alias scratch overflow")?;
        let _scratch = self
            .shared
            .control
            .reserve(ControlKind::Frame, u64::try_from(bytes)?)?;
        let entry = {
            self.shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                .resolve(id, kind, inventory.recipient())?
        };
        if !inventory.authenticates(id, &entry) {
            bail!("Input source entry identity changed")
        }
        self.shared.gate.check_context(&entry.owner)?;
        self.check_handle_generation(&entry)?;
        let alias = {
            let mut table = self
                .shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?;
            let current = table.resolve(id, kind, inventory.recipient())?;
            if !inventory.authenticates(id, &current) {
                bail!("Input source entry identity changed")
            }
            table.alias(current, recipient.clone())?
        };
        Ok(alias)
    }
}
