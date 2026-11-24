//! profile_capture's fixed five-field App snapshot, inside its Store scope.
//! This is protocol canonicalization; the old authority-record canonical form
//! and Core/Gate permission state remain separate.
use super::*;
use tobkiri_canon::{CanonError, MAX_SAFE_INTEGER, Variant, encode};

fn canonical_error(message: &'static str) -> OriginalAuthorityError {
    OriginalAuthorityError {
        stage: OriginalAuthorityStage::Snapshot,
        class: "tobkiri_protocol.errors.CanonicalizationError",
        message: message.into(),
        cause: None,
        context: None,
        suppressed: false,
    }
}
pub(super) fn digest<S: OriginalAuthorityOwner>(
    op: &Operation<'_, S>,
    material: &OriginalAuthorityMaterial,
    bundle_lock_digest: &str,
) -> Result<Digest> {
    let (epoch, reason) = match material {
        OriginalAuthorityMaterial::Genesis {
            epoch,
            reason_digest,
        } => (epoch, reason_digest.as_str()),
        OriginalAuthorityMaterial::Existing {
            record_epoch,
            reason_digest,
            ..
        } => (record_epoch, reason_digest.as_str()),
    };
    // _validate_value visits schema, then the SECOND epoch read, before bundle
    // data. This rejection belongs here, never at either integer read or after
    // close. Integer material has already been normalized without a range cap.
    let maximum = MAX_SAFE_INTEGER.to_string();
    if epoch.decimal().trim_start_matches('-').len() > maximum.len()
        || (epoch.decimal().trim_start_matches('-').len() == maximum.len()
            && epoch.decimal().trim_start_matches('-') > maximum.as_str())
    {
        return Err(canonical_error(
            "integer exceeds I-JSON safe range; use a canonical decimal string",
        ));
    }
    // All five fields and their order are source-defined. The caller's string
    // is copied unchanged; even a non-digest string is valid canonical DATA.
    // Prepay duplicate input strings, BTree storage, encoded Vec's growth and
    // source-size check. This reservation joins the original account, not a
    // new capacity frame or an allowance increase.
    let encoded_string = bundle_lock_digest
        .chars()
        .try_fold(2usize, |size, ch| {
            let width = match ch {
                '"' | '\\' | '\n' | '\r' | '\t' | '\u{8}' | '\u{c}' => 2,
                '\0'..='\u{1f}' => 6,
                _ => ch.len_utf8(),
            };
            size.checked_add(width)
        })
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Snapshot))?;
    let size = encoded_string
        .checked_add(512)
        .and_then(|n| n.checked_mul(3))
        .and_then(|n| n.checked_add(bundle_lock_digest.len()))
        .and_then(|n| n.checked_add(16384))
        .ok_or_else(|| OriginalAuthorityError::capacity(OriginalAuthorityStage::Snapshot))?;
    let capacity = op.reserve(OriginalAuthorityStage::Snapshot, size)?;
    let integer = CanonValue::BigInt(epoch.decimal().into());
    let value = object([
        (
            "schema",
            CanonValue::Str("io.tobkiri.profile-authority-snapshot.v1".into()),
        ),
        ("security_epoch", integer),
        (
            "security_epoch_reason_digest",
            CanonValue::Str(reason.into()),
        ),
        (
            "bundle_lock_digest",
            CanonValue::Str(bundle_lock_digest.into()),
        ),
        ("grant_import", CanonValue::Str("none".into())),
    ]);
    let encoded = encode(&value, Variant::StrictV1).map_err(|error| match error {
        CanonError::Limit("4 MiB") => canonical_error("canonical JSON exceeds size limit"),
        _ => OriginalAuthorityError::unsupported(
            OriginalAuthorityStage::Snapshot,
            "fixed protocol snapshot canonicalization",
        ),
    })?;
    let digest = Digest::hash(&encoded);
    drop(encoded);
    drop(value);
    drop(capacity);
    Ok(digest)
}
