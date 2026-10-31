//! One original Host operation over the selected old application state.
//!
//! No SQL, pathname, key, success witness or approval is supplied by a guest.
//! The two epoch reads stay distinct. These values are App candidate DATA,
//! never a Core Profile epoch, grant, or permission to execute a Profile.
use std::{path::Path, sync::Arc, time::Instant};
use tobkiri_canon::{CanonValue, Digest};
use tobkiri_gate::{CapacityKind, CoreCancellation, NativeSetupInvocation, NativeSetupReservation};

#[path = "original_authority_crypto.rs"]
mod crypto;
#[path = "original_authority_io.rs"]
mod io;
#[path = "original_authority_json.rs"]
mod json;
#[path = "original_authority_lease.rs"]
mod lease;
#[path = "original_authority_schema.rs"]
mod schema;

/// Implemented only by the trusted composition retaining the actual selected
/// AppConfig/ShutdownState. A path value or native allocation lease is not this
/// operation's source capability. This is separate from module metadata policy.
pub trait OriginalAuthorityOwner: Send + Sync {
    fn with_original_user_data<R>(
        &self,
        use_root: impl FnOnce(&Path) -> R,
    ) -> Result<R, OriginalAuthorityError>;
    fn check_original_owner(&self) -> Result<(), OriginalAuthorityError>;
}

/// Lossless normalized Python integer. Its range is deliberately independent
/// of Core/Gate integers and of the later protocol canonicalization boundary.
#[derive(Debug, PartialEq, Eq)]
pub struct OriginalAuthorityEpoch(String);
impl OriginalAuthorityEpoch {
    pub fn decimal(&self) -> &str {
        &self.0
    }
}

