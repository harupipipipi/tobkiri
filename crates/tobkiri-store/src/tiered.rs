use crate::{DataRoot, StoreError, canonical};
use rusqlite::{
    OptionalExtension,
    hooks::{AuthAction, AuthContext, Authorization},
    params,
    types::{Value, ValueRef},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        Arc, Mutex, OnceLock, Weak,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::Duration,
};
use tobkiri_canon::{
    AllocationBudget, AllocationKind, AllocationRequest, CanonValue, CoreRef, CoreValue, Digest,
    ids::{PackId, ProfileId},
};
use tobkiri_gate::{
    CallCx, ControlKind, ControlReservation, DispatchPermit, EffectRequest, Gate, Grant, Outcome,
    StoreOperation, StoreTier,
};

mod storage_commit;
use storage_commit::CommitAdmission;

const MAX_DATABASE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_JOURNAL_BYTES: u64 = 64 * 1024 * 1024;
const MAX_DATA_VALUE_BYTES: usize = 1024 * 1024;
const MAX_LIST_ROWS: u32 = 1000;
const DATABASE_CONTROL_BYTES: u64 = 64 * 1024;
const TASK_CONTROL_BYTES: u64 = 16 * 1024;

struct SqlParameterReservation {
    budget: Arc<dyn AllocationBudget>,
    bytes: u64,
}
impl Drop for SqlParameterReservation {
    fn drop(&mut self) {
        self.budget.release(self.bytes);
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StoreDescriptor {
    pub owner: PackId,
    pub name: String,
    pub tier: StoreTier,
    pub data_shape: String,
    pub orphaned: bool,
}
struct Catalog {
    root: DataRoot,
    profile: ProfileId,
    disk_policy: crate::StoreDiskPolicy,
    connection: Mutex<rusqlite::Connection>,
    _directory: crate::vfs::DatabaseGuard,
}
#[derive(Clone)]
pub struct TieredStore {
    catalog: Arc<Catalog>,
}
impl TieredStore {
    pub fn open(root: &DataRoot, profile: &ProfileId) -> Result<Self, StoreError> {
        Self::open_with_disk_policy(root, profile, crate::StoreDiskPolicy::default())
    }
    /// Narrow native DATA safety ceilings without changing storage authority.
    pub fn open_with_disk_policy(
        root: &DataRoot,
        profile: &ProfileId,
        disk_policy: crate::StoreDiskPolicy,
    ) -> Result<Self, StoreError> {
        let (conn, dir) =
            root.database(&DataRoot::profile_relative(profile, "data/stores.meta")?)?;
        configure_limits(&conn);
        conn.execute_batch("CREATE TABLE IF NOT EXISTS stores(owner TEXT NOT NULL,name TEXT NOT NULL,tier TEXT NOT NULL,shape TEXT NOT NULL,orphaned INTEGER NOT NULL DEFAULT 0,path TEXT NOT NULL,PRIMARY KEY(owner,name)); CREATE TABLE IF NOT EXISTS seed_versions(owner TEXT NOT NULL,name TEXT NOT NULL,hash TEXT NOT NULL,PRIMARY KEY(owner,name));")?;
        crate::data_moves::initialize(&conn, profile)?;
        Ok(Self {
            catalog: Arc::new(Catalog {
                root: root.clone(),
                profile: profile.clone(),
                disk_policy,
                connection: Mutex::new(conn),
                _directory: dir,
            }),
        })
    }
    /// Core registers a verified manifest store. It never transfers existing data.
    pub fn register(
        &self,
        owner: &PackId,
        name: &str,
        tier: StoreTier,
        data_shape: &str,
    ) -> Result<(), StoreError> {
        crate::secure::name(name)?;
        if data_shape.is_empty() || data_shape.len() > 512 {
            return Err(StoreError::Invalid("missing data shape".into()));
        }
        let mut conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        crate::data_moves::check(&tx, &self.catalog.profile, owner, name, tier)?;
        let existing: Option<(String, String, bool)> = tx
            .query_row(
                "SELECT tier,shape,orphaned FROM stores WHERE owner=?1 AND name=?2",
                params![owner.as_str(), name],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?;
        if let Some((old_tier, shape, orphaned)) = existing {
            if old_tier != tier_name(tier) || shape != data_shape || orphaned {
                return Err(StoreError::Conflict(
                    "store already registered with other metadata".into(),
                ));
            }
            return Ok(());
        }
        let path = store_path(&self.catalog.profile, owner, name, tier)?;
        // Catalog registration is trusted metadata only. The bounded DATA
        // engine initializes the physical database under its actual loan;
        // registration must never open native SQLite on guest DATA.
        tx.execute(
            "INSERT INTO stores(owner,name,tier,shape,path) VALUES(?1,?2,?3,?4,?5)",
            params![
                owner.as_str(),
                name,
                tier_name(tier),
                data_shape,
                path.to_string_lossy()
            ],
        )?;
        tx.commit()?;
        Ok(())
    }
    pub fn describe(&self, owner: &PackId, name: &str) -> Result<StoreDescriptor, StoreError> {
        let conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        crate::data_moves::check(&conn, &self.catalog.profile, owner, name, StoreTier::Public)?;
        let meta = descriptor(&conn, owner, name)?;
        crate::data_moves::check(&conn, &self.catalog.profile, owner, name, meta.tier)?;
        Ok(meta)
    }
    /// Core applies a hash-verified KV seed at installation, never a guest import.
    /// Read-only stores replace their seed on revision changes; other tiers fill gaps.
    pub fn apply_seed(
        &self,
        owner: &PackId,
        name: &str,
        hash: &tobkiri_canon::ids::PackHash,
        seed: &CanonValue,
    ) -> Result<(), StoreError> {
        crate::secure::name(name)?;
        let CanonValue::Obj(entries) = seed else {
            return Err(StoreError::Invalid(
                "manifest DATA seed must be a key/value object".into(),
            ));
        };
        if entries.len() > 4096 {
            return Err(StoreError::Quota);
        }
        let mut encoded = Vec::with_capacity(entries.len());
        let mut total = 0usize;
        for (key, value) in entries {
            if key.is_empty() || key.len() > 1024 || key.contains('\0') {
                return Err(StoreError::Invalid("invalid seed key".into()));
            }
            let bytes = tobkiri_canon::encode(value, tobkiri_canon::Variant::StrictV1)
                .map_err(|error| StoreError::Canon(error.to_string()))?;
            if bytes.len() > MAX_DATA_VALUE_BYTES {
                return Err(StoreError::Quota);
            }
            total = total.checked_add(bytes.len()).ok_or(StoreError::Quota)?;
            if total > 8 * 1024 * 1024 {
                return Err(StoreError::Quota);
            }
            encoded.push((key.as_str(), bytes));
        }

        let mut catalog = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let catalog_tx =
            catalog.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        crate::data_moves::check(
            &catalog_tx,
            &self.catalog.profile,
            owner,
            name,
            StoreTier::Public,
        )?;
        let meta = descriptor(&catalog_tx, owner, name)?;
        crate::data_moves::check(&catalog_tx, &self.catalog.profile, owner, name, meta.tier)?;
        if meta.orphaned {
            return Err(StoreError::Denied(
                "orphaned store cannot accept a seed".into(),
            ));
        }
        let path = store_path(&self.catalog.profile, owner, name, meta.tier)?;
        let seed_hash: Option<String> = catalog_tx
            .query_row(
                "SELECT hash FROM seed_versions WHERE owner=?1 AND name=?2",
                params![owner.as_str(), name],
                |row| row.get(0),
            )
            .optional()?;
        if seed_hash.as_deref() == Some(hash.as_str()) {
            return Ok(());
        }
        let owner_lease = self
            .catalog
            .root
            .owner_lease(&self.catalog.profile, owner)?;
        let custody = crate::data_custody::DataCustody::trusted_seed(owner_lease);
        preflight_data_database(&self.catalog.root, &path)?;
        let mut database = self.catalog.root.data_database_with_disk_policy(
            &path,
            true,
            self.catalog.disk_policy,
            custody,
        )?;
        let connection = &mut database.connection;
        configure_data_connection(connection, &self.catalog.root, &path)?;
        connection.authorizer(None::<fn(AuthContext<'_>) -> Authorization>)?;
        let seed_result: Result<(), StoreError> = (|| {
            let tx =
                connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            if meta.tier == StoreTier::ReadOnly {
                tx.execute("DELETE FROM __tobkiri_kv WHERE store=?1", [name])?;
            }
            for (key, value) in &encoded {
                if meta.tier == StoreTier::ReadOnly {
                    tx.execute(
                        "INSERT INTO __tobkiri_kv(store,key,value) VALUES(?1,?2,?3)",
                        params![name, key, value],
                    )?;
                } else {
                    tx.execute(
                        "INSERT OR IGNORE INTO __tobkiri_kv(store,key,value) VALUES(?1,?2,?3)",
                        params![name, key, value],
                    )?;
                }
            }
            tx.commit()?;
            Ok(())
        })();
        let restore_authorizer = restrict(connection);
        seed_result?;
        restore_authorizer?;
        // Catalog hash update follows the idempotent physical seed transaction.
        // A crash between these commits safely reruns the same replace/fill.
        catalog_tx.execute(
            "INSERT INTO seed_versions(owner,name,hash) VALUES(?1,?2,?3)
             ON CONFLICT(owner,name) DO UPDATE SET hash=excluded.hash",
            params![owner.as_str(), name, hash.as_str()],
        )?;
        catalog_tx.commit()?;
        Ok(())
    }
    pub fn open_request(
        &self,
        owner: &PackId,
        name: &str,
    ) -> Result<(EffectRequest, Digest), StoreError> {
        let meta = self.describe(owner, name)?;
        Ok((
            EffectRequest::Storage {
                profile: self.catalog.profile.clone(),
                owner: owner.clone(),
                store: name.into(),
                tier: meta.tier,
                operation: StoreOperation::Open,
            },
            Digest::hash(&canonical(&(owner, name))?),
        ))
    }
    /// Only the gate can construct a usable guest store handle.
    pub fn open_store(
        &self,
        cx: &CallCx,
        owner: &PackId,
        name: &str,
        grant: Grant,
    ) -> Result<StoreHandle, StoreError> {
        let mut conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        crate::data_moves::check(&tx, &self.catalog.profile, owner, name, StoreTier::Public)?;
        let meta = descriptor(&tx, owner, name)?;
        crate::data_moves::check(&tx, &self.catalog.profile, owner, name, meta.tier)?;
        if meta.orphaned {
            return Err(StoreError::Denied(
                "orphaned data needs an approved owner transfer".into(),
            ));
        }
        local_access(cx, &self.catalog.profile, &meta, false)?;
        let path: String = tx.query_row(
            "SELECT path FROM stores WHERE owner=?1 AND name=?2",
            params![owner.as_str(), name],
            |r| r.get(0),
        )?;
        let request = EffectRequest::Storage {
            profile: self.catalog.profile.clone(),
            owner: owner.clone(),
            store: name.into(),
            tier: meta.tier,
            operation: StoreOperation::Open,
        };
        let args = Digest::hash(&canonical(&(owner, name))?);
        // All long-lived Core control and physical-owner capacity is reserved
        // before SQLite opens the guest database. The owner lease is retained
        // behind the connection until the database descriptor is closed.
        let registration = cx
            .control()
            .reserve(ControlKind::Registration, DATABASE_CONTROL_BYTES)?;
        let owner_lease = self
            .catalog
            .root
            .owner_lease(&self.catalog.profile, owner)?;
        let path = PathBuf::from(path);
        crate::secure::validate_relative(&path)?;
        preflight_data_database(&self.catalog.root, &path)?;
        cx.check_active()?;
        grant.consume(cx, &request, &args)?;
        let custody = crate::data_custody::DataCustody::registered(Some(owner_lease), registration);
        let database = self.catalog.root.data_database_with_disk_policy(
            &path,
            true,
            self.catalog.disk_policy,
            custody,
        )?;
        configure_data_connection(&database.connection, &self.catalog.root, &path)?;
        tx.commit()?;
        let native = Arc::new(NativeDatabase {
            connection: Mutex::new(Some(database.connection)),
            active_transaction: AtomicBool::new(false),
            quarantined: AtomicBool::new(false),
            _directory: Some(database.directory),
            _custody: database.custody,
            _global_identity: None,
            process_id: std::process::id(),
        });
        Ok(StoreHandle {
            catalog: self.catalog.clone(),
            owner: owner.clone(),
            name: name.into(),
            path,
            native,
        })
    }
    /// Owner-only designation; stale writable handles re-read metadata per effect.
    pub fn set_read_only(
        &self,
        cx: &CallCx,
        owner: &PackId,
        name: &str,
        grant: Grant,
    ) -> Result<(), StoreError> {
        let mut conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let meta = descriptor(&tx, owner, name)?;
        crate::data_moves::check(&tx, &self.catalog.profile, owner, name, meta.tier)?;
        if cx.principal().pack().is_none_or(|(p, _)| p != owner)
            || *cx.profile() != self.catalog.profile
            || meta.tier != StoreTier::Public
        {
            return Err(StoreError::Denied(
                "only owner may designate a public store read-only".into(),
            ));
        }
        let req = EffectRequest::Storage {
            profile: self.catalog.profile.clone(),
            owner: owner.clone(),
            store: name.into(),
            tier: meta.tier,
            operation: StoreOperation::SetReadOnly,
        };
        grant.consume(
            cx,
            &req,
            &Digest::hash(&canonical(&(owner, name, "read-only"))?),
        )?;
        tx.execute(
            "UPDATE stores SET tier='read-only' WHERE owner=?1 AND name=?2",
            params![owner.as_str(), name],
        )?;
        tx.commit()?;
        Ok(())
    }
    /// Preserve all Pack data as an orphan, with no destructive deletion.
    pub fn orphan_pack(&self, owner: &PackId) -> Result<Vec<StoreDescriptor>, StoreError> {
        let mut conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        orphan_native_database(&self.catalog, &mut conn, owner)
    }
    /// Approved takeover requires identical data shape; no implicit conversion.
    /// Transfer every private store together because a Pack shares one private DB.
    pub fn transfer_private_database(
        &self,
        cx: &CallCx,
        old_owner: &PackId,
        new_owner: &PackId,
        implemented_shapes: &std::collections::BTreeMap<String, String>,
        grant: Grant,
    ) -> Result<(), StoreError> {
        if implemented_shapes.is_empty() || implemented_shapes.len() > MAX_LIST_ROWS as usize {
            return Err(StoreError::Quota);
        }
        if *cx.profile() != self.catalog.profile
            || cx.principal().pack().is_none_or(|(p, _)| p != new_owner)
        {
            return Err(StoreError::Denied(
                "private takeover caller must be the new Profile-local owner".into(),
            ));
        }
        let mut conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        crate::data_moves::check_owner(&tx, &self.catalog.profile, old_owner)?;
        crate::data_moves::check_owner(&tx, &self.catalog.profile, new_owner)?;
        let mut statement=tx.prepare("SELECT name,shape,orphaned,path FROM stores WHERE owner=?1 AND tier='private' ORDER BY name LIMIT 1001")?;
        let rows = statement
            .query_map([old_owner.as_str()], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, bool>(2)?,
                    r.get::<_, String>(3)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        drop(statement);
        if rows.is_empty()
            || rows.iter().any(|(name, shape, orphaned, _)| {
                !*orphaned || implemented_shapes.get(name) != Some(shape)
            })
            || rows.len() != implemented_shapes.len()
        {
            return Err(StoreError::Denied(
                "private takeover needs every orphaned data shape, exactly".into(),
            ));
        }
        let collision:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM stores WHERE owner=?1 AND (tier='private' OR name IN(SELECT name FROM stores WHERE owner=?2 AND tier='private')))",params![new_owner.as_str(),old_owner.as_str()],|r|r.get(0))?;
        if collision {
            return Err(StoreError::Conflict(
                "new owner already holds conflicting private data".into(),
            ));
        }
        let request = EffectRequest::Storage {
            profile: self.catalog.profile.clone(),
            owner: old_owner.clone(),
            store: "*".into(),
            tier: StoreTier::Private,
            operation: StoreOperation::Transfer,
        };
        let args = Digest::hash(&canonical(&(old_owner, new_owner, implemented_shapes))?);
        let source = PathBuf::from(&rows[0].3);
        if rows.iter().any(|r| r.3 != rows[0].3) {
            return Err(StoreError::Conflict(
                "private stores do not share one database".into(),
            ));
        }
        let target = store_path(
            &self.catalog.profile,
            new_owner,
            "private",
            StoreTier::Private,
        )?;
        let expected = rows
            .into_iter()
            .map(|(name, shape, orphaned, path)| TransferRow {
                name,
                shape,
                orphaned,
                path,
                tier: StoreTier::Private,
            })
            .collect::<Vec<_>>();
        tx.rollback()?;
        transfer_native_database(
            &self.catalog,
            &mut conn,
            cx,
            old_owner,
            new_owner,
            &expected,
            &source,
            &target,
            true,
            grant,
            request,
            args,
        )
    }
    pub fn transfer(
        &self,
        cx: &CallCx,
        old_owner: &PackId,
        name: &str,
        new_owner: &PackId,
        implemented_shape: &str,
        grant: Grant,
    ) -> Result<(), StoreError> {
        if cx.principal().pack().is_none_or(|(p, _)| p != new_owner) {
            return Err(StoreError::Denied(
                "transfer caller must be the new owner".into(),
            ));
        }
        let mut conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        crate::data_moves::check(
            &tx,
            &self.catalog.profile,
            old_owner,
            name,
            StoreTier::Public,
        )?;
        crate::data_moves::check(
            &tx,
            &self.catalog.profile,
            new_owner,
            name,
            StoreTier::Public,
        )?;
        let meta = descriptor(&tx, old_owner, name)?;
        if *cx.profile() != self.catalog.profile
            || !meta.orphaned
            || meta.data_shape != implemented_shape
        {
            return Err(StoreError::Denied(
                "transfer needs orphaned data and the exact implemented data shape".into(),
            ));
        }
        let collision: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM stores WHERE owner=?1 AND name=?2)",
            params![new_owner.as_str(), name],
            |r| r.get(0),
        )?;
        if collision {
            return Err(StoreError::Conflict(
                "new owner already has this store".into(),
            ));
        }
        if meta.tier == StoreTier::Private {
            let count: i64 = tx.query_row(
                "SELECT COUNT(*) FROM stores WHERE owner=?1 AND tier='private'",
                [old_owner.as_str()],
                |r| r.get(0),
            )?;
            if count > 1 {
                return Err(StoreError::Unavailable(
                    "transfer all private data shapes together with transfer_private_database"
                        .into(),
                ));
            }
        }
        let request = EffectRequest::Storage {
            profile: self.catalog.profile.clone(),
            owner: old_owner.clone(),
            store: name.into(),
            tier: meta.tier,
            operation: StoreOperation::Transfer,
        };
        let args = Digest::hash(&canonical(&(
            old_owner,
            name,
            new_owner,
            implemented_shape,
        ))?);
        let old_path: String = tx.query_row(
            "SELECT path FROM stores WHERE owner=?1 AND name=?2",
            params![old_owner.as_str(), name],
            |r| r.get(0),
        )?;
        let new_path = store_path(&self.catalog.profile, new_owner, name, meta.tier)?;
        let expected = [TransferRow {
            name: name.into(),
            shape: meta.data_shape,
            orphaned: true,
            path: old_path.clone(),
            tier: meta.tier,
        }];
        tx.rollback()?;
        transfer_native_database(
            &self.catalog,
            &mut conn,
            cx,
            old_owner,
            new_owner,
            &expected,
            std::path::Path::new(&old_path),
            &new_path,
            false,
            grant,
            request,
            args,
        )
    }
}
#[derive(Clone)]
struct TransferRow {
    name: String,
    shape: String,
    orphaned: bool,
    path: String,
    tier: StoreTier,
}

fn orphan_catalog_matches(
    connection: &rusqlite::Connection,
    owner: &PackId,
    rows: &[crate::data_moves::MoveRow],
    orphaned: bool,
) -> Result<bool, StoreError> {
    for expected in rows {
        let actual: Option<(String, String, bool, String)> = connection
            .query_row(
                "SELECT tier,shape,orphaned,path FROM stores WHERE owner=?1 AND name=?2",
                params![owner.as_str(), &expected.name],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()?;
        if actual
            != Some((
                tier_name(expected.tier).into(),
                expected.shape.clone(),
                orphaned,
                if orphaned {
                    expected.target.clone()
                } else {
                    expected.source.clone()
                },
            ))
        {
            return Ok(false);
        }
    }
    Ok(true)
}
fn orphan_native_database(
    catalog: &Catalog,
    connection: &mut rusqlite::Connection,
    owner: &PackId,
) -> Result<Vec<StoreDescriptor>, StoreError> {
    let prepared =
        connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    crate::data_moves::check_owner(&prepared, &catalog.profile, owner)?;
    let mut query = prepared.prepare("SELECT name,tier,shape,path FROM stores WHERE owner=?1 AND orphaned=0 ORDER BY name LIMIT 1001")?;
    let records = query
        .query_map([owner.as_str()], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        })?
        .collect::<Result<Vec<_>, _>>()?;
    drop(query);
    if records.is_empty() {
        return Ok(Vec::new());
    }
    if records.len() > MAX_LIST_ROWS as usize {
        return Err(StoreError::Quota);
    }
    let _owner = catalog.root.owner_lease(&catalog.profile, owner)?;
    let source = DataRoot::profile_relative(&catalog.profile, &format!("data/{owner}"))?;
    let target = DataRoot::profile_relative(&catalog.profile, &format!("orphaned/{owner}"))?;
    let directory = catalog
        .root
        .move_path_identity(&source)?
        .ok_or(StoreError::NotFound)?;
    if catalog.root.move_path_identity(&target)?.is_some() {
        return Err(StoreError::Conflict(
            "orphan directory already exists".into(),
        ));
    }
    let mut rows = Vec::with_capacity(records.len());
    for (name, tier, shape, path) in records {
        let tier = parse_tier(&tier)?;
        let expected = store_path(&catalog.profile, owner, &name, tier)?;
        if std::path::Path::new(&path) != expected {
            return Err(StoreError::Conflict("orphan DATA path changed".into()));
        }
        preflight_data_database(&catalog.root, &expected)?;
        let journal_path = sidecar_path(&expected, "-journal")?;
        rows.push(crate::data_moves::MoveRow {
            name,
            tier,
            shape,
            source: path,
            target: match tier {
                StoreTier::Private => format!(
                    "profiles/{}/orphaned/{owner}/private.sqlite",
                    catalog.profile
                ),
                _ => format!(
                    "profiles/{}/orphaned/{owner}/public/{}.sqlite",
                    catalog.profile,
                    expected
                        .file_stem()
                        .and_then(|name| name.to_str())
                        .ok_or_else(|| StoreError::Invalid("invalid DATA path".into()))?
                ),
            },
            main: catalog.root.move_path_identity(&expected)?,
            journal: catalog.root.move_path_identity(&journal_path)?,
        });
    }
    let intent = crate::data_moves::MoveIntent {
        version: 1,
        profile: catalog.profile.clone(),
        kind: crate::data_moves::MoveKind::Orphan,
        old_owner: owner.clone(),
        new_owner: owner.clone(),
        source: source.to_string_lossy().into_owned(),
        target: target.to_string_lossy().into_owned(),
        grant: None,
        directory: Some(directory),
        rows,
    };
    let id = crate::data_moves::insert_prepared(&prepared, &intent)?;
    if prepared.commit().is_err()
        || !connection.is_autocommit()
        || crate::data_moves::phase(connection, &catalog.profile, &id)?.as_deref()
            != Some("prepared")
    {
        return Err(StoreError::PendingDataMove);
    }
    crate::data_moves::checkpoint("orphan-prepared");
    let moved = catalog.root.rename(&source, &target);
    let at_source = catalog.root.move_path_identity(&source).ok() == Some(Some(directory))
        && catalog.root.move_path_identity(&target).ok() == Some(None);
    let at_target = catalog.root.move_path_identity(&source).ok() == Some(None)
        && catalog.root.move_path_identity(&target).ok() == Some(Some(directory));
    if let Err(error) = moved {
        if at_target {
            catalog
                .root
                .rename(&target, &source)
                .map_err(|_| StoreError::PendingDataMove)?;
            if catalog.root.move_path_identity(&source)? != Some(directory)
                || catalog.root.move_path_identity(&target)?.is_some()
            {
                return Err(StoreError::PendingDataMove);
            }
        } else if !at_source {
            return Err(StoreError::PendingDataMove);
        }
        crate::data_moves::clear(connection, &catalog.profile, &id, "prepared")?;
        return Err(error);
    }
    if !at_target {
        return Err(StoreError::PendingDataMove);
    }
    crate::data_moves::checkpoint("orphan-dir-synced");
    let metadata = (|| -> Result<(), StoreError> {
        let tx = connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        if crate::data_moves::phase(&tx, &catalog.profile, &id)?.as_deref() != Some("prepared")
            || !orphan_catalog_matches(&tx, owner, &intent.rows, false)?
        {
            return Err(StoreError::PendingDataMove);
        }
        for row in &intent.rows {
            if tx.execute(
                "UPDATE stores SET orphaned=1,path=?1 WHERE owner=?2 AND name=?3 AND orphaned=0",
                params![&row.target, owner.as_str(), &row.name],
            )? != 1
            {
                return Err(StoreError::PendingDataMove);
            }
        }
        crate::data_moves::mark_committed(&tx, &id)?;
        tx.commit()?;
        if !connection.is_autocommit() {
            return Err(StoreError::PendingDataMove);
        }
        Ok(())
    })();
    if let Err(error) = metadata {
        if !connection.is_autocommit()
            && (connection.execute_batch("ROLLBACK;").is_err() || !connection.is_autocommit())
        {
            return Err(StoreError::PendingDataMove);
        }
        let phase = crate::data_moves::phase(connection, &catalog.profile, &id)?;
        if phase.as_deref() == Some("prepared")
            && orphan_catalog_matches(connection, owner, &intent.rows, false).unwrap_or(false)
        {
            catalog
                .root
                .rename(&target, &source)
                .map_err(|_| StoreError::PendingDataMove)?;
            if catalog.root.move_path_identity(&source)? != Some(directory)
                || catalog.root.move_path_identity(&target)?.is_some()
            {
                return Err(StoreError::PendingDataMove);
            }
            crate::data_moves::clear(connection, &catalog.profile, &id, "prepared")?;
            return Err(error);
        }
        if phase.as_deref() == Some("committed")
            && orphan_catalog_matches(connection, owner, &intent.rows, true).unwrap_or(false)
            && catalog.root.move_path_identity(&target)? == Some(directory)
            && catalog.root.move_path_identity(&source)?.is_none()
        {
            crate::data_moves::clear(connection, &catalog.profile, &id, "committed")?;
            return Err(StoreError::CommitUnknown);
        }
        return Err(StoreError::PendingDataMove);
    }
    crate::data_moves::checkpoint("orphan-catalog-committed");
    if catalog.root.move_path_identity(&target)? != Some(directory)
        || catalog.root.move_path_identity(&source)?.is_some()
    {
        return Err(StoreError::PendingDataMove);
    }
    crate::data_moves::clear(connection, &catalog.profile, &id, "committed")?;
    intent
        .rows
        .iter()
        .map(|row| descriptor(connection, owner, &row.name))
        .collect()
}

fn transfer_catalog_matches(
    connection: &rusqlite::Connection,
    old_owner: &PackId,
    new_owner: &PackId,
    rows: &[TransferRow],
    target: &std::path::Path,
    all_private: bool,
    transferred: bool,
) -> Result<bool, StoreError> {
    let target = target
        .to_str()
        .ok_or_else(|| StoreError::Invalid("non-UTF8 DATA target".into()))?;
    if all_private {
        for (owner, count) in [
            (old_owner, if transferred { 0 } else { rows.len() }),
            (new_owner, if transferred { rows.len() } else { 0 }),
        ] {
            let actual: u64 = connection.query_row(
                "SELECT count(*) FROM stores WHERE owner=?1 AND tier='private'",
                [owner.as_str()],
                |row| crate::sql_u64(row, 0),
            )?;
            if actual != count as u64 {
                return Ok(false);
            }
        }
    }
    for expected in rows {
        let read = |owner: &PackId| -> Result<Option<(String, String, bool, String)>, StoreError> {
            Ok(connection
                .query_row(
                    "SELECT tier,shape,orphaned,path FROM stores WHERE owner=?1 AND name=?2",
                    params![owner.as_str(), &expected.name],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )
                .optional()?)
        };
        let old = read(old_owner)?;
        let new = read(new_owner)?;
        let wanted = (
            tier_name(expected.tier).to_owned(),
            expected.shape.clone(),
            if transferred {
                false
            } else {
                expected.orphaned
            },
            if transferred {
                target.to_owned()
            } else {
                expected.path.clone()
            },
        );
        if if transferred {
            old.is_some() || new != Some(wanted)
        } else {
            old != Some(wanted) || new.is_some()
        } {
            return Ok(false);
        }
    }
    Ok(true)
}

#[allow(clippy::too_many_arguments)]
fn transfer_native_database(
    catalog: &Catalog,
    connection: &mut rusqlite::Connection,
    cx: &CallCx,
    old_owner: &PackId,
    new_owner: &PackId,
    rows: &[TransferRow],
    source: &std::path::Path,
    target: &std::path::Path,
    all_private: bool,
    grant: Grant,
    request: EffectRequest,
    args: Digest,
) -> Result<(), StoreError> {
    #[cfg(not(unix))]
    {
        let _ = (
            catalog,
            connection,
            cx,
            old_owner,
            new_owner,
            rows,
            source,
            target,
            all_private,
            grant,
            request,
            args,
        );
        Err(StoreError::Unavailable(
            "native DATA takeover unavailable".into(),
        ))
    }
    #[cfg(unix)]
    {
        if rows.is_empty() || rows.len() > MAX_LIST_ROWS as usize || old_owner == new_owner {
            return Err(StoreError::Invalid("invalid DATA takeover group".into()));
        }
        let relative = match rows[0].tier {
            StoreTier::Private => format!("orphaned/{old_owner}/private.sqlite"),
            _ => format!("orphaned/{old_owner}/public/{}.sqlite", rows[0].name),
        };
        if source != DataRoot::profile_relative(&catalog.profile, &relative)?
            || rows
                .iter()
                .any(|row| std::path::Path::new(&row.path) != source)
        {
            return Err(StoreError::Denied(
                "DATA takeover source is not its canonical orphan path".into(),
            ));
        }
        let bytes = rows.iter().try_fold(TASK_CONTROL_BYTES, |total, row| {
            total
                .checked_add((row.name.len() + row.shape.len() + row.path.len() + 256) as u64)
                .ok_or(StoreError::Quota)
        })?;
        let _task = cx.control().reserve(ControlKind::Task, bytes)?;
        let old_registration = cx
            .control()
            .reserve(ControlKind::Registration, DATABASE_CONTROL_BYTES)?;
        let new_registration = cx
            .control()
            .reserve(ControlKind::Registration, DATABASE_CONTROL_BYTES)?;
        // Keep ordinary open's catalog -> owner -> physical quota lock order.
        let old_custody = crate::data_custody::DataCustody::registered(
            Some(catalog.root.owner_lease(&catalog.profile, old_owner)?),
            old_registration,
        );
        let new_custody = crate::data_custody::DataCustody::registered(
            Some(catalog.root.owner_lease(&catalog.profile, new_owner)?),
            new_registration,
        );
        preflight_data_database(&catalog.root, source)?;
        let permit = grant.consume(cx, &request, &args)?;
        let source_guard = catalog.root.data_transfer_registration(
            source,
            false,
            catalog.disk_policy,
            old_custody,
        )?;
        let target_guard = catalog.root.data_transfer_registration(
            target,
            true,
            catalog.disk_policy,
            new_custody,
        )?;
        source_guard.with_exclusive_data_transfer(&target_guard, |files| {
            check_native_completion(cx, permit.deadline(), false)?;
            let [main, journal] = files.file_identities();
            let intent = crate::data_moves::MoveIntent {
                version: 1, profile: catalog.profile.clone(), kind: crate::data_moves::MoveKind::Takeover,
                old_owner: old_owner.clone(), new_owner: new_owner.clone(),
                source: source.to_str().ok_or_else(|| StoreError::Invalid("non-UTF8 DATA source".into()))?.into(),
                target: target.to_str().ok_or_else(|| StoreError::Invalid("non-UTF8 DATA target".into()))?.into(),
                grant: Some(permit.id().clone()), directory: None,
                rows: rows.iter().map(|row| crate::data_moves::MoveRow { name: row.name.clone(), tier: row.tier, shape: row.shape.clone(), source: row.path.clone(), target: target.to_string_lossy().into_owned(), main, journal }).collect(),
            };
            let prepared = connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
            if !transfer_catalog_matches(&prepared, old_owner, new_owner, rows, target, all_private, false)? { return Err(StoreError::Conflict("DATA takeover metadata changed during admission".into())); }
            let id = crate::data_moves::insert_prepared(&prepared, &intent)?;
            if prepared.commit().is_err() || !connection.is_autocommit() || crate::data_moves::phase(connection, &catalog.profile, &id)?.as_deref() != Some("prepared") { return Err(StoreError::PendingDataMove); }
            crate::data_moves::checkpoint("prepared");
            if let Err(error) = files.move_to_target() {
                // Only the helper's exact, fsynced rollback proof permits
                // clearing PREPARED. Uncertainty keeps both endpoints blocked.
                if matches!(error, StoreError::CommitUnknown) { return Err(StoreError::PendingDataMove); }
                crate::data_moves::clear(connection, &catalog.profile, &id, "prepared")?;
                return Err(error);
            }
            let metadata = (|| -> Result<(), StoreError> {
                let tx = connection.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
                if crate::data_moves::phase(&tx, &catalog.profile, &id)?.as_deref() != Some("prepared") || !transfer_catalog_matches(&tx, old_owner, new_owner, rows, target, all_private, false)? { return Err(StoreError::PendingDataMove); }
                for row in rows {
                    let changed = tx.execute("UPDATE stores SET owner=?1,path=?2,orphaned=0 WHERE owner=?3 AND name=?4 AND orphaned=1", params![new_owner.as_str(), &intent.target, old_owner.as_str(), &row.name])?;
                    if changed != 1 { return Err(StoreError::Conflict("DATA takeover catalog row changed".into())); }
                    tx.execute("UPDATE seed_versions SET owner=?1 WHERE owner=?2 AND name=?3", params![new_owner.as_str(), old_owner.as_str(), &row.name])?;
                }
                crate::data_moves::mark_committed(&tx, &id)?;
                tx.commit()?;
                if !connection.is_autocommit() { return Err(StoreError::PendingDataMove); }
                Ok(())
            })();
            match metadata {
                Ok(()) => {
                    files.accept_target().map_err(|_| StoreError::PendingDataMove)?;
                    crate::data_moves::checkpoint("catalog-committed");
                    crate::data_moves::clear(connection, &catalog.profile, &id, "committed")?;
                    check_native_completion(cx, permit.deadline(), true)?;
                    Ok(())
                }
                Err(error) => {
                    if !connection.is_autocommit() && (connection.execute_batch("ROLLBACK;").is_err() || !connection.is_autocommit()) { files.quarantine(); return Err(StoreError::PendingDataMove); }
                    let phase = crate::data_moves::phase(connection, &catalog.profile, &id)?;
                    if phase.as_deref() == Some("prepared") && transfer_catalog_matches(connection, old_owner, new_owner, rows, target, all_private, false).unwrap_or(false) {
                        files.rollback_to_source().map_err(|_| StoreError::PendingDataMove)?;
                        crate::data_moves::clear(connection, &catalog.profile, &id, "prepared")?;
                        Err(error)
                    } else if phase.as_deref() == Some("committed") && transfer_catalog_matches(connection, old_owner, new_owner, rows, target, all_private, true).unwrap_or(false) {
                        files.accept_target().map_err(|_| StoreError::PendingDataMove)?;
                        crate::data_moves::clear(connection, &catalog.profile, &id, "committed")?;
                        Err(StoreError::CommitUnknown)
                    } else {
                        files.quarantine();
                        Err(StoreError::PendingDataMove)
                    }
                }
            }
        })
    }
}
fn tier_name(t: StoreTier) -> &'static str {
    match t {
        StoreTier::Private => "private",
        StoreTier::Public => "public",
        StoreTier::ReadOnly => "read-only",
    }
}
fn parse_tier(t: &str) -> Result<StoreTier, StoreError> {
    match t {
        "private" => Ok(StoreTier::Private),
        "public" => Ok(StoreTier::Public),
        "read-only" => Ok(StoreTier::ReadOnly),
        _ => Err(StoreError::Invalid("unknown store tier".into())),
    }
}
fn store_path(
    profile: &ProfileId,
    owner: &PackId,
    name: &str,
    tier: StoreTier,
) -> Result<PathBuf, StoreError> {
    DataRoot::profile_relative(
        profile,
        &match tier {
            StoreTier::Private => format!("data/{owner}/private.sqlite"),
            _ => format!("data/{owner}/public/{name}.sqlite"),
        },
    )
}
fn descriptor(
    conn: &rusqlite::Connection,
    owner: &PackId,
    name: &str,
) -> Result<StoreDescriptor, StoreError> {
    let row: Option<(String, String, bool)> = conn
        .query_row(
            "SELECT tier,shape,orphaned FROM stores WHERE owner=?1 AND name=?2",
            params![owner.as_str(), name],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    let (tier, shape, orphaned) = row.ok_or(StoreError::NotFound)?;
    Ok(StoreDescriptor {
        owner: owner.clone(),
        name: name.into(),
        tier: parse_tier(&tier)?,
        data_shape: shape,
        orphaned,
    })
}
fn local_access(
    cx: &CallCx,
    profile: &ProfileId,
    meta: &StoreDescriptor,
    write: bool,
) -> Result<(), StoreError> {
    let own = cx.principal().pack().is_some_and(|(p, _)| p == &meta.owner);
    if *cx.profile() != *profile {
        return Err(StoreError::Denied(
            "Profile storage is isolated; use the dangerous user-global store".into(),
        ));
    }
    if meta.orphaned
        || meta.tier == StoreTier::Private && !own
        || write && meta.tier == StoreTier::ReadOnly && !own
    {
        return Err(StoreError::Denied(
            "store ownership or tier forbids operation".into(),
        ));
    }
    Ok(())
}
fn configure_limits(conn: &rusqlite::Connection) {
    unsafe {
        rusqlite::ffi::sqlite3_limit(
            conn.handle(),
            rusqlite::ffi::SQLITE_LIMIT_LENGTH,
            MAX_DATA_VALUE_BYTES as i32,
        );
        rusqlite::ffi::sqlite3_limit(
            conn.handle(),
            rusqlite::ffi::SQLITE_LIMIT_SQL_LENGTH,
            64 * 1024,
        );
        rusqlite::ffi::sqlite3_limit(conn.handle(), rusqlite::ffi::SQLITE_LIMIT_COLUMN, 256);
        rusqlite::ffi::sqlite3_limit(conn.handle(), rusqlite::ffi::SQLITE_LIMIT_ATTACHED, 0);
        // Cap preparation and expression allocations before row conversion or progress callbacks.
        rusqlite::ffi::sqlite3_limit(conn.handle(), rusqlite::ffi::SQLITE_LIMIT_VDBE_OP, 25_000);
        rusqlite::ffi::sqlite3_limit(conn.handle(), rusqlite::ffi::SQLITE_LIMIT_EXPR_DEPTH, 128);
        rusqlite::ffi::sqlite3_limit(
            conn.handle(),
            rusqlite::ffi::SQLITE_LIMIT_COMPOUND_SELECT,
            64,
        );
        rusqlite::ffi::sqlite3_limit(conn.handle(), rusqlite::ffi::SQLITE_LIMIT_FUNCTION_ARG, 64);
        rusqlite::ffi::sqlite3_limit(
            conn.handle(),
            rusqlite::ffi::SQLITE_LIMIT_VARIABLE_NUMBER,
            256,
        );
        rusqlite::ffi::sqlite3_limit(
            conn.handle(),
            rusqlite::ffi::SQLITE_LIMIT_LIKE_PATTERN_LENGTH,
            4096,
        );
        rusqlite::ffi::sqlite3_limit(conn.handle(), rusqlite::ffi::SQLITE_LIMIT_TRIGGER_DEPTH, 0);
    }
}
/// Restrictive authorizer; unknown actions, external DBs and extensions are denied.
pub fn restrict(conn: &rusqlite::Connection) -> Result<(), StoreError> {
    conn.authorizer(Some(|cx: AuthContext<'_>| {
        if cx.database_name.is_some_and(|n| n != "main") {
            return Authorization::Deny;
        }
        let internal_table = match cx.action {
            AuthAction::Read { table_name, .. }
            | AuthAction::Insert { table_name }
            | AuthAction::Update { table_name, .. }
            | AuthAction::Delete { table_name }
            | AuthAction::CreateTable { table_name }
            | AuthAction::CreateIndex { table_name, .. }
            | AuthAction::DropTable { table_name }
            | AuthAction::DropIndex { table_name, .. } => table_name.starts_with("__tobkiri_"),
            _ => false,
        };
        if internal_table {
            return Authorization::Deny;
        }
        match cx.action {
            AuthAction::Select
            | AuthAction::Read { .. }
            | AuthAction::Insert { .. }
            | AuthAction::Update { .. }
            | AuthAction::Delete { .. }
            | AuthAction::CreateTable { .. }
            | AuthAction::CreateIndex { .. }
            | AuthAction::DropTable { .. }
            | AuthAction::DropIndex { .. }
            | AuthAction::Recursive => Authorization::Allow,
            AuthAction::Function { function_name }
                if matches!(
                    function_name.to_ascii_lowercase().as_str(),
                    "count"
                        | "sum"
                        | "avg"
                        | "min"
                        | "max"
                        | "coalesce"
                        | "ifnull"
                        | "nullif"
                        | "length"
                        | "lower"
                        | "upper"
                        | "substr"
                        | "substring"
                        | "trim"
                        | "ltrim"
                        | "rtrim"
                        | "replace"
                        | "abs"
                        | "round"
                        | "typeof"
                        | "hex"
                        | "unhex"
                        | "printf"
                        | "format"
                        | "iif"
                        | "json"
                        | "json_array"
                        | "json_object"
                        | "json_extract"
                        | "json_valid"
                        | "json_type"
                        | "json_quote"
                        | "json_set"
                        | "json_remove"
                        | "json_patch"
                        | "json_insert"
                        | "json_replace"
                        | "json_array_length"
                        | "json_group_array"
                        | "json_group_object"
                ) =>
            {
                Authorization::Allow
            }
            AuthAction::Pragma {
                pragma_name,
                pragma_value,
            } if matches!(
                pragma_name.to_ascii_lowercase().as_str(),
                "table_info" | "table_xinfo" | "index_info" | "index_list" | "foreign_key_list"
            ) && pragma_value.is_some() =>
            {
                Authorization::Allow
            }
            _ => Authorization::Deny,
        }
    }))?;
    Ok(())
}

fn sidecar_path(path: &std::path::Path, suffix: &str) -> Result<PathBuf, StoreError> {
    let name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| StoreError::Invalid("invalid SQLite filename".into()))?;
    Ok(path.with_file_name(format!("{name}{suffix}")))
}

fn preflight_data_database(root: &DataRoot, path: &std::path::Path) -> Result<(), StoreError> {
    let Some(main_size) = root.file_len(path)? else {
        return Ok(());
    };
    if main_size > MAX_DATABASE_BYTES {
        return Err(StoreError::Quota);
    }
    for suffix in ["-wal", "-shm"] {
        if root.file_len(sidecar_path(path, suffix)?)?.is_some() {
            return Err(StoreError::Unavailable(
                "existing SQLite WAL state needs aggregate WAL disk accounting".into(),
            ));
        }
    }
    if root
        .file_len(sidecar_path(path, "-journal")?)?
        .is_some_and(|size| size > MAX_JOURNAL_BYTES)
    {
        return Err(StoreError::Quota);
    }
    // The quota VFS counts an existing DELETE journal and reserves rollback
    // capacity before SQLite's normal hot-journal recovery may mutate the DB.
    if main_size == 0 {
        return Ok(());
    }
    if main_size < 100 {
        return Err(StoreError::Invalid("truncated SQLite DATA database".into()));
    }
    // The wrapper reads and checks the journal-format header through SQLite's
    // own open handle before returning xOpen. Opening a separate inspection FD
    // here would release another connection's process-wide POSIX locks on close.
    Ok(())
}

fn configure_data_connection(
    connection: &rusqlite::Connection,
    root: &DataRoot,
    path: &std::path::Path,
) -> Result<(), StoreError> {
    // Limits and defensive switches precede any prepare of guest SQL. Setup
    // statements below are trusted, fixed literals in Core.
    configure_limits(connection);
    connection.set_db_config(rusqlite::config::DbConfig::SQLITE_DBCONFIG_DEFENSIVE, true)?;
    connection.set_db_config(
        rusqlite::config::DbConfig::SQLITE_DBCONFIG_TRUSTED_SCHEMA,
        false,
    )?;
    // rusqlite is built without its load-extension feature, so the native
    // extension loader remains at SQLite's default disabled state. Guest SQL
    // is also checked by `restrict`, whose function allowlist excludes
    // `load_extension`.
    connection.busy_timeout(Duration::from_millis(50))?;

    let existing_size = root.file_len(path)?.unwrap_or(0);
    if existing_size == 0 {
        connection.execute_batch("PRAGMA page_size=4096;")?;
    }
    let mode: String = connection.query_row("PRAGMA journal_mode", [], |row| row.get(0))?;
    if !mode.eq_ignore_ascii_case("delete") {
        return Err(StoreError::Unavailable(format!(
            "SQLite journal mode {mode} is not admitted by bounded DELETE-mode DATA"
        )));
    }
    // Limit page images plus a conservative 64-byte per-page journal overhead
    // to 64 MiB. This independently bounds main DB and rollback journal at
    // 64 MiB each, including small-page databases and journal headers.
    let page_size: u64 =
        connection.query_row("PRAGMA page_size", [], |row| crate::sql_u64(row, 0))?;
    if page_size == 0 || page_size > 65_536 || !page_size.is_power_of_two() {
        return Err(StoreError::Invalid("unsupported SQLite page size".into()));
    }
    let journal_page_bytes = page_size.checked_add(64).ok_or(StoreError::Quota)?;
    let max_pages = MAX_JOURNAL_BYTES / journal_page_bytes;
    let configured_max: u64 =
        connection.query_row(&format!("PRAGMA max_page_count={max_pages}"), [], |row| {
            crate::sql_u64(row, 0)
        })?;
    if configured_max > max_pages {
        return Err(StoreError::Quota);
    }
    connection.execute_batch(
        "PRAGMA temp_store=MEMORY;
         PRAGMA cache_size=-512;
         PRAGMA mmap_size=0;
         PRAGMA synchronous=FULL;
         PRAGMA foreign_keys=ON;
         PRAGMA trusted_schema=OFF;
         CREATE TABLE IF NOT EXISTS __tobkiri_kv(
             store TEXT NOT NULL,
             key TEXT NOT NULL,
             value BLOB NOT NULL,
             PRIMARY KEY(store,key)
         ) WITHOUT ROWID;",
    )?;
    let temp_store: u32 = connection.query_row("PRAGMA temp_store", [], |row| row.get(0))?;
    let cache_size: i64 = connection.query_row("PRAGMA cache_size", [], |row| row.get(0))?;
    let mmap_size: u64 =
        connection.query_row("PRAGMA mmap_size", [], |row| crate::sql_u64(row, 0))?;
    if temp_store != 2 || cache_size != -512 || mmap_size != 0 {
        return Err(StoreError::Unavailable(
            "SQLite could not enforce bounded in-memory temporary storage and cache".into(),
        ));
    }
    restrict(connection)?;
    Ok(())
}

struct NativeSqlWork {
    remaining: AtomicU64,
    exhausted: AtomicBool,
}
fn with_progress<T>(
    database: &NativeDatabase,
    connection: &rusqlite::Connection,
    cx: &CallCx,
    deadline: std::time::Instant,
    run: impl FnOnce() -> Result<T, StoreError>,
) -> Result<T, StoreError> {
    with_progress_limit(
        database,
        connection,
        cx,
        deadline,
        cx.limits().fuel_per_call,
        run,
    )
}
fn with_progress_limit<T>(
    database: &NativeDatabase,
    connection: &rusqlite::Connection,
    cx: &CallCx,
    deadline: std::time::Instant,
    fuel_limit: u64,
    run: impl FnOnce() -> Result<T, StoreError>,
) -> Result<T, StoreError> {
    let work = Arc::new(NativeSqlWork {
        remaining: AtomicU64::new(cx.limits().fuel_per_call.min(fuel_limit)),
        exhausted: AtomicBool::new(false),
    });
    with_progress_work(database, connection, cx, deadline, &work, run)
}
fn with_progress_work<T>(
    database: &NativeDatabase,
    connection: &rusqlite::Connection,
    cx: &CallCx,
    deadline: std::time::Instant,
    work: &Arc<NativeSqlWork>,
    run: impl FnOnce() -> Result<T, StoreError>,
) -> Result<T, StoreError> {
    if database.is_quarantined() {
        return Err(StoreError::Unavailable(
            "SQLite DATA connection is quarantined after progress-handler failure".into(),
        ));
    }
    cx.check_active()?;
    if std::time::Instant::now() >= deadline {
        return Err(StoreError::Gate(tobkiri_gate::GateError::Deadline));
    }
    if work.remaining.load(Ordering::Acquire) == 0 {
        return Err(StoreError::Gate(tobkiri_gate::GateError::Limit(
            "SQLite instruction budget exhausted".into(),
        )));
    }
    let progress_cx = cx.clone();
    let progress_work = work.clone();
    if let Err(error) = connection.progress_handler(
        1,
        Some(move || {
            let available = progress_work.remaining.fetch_update(
                Ordering::AcqRel,
                Ordering::Acquire,
                |remaining| remaining.checked_sub(1),
            );
            if !matches!(available, Ok(remaining) if remaining > 1) {
                progress_work.exhausted.store(true, Ordering::Release);
                return true;
            }
            progress_cx.check_active().is_err() || std::time::Instant::now() >= deadline
        }),
    ) {
        database.quarantine();
        return Err(StoreError::Unavailable(format!(
            "SQLite progress handler could not be installed; connection quarantined: {error}"
        )));
    }
    struct Reset<'a> {
        database: &'a NativeDatabase,
        connection: &'a rusqlite::Connection,
        armed: bool,
    }
    impl Reset<'_> {
        fn clear(&mut self) -> Result<(), StoreError> {
            self.armed = false;
            match self.connection.progress_handler(0, None::<fn() -> bool>) {
                Ok(()) => Ok(()),
                Err(error) => {
                    self.database.quarantine();
                    Err(StoreError::Unavailable(format!(
                        "SQLite progress handler could not be cleared; connection quarantined: {error}"
                    )))
                }
            }
        }
    }
    impl Drop for Reset<'_> {
        fn drop(&mut self) {
            if self.armed
                && self
                    .connection
                    .progress_handler(0, None::<fn() -> bool>)
                    .is_err()
            {
                self.database.quarantine();
            }
        }
    }
    let mut reset = Reset {
        database,
        connection,
        armed: true,
    };
    let result = run();
    reset.clear()?;
    if let Err(error) = cx.check_active() {
        return Err(error.into());
    }
    if std::time::Instant::now() >= deadline {
        return Err(StoreError::Gate(tobkiri_gate::GateError::Deadline));
    }
    if work.exhausted.load(Ordering::Acquire) {
        return Err(StoreError::Gate(tobkiri_gate::GateError::Limit(
            "SQLite instruction budget exhausted".into(),
        )));
    }
    result
}

