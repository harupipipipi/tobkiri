use anyhow::{Context, Result, bail};
use bytes::Bytes;
use reqwest::{Client, Method, Response};
use std::future::{Future, poll_fn};
use std::net::{IpAddr, SocketAddr};
use std::pin::pin;
use std::sync::Arc;
use std::task::Poll;
use std::time::{Duration, Instant};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreBuffer, CoreValue,
    Digest,
};
use tobkiri_gate::{CallCx, DispatchPermit, EffectRequest, Grant};
use url::Url;

const MAX_RECEIVE_CHUNK_BYTES: usize = 2 * 1024 * 1024;
const MAX_RECEIVE_BYTES: u64 = 32 * 1024 * 1024;
const MAX_READY_CHUNKS: usize = 32;

/// No public constructor: a consumed gate grant precedes DNS and network I/O.
pub(crate) struct NetClient {
    client: Client,
    permit: DispatchPermit,
    cx: CallCx,
    url: Url,
    method: Method,
}

impl NetClient {
    pub(crate) async fn from_grant(
        grant: Grant,
        cx: &CallCx,
        request: &EffectRequest,
        args: &Digest,
        timeout_ms: Option<u32>,
    ) -> Result<Self> {
        let EffectRequest::Network {
            origin,
            method,
            url,
        } = request
        else {
            bail!("network client requires network authority")
        };
        let parsed = Url::parse(url).context("invalid network URL")?;
        if !matches!(parsed.scheme(), "http" | "https")
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || parsed.fragment().is_some()
        {
            bail!("network URL has forbidden scheme, credentials or fragment")
        }
        if parsed.origin().ascii_serialization() != *origin {
            bail!("network origin is not the gate-authorized origin")
        }
        let method = Method::from_bytes(method.as_bytes()).context("invalid network method")?;
        let permit = grant.consume(cx, request, args)?;
        let cx = cx.clone().narrow_limits(permit.limits());
        let remaining = cx
            .deadline()
            .checked_duration_since(Instant::now())
            .context("original network deadline elapsed")?;
        let timeout = timeout_ms
            .map(|ms| Duration::from_millis(u64::from(ms)))
            .map(|n| n.min(remaining))
            .unwrap_or(remaining);
        if timeout.is_zero() {
            bail!("network request timeout is zero")
        }
        let mut builder = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .timeout(timeout);
        let hostname = parsed.host_str().context("network URL has no host")?;
        if hostname.trim_matches(['[', ']']).parse::<IpAddr>().is_err() {
            let port = parsed
                .port_or_known_default()
                .context("network URL has no port")?;
            let addresses: Vec<SocketAddr> = tokio::select! {
                _ = cx.cancel().cancelled() => bail!("network request cancelled during DNS"),
                _ = tokio::time::sleep_until(cx.deadline().into()) => bail!("original network deadline elapsed during DNS"),
                result = tokio::net::lookup_host((hostname,port)) => result?.take(65).collect(),
            };
            if addresses.len() > 64 {
                bail!("network DNS answer count exceeded")
            }
            if addresses.is_empty() {
                bail!("network hostname resolved to no addresses")
            }
            // DNS names cannot grant implicit access to local/private hosts.
            // An explicitly granted IP origin is the unambiguous v1 path.
            if addresses.iter().any(|a| !public_address(a.ip())) {
                bail!("DNS target resolves to a local or private address")
            }
            builder = builder.resolve_to_addrs(hostname, &addresses);
        }
        Ok(Self {
            client: builder.build()?,
            permit,
            cx: cx.clone(),
            url: parsed,
            method,
        })
    }

    pub(crate) fn permit(&self) -> &DispatchPermit {
        &self.permit
    }

