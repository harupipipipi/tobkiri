use super::*;
use std::{ffi::OsString, fs, net::Ipv4Addr, time::Duration};
use tobkiri_canon::Digest;
use tobkiri_gate::{CapacityLedger, CoreControl, GateError, NativeSetupAccount, NativeSetupLease};
use tobkiri_wasm::{LEGACY_LOCAL_CHAT_RESPONSE_LIMIT, WasmEngine};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    net::TcpListener,
    time::timeout,
};

const WAIT: Duration = Duration::from_secs(5);
const PROFILES: &str = "packs/rumi_provider_registry_pack/profiles";
const STORE: &str = "provider_registry.store.json";
const CONNECTION: &str = "local/model";
const ORIGINAL_SAVE: &str = include_str!(concat!(
    "../../../../packs/local.provider-registry-rust/evidence/original-python/cases/",
    "local-endpoint-canonicalization/actions/save-revision-0.store-after.json"
));
const ORIGINAL_REQUEST: &str = concat!(
    r#"{"model":"saved/model","messages":[{"role":"user","content":"猫"}],"stream":false,"#,
    r#""temperature":0.25,"tools":[{"type":"function","function":{"name":"find"}}]}"#,
);

struct TestLease;

impl NativeSetupLease for TestLease {
    fn validate(&self) -> Result<(), GateError> {
        Ok(())
    }

    fn allocation_domain(&self) -> Digest {
        Digest::hash(b"original local chat composed socket test")
    }
}

fn native_account() -> (WasmEngine, NativeSetupAccount) {
    let control = CoreControl::default();
    let account = CapacityLedger::new()
        .native_setup_account(&control, || Ok(Arc::new(TestLease)))
        .unwrap();
    (WasmEngine::new_with_control(&control).unwrap(), account)
}

fn write_registry(root: &DataRoot, profile: &ProfileId, endpoint: &str, enabled: bool) -> String {
    let directory = root.path().join(PROFILES).join(profile.as_str());
    fs::create_dir_all(&directory).unwrap();
    let document = ORIGINAL_SAVE
        .replace("http://[::1]:1234/v1", endpoint)
        .replace(
            r#""profile_id": "defaults""#,
            &format!(r#""profile_id": "{profile}""#),
        )
        .replace(r#""enabled": true"#, &format!(r#""enabled": {enabled}"#));
    fs::write(directory.join(STORE), &document).unwrap();
    document
}

fn assert_registry_unchanged(root: &DataRoot, profile: &ProfileId, expected: &str) {
    let directory = root.path().join(PROFILES).join(profile.as_str());
    let entries: Vec<_> = fs::read_dir(&directory)
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(entries, [OsString::from(STORE)]);
    assert_eq!(
        fs::read(directory.join(STORE)).unwrap(),
        expected.as_bytes()
    );
}

fn original_request<'a>(
    profile: &'a ProfileId,
    endpoint: &'a str,
    revision: u64,
    cancellation: &'a CoreCancellation,
) -> OriginalLocalChatRequest<'a> {
    OriginalLocalChatRequest {
        profile,
        provider: LegacyLocalChatSelection {
            connection_id: CONNECTION,
            record_revision: revision,
            endpoint,
        },
        body: ORIGINAL_REQUEST.as_bytes(),
        expected_policy: None,
        deadline: Instant::now() + WAIT,
        cancellation,
    }
}

#[tokio::test]
async fn selected_profile_posts_original_json_and_releases_real_native_capacity() {
    assert_original_json_post(false).await;
}

#[tokio::test]
async fn policy_checked_post_releases_real_native_capacity() {
    assert_original_json_post(true).await;
}