/// Roll back only the exact transaction already held by this connection claim.
/// Cleanup must remain possible after cancellation, without reviving guest SQL.
fn rollback_native_transaction(
    database: &NativeDatabase,
    connection: &rusqlite::Connection,
) -> Result<(), StoreError> {
    if let Err(error) = connection.progress_handler(0, None::<fn() -> bool>) {
        database.quarantine();
        return Err(StoreError::Unavailable(format!(
            "SQLite rollback progress cleanup is unconfirmed; connection quarantined: {error}"
        )));
    }
    if connection.is_autocommit() {
        return Ok(());
    }
    let result = with_internal_authorizer(database, connection, || {
        connection.execute_batch("ROLLBACK;")?;
        Ok(())
    });
    if result.is_err() || !connection.is_autocommit() {
        database.quarantine();
        return Err(StoreError::Unavailable(
            "SQLite transaction rollback is unconfirmed; connection quarantined".into(),
        ));
    }
    Ok(())
}

/// A fixed savepoint keeps an autocommit write provisional until its bounded
/// result and activity checks succeed. SQL/host errors before RELEASE therefore
/// have a verified rollback, including SQLite's partial `OR FAIL` behavior.
fn with_native_operation<T>(
    database: &NativeDatabase,
    connection: &rusqlite::Connection,
    cx: &CallCx,
    deadline: std::time::Instant,
    fuel_limit: u64,
    autocommit_write: bool,
    admission: CommitAdmission<'_>,
    run: impl FnOnce() -> Result<T, StoreError>,
) -> Result<T, StoreError> {
    let work = Arc::new(NativeSqlWork {
        remaining: AtomicU64::new(cx.limits().fuel_per_call.min(fuel_limit)),
        exhausted: AtomicBool::new(false),
    });
    if !autocommit_write {
        return with_progress_work(database, connection, cx, deadline, &work, run);
    }
    if !connection.is_autocommit() {
        database.quarantine();
        return Err(StoreError::Unavailable(
            "SQLite DATA has an unmanaged transaction; connection quarantined".into(),
        ));
    }
    let opened = with_progress_work(database, connection, cx, deadline, &work, || {
        with_internal_authorizer(database, connection, || {
            connection.execute_batch("SAVEPOINT __tobkiri_call;")?;
            Ok(())
        })
    });
    if let Err(error) = opened {
        rollback_native_transaction(database, connection)?;
        return Err(error);
    }
    let output = match with_progress_work(database, connection, cx, deadline, &work, run) {
        Ok(output) => output,
        Err(error) => {
            rollback_native_transaction(database, connection)?;
            return Err(error);
        }
    };
    let mut commit_started = false;
    let mut commit_succeeded = false;
    let committed = admission.finish(database, connection, || {
        with_progress_work(database, connection, cx, deadline, &work, || {
            commit_started = true;
            with_internal_authorizer(database, connection, || {
                connection.execute_batch("RELEASE __tobkiri_call;")?;
                // Latch SQL certainty before authorizer/progress restoration and
                // delivery checks can fail after the actual commit.
                commit_succeeded = true;
                Ok(())
            })
        })
    });
    if let Err(error) = committed {
        if commit_succeeded && connection.is_autocommit() {
            return Err(StoreError::CommitUnknown);
        }
        if !commit_started || !connection.is_autocommit() {
            rollback_native_transaction(database, connection)?;
            return Err(error);
        }
        return Err(database.commit_unknown());
    }
    if !connection.is_autocommit() {
        return Err(database.commit_unknown());
    }
    Ok(output)
}

