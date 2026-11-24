//! Fixed host-session identity housekeeping, outside guest effect dispatch.

use crate::{Core, CoreError, Digest};

impl Core {
    /// Create a fresh 256-bit process-session namespace for trusted transport.
    ///
    /// Hosts bind this identifier, generation, receiver and ticket to every ACK.
    /// It is not bearer authority, a guest randomness interface or an OS/VM
    /// rollback defense. An entropy failure refuses session construction.
    pub fn new_transport_namespace() -> Result<Digest, CoreError> {
        const DOMAIN: &[u8] = b"tobkiri.host-transport-session/v1\0";
        let mut material = [0_u8; DOMAIN.len() + 32];
        material[..DOMAIN.len()].copy_from_slice(DOMAIN);
        getrandom::fill(&mut material[DOMAIN.len()..])
            .map_err(|_| CoreError::Gate(tobkiri_gate::GateError::Unavailable))?;
        Ok(Digest::hash(&material))
    }
}
