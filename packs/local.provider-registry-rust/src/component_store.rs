//! Pack-private storage for the original Registry's decoded document.

use crate::{
    component::tobkiri::core::{
        storage::{self, SqlResultPolicy, Store},
        types::{Error, ErrorCode},
    },
    failure::{SourceError, SourceResult},
    state::DocumentStore,
};
use ordered_python_compat::{CodecError, PyValue, dumps, loads};
use sha2::{Digest as _, Sha256};

const STORE_NAME: &str = "provider-registry";
// Core's typed SQL limit counts decoded text parameters, including the tokens.
const MAX_SQL_PARAMETER_BYTES: usize = 1024 * 1024;
const CREATE_TABLE: &str = "CREATE TABLE IF NOT EXISTS provider_registry (\
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), document TEXT NOT NULL, token TEXT NOT NULL)";
const SELECT_TABLE: &str = "SELECT 1 FROM sqlite_schema \
    WHERE type = 'table' AND name = 'provider_registry'";
const SELECT_DOCUMENT: &str = "SELECT document, token FROM provider_registry WHERE singleton = 1";
const INSERT_DOCUMENT: &str = "INSERT INTO provider_registry (singleton, document, token) \
    VALUES (1, ?1, ?2) ON CONFLICT (singleton) DO NOTHING";
const UPDATE_DOCUMENT: &str = "UPDATE provider_registry SET document = ?1, token = ?2 \
    WHERE singleton = 1 AND token COLLATE BINARY = ?3";

#[derive(Default)]
enum Observation {
    #[default]
    Unobserved,
    Absent,
    Present(String),
}

struct PendingWrite {
    statement: &'static str,
    params: Vec<String>,
}

pub(super) struct SqlDocumentStore {
    store: Option<Store>,
    table_exists: bool,
    observed: Observation,
    pending: Option<PendingWrite>,
}

impl SqlDocumentStore {
    /// Delay host imports until the Registry first asks for its document.
    pub(super) fn new() -> Self {
        Self {
            store: None,
            table_exists: false,
            observed: Observation::Unobserved,
            pending: None,
        }
    }

    fn store(&mut self) -> SourceResult<&Store> {
        if self.store.is_none() {
            self.store = Some(storage::open(STORE_NAME).map_err(storage_error)?);
        }
        Ok(self.store.as_ref().expect("private store was opened"))
    }

    fn require_no_pending_write(&self) -> SourceResult<()> {
        if self.pending.is_some() {
            return Err(SourceError::new(
                "StorageError",
                "provider registry write is already pending",
            ));
        }
        Ok(())
    }

    /// Commit after the component has verified that it can return the reply.
    pub(super) fn commit(&mut self) -> SourceResult<()> {
        // An error after dispatch can leave an unknown commit outcome. Consume
        // the pending write before dispatch so it cannot be replayed.
        let Some(PendingWrite { statement, params }) = self.pending.take() else {
            return Ok(());
        };
        if !self.table_exists {
            self.store()?
                .sql_exec_typed(CREATE_TABLE, &[], 0, SqlResultPolicy::ForbidRows)
                .map_err(storage_error)?;
            self.table_exists = true;
        }
        let result = self
            .store()?
            .sql_exec_typed(statement, &params, 0, SqlResultPolicy::ForbidRows)
            .map_err(storage_error)?;
        match result.affected_rows.as_deref() {
            Some("1") => Ok(()),
            Some("0") => Err(SourceError::conflict("provider registry revision is stale")),
            _ => Err(invalid_result()),
        }
    }
}

impl DocumentStore for SqlDocumentStore {
    fn read(&mut self) -> SourceResult<Option<PyValue>> {
        self.require_no_pending_write()?;
        self.observed = Observation::Unobserved;
        let table_exists = self.table_exists;
        let store = self.store()?;
        if !table_exists
            && store
                .sql_query_typed(SELECT_TABLE, &[], 1)
                .map_err(storage_error)?
                .rows
                .is_empty()
        {
            self.observed = Observation::Absent;
            return Ok(None);
        }
        let result = store
            .sql_query_typed(SELECT_DOCUMENT, &[], 1)
            .map_err(storage_error)?;
        self.table_exists = true;
        match result.rows.as_slice() {
            [] => {
                self.observed = Observation::Absent;
                Ok(None)
            }
            [row] => {
                let [document, token] = row.as_slice() else {
                    return Err(invalid_result());
                };
                let document = decode_cell(document)?;
                let token = decode_cell(token)?;
                if document_token(&document) != token {
                    return Err(SourceError::new(
                        "StorageError",
                        "provider registry document does not match its stored token",
                    ));
                }
                let value = loads(&document).map_err(document_error)?;
                self.observed = Observation::Present(token);
                Ok(Some(value))
            }
            _ => Err(invalid_result()),
        }
    }