fn check_native_completion(
    cx: &CallCx,
    deadline: std::time::Instant,
    committed_write: bool,
) -> Result<(), StoreError> {
    let result = cx.check_active().and_then(|()| {
        if std::time::Instant::now() >= deadline {
            Err(tobkiri_gate::GateError::Deadline)
        } else {
            Ok(())
        }
    });
    result.map_err(|error| {
        if committed_write {
            StoreError::CommitUnknown
        } else {
            error.into()
        }
    })
}

fn with_internal_authorizer<T>(
    database: &NativeDatabase,
    connection: &rusqlite::Connection,
    run: impl FnOnce() -> Result<T, StoreError>,
) -> Result<T, StoreError> {
    if let Err(error) = connection.authorizer(None::<fn(AuthContext<'_>) -> Authorization>) {
        database.quarantine();
        return Err(error.into());
    }
    let result = run();
    let restored = restrict(connection);
    if restored.is_err() {
        database.quarantine();
    }
    match (result, restored) {
        (Ok(value), Ok(())) => Ok(value),
        (Err(error), _) => Err(error),
        (Ok(_), Err(error)) => Err(error),
    }
}

fn effective_output_limit(cx: &CallCx, permit: &DispatchPermit) -> Result<usize, StoreError> {
    let bytes = cx.limits().output_bytes.min(permit.limits().output_bytes);
    usize::try_from(bytes).map_err(|_| StoreError::Quota)
}

fn execute_fixed_native(
    connection: &rusqlite::Connection,
    store: &str,
    request: &crate::FixedDataRequest,
    output_limit: usize,
    output_budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, StoreError> {
    let fields = match request.arguments().inspect() {
        CoreRef::Obj(fields) => fields,
        _ => return Err(StoreError::Invalid("KV argument record required".into())),
    };
    let budget_err = |_| StoreError::Quota;
    match request.operation() {
        crate::FixedDataOperation::Get => {
            let key = core_text(fields.get("key"), "KV key required")?;
            let value: Option<Vec<u8>> = connection
                .query_row(
                    "SELECT value FROM __tobkiri_kv WHERE store=?1 AND key=?2",
                    params![store, key],
                    |row| row.get(0),
                )
                .optional()?;
            let Some(value) = value else {
                return CoreValue::null(output_budget).map_err(budget_err);
            };
            let encoded_max = value
                .len()
                .checked_mul(4)
                .and_then(|bytes| bytes.checked_add(16))
                .ok_or(StoreError::Quota)?;
            if value.len() > MAX_DATA_VALUE_BYTES || encoded_max > output_limit {
                return Err(StoreError::Quota);
            }
            let mut result =
                CoreValue::array_builder(output_budget, value.len()).map_err(budget_err)?;
            for byte in value {
                result
                    .push(CoreValue::int(i64::from(byte), output_budget).map_err(budget_err)?)
                    .map_err(budget_err)?;
            }
            result.finish().map_err(budget_err)
        }
        crate::FixedDataOperation::Put => {
            let key = core_text(fields.get("key"), "KV key required")?;
            let value = core_bytes(fields.get("value"))?;
            connection.execute(
                "INSERT INTO __tobkiri_kv(store,key,value) VALUES(?1,?2,?3)
                 ON CONFLICT(store,key) DO UPDATE SET value=excluded.value",
                params![store, key, value],
            )?;
            CoreValue::null(output_budget).map_err(budget_err)
        }
        crate::FixedDataOperation::Delete => {
            let key = core_text(fields.get("key"), "KV key required")?;
            connection.execute(
                "DELETE FROM __tobkiri_kv WHERE store=?1 AND key=?2",
                params![store, key],
            )?;
            CoreValue::null(output_budget).map_err(budget_err)
        }
        crate::FixedDataOperation::List => {
            let prefix = core_text(fields.get("prefix"), "KV prefix required")?;
            let after_value = fields
                .get("after")
                .ok_or_else(|| StoreError::Invalid("KV continuation required".into()))?;
            let after = match after_value.inspect() {
                CoreRef::Null => None,
                CoreRef::Str(value) => Some(value),
                _ => return Err(StoreError::Invalid("invalid KV continuation".into())),
            };
            let limit_value = fields
                .get("limit")
                .ok_or_else(|| StoreError::Invalid("invalid KV limit".into()))?;
            let CoreRef::Int(limit) = limit_value.inspect() else {
                return Err(StoreError::Invalid("invalid KV limit".into()));
            };
            let limit = u32::try_from(limit)
                .map_err(|_| StoreError::Invalid("invalid KV limit".into()))?
                .min(MAX_LIST_ROWS);
            let mut statement = connection.prepare(
                "SELECT key FROM __tobkiri_kv
                 WHERE store=?1 AND substr(key,1,length(?2))=?2
                   AND (?3 IS NULL OR key>?3)
                 ORDER BY key LIMIT ?4",
            )?;
            let mut rows = statement.query(params![store, prefix, after, limit])?;
            let mut keys = Vec::new();
            let mut bytes = 0usize;
            while let Some(row) = rows.next()? {
                let key: String = row.get(0)?;
                bytes = bytes
                    .checked_add(key.len().saturating_mul(6))
                    .and_then(|bytes| bytes.checked_add(3))
                    .ok_or(StoreError::Quota)?;
                if bytes > output_limit {
                    return Err(StoreError::Quota);
                }
                keys.push(key);
            }
            let mut result =
                CoreValue::array_builder(output_budget, keys.len()).map_err(budget_err)?;
            for key in keys {
                result
                    .push(CoreValue::string(&key, output_budget).map_err(budget_err)?)
                    .map_err(budget_err)?;
            }
            result.finish().map_err(budget_err)
        }
    }
}

fn core_text(value: Option<CoreValue>, message: &str) -> Result<String, StoreError> {
    let Some(value) = value else {
        return Err(StoreError::Invalid(message.into()));
    };
    match value.inspect() {
        CoreRef::Str(value) => Ok(value.to_owned()),
        _ => Err(StoreError::Invalid(message.into())),
    }
}

fn core_bytes(value: Option<CoreValue>) -> Result<Vec<u8>, StoreError> {
    let Some(value) = value else {
        return Err(StoreError::Invalid("KV value required".into()));
    };
    let CoreRef::Arr(bytes) = value.inspect() else {
        return Err(StoreError::Invalid("KV bytes required".into()));
    };
    if bytes.len() > MAX_DATA_VALUE_BYTES {
        return Err(StoreError::Quota);
    }
    let mut output = Vec::with_capacity(bytes.len());
    for byte in bytes.iter() {
        match byte.inspect() {
            CoreRef::Int(value) => output.push(
                u8::try_from(value)
                    .map_err(|_| StoreError::Invalid("KV byte outside u8".into()))?,
            ),
            _ => return Err(StoreError::Invalid("KV byte outside u8".into())),
        }
    }
    Ok(output)
}

fn execute_legacy_native(
    connection: &rusqlite::Connection,
    store: &str,
    call: StoreCall,
    output_limit: usize,
) -> Result<StoreResult, StoreError> {
    match call {
        StoreCall::KvGet { key } => {
            let value: Option<Vec<u8>> = connection
                .query_row(
                    "SELECT value FROM __tobkiri_kv WHERE store=?1 AND key=?2",
                    params![store, key],
                    |row| row.get(0),
                )
                .optional()?;
            if value
                .as_ref()
                .is_some_and(|value| value.len() > output_limit)
            {
                return Err(StoreError::Quota);
            }
            Ok(StoreResult::Value(value))
        }
        StoreCall::KvPut { key, value } => {
            if value.len() > MAX_DATA_VALUE_BYTES {
                return Err(StoreError::Quota);
            }
            connection.execute(
                "INSERT INTO __tobkiri_kv(store,key,value) VALUES(?1,?2,?3)
                 ON CONFLICT(store,key) DO UPDATE SET value=excluded.value",
                params![store, key, value],
            )?;
            Ok(StoreResult::Unit)
        }
        StoreCall::KvDelete { key } => {
            connection.execute(
                "DELETE FROM __tobkiri_kv WHERE store=?1 AND key=?2",
                params![store, key],
            )?;
            Ok(StoreResult::Unit)
        }
        StoreCall::KvList {
            prefix,
            after,
            limit,
        } => {
            let mut statement = connection.prepare(
                "SELECT key FROM __tobkiri_kv
                 WHERE store=?1 AND substr(key,1,length(?2))=?2
                   AND (?3 IS NULL OR key>?3)
                 ORDER BY key LIMIT ?4",
            )?;
            let mut rows =
                statement.query(params![store, prefix, after, limit.min(MAX_LIST_ROWS)])?;
            let mut keys = Vec::new();
            let mut bytes = 0usize;
            while let Some(row) = rows.next()? {
                let key: String = row.get(0)?;
                bytes = bytes
                    .checked_add(key.len().saturating_mul(6))
                    .and_then(|count| count.checked_add(3))
                    .ok_or(StoreError::Quota)?;
                if bytes > output_limit {
                    return Err(StoreError::Quota);
                }
                keys.push(key);
            }
            Ok(StoreResult::Keys(keys))
        }
        StoreCall::SqlExec { .. } | StoreCall::SqlQuery { .. } => Err(StoreError::Unavailable(
            "legacy SQL must use the typed gated native statement API".into(),
        )),
    }
}

/// The complete argument object used to bind a store grant before execution.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "kebab-case", deny_unknown_fields)]
pub enum StoreCall {
    KvGet {
        key: String,
    },
    KvPut {
        key: String,
        value: Vec<u8>,
    },
    KvDelete {
        key: String,
    },
    KvList {
        prefix: String,
        after: Option<String>,
        limit: u32,
    },
    SqlExec {
        sql: String,
        params: Vec<CanonValue>,
    },
    SqlQuery {
        sql: String,
        params: Vec<CanonValue>,
        limit: u32,
    },
}
impl StoreCall {
    pub fn digest(&self) -> Result<Digest, StoreError> {
        Ok(Digest::hash(&canonical(self)?))
    }
    pub fn operation(&self) -> StoreOperation {
        match self {
            Self::KvGet { .. } | Self::KvList { .. } => StoreOperation::Read,
            Self::KvPut { .. } => StoreOperation::Write,
            Self::KvDelete { .. } => StoreOperation::Delete,
            Self::SqlExec { .. } => StoreOperation::SqlWrite,
            Self::SqlQuery { .. } => StoreOperation::SqlRead,
        }
    }
}
#[derive(Debug)]
pub enum StoreResult {
    Value(Option<Vec<u8>>),
    Keys(Vec<String>),
    Rows(Vec<Vec<CanonValue>>),
    Changed(u64),
    Unit,
}
/// User-wide storage is available only under explicit dangerous authority.
#[derive(Clone)]
pub struct UserGlobalStore {
    name: String,
    native: Arc<NativeDatabase>,
    _registration: Arc<ControlReservation>,
}
impl UserGlobalStore {
    pub fn open_request(name: &str) -> Result<(EffectRequest, Digest), StoreError> {
        crate::secure::name(name)?;
        Ok((
            EffectRequest::UserGlobalStorage {
                store: name.into(),
                operation: StoreOperation::Open,
            },
            Digest::hash(&canonical(&name)?),
        ))
    }
    pub fn open(
        root: &DataRoot,
        cx: &CallCx,
        name: &str,
        grant: Grant,
    ) -> Result<Self, StoreError> {
        Self::open_with_disk_policy(root, cx, name, grant, crate::StoreDiskPolicy::default())
    }
    /// Use narrower native ceilings for the physical account-root database.
    pub fn open_with_disk_policy(
        root: &DataRoot,
        cx: &CallCx,
        name: &str,
        grant: Grant,
        disk_policy: crate::StoreDiskPolicy,
    ) -> Result<Self, StoreError> {
        let (request, args) = Self::open_request(name)?;
        root.validate_native_data_root()?;
        let registration = Arc::new(
            cx.control()
                .reserve(ControlKind::Registration, TASK_CONTROL_BYTES)?,
        );
        let path = PathBuf::from("user-global").join(format!("{name}.sqlite"));
        preflight_data_database(root, &path)?;
        cx.check_active()?;
        grant.consume(cx, &request, &args)?;
        let native = open_user_global_native(root, cx, name, &path, disk_policy)?;
        Ok(Self {
            name: name.into(),
            native,
            _registration: registration,
        })
    }
    pub fn request(&self, call: &StoreCall) -> EffectRequest {
        EffectRequest::UserGlobalStorage {
            store: self.name.clone(),
            operation: call.operation(),
        }
    }
    pub fn execute(
        &self,
        cx: &CallCx,
        call: StoreCall,
        grant: Grant,
    ) -> Result<StoreResult, StoreError> {
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "user-global connection has an active SQL transaction".into(),
            ));
        }
        cx.check_active()?;
        self.native.check_owner_live()?;
        let task_bytes = match &call {
            StoreCall::KvPut { value, .. } => value.len() as u64 + TASK_CONTROL_BYTES,
            StoreCall::KvGet { .. } => MAX_DATA_VALUE_BYTES as u64 + TASK_CONTROL_BYTES,
            StoreCall::KvList { .. } => 1024 * 1024 + TASK_CONTROL_BYTES,
            StoreCall::KvDelete { .. } | StoreCall::SqlExec { .. } | StoreCall::SqlQuery { .. } => {
                TASK_CONTROL_BYTES
            }
        };
        let _task = cx.control().reserve(ControlKind::Task, task_bytes)?;
        let effect = self.request(&call);
        let digest = call.digest()?;
        let permit = grant.consume(cx, &effect, &digest)?;
        let output_limit = effective_output_limit(cx, &permit)?;
        let connection = self.native.try_lock_connection()?;
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "user-global connection has an active SQL transaction".into(),
            ));
        }
        let write = call.operation().is_write();
        let result = with_native_operation(
            &self.native,
            &connection,
            cx,
            permit.deadline(),
            permit.limits().fuel_per_call,
            write,
            CommitAdmission::consumed(&permit, &effect, &digest),
            || {
                with_internal_authorizer(&self.native, &connection, || {
                    execute_legacy_native(&connection, &self.name, call, output_limit)
                })
            },
        )?;
        check_native_completion(cx, permit.deadline(), write)?;
        Ok(result)
    }

    pub fn fixed_request(
        &self,
        request: &crate::FixedDataRequest,
    ) -> Result<(EffectRequest, Digest), StoreError> {
        Ok((
            EffectRequest::UserGlobalStorage {
                store: self.name.clone(),
                operation: request.operation().storage_operation(),
            },
            request.digest().clone(),
        ))
    }

    pub fn execute_fixed(
        &self,
        cx: &CallCx,
        request: crate::FixedDataRequest,
        grant: Grant,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<CoreValue, StoreError> {
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "user-global connection has an active SQL transaction".into(),
            ));
        }
        cx.check_active()?;
        self.native.check_owner_live()?;
        let operation = request.operation().storage_operation();
        let task_bytes = match request.operation() {
            crate::FixedDataOperation::Put | crate::FixedDataOperation::Get => {
                MAX_DATA_VALUE_BYTES as u64 + TASK_CONTROL_BYTES
            }
            crate::FixedDataOperation::List => 1024 * 1024 + TASK_CONTROL_BYTES,
            crate::FixedDataOperation::Delete => TASK_CONTROL_BYTES,
        };
        let _task = cx.control().reserve(ControlKind::Task, task_bytes)?;
        let request_effect = EffectRequest::UserGlobalStorage {
            store: self.name.clone(),
            operation,
        };
        let permit = grant.consume(cx, &request_effect, request.digest())?;
        let output_limit = effective_output_limit(cx, &permit)?;
        let connection = self.native.try_lock_connection()?;
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "user-global connection has an active SQL transaction".into(),
            ));
        }
        let output = with_native_operation(
            &self.native,
            &connection,
            cx,
            permit.deadline(),
            permit.limits().fuel_per_call,
            operation.is_write(),
            CommitAdmission::consumed(&permit, &request_effect, request.digest()),
            || {
                with_internal_authorizer(&self.native, &connection, || {
                    execute_fixed_native(
                        &connection,
                        &self.name,
                        &request,
                        output_limit,
                        &output_budget,
                    )
                })
            },
        )?;
        check_native_completion(cx, permit.deadline(), operation.is_write())?;
        Ok(output)
    }

    pub async fn sql_exec_typed(
        &self,
        cx: &CallCx,
        statement: crate::OwnedSqlStatement,
        result_policy: crate::SqlResultPolicy,
        gate: Arc<dyn Gate>,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<crate::OwnedSqlResult, StoreError> {
        let target = NativeSqlTarget::UserGlobal(self.clone());
        execute_store_sql(
            &target,
            cx,
            statement,
            Some(result_policy),
            gate,
            output_budget,
            false,
        )
        .await
    }

    pub async fn sql_query_typed(
        &self,
        cx: &CallCx,
        statement: crate::OwnedSqlStatement,
        gate: Arc<dyn Gate>,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<crate::OwnedSqlResult, StoreError> {
        let target = NativeSqlTarget::UserGlobal(self.clone());
        execute_store_sql(&target, cx, statement, None, gate, output_budget, false).await
    }

    pub async fn sql_begin(
        &self,
        cx: &CallCx,
        gate: Arc<dyn Gate>,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<Arc<NativeSqlTransaction>, StoreError> {
        begin_sql_transaction(
            NativeSqlTarget::UserGlobal(self.clone()),
            cx,
            gate,
            output_budget,
        )
        .await
    }
}

fn open_user_global_native(
    root: &DataRoot,
    cx: &CallCx,
    name: &str,
    path: &std::path::Path,
    disk_policy: crate::StoreDiskPolicy,
) -> Result<Arc<NativeDatabase>, StoreError> {
    let key = (root.physical_identity_key()?, name.to_owned());
    let globals = user_global_registry();
    let mut globals = globals
        .lock()
        .map_err(|_| StoreError::Conflict("user-global database registry poisoned".into()))?;
    globals.retain(|_, database| database.strong_count() > 0);
    if let Some(database) = globals.get(&key).and_then(Weak::upgrade) {
        if database.is_quarantined() {
            drop(globals);
            return Err(StoreError::Unavailable(
                "user-global DATA connection is quarantined".into(),
            ));
        }
        match database.check_owner_live() {
            Ok(()) => {
                root.validate_data_disk_policy(path, disk_policy)?;
                return Ok(database);
            }
            Err(error) => {
                drop(globals);
                return Err(error);
            }
        }
    }
    let registration = cx
        .control()
        .reserve(ControlKind::Registration, DATABASE_CONTROL_BYTES)?;
    preflight_data_database(root, path)?;
    let custody = crate::data_custody::DataCustody::registered_global(root, registration)?;
    let opened = root.data_database_with_disk_policy(path, true, disk_policy, custody)?;
    configure_data_connection(&opened.connection, root, path)?;
    let database = Arc::new(NativeDatabase {
        connection: Mutex::new(Some(opened.connection)),
        active_transaction: AtomicBool::new(false),
        quarantined: AtomicBool::new(false),
        _directory: Some(opened.directory),
        _custody: opened.custody,
        _global_identity: Some(key.clone()),
        process_id: std::process::id(),
    });
    globals.insert(key, Arc::downgrade(&database));
    Ok(database)
}

type UserGlobalIdentity = ((u64, u64), String);
fn user_global_registry() -> &'static Mutex<BTreeMap<UserGlobalIdentity, Weak<NativeDatabase>>> {
    static GLOBALS: OnceLock<Mutex<BTreeMap<UserGlobalIdentity, Weak<NativeDatabase>>>> =
        OnceLock::new();
    GLOBALS.get_or_init(|| Mutex::new(BTreeMap::new()))
}

#[derive(Clone)]
enum NativeSqlTarget {
    Profile(StoreHandle),
    UserGlobal(UserGlobalStore),
}
impl NativeSqlTarget {
    fn native(&self) -> &NativeDatabase {
        match self {
            Self::Profile(store) => &store.native,
            Self::UserGlobal(store) => &store.native,
        }
    }

    fn current_tier(
        &self,
        cx: &CallCx,
        operation: StoreOperation,
    ) -> Result<Option<StoreTier>, StoreError> {
        match self {
            Self::Profile(store) => Ok(Some(store.current_metadata(Some(cx), operation)?.tier)),
            Self::UserGlobal(store) => {
                cx.check_active()?;
                store.native.check_owner_live()?;
                Ok(None)
            }
        }
    }

    fn request(&self, tier: Option<StoreTier>, operation: StoreOperation) -> EffectRequest {
        match self {
            Self::Profile(store) => storage_request(
                store,
                tier.expect("profile store request always has a tier"),
                operation,
            ),
            Self::UserGlobal(store) => EffectRequest::UserGlobalStorage {
                store: store.name.clone(),
                operation,
            },
        }
    }

    fn operation_digest(&self, operation: &str) -> Result<Digest, StoreError> {
        match self {
            Self::Profile(store) => Ok(Digest::hash(&canonical(&(
                &store.owner,
                &store.name,
                operation,
            ))?)),
            Self::UserGlobal(store) => Ok(Digest::hash(&canonical(&(
                "user-global",
                &store.name,
                operation,
            ))?)),
        }
    }
}
/// A reusable database handle whose every operation consumes a fresh grant.
struct NativeDatabase {
    connection: Mutex<Option<rusqlite::Connection>>,
    active_transaction: AtomicBool,
    quarantined: AtomicBool,
    // Field order is intentional: close SQLite before releasing the VFS and
    // exact-owner descriptors/OS lease retained underneath it.
    _directory: Option<crate::vfs::DatabaseGuard>,
    _custody: Arc<crate::data_custody::DataCustody>,
    _global_identity: Option<UserGlobalIdentity>,
    process_id: u32,
}
struct NativeConnectionClaim<'a> {
    database: &'a NativeDatabase,
    guard: std::sync::MutexGuard<'a, Option<rusqlite::Connection>>,
}
impl Drop for NativeConnectionClaim<'_> {
    fn drop(&mut self) {
        if std::thread::panicking() {
            // A panic can bypass provisional rollback. Block aliases before
            // this exact connection claim releases its lock during unwinding.
            self.database.quarantine();
        }
    }
}
impl std::ops::Deref for NativeConnectionClaim<'_> {
    type Target = rusqlite::Connection;
    fn deref(&self) -> &Self::Target {
        self.guard
            .as_ref()
            .expect("a live connection claim cannot be empty")
    }
}
impl NativeDatabase {
    fn is_quarantined(&self) -> bool {
        self.quarantined.load(Ordering::Acquire)
            || self
                ._directory
                .as_ref()
                .is_some_and(|directory| directory.is_quarantined())
    }
    fn quarantine(&self) {
        self.quarantined.store(true, Ordering::Release);
        if let Some(directory) = &self._directory {
            directory.quarantine();
        }
    }
    fn commit_unknown(&self) -> StoreError {
        self.quarantine();
        StoreError::CommitUnknown
    }
    fn check_owner_live(&self) -> Result<(), StoreError> {
        if self.process_id != std::process::id() {
            return Err(StoreError::Unavailable(
                "inherited native DATA database handle".into(),
            ));
        }
        self._custody.check_owner_live()
    }
    fn try_lock_connection(&self) -> Result<NativeConnectionClaim<'_>, StoreError> {
        self.check_owner_live()?;
        if self.is_quarantined() {
            return Err(StoreError::Unavailable(
                "SQLite DATA connection is quarantined".into(),
            ));
        }
        let connection = self.claim_connection()?;
        if self.is_quarantined() {
            drop(connection);
            return Err(StoreError::Unavailable(
                "SQLite DATA connection is quarantined".into(),
            ));
        }
        self.check_owner_live()?;
        Ok(connection)
    }
    fn claim_connection(&self) -> Result<NativeConnectionClaim<'_>, StoreError> {
        let guard = self.connection.try_lock().map_err(|error| match error {
            std::sync::TryLockError::WouldBlock => {
                StoreError::Conflict("store connection is busy".into())
            }
            std::sync::TryLockError::Poisoned(_) => {
                self.quarantine();
                StoreError::Unavailable("SQLite DATA connection lock poisoned".into())
            }
        })?;
        if guard.is_none() {
            return Err(StoreError::Unavailable(
                "SQLite DATA connection is closed".into(),
            ));
        }
        Ok(NativeConnectionClaim {
            database: self,
            guard,
        })
    }
}
impl Drop for NativeDatabase {
    fn drop(&mut self) {
        let poisoned = self.connection.is_poisoned();
        let connection = self
            .connection
            .get_mut()
            .unwrap_or_else(|poison| poison.into_inner())
            .take();
        if let (Some(connection), Some(directory)) = (connection, self._directory.take()) {
            if poisoned || self.quarantined.load(Ordering::Acquire) || directory.is_quarantined() {
                directory.retain_connection(connection);
            } else if let Err((connection, _)) = connection.close() {
                directory.retain_connection(connection);
            }
        }
        let retired = if let Some(identity) = &self._global_identity
            && let Ok(mut registry) = user_global_registry().lock()
        {
            // A newer native connection may have replaced this dying weak
            // while the old connection was finishing its physical close.
            if registry
                .get(identity)
                .is_some_and(|current| current.strong_count() == 0)
            {
                registry.remove(identity)
            } else {
                None
            }
        } else {
            None
        };
        // Keep even weak/control teardown outside the registry guard.
        drop(retired);
    }
}
#[derive(Clone)]
pub struct StoreHandle {
    catalog: Arc<Catalog>,
    owner: PackId,
    name: String,
    path: PathBuf,
    native: Arc<NativeDatabase>,
}
impl StoreHandle {
    /// Bind the exact owned guest request before Gate authorization.
    pub fn fixed_request(
        &self,
        request: &crate::FixedDataRequest,
    ) -> Result<(EffectRequest, Digest), StoreError> {
        let meta = self.current_metadata(None, request.operation().storage_operation())?;
        Ok((
            EffectRequest::Storage {
                profile: self.catalog.profile.clone(),
                owner: self.owner.clone(),
                store: self.name.clone(),
                tier: meta.tier,
                operation: request.operation().storage_operation(),
            },
            request.digest().clone(),
        ))
    }

