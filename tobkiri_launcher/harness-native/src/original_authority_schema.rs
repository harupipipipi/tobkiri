//! Only the schema/audit/model closure actually reached by AuthorityStore open.
//! Permission editing, granting and approval APIs are deliberately absent.
#[cfg(unix)]
use super::io::{Database, Rows, Store};
use super::*;
use rusqlite::{params, types::Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Mutex, OnceLock},
    time::{Duration, Instant},
};
use tobkiri_canon::{Variant, encode};
use tobkiri_gate::{ControlKind, ControlReservation};

const DDL: &str = r#"
CREATE TABLE IF NOT EXISTS authority_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS authority_records (record_type TEXT NOT NULL, record_id TEXT NOT NULL,
 record_digest TEXT NOT NULL, encrypted_payload BLOB NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY (record_type, record_id)) STRICT;
CREATE TABLE IF NOT EXISTS execution_sessions (session_id TEXT PRIMARY KEY, domain_id TEXT NOT NULL,
 profile_id TEXT NOT NULL, activation_id TEXT NOT NULL, boot_epoch INTEGER NOT NULL,
 channel_digest TEXT NOT NULL, principal_id TEXT NOT NULL, active INTEGER NOT NULL CHECK (active IN (0, 1)),
 created_at REAL NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS grant_usage (grant_id TEXT PRIMARY KEY, reserved_uses INTEGER NOT NULL DEFAULT 0,
 committed_uses INTEGER NOT NULL DEFAULT 0) STRICT;
CREATE TABLE IF NOT EXISTS invocation_leases (lease_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
 lease_digest TEXT NOT NULL, encrypted_payload BLOB NOT NULL, caller_principal_id TEXT NOT NULL,
 target_principal_id TEXT NOT NULL, caller_artifact_digest TEXT NOT NULL, target_artifact_digest TEXT NOT NULL,
 caller_publisher_lineage TEXT NOT NULL, target_publisher_lineage TEXT NOT NULL, host_extension_id TEXT NOT NULL,
 caller_domain_id TEXT NOT NULL, target_domain_id TEXT NOT NULL, profile_id TEXT NOT NULL, activation_id TEXT NOT NULL,
 grant_id TEXT NOT NULL, provider_authority_id TEXT NOT NULL, audit_reservation_id TEXT NOT NULL,
 security_epoch INTEGER NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL, outcome_digest TEXT) STRICT;
CREATE TABLE IF NOT EXISTS revocations (revocation_id TEXT PRIMARY KEY, target_kind TEXT NOT NULL,
 target_id TEXT NOT NULL, security_epoch INTEGER NOT NULL, reason_digest TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE (target_kind, target_id)) STRICT;
CREATE INDEX IF NOT EXISTS revocations_target ON revocations(target_kind, target_id);
CREATE TABLE IF NOT EXISTS authority_audit (sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL, event_state TEXT NOT NULL, previous_digest TEXT NOT NULL,
 event_digest TEXT NOT NULL, encrypted_payload BLOB NOT NULL, created_at REAL NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS activation_reservations (reservation_id TEXT PRIMARY KEY, activation_id TEXT NOT NULL,
 profile_id TEXT NOT NULL, plan_digest TEXT NOT NULL, profile_authority_digest TEXT NOT NULL,
 security_epoch INTEGER NOT NULL, fencing_token INTEGER NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK (state IN ('prepared', 'ready_without_authority', 'committing', 'active', 'aborted', 'retired')),
 created_at REAL NOT NULL, updated_at REAL NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS host_pending_effects (effect_id TEXT PRIMARY KEY, revision INTEGER NOT NULL CHECK (revision > 0),
 state TEXT NOT NULL, payload_digest TEXT NOT NULL, encrypted_payload BLOB NOT NULL, created_at REAL NOT NULL,
 updated_at REAL NOT NULL) STRICT;
CREATE INDEX IF NOT EXISTS host_pending_effects_state ON host_pending_effects(state, updated_at);
"#;
const BASE: &[&str] = &[
    "authority_meta",
    "authority_records",
    "execution_sessions",
    "grant_usage",
    "invocation_leases",
    "revocations",
    "authority_audit",
];
const LEASE_COLUMNS: &[&str] = &[
    "lease_id",
    "request_id",
    "lease_digest",
    "encrypted_payload",
    "caller_principal_id",
    "target_principal_id",
    "caller_artifact_digest",
    "target_artifact_digest",
    "caller_publisher_lineage",
    "target_publisher_lineage",
    "host_extension_id",
    "caller_domain_id",
    "target_domain_id",
    "profile_id",
    "activation_id",
    "grant_id",
    "provider_authority_id",
    "audit_reservation_id",
    "security_epoch",
    "expires_at",
    "state",
    "outcome_digest",
];

fn error(message: &'static str) -> OriginalAuthorityError {
    OriginalAuthorityError::new(OriginalAuthorityStage::Initialize, message, None)
}
fn text(row: &[Value], index: usize) -> Result<&str> {
    match row.get(index) {
        Some(Value::Text(value)) => Ok(value),
        _ => Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Initialize,
            "unexpected SQLite field type",
        )),
    }
}
fn blob(row: &[Value], index: usize) -> Result<&[u8]> {
    match row.get(index) {
        Some(Value::Blob(value)) => Ok(value),
        _ => Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Audit,
            "unexpected encrypted payload type",
        )),
    }
}
fn names(rows: &Rows, column: usize) -> Result<BTreeSet<&str>> {
    rows.rows.iter().map(|row| text(row, column)).collect()
}
pub(super) fn valid_digest(value: &str) -> bool {
    let hex = value.strip_prefix("sha256:").unwrap_or(value);
    hex.len() == 64
        && hex
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}
pub(super) fn valid_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=255).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b"._:/-".contains(b))
}

