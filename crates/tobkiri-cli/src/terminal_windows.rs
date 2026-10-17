//! Synchronous Windows input with explicit cancellation custody. Only one
//! native unit is read at a time; stdio buffering never owns a later line.
//!
//! Dropping the future requests cancellation, not release. The native worker
//! owns the producer allocation, duplicated input HANDLE and terminal turn
//! until ReadFile/ReadConsoleW has actually returned. A separate supervisor
//! repeats CancelSynchronousIo until the worker's real thread handle signals;
//! ERROR_NOT_FOUND is only a race observation, never proof of completion.
//! An incomplete/error line quarantines this Input for the host lifetime. A
//! fresh CLI session is required; its remaining text cannot become approval.
use super::{allocation_error, io_error};
use std::{
    io,
    os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle},
    ptr::{null, null_mut},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
};
use tobkiri_core::{AllocationBudget, AllocationError, CoreBuffer, CoreBufferBuilder, HostError};
use windows_sys::Win32::{
    Foundation::{
        DUPLICATE_SAME_ACCESS, DuplicateHandle, ERROR_BROKEN_PIPE, ERROR_HANDLE_EOF, HANDLE,
        INVALID_HANDLE_VALUE, WAIT_OBJECT_0,
    },
    Storage::FileSystem::{FILE_TYPE_CHAR, FILE_TYPE_DISK, FILE_TYPE_PIPE, GetFileType, ReadFile},
    System::{
        Console::{GetConsoleMode, GetStdHandle, ReadConsoleW, STD_INPUT_HANDLE},
        IO::CancelSynchronousIo,
        Threading::{
            CreateEventW, GetCurrentProcess, GetCurrentThread, INFINITE, SetEvent,
            WaitForMultipleObjects, WaitForSingleObject,
        },
    },
};

const CHUNK: usize = 8; // UTF-16 scalar (4 UTF-8 bytes) plus a delayed CR.
const METADATA_LIMIT: usize = 1024 * 1024;
type Turn = tokio::sync::OwnedMutexGuard<()>;

