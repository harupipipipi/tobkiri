//! Closed native transport observations. A message or guest error cannot
//! impersonate reqwest's concrete error type or change authority/retry policy.

/// Return stable metadata without exposing a URL, header, body or error chain.
/// Existing authority, allocation and sealed storage failures take priority.
pub(crate) fn failure_metadata(error: &anyhow::Error) -> Option<(&'static str, &'static str)> {
    if error.downcast_ref::<tobkiri_gate::GateError>().is_some()
        || error.downcast_ref::<crate::filesystem::FsError>().is_some()
        || error.downcast_ref::<tobkiri_store::StoreError>().is_some()
        || error
            .downcast_ref::<tobkiri_canon::AllocationError>()
            .is_some()
        || error
            .downcast_ref::<wasmtime::component::HostAllocationError>()
            .is_some()
    {
        return None;
    }
    let native = error.downcast_ref::<reqwest::Error>()?;
    Some(if native.is_timeout() {
        (
            "network-timeout",
            "network-timeout: native transport timed out",
        )
    } else if native.is_connect() {
        (
            "network-connect",
            "network-connect: native transport connection failed",
        )
    } else if native.is_body() {
        ("network-body", "network-body: native response body failed")
    } else if native.is_decode() {
        (
            "network-decode",
            "network-decode: native response decoding failed",
        )
    } else {
        (
            "network-transport",
            "network-transport: native transport failed",
        )
    })
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use std::time::Duration;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    // A private loopback fixture, never an external service or model. The
    // server deliberately retains an incomplete body until the native timeout.
    pub(crate) async fn native_body_timeout() -> anyhow::Error {
        tokio::time::timeout(Duration::from_secs(8), async {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .unwrap();
            let address = listener.local_addr().unwrap();
            let (finish, wait) = tokio::sync::oneshot::channel::<()>();
            let server = tokio::spawn(async move {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = [0u8; 4096];
                let _ = socket.read(&mut request).await.unwrap();
                socket
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\nConnection: close\r\n\r\npartial")
                    .await
                    .unwrap();
                let _ = wait.await;
            });
            let response = reqwest::Client::builder()
                .no_proxy()
                .timeout(Duration::from_secs(2))
                .build()
                .unwrap()
                .get(format!("http://{address}/PRIVATE-FIXTURE-ONLY"))
                .send()
                .await
                .unwrap();
            let error = response.bytes().await.unwrap_err();
            let _ = finish.send(());
            server.await.unwrap();
            assert!(error.is_timeout());
            anyhow::Error::new(error).context("PRIVATE REQUEST OR PROVIDER DIAGNOSTIC")
        })
        .await
        .expect("bounded private fixture must terminate")
    }

    #[tokio::test]
    async fn native_body_timeout_is_typed_and_private_context_is_not_metadata() {
        let error = native_body_timeout().await;
        assert_eq!(
            failure_metadata(&error),
            Some((
                "network-timeout",
                "network-timeout: native transport timed out"
            ))
        );
        assert!(error.downcast_ref::<reqwest::Error>().is_some());
        let metadata = failure_metadata(&error).unwrap();
        assert!(!metadata.1.contains("PRIVATE"));
        assert!(!metadata.1.contains("127.0.0.1"));
        let sealed = error.context(tobkiri_store::StoreError::CommitUnknown);
        assert!(failure_metadata(&sealed).is_none());
        assert!(crate::core_imports::storage_failure_metadata(&sealed).is_some());
    }

    #[test]
    fn private_text_cannot_forge_native_transport_metadata() {
        let imitation = anyhow::anyhow!("network-timeout PRIVATE reqwest::Error");
        assert!(failure_metadata(&imitation).is_none());
        let gate = imitation.context(tobkiri_gate::GateError::Denied("PRIVATE".into()));
        assert!(failure_metadata(&gate).is_none());
    }
}
