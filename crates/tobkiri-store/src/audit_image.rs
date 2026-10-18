//! Bounded identity of all SQLite values previously verified in one connection.
//!
//! Every append still scans all rows. A matching image only reuses their prior
//! semantic validation; it is neither a tail-only check nor a persistent trust
//! cache. Storage classes, lengths and all quota metadata participate.

use crate::StoreError;
use rusqlite::{OptionalExtension, types::ValueRef};
use sha2::{Digest as _, Sha256};
use tobkiri_canon::Digest;

#[derive(Clone)]
pub(super) struct VerifiedImage {
    hash: Sha256,
    rows: u64,
    pub(super) last: Option<(u64, Digest)>,
}

impl VerifiedImage {
    pub(super) fn scan(connection: &rusqlite::Connection) -> Result<Self, StoreError> {
        #[cfg(feature = "closed-request-metrics")]
        let observation = tobkiri_gate::closed_metrics::current_span(
            tobkiri_gate::closed_metrics::Metric::AuditImageScan,
        );
        let result = Self::scan_inner(
            connection,
            #[cfg(feature = "closed-request-metrics")]
            &observation,
        );
        #[cfg(feature = "closed-request-metrics")]
        observation.finish();
        result
    }

    fn scan_inner(
        connection: &rusqlite::Connection,
        #[cfg(feature = "closed-request-metrics")] observation: &tobkiri_gate::closed_metrics::Span,
    ) -> Result<Self, StoreError> {
        let mut hash = Sha256::new();
        hash.update(b"tobkiri.audit.sqlite-value-image.v1\0");
        let mut image = Self {
            hash,
            rows: 0,
            last: None,
        };
        let mut statement = connection.prepare(
            "SELECT seq,prev_digest,digest,body,writer,minute,size,event_id FROM audit ORDER BY seq",
        )?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            // Match the full verifier's integrity classification before the
            // typed tail query can turn a corrupt sequence into persistence.
            crate::sql_u64(row, 0)
                .map_err(|_| StoreError::Integrity(tobkiri_gate::IntegrityFaultCode::AuditChain))?;
            let fields = [
                row.get_ref(0)?,
                row.get_ref(1)?,
                row.get_ref(2)?,
                row.get_ref(3)?,
                row.get_ref(4)?,
                row.get_ref(5)?,
                row.get_ref(6)?,
                row.get_ref(7)?,
            ];
            #[cfg(feature = "closed-request-metrics")]
            {
                // SQLite value payload bytes only, not page I/O or RSS. This
                // loop already reads all eight fields; no second SQL/row walk.
                observation.add_units(1, 0);
                for value in &fields {
                    if let ValueRef::Text(bytes) | ValueRef::Blob(bytes) = value {
                        observation.add_units(0, bytes.len() as u64);
                    }
                }
            }
            image.append_row(fields)?;
        }
        let last: Option<(u64, String)> = connection
            .query_row(
                "SELECT seq,digest FROM audit ORDER BY seq DESC LIMIT 1",
                [],
                |row| Ok((crate::sql_u64(row, 0)?, row.get(1)?)),
            )
            .optional()?;
        image.last = last
            .map(|(seq, digest)| {
                Digest::parse(&digest)
                    .map(|digest| (seq, digest))
                    .map_err(|_| {
                        StoreError::Integrity(tobkiri_gate::IntegrityFaultCode::AuditChain)
                    })
            })
            .transpose()?;
        Ok(image)
    }

    pub(super) fn matches(&self, previous: &Self) -> bool {
        self.rows == previous.rows
            && self.last == previous.last
            && self.hash.clone().finalize() == previous.hash.clone().finalize()
    }

    /// Extend only a locally validated INSERT candidate, before its commit.
    pub(super) fn append_row(&mut self, fields: [ValueRef<'_>; 8]) -> Result<(), StoreError> {
        self.hash.update([0xff]);
        for (index, value) in fields.into_iter().enumerate() {
            self.hash.update([index as u8]);
            match value {
                ValueRef::Null => self.hash.update([0]),
                ValueRef::Integer(value) => {
                    self.hash.update([1]);
                    self.hash.update(value.to_le_bytes());
                }
                ValueRef::Real(value) => {
                    self.hash.update([2]);
                    self.hash.update(value.to_bits().to_le_bytes());
                }
                ValueRef::Text(value) => self.bytes(3, value)?,
                ValueRef::Blob(value) => self.bytes(4, value)?,
            }
        }
        self.rows = self.rows.checked_add(1).ok_or(StoreError::Quota)?;
        Ok(())
    }

    fn bytes(&mut self, tag: u8, value: &[u8]) -> Result<(), StoreError> {
        self.hash.update([tag]);
        let length = u64::try_from(value.len()).map_err(|_| StoreError::Quota)?;
        self.hash.update(length.to_le_bytes());
        self.hash.update(value);
        Ok(())
    }
}
