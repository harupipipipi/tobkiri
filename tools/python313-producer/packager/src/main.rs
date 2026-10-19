// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT
//! Bounded stdin/stdout worker. No filesystem inputs, subprocesses, or networking.
mod upstream_strip;

use anyhow::{Context, Result, bail};
use std::io::{self, Read, Write};
use wasmparser::{Parser, Payload, Validator, WasmFeatures};

const LIMIT: u64 = 128 * 1024 * 1024;

struct BoundedWriter<W> {
    inner: W,
    written: u64,
}
impl<W: Write> Write for BoundedWriter<W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() as u64 > LIMIT - self.written {
            return Err(io::Error::other("compressed output limit exceeded"));
        }
        let count = self.inner.write(bytes)?;
        self.written += count as u64;
        Ok(count)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

fn validate_wasm(bytes: &[u8]) -> Result<()> {
    Validator::new_with_features(WasmFeatures::all())
        .validate_all(bytes)
        .context("invalid Wasm input")?;
    let mut code = false;
    for payload in Parser::new(0).parse_all(bytes) {
        if matches!(payload?, Payload::CodeSectionStart { count, .. } if count > 0) {
            code = true;
        }
    }
    if !code {
        bail!("Wasm asset has no function bodies; empty/stub assets are forbidden");
    }
    Ok(())
}

fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 2 || !matches!(args[1].as_str(), "wasm-strip" | "wasm-raw" | "tar") {
        bail!("expected one mode: wasm-strip, wasm-raw, or tar");
    }
    let mut input = Vec::new();
    io::stdin().lock().take(LIMIT + 1).read_to_end(&mut input)?;
    if input.is_empty() || input.len() as u64 > LIMIT {
        bail!("empty or oversized input");
    }
    if args[1] != "tar" {
        validate_wasm(&input)?;
    }
    let output = if args[1] == "wasm-strip" {
        upstream_strip::strip(&input)?
    } else {
        input
    };
    if args[1] != "tar" {
        validate_wasm(&output)?;
    }
    let writer = BoundedWriter { inner: io::stdout().lock(), written: 0 };
    let mut encoder = zstd::Encoder::new(writer, 19)?;
    encoder.write_all(&output)?;
    encoder.finish()?.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_module_is_rejected() {
        assert!(validate_wasm(b"\0asm\x01\0\0\0").is_err());
    }

    #[test]
    fn malformed_module_is_rejected() {
        assert!(validate_wasm(b"not wasm").is_err());
    }

    #[test]
    fn strip_preserves_linking_sections_and_removes_other_metadata() {
        let mut module = b"\0asm\x01\0\0\0\x01\x04\x01\x60\x00\x00\x03\x02\x01\x00\x0a\x04\x01\x02\x00\x0b".to_vec();
        for name in ["name", "component-type:test", "dylink.0", "producers", ".debug_info"] {
            // All fixture lengths fit single-byte ULEB128; custom payload is empty.
            module.extend_from_slice(&[0, (name.len() + 1) as u8, name.len() as u8]);
            module.extend_from_slice(name.as_bytes());
        }
        validate_wasm(&module).unwrap();
        let stripped = upstream_strip::strip(&module).unwrap();
        validate_wasm(&stripped).unwrap();
        let names: Vec<_> = Parser::new(0).parse_all(&stripped)
            .filter_map(|payload| match payload.unwrap() {
                Payload::CustomSection(section) => Some(section.name().to_owned()),
                _ => None,
            }).collect();
        assert_eq!(names, ["name", "component-type:test", "dylink.0"]);
        assert_eq!(upstream_strip::strip(&stripped).unwrap(), stripped);
    }

    #[test]
    fn synthetic_function_module_is_valid() {
        // One () -> () function, no exports; syntax test, not a real asset claim.
        let module = b"\0asm\x01\0\0\0\x01\x04\x01\x60\x00\x00\x03\x02\x01\x00\x0a\x04\x01\x02\x00\x0b";
        validate_wasm(module).unwrap();
        let stripped = upstream_strip::strip(module).unwrap();
        validate_wasm(&stripped).unwrap();
    }
}