#[derive(Debug)]
pub enum OriginalAuthorityMaterial {
    /// Only the original absent-file branch; no directory, DB, or key created.
    Genesis {
        epoch: OriginalAuthorityEpoch,
        reason_digest: Digest,
    },
    Existing {
        epoch: OriginalAuthorityEpoch,
        record_epoch: OriginalAuthorityEpoch,
        advanced_at: f64,
        reason_digest: String,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OriginalAuthorityStage {
    Source,
    Process,
    Path,
    Key,
    Guard,
    Connect,
    Initialize,
    Audit,
    Migration,
    Epoch,
    EpochRecord,
    Close,
}

/// Actual finite source error categories, not flattened anyhow/debug strings.
#[derive(Debug)]
pub enum OriginalAuthorityCause {
    Io(OriginalAuthorityIoError),
    Sqlite {
        code: i32,
        message: String,
    },
    SecurePath {
        message: &'static str,
        context: Option<Box<OriginalAuthorityCause>>,
    },
    InvalidToken,
    InvalidKey,
    InvalidRecord(String),
    Validation(&'static str),
    Capacity,
    Cancelled,
    Deadline,
    SourceUnavailable,
    Unsupported(&'static str),
    Python {
        class: &'static str,
        message: String,
        cause: Option<Box<OriginalAuthorityCause>>,
        context: Option<Box<OriginalAuthorityCause>>,
        suppressed: bool,
    },
}
#[derive(Debug)]
pub struct OriginalAuthorityIoError {
    pub kind: std::io::ErrorKind,
    pub errno: Option<i32>,
    pub filename: Option<String>,
    pub context: Option<Box<OriginalAuthorityCause>>,
}
impl OriginalAuthorityIoError {
    fn kind(&self) -> std::io::ErrorKind {
        self.kind
    }
}
#[derive(Debug)]
pub struct OriginalAuthorityError {
    pub stage: OriginalAuthorityStage,
    pub class: &'static str,
    pub message: String,
    pub cause: Option<OriginalAuthorityCause>,
    pub context: Option<Box<OriginalAuthorityError>>,
    pub suppressed: bool,
}
impl std::fmt::Display for OriginalAuthorityError {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(out, "{}: {}", self.class, self.message)
    }
}
impl std::error::Error for OriginalAuthorityError {}
impl OriginalAuthorityError {
    pub fn unavailable() -> Self {
        let mut error = Self::new(
            OriginalAuthorityStage::Source,
            "original application owner is unavailable",
            Some(OriginalAuthorityCause::SourceUnavailable),
        );
        error.class = "tobkiri.native.OriginalAuthorityIntegrationFailure";
        error
    }
    fn new(
        stage: OriginalAuthorityStage,
        message: &'static str,
        cause: Option<OriginalAuthorityCause>,
    ) -> Self {
        Self {
            stage,
            class: "core_runtime.authority.store_contracts.AuthorityStoreError",
            message: message.into(),
            suppressed: cause.is_some(),
            cause,
            context: None,
        }
    }
    fn sql(stage: OriginalAuthorityStage, message: &'static str, error: rusqlite::Error) -> Self {
        match error {
            rusqlite::Error::SqliteFailure(code, Some(text)) => Self::new(
                stage,
                message,
                Some(OriginalAuthorityCause::Sqlite {
                    code: code.extended_code,
                    message: text,
                }),
            ),
            _ => Self::unsupported(stage, "non-SQLite rusqlite error projection"),
        }
    }
    fn raw_sql(stage: OriginalAuthorityStage, error: rusqlite::Error) -> Self {
        match error {
            rusqlite::Error::SqliteFailure(code, Some(message)) => Self {
                stage,
                class: sqlite_class(code.extended_code),
                message,
                cause: None,
                context: None,
                suppressed: false,
            },
            _ => Self::unsupported(stage, "non-SQLite rusqlite error projection"),
        }
    }
    fn capacity(stage: OriginalAuthorityStage) -> Self {
        let mut error = Self::new(
            stage,
            "original authority operation capacity is unavailable",
            Some(OriginalAuthorityCause::Capacity),
        );
        error.class = "tobkiri.native.OriginalAuthorityIntegrationFailure";
        error
    }
    fn unsupported(stage: OriginalAuthorityStage, feature: &'static str) -> Self {
        Self {
            stage,
            class: "tobkiri.native.OriginalAuthorityIntegrationFailure",
            message: "original AuthorityStore source boundary is not implemented".into(),
            cause: Some(OriginalAuthorityCause::Unsupported(feature)),
            context: None,
            suppressed: false,
        }
    }
}
fn sqlite_class(code: i32) -> &'static str {
    match code & 255 {
        2 | 12 => "sqlite3.InternalError",
        7 => "builtins.MemoryError",
        18 => "sqlite3.DataError",
        19 | 20 => "sqlite3.IntegrityError",
        21 | 25 => "sqlite3.InterfaceError",
        1 | 3 | 4 | 5 | 6 | 8 | 9 | 10 | 13 | 14 | 15 | 16 | 17 => "sqlite3.OperationalError",
        _ => "sqlite3.DatabaseError",
    }
}
type Result<T, E = OriginalAuthorityError> = std::result::Result<T, E>;

struct Operation<'a, S> {
    owner: &'a S,
    invocation: &'a NativeSetupInvocation,
    cancellation: &'a CoreCancellation,
    deadline: Instant,
    pid: u32,
    cleanup: Arc<io::CleanupState>,
    #[cfg(test)]
    fixture: Option<&'a tests::Fixture>,
}
impl<S: OriginalAuthorityOwner> Operation<'_, S> {
    fn check(&self, stage: OriginalAuthorityStage) -> Result<()> {
        if std::process::id() != self.pid {
            return Err(OriginalAuthorityError::new(
                stage,
                "authority store cannot be used after fork",
                None,
            ));
        }
        self.invocation
            .allocation_domain()
            .map_err(|_| OriginalAuthorityError::unavailable())?;
        self.owner.check_original_owner()?;
        if self.cancellation.is_cancelled() {
            return Err(OriginalAuthorityError::new(
                stage,
                "original authority operation cancelled",
                Some(OriginalAuthorityCause::Cancelled),
            ));
        }
        if Instant::now() >= self.deadline {
            return Err(OriginalAuthorityError::new(
                stage,
                "original authority operation deadline exceeded",
                Some(OriginalAuthorityCause::Deadline),
            ));
        }
        Ok(())
    }
    fn reserve(
        &self,
        stage: OriginalAuthorityStage,
        bytes: usize,
    ) -> Result<NativeSetupReservation> {
        self.check(stage)?;
        self.invocation
            .reserve(
                CapacityKind::VfsBuffer,
                u64::try_from(bytes.max(1)).map_err(|_| OriginalAuthorityError::capacity(stage))?,
            )
            .map_err(|_| OriginalAuthorityError::capacity(stage))
    }
}

