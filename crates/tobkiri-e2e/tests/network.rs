//! Deterministic test-only external SSE infrastructure; all chat uses wasm/Flow.
mod support;
use anyhow::Result;
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};
use support::*;

struct Server {
    origin: String,
    stop: Arc<AtomicBool>,
    requests: Arc<Mutex<Vec<serde_json::Value>>>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Server {
    fn new() -> Result<Self> {
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        let origin = format!("http://{}", listener.local_addr()?);
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let flag = stop.clone();
        let captured = requests.clone();
        let handle = thread::spawn(move || {
            for stream in listener.incoming() {
                if flag.load(Ordering::SeqCst) {
                    break;
                }
                let Ok(mut stream) = stream else {
                    break;
                };
                let _ = stream.set_read_timeout(Some(Duration::from_secs(3)));
                let _ = stream.set_write_timeout(Some(Duration::from_secs(3)));
                let mut input = Vec::new();
                let mut buffer = [0; 1024];
                let body_start = loop {
                    match stream.read(&mut buffer) {
                        Ok(0) | Err(_) => break None,
                        Ok(n) => {
                            input.extend_from_slice(&buffer[..n]);
                            if input.len() > 1024 * 1024 {
                                break None;
                            }
                            if let Some(end) = input.windows(4).position(|p| p == b"\r\n\r\n") {
                                break Some(end + 4);
                            }
                        }
                    }
                };
                let Some(body_start) = body_start else {
                    continue;
                };
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
                    continue;
                }
                while input.len() < body_start + length {
                    match stream.read(&mut buffer) {
                        Ok(0) | Err(_) => break,
                        Ok(n) => input.extend_from_slice(&buffer[..n]),
                    }
                }
                if input.len() < body_start + length {
                    continue;
                }
                let Ok(body) = serde_json::from_slice::<serde_json::Value>(
                    &input[body_start..body_start + length],
                ) else {
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
                if stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n").is_err(){continue;}
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
                    if write!(stream, "{:x}\r\n", chunk.len())
                        .and_then(|_| stream.write_all(chunk))
                        .and_then(|_| stream.write_all(b"\r\n"))
                        .is_err()
                    {
                        break;
                    }
                }
                let _ = stream.write_all(b"0\r\n\r\n");
            }
        });
        Ok(Self {
            origin,
            stop,
            requests,
            thread: Some(handle),
        })
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = TcpStream::connect(self.origin.trim_start_matches("http://"));
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
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
        .await?;
    assert_eq!(
        deltas(&events),
        vec!["Hello", " ", "猫", " is", " streamed", "."]
    );
    assert_eq!(fixture.host.output_text()?.trim(), "Hello 猫 is streamed.");
    let requests = server.requests.lock().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["stream"], true);
    assert_eq!(requests[0]["model"], "gemma3-1b-local");
    assert!(events.iter().any(
        |event| matches!(&event.kind,tobkiri_flow::RunEventKind::StepValue{step,..} if step=="save")
    ));
    drop(requests);
    let second = fixture.turn(&session, "Check saved conversation").await?;
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