#[cfg(unix)]
pub(super) fn initialize<S: OriginalAuthorityOwner>(store: &mut Store<'_, '_, S>) -> Result<()> {
    let db = store.connection(OriginalAuthorityStage::Initialize)?;
    let result = initialize_connection(store, &db);
    db.complete(result)
}
#[cfg(unix)]
fn initialize_connection<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    db: &Database<'_, '_, '_, S>,
) -> Result<()> {
    let tables = db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        [],
    )?;
    let existing = names(&tables, 0)?;
    let version = if existing.is_empty() {
        None
    } else {
        if BASE.iter().any(|name| !existing.contains(name))
            || existing.iter().any(|name| {
                !BASE.contains(name)
                    && ![
                        "activation_reservations",
                        "host_pending_effects",
                        "host_extension_registrations",
                    ]
                    .contains(name)
            })
        {
            return Err(error(
                "authority database table set is partial or inconsistent",
            ));
        }
        let rows = db.query(
            "SELECT value FROM authority_meta WHERE key='schema_version'",
            [],
        )?;
        let value = rows
            .rows
            .first()
            .map(|row| text(row, 0))
            .transpose()?
            .ok_or_else(|| error("authority database schema version is missing"))?;
        if !["1", "2", "3", "4"].contains(&value) {
            return Err(error("authority database schema version is unsupported"));
        }
        Some(value.to_owned())
    };
    if version.as_deref().is_some_and(|version| {
        (version == "4") != existing.contains("host_extension_registrations")
    }) {
        return Err(error(
            "authority registration schema is partial or inconsistent",
        ));
    }
    if version.as_deref() == Some("4") && existing.len() != BASE.len() + 3 {
        return Err(error(
            "authority database table set is partial or inconsistent",
        ));
    }
    db.script(DDL)?;
    db.execute("INSERT OR IGNORE INTO authority_meta(key, value) VALUES ('schema_version', '1'), ('security_epoch', '1')", [])?;
    db.execute(
        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES (?, ?)",
        params!["security_epoch_advanced_at", clock_string(store.clock())?],
    )?;
    db.execute(
        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES (?, ?)",
        params![
            "security_epoch_reason_digest",
            Digest::hash(br#"{"reason":"genesis"}"#).as_str()
        ],
    )?;
    db.execute(
        "INSERT OR IGNORE INTO authority_meta(key, value) VALUES ('activation_fencing_token', '0')",
        [],
    )?;
    let lease_columns = db.query("PRAGMA table_info(invocation_leases)", [])?;
    let actual = names(&lease_columns, 1)?;
    let expected: BTreeSet<_> = LEASE_COLUMNS
        .iter()
        .copied()
        .filter(|name| version.as_deref() != Some("1") || *name != "request_id")
        .collect();
    if actual != expected {
        return Err(error(
            "authority database lease schema is partial or inconsistent",
        ));
    }
    let pending = db.query("PRAGMA table_info(host_pending_effects)", [])?;
    if names(&pending, 1)?
        != BTreeSet::from([
            "effect_id",
            "revision",
            "state",
            "payload_digest",
            "encrypted_payload",
            "created_at",
            "updated_at",
        ])
    {
        return Err(error(
            "authority database pending-effect schema is partial or inconsistent",
        ));
    }
    verify_audit(store, db)?;
    if version.as_deref() == Some("1") {
        migrate(store, db)?;
    } else {
        db.execute("BEGIN IMMEDIATE", [])?;
    }
    db.execute("CREATE TABLE IF NOT EXISTS host_extension_registrations (registration_id TEXT PRIMARY KEY, encrypted_payload BLOB NOT NULL) STRICT", [])?;
    let registration = db.query("PRAGMA table_info(host_extension_registrations)", [])?;
    if names(&registration, 1)? != BTreeSet::from(["registration_id", "encrypted_payload"]) {
        return Err(error(
            "authority registration schema is partial or inconsistent",
        ));
    }
    db.execute(
        "CREATE INDEX IF NOT EXISTS leases_request ON invocation_leases(request_id)",
        [],
    )?;
    db.execute(
        "UPDATE authority_meta SET value='4' WHERE key='schema_version'",
        [],
    )?;
    db.commit()
}

