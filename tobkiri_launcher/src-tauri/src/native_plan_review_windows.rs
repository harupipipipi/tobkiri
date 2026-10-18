//! A native, owned review window. Pack/webview content is never the approval UI.
//!
//! Called on a blocking worker. Only parent-window admission/restoration runs on
//! Tauri's UI thread; that thread never waits for the review worker.

use std::cell::Cell;
use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicUsize, Ordering};
use std::sync::{mpsc, Arc, OnceLock};
use std::time::{Duration, Instant};
use windows_sys::core::w;
use windows_sys::Win32::Foundation::{
    GetLastError, HINSTANCE, HWND, LPARAM, LRESULT, RECT, WAIT_FAILED, WPARAM,
};
use windows_sys::Win32::Graphics::Gdi::{
    CreateSolidBrush, DeleteObject, GetMonitorInfoW, GetStockObject, GetSysColorBrush,
    MonitorFromWindow, SetBkColor, SetTextColor, COLOR_BTNFACE, DEFAULT_GUI_FONT, HBRUSH, HDC,
    MONITORINFO, MONITOR_DEFAULTTONEAREST,
};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::SystemServices::{SS_LEFT, SS_NOPREFIX};
use windows_sys::Win32::System::Threading::{GetCurrentProcessId, GetCurrentThreadId};
use windows_sys::Win32::UI::Controls::{EM_GETLIMITTEXT, EM_SETLIMITTEXT};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    EnableWindow, IsWindowEnabled, SetFocus, VK_ESCAPE, VK_RETURN,
};
use windows_sys::Win32::UI::WindowsAndMessaging::*;

