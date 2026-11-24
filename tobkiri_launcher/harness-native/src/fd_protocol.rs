//! Closed existing-FD protocol, not a source controller or admission grant.
use crate::{write_fd_read, write_fd_stat};
use std::{
    fs::File,
    io::{self, Cursor, Write},
};
use tobkiri_canon::{AllocationError, CoreBuffer, CoreValue};
use tobkiri_gate::NativeSetupInvocation;
use tobkiri_wasm::{NativeDataIoError, NativeInspectionOutput};

#[cfg(test)]
#[path = "fd_protocol_tests.rs"]
mod tests;

const REQUEST: &str = "io.tobkiri.native-inspection-request.v1";
const RESPONSE: &str = "io.tobkiri.native-inspection-response.v1";

/// Transport association chosen once by the actual native bootstrap. No field
/// is a Profile/principal, source permission, Store constructor or fresh budget.
/// This borrows no path and retains the genuine original invocation custody.
#[derive(Clone)]
pub struct NativeFdProtocol {
    invocation: NativeSetupInvocation,
    store: u64,
    generation: u64,
    binding: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NativeFdOutcome {
    Read(usize),
    Stat,
}

#[derive(Clone, Copy)]
enum Operation {
    Read(usize),
    Stat,
}

impl NativeFdProtocol {
    /// Bind native-issued DATA identities to the actual original invocation.
    /// Native bootstrap must retain its real Store/generation and nonreused FD
    /// registry binding. These integers neither create nor prove those owners.
    /// Deep native registry/source state still needs original prepaid custody.
    pub fn new(
        invocation: NativeSetupInvocation,
        store: u64,
        generation: u64,
        binding: u64,
    ) -> Option<Self> {
        if store == 0 || generation == 0 || binding == 0 {
            return None;
        }
        Some(Self {
            invocation,
            store,
            generation,
            binding,
        })
    }

    /// Dispatch only read/fstat for the caller's actual already-admitted File.
    ///
    /// The real source controller MUST validate source/role/phase/occurrence
    /// and retain its admission owner and this actual File through the original
    /// managed worker/Join before calling. This protocol never opens a path,
    /// looks up a raw OS descriptor or establishes authority from a frame.
    /// Unknown/foreign/unsupported frames fail before file access, including
    /// negative reads. Original syscall errors stay typed/undisclosed inside;
    /// the controller must supply its proven exception disclosure channel.
    /// Close, registry admission, Python argument-error restoration and complete
    /// production bootstrap are deliberately not implemented by this adapter.
    pub fn inspect(
        &self,
        output: &mut NativeInspectionOutput<'_>,
        request: &CoreBuffer,
        file: &File,
    ) -> Result<Result<NativeFdOutcome, io::Error>, NativeDataIoError> {
        output.charge_work(0)?;
        if !self.invocation.same_invocation(output.invocation()) {
            return reject(output, NativeDataIoError::Unavailable);
        }
        let frame =
            match CoreValue::parse_guest(request.as_bytes(), output.allocation_owners().graph()) {
                Ok(frame) => frame,
                Err(error) => {
                    output.charge_work(0)?;
                    let failure = match error {
                        AllocationError::Invalid | AllocationError::Unavailable => {
                            NativeDataIoError::Unavailable
                        }
                        _ => NativeDataIoError::Capacity,
                    };
                    return reject(output, failure);
                }
            };
        let Some(operation) = self.operation(&frame) else {
            return reject(output, NativeDataIoError::Unavailable);
        };
        output.charge_work(1)?;

        // Four positive u64 hex values need at most 4*(16+2)+3 bytes.
        // The fixed JSON syntax/format and optional read-result prefix fit the
        // rest of this stack buffer; no heap formatting/copy is introduced.
        let mut prefix = [0_u8; 256];
        let mut cursor = Cursor::new(prefix.as_mut_slice());
        let p = self.invocation.provenance();
        if write!(cursor,
            "{{\"format\":\"{RESPONSE}\",\"scope\":[\"{:x}\",\"{:x}\",\"{:x}\",\"{:x}\"],\"result\":",
            p.account_generation, p.invocation, self.store, self.generation,
        ).is_err() {
            return reject(output, NativeDataIoError::Capacity);
        }
        if matches!(operation, Operation::Read(_))
            && cursor
                .write_all(b"{\"kind\":\"bytes\",\"value\":\"")
                .is_err()
        {
            return reject(output, NativeDataIoError::Capacity);
        }
        let len = cursor.position() as usize;
        match operation {
            Operation::Read(size) => write_fd_read(output, file, size, &prefix[..len], b"\"}}")
                .map(|result| result.map(NativeFdOutcome::Read)),
            Operation::Stat => write_fd_stat(output, file, &prefix[..len], b"}")
                .map(|result| result.map(|()| NativeFdOutcome::Stat)),
        }
    }

    fn operation(&self, frame: &CoreValue) -> Option<Operation> {
        exact(frame, &["format", "scope", "target", "operation", "args"])?;
        if frame.get_key("format")?.as_str() != Some(REQUEST) {
            return None;
        }
        let scope = frame.get_key("scope")?;
        let scope = scope.array()?;
        let p = self.invocation.provenance();
        let expected = [
            p.account_generation,
            p.invocation,
            self.store,
            self.generation,
        ];
        if scope.len() != expected.len() {
            return None;
        }
        for (field, expected) in scope.iter().zip(expected) {
            if canonical_hex(field.as_str()?)? != expected {
                return None;
            }
        }
        let target = frame.get_key("target")?;
        exact(&target, &["binding", "steps"])?;
        let binding = target.get_key("binding")?;
        if canonical_hex(binding.as_str()?.strip_prefix("fd_")?)? != self.binding
            || !target.get_key("steps")?.array()?.is_empty()
        {
            return None;
        }
        let args = frame.get_key("args")?;
        let args = args.array()?;
        match frame.get_key("operation")?.as_str()? {
            "read" if args.len() == 1 => {
                // Check the signed value BEFORE conversion, prepayment or IO.
                let size = usize::try_from(args.get(0)?.as_i64()?).ok()?;
                Some(Operation::Read(size))
            }
            "fstat" if args.is_empty() => Some(Operation::Stat),
            _ => None,
        }
    }
}

fn canonical_hex(text: &str) -> Option<u64> {
    if text.is_empty()
        || text.len() > 16
        || text.starts_with('0')
        || !text
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return None;
    }
    u64::from_str_radix(text, 16).ok().filter(|&n| n != 0)
}

fn exact(value: &CoreValue, keys: &[&str]) -> Option<()> {
    let object = value.object()?;
    (object.len() == keys.len() && keys.iter().all(|key| object.get(key).is_some())).then_some(())
}

fn reject<T>(
    output: &mut NativeInspectionOutput<'_>,
    error: NativeDataIoError,
) -> Result<T, NativeDataIoError> {
    Err(output.reject_execution(error))
}
