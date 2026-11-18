//! Deterministic test-only external SSE infrastructure; all chat uses wasm/Flow.
mod support;
use anyhow::{Context, Result};
use std::{
    io::{self, Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};
use support::*;
use tobkiri_core::{NativeInputRouteStatus, NativeInputStatus};
use tobkiri_flow::RunEventKind;
use tokio::sync::oneshot;

const FIRST_DELTA: &str = "Hello";
const PEER_CLOSE_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Default)]
struct RequestDiagnostics {
    clock: Option<Instant>,
    accept_us: u128,
    headers_us: Option<u128>,
    body_us: Option<u128>,
    send_start_us: Option<u128>,
    send_end_us: Option<u128>,
    received_bytes: usize,
    header_bytes: usize,
    body_bytes: usize,
    write_attempts: usize,
    completed_write_bytes: usize,
    error_stage: Option<&'static str>,
    error_kind: Option<io::ErrorKind>,
    error_us: Option<u128>,
}
impl RequestDiagnostics {
    fn error(&mut self, stage: &'static str, kind: Option<io::ErrorKind>) {
        if self.error_stage.is_none() {
            self.error_stage = Some(stage);
            self.error_kind = kind;
            self.error_us = self.clock.map(|clock| clock.elapsed().as_micros());
        }
    }
    // Count completed logical writes, not syscalls or partially failed writes.
    fn write_result(
        &mut self,
        stage: &'static str,
        bytes: usize,
        result: io::Result<()>,
    ) -> io::Result<()> {
        self.write_attempts = self.write_attempts.saturating_add(1);
        match &result {
            Ok(()) => {
                self.completed_write_bytes = self.completed_write_bytes.saturating_add(bytes);
            }
            Err(error) => self.error(stage, Some(error.kind())),
        }
        result
    }
}

#[derive(Default)]
struct ServerDiagnostics {
    accepted: usize,
    accept_error: Option<io::ErrorKind>,
    // Diagnostics retain only the two expected requests. Extra requests keep
    // their original behavior and increment accepted without growing storage.
    requests: [Option<RequestDiagnostics>; 2],
}
impl ServerDiagnostics {
    fn record(&mut self, index: usize, request: RequestDiagnostics) {
        if let Some(slot) = self.requests.get_mut(index) {
            *slot = Some(request);
        }
    }
}

struct Server {
    origin: String,
    stop: Arc<AtomicBool>,
    requests: Arc<Mutex<Vec<serde_json::Value>>>,
    diagnostics: Arc<Mutex<ServerDiagnostics>>,
    thread: Option<thread::JoinHandle<()>>,
}

enum Response {
    ResumeAfterFirstDelta(mpsc::Receiver<()>),
    ObserveCancellation {
        requested: mpsc::Receiver<Instant>,
        peer_close: Option<oneshot::Sender<io::Result<Instant>>>,
    },
    Error(u16, String),
}

fn observe_peer_close(
    stream: &mut TcpStream,
    requested: &mpsc::Receiver<Instant>,
) -> io::Result<Instant> {
    let cancellation = requested
        .recv_timeout(Duration::from_secs(30))
        .map_err(|error| {
            let kind = match error {
                mpsc::RecvTimeoutError::Timeout => io::ErrorKind::TimedOut,
                mpsc::RecvTimeoutError::Disconnected => io::ErrorKind::BrokenPipe,
            };
            io::Error::new(kind, error)
        })?;
    let remaining = PEER_CLOSE_TIMEOUT.saturating_sub(cancellation.elapsed());
    if remaining.is_zero() {
        return Err(io::ErrorKind::TimedOut.into());
    }
    stream.set_read_timeout(Some(remaining))?;
    match stream.read(&mut [0]) {
        Ok(0) => Ok(Instant::now()),
        Err(error) if error.kind() == io::ErrorKind::ConnectionReset => Ok(Instant::now()),
        Err(error) => Err(error),
        Ok(_) => Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "expected the HTTP peer to close the unfinished response",
        )),
    }
}

impl Server {
    fn new(resume_first_response: mpsc::Receiver<()>) -> Result<Self> {
        Self::with_response(Response::ResumeAfterFirstDelta(resume_first_response))
    }

    fn error_response(status: u16, body: &str) -> Result<Self> {
        Self::with_response(Response::Error(status, body.to_owned()))
    }