#[derive(Clone, Copy)]
enum Kind {
    Console,
    Bytes,
}
struct SharedInput {
    handle: OwnedHandle,
    kind: Kind,
    quarantined: AtomicBool,
    #[cfg(test)]
    hooks: Hooks,
}
pub(super) struct Input {
    shared: Arc<SharedInput>,
    serial: Arc<tokio::sync::Mutex<()>>,
    #[cfg(test)]
    last_work: Mutex<Option<Arc<Work>>>,
}
impl Input {
    pub(super) fn new() -> Result<Self, HostError> {
        // Duplicate, never take ownership of the process-global stdin HANDLE.
        let raw = unsafe { GetStdHandle(STD_INPUT_HANDLE) };
        Self::owned(duplicate(raw)?)
    }
    fn owned(handle: OwnedHandle) -> Result<Self, HostError> {
        let kind = match unsafe { GetFileType(handle.as_raw_handle()) } {
            FILE_TYPE_CHAR => {
                let mut mode = 0;
                if unsafe { GetConsoleMode(handle.as_raw_handle(), &mut mode) } == 0 {
                    return Err(HostError(
                        "Windows input character device is not a console".into(),
                    ));
                }
                // Preserve processed/line/echo modes. ReadConsoleW with count=1
                // retains Windows editing and UTF-16 without a code-page change.
                Kind::Console
            }
            FILE_TYPE_PIPE | FILE_TYPE_DISK => Kind::Bytes,
            _ => {
                return Err(HostError(
                    "unsupported Windows terminal input handle".into(),
                ));
            }
        };
        Ok(Self {
            shared: Arc::new(SharedInput {
                handle,
                kind,
                quarantined: AtomicBool::new(false),
                #[cfg(test)]
                hooks: Hooks::default(),
            }),
            serial: Arc::new(tokio::sync::Mutex::new(())),
            #[cfg(test)]
            last_work: Mutex::new(None),
        })
    }
    pub(super) async fn line(&self, turn: Option<Turn>) -> Result<String, HostError> {
        match self
            .read(Producer::Metadata(Vec::new()), METADATA_LIMIT, turn)
            .await?
        {
            Producer::Metadata(bytes) => String::from_utf8(bytes)
                .map_err(|_| HostError("terminal input is not UTF-8".into())),
            Producer::Guest(_) => unreachable!(),
        }
    }
    pub(super) async fn line_budgeted(
        &self,
        maximum: usize,
        budget: &Arc<dyn AllocationBudget>,
        turn: Option<Turn>,
    ) -> Result<CoreBuffer, HostError> {
        if maximum == 0 {
            return Err(HostError("terminal input limit is zero".into()));
        }
        let capacity = maximum
            .checked_add(CHUNK)
            .ok_or_else(|| HostError("terminal input limit overflow".into()))?;
        match self
            .read(
                Producer::Guest(CoreBuffer::builder(budget, capacity)),
                maximum,
                turn,
            )
            .await?
        {
            Producer::Guest(builder) => {
                let value = builder.finish().map_err(allocation_error)?;
                value.as_str().map_err(allocation_error)?;
                Ok(value)
            }
            Producer::Metadata(_) => unreachable!(),
        }
    }
    async fn read(
        &self,
        producer: Producer,
        maximum: usize,
        turn: Option<Turn>,
    ) -> Result<Producer, HostError> {
        let serial = self.serial.clone().lock_owned().await;
        if self.shared.quarantined.load(Ordering::Acquire) {
            return Err(HostError("Windows input has an incomplete line; start a fresh CLI session before further input or approval".into()));
        }
        let work = Arc::new(Work {
            cancel: event()?,
            ready: event()?,
            cancelled: AtomicBool::new(false),
            thread: Mutex::new(None),
            #[cfg(test)]
            cancel_successes: Default::default(),
            #[cfg(test)]
            cancel_not_found: Default::default(),
        });
        #[cfg(test)]
        {
            *self.last_work.lock().unwrap() = Some(work.clone());
        }
        // This monitor exists before a worker can enter a native read. Its
        // lifetime is independent of Tokio and the caller's dropped future.
        let observer = work.clone();
        std::thread::Builder::new()
            .name("tobkiri-input-cancel".into())
            .spawn(move || supervise(observer))
            .map_err(io_error)?;
        let (send, receive) = tokio::sync::oneshot::channel();
        let input = self.shared.clone();
        let worker = work.clone();
        if let Err(error) = std::thread::Builder::new()
            .name("tobkiri-input".into())
            .spawn(move || {
                let _serial = serial;
                let _turn = turn;
                let result = (|| {
                    let thread = Arc::new(duplicate(unsafe { GetCurrentThread() })?);
                    *worker.thread.lock().map_err(|_| {
                        HostError("Windows input worker state is unavailable".into())
                    })? = Some(thread);
                    unsafe { SetEvent(worker.ready.as_raw_handle()) };
                    produce(&input, &worker, producer, maximum)
                })();
                let _ = send.send(result);
                // Also wakes the monitor if obtaining the native thread handle
                // failed before publication. No native read began in that case.
                unsafe { SetEvent(worker.ready.as_raw_handle()) };
            })
        {
            unsafe { SetEvent(work.ready.as_raw_handle()) };
            return Err(io_error(error));
        }
        let mut cancelled = CancelOnDrop { work, armed: true };
        let result = receive
            .await
            .map_err(|_| HostError("Windows terminal read worker failed".into()))?;
        cancelled.armed = false;
        result
    }
}

