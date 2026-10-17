use crate::engine::{self, Artifact, Backend, Receipt, ToolError};
use serde_json::{Value, json};

wit_bindgen::generate!({path:"wit",world:"pack",generate_all});

use exports::local::harness_agent::tool_provider::Guest;
use exports::tobkiri::core::lifecycle;
use tobkiri::core::{
    clock, flow, random, storage,
    types::{Error, ErrorCode},
};

struct Pack;
struct CoreStore(storage::Store);

fn core_error(_: Error) -> ToolError {
    ToolError::new(
        "backend",
        "Core operation failed; inspect the run before retrying an uncertain mutation",
    )
}

fn initialize() -> Result<CoreStore, Error> {
    let store = storage::open("artifacts")?;
    // One immutable row is both the state change and its durable receipt. A
    // crash between commit and delivery is reconciled by the same call ID.
    store.sql_exec("CREATE TABLE IF NOT EXISTS artifact_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL UNIQUE, signature TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL, deleted INTEGER NOT NULL, result TEXT NOT NULL)", &[])?;
    store.sql_exec(
        "CREATE INDEX IF NOT EXISTS artifact_paths ON artifact_events(path, seq)",
        &[],
    )?;
    Ok(CoreStore(store))
}

fn cell<T: serde::de::DeserializeOwned>(row: &[String], index: usize) -> Result<T, ToolError> {
    row.get(index)
        .and_then(|v| serde_json::from_str(v).ok())
        .ok_or_else(|| ToolError::new("backend", "invalid stored artifact record"))
}

impl Backend for CoreStore {
    fn get(&mut self, path: &str) -> Result<Option<Artifact>, ToolError> {
        let rows = self.0.sql_query("SELECT path,content,deleted FROM artifact_events WHERE path=? ORDER BY seq DESC LIMIT 1", &[json!(path).to_string()], 1).map_err(core_error)?;
        rows.first()
            .map(|row| {
                Ok(Artifact {
                    path: cell(row, 0)?,
                    content: cell(row, 1)?,
                    deleted: cell::<i64>(row, 2)? != 0,
                })
            })
            .transpose()
    }
    fn list(&mut self, prefix: &str, after: &str, limit: u32) -> Result<Vec<Value>, ToolError> {
        let rows = self.0.sql_query("SELECT e.result FROM artifact_events e WHERE e.seq=(SELECT MAX(n.seq) FROM artifact_events n WHERE n.path=e.path) AND e.deleted=0 AND e.path>? AND substr(e.path,1,length(?))=? ORDER BY e.path LIMIT ?", &[json!(after).to_string(),json!(prefix).to_string(),json!(prefix).to_string(),json!(limit).to_string()], limit).map_err(core_error)?;
        rows.iter()
            .map(|row| {
                let metadata: String = cell(row, 0)?;
                serde_json::from_str(&metadata)
                    .map_err(|_| ToolError::new("backend", "invalid artifact metadata"))
            })
            .collect()
    }
    fn receipt(&mut self, id: &str) -> Result<Option<Receipt>, ToolError> {
        let rows = self
            .0
            .sql_query(
                "SELECT signature,result FROM artifact_events WHERE call_id=? LIMIT 1",
                &[json!(id).to_string()],
                1,
            )
            .map_err(core_error)?;
        rows.first()
            .map(|row| {
                let result: String = cell(row, 1)?;
                Ok(Receipt {
                    signature: cell(row, 0)?,
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
        self.0.sql_exec("INSERT INTO artifact_events(call_id,signature,path,content,deleted,result) VALUES (?,?,?,?,?,?) ON CONFLICT(call_id) DO NOTHING", &[
            json!(id).to_string(),json!(signature).to_string(),json!(artifact.path).to_string(),json!(artifact.content).to_string(),json!(i64::from(artifact.deleted)).to_string(),json!(result.to_string()).to_string(),
        ]).map_err(core_error)?;
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