fn clock_string(now: f64) -> Result<String> {
    let bytes = encode(&CanonValue::Float(now), Variant::LegacyFloat).map_err(|_| {
        OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Initialize,
            "Python clock float encoding",
        )
    })?;
    String::from_utf8(bytes).map_err(|_| {
        OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Initialize,
            "Python clock float encoding",
        )
    })
}
#[cfg(unix)]
fn decoded<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    token: &[u8],
) -> Result<Funded<json::Document>> {
    store.assert_crypto()?;
    let plaintext = store.keys.decrypt(store.op, token)?;
    let value = json::decode(&plaintext, store.op)?;
    if value.entries(value.root).is_none() {
        return Err(OriginalAuthorityError::new(
            OriginalAuthorityStage::Audit,
            "authority record is not an object",
            None,
        ));
    }
    Ok(value)
}
#[cfg(unix)]
fn digest<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    value: &CanonValue,
) -> Result<Digest> {
    let size = json_bound(value)?;
    let capacity = store.op.reserve(OriginalAuthorityStage::Audit, size)?;
    let encoded = encode(value, Variant::LegacyFloat).map_err(|_| {
        OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Audit,
            "Python authority canonical JSON exception",
        )
    })?;
    let result = Digest::hash(&encoded);
    drop(encoded);
    drop(capacity);
    Ok(result)
}
fn json_bound(value: &CanonValue) -> Result<usize> {
    fn add(total: &mut usize, n: usize) -> Result<()> {
        *total = total
            .checked_add(n)
            .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
        Ok(())
    }
    fn visit(value: &CanonValue, total: &mut usize) -> Result<()> {
        match value {
            CanonValue::Null | CanonValue::Bool(_) | CanonValue::Int(_) | CanonValue::Float(_) => {
                add(total, 32)?
            }
            CanonValue::BigInt(v) => add(total, v.len())?,
            CanonValue::Str(v) => add(
                total,
                v.len()
                    .checked_mul(6)
                    .and_then(|n| n.checked_add(2))
                    .ok_or_else(|| {
                        OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit)
                    })?,
            )?,
            CanonValue::Arr(v) => {
                add(total, 2 + v.len())?;
                for v in v {
                    visit(v, total)?;
                }
            }
            CanonValue::Obj(v) => {
                add(total, 2 + v.len() * 2)?;
                for (k, v) in v {
                    add(total, k.len() * 6 + 2)?;
                    visit(v, total)?;
                }
            }
        }
        Ok(())
    }
    let mut total = 0;
    visit(value, &mut total)?;
    Ok(total)
}