fn duplicate(handle: HANDLE) -> Result<OwnedHandle, HostError> {
    if handle.is_null() || handle == INVALID_HANDLE_VALUE {
        return Err(HostError("Windows input handle is unavailable".into()));
    }
    let process = unsafe { GetCurrentProcess() };
    let mut result = null_mut();
    if unsafe {
        DuplicateHandle(
            process,
            handle,
            process,
            &mut result,
            0,
            0,
            DUPLICATE_SAME_ACCESS,
        )
    } == 0
    {
        return Err(io_error(io::Error::last_os_error()));
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(result) })
}
fn event() -> Result<OwnedHandle, HostError> {
    let value = unsafe { CreateEventW(null(), 1, 0, null()) };
    if value.is_null() {
        return Err(io_error(io::Error::last_os_error()));
    }
    Ok(unsafe { OwnedHandle::from_raw_handle(value) })
}
struct Work {
    cancel: OwnedHandle,
    ready: OwnedHandle,
    cancelled: AtomicBool,
    thread: Mutex<Option<Arc<OwnedHandle>>>,
    #[cfg(test)]
    cancel_successes: std::sync::atomic::AtomicUsize,
    #[cfg(test)]
    cancel_not_found: std::sync::atomic::AtomicUsize,
}
struct CancelOnDrop {
    work: Arc<Work>,
    armed: bool,
}
impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        if self.armed {
            self.work.cancelled.store(true, Ordering::Release);
            unsafe { SetEvent(self.work.cancel.as_raw_handle()) };
        }
    }
}
fn supervise(work: Arc<Work>) {
    if unsafe { WaitForSingleObject(work.ready.as_raw_handle(), INFINITE) } != WAIT_OBJECT_0 {
        return;
    }
    let Some(thread) = work
        .thread
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .clone()
    else {
        return;
    };
    let handles = [thread.as_raw_handle(), work.cancel.as_raw_handle()];
    let result = unsafe { WaitForMultipleObjects(2, handles.as_ptr(), 0, INFINITE) };
    if result == WAIT_OBJECT_0 {
        return;
    }
    // Even an unexpected wait error must not release worker-owned custody.
    // Cancelling repeatedly closes the check-before-read / NOT_FOUND race.
    while unsafe { WaitForSingleObject(thread.as_raw_handle(), 5) } != WAIT_OBJECT_0 {
        let cancelled = unsafe { CancelSynchronousIo(thread.as_raw_handle()) };
        #[cfg(test)]
        {
            if cancelled != 0 {
                work.cancel_successes.fetch_add(1, Ordering::SeqCst);
            } else if io::Error::last_os_error().raw_os_error()
                == Some(windows_sys::Win32::Foundation::ERROR_NOT_FOUND as i32)
            {
                work.cancel_not_found.fetch_add(1, Ordering::SeqCst);
            }
        }
        #[cfg(not(test))]
        let _ = cancelled;
    }
}

