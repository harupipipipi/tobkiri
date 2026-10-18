//! Pack-owned, revisioned Run storage over the ordinary typed SQL 1.1 port.
//!
//! This module has no provider, workflow, KV, clock or retry callback. Every
//! method closes its transaction before returning. Only a newly inserted claim
//! whose commit was acknowledged can produce a dispatch permit. A saved claim,
//! matching readback, rollback, timeout or unknown commit cannot produce one.
//!
//! Append-only rows are an invariant of this API and its UNIQUE constraints,
//! not protection against malicious code with the same private Store owner.
//! The caller validates workflow semantics, exact captured bindings, accounting
//! and legacy execution retirement; this generic layer validates storage types,
//! bounds, hashes, identities, revisions and claim transitions.

use sha2::{Digest as _, Sha256};
use std::{collections::BTreeSet, fmt};

/// Raw chunk bound. Storage uses 2x canonical HEX TEXT, avoiding Core's BLOB
/// integer-array ABI path while staying below its 1 MiB per-cell limit.
pub const CHUNK_BYTES: usize = 64 * 1024;
/// Explicit repository-record bound; this is not a model output-token limit.
pub const MAX_RECORD_BYTES: usize = 8 * 1024 * 1024;
/// Bound a short transition's number of settlement/legacy claim records.
pub const MAX_TRANSITION_CLAIMS: usize = 256;
const SCHEMA_VERSION: i64 = 1;
const MAX_ID_BYTES: usize = 256;
const CHUNK_BATCH_ROWS: u32 = 4;

/// Lossless cells supported by this repository. No floating-point counters.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SqlCell {
    Null,
    Integer(i64),
    Text(String),
    Blob(Vec<u8>),
}

/// Query results must preserve exact column order and typed cells.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SqlRows {
    pub columns: Vec<String>,
    pub rows: Vec<Vec<SqlCell>>,
}

/// Closed adapter failures. Never include SQL/HTTP payloads or credentials.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SqlErrorKind {
    Conflict,
    Denied,
    Cancelled,
    Limit,
    Unavailable,
    Invalid,
    Internal,
    CommitUnknown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SqlError {
    pub kind: SqlErrorKind,
}

impl SqlError {
    pub const fn new(kind: SqlErrorKind) -> Self {
        Self { kind }
    }

    pub const fn commit_unknown() -> Self {
        Self::new(SqlErrorKind::CommitUnknown)
    }
}

/// The adapter uses Core's sql-begin resource, never raw BEGIN or a KV fallback.
/// The transaction owns its resource independently of the borrow of `self`.
pub trait SqlDatabase {
    type Transaction: SqlTransaction;
    fn begin(&mut self) -> std::result::Result<Self::Transaction, SqlError>;
}

/// Every method must preserve the ordinary Store/Gate/epoch/deadline checks.
/// `exec` uses forbid-rows and returns an exact checked affected-row count.
/// `commit` returns Ok only for a confirmed Core commit. Failed commit calls
/// must be classified as CommitUnknown if physical completion is uncertain.
/// Drop must retain the Core resource's existing rollback/custody semantics.
pub trait SqlTransaction {
    fn exec(&mut self, statement: &str, params: &[SqlCell]) -> std::result::Result<u64, SqlError>;
    fn query(
        &mut self,
        statement: &str,
        params: &[SqlCell],
        max_rows: u32,
    ) -> std::result::Result<SqlRows, SqlError>;
    fn commit(self) -> std::result::Result<(), SqlError>;
    fn rollback(self) -> std::result::Result<(), SqlError>;
}

/// No error is an instruction to replay a claim or a provider call.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RepositoryError {
    Invalid,
    Limit,
    Schema,
    Integrity,
    Conflict,
    PendingGeneration,
    CommitUnknown,
    Storage(SqlErrorKind),
}

impl From<SqlError> for RepositoryError {
    fn from(error: SqlError) -> Self {
        match error.kind {
            SqlErrorKind::CommitUnknown => Self::CommitUnknown,
            SqlErrorKind::Conflict => Self::Conflict,
            kind => Self::Storage(kind),
        }
    }
}

impl fmt::Display for RepositoryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Invalid => "invalid run repository input",
            Self::Limit => "run repository bound exceeded",
            Self::Schema => "run repository schema mismatch",
            Self::Integrity => "run repository integrity mismatch",
            Self::Conflict => "run repository revision or identity conflict",
            Self::PendingGeneration => "generation reconciliation or authorization required",
            Self::CommitUnknown => "run repository commit or rollback is unconfirmed",
            Self::Storage(_) => "run repository storage operation failed",
        })
    }
}

impl std::error::Error for RepositoryError {}

type Result<T> = std::result::Result<T, RepositoryError>;

/// Exactly 64 lower-case hexadecimal bytes. The representation never changes
/// an existing workflow digest by adding a prefix or normalizing its input.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Digest(String);

impl Digest {
    pub fn parse(value: &str) -> Result<Self> {
        if value.len() != 64
            || !value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(RepositoryError::Invalid);
        }
        Ok(Self(value.to_owned()))
    }

    pub fn hash(bytes: &[u8]) -> Self {
        Self(format!("{:x}", Sha256::digest(bytes)))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunIdentity {
    pub run_id: String,
    pub run_request_digest: Digest,
}

/// A cursor is kept outside the serialized workflow Run by the integration.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Head {
    pub run_id: String,
    pub run_request_digest: Digest,
    pub revision: i64,
    pub snapshot_digest: Digest,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub head: Head,
    pub bytes: Vec<u8>,
}

/// The exact existing AttemptContext, independent of workflow.rs types.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GenerationClaim {
    pub run_id: String,
    pub run_request_digest: Digest,
    pub phase: String,
    pub generation_key: String,
    pub attempt_id: String,
    pub request_digest: Digest,
    pub route_digest: Digest,
}

/// A locally owned, one-use capability. Deliberately no Clone/Serialize/
/// Deserialize/Default constructor. A process crash loses it permanently.
#[derive(Debug)]
pub struct DispatchPermit {
    claim: GenerationClaim,
    committed_head: Head,
}

impl DispatchPermit {
    pub fn claim(&self) -> &GenerationClaim {
        &self.claim
    }

    pub fn committed_head(&self) -> &Head {
        &self.committed_head
    }