#[cfg(unix)]
fn verify_rows<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    rows: &Rows,
    previous: &str,
) -> Result<()> {
    let mut previous = previous.to_owned();
    let mut decoded_rows = Vec::new();
    for row in &rows.rows {
        let payload = decoded(store, blob(row, 6)?)?;
        let created = match row.get(7) {
            Some(Value::Real(v)) => CanonValue::Float(*v),
            Some(Value::Integer(v)) => CanonValue::Int(*v),
            _ => return Err(error("authoritative audit chain is invalid")),
        };
        let encoded = json::canonical(
            &payload,
            Some(json::Audit {
                id: text(row, 1)?,
                kind: text(row, 2)?,
                state: text(row, 3)?,
                previous: &previous,
                created: &created,
            }),
            store.op,
        )?;
        let expected = Digest::hash(&encoded);
        if text(row, 4)? != previous
            || !constant_eq(text(row, 5)?.as_bytes(), expected.as_str().as_bytes())
        {
            return Err(OriginalAuthorityError::new(
                OriginalAuthorityStage::Audit,
                "authoritative audit chain is invalid",
                None,
            ));
        }
        previous = text(row, 5)?.into();
        // Source _verify_audit_rows retains decoded output until the entire
        // fetchall has been checked; do not turn it into a streaming verifier.
        decoded_rows.push(payload);
    }
    Ok(())
}

struct Tip {
    sequence: i64,
    previous: String,
    event: String,
    count: i64,
    verified: Instant,
    _control: ControlReservation,
}
#[derive(Clone)]
struct TipData {
    sequence: i64,
    previous: String,
    event: String,
    count: i64,
    verified: Instant,
}
type Tips = BTreeMap<(u32, io::Identity), Tip>;
fn tips() -> &'static Mutex<Tips> {
    static TIPS: OnceLock<Mutex<Tips>> = OnceLock::new();
    TIPS.get_or_init(Mutex::default)
}
fn zero_digest() -> String {
    format!("sha256:{}", "0".repeat(64))
}
#[cfg(unix)]
fn record_tip<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    rows: &Rows,
    count: i64,
    verified: Option<Instant>,
) -> Result<()> {
    let Some(row) = rows.rows.last() else {
        return Ok(());
    };
    let sequence = match row.first() {
        Some(Value::Integer(value)) => *value,
        _ => return Err(error("authoritative audit chain is invalid")),
    };
    let previous = text(row, 4)?;
    let event = text(row, 5)?;
    // Tip payload comes from schema-fixed digests. Reject non-digest oversized
    // input as a record gap instead of calling it trusted unbounded metadata.
    if previous.len() > 71 || event.len() > 71 {
        return Err(OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Audit,
            "audit tip digest representation",
        ));
    }
    let mut tips = tips()
        .lock()
        .map_err(|_| OriginalAuthorityError::unavailable())?;
    let key = (std::process::id(), store.identity());
    let when = verified
        .map(|fallback| tips.get(&key).map(|tip| tip.verified).unwrap_or(fallback))
        .unwrap_or_else(Instant::now);
    if let Some(existing) = tips.get_mut(&key) {
        existing.sequence = sequence;
        existing.previous = previous.into();
        existing.event = event.into();
        existing.count = count;
        existing.verified = when;
    } else {
        let control = store
            .op
            .invocation
            .control()
            .reserve(ControlKind::Catalog, 512)
            .map_err(|_| OriginalAuthorityError::capacity(OriginalAuthorityStage::Audit))?;
        tips.insert(
            key,
            Tip {
                sequence,
                previous: previous.into(),
                event: event.into(),
                count,
                verified: when,
                _control: control,
            },
        );
    }
    Ok(())
}
#[cfg(unix)]
fn verify_audit<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    db: &Database<'_, '_, '_, S>,
) -> Result<()> {
    let key = (std::process::id(), store.identity());
    let cached = tips()
        .lock()
        .map_err(|_| OriginalAuthorityError::unavailable())?
        .get(&key)
        .map(|tip| TipData {
            sequence: tip.sequence,
            previous: tip.previous.clone(),
            event: tip.event.clone(),
            count: tip.count,
            verified: tip.verified,
        });
    let Some(tip) = cached else {
        let rows = db.query("SELECT * FROM authority_audit ORDER BY sequence", [])?;
        verify_rows(store, &rows, &zero_digest())?;
        record_tip(store, &rows, rows.rows.len() as i64, None)?;
        return Ok(());
    };
    let below = db.query(
        "SELECT COUNT(*) AS count FROM authority_audit WHERE sequence<=?",
        params![tip.sequence],
    )?;
    let below = match below.rows.first().and_then(|row| row.first()) {
        Some(Value::Integer(value)) => *value,
        _ => -1,
    };
    let row = db.query(
        "SELECT * FROM authority_audit WHERE sequence=?",
        params![tip.sequence],
    )?;
    let mut incremental = below == tip.count && !row.rows.is_empty();
    let mut newer = None;
    if incremental {
        let attempt = (|| {
            verify_rows(store, &row, &tip.previous)?;
            if text(&row.rows[0], 5)? != tip.event {
                return Err(error("audit tip mismatch"));
            }
            let rows = db.query(
                "SELECT * FROM authority_audit WHERE sequence>? ORDER BY sequence",
                params![tip.sequence],
            )?;
            verify_rows(store, &rows, &tip.event)?;
            Ok(rows)
        })();
        match attempt {
            Ok(rows) => newer = Some(rows),
            Err(error)
                if error.class == "core_runtime.authority.store_contracts.AuthorityStoreError" =>
            {
                incremental = false
            }
            Err(error) => return Err(error),
        }
    }
    if !incremental {
        tips()
            .lock()
            .map_err(|_| OriginalAuthorityError::unavailable())?
            .remove(&key);
        return Err(OriginalAuthorityError::new(
            OriginalAuthorityStage::Audit,
            "authoritative audit chain is invalid",
            None,
        ));
    }
    let newer = newer.expect("verified incremental rows");
    if !newer.rows.is_empty() {
        record_tip(
            store,
            &newer,
            tip.count + newer.rows.len() as i64,
            Some(tip.verified),
        )?;
    }
    if tip.verified.elapsed() <= Duration::from_secs(300) {
        return Ok(());
    }
    tips()
        .lock()
        .map_err(|_| OriginalAuthorityError::unavailable())?
        .remove(&key);
    let rows = db.query("SELECT * FROM authority_audit ORDER BY sequence", [])?;
    verify_rows(store, &rows, &zero_digest())?;
    record_tip(store, &rows, rows.rows.len() as i64, None)
}
fn constant_eq(one: &[u8], two: &[u8]) -> bool {
    if one.len() != two.len() {
        return false;
    }
    one.iter()
        .zip(two)
        .fold(0_u8, |difference, (one, two)| difference | (one ^ two))
        == 0
}