enum Producer {
    Metadata(Vec<u8>),
    Guest(CoreBufferBuilder),
}
impl Producer {
    fn len(&self) -> usize {
        match self {
            Self::Metadata(v) => v.len(),
            Self::Guest(v) => v.len(),
        }
    }
    fn chunk(
        &mut self,
        write: impl FnOnce(&mut [u8]) -> Result<usize, AllocationError>,
    ) -> Result<(), HostError> {
        match self {
            Self::Guest(builder) => builder
                .write_chunk(CHUNK, write)
                .map(|_| ())
                .map_err(allocation_error),
            Self::Metadata(bytes) => {
                // Metadata has a separate trusted, bounded allocation. It is
                // never reused as a guest payload or assigned a guest budget.
                bytes
                    .try_reserve_exact(CHUNK)
                    .map_err(|_| HostError("terminal metadata allocation failed".into()))?;
                let start = bytes.len();
                bytes.resize(start + CHUNK, 0);
                let result = write(&mut bytes[start..]);
                bytes.truncate(start + result.as_ref().copied().unwrap_or(0));
                result.map(|_| ()).map_err(allocation_error)
            }
        }
    }
}
#[derive(Default)]
struct Decoder {
    high: Option<u16>,
    cr: bool,
}
impl Decoder {
    fn unit(&mut self, unit: u16, out: &mut [u8]) -> Result<usize, AllocationError> {
        if let Some(high) = self.high.take() {
            if !(0xdc00..=0xdfff).contains(&unit) {
                return Err(AllocationError::Invalid);
            }
            let value = 0x10000 + ((u32::from(high) - 0xd800) << 10) + (u32::from(unit) - 0xdc00);
            let ch = char::from_u32(value).ok_or(AllocationError::Invalid)?;
            return Ok(ch.encode_utf8(out).len());
        }
        if (0xd800..=0xdbff).contains(&unit) {
            self.high = Some(unit);
            return Ok(0);
        }
        let ch = char::from_u32(u32::from(unit)).ok_or(AllocationError::Invalid)?;
        Ok(ch.encode_utf8(out).len())
    }
}
fn produce(
    input: &SharedInput,
    work: &Work,
    mut producer: Producer,
    maximum: usize,
) -> Result<Producer, HostError> {
    let mut decoder = Decoder::default();
    let mut issued = false;
    let mut boundary = false;
    let result = (|| {
        loop {
            if work.cancelled.load(Ordering::Acquire) {
                return Err(HostError("terminal input was cancelled".into()));
            }
            let length = producer.len();
            let mut failure = None;
            let mut eof = false;
            producer.chunk(|tail| {
                // Funding and physical destination allocation precede every
                // syscall, including reads which later return EOF or cancel.
                if work.cancelled.load(Ordering::Acquire) { return Err(AllocationError::Unavailable); }
                #[cfg(test)] input.hooks.before();
                issued = true;
                let mut count = 0;
                let consumed;
                let ok = match input.kind {
                    Kind::Bytes => { let ok = unsafe { ReadFile(input.handle.as_raw_handle(), tail.as_mut_ptr(), 1, &mut count, null_mut()) }; consumed = count != 0; ok },
                    Kind::Console => {
                        // Fixed inline scratch has no heap payload; the full
                        // resulting UTF-8 tail is already producer-funded.
                        let mut unit = 0u16;
                        let ok = unsafe { ReadConsoleW(input.handle.as_raw_handle(), (&mut unit as *mut u16).cast(), 1, &mut count, null()) };
                        consumed = count != 0;
                        if ok != 0 && count != 0 { count = decoder.unit(unit, tail)? as u32; }
                        ok
                    }
                };
                let error = if ok == 0 { Some(io::Error::last_os_error()) } else { None };
                #[cfg(test)] input.hooks.after();
                if let Some(error) = error {
                    if matches!(error.raw_os_error(), Some(code) if code == ERROR_BROKEN_PIPE as i32 || code == ERROR_HANDLE_EOF as i32) { eof = true; }
                    else { failure = Some(error); return Err(AllocationError::Unavailable); }
                } else if !consumed {
                    eof = true;
                } else if count == 0 {
                    // A high surrogate consumed one native unit but emits no
                    // UTF-8 until its next low surrogate; this is not EOF.
                    return Ok(0);
                }
                if work.cancelled.load(Ordering::Acquire) { return Err(AllocationError::Unavailable); }
                if eof {
                    boundary = true;
                    if decoder.high.is_some() { return Err(AllocationError::Invalid); }
                    if decoder.cr {
                        if length == maximum { return Err(AllocationError::Limit); }
                        tail[0] = b'\r'; decoder.cr = false; return Ok(1);
                    }
                    return Ok(0);
                }
                let count = count as usize;
                if count == 1 && tail[0] == b'\n' { decoder.cr = false; boundary = true; return Ok(0); }
                let delayed = usize::from(decoder.cr);
                let cr = count == 1 && tail[0] == b'\r';
                let written = delayed + if cr { 0 } else { count };
                if length.checked_add(written).ok_or(AllocationError::Overflow)? > maximum { return Err(AllocationError::Limit); }
                if delayed != 0 {
                    if !cr { tail.copy_within(..count, 1); }
                    tail[0] = b'\r';
                }
                decoder.cr = cr;
                Ok(written)
            }).map_err(|error| failure.map(io_error).unwrap_or(error))?;
            if boundary {
                if eof && producer.len() == 0 {
                    return Err(HostError("end of input".into()));
                }
                return Ok(());
            }
        }
    })();
    if issued && !boundary {
        input.quarantined.store(true, Ordering::Release);
    }
    result.map(|()| producer)
}