// Payload drops before its move-only physical capacity custody. This type is
// never part of a detached caller result or a guest interface.
struct Funded<T> {
    value: T,
    _capacity: NativeSetupReservation,
}
impl<T> std::ops::Deref for Funded<T> {
    type Target = T;
    fn deref(&self) -> &T {
        &self.value
    }
}
impl<T> std::ops::DerefMut for Funded<T> {
    fn deref_mut(&mut self) -> &mut T {
        &mut self.value
    }
}

/// Execute only after fixed native admission. The same source owner and actual
/// invocation survive open, both reads, close and all failure cleanup. The
/// absence decision is made here, never supplied by a serialized caller.
pub fn read_original_authority<S: OriginalAuthorityOwner>(
    owner: &S,
    invocation: &NativeSetupInvocation,
    cancellation: &CoreCancellation,
) -> std::result::Result<OriginalAuthorityRead, OriginalAuthorityAdmission> {
    let mut read = OriginalAuthorityRead::new(invocation)?;
    read.run(owner, cancellation)?;
    Ok(read)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OriginalAuthorityAdmission {
    Capacity { code: &'static str },
    AlreadyAttempted,
}
impl std::fmt::Display for OriginalAuthorityAdmission {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(out, "{self:?}")
    }
}
impl std::error::Error for OriginalAuthorityAdmission {}

/// Worker-internal read-to-detach custody. No budget getter, callback argument,
/// connection, raw key, or material-consuming escape method is exposed.
/// Production drops this BEFORE sending the separately prepaid detached DATA.
pub struct OriginalAuthorityRead {
    result: Option<Result<OriginalAuthorityMaterial>>,
    cleanup: Arc<io::CleanupState>,
    attempted: bool,
    // Result/retained native IO are destroyed before their capacity custody.
    capacity: NativeSetupReservation,
    invocation: NativeSetupInvocation,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct OriginalAuthorityCloseReceipt {
    pub open_files: usize,
    pub open_connections: usize,
    pub uncertain: bool,
}
impl OriginalAuthorityCloseReceipt {
    pub fn verified(self) -> bool {
        self.open_files == 0 && self.open_connections == 0 && !self.uncertain
    }
}
impl OriginalAuthorityRead {
    pub fn new(
        invocation: &NativeSetupInvocation,
    ) -> std::result::Result<Self, OriginalAuthorityAdmission> {
        let control = invocation
            .control()
            .reserve(tobkiri_gate::ControlKind::Context, 4096)
            .map_err(|_| OriginalAuthorityAdmission::Capacity {
                code: "native-control",
            })?;
        // Fixed native SQL is limited to 1 MiB. This pre-admits two overlapping
        // diagnostic buffers plus the bounded epoch/exception record before
        // any source operation can construct them. This is NOT a new budget.
        let capacity = invocation
            .reserve(CapacityKind::VfsBuffer, 2 * 1024 * 1024 + 16384)
            .map_err(|error| OriginalAuthorityAdmission::Capacity { code: error.code() })?;
        Ok(Self {
            result: None,
            cleanup: Arc::new(io::CleanupState::new(control)),
            attempted: false,
            capacity,
            invocation: invocation.clone(),
        })
    }
    pub fn run<S: OriginalAuthorityOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
    ) -> std::result::Result<(), OriginalAuthorityAdmission> {
        #[cfg(test)]
        {
            self.run_inner(owner, cancellation, None)
        }
        #[cfg(not(test))]
        {
            self.run_inner(owner, cancellation)
        }
    }
    fn run_inner<S: OriginalAuthorityOwner>(
        &mut self,
        owner: &S,
        cancellation: &CoreCancellation,
        #[cfg(test)] fixture: Option<&tests::Fixture>,
    ) -> std::result::Result<(), OriginalAuthorityAdmission> {
        if self.attempted {
            return Err(OriginalAuthorityAdmission::AlreadyAttempted);
        }
        self.attempted = true;
        let deadline = self
            .invocation
            .deadline()
            .map_err(|error| OriginalAuthorityAdmission::Capacity { code: error.code() })?;
        let operation = Operation {
            owner,
            invocation: &self.invocation,
            cancellation,
            deadline,
            pid: std::process::id(),
            cleanup: self.cleanup.clone(),
            #[cfg(test)]
            fixture,
        };
        let outcome = operation
            .check(OriginalAuthorityStage::Source)
            .and_then(|()| {
                owner
                    .with_original_user_data(|root| io::read_selected(&operation, root))
                    .and_then(|result| result)
            });
        drop(operation);
        let bytes = outcome_bytes(&outcome);
        self.result = Some(outcome);
        // The scratch temporaries have physically died. Only the actual
        // retained result's String capacities and owned record allocations
        // remain charged until its worker-side detach/drop.
        self.capacity
            .resize(bytes.max(1) as u64)
            .map_err(|error| OriginalAuthorityAdmission::Capacity { code: error.code() })?;
        Ok(())
    }
    pub fn result(&self) -> Option<Result<&OriginalAuthorityMaterial, &OriginalAuthorityError>> {
        self.result.as_ref().map(|result| result.as_ref())
    }
    pub fn cleanup_verified(&self) -> bool {
        self.cleanup.verified()
    }
    pub fn close_receipt(&self) -> OriginalAuthorityCloseReceipt {
        self.cleanup.receipt()
    }
    /// Kept by the native coordinator ONLY when actual close was uncertain.
    /// It is never sent with detached DATA or used as a success witness.
    pub fn uncertain_cleanup(&self) -> Option<OriginalAuthorityCleanup> {
        (!self.cleanup.verified()).then(|| OriginalAuthorityCleanup(self.cleanup.clone()))
    }
}
pub struct OriginalAuthorityCleanup(Arc<io::CleanupState>);
impl OriginalAuthorityCleanup {
    pub fn is_verified(&self) -> bool {
        self.0.verified()
    }
}
fn outcome_bytes(value: &Result<OriginalAuthorityMaterial>) -> usize {
    fn cause(value: &OriginalAuthorityCause) -> usize {
        match value {
            OriginalAuthorityCause::Sqlite { message, .. }
            | OriginalAuthorityCause::InvalidRecord(message) => message.capacity(),
            OriginalAuthorityCause::SecurePath { context, .. } => context
                .as_ref()
                .map(|value| std::mem::size_of::<OriginalAuthorityCause>() + cause(value))
                .unwrap_or(0),
            OriginalAuthorityCause::Io(value) => {
                value.filename.as_ref().map(String::capacity).unwrap_or(0)
                    + value
                        .context
                        .as_ref()
                        .map(|value| std::mem::size_of::<OriginalAuthorityCause>() + cause(value))
                        .unwrap_or(0)
            }
            OriginalAuthorityCause::Python {
                message,
                cause: source,
                context,
                ..
            } => {
                message.capacity()
                    + source
                        .as_ref()
                        .map(|value| std::mem::size_of::<OriginalAuthorityCause>() + cause(value))
                        .unwrap_or(0)
                    + context
                        .as_ref()
                        .map(|value| std::mem::size_of::<OriginalAuthorityCause>() + cause(value))
                        .unwrap_or(0)
            }
            _ => 0,
        }
    }
    fn error(value: &OriginalAuthorityError) -> usize {
        std::mem::size_of::<OriginalAuthorityError>()
            + value.message.capacity()
            + value.cause.as_ref().map(cause).unwrap_or(0)
            + value
                .context
                .as_ref()
                .map(|value| error(value))
                .unwrap_or(0)
    }
    match value {
        Ok(OriginalAuthorityMaterial::Genesis { epoch, .. }) => {
            std::mem::size_of::<OriginalAuthorityMaterial>() + epoch.0.capacity()
        }
        Ok(OriginalAuthorityMaterial::Existing {
            epoch,
            record_epoch,
            reason_digest,
            ..
        }) => {
            std::mem::size_of::<OriginalAuthorityMaterial>()
                + epoch.0.capacity()
                + record_epoch.0.capacity()
                + reason_digest.capacity()
        }
        Err(value) => error(value),
    }
}

#[cfg(test)]
#[path = "original_authority_tests.rs"]
mod tests;

fn genesis() -> OriginalAuthorityMaterial {
    // This is authority.v4_models.authority_digest, not protocol canonical_digest.
    let reason_digest = Digest::hash(br#"{"reason":"genesis"}"#);
    OriginalAuthorityMaterial::Genesis {
        epoch: OriginalAuthorityEpoch("1".into()),
        reason_digest,
    }
}

fn integer(value: &str, stage: OriginalAuthorityStage) -> Result<OriginalAuthorityEpoch> {
    let mut text = value.trim_matches(python_space);
    let negative = text.starts_with('-');
    if text.starts_with(['+', '-']) {
        text = &text[1..];
    }
    let mut previous_digit = false;
    let mut count = 0;
    for ch in text.chars() {
        if ch == '_' && previous_digit {
            previous_digit = false;
            continue;
        }
        let Some(_) = decimal(ch) else {
            return Err(invalid_integer(value, stage, None));
        };
        previous_digit = true;
        count += 1;
    }
    if !previous_digit || count > 4300 {
        return Err(invalid_integer(
            value,
            stage,
            (count > 4300).then_some(count),
        ));
    }
    let mut output = String::new();
    output
        .try_reserve_exact(count)
        .map_err(|_| OriginalAuthorityError::capacity(stage))?;
    for ch in text.chars() {
        if let Some(digit) = decimal(ch) {
            output.push(char::from(b'0' + digit));
        }
    }
    let trimmed = output.trim_start_matches('0');
    if stage == OriginalAuthorityStage::Epoch && (negative || trimmed.is_empty()) {
        return Err(OriginalAuthorityError::new(
            stage,
            "security epoch is unavailable",
            None,
        ));
    }
    Ok(OriginalAuthorityEpoch(if trimmed.is_empty() {
        "0".into()
    } else if negative {
        format!("-{trimmed}")
    } else {
        trimmed.to_owned()
    }))
}
fn invalid_integer(
    value: &str,
    stage: OriginalAuthorityStage,
    digits: Option<usize>,
) -> OriginalAuthorityError {
    let message = match digits {
        Some(digits) => format!(
            "Exceeds the limit (4300 digits) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
        ),
        None => format!(
            "invalid literal for int() with base 10: {}",
            python_repr(value)
        ),
    };
    if stage == OriginalAuthorityStage::EpochRecord {
        return OriginalAuthorityError::new(
            stage,
            "security epoch metadata is invalid",
            Some(python_error("builtins.ValueError", message)),
        );
    }
    OriginalAuthorityError {
        stage,
        class: "builtins.ValueError",
        message,
        cause: None,
        context: None,
        suppressed: false,
    }
}
fn python_error(class: &'static str, message: impl Into<String>) -> OriginalAuthorityCause {
    OriginalAuthorityCause::Python {
        class,
        message: message.into(),
        cause: None,
        context: None,
        suppressed: false,
    }
}
fn python_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for value in value.chars().take(200) {
        match value {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            value if value == quote => {
                out.push('\\');
                out.push(value);
            }
            value if value.is_control() || (python_space(value) && value != ' ') => {
                use std::fmt::Write;
                if (value as u32) < 256 {
                    let _ = write!(out, "\\x{:02x}", value as u32);
                } else {
                    let _ = write!(out, "\\u{:04x}", value as u32);
                }
            }
            value => out.push(value),
        }
    }
    out.push(quote);
    out
}
fn python_space(ch: char) -> bool {
    matches!(ch, '\u{0009}'..='\u{000d}' | '\u{0020}' | '\u{0085}' |
                  '\u{00a0}' | '\u{1680}' | '\u{2000}'..='\u{200a}' | '\u{2028}' |
                  '\u{2029}' | '\u{202f}' | '\u{205f}' | '\u{3000}')
}
fn python_float(value: &str) -> std::result::Result<f64, ()> {
    let text = value.trim_matches(python_space);
    let mut normalized = String::new();
    let mut chars = text.chars().peekable();
    let mut previous_digit = false;
    while let Some(ch) = chars.next() {
        if ch == '_' {
            if !previous_digit || chars.peek().copied().and_then(decimal).is_none() {
                return Err(());
            }
            previous_digit = false;
        } else if let Some(digit) = decimal(ch) {
            normalized.push(char::from(b'0' + digit));
            previous_digit = true;
        } else if ch.is_ascii() {
            normalized.push(ch);
            previous_digit = false;
        } else {
            return Err(());
        }
    }
    let (negative, unsigned) = match normalized.as_bytes().first() {
        Some(b'-') => (true, &normalized[1..]),
        Some(b'+') => (false, &normalized[1..]),
        _ => (false, normalized.as_str()),
    };
    if unsigned.eq_ignore_ascii_case("nan") {
        return Ok(if negative { -f64::NAN } else { f64::NAN });
    }
    if unsigned.eq_ignore_ascii_case("inf") || unsigned.eq_ignore_ascii_case("infinity") {
        return Ok(if negative {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        });
    }
    // Rust's parser admits these non-finite spellings too; all other letters
    // must be the decimal exponent marker accepted by Python float(str).
    if normalized
        .bytes()
        .any(|byte| byte.is_ascii_alphabetic() && !b"eE".contains(&byte))
    {
        return Err(());
    }
    normalized.parse().map_err(|_| ())
}
// Unicode 15.1 Decimal_Number runs used by CPython 3.13's int(str, 10).
fn decimal(ch: char) -> Option<u8> {
    const STARTS: &[u32] = &[
        0x0030, 0x0660, 0x06f0, 0x07c0, 0x0966, 0x09e6, 0x0a66, 0x0ae6, 0x0b66, 0x0be6, 0x0c66,
        0x0ce6, 0x0d66, 0x0de6, 0x0e50, 0x0ed0, 0x0f20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946,
        0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0,
        0xa9f0, 0xaa50, 0xabf0, 0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0,
        0x112f0, 0x11450, 0x114d0, 0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50,
        0x11da0, 0x11f50, 0x16a60, 0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6,
        0x1e140, 0x1e2f0, 0x1e4f0, 0x1e950, 0x1fbf0,
    ];
    let value = ch as u32;
    STARTS.iter().find_map(|start| {
        value
            .checked_sub(*start)
            .filter(|n| *n < 10)
            .map(|n| n as u8)
    })
}

fn object(entries: impl IntoIterator<Item = (&'static str, CanonValue)>) -> CanonValue {
    CanonValue::Obj(
        entries
            .into_iter()
            .map(|(key, value)| (key.to_owned(), value))
            .collect(),
    )
}
