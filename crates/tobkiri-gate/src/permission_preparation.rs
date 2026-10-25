//! In-memory preparation retains the original native decision and reservation.
//! No policy row or successful terminal is published until ordinary commit.
//! A batch commits permission effects together, without making a durable
//! whole-Profile save decision or committing its head and security epoch.
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

/// Borrowed native facts for the Store's existing permission-effect protocol.
/// The enclosing batch, not these descriptive fields, supplies Gate custody.
pub struct PermissionCommit<'a> {
    pub reservation: &'a Reservation,
    pub expires_unix_ms: u64,
    pub effect: PermissionEffect<'a>,
}

/// Gate-only construction retains the original preparations throughout the
/// synchronous Store transaction. No cloned policy array or serialized receipt
/// is created. This is not authority supplied by a Pack or a UI payload.
pub struct PermissionApprovalBatch<'a, 'source> {
    approvals: &'a [PreparedPermissionApproval<'source>],
    expires_unix_ms: u64,
}
impl PermissionApprovalBatch<'_, '_> {
    /// Recheck the same original cancellation and monotonic deadline even if
    /// acquiring the Store transaction waited for another writer.
    pub fn check_active(&self) -> Result<(), GateError> {
        self.approvals
            .first()
            .ok_or_else(|| GateError::Invalid("empty permission batch".into()))?
            .cx
            .check_active()
    }

    pub fn iter(&self) -> impl Iterator<Item = PermissionCommit<'_>> {
        self.approvals.iter().map(|approval| PermissionCommit {
            reservation: &approval.reservation,
            expires_unix_ms: self.expires_unix_ms,
            effect: approval.target.effect(&approval.permissions),
        })
    }
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

    /// Install all approvals from this exact Gate and original context in one
    /// Store transaction. A pre-commit failure leaves every policy mutation
    /// uncommitted; dropping the retained originals uses ordinary abandonment.
    /// Audit delivery follows the durable terminals and can still fail after
    /// the batch committed. Neither result is a whole-Profile commit receipt.
    pub fn commit_all(approvals: Vec<Self>) -> Result<(), GateError> {
        let first = approvals
            .first()
            .ok_or_else(|| GateError::Invalid("empty permission batch".into()))?;
        for approval in &approvals {
            if !std::ptr::eq(approval.gate, first.gate) || !std::ptr::eq(approval.cx, first.cx) {
                return Err(GateError::Denied(
                    "permission batch has different native custody".into(),
                ));
            }
        }
        first.cx.check_active()?;
        if first.gate.epoch(first.cx.profile())? != first.cx.security_epoch() {
            return Err(GateError::StaleEpoch);
        }
        for approval in &approvals {
            approval.cx.check_active()?;
            approval
                .gate
                .event(&approval.reservation, approval.target.event_kind(), None)?;
            approval.gate.approvals.dispatch(&approval.reservation.id)?;
        }
        first.cx.check_active()?;
        first
            .gate
            .approvals
            .commit_permission_batch(&PermissionApprovalBatch {
                approvals: &approvals,
                expires_unix_ms: deadline_unix_ms(first.cx.deadline())?,
            })?;
        // Do not allocate a second terminal array. Original reservations retain
        // their already admitted terminal slots and audit custody throughout.
        for approval in &approvals {
            let terminal = approval
                .gate
                .approvals
                .terminal_delivery(&approval.reservation.id)?
                .ok_or(GateError::Unavailable)?;
            approval.gate.deliver_terminal(&terminal)?;
            if terminal.decision.outcome != Outcome::Succeeded {
                return Err(GateError::Deadline);
            }
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