#[cfg(test)]
#[derive(Default)]
struct Pause {
    enabled: AtomicBool,
    reached: AtomicBool,
    release: (Mutex<bool>, std::sync::Condvar),
}
#[cfg(test)]
impl Pause {
    fn wait(&self) {
        if self.enabled.load(Ordering::SeqCst) {
            self.reached.store(true, Ordering::SeqCst);
            let (mutex, ready) = &self.release;
            let mut released = mutex.lock().unwrap();
            while !*released {
                released = ready.wait(released).unwrap();
            }
        }
    }
    fn open(&self) {
        *self.release.0.lock().unwrap() = true;
        self.release.1.notify_all();
    }
}
#[cfg(test)]
#[derive(Default)]
struct Hooks {
    before: Pause,
    after: Pause,
    reads: std::sync::atomic::AtomicUsize,
}
#[cfg(test)]
impl Hooks {
    fn before(&self) {
        self.before.wait();
        self.reads.fetch_add(1, Ordering::SeqCst);
    }
    fn after(&self) {
        self.after.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{sync::atomic::AtomicUsize, time::Duration};
    use tobkiri_core::AllocationRequest;
    use windows_sys::Win32::{
        Foundation::WAIT_TIMEOUT, Storage::FileSystem::WriteFile, System::Pipes::CreatePipe,
    };

    #[derive(Default)]
    struct Budget {
        value: Mutex<(u64, u64)>,
        calls: AtomicUsize,
    }
    impl Budget {
        fn allow(&self) {
            self.value.lock().unwrap().1 = 1024 * 1024;
        }
        fn used(&self) -> u64 {
            self.value.lock().unwrap().0
        }
    }
    impl AllocationBudget for Budget {
        fn reserve(&self, request: AllocationRequest) -> Result<(), AllocationError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            let mut value = self.value.lock().unwrap();
            let total = value
                .0
                .checked_add(request.bytes)
                .ok_or(AllocationError::Overflow)?;
            if total > value.1 {
                return Err(AllocationError::Limit);
            }
            value.0 = total;
            Ok(())
        }
        fn release(&self, bytes: u64) {
            self.value.lock().unwrap().0 -= bytes;
        }
    }
    fn pipe(bytes: &[u8]) -> (Arc<Input>, OwnedHandle) {
        let mut read = null_mut();
        let mut write = null_mut();
        assert_ne!(
            unsafe { CreatePipe(&mut read, &mut write, null(), 4096) },
            0,
            "{}",
            io::Error::last_os_error()
        );
        let read = unsafe { OwnedHandle::from_raw_handle(read) };
        let write = unsafe { OwnedHandle::from_raw_handle(write) };
        write_bytes(&write, bytes);
        (Arc::new(Input::owned(read).unwrap()), write)
    }
    fn write_bytes(handle: &OwnedHandle, bytes: &[u8]) {
        if bytes.is_empty() {
            return;
        }
        let mut count = 0;
        assert_ne!(
            unsafe {
                WriteFile(
                    handle.as_raw_handle(),
                    bytes.as_ptr(),
                    bytes.len() as u32,
                    &mut count,
                    null_mut(),
                )
            },
            0
        );
        assert_eq!(count as usize, bytes.len());
    }
    fn remaining(input: &Input, expected: &[u8]) {
        let mut bytes = vec![0; expected.len()];
        let mut count = 0;
        assert_ne!(
            unsafe {
                ReadFile(
                    input.shared.handle.as_raw_handle(),
                    bytes.as_mut_ptr(),
                    bytes.len() as u32,
                    &mut count,
                    null_mut(),
                )
            },
            0
        );
        assert_eq!(&bytes[..count as usize], expected);
    }
    async fn until(mut predicate: impl FnMut() -> bool) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while !predicate() {
                tokio::time::sleep(Duration::from_millis(2)).await;
            }
        })
        .await
        .expect("native worker did not reach the expected state");
    }
    fn thread(work: &Work) -> Arc<OwnedHandle> {
        work.thread.lock().unwrap().as_ref().unwrap().clone()
    }

    #[tokio::test]
    async fn windows_pipe_denial_precedes_os_read_and_lines_do_not_read_ahead() {
        let (input, _writer) = pipe("日😀\r\napprove\n".as_bytes());
        let budget = Arc::new(Budget::default());
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        assert!(input.line_budgeted(7, &funding, None).await.is_err());
        assert_eq!(input.shared.hooks.reads.load(Ordering::SeqCst), 0);
        assert_eq!(budget.used(), 0);
        budget.allow();
        let first = input.line_budgeted(7, &funding, None).await.unwrap();
        assert_eq!(first.as_bytes(), "日😀".as_bytes());
        assert_eq!(input.shared.hooks.reads.load(Ordering::SeqCst), 9);
        let used = budget.used();
        assert_eq!(input.line(None).await.unwrap(), "approve");
        // Approval metadata is not assigned to the previous guest's account.
        assert_eq!(budget.used(), used);
        drop(first);
        assert_eq!(budget.used(), 0);
    }
    #[tokio::test]
    async fn windows_pipe_eof_utf8_and_exact_crlf_limits() {
        let (input, writer) = pipe(b"x\r\nlast\r");
        drop(writer);
        let budget = Arc::new(Budget::default());
        budget.allow();
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        assert_eq!(
            input
                .line_budgeted(1, &funding, None)
                .await
                .unwrap()
                .as_bytes(),
            b"x"
        );
        assert_eq!(
            input
                .line_budgeted(5, &funding, None)
                .await
                .unwrap()
                .as_bytes(),
            b"last\r"
        );
        assert!(
            input
                .line_budgeted(5, &funding, None)
                .await
                .unwrap_err()
                .0
                .contains("end of input")
        );
        assert_eq!(budget.used(), 0);
        let (invalid, _writer) = pipe(b"\xff\napprove\n");
        assert!(invalid.line_budgeted(1, &funding, None).await.is_err());
        // Invalid complete UTF-8 cannot cross the explicit newline boundary.
        assert_eq!(invalid.line(None).await.unwrap(), "approve");
    }
    #[tokio::test]
    async fn windows_overflow_quarantines_before_remaining_text_can_be_approval() {
        let (input, _writer) = pipe(b"long\napprove\n");
        let budget = Arc::new(Budget::default());
        budget.allow();
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        assert!(input.line_budgeted(2, &funding, None).await.is_err());
        assert_eq!(budget.used(), 0);
        assert!(input.line(None).await.unwrap_err().0.contains("fresh CLI"));
        assert_eq!(input.shared.hooks.reads.load(Ordering::SeqCst), 3);
        remaining(&input, b"g\napprove\n");
    }
    #[tokio::test]
    async fn windows_cancel_keeps_budget_and_turn_until_native_read_really_stops() {
        let (input, writer) = pipe(b"");
        input
            .shared
            .hooks
            .after
            .enabled
            .store(true, Ordering::SeqCst);
        let budget = Arc::new(Budget::default());
        budget.allow();
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        let turn = Arc::new(tokio::sync::Mutex::new(()));
        let held = turn.clone().lock_owned().await;
        let owned = input.clone();
        let request =
            tokio::spawn(async move { owned.line_budgeted(32, &funding, Some(held)).await });
        until(|| input.shared.hooks.reads.load(Ordering::SeqCst) != 0).await;
        let work = input.last_work.lock().unwrap().as_ref().unwrap().clone();
        let native = thread(&work);
        assert!(budget.used() > 0);
        request.abort();
        assert!(request.await.unwrap_err().is_cancelled());
        until(|| input.shared.hooks.after.reached.load(Ordering::SeqCst)).await;
        until(|| work.cancel_successes.load(Ordering::SeqCst) != 0).await;
        // CancelSynchronousIo succeeded and the syscall returned, but the
        // actual worker still owns the producer and terminal turn.
        assert_eq!(
            unsafe { WaitForSingleObject(native.as_raw_handle(), 0) },
            WAIT_TIMEOUT
        );
        assert!(turn.try_lock().is_err());
        assert!(budget.used() > 0);
        input.shared.hooks.after.open();
        until(|| unsafe { WaitForSingleObject(native.as_raw_handle(), 0) } == WAIT_OBJECT_0).await;
        assert_eq!(budget.used(), 0);
        assert!(turn.try_lock().is_ok());
        assert!(input.line(None).await.unwrap_err().0.contains("fresh CLI"));
        write_bytes(&writer, b"approve\n");
        remaining(&input, b"approve\n");
    }
    #[tokio::test]
    async fn windows_cancel_not_found_is_retried_after_the_check_read_race() {
        let (input, writer) = pipe(b"");
        input
            .shared
            .hooks
            .before
            .enabled
            .store(true, Ordering::SeqCst);
        let budget = Arc::new(Budget::default());
        budget.allow();
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        let owned = input.clone();
        let request = tokio::spawn(async move { owned.line_budgeted(32, &funding, None).await });
        until(|| input.shared.hooks.before.reached.load(Ordering::SeqCst)).await;
        let work = input.last_work.lock().unwrap().as_ref().unwrap().clone();
        let native = thread(&work);
        request.abort();
        assert!(request.await.unwrap_err().is_cancelled());
        until(|| work.cancel_not_found.load(Ordering::SeqCst) != 0).await;
        assert!(budget.used() > 0);
        assert_eq!(
            unsafe { WaitForSingleObject(native.as_raw_handle(), 0) },
            WAIT_TIMEOUT
        );
        // Worker now enters ReadFile after an unsuccessful cancellation; the
        // supervisor must repeat, not declare the earlier NOT_FOUND complete.
        input.shared.hooks.before.open();
        until(|| unsafe { WaitForSingleObject(native.as_raw_handle(), 0) } == WAIT_OBJECT_0).await;
        assert!(work.cancel_successes.load(Ordering::SeqCst) > 0);
        assert_eq!(budget.used(), 0);
        assert!(input.line(None).await.is_err());
        write_bytes(&writer, b"approve\n");
        remaining(&input, b"approve\n");
    }
    #[test]
    fn windows_utf16_surrogates_reject_unpaired_units_without_repair() {
        let mut decoder = Decoder::default();
        let mut tail = [0; CHUNK];
        assert_eq!(decoder.unit(0xd83d, &mut tail).unwrap(), 0);
        let count = decoder.unit(0xde00, &mut tail).unwrap();
        assert_eq!(&tail[..count], "😀".as_bytes());
        assert!(decoder.unit(0xde00, &mut tail).is_err());
        assert_eq!(decoder.unit(0xd83d, &mut tail).unwrap(), 0);
        assert!(decoder.unit(b'a' as u16, &mut tail).is_err());
    }
    /// Run in an otherwise idle, isolated native Console/ConPTY. No console is
    /// allocated, input flushed or console mode changed by the implementation.
    #[tokio::test]
    #[ignore = "requires an attached isolated native Windows console (CONIN$)"]
    async fn windows_console_utf16_processed_mode_and_line_boundary() {
        use windows_sys::Win32::{
            Foundation::GENERIC_READ,
            Foundation::GENERIC_WRITE,
            Storage::FileSystem::{CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING},
            System::Console::{
                GetNumberOfConsoleInputEvents, INPUT_RECORD, KEY_EVENT, KEY_EVENT_RECORD,
                WriteConsoleInputW,
            },
        };
        let name = "CONIN$\0".encode_utf16().collect::<Vec<_>>();
        let raw = unsafe {
            CreateFileW(
                name.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                null(),
                OPEN_EXISTING,
                0,
                null_mut(),
            )
        };
        assert_ne!(
            raw, INVALID_HANDLE_VALUE,
            "test requires its own attached console"
        );
        let handle = unsafe { OwnedHandle::from_raw_handle(raw) };
        let mut mode = 0;
        assert_ne!(unsafe { GetConsoleMode(raw, &mut mode) }, 0);
        assert_ne!(
            mode & windows_sys::Win32::System::Console::ENABLE_PROCESSED_INPUT,
            0
        );
        assert_ne!(
            mode & windows_sys::Win32::System::Console::ENABLE_LINE_INPUT,
            0
        );
        let mut pending = 0;
        assert_ne!(
            unsafe { GetNumberOfConsoleInputEvents(raw, &mut pending) },
            0
        );
        assert_eq!(
            pending, 0,
            "do not consume or flush pre-existing user input"
        );
        let records = "日😀\rapprove\r"
            .encode_utf16()
            .map(|unit| {
                let mut record: INPUT_RECORD = unsafe { std::mem::zeroed() };
                record.EventType = KEY_EVENT as u16;
                record.Event.KeyEvent = KEY_EVENT_RECORD {
                    bKeyDown: 1,
                    wRepeatCount: 1,
                    wVirtualKeyCode: if unit == 13 { 13 } else { 0 },
                    wVirtualScanCode: 0,
                    uChar: windows_sys::Win32::System::Console::KEY_EVENT_RECORD_0 {
                        UnicodeChar: unit,
                    },
                    dwControlKeyState: 0,
                };
                record
            })
            .collect::<Vec<_>>();
        let mut written = 0;
        assert_ne!(
            unsafe {
                WriteConsoleInputW(raw, records.as_ptr(), records.len() as u32, &mut written)
            },
            0
        );
        assert_eq!(written as usize, records.len());
        let input = Input::owned(handle).unwrap();
        let budget = Arc::new(Budget::default());
        budget.allow();
        let funding: Arc<dyn AllocationBudget> = budget.clone();
        assert_eq!(
            input
                .line_budgeted(7, &funding, None)
                .await
                .unwrap()
                .as_bytes(),
            "日😀".as_bytes()
        );
        assert_eq!(input.line(None).await.unwrap(), "approve");
        let mut after = 0;
        assert_ne!(unsafe { GetConsoleMode(raw, &mut after) }, 0);
        assert_eq!(after, mode);

        // This test's Console/ConPTY must be disposable: deliberately leave a
        // partial cooked line, cancel its real ReadConsoleW, and prove this
        // Input cannot hand that text to a later approval. Never flush a user's
        // existing console or alter processed/echo modes to simulate success.
        let input = Arc::new(input);
        input
            .shared
            .hooks
            .after
            .enabled
            .store(true, Ordering::SeqCst);
        let mut partial: INPUT_RECORD = unsafe { std::mem::zeroed() };
        partial.EventType = KEY_EVENT as u16;
        partial.Event.KeyEvent = KEY_EVENT_RECORD {
            bKeyDown: 1,
            wRepeatCount: 1,
            wVirtualKeyCode: 65,
            wVirtualScanCode: 0,
            uChar: windows_sys::Win32::System::Console::KEY_EVENT_RECORD_0 {
                UnicodeChar: b'x' as u16,
            },
            dwControlKeyState: 0,
        };
        assert_ne!(
            unsafe { WriteConsoleInputW(raw, &partial, 1, &mut written) },
            0
        );
        let turn = Arc::new(tokio::sync::Mutex::new(()));
        let held = turn.clone().lock_owned().await;
        let owned = input.clone();
        let previous = input.shared.hooks.reads.load(Ordering::SeqCst);
        let request =
            tokio::spawn(async move { owned.line_budgeted(32, &funding, Some(held)).await });
        until(|| input.shared.hooks.reads.load(Ordering::SeqCst) > previous).await;
        let work = input.last_work.lock().unwrap().as_ref().unwrap().clone();
        let native = thread(&work);
        request.abort();
        assert!(request.await.unwrap_err().is_cancelled());
        until(|| input.shared.hooks.after.reached.load(Ordering::SeqCst)).await;
        assert!(budget.used() > 0 && turn.try_lock().is_err());
        assert_eq!(
            unsafe { WaitForSingleObject(native.as_raw_handle(), 0) },
            WAIT_TIMEOUT
        );
        input.shared.hooks.after.open();
        until(|| unsafe { WaitForSingleObject(native.as_raw_handle(), 0) } == WAIT_OBJECT_0).await;
        assert!(work.cancel_successes.load(Ordering::SeqCst) > 0);
        assert_eq!(budget.used(), 0);
        assert!(turn.try_lock().is_ok());
        let count = input.shared.hooks.reads.load(Ordering::SeqCst);
        assert!(input.line(None).await.unwrap_err().0.contains("fresh CLI"));
        assert_eq!(input.shared.hooks.reads.load(Ordering::SeqCst), count);
        assert_ne!(unsafe { GetConsoleMode(raw, &mut after) }, 0);
        assert_eq!(after, mode);
    }
}
