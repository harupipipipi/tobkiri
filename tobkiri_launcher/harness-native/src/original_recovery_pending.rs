//! Read the original pending record before crossing into Authority again.
use super::*;

pub(super) const NAME: &str = "pending.json";
const SCHEMA: &str = "io.tobkiri.defaultspack-pending-activation.v1";
const KEYS: [&str; 7] = [
    "schema",
    "reservation_id",
    "activation_id",
    "envelope_path",
    "envelope_digest",
    "workspace_digest",
    "fencing_token",
];

/// Read and validate the record while its caller holds the activation lock.
pub(super) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    workspace_digest: &Digest,
) -> Result<()> {
    let bytes = state
        .read(op, NAME)
        .map_err(|error| read_error(op, error))?;
    let doc = json::decode_strict(&bytes, op).map_err(|error| read_error(op, error))?;
    if !exact(&doc, doc.root, &KEYS)
        || !doc
            .get(doc.root, "schema")
            .is_some_and(|id| doc.string_is(id, SCHEMA))
        || !doc
            .get(doc.root, "workspace_digest")
            .is_some_and(|id| doc.string_is(id, workspace_digest.as_str()))
    {
        return Err(raw(APP_DENIED, "pending activation journal is invalid"));
    }
    // The other values are interpreted after this shape check in the original.
    Ok(())
}

fn read_error<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    error: OriginalAuthorityError,
) -> OriginalAuthorityError {
    const PREFIX: &str = "pending activation journal is unavailable: ";
    let stage = OriginalAuthorityStage::Source;
    let mut strerror = [0_u8; 256];
    let mut os_error = None;
    let scratch_bytes = match error.class {
        RAW_IO => {
            let Some(OriginalAuthorityCause::Io(io)) = &error.cause else {
                return error;
            };
            let Some(errno) = io.errno else {
                return error;
            };
            // Other I/O filenames are nested below SecurePersistenceError.
            if !matches!(io.filename.as_deref(), Some(NAME) | None)
                || unsafe { libc::strerror_r(errno, strerror.as_mut_ptr().cast(), strerror.len()) }
                    != 0
            {
                return OriginalAuthorityError::unsupported(stage, "pending journal I/O message");
            }
            let Some(text) = std::ffi::CStr::from_bytes_until_nul(&strerror)
                .ok()
                .and_then(|text| text.to_str().ok())
            else {
                return OriginalAuthorityError::unsupported(stage, "pending journal I/O message");
            };
            let suffix = if io.filename.is_some() {
                ": 'pending.json'"
            } else {
                ""
            };
            os_error = Some((errno, text, suffix));
            text.len() + suffix.len() + 32
        }
        "builtins.FileNotFoundError"
        | "tobkiri_protocol.errors.ProtocolError"
        | "tobkiri_protocol.errors.CanonicalizationError"
        | SECURE_PERSISTENCE_ERROR => error.message.len(),
        _ => return error,
    };
    let _message = match op.reserve(stage, scratch_bytes.saturating_add(PREFIX.len() + 512)) {
        Ok(message) => message,
        Err(error) => return error,
    };
    let message = match os_error {
        Some((errno, text, suffix)) => format!("{PREFIX}[Errno {errno}] {text}{suffix}"),
        None => format!("{PREFIX}{}", error.message),
    };
    caused(raw(APP_DENIED, message), error)
}
