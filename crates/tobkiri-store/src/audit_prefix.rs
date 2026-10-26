//! Constant-size evidence for an already verified audit prefix. This is not a
//! database-change counter: every use scans all five chain columns in the SAME
//! SQLite snapshot. Only an identical, typed and length-framed SHA-256 proof
//! permits skipping the old prefix's JSON decode/canonical hash work.

use crate::StoreError;
use rusqlite::{Connection, types::ValueRef};
use sha2::{Digest as _, Sha256};
use tobkiri_canon::{Digest, ids::ProfileId};
use tobkiri_gate::IntegrityFaultCode;

#[cfg(test)]
#[path = "audit_prefix_hash_tests.rs"]
mod hash_tests;

#[derive(Clone)]
pub(super) struct VerifiedPrefix {
    state: Sha256,
    rows: u64,
    tail: Option<[u8; 71]>,
}

fn integrity() -> StoreError {
    StoreError::Integrity(IntegrityFaultCode::AuditChain)
}

fn digest_bytes(value: ValueRef<'_>) -> Result<&[u8; 71], StoreError> {
    match value {
        ValueRef::Text(bytes)
            if bytes.len() == 71
                && bytes.starts_with(b"sha256:")
                && bytes[7..]
                    .iter()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)) =>
        {
            bytes.try_into().map_err(|_| integrity())
        }
        _ => Err(integrity()),
    }
}

impl VerifiedPrefix {
    fn empty(profile: &ProfileId) -> Self {
        let mut state = Sha256::new();
        state.update(b"tobkiri.audit.verified-prefix.v1\0");
        state.update((profile.as_str().len() as u64).to_be_bytes());
        state.update(profile.as_str().as_bytes());
        Self {
            state,
            rows: 0,
            tail: None,
        }
    }

    /// Borrow SQLite bytes; never copy or retain historical bodies/rows.
    pub(super) fn scan(conn: &Connection, profile: &ProfileId) -> Result<Self, StoreError> {
        let mut proof = Self::empty(profile);
        let mut statement =
            conn.prepare("SELECT seq,prev_digest,digest,body,event_id FROM audit ORDER BY seq")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let seq = match row.get_ref(0)? {
                ValueRef::Integer(seq) if seq > 0 => seq,
                _ => return Err(integrity()),
            };
            let prev = digest_bytes(row.get_ref(1)?)?;
            let digest = digest_bytes(row.get_ref(2)?)?;
            let body = match row.get_ref(3)? {
                ValueRef::Blob(body) if body.len() <= 64 * 1024 => body,
                _ => return Err(integrity()),
            };
            let event_id = match row.get_ref(4)? {
                ValueRef::Null => None,
                value => Some(digest_bytes(value)?),
            };
            proof.push(seq, prev, digest, body, event_id)?;
        }
        Ok(proof)
    }

    // Column index, SQLite type and length prevent concatenation, NULL/empty,
    // TEXT/BLOB or column-boundary aliases. No SQL coercions participate.
    fn field_header(index: u8, sql_type: u8, length: u64) -> [u8; 10] {
        let mut header = [0; 10];
        header[0] = index;
        header[1] = sql_type;
        header[2..].copy_from_slice(&length.to_be_bytes());
        header
    }

    fn push(
        &mut self,
        seq: i64,
        prev: &[u8; 71],
        digest: &[u8; 71],
        body: &[u8],
        event_id: Option<&[u8; 71]>,
    ) -> Result<(), StoreError> {
        let count = self.rows.checked_add(1).ok_or_else(integrity)?;
        if u64::try_from(seq).ok() != Some(count) {
            return Err(integrity());
        }
        // Scanned digests retain all checks above; expected digests have the
        // same fixed size in Canon's Digest. Only SHA update chunking changes:
        // marker (4) + sequence (10+8) + two digests (2*(10+71)) + body
        // header (10) = 194 bytes. Historical bodies remain borrowed.
        let mut prefix = [0; 194];
        prefix[..4].copy_from_slice(b"row\0");
        prefix[4..14].copy_from_slice(&Self::field_header(0, 1, 8));
        prefix[14..22].copy_from_slice(&seq.to_be_bytes());
        prefix[22..32].copy_from_slice(&Self::field_header(1, 3, 71));
        prefix[32..103].copy_from_slice(prev);
        prefix[103..113].copy_from_slice(&Self::field_header(2, 3, 71));
        prefix[113..184].copy_from_slice(digest);
        prefix[184..].copy_from_slice(&Self::field_header(3, 4, body.len() as u64));
        self.state.update(prefix);
        self.state.update(body);
        match event_id {
            Some(id) => {
                let mut suffix = [0; 81];
                suffix[..10].copy_from_slice(&Self::field_header(4, 3, 71));
                suffix[10..].copy_from_slice(id);
                self.state.update(suffix);
            }
            None => self.state.update(Self::field_header(4, 0, 0)),
        }
        self.rows = count;
        self.tail = Some(*digest);
        Ok(())
    }

    /// Expected extension uses the exact canonical bytes bound to INSERT.
    /// The caller must rescan and match actual stored rows before publishing.
    pub(super) fn extend(
        &mut self,
        seq: i64,
        prev: &Digest,
        digest: &Digest,
        body: &[u8],
        event_id: Option<&Digest>,
    ) -> Result<(), StoreError> {
        self.push(
            seq,
            prev.as_str()
                .as_bytes()
                .try_into()
                .map_err(|_| integrity())?,
            digest
                .as_str()
                .as_bytes()
                .try_into()
                .map_err(|_| integrity())?,
            body,
            event_id
                .map(|id| id.as_str().as_bytes().try_into().map_err(|_| integrity()))
                .transpose()?,
        )
    }

    pub(super) fn matches(&self, other: &Self) -> bool {
        self.rows == other.rows
            && self.tail == other.tail
            && self.state.clone().finalize() == other.state.clone().finalize()
    }
}