#[cfg(unix)]
fn migrate<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    db: &Database<'_, '_, '_, S>,
) -> Result<()> {
    db.execute("BEGIN IMMEDIATE", [])?;
    db.execute(
        "ALTER TABLE invocation_leases ADD COLUMN request_id TEXT NOT NULL DEFAULT ''",
        [],
    )?;
    db.execute(
        "UPDATE invocation_leases SET request_id='legacy-' || lease_id",
        [],
    )?;
    let rows = db.query("SELECT lease_id, lease_digest, encrypted_payload, grant_id, audit_reservation_id, state FROM invocation_leases WHERE state IN (?, ?)", params!["issued", "dispatched"])?;
    for row in &rows.rows {
        let mut payload = decoded(store, blob(row, 2)?)?;
        let id = text(row, 0)?;
        if !payload
            .get(payload.root, "lease_id")
            .is_some_and(|node| payload.string_is(node, id))
        {
            return Err(error("historical InvocationLease identity is inconsistent"));
        }
        if !constant_eq(
            Digest::hash(&json::canonical(&payload, None, store.op)?)
                .as_str()
                .as_bytes(),
            text(row, 1)?.as_bytes(),
        ) {
            return Err(error("historical InvocationLease digest is inconsistent"));
        }
        payload.insert_string("request_id", &format!("legacy-{id}"));
        let activation = digest(
            store,
            &object([("legacy_unbound_activation", CanonValue::Str(id.into()))]),
        )?;
        payload.insert_string("activation_digest", activation.as_str());
        let plan = digest(
            store,
            &object([("legacy_unbound_plan", CanonValue::Str(id.into()))]),
        )?;
        payload.insert_string("plan_digest", plan.as_str());
        let payload = lease::normalize(&payload, store.op)?;
        let lease_digest = digest(store, &payload)?;
        let encoded_capacity = store
            .op
            .reserve(OriginalAuthorityStage::Migration, json_bound(&payload)?)?;
        let encoded = encode(&payload, Variant::LegacyFloat).map_err(|_| {
            OriginalAuthorityError::unsupported(
                OriginalAuthorityStage::Migration,
                "lease canonicalization",
            )
        })?;
        store.assert_crypto()?;
        let encrypted = store.keys.encrypt(store.op, &encoded)?;
        db.execute(
            "UPDATE invocation_leases SET lease_digest=?, encrypted_payload=? WHERE lease_id=?",
            params![lease_digest.as_str(), &encrypted.value, id],
        )?;
        drop(encrypted);
        drop(encoded);
        drop(encoded_capacity);
        let dispatched = text(row, 5)? == "dispatched";
        let state = if dispatched { "ambiguous" } else { "revoked" };
        let outcome = digest(
            store,
            &object([
                (
                    "status",
                    CanonValue::Str(
                        if dispatched {
                            "ambiguous_after_authority_schema_upgrade"
                        } else {
                            "revoked_after_authority_schema_upgrade"
                        }
                        .into(),
                    ),
                ),
                ("lease_id", CanonValue::Str(id.into())),
            ]),
        )?;
        db.execute(
            "UPDATE invocation_leases SET state=?, outcome_digest=? WHERE lease_id=?",
            params![state, outcome.as_str(), id],
        )?;
        if dispatched {
            db.execute("UPDATE grant_usage SET reserved_uses=reserved_uses-1, committed_uses=committed_uses+1 WHERE grant_id=? AND reserved_uses > 0", params![text(row, 3)?])?;
        } else {
            db.execute("UPDATE grant_usage SET reserved_uses=reserved_uses-1 WHERE grant_id=? AND reserved_uses > 0", params![text(row, 3)?])?;
        }
        let payload = object([
            ("lease_id", CanonValue::Str(id.into())),
            ("reservation_id", CanonValue::Str(text(row, 4)?.into())),
            ("outcome_digest", CanonValue::Str(outcome.as_str().into())),
            ("legacy_lease_digest", CanonValue::Str(text(row, 1)?.into())),
            (
                "migrated_lease_digest",
                CanonValue::Str(lease_digest.as_str().into()),
            ),
        ]);
        append_audit(store, db, &format!("schema-v2-{id}"), state, payload)?;
    }
    Ok(())
}

