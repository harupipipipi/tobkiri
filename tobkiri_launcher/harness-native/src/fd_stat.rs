//! Bounded Unix FD stat output through the original native inspection DATA writer.

use std::{
    fs::File,
    io::{self, Cursor, Write},
    os::unix::fs::MetadataExt,
};
use tobkiri_wasm::{NativeDataIoError, NativeInspectionOutput};

#[cfg(test)]
#[path = "fd_stat_tests.rs"]
mod writer_tests;

// Empty numeric values mark where each decimal field goes. The separate digit
// bounds cover u32 mode, three u64 fields, and a signed i128 nanosecond value.
const STAT_JSON_LITERAL_BYTES: usize =
    b"{\"kind\":\"stat\",\"st_mode\":,\"st_dev\":,\"st_ino\":,\"st_size\":,\"st_mtime_ns\":}".len();
const MAX_STAT_JSON_BYTES: usize = STAT_JSON_LITERAL_BYTES + 10 + 20 + 20 + 20 + 40;
const _: () = assert!(MAX_STAT_JSON_BYTES < 256);

/// Append `prefix + stat JSON object + suffix` for the caller's original file.
///
/// The maximum frame is prepaid before `File::metadata()` performs its fstat.
/// The caller keeps the actual file, source admission, and phase through the
/// original physical Join. This helper creates no path or FD authority. A
/// failed publication cannot replay metadata work. The JSON field order is the
/// native stat protocol order, and `st_mtime_ns` preserves signed nanoseconds.
pub fn write_fd_stat(
    output: &mut NativeInspectionOutput<'_>,
    file: &File,
    prefix: &[u8],
    suffix: &[u8],
) -> Result<Result<(), io::Error>, NativeDataIoError> {
    let Some(maximum) = prefix
        .len()
        .checked_add(MAX_STAT_JSON_BYTES)
        .and_then(|n| n.checked_add(suffix.len()))
    else {
        return force_capacity(output);
    };
    if maximum > tobkiri_canon::MAX_BYTES {
        return force_capacity(output);
    }

    let mut format_bound_failure = false;
    let result = output.write_chunk_checked(maximum, |tail, checkpoint| {
        tail[..prefix.len()].copy_from_slice(prefix);

        if checkpoint.charge_work(1).is_err() {
            return Ok(0);
        }
        let metadata_result = file.metadata();
        // Always check immediately after the syscall, including when it
        // returned an IO error. The original checkpoint failure takes priority.
        if checkpoint.charge_work(1).is_err() {
            return Ok(0);
        }
        let metadata = match metadata_result {
            Ok(metadata) => metadata,
            Err(error) => return Err(error),
        };

        let mtime_ns = i128::from(metadata.mtime()) * 1_000_000_000
            + i128::from(metadata.mtime_nsec());
        let json_start = prefix.len();
        let mut writer = Cursor::new(&mut tail[json_start..]);
        let formatted = write!(
            writer,
            "{{\"kind\":\"stat\",\"st_mode\":{},\"st_dev\":{},\"st_ino\":{},\"st_size\":{},\"st_mtime_ns\":{}}}",
            metadata.mode(),
            metadata.dev(),
            metadata.ino(),
            metadata.size(),
            mtime_ns,
        );
        let json_len = match usize::try_from(writer.position()) {
            Ok(json_len) => json_len,
            Err(_) => {
                format_bound_failure = true;
                return Ok(0);
            }
        };
        if formatted.is_err() || json_len > MAX_STAT_JSON_BYTES {
            format_bound_failure = true;
            return Ok(0);
        }

        let Some(frame_len) = prefix
            .len()
            .checked_add(json_len)
            .and_then(|n| n.checked_add(suffix.len()))
        else {
            format_bound_failure = true;
            return Ok(0);
        };
        let Some(suffix_start) = prefix.len().checked_add(json_len) else {
            format_bound_failure = true;
            return Ok(0);
        };
        let Some(suffix_end) = suffix_start.checked_add(suffix.len()) else {
            format_bound_failure = true;
            return Ok(0);
        };
        if suffix_end > tail.len() {
            format_bound_failure = true;
            return Ok(0);
        }
        tail[suffix_start..suffix_end].copy_from_slice(suffix);

        if checkpoint.charge_work(1).is_err() {
            return Ok(0);
        }
        Ok(frame_len)
    });

    if format_bound_failure {
        return force_capacity(output);
    }
    result.map(|frame_result| frame_result.map(|_| ()))
}

fn force_capacity(
    output: &mut NativeInspectionOutput<'_>,
) -> Result<Result<(), io::Error>, NativeDataIoError> {
    match output.write_chunk_checked(usize::MAX, |_, _| Ok::<usize, io::Error>(0)) {
        Err(error) => Err(error),
        Ok(_) => Err(NativeDataIoError::Capacity),
    }
}

#[cfg(test)]
mod bound_tests {
    use super::{MAX_STAT_JSON_BYTES, STAT_JSON_LITERAL_BYTES};

    #[test]
    fn static_json_bound_covers_all_integer_widths_and_sign() {
        let actual_maximum = STAT_JSON_LITERAL_BYTES
            + u32::MAX.to_string().len()
            + u64::MAX.to_string().len()
            + u64::MAX.to_string().len()
            + u64::MAX.to_string().len()
            + i128::MIN.to_string().len();
        assert!(MAX_STAT_JSON_BYTES >= actual_maximum);
    }
}