    pub(crate) async fn send_owned(
        self,
        headers: CoreValue,
        body: Option<CoreBuffer>,
        budget: Arc<dyn AllocationBudget>,
    ) -> Result<NetResponse> {
        self.cx.check_active()?;
        let items = headers
            .array()
            .context("network headers must be an array")?;
        if items.len() > 128 {
            bail!("network header count exceeded")
        }
        let mut total = 0usize;
        for pair in items.iter() {
            let pair = pair.array().context("network header must be a pair")?;
            if pair.len() != 2 {
                bail!("network header pair arity mismatch")
            }
            let name = pair.get(0).context("header name missing")?;
            let value = pair.get(1).context("header value missing")?;
            total = total
                .checked_add(name.as_str().context("header name type")?.len())
                .and_then(|n| n.checked_add(value.as_str()?.len()))
                .context("network header size overflow")?;
        }
        if total > 65536 {
            bail!("network headers exceed their aggregate bound")
        }
        // HTTP parser/transport remains native bounded infrastructure. Prepay
        // retained guest-derived header/body data separately before native
        // request copying and hold that quota through response/transport drop.
        let maximum = self.cx.limits().output_bytes.min(MAX_RECEIVE_BYTES);
        let reserve = receive_quota_bytes(maximum, total)?;
        let quota = ReceiveQuota::new(budget.clone(), reserve)?;
        let mut request = self.client.request(self.method, self.url);
        for pair in items.iter() {
            let pair = pair.array().context("invalid header pair")?;
            let name = pair.get(0).context("header name")?;
            let value = pair.get(1).context("header value")?;
            request = request.header(
                name.as_str().context("header name type")?,
                value.as_str().context("header value type")?,
            );
        }
        if let Some(body) = body {
            request = request.body(Bytes::from_owner(BodyOwner(body)));
        }
        let response = tokio::select! {
            _=self.cx.cancel().cancelled()=>bail!("network request cancelled"),
            _=tokio::time::sleep_until(self.cx.deadline().into())=>bail!("original network deadline elapsed"),
            response=request.send()=>response.context("network request failed")?,
        };
        let status = response.status().as_u16();
        if response.headers().len() > 128 {
            bail!("response header count exceeded")
        }
        let mut header_bytes = 0usize;
        for (name, value) in response.headers() {
            header_bytes = header_bytes
                .checked_add(name.as_str().len())
                .and_then(|n| n.checked_add(value.as_bytes().len()))
                .context("response header size overflow")?;
        }
        if header_bytes > 65536 {
            bail!("response headers exceed their bound")
        }
        let mut output_headers = CoreValue::array_builder(&budget, response.headers().len())?;
        for (name, value) in response.headers() {
            output_headers.push(CoreValue::array_from(
                [
                    CoreValue::string(name.as_str(), &budget),
                    value
                        .to_str()
                        .map_err(|_| AllocationError::Invalid)
                        .and_then(|value| CoreValue::string(value, &budget)),
                ]
                .into_iter(),
                &budget,
            )?)?;
        }
        let output_headers = output_headers.finish()?;
        Ok(NetResponse {
            response: Some(response),
            pending: PendingChunks::new(),
            cx: self.cx,
            status,
            headers: output_headers,
            received: 0,
            _request_headers: headers,
            _quota: quota,
        })
    }
}
struct BodyOwner(CoreBuffer);
impl AsRef<[u8]> for BodyOwner {
    fn as_ref(&self) -> &[u8] {
        self.0.as_bytes()
    }
}
fn receive_quota_bytes(maximum: u64, header_bytes: usize) -> Result<u64> {
    maximum
        .checked_add(MAX_RECEIVE_CHUNK_BYTES as u64)
        .and_then(|n| n.checked_add(u64::try_from(header_bytes).ok()?.checked_mul(4)?))
        .and_then(|n| n.checked_add(std::mem::size_of::<PendingChunks>() as u64))
        .context("network retained buffer quota overflow")
}

