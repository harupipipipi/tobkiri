//! Deterministic test-only external SSE infrastructure; all chat uses wasm/Flow.
mod support;
use anyhow::{Context, Result};
use std::{
    io::{self, Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};
use support::*;

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
impl Server {
    fn new() -> Result<Self> {
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
            for stream in listener.incoming() {
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
                let response_headers = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n";
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
                let mut sse = String::new();
                for delta in ["Hello", " ", "猫", " is", " streamed", "."] {
                    sse.push_str(&format!(
                        "data: {}\r\n\r\n",
                        serde_json::json!({"choices":[{"delta":{"content":delta}}]})
                    ));
                }
                sse.push_str(&format!("data: {}\n\n",serde_json::json!({"choices":[],"usage":{"prompt_tokens":prompt_tokens,"completion_tokens":6}})));
                sse.push_str("data: [DONE]\n\n");
                // Deliberately split UTF-8 and SSE frame boundaries in HTTP chunks.
                for chunk in sse.as_bytes().chunks(3) {
                    if metadata
                        .write_result(
                            "chunk-prefix-write",
                            3,
                            write!(stream, "{:x}\r\n", chunk.len()),
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
    let server = Server::new()?;
    let fixture = Fixture::new("harness-live", Some(&server.origin))?;
    let (_, session) = fixture.start().await?;
    let events = fixture
        .turn(&session, "Use the declared local external provider")
        .await
        .context("network: first turn")?;
    assert_eq!(
        deltas(&events),
        vec!["Hello", " ", "猫", " is", " streamed", "."]
    );
    assert_eq!(fixture.host.output_text()?.trim(), "Hello 猫 is streamed.");
    {
        let requests = server.requests.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0]["stream"], true);
        assert_eq!(requests[0]["model"], "gemma3-1b-local");
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
        vec!["Hello", " ", "猫", " is", " streamed", "."]
    );
    let requests = server.requests.lock().unwrap();
    assert_eq!(
        requests.len(),
        2,
        "the saved turn must call the provider again"
    );
    assert_eq!(
        requests[1]["messages"]
            .as_array()
            .and_then(|messages| messages.last())
            .and_then(|message| message["content"].as_str()),
        Some("Check saved conversation")
    );
    Ok(())
}