    /// Consume even a mismatched permit; it cannot be retried after this call.
    pub fn consume(self, expected: &GenerationClaim) -> Result<()> {
        if self.claim == *expected {
            Ok(())
        } else {
            Err(RepositoryError::Conflict)
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ResolutionDisposition {
    AcceptedCompletion,
    RejectedCompletion,
    TerminalContentUnavailable,
}

impl ResolutionDisposition {
    fn as_str(self) -> &'static str {
        match self {
            Self::AcceptedCompletion => "accepted_completion",
            Self::RejectedCompletion => "rejected_completion",
            Self::TerminalContentUnavailable => "terminal_content_unavailable",
        }
    }

    fn parse(value: &str) -> Result<Self> {
        match value {
            "accepted_completion" => Ok(Self::AcceptedCompletion),
            "rejected_completion" => Ok(Self::RejectedCompletion),
            "terminal_content_unavailable" => Ok(Self::TerminalContentUnavailable),
            _ => Err(RepositoryError::Integrity),
        }
    }
}

/// Opaque receipt bytes contain the domain's validated accounting/content/
/// provenance. This repository never infers terminal success or zero cost.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Resolution {
    pub claim: GenerationClaim,
    pub disposition: ResolutionDisposition,
    /// Exact latest SQL progress observed by the resolver. None requires that
    /// there is still no SQL progress; it never skips the comparison.
    pub expected_progress: Option<ProgressStamp>,
    pub receipt: Vec<u8>,
}

impl Resolution {
    /// Binds the identity and disposition as well as the exact receipt bytes.
    pub fn digest(&self) -> Digest {
        let sequence = self
            .expected_progress
            .as_ref()
            .map(|stamp| stamp.sequence.to_string())
            .unwrap_or_default();
        let progress_digest = self
            .expected_progress
            .as_ref()
            .map(|stamp| stamp.digest.as_str())
            .unwrap_or("");
        binding_digest(
            "deepthink.resolution.v1",
            &[
                self.claim.run_id.as_bytes(),
                self.claim.run_request_digest.as_str().as_bytes(),
                self.claim.phase.as_bytes(),
                self.claim.generation_key.as_bytes(),
                self.claim.attempt_id.as_bytes(),
                self.claim.request_digest.as_str().as_bytes(),
                self.claim.route_digest.as_str().as_bytes(),
                self.disposition.as_str().as_bytes(),
                sequence.as_bytes(),
                progress_digest.as_bytes(),
                &self.receipt,
            ],
        )
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredResolution {
    pub resolution: Resolution,
    pub digest: Digest,
    pub resolved_revision: i64,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProgressStamp {
    pub sequence: u64,
    pub digest: Digest,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ProgressRecord {
    pub stamp: ProgressStamp,
    pub bytes: Vec<u8>,
}

/// Adoption is deliberately separate from fresh creation. The caller must
/// have verified a real old-execution retirement receipt outside this module.
/// A user boolean, changed Profile epoch or two KV reads is not that proof.
/// This API records the evidence but does not authenticate its semantics.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LegacyAdoption {
    pub source_snapshot_digest: Digest,
    pub retirement_receipt: Vec<u8>,
    pub historical_claims: Vec<GenerationClaim>,
    pub settlements: Vec<Resolution>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SuccessorAuthorization {
    pub old_attempt_id: String,
    pub resolution_digest: Digest,
    /// Validated explicit User action and captured binding provenance.
    pub receipt: Vec<u8>,
}

/// Schema names are reserved only within this Pack's ordinary private Store.
const SCHEMA: &[(&str, &str)] = &[
    (
        "dt_repository_schema",
        "CREATE TABLE dt_repository_schema (slot TEXT PRIMARY KEY NOT NULL CHECK(slot='schema'), version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version=1), schema_digest TEXT NOT NULL)",
    ),
    (
        "dt_run_heads",
        "CREATE TABLE dt_run_heads (run_id TEXT PRIMARY KEY NOT NULL, run_request_digest TEXT NOT NULL, revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0), snapshot_digest TEXT NOT NULL)",
    ),
    (
        "dt_run_snapshots",
        "CREATE TABLE dt_run_snapshots (run_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0), previous_digest TEXT, snapshot_digest TEXT NOT NULL, byte_len INTEGER NOT NULL CHECK(typeof(byte_len)='integer' AND byte_len>0 AND byte_len<=8388608), chunk_count INTEGER NOT NULL CHECK(typeof(chunk_count)='integer' AND chunk_count>0 AND chunk_count<=128), PRIMARY KEY(run_id,revision), FOREIGN KEY(run_id) REFERENCES dt_run_heads(run_id))",
    ),
    (
        "dt_snapshot_chunks",
        "CREATE TABLE dt_snapshot_chunks (run_id TEXT NOT NULL, revision INTEGER NOT NULL, ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0 AND ordinal<128), payload TEXT NOT NULL CHECK(typeof(payload)='text' AND length(payload)>0 AND length(payload)<=131072 AND length(payload)%2=0), PRIMARY KEY(run_id,revision,ordinal), FOREIGN KEY(run_id,revision) REFERENCES dt_run_snapshots(run_id,revision))",
    ),
    (
        "dt_generation_claims",
        "CREATE TABLE dt_generation_claims (run_id TEXT NOT NULL, run_request_digest TEXT NOT NULL, phase TEXT NOT NULL, generation_key TEXT NOT NULL, attempt_id TEXT NOT NULL, request_digest TEXT NOT NULL, route_digest TEXT NOT NULL, claimed_revision INTEGER NOT NULL CHECK(typeof(claimed_revision)='integer' AND claimed_revision>=0), PRIMARY KEY(run_id,attempt_id), UNIQUE(run_id,generation_key), FOREIGN KEY(run_id) REFERENCES dt_run_heads(run_id))",
    ),
    (
        "dt_generation_resolutions",
        "CREATE TABLE dt_generation_resolutions (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, disposition TEXT NOT NULL CHECK(disposition IN ('accepted_completion','rejected_completion','terminal_content_unavailable')), receipt_digest TEXT NOT NULL, byte_len INTEGER NOT NULL CHECK(typeof(byte_len)='integer' AND byte_len>0 AND byte_len<=8388608), chunk_count INTEGER NOT NULL CHECK(typeof(chunk_count)='integer' AND chunk_count>0 AND chunk_count<=128), resolved_revision INTEGER NOT NULL CHECK(typeof(resolved_revision)='integer' AND resolved_revision>=0), progress_sequence INTEGER, progress_digest TEXT, PRIMARY KEY(run_id,attempt_id), FOREIGN KEY(run_id,attempt_id) REFERENCES dt_generation_claims(run_id,attempt_id))",
    ),
    (
        "dt_resolution_chunks",
        "CREATE TABLE dt_resolution_chunks (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0 AND ordinal<128), payload TEXT NOT NULL CHECK(typeof(payload)='text' AND length(payload)>0 AND length(payload)<=131072 AND length(payload)%2=0), PRIMARY KEY(run_id,attempt_id,ordinal), FOREIGN KEY(run_id,attempt_id) REFERENCES dt_generation_resolutions(run_id,attempt_id))",
    ),
    (
        "dt_successor_authorizations",
        "CREATE TABLE dt_successor_authorizations (run_id TEXT NOT NULL, old_attempt_id TEXT NOT NULL, new_attempt_id TEXT NOT NULL, resolution_digest TEXT NOT NULL, authorization_digest TEXT NOT NULL, receipt TEXT NOT NULL CHECK(typeof(receipt)='text' AND length(receipt)>0 AND length(receipt)<=131072 AND length(receipt)%2=0), authorized_revision INTEGER NOT NULL CHECK(typeof(authorized_revision)='integer' AND authorized_revision>0), PRIMARY KEY(run_id,old_attempt_id), UNIQUE(run_id,new_attempt_id), FOREIGN KEY(run_id,old_attempt_id) REFERENCES dt_generation_resolutions(run_id,attempt_id), FOREIGN KEY(run_id,new_attempt_id) REFERENCES dt_generation_claims(run_id,attempt_id))",
    ),
    (
        "dt_legacy_adoptions",
        "CREATE TABLE dt_legacy_adoptions (run_id TEXT PRIMARY KEY NOT NULL, source_snapshot_digest TEXT NOT NULL, retirement_digest TEXT NOT NULL, retirement_receipt TEXT NOT NULL CHECK(typeof(retirement_receipt)='text' AND length(retirement_receipt)>0 AND length(retirement_receipt)<=131072 AND length(retirement_receipt)%2=0), FOREIGN KEY(run_id) REFERENCES dt_run_heads(run_id))",
    ),
    (
        "dt_progress_heads",
        "CREATE TABLE dt_progress_heads (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(typeof(sequence)='integer' AND sequence>0), progress_digest TEXT NOT NULL, PRIMARY KEY(run_id,attempt_id), FOREIGN KEY(run_id,attempt_id) REFERENCES dt_generation_claims(run_id,attempt_id))",
    ),
    (
        "dt_progress_records",
        "CREATE TABLE dt_progress_records (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(typeof(sequence)='integer' AND sequence>0), progress_digest TEXT NOT NULL, byte_len INTEGER NOT NULL CHECK(typeof(byte_len)='integer' AND byte_len>0 AND byte_len<=8388608), chunk_count INTEGER NOT NULL CHECK(typeof(chunk_count)='integer' AND chunk_count>0 AND chunk_count<=128), PRIMARY KEY(run_id,attempt_id,sequence), FOREIGN KEY(run_id,attempt_id) REFERENCES dt_generation_claims(run_id,attempt_id))",
    ),
    (
        "dt_progress_chunks",
        "CREATE TABLE dt_progress_chunks (run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=0 AND ordinal<128), chunk_digest TEXT NOT NULL, payload TEXT NOT NULL CHECK(typeof(payload)='text' AND length(payload)>0 AND length(payload)<=131072 AND length(payload)%2=0), PRIMARY KEY(run_id,attempt_id,ordinal), FOREIGN KEY(run_id,attempt_id) REFERENCES dt_generation_claims(run_id,attempt_id))",
    ),
];

const SELECT_SCHEMA: &str = "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE tbl_name IN ('dt_repository_schema','dt_run_heads','dt_run_snapshots','dt_snapshot_chunks','dt_generation_claims','dt_generation_resolutions','dt_resolution_chunks','dt_successor_authorizations','dt_legacy_adoptions','dt_progress_heads','dt_progress_records','dt_progress_chunks') OR name IN ('dt_repository_schema','dt_run_heads','dt_run_snapshots','dt_snapshot_chunks','dt_generation_claims','dt_generation_resolutions','dt_resolution_chunks','dt_successor_authorizations','dt_legacy_adoptions','dt_progress_heads','dt_progress_records','dt_progress_chunks') ORDER BY type,name";
const SELECT_HEAD: &str =
    "SELECT run_request_digest,revision,snapshot_digest FROM dt_run_heads WHERE run_id=?";
const SELECT_CLAIM: &str = "SELECT run_request_digest,phase,generation_key,attempt_id,request_digest,route_digest,claimed_revision FROM dt_generation_claims WHERE run_id=? AND attempt_id=?";
const SELECT_BLOCKERS: &str = "SELECT c.attempt_id,r.disposition FROM dt_generation_claims c LEFT JOIN dt_generation_resolutions r ON r.run_id=c.run_id AND r.attempt_id=c.attempt_id LEFT JOIN dt_successor_authorizations a ON a.run_id=c.run_id AND a.old_attempt_id=c.attempt_id WHERE c.run_id=? AND (r.attempt_id IS NULL OR (r.disposition='terminal_content_unavailable' AND a.old_attempt_id IS NULL)) LIMIT 2";
const SELECT_RESOLUTION: &str = "SELECT disposition,receipt_digest,byte_len,chunk_count,resolved_revision,progress_sequence,progress_digest FROM dt_generation_resolutions WHERE run_id=? AND attempt_id=?";
const SELECT_PROGRESS: &str =
    "SELECT sequence,progress_digest FROM dt_progress_heads WHERE run_id=? AND attempt_id=?";
// Every join uses a primary key from the existing schema. Read the scalar
// preconditions together in the same transaction; do not aggregate away a
// missing claim, incomplete progress record or an already frozen resolution.
const SELECT_PROGRESS_PRECONDITIONS: &str = "SELECT h.run_request_digest AS run_request_digest,h.revision AS revision,h.snapshot_digest AS snapshot_digest,c.run_request_digest AS claim_run_request_digest,c.phase AS phase,c.generation_key AS generation_key,c.attempt_id AS attempt_id,c.request_digest AS request_digest,c.route_digest AS route_digest,c.claimed_revision AS claimed_revision,r.attempt_id AS resolved_attempt_id,p.sequence AS sequence,p.progress_digest AS progress_digest,d.progress_digest AS record_progress_digest,d.byte_len AS byte_len,d.chunk_count AS chunk_count FROM dt_run_heads h LEFT JOIN dt_generation_claims c ON c.run_id=h.run_id AND c.attempt_id=? LEFT JOIN dt_generation_resolutions r ON r.run_id=c.run_id AND r.attempt_id=c.attempt_id LEFT JOIN dt_progress_heads p ON p.run_id=c.run_id AND p.attempt_id=c.attempt_id LEFT JOIN dt_progress_records d ON d.run_id=p.run_id AND d.attempt_id=p.attempt_id AND d.sequence=p.sequence WHERE h.run_id=? LIMIT 2";
const PROGRESS_PRECONDITION_COLUMNS: &[&str] = &[
    "run_request_digest",
    "revision",
    "snapshot_digest",
    "claim_run_request_digest",
    "phase",
    "generation_key",
    "attempt_id",
    "request_digest",
    "route_digest",
    "claimed_revision",
    "resolved_attempt_id",
    "sequence",
    "progress_digest",
    "record_progress_digest",
    "byte_len",
    "chunk_count",
];

pub struct RunRepository<D: SqlDatabase> {
    database: D,
}

impl<D: SqlDatabase> RunRepository<D> {
    /// Creates the entire schema only if none of its objects exists. A partial,
    /// foreign, incompatible or tampered schema is never repaired implicitly.
    pub fn open(mut database: D) -> Result<Self> {
        transaction(&mut database, initialize_schema)?;
        Ok(Self { database })
    }

    /// Inspect an existing ledger without creating or repairing any object.
    /// This does not mint a dispatch permit or adopt a legacy checkpoint.
    pub fn open_existing(mut database: D) -> Result<Self> {
        transaction(&mut database, |tx| check_schema(tx, false))?;
        Ok(Self { database })
    }

    /// Ordinary STATUS may inspect a Store before its first managed Run exists.
    /// Only a completely absent ledger is None; any present schema must validate.
    /// This never creates schema or turns a failed read/commit into absence.
    pub fn open_existing_optional(mut database: D) -> Result<Option<Self>> {
        let present = transaction(&mut database, |tx| {
            let rows = read_schema(tx)?;
            if rows.is_empty() {
                return Ok(false);
            }
            validate_schema_rows(tx, rows)?;
            Ok(true)
        })?;
        Ok(present.then_some(Self { database }))
    }

    pub fn validate_schema(&mut self) -> Result<()> {
        transaction(&mut self.database, |tx| check_schema(tx, false))
    }

    pub fn load(&mut self, run_id: &str) -> Result<Option<Snapshot>> {
        validate_id(run_id)?;
        transaction(&mut self.database, |tx| {
            let Some(head) = read_head(tx, run_id)? else {
                return Ok(None);
            };
            let rows = query(
                tx,
                "SELECT previous_digest,snapshot_digest,byte_len,chunk_count FROM dt_run_snapshots WHERE run_id=? AND revision=?",
                &[text(run_id), SqlCell::Integer(head.revision)],
                2,
                &[
                    "previous_digest",
                    "snapshot_digest",
                    "byte_len",
                    "chunk_count",
                ],
            )?;
            let row = one(&rows)?;
            match (&row[0], head.revision) {
                (SqlCell::Null, 0) => {}
                (SqlCell::Text(previous), revision) if revision > 0 => {
                    stored_digest(previous)?;
                }
                _ => return Err(RepositoryError::Integrity),
            }
            if stored_digest(cell_text(&row[1])?)? != head.snapshot_digest {
                return Err(RepositoryError::Integrity);
            }
            let (byte_len, count) = record_dimensions(&row[2], &row[3])?;
            let bytes = read_chunks(
                tx,
                "SELECT COUNT(*) AS total FROM dt_snapshot_chunks WHERE run_id=? AND revision=?",
                "SELECT ordinal,payload FROM dt_snapshot_chunks WHERE run_id=? AND revision=? AND ordinal>=? AND ordinal<? ORDER BY ordinal",
                &[text(run_id), SqlCell::Integer(head.revision)],
                byte_len,
                count,
            )?;
            if Digest::hash(&bytes) != head.snapshot_digest {
                return Err(RepositoryError::Integrity);
            }
            Ok(Some(Snapshot { head, bytes }))
        })
    }

    /// Fresh Runs have no historical effects. An existing ledger conflicts;
    /// callers must not interpret load failure as permission to create anew.
    pub fn create_fresh(&mut self, identity: &RunIdentity, bytes: &[u8]) -> Result<Head> {
        validate_identity(identity)?;
        validate_record(bytes)?;
        transaction(&mut self.database, |tx| create_run(tx, identity, bytes))
    }

    /// Exact legacy bytes and all historical attempt identities are adopted in
    /// one transaction. No imported claim, even unresolved, yields a permit.
    pub fn adopt_legacy(
        &mut self,
        identity: &RunIdentity,
        bytes: &[u8],
        adoption: &LegacyAdoption,
    ) -> Result<Head> {
        validate_identity(identity)?;
        validate_record(bytes)?;
        validate_small_receipt(&adoption.retirement_receipt)?;
        if Digest::hash(bytes) != adoption.source_snapshot_digest {
            return Err(RepositoryError::Invalid);
        }
        if adoption.historical_claims.len() > MAX_TRANSITION_CLAIMS {
            return Err(RepositoryError::Limit);
        }
        validate_settlements(&adoption.settlements)?;
        let mut attempt_ids = BTreeSet::new();
        let mut generation_keys = BTreeSet::new();
        for claim in &adoption.historical_claims {
            validate_claim(claim)?;
            if claim.run_id != identity.run_id
                || claim.run_request_digest != identity.run_request_digest
                || !attempt_ids.insert(&claim.attempt_id)
                || !generation_keys.insert(&claim.generation_key)
            {
                return Err(RepositoryError::Invalid);
            }
        }
        transaction(&mut self.database, |tx| {
            let head = create_run(tx, identity, bytes)?;
            for claim in &adoption.historical_claims {
                insert_claim(tx, &head, claim)?;
            }
            for settlement in &adoption.settlements {
                insert_resolution(tx, &head, settlement)?;
            }
            changed_one(tx.exec("INSERT INTO dt_legacy_adoptions(run_id,source_snapshot_digest,retirement_digest,retirement_receipt) VALUES (?,?,?,?)", &[text(&identity.run_id), text(adoption.source_snapshot_digest.as_str()), text(Digest::hash(&adoption.retirement_receipt).as_str()), bytes_cell(&adoption.retirement_receipt)])?)?;
            Ok(head)
        })
    }

    /// Completion/rejection receipts and the next snapshot share the head CAS.
    /// Duplicate settlements conflict, including identical receipt replay.
    /// Read existing receipts explicitly instead of double-counting them.
    pub fn commit_snapshot(
        &mut self,
        expected: &Head,
        bytes: &[u8],
        settlements: &[Resolution],
    ) -> Result<Head> {
        validate_head(expected)?;
        validate_record(bytes)?;
        validate_settlements(settlements)?;
        let next = next_head(expected, bytes)?;
        transaction(&mut self.database, |tx| {
            require_head(tx, expected)?;
            for settlement in settlements {
                insert_resolution(tx, &next, settlement)?;
            }
            append_snapshot(tx, &next, Some(&expected.snapshot_digest), bytes)?;
            advance_head(tx, expected, &next)?;
            Ok(next)
        })
    }

    /// No pending generation or unauthorised unavailable-content resolution
    /// may be bypassed by the ordinary fresh/retry claim path.
    pub fn claim_generation(
        &mut self,
        expected: &Head,
        bytes: &[u8],
        claim: &GenerationClaim,
    ) -> Result<(Head, DispatchPermit)> {
        validate_transition(expected, bytes, claim)?;
        let next = next_head(expected, bytes)?;
        let committed = transaction(&mut self.database, |tx| {
            require_head(tx, expected)?;
            if !blocking_claims(tx, &expected.run_id)?.is_empty() {
                return Err(RepositoryError::PendingGeneration);
            }
            insert_claim(tx, &next, claim)?;
            append_snapshot(tx, &next, Some(&expected.snapshot_digest), bytes)?;
            advance_head(tx, expected, &next)?;
            Ok(next)
        })?;
        // This is the only minting point for an ordinary claim: after commit.
        let permit = DispatchPermit {
            claim: claim.clone(),
            committed_head: committed.clone(),
        };
        Ok((committed, permit))
    }

    /// One explicit successor of an unavailable-output attempt. It does not
    /// authorize replay of any pending/completed tool effect. The integration
    /// validates that separate domain condition before entering this method.
    pub fn claim_successor(
        &mut self,
        expected: &Head,
        bytes: &[u8],
        authorization: &SuccessorAuthorization,
        successor: &GenerationClaim,
    ) -> Result<(Head, DispatchPermit)> {
        validate_transition(expected, bytes, successor)?;
        validate_id(&authorization.old_attempt_id)?;
        validate_small_receipt(&authorization.receipt)?;
        if authorization.old_attempt_id == successor.attempt_id {
            return Err(RepositoryError::Invalid);
        }
        let next = next_head(expected, bytes)?;
        let committed = transaction(&mut self.database, |tx| {
            require_head(tx, expected)?;
            let old = read_resolution(tx, &expected.run_id, &authorization.old_attempt_id)?
                .ok_or(RepositoryError::Conflict)?;
            if old.resolution.disposition != ResolutionDisposition::TerminalContentUnavailable
                || old.digest != authorization.resolution_digest
                || old.resolution.claim.run_request_digest != successor.run_request_digest
                || old.resolution.claim.route_digest != successor.route_digest
                || old.resolution.claim.phase != successor.phase
                || old.resolution.claim.generation_key == successor.generation_key
            {
                return Err(RepositoryError::Conflict);
            }
            let blockers = blocking_claims(tx, &expected.run_id)?;
            if blockers
                != vec![(
                    authorization.old_attempt_id.clone(),
                    Some(ResolutionDisposition::TerminalContentUnavailable),
                )]
            {
                return Err(RepositoryError::PendingGeneration);
            }
            insert_claim(tx, &next, successor)?;
            let digest = binding_digest(
                "deepthink.successor-authorization.v1",
                &[
                    expected.run_id.as_bytes(),
                    expected.run_request_digest.as_str().as_bytes(),
                    authorization.old_attempt_id.as_bytes(),
                    successor.attempt_id.as_bytes(),
                    successor.generation_key.as_bytes(),
                    successor.phase.as_bytes(),
                    successor.request_digest.as_str().as_bytes(),
                    successor.route_digest.as_str().as_bytes(),
                    authorization.resolution_digest.as_str().as_bytes(),
                    &authorization.receipt,
                ],
            );
            changed_one(tx.exec("INSERT INTO dt_successor_authorizations(run_id,old_attempt_id,new_attempt_id,resolution_digest,authorization_digest,receipt,authorized_revision) VALUES (?,?,?,?,?,?,?)", &[text(&expected.run_id), text(&authorization.old_attempt_id), text(&successor.attempt_id), text(authorization.resolution_digest.as_str()), text(digest.as_str()), bytes_cell(&authorization.receipt), SqlCell::Integer(next.revision)])?)?;
            append_snapshot(tx, &next, Some(&expected.snapshot_digest), bytes)?;
            advance_head(tx, expected, &next)?;
            Ok(next)
        })?;
        // An existing authorization/readback can never reach this mint point.
        let permit = DispatchPermit {
            claim: successor.clone(),
            committed_head: committed.clone(),
        };
        Ok((committed, permit))
    }

    pub fn read_resolution(
        &mut self,
        run_id: &str,
        attempt_id: &str,
    ) -> Result<Option<StoredResolution>> {
        validate_id(run_id)?;
        validate_id(attempt_id)?;
        transaction(&mut self.database, |tx| {
            read_resolution(tx, run_id, attempt_id)
        })
    }

    /// Append an immutable progress stamp and CAS-replace only changed latest
    /// payload chunks, without advancing/copying the Run snapshot. Historical
    /// stamp hashes remain; intermediate full-prefix payloads are not an archive.
    /// This avoids quadratic disk usage and keeps the existing Store quota.
    /// The domain validates semantic monotonicity. Resolution freezes payload.
    pub fn save_progress(
        &mut self,
        expected: &Head,
        claim: &GenerationClaim,
        sequence: u64,
        bytes: &[u8],
    ) -> Result<ProgressStamp> {
        validate_transition(expected, bytes, claim)?;
        let sequence_sql = checked_sequence(sequence)?;
        let next = ProgressStamp {
            sequence,
            digest: Digest::hash(bytes),
        };
        transaction(&mut self.database, |tx| {
            let (previous, dimensions) =
                progress_save_preconditions(tx, expected, claim, sequence)?;
            let old_chunks = progress_chunk_metadata(tx, claim, dimensions)?;
            changed_one(tx.exec("INSERT INTO dt_progress_records(run_id,attempt_id,sequence,progress_digest,byte_len,chunk_count) VALUES (?,?,?,?,?,?)", &[text(&claim.run_id), text(&claim.attempt_id), SqlCell::Integer(sequence_sql), text(next.digest.as_str()), SqlCell::Integer(bytes.len() as i64), SqlCell::Integer(bytes.len().div_ceil(CHUNK_BYTES) as i64)])?)?;
            for (ordinal, chunk) in bytes.chunks(CHUNK_BYTES).enumerate() {
                let digest = Digest::hash(chunk);
                match old_chunks.get(ordinal) {
                    Some((old_digest, old_len)) if old_digest == &digest && *old_len == chunk.len() => {},
                    Some((old_digest, old_len)) => changed_one(tx.exec("UPDATE dt_progress_chunks SET chunk_digest=?,payload=? WHERE run_id=? AND attempt_id=? AND ordinal=? AND chunk_digest=? AND length(payload)=?", &[text(digest.as_str()), bytes_cell(chunk), text(&claim.run_id), text(&claim.attempt_id), SqlCell::Integer(ordinal as i64), text(old_digest.as_str()), SqlCell::Integer((*old_len * 2) as i64)])?)?,
                    None => changed_one(tx.exec("INSERT INTO dt_progress_chunks(run_id,attempt_id,ordinal,chunk_digest,payload) VALUES (?,?,?,?,?)", &[text(&claim.run_id), text(&claim.attempt_id), SqlCell::Integer(ordinal as i64), text(digest.as_str()), bytes_cell(chunk)])?)?,
                }
            }
            let new_count = bytes.len().div_ceil(CHUNK_BYTES);
            if old_chunks.len() > new_count {
                let deleted = tx.exec(
                    "DELETE FROM dt_progress_chunks WHERE run_id=? AND attempt_id=? AND ordinal>=?",
                    &[
                        text(&claim.run_id),
                        text(&claim.attempt_id),
                        SqlCell::Integer(new_count as i64),
                    ],
                )?;
                if deleted != (old_chunks.len() - new_count) as u64 {
                    return Err(RepositoryError::Conflict);
                }
            }
            match previous {
                None => changed_one(tx.exec("INSERT INTO dt_progress_heads(run_id,attempt_id,sequence,progress_digest) VALUES (?,?,?,?)", &[text(&claim.run_id), text(&claim.attempt_id), SqlCell::Integer(sequence_sql), text(next.digest.as_str())])?)?,
                Some(old) => changed_one(tx.exec("UPDATE dt_progress_heads SET sequence=?,progress_digest=? WHERE run_id=? AND attempt_id=? AND sequence=? AND progress_digest=?", &[SqlCell::Integer(sequence_sql), text(next.digest.as_str()), text(&claim.run_id), text(&claim.attempt_id), SqlCell::Integer(checked_sequence(old.sequence)?), text(old.digest.as_str())])?)?,
            }
            Ok(next)
        })
    }

    pub fn load_progress(
        &mut self,
        expected: &Head,
        claim: &GenerationClaim,
    ) -> Result<Option<ProgressRecord>> {
        validate_head(expected)?;
        validate_claim(claim)?;
        transaction(&mut self.database, |tx| {
            require_head(tx, expected)?;
            require_claim(tx, expected, claim)?;
            let Some(stamp) = read_progress_stamp(tx, &claim.run_id, &claim.attempt_id)? else {
                return Ok(None);
            };
            let (byte_len, count) = progress_dimensions(tx, claim, &stamp)?;
            let bytes = read_chunks(
                tx,
                "SELECT COUNT(*) AS total FROM dt_progress_chunks WHERE run_id=? AND attempt_id=?",
                "SELECT ordinal,payload FROM dt_progress_chunks WHERE run_id=? AND attempt_id=? AND ordinal>=? AND ordinal<? ORDER BY ordinal",
                &[text(&claim.run_id), text(&claim.attempt_id)],
                byte_len,
                count,
            )?;
            if Digest::hash(&bytes) != stamp.digest {
                return Err(RepositoryError::Integrity);
            }
            Ok(Some(ProgressRecord { stamp, bytes }))
        })
    }
}

fn transaction<D: SqlDatabase, T>(
    database: &mut D,
    operation: impl FnOnce(&mut D::Transaction) -> Result<T>,
) -> Result<T> {
    let mut tx = database.begin()?;
    match operation(&mut tx) {
        Ok(value) => {
            // No retry, rollback-to-success or readback after a failed commit.
            tx.commit()?;
            Ok(value)
        }
        Err(error) => match tx.rollback() {
            Ok(()) => Err(error),
            // The transaction resource retains actual custody; uncertainty
            // overrides an ordinary conflict/validation failure.
            Err(_) => Err(RepositoryError::CommitUnknown),
        },
    }
}

fn schema_digest() -> Digest {
    binding_digest(
        "deepthink.run-repository.schema.v1",
        &SCHEMA
            .iter()
            .map(|(_, ddl)| ddl.as_bytes())
            .collect::<Vec<_>>(),
    )
}

fn initialize_schema<T: SqlTransaction>(tx: &mut T) -> Result<()> {
    check_schema(tx, true)
}

fn read_schema<T: SqlTransaction>(tx: &mut T) -> Result<Vec<Vec<SqlCell>>> {
    query(
        tx,
        SELECT_SCHEMA,
        &[],
        64,
        &["type", "name", "tbl_name", "sql"],
    )
}

fn check_schema<T: SqlTransaction>(tx: &mut T, may_create: bool) -> Result<()> {
    let rows = read_schema(tx)?;
    if rows.is_empty() && may_create {
        for (_, ddl) in SCHEMA {
            tx.exec(ddl, &[])?;
        }
        changed_one(tx.exec(
            "INSERT INTO dt_repository_schema(slot,version,schema_digest) VALUES ('schema',?,?)",
            &[
                SqlCell::Integer(SCHEMA_VERSION),
                text(schema_digest().as_str()),
            ],
        )?)?;
        // Also inspect the schema actually persisted by SQLite before commit.
        return check_schema(tx, false);
    }
    validate_schema_rows(tx, rows)
}

fn validate_schema_rows<T: SqlTransaction>(tx: &mut T, rows: Vec<Vec<SqlCell>>) -> Result<()> {
    let mut found = BTreeSet::new();
    for row in rows {
        let kind = cell_text(&row[0])?;
        let name = cell_text(&row[1])?;
        let table = cell_text(&row[2])?;
        match kind {
            "table" => {
                let Some((_, ddl)) = SCHEMA.iter().find(|(expected, _)| *expected == name) else {
                    return Err(RepositoryError::Schema);
                };
                if table != name || cell_text(&row[3])? != *ddl || !found.insert(name.to_owned()) {
                    return Err(RepositoryError::Schema);
                }
            }
            "index"
                if row[3] == SqlCell::Null
                    && SCHEMA.iter().any(|(expected, _)| *expected == table)
                    && name.starts_with(&format!("sqlite_autoindex_{table}_")) => {}
            _ => return Err(RepositoryError::Schema),
        }
    }
    if found.len() != SCHEMA.len() {
        return Err(RepositoryError::Schema);
    }
    let marker = query(
        tx,
        "SELECT slot,version,schema_digest FROM dt_repository_schema",
        &[],
        2,
        &["slot", "version", "schema_digest"],
    )?;
    let row = one(&marker)?;
    if cell_text(&row[0])? != "schema"
        || cell_int(&row[1])? != SCHEMA_VERSION
        || cell_text(&row[2])? != schema_digest().as_str()
    {
        return Err(RepositoryError::Schema);
    }
    Ok(())
}

fn create_run<T: SqlTransaction>(tx: &mut T, identity: &RunIdentity, bytes: &[u8]) -> Result<Head> {
    if read_head(tx, &identity.run_id)?.is_some() {
        return Err(RepositoryError::Conflict);
    }
    let head = Head {
        run_id: identity.run_id.clone(),
        run_request_digest: identity.run_request_digest.clone(),
        revision: 0,
        snapshot_digest: Digest::hash(bytes),
    };
    changed_one(tx.exec("INSERT INTO dt_run_heads(run_id,run_request_digest,revision,snapshot_digest) VALUES (?,?,?,?)", &[text(&head.run_id), text(head.run_request_digest.as_str()), SqlCell::Integer(0), text(head.snapshot_digest.as_str())])?)?;
    append_snapshot(tx, &head, None, bytes)?;
    Ok(head)
}

fn read_head<T: SqlTransaction>(tx: &mut T, run_id: &str) -> Result<Option<Head>> {
    let rows = query(
        tx,
        SELECT_HEAD,
        &[text(run_id)],
        2,
        &["run_request_digest", "revision", "snapshot_digest"],
    )?;
    let Some(row) = optional_one(&rows)? else {
        return Ok(None);
    };
    let head = Head {
        run_id: run_id.to_owned(),
        run_request_digest: stored_digest(cell_text(&row[0])?)?,
        revision: cell_int(&row[1])?,
        snapshot_digest: stored_digest(cell_text(&row[2])?)?,
    };
    validate_head(&head).map_err(|_| RepositoryError::Integrity)?;
    Ok(Some(head))
}

fn require_head<T: SqlTransaction>(tx: &mut T, expected: &Head) -> Result<()> {
    if read_head(tx, &expected.run_id)?.as_ref() == Some(expected) {
        Ok(())
    } else {
        Err(RepositoryError::Conflict)
    }
}

fn append_snapshot<T: SqlTransaction>(
    tx: &mut T,
    head: &Head,
    previous: Option<&Digest>,
    bytes: &[u8],
) -> Result<()> {
    changed_one(tx.exec("INSERT INTO dt_run_snapshots(run_id,revision,previous_digest,snapshot_digest,byte_len,chunk_count) VALUES (?,?,?,?,?,?)", &[text(&head.run_id), SqlCell::Integer(head.revision), previous.map(|v| text(v.as_str())).unwrap_or(SqlCell::Null), text(head.snapshot_digest.as_str()), SqlCell::Integer(bytes.len() as i64), SqlCell::Integer(bytes.len().div_ceil(CHUNK_BYTES) as i64)])?)?;
    for (ordinal, chunk) in bytes.chunks(CHUNK_BYTES).enumerate() {
        changed_one(tx.exec(
            "INSERT INTO dt_snapshot_chunks(run_id,revision,ordinal,payload) VALUES (?,?,?,?)",
            &[
                text(&head.run_id),
                SqlCell::Integer(head.revision),
                SqlCell::Integer(ordinal as i64),
                bytes_cell(chunk),
            ],
        )?)?;
    }
    Ok(())
}

fn advance_head<T: SqlTransaction>(tx: &mut T, expected: &Head, next: &Head) -> Result<()> {
    changed_one(tx.exec("UPDATE dt_run_heads SET revision=?,snapshot_digest=? WHERE run_id=? AND run_request_digest=? AND revision=? AND snapshot_digest=?", &[SqlCell::Integer(next.revision), text(next.snapshot_digest.as_str()), text(&expected.run_id), text(expected.run_request_digest.as_str()), SqlCell::Integer(expected.revision), text(expected.snapshot_digest.as_str())])?)
}

fn insert_claim<T: SqlTransaction>(tx: &mut T, head: &Head, claim: &GenerationClaim) -> Result<()> {
    if claim.run_id != head.run_id || claim.run_request_digest != head.run_request_digest {
        return Err(RepositoryError::Conflict);
    }
    changed_one(tx.exec("INSERT INTO dt_generation_claims(run_id,run_request_digest,phase,generation_key,attempt_id,request_digest,route_digest,claimed_revision) VALUES (?,?,?,?,?,?,?,?)", &[text(&claim.run_id), text(claim.run_request_digest.as_str()), text(&claim.phase), text(&claim.generation_key), text(&claim.attempt_id), text(claim.request_digest.as_str()), text(claim.route_digest.as_str()), SqlCell::Integer(head.revision)])?)
}

fn read_claim<T: SqlTransaction>(
    tx: &mut T,
    run_id: &str,
    attempt_id: &str,
) -> Result<Option<(GenerationClaim, i64)>> {
    let rows = query(
        tx,
        SELECT_CLAIM,
        &[text(run_id), text(attempt_id)],
        2,
        &[
            "run_request_digest",
            "phase",
            "generation_key",
            "attempt_id",
            "request_digest",
            "route_digest",
            "claimed_revision",
        ],
    )?;
    let Some(row) = optional_one(&rows)? else {
        return Ok(None);
    };
    let claim = GenerationClaim {
        run_id: run_id.to_owned(),
        run_request_digest: stored_digest(cell_text(&row[0])?)?,
        phase: cell_text(&row[1])?.to_owned(),
        generation_key: cell_text(&row[2])?.to_owned(),
        attempt_id: cell_text(&row[3])?.to_owned(),
        request_digest: stored_digest(cell_text(&row[4])?)?,
        route_digest: stored_digest(cell_text(&row[5])?)?,
    };
    let revision = cell_int(&row[6])?;
    validate_claim(&claim).map_err(|_| RepositoryError::Integrity)?;
    if claim.attempt_id != attempt_id || revision < 0 {
        return Err(RepositoryError::Integrity);
    }
    Ok(Some((claim, revision)))
}

fn blocking_claims<T: SqlTransaction>(
    tx: &mut T,
    run_id: &str,
) -> Result<Vec<(String, Option<ResolutionDisposition>)>> {
    query(
        tx,
        SELECT_BLOCKERS,
        &[text(run_id)],
        2,
        &["attempt_id", "disposition"],
    )?
    .into_iter()
    .map(|row| {
        let attempt = cell_text(&row[0])?.to_owned();
        validate_id(&attempt).map_err(|_| RepositoryError::Integrity)?;
        let disposition = match &row[1] {
            SqlCell::Null => None,
            SqlCell::Text(value) => Some(ResolutionDisposition::parse(value)?),
            _ => return Err(RepositoryError::Integrity),
        };
        Ok((attempt, disposition))
    })
    .collect()
}

fn require_claim<T: SqlTransaction>(
    tx: &mut T,
    head: &Head,
    expected: &GenerationClaim,
) -> Result<()> {
    if expected.run_id != head.run_id || expected.run_request_digest != head.run_request_digest {
        return Err(RepositoryError::Conflict);
    }
    let (claim, revision) =
        read_claim(tx, &head.run_id, &expected.attempt_id)?.ok_or(RepositoryError::Conflict)?;
    if claim != *expected || revision > head.revision {
        return Err(RepositoryError::Conflict);
    }
    Ok(())
}

fn read_progress_stamp<T: SqlTransaction>(
    tx: &mut T,
    run_id: &str,
    attempt_id: &str,
) -> Result<Option<ProgressStamp>> {
    let rows = query(
        tx,
        SELECT_PROGRESS,
        &[text(run_id), text(attempt_id)],
        2,
        &["sequence", "progress_digest"],
    )?;
    let Some(row) = optional_one(&rows)? else {
        return Ok(None);
    };
    Ok(Some(stored_progress_stamp(&row[0], &row[1])?))
}

fn stored_progress_stamp(sequence: &SqlCell, digest: &SqlCell) -> Result<ProgressStamp> {
    let sequence = u64::try_from(cell_int(sequence)?).map_err(|_| RepositoryError::Integrity)?;
    checked_sequence(sequence).map_err(|_| RepositoryError::Integrity)?;
    Ok(ProgressStamp {
        sequence,
        digest: stored_digest(cell_text(digest)?)?,
    })
}

fn progress_dimensions<T: SqlTransaction>(
    tx: &mut T,
    claim: &GenerationClaim,
    stamp: &ProgressStamp,
) -> Result<(usize, usize)> {
    let keys = [
        text(&claim.run_id),
        text(&claim.attempt_id),
        SqlCell::Integer(checked_sequence(stamp.sequence)?),
    ];
    let rows = query(
        tx,
        "SELECT progress_digest,byte_len,chunk_count FROM dt_progress_records WHERE run_id=? AND attempt_id=? AND sequence=?",
        &keys,
        2,
        &["progress_digest", "byte_len", "chunk_count"],
    )?;
    let row = one(&rows)?;
    if stored_digest(cell_text(&row[0])?)? != stamp.digest {
        return Err(RepositoryError::Integrity);
    }
    record_dimensions(&row[1], &row[2])
}

/// One bounded scalar read replaces five separate reads, not their checks.
/// Chunk metadata is still checked separately and every subsequent write keeps
/// its old affected-row/CAS condition. No payload or immutable history scan is
/// included in this query, and the caller keeps the same short transaction.
fn progress_save_preconditions<T: SqlTransaction>(
    tx: &mut T,
    expected: &Head,
    claim: &GenerationClaim,
    sequence: u64,
) -> Result<(Option<ProgressStamp>, (usize, usize))> {
    let rows = query(
        tx,
        SELECT_PROGRESS_PRECONDITIONS,
        &[text(&claim.attempt_id), text(&expected.run_id)],
        2,
        PROGRESS_PRECONDITION_COLUMNS,
    )?;
    let row = optional_one(&rows)?.ok_or(RepositoryError::Conflict)?;
    let stored_head = Head {
        run_id: expected.run_id.clone(),
        run_request_digest: stored_digest(cell_text(&row[0])?)?,
        revision: cell_int(&row[1])?,
        snapshot_digest: stored_digest(cell_text(&row[2])?)?,
    };
    validate_head(&stored_head).map_err(|_| RepositoryError::Integrity)?;
    if stored_head != *expected {
        return Err(RepositoryError::Conflict);
    }
    // A LEFT JOIN's absent row is all NULL. A partially NULL claim must fail
    // typed decoding rather than becoming permission to create progress.
    if row[3..10].iter().all(|cell| matches!(cell, SqlCell::Null)) {
        return Err(RepositoryError::Conflict);
    }
    let stored_claim = GenerationClaim {
        run_id: expected.run_id.clone(),
        run_request_digest: stored_digest(cell_text(&row[3])?)?,
        phase: cell_text(&row[4])?.to_owned(),
        generation_key: cell_text(&row[5])?.to_owned(),
        attempt_id: cell_text(&row[6])?.to_owned(),
        request_digest: stored_digest(cell_text(&row[7])?)?,
        route_digest: stored_digest(cell_text(&row[8])?)?,
    };
    let claimed_revision = cell_int(&row[9])?;
    validate_claim(&stored_claim).map_err(|_| RepositoryError::Integrity)?;
    if stored_claim.attempt_id != claim.attempt_id || claimed_revision < 0 {
        return Err(RepositoryError::Integrity);
    }
    if stored_claim != *claim || claimed_revision > expected.revision {
        return Err(RepositoryError::Conflict);
    }
    match &row[10] {
        SqlCell::Null => {}
        SqlCell::Text(attempt_id) if attempt_id == &claim.attempt_id => {
            return Err(RepositoryError::Conflict);
        }
        _ => return Err(RepositoryError::Integrity),
    }
    match (&row[11], &row[12]) {
        (SqlCell::Null, SqlCell::Null) => {
            if row[13..16]
                .iter()
                .any(|cell| !matches!(cell, SqlCell::Null))
            {
                return Err(RepositoryError::Integrity);
            }
            Ok((None, (0, 0)))
        }
        _ => {
            let previous = stored_progress_stamp(&row[11], &row[12])?;
            if previous.sequence >= sequence {
                return Err(RepositoryError::Conflict);
            }
            if stored_digest(cell_text(&row[13])?)? != previous.digest {
                return Err(RepositoryError::Integrity);
            }
            let dimensions = record_dimensions(&row[14], &row[15])?;
            Ok((Some(previous), dimensions))
        }
    }
}

fn progress_chunk_metadata<T: SqlTransaction>(
    tx: &mut T,
    claim: &GenerationClaim,
    (len, count): (usize, usize),
) -> Result<Vec<(Digest, usize)>> {
    let rows = query(
        tx,
        "SELECT ordinal,chunk_digest,length(payload) AS byte_len FROM dt_progress_chunks WHERE run_id=? AND attempt_id=? ORDER BY ordinal",
        &[text(&claim.run_id), text(&claim.attempt_id)],
        129,
        &["ordinal", "chunk_digest", "byte_len"],
    )?;
    if rows.len() != count {
        return Err(RepositoryError::Integrity);
    }
    rows.into_iter()
        .enumerate()
        .map(|(ordinal, row)| {
            let chunk_len = encoded_byte_length(&row[2])?;
            if cell_int(&row[0])? != ordinal as i64
                || chunk_len != (len - ordinal * CHUNK_BYTES).min(CHUNK_BYTES)
            {
                return Err(RepositoryError::Integrity);
            }
            Ok((stored_digest(cell_text(&row[1])?)?, chunk_len))
        })
        .collect()
}

fn insert_resolution<T: SqlTransaction>(
    tx: &mut T,
    head: &Head,
    resolution: &Resolution,
) -> Result<()> {
    if resolution.claim.run_id != head.run_id
        || resolution.claim.run_request_digest != head.run_request_digest
    {
        return Err(RepositoryError::Conflict);
    }
    let (claim, claimed_revision) = read_claim(tx, &head.run_id, &resolution.claim.attempt_id)?
        .ok_or(RepositoryError::Conflict)?;
    if claim != resolution.claim || claimed_revision > head.revision {
        return Err(RepositoryError::Conflict);
    }
    if read_progress_stamp(tx, &head.run_id, &claim.attempt_id)? != resolution.expected_progress {
        return Err(RepositoryError::Conflict);
    }
    let (sequence, progress_digest) = match &resolution.expected_progress {
        None => (SqlCell::Null, SqlCell::Null),
        Some(stamp) => (
            SqlCell::Integer(checked_sequence(stamp.sequence)?),
            text(stamp.digest.as_str()),
        ),
    };
    let bytes = &resolution.receipt;
    changed_one(tx.exec("INSERT INTO dt_generation_resolutions(run_id,attempt_id,disposition,receipt_digest,byte_len,chunk_count,resolved_revision,progress_sequence,progress_digest) VALUES (?,?,?,?,?,?,?,?,?)", &[text(&head.run_id), text(&claim.attempt_id), text(resolution.disposition.as_str()), text(resolution.digest().as_str()), SqlCell::Integer(bytes.len() as i64), SqlCell::Integer(bytes.len().div_ceil(CHUNK_BYTES) as i64), SqlCell::Integer(head.revision), sequence, progress_digest])?)?;
    for (ordinal, chunk) in bytes.chunks(CHUNK_BYTES).enumerate() {
        changed_one(tx.exec(
            "INSERT INTO dt_resolution_chunks(run_id,attempt_id,ordinal,payload) VALUES (?,?,?,?)",
            &[
                text(&head.run_id),
                text(&claim.attempt_id),
                SqlCell::Integer(ordinal as i64),
                bytes_cell(chunk),
            ],
        )?)?;
    }
    Ok(())
}

fn read_resolution<T: SqlTransaction>(
    tx: &mut T,
    run_id: &str,
    attempt_id: &str,
) -> Result<Option<StoredResolution>> {
    let rows = query(
        tx,
        SELECT_RESOLUTION,
        &[text(run_id), text(attempt_id)],
        2,
        &[
            "disposition",
            "receipt_digest",
            "byte_len",
            "chunk_count",
            "resolved_revision",
            "progress_sequence",
            "progress_digest",
        ],
    )?;
    let Some(row) = optional_one(&rows)? else {
        return Ok(None);
    };
    let (claim, claimed_revision) =
        read_claim(tx, run_id, attempt_id)?.ok_or(RepositoryError::Integrity)?;
    let disposition = ResolutionDisposition::parse(cell_text(&row[0])?)?;
    let digest = stored_digest(cell_text(&row[1])?)?;
    let (byte_len, count) = record_dimensions(&row[2], &row[3])?;
    let resolved_revision = cell_int(&row[4])?;
    let head = read_head(tx, run_id)?.ok_or(RepositoryError::Integrity)?;
    if resolved_revision < claimed_revision
        || resolved_revision > head.revision
        || claim.run_request_digest != head.run_request_digest
    {
        return Err(RepositoryError::Integrity);
    }
    let expected_progress = match (&row[5], &row[6]) {
        (SqlCell::Null, SqlCell::Null) => None,
        (SqlCell::Integer(_), SqlCell::Text(_)) => Some(stored_progress_stamp(&row[5], &row[6])?),
        _ => return Err(RepositoryError::Integrity),
    };
    if read_progress_stamp(tx, run_id, attempt_id)? != expected_progress {
        return Err(RepositoryError::Integrity);
    }
    let receipt = read_chunks(
        tx,
        "SELECT COUNT(*) AS total FROM dt_resolution_chunks WHERE run_id=? AND attempt_id=?",
        "SELECT ordinal,payload FROM dt_resolution_chunks WHERE run_id=? AND attempt_id=? AND ordinal>=? AND ordinal<? ORDER BY ordinal",
        &[text(run_id), text(attempt_id)],
        byte_len,
        count,
    )?;
    let resolution = Resolution {
        claim,
        disposition,
        expected_progress,
        receipt,
    };
    if resolution.digest() != digest {
        return Err(RepositoryError::Integrity);
    }
    Ok(Some(StoredResolution {
        resolution,
        digest,
        resolved_revision,
    }))
}

fn query<T: SqlTransaction>(
    tx: &mut T,
    sql: &str,
    params: &[SqlCell],
    max_rows: u32,
    columns: &[&str],
) -> Result<Vec<Vec<SqlCell>>> {
    let result = tx.query(sql, params, max_rows)?;
    if result
        .columns
        .iter()
        .map(String::as_str)
        .ne(columns.iter().copied())
        || result.rows.len() > max_rows as usize
        || result.rows.iter().any(|row| row.len() != columns.len())
    {
        return Err(RepositoryError::Integrity);
    }
    Ok(result.rows)
}

fn text(value: &str) -> SqlCell {
    SqlCell::Text(value.to_owned())
}
// Opaque bytes are HEX TEXT inside SQL, not a JSON integer array. Hashes and
// the public repository API still refer to the original bytes. The fixed Store
// quota is unchanged; this representation uses two text bytes per raw byte.
fn bytes_cell(bytes: &[u8]) -> SqlCell {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        encoded.push(char::from(HEX[(byte >> 4) as usize]));
        encoded.push(char::from(HEX[(byte & 15) as usize]));
    }
    SqlCell::Text(encoded)
}
fn encoded_byte_length(cell: &SqlCell) -> Result<usize> {
    let encoded = usize::try_from(cell_int(cell)?).map_err(|_| RepositoryError::Integrity)?;
    if encoded == 0 || encoded > CHUNK_BYTES * 2 || encoded % 2 != 0 {
        return Err(RepositoryError::Integrity);
    }
    Ok(encoded / 2)
}
fn cell_bytes(cell: &SqlCell) -> Result<Vec<u8>> {
    let encoded = cell_text(cell)?;
    let length = encoded_byte_length(&SqlCell::Integer(encoded.len() as i64))?;
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(length)
        .map_err(|_| RepositoryError::Limit)?;
    fn digit(byte: u8) -> Result<u8> {
        match byte {
            b'0'..=b'9' => Ok(byte - b'0'),
            b'a'..=b'f' => Ok(byte - b'a' + 10),
            _ => Err(RepositoryError::Integrity),
        }
    }
    for pair in encoded.as_bytes().chunks_exact(2) {
        bytes.push((digit(pair[0])? << 4) | digit(pair[1])?);
    }
    Ok(bytes)
}
fn cell_text(value: &SqlCell) -> Result<&str> {
    if let SqlCell::Text(text) = value {
        Ok(text)
    } else {
        Err(RepositoryError::Integrity)
    }
}
fn cell_int(value: &SqlCell) -> Result<i64> {
    if let SqlCell::Integer(number) = value {
        Ok(*number)
    } else {
        Err(RepositoryError::Integrity)
    }
}
fn stored_digest(value: &str) -> Result<Digest> {
    Digest::parse(value).map_err(|_| RepositoryError::Integrity)
}
fn one(rows: &[Vec<SqlCell>]) -> Result<&Vec<SqlCell>> {
    optional_one(rows)?.ok_or(RepositoryError::Integrity)
}
fn optional_one(rows: &[Vec<SqlCell>]) -> Result<Option<&Vec<SqlCell>>> {
    match rows {
        [] => Ok(None),
        [row] => Ok(Some(row)),
        _ => Err(RepositoryError::Integrity),
    }
}
fn changed_one(count: u64) -> Result<()> {
    if count == 1 {
        Ok(())
    } else {
        Err(RepositoryError::Conflict)
    }
}

fn validate_id(value: &str) -> Result<()> {
    if value.is_empty() || value.len() > MAX_ID_BYTES || value.chars().any(char::is_control) {
        Err(RepositoryError::Invalid)
    } else {
        Ok(())
    }
}
fn validate_identity(identity: &RunIdentity) -> Result<()> {
    validate_id(&identity.run_id)
}
fn validate_head(head: &Head) -> Result<()> {
    validate_id(&head.run_id)?;
    if head.revision < 0 {
        Err(RepositoryError::Invalid)
    } else {
        Ok(())
    }
}
fn validate_claim(claim: &GenerationClaim) -> Result<()> {
    for id in [
        &claim.run_id,
        &claim.phase,
        &claim.generation_key,
        &claim.attempt_id,
    ] {
        validate_id(id)?;
    }
    Ok(())
}
fn validate_record(bytes: &[u8]) -> Result<()> {
    if bytes.is_empty() {
        Err(RepositoryError::Invalid)
    } else if bytes.len() > MAX_RECORD_BYTES {
        Err(RepositoryError::Limit)
    } else {
        Ok(())
    }
}
fn validate_small_receipt(bytes: &[u8]) -> Result<()> {
    validate_record(bytes)?;
    if bytes.len() > CHUNK_BYTES {
        Err(RepositoryError::Limit)
    } else {
        Ok(())
    }
}
fn validate_settlements(settlements: &[Resolution]) -> Result<()> {
    if settlements.len() > MAX_TRANSITION_CLAIMS {
        return Err(RepositoryError::Limit);
    }
    let mut seen = BTreeSet::new();
    let mut total = 0usize;
    for settlement in settlements {
        validate_claim(&settlement.claim)?;
        validate_record(&settlement.receipt)?;
        if let Some(stamp) = &settlement.expected_progress {
            checked_sequence(stamp.sequence)?;
        }
        total = total
            .checked_add(settlement.receipt.len())
            .ok_or(RepositoryError::Limit)?;
        if total > MAX_RECORD_BYTES {
            return Err(RepositoryError::Limit);
        }
        if !seen.insert((&settlement.claim.run_id, &settlement.claim.attempt_id)) {
            return Err(RepositoryError::Invalid);
        }
    }
    Ok(())
}
fn validate_transition(head: &Head, bytes: &[u8], claim: &GenerationClaim) -> Result<()> {
    validate_head(head)?;
    validate_record(bytes)?;
    validate_claim(claim)?;
    if head.run_id != claim.run_id || head.run_request_digest != claim.run_request_digest {
        return Err(RepositoryError::Conflict);
    }
    Ok(())
}
fn next_head(head: &Head, bytes: &[u8]) -> Result<Head> {
    Ok(Head {
        run_id: head.run_id.clone(),
        run_request_digest: head.run_request_digest.clone(),
        revision: head.revision.checked_add(1).ok_or(RepositoryError::Limit)?,
        snapshot_digest: Digest::hash(bytes),
    })
}
fn checked_sequence(sequence: u64) -> Result<i64> {
    if sequence == 0 {
        return Err(RepositoryError::Invalid);
    }
    i64::try_from(sequence).map_err(|_| RepositoryError::Limit)
}
fn record_dimensions(len: &SqlCell, count: &SqlCell) -> Result<(usize, usize)> {
    let len = usize::try_from(cell_int(len)?).map_err(|_| RepositoryError::Integrity)?;
    let count = usize::try_from(cell_int(count)?).map_err(|_| RepositoryError::Integrity)?;
    if len == 0 || len > MAX_RECORD_BYTES || count != len.div_ceil(CHUNK_BYTES) {
        return Err(RepositoryError::Integrity);
    }
    Ok((len, count))
}
fn read_chunks<T: SqlTransaction>(
    tx: &mut T,
    count_sql: &str,
    chunks_sql: &str,
    keys: &[SqlCell],
    len: usize,
    count: usize,
) -> Result<Vec<u8>> {
    let total = query(tx, count_sql, keys, 1, &["total"])?;
    if cell_int(&one(&total)?[0])? != count as i64 {
        return Err(RepositoryError::Integrity);
    }
    let mut bytes = Vec::new();
    bytes
        .try_reserve_exact(len)
        .map_err(|_| RepositoryError::Limit)?;
    for start in (0..count).step_by(CHUNK_BATCH_ROWS as usize) {
        let end = (start + CHUNK_BATCH_ROWS as usize).min(count);
        let mut params = keys.to_vec();
        params.extend([SqlCell::Integer(start as i64), SqlCell::Integer(end as i64)]);
        let rows = query(
            tx,
            chunks_sql,
            &params,
            CHUNK_BATCH_ROWS,
            &["ordinal", "payload"],
        )?;
        if rows.len() != end - start {
            return Err(RepositoryError::Integrity);
        }
        append_chunk_rows(&mut bytes, rows, len, start)?;
    }
    if bytes.len() != len {
        return Err(RepositoryError::Integrity);
    }
    Ok(bytes)
}
fn append_chunk_rows(
    bytes: &mut Vec<u8>,
    rows: Vec<Vec<SqlCell>>,
    len: usize,
    start: usize,
) -> Result<()> {
    for (offset, row) in rows.into_iter().enumerate() {
        let ordinal = start
            .checked_add(offset)
            .ok_or(RepositoryError::Integrity)?;
        if row.len() != 2 || cell_int(&row[0])? != ordinal as i64 {
            return Err(RepositoryError::Integrity);
        }
        let chunk = cell_bytes(&row[1])?;
        let expected_len = len
            .checked_sub(bytes.len())
            .ok_or(RepositoryError::Integrity)?
            .min(CHUNK_BYTES);
        if chunk.len() != expected_len || chunk.is_empty() {
            return Err(RepositoryError::Integrity);
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(())
}
fn binding_digest(domain: &str, fields: &[&[u8]]) -> Digest {
    let mut hash = Sha256::new();
    hash.update(domain.as_bytes());
    for field in fields {
        hash.update((field.len() as u64).to_be_bytes());
        hash.update(field);
    }
    Digest(format!("{:x}", hash.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        collections::VecDeque,
        sync::{Arc, Mutex},
    };

    // This adapter tests failure/capability boundaries, not SQLite/Gate. The
    // independent actual typed Store integration is a separate proof.
    enum Step {
        Query(&'static str, SqlRows),
        QueryError(&'static str, SqlError),
        Exec(&'static str, std::result::Result<u64, SqlError>),
        Commit(std::result::Result<(), SqlError>),
        Rollback(std::result::Result<(), SqlError>),
    }
    #[derive(Default)]
    struct Trace {
        steps: VecDeque<Step>,
        begins: usize,
        commits: usize,
        rollbacks: usize,
        queries: Vec<(String, Vec<SqlCell>, u32)>,
        writes: Vec<(String, Vec<SqlCell>)>,
    }
    #[derive(Clone)]
    struct FakeDatabase(Arc<Mutex<Trace>>);
    struct FakeTransaction(Arc<Mutex<Trace>>);
    impl SqlDatabase for FakeDatabase {
        type Transaction = FakeTransaction;
        fn begin(&mut self) -> std::result::Result<Self::Transaction, SqlError> {
            self.0.lock().unwrap().begins += 1;
            Ok(FakeTransaction(self.0.clone()))
        }
    }
    impl SqlTransaction for FakeTransaction {
        fn exec(&mut self, sql: &str, params: &[SqlCell]) -> std::result::Result<u64, SqlError> {
            let mut trace = self.0.lock().unwrap();
            let Some(Step::Exec(prefix, result)) = trace.steps.pop_front() else {
                panic!("unexpected exec: {sql}")
            };
            assert!(sql.starts_with(prefix), "expected {prefix}, got {sql}");
            trace.writes.push((sql.to_owned(), params.to_vec()));
            result
        }
        fn query(
            &mut self,
            sql: &str,
            params: &[SqlCell],
            max_rows: u32,
        ) -> std::result::Result<SqlRows, SqlError> {
            let mut trace = self.0.lock().unwrap();
            let (prefix, result) = match trace.steps.pop_front() {
                Some(Step::Query(prefix, rows)) => (prefix, Ok(rows)),
                Some(Step::QueryError(prefix, error)) => (prefix, Err(error)),
                _ => panic!("unexpected query: {sql}"),
            };
            assert!(sql.starts_with(prefix), "expected {prefix}, got {sql}");
            assert!(max_rows <= 256);
            trace
                .queries
                .push((sql.to_owned(), params.to_vec(), max_rows));
            result
        }
        fn commit(self) -> std::result::Result<(), SqlError> {
            let mut trace = self.0.lock().unwrap();
            trace.commits += 1;
            let Some(Step::Commit(result)) = trace.steps.pop_front() else {
                panic!("unexpected commit")
            };
            result
        }
        fn rollback(self) -> std::result::Result<(), SqlError> {
            let mut trace = self.0.lock().unwrap();
            trace.rollbacks += 1;
            let Some(Step::Rollback(result)) = trace.steps.pop_front() else {
                panic!("unexpected rollback")
            };
            result
        }
    }
    fn rows(columns: &[&str], values: Vec<Vec<SqlCell>>) -> SqlRows {
        SqlRows {
            columns: columns.iter().map(|value| (*value).to_owned()).collect(),
            rows: values,
        }
    }
    fn q(sql: &'static str, columns: &[&str], values: Vec<Vec<SqlCell>>) -> Step {
        Step::Query(sql, rows(columns, values))
    }
    fn write(prefix: &'static str) -> Step {
        Step::Exec(prefix, Ok(1))
    }
    fn fixture(steps: Vec<Step>) -> (RunRepository<FakeDatabase>, Arc<Mutex<Trace>>) {
        let trace = Arc::new(Mutex::new(Trace {
            steps: steps.into(),
            ..Trace::default()
        }));
        (
            RunRepository {
                database: FakeDatabase(trace.clone()),
            },
            trace,
        )
    }
    fn finished(trace: &Arc<Mutex<Trace>>) {
        assert!(trace.lock().unwrap().steps.is_empty());
    }
    fn identity() -> RunIdentity {
        RunIdentity {
            run_id: "public-run".into(),
            run_request_digest: Digest::hash(b"run-input"),
        }
    }
    fn head() -> Head {
        Head {
            run_id: identity().run_id,
            run_request_digest: identity().run_request_digest,
            revision: 3,
            snapshot_digest: Digest::hash(b"before"),
        }
    }
    fn claim() -> GenerationClaim {
        GenerationClaim {
            run_id: identity().run_id,
            run_request_digest: identity().run_request_digest,
            phase: "goal_contract".into(),
            generation_key: "goal_contract/0#0".into(),
            attempt_id: "goal_contract/0#0:1".into(),
            request_digest: Digest::hash(b"request"),
            route_digest: Digest::hash(b"route"),
        }
    }
    fn resolution(disposition: ResolutionDisposition) -> Resolution {
        Resolution {
            claim: claim(),
            disposition,
            expected_progress: None,
            receipt: br#"{"cost_usd":"0.00066334","content":"unavailable"}"#.to_vec(),
        }
    }
    fn head_query(head: &Head) -> Step {
        q(
            SELECT_HEAD,
            &["run_request_digest", "revision", "snapshot_digest"],
            vec![vec![
                text(head.run_request_digest.as_str()),
                SqlCell::Integer(head.revision),
                text(head.snapshot_digest.as_str()),
            ]],
        )
    }
    fn claim_query(claim: &GenerationClaim, revision: i64) -> Step {
        q(
            SELECT_CLAIM,
            &[
                "run_request_digest",
                "phase",
                "generation_key",
                "attempt_id",
                "request_digest",
                "route_digest",
                "claimed_revision",
            ],
            vec![vec![
                text(claim.run_request_digest.as_str()),
                text(&claim.phase),
                text(&claim.generation_key),
                text(&claim.attempt_id),
                text(claim.request_digest.as_str()),
                text(claim.route_digest.as_str()),
                SqlCell::Integer(revision),
            ]],
        )
    }
    fn blockers(values: Vec<Vec<SqlCell>>) -> Step {
        q(SELECT_BLOCKERS, &["attempt_id", "disposition"], values)
    }
    fn progress_query(stamp: Option<&ProgressStamp>) -> Step {
        q(
            SELECT_PROGRESS,
            &["sequence", "progress_digest"],
            stamp
                .map(|stamp| {
                    vec![vec![
                        SqlCell::Integer(stamp.sequence as i64),
                        text(stamp.digest.as_str()),
                    ]]
                })
                .unwrap_or_default(),
        )
    }
    fn progress_precondition_row(
        head: &Head,
        claim: &GenerationClaim,
        previous: Option<(&ProgressStamp, usize)>,
    ) -> Vec<SqlCell> {
        let mut row = vec![
            text(head.run_request_digest.as_str()),
            SqlCell::Integer(head.revision),
            text(head.snapshot_digest.as_str()),
            text(claim.run_request_digest.as_str()),
            text(&claim.phase),
            text(&claim.generation_key),
            text(&claim.attempt_id),
            text(claim.request_digest.as_str()),
            text(claim.route_digest.as_str()),
            SqlCell::Integer(head.revision),
            SqlCell::Null,
        ];
        row.extend(match previous {
            Some((stamp, len)) => [
                SqlCell::Integer(stamp.sequence as i64),
                text(stamp.digest.as_str()),
                text(stamp.digest.as_str()),
                SqlCell::Integer(len as i64),
                SqlCell::Integer(len.div_ceil(CHUNK_BYTES) as i64),
            ],
            None => std::array::from_fn(|_| SqlCell::Null),
        });
        row
    }
    fn progress_preconditions_query(values: Vec<Vec<SqlCell>>) -> Step {
        q(
            SELECT_PROGRESS_PRECONDITIONS,
            PROGRESS_PRECONDITION_COLUMNS,
            values,
        )
    }
    fn chunk_metadata_query(bytes: &[u8]) -> Step {
        q(
            "SELECT ordinal,chunk_digest,length(payload)",
            &["ordinal", "chunk_digest", "byte_len"],
            bytes
                .chunks(CHUNK_BYTES)
                .enumerate()
                .map(|(index, chunk)| {
                    vec![
                        SqlCell::Integer(index as i64),
                        text(Digest::hash(chunk).as_str()),
                        SqlCell::Integer((chunk.len() * 2) as i64),
                    ]
                })
                .collect(),
        )
    }
    fn snapshot_writes(bytes: &[u8]) -> Vec<Step> {
        let mut steps = vec![write("INSERT INTO dt_run_snapshots")];
        steps.extend(
            bytes
                .chunks(CHUNK_BYTES)
                .map(|_| write("INSERT INTO dt_snapshot_chunks")),
        );
        steps.push(write("UPDATE dt_run_heads"));
        steps
    }
    fn claim_steps(commit: std::result::Result<(), SqlError>) -> Vec<Step> {
        let mut steps = vec![
            head_query(&head()),
            blockers(vec![]),
            write("INSERT INTO dt_generation_claims"),
        ];
        steps.extend(snapshot_writes(b"pending"));
        steps.push(Step::Commit(commit));
        steps
    }
    fn schema_rows() -> SqlRows {
        rows(
            &["type", "name", "tbl_name", "sql"],
            SCHEMA
                .iter()
                .map(|(name, ddl)| vec![text("table"), text(name), text(name), text(ddl)])
                .collect(),
        )
    }
    fn marker_query() -> Step {
        q(
            "SELECT slot,version,schema_digest",
            &["slot", "version", "schema_digest"],
            vec![vec![
                text("schema"),
                SqlCell::Integer(1),
                text(schema_digest().as_str()),
            ]],
        )
    }

    #[test]
    fn confirmed_insert_and_commit_are_required_before_a_one_use_permit_exists() {
        let (mut repo, trace) = fixture(claim_steps(Ok(())));
        let (next, permit) = repo
            .claim_generation(&head(), b"pending", &claim())
            .unwrap();
        assert_eq!(next.revision, 4);
        assert_eq!(permit.committed_head(), &next);
        assert_eq!(trace.lock().unwrap().commits, 1);
        assert_eq!(permit.consume(&claim()), Ok(()));
        finished(&trace);
    }

    #[test]
    fn commit_unknown_and_matching_pending_readback_never_remint_a_permit() {
        let mut steps = claim_steps(Err(SqlError::commit_unknown()));
        let next = next_head(&head(), b"pending").unwrap();
        steps.extend([
            head_query(&next),
            blockers(vec![vec![text(&claim().attempt_id), SqlCell::Null]]),
            Step::Rollback(Ok(())),
        ]);
        let (mut repo, trace) = fixture(steps);
        assert!(matches!(
            repo.claim_generation(&head(), b"pending", &claim()),
            Err(RepositoryError::CommitUnknown)
        ));
        assert!(matches!(
            repo.claim_generation(&next, b"pending", &claim()),
            Err(RepositoryError::PendingGeneration)
        ));
        assert_eq!(
            trace
                .lock()
                .unwrap()
                .writes
                .iter()
                .filter(|(sql, _)| sql.starts_with("INSERT INTO dt_generation_claims"))
                .count(),
            1
        );
        finished(&trace);
    }

    #[test]
    fn lost_cas_rolls_back_the_inserted_claim_and_cannot_dispatch() {
        let mut steps = claim_steps(Ok(()));
        steps.pop();
        steps.pop();
        steps.extend([
            Step::Exec("UPDATE dt_run_heads", Ok(0)),
            Step::Rollback(Ok(())),
        ]);
        let (mut repo, trace) = fixture(steps);
        assert!(matches!(
            repo.claim_generation(&head(), b"pending", &claim()),
            Err(RepositoryError::Conflict)
        ));
        assert_eq!(trace.lock().unwrap().commits, 0);
        finished(&trace);
    }

    #[test]
    fn rollback_uncertainty_overrides_a_known_cas_conflict() {
        let (mut repo, trace) = fixture(vec![
            head_query(&head()),
            blockers(vec![]),
            Step::Exec("INSERT INTO dt_generation_claims", Ok(0)),
            Step::Rollback(Err(SqlError::new(SqlErrorKind::Internal))),
        ]);
        assert!(matches!(
            repo.claim_generation(&head(), b"pending", &claim()),
            Err(RepositoryError::CommitUnknown)
        ));
        finished(&trace);
    }

    #[test]
    fn a_permit_is_bound_to_all_attempt_fields_and_is_consumed_on_mismatch() {
        let (mut repo, trace) = fixture(claim_steps(Ok(())));
        let (_, permit) = repo
            .claim_generation(&head(), b"pending", &claim())
            .unwrap();
        let mut wrong = claim();
        wrong.route_digest = Digest::hash(b"other-route");
        assert_eq!(permit.consume(&wrong), Err(RepositoryError::Conflict));
        finished(&trace);
    }

    #[test]
    fn content_unavailable_blocks_the_ordinary_claim_path_even_with_a_new_nonce() {
        let (mut repo, trace) = fixture(vec![
            head_query(&head()),
            blockers(vec![vec![
                text("old-attempt"),
                text("terminal_content_unavailable"),
            ]]),
            Step::Rollback(Ok(())),
        ]);
        let mut fresh = claim();
        fresh.attempt_id = "new-nonce".into();
        assert!(matches!(
            repo.claim_generation(&head(), b"pending", &fresh),
            Err(RepositoryError::PendingGeneration)
        ));
        assert!(trace.lock().unwrap().writes.is_empty());
        finished(&trace);
    }

    #[test]
    fn settlement_and_snapshot_share_one_cas_and_one_confirmed_commit() {
        let settlement = resolution(ResolutionDisposition::AcceptedCompletion);
        let mut steps = vec![
            head_query(&head()),
            claim_query(&claim(), 3),
            progress_query(None),
            write("INSERT INTO dt_generation_resolutions"),
            write("INSERT INTO dt_resolution_chunks"),
        ];
        steps.extend(snapshot_writes(b"completed"));
        steps.push(Step::Commit(Ok(())));
        let (mut repo, trace) = fixture(steps);
        assert_eq!(
            repo.commit_snapshot(&head(), b"completed", &[settlement.clone()])
                .unwrap()
                .revision,
            4
        );
        let trace = trace.lock().unwrap();
        assert_eq!(trace.commits, 1);
        assert_eq!(trace.rollbacks, 0);
        let (_, params) = trace
            .writes
            .iter()
            .find(|(sql, _)| sql.starts_with("INSERT INTO dt_generation_resolutions"))
            .unwrap();
        assert_eq!(params[3], text(settlement.digest().as_str()));
        assert!(trace.steps.is_empty());
    }

    #[test]
    fn resolution_loses_to_a_newer_progress_stamp_without_writing_any_receipt() {
        let old = ProgressStamp {
            sequence: 1,
            digest: Digest::hash(b"old-prefix"),
        };
        let new = ProgressStamp {
            sequence: 2,
            digest: Digest::hash(b"longer-prefix"),
        };
        let mut settlement = resolution(ResolutionDisposition::TerminalContentUnavailable);
        settlement.expected_progress = Some(old);
        let (mut repo, trace) = fixture(vec![
            head_query(&head()),
            claim_query(&claim(), 3),
            progress_query(Some(&new)),
            Step::Rollback(Ok(())),
        ]);
        assert_eq!(
            repo.commit_snapshot(&head(), b"resolved", &[settlement]),
            Err(RepositoryError::Conflict)
        );
        assert!(trace.lock().unwrap().writes.is_empty());
        finished(&trace);
    }

    #[test]
    fn absent_expected_progress_is_a_comparison_not_an_escape_hatch() {
        let stamp = ProgressStamp {
            sequence: 1,
            digest: Digest::hash(b"prefix"),
        };
        let (mut repo, trace) = fixture(vec![
            head_query(&head()),
            claim_query(&claim(), 3),
            progress_query(Some(&stamp)),
            Step::Rollback(Ok(())),
        ]);
        assert_eq!(
            repo.commit_snapshot(
                &head(),
                b"resolved",
                &[resolution(ResolutionDisposition::RejectedCompletion)]
            ),
            Err(RepositoryError::Conflict)
        );
        finished(&trace);
    }

    #[test]
    fn partial_schema_is_rejected_without_recreating_or_deleting_it() {
        let mut partial = schema_rows();
        partial.rows.pop();
        let trace = Arc::new(Mutex::new(Trace {
            steps: vec![Step::Query(SELECT_SCHEMA, partial), Step::Rollback(Ok(()))].into(),
            ..Trace::default()
        }));
        assert!(matches!(
            RunRepository::open(FakeDatabase(trace.clone())),
            Err(RepositoryError::Schema)
        ));
        assert!(trace.lock().unwrap().writes.is_empty());
        finished(&trace);
    }

    #[test]
    fn existing_only_open_never_creates_missing_or_partial_schema() {
        let mut partial = schema_rows();
        partial.rows.pop();
        for present in [rows(&["type", "name", "tbl_name", "sql"], vec![]), partial] {
            let trace = Arc::new(Mutex::new(Trace {
                steps: vec![Step::Query(SELECT_SCHEMA, present), Step::Rollback(Ok(()))].into(),
                ..Trace::default()
            }));
            assert!(matches!(
                RunRepository::open_existing(FakeDatabase(trace.clone())),
                Err(RepositoryError::Schema)
            ));
            assert!(trace.lock().unwrap().writes.is_empty());
            assert_eq!(trace.lock().unwrap().commits, 0);
            finished(&trace);
        }
    }

    #[test]
    fn existing_only_open_checks_marker_and_preserves_commit_uncertainty() {
        for ack in [Ok(()), Err(SqlError::commit_unknown())] {
            let unknown = ack.is_err();
            let trace = Arc::new(Mutex::new(Trace {
                steps: vec![
                    Step::Query(SELECT_SCHEMA, schema_rows()),
                    marker_query(),
                    Step::Commit(ack),
                ]
                .into(),
                ..Trace::default()
            }));
            let result = RunRepository::open_existing(FakeDatabase(trace.clone()));
            if unknown {
                assert!(matches!(result, Err(RepositoryError::CommitUnknown)));
            } else {
                assert!(result.is_ok());
            }
            assert!(trace.lock().unwrap().writes.is_empty());
            assert_eq!(trace.lock().unwrap().queries.len(), 2);
            finished(&trace);
        }
    }

    #[test]
    fn optional_existing_open_confirms_absence_without_creating_schema() {
        let trace = Arc::new(Mutex::new(Trace {
            steps: vec![
                Step::Query(
                    SELECT_SCHEMA,
                    rows(&["type", "name", "tbl_name", "sql"], vec![]),
                ),
                Step::Commit(Ok(())),
            ]
            .into(),
            ..Trace::default()
        }));
        assert!(
            RunRepository::open_existing_optional(FakeDatabase(trace.clone()))
                .unwrap()
                .is_none()
        );
        let trace = trace.lock().unwrap();
        assert!(trace.writes.is_empty());
        assert_eq!((trace.begins, trace.commits, trace.rollbacks), (1, 1, 0));
        assert_eq!(trace.queries.len(), 1);
        assert!(trace.steps.is_empty());
    }

    #[test]
    fn optional_existing_open_reads_a_missing_run_without_ddl_or_claims() {
        let trace = Arc::new(Mutex::new(Trace {
            steps: vec![
                Step::Query(SELECT_SCHEMA, schema_rows()),
                marker_query(),
                Step::Commit(Ok(())),
                q(
                    SELECT_HEAD,
                    &["run_request_digest", "revision", "snapshot_digest"],
                    vec![],
                ),
                Step::Commit(Ok(())),
            ]
            .into(),
            ..Trace::default()
        }));
        let mut repo = RunRepository::open_existing_optional(FakeDatabase(trace.clone()))
            .unwrap()
            .unwrap();
        assert!(repo.load(&identity().run_id).unwrap().is_none());
        let trace = trace.lock().unwrap();
        assert!(trace.writes.is_empty());
        assert_eq!((trace.begins, trace.commits, trace.rollbacks), (2, 2, 0));
        assert_eq!(trace.queries.len(), 3);
        assert!(trace.steps.is_empty());
    }

    #[test]
    fn optional_existing_open_rejects_partial_or_foreign_schema_as_errors() {
        let mut partial = schema_rows();
        partial.rows.pop();
        let mut foreign = schema_rows();
        foreign.rows.push(vec![
            text("trigger"),
            text("foreign"),
            text("dt_run_heads"),
            text("CREATE TRIGGER foreign"),
        ]);
        for present in [partial, foreign] {
            let trace = Arc::new(Mutex::new(Trace {
                steps: vec![Step::Query(SELECT_SCHEMA, present), Step::Rollback(Ok(()))].into(),
                ..Trace::default()
            }));
            assert!(matches!(
                RunRepository::open_existing_optional(FakeDatabase(trace.clone())),
                Err(RepositoryError::Schema)
            ));
            assert!(trace.lock().unwrap().writes.is_empty());
            assert_eq!(trace.lock().unwrap().commits, 0);
            finished(&trace);
        }
    }

    #[test]
    fn optional_existing_open_rejects_wrong_version_or_digest() {
        for (version, digest) in [(2, schema_digest()), (1, Digest::hash(b"foreign-schema"))] {
            let trace = Arc::new(Mutex::new(Trace {
                steps: vec![
                    Step::Query(SELECT_SCHEMA, schema_rows()),
                    q(
                        "SELECT slot,version,schema_digest",
                        &["slot", "version", "schema_digest"],
                        vec![vec![
                            text("schema"),
                            SqlCell::Integer(version),
                            text(digest.as_str()),
                        ]],
                    ),
                    Step::Rollback(Ok(())),
                ]
                .into(),
                ..Trace::default()
            }));
            assert!(matches!(
                RunRepository::open_existing_optional(FakeDatabase(trace.clone())),
                Err(RepositoryError::Schema)
            ));
            assert!(trace.lock().unwrap().writes.is_empty());
            finished(&trace);
        }
    }

    #[test]
    fn optional_existing_open_never_converts_query_or_rollback_failure_to_absence() {
        for rollback_unknown in [false, true] {
            let trace = Arc::new(Mutex::new(Trace {
                steps: vec![
                    Step::QueryError(SELECT_SCHEMA, SqlError::new(SqlErrorKind::Denied)),
                    Step::Rollback(if rollback_unknown {
                        Err(SqlError::commit_unknown())
                    } else {
                        Ok(())
                    }),
                ]
                .into(),
                ..Trace::default()
            }));
            let expected = if rollback_unknown {
                RepositoryError::CommitUnknown
            } else {
                RepositoryError::Storage(SqlErrorKind::Denied)
            };
            assert!(
                matches!(RunRepository::open_existing_optional(FakeDatabase(trace.clone())), Err(error) if error == expected)
            );
            assert!(trace.lock().unwrap().writes.is_empty());
            assert_eq!(trace.lock().unwrap().commits, 0);
            finished(&trace);
        }
    }

    #[test]
    fn optional_existing_open_preserves_unknown_commit_even_for_absent_schema() {
        for present in [false, true] {
            let mut steps = vec![Step::Query(
                SELECT_SCHEMA,
                if present {
                    schema_rows()
                } else {
                    rows(&["type", "name", "tbl_name", "sql"], vec![])
                },
            )];
            if present {
                steps.push(marker_query());
            }
            steps.push(Step::Commit(Err(SqlError::commit_unknown())));
            let trace = Arc::new(Mutex::new(Trace {
                steps: steps.into(),
                ..Trace::default()
            }));
            assert!(matches!(
                RunRepository::open_existing_optional(FakeDatabase(trace.clone())),
                Err(RepositoryError::CommitUnknown)
            ));
            assert!(trace.lock().unwrap().writes.is_empty());
            assert_eq!(trace.lock().unwrap().rollbacks, 0);
            finished(&trace);
        }
    }

    #[test]
    fn schema_reopen_rejects_an_added_trigger() {
        let mut altered = schema_rows();
        altered.rows.push(vec![
            text("trigger"),
            text("surprise"),
            text("dt_generation_claims"),
            text("CREATE TRIGGER surprise"),
        ]);
        let trace = Arc::new(Mutex::new(Trace {
            steps: vec![Step::Query(SELECT_SCHEMA, altered), Step::Rollback(Ok(()))].into(),
            ..Trace::default()
        }));
        assert!(matches!(
            RunRepository::open(FakeDatabase(trace.clone())),
            Err(RepositoryError::Schema)
        ));
        finished(&trace);
    }

    #[test]
    fn schema_initialization_is_all_or_nothing_and_rechecks_the_actual_schema() {
        let mut steps = vec![Step::Query(
            SELECT_SCHEMA,
            rows(&["type", "name", "tbl_name", "sql"], vec![]),
        )];
        steps.extend(SCHEMA.iter().map(|_| Step::Exec("CREATE TABLE", Ok(0))));
        steps.extend([
            write("INSERT INTO dt_repository_schema"),
            Step::Query(SELECT_SCHEMA, schema_rows()),
            marker_query(),
            Step::Commit(Ok(())),
        ]);
        let trace = Arc::new(Mutex::new(Trace {
            steps: steps.into(),
            ..Trace::default()
        }));
        RunRepository::open(FakeDatabase(trace.clone())).unwrap();
        assert_eq!(trace.lock().unwrap().commits, 1);
        finished(&trace);
    }

    #[test]
    fn bounds_and_integer_overflow_fail_before_sql() {
        for bad in [
            "x".repeat(64),
            "A".repeat(64),
            format!("sha256:{}", "a".repeat(64)),
        ] {
            assert!(Digest::parse(&bad).is_err());
        }
        let (mut repo, trace) = fixture(vec![]);
        let mut exhausted = head();
        exhausted.revision = i64::MAX;
        assert_eq!(
            repo.commit_snapshot(&exhausted, b"next", &[]),
            Err(RepositoryError::Limit)
        );
        assert_eq!(
            repo.create_fresh(&identity(), &vec![0; MAX_RECORD_BYTES + 1]),
            Err(RepositoryError::Limit)
        );
        assert_eq!(
            repo.save_progress(&head(), &claim(), u64::MAX, b"progress"),
            Err(RepositoryError::Limit)
        );
        assert_eq!(
            repo.save_progress(&head(), &claim(), 0, b"progress"),
            Err(RepositoryError::Invalid)
        );
        assert_eq!(trace.lock().unwrap().begins, 0);
    }

    #[test]
    fn duplicate_settlement_and_adoption_digest_mismatch_fail_before_sql() {
        let (mut repo, trace) = fixture(vec![]);
        let settlement = resolution(ResolutionDisposition::AcceptedCompletion);
        assert_eq!(
            repo.commit_snapshot(&head(), b"next", &[settlement.clone(), settlement]),
            Err(RepositoryError::Invalid)
        );
        let adoption = LegacyAdoption {
            source_snapshot_digest: Digest::hash(b"other"),
            retirement_receipt: b"explicit external retirement evidence".to_vec(),
            historical_claims: vec![claim()],
            settlements: vec![],
        };
        assert_eq!(
            repo.adopt_legacy(&identity(), b"legacy", &adoption),
            Err(RepositoryError::Invalid)
        );
        assert_eq!(trace.lock().unwrap().begins, 0);
    }

    #[test]
    fn adoption_records_history_without_dispatch_and_cannot_be_replaced_by_fresh_create() {
        let legacy = b"legacy";
        let adoption = LegacyAdoption {
            source_snapshot_digest: Digest::hash(legacy),
            retirement_receipt: b"externally verified exited worker receipt".to_vec(),
            historical_claims: vec![claim()],
            settlements: vec![],
        };
        let adopted = Head {
            run_id: identity().run_id,
            run_request_digest: identity().run_request_digest,
            revision: 0,
            snapshot_digest: Digest::hash(legacy),
        };
        let (mut repo, trace) = fixture(vec![
            q(
                SELECT_HEAD,
                &["run_request_digest", "revision", "snapshot_digest"],
                vec![],
            ),
            write("INSERT INTO dt_run_heads"),
            write("INSERT INTO dt_run_snapshots"),
            write("INSERT INTO dt_snapshot_chunks"),
            write("INSERT INTO dt_generation_claims"),
            write("INSERT INTO dt_legacy_adoptions"),
            Step::Commit(Ok(())),
            head_query(&adopted),
            Step::Rollback(Ok(())),
            head_query(&adopted),
            blockers(vec![vec![text(&claim().attempt_id), SqlCell::Null]]),
            Step::Rollback(Ok(())),
        ]);
        assert_eq!(
            repo.adopt_legacy(&identity(), legacy, &adoption).unwrap(),
            adopted
        );
        assert_eq!(
            repo.create_fresh(&identity(), b"replacement"),
            Err(RepositoryError::Conflict)
        );
        assert!(matches!(
            repo.claim_generation(&adopted, b"pending", &claim()),
            Err(RepositoryError::PendingGeneration)
        ));
        finished(&trace);
    }

    #[test]
    fn snapshot_over_one_mib_uses_bounded_cells_and_query_batches() {
        let bytes: Vec<u8> = (0..CHUNK_BYTES * 17 + 9)
            .map(|index| (index % 251) as u8)
            .collect();
        let next = next_head(&head(), &bytes).unwrap();
        let mut steps = vec![head_query(&head())];
        steps.extend(snapshot_writes(&bytes));
        steps.push(Step::Commit(Ok(())));
        steps.push(head_query(&next));
        steps.push(q(
            "SELECT previous_digest,snapshot_digest,byte_len,chunk_count",
            &[
                "previous_digest",
                "snapshot_digest",
                "byte_len",
                "chunk_count",
            ],
            vec![vec![
                text(head().snapshot_digest.as_str()),
                text(next.snapshot_digest.as_str()),
                SqlCell::Integer(bytes.len() as i64),
                SqlCell::Integer(18),
            ]],
        ));
        steps.push(q(
            "SELECT COUNT(*) AS total FROM dt_snapshot_chunks",
            &["total"],
            vec![vec![SqlCell::Integer(18)]],
        ));
        let chunks = bytes
            .chunks(CHUNK_BYTES)
            .enumerate()
            .map(|(index, chunk)| vec![SqlCell::Integer(index as i64), bytes_cell(chunk)])
            .collect::<Vec<_>>();
        for batch in chunks.chunks(CHUNK_BATCH_ROWS as usize) {
            steps.push(q(
                "SELECT ordinal,payload FROM dt_snapshot_chunks",
                &["ordinal", "payload"],
                batch.to_vec(),
            ));
        }
        steps.push(Step::Commit(Ok(())));
        let (mut repo, trace) = fixture(steps);
        assert_eq!(repo.commit_snapshot(&head(), &bytes, &[]).unwrap(), next);
        assert_eq!(repo.load(&head().run_id).unwrap().unwrap().bytes, bytes);
        assert!(
            trace
                .lock()
                .unwrap()
                .writes
                .iter()
                .flat_map(|(_, params)| params)
                .all(|cell| match cell {
                    SqlCell::Blob(_) => false,
                    SqlCell::Text(value) => value.len() <= CHUNK_BYTES * 2,
                    _ => true,
                })
        );
        finished(&trace);
    }

    #[test]
    fn chunk_order_length_and_typed_integer_corruption_are_rejected() {
        let mut out = Vec::new();
        assert_eq!(
            append_chunk_rows(
                &mut out,
                vec![vec![SqlCell::Integer(1), bytes_cell(&[1])]],
                1,
                0
            ),
            Err(RepositoryError::Integrity)
        );
        assert_eq!(
            append_chunk_rows(&mut out, vec![vec![text("0"), bytes_cell(&[1])]], 1, 0),
            Err(RepositoryError::Integrity)
        );
        assert_eq!(
            append_chunk_rows(
                &mut out,
                vec![vec![SqlCell::Integer(0), bytes_cell(&[1, 2])]],
                1,
                0
            ),
            Err(RepositoryError::Integrity)
        );
        assert_eq!(
            record_dimensions(&SqlCell::Integer(-1), &SqlCell::Integer(1)),
            Err(RepositoryError::Integrity)
        );
        assert_eq!(
            record_dimensions(&SqlCell::Integer(1), &SqlCell::Integer(2)),
            Err(RepositoryError::Integrity)
        );
    }

    #[test]
    fn progress_reuses_unchanged_chunks_and_updates_only_latest_payload() {
        let mut old = vec![b'a'; CHUNK_BYTES];
        old.extend_from_slice(b"p1");
        let mut new = vec![b'a'; CHUNK_BYTES];
        new.extend_from_slice(b"p2");
        let stamp = ProgressStamp {
            sequence: 1,
            digest: Digest::hash(&old),
        };
        let (mut repo, trace) = fixture(vec![
            progress_preconditions_query(vec![progress_precondition_row(
                &head(),
                &claim(),
                Some((&stamp, old.len())),
            )]),
            chunk_metadata_query(&old),
            write("INSERT INTO dt_progress_records"),
            write("UPDATE dt_progress_chunks"),
            write("UPDATE dt_progress_heads"),
            Step::Commit(Ok(())),
        ]);
        assert_eq!(
            repo.save_progress(&head(), &claim(), 2, &new).unwrap(),
            ProgressStamp {
                sequence: 2,
                digest: Digest::hash(&new)
            }
        );
        let trace = trace.lock().unwrap();
        assert_eq!(trace.writes.len(), 3);
        assert_eq!(trace.queries.len(), 2);
        assert_eq!(
            trace.queries[0].1,
            [text(&claim().attempt_id), text(&head().run_id)]
        );
        assert_eq!(trace.queries[0].2, 2);
        assert_eq!(trace.queries[1].2, 129);
        assert_eq!(trace.begins, 1);
        assert_eq!(trace.commits, 1);
        assert!(
            !trace
                .writes
                .iter()
                .any(|(sql, _)| sql.contains("dt_run_snapshots") || sql.starts_with("DELETE"))
        );
        assert_eq!(trace.writes[1].1[1], bytes_cell(b"p2"));
        assert!(trace.steps.is_empty());
    }

    #[test]
    fn progress_cannot_overwrite_a_resolved_attempt_or_repeat_a_sequence() {
        let stamp = ProgressStamp {
            sequence: 5,
            digest: Digest::hash(b"prefix"),
        };
        let mut resolved = progress_precondition_row(&head(), &claim(), Some((&stamp, 6)));
        resolved[10] = text(&claim().attempt_id);
        let (mut repo, trace) = fixture(vec![
            progress_preconditions_query(vec![resolved]),
            Step::Rollback(Ok(())),
            progress_preconditions_query(vec![progress_precondition_row(
                &head(),
                &claim(),
                Some((&stamp, 6)),
            )]),
            Step::Rollback(Ok(())),
        ]);
        assert_eq!(
            repo.save_progress(&head(), &claim(), 6, b"new-prefix"),
            Err(RepositoryError::Conflict)
        );
        assert_eq!(
            repo.save_progress(&head(), &claim(), 5, b"prefix"),
            Err(RepositoryError::Conflict)
        );
        assert!(trace.lock().unwrap().writes.is_empty());
        finished(&trace);
    }

    #[test]
    fn initial_progress_uses_two_bounded_queries_and_does_not_advance_run_head() {
        let (mut repo, trace) = fixture(vec![
            progress_preconditions_query(vec![progress_precondition_row(&head(), &claim(), None)]),
            chunk_metadata_query(&[]),
            write("INSERT INTO dt_progress_records"),
            write("INSERT INTO dt_progress_chunks"),
            write("INSERT INTO dt_progress_heads"),
            Step::Commit(Ok(())),
        ]);
        let saved = repo
            .save_progress(&head(), &claim(), 1, b"first-prefix")
            .unwrap();
        assert_eq!(saved.sequence, 1);
        assert_eq!(saved.digest, Digest::hash(b"first-prefix"));
        let trace = trace.lock().unwrap();
        assert_eq!(trace.queries.len(), 2);
        assert_eq!(
            trace.queries[0].1,
            [text(&claim().attempt_id), text(&head().run_id)]
        );
        assert_eq!(trace.queries[0].2, 2);
        assert_eq!(
            trace.queries[1].1,
            [text(&claim().run_id), text(&claim().attempt_id)]
        );
        assert_eq!(trace.queries[1].2, 129);
        assert_eq!(trace.writes.len(), 3);
        assert!(trace.writes.iter().all(|(sql, _)| !sql.contains("dt_run_")));
        assert_eq!((trace.begins, trace.commits, trace.rollbacks), (1, 1, 0));
        assert!(trace.steps.is_empty());
    }

    #[test]
    fn progress_scalar_join_requires_one_exact_typed_row() {
        let row = progress_precondition_row(&head(), &claim(), None);
        let mut malformed_columns = rows(PROGRESS_PRECONDITION_COLUMNS, vec![row.clone()]);
        malformed_columns.columns.swap(0, 3);
        let mut short_row = row.clone();
        short_row.pop();
        for (returned, error) in [
            (
                rows(PROGRESS_PRECONDITION_COLUMNS, vec![]),
                RepositoryError::Conflict,
            ),
            (
                rows(
                    PROGRESS_PRECONDITION_COLUMNS,
                    vec![row.clone(), row.clone()],
                ),
                RepositoryError::Integrity,
            ),
            (
                rows(
                    PROGRESS_PRECONDITION_COLUMNS,
                    vec![row.clone(), row.clone(), row],
                ),
                RepositoryError::Integrity,
            ),
            (
                rows(PROGRESS_PRECONDITION_COLUMNS, vec![short_row]),
                RepositoryError::Integrity,
            ),
            (malformed_columns, RepositoryError::Integrity),
        ] {
            let (mut repo, trace) = fixture(vec![
                Step::Query(SELECT_PROGRESS_PRECONDITIONS, returned),
                Step::Rollback(Ok(())),
            ]);
            assert_eq!(
                repo.save_progress(&head(), &claim(), 1, b"prefix"),
                Err(error)
            );
            assert!(trace.lock().unwrap().writes.is_empty());
            finished(&trace);
        }
    }

    #[test]
    fn progress_scalar_join_preserves_head_and_claim_identity_and_revision_guards() {
        let base = progress_precondition_row(&head(), &claim(), None);
        let mut cases = Vec::new();
        // Valid but stale identities/revisions are conflicts, not new claims.
        for (column, value) in [
            (0, text(Digest::hash(b"other-run-request").as_str())),
            (1, SqlCell::Integer(4)),
            (2, text(Digest::hash(b"other-snapshot").as_str())),
            (3, text(Digest::hash(b"other-claim-run-request").as_str())),
            (4, text("other-phase")),
            (5, text("other-generation")),
            (7, text(Digest::hash(b"other-request").as_str())),
            (8, text(Digest::hash(b"other-route").as_str())),
            (9, SqlCell::Integer(4)),
            (10, text(&claim().attempt_id)),
        ] {
            let mut row = base.clone();
            row[column] = value;
            cases.push((row, RepositoryError::Conflict));
        }
        let mut missing_claim = base.clone();
        missing_claim[3..10].fill(SqlCell::Null);
        cases.push((missing_claim, RepositoryError::Conflict));
        for (column, value) in [
            (0, text("not-a-digest")),
            (1, SqlCell::Integer(-1)),
            (1, text("3")),
            (3, SqlCell::Null),
            (4, text("")),
            (5, text(&"x".repeat(MAX_ID_BYTES + 1))),
            (6, text("another-attempt")),
            (8, text("BAD")),
            (9, SqlCell::Integer(-1)),
            (9, text("3")),
            (10, SqlCell::Integer(1)),
            (10, text("another-attempt")),
        ] {
            let mut row = base.clone();
            row[column] = value;
            cases.push((row, RepositoryError::Integrity));
        }
        for (row, error) in cases {
            let (mut repo, trace) = fixture(vec![
                progress_preconditions_query(vec![row]),
                Step::Rollback(Ok(())),
            ]);
            assert_eq!(
                repo.save_progress(&head(), &claim(), 1, b"prefix"),
                Err(error)
            );
            assert!(trace.lock().unwrap().writes.is_empty());
            finished(&trace);
        }
    }

    #[test]
    fn progress_scalar_join_rejects_partial_progress_or_inconsistent_dimensions() {
        let stamp = ProgressStamp {
            sequence: 1,
            digest: Digest::hash(b"old"),
        };
        let base = progress_precondition_row(&head(), &claim(), Some((&stamp, 3)));
        for (column, value) in [
            (11, SqlCell::Null),
            (11, SqlCell::Integer(0)),
            (11, SqlCell::Integer(-1)),
            (11, text("1")),
            (12, SqlCell::Null),
            (12, text("not-a-digest")),
            (13, SqlCell::Null),
            (13, text(Digest::hash(b"different-record").as_str())),
            (14, SqlCell::Null),
            (14, SqlCell::Integer(0)),
            (14, SqlCell::Integer(MAX_RECORD_BYTES as i64 + 1)),
            (14, text("3")),
            (15, SqlCell::Null),
            (15, SqlCell::Integer(2)),
        ] {
            let mut row = base.clone();
            row[column] = value;
            let (mut repo, trace) = fixture(vec![
                progress_preconditions_query(vec![row]),
                Step::Rollback(Ok(())),
            ]);
            assert_eq!(
                repo.save_progress(&head(), &claim(), 2, b"prefix"),
                Err(RepositoryError::Integrity)
            );
            assert!(trace.lock().unwrap().writes.is_empty());
            finished(&trace);
        }
        let mut orphan_record = progress_precondition_row(&head(), &claim(), None);
        orphan_record[13] = text(stamp.digest.as_str());
        let (mut repo, trace) = fixture(vec![
            progress_preconditions_query(vec![orphan_record]),
            Step::Rollback(Ok(())),
        ]);
        assert_eq!(
            repo.save_progress(&head(), &claim(), 2, b"prefix"),
            Err(RepositoryError::Integrity)
        );
        finished(&trace);
    }

    #[test]
    fn progress_save_still_checks_separate_chunk_metadata_before_any_write() {
        let stamp = ProgressStamp {
            sequence: 1,
            digest: Digest::hash(b"old"),
        };
        for metadata in [
            vec![],
            vec![vec![
                SqlCell::Integer(1),
                text(stamp.digest.as_str()),
                SqlCell::Integer(6),
            ]],
            vec![vec![
                SqlCell::Integer(0),
                text(stamp.digest.as_str()),
                SqlCell::Integer(5),
            ]],
            vec![vec![
                SqlCell::Integer(0),
                text(stamp.digest.as_str()),
                SqlCell::Integer(4),
            ]],
            vec![vec![
                SqlCell::Integer(0),
                text("not-a-digest"),
                SqlCell::Integer(6),
            ]],
        ] {
            let (mut repo, trace) = fixture(vec![
                progress_preconditions_query(vec![progress_precondition_row(
                    &head(),
                    &claim(),
                    Some((&stamp, 3)),
                )]),
                q(
                    "SELECT ordinal,chunk_digest,length(payload)",
                    &["ordinal", "chunk_digest", "byte_len"],
                    metadata,
                ),
                Step::Rollback(Ok(())),
            ]);
            assert_eq!(
                repo.save_progress(&head(), &claim(), 2, b"prefix"),
                Err(RepositoryError::Integrity)
            );
            assert!(trace.lock().unwrap().writes.is_empty());
            assert_eq!(trace.lock().unwrap().queries.len(), 2);
            finished(&trace);
        }
        // Missing progress head cannot hide orphan latest payload chunks.
        let (mut repo, trace) = fixture(vec![
            progress_preconditions_query(vec![progress_precondition_row(&head(), &claim(), None)]),
            chunk_metadata_query(b"orphan"),
            Step::Rollback(Ok(())),
        ]);
        assert_eq!(
            repo.save_progress(&head(), &claim(), 1, b"prefix"),
            Err(RepositoryError::Integrity)
        );
        finished(&trace);
    }

    #[test]
    fn progress_save_keeps_exact_chunk_and_head_cas_and_does_not_retry_unknown_commit() {
        let stamp = ProgressStamp {
            sequence: 1,
            digest: Digest::hash(b"old"),
        };
        for failed_write in 0..2 {
            let mut steps = vec![
                progress_preconditions_query(vec![progress_precondition_row(
                    &head(),
                    &claim(),
                    Some((&stamp, 3)),
                )]),
                chunk_metadata_query(b"old"),
                write("INSERT INTO dt_progress_records"),
            ];
            if failed_write == 0 {
                steps.push(Step::Exec("UPDATE dt_progress_chunks", Ok(0)));
            } else {
                steps.push(write("UPDATE dt_progress_chunks"));
                steps.push(Step::Exec("UPDATE dt_progress_heads", Ok(0)));
            }
            steps.push(Step::Rollback(Ok(())));
            let (mut repo, trace) = fixture(steps);
            assert_eq!(
                repo.save_progress(&head(), &claim(), 2, b"new"),
                Err(RepositoryError::Conflict)
            );
            let trace = trace.lock().unwrap();
            assert_eq!(trace.writes[1].1[5], text(stamp.digest.as_str()));
            assert_eq!(trace.writes[1].1[6], SqlCell::Integer(6));
            if failed_write == 1 {
                assert_eq!(trace.writes[2].1[4], SqlCell::Integer(1));
                assert_eq!(trace.writes[2].1[5], text(stamp.digest.as_str()));
            }
            assert_eq!((trace.begins, trace.commits, trace.rollbacks), (1, 0, 1));
            assert!(trace.steps.is_empty());
        }
        let (mut repo, trace) = fixture(vec![
            progress_preconditions_query(vec![progress_precondition_row(
                &head(),
                &claim(),
                Some((&stamp, 3)),
            )]),
            chunk_metadata_query(b"old"),
            write("INSERT INTO dt_progress_records"),
            write("UPDATE dt_progress_chunks"),
            write("UPDATE dt_progress_heads"),
            Step::Commit(Err(SqlError::commit_unknown())),
        ]);
        assert_eq!(
            repo.save_progress(&head(), &claim(), 2, b"new"),
            Err(RepositoryError::CommitUnknown)
        );
        let trace = trace.lock().unwrap();
        assert_eq!((trace.begins, trace.commits, trace.rollbacks), (1, 1, 0));
        assert_eq!(trace.queries.len(), 2);
        assert!(trace.steps.is_empty());
    }

    #[test]
    fn progress_save_surplus_chunk_deletion_retains_exact_affected_count_guard() {
        let old = vec![b'a'; CHUNK_BYTES + 1];
        let new = &old[..CHUNK_BYTES];
        let stamp = ProgressStamp {
            sequence: 1,
            digest: Digest::hash(&old),
        };
        for deleted in [0, 1, 2] {
            let mut steps = vec![
                progress_preconditions_query(vec![progress_precondition_row(
                    &head(),
                    &claim(),
                    Some((&stamp, old.len())),
                )]),
                chunk_metadata_query(&old),
                write("INSERT INTO dt_progress_records"),
                Step::Exec("DELETE FROM dt_progress_chunks", Ok(deleted)),
            ];
            if deleted == 1 {
                steps.extend([write("UPDATE dt_progress_heads"), Step::Commit(Ok(()))]);
            } else {
                steps.push(Step::Rollback(Ok(())));
            }
            let (mut repo, trace) = fixture(steps);
            let result = repo.save_progress(&head(), &claim(), 2, new);
            if deleted == 1 {
                assert_eq!(result.unwrap().digest, Digest::hash(new));
            } else {
                assert_eq!(result, Err(RepositoryError::Conflict));
            }
            let trace = trace.lock().unwrap();
            assert_eq!(trace.writes[1].1[2], SqlCell::Integer(1));
            assert!(
                !trace
                    .writes
                    .iter()
                    .any(|(sql, _)| sql.starts_with("UPDATE dt_progress_chunks"))
            );
            assert!(trace.steps.is_empty());
        }
    }

    fn resolution_read_steps(head: &Head, resolution: &Resolution) -> Vec<Step> {
        let (sequence, progress_digest) = resolution
            .expected_progress
            .as_ref()
            .map(|stamp| {
                (
                    SqlCell::Integer(stamp.sequence as i64),
                    text(stamp.digest.as_str()),
                )
            })
            .unwrap_or((SqlCell::Null, SqlCell::Null));
        vec![
            q(
                SELECT_RESOLUTION,
                &[
                    "disposition",
                    "receipt_digest",
                    "byte_len",
                    "chunk_count",
                    "resolved_revision",
                    "progress_sequence",
                    "progress_digest",
                ],
                vec![vec![
                    text(resolution.disposition.as_str()),
                    text(resolution.digest().as_str()),
                    SqlCell::Integer(resolution.receipt.len() as i64),
                    SqlCell::Integer(1),
                    SqlCell::Integer(head.revision),
                    sequence,
                    progress_digest,
                ]],
            ),
            claim_query(&resolution.claim, head.revision),
            head_query(head),
            progress_query(resolution.expected_progress.as_ref()),
            q(
                "SELECT COUNT(*) AS total FROM dt_resolution_chunks",
                &["total"],
                vec![vec![SqlCell::Integer(1)]],
            ),
            q(
                "SELECT ordinal,payload FROM dt_resolution_chunks",
                &["ordinal", "payload"],
                vec![vec![SqlCell::Integer(0), bytes_cell(&resolution.receipt)]],
            ),
        ]
    }

    #[test]
    fn explicit_successor_requires_unique_authorization_and_confirmed_commit() {
        let old = resolution(ResolutionDisposition::TerminalContentUnavailable);
        let mut successor = claim();
        successor.attempt_id = "new-attempt".into();
        successor.generation_key = "goal_contract/0#1".into();
        let authorization = SuccessorAuthorization {
            old_attempt_id: claim().attempt_id,
            resolution_digest: old.digest(),
            receipt: b"verified explicit user regeneration".to_vec(),
        };
        let mut steps = vec![head_query(&head())];
        steps.extend(resolution_read_steps(&head(), &old));
        steps.extend([
            blockers(vec![vec![
                text(&claim().attempt_id),
                text("terminal_content_unavailable"),
            ]]),
            write("INSERT INTO dt_generation_claims"),
            write("INSERT INTO dt_successor_authorizations"),
        ]);
        steps.extend(snapshot_writes(b"pending-successor"));
        steps.push(Step::Commit(Ok(())));
        let (mut repo, trace) = fixture(steps);
        let (_, permit) = repo
            .claim_successor(&head(), b"pending-successor", &authorization, &successor)
            .unwrap();
        assert_eq!(permit.consume(&successor), Ok(()));
        finished(&trace);
    }

    #[test]
    fn successor_unique_conflict_rolls_back_new_claim_and_never_mints() {
        let old = resolution(ResolutionDisposition::TerminalContentUnavailable);
        let mut successor = claim();
        successor.attempt_id = "new-attempt".into();
        successor.generation_key = "goal_contract/0#1".into();
        let authorization = SuccessorAuthorization {
            old_attempt_id: claim().attempt_id,
            resolution_digest: old.digest(),
            receipt: b"different user nonce cannot bypass unique old attempt".to_vec(),
        };
        let mut steps = vec![head_query(&head())];
        steps.extend(resolution_read_steps(&head(), &old));
        steps.extend([
            blockers(vec![vec![
                text(&claim().attempt_id),
                text("terminal_content_unavailable"),
            ]]),
            write("INSERT INTO dt_generation_claims"),
            Step::Exec(
                "INSERT INTO dt_successor_authorizations",
                Err(SqlError::new(SqlErrorKind::Conflict)),
            ),
            Step::Rollback(Ok(())),
        ]);
        let (mut repo, trace) = fixture(steps);
        assert!(matches!(
            repo.claim_successor(&head(), b"pending-successor", &authorization, &successor),
            Err(RepositoryError::Conflict)
        ));
        assert_eq!(trace.lock().unwrap().commits, 0);
        finished(&trace);
    }

    #[test]
    fn receipt_digest_binds_progress_disposition_and_exact_usage_bytes() {
        let original = resolution(ResolutionDisposition::AcceptedCompletion);
        let mut changed = original.clone();
        changed.disposition = ResolutionDisposition::RejectedCompletion;
        assert_ne!(original.digest(), changed.digest());
        changed = original.clone();
        changed.receipt.push(b' ');
        assert_ne!(original.digest(), changed.digest());
        changed = original.clone();
        changed.expected_progress = Some(ProgressStamp {
            sequence: 1,
            digest: Digest::hash(b"prefix"),
        });
        assert_ne!(original.digest(), changed.digest());
        changed = original.clone();
        changed.claim.request_digest = Digest::hash(b"different-request");
        assert_ne!(original.digest(), changed.digest());
    }

    #[test]
    fn opaque_byte_storage_is_lowercase_hex_text_with_exact_raw_hashes() {
        let raw: Vec<u8> = (0..=255).cycle().take(CHUNK_BYTES).collect();
        let encoded = bytes_cell(&raw);
        let SqlCell::Text(value) = &encoded else {
            panic!("opaque storage used the BLOB ABI")
        };
        assert_eq!(value.len(), CHUNK_BYTES * 2);
        assert!(
            value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        );
        let decoded = cell_bytes(&encoded).unwrap();
        assert_eq!(decoded, raw);
        assert_eq!(Digest::hash(&decoded), Digest::hash(&raw));
        assert!(SCHEMA.iter().all(|(_, ddl)| !ddl.contains(" BLOB ")));
    }

    #[test]
    fn opaque_text_codec_rejects_noncanonical_or_unbounded_storage() {
        for value in ["", "0", "00A0", "0x00", "猫", "0 ", "gg"] {
            assert_eq!(cell_bytes(&text(value)), Err(RepositoryError::Integrity));
        }
        assert_eq!(
            cell_bytes(&SqlCell::Blob(vec![0])),
            Err(RepositoryError::Integrity)
        );
        assert_eq!(
            cell_bytes(&text(&"00".repeat(CHUNK_BYTES + 1))),
            Err(RepositoryError::Integrity)
        );
        for length in [-1, 0, 1, (CHUNK_BYTES * 2 + 2) as i64] {
            assert_eq!(
                encoded_byte_length(&SqlCell::Integer(length)),
                Err(RepositoryError::Integrity)
            );
        }
    }
}
