//! Descriptive approval provenance retained by the original live dispatch.
//! This is neither a new approval nor authority independent of that dispatch.
use crate::{ApprovalPrompt, GateError, Risk};
use sha2::{Digest as _, Sha256};
use std::io::{self, Write};
use tobkiri_canon::Digest;

/// How the original Gate accepted its trusted approval answer. This does not
/// identify a GUI surface or replace verification of a live Core invocation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TrustedApprovalMethod {
    Prompt,
    DeviceAttestation,
}

/// Immutable description of an answer accepted for the exact original prompt.
/// There is no guest/Serde constructor. Copying this description never proves
/// liveness, parent ancestry, current epoch, funding or permission to dispatch.
/// Native consumers must authenticate it through the SAME live Core frame.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TrustedApprovalEvidence {
    snapshot: Digest,
    method: TrustedApprovalMethod,
    risk: Risk,
}

impl TrustedApprovalEvidence {
    pub(crate) fn accepted(snapshot: Digest, method: TrustedApprovalMethod, risk: Risk) -> Self {
        Self {
            snapshot,
            method,
            risk,
        }
    }

    /// Complete original typed-prompt commitment, not a Pack policy digest.
    pub fn snapshot_digest(&self) -> &Digest {
        &self.snapshot
    }

    /// Answer channel accepted by the original Gate verification.
    pub fn method(&self) -> TrustedApprovalMethod {
        self.method
    }

    /// Risk shown by the original prompt, including committed launch overrides.
    pub fn risk(&self) -> Risk {
        self.risk
    }
}

impl ApprovalPrompt {
    /// Commitment to the complete typed prompt passed to TrustedPrompt. Its
    /// versioned JSON serialization is streamed into SHA-256 without creating
    /// another prompt/JSON/CanonValue allocation. This is NOT a legacy Pack
    /// policy digest, a device signature, or proof that the prompt was approved.
    pub fn snapshot_digest(&self) -> Result<Digest, GateError> {
        let mut writer = SnapshotWriter(Sha256::new());
        writer.0.update(b"tobkiri.trusted-approval-prompt/1\0");
        serde_json::to_writer(&mut writer, self)
            .map_err(|_| GateError::Invalid("approval snapshot serialization".into()))?;
        let hash: [u8; 32] = writer.0.finalize().into();
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut encoded = [0u8; 71];
        encoded[..7].copy_from_slice(b"sha256:");
        for (index, byte) in hash.into_iter().enumerate() {
            encoded[7 + index * 2] = HEX[usize::from(byte >> 4)];
            encoded[8 + index * 2] = HEX[usize::from(byte & 15)];
        }
        let encoded = std::str::from_utf8(&encoded)
            .map_err(|_| GateError::Invalid("approval snapshot encoding".into()))?;
        Digest::parse(encoded).map_err(|_| GateError::Invalid("approval snapshot digest".into()))
    }
}

struct SnapshotWriter(Sha256);

impl Write for SnapshotWriter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{EffectRequest, GrantId, Principal};
    use tobkiri_canon::ids::{PackId, ProfileId, RequestId};

    #[test]
    fn snapshot_binds_every_prompt_field_including_risk_and_summary() {
        let original = ApprovalPrompt {
            reservation: GrantId("grant_snapshot".into()),
            profile: ProfileId::parse("prof_snapshot").unwrap(),
            principal: Principal::User,
            request: RequestId::parse("req_snapshot").unwrap(),
            effect: EffectRequest::Clock,
            args_digest: Digest::hash(b"original arguments"),
            security_epoch: 4,
            risk: Risk::Normal,
            summary: "Original prompt \"quoted\" 日本語\n".into(),
        };
        let expected = original.snapshot_digest().unwrap();
        assert_eq!(expected, original.clone().snapshot_digest().unwrap());
        let mutations: [fn(&mut ApprovalPrompt); 9] = [
            |p| p.reservation = GrantId("grant_other".into()),
            |p| p.profile = ProfileId::parse("prof_other").unwrap(),
            |p| {
                p.principal = Principal::Input {
                    source: "input_other".parse().unwrap(),
                }
            },
            |p| p.request = RequestId::parse("req_other").unwrap(),
            |p| {
                p.effect = EffectRequest::Native {
                    pack: PackId::parse("local.other").unwrap(),
                }
            },
            |p| p.args_digest = Digest::hash(b"changed arguments"),
            |p| p.security_epoch += 1,
            |p| p.risk = Risk::Dangerous,
            |p| p.summary.push('!'),
        ];
        for mutate in mutations {
            let mut changed = original.clone();
            mutate(&mut changed);
            assert_ne!(expected, changed.snapshot_digest().unwrap());
        }
    }
}
