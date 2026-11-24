//! Bounded Unix FD reads through the original native inspection DATA writer.

use std::{fs::File, io, os::fd::AsRawFd};
use tobkiri_wasm::{NativeDataIoError, NativeInspectionOutput};

const BASE64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

#[cfg(test)]
#[path = "fd_read_tests.rs"]
mod writer_tests;

/// Read at most `requested` bytes from the caller's original file descriptor,
/// then append `prefix + base64(actual bytes) + suffix` to the inspection output.
///
/// The maximum frame is prepaid; short reads and EOF are preserved. A zero-byte
/// request still calls read, and only EINTR is retried. Caller retains the File.
/// It must admit the actual source/FD/phase and keep ownership through the
/// original worker Join. This helper grants no filesystem authority. A failed
/// publication does not rewind the FD and must not cause a read to be replayed.
/// Python signal-handler exception compatibility remains a separate proof.
pub fn write_fd_read(
    output: &mut NativeInspectionOutput<'_>,
    file: &File,
    requested: usize,
    prefix: &[u8],
    suffix: &[u8],
) -> Result<Result<usize, io::Error>, NativeDataIoError> {
    let Some(groups) = requested.checked_add(2).map(|n| n / 3) else {
        return force_capacity(output);
    };
    let Some(encoded_maximum) = groups.checked_mul(4) else {
        return force_capacity(output);
    };
    let Some(maximum) = prefix
        .len()
        .checked_add(encoded_maximum)
        .and_then(|n| n.checked_add(suffix.len()))
    else {
        return force_capacity(output);
    };
    if requested > isize::MAX as usize {
        return force_capacity(output);
    }

    let mut actual_read = None;
    let mut impossible_size = false;
    let result = output.write_chunk_checked(maximum, |tail, checkpoint| {
        tail[..prefix.len()].copy_from_slice(prefix);

        // The raw region is right-aligned in the prepaid tail. Its lead over
        // the encoded destination covers one byte of expansion per 3-byte
        // group, so forward encoding cannot overwrite a future unread group.
        let raw_start = tail.len() - requested;
        let read = read_fd_attempts(file.as_raw_fd(), &mut tail[raw_start..], || {
            checkpoint.charge_work(1).is_ok()
        });
        let count = match read {
            ReadOutcome::CheckpointFailed => return Ok(0),
            ReadOutcome::Read(Err(error)) => return Err(error),
            ReadOutcome::Read(Ok(count)) => count,
        };
        // Account the actual native byte-processing work before encoding. A
        // failure here never replays the successful read or rewinds its FD.
        if checkpoint.charge_work(count as u64).is_err() {
            return Ok(0);
        }

        let Some(encoded_len) = encoded_len(count) else {
            impossible_size = true;
            return Ok(0);
        };
        let Some(frame_len) = prefix
            .len()
            .checked_add(encoded_len)
            .and_then(|n| n.checked_add(suffix.len()))
        else {
            impossible_size = true;
            return Ok(0);
        };

        let Some(written) = encode_base64_forward_in_place(tail, raw_start, count, prefix.len())
        else {
            impossible_size = true;
            return Ok(0);
        };
        debug_assert_eq!(written, encoded_len);
        tail[prefix.len() + written..frame_len].copy_from_slice(suffix);

        if checkpoint.charge_work(1).is_err() {
            return Ok(0);
        }
        actual_read = Some(count);
        Ok(frame_len)
    });

    if impossible_size {
        return force_capacity(output);
    }
    result.map(|frame_result| frame_result.map(|_| actual_read.unwrap_or(0)))
}

fn force_capacity(
    output: &mut NativeInspectionOutput<'_>,
) -> Result<Result<usize, io::Error>, NativeDataIoError> {
    match output.write_chunk_checked(usize::MAX, |_, _| Ok::<usize, io::Error>(0)) {
        Err(error) => Err(error),
        Ok(_) => Err(NativeDataIoError::Capacity),
    }
}

fn encoded_len(bytes: usize) -> Option<usize> {
    bytes
        .checked_add(2)
        .map(|n| n / 3)
        .and_then(|groups| groups.checked_mul(4))
}

enum ReadOutcome {
    CheckpointFailed,
    Read(io::Result<usize>),
}

fn read_fd_attempts<F>(fd: libc::c_int, raw: &mut [u8], mut checkpoint: F) -> ReadOutcome
where
    F: FnMut() -> bool,
{
    loop {
        if !checkpoint() {
            return ReadOutcome::CheckpointFailed;
        }
        // SAFETY: `raw` is a writable borrowed slice with `raw.len()` bytes;
        // the caller keeps the borrowed FD alive for this syscall.
        let count = unsafe { libc::read(fd, raw.as_mut_ptr().cast(), raw.len()) };
        if count >= 0 {
            // `ssize_t` is pointer-sized; a nonnegative result fits `usize`.
            // POSIX read guarantees the result is no greater than `raw.len()`.
            return ReadOutcome::Read(Ok(count as usize));
        }

        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return ReadOutcome::Read(Err(error));
        }
    }
}

