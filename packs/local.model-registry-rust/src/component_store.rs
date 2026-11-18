//! Persist the latest owner document with an atomic conditional write.

use crate::component::tobkiri::core::{
    storage::{self, SqlResultPolicy, Store},
    types::{Error, ErrorCode},
};
use ordered_python_compat::PyValue;
use sha2::{Digest as _, Sha256};

const STORE: &str = "model-registry";
const SEED: &str = "document";
const MAX_SQL_PARAMETERS: usize = 1024 * 1024;
const TABLE: &str = "CREATE TABLE IF NOT EXISTS model_registry (\
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), \
    document TEXT NOT NULL, token TEXT NOT NULL)";
const HAS_TABLE: &str = "SELECT 1 FROM sqlite_schema \
    WHERE type = 'table' AND name = 'model_registry'";
const READ: &str = "SELECT document, token FROM model_registry WHERE singleton = 1";
const INSERT: &str = "INSERT INTO model_registry (singleton, document, token) \
    VALUES (1, ?1, ?2) ON CONFLICT (singleton) DO NOTHING";
const UPDATE: &str = "UPDATE model_registry SET document = ?1, token = ?2 \
    WHERE singleton = 1 AND token COLLATE BINARY = ?3";

pub(super) struct DocumentStore {
    store: Store,
    table_exists: bool,
    token: Option<String>,
}

impl DocumentStore {
    /// Read the current native row or the old text imported into private DATA.
    pub(super) fn read() -> Result<(Self, Option<PyValue>), Error> {
        let store = storage::open(STORE)?;
        let table_exists = !store.sql_query_typed(HAS_TABLE, &[], 1)?.rows.is_empty();
        if table_exists {
            match store.sql_query_typed(READ, &[], 1)?.rows.as_slice() {
                [] => {}
                [row] => {
                    let [document, token] = row.as_slice() else {
                        return Err(invalid_store());
                    };
                    let document = decode_cell(document)?;
                    let token = decode_cell(token)?;
                    if document_token(&document) != token {
                        return Err(invalid_store());
                    }
                    let document = crate::component::decode_document(&document)?;
                    return Ok((
                        Self {
                            store,
                            table_exists,
                            token: Some(token),
                        },
                        Some(document),
                    ));
                }
                _ => return Err(invalid_store()),
            }
        }
        let seeded = store.kv_get(SEED)?;
        let document = seeded
            .as_deref()
            .map(decode_text)
            .transpose()?
            .map(|text| crate::component::decode_document(&text))
            .transpose()?;
        Ok((
            Self {
                store,
                table_exists,
                token: None,
            },
            document,
        ))
    }

    /// A native update cannot overwrite a row committed after this read.
    pub(super) fn commit(self, document: &str) -> Result<(), Error> {
        let token = document_token(document);
        if document.len() + token.len() + self.token.as_ref().map_or(0, String::len)
            > MAX_SQL_PARAMETERS
        {
            return Err(crate::component::failure(
                ErrorCode::LimitExceeded,
                "model Registry document exceeds storage limits",
            ));
        }
        let mut params = vec![encode_cell(document), encode_cell(&token)];
        let statement = match self.token {
            Some(observed) => {
                params.push(encode_cell(&observed));
                UPDATE
            }
            None => INSERT,
        };
        if !self.table_exists {
            self.store
                .sql_exec_typed(TABLE, &[], 0, SqlResultPolicy::ForbidRows)?;
        }
        let result =
            self.store
                .sql_exec_typed(statement, &params, 0, SqlResultPolicy::ForbidRows)?;
        match result.affected_rows.as_deref() {
            Some("1") => Ok(()),
            Some("0") => Err(crate::component::failure(
                ErrorCode::Conflict,
                "model Registry revision is stale",
            )),
            _ => Err(invalid_store()),
        }
    }
}

fn document_token(document: &str) -> String {
    format!("{:x}", Sha256::digest(document.as_bytes()))
}

fn decode_text(stored: &[u8]) -> Result<String, Error> {
    serde_json::from_slice(stored).map_err(|_| invalid_store())
}

fn decode_cell(stored: &str) -> Result<String, Error> {
    let cell: serde_json::Value = serde_json::from_str(stored).map_err(|_| invalid_store())?;
    if cell["type"] != "text" || cell.as_object().map(serde_json::Map::len) != Some(2) {
        return Err(invalid_store());
    }
    cell["value"]
        .as_str()
        .map(str::to_owned)
        .ok_or_else(invalid_store)
}

fn encode_cell(text: &str) -> String {
    serde_json::json!({"type":"text","value":text}).to_string()
}

fn invalid_store() -> Error {
    crate::component::failure(ErrorCode::Invalid, "model Registry DATA is invalid")
}
