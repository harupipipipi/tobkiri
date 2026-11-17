//! Read the original pending record before crossing into Authority again.
use super::*;

pub(in crate::original_authority::io::publication) const NAME: &str = "pending.json";
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
pub(in crate::original_authority::io::publication) fn read<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    workspace_digest: &Digest,
) -> Result<Funded<Document>> {
    let doc = read_state(op, state, Path::new(NAME), "pending activation journal")?;
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
    Ok(doc)
}

pub(in crate::original_authority::io::publication) fn read_state<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    path: &Path,
    label: &'static str,
) -> Result<Funded<Document>> {
    read_state_with(op, state, path, label, |bytes| {
        json::decode_strict(bytes, op)
    })
}

pub(in crate::original_authority::io::publication) fn read_state_with<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    state: &Directory,
    path: &Path,
    label: &'static str,
    decode: impl FnOnce(&[u8]) -> Result<Funded<Document>>,
) -> Result<Funded<Document>> {
    let bytes = state
        .read_relative(op, path)
        .map_err(|error| read_error(op, label, error))?;
    decode(&bytes).map_err(|error| read_error(op, label, error))
}

fn read_error<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    label: &'static str,
    error: OriginalAuthorityError,
) -> OriginalAuthorityError {
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
            if unsafe { libc::strerror_r(errno, strerror.as_mut_ptr().cast(), strerror.len()) } != 0
            {
                return OriginalAuthorityError::unsupported(stage, "activation record I/O message");
            }
            let Some(text) = std::ffi::CStr::from_bytes_until_nul(&strerror)
                .ok()
                .and_then(|text| text.to_str().ok())
            else {
                return OriginalAuthorityError::unsupported(stage, "activation record I/O message");
            };
            let filename = io.filename.as_deref();
            os_error = Some((errno, text, filename));
            text.len() + filename.map_or(0, |name| name.len().saturating_mul(12)) + 32
        }
        "builtins.FileNotFoundError"
        | "tobkiri_protocol.errors.ProtocolError"
        | "tobkiri_protocol.errors.CanonicalizationError"
        | SECURE_PERSISTENCE_ERROR => error.message.len(),
        _ => return error,
    };
    let _message = match op.reserve(stage, scratch_bytes.saturating_add(label.len() + 512)) {
        Ok(message) => message,
        Err(error) => return error,
    };
    let message = match os_error {
        Some((errno, text, Some(filename))) => format!(
            "{label} is unavailable: [Errno {errno}] {text}: {}",
            python_repr(filename)
        ),
        Some((errno, text, None)) => format!("{label} is unavailable: [Errno {errno}] {text}"),
        None => format!("{label} is unavailable: {}", error.message),
    };
    caused(raw(APP_DENIED, message), error)
}
