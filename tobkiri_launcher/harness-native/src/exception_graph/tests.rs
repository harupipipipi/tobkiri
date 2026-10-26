use super::*;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};

#[derive(Default)]
struct Budget {
    bytes: AtomicU64,
    refused: AtomicBool,
}
impl AllocationBudget for Budget {
    fn reserve(&self, request: AllocationRequest) -> std::result::Result<(), AllocationError> {
        if self.refused.load(Ordering::SeqCst) {
            return Err(AllocationError::Limit);
        }
        self.bytes.fetch_add(request.bytes, Ordering::SeqCst);
        Ok(())
    }
    fn release(&self, bytes: u64) {
        self.bytes.fetch_sub(bytes, Ordering::SeqCst);
    }
}
fn node(kind: &str, args: Value, fields: Value) -> Value {
    json!({"kind":kind,"args":args,"fields":fields,"cause":null,"context":null,"suppress_context":false})
}
fn frame(nodes: Value) -> Value {
    json!({"format":FORMAT,"graph_id":7,"root":0,"nodes":nodes})
}
fn decode(
    value: &Value,
    code: &str,
    message: &str,
    b: &Arc<dyn AllocationBudget>,
) -> Result<NativeErrorGraph> {
    NativeErrorGraph::decode(
        CoreBuffer::copy_from(code.as_bytes(), b).unwrap(),
        CoreBuffer::copy_from(message.as_bytes(), b).unwrap(),
        CoreBuffer::copy_from(&serde_json::to_vec(value).unwrap(), b).unwrap(),
        b,
    )
}

#[test]
fn original_formatted_schema_args_and_diagnostics_are_preserved_without_reformatting() {
    let budget = Arc::new(Budget::default());
    let b: Arc<dyn AllocationBudget> = budget.clone();
    let original = "Profile failed: $.base: missing; $.packs: invalid";
    let value = frame(json!([node(
        "schema-validation-error",
        json!([original]),
        json!({"diagnostics":["$.base: missing","$.packs: invalid"]})
    )]));
    let graph = decode(&value, "schema-validation-error", original, &b).unwrap();
    assert_eq!(graph.root().kind(), NativeErrorKind::SchemaValidationError);
    assert_eq!(
        graph.root().args().get_index(0).unwrap().as_str(),
        Some(original)
    );
    assert_eq!(
        graph
            .root()
            .fields()
            .get_key("diagnostics")
            .unwrap()
            .array()
            .unwrap()
            .len(),
        2
    );
    let retained = graph.root().args();
    drop(graph);
    assert!(budget.bytes.load(Ordering::SeqCst) > 0);
    drop(retained);
    assert_eq!(budget.bytes.load(Ordering::SeqCst), 0);
}

#[test]
fn shared_cyclic_cause_and_context_edges_are_distinct_from_suppression() {
    let b: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let mut first = node("value-error", json!(["first"]), json!({}));
    first["cause"] = json!(1);
    first["context"] = json!(1);
    first["suppress_context"] = json!(true);
    let mut second = node("runtime-error", json!(["second"]), json!({}));
    second["context"] = json!(0);
    let graph = decode(&frame(json!([first, second])), "value-error", "first", &b).unwrap();
    assert_eq!(graph.graph_id(), 7);
    assert_eq!(graph.root().cause(), Some(1));
    assert_eq!(graph.root().context(), Some(1));
    assert!(graph.root().suppress_context());
    assert_eq!(graph.node(1).unwrap().context(), Some(0));
    assert_eq!(graph.node(1).unwrap().cause(), None);
    assert!(!graph.node(1).unwrap().suppress_context());
    assert!(graph.node(2).is_none());
}

#[test]
fn json_positions_count_unicode_characters_and_preserve_the_original_message() {
    let b: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let message = "Expected value: line 2 column 2 (char 3)";
    let mut value = frame(json!([node(
        "json-decode-error",
        json!([message]),
        json!({"msg":"Expected value","doc":"あ\n🚀?","pos":3,"lineno":2,"colno":2})
    )]));
    assert!(decode(&value, "json-decode-error", message, &b).is_ok());
    value["nodes"][0]["fields"]["pos"] = json!(7);
    assert!(decode(&value, "json-decode-error", message, &b).is_err());
    value["nodes"][0]["fields"]["pos"] = json!(3);
    value["nodes"][0]["fields"]["colno"] = json!(5);
    assert!(decode(&value, "json-decode-error", message, &b).is_err());
}