#[cfg(unix)]
fn append_audit<S: OriginalAuthorityOwner>(
    store: &Store<'_, '_, S>,
    db: &Database<'_, '_, '_, S>,
    id: &str,
    state: &str,
    payload: CanonValue,
) -> Result<()> {
    let rows = db.query(
        "SELECT event_digest FROM authority_audit ORDER BY sequence DESC LIMIT 1",
        [],
    )?;
    let previous = rows
        .rows
        .first()
        .map(|row| text(row, 0))
        .transpose()?
        .map(str::to_owned)
        .unwrap_or_else(|| format!("sha256:{}", "0".repeat(64)));
    let created = store.clock();
    let record = object([
        ("event_id", CanonValue::Str(id.into())),
        ("event_type", CanonValue::Str("host_effect".into())),
        ("event_state", CanonValue::Str(state.into())),
        ("previous_digest", CanonValue::Str(previous.clone())),
        ("payload", payload.clone()),
        ("created_at", CanonValue::Float(created)),
    ]);
    let hash = digest(store, &record)?;
    let capacity = store
        .op
        .reserve(OriginalAuthorityStage::Migration, json_bound(&payload)?)?;
    let encoded = encode(&payload, Variant::LegacyFloat).map_err(|_| {
        OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Migration,
            "audit canonicalization",
        )
    })?;
    store.assert_crypto()?;
    let encrypted = store.keys.encrypt(store.op, &encoded)?;
    let result = db.execute("INSERT INTO authority_audit(event_id, event_type, event_state, previous_digest, event_digest, encrypted_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        params![id, "host_effect", state, previous, hash.as_str(), &encrypted.value, created]);
    drop(encrypted);
    drop(encoded);
    drop(capacity);
    result.map(|_| ()).map_err(|mut error| {
        if error.class.starts_with("sqlite3.") || error.class == "builtins.MemoryError" {
            let source = python_error(error.class, std::mem::take(&mut error.message));
            error.class = "core_runtime.authority.store_contracts.AuditUnavailable";
            error.message = "authoritative audit append failed".into();
            error.cause = Some(source);
            error.suppressed = true;
        }
        error
    })
}