    /// Run the existing fixed KV ABI against this store's actual bounded native
    /// database. Its request and output graphs keep their real guest allocation
    /// owners; SQLite's internal cache is separately bounded by connection setup.
    pub fn execute_fixed(
        &self,
        cx: &CallCx,
        request: crate::FixedDataRequest,
        grant: Grant,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<CoreValue, StoreError> {
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "store connection has an active SQL transaction".into(),
            ));
        }
        let operation = request.operation().storage_operation();
        let meta = self.current_metadata(Some(cx), operation)?;
        let effect = EffectRequest::Storage {
            profile: self.catalog.profile.clone(),
            owner: self.owner.clone(),
            store: self.name.clone(),
            tier: meta.tier,
            operation,
        };
        let task_bytes = match request.operation() {
            crate::FixedDataOperation::Put => MAX_DATA_VALUE_BYTES as u64 + TASK_CONTROL_BYTES,
            crate::FixedDataOperation::Get => MAX_DATA_VALUE_BYTES as u64 + TASK_CONTROL_BYTES,
            crate::FixedDataOperation::List => 1024 * 1024 + TASK_CONTROL_BYTES,
            crate::FixedDataOperation::Delete => TASK_CONTROL_BYTES,
        };
        let _task = cx.control().reserve(ControlKind::Task, task_bytes)?;
        let permit = grant.consume(cx, &effect, request.digest())?;
        let output_limit = effective_output_limit(cx, &permit)?;
        self.native.check_owner_live()?;
        let connection = self.native.try_lock_connection()?;
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "store connection has an active SQL transaction".into(),
            ));
        }
        self.recheck_metadata(cx, operation, &meta)?;
        let output = with_native_operation(
            &self.native,
            &connection,
            cx,
            permit.deadline(),
            permit.limits().fuel_per_call,
            operation.is_write(),
            CommitAdmission::consumed(&permit, &effect, request.digest()),
            || {
                with_internal_authorizer(&self.native, &connection, || {
                    execute_fixed_native(
                        &connection,
                        &self.name,
                        &request,
                        output_limit,
                        &output_budget,
                    )
                })
            },
        )?;
        check_native_completion(cx, permit.deadline(), operation.is_write())?;
        Ok(output)
    }

    pub async fn sql_exec_typed(
        &self,
        cx: &CallCx,
        statement: crate::OwnedSqlStatement,
        result_policy: crate::SqlResultPolicy,
        gate: Arc<dyn Gate>,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<crate::OwnedSqlResult, StoreError> {
        let target = NativeSqlTarget::Profile(self.clone());
        execute_store_sql(
            &target,
            cx,
            statement,
            Some(result_policy),
            gate,
            output_budget,
            false,
        )
        .await
    }

    pub async fn sql_query_typed(
        &self,
        cx: &CallCx,
        statement: crate::OwnedSqlStatement,
        gate: Arc<dyn Gate>,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<crate::OwnedSqlResult, StoreError> {
        let target = NativeSqlTarget::Profile(self.clone());
        execute_store_sql(&target, cx, statement, None, gate, output_budget, false).await
    }

    pub async fn sql_begin(
        &self,
        cx: &CallCx,
        gate: Arc<dyn Gate>,
        _output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<Arc<NativeSqlTransaction>, StoreError> {
        begin_sql_transaction(
            NativeSqlTarget::Profile(self.clone()),
            cx,
            gate,
            _output_budget,
        )
        .await
    }

    fn current_metadata(
        &self,
        cx: Option<&CallCx>,
        operation: StoreOperation,
    ) -> Result<StoreDescriptor, StoreError> {
        let conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let meta = descriptor(&conn, &self.owner, &self.name)?;
        crate::data_moves::check(
            &conn,
            &self.catalog.profile,
            &self.owner,
            &self.name,
            meta.tier,
        )?;
        if meta.orphaned {
            return Err(StoreError::Denied("store is orphaned".into()));
        }
        let path: String = conn.query_row(
            "SELECT path FROM stores WHERE owner=?1 AND name=?2",
            params![self.owner.as_str(), self.name],
            |r| r.get(0),
        )?;
        if std::path::Path::new(&path) != self.path {
            return Err(StoreError::Denied("store path changed".into()));
        }
        if let Some(cx) = cx {
            cx.check_active()?;
            local_access(cx, &self.catalog.profile, &meta, operation.is_write())?;
        }
        self.native.check_owner_live()?;
        Ok(meta)
    }

    fn recheck_metadata(
        &self,
        cx: &CallCx,
        operation: StoreOperation,
        admitted: &StoreDescriptor,
    ) -> Result<(), StoreError> {
        if self.current_metadata(Some(cx), operation)? != *admitted {
            return Err(StoreError::Conflict(
                "store metadata changed during admission".into(),
            ));
        }
        Ok(())
    }

    pub fn request(&self, call: &StoreCall) -> Result<EffectRequest, StoreError> {
        let conn = self
            .catalog
            .connection
            .lock()
            .map_err(|_| StoreError::Conflict("catalog lock poisoned".into()))?;
        let meta = descriptor(&conn, &self.owner, &self.name)?;
        Ok(EffectRequest::Storage {
            profile: self.catalog.profile.clone(),
            owner: self.owner.clone(),
            store: self.name.clone(),
            tier: meta.tier,
            operation: call.operation(),
        })
    }
    pub fn execute(
        &self,
        cx: &CallCx,
        call: StoreCall,
        grant: Grant,
    ) -> Result<StoreResult, StoreError> {
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "store connection has an active SQL transaction".into(),
            ));
        }
        let meta = self.current_metadata(Some(cx), call.operation())?;
        let req = EffectRequest::Storage {
            profile: self.catalog.profile.clone(),
            owner: self.owner.clone(),
            store: self.name.clone(),
            tier: meta.tier,
            operation: call.operation(),
        };
        let task_bytes = match &call {
            StoreCall::KvPut { value, .. } => value.len() as u64 + TASK_CONTROL_BYTES,
            StoreCall::KvGet { .. } => MAX_DATA_VALUE_BYTES as u64 + TASK_CONTROL_BYTES,
            StoreCall::KvList { .. } => 1024 * 1024 + TASK_CONTROL_BYTES,
            StoreCall::KvDelete { .. } | StoreCall::SqlExec { .. } | StoreCall::SqlQuery { .. } => {
                TASK_CONTROL_BYTES
            }
        };
        let _task = cx.control().reserve(ControlKind::Task, task_bytes)?;
        let digest = call.digest()?;
        let permit = grant.consume(cx, &req, &digest)?;
        let output_limit = effective_output_limit(cx, &permit)?;
        let connection = self.native.try_lock_connection()?;
        if self.native.active_transaction.load(Ordering::Acquire) {
            return Err(StoreError::Conflict(
                "store connection has an active SQL transaction".into(),
            ));
        }
        self.recheck_metadata(cx, call.operation(), &meta)?;
        let write = call.operation().is_write();
        let result = with_native_operation(
            &self.native,
            &connection,
            cx,
            permit.deadline(),
            permit.limits().fuel_per_call,
            write,
            CommitAdmission::consumed(&permit, &req, &digest),
            || {
                with_internal_authorizer(&self.native, &connection, || {
                    execute_legacy_native(&connection, &self.name, call, output_limit)
                })
            },
        )?;
        check_native_completion(cx, permit.deadline(), write)?;
        Ok(result)
    }
}

