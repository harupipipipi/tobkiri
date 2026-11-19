use super::*;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::oneshot,
    time::timeout,
};

const WAIT: Duration = Duration::from_secs(5);
const BODY: &[u8] = br#"{"model":"fixture","messages":[],"stream":false,"parameters":{}}"#;

#[derive(Default)]
struct Budget(AtomicU64);

impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        self.0
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |used| {
                used.checked_add(request.bytes)
                    .filter(|next| *next <= 64 * 1024 * 1024)
            })
            .map(drop)
            .map_err(|_| AllocationError::Limit)
    }

    fn release(&self, bytes: u64) {
        assert!(self.0.fetch_sub(bytes, Ordering::AcqRel) >= bytes);
    }
}

async fn receive_request(socket: &mut TcpStream) -> (String, Vec<u8>) {
    let mut prefix = Vec::new();
    while !prefix.ends_with(b"\r\n\r\n") {
        assert!(prefix.len() < 8192);
        prefix.push(socket.read_u8().await.unwrap());
    }
    let prefix = String::from_utf8(prefix).unwrap();
    let length = prefix
        .lines()
        .filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.eq_ignore_ascii_case("Content-Length"))
        .unwrap()
        .1
        .trim()
        .parse()
        .unwrap();
    let mut raw = vec![0; length];
    socket.read_exact(&mut raw).await.unwrap();
    (prefix, raw)
}

fn request<'a>(
    address: SocketAddr,
    cancellation: &'a CoreCancellation,
    budget: &Arc<dyn AllocationBudget>,
) -> LegacyLocalChatHttpRequest<'a> {
    LegacyLocalChatHttpRequest {
        address,
        body: CoreBuffer::copy_from(BODY, budget).unwrap(),
        deadline: Instant::now() + WAIT,
        cancellation,
    }
}

#[tokio::test]
async fn original_local_chat_preserves_the_bytes_and_fixed_headers() {
    const REPLY: &[u8] = b"{ \"usage\":{}, \"choices\":[], \"choices\": [] }\n";
    for host in [
        IpAddr::from(Ipv4Addr::LOCALHOST),
        IpAddr::from(Ipv6Addr::LOCALHOST),
    ] {
        let listener = TcpListener::bind(SocketAddr::new(host, 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let (prefix, raw) = receive_request(&mut socket).await;
            assert!(prefix.starts_with("POST /v1/chat/completions HTTP/1.1\r\n"));
            let lower = prefix.to_ascii_lowercase();
            assert!(lower.contains("user-agent: rumiai/1.0\r\n"));
            assert!(lower.contains("accept: application/json\r\n"));
            assert!(lower.contains("accept-encoding: identity\r\n"));
            assert!(!lower.contains("authorization:"));
            assert_eq!(raw, BODY);
            socket
                .write_all(
                    format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n", REPLY.len())
                        .as_bytes(),
                )
                .await
                .unwrap();
            socket.write_all(REPLY).await.unwrap();
        });
        let counted = Arc::new(Budget::default());
        let budget: Arc<dyn AllocationBudget> = counted.clone();
        let cancellation = CoreCancellation::new();
        let mut permitted = 0;
        let result = post_brokered_legacy_local_chat(
            request(address, &cancellation, &budget),
            &budget,
            |phase| {
                permitted += usize::from(phase == LegacyLocalChatHttpCheck::Dispatch);
                Ok(())
            },
        )
        .await
        .unwrap();
        assert_eq!(result.as_bytes(), REPLY);
        assert_eq!(permitted, 1);
        drop(result);
        assert_eq!(counted.0.load(Ordering::Acquire), 0);
        timeout(WAIT, server).await.unwrap().unwrap();
    }
}