/// No allocation, resizing or payload copy on chunk admission. The response's
/// ReceiveQuota prepays this complete inline ring and all retained transport
/// bytes. A destination allocation never advances these unpublished bytes.
struct PendingChunks {
    chunks: [Bytes; MAX_READY_CHUNKS],
    head: usize,
    count: usize,
    bytes: usize,
}
impl PendingChunks {
    fn new() -> Self {
        Self {
            chunks: std::array::from_fn(|_| Bytes::new()),
            head: 0,
            count: 0,
            bytes: 0,
        }
    }
    fn is_empty(&self) -> bool {
        self.bytes == 0
    }
    fn is_full(&self) -> bool {
        self.count == MAX_READY_CHUNKS
    }
    fn push(&mut self, chunk: Bytes) -> Result<()> {
        if chunk.is_empty() {
            return Ok(());
        }
        if self.is_full() {
            bail!("network pending chunk count exceeded")
        }
        let bytes = self
            .bytes
            .checked_add(chunk.len())
            .context("network pending byte count overflow")?;
        let tail = (self.head + self.count) % MAX_READY_CHUNKS;
        self.chunks[tail] = chunk;
        self.count += 1;
        self.bytes = bytes;
        Ok(())
    }
    fn copy_into(&self, output: &mut [u8]) -> std::result::Result<(), AllocationError> {
        if output.len() > self.bytes {
            return Err(AllocationError::Invalid);
        }
        let mut offset = 0;
        for index in 0..self.count {
            let chunk = &self.chunks[(self.head + index) % MAX_READY_CHUNKS];
            let count = chunk.len().min(output.len() - offset);
            output[offset..offset + count].copy_from_slice(&chunk[..count]);
            offset += count;
            if offset == output.len() {
                break;
            }
        }
        Ok(())
    }
    fn consume(&mut self, mut count: usize) {
        debug_assert!(count <= self.bytes);
        self.bytes -= count;
        while count != 0 {
            let chunk = &mut self.chunks[self.head];
            let consumed = chunk.len().min(count);
            let _ = chunk.split_to(consumed);
            count -= consumed;
            if chunk.is_empty() {
                // Drop the actual backing allocation before quota retirement.
                *chunk = Bytes::new();
                self.head = (self.head + 1) % MAX_READY_CHUNKS;
                self.count -= 1;
            }
        }
    }
    fn clear(&mut self) {
        for chunk in &mut self.chunks {
            *chunk = Bytes::new();
        }
        self.head = 0;
        self.count = 0;
        self.bytes = 0;
    }
}

/// The generic boundary is private and gives tests the same coalescing path
/// without opening a socket. Production always uses the existing Response.
pub(crate) trait ChunkSource: Send {
    fn next_chunk(&mut self) -> impl Future<Output = Result<Option<Bytes>>> + Send;
}
impl ChunkSource for Response {
    async fn next_chunk(&mut self) -> Result<Option<Bytes>> {
        self.chunk().await.context("network response read failed")
    }
}
struct ReceiveQuota {
    budget: Arc<dyn AllocationBudget>,
    bytes: u64,
}
impl ReceiveQuota {
    fn new(budget: Arc<dyn AllocationBudget>, bytes: u64) -> Result<Self> {
        budget.reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes,
            elements: 128 + MAX_READY_CHUNKS as u64,
            depth: 0,
            work: 0,
        })?;
        Ok(Self { budget, bytes })
    }
}
impl Drop for ReceiveQuota {
    fn drop(&mut self) {
        self.budget.release(self.bytes)
    }
}