/// Encode from a right-aligned raw region into an earlier destination in the
/// same buffer. The gap must be at least the number of 3-byte groups: at each
/// step the destination grows by one byte relative to the next unread group.
fn encode_base64_forward_in_place(
    buffer: &mut [u8],
    input_start: usize,
    input_len: usize,
    output_start: usize,
) -> Option<usize> {
    let encoded_len = encoded_len(input_len)?;
    let input_end = input_start.checked_add(input_len)?;
    let output_end = output_start.checked_add(encoded_len)?;
    let groups = input_len.checked_add(2)? / 3;
    if input_end > buffer.len()
        || output_end > buffer.len()
        || input_start.checked_sub(output_start)? < groups
    {
        return None;
    }

    let mut consumed = 0usize;
    let mut written = 0usize;
    while consumed < input_len {
        let remaining = input_len - consumed;
        let source = input_start.checked_add(consumed)?;
        let destination = output_start.checked_add(written)?;

        // Load this full 1–3 byte group before writing its four encoded bytes.
        let first = *buffer.get(source)?;
        let second = if remaining > 1 {
            *buffer.get(source.checked_add(1)?)?
        } else {
            0
        };
        let third = if remaining > 2 {
            *buffer.get(source.checked_add(2)?)?
        } else {
            0
        };
        let encoded = [
            BASE64[(first >> 2) as usize],
            BASE64[(((first & 0x03) << 4) | (second >> 4)) as usize],
            if remaining > 1 {
                BASE64[(((second & 0x0f) << 2) | (third >> 6)) as usize]
            } else {
                b'='
            },
            if remaining > 2 {
                BASE64[(third & 0x3f) as usize]
            } else {
                b'='
            },
        ];
        for (offset, byte) in encoded.into_iter().enumerate() {
            *buffer.get_mut(destination.checked_add(offset)?)? = byte;
        }
        consumed = consumed.checked_add(remaining.min(3))?;
        written = written.checked_add(4)?;
    }
    Some(written)
}

#[cfg(test)]
mod tests {
    use super::{ReadOutcome, encode_base64_forward_in_place, read_fd_attempts};
    use base64::{Engine, engine::general_purpose::STANDARD};
    use std::{
        io::{Seek, SeekFrom, Write},
        os::fd::AsRawFd,
    };

    const MAX_RAW: usize = 512;
    const PREFIX: usize = 7;
    const MAX_ENCODED: usize = MAX_RAW.div_ceil(3) * 4;
    const FRAME_CAPACITY: usize = PREFIX + MAX_ENCODED + 5;

    #[test]
    fn in_place_encoding_handles_short_reads_padding_and_overlap() {
        let mut raw = [0u8; MAX_RAW];
        let mut frame = [0u8; FRAME_CAPACITY];
        let mut expected = [0u8; MAX_ENCODED];
        for (index, byte) in raw.iter_mut().enumerate() {
            *byte = (index.wrapping_mul(31).wrapping_add(7)) as u8;
        }

        for requested in 0..=MAX_RAW {
            for actual in [0, requested, requested.saturating_sub(1), requested / 2] {
                for (prefix, suffix) in [(0, 0), (PREFIX, 5)] {
                    let encoded_len = STANDARD
                        .encode_slice(&raw[..actual], &mut expected)
                        .unwrap();
                    frame.fill(0x5a);
                    let maximum = prefix + super::encoded_len(requested).unwrap() + suffix;
                    let raw_start = maximum - requested;
                    frame[raw_start..raw_start + requested].copy_from_slice(&raw[..requested]);
                    let written = encode_base64_forward_in_place(
                        &mut frame[..maximum],
                        raw_start,
                        actual,
                        prefix,
                    )
                    .expect("maximum-request layout covers shorter reads");
                    assert_eq!(
                        written, encoded_len,
                        "requested={requested}, actual={actual}"
                    );
                    assert_eq!(&frame[prefix..prefix + written], &expected[..encoded_len]);
                }
            }
        }
    }

    #[test]
    fn direct_fd_reads_preserve_chunks_eof_zero_count_and_errors() {
        let mut file = tempfile::tempfile().expect("temporary file");
        file.write_all(b"abcdef").expect("write fixture");
        file.seek(SeekFrom::Start(0)).expect("rewind fixture");
        let mut attempts = 0usize;
        let mut buffer = [0u8; 8];

        let first = read_fd_attempts(file.as_raw_fd(), &mut buffer[..4], || true);
        assert!(matches!(first, ReadOutcome::Read(Ok(4))));
        assert_eq!(&buffer[..4], b"abcd");
        assert_eq!(file.stream_position().expect("position"), 4);

        let second = read_fd_attempts(file.as_raw_fd(), &mut buffer, || true);
        assert!(matches!(second, ReadOutcome::Read(Ok(2))));
        assert_eq!(&buffer[..2], b"ef");
        assert_eq!(file.stream_position().expect("position"), 6);

        assert!(
            matches!(
                read_fd_attempts(-1, &mut [], || true),
                ReadOutcome::Read(Err(error)) if error.raw_os_error() == Some(libc::EBADF)
            ),
            "an empty read must still validate the actual descriptor in the kernel"
        );

        let eof = read_fd_attempts(file.as_raw_fd(), &mut buffer, || true);
        assert!(matches!(eof, ReadOutcome::Read(Ok(0))));
        assert_eq!(file.stream_position().expect("position"), 6);

        let zero = read_fd_attempts(file.as_raw_fd(), &mut [], || {
            attempts += 1;
            true
        });
        assert!(matches!(zero, ReadOutcome::Read(Ok(0))));
        assert_eq!(file.stream_position().expect("position"), 6);
        assert_eq!(attempts, 1, "zero-byte requests still invoke read");

        let error = read_fd_attempts(-1, &mut buffer, || {
            attempts += 1;
            true
        });
        match error {
            ReadOutcome::Read(Err(error)) => {
                assert_eq!(error.raw_os_error(), Some(libc::EBADF));
            }
            _ => panic!("invalid descriptor must preserve its syscall error"),
        }
        assert_eq!(attempts, 2, "non-EINTR errors are not retried");
        assert_eq!(file.stream_position().expect("position"), 6);
    }
}