    fn with_response(mut response: Response) -> Result<Self> {
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        let origin = format!("http://{}", listener.local_addr()?);
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let diagnostics = Arc::new(Mutex::new(ServerDiagnostics::default()));
        let flag = stop.clone();
        let captured = requests.clone();
        let trace = diagnostics.clone();
        let started = Instant::now();
        let handle = thread::spawn(move || {
            'connections: for stream in listener.incoming() {
                if flag.load(Ordering::SeqCst) {
                    break;
                }
                let mut stream = match stream {
                    Ok(stream) => stream,
                    Err(error) => {
                        if let Ok(mut diagnostics) = trace.lock() {
                            diagnostics.accept_error = Some(error.kind());
                        }
                        break;
                    }
                };
                let mut metadata = RequestDiagnostics {
                    clock: Some(started),
                    accept_us: started.elapsed().as_micros(),
                    ..RequestDiagnostics::default()
                };
                let request_index = if let Ok(mut diagnostics) = trace.lock() {
                    let index = diagnostics.accepted;
                    diagnostics.accepted = diagnostics.accepted.saturating_add(1);
                    diagnostics.record(index, metadata);
                    index
                } else {
                    usize::MAX
                };
                let record = |metadata| {
                    if let Ok(mut diagnostics) = trace.lock() {
                        diagnostics.record(request_index, metadata);
                    }
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
                let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
                let mut input = Vec::new();
                let mut buffer = [0; 1024];
                let body_start = loop {
                    match stream.read(&mut buffer) {
                        Ok(0) => {
                            metadata.error("headers-eof", Some(io::ErrorKind::UnexpectedEof));
                            break None;
                        }
                        Err(error) => {
                            metadata.error("headers-read", Some(error.kind()));
                            break None;
                        }
                        Ok(n) => {
                            input.extend_from_slice(&buffer[..n]);
                            metadata.received_bytes = input.len();
                            if input.len() > 1024 * 1024 {
                                metadata.error("headers-limit", None);
                                break None;
                            }
                            if let Some(end) = input.windows(4).position(|p| p == b"\r\n\r\n") {
                                break Some(end + 4);
                            }
                        }
                    }
                };
                let Some(body_start) = body_start else {
                    record(metadata);
                    continue;
                };
                metadata.headers_us = Some(started.elapsed().as_micros());
                metadata.header_bytes = body_start;
                record(metadata);
                let header = String::from_utf8_lossy(&input[..body_start]);
                if header.lines().next() != Some("POST /v1/chat/completions HTTP/1.1") {
                    metadata.error("request-path", None);
                    record(metadata);
                    continue;
                }
                let length = header
                    .lines()
                    .find_map(|line| {
                        line.split_once(':')
                            .filter(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                            .and_then(|(_, n)| n.trim().parse::<usize>().ok())
                    })
                    .unwrap_or(0);
                if length > 1024 * 1024 {
                    metadata.error("body-limit", None);
                    record(metadata);
                    continue;
                }
                while input.len() < body_start + length {
                    match stream.read(&mut buffer) {
                        Ok(0) => {
                            metadata.error("body-eof", Some(io::ErrorKind::UnexpectedEof));
                            break;
                        }
                        Err(error) => {
                            metadata.error("body-read", Some(error.kind()));
                            break;
                        }
                        Ok(n) => {
                            input.extend_from_slice(&buffer[..n]);
                            metadata.received_bytes = input.len();
                        }
                    }
                }
                if input.len() < body_start + length {
                    record(metadata);
                    continue;
                }
                metadata.body_us = Some(started.elapsed().as_micros());
                metadata.body_bytes = length;
                record(metadata);
                let Ok(body) = serde_json::from_slice::<serde_json::Value>(
                    &input[body_start..body_start + length],
                ) else {
                    metadata.error("request-json", Some(io::ErrorKind::InvalidData));
                    record(metadata);
                    continue;
                };
                let prompt_tokens = body["messages"]
                    .as_array()
                    .map(|messages| {
                        messages
                            .iter()
                            .map(|message| message["content"].as_str().unwrap_or("").len())
                            .sum::<usize>()
                    })
                    .unwrap_or(0);
                captured.lock().unwrap().push(body);
                metadata.send_start_us = Some(started.elapsed().as_micros());
                record(metadata);
                if let Response::Error(status, body) = &response {
                    let content_type = if *status == 200 {
                        "text/event-stream"
                    } else {
                        "application/json"
                    };
                    let headers = format!(
                        "HTTP/1.1 {status} Test response\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len(),
                    );
                    let _ = metadata.write_result(
                        "error-headers-write",
                        headers.len(),
                        stream.write_all(headers.as_bytes()),
                    );
                    let _ = metadata.write_result(
                        "error-body-write",
                        body.len(),
                        stream.write_all(body.as_bytes()),
                    );
                    metadata.send_end_us = Some(started.elapsed().as_micros());
                    record(metadata);
                    continue;
                }
                let response_headers: &[u8] = if request_index == 0
                    && matches!(&response, Response::ObserveCancellation { .. })
                {
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                } else {
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                };
                if metadata
                    .write_result(
                        "response-headers-write",
                        response_headers.len(),
                        stream.write_all(response_headers),
                    )
                    .is_err()
                {
                    metadata.send_end_us = Some(started.elapsed().as_micros());
                    record(metadata);
                    continue;
                }
                let delta_frame = |delta: &str| {
                    format!(
                        "data: {}\r\n\r\n",
                        serde_json::json!({"choices":[{"delta":{"content":delta}}]})
                    )
                };
                let first_frame = delta_frame(FIRST_DELTA);
                let mut sse = String::new();
                for delta in [" ", "猫", " is", " streamed"] {
                    sse.push_str(&delta_frame(delta));
                }
                sse.push_str(&format!(
                    "data: {}\n\n",
                    serde_json::json!({
                        "choices": [{"delta": {"content": "."}}],
                        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 6},
                    })
                ));
                sse.push_str("data: [DONE]\n\n");
                // Keep the first frame whole and split later UTF-8 and SSE boundaries.
                let chunks =
                    std::iter::once(first_frame.as_bytes()).chain(sse.as_bytes().chunks(3));
                for (chunk_index, chunk) in chunks.enumerate() {
                    let prefix = format!("{:x}\r\n", chunk.len());
                    if metadata
                        .write_result(
                            "chunk-prefix-write",
                            prefix.len(),
                            stream.write_all(prefix.as_bytes()),
                        )
                        .and_then(|_| {
                            metadata.write_result(
                                "chunk-body-write",
                                chunk.len(),
                                stream.write_all(chunk),
                            )
                        })
                        .and_then(|_| {
                            metadata.write_result(
                                "chunk-suffix-write",
                                2,
                                stream.write_all(b"\r\n"),
                            )
                        })
                        .is_err()
                    {
                        break;
                    }
                    if request_index == 0 && chunk_index == 0 {
                        match &mut response {
                            Response::ResumeAfterFirstDelta(resume) => {
                                if let Err(error) = resume.recv_timeout(Duration::from_secs(20)) {
                                    let kind = match error {
                                        mpsc::RecvTimeoutError::Timeout => io::ErrorKind::TimedOut,
                                        mpsc::RecvTimeoutError::Disconnected => {
                                            io::ErrorKind::BrokenPipe
                                        }
                                    };
                                    metadata.error("wait-for-first-flow-delta", Some(kind));
                                    metadata.send_end_us = Some(started.elapsed().as_micros());
                                    record(metadata);
                                    continue 'connections;
                                }
                            }
                            Response::ObserveCancellation {
                                requested,
                                peer_close,
                            } => {
                                // Leave this response unfinished and the socket open during the read.
                                metadata.send_end_us = Some(started.elapsed().as_micros());
                                record(metadata);
                                let result = observe_peer_close(&mut stream, requested);
                                if let Err(error) = &result {
                                    metadata.error("peer-close-read", Some(error.kind()));
                                    record(metadata);
                                }
                                if let Some(peer_close) = peer_close.take() {
                                    let _ = peer_close.send(result);
                                }
                                continue 'connections;
                            }
                            Response::Error(..) => unreachable!("error response already sent"),
                        }
                    }
                }
                let _ = metadata.write_result(
                    "terminal-chunk-write",
                    5,
                    stream.write_all(b"0\r\n\r\n"),
                );
                metadata.send_end_us = Some(started.elapsed().as_micros());
                record(metadata);
            }
        });
        Ok(Self {
            origin,
            stop,
            requests,
            diagnostics,
            thread: Some(handle),
        })
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(self.origin.trim_start_matches("http://"));
        let thread_panicked = self
            .thread
            .take()
            .is_some_and(|thread| thread.join().is_err());
        // The test harness exposes captured output on failure. This fixed-size
        // summary contains no request, header, prompt, key or response content.
        match self.diagnostics.lock() {
            Ok(diagnostics) => {
                eprintln!(
                    "NETWORK_SERVER_DIAGNOSTICS thread_panicked={thread_panicked} accepted={} accept_error={:?}",
                    diagnostics.accepted, diagnostics.accept_error
                );
                for (index, request) in diagnostics.requests.iter().enumerate() {
                    if let Some(request) = request {
                        eprintln!(
                            "NETWORK_REQUEST_DIAGNOSTICS request_index={} accept_us={} headers_us={:?} body_us={:?} send_start_us={:?} send_end_us={:?} received_bytes={} header_bytes={} body_bytes={} write_attempts={} completed_write_bytes={} error_stage={:?} error_kind={:?} error_us={:?}",
                            index + 1,
                            request.accept_us,
                            request.headers_us,
                            request.body_us,
                            request.send_start_us,
                            request.send_end_us,
                            request.received_bytes,
                            request.header_bytes,
                            request.body_bytes,
                            request.write_attempts,
                            request.completed_write_bytes,
                            request.error_stage,
                            request.error_kind,
                            request.error_us,
                        );
                    }
                }
            }
            Err(_) => eprintln!(
                "NETWORK_SERVER_DIAGNOSTICS thread_panicked={thread_panicked} metadata=unavailable"
            ),
        }
    }
}