pub(crate) struct NetResponse<S = Response> {
    response: Option<S>,
    pending: PendingChunks,
    cx: CallCx,
    pub(crate) status: u16,
    pub(crate) headers: CoreValue,
    received: u64,
    _request_headers: CoreValue,
    // Last field: guest-derived pending/header/response storage is destroyed
    // before quota release. cancel() explicitly drops transport first too.
    _quota: ReceiveQuota,
}
impl<S> NetResponse<S> {
    pub(crate) fn check_active(&mut self) -> Result<()> {
        if let Err(error) = self.cx.check_active() {
            self.cancel();
            return Err(error.into());
        }
        Ok(())
    }
    fn accept_chunk(&mut self, chunk: Option<Bytes>) -> Result<()> {
        let Some(chunk) = chunk else {
            self.response.take();
            return Ok(());
        };
        let Some(received) = self.received.checked_add(chunk.len() as u64) else {
            self.cancel();
            bail!("network byte count overflow")
        };
        if chunk.len() > MAX_RECEIVE_CHUNK_BYTES
            || received > self.cx.limits().output_bytes.min(MAX_RECEIVE_BYTES)
        {
            self.cancel();
            bail!("network response exceeds its bounded transport/output quota")
        }
        self.pending.push(chunk)?;
        self.received = received;
        Ok(())
    }
    pub(crate) fn cancel(&mut self) {
        self.response.take();
        self.pending.clear();
    }
}
impl<S: ChunkSource> NetResponse<S> {
    pub(crate) async fn read_owned(
        &mut self,
        max: u32,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<CoreBuffer>> {
        if max == 0 || u64::from(max) > self.cx.limits().output_bytes {
            bail!("network read exceeds its output bound")
        }
        self.check_active()?;
        // Fund bounded chunk attempts/ring bookkeeping before any read. Byte
        // copying is separately prepaid by CoreBuffer's destination owner.
        budget.reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes: 0,
            elements: MAX_READY_CHUNKS as u64,
            depth: 0,
            work: MAX_READY_CHUNKS as u64,
        })?;
        let mut attempts = 0;
        if self.pending.is_empty() {
            let Some(response) = self.response.as_mut() else {
                return Ok(None);
            };
            let chunk = tokio::select! {
                biased;
                _=self.cx.cancel().cancelled()=>{self.cancel();bail!("network response cancelled")},
                _=tokio::time::sleep_until(self.cx.deadline().into())=>{self.cancel();bail!("original network response deadline elapsed")},
                chunk=response.next_chunk()=>chunk?,
            };
            attempts += 1;
            self.accept_chunk(chunk)?;
        }
        while self.pending.bytes < max as usize
            && !self.pending.is_full()
            && attempts < MAX_READY_CHUNKS
        {
            self.check_active()?;
            let Some(response) = self.response.as_mut() else {
                break;
            };
            let ready = {
                let mut chunk = pin!(response.next_chunk());
                // Always returns Ready after one poll with the actual task
                // waker. There is no second wait, timer or request here.
                poll_fn(|task| Poll::Ready(chunk.as_mut().poll(task))).await
            };
            attempts += 1;
            match ready {
                Poll::Pending => break,
                Poll::Ready(chunk) => self.accept_chunk(chunk?)?,
            }
        }
        self.check_active()?;
        if self.pending.is_empty() && self.response.is_none() {
            return Ok(None);
        }
        let count = self.pending.bytes.min(max as usize);
        // Both destination payload and its final shared block must succeed
        // before advancing the cursor. EOF with queued data still returns data;
        // an empty/Pending burst returns Some(empty), never a fabricated EOF.
        let value =
            CoreBuffer::write_exact(count, budget, |output| self.pending.copy_into(output))?;
        self.check_active()?;
        self.pending.consume(count);
        Ok(Some(value))
    }
}
impl<S> Drop for NetResponse<S> {
    fn drop(&mut self) {
        self.cancel()
    }
}

