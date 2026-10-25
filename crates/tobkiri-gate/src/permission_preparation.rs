//! In-memory preparation retains the original native decision and reservation.
//! No policy row or successful terminal is published until ordinary commit.
//! This is not a multi-effect transaction or a durable Profile save journal.
use super::*;

pub(super) enum PreparedPermissionTarget<'a> {
    Pack {
        pack: &'a PackId,
        hash: &'a PackHash,
    },
    User {
        profile_hash: &'a Digest,
        revision: &'a Digest,
        epoch: u64,
    },
}
impl PreparedPermissionTarget<'_> {
    fn effect<'a>(&'a self, permissions: &'a PermissionSet) -> PermissionEffect<'a> {
        match self {
            Self::Pack { pack, hash } => PermissionEffect::Pack {
                pack,
                hash,
                permissions,
            },
            Self::User {
                profile_hash,
                revision,
                epoch,
            } => PermissionEffect::User {
                profile_hash,
                revision,
                epoch: *epoch,
                permissions,
            },
        }
    }

    fn event_kind(&self) -> &'static str {
        match self {
            Self::Pack { .. } => "permission-update",
            Self::User { .. } => "user-permission-update",
        }
    }
}

/// Non-serializable, non-cloneable native preparation. It installs no rights.
/// The same original context, epoch, expiry and exact reservation are checked
/// at commit. Dropping it abandons the original reservation and audit custody.
#[must_use = "Permission preparation must be explicitly committed or dropped"]
pub struct PreparedPermissionApproval<'a> {
    gate: &'a GateEngine,
    cx: &'a CallCx,
    reservation: Reservation,
    target: PreparedPermissionTarget<'a>,
    permissions: PermissionSet,
    // Drop order keeps the control charge through actual abandonment/delivery.
    _pending: PendingReservation<'a>,
    _retained: ControlReservation,
}
impl<'a> PreparedPermissionApproval<'a> {
    pub(super) fn new(
        gate: &'a GateEngine,
        cx: &'a CallCx,
        reservation: Reservation,
        target: PreparedPermissionTarget<'a>,
        permissions: PermissionSet,
        pending: PendingReservation<'a>,
        retained: ControlReservation,
    ) -> Self {
        Self {
            gate,
            cx,
            reservation,
            target,
            permissions,
            _pending: pending,
            _retained: retained,
        }
    }

    /// Commit exactly this permission effect via the existing Store transaction
    /// and immutable terminal protocol. This does not atomically commit other
    /// permissions, a Profile head or its security epoch.
    pub fn commit(self) -> Result<(), GateError> {
        self.cx.check_active()?;
        if self.gate.epoch(self.cx.profile())? != self.cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        self.gate
            .event(&self.reservation, self.target.event_kind(), None)?;
        self.gate.approvals.dispatch(&self.reservation.id)?;
        let terminal = self.gate.approvals.commit_permission_effect(
            &self.reservation,
            deadline_unix_ms(self.cx.deadline())?,
            self.target.effect(&self.permissions),
        )?;
        self.gate.deliver_terminal(&terminal)?;
        if terminal.decision.outcome != Outcome::Succeeded {
            return Err(GateError::Deadline);
        }
        Ok(())
    }
}

/// Conservative Core-control custody for owned policy collections retained
/// across subsequent prompts. It counts actual Vec/String capacities. The
/// fixed per-entry BTree allowance covers nodes under the pinned Rust runtime;
/// it is not a whole-process RSS measurement or additional Pack authority.
///
/// Exhaustive field patterns make new policy fields require an accounting
/// decision. Existing transient parsing/mediation allocations are separate.
pub(super) fn retained_permission_bytes(p: &PermissionSet) -> Result<u64, GateError> {
    let PermissionSet {
        network,
        fs,
        proc,
        pty: _,
        secrets,
        storage,
        native: _,
        cross_profile_storage: _,
        audit_read: _,
        audit_append: _,
        host_io,
        ports,
        mediator: _,
        wasm,
        loops,
    } = p;
    let NetworkPermissions {
        origins,
        user_origins,
    } = network;
    let user_origin_bytes = match user_origins {
        Some(UserOrigins {
            max,
            allow_loopback,
        }) => std::mem::size_of_val(max) + std::mem::size_of_val(allow_loopback),
        None => 0,
    };
    let StoragePermissions {
        private: _,
        public_read: _,
        public_write: _,
    } = storage;
    let ResourceLimits {
        fuel_per_call: _,
        memory_mib: _,
        wall_ms: _,
        table_elements: _,
        output_bytes: _,
    } = wasm;
    let LoopLimits {
        max_iterations: _,
        max_wall_ms: _,
        max_cost_units: _,
    } = loops;
    let mut total = 1_u64;
    let mut add = |bytes: usize| -> Result<(), GateError> {
        total = total
            .checked_add(u64::try_from(bytes).map_err(|_| GateError::Unavailable)?)
            .ok_or(GateError::Unavailable)?;
        Ok(())
    };
    add(user_origin_bytes)?;
    for set in [origins, proc, host_io, ports] {
        add(set.len().checked_mul(1024).ok_or(GateError::Unavailable)?)?;
        for value in set {
            add(value.capacity())?;
        }
    }
    add(fs
        .capacity()
        .checked_mul(std::mem::size_of::<FsPermission>())
        .ok_or(GateError::Unavailable)?)?;
    for FsPermission {
        root,
        read: _,
        write: _,
    } in fs
    {
        add(root.capacity())?;
    }
    add(secrets
        .capacity()
        .checked_mul(std::mem::size_of::<SecretPermission>())
        .ok_or(GateError::Unavailable)?)?;
    for SecretPermission {
        name,
        reveal: _,
        optional: _,
    } in secrets
    {
        add(name.capacity())?;
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retained_policy_charge_includes_spare_owned_capacity() {
        let mut p = PermissionSet::default();
        let mut command = String::with_capacity(8192);
        command.push('x');
        let string_capacity = command.capacity();
        p.proc.insert(command);
        p.fs = Vec::with_capacity(128);
        p.secrets = Vec::with_capacity(64);
        let owned_buffers = string_capacity
            + p.fs.capacity() * std::mem::size_of::<FsPermission>()
            + p.secrets.capacity() * std::mem::size_of::<SecretPermission>();
        assert!(retained_permission_bytes(&p).unwrap() >= owned_buffers as u64);
        assert!(retained_permission_bytes(&PermissionSet::default()).unwrap() > 0);
    }
}