async fn assert_original_json_post(check_policy: bool) {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open_existing(temp.path().canonicalize().unwrap()).unwrap();
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let endpoint = format!("http://{}/v1", listener.local_addr().unwrap());
    let other_listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let other_endpoint = format!("http://{}/v1", other_listener.local_addr().unwrap());
    let profile = ProfileId::parse("work").unwrap();
    let defaults = ProfileId::parse("defaults").unwrap();
    let saved = write_registry(&root, &profile, &endpoint, true);
    let other_saved = write_registry(&root, &defaults, &other_endpoint, true);

    let mut reply = br#"{ "choices": [{"message": {"content": "ok"}}], "usage": {} }"#.to_vec();
    reply.resize(LEGACY_LOCAL_CHAT_RESPONSE_LIMIT, b' ');
    *reply.last_mut().unwrap() = b'\n';
    let reply = Arc::new(reply);
    let served = reply.clone();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let mut socket = BufReader::new(socket);
        let mut request_line = String::new();
        socket.read_line(&mut request_line).await.unwrap();
        assert_eq!(request_line, "POST /v1/chat/completions HTTP/1.1\r\n");
        let mut headers = String::new();
        loop {
            let mut line = String::new();
            assert_ne!(socket.read_line(&mut line).await.unwrap(), 0);
            if line == "\r\n" {
                break;
            }
            headers.push_str(&line);
        }
        let headers = headers.to_ascii_lowercase();
        assert!(headers.contains("content-type: application/json\r\n"));
        assert!(headers.contains("accept: application/json\r\n"));
        assert!(!headers.contains("authorization:"));
        assert!(headers.contains(&format!("content-length: {}\r\n", ORIGINAL_REQUEST.len())));
        let mut body = vec![0; ORIGINAL_REQUEST.len()];
        socket.read_exact(&mut body).await.unwrap();
        assert_eq!(body, ORIGINAL_REQUEST.as_bytes());
        let header = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n",
            served.len()
        );
        socket.get_mut().write_all(header.as_bytes()).await.unwrap();
        socket.get_mut().write_all(served.as_slice()).await.unwrap();
    });

    let (engine, account) = native_account();
    let store = engine
        .native_setup_store(account.begin().unwrap(), ())
        .unwrap();
    let budget = store.allocation_owners().graph().clone();
    let observed = format!("{endpoint}/");
    let mut request = original_request(&profile, &observed, 1, store.cancellation());
    let policy = check_policy.then(|| {
        observe_original_local_chat_policy(&root, &profile, &request.provider, &budget).unwrap()
    });
    request.expected_policy = policy.as_ref();
    let initial = account.snapshot().unwrap();
    let mut dispatches = 0;
    let output = post_original_local_chat(&root, request, &budget, |phase| {
        if phase == OriginalLocalChatCheck::Dispatch {
            dispatches += 1;
        }
        Ok(())
    })
    .await
    .unwrap();
    assert_eq!(dispatches, 1);
    assert_eq!(output.as_bytes(), reply.as_slice());
    assert!(
        account.snapshot().unwrap().charged_bytes >= initial.charged_bytes + reply.len() as u64
    );
    drop(output);
    assert_eq!(
        account.snapshot().unwrap().charged_bytes,
        initial.charged_bytes
    );
    drop((policy, budget, store));
    tobkiri_wasm::reap_allocation_owners().unwrap();
    let final_state = account.snapshot().unwrap();
    assert_eq!(
        (final_state.charged_bytes, final_state.reservations),
        (0, 0)
    );

    timeout(WAIT, server).await.unwrap().unwrap();
    assert!(
        timeout(Duration::from_millis(100), other_listener.accept())
            .await
            .is_err()
    );
    assert_registry_unchanged(&root, &profile, &saved);
    assert_registry_unchanged(&root, &defaults, &other_saved);
}