fn public_address(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(ip) => {
            let [a, b, c, _] = ip.octets();
            !ip.is_private()
                && !ip.is_loopback()
                && !ip.is_link_local()
                && !ip.is_unspecified()
                && !ip.is_broadcast()
                && !ip.is_multicast()
                && !ip.is_documentation()
                && a != 0
                && a < 240
                && !(a == 100 && (64..=127).contains(&b))
                && !(a == 198 && (18..=19).contains(&b))
                && !(a == 192 && b == 0 && c == 0)
                && !(a == 192 && b == 88 && c == 99)
        }
        IpAddr::V6(ip) => {
            if let Some(v4) = ip.to_ipv4_mapped() {
                return public_address(IpAddr::V4(v4));
            }
            let segments = ip.segments();
            !ip.is_loopback()
                && !ip.is_unspecified()
                && !ip.is_multicast()
                && (segments[0] & 0xfe00) != 0xfc00
                && (segments[0] & 0xffc0) != 0xfe80
                && (segments[0] & 0xffc0) != 0xfec0
                && !(segments[0] == 0x2001 && segments[1] == 0x0db8)
                && segments[..6].iter().any(|s| *s != 0)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::Mutex;
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
    use std::task::Context as TaskContext;
    use tobkiri_canon::ids::{ProfileId, RequestId};
    use tobkiri_gate::{CoreCancellation, Principal, ResourceLimits};

    #[derive(Default)]
    struct Budget {
        live: AtomicU64,
        calls: AtomicUsize,
        deny_on: AtomicUsize,
        requests: Mutex<Vec<AllocationRequest>>,
    }
    impl AllocationBudget for Budget {
        fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
            let call = self.calls.fetch_add(1, Ordering::SeqCst) + 1;
            self.requests.lock().unwrap().push(request);
            if self.deny_on.load(Ordering::SeqCst) == call {
                return Err(AllocationError::Limit);
            }
            self.live.fetch_add(request.bytes, Ordering::SeqCst);
            Ok(())
        }
        fn release(&self, bytes: u64) {
            let previous = self.live.fetch_sub(bytes, Ordering::SeqCst);
            assert!(
                previous >= bytes,
                "release must follow an actual reservation"
            );
        }
    }
    enum ChunkStep {
        Data(Bytes),
        Pending,
        Eof,
        Error,
        Cancel(CoreCancellation, Bytes),
    }
    struct DropWitness {
        budget: Arc<Budget>,
        minimum: u64,
        drops: Arc<AtomicUsize>,
    }
    impl Drop for DropWitness {
        fn drop(&mut self) {
            assert!(self.budget.live.load(Ordering::SeqCst) >= self.minimum);
            self.drops.fetch_add(1, Ordering::SeqCst);
        }
    }
    struct Scripted {
        steps: VecDeque<ChunkStep>,
        polls: Arc<AtomicUsize>,
        _drop_witness: Option<DropWitness>,
    }
    impl ChunkSource for Scripted {
        async fn next_chunk(&mut self) -> Result<Option<Bytes>> {
            poll_fn(|_| {
                self.polls.fetch_add(1, Ordering::SeqCst);
                match self.steps.pop_front().unwrap_or(ChunkStep::Eof) {
                    ChunkStep::Data(bytes) => Poll::Ready(Ok(Some(bytes))),
                    ChunkStep::Pending => Poll::Pending,
                    ChunkStep::Eof => Poll::Ready(Ok(None)),
                    ChunkStep::Error => Poll::Ready(Err(anyhow::anyhow!("fixture read failure"))),
                    ChunkStep::Cancel(cancel, bytes) => {
                        cancel.cancel();
                        Poll::Ready(Ok(Some(bytes)))
                    }
                }
            })
            .await
        }
    }
    fn context(output_bytes: u64, deadline: Instant) -> CallCx {
        CallCx::root(
            ProfileId::parse("profile-network-coalesce").unwrap(),
            Principal::User,
            RequestId::parse("network-coalesce-request").unwrap(),
            CoreCancellation::new(),
            deadline,
            ResourceLimits {
                output_bytes,
                ..Default::default()
            },
        )
    }
    fn scripted(
        steps: impl IntoIterator<Item = ChunkStep>,
        output_bytes: u64,
    ) -> (NetResponse<Scripted>, Arc<AtomicUsize>, Arc<Budget>) {
        let polls = Arc::new(AtomicUsize::new(0));
        let transport = Arc::new(Budget::default());
        let transport_budget: Arc<dyn AllocationBudget> = transport.clone();
        let header_budget: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
        let headers = CoreValue::null(&header_budget).unwrap();
        let quota = ReceiveQuota::new(
            transport_budget,
            receive_quota_bytes(output_bytes.min(MAX_RECEIVE_BYTES), 0).unwrap(),
        )
        .unwrap();
        (
            NetResponse {
                response: Some(Scripted {
                    steps: steps.into_iter().collect(),
                    polls: polls.clone(),
                    _drop_witness: None,
                }),
                pending: PendingChunks::new(),
                cx: context(output_bytes, Instant::now() + Duration::from_secs(30)),
                status: 200,
                headers: headers.clone(),
                received: 0,
                _request_headers: headers,
                _quota: quota,
            },
            polls,
            transport,
        )
    }
    fn output_budget() -> (Arc<Budget>, Arc<dyn AllocationBudget>) {
        let owner = Arc::new(Budget::default());
        (owner.clone(), owner)
    }
    fn data(value: &'static [u8]) -> ChunkStep {
        ChunkStep::Data(Bytes::from_static(value))
    }
    fn poll_once<T>(future: std::pin::Pin<&mut impl Future<Output = T>>) -> Poll<T> {
        let mut task = TaskContext::from_waker(futures_util::task::noop_waker_ref());
        future.poll(&mut task)
    }

    #[tokio::test]
    async fn coalesce_ready_chunks_up_to_max_and_retain_the_unpublished_tail() {
        let (mut response, polls, _) = scripted(
            [data(b"ab"), data(b"cdefg"), data(b"hi"), ChunkStep::Pending],
            8192,
        );
        let (_, budget) = output_budget();
        let first = response.read_owned(5, &budget).await.unwrap().unwrap();
        assert_eq!(first.as_bytes(), b"abcde");
        assert_eq!(polls.load(Ordering::SeqCst), 2);
        assert_eq!(response.pending.bytes, 2);
        let second = response.read_owned(8192, &budget).await.unwrap().unwrap();
        assert_eq!(second.as_bytes(), b"fghi");
        assert_eq!(polls.load(Ordering::SeqCst), 4);
        assert!(response.response.is_some());
        assert!(response.read_owned(8192, &budget).await.unwrap().is_none());
        assert_eq!(response.received, 9);
    }

    #[tokio::test]
    async fn coalesce_pending_is_not_eof_and_has_no_second_wait() {
        let (mut response, polls, _) = scripted(
            [data(b"a"), ChunkStep::Pending, data(b"b"), ChunkStep::Eof],
            8192,
        );
        let (_, budget) = output_budget();
        let first = {
            let mut read = pin!(response.read_owned(8192, &budget));
            match poll_once(read.as_mut()) {
                Poll::Ready(result) => result.unwrap().unwrap(),
                Poll::Pending => panic!("ready data must not wait for another chunk"),
            }
        };
        assert_eq!(first.as_bytes(), b"a");
        assert_eq!(polls.load(Ordering::SeqCst), 2);
        assert!(response.response.is_some());
        let second = response.read_owned(8192, &budget).await.unwrap().unwrap();
        assert_eq!(second.as_bytes(), b"b");
        assert!(response.response.is_none());
        assert!(response.read_owned(8192, &budget).await.unwrap().is_none());
        assert_eq!(polls.load(Ordering::SeqCst), 4);
    }

    #[tokio::test]
    async fn coalesce_ring_wrap_preserves_order_across_repeated_small_reads() {
        let chunks = (0..65u8)
            .map(|byte| ChunkStep::Data(Bytes::from(vec![byte])))
            .chain([ChunkStep::Eof]);
        let (mut response, _, _) = scripted(chunks, 8192);
        let (_, budget) = output_budget();
        let mut output = Vec::new();
        while let Some(value) = response.read_owned(3, &budget).await.unwrap() {
            assert!(value.len() <= 3);
            output.extend_from_slice(value.as_bytes());
        }
        assert_eq!(output, (0..65u8).collect::<Vec<_>>());
        assert_eq!(response.received, 65);
    }

    #[tokio::test]
    async fn coalesce_bounds_empty_and_nonempty_bursts_at_32_source_chunks() {
        for empty in [true, false] {
            let chunks = (0..MAX_READY_CHUNKS)
                .map(|_| data(if empty { b"" } else { b"x" }))
                .chain([data(b"y"), ChunkStep::Eof]);
            let (mut response, polls, _) = scripted(chunks, 8192);
            let (_, budget) = output_budget();
            let first = response.read_owned(8192, &budget).await.unwrap().unwrap();
            assert_eq!(first.len(), if empty { 0 } else { MAX_READY_CHUNKS });
            assert_eq!(polls.load(Ordering::SeqCst), MAX_READY_CHUNKS);
            assert!(response.response.is_some());
            assert_eq!(
                response
                    .read_owned(8192, &budget)
                    .await
                    .unwrap()
                    .unwrap()
                    .as_bytes(),
                b"y"
            );
            assert!(response.read_owned(8192, &budget).await.unwrap().is_none());
        }
    }

    #[tokio::test]
    async fn coalesce_denials_preserve_all_received_bytes_until_destination_commit() {
        for deny_on in [1, 2, 3] {
            let (mut response, polls, _) = scripted(
                [data(b"ab"), data(b"cd"), ChunkStep::Pending, ChunkStep::Eof],
                8192,
            );
            let (owner, budget) = output_budget();
            owner.deny_on.store(deny_on, Ordering::SeqCst);
            let error = response.read_owned(8192, &budget).await.unwrap_err();
            assert_eq!(
                error.downcast_ref::<AllocationError>(),
                Some(&AllocationError::Limit)
            );
            assert_eq!(owner.live.load(Ordering::SeqCst), 0);
            if deny_on == 1 {
                assert_eq!(polls.load(Ordering::SeqCst), 0);
                assert_eq!(response.received, 0);
            } else {
                assert_eq!(response.pending.bytes, 4);
                assert_eq!(response.received, 4);
            }
            owner.deny_on.store(0, Ordering::SeqCst);
            let value = response.read_owned(8192, &budget).await.unwrap().unwrap();
            assert_eq!(value.as_bytes(), b"abcd");
            assert_eq!(response.pending.bytes, 0);
            drop(value);
            assert_eq!(owner.live.load(Ordering::SeqCst), 0);
            let requests = owner.requests.lock().unwrap();
            assert_eq!(requests[0].bytes, 0);
            assert_eq!(requests[0].work, MAX_READY_CHUNKS as u64);
        }
    }

    #[tokio::test]
    async fn coalesce_dropping_the_initial_pending_read_keeps_transport_and_quota() {
        let (mut response, polls, transport) =
            scripted([ChunkStep::Pending, data(b"still here")], 8192);
        let (_, budget) = output_budget();
        let held = transport.live.load(Ordering::SeqCst);
        {
            let mut read = pin!(response.read_owned(8192, &budget));
            assert!(poll_once(read.as_mut()).is_pending());
        }
        assert_eq!(polls.load(Ordering::SeqCst), 1);
        assert_eq!(transport.live.load(Ordering::SeqCst), held);
        assert!(response.response.is_some());
        let value = response.read_owned(8192, &budget).await.unwrap().unwrap();
        assert_eq!(value.as_bytes(), b"still here");
        drop(response);
        assert_eq!(transport.live.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn coalesce_preserves_size_and_cumulative_transport_bounds() {
        let cases = [
            (
                vec![ChunkStep::Data(Bytes::from(vec![
                    0;
                    MAX_RECEIVE_CHUNK_BYTES
                        + 1
                ]))],
                MAX_RECEIVE_BYTES,
                0,
            ),
            (vec![data(b"1234"), data(b"56789")], 8, 0),
            (vec![data(b"abc")], MAX_RECEIVE_BYTES, u64::MAX - 1),
        ];
        for (steps, output, received) in cases {
            let (mut response, _, _) = scripted(steps, output);
            response.received = received;
            let (_, budget) = output_budget();
            assert!(
                response
                    .read_owned(output.min(8192) as u32, &budget)
                    .await
                    .is_err()
            );
            assert!(response.response.is_none());
            assert!(response.pending.is_empty());
        }
        let (mut response, polls, _) = scripted([data(b"untouched")], 8);
        let (_, budget) = output_budget();
        for max in [0, 9] {
            assert!(response.read_owned(max, &budget).await.is_err());
        }
        assert_eq!(polls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn coalesce_cancel_and_expired_deadline_never_publish_accumulated_data() {
        let (mut response, polls, _) = scripted([], 8192);
        let cancellation = response.cx.cancel().clone();
        response.response.as_mut().unwrap().steps = [
            data(b"a"),
            ChunkStep::Cancel(cancellation.clone(), Bytes::from_static(b"b")),
        ]
        .into_iter()
        .collect();
        let (_, budget) = output_budget();
        assert!(response.read_owned(8192, &budget).await.is_err());
        assert_eq!(polls.load(Ordering::SeqCst), 2);
        assert!(response.response.is_none());
        assert!(response.pending.is_empty());

        let (mut response, polls, _) = scripted([ChunkStep::Pending], 8192);
        let cancellation = response.cx.cancel().clone();
        {
            let mut read = pin!(response.read_owned(8192, &budget));
            assert!(poll_once(read.as_mut()).is_pending());
            cancellation.cancel();
            assert!(matches!(poll_once(read.as_mut()), Poll::Ready(Err(_))));
        }
        assert_eq!(polls.load(Ordering::SeqCst), 1);
        assert!(response.response.is_none());

        let (mut response, polls, _) = scripted([data(b"untouched")], 8192);
        response.cx = context(8192, Instant::now() - Duration::from_secs(1));
        assert!(response.read_owned(8192, &budget).await.is_err());
        assert_eq!(polls.load(Ordering::SeqCst), 0);
        assert!(response.response.is_none());
    }

    #[tokio::test]
    async fn coalesce_ready_error_is_not_partial_success_or_eof() {
        let (mut response, _, _) =
            scripted([data(b"retained"), ChunkStep::Error, ChunkStep::Eof], 8192);
        let (_, budget) = output_budget();
        assert!(response.read_owned(8192, &budget).await.is_err());
        assert_eq!(response.pending.bytes, 8);
        assert!(response.response.is_some());
        assert_eq!(
            response
                .read_owned(8192, &budget)
                .await
                .unwrap()
                .unwrap()
                .as_bytes(),
            b"retained"
        );
        assert!(response.read_owned(8192, &budget).await.unwrap().is_none());
    }

    #[test]
    fn coalesce_ring_and_transport_die_before_receive_quota_is_released() {
        struct OwnedChunk {
            bytes: Vec<u8>,
            _witness: DropWitness,
        }
        impl AsRef<[u8]> for OwnedChunk {
            fn as_ref(&self) -> &[u8] {
                &self.bytes
            }
        }
        let (mut response, _, transport) = scripted([], 8192);
        let reserved = transport.live.load(Ordering::SeqCst);
        assert_eq!(
            reserved,
            8192 + MAX_RECEIVE_CHUNK_BYTES as u64 + std::mem::size_of::<PendingChunks>() as u64
        );
        let drops = Arc::new(AtomicUsize::new(0));
        let witness = || DropWitness {
            budget: transport.clone(),
            minimum: reserved,
            drops: drops.clone(),
        };
        response.response.as_mut().unwrap()._drop_witness = Some(witness());
        response
            .pending
            .push(Bytes::from_owner(OwnedChunk {
                bytes: vec![1, 2, 3],
                _witness: witness(),
            }))
            .unwrap();
        drop(response);
        assert_eq!(drops.load(Ordering::SeqCst), 2);
        assert_eq!(transport.live.load(Ordering::SeqCst), 0);
        assert!(receive_quota_bytes(u64::MAX, 0).is_err());
        assert!(receive_quota_bytes(0, usize::MAX).is_err());
    }

    #[test]
    fn special_use_dns_targets_are_not_treated_as_public_routes() {
        for value in [
            "100.64.0.0",
            "100.127.255.255",
            "198.18.0.0",
            "198.19.255.255",
            "192.0.0.0",
            "192.0.0.255",
            "192.88.99.1",
            "240.0.0.0",
            "254.255.255.255",
            "127.0.0.1",
            "10.0.0.1",
            "169.254.0.1",
            "fc00::1",
            "fdff::1",
            "fe80::1",
            "fec0::1",
            "feff::1",
            "::ffff:127.0.0.1",
            "2001:db8::1",
        ] {
            assert!(!public_address(value.parse().unwrap()), "{value}");
        }
        for value in [
            "8.8.8.8",
            "1.1.1.1",
            "100.63.255.255",
            "100.128.0.0",
            "198.17.255.255",
            "198.20.0.0",
            "2606:4700:4700::1111",
        ] {
            assert!(public_address(value.parse().unwrap()), "{value}");
        }
    }
}