/// A transaction pins the actual native connection and its owner lease. Each
/// guest SQL call receives a fresh Gate and allocation budget; no caller's
/// frame budget is retained for later results.
pub struct NativeSqlTransaction {
    target: NativeSqlTarget,
    profile: ProfileId,
    principal: tobkiri_gate::Principal,
    security_epoch: u64,
    revision: Option<Digest>,
    deadline: std::time::Instant,
    state: Mutex<crate::SqlTransactionState>,
    in_flight: AtomicBool,
    _registration: ControlReservation,
}
impl NativeSqlTransaction {
    pub fn state(&self) -> crate::SqlTransactionState {
        self.state
            .lock()
            .map(|state| *state)
            .unwrap_or(crate::SqlTransactionState::RollbackOnly)
    }

    pub async fn exec_typed(
        &self,
        cx: &CallCx,
        statement: crate::OwnedSqlStatement,
        result_policy: crate::SqlResultPolicy,
        gate: Arc<dyn Gate>,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<crate::OwnedSqlResult, StoreError> {
        self.check_context(cx)?;
        let mut operation = self.enter()?;
        let result = execute_store_sql(
            &self.target,
            cx,
            statement,
            Some(result_policy),
            gate,
            output_budget,
            true,
        )
        .await;
        operation.finish(result.is_ok(), None);
        result
    }

    pub async fn query_typed(
        &self,
        cx: &CallCx,
        statement: crate::OwnedSqlStatement,
        gate: Arc<dyn Gate>,
        output_budget: Arc<dyn AllocationBudget>,
    ) -> Result<crate::OwnedSqlResult, StoreError> {
        self.check_context(cx)?;
        let mut operation = self.enter()?;
        let result =
            execute_store_sql(&self.target, cx, statement, None, gate, output_budget, true).await;
        operation.finish(result.is_ok(), None);
        result
    }

    pub async fn commit(&self, cx: &CallCx, gate: Arc<dyn Gate>) -> Result<(), StoreError> {
        self.check_context(cx)?;
        match self.state() {
            crate::SqlTransactionState::Committed => return Ok(()),
            crate::SqlTransactionState::RolledBack => {
                return Err(StoreError::Conflict(
                    "transaction was already rolled back".into(),
                ));
            }
            crate::SqlTransactionState::CommitUnknown => {
                return Err(StoreError::CommitUnknown);
            }
            crate::SqlTransactionState::RollbackOnly => {
                return Err(StoreError::Conflict("transaction is rollback-only".into()));
            }
            crate::SqlTransactionState::Active => (),
        }
        let mut operation = self.enter()?;
        let tier = self.target.current_tier(cx, StoreOperation::SqlWrite)?;
        let request = self.target.request(tier, StoreOperation::SqlWrite);
        let digest = self.target.operation_digest("sql-commit")?;
        let grant = gate.authorize(cx, request.clone(), digest.clone()).await?;
        let permit = grant.consume(cx, &request, &digest)?;
        let grant_id = permit.id().clone();
        if let Err(error) = gate.watch_lease(cx, &permit) {
            let _ = gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "storage-lease-watch-failed".into(),
                    },
                )
                .await;
            operation.finish(false, None);
            return Err(error.into());
        }
        let rechecked = self.target.current_tier(cx, StoreOperation::SqlWrite);
        if !matches!(&rechecked, Ok(current) if *current == tier) {
            let error = match rechecked {
                Ok(_) => {
                    StoreError::Conflict("store tier changed during SQL commit admission".into())
                }
                Err(error) => error,
            };
            let _ = gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "storage-sql-commit-admission-changed".into(),
                    },
                )
                .await;
            operation.finish(false, None);
            return Err(error);
        }
        let (result, commit_started, commit_succeeded) = {
            match self.target.native().try_lock_connection() {
                Err(error) => (Err(error), false, false),
                Ok(connection) => {
                    let mut commit_started = false;
                    let mut commit_succeeded = false;
                    let mut fence_entered = false;
                    let result = CommitAdmission::consumed(&permit, &request, &digest).finish(
                        self.target.native(),
                        &connection,
                        || {
                            fence_entered = true;
                            with_progress_limit(
                                self.target.native(),
                                &connection,
                                cx,
                                permit.deadline(),
                                permit.limits().fuel_per_call,
                                || {
                                    commit_started = true;
                                    with_internal_authorizer(
                                        self.target.native(),
                                        &connection,
                                        || {
                                            connection.execute_batch("COMMIT;")?;
                                            commit_succeeded = connection.is_autocommit();
                                            if !commit_succeeded {
                                                return Err(self.target.native().commit_unknown());
                                            }
                                            Ok(())
                                        },
                                    )
                                },
                            )
                        },
                    );
                    if !fence_entered {
                        let error = result.err().unwrap_or_else(|| {
                            StoreError::Unavailable("storage fence skipped commit".into())
                        });
                        let rollback =
                            rollback_native_transaction(self.target.native(), &connection);
                        if rollback.is_ok() {
                            self.target
                                .native()
                                .active_transaction
                                .store(false, Ordering::Release);
                            operation.finish(false, Some(crate::SqlTransactionState::RolledBack));
                        } else {
                            operation.finish(false, None);
                        }
                        drop(connection);
                        let _ = gate
                            .record_outcome(
                                grant_id,
                                Outcome::Failed {
                                    code: "storage-sql-commit-fence-refused".into(),
                                },
                            )
                            .await;
                        return Err(rollback.err().unwrap_or(error));
                    }
                    (result, commit_started, commit_succeeded)
                }
            }
        };
        if let Err(error) = result {
            if !commit_started {
                let _ = gate
                    .record_outcome(
                        grant_id,
                        Outcome::Failed {
                            code: "storage-sql-commit-not-started".into(),
                        },
                    )
                    .await;
                operation.finish(false, None);
                return Err(error);
            }
            operation.finish(false, Some(crate::SqlTransactionState::CommitUnknown));
            let _ = gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "storage-sql-commit-unknown".into(),
                    },
                )
                .await;
            return Err(if commit_succeeded {
                self.target
                    .native()
                    .active_transaction
                    .store(false, Ordering::Release);
                StoreError::CommitUnknown
            } else {
                self.target.native().commit_unknown()
            });
        }
        if gate
            .record_outcome(grant_id, Outcome::Succeeded)
            .await
            .is_err()
            || check_native_completion(cx, permit.deadline(), true).is_err()
        {
            self.target
                .native()
                .active_transaction
                .store(false, Ordering::Release);
            operation.finish(false, Some(crate::SqlTransactionState::CommitUnknown));
            return Err(StoreError::CommitUnknown);
        }
        self.target
            .native()
            .active_transaction
            .store(false, Ordering::Release);
        operation.finish(true, Some(crate::SqlTransactionState::Committed));
        Ok(())
    }

    /// Rollback is cleanup, so it needs a live same-owner call context but no
    /// fresh write prompt. Drop also attempts this exact non-replaying cleanup.
    pub async fn rollback(&self, cx: &CallCx) -> Result<(), StoreError> {
        self.check_context(cx)?;
        match self.state() {
            crate::SqlTransactionState::RolledBack => return Ok(()),
            crate::SqlTransactionState::Committed => {
                return Err(StoreError::Conflict(
                    "committed transaction cannot be rolled back".into(),
                ));
            }
            crate::SqlTransactionState::CommitUnknown => {
                return Err(StoreError::CommitUnknown);
            }
            crate::SqlTransactionState::Active | crate::SqlTransactionState::RollbackOnly => (),
        }
        let mut operation = self.enter_rollback()?;
        cx.check_active()?;
        self.target.native().check_owner_live()?;
        let result = (|| {
            let connection = self.target.native().try_lock_connection()?;
            with_progress(self.target.native(), &connection, cx, cx.deadline(), || {
                rollback_native_transaction(self.target.native(), &connection)
            })
        })();
        match result {
            Ok(()) => {
                self.target
                    .native()
                    .active_transaction
                    .store(false, Ordering::Release);
                operation.finish(true, Some(crate::SqlTransactionState::RolledBack));
                Ok(())
            }
            Err(error) => {
                operation.finish(false, Some(crate::SqlTransactionState::RollbackOnly));
                Err(error)
            }
        }
    }

    fn enter(&self) -> Result<TransactionOperation<'_>, StoreError> {
        if self.state() != crate::SqlTransactionState::Active {
            return Err(StoreError::Conflict("transaction is not active".into()));
        }
        self.enter_exclusive()
    }

    fn check_context(&self, cx: &CallCx) -> Result<(), StoreError> {
        if cx.profile() != &self.profile
            || cx.principal() != &self.principal
            || cx.security_epoch() != self.security_epoch
            || cx.revision() != self.revision.as_ref()
            || cx.deadline() > self.deadline
        {
            return Err(StoreError::Denied(
                "SQL transaction belongs to another profile, principal, revision, or epoch".into(),
            ));
        }
        cx.check_active()?;
        Ok(())
    }

    fn enter_rollback(&self) -> Result<TransactionOperation<'_>, StoreError> {
        if !matches!(
            self.state(),
            crate::SqlTransactionState::Active | crate::SqlTransactionState::RollbackOnly
        ) {
            return Err(StoreError::Conflict(
                "transaction cannot be rolled back".into(),
            ));
        }
        self.enter_exclusive()
    }

    fn enter_exclusive(&self) -> Result<TransactionOperation<'_>, StoreError> {
        self.in_flight
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| StoreError::Conflict("transaction operation is in flight".into()))?;
        Ok(TransactionOperation {
            state: &self.state,
            in_flight: &self.in_flight,
            finished: false,
        })
    }
}
impl Drop for NativeSqlTransaction {
    fn drop(&mut self) {
        let state = match self.state.get_mut() {
            Ok(state) => state,
            Err(poisoned) => poisoned.into_inner(),
        };
        if !matches!(
            *state,
            crate::SqlTransactionState::Active | crate::SqlTransactionState::RollbackOnly
        ) {
            return;
        }
        if self.target.native().check_owner_live().is_err() {
            return;
        }
        if let Ok(connection) = self.target.native().claim_connection() {
            if rollback_native_transaction(self.target.native(), &connection).is_ok() {
                self.target
                    .native()
                    .active_transaction
                    .store(false, Ordering::Release);
                *state = crate::SqlTransactionState::RolledBack;
            }
        } else {
            self.target
                .native()
                .quarantined
                .store(true, Ordering::Release);
        }
    }
}

