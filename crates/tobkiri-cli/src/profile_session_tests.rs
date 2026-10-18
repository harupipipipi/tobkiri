use super::*;
use std::{cell::RefCell, collections::VecDeque, rc::Rc};

fn command(id: u64) -> Vec<u8> {
    format!(
        r#"{{"id":{id},"shape":"local.fixture.control@1.0","op":"status","args":{{"key":"same"}}}}"#
    )
    .into_bytes()
}

type Trace = Rc<RefCell<Vec<String>>>;

struct Lines {
    lines: VecDeque<Result<Option<Vec<u8>>>>,
    reads: usize,
    trace: Trace,
}
impl Lines {
    fn new(lines: impl IntoIterator<Item = Vec<u8>>, trace: Trace) -> Self {
        Self {
            lines: lines.into_iter().map(|line| Ok(Some(line))).collect(),
            reads: 0,
            trace,
        }
    }
}
#[async_trait(?Send)]
impl RequestInput for Lines {
    async fn next(&mut self) -> Result<Option<Vec<u8>>> {
        self.reads += 1;
        self.trace.borrow_mut().push(format!("read{}", self.reads));
        self.lines.pop_front().unwrap_or(Ok(None))
    }
}

struct Calls {
    ids: Vec<u64>,
    fail: bool,
    fail_close: bool,
    closes: usize,
    trace: Trace,
}
impl Calls {
    fn new(trace: Trace) -> Self {
        Self {
            ids: Vec::new(),
            fail: false,
            fail_close: false,
            closes: 0,
            trace,
        }
    }
}
#[async_trait(?Send)]
impl Dispatcher for Calls {
    async fn invoke(&mut self, request: &Invocation) -> Result<()> {
        self.ids.push(request.id);
        self.trace
            .borrow_mut()
            .push(format!("invoke{}", request.id));
        tokio::task::yield_now().await;
        self.trace
            .borrow_mut()
            .push(format!("drained{}", request.id));
        if self.fail {
            bail!("stream outcome or cleanup unknown; no replay");
        }
        Ok(())
    }
    async fn close(&mut self) -> Result<(), CoreError> {
        self.closes += 1;
        self.trace.borrow_mut().push("close".into());
        tokio::task::yield_now().await;
        self.trace.borrow_mut().push("closed".into());
        if self.fail_close {
            Err(CoreError::Runtime("physical cleanup unknown".into()))
        } else {
            Ok(())
        }
    }
}

fn setup(lines: impl IntoIterator<Item = Vec<u8>>) -> (Lines, Calls, Trace) {
    let trace = Rc::new(RefCell::new(Vec::new()));
    (
        Lines::new(lines, trace.clone()),
        Calls::new(trace.clone()),
        trace,
    )
}