#[tokio::test]
async fn changed_or_missing_registry_selection_cannot_obtain_dispatch_or_connect() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open_existing(temp.path().canonicalize().unwrap()).unwrap();
    let registered = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let endpoint = format!("http://{}/v1", registered.local_addr().unwrap());
    let other = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let other_endpoint = format!("http://{}/v1", other.local_addr().unwrap());
    let profile = ProfileId::parse("defaults").unwrap();
    let absent = ProfileId::parse("other").unwrap();
    let (engine, account) = native_account();
    let store = engine
        .native_setup_store(account.begin().unwrap(), ())
        .unwrap();
    let budget = store.allocation_owners().graph().clone();
    let initial = account.snapshot().unwrap().charged_bytes;

    for (selected_profile, observed, revision, enabled) in [
        (&profile, endpoint.as_str(), 2, true),
        (&profile, other_endpoint.as_str(), 1, true),
        (&profile, endpoint.as_str(), 1, false),
        (&absent, endpoint.as_str(), 1, true),
    ] {
        let saved = write_registry(&root, &profile, &endpoint, enabled);
        let result = post_original_local_chat(
            &root,
            original_request(selected_profile, observed, revision, store.cancellation()),
            &budget,
            |phase| {
                assert_eq!(phase, OriginalLocalChatCheck::Live);
                Ok(())
            },
        )
        .await;
        if selected_profile == &absent {
            assert!(matches!(
                result,
                Err(OriginalLocalChatError::RegistryUnavailable)
            ));
        } else {
            assert!(matches!(
                result,
                Err(OriginalLocalChatError::Source(
                    LegacyLocalChatSourceError::SelectionChanged
                ))
            ));
        }
        assert_eq!(account.snapshot().unwrap().charged_bytes, initial);
        assert_registry_unchanged(&root, &profile, &saved);
    }
    assert!(!root.path().join(PROFILES).join(absent.as_str()).exists());

    let (registered_connection, observed_connection) = tokio::join!(
        timeout(Duration::from_millis(100), registered.accept()),
        timeout(Duration::from_millis(100), other.accept())
    );
    assert!(registered_connection.is_err());
    assert!(observed_connection.is_err());
}

#[tokio::test]
async fn changed_policy_under_the_same_selection_cannot_obtain_dispatch() {
    let temp = tempfile::tempdir().unwrap();
    let root = DataRoot::open_existing(temp.path().canonicalize().unwrap()).unwrap();
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).await.unwrap();
    let endpoint = format!("http://{}/v1", listener.local_addr().unwrap());
    let profile = ProfileId::parse("defaults").unwrap();
    let original = write_registry(&root, &profile, &endpoint, true);
    let (engine, account) = native_account();
    let store = engine
        .native_setup_store(account.begin().unwrap(), ())
        .unwrap();
    let budget = store.allocation_owners().graph().clone();
    let mut request = original_request(&profile, &endpoint, 1, store.cancellation());
    let policy =
        observe_original_local_chat_policy(&root, &profile, &request.provider, &budget).unwrap();
    assert!(policy.document().unwrap().is_empty());
    request.expected_policy = Some(&policy);

    let different_revision = LegacyLocalChatSelection {
        record_revision: request.provider.record_revision + 1,
        ..request.provider
    };
    assert!(matches!(
        revalidate_original_local_chat_policy(
            &root,
            &profile,
            &different_revision,
            &policy,
            &budget
        ),
        Err(OriginalLocalChatError::Source(
            LegacyLocalChatSourceError::SelectionChanged,
        )),
    ));

    let path = root
        .path()
        .join(PROFILES)
        .join(profile.as_str())
        .join(STORE);
    let mut changed: serde_json::Value = serde_json::from_str(&original).unwrap();
    fs::write(&path, serde_json::to_vec(&changed).unwrap()).unwrap();
    revalidate_original_local_chat_policy(&root, &profile, &request.provider, &policy, &budget)
        .unwrap();

    changed["providers"][CONNECTION]["allowed_models"] = serde_json::json!([]);
    fs::write(&path, serde_json::to_vec(&changed).unwrap()).unwrap();
    let result = post_original_local_chat(&root, request, &budget, |phase| {
        assert_eq!(phase, OriginalLocalChatCheck::Live);
        Ok(())
    })
    .await;
    assert!(matches!(
        result,
        Err(OriginalLocalChatError::Source(
            LegacyLocalChatSourceError::SelectionChanged,
        ))
    ));
    assert!(
        timeout(Duration::from_millis(100), listener.accept())
            .await
            .is_err()
    );
}