struct BeginSlot<'a> {
    active: &'a AtomicBool,
    committed: bool,
}
impl Drop for BeginSlot<'_> {
    fn drop(&mut self) {
        if !self.committed {
            self.active.store(false, Ordering::Release);
        }
    }
}

struct TransactionOperation<'a> {
    state: &'a Mutex<crate::SqlTransactionState>,
    in_flight: &'a AtomicBool,
    finished: bool,
}
impl TransactionOperation<'_> {
    fn finish(&mut self, success: bool, terminal: Option<crate::SqlTransactionState>) {
        if let Ok(mut state) = self.state.lock() {
            if let Some(terminal) = terminal {
                *state = terminal;
            } else if !success && *state == crate::SqlTransactionState::Active {
                *state = crate::SqlTransactionState::RollbackOnly;
            }
        }
        self.finished = true;
        self.in_flight.store(false, Ordering::Release);
    }
}
impl Drop for TransactionOperation<'_> {
    fn drop(&mut self) {
        if !self.finished {
            if let Ok(mut state) = self.state.lock()
                && *state == crate::SqlTransactionState::Active
            {
                *state = crate::SqlTransactionState::RollbackOnly;
            }
            self.in_flight.store(false, Ordering::Release);
        }
    }
}

async fn begin_sql_transaction(
    target: NativeSqlTarget,
    cx: &CallCx,
    gate: Arc<dyn Gate>,
    _output_budget: Arc<dyn AllocationBudget>,
) -> Result<Arc<NativeSqlTransaction>, StoreError> {
    let _task = cx
        .control()
        .reserve(ControlKind::Task, TASK_CONTROL_BYTES)?;
    let registration = cx
        .control()
        .reserve(ControlKind::Registration, TASK_CONTROL_BYTES)?;
    let tier = target.current_tier(cx, StoreOperation::SqlWrite)?;
    target
        .native()
        .active_transaction
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .map_err(|_| StoreError::Conflict("store already has a transaction".into()))?;
    let mut begin_slot = BeginSlot {
        active: &target.native().active_transaction,
        committed: false,
    };
    let request = target.request(tier, StoreOperation::SqlWrite);
    let digest = target.operation_digest("sql-begin-deferred")?;
    let grant = gate.authorize(cx, request.clone(), digest.clone()).await?;
    let permit = grant.consume(cx, &request, &digest)?;
    let grant_id = permit.id().clone();
    if let Err(error) = gate.watch_lease(cx, &permit) {
        let _ = gate
            .record_outcome(
                grant_id,
                Outcome::Failed {
                    code: "storage-lease-watch-failed".into(),
                },
            )
            .await;
        return Err(error.into());
    }
    match target.current_tier(cx, StoreOperation::SqlWrite) {
        Ok(current_tier) if current_tier == tier => (),
        current => {
            let _ = gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "storage-sql-admission-changed".into(),
                    },
                )
                .await;
            return match current {
                Ok(_) => Err(StoreError::Conflict(
                    "store tier changed during SQL admission".into(),
                )),
                Err(error) => Err(error),
            };
        }
    }
    let begin = (|| {
        let connection = target.native().try_lock_connection()?;
        let result = with_progress_limit(
            target.native(),
            &connection,
            cx,
            permit.deadline(),
            permit.limits().fuel_per_call,
            || {
                with_internal_authorizer(target.native(), &connection, || {
                    connection.execute_batch("BEGIN DEFERRED;")?;
                    Ok(())
                })
            },
        );
        if result.is_err()
            && let Err(error) = rollback_native_transaction(target.native(), &connection)
        {
            // The BEGIN may have succeeded before its activity or handler
            // cleanup failed. Keep this occupied until cleanup is proven.
            begin_slot.committed = true;
            return Err(error);
        }
        result
    })();
    if let Err(error) = begin {
        let _ = gate
            .record_outcome(
                grant_id,
                Outcome::Failed {
                    code: "storage-sql-begin-failed".into(),
                },
            )
            .await;
        return Err(error);
    }
    begin_slot.committed = true;
    drop(begin_slot);
    let transaction = Arc::new(NativeSqlTransaction {
        target,
        profile: cx.profile().clone(),
        principal: cx.principal().clone(),
        security_epoch: cx.security_epoch(),
        revision: cx.revision().cloned(),
        deadline: cx.deadline(),
        state: Mutex::new(crate::SqlTransactionState::Active),
        in_flight: AtomicBool::new(false),
        _registration: registration,
    });
    if let Err(error) = gate.record_outcome(grant_id, Outcome::Succeeded).await {
        drop(transaction);
        return Err(error.into());
    }
    check_native_completion(cx, permit.deadline(), false)?;
    Ok(transaction)
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct SqlFingerprint {
    readonly: bool,
    parameter_count: usize,
    columns: Vec<String>,
}

async fn execute_store_sql(
    target: &NativeSqlTarget,
    cx: &CallCx,
    statement: crate::OwnedSqlStatement,
    policy: Option<crate::SqlResultPolicy>,
    gate: Arc<dyn Gate>,
    output_budget: Arc<dyn AllocationBudget>,
    in_transaction: bool,
) -> Result<crate::OwnedSqlResult, StoreError> {
    cx.check_active()?;
    if target.native().active_transaction.load(Ordering::Acquire) != in_transaction {
        return Err(StoreError::Conflict(if in_transaction {
            "transaction is no longer active".into()
        } else {
            "store connection has an active SQL transaction".into()
        }));
    }
    if statement.statement().len() > 64 * 1024 || statement.max_rows() > MAX_LIST_ROWS {
        return Err(StoreError::Quota);
    }
    if statement
        .statement()
        .trim()
        .trim_end_matches(';')
        .trim()
        .is_empty()
    {
        return Err(StoreError::Invalid("empty SQL statement".into()));
    }
    let digest = statement
        .value()
        .digest_guest(&output_budget)
        .map_err(|_| StoreError::Quota)?;
    let parameter_bytes = measure_sql_params(&statement)?;
    let parameter_count = statement
        .params()
        .array()
        .ok_or_else(|| StoreError::Invalid("SQL positional parameters required".into()))?
        .len();
    let parameter_copy_bytes = parameter_bytes
        .checked_add(
            parameter_count
                .checked_mul(std::mem::size_of::<Value>())
                .ok_or(StoreError::Quota)?,
        )
        .ok_or(StoreError::Quota)?;
    let parameter_copy_bytes_u64 =
        u64::try_from(parameter_copy_bytes).map_err(|_| StoreError::Quota)?;
    let parameter_count_u64 = u64::try_from(parameter_count).map_err(|_| StoreError::Quota)?;
    let parameter_copy_work = parameter_copy_bytes_u64
        .checked_add(parameter_count_u64)
        .ok_or(StoreError::Quota)?;
    output_budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: parameter_copy_bytes_u64,
            elements: parameter_count_u64,
            depth: 1,
            work: parameter_copy_work,
        })
        .map_err(|_| StoreError::Quota)?;
    let _parameter_copy_reservation = SqlParameterReservation {
        budget: output_budget.clone(),
        bytes: parameter_copy_bytes_u64,
    };
    let input_bytes = statement
        .statement()
        .len()
        .checked_add(parameter_bytes)
        .and_then(|bytes| {
            bytes.checked_add(
                statement
                    .params()
                    .array()
                    .map_or(0, |params| params.len() * std::mem::size_of::<Value>()),
            )
        })
        .ok_or(StoreError::Quota)?;
    let task_bytes = u64::try_from(input_bytes)
        .ok()
        .and_then(|input| input.checked_add(64 * 1024))
        .and_then(|input| input.checked_add(cx.limits().output_bytes.min(4 * 1024 * 1024)))
        .ok_or(StoreError::Quota)?;
    let _task = cx.control().reserve(ControlKind::Task, task_bytes)?;
    let params = owned_sql_params(&statement)?;

    let admission_tier = target.current_tier(cx, StoreOperation::SqlRead)?;
    let read_request = target.request(admission_tier, StoreOperation::SqlRead);
    // The read authority is always resolved before SQLite prepares guest text.
    let read_grant = gate
        .authorize(cx, read_request.clone(), digest.clone())
        .await?;
    if target.native().active_transaction.load(Ordering::Acquire) != in_transaction {
        return Err(StoreError::Conflict(
            "store transaction state changed during SQL admission".into(),
        ));
    }
    let first_fingerprint =
        classify_typed_sql(target, cx, &statement, &params, policy, in_transaction)?;
    let operation = if first_fingerprint.readonly {
        StoreOperation::SqlRead
    } else {
        StoreOperation::SqlWrite
    };
    let (request, grant) = if operation == StoreOperation::SqlRead {
        (read_request, read_grant)
    } else {
        drop(read_grant);
        let tier = target.current_tier(cx, StoreOperation::SqlWrite)?;
        let request = target.request(tier, StoreOperation::SqlWrite);
        let grant = gate.authorize(cx, request.clone(), digest.clone()).await?;
        (request, grant)
    };
    let current = target.current_tier(cx, operation)?;
    if current != admission_tier {
        return Err(StoreError::Conflict(
            "store tier changed during SQL admission".into(),
        ));
    }
    let permit = grant.consume(cx, &request, &digest)?;
    let grant_id = permit.id().clone();
    if let Err(error) = gate.watch_lease(cx, &permit) {
        let _ = gate
            .record_outcome(
                grant_id,
                Outcome::Failed {
                    code: "storage-lease-watch-failed".into(),
                },
            )
            .await;
        return Err(error.into());
    }
    let output_limit = match effective_output_limit(cx, &permit) {
        Ok(limit) => limit,
        Err(error) => {
            let _ = gate
                .record_outcome(
                    grant_id,
                    Outcome::Failed {
                        code: "storage-output-limit-failed".into(),
                    },
                )
                .await;
            return Err(error);
        }
    };
    let result = execute_authorized_typed_sql(
        target,
        TypedSqlExecution {
            cx,
            statement: &statement,
            params: &params,
            expected_fingerprint: &first_fingerprint,
            admitted_tier: admission_tier,
            policy,
            max_rows: statement.max_rows(),
            output_limit,
            output_budget: &output_budget,
            deadline: permit.deadline(),
            fuel_limit: permit.limits().fuel_per_call,
            admission: CommitAdmission::consumed(&permit, &request, &digest),
            in_transaction,
        },
    );
    match result {
        Ok(mut result) => {
            let committed_write = !first_fingerprint.readonly && !in_transaction;
            if let Err(error) = gate.record_outcome(grant_id, Outcome::Succeeded).await {
                return Err(if committed_write {
                    StoreError::CommitUnknown
                } else {
                    error.into()
                });
            }
            check_native_completion(cx, permit.deadline(), committed_write)?;
            if committed_write {
                result.mark_confirmed_write_commit();
            }
            Ok(result)
        }
        Err(error) => {
            let code = if matches!(error, StoreError::CommitUnknown) {
                "storage-sql-commit-unknown"
            } else {
                "storage-sql-failed"
            };
            let _ = gate
                .record_outcome(grant_id, Outcome::Failed { code: code.into() })
                .await;
            Err(error)
        }
    }
}

fn storage_request(
    store: &StoreHandle,
    tier: StoreTier,
    operation: StoreOperation,
) -> EffectRequest {
    EffectRequest::Storage {
        profile: store.catalog.profile.clone(),
        owner: store.owner.clone(),
        store: store.name.clone(),
        tier,
        operation,
    }
}

fn prepare_typed<'a>(
    connection: &'a rusqlite::Connection,
    sql: &str,
    params: &[Value],
) -> Result<rusqlite::Statement<'a>, StoreError> {
    let mut prepared = connection.prepare(sql)?;
    if prepared.parameter_count() != params.len() {
        return Err(StoreError::Invalid(
            "SQL positional parameter count does not match".into(),
        ));
    }
    for (index, value) in params.iter().enumerate() {
        prepared.raw_bind_parameter(index + 1, value)?;
    }
    Ok(prepared)
}

fn sql_fingerprint(statement: &rusqlite::Statement<'_>) -> Result<SqlFingerprint, StoreError> {
    let columns = (0..statement.column_count())
        .map(|index| statement.column_name(index).map(str::to_owned))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(SqlFingerprint {
        readonly: statement.readonly(),
        parameter_count: statement.parameter_count(),
        columns,
    })
}

fn validate_sql_shape(
    fingerprint: &SqlFingerprint,
    policy: Option<crate::SqlResultPolicy>,
) -> Result<(), StoreError> {
    if fingerprint.columns.len() > 256 {
        return Err(StoreError::Quota);
    }
    match policy {
        None if !fingerprint.readonly || fingerprint.columns.is_empty() => Err(StoreError::Denied(
            "SQL query must be a read-only statement with result columns".into(),
        )),
        Some(crate::SqlResultPolicy::ForbidRows) if !fingerprint.columns.is_empty() => Err(
            StoreError::Denied("SQL exec policy forbids result columns".into()),
        ),
        _ => Ok(()),
    }
}

fn classify_typed_sql(
    target: &NativeSqlTarget,
    cx: &CallCx,
    statement: &crate::OwnedSqlStatement,
    params: &[Value],
    policy: Option<crate::SqlResultPolicy>,
    in_transaction: bool,
) -> Result<SqlFingerprint, StoreError> {
    let database = target.native();
    if database.active_transaction.load(Ordering::Acquire) != in_transaction {
        return Err(StoreError::Conflict(
            "store transaction state changed during SQL preparation".into(),
        ));
    }
    let connection = database.try_lock_connection()?;
    cx.check_active()?;
    let prepared = prepare_typed(&connection, statement.statement(), params)?;
    let fingerprint = sql_fingerprint(&prepared)?;
    validate_sql_shape(&fingerprint, policy)?;
    cx.check_active()?;
    Ok(fingerprint)
}

