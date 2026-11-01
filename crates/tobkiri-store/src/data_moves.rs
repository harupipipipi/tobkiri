//! Bounded trusted move intent. Pending rows deny access; startup never replays
//! a mutation or guesses which side of an interrupted move is authoritative.
use crate::{StoreError, canonical};
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use tobkiri_canon::{
    Digest,
    ids::{PackId, ProfileId},
};
use tobkiri_gate::{GrantId, StoreTier};

pub(crate) const MAX_RECORDS: usize = 64;
pub(crate) const MAX_BODY: usize = 64 * 1024;
pub(crate) const MAX_TOTAL: usize = 1024 * 1024;
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub(crate) enum MoveKind {
    Takeover,
    Orphan,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct MoveRow {
    pub name: String,
    pub tier: StoreTier,
    pub shape: String,
    pub source: String,
    pub target: String,
    pub main: Option<(u64, u64)>,
    pub journal: Option<(u64, u64)>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct MoveIntent {
    pub version: u32,
    pub profile: ProfileId,
    pub kind: MoveKind,
    pub old_owner: PackId,
    pub new_owner: PackId,
    pub source: String,
    pub target: String,
    pub grant: Option<GrantId>,
    pub directory: Option<(u64, u64)>,
    pub rows: Vec<MoveRow>,
}
impl MoveIntent {
    fn validate(&self, profile: &ProfileId) -> Result<(), StoreError> {
        let bad = || StoreError::PendingDataMove;
        if self.version != 1
            || &self.profile != profile
            || self.rows.is_empty()
            || self.rows.len() > 1000
        {
            return Err(bad());
        }
        crate::secure::validate_relative(std::path::Path::new(&self.source)).map_err(|_| bad())?;
        crate::secure::validate_relative(std::path::Path::new(&self.target)).map_err(|_| bad())?;
        match self.kind {
            MoveKind::Takeover
                if self.old_owner == self.new_owner
                    || self.grant.is_none()
                    || self.directory.is_some() =>
            {
                return Err(bad());
            }
            MoveKind::Orphan
                if self.old_owner != self.new_owner
                    || self.grant.is_some()
                    || self.directory.is_none() =>
            {
                return Err(bad());
            }
            _ => (),
        }
        let mut previous: Option<&str> = None;
        for row in &self.rows {
            crate::secure::name(&row.name).map_err(|_| bad())?;
            if row.shape.is_empty()
                || row.shape.len() > 512
                || previous.is_some_and(|name| name >= row.name.as_str())
            {
                return Err(bad());
            }
            previous = Some(&row.name);
            let leaf = match row.tier {
                StoreTier::Private => "private.sqlite".to_owned(),
                _ => format!("public/{}.sqlite", row.name),
            };
            let (source, target) = match self.kind {
                MoveKind::Takeover => (
                    format!("profiles/{profile}/orphaned/{}/{leaf}", self.old_owner),
                    format!("profiles/{profile}/data/{}/{leaf}", self.new_owner),
                ),
                MoveKind::Orphan => (
                    format!("profiles/{profile}/data/{}/{leaf}", self.old_owner),
                    format!("profiles/{profile}/orphaned/{}/{leaf}", self.old_owner),
                ),
            };
            if row.source != source || row.target != target {
                return Err(bad());
            }
            if self.kind == MoveKind::Takeover
                && (row.main.is_none() || row.source != self.source || row.target != self.target)
            {
                return Err(bad());
            }
        }
        if self.kind == MoveKind::Orphan
            && (self.source != format!("profiles/{profile}/data/{}", self.old_owner)
                || self.target != format!("profiles/{profile}/orphaned/{}", self.old_owner))
        {
            return Err(bad());
        }
        Ok(())
    }
    pub(crate) fn encoded(&self) -> Result<(String, Vec<u8>), StoreError> {
        self.validate(&self.profile)?;
        let estimate = self.rows.iter().try_fold(2048usize, |total, row| {
            total
                .checked_add(
                    row.name.len()
                        + row.shape.len().saturating_mul(6)
                        + row.source.len()
                        + row.target.len()
                        + 512,
                )
                .ok_or(StoreError::Quota)
        })?;
        if estimate > MAX_BODY {
            return Err(StoreError::Quota);
        }
        let body = canonical(self)?;
        if body.len() > MAX_BODY {
            return Err(StoreError::Quota);
        }
        Ok((Digest::hash(&body).hex().to_owned(), body))
    }
}

pub(crate) fn initialize(connection: &Connection, profile: &ProfileId) -> Result<(), StoreError> {
    // This is the trusted catalog, not a guest DATA connection. One fixed
    // capacity trigger is required; arbitrary guest triggers stay disabled.
    unsafe {
        rusqlite::ffi::sqlite3_limit(
            connection.handle(),
            rusqlite::ffi::SQLITE_LIMIT_TRIGGER_DEPTH,
            1,
        );
    }
    connection.execute_batch("PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS data_moves(id TEXT PRIMARY KEY CHECK(length(id)=64),phase TEXT NOT NULL CHECK(phase IN('prepared','committed')),body BLOB NOT NULL CHECK(length(body)<=65536));
        CREATE TRIGGER IF NOT EXISTS data_moves_capacity BEFORE INSERT ON data_moves BEGIN
          SELECT CASE WHEN (SELECT count(*) FROM data_moves)>=64 OR (SELECT coalesce(sum(length(body)),0) FROM data_moves)+length(NEW.body)>1048576 THEN RAISE(ABORT,'DATA move journal capacity') END;
        END;")?;
    let synchronous: u32 = connection.query_row("PRAGMA synchronous", [], |row| row.get(0))?;
    if synchronous != 2 {
        return Err(StoreError::Unavailable(
            "durable DATA move catalog unavailable".into(),
        ));
    }
    visit(connection, profile, |_, _, _| Ok(()))
}
fn visit(
    connection: &Connection,
    profile: &ProfileId,
    mut visit: impl FnMut(&str, &str, &MoveIntent) -> Result<(), StoreError>,
) -> Result<(), StoreError> {
    let mut operation = || -> Result<(), StoreError> {
        let (count, total, maximum): (u64, u64, u64) = connection.query_row("SELECT count(*),coalesce(sum(length(body)),0),coalesce(max(length(body)),0) FROM data_moves", [], |row| Ok((crate::sql_u64(row, 0)?, crate::sql_u64(row, 1)?, crate::sql_u64(row, 2)?)))?;
        if count > MAX_RECORDS as u64 || total > MAX_TOTAL as u64 || maximum > MAX_BODY as u64 {
            return Err(StoreError::PendingDataMove);
        }
        let mut statement = connection.prepare("SELECT CASE WHEN length(id)=64 THEN id ELSE '' END,CASE WHEN length(phase)<=9 THEN phase ELSE '' END,CASE WHEN length(body)<=65536 THEN body ELSE x'' END FROM data_moves ORDER BY id LIMIT 65")?;
        let mut rows = statement.query([])?;
        while let Some(row) = rows.next()? {
            let id: String = row.get(0)?;
            let phase: String = row.get(1)?;
            let raw = row
                .get_ref(2)?
                .as_blob()
                .map_err(|_| StoreError::PendingDataMove)?;
            if id.len() != 64
                || !matches!(phase.as_str(), "prepared" | "committed")
                || Digest::hash(raw).hex() != id
            {
                return Err(StoreError::PendingDataMove);
            }
            let intent: MoveIntent = crate::decode(raw).map_err(|_| StoreError::PendingDataMove)?;
            intent.validate(profile)?;
            if canonical(&intent)? != raw {
                return Err(StoreError::PendingDataMove);
            }
            visit(&id, &phase, &intent)?;
        }
        Ok(())
    };
    operation().map_err(|_| StoreError::PendingDataMove)
}
pub(crate) fn check(
    connection: &Connection,
    profile: &ProfileId,
    owner: &PackId,
    name: &str,
    tier: StoreTier,
) -> Result<(), StoreError> {
    visit(connection, profile, |_, _, intent| {
        if (&intent.old_owner == owner || &intent.new_owner == owner)
            && (intent.kind == MoveKind::Orphan
                || intent.rows.iter().any(|row| {
                    row.name == name || tier == StoreTier::Private && row.tier == StoreTier::Private
                }))
        {
            return Err(StoreError::PendingDataMove);
        }
        Ok(())
    })
}
pub(crate) fn check_owner(
    connection: &Connection,
    profile: &ProfileId,
    owner: &PackId,
) -> Result<(), StoreError> {
    visit(connection, profile, |_, _, intent| {
        if &intent.old_owner == owner || &intent.new_owner == owner {
            Err(StoreError::PendingDataMove)
        } else {
            Ok(())
        }
    })
}
pub(crate) fn insert_prepared(
    connection: &Connection,
    intent: &MoveIntent,
) -> Result<String, StoreError> {
    let (id, body) = intent.encoded()?;
    match intent.kind {
        MoveKind::Orphan => check_owner(connection, &intent.profile, &intent.old_owner)?,
        MoveKind::Takeover => {
            // Validation binds every row to this one physical source/target.
            // Multiple logical rows can therefore only be shared-private aliases.
            let row = intent.rows.first().ok_or(StoreError::PendingDataMove)?;
            check(
                connection,
                &intent.profile,
                &intent.old_owner,
                &row.name,
                row.tier,
            )?;
            check(
                connection,
                &intent.profile,
                &intent.new_owner,
                &row.name,
                row.tier,
            )?;
        }
    }
    let (count, total): (u64, u64) = connection.query_row(
        "SELECT count(*),coalesce(sum(length(body)),0) FROM data_moves",
        [],
        |row| Ok((crate::sql_u64(row, 0)?, crate::sql_u64(row, 1)?)),
    )?;
    if count >= MAX_RECORDS as u64
        || total
            .checked_add(body.len() as u64)
            .is_none_or(|bytes| bytes > MAX_TOTAL as u64)
    {
        return Err(StoreError::Quota);
    }
    connection.execute(
        "INSERT INTO data_moves(id,phase,body) VALUES(?1,'prepared',?2)",
        params![&id, body],
    )?;
    Ok(id)
}
pub(crate) fn phase(
    connection: &Connection,
    profile: &ProfileId,
    id: &str,
) -> Result<Option<String>, StoreError> {
    let mut found = None;
    visit(connection, profile, |current, phase, _| {
        if current == id {
            found = Some(phase.to_owned());
        }
        Ok(())
    })?;
    Ok(found)
}
pub(crate) fn mark_committed(connection: &Connection, id: &str) -> Result<(), StoreError> {
    if connection.execute(
        "UPDATE data_moves SET phase='committed' WHERE id=?1 AND phase='prepared'",
        [id],
    )? != 1
    {
        return Err(StoreError::PendingDataMove);
    }
    Ok(())
}
pub(crate) fn clear(
    connection: &mut Connection,
    profile: &ProfileId,
    id: &str,
    phase_expected: &str,
) -> Result<(), StoreError> {
    let result = (|| -> Result<(), StoreError> {
        let tx = connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        if phase(&tx, profile, id)?.as_deref() != Some(phase_expected) {
            return Err(StoreError::PendingDataMove);
        }
        if tx.execute(
            "DELETE FROM data_moves WHERE id=?1 AND phase=?2",
            params![id, phase_expected],
        )? != 1
        {
            return Err(StoreError::PendingDataMove);
        }
        tx.commit()?;
        if !connection.is_autocommit() || phase(connection, profile, id)?.is_some() {
            return Err(StoreError::PendingDataMove);
        }
        Ok(())
    })();
    result.map_err(|_| StoreError::PendingDataMove)
}

pub(crate) fn checkpoint(_stage: &str) {
    #[cfg(all(test, unix))]
    if std::env::var("TOBKIRI_TEST_DATA_MOVE_STAGE")
        .ok()
        .as_deref()
        == Some(_stage)
    {
        if let Ok(path) = std::env::var("TOBKIRI_TEST_DATA_MOVE_READY") {
            std::fs::write(path, _stage).expect("child checkpoint marker");
        }
        loop {
            std::thread::park();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn takeover(tier: StoreTier, names: &[&str]) -> MoveIntent {
        let profile = ProfileId::parse("prof_move_overlap").unwrap();
        let old_owner = PackId::parse("local.move-source").unwrap();
        let new_owner = PackId::parse("local.move-target").unwrap();
        let leaf = match tier {
            StoreTier::Private => "private.sqlite".to_owned(),
            _ => format!("public/{}.sqlite", names[0]),
        };
        let source = format!("profiles/{profile}/orphaned/{old_owner}/{leaf}");
        let target = format!("profiles/{profile}/data/{new_owner}/{leaf}");
        MoveIntent {
            version: 1,
            profile,
            kind: MoveKind::Takeover,
            old_owner,
            new_owner,
            source: source.clone(),
            target: target.clone(),
            // Descriptive journal metadata only; an id grants no dispatch right.
            grant: Some(serde_json::from_str(r#""journal-fixture-grant""#).unwrap()),
            directory: None,
            rows: names
                .iter()
                .map(|name| MoveRow {
                    name: (*name).into(),
                    tier,
                    shape: "local.move-data@1.0".into(),
                    source: source.clone(),
                    target: target.clone(),
                    main: Some((1, 2)),
                    journal: None,
                })
                .collect(),
        }
    }

    #[test]
    fn orphan_admission_denies_a_pending_takeover_of_another_store_for_the_owner() {
        let pending = takeover(StoreTier::Public, &["first"]);
        let owner = pending.new_owner.clone();
        let source = format!("profiles/{}/data/{owner}", pending.profile);
        let target = format!("profiles/{}/orphaned/{owner}", pending.profile);
        let orphan = MoveIntent {
            version: 1,
            profile: pending.profile.clone(),
            kind: MoveKind::Orphan,
            old_owner: owner.clone(),
            new_owner: owner.clone(),
            source: source.clone(),
            target: target.clone(),
            grant: None,
            directory: Some((1, 3)),
            rows: vec![MoveRow {
                name: "second".into(),
                tier: StoreTier::Public,
                shape: "local.move-data@1.0".into(),
                source: format!("{source}/public/second.sqlite"),
                target: format!("{target}/public/second.sqlite"),
                main: Some((1, 4)),
                journal: None,
            }],
        };
        let mut connection = Connection::open_in_memory().unwrap();
        initialize(&connection, &pending.profile).unwrap();
        let tx = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .unwrap();
        let id = insert_prepared(&tx, &pending).unwrap();
        tx.commit().unwrap();
        let tx = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .unwrap();
        assert!(matches!(
            insert_prepared(&tx, &orphan),
            Err(StoreError::PendingDataMove)
        ));
        assert_eq!(
            phase(&tx, &pending.profile, &id).unwrap().as_deref(),
            Some("prepared")
        );
        assert_eq!(
            tx.query_row("SELECT count(*) FROM data_moves", [], |row| row
                .get::<_, u32>(0))
                .unwrap(),
            1
        );
    }

    #[test]
    fn takeover_admission_keeps_private_aliases_in_one_pending_physical_group() {
        let pending = takeover(StoreTier::Private, &["first", "second"]);
        assert_eq!(pending.rows[0].source, pending.rows[1].source);
        assert_eq!(pending.rows[0].target, pending.rows[1].target);
        let mut connection = Connection::open_in_memory().unwrap();
        initialize(&connection, &pending.profile).unwrap();
        let tx = connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
            .unwrap();
        let id = insert_prepared(&tx, &pending).unwrap();
        tx.commit().unwrap();
        assert_eq!(
            phase(&connection, &pending.profile, &id)
                .unwrap()
                .as_deref(),
            Some("prepared")
        );
        for owner in [&pending.old_owner, &pending.new_owner] {
            for name in ["first", "second", "another-private-alias"] {
                assert!(matches!(
                    check(
                        &connection,
                        &pending.profile,
                        owner,
                        name,
                        StoreTier::Private
                    ),
                    Err(StoreError::PendingDataMove)
                ));
            }
            check(
                &connection,
                &pending.profile,
                owner,
                "unrelated",
                StoreTier::Public,
            )
            .unwrap();
        }
    }

    #[test]
    fn durable_journal_bounds_record_body_count_and_aggregate_bytes() {
        let connection = Connection::open_in_memory().unwrap();
        initialize(&connection, &ProfileId::parse("prof_move_bounds").unwrap()).unwrap();
        let body = vec![0u8; MAX_BODY];
        for index in 0..MAX_TOTAL / MAX_BODY {
            connection
                .execute(
                    "INSERT INTO data_moves(id,phase,body) VALUES(?1,'prepared',?2)",
                    params![format!("{index:064x}"), &body],
                )
                .unwrap();
        }
        assert!(
            connection
                .execute(
                    "INSERT INTO data_moves(id,phase,body) VALUES(?1,'prepared',x'00')",
                    ["f".repeat(64)]
                )
                .is_err()
        );
        connection.execute("DELETE FROM data_moves", []).unwrap();
        assert!(
            connection
                .execute(
                    "INSERT INTO data_moves(id,phase,body) VALUES(?1,'prepared',?2)",
                    params!["f".repeat(64), vec![0u8; MAX_BODY + 1]]
                )
                .is_err()
        );
        for index in 0..MAX_RECORDS {
            connection
                .execute(
                    "INSERT INTO data_moves(id,phase,body) VALUES(?1,'prepared',x'00')",
                    [format!("{index:064x}")],
                )
                .unwrap();
        }
        assert!(
            connection
                .execute(
                    "INSERT INTO data_moves(id,phase,body) VALUES(?1,'prepared',x'00')",
                    ["f".repeat(64)]
                )
                .is_err()
        );
    }
}
