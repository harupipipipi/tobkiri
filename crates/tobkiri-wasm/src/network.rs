use anyhow::{Context, Result, bail};
use bytes::Bytes;
use reqwest::{Client, Method, Response};
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tobkiri_canon::{
    AllocationBudget, AllocationError, AllocationKind, AllocationRequest, CoreBuffer, CoreValue,
    Digest,
};
use tobkiri_gate::{CallCx, DispatchPermit, EffectRequest, Grant};
use url::Url;

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
        let maximum = self.cx.limits().output_bytes.min(32 * 1024 * 1024);
        let reserve = maximum
            .checked_add(2 * 1024 * 1024)
            .and_then(|n| n.checked_add((total as u64) * 4))
            .context("network retained buffer quota overflow")?;
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
            pending: Bytes::new(),
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
struct ReceiveQuota {
    budget: Arc<dyn AllocationBudget>,
    bytes: u64,
}
impl ReceiveQuota {
    fn new(budget: Arc<dyn AllocationBudget>, bytes: u64) -> Result<Self> {
        budget.reserve(AllocationRequest {
            kind: AllocationKind::Temporary,
            bytes,
            elements: 128,
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

pub(crate) struct NetResponse {
    response: Option<Response>,
    pending: Bytes,
    cx: CallCx,
    pub(crate) status: u16,
    pub(crate) headers: CoreValue,
    received: u64,
    _request_headers: CoreValue,
    // Last field: guest-derived pending/header/response storage is destroyed
    // before quota release. cancel() explicitly drops transport first too.
    _quota: ReceiveQuota,
}
impl NetResponse {
    pub(crate) fn check_active(&mut self) -> Result<()> {
        if let Err(error) = self.cx.check_active() {
            self.cancel();
            return Err(error.into());
        }
        Ok(())
    }
    pub(crate) async fn read_owned(
        &mut self,
        max: u32,
        budget: &Arc<dyn AllocationBudget>,
    ) -> Result<Option<CoreBuffer>> {
        if max == 0 || u64::from(max) > self.cx.limits().output_bytes {
            bail!("network read exceeds its output bound")
        }
        self.check_active()?;
        if self.pending.is_empty() {
            let Some(response) = self.response.as_mut() else {
                return Ok(None);
            };
            let chunk = tokio::select! {
                _=self.cx.cancel().cancelled()=>{self.cancel();bail!("network response cancelled")},
                _=tokio::time::sleep_until(self.cx.deadline().into())=>{self.cancel();bail!("original network response deadline elapsed")},
                chunk=response.chunk()=>chunk.context("network response read failed")?,
            };
            let Some(chunk) = chunk else {
                self.response.take();
                return Ok(None);
            };
            self.received = self
                .received
                .checked_add(chunk.len() as u64)
                .context("network byte count overflow")?;
            if chunk.len() > 2 * 1024 * 1024
                || self.received > self.cx.limits().output_bytes.min(32 * 1024 * 1024)
            {
                self.cancel();
                bail!("network response exceeds its bounded transport/output quota")
            }
            self.pending = chunk;
        }
        let count = self.pending.len().min(max as usize);
        // Destination is admitted BEFORE copying; split only after success so
        // an allocation error does not silently advance a published cursor.
        let value = CoreBuffer::copy_from(&self.pending[..count], budget)?;
        let _ = self.pending.split_to(count);
        Ok(Some(value))
    }
    pub(crate) fn cancel(&mut self) {
        self.response.take();
        self.pending = Bytes::new();
    }
}
impl Drop for NetResponse {
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
