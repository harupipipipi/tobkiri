//! Scoped handle transfer at the typed component boundary.
use crate::{
    PackHost, ProviderFrame,
    owned_values::{ValueOwners, rewrite_handles_owned},
};
use anyhow::{Context, Result, bail};
use std::sync::Arc;
use tobkiri_canon::ids::ShapeRef;
use tobkiri_gate::{CallCx, ControlKind};
use tobkiri_manifest::WitType;
use wasmtime::component::Val;

/// Already-held source/recipient references for one typed handle transfer.
/// This groups call parameters only; it grants no authority or ownership.
#[derive(Clone, Copy)]
pub(crate) struct HandleSourceContext<'a> {
    pub(crate) owner: &'a CallCx,
    pub(crate) recipient: &'a CallCx,
    pub(crate) authority: &'a ShapeRef,
    pub(crate) provider: &'a Arc<dyn ProviderFrame>,
}

impl PackHost {
    /// A transferred existing handle gets a new recipient alias. It never
    /// becomes an unauthenticated provider-local ID or acquires new authority.
    pub(crate) fn transfer_argument_handles(
        &self,
        value: &Val,
        ty: &WitType,
        caller: &CallCx,
        recipient: &CallCx,
        owners: &ValueOwners,
        provider: &Arc<dyn ProviderFrame>,
    ) -> Result<Val> {
        rewrite_handles_owned(value, ty, owners, &mut |kind, id| {
            if let Some(inventory) = self.native_input_argument(provider, kind, id)? {
                return self.alias_native_input_argument(inventory.get(), kind, id, recipient);
            }
            let mut table = self
                .shared
                .handles
                .lock()
                .map_err(|_| anyhow::anyhow!("handle table poisoned"))?;
            let entry = table.resolve(id, kind, caller)?;
            self.shared.gate.check_context(&entry.owner)?;
            self.check_handle_generation(&entry)?;
            table.alias(entry, recipient.clone())
        })
    }

    /// Source-open effects are admitted before exposing any new handle. The
    /// bounded temporary list is trusted control metadata; actual Val copies
    /// remain funded by the producing payload owner.
    pub(crate) async fn transfer_result_handles(
        &self,
        value: &Val,
        ty: &WitType,
        generation: u64,
        source: HandleSourceContext<'_>,
        owners: &ValueOwners,
    ) -> Result<Val> {
        let HandleSourceContext {
            owner, recipient, ..
        } = source;
        const MAX: usize = 1024;
        let _control = self
            .shared
            .control
            .reserve(ControlKind::Frame, (MAX * 32) as u64)?;
        let mut handles = Vec::new();
        handles.try_reserve(MAX)?;
        let probe = rewrite_handles_owned(value, ty, owners, &mut |kind, id| {
            if !handles.contains(&(kind, id)) {
                if handles.len() == MAX {
                    bail!("returned handle count exceeded")
                }
                handles.push((kind, id));
            }
            Ok(id)
        })?;
        drop(probe);
        let mut mapped = Vec::new();
        mapped.try_reserve(handles.len())?;
        for (kind, id) in handles {
            // Forwarding an already scoped core handle preserves its original
            // source frame and one lease. Local handles open a new lease once.
            let existing = {
                self.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .resolve(id, kind, owner)
                    .ok()
            };
            let mapped_id = if let Some(entry) = existing {
                self.shared.gate.check_context(&entry.owner)?;
                self.check_handle_generation(&entry)?;
                self.shared
                    .handles
                    .lock()
                    .map_err(|_| anyhow::anyhow!("handle table poisoned"))?
                    .alias(entry, recipient.clone())?
            } else {
                self.register_guest_handle(kind, id, Some(generation), source)
                    .await?
            };
            mapped.push((kind, id, mapped_id));
        }
        rewrite_handles_owned(value, ty, owners, &mut |kind, id| {
            mapped
                .iter()
                .find(|entry| entry.0 == kind && entry.1 == id)
                .map(|entry| entry.2)
                .context("returned handle mapping missing")
        })
    }
}