#[test]
fn request_schema_is_generic_and_preserves_typed_arguments() {
    let Request::Invoke(parsed) = parse_request(&command(1)).unwrap() else {
        panic!("not an invocation");
    };
    assert_eq!(parsed.id, 1);
    assert_eq!(parsed.shape.to_string(), "local.fixture.control@1.0");
    assert_eq!(parsed.operation.as_str(), "status");
    assert_eq!(
        parsed.args,
        tobkiri_core::parse(br#"{"key":"same"}"#, Variant::StrictV1).unwrap()
    );
    let bytes = command(MAX_REQUEST_ID as u64);
    assert_eq!(parse_request(&bytes).unwrap().id(), MAX_REQUEST_ID as u64);
    assert!(matches!(
        parse_request(br#"{"id":2,"close":true}"#).unwrap(),
        Request::Close { id: 2 }
    ));
}

#[test]
fn malformed_duplicate_unknown_and_nonobject_fields_fail_before_dispatch() {
    for raw in [
        r#"[]"#,
        r#"{"id":1,"shape":"local.fixture.control@1.0","op":"status","args":{},"approved":true}"#,
        r#"{"id":1,"id":2,"shape":"local.fixture.control@1.0","op":"status","args":{}}"#,
        r#"{"id":1,"shape":"local.fixture.control@1.0","op":"status","args":{"x":1,"x":2}}"#,
        r#"{"id":1,"shape":"local.fixture.control@1.0","op":"status"}"#,
        r#"{"id":1,"shape":"local.fixture.control@1.0","op":"status","args":"{}"}"#,
        r#"{"id":0,"shape":"local.fixture.control@1.0","op":"status","args":{}}"#,
        r#"{"id":-1,"shape":"local.fixture.control@1.0","op":"status","args":{}}"#,
        r#"{"id":true,"shape":"local.fixture.control@1.0","op":"status","args":{}}"#,
        r#"{"id":9007199254740992,"shape":"local.fixture.control@1.0","op":"status","args":{}}"#,
        r#"{"id":1,"shape":"invalid","op":"status","args":{}}"#,
        r#"{"id":1,"close":false}"#,
        r#"{"id":1,"close":true,"shape":"local.fixture.control@1.0","op":"status","args":{}}"#,
        r#"{"id":1,"close":true,"approved":true}"#,
        r#"{"id":1,"close":true,"close":true}"#,
    ] {
        assert!(parse_request(raw.as_bytes()).is_err(), "{raw}");
    }
}

#[test]
fn exact_64k_request_is_allowed_and_one_extra_byte_is_rejected() {
    let prefix =
        br#"{"id":1,"shape":"local.fixture.control@1.0","op":"status","args":{"padding":""#;
    let suffix = br#""}}"#;
    let mut bytes = prefix.to_vec();
    bytes.resize(MAX_REQUEST_BYTES - suffix.len(), b'a');
    bytes.extend_from_slice(suffix);
    assert_eq!(bytes.len(), MAX_REQUEST_BYTES);
    assert!(parse_request(&bytes).is_ok());
    bytes.insert(prefix.len(), b'a');
    assert!(parse_request(&bytes).is_err());
}

#[tokio::test]
async fn two_commands_wait_for_each_dispatch_to_drain_then_close_once_at_eof() {
    let (mut input, mut calls, trace) = setup([command(1), command(2)]);
    let mut output = Vec::new();
    run_loop(&mut input, &mut calls, &mut output).await.unwrap();
    assert_eq!(calls.ids, [1, 2]);
    assert_eq!(calls.closes, 1);
    assert_eq!(
        &*trace.borrow(),
        &[
            "read1", "invoke1", "drained1", "read2", "invoke2", "drained2", "read3", "close",
            "closed"
        ]
    );
    let text = String::from_utf8(output).unwrap();
    assert!(text.contains(r#"{"kind":"request_started","id":1}"#));
    assert!(text.contains(r#"{"kind":"request_finished","id":2,"ok":true}"#));
    assert!(text.ends_with("{\"kind\":\"session_finished\",\"ok\":true}\n"));
}

#[tokio::test]
async fn idle_eof_awaits_close_without_invoking_a_port() {
    let (mut input, mut calls, trace) = setup([]);
    run_loop(&mut input, &mut calls, &mut Vec::new())
        .await
        .unwrap();
    assert_eq!(input.reads, 1);
    assert!(calls.ids.is_empty());
    assert_eq!(&*trace.borrow(), &["read1", "close", "closed"]);
}

#[tokio::test]
async fn idle_eof_preserves_a_close_failure() {
    let (mut input, mut calls, _) = setup([]);
    calls.fail_close = true;
    let mut output = Vec::new();
    let error = run_loop(&mut input, &mut calls, &mut output)
        .await
        .unwrap_err();
    assert!(format!("{error:#}").contains("physical cleanup unknown"));
    assert!(
        String::from_utf8(output)
            .unwrap()
            .contains(r#""kind":"session_finished","ok":false"#)
    );
    assert_eq!(calls.closes, 1);
}

#[tokio::test]
async fn explicit_close_after_two_commands_never_reads_or_invokes_the_fourth_line() {
    let (mut input, mut calls, _) = setup([
        command(1),
        command(2),
        br#"{"id":3,"close":true}"#.to_vec(),
        command(4),
    ]);
    let mut output = Vec::new();
    run_loop(&mut input, &mut calls, &mut output).await.unwrap();
    assert_eq!(input.reads, 3);
    assert_eq!(calls.ids, [1, 2]);
    assert_eq!(calls.closes, 1);
    let text = String::from_utf8(output).unwrap();
    assert!(text.ends_with(
        "{\"kind\":\"session_closing\",\"id\":3}\n{\"kind\":\"session_finished\",\"ok\":true}\n"
    ));
    assert!(!text.contains("\"kind\":\"request_started\",\"id\":3"));
}

#[tokio::test]
async fn explicit_close_first_is_a_native_control_without_any_port_dispatch() {
    let (mut input, mut calls, _) = setup([br#"{"id":1,"close":true}"#.to_vec(), command(2)]);
    run_loop(&mut input, &mut calls, &mut Vec::new())
        .await
        .unwrap();
    assert_eq!(input.reads, 1);
    assert!(calls.ids.is_empty());
    assert_eq!(calls.closes, 1);
}

#[tokio::test]
async fn explicit_close_keeps_the_actual_session_cleanup_failure_visible() {
    let (mut input, mut calls, _) = setup([br#"{"id":1,"close":true}"#.to_vec(), command(2)]);
    calls.fail_close = true;
    let mut output = Vec::new();
    let error = run_loop(&mut input, &mut calls, &mut output)
        .await
        .unwrap_err();
    assert!(format!("{error:#}").contains("physical cleanup unknown"));
    assert!(
        String::from_utf8(output)
            .unwrap()
            .ends_with("{\"kind\":\"session_finished\",\"ok\":false}\n")
    );
    assert_eq!(input.reads, 1);
    assert!(calls.ids.is_empty());
    assert_eq!(calls.closes, 1);
}

#[tokio::test]
async fn unknown_dispatch_never_reads_a_second_command_and_awaits_close() {
    let (mut input, mut calls, trace) = setup([command(1), command(2)]);
    calls.fail = true;
    let mut output = Vec::new();
    assert!(run_loop(&mut input, &mut calls, &mut output).await.is_err());
    assert_eq!(input.reads, 1);
    assert_eq!(calls.ids, [1]);
    assert_eq!(calls.closes, 1);
    assert_eq!(
        &*trace.borrow(),
        &["read1", "invoke1", "drained1", "close", "closed"]
    );
    assert!(
        String::from_utf8(output)
            .unwrap()
            .contains(r#""kind":"request_finished","id":1,"ok":false"#)
    );
}

#[tokio::test]
async fn operation_and_session_cleanup_errors_are_both_retained() {
    let (mut input, mut calls, _) = setup([command(1), command(2)]);
    calls.fail = true;
    calls.fail_close = true;
    let error = run_loop(&mut input, &mut calls, &mut Vec::new())
        .await
        .unwrap_err();
    let text = format!("{error:#}");
    assert!(text.contains("stream outcome or cleanup unknown"));
    assert!(text.contains("physical cleanup unknown"));
    assert_eq!(input.reads, 1);
    assert_eq!(calls.closes, 1);
}

#[tokio::test]
async fn invalid_or_oversized_line_never_dispatches_or_reads_the_next_line() {
    for first in [
        b"not JSON".to_vec(),
        vec![b'x'; MAX_REQUEST_BYTES + 1],
        br#"{"id":1,"close":false}"#.to_vec(),
    ] {
        let (mut input, mut calls, _) = setup([first, command(2)]);
        assert!(
            run_loop(&mut input, &mut calls, &mut Vec::new())
                .await
                .is_err()
        );
        assert_eq!(input.reads, 1);
        assert!(calls.ids.is_empty());
        assert_eq!(calls.closes, 1);
    }
}

#[tokio::test]
async fn repeated_or_decreasing_id_stops_instead_of_reinvoking_a_port() {
    for next in [1, 2] {
        let (mut input, mut calls, _) = setup([command(2), command(next), command(3)]);
        assert!(
            run_loop(&mut input, &mut calls, &mut Vec::new())
                .await
                .is_err()
        );
        assert_eq!(calls.ids, [2]);
        assert_eq!(input.reads, 2);
        assert_eq!(calls.closes, 1);
    }
    let (mut input, mut calls, _) =
        setup([command(1), br#"{"id":1,"close":true}"#.to_vec(), command(2)]);
    assert!(
        run_loop(&mut input, &mut calls, &mut Vec::new())
            .await
            .is_err()
    );
    assert_eq!(calls.ids, [1]);
    assert_eq!(input.reads, 2);
    assert_eq!(calls.closes, 1);
}

#[tokio::test]
async fn physical_input_failure_does_not_retry_the_read() {
    let (mut input, mut calls, _) = setup([command(2)]);
    input
        .lines
        .push_front(Err(anyhow!("input cleanup unknown")));
    let error = run_loop(&mut input, &mut calls, &mut Vec::new())
        .await
        .unwrap_err();
    assert!(error.to_string().contains("input cleanup unknown"));
    assert_eq!(input.reads, 1);
    assert!(calls.ids.is_empty());
    assert_eq!(calls.closes, 1);
}

struct FlushFailure {
    bytes: Vec<u8>,
    flushes: usize,
    fail_at: usize,
}
impl Write for FlushFailure {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.flushes += 1;
        if self.flushes == self.fail_at {
            Err(std::io::Error::other("output acknowledgement lost"))
        } else {
            Ok(())
        }
    }
}

#[tokio::test]
async fn ready_output_failure_closes_without_reading_a_command() {
    let (mut input, mut calls, _) = setup([command(1)]);
    let mut output = FlushFailure {
        bytes: Vec::new(),
        flushes: 0,
        fail_at: 1,
    };
    assert!(run_loop(&mut input, &mut calls, &mut output).await.is_err());
    assert_eq!(input.reads, 0);
    assert!(calls.ids.is_empty());
    assert_eq!(calls.closes, 1);
}

#[tokio::test]
async fn request_completion_output_failure_prevents_the_next_command() {
    let (mut input, mut calls, _) = setup([command(1), command(2)]);
    let mut output = FlushFailure {
        bytes: Vec::new(),
        flushes: 0,
        fail_at: 3,
    };
    assert!(run_loop(&mut input, &mut calls, &mut output).await.is_err());
    assert_eq!(input.reads, 1);
    assert_eq!(calls.ids, [1]);
    assert_eq!(calls.closes, 1);
}