#[tokio::test]
async fn wasm_external_provider_streams_real_sse_through_flow_and_persistence() -> Result<()> {
    let provider_model = "selected-local-model";
    let (resume_first_response, wait_for_delta) = mpsc::channel();
    let server = Server::new(wait_for_delta)?;
    let mut resume_first_response = Some(resume_first_response);
    let mut fixture = Fixture::new("harness-live", Some(&server.origin))?;
    fixture.replace_pack_json(
        "local.openai-compat-provider",
        "config.json",
        &serde_json::json!({
            "base_url": server.origin,
            "model": provider_model,
            "secret_name": null,
            "timeout_ms": 300000,
            "temperature": "0.4",
        }),
    )?;
    fixture.replace_pack_json(
        "local.harness-prompt",
        "config.json",
        &serde_json::json!({
            "system_prompt": "Answer in plain text. Be helpful and concise.",
            "max_tokens": 2048,
        }),
    )?;
    let (_, session) = fixture.start().await?;
    let oversized_history = format!(
        "Use the declared local external provider. {}",
        "猫".repeat(750)
    );
    fixture
        .host
        .lines
        .lock()
        .unwrap()
        .push_back(oversized_history.clone());
    let input = within_session(
        &session,
        "network: physical Input read",
        session.read_input(
            fixture.input.clone(),
            fixture.shape.clone(),
            fixture.input_operation.clone(),
        ),
    )
    .await?;
    let mut run = within_session(
        &session,
        "network: NativeInput admission",
        session.submit_native_input(input),
    )
    .await?;
    assert_eq!(run.routes().len(), 1);
    let plan_digest = run.routes()[0].target.plan_digest.clone();
    let mut events = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    while let Some(tagged) =
        next_native_event(&mut run, "network: first turn progress", deadline).await?
    {
        assert_eq!(tagged.route_index, 0);
        assert_eq!(tagged.event.plan_digest, plan_digest);
        if let Some(delta) = delta_text(&tagged.event)
            && let Some(resume) = resume_first_response.take()
        {
            assert_eq!(delta, FIRST_DELTA);
            resume.send(()).context("first SSE response gate closed")?;
        }
        events.push(tagged.event);
    }
    anyhow::ensure!(
        resume_first_response.is_none(),
        "the Flow must emit the first delta before the server sends more: {:?}",
        failure_events(&events)
    );
    assert_native_completion(
        &native_completion(&mut run, "network: first turn completion").await?,
        NativeInputStatus::Succeeded,
        &[NativeInputRouteStatus::Succeeded],
    )?;
    assert!(
        events
            .iter()
            .any(|event| matches!(event.kind, RunEventKind::Completed { .. }))
    );
    assert_eq!(
        deltas(&events),
        vec![FIRST_DELTA, " ", "猫", " is", " streamed", "."]
    );
    assert!(events.iter().any(|event| {
        matches!(&event.kind, RunEventKind::Emitted { value, .. }
            if value.get_key("kind").is_some_and(|kind| kind.as_str() == Some("usage"))
                && value.get_key("completion-tokens").and_then(|tokens| tokens.as_i64()) == Some(6))
    }));
    assert_eq!(fixture.host.output_text()?.trim(), "Hello 猫 is streamed.");
    {
        let requests = server.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0]["stream"], true);
        assert_eq!(requests[0]["model"], provider_model);
        assert_eq!(requests[0]["max_tokens"], 2048);
        assert_eq!(requests[0]["temperature"], 0.4);
        assert_eq!(requests[0]["messages"][1]["content"], oversized_history);
        assert!(events.iter().any(
            |event| matches!(&event.kind,tobkiri_flow::RunEventKind::StepValue{step,..} if step=="save")
        ));
    }
    let second = fixture
        .turn(&session, "Check saved conversation")
        .await
        .context("network: second turn")?;
    assert_eq!(loaded_message_count(&second), Some(2));
    assert_eq!(
        deltas(&second),
        vec![FIRST_DELTA, " ", "猫", " is", " streamed", "."]
    );
    let requests = server.requests.lock().unwrap();
    assert_eq!(
        requests.len(),
        2,
        "the saved turn must call the provider again"
    );
    assert_eq!(
        requests[1]["messages"],
        serde_json::json!([
            {"role": "system", "content": "Answer in plain text. Be helpful and concise."},
            {"role": "user", "content": "Check saved conversation"},
        ]),
        "old turns stay persisted and fit in the prompt only when within the history budget",
    );
    Ok(())
}