#[test]
fn only_path_free_two_argument_os_errors_have_a_portable_representation() {
    let b: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let mut value = frame(json!([node(
        "file-not-found-error",
        json!([2, "No such file or directory"]),
        json!({"errno":2,"strerror":"No such file or directory","filename":null,"filename2":null})
    )]));
    let message = "[Errno 2] No such file or directory";
    assert!(decode(&value, "file-not-found-error", message, &b).is_ok());
    value["nodes"][0]["fields"]["filename"] = json!("/private/native/root");
    assert!(matches!(
        decode(&value, "file-not-found-error", message, &b),
        Err(NativeErrorGraphError::Unsupported)
    ));
    value["nodes"][0]["fields"]["filename"] = Value::Null;
    value["nodes"][0]["fields"]["winerror"] = json!(2);
    assert!(decode(&value, "file-not-found-error", message, &b).is_err());
}

#[test]
fn unknown_classes_wrong_codes_extra_fields_and_boolean_indices_never_become_domain_errors() {
    let b: Arc<dyn AllocationBudget> = Arc::new(Budget::default());
    let original = frame(json!([node(
        "invalid-artifact-error",
        json!(["bad artifact"]),
        json!({})
    )]));
    let graph = decode(&original, "invalid-artifact-error", "bad artifact", &b).unwrap();
    assert_eq!(graph.root().kind(), NativeErrorKind::InvalidArtifactError);
    assert!(decode(&original, "value-error", "bad artifact", &b).is_err());
    for (key, value) in [
        ("kind", json!("arbitrary.import.Error")),
        ("cause", json!(true)),
        ("context", json!(999)),
        ("suppress_context", json!(0)),
    ] {
        let mut changed = original.clone();
        changed["nodes"][0][key] = value;
        assert!(decode(&changed, "invalid-artifact-error", "bad artifact", &b).is_err());
    }
    let mut changed = original.clone();
    changed["ignored"] = json!(true);
    assert!(decode(&changed, "invalid-artifact-error", "bad artifact", &b).is_err());
    changed = original;
    changed["nodes"][0]["fields"]["arbitrary"] = json!("extra");
    assert!(decode(&changed, "invalid-artifact-error", "bad artifact", &b).is_err());
}

#[test]
fn original_owner_refusal_is_not_converted_to_a_supported_exception() {
    let concrete = Arc::new(Budget::default());
    let b: Arc<dyn AllocationBudget> = concrete.clone();
    let code = CoreBuffer::copy_from(b"value-error", &b).unwrap();
    let message = CoreBuffer::copy_from(b"original", &b).unwrap();
    let details = CoreBuffer::copy_from(
        &serde_json::to_vec(&frame(json!([node(
            "value-error",
            json!(["original"]),
            json!({})
        )])))
        .unwrap(),
        &b,
    )
    .unwrap();
    concrete.refused.store(true, Ordering::SeqCst);
    assert!(matches!(
        NativeErrorGraph::decode(code, message, details, &b),
        Err(NativeErrorGraphError::Allocation(AllocationError::Limit))
    ));
    assert_eq!(concrete.bytes.load(Ordering::SeqCst), 0);
}

#[test]
fn all_three_original_buffers_share_one_envelope_and_release_on_rejection() {
    let concrete = Arc::new(Budget::default());
    let b: Arc<dyn AllocationBudget> = concrete.clone();
    let code = CoreBuffer::copy_from(b"value-error", &b).unwrap();
    let message = CoreBuffer::write_exact(MAX_BYTES, &b, |bytes| {
        bytes.fill(b'x');
        Ok(())
    })
    .unwrap();
    let details = CoreBuffer::copy_from(b"{}", &b).unwrap();
    assert!(matches!(
        NativeErrorGraph::decode(code, message, details, &b),
        Err(NativeErrorGraphError::Envelope)
    ));
    assert_eq!(concrete.bytes.load(Ordering::SeqCst), 0);
}