#[tokio::test]
async fn denied_and_expired_original_authority_make_no_connection() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let address = listener.local_addr().unwrap();
    let cancellation = CoreCancellation::new();
    let budget: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let result = post_brokered_legacy_local_chat(
        request(address, &cancellation, &budget),
        &budget,
        |phase| match phase {
            LegacyLocalChatHttpCheck::Live => Ok(()),
            LegacyLocalChatHttpCheck::Dispatch => bail!("fixture denied dispatch"),
        },
    )
    .await;
    assert_eq!(result.unwrap_err(), LegacyLocalChatHttpError::Revoked);

    let mut expired = request(address, &cancellation, &budget);
    expired.deadline = Instant::now();
    let result = post_brokered_legacy_local_chat(expired, &budget, |_| unreachable!()).await;
    assert_eq!(result.unwrap_err(), LegacyLocalChatHttpError::Deadline);
    let invalid = request(
        SocketAddr::from(([127, 0, 0, 2], address.port())),
        &cancellation,
        &budget,
    );
    let result = post_brokered_legacy_local_chat(invalid, &budget, |_| unreachable!()).await;
    assert_eq!(
        result.unwrap_err(),
        LegacyLocalChatHttpError::InvalidRequest
    );
    assert!(
        timeout(Duration::from_millis(100), listener.accept())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn original_local_chat_does_not_follow_redirects() {
    let redirect = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let target = redirect.local_addr().unwrap();
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        receive_request(&mut socket).await;
        socket.write_all(format!(
            "HTTP/1.1 307 Temporary Redirect\r\nLocation: http://{target}/capture\r\nContent-Length: 0\r\n\r\n"
        ).as_bytes()).await.unwrap();
    });
    let cancellation = CoreCancellation::new();
    let budget: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let result =
        post_brokered_legacy_local_chat(request(address, &cancellation, &budget), &budget, |_| {
            Ok(())
        })
        .await;
    assert_eq!(result.unwrap_err(), LegacyLocalChatHttpError::Unavailable);
    timeout(WAIT, server).await.unwrap().unwrap();
    assert!(
        timeout(Duration::from_millis(100), redirect.accept())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn checks_the_exact_response_boundary_without_requiring_content_length() {
    for extra in [0, 1] {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            receive_request(&mut socket).await;
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                .await
                .unwrap();
            let chunk = [b'a'; 64 * 1024];
            for _ in 0..LEGACY_LOCAL_CHAT_RESPONSE_LIMIT / chunk.len() {
                socket.write_all(b"10000\r\n").await.unwrap();
                socket.write_all(&chunk).await.unwrap();
                socket.write_all(b"\r\n").await.unwrap();
            }
            let finish = if extra == 1 {
                &b"1\r\nb\r\n0\r\n\r\n"[..]
            } else {
                b"0\r\n\r\n"
            };
            socket.write_all(finish).await.unwrap();
        });
        let regional = Arc::new(Budget::default());
        let budget: Arc<dyn AllocationBudget> = regional.clone();
        let cancellation = CoreCancellation::new();
        let result = post_brokered_legacy_local_chat(
            request(address, &cancellation, &budget),
            &budget,
            |_| Ok(()),
        )
        .await;
        if extra == 0 {
            let result = result.unwrap();
            assert_eq!(result.len(), LEGACY_LOCAL_CHAT_RESPONSE_LIMIT);
            assert!(result.as_bytes().iter().all(|byte| *byte == b'a'));
        } else {
            assert_eq!(
                result.unwrap_err(),
                LegacyLocalChatHttpError::ResponseTooLarge
            );
        }
        timeout(WAIT, server).await.unwrap().unwrap();
        assert_eq!(regional.0.load(Ordering::Acquire), 0);
    }
}

#[tokio::test]
async fn cancel_closes_the_physical_response_socket() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let address = listener.local_addr().unwrap();
    let (responded, after_headers) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        receive_request(&mut socket).await;
        socket
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{")
            .await
            .unwrap();
        responded.send(()).unwrap();
        let mut received = [0];
        match socket.read(&mut received).await {
            Ok(0) => (),
            Err(error) if error.kind() == std::io::ErrorKind::ConnectionReset => (),
            value => panic!("native provider socket did not close: {value:?}"),
        }
    });
    let cancellation = CoreCancellation::new();
    let worker_cancel = cancellation.clone();
    let worker = tokio::spawn(async move {
        let budget: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
        post_brokered_legacy_local_chat(request(address, &worker_cancel, &budget), &budget, |_| {
            Ok(())
        })
        .await
    });
    timeout(WAIT, after_headers).await.unwrap().unwrap();
    cancellation.cancel();
    assert_eq!(
        timeout(WAIT, worker).await.unwrap().unwrap().unwrap_err(),
        LegacyLocalChatHttpError::Revoked
    );
    timeout(WAIT, server).await.unwrap().unwrap();
}