#[tokio::test]
async fn wasm_external_provider_closes_unfinished_sse_when_flow_is_cancelled() -> Result<()> {
    let (server, request_cancellation, peer_closed) = {
        let (request_cancellation, requested) = mpsc::channel();
        let (peer_close, peer_closed) = oneshot::channel();
        let server = Server::with_response(Response::ObserveCancellation {
            requested,
            peer_close: Some(peer_close),
        })?;
        (server, request_cancellation, peer_closed)
    };
    let fixture = Fixture::new("harness-live", Some(&server.origin))?;
    let (_, session) = fixture.start().await?;
    let first_prompt = "Cancel this unfinished turn";
    fixture
        .host
        .lines
        .lock()
        .unwrap()
        .push_back(first_prompt.into());
    let input = within_session(
        &session,
        "network cancellation: physical Input read",
        session.read_input(
            fixture.input.clone(),
            fixture.shape.clone(),
            fixture.input_operation.clone(),
        ),
    )
    .await?;
    let mut run = within_session(
        &session,
        "network cancellation: NativeInput admission",
        session.submit_native_input(input),
    )
    .await?;
    assert_eq!(run.routes().len(), 1);
    let plan_digest = run.routes()[0].target.plan_digest.clone();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
    let mut events = Vec::new();
    let cancellation = loop {
        let tagged = next_native_event(&mut run, "network cancellation: first delta", deadline)
            .await?
            .with_context(|| {
                format!(
                    "Flow ended before cancellation: {:?}",
                    failure_events(&events)
                )
            })?;
        assert_eq!(tagged.route_index, 0);
        assert_eq!(tagged.event.plan_digest, plan_digest);
        let delta = delta_text(&tagged.event);
        events.push(tagged.event);
        if let Some(delta) = delta {
            assert_eq!(delta, FIRST_DELTA);
            let cancellation = Instant::now();
            run.cancel.cancel();
            request_cancellation
                .send(cancellation)
                .context("server closed the cancellation gate")?;
            break cancellation;
        }
    };
    let disconnect_deadline = tokio::time::Instant::from_std(cancellation + PEER_CLOSE_TIMEOUT);
    let closed = tokio::time::timeout_at(disconnect_deadline, peer_closed)
        .await
        .context("HTTP peer did not close within 3 seconds of Flow cancellation")?
        .context("server ended without observing the HTTP peer")?
        .context("server did not read HTTP peer EOF or reset")?;
    assert!(closed.duration_since(cancellation) <= PEER_CLOSE_TIMEOUT);

    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    while let Some(tagged) = next_native_event(
        &mut run,
        "network cancellation: drain original run",
        deadline,
    )
    .await?
    {
        assert_eq!(tagged.route_index, 0);
        assert_eq!(tagged.event.plan_digest, plan_digest);
        events.push(tagged.event);
    }
    assert_native_completion(
        &native_completion(&mut run, "network cancellation: NativeInput completion").await?,
        NativeInputStatus::Cancelled,
        &[NativeInputRouteStatus::Cancelled],
    )?;
    assert_eq!(deltas(&events), [FIRST_DELTA]);
    assert!(!events.iter().any(|event| {
        matches!(&event.kind, RunEventKind::StepValue { step, .. } if step == "save")
            || matches!(&event.kind, RunEventKind::Completed { .. })
    }));

    let follow_up = fixture.turn(&session, "Check the conversation").await?;
    assert_eq!(
        loaded_message_count(&follow_up),
        Some(0),
        "cancelled turn was persisted into the next turn"
    );
    {
        let requests = server.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert_eq!(requests[0]["stream"], true);
        assert_eq!(requests[0]["messages"][1]["content"], first_prompt);
    }
    within_session(
        &session,
        "network cancellation: Session close",
        session.close(),
    )
    .await?;
    Ok(())
}