const MAX_REVIEW_BYTES: usize = 512 * 1024;
const REVIEW_SECONDS: u64 = 240;
const OWNER_WAIT: Duration = Duration::from_secs(5);
const TIMER_ID: usize = 1;
const APPROVE_ID: usize = 100;
const DENY_ID: usize = 101;
const CLASS_NAME: *const u16 = w!("Tobkiri.TrustedReview.Window.v1");
const OWNER_PROPERTY: *const u16 = w!("Tobkiri.TrustedReview.Owner.v1");
static REVIEW_OPEN: AtomicBool = AtomicBool::new(false);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Decision {
    Pending,
    Approve,
    Deny,
    Unavailable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ParentRestoration {
    SameLiveOwner,
    OwnerGone,
    Unknown,
}

impl ParentRestoration {
    fn cleanup_complete(self) -> bool {
        self != Self::Unknown
    }
}

// None means that the same live, token-bound owner is still observed. Known
// destruction may complete cleanup, but can never attest an approvable owner.
fn owner_loss(same_owner: bool, live_window: bool) -> Option<ParentRestoration> {
    if !live_window {
        Some(ParentRestoration::OwnerGone)
    } else if !same_owner {
        Some(ParentRestoration::Unknown)
    } else {
        None
    }
}

/// An Arc is retained by every queued UI callback and by the actual dialog.
/// A lost close/restore acknowledgement leaves admission closed, not reusable.
struct ReviewLease {
    parent: AtomicUsize,
    parent_thread: AtomicU32,
    parent_active: AtomicBool,
    dialog_active: AtomicBool,
    uncertain: AtomicBool,
    abort: AtomicBool,
    cancelled: Arc<AtomicBool>,
    deadline: Instant,
}

impl ReviewLease {
    fn acquire(cancelled: Arc<AtomicBool>, deadline: Instant) -> Result<Arc<Self>, String> {
        REVIEW_OPEN
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .map_err(|_| "Finish the existing native review first".to_string())?;
        Ok(Arc::new(Self {
            parent: AtomicUsize::new(0),
            parent_thread: AtomicU32::new(0),
            parent_active: AtomicBool::new(false),
            dialog_active: AtomicBool::new(false),
            uncertain: AtomicBool::new(false),
            abort: AtomicBool::new(false),
            cancelled,
            deadline,
        }))
    }

    fn stopped(&self) -> bool {
        self.abort.load(Ordering::Acquire)
            || self.cancelled.load(Ordering::Acquire)
            || Instant::now() >= self.deadline
    }

    fn token(&self) -> *mut std::ffi::c_void {
        (self as *const Self).cast_mut().cast()
    }

    // This is an observation only. Parent mutations additionally run on the
    // parent's owning UI thread, where HWND destruction cannot race them.
    fn parent_matches(&self) -> bool {
        let parent = self.parent.load(Ordering::Acquire) as HWND;
        let mut pid = 0;
        unsafe {
            !parent.is_null()
                && IsWindow(parent) != 0
                && GetWindowThreadProcessId(parent, &mut pid)
                    == self.parent_thread.load(Ordering::Acquire)
                && pid == GetCurrentProcessId()
                && GetPropW(parent, OWNER_PROPERTY) == self.token()
        }
    }
}

impl Drop for ReviewLease {
    fn drop(&mut self) {
        if !self.uncertain.load(Ordering::Acquire)
            && !self.parent_active.load(Ordering::Acquire)
            && !self.dialog_active.load(Ordering::Acquire)
        {
            REVIEW_OPEN.store(false, Ordering::Release);
        }
    }
}

struct ParentLease {
    window: tauri::WebviewWindow,
    lease: Arc<ReviewLease>,
    restored: bool,
}

impl ParentLease {
    fn acquire(window: &tauri::WebviewWindow, lease: Arc<ReviewLease>) -> Result<Self, String> {
        // Refuse calling this modal worker on the owner's thread. Otherwise a
        // queued parent admission could deadlock the UI thread.
        let raw = window
            .hwnd()
            .map_err(|_| "Native review owner is unavailable")?;
        let parent = raw.0 as HWND;
        let mut pid = 0;
        let worker = unsafe { GetCurrentThreadId() };
        let owner_thread = unsafe { GetWindowThreadProcessId(parent, &mut pid) };
        if owner_thread == 0 || owner_thread == worker || pid != unsafe { GetCurrentProcessId() } {
            return Err("Native review requires its own worker and an owned parent".into());
        }
        let (tx, rx) = mpsc::sync_channel(1);
        let callback_lease = lease.clone();
        let ui_window = window.clone();
        window
            .run_on_main_thread(move || {
                let result = admit_parent(&ui_window, &callback_lease, worker);
                // If the worker has already stopped waiting, undo this exact
                // parent's admission here, before releasing the callback Arc.
                if tx.send(result).is_err() {
                    callback_lease.abort.store(true, Ordering::Release);
                    if !restore_parent(&callback_lease).cleanup_complete() {
                        callback_lease.uncertain.store(true, Ordering::Release);
                    }
                }
            })
            .map_err(|_| "Native review owner could not be scheduled".to_string())?;
        match rx.recv_timeout(OWNER_WAIT) {
            Ok(Ok(())) if !lease.stopped() => Ok(Self {
                window: window.clone(),
                lease,
                restored: false,
            }),
            Ok(Ok(())) => {
                let mut parent = Self {
                    window: window.clone(),
                    lease,
                    restored: false,
                };
                let _ = parent.restore();
                Err("Native review was cancelled before display".into())
            }
            Ok(Err(error)) => Err(error),
            Err(_) => {
                lease.abort.store(true, Ordering::Release);
                // Also enqueue restoration: the admission callback can have
                // sent just as recv_timeout stopped waiting. A successful
                // channel send alone is not acknowledgement by this worker.
                let mut parent = Self {
                    window: window.clone(),
                    lease,
                    restored: false,
                };
                let _ = parent.restore();
                Err("Native review owner admission was not confirmed".into())
            }
        }
    }

    fn restore(&mut self) -> Result<ParentRestoration, String> {
        if self.restored {
            return Err("Native review owner restoration was already attempted".into());
        }
        self.restored = true;
        let (tx, rx) = mpsc::sync_channel(1);
        let lease = self.lease.clone();
        if self
            .window
            .run_on_main_thread(move || {
                let restored = restore_parent(&lease);
                if !restored.cleanup_complete() {
                    lease.uncertain.store(true, Ordering::Release);
                }
                let _ = tx.send(restored);
            })
            .is_err()
        {
            self.lease.uncertain.store(true, Ordering::Release);
            return Err("Native review owner restoration could not be scheduled".into());
        }
        match rx.recv_timeout(OWNER_WAIT) {
            Ok(restored) if restored.cleanup_complete() => Ok(restored),
            _ => {
                self.lease.uncertain.store(true, Ordering::Release);
                Err("Native review owner restoration was not confirmed".into())
            }
        }
    }
}

impl Drop for ParentLease {
    fn drop(&mut self) {
        if !self.restored {
            self.lease.abort.store(true, Ordering::Release);
            let _ = self.restore();
        }
    }
}

fn admit_parent(
    window: &tauri::WebviewWindow,
    lease: &ReviewLease,
    worker: u32,
) -> Result<(), String> {
    if lease.stopped() {
        return Err("Native review was cancelled before display".into());
    }
    let parent = window
        .hwnd()
        .map_err(|_| "Native review owner is unavailable")?
        .0 as HWND;
    let mut pid = 0;
    unsafe {
        let thread = GetWindowThreadProcessId(parent, &mut pid);
        if IsWindow(parent) == 0
            || thread != GetCurrentThreadId()
            || thread == worker
            || pid != GetCurrentProcessId()
            || IsWindowEnabled(parent) == 0
            || !GetPropW(parent, OWNER_PROPERTY).is_null()
        {
            return Err("Native review owner is not available for a modal review".into());
        }
        lease.parent.store(parent as usize, Ordering::Release);
        lease.parent_thread.store(thread, Ordering::Release);
        if SetPropW(parent, OWNER_PROPERTY, lease.token()) == 0 {
            return Err("Native review owner could not be retained".into());
        }
        lease.parent_active.store(true, Ordering::Release);
        EnableWindow(parent, 0);
        if lease.stopped() || !lease.parent_matches() || IsWindowEnabled(parent) != 0 {
            if !restore_parent(lease).cleanup_complete() {
                lease.uncertain.store(true, Ordering::Release);
            }
            return Err("Native review owner admission failed".into());
        }
    }
    Ok(())
}

// Called only by an owning UI-thread closure. A destroyed parent has lost its
// property, so a recycled HWND is never enabled or stripped of another token.
fn restore_parent(lease: &ReviewLease) -> ParentRestoration {
    if !lease.parent_active.load(Ordering::Acquire) {
        // No retained owner remains to attest, even if there is no cleanup
        // work. This path must never be treated as SameLiveOwner.
        return ParentRestoration::OwnerGone;
    }
    let parent = lease.parent.load(Ordering::Acquire) as HWND;
    unsafe {
        if let Some(lost) = owner_loss(lease.parent_matches(), IsWindow(parent) != 0) {
            if lost == ParentRestoration::OwnerGone {
                lease.parent_active.store(false, Ordering::Release);
            }
            // A live mismatched HWND may have been recycled or its property
            // changed. Neither is permission to mutate it or reuse admission.
            return lost;
        }
        if GetCurrentThreadId() != lease.parent_thread.load(Ordering::Acquire) {
            return ParentRestoration::Unknown;
        }
        EnableWindow(parent, 1);
        if let Some(lost) = owner_loss(lease.parent_matches(), IsWindow(parent) != 0) {
            // Synchronous owner reentrancy may destroy the window. Its token
            // is then gone; do not touch any subsequently reused HWND.
            if lost == ParentRestoration::OwnerGone {
                lease.parent_active.store(false, Ordering::Release);
            }
            return lost;
        }
        if IsWindowEnabled(parent) == 0 || RemovePropW(parent, OWNER_PROPERTY) != lease.token() {
            return ParentRestoration::Unknown;
        }
        lease.parent_active.store(false, Ordering::Release);
        ParentRestoration::SameLiveOwner
    }
}

struct PreparedReview {
    title: Vec<u16>,
    accept: Vec<u16>,
    text: Vec<u16>,
    dangerous: bool,
}

impl PreparedReview {
    fn new(
        title: &str,
        explanation: &str,
        accept: &str,
        detail: &str,
        dangerous: bool,
    ) -> Result<Self, String> {
        if detail.len() > MAX_REVIEW_BYTES
            || explanation.len() > 8 * 1024
            || title.len() > 256
            || accept.len() > 256
            || detail.is_empty()
            || title.is_empty()
            || accept.is_empty()
        {
            return Err("Native review text exceeds its complete-display bound".into());
        }
        Ok(Self {
            title: display_text(&[title])?,
            accept: display_text(&[accept])?,
            text: display_text(&[explanation, "\n\n", detail])?,
            dangerous,
        })
    }
}

// CR/LF becomes Windows CRLF. Invisible control/directional formatting is
// shown as a visible JSON Unicode escape, not applied to trusted field labels.
// NUL is rejected instead of terminating a Win32 string early.
fn display_text(parts: &[&str]) -> Result<Vec<u16>, String> {
    let capacity = parts
        .iter()
        .try_fold(1usize, |n, part| n.checked_add(part.len().checked_mul(6)?))
        .ok_or("Native review text length overflow")?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(capacity)
        .map_err(|_| "Native review allocation failed")?;
    for part in parts {
        let mut chars = part.chars().peekable();
        while let Some(ch) = chars.next() {
            match ch {
                '\0' => return Err("Native review contains an embedded NUL".into()),
                '\r' => {
                    if chars.peek() == Some(&'\n') {
                        chars.next();
                    }
                    result.extend_from_slice(&[13, 10]);
                }
                '\n' => result.extend_from_slice(&[13, 10]),
                ch if visible_escape(ch) => {
                    let code = ch as u32;
                    let digits = b"0123456789abcdef";
                    result.extend_from_slice(&[b'\\' as u16, b'u' as u16]);
                    for shift in [12, 8, 4, 0] {
                        result.push(digits[((code >> shift) & 15) as usize] as u16);
                    }
                }
                ch => {
                    let mut units = [0; 2];
                    result.extend_from_slice(ch.encode_utf16(&mut units));
                }
            }
        }
    }
    if result.len() >= i32::MAX as usize {
        return Err("Native review UTF-16 text is too long".into());
    }
    result.push(0);
    Ok(result)
}

fn visible_escape(ch: char) -> bool {
    (ch.is_control() && ch != '\t')
        || matches!(ch, '\u{061c}' | '\u{200e}' | '\u{200f}' | '\u{202a}'..='\u{202e}'
            | '\u{2066}'..='\u{2069}')
}

fn approved(decision: Decision, verified: bool, stopped: bool, owner: bool) -> bool {
    decision == Decision::Approve && verified && !stopped && owner
}

fn restored_approval(
    candidate: bool,
    restoration: ParentRestoration,
    stopped: bool,
    uncertain: bool,
) -> bool {
    candidate && restoration == ParentRestoration::SameLiveOwner && !stopped && !uncertain
}

#[derive(Debug, PartialEq, Eq)]
enum PumpAction {
    Finished,
    Deny,
    Wait,
    Dispatch,
}

fn pump_action(destroyed: bool, stopped: bool, queued_message: bool) -> PumpAction {
    if destroyed {
        PumpAction::Finished
    } else if stopped {
        PumpAction::Deny
    } else if queued_message {
        PumpAction::Dispatch
    } else {
        PumpAction::Wait
    }
}

fn clicked_button(
    id: usize,
    notification: usize,
    control: HWND,
    approve: HWND,
    deny: HWND,
) -> Option<Decision> {
    if notification != BN_CLICKED as usize || control.is_null() {
        return None;
    }
    if id == APPROVE_ID && control == approve {
        Some(Decision::Approve)
    } else if id == DENY_ID && control == deny {
        Some(Decision::Deny)
    } else {
        None
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum CloseAttempt {
    Idle,
    Started,
    Unknown,
}

fn begin_close(attempt: &Cell<CloseAttempt>) -> bool {
    if attempt.get() != CloseAttempt::Idle {
        return false;
    }
    attempt.set(CloseAttempt::Started);
    true
}

fn finish_close(attempt: &Cell<CloseAttempt>, native_success: bool, destroyed: bool) {
    if !native_success || !destroyed {
        attempt.set(CloseAttempt::Unknown);
    }
}

fn close_loop_failed(attempt: CloseAttempt) -> bool {
    attempt == CloseAttempt::Unknown
}

fn repeated_close_decision(
    attempt: CloseAttempt,
    current: Decision,
    incoming: Decision,
) -> Decision {
    // Reentrancy can only downgrade a pending approval; it cannot request a
    // second physical close, upgrade Deny, or replace sticky unknown.
    if attempt == CloseAttempt::Unknown
        || current == Decision::Unavailable
        || incoming == Decision::Unavailable
    {
        Decision::Unavailable
    } else if current == Decision::Deny || incoming == Decision::Deny {
        Decision::Deny
    } else {
        current
    }
}

struct Dialog {
    lease: Arc<ReviewLease>,
    review: PreparedReview,
    hwnd: Cell<HWND>,
    edit: Cell<HWND>,
    approve: Cell<HWND>,
    deny: Cell<HWND>,
    banner: Cell<HWND>,
    brush: HBRUSH,
    verified: Cell<bool>,
    destroyed: Cell<bool>,
    close_attempt: Cell<CloseAttempt>,
    decision: Cell<Decision>,
}

impl Dialog {
    fn close(&self, decision: Decision) {
        // One physical attempt, including reentrant calls and error cleanup.
        // An unknown result cannot authorize another DestroyWindow or a new
        // decision; the possibly live WndProc storage remains retained below.
        if self.destroyed.get() {
            return;
        }
        if !begin_close(&self.close_attempt) {
            self.decision.set(repeated_close_decision(
                self.close_attempt.get(),
                self.decision.get(),
                decision,
            ));
            return;
        }
        self.decision.set(decision);
        let hwnd = self.hwnd.get();
        let native_success = if hwnd.is_null() {
            false
        } else {
            unsafe {
                KillTimer(hwnd, TIMER_ID);
                DestroyWindow(hwnd) != 0
            }
        };
        finish_close(&self.close_attempt, native_success, self.destroyed.get());
        if close_loop_failed(self.close_attempt.get()) {
            self.decision.set(Decision::Unavailable);
            self.lease.uncertain.store(true, Ordering::Release);
        }
    }

    fn allowed(&self) -> bool {
        approved(
            self.decision.get(),
            self.verified.get(),
            self.lease.stopped(),
            self.lease.parent_matches(),
        )
    }

    fn layout(&self) -> bool {
        let mut rect = RECT::default();
        unsafe {
            if GetClientRect(self.hwnd.get(), &mut rect) == 0 {
                return false;
            }
            let width = rect.right - rect.left;
            let height = rect.bottom - rect.top;
            if width < 540 || height < 300 {
                return false;
            }
            let button_y = height - 72;
            let half = (width - 48) / 2;
            MoveWindow(self.banner.get(), 16, 12, width - 32, 60, 1) != 0
                && MoveWindow(self.edit.get(), 16, 82, width - 32, button_y - 96, 1) != 0
                && MoveWindow(self.deny.get(), 16, button_y, half, 56, 1) != 0
                && MoveWindow(self.approve.get(), 32 + half, button_y, half, 56, 1) != 0
        }
    }
}

impl Drop for Dialog {
    fn drop(&mut self) {
        if !self.brush.is_null() {
            unsafe {
                DeleteObject(self.brush);
            }
        }
    }
}

unsafe extern "system" fn window_proc(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if message == WM_NCCREATE {
        let create = &*(lparam as *const CREATESTRUCTW);
        let pointer = create.lpCreateParams as *const Dialog;
        if pointer.is_null() {
            return 0;
        }
        let dialog = &*pointer;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, pointer as isize);
        dialog.hwnd.set(hwnd);
        dialog.lease.dialog_active.store(true, Ordering::Release);
        return 1;
    }
    let pointer = GetWindowLongPtrW(hwnd, GWLP_USERDATA) as *const Dialog;
    if pointer.is_null() {
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }
    // All mutable fields are Cells: Win32 calls below can synchronously reenter
    // this procedure, so no exclusive Rust reference is held across them.
    let dialog = &*pointer;
    match message {
        WM_CLOSE => {
            dialog.close(Decision::Deny);
            0
        }
        WM_TIMER if wparam == TIMER_ID => {
            if dialog.lease.stopped() || !dialog.lease.parent_matches() {
                dialog.close(Decision::Deny);
            }
            0
        }
        WM_COMMAND => {
            let id = wparam & 0xffff;
            let notification = (wparam >> 16) & 0xffff;
            let control = lparam as HWND;
            if let Some(decision) = clicked_button(
                id,
                notification,
                control,
                dialog.approve.get(),
                dialog.deny.get(),
            ) {
                if decision == Decision::Approve {
                    if dialog.verified.get()
                        && !dialog.lease.stopped()
                        && dialog.lease.parent_matches()
                    {
                        dialog.close(Decision::Approve);
                    } else {
                        dialog.close(Decision::Deny);
                    }
                } else {
                    dialog.close(Decision::Deny);
                }
            }
            0
        }
        WM_SIZE => {
            if !dialog.edit.get().is_null() && !dialog.layout() {
                dialog.close(Decision::Unavailable);
            }
            0
        }
        WM_GETMINMAXINFO => {
            let size = &mut *(lparam as *mut MINMAXINFO);
            size.ptMinTrackSize.x = 600;
            size.ptMinTrackSize.y = 440;
            0
        }
        WM_CTLCOLORSTATIC if lparam as HWND == dialog.banner.get() && dialog.review.dangerous => {
            SetTextColor(wparam as HDC, 0x00ff_ffff);
            SetBkColor(wparam as HDC, 0x0020_2090);
            dialog.brush as LRESULT
        }
        WM_NCDESTROY => {
            KillTimer(hwnd, TIMER_ID);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
            dialog.destroyed.set(true);
            dialog.lease.dialog_active.store(false, Ordering::Release);
            DefWindowProcW(hwnd, message, wparam, lparam)
        }
        _ => DefWindowProcW(hwnd, message, wparam, lparam),
    }
}

fn register_class() -> Result<HINSTANCE, String> {
    static REGISTERED: OnceLock<Result<u16, u32>> = OnceLock::new();
    let instance = unsafe { GetModuleHandleW(null()) };
    if instance.is_null() {
        return Err("Native review instance is unavailable".into());
    }
    match REGISTERED.get_or_init(|| unsafe {
        let class = WNDCLASSW {
            lpfnWndProc: Some(window_proc),
            hInstance: instance,
            hCursor: LoadCursorW(null_mut(), IDC_ARROW),
            hbrBackground: GetSysColorBrush(COLOR_BTNFACE),
            lpszClassName: CLASS_NAME,
            ..Default::default()
        };
        let atom = RegisterClassW(&class);
        if atom == 0 {
            Err(GetLastError())
        } else {
            Ok(atom)
        }
    }) {
        Ok(_) => Ok(instance),
        Err(_) => Err("Native review window class is unavailable".into()),
    }
}

fn window_bounds(work: RECT) -> Result<(i32, i32, i32, i32), String> {
    let width = work
        .right
        .checked_sub(work.left)
        .and_then(|n| n.checked_sub(32))
        .ok_or("Native review screen bounds are invalid")?
        .min(1000);
    let height = work
        .bottom
        .checked_sub(work.top)
        .and_then(|n| n.checked_sub(32))
        .ok_or("Native review screen bounds are invalid")?
        .min(760);
    if width < 600 || height < 440 {
        return Err("Native review needs room for full text and both buttons".into());
    }
    let x = work
        .left
        .checked_add((work.right - work.left - width) / 2)
        .ok_or("Native review screen bounds are invalid")?;
    let y = work
        .top
        .checked_add((work.bottom - work.top - height) / 2)
        .ok_or("Native review screen bounds are invalid")?;
    Ok((x, y, width, height))
}

unsafe fn child(
    parent: HWND,
    instance: HINSTANCE,
    class: *const u16,
    text: *const u16,
    style: u32,
    id: usize,
) -> Result<HWND, String> {
    let control = CreateWindowExW(
        0,
        class,
        text,
        WS_CHILD | WS_VISIBLE | style,
        0,
        0,
        1,
        1,
        parent,
        id as HMENU,
        instance,
        null(),
    );
    if control.is_null() {
        return Err("Native review control could not be created".into());
    }
    SendMessageW(
        control,
        WM_SETFONT,
        GetStockObject(DEFAULT_GUI_FONT) as usize,
        1,
    );
    Ok(control)
}

fn exact_readback(edit: HWND, expected: &[u16]) -> Result<(), String> {
    let length = expected
        .len()
        .checked_sub(1)
        .ok_or("Missing review terminator")?;
    let mut readback = Vec::new();
    readback
        .try_reserve_exact(expected.len())
        .map_err(|_| "Native review readback allocation failed")?;
    readback.resize(expected.len(), 0);
    unsafe {
        if GetWindowTextLengthW(edit) != length as i32 {
            return Err("Native review control did not retain the complete text".into());
        }
        let copied = GetWindowTextW(edit, readback.as_mut_ptr(), readback.len() as i32);
        if !complete_readback(expected, &readback, length as i32, copied) {
            return Err("Native review control text did not match the complete request".into());
        }
    }
    Ok(())
}

fn complete_readback(expected: &[u16], actual: &[u16], reported: i32, copied: i32) -> bool {
    let Some(length) = expected.len().checked_sub(1) else {
        return false;
    };
    expected.last() == Some(&0)
        && reported >= 0
        && copied >= 0
        && reported as usize == length
        && copied as usize == length
        && actual == expected
}

fn run_dialog(lease: Arc<ReviewLease>, review: PreparedReview) -> Result<bool, String> {
    let instance = register_class()?;
    let parent = lease.parent.load(Ordering::Acquire) as HWND;
    let mut monitor: MONITORINFO = unsafe { std::mem::zeroed() };
    monitor.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
    unsafe {
        if GetMonitorInfoW(
            MonitorFromWindow(parent, MONITOR_DEFAULTTONEAREST),
            &mut monitor,
        ) == 0
        {
            return Err("Native review screen is unavailable".into());
        }
    }
    let (x, y, width, height) = window_bounds(monitor.rcWork)?;
    let brush = unsafe { CreateSolidBrush(0x0020_2090) };
    if brush.is_null() {
        return Err("Native review warning style is unavailable".into());
    }
    let dialog = Box::new(Dialog {
        lease,
        review,
        brush,
        hwnd: Cell::new(null_mut()),
        edit: Cell::new(null_mut()),
        approve: Cell::new(null_mut()),
        deny: Cell::new(null_mut()),
        banner: Cell::new(null_mut()),
        verified: Cell::new(false),
        destroyed: Cell::new(false),
        close_attempt: Cell::new(CloseAttempt::Idle),
        decision: Cell::new(Decision::Pending),
    });
    let outcome = (|| -> Result<bool, String> {
        if dialog.lease.stopped() || !dialog.lease.parent_matches() {
            return Ok(false);
        }
        let hwnd = unsafe {
            CreateWindowExW(
                WS_EX_DLGMODALFRAME | WS_EX_CONTROLPARENT,
                CLASS_NAME,
                dialog.review.title.as_ptr(),
                WS_POPUP | WS_CAPTION | WS_SYSMENU | WS_THICKFRAME,
                x,
                y,
                width,
                height,
                parent,
                null_mut(),
                instance,
                (&*dialog as *const Dialog).cast(),
            )
        };
        if hwnd.is_null() {
            return Err("Native review window could not be created".into());
        }
        unsafe {
            if GetWindow(hwnd, GW_OWNER) != parent || !dialog.lease.parent_matches() {
                return Err("Native review owner changed before display".into());
            }
            let banner_text = if dialog.review.dangerous {
                w!(
                    "DANGEROUS REQUEST: serious or lasting changes\r\nRead the complete request. Formatting controls are shown as \\u escapes."
                )
            } else {
                w!(
                    "Review the complete Tobkiri request\r\nFormatting controls are shown as \\u escapes. Closing this window denies."
                )
            };
            dialog.banner.set(child(
                hwnd,
                instance,
                w!("STATIC"),
                banner_text,
                SS_LEFT | SS_NOPREFIX,
                0,
            )?);
            dialog.edit.set(child(
                hwnd,
                instance,
                w!("EDIT"),
                w!(""),
                WS_TABSTOP
                    | WS_VSCROLL
                    | WS_HSCROLL
                    | ES_MULTILINE as u32
                    | ES_AUTOVSCROLL as u32
                    | ES_AUTOHSCROLL as u32
                    | ES_READONLY as u32
                    | ES_NOHIDESEL as u32,
                0,
            )?);
            dialog.deny.set(child(
                hwnd,
                instance,
                w!("BUTTON"),
                w!("Deny"),
                WS_TABSTOP | BS_PUSHBUTTON as u32 | BS_MULTILINE as u32,
                DENY_ID,
            )?);
            dialog.approve.set(child(
                hwnd,
                instance,
                w!("BUTTON"),
                dialog.review.accept.as_ptr(),
                WS_TABSTOP | WS_DISABLED | BS_PUSHBUTTON as u32 | BS_MULTILINE as u32,
                APPROVE_ID,
            )?);
            // Standard EDIT defaults to 32,767 input units. Set a checked full
            // limit explicitly, and verify actual retained UTF-16, not just the
            // requested limit or a prefix, before enabling approval.
            let units = dialog.review.text.len() - 1;
            SendMessageW(dialog.edit.get(), EM_SETLIMITTEXT, units.max(1), 0);
            if SendMessageW(dialog.edit.get(), EM_GETLIMITTEXT, 0, 0) < units as isize
                || SetWindowTextW(dialog.edit.get(), dialog.review.text.as_ptr()) == 0
            {
                return Err("Native review control could not accept the complete text".into());
            }
            exact_readback(dialog.edit.get(), &dialog.review.text)?;
            if !dialog.layout() {
                return Err("Native review layout could not be displayed".into());
            }
            if dialog.lease.stopped() || !dialog.lease.parent_matches() {
                return Ok(false);
            }
            dialog.verified.set(true);
            EnableWindow(dialog.approve.get(), 1);
            if IsWindowEnabled(dialog.approve.get()) == 0 {
                return Err("Native review approval control is unavailable".into());
            }
            if SetTimer(hwnd, TIMER_ID, 50, None) == 0 {
                return Err("Native review cancellation monitor is unavailable".into());
            }
            ShowWindow(hwnd, SW_SHOW);
            SetForegroundWindow(hwnd);
            SetFocus(dialog.deny.get());
            while !dialog.destroyed.get() {
                if close_loop_failed(dialog.close_attempt.get()) {
                    return Err("Native review destruction was not confirmed".into());
                }
                if dialog.lease.stopped() || !dialog.lease.parent_matches() {
                    dialog.close(Decision::Deny);
                    continue;
                }
                // A cross-thread sent WM_CLOSE can run inside message
                // retrieval and destroy the timer/window without producing a
                // queued message. A bounded wait plus nonblocking Peek keeps
                // that path from trapping a worker in GetMessage forever.
                if MsgWaitForMultipleObjectsEx(0, null(), 50, QS_ALLINPUT, MWMO_INPUTAVAILABLE)
                    == WAIT_FAILED
                {
                    return Err("Native review message wait failed".into());
                }
                let mut message: MSG = std::mem::zeroed();
                let queued = PeekMessageW(&mut message, null_mut(), 0, 0, PM_REMOVE) != 0;
                if close_loop_failed(dialog.close_attempt.get()) {
                    return Err("Native review destruction was not confirmed".into());
                }
                // PeekMessage can dispatch sent messages itself. Check actual
                // destruction BEFORE inspecting or dispatching any queued row.
                match pump_action(
                    dialog.destroyed.get(),
                    dialog.lease.stopped() || !dialog.lease.parent_matches(),
                    queued,
                ) {
                    PumpAction::Finished => break,
                    PumpAction::Deny => {
                        dialog.close(Decision::Deny);
                        continue;
                    }
                    PumpAction::Wait => continue,
                    PumpAction::Dispatch => {}
                }
                if message.message == WM_QUIT {
                    return Err(
                        "Native review message loop ended without a confirmed decision".into(),
                    );
                }
                // Never map Enter to a default approval. Space/click on the
                // explicitly focused Approve control remains an explicit act.
                if matches!(
                    message.message,
                    WM_KEYDOWN | WM_SYSKEYDOWN | WM_CHAR | WM_SYSCHAR
                ) {
                    if message.wParam == VK_ESCAPE as usize {
                        dialog.close(Decision::Deny);
                        continue;
                    }
                    if message.wParam == VK_RETURN as usize {
                        continue;
                    }
                }
                if dialog.lease.stopped() || !dialog.lease.parent_matches() {
                    dialog.close(Decision::Deny);
                    continue;
                }
                if IsDialogMessageW(hwnd, &message) == 0 {
                    TranslateMessage(&message);
                    DispatchMessageW(&message);
                }
            }
        }
        match dialog.decision.get() {
            Decision::Approve => Ok(dialog.allowed()),
            Decision::Deny => Ok(false),
            _ => Err("Native review did not finish with a confirmed decision".into()),
        }
    })();
    if !dialog.hwnd.get().is_null()
        && !dialog.destroyed.get()
        && dialog.close_attempt.get() == CloseAttempt::Idle
    {
        dialog.close(Decision::Unavailable);
    }
    if !dialog.hwnd.get().is_null() && !dialog.destroyed.get() {
        // A live WndProc must never point at freed Rust storage. Bound future
        // admission to zero rather than reusing a possibly live dialog.
        dialog.lease.uncertain.store(true, Ordering::Release);
        std::mem::forget(dialog);
        return Err("Native review destruction was not confirmed".into());
    }
    outcome
}

/// Display the complete trusted Core review. No result is approved unless the
/// actual read-only control retained the full view, the user clicked Approve,
/// the owned dialog was destroyed and its parent was restored, and cancellation
/// and expiry are still false. This function must run on a blocking worker.
pub(crate) fn show_detail_cancellable_with_warning(
    window: &tauri::WebviewWindow,
    title: &str,
    explanation: &str,
    accept: &str,
    detail: &str,
    cancelled: Arc<AtomicBool>,
    dangerous: bool,
) -> Result<bool, String> {
    let review = PreparedReview::new(title, explanation, accept, detail, dangerous)?;
    let deadline = Instant::now() + Duration::from_secs(REVIEW_SECONDS);
    let lease = ReviewLease::acquire(cancelled, deadline)?;
    if lease.stopped() {
        return Ok(false);
    }
    let mut parent = ParentLease::acquire(window, lease.clone())?;
    let outcome = run_dialog(lease.clone(), review);
    let restoration = parent.restore()?;
    match outcome {
        Ok(true)
            if restored_approval(
                true,
                restoration,
                lease.stopped(),
                lease.uncertain.load(Ordering::Acquire),
            ) =>
        {
            Ok(true)
        }
        Ok(_) => Ok(false),
        Err(error) => Err(error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(units: &[u16]) -> String {
        assert_eq!(units.last(), Some(&0));
        String::from_utf16(&units[..units.len() - 1]).unwrap()
    }

    #[test]
    fn complete_maximum_ascii_review_and_suffix_exceed_edit_default() {
        let mut detail = "x".repeat(MAX_REVIEW_BYTES - 9);
        detail.push_str("END-exact");
        let prepared = PreparedReview::new("Tobkiri", "Review", "Approve", &detail, false).unwrap();
        assert!(prepared.text.len() > 32767);
        assert_eq!(text(&prepared.text), format!("Review\r\n\r\n{detail}"));
        detail.push('x');
        assert!(PreparedReview::new("Tobkiri", "Review", "Approve", &detail, false).is_err());
    }

    #[test]
    fn complete_unicode_surrogate_pairs_and_last_binding_survive() {
        let detail = format!("{}\nlast_binding:正確🔐", "完整🔍".repeat(20000));
        let prepared = PreparedReview::new("Tobkiri", "Review", "Approve", &detail, true).unwrap();
        assert!(prepared.dangerous);
        assert_eq!(
            text(&prepared.text),
            format!("Review\r\n\r\n{}", detail.replace('\n', "\r\n"))
        );
        assert!(text(&prepared.text).ends_with("last_binding:正確🔐"));
    }

    #[test]
    fn windows_line_endings_are_normalized_without_dropping_lines() {
        assert_eq!(
            text(&display_text(&["a\nb\rc\r\nd"]).unwrap()),
            "a\r\nb\r\nc\r\nd"
        );
    }

    #[test]
    fn nul_never_silently_truncates_any_control() {
        for (title, explanation, accept, detail) in [
            ("a\0b", "explain", "accept", "detail"),
            ("title", "a\0b", "accept", "detail"),
            ("title", "explain", "a\0b", "detail"),
            ("title", "explain", "accept", "a\0b"),
        ] {
            assert!(PreparedReview::new(title, explanation, accept, detail, false).is_err());
        }
    }

    #[test]
    fn invisible_direction_and_control_bytes_are_visible_not_executed() {
        let rendered = text(&display_text(&["a\u{202e}b\u{2066}c\u{0001}\t末尾"]).unwrap());
        assert_eq!(rendered, "a\\u202eb\\u2066c\\u0001\t末尾");
    }

    #[test]
    fn plain_review_json_keeps_all_bindings_and_semantic_unicode() {
        let original =
            serde_json::json!({"risk":"Dangerous", "binding":"exact🔐", "text":"a\u{202e}b"});
        let raw = serde_json::to_string_pretty(&original).unwrap();
        let visible = text(&display_text(&[&raw]).unwrap());
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&visible).unwrap(),
            original
        );
    }

    #[test]
    fn only_explicit_approval_with_full_view_and_current_owner_succeeds() {
        assert!(approved(Decision::Approve, true, false, true));
        for decision in [Decision::Pending, Decision::Deny, Decision::Unavailable] {
            assert!(!approved(decision, true, false, true));
        }
        assert!(!approved(Decision::Approve, false, false, true));
        assert!(!approved(Decision::Approve, true, true, true));
        assert!(!approved(Decision::Approve, true, false, false));
    }

    #[test]
    fn owner_loss_before_restore_cleans_up_without_approving_old_click() {
        let restoration = owner_loss(false, false).unwrap();
        assert_eq!(restoration, ParentRestoration::OwnerGone);
        assert!(restoration.cleanup_complete());
        assert!(!restored_approval(true, restoration, false, false));
        let recycled = owner_loss(false, true).unwrap();
        assert_eq!(recycled, ParentRestoration::Unknown);
        assert!(!recycled.cleanup_complete());
        assert!(!restored_approval(true, recycled, false, false));
    }

    #[test]
    fn owner_loss_inside_enable_window_cannot_reuse_pre_restore_liveness() {
        assert_eq!(owner_loss(true, true), None);
        // EnableWindow may synchronously reenter and destroy the same owner.
        let after_enable = owner_loss(false, false).unwrap();
        assert!(!restored_approval(true, after_enable, false, false));
        assert!(!restored_approval(
            true,
            ParentRestoration::Unknown,
            false,
            false
        ));
        assert!(restored_approval(
            true,
            ParentRestoration::SameLiveOwner,
            false,
            false
        ));
        assert!(!restored_approval(
            true,
            ParentRestoration::SameLiveOwner,
            true,
            false
        ));
        assert!(!restored_approval(
            true,
            ParentRestoration::SameLiveOwner,
            false,
            true
        ));
    }

    #[test]
    fn sent_close_without_queued_message_finishes_before_dispatch() {
        assert_eq!(pump_action(false, false, false), PumpAction::Wait);
        // Peek dispatches a sent WM_CLOSE, destroying window and timer, yet
        // returns false because there was no queued message to remove.
        assert_eq!(pump_action(true, false, false), PumpAction::Finished);
        // A queued row remaining after the sent close must not be dispatched.
        assert_eq!(pump_action(true, false, true), PumpAction::Finished);
        assert_eq!(pump_action(false, true, false), PumpAction::Deny);
        assert_eq!(pump_action(false, true, true), PumpAction::Deny);
        assert_eq!(pump_action(false, false, true), PumpAction::Dispatch);
    }

    #[test]
    fn control_id_or_menu_command_alone_cannot_approve() {
        // Opaque values only: this unit test does not call Win32.
        let approve = 10usize as HWND;
        let deny = 11usize as HWND;
        assert_eq!(
            clicked_button(APPROVE_ID, BN_CLICKED as usize, approve, approve, deny),
            Some(Decision::Approve)
        );
        assert_eq!(
            clicked_button(DENY_ID, BN_CLICKED as usize, deny, approve, deny),
            Some(Decision::Deny)
        );
        assert_eq!(
            clicked_button(APPROVE_ID, 0, null_mut(), approve, deny),
            None
        );
        assert_eq!(clicked_button(APPROVE_ID, 1, approve, approve, deny), None);
        assert_eq!(clicked_button(APPROVE_ID, 0, deny, approve, deny), None);
        assert_eq!(clicked_button(DENY_ID, 0, approve, approve, deny), None);
    }

    #[test]
    fn readback_must_include_entire_suffix_and_exact_count() {
        let expected = display_text(&[&format!("{}last🔐", "x".repeat(40000))]).unwrap();
        let length = (expected.len() - 1) as i32;
        assert!(complete_readback(&expected, &expected, length, length));
        let mut wrong_suffix = expected.clone();
        wrong_suffix[40000] = b'X' as u16;
        assert!(!complete_readback(&expected, &wrong_suffix, length, length));
        assert!(!complete_readback(
            &expected,
            &expected[..32768],
            32767,
            32767
        ));
        assert!(!complete_readback(&expected, &expected, length, length - 1));
        assert!(!complete_readback(&expected, &expected, -1, length));
        assert!(!complete_readback(&[], &[], 0, 0));
    }

    #[test]
    fn queued_parent_restore_retains_single_review_admission() {
        let cancelled = Arc::new(AtomicBool::new(false));
        let worker =
            ReviewLease::acquire(cancelled.clone(), Instant::now() + Duration::from_secs(10))
                .unwrap();
        let queued_restore = worker.clone();
        worker.parent_active.store(true, Ordering::Release);
        assert!(ReviewLease::acquire(cancelled, Instant::now()).is_err());
        assert!(!worker.stopped());
        worker.cancelled.store(true, Ordering::Release);
        assert!(worker.stopped());
        drop(worker);
        assert!(REVIEW_OPEN.load(Ordering::Acquire));
        queued_restore.parent_active.store(false, Ordering::Release);
        drop(queued_restore);
        assert!(!REVIEW_OPEN.load(Ordering::Acquire));
    }

    #[test]
    fn small_and_offset_screens_keep_window_and_both_actions_in_work_area() {
        for (left, top, width, height) in [
            (0, 0, 640, 480),
            (-1920, 0, 1920, 1080),
            (100, 80, 800, 600),
        ] {
            let work = RECT {
                left,
                top,
                right: left + width,
                bottom: top + height,
            };
            let (x, y, w, h) = window_bounds(work).unwrap();
            assert!(x >= left && y >= top && x + w <= work.right && y + h <= work.bottom);
            assert!(w >= 600 && h >= 440);
        }
        assert!(window_bounds(RECT {
            left: 0,
            top: 0,
            right: 500,
            bottom: 300
        })
        .is_err());
    }

    #[test]
    fn invalid_screen_arithmetic_fails_closed() {
        assert!(window_bounds(RECT {
            left: i32::MIN,
            top: 0,
            right: i32::MAX,
            bottom: 1000
        })
        .is_err());
        assert!(window_bounds(RECT {
            left: 1000,
            top: 0,
            right: 0,
            bottom: 1000
        })
        .is_err());
    }
    #[test]
    fn native_close_failure_stops_loop_before_a_second_destroy_attempt() {
        let attempt = Cell::new(CloseAttempt::Idle);
        let mut physical_calls = 0;
        if begin_close(&attempt) {
            physical_calls += 1;
            finish_close(&attempt, false, false);
        }
        assert!(close_loop_failed(attempt.get()));
        // Used by both loop checks: cancellation may remain true forever,
        // but unknown close exits to retained-storage Err, not close+continue.
        assert!(!begin_close(&attempt));
        assert_eq!(physical_calls, 1);
        assert_eq!(attempt.get(), CloseAttempt::Unknown);
    }

    #[test]
    fn native_success_without_nc_destroy_is_sticky_unknown() {
        let attempt = Cell::new(CloseAttempt::Idle);
        assert!(begin_close(&attempt));
        finish_close(&attempt, true, false);
        assert!(close_loop_failed(attempt.get()));
        assert!(!begin_close(&attempt));
        assert!(!restored_approval(
            true,
            ParentRestoration::SameLiveOwner,
            false,
            close_loop_failed(attempt.get())
        ));
    }

    #[test]
    fn reentrant_close_is_not_a_second_physical_attempt() {
        let attempt = Cell::new(CloseAttempt::Idle);
        assert!(begin_close(&attempt));
        assert!(!begin_close(&attempt));
        assert_eq!(
            repeated_close_decision(attempt.get(), Decision::Approve, Decision::Deny),
            Decision::Deny
        );
        assert_eq!(
            repeated_close_decision(attempt.get(), Decision::Deny, Decision::Approve),
            Decision::Deny
        );
        assert_eq!(
            repeated_close_decision(
                CloseAttempt::Unknown,
                Decision::Unavailable,
                Decision::Approve
            ),
            Decision::Unavailable
        );
        finish_close(&attempt, true, true);
        assert!(!close_loop_failed(attempt.get()));
        assert!(!begin_close(&attempt));
        // NCDESTROY remains the separate proof for freeing WndProc storage.
        assert_eq!(attempt.get(), CloseAttempt::Started);
    }
}

// These tests deliberately require an operator and one exact selector per
// process. They run a real Wry/Win32 renderer, never Core or Gate.
#[cfg(all(test, windows))]
mod manual_gui {
    use super::*;
    use std::sync::Mutex;

    const PARENT_LABEL: &str = "r48-native-renderer-parent";
    const NORMAL_SELECTOR: &str =
        "native_plan_review_windows::manual_gui::manual_full_unicode_review_normal";
    const DANGEROUS_SELECTOR: &str =
        "native_plan_review_windows::manual_gui::manual_full_unicode_review_dangerous";

    fn exact_manual_selection(args: &[std::ffi::OsString], expected: &str) -> bool {
        if !matches!(expected, NORMAL_SELECTOR | DANGEROUS_SELECTOR) {
            return false;
        }
        let (mut selector, mut ignored, mut exact, mut nocapture, mut threads) =
            (false, false, false, false, false);
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            let Some(arg) = arg.to_str() else {
                return false;
            };
            match arg {
                "--ignored" if !ignored => ignored = true,
                "--exact" if !exact => exact = true,
                "--nocapture" if !nocapture => nocapture = true,
                "--test-threads=1" if !threads => threads = true,
                "--test-threads" if !threads => {
                    if args.next().and_then(|value| value.to_str()) != Some("1") {
                        return false;
                    }
                    threads = true;
                }
                arg if arg == expected && !selector => selector = true,
                // This includes every other positional, unknown/duplicate
                // flag, second selector, and unexpected flag-value form.
                _ => return false,
            }
        }
        selector && ignored && exact && nocapture && threads
    }

    fn full_unicode_fixture() -> String {
        let prefix = "{\n  \"test_only\": true,\n  \"caller\": \"RENDERER FIXTURE - no Core request\",\n  \"profile\": \"TEST-PROFILE\",\n  \"padding\": \"";
        let suffix = "\",\n  \"last_binding\": \"R48-FULL-REVIEW-END-正確🔐\",\n  \"fixture_digest\": \"sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee\"\n}";
        let padding = MAX_REVIEW_BYTES - prefix.len() - suffix.len();
        let unit = "完全🔐";
        let mut detail = String::with_capacity(MAX_REVIEW_BYTES);
        detail.push_str(prefix);
        detail.push_str(&unit.repeat(padding / unit.len()));
        detail.push_str(&"x".repeat(padding % unit.len()));
        detail.push_str(suffix);
        assert_eq!(detail.len(), MAX_REVIEW_BYTES);
        assert!(detail.encode_utf16().count() > 32767);
        let value: serde_json::Value = serde_json::from_str(&detail).unwrap();
        assert_eq!(value["last_binding"], "R48-FULL-REVIEW-END-正確🔐");
        detail
    }

    fn run_manual_renderer(dangerous: bool) {
        let expected = if dangerous {
            DANGEROUS_SELECTOR
        } else {
            NORMAL_SELECTOR
        };
        let args: Vec<_> = std::env::args_os().skip(1).collect();
        assert!(
            exact_manual_selection(&args, expected),
            "manual GUI requires --ignored --exact and exactly one full test selector"
        );
        // Test-only dependency; no DataRoot, live Profile, shared cache, Core
        // constructor, startup function, plugin or command handler is used.
        let cache = tempfile::Builder::new()
            .prefix("tobkiri-r48-renderer-")
            .tempdir()
            .expect("fresh test-only WebView cache");
        let cache_path = cache.path().join("webview");
        let mut context = tauri::generate_context!("tauri.core-host.conf.json");
        context.config_mut().identifier = "dev.tobkiri.r48-renderer-test".into();
        context.config_mut().app.windows.clear();
        // This clears only Config's list, not Context.runtime_authority.
        // Isolation here comes from no Core/handler/plugin registration,
        // a different window label, about:blank and disabled JavaScript.
        context.config_mut().app.security.capabilities.clear();
        context.config_mut().app.with_global_tauri = false;
        context.config_mut().app.tray_icon = None;

        let detail = full_unicode_fixture();
        let cancelled = Arc::new(AtomicBool::new(false));
        let finished = Arc::new(AtomicBool::new(false));
        let worker_slot = Arc::new(Mutex::new(None::<std::thread::JoinHandle<()>>));
        let shutdown_error = Arc::new(Mutex::new(None::<String>));
        let (result_tx, result_rx) = mpsc::sync_channel::<Result<bool, String>>(1);
        let setup_cancelled = cancelled.clone();
        let setup_finished = finished.clone();
        let setup_slot = worker_slot.clone();
        let setup_shutdown_error = shutdown_error.clone();
        let app = tauri::Builder::default()
            .any_thread()
            .setup(move |app| {
                let parent = tauri::WebviewWindowBuilder::new(
                    app, PARENT_LABEL,
                    tauri::WebviewUrl::External("about:blank".parse().unwrap()),
                )
                .title("Tobkiri renderer TEST ONLY - no Core authority")
                .inner_size(800.0, 600.0)
                .data_directory(cache_path)
                .disable_javascript()
                .on_navigation(|url| url.as_str() == "about:blank")
                .build()?;
                let handle = app.handle().clone();
                let worker = std::thread::Builder::new()
                    .name("r48-manual-renderer".into())
                    .spawn(move || {
                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            show_detail_cancellable_with_warning(
                                &parent,
                                "TEST ONLY: complete Tobkiri review renderer",
                                "This is a renderer fixture, not a Core approval request. No action will be authorized. Scroll to R48-FULL-REVIEW-END-正確🔐. Choose Approve, Deny, close or Escape manually. The normal four-minute review limit applies.",
                                if dangerous { "TEST: approve dangerous fixture" } else { "TEST: approve fixture" },
                                &detail,
                                setup_cancelled.clone(), dangerous,
                            )
                        })).unwrap_or_else(|_| Err("Manual renderer worker panicked".into()));
                        eprintln!("R48 renderer-only response (NO Gate authority): {result:?}");
                        let _ = result_tx.send(result);
                        // Do not call AppHandle::exit(0): its failure fallback
                        // can process::exit and bypass the assertions below.
                        let close_finished = setup_finished.clone();
                        let close_error = setup_shutdown_error.clone();
                        if handle.run_on_main_thread(move || {
                            close_finished.store(true, Ordering::Release);
                            if parent.close().is_err() {
                                if let Ok(mut error) = close_error.lock() {
                                    *error = Some("Test parent close could not be scheduled".into());
                                }
                            }
                        }).is_err() {
                            setup_cancelled.store(true, Ordering::Release);
                            setup_finished.store(true, Ordering::Release);
                            if let Ok(mut error) = setup_shutdown_error.lock() {
                                *error = Some("Test shutdown callback could not be scheduled; close parent manually".into());
                            }
                        }
                    })?;
                *setup_slot.lock().map_err(|_| std::io::Error::other("test worker slot poisoned"))? = Some(worker);
                Ok(())
            })
            .build(context)
            .expect("build the controlled renderer-only parent");

        let event_cancelled = cancelled.clone();
        let event_finished = finished.clone();
        let exit_code = app.run_return(move |_handle, event| {
            match event {
                tauri::RunEvent::WindowEvent {
                    label,
                    event: tauri::WindowEvent::CloseRequested { api, .. },
                    ..
                } if label == PARENT_LABEL && !event_finished.load(Ordering::Acquire) => {
                    // A normal parent-close request first cancels the owned
                    // dialog. Keep the event loop alive for its restore ACK.
                    event_cancelled.store(true, Ordering::Release);
                    api.prevent_close();
                }
                tauri::RunEvent::ExitRequested { .. } => {
                    event_cancelled.store(true, Ordering::Release);
                }
                _ => {}
            }
        });
        // The event loop is no longer live here; no blocking worker join or
        // channel receive ran on its UI thread while the dialog needed it.
        cancelled.store(true, Ordering::Release);
        worker_slot
            .lock()
            .unwrap()
            .take()
            .expect("renderer worker exists")
            .join()
            .expect("renderer worker joined");
        let result = result_rx
            .try_recv()
            .expect("actual renderer result was retained");
        let shutdown_error = shutdown_error.lock().unwrap().take();
        println!(
            "R48_RENDERER_ONLY_COMPLETE {}",
            serde_json::json!({
                "dangerous_fixture": dangerous, "detail_utf8_bytes": MAX_REVIEW_BYTES,
                "native_response": &result, "app_exit_code": exit_code,
                "shutdown_error": &shutdown_error, "core_or_gate_authority": false,
            })
        );
        assert_eq!(exit_code, 0, "normal event-loop exit required");
        assert!(
            shutdown_error.is_none(),
            "test parent shutdown was not confirmed"
        );
        assert!(
            result.is_ok(),
            "renderer returned Unavailable, not a manual response"
        );
        // Cache deletion is best effort in TempDir::drop. WebView/OS physical
        // cleanup is not certified by this renderer test or by its exit code.
        drop(cache);
    }

    fn os_args(args: &[&str]) -> Vec<std::ffi::OsString> {
        args.iter()
            .map(|arg| std::ffi::OsString::from(*arg))
            .collect()
    }

    fn valid_args(selector: &str) -> Vec<std::ffi::OsString> {
        os_args(&[
            selector,
            "--ignored",
            "--exact",
            "--test-threads=1",
            "--nocapture",
        ])
    }

    #[test]
    fn exact_manual_selection_accepts_only_the_documented_complete_forms() {
        assert!(exact_manual_selection(
            &valid_args(NORMAL_SELECTOR),
            NORMAL_SELECTOR
        ));
        assert!(exact_manual_selection(
            &valid_args(DANGEROUS_SELECTOR),
            DANGEROUS_SELECTOR
        ));
        assert!(exact_manual_selection(
            &os_args(&[
                "--ignored",
                "--exact",
                NORMAL_SELECTOR,
                "--test-threads",
                "1",
                "--nocapture"
            ]),
            NORMAL_SELECTOR
        ));
        for missing in 0..5 {
            let mut args = valid_args(NORMAL_SELECTOR);
            args.remove(missing);
            assert!(!exact_manual_selection(&args, NORMAL_SELECTOR));
        }
    }

    #[test]
    fn unknown_positionals_flags_duplicates_and_thread_values_cannot_open_gui() {
        for extra in [
            "another_ignored_test",
            NORMAL_SELECTOR,
            DANGEROUS_SELECTOR,
            "--unknown",
            "--include-ignored",
            "--ignored",
            "--exact",
            "--nocapture",
            "--test-threads=1",
            "--test-threads=2",
            "1",
            "--",
        ] {
            let mut args = valid_args(NORMAL_SELECTOR);
            args.push(extra.into());
            assert!(
                !exact_manual_selection(&args, NORMAL_SELECTOR),
                "unexpected {extra}"
            );
        }
        assert!(!exact_manual_selection(
            &os_args(&["--ignored"]),
            NORMAL_SELECTOR
        ));
        assert!(!exact_manual_selection(
            &os_args(&[
                "--ignored",
                "--exact",
                "native_plan_review_windows::manual_gui",
                "--test-threads=1",
                "--nocapture"
            ]),
            NORMAL_SELECTOR
        ));
        for value in ["0", "2", "--nocapture", ""] {
            assert!(!exact_manual_selection(
                &os_args(&[
                    NORMAL_SELECTOR,
                    "--ignored",
                    "--exact",
                    "--nocapture",
                    "--test-threads",
                    value
                ]),
                NORMAL_SELECTOR
            ));
        }
        assert!(!exact_manual_selection(
            &valid_args(NORMAL_SELECTOR),
            "another_ignored_test"
        ));
    }

    #[test]
    fn non_unicode_anywhere_is_rejected_before_context_or_window_creation() {
        use std::os::windows::ffi::OsStringExt;
        let invalid = std::ffi::OsString::from_wide(&[0xd800]);
        let mut args = valid_args(NORMAL_SELECTOR);
        args.push(invalid.clone());
        assert!(!exact_manual_selection(&args, NORMAL_SELECTOR));
        let mut args = os_args(&[
            NORMAL_SELECTOR,
            "--ignored",
            "--exact",
            "--nocapture",
            "--test-threads",
        ]);
        args.push(invalid);
        assert!(!exact_manual_selection(&args, NORMAL_SELECTOR));
    }

    #[test]
    #[ignore = "manual Windows GUI only; select this exact test in a fresh process"]
    fn manual_full_unicode_review_normal() {
        run_manual_renderer(false);
    }

    #[test]
    #[ignore = "manual Windows GUI only; select this exact test in a fresh process"]
    fn manual_full_unicode_review_dangerous() {
        run_manual_renderer(true);
    }
}
