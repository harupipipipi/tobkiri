use crate::{
    engine::{self, Artifact, Backend, Receipt, ToolError},
    sql_wire,
};
use serde_json::{Value, json};

wit_bindgen::generate!({path:"wit",world:"pack",generate_all});

use exports::local::harness_agent::tool_provider::Guest;
use exports::tobkiri::core1_0_0::lifecycle;
use tobkiri::core1_0_0::{
    clock, flow, random,
    types::{Error, ErrorCode},
};
use tobkiri::core1_1_0::{
    storage,
    types::{Error as StorageError, ErrorCode as StorageErrorCode},
};

struct Pack;
struct CoreStore(storage::Store);

fn core_error(_: StorageError) -> ToolError {
    ToolError::new(
        "backend",
        "Core operation failed; inspect the run before retrying an uncertain mutation",
    )
}

// These are distinct nominal Core packages. Preserve the public error code
// explicitly rather than aliasing 1.1 storage resources/errors as 1.0 types.
fn lifecycle_error(error: StorageError) -> Error {
    let code = match error.code {
        StorageErrorCode::Denied => ErrorCode::Denied,
        StorageErrorCode::ApprovalRequired => ErrorCode::ApprovalRequired,
        StorageErrorCode::Cancelled => ErrorCode::Cancelled,
        StorageErrorCode::NotFound => ErrorCode::NotFound,
        StorageErrorCode::Invalid => ErrorCode::Invalid,
        StorageErrorCode::LimitExceeded => ErrorCode::LimitExceeded,
        StorageErrorCode::Conflict => ErrorCode::Conflict,
        StorageErrorCode::Unavailable => ErrorCode::Unavailable,
        StorageErrorCode::Internal => ErrorCode::Internal,
    };
    Error {
        code,
        message: error.message,
    }
}

fn initialize() -> Result<CoreStore, Error> {
    let store = storage::open("artifacts").map_err(lifecycle_error)?;
    // One immutable row is both the state change and its durable receipt. A
    // crash between commit and delivery is reconciled by the same call ID.
    store.sql_exec_typed("CREATE TABLE IF NOT EXISTS artifact_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL UNIQUE, signature TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL, deleted INTEGER NOT NULL, result TEXT NOT NULL)", &[], 0, storage::SqlResultPolicy::ForbidRows).map_err(lifecycle_error)?;
    // No secondary path index is required for correctness. Explicit CREATE
    // INDEX also requests SQLite's REINDEX action, which Core's restrictive
    // authorizer denies. Keep that policy intact; UNIQUE(call_id) in the table
    // still enforces the immutable mutation/receipt identity atomically.
    Ok(CoreStore(store))
}

fn cell(row: &[String], index: usize) -> Result<&str, ToolError> {
    row.get(index)
        .map(String::as_str)
        .ok_or_else(|| ToolError::new("backend", "invalid stored artifact record"))
}

impl CoreStore {
    fn query(
        &self,
        statement: &str,
        params: &[String],
        limit: u32,
        columns: &[&str],
    ) -> Result<Vec<Vec<String>>, ToolError> {
        let result = self
            .0
            .sql_query_typed(statement, params, limit)
            .map_err(core_error)?;
        if result
            .columns
            .iter()
            .map(String::as_str)
            .ne(columns.iter().copied())
            || result.rows.iter().any(|row| row.len() != columns.len())
        {
            return Err(ToolError::new(
                "backend",
                "typed SQL row/column contract mismatch",
            ));
        }
        Ok(result.rows)
    }
}

impl Backend for CoreStore {
    fn get(&mut self, path: &str) -> Result<Option<Artifact>, ToolError> {
        let rows = self.query("SELECT path,content,deleted FROM artifact_events WHERE path=? ORDER BY seq DESC LIMIT 1", &[sql_wire::text(path)], 1, &["path", "content", "deleted"])?;
        rows.first()
            .map(|row| {
                Ok(Artifact {
                    path: sql_wire::read_text(cell(row, 0)?)?,
                    content: sql_wire::read_text(cell(row, 1)?)?,
                    deleted: sql_wire::read_integer(cell(row, 2)?)? != 0,
                })
            })
            .transpose()
    }
    fn list(&mut self, prefix: &str, after: &str, limit: u32) -> Result<Vec<Value>, ToolError> {
        let rows = self.query("SELECT e.result FROM artifact_events e WHERE e.seq=(SELECT MAX(n.seq) FROM artifact_events n WHERE n.path=e.path) AND e.deleted=0 AND e.path>? AND substr(e.path,1,length(?))=? ORDER BY e.path LIMIT ?", &[sql_wire::text(after),sql_wire::text(prefix),sql_wire::text(prefix),sql_wire::integer(i64::from(limit))], limit, &["result"])?;
        rows.iter()
            .map(|row| {
                let metadata = sql_wire::read_text(cell(row, 0)?)?;
                serde_json::from_str(&metadata)
                    .map_err(|_| ToolError::new("backend", "invalid artifact metadata"))
            })
            .collect()
    }
    fn receipt(&mut self, id: &str) -> Result<Option<Receipt>, ToolError> {
        let rows = self.query(
            "SELECT signature,result FROM artifact_events WHERE call_id=? LIMIT 1",
            &[sql_wire::text(id)],
            1,
            &["signature", "result"],
        )?;
        rows.first()
            .map(|row| {
                let result = sql_wire::read_text(cell(row, 1)?)?;
                Ok(Receipt {
                    signature: sql_wire::read_text(cell(row, 0)?)?,
                    result: serde_json::from_str(&result)
                        .map_err(|_| ToolError::new("backend", "invalid stored receipt"))?,
                })
            })
            .transpose()
    }
    fn commit(
        &mut self,
        id: &str,
        signature: &str,
        artifact: Artifact,
        result: Value,
    ) -> Result<Receipt, ToolError> {
        self.0.sql_exec_typed("INSERT INTO artifact_events(call_id,signature,path,content,deleted,result) VALUES (?,?,?,?,?,?) ON CONFLICT(call_id) DO NOTHING", &[
            sql_wire::text(id),sql_wire::text(signature),sql_wire::text(&artifact.path),sql_wire::text(&artifact.content),sql_wire::integer(i64::from(artifact.deleted)),sql_wire::text(&result.to_string()),
        ], 0, storage::SqlResultPolicy::ForbidRows).map_err(core_error)?;
        self.receipt(id)?.ok_or_else(|| {
            ToolError::new(
                "backend",
                "committed receipt is unavailable; inspect before retrying",
            )
        })
    }
    fn now_ms(&mut self) -> Result<u64, ToolError> {
        Ok(clock::now_ms())
    }
    fn random(&mut self, length: u32) -> Result<Vec<u8>, ToolError> {
        Ok(random::bytes(length))
    }
}

impl lifecycle::Guest for Pack {
    fn init(config: String) -> Result<(), Error> {
        if serde_json::from_str::<Value>(&config).ok() != Some(json!({})) {
            return Err(Error {
                code: ErrorCode::Invalid,
                message: "harness-tools expects an empty configuration".into(),
            });
        }
        initialize()?;
        Ok(())
    }
    fn shutdown() {}
}

impl Guest for Pack {
    fn invoke(call: String) -> Result<String, Error> {
        if flow::is_cancelled() {
            return Err(Error {
                code: ErrorCode::Cancelled,
                message: "run cancelled".into(),
            });
        }
        Ok(engine::invoke(&mut initialize()?, &call).to_string())
    }
}

export!(Pack);