/// Borrowed inputs and copied bounds for one already-consumed SQL operation.
/// This creates no new authority or funding owner and retains no connection
/// guard across the caller's Gate awaits.
struct TypedSqlExecution<'a> {
    cx: &'a CallCx,
    statement: &'a crate::OwnedSqlStatement,
    params: &'a [Value],
    expected_fingerprint: &'a SqlFingerprint,
    admitted_tier: Option<StoreTier>,
    policy: Option<crate::SqlResultPolicy>,
    max_rows: u32,
    output_limit: usize,
    output_budget: &'a Arc<dyn AllocationBudget>,
    deadline: std::time::Instant,
    fuel_limit: u64,
    admission: CommitAdmission<'a>,
    in_transaction: bool,
}

fn execute_authorized_typed_sql(
    target: &NativeSqlTarget,
    execution: TypedSqlExecution<'_>,
) -> Result<crate::OwnedSqlResult, StoreError> {
    let TypedSqlExecution {
        cx,
        statement,
        params,
        expected_fingerprint,
        admitted_tier,
        policy,
        max_rows,
        output_limit,
        output_budget,
        deadline,
        fuel_limit,
        admission,
        in_transaction,
    } = execution;
    let database = target.native();
    if database.active_transaction.load(Ordering::Acquire) != in_transaction {
        return Err(StoreError::Conflict(
            "store transaction state changed before SQL execution".into(),
        ));
    }
    let connection = database.try_lock_connection()?;
    cx.check_active()?;
    let operation = if expected_fingerprint.readonly {
        StoreOperation::SqlRead
    } else {
        StoreOperation::SqlWrite
    };
    if target.current_tier(cx, operation)? != admitted_tier {
        return Err(StoreError::Conflict(
            "store tier changed after SQL grant consumption".into(),
        ));
    }
    let result = {
        let mut prepared = prepare_typed(&connection, statement.statement(), params)?;
        let fingerprint = sql_fingerprint(&prepared)?;
        validate_sql_shape(&fingerprint, policy)?;
        if &fingerprint != expected_fingerprint {
            return Err(StoreError::Conflict(
                "SQLite statement classification changed during authorization".into(),
            ));
        }
        with_native_operation(
            database,
            &connection,
            cx,
            deadline,
            fuel_limit,
            !fingerprint.readonly && !in_transaction,
            admission,
            || {
                step_typed(
                    &connection,
                    &mut prepared,
                    &fingerprint,
                    policy,
                    max_rows,
                    output_limit,
                    output_budget,
                )
            },
        )
    };
    drop(connection);
    result
}

fn measure_sql_params(statement: &crate::OwnedSqlStatement) -> Result<usize, StoreError> {
    let CoreRef::Arr(params) = statement.params().inspect() else {
        return Err(StoreError::Invalid(
            "SQL positional parameters required".into(),
        ));
    };
    if params.len() > 256 {
        return Err(StoreError::Quota);
    }
    let mut bytes = 0usize;
    for cell in params.iter() {
        let CoreRef::Obj(fields) = cell.inspect() else {
            return Err(StoreError::Invalid("SQL cell must be tagged".into()));
        };
        let kind_value = fields
            .get("type")
            .ok_or_else(|| StoreError::Invalid("SQL cell type missing".into()))?;
        let CoreRef::Str(kind) = kind_value.inspect() else {
            return Err(StoreError::Invalid("SQL cell type must be text".into()));
        };
        let value = fields
            .get("value")
            .ok_or_else(|| StoreError::Invalid("SQL cell value missing".into()))?;
        match (kind, value.inspect()) {
            ("null", CoreRef::Null) => (),
            ("integer", CoreRef::Str(value)) => {
                crate::SqlInteger::parse(value)?;
            }
            ("real", CoreRef::Str(value)) => {
                crate::SqlReal::parse(value)?;
            }
            ("text", CoreRef::Str(value)) => {
                bytes = bytes.checked_add(value.len()).ok_or(StoreError::Quota)?;
            }
            ("blob", CoreRef::Arr(value)) => {
                for byte in value.iter() {
                    if !matches!(byte.inspect(), CoreRef::Int(0..=255)) {
                        return Err(StoreError::Invalid("SQL blob byte out of range".into()));
                    }
                }
                bytes = bytes.checked_add(value.len()).ok_or(StoreError::Quota)?;
            }
            _ => return Err(StoreError::Invalid("SQL cell type/value mismatch".into())),
        }
        if bytes > MAX_DATA_VALUE_BYTES {
            return Err(StoreError::Quota);
        }
    }
    Ok(bytes)
}

fn owned_sql_params(statement: &crate::OwnedSqlStatement) -> Result<Vec<Value>, StoreError> {
    let CoreRef::Arr(params) = statement.params().inspect() else {
        return Err(StoreError::Invalid(
            "SQL positional parameters required".into(),
        ));
    };
    if params.len() > 256 {
        return Err(StoreError::Quota);
    }
    let mut values = Vec::with_capacity(params.len());
    let mut bytes = 0usize;
    for cell in params.iter() {
        let CoreRef::Obj(fields) = cell.inspect() else {
            return Err(StoreError::Invalid("SQL cell must be tagged".into()));
        };
        let kind = core_text(fields.get("type"), "SQL cell type missing")?;
        let value = fields
            .get("value")
            .ok_or_else(|| StoreError::Invalid("SQL cell value missing".into()))?;
        let value = match (kind.as_str(), value.inspect()) {
            ("null", CoreRef::Null) => Value::Null,
            ("integer", CoreRef::Str(value)) => Value::Integer(
                crate::SqlInteger::parse(value)?
                    .as_str()
                    .parse()
                    .map_err(|_| StoreError::Invalid("invalid SQL integer".into()))?,
            ),
            ("real", CoreRef::Str(value)) => Value::Real(
                crate::SqlReal::parse(value)?
                    .as_str()
                    .parse()
                    .map_err(|_| StoreError::Invalid("invalid SQL real".into()))?,
            ),
            ("text", CoreRef::Str(value)) => {
                bytes = bytes.checked_add(value.len()).ok_or(StoreError::Quota)?;
                Value::Text(value.to_owned())
            }
            ("blob", CoreRef::Arr(value)) => {
                if value.len() > MAX_DATA_VALUE_BYTES {
                    return Err(StoreError::Quota);
                }
                bytes = bytes.checked_add(value.len()).ok_or(StoreError::Quota)?;
                let mut blob = Vec::with_capacity(value.len());
                for byte in value.iter() {
                    match byte.inspect() {
                        CoreRef::Int(value) => blob.push(u8::try_from(value).map_err(|_| {
                            StoreError::Invalid("SQL blob byte out of range".into())
                        })?),
                        _ => {
                            return Err(StoreError::Invalid(
                                "SQL blob byte must be integer".into(),
                            ));
                        }
                    }
                }
                Value::Blob(blob)
            }
            _ => return Err(StoreError::Invalid("SQL cell type/value mismatch".into())),
        };
        if bytes > MAX_DATA_VALUE_BYTES {
            return Err(StoreError::Quota);
        }
        values.push(value);
    }
    Ok(values)
}

/// The envelope includes both arrays and every key, colon and separator.
/// Metadata values are added below; no temporary serialization is allocated.
const SQL_RESULT_ENVELOPE_BYTES: usize =
    br#"{"affected-rows":,"columns":[],"last-insert-rowid":,"rows":[]}"#.len();

struct SqlWireSize {
    bytes: usize,
    limit: usize,
}
impl SqlWireSize {
    fn new(limit: usize, metadata_minimum: usize) -> Result<Self, StoreError> {
        let mut size = Self { bytes: 0, limit };
        size.add(SQL_RESULT_ENVELOPE_BYTES)?;
        size.add(metadata_minimum)?;
        Ok(size)
    }
    fn add(&mut self, bytes: usize) -> Result<(), StoreError> {
        self.bytes = self
            .bytes
            .checked_add(bytes)
            .filter(|total| *total <= self.limit)
            .ok_or(StoreError::Quota)?;
        Ok(())
    }
}