#[tokio::test]
async fn wasm_provider_keeps_http_diagnostics_in_failed_flow() -> Result<()> {
    for (status, body, prefix, expected) in [
        (
            400,
            r#"{"error":{"message":"Model missing-local-model was not found","code":"model_not_found","param":"model"}}"#,
            "provider HTTP 400",
            "missing-local-model",
        ),
        (
            401,
            r#"{"error":{"message":"Incorrect API key provided: sk-private-test-value","code":"invalid_api_key"}}"#,
            "provider HTTP 401",
            "invalid_api_key",
        ),
        (
            200,
            "data: {\"choices\":[{\"delta\":{\"content\":\"partial\"}}]}\n\ndata: {\"error\":{\"message\":\"Context window is full\",\"code\":\"context_length_exceeded\"}}\n\n",
            "provider stream failed",
            "context_length_exceeded",
        ),
    ] {
        let server = Server::error_response(status, body)?;
        let fixture = Fixture::new("harness-live", Some(&server.origin))?;
        let (_, session) = fixture.start().await?;
        fixture
            .host
            .lines
            .lock()
            .unwrap()
            .push_back("Report API failure".into());
        let input = within_session(
            &session,
            "network error: physical Input read",
            session.read_input(
                fixture.input.clone(),
                fixture.shape.clone(),
                fixture.input_operation.clone(),
            ),
        )
        .await?;
        let mut run = within_session(
            &session,
            "network error: NativeInput admission",
            session.submit_native_input(input),
        )
        .await?;
        assert_eq!(run.routes().len(), 1);
        let plan_digest = run.routes()[0].target.plan_digest.clone();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        let mut diagnostics = Vec::new();
        let mut partial_text = Vec::new();
        let mut failed = false;
        while let Some(tagged) =
            next_native_event(&mut run, "network error: Flow progress", deadline).await?
        {
            assert_eq!(tagged.route_index, 0);
            assert_eq!(tagged.event.plan_digest, plan_digest);
            match &tagged.event.kind {
                RunEventKind::Emitted { shape, value } => {
                    assert_eq!(shape.to_string(), "local.harness-chat.assistant-output@1.0");
                    let text = value
                        .get_key("text")
                        .and_then(|text| text.as_str().map(str::to_owned))
                        .expect("event text");
                    match value
                        .get_key("kind")
                        .and_then(|kind| kind.as_str().map(str::to_owned))
                        .as_deref()
                    {
                        Some("error") => diagnostics.push(text),
                        Some("delta") => {
                            assert!(diagnostics.is_empty(), "delta after error");
                            partial_text.push(text);
                        }
                        kind => panic!("unexpected error-stream event: {kind:?}"),
                    }
                }
                RunEventKind::StepFailed { step, error, .. } => {
                    assert_eq!(step, "generate");
                    // The existing Core stream facade closes guest pull errors
                    // as component failures; only chat data carries diagnostics.
                    assert_eq!(error.code, "component");
                }
                RunEventKind::Failed { error } => {
                    failed = true;
                    assert_eq!(error.code, "component");
                }
                RunEventKind::Completed { .. } => panic!("API failure completed the Flow"),
                RunEventKind::StepValue { step, .. } if step == "save" => {
                    panic!("API failure saved the turn")
                }
                _ => {}
            }
        }
        assert_native_completion(
            &native_completion(&mut run, "network error: NativeInput completion").await?,
            NativeInputStatus::Failed,
            &[NativeInputRouteStatus::Failed],
        )?;
        assert!(failed, "missing Flow failure event");
        assert_eq!(diagnostics.len(), 1, "duplicate or missing error event");
        if status == 200 {
            assert_eq!(partial_text, ["partial"]);
        } else {
            assert!(partial_text.is_empty());
        }
        let diagnostic = diagnostics.join("; ");
        assert!(diagnostic.contains(prefix), "{diagnostic}");
        assert!(diagnostic.contains(expected), "{diagnostic}");
        assert!(
            !diagnostic.contains("sk-private-test-value"),
            "{diagnostic}"
        );
        assert_eq!(server.requests.lock().unwrap().len(), 1);
    }
    Ok(())
}