    fn write(&mut self, document: &PyValue) -> SourceResult<()> {
        self.require_no_pending_write()?;
        let observed = std::mem::take(&mut self.observed);
        let (statement, expected_token) = match observed {
            Observation::Unobserved => {
                return Err(SourceError::new(
                    "StorageError",
                    "provider registry write requires a prior read",
                ));
            }
            Observation::Absent => (INSERT_DOCUMENT, None),
            Observation::Present(previous) => (UPDATE_DOCUMENT, Some(previous)),
        };
        let document = dumps(document).map_err(document_error)?;
        let token = document_token(&document);
        let parameter_bytes =
            document.len() + token.len() + expected_token.as_ref().map_or(0, String::len);
        if parameter_bytes > MAX_SQL_PARAMETER_BYTES {
            return Err(SourceError::new(
                "StorageLimitExceeded",
                "provider registry document exceeds the private store limit",
            ));
        }
        let mut params = vec![text_param(&document)?, text_param(&token)?];
        if let Some(expected_token) = expected_token {
            params.push(text_param(&expected_token)?);
        }
        self.pending = Some(PendingWrite { statement, params });
        Ok(())
    }
}

fn decode_cell(cell: &str) -> SourceResult<String> {
    let envelope = loads(cell).map_err(|_| invalid_result())?;
    if envelope.as_object().map(|fields| fields.len()) != Some(2)
        || envelope.get("type").and_then(PyValue::as_str) != Some("text")
    {
        return Err(invalid_result());
    }
    envelope
        .into_object()
        .expect("SQL cell shape was validated")
        .into_iter()
        .find_map(|(key, value)| (key == "value").then_some(value))
        .and_then(PyValue::into_string)
        .ok_or_else(invalid_result)
}

fn document_token(document: &str) -> String {
    format!("{:x}", Sha256::digest(document.as_bytes()))
}

fn text_param(value: &str) -> SourceResult<String> {
    let quoted = serde_json::to_string(value).map_err(|_| invalid_result())?;
    Ok(format!(r#"{{"type":"text","value":{quoted}}}"#))
}

fn document_error(error: CodecError) -> SourceError {
    match error {
        CodecError::ByteLimit
        | CodecError::NodeLimit
        | CodecError::IntegerLimit
        | CodecError::Resource => SourceError::new(
            "StorageLimitExceeded",
            "provider registry document exceeds the supported limits",
        ),
        CodecError::Invalid | CodecError::NonFinite => {
            SourceError::value("provider registry document is invalid")
        }
    }
}

fn invalid_result() -> SourceError {
    SourceError::new(
        "StorageError",
        "provider registry storage returned an invalid result",
    )
}

fn storage_error(error: Error) -> SourceError {
    // Keep native messages out of the public Registry error. A Core storage
    // conflict or an unknown outcome is separate from a confirmed stale row.
    let (exception, message) = match error.code {
        ErrorCode::Denied => ("PermissionError", "provider registry storage was denied"),
        ErrorCode::ApprovalRequired => (
            "ApprovalRequired",
            "provider registry storage requires approval",
        ),
        ErrorCode::Cancelled => (
            "StorageCancelled",
            "provider registry storage was cancelled",
        ),
        ErrorCode::LimitExceeded => (
            "StorageLimitExceeded",
            "provider registry storage limit was exceeded",
        ),
        ErrorCode::Conflict => ("StorageConflict", "provider registry storage conflict"),
        ErrorCode::Unavailable => (
            "StorageUnavailable",
            "provider registry storage is unavailable",
        ),
        _ => ("StorageError", "provider registry storage operation failed"),
    };
    SourceError::new(exception, message)
}
