//! Incremental SSE framing. UTF-8 is decoded after a complete frame is captured.

use std::collections::VecDeque;

/// An SSE decoder with bounded buffer and correct LF/CRLF handling.
#[derive(Default)]
pub struct Decoder {
    pending: Vec<u8>,
    frames: VecDeque<String>,
}

impl Decoder {
    /// Feed a response chunk without assuming token or UTF-8 boundaries.
    pub fn feed(&mut self, bytes: &[u8]) -> Result<(), &'static str> {
        self.pending.extend_from_slice(bytes);
        if self.pending.len() > 1024 * 1024 {
            return Err("SSE frame exceeds one MiB");
        }
        while let Some((end, delimiter)) = boundary(&self.pending) {
            let frame = self.pending.drain(..end + delimiter).collect::<Vec<_>>();
            let text = std::str::from_utf8(&frame[..end]).map_err(|_| "invalid SSE UTF-8")?;
            let data = text.lines().filter_map(|line| line.strip_prefix("data:").map(|value| value.strip_prefix(' ').unwrap_or(value))).collect::<Vec<_>>();
            if !data.is_empty() {
                self.frames.push_back(data.join("\n"));
            }
        }
        Ok(())
    }

    /// Return the oldest complete data frame; comments and other fields ignored.
    pub fn next_frame(&mut self) -> Option<String> {
        self.frames.pop_front()
    }

    /// EOF with an unterminated frame is invalid, not a successful completion.
    pub fn finish(&self) -> Result<(), &'static str> {
        if self.pending.iter().all(u8::is_ascii_whitespace) { Ok(()) } else { Err("truncated SSE frame") }
    }
}

fn boundary(bytes: &[u8]) -> Option<(usize, usize)> {
    for index in 0..bytes.len() {
        if bytes[index..].starts_with(b"\n\n") { return Some((index, 2)); }
        if bytes[index..].starts_with(b"\r\n\r\n") { return Some((index, 4)); }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fragmented_utf8_crlf_and_multiple_data_lines() {
        let mut decoder = Decoder::default();
        let value = ":heartbeat\r\ndata: {\"text\":\"猫\"}\r\n\r\ndata: first\ndata: second\n\n".as_bytes();
        for chunk in value.chunks(1) { decoder.feed(chunk).unwrap(); }
        assert_eq!(decoder.next_frame().unwrap(), "{\"text\":\"猫\"}");
        assert_eq!(decoder.next_frame().unwrap(), "first\nsecond");
        assert!(decoder.next_frame().is_none());
        decoder.finish().unwrap();
    }

    #[test]
    fn truncation_and_frame_limit_fail_closed() {
        let mut decoder = Decoder::default();
        decoder.feed(b"data: unfinished").unwrap();
        assert!(decoder.finish().is_err());
        assert!(decoder.feed(&vec![b'x'; 1024 * 1024]).is_err());
    }
}