/// serde_json and StrictV1 use the same JSON string escaping. Count its actual
/// bytes without allocating an encoded buffer. Charge the extra traversal to
/// the same invoking payload owner before visiting guest text.
fn sql_json_string_size(
    text: &str,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<usize, StoreError> {
    charge_sql_measurement(budget, text.len())?;
    struct Counter(usize);
    impl std::io::Write for Counter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0 = self
                .0
                .checked_add(bytes.len())
                .ok_or_else(|| std::io::Error::from(std::io::ErrorKind::OutOfMemory))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut count = Counter(0);
    serde_json::to_writer(&mut count, text).map_err(|_| StoreError::Quota)?;
    Ok(count.0)
}

fn charge_sql_measurement(
    budget: &Arc<dyn AllocationBudget>,
    elements: usize,
) -> Result<(), StoreError> {
    let elements = u64::try_from(elements).map_err(|_| StoreError::Quota)?;
    budget
        .reserve(AllocationRequest {
            kind: AllocationKind::Encode,
            bytes: 0,
            elements,
            depth: 1,
            work: elements.checked_add(1).ok_or(StoreError::Quota)?,
        })
        .map_err(|_| StoreError::Quota)
}

/// All SQLite integers and finite f64 scientific strings fit this fixed scratch
/// buffer. Keep measurement and construction on the same native formatter.
struct SqlNumberText {
    bytes: [u8; 64],
    length: usize,
}
impl SqlNumberText {
    fn format(args: std::fmt::Arguments<'_>) -> Result<Self, StoreError> {
        let mut text = Self {
            bytes: [0; 64],
            length: 0,
        };
        std::fmt::write(&mut text, args).map_err(|_| StoreError::Quota)?;
        Ok(text)
    }
    fn real(value: f64) -> Result<Self, StoreError> {
        if !value.is_finite() {
            return Err(StoreError::Invalid("nonfinite SQL real".into()));
        }
        Self::format(format_args!("{value:e}"))
    }
    fn as_str(&self) -> &str {
        std::str::from_utf8(&self.bytes[..self.length])
            .expect("native numeric formatter produces UTF-8")
    }
    fn json_size(&self) -> Result<usize, StoreError> {
        self.length.checked_add(2).ok_or(StoreError::Quota)
    }
}
impl std::fmt::Write for SqlNumberText {
    fn write_str(&mut self, value: &str) -> std::fmt::Result {
        let end = self
            .length
            .checked_add(value.len())
            .ok_or(std::fmt::Error)?;
        let target = self
            .bytes
            .get_mut(self.length..end)
            .ok_or(std::fmt::Error)?;
        target.copy_from_slice(value.as_bytes());
        self.length = end;
        Ok(())
    }
}

fn step_typed(
    connection: &rusqlite::Connection,
    statement: &mut rusqlite::Statement<'_>,
    fingerprint: &SqlFingerprint,
    policy: Option<crate::SqlResultPolicy>,
    max_rows: u32,
    output_limit: usize,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<crate::OwnedSqlResult, StoreError> {
    // Result-bearing reads have null/null metadata. An empty-column operation
    // reports changes even if SQLite classifies it read-only. Reserve the exact
    // known nulls and minimum quoted numbers, then add the remainder after the
    // one execution, before constructing the metadata values.
    let metadata_minimum = match (fingerprint.readonly, fingerprint.columns.is_empty()) {
        (true, false) => 8,
        (true, true) => 7,
        (false, _) => 6,
    };
    let mut output_size = SqlWireSize::new(output_limit, metadata_minimum)?;
    for (index, column) in fingerprint.columns.iter().enumerate() {
        output_size.add(usize::from(index > 0))?;
        output_size.add(sql_json_string_size(column, budget)?)?;
    }
    let mut columns = CoreValue::array_builder(budget, fingerprint.columns.len())
        .map_err(|_| StoreError::Quota)?;
    for column in &fingerprint.columns {
        columns
            .push(CoreValue::string(column, budget).map_err(|_| StoreError::Quota)?)
            .map_err(|_| StoreError::Quota)?;
    }
    let columns = columns.finish().map_err(|_| StoreError::Quota)?;
    let mut row_values = Vec::new();
    let mut affected = None;
    if fingerprint.columns.is_empty() {
        statement.raw_execute()?;
        affected = Some(connection.changes());
    } else {
        let mut rows = statement.raw_query();
        while let Some(row) = rows.next()? {
            if row_values.len() >= usize::try_from(max_rows).map_err(|_| StoreError::Quota)? {
                return Err(StoreError::Quota);
            }
            // Inspect this same SQLite row before allocating its output cells.
            // Re-reading borrowed columns does not step or replay the statement.
            output_size.add(2 + usize::from(!row_values.is_empty()))?;
            for index in 0..fingerprint.columns.len() {
                output_size.add(usize::from(index > 0))?;
                output_size.add(typed_cell_size(row.get_ref(index)?, budget)?)?;
            }
            let mut cells = Vec::with_capacity(fingerprint.columns.len());
            for index in 0..fingerprint.columns.len() {
                cells.push(typed_cell(row.get_ref(index)?, budget)?);
            }
            row_values.push(
                CoreValue::array_shared(
                    cells
                        .into_iter()
                        .map(Ok::<_, tobkiri_canon::AllocationError>),
                    budget,
                )
                .map_err(|_| StoreError::Quota)?,
            );
        }
        if !fingerprint.readonly {
            affected = Some(connection.changes());
        }
    }
    if policy == Some(crate::SqlResultPolicy::ForbidRows) && !row_values.is_empty() {
        return Err(StoreError::Denied("SQL exec returned rows".into()));
    }
    let affected_text = affected
        .map(|value| SqlNumberText::format(format_args!("{value}")))
        .transpose()?;
    let last_insert_text = if fingerprint.readonly {
        None
    } else {
        Some(SqlNumberText::format(format_args!(
            "{}",
            connection.last_insert_rowid()
        ))?)
    };
    let metadata_size = affected_text
        .as_ref()
        .map_or(Ok(4usize), SqlNumberText::json_size)?
        .checked_add(
            last_insert_text
                .as_ref()
                .map_or(Ok(4usize), SqlNumberText::json_size)?,
        )
        .ok_or(StoreError::Quota)?;
    output_size.add(
        metadata_size
            .checked_sub(metadata_minimum)
            .ok_or(StoreError::Quota)?,
    )?;
    let rows = CoreValue::array_shared(
        row_values
            .into_iter()
            .map(Ok::<_, tobkiri_canon::AllocationError>),
        budget,
    )
    .map_err(|_| StoreError::Quota)?;
    let affected = match affected_text {
        Some(text) => CoreValue::string(text.as_str(), budget),
        None => CoreValue::null(budget),
    }
    .map_err(|_| StoreError::Quota)?;
    let last_insert = match last_insert_text {
        Some(text) => CoreValue::string(text.as_str(), budget),
        None => CoreValue::null(budget),
    }
    .map_err(|_| StoreError::Quota)?;
    let result = CoreValue::object_shared(
        [
            Ok(("columns", columns)),
            Ok(("rows", rows)),
            Ok(("affected-rows", affected)),
            Ok(("last-insert-rowid", last_insert)),
        ]
        .into_iter(),
        budget,
    )
    .map_err(|_| StoreError::Quota)?;
    crate::OwnedSqlResult::from_value(result)
}

fn typed_cell(
    value: ValueRef<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<CoreValue, StoreError> {
    let (kind, cell) = match value {
        ValueRef::Null => ("null", CoreValue::null(budget)),
        ValueRef::Integer(value) => {
            let text = SqlNumberText::format(format_args!("{value}"))?;
            ("integer", CoreValue::string(text.as_str(), budget))
        }
        ValueRef::Real(value) => {
            let text = SqlNumberText::real(value)?;
            ("real", CoreValue::string(text.as_str(), budget))
        }
        ValueRef::Text(bytes) => {
            let text = std::str::from_utf8(bytes)
                .map_err(|_| StoreError::Invalid("SQLite returned non-UTF8 text".into()))?;
            ("text", CoreValue::string(text, budget))
        }
        ValueRef::Blob(bytes) => {
            if bytes.len() > MAX_DATA_VALUE_BYTES {
                return Err(StoreError::Quota);
            }
            let mut array =
                CoreValue::array_builder(budget, bytes.len()).map_err(|_| StoreError::Quota)?;
            for byte in bytes {
                array
                    .push(CoreValue::int(i64::from(*byte), budget).map_err(|_| StoreError::Quota)?)
                    .map_err(|_| StoreError::Quota)?;
            }
            ("blob", array.finish())
        }
    };
    let cell = cell.map_err(|_| StoreError::Quota)?;
    CoreValue::object_shared(
        [
            Ok((
                "type",
                CoreValue::string(kind, budget).map_err(|_| StoreError::Quota)?,
            )),
            Ok(("value", cell)),
        ]
        .into_iter(),
        budget,
    )
    .map_err(|_| StoreError::Quota)
}

fn typed_cell_size(
    value: ValueRef<'_>,
    budget: &Arc<dyn AllocationBudget>,
) -> Result<usize, StoreError> {
    charge_sql_measurement(budget, 1)?;
    let (kind, size) = match value {
        ValueRef::Null => ("null", 4usize),
        ValueRef::Integer(value) => (
            "integer",
            SqlNumberText::format(format_args!("{value}"))?.json_size()?,
        ),
        ValueRef::Real(value) => ("real", SqlNumberText::real(value)?.json_size()?),
        ValueRef::Text(bytes) => {
            let text = std::str::from_utf8(bytes)
                .map_err(|_| StoreError::Invalid("SQLite returned non-UTF8 text".into()))?;
            ("text", sql_json_string_size(text, budget)?)
        }
        ValueRef::Blob(bytes) => {
            if bytes.len() > MAX_DATA_VALUE_BYTES {
                return Err(StoreError::Quota);
            }
            charge_sql_measurement(budget, bytes.len())?;
            let mut size = 2usize;
            for (index, byte) in bytes.iter().enumerate() {
                let digits = if *byte < 10 {
                    1
                } else if *byte < 100 {
                    2
                } else {
                    3
                };
                size = size
                    .checked_add(usize::from(index > 0))
                    .and_then(|size| size.checked_add(digits))
                    .ok_or(StoreError::Quota)?;
            }
            ("blob", size)
        }
    };
    br#"{"type":,"value":}"#
        .len()
        .checked_add(kind.len())
        .and_then(|bytes| bytes.checked_add(2))
        .and_then(|bytes| bytes.checked_add(size))
        .ok_or(StoreError::Quota)
}

#[cfg(all(test, unix))]
#[path = "data_move_tests.rs"]
mod data_move_tests;

#[cfg(all(test, unix))]
mod native_review_tests {
    use super::*;
    use tobkiri_canon::{AllocationError, ids::RequestId};
    use tobkiri_gate::{CancellationToken, CoreControl, Principal, ResourceLimits};

    fn context() -> CallCx {
        CallCx::root(
            ProfileId::parse("prof_native_review").unwrap(),
            Principal::User,
            RequestId::parse("req_native_review").unwrap(),
            CancellationToken::new(),
            std::time::Instant::now() + Duration::from_secs(30),
            ResourceLimits::unlimited(),
        )
    }

    struct DenyStrings;
    impl AllocationBudget for DenyStrings {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            if request.kind == AllocationKind::String {
                Err(AllocationError::Limit)
            } else {
                Ok(())
            }
        }
        fn release(&self, _: u64) {}
    }

    fn database() -> (tempfile::TempDir, DataRoot, NativeDatabase) {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().canonicalize().unwrap()).unwrap();
        let control = CoreControl::default();
        let custody = crate::data_custody::DataCustody::registered(
            None,
            control
                .reserve(ControlKind::Registration, DATABASE_CONTROL_BYTES)
                .unwrap(),
        );
        let path = std::path::Path::new("data.sqlite");
        let opened = root
            .data_database_with_disk_policy(path, true, crate::StoreDiskPolicy::default(), custody)
            .unwrap();
        configure_data_connection(&opened.connection, &root, path).unwrap();
        let database = NativeDatabase {
            connection: Mutex::new(Some(opened.connection)),
            active_transaction: AtomicBool::new(false),
            quarantined: AtomicBool::new(false),
            _directory: Some(opened.directory),
            _custody: opened.custody,
            _global_identity: None,
            process_id: std::process::id(),
        };
        (temp, root, database)
    }

    #[derive(Default)]
    struct SqlResultBudget {
        bytes: AtomicU64,
        large_string_allocations: AtomicU64,
    }
    impl AllocationBudget for SqlResultBudget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.bytes
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |bytes| {
                    bytes
                        .checked_add(request.bytes)
                        .filter(|bytes| *bytes <= 8 * 1024 * 1024)
                })
                .map_err(|_| AllocationError::Limit)?;
            if request.kind == AllocationKind::String && request.bytes >= 1024 {
                self.large_string_allocations.fetch_add(1, Ordering::AcqRel);
            }
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.bytes
                .fetch_update(Ordering::AcqRel, Ordering::Acquire, |live| {
                    live.checked_sub(bytes)
                })
                .expect("SQL output allocation must remain charged until actual release");
        }
    }

    fn measured_sql_result(
        database: &NativeDatabase,
        connection: &rusqlite::Connection,
        sql: &str,
        params: &[Value],
        output_limit: usize,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<crate::OwnedSqlResult, StoreError> {
        let control = CoreControl::default();
        let cx = CallCx::try_root_with_control(
            ProfileId::parse("prof_sql_output").unwrap(),
            Principal::User,
            RequestId::parse("req_sql_output").unwrap(),
            CancellationToken::new_with_control(&control).unwrap(),
            std::time::Instant::now() + Duration::from_secs(30),
            ResourceLimits::unlimited(),
            control,
        )
        .unwrap();
        let mut prepared = prepare_typed(connection, sql, params)?;
        let fingerprint = sql_fingerprint(&prepared)?;
        with_native_operation(
            database,
            connection,
            &cx,
            cx.deadline(),
            cx.limits().fuel_per_call,
            !fingerprint.readonly,
            CommitAdmission::RawReviewFixture,
            || {
                step_typed(
                    connection,
                    &mut prepared,
                    &fingerprint,
                    Some(crate::SqlResultPolicy::AllowRows),
                    MAX_LIST_ROWS,
                    output_limit,
                    budget,
                )
            },
        )
    }

    #[test]
    fn typed_sql_two_large_ascii_rows_fit_the_actual_four_mib_output_bound() {
        let (_temp, _root, database) = database();
        let connection = database.try_lock_connection().unwrap();
        connection
            .execute_batch("CREATE TABLE payload(id INTEGER PRIMARY KEY, value TEXT);")
            .unwrap();
        let text = "x".repeat(600 * 1024);
        connection
            .execute("INSERT INTO payload VALUES(1,?1),(2,?1)", [&text])
            .unwrap();
        let meter = Arc::new(SqlResultBudget::default());
        let budget: Arc<dyn AllocationBudget> = meter.clone();
        let result = measured_sql_result(
            &database,
            &connection,
            "SELECT value FROM payload ORDER BY id",
            &[],
            4 * 1024 * 1024,
            &budget,
        )
        .unwrap();
        assert_eq!(
            result
                .value()
                .get_key("rows")
                .unwrap()
                .array()
                .unwrap()
                .len(),
            2
        );
        let encoded = result.value().encode_guest(&budget).unwrap();
        assert!(encoded.len() > 1024 * 1024);
        assert!(encoded.len() < 4 * 1024 * 1024);
        assert_eq!(meter.large_string_allocations.load(Ordering::Acquire), 2);
        drop(encoded);
        drop(result);
        assert_eq!(meter.bytes.load(Ordering::Acquire), 0);
    }

    #[test]
    fn typed_sql_escaped_cells_and_all_envelopes_match_the_encoder_at_the_boundary() {
        let (_temp, _root, database) = database();
        let connection = database.try_lock_connection().unwrap();
        let unit: String = (0u8..=31)
            .map(char::from)
            .chain(['"', '\\', '雪', '\u{2028}'])
            .collect();
        let text = unit.repeat(32);
        let params = [
            Value::Text(text),
            Value::Integer(i64::MIN),
            Value::Real(-0.0),
            Value::Blob(vec![0, 9, 10, 99, 100, 255]),
        ];
        let sql = r#"SELECT ?1 AS "quo""te", NULL AS n, ?2 AS i, ?3 AS r, ?4 AS b"#;
        let meter = Arc::new(SqlResultBudget::default());
        let budget: Arc<dyn AllocationBudget> = meter.clone();
        let result = measured_sql_result(
            &database,
            &connection,
            sql,
            &params,
            4 * 1024 * 1024,
            &budget,
        )
        .unwrap();
        let encoded = result.value().encode_guest(&budget).unwrap();
        let exact_size = encoded.len();
        assert!(encoded.as_str().unwrap().contains(r#"\u0000"#));
        assert!(encoded.as_str().unwrap().contains('雪'));
        drop(encoded);
        drop(result);
        assert_eq!(meter.bytes.load(Ordering::Acquire), 0);

        let exact =
            measured_sql_result(&database, &connection, sql, &params, exact_size, &budget).unwrap();
        assert_eq!(
            exact.value().encode_guest(&budget).unwrap().len(),
            exact_size
        );
        drop(exact);
        assert_eq!(meter.bytes.load(Ordering::Acquire), 0);

        let denied_meter = Arc::new(SqlResultBudget::default());
        let denied_budget: Arc<dyn AllocationBudget> = denied_meter.clone();
        assert!(matches!(
            measured_sql_result(
                &database,
                &connection,
                sql,
                &params,
                exact_size - 1,
                &denied_budget,
            ),
            Err(StoreError::Quota)
        ));
        // The entire borrowed row is measured before allocating its text cells.
        assert_eq!(
            denied_meter
                .large_string_allocations
                .load(Ordering::Acquire),
            0
        );
        assert_eq!(denied_meter.bytes.load(Ordering::Acquire), 0);
    }

    #[test]
    fn typed_sql_returning_is_not_replayed_and_metadata_overflow_rolls_back() {
        let (_temp, _root, database) = database();
        let connection = database.try_lock_connection().unwrap();
        connection
            .execute_batch(
                "CREATE TABLE payload(id INTEGER PRIMARY KEY, value TEXT);
                 INSERT INTO payload VALUES(999,'seed');",
            )
            .unwrap();
        let meter = Arc::new(SqlResultBudget::default());
        let budget: Arc<dyn AllocationBudget> = meter.clone();
        let sql = "INSERT INTO payload(value) VALUES(?1) RETURNING value";
        let params = [Value::Text("actual returned value".into())];
        let first = measured_sql_result(
            &database,
            &connection,
            sql,
            &params,
            4 * 1024 * 1024,
            &budget,
        )
        .unwrap();
        let encoded = first.value().encode_guest(&budget).unwrap();
        let exact_size = encoded.len();
        assert_eq!(
            first.value().get_key("affected-rows").unwrap().as_str(),
            Some("1")
        );
        assert_eq!(
            first.value().get_key("last-insert-rowid").unwrap().as_str(),
            Some("1000")
        );
        drop(encoded);
        drop(first);
        let second =
            measured_sql_result(&database, &connection, sql, &params, exact_size, &budget).unwrap();
        assert_eq!(
            second.value().encode_guest(&budget).unwrap().len(),
            exact_size
        );
        drop(second);
        assert!(matches!(
            measured_sql_result(
                &database,
                &connection,
                sql,
                &params,
                exact_size - 1,
                &budget,
            ),
            Err(StoreError::Quota)
        ));
        assert_eq!(
            connection
                .query_row("SELECT count(*),max(id) FROM payload", [], |row| {
                    Ok((row.get::<_, u32>(0)?, row.get::<_, u32>(1)?))
                })
                .unwrap(),
            (3, 1001)
        );
        assert!(connection.is_autocommit());
        assert!(!database.quarantined.load(Ordering::Acquire));
        assert_eq!(meter.bytes.load(Ordering::Acquire), 0);
    }

    #[test]
    fn typed_sql_output_size_checked_add_rejects_overflow() {
        let mut size = SqlWireSize {
            bytes: usize::MAX,
            limit: usize::MAX,
        };
        assert!(matches!(size.add(1), Err(StoreError::Quota)));
    }

    #[test]
    fn growing_transaction_uses_memory_subjournals_and_keeps_statement_rollback() {
        let (_temp, root, database) = database();
        let connection = database.try_lock_connection().unwrap();
        connection
            .execute_batch("CREATE TABLE payload(id INTEGER PRIMARY KEY, value BLOB NOT NULL);")
            .unwrap();
        with_internal_authorizer(&database, &connection, || {
            connection.execute_batch("BEGIN DEFERRED;")?;
            Ok(())
        })
        .unwrap();
        let first = vec![1u8; 128 * 1024];
        connection
            .execute(
                "WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<20)
                 INSERT INTO payload SELECT i,?1 FROM n",
                [&first],
            )
            .unwrap();
        assert!(root.file_len("data.sqlite").unwrap().unwrap() > 512 * 1024);
        let second = vec![2u8; 128 * 1024];
        assert_eq!(
            connection
                .execute("UPDATE payload SET value=?1", [&second])
                .unwrap(),
            20
        );
        assert!(matches!(
            connection.execute("UPDATE payload SET id=1", []),
            Err(rusqlite::Error::SqliteFailure(error, _))
                if error.code == rusqlite::ErrorCode::ConstraintViolation
        ));
        assert_eq!(
            connection
                .query_row("SELECT min(id),max(id),count(*) FROM payload", [], |row| {
                    Ok((
                        row.get::<_, u32>(0)?,
                        row.get::<_, u32>(1)?,
                        row.get::<_, u32>(2)?,
                    ))
                })
                .unwrap(),
            (1, 20, 20)
        );
        let retained: Vec<u8> = connection
            .query_row("SELECT value FROM payload WHERE id=1", [], |row| row.get(0))
            .unwrap();
        assert_eq!(retained, second);
        with_internal_authorizer(&database, &connection, || {
            connection.execute_batch("ROLLBACK;")?;
            Ok(())
        })
        .unwrap();
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM payload", [], |row| row
                    .get::<_, u32>(0))
                .unwrap(),
            0
        );
    }

    #[test]
    fn poisoned_connection_is_quarantined_instead_of_retried_as_busy() {
        let (_temp, _root, database) = database();
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = database.try_lock_connection().unwrap();
            panic!("inject native connection poison");
        }));
        assert!(matches!(
            database.try_lock_connection(),
            Err(StoreError::Unavailable(_))
        ));
        assert!(database.quarantined.load(Ordering::Acquire));
        assert!(database._directory.as_ref().unwrap().is_quarantined());
    }

    #[test]
    fn typed_write_output_allocation_failure_rolls_back_the_actual_insert() {
        let (_temp, _root, database) = database();
        let cx = context();
        let connection = database.try_lock_connection().unwrap();
        connection
            .execute_batch("CREATE TABLE payload(id INTEGER PRIMARY KEY);")
            .unwrap();
        let mut prepared =
            prepare_typed(&connection, "INSERT INTO payload DEFAULT VALUES", &[]).unwrap();
        let fingerprint = sql_fingerprint(&prepared).unwrap();
        let budget: Arc<dyn AllocationBudget> = Arc::new(DenyStrings);
        let result = with_native_operation(
            &database,
            &connection,
            &cx,
            cx.deadline(),
            cx.limits().fuel_per_call,
            true,
            CommitAdmission::RawReviewFixture,
            || {
                step_typed(
                    &connection,
                    &mut prepared,
                    &fingerprint,
                    Some(crate::SqlResultPolicy::ForbidRows),
                    10,
                    1024,
                    &budget,
                )
            },
        );
        assert!(matches!(result, Err(StoreError::Quota)));
        assert!(connection.is_autocommit());
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM payload", [], |row| row
                    .get::<_, u32>(0))
                .unwrap(),
            0
        );
        assert!(!database.quarantined.load(Ordering::Acquire));
    }

    #[test]
    fn partial_or_fail_statement_is_rolled_back_before_failed_return() {
        let (_temp, _root, database) = database();
        let cx = context();
        let connection = database.try_lock_connection().unwrap();
        connection
            .execute_batch("CREATE TABLE payload(id INTEGER PRIMARY KEY);")
            .unwrap();
        let result = with_native_operation(
            &database,
            &connection,
            &cx,
            cx.deadline(),
            cx.limits().fuel_per_call,
            true,
            CommitAdmission::RawReviewFixture,
            || {
                connection.execute("INSERT OR FAIL INTO payload VALUES(1),(2),(1)", [])?;
                Ok(())
            },
        );
        assert!(matches!(result, Err(StoreError::Sql(_))));
        assert!(connection.is_autocommit());
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM payload", [], |row| row
                    .get::<_, u32>(0))
                .unwrap(),
            0
        );
        assert!(!database.quarantined.load(Ordering::Acquire));
    }

    #[test]
    fn cancellation_after_provisional_write_has_verified_rollback() {
        let (_temp, _root, database) = database();
        let cx = context();
        let connection = database.try_lock_connection().unwrap();
        connection
            .execute_batch("CREATE TABLE payload(id INTEGER PRIMARY KEY);")
            .unwrap();
        let result = with_native_operation(
            &database,
            &connection,
            &cx,
            cx.deadline(),
            cx.limits().fuel_per_call,
            true,
            CommitAdmission::RawReviewFixture,
            || {
                connection.execute("INSERT INTO payload VALUES(1)", [])?;
                cx.cancel().cancel();
                Ok(())
            },
        );
        assert!(matches!(
            result,
            Err(StoreError::Gate(tobkiri_gate::GateError::Cancelled))
        ));
        assert!(connection.is_autocommit());
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM payload", [], |row| row
                    .get::<_, u32>(0))
                .unwrap(),
            0
        );
        assert!(!database.quarantined.load(Ordering::Acquire));
    }

    #[test]
    fn committed_write_delivery_failure_is_unknown_without_physical_quarantine() {
        let (_temp, _root, database) = database();
        let cx = context();
        let connection = database.try_lock_connection().unwrap();
        connection
            .execute_batch("CREATE TABLE payload(id INTEGER PRIMARY KEY);")
            .unwrap();
        with_native_operation(
            &database,
            &connection,
            &cx,
            cx.deadline(),
            cx.limits().fuel_per_call,
            true,
            CommitAdmission::RawReviewFixture,
            || {
                connection.execute("INSERT INTO payload VALUES(1)", [])?;
                Ok(())
            },
        )
        .unwrap();
        cx.cancel().cancel();
        assert!(matches!(
            check_native_completion(&cx, cx.deadline(), false),
            Err(StoreError::Gate(tobkiri_gate::GateError::Cancelled))
        ));
        assert!(!database.quarantined.load(Ordering::Acquire));
        assert!(matches!(
            check_native_completion(&cx, cx.deadline(), true),
            Err(StoreError::CommitUnknown)
        ));
        assert!(!database.quarantined.load(Ordering::Acquire));
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM payload", [], |row| row
                    .get::<_, u32>(0))
                .unwrap(),
            1
        );
    }

    #[test]
    fn confirmed_native_close_releases_its_exact_route_slot_and_custody() {
        let (_temp, _root, database) = database();
        let retained = database
            ._directory
            .as_ref()
            .unwrap()
            .retained_connection_probe();
        let custody = Arc::downgrade(&database._custody);
        assert!(retained.upgrade().unwrap().lock().unwrap().is_none());
        drop(database);
        assert!(retained.upgrade().is_none());
        assert!(custody.upgrade().is_none());
    }

    #[test]
    fn uncertain_native_connection_retains_preadmitted_slot_and_denies_reopen() {
        let (_temp, root, database) = database();
        let retained = database
            ._directory
            .as_ref()
            .unwrap()
            .retained_connection_probe();
        let custody = Arc::downgrade(&database._custody);
        let exact_connection = unsafe { database.try_lock_connection().unwrap().handle() };
        assert!(matches!(
            database.commit_unknown(),
            StoreError::CommitUnknown
        ));
        drop(database);
        let retained = retained.upgrade().unwrap();
        let retained = retained.lock().unwrap();
        assert_eq!(
            unsafe { retained.as_ref().unwrap().handle() },
            exact_connection
        );
        assert!(custody.upgrade().is_some());
        let control = CoreControl::default();
        let new_custody = crate::data_custody::DataCustody::registered(
            None,
            control
                .reserve(ControlKind::Registration, DATABASE_CONTROL_BYTES)
                .unwrap(),
        );
        assert!(matches!(
            root.data_database_with_disk_policy(
                std::path::Path::new("data.sqlite"),
                true,
                crate::StoreDiskPolicy::default(),
                new_custody,
            ),
            Err(StoreError::Unavailable(_))
        ));
        assert_eq!(control.usage().count(ControlKind::Registration), 0);
    }

    #[test]
    fn poisoned_retention_slot_keeps_the_exact_connection_without_resuming_sql() {
        let (_temp, _root, database) = database();
        let retained = database
            ._directory
            .as_ref()
            .unwrap()
            .retained_connection_probe();
        let retained = retained.upgrade().unwrap();
        let exact_connection = unsafe { database.try_lock_connection().unwrap().handle() };
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _guard = retained.lock().unwrap();
            panic!("inject retention slot poison");
        }));
        database.commit_unknown();
        drop(database);
        assert!(retained.is_poisoned());
        let retained = retained.lock().unwrap_err().into_inner();
        assert_eq!(
            unsafe { retained.as_ref().unwrap().handle() },
            exact_connection
        );
    }
}
