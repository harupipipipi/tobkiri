//! Tool contracts independent of the guest ABI. Storage adapters must commit an
//! artifact mutation and its receipt atomically, including on uncertain delivery.
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const MAX_CONTENT_BYTES: usize = 256 * 1024;
// Core's StrictV1 JSON boundary admits exact interoperable integers only.
const MAX_INTEGER: i64 = 9_007_199_254_740_991;

#[derive(Debug, Clone, Serialize)]
pub struct ToolError {
    pub code: String,
    pub message: String,
}

impl ToolError {
    pub fn new(code: &str, message: &str) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
}
type Result<T> = std::result::Result<T, ToolError>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Call {
    pub name: String,
    #[serde(default)]
    pub id: Option<String>,
    pub arguments: Value,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Artifact {
    pub path: String,
    pub content: String,
    pub deleted: bool,
}

impl Artifact {
    pub fn metadata(&self) -> Value {
        json!({"path": self.path, "exists": !self.deleted,
            "size": if self.deleted {0} else {self.content.len()},
            "digest": if self.deleted {None} else {Some(digest(self.content.as_bytes()))}})
    }
}

#[derive(Clone)]
pub struct Receipt {
    pub signature: String,
    pub result: Value,
}

/// No host path, shell or network access is part of this tool boundary.
pub trait Backend {
    fn get(&mut self, path: &str) -> Result<Option<Artifact>>;
    fn list(&mut self, prefix: &str, after: &str, limit: u32) -> Result<Vec<Value>>;
    fn receipt(&mut self, id: &str) -> Result<Option<Receipt>>;
    fn commit(
        &mut self,
        id: &str,
        signature: &str,
        artifact: Artifact,
        result: Value,
    ) -> Result<Receipt>;
    fn now_ms(&mut self) -> Result<u64>;
    fn random(&mut self, length: u32) -> Result<Vec<u8>>;
}

pub fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn invalid(message: &str) -> ToolError {
    ToolError::new("invalid", message)
}

fn args<T: DeserializeOwned>(value: &Value) -> Result<T> {
    serde_json::from_value(value.clone())
        .map_err(|_| invalid("tool arguments do not match the declared schema"))
}

/// Logical portable artifact paths, never absolute native file paths.
pub fn validate_path(path: &str) -> Result<()> {
    if path.is_empty()
        || path.len() > 240
        || path.starts_with('/')
        || path.contains(['\\', ':'])
        || path.chars().any(char::is_control)
        || path
            .split('/')
            .any(|p| p.is_empty() || p == "." || p == "..")
    {
        return Err(invalid(
            "artifact path must be a relative slash-separated logical path of 1..240 bytes",
        ));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PathArgs {
    path: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct WriteArgs {
    path: String,
    content: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ListArgs {
    #[serde(default)]
    prefix: String,
    #[serde(default)]
    after: String,
    #[serde(default = "default_limit")]
    limit: u32,
}
fn default_limit() -> u32 {
    25
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Calculation {
    operation: String,
    operands: Vec<i64>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct JsonArgs {
    text: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RandomArgs {
    length: u32,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EmptyArgs {}

/// Execute one declared tool. Reusing a mutation ID cannot repeat the effect.
pub fn invoke(backend: &mut impl Backend, input: &str) -> Value {
    let result = (|| {
        if input.len() > MAX_CONTENT_BYTES * 2 + 4096 {
            return Err(invalid("tool call exceeds input byte limit"));
        }
        let call: Call =
            serde_json::from_str(input).map_err(|_| invalid("invalid tool call envelope"))?;
        execute(backend, call)
    })();
    match result {
        Ok(result) => json!({"ok": true, "result": result}),
        Err(error) => json!({"ok": false, "error": error}),
    }
}

fn execute(backend: &mut impl Backend, call: Call) -> Result<Value> {
    match call.name.as_str() {
        "artifact.write" | "artifact.delete" => {
            let id = call
                .id
                .as_deref()
                .filter(|id| !id.is_empty() && id.len() <= 160 && !id.chars().any(char::is_control))
                .ok_or_else(|| {
                    invalid("artifact mutations require a stable call id of 1..160 bytes")
                })?;
            let artifact = if call.name == "artifact.write" {
                let data: WriteArgs = args(&call.arguments)?;
                if data.content.len() > MAX_CONTENT_BYTES {
                    return Err(invalid("artifact content exceeds 256 KiB"));
                }
                Artifact {
                    path: data.path,
                    content: data.content,
                    deleted: false,
                }
            } else {
                let data: PathArgs = args(&call.arguments)?;
                Artifact {
                    path: data.path,
                    content: String::new(),
                    deleted: true,
                }
            };
            validate_path(&artifact.path)?;
            let signature = digest(
                json!({"name":call.name,"arguments":call.arguments})
                    .to_string()
                    .as_bytes(),
            );
            let receipt = match backend.receipt(id)? {
                Some(receipt) => receipt,
                None => {
                    let result = artifact.metadata();
                    backend.commit(id, &signature, artifact, result)?
                }
            };
            if receipt.signature != signature {
                return Err(ToolError::new(
                    "conflict",
                    "call id already belongs to different arguments",
                ));
            }
            Ok(receipt.result)
        }
        "artifact.read" | "artifact.stat" => {
            let data: PathArgs = args(&call.arguments)?;
            validate_path(&data.path)?;
            let artifact = backend.get(&data.path)?.filter(|a| !a.deleted);
            match artifact {
                Some(artifact) => {
                    let mut result = artifact.metadata();
                    if call.name == "artifact.read" {
                        result["content"] = json!(artifact.content);
                    }
                    Ok(result)
                }
                None if call.name == "artifact.stat" => {
                    Ok(json!({"path":data.path,"exists":false,"size":0,"digest":null}))
                }
                None => Err(ToolError::new("not_found", "artifact does not exist")),
            }
        }
        "artifact.list" => {
            let data: ListArgs = args(&call.arguments)?;
            if !data.prefix.is_empty() {
                validate_path(data.prefix.trim_end_matches('/'))?;
            }
            if !data.after.is_empty() {
                validate_path(&data.after)?;
            }
            if !(1..=100).contains(&data.limit) {
                return Err(invalid("list limit must be 1..100"));
            }
            let artifacts = backend.list(&data.prefix, &data.after, data.limit + 1)?;
            let more = artifacts.len() > data.limit as usize;
            let items: Vec<Value> = artifacts.into_iter().take(data.limit as usize).collect();
            let next = if more {
                items.last().and_then(|v| v["path"].as_str())
            } else {
                None
            };
            Ok(json!({"items": items, "next_after": next}))
        }
        "calculator" => {
            let data: Calculation = args(&call.arguments)?;
            if !(2..=64).contains(&data.operands.len()) {
                return Err(invalid("calculator requires 2..64 integer operands"));
            }
            if data
                .operands
                .iter()
                .any(|v| !(-MAX_INTEGER..=MAX_INTEGER).contains(v))
            {
                return Err(invalid(
                    "operands must be within the StrictV1 safe integer range",
                ));
            }
            let mut operands = data.operands.into_iter();
            let mut result = operands.next().unwrap();
            for right in operands {
                result = match data.operation.as_str() {
                    "add" => result.checked_add(right),
                    "subtract" => result.checked_sub(right),
                    "multiply" => result.checked_mul(right),
                    "divide" => {
                        if result.checked_rem(right) != Some(0) {
                            return Err(invalid(
                                "division must be exact and divisor must be nonzero",
                            ));
                        }
                        result.checked_div(right)
                    }
                    _ => return Err(invalid("unsupported calculator operation")),
                }
                .ok_or_else(|| invalid("integer result exceeds signed 64-bit range"))?;
                if !(-MAX_INTEGER..=MAX_INTEGER).contains(&result) {
                    return Err(invalid("result exceeds the StrictV1 safe integer range"));
                }
            }
            Ok(json!({"value":result}))
        }
        "json.validate" => {
            let data: JsonArgs = args(&call.arguments)?;
            if data.text.len() > MAX_CONTENT_BYTES {
                return Err(invalid("JSON exceeds 256 KiB"));
            }
            match serde_json::from_str::<Value>(&data.text) {
                Ok(value) => Ok(json!({"valid":true,"type": match value {
                    Value::Null => "null", Value::Bool(_) => "boolean", Value::Number(_) => "number",
                    Value::String(_) => "string", Value::Array(_) => "array", Value::Object(_) => "object",
                }})),
                Err(error) => {
                    Ok(json!({"valid":false,"line":error.line(),"column":error.column()}))
                }
            }
        }
        "clock.now" => {
            let _: EmptyArgs = args(&call.arguments)?;
            Ok(json!({"unix_ms": backend.now_ms()?}))
        }
        "random.bytes" => {
            let data: RandomArgs = args(&call.arguments)?;
            if !(1..=256).contains(&data.length) {
                return Err(invalid("random length must be 1..256"));
            }
            let bytes = backend.random(data.length)?;
            if bytes.len() != data.length as usize {
                return Err(ToolError::new(
                    "backend",
                    "random source returned the wrong length",
                ));
            }
            Ok(
                json!({"hex": bytes.iter().map(|v| format!("{v:02x}")).collect::<String>(),"length":data.length}),
            )
        }
        _ => Err(ToolError::new(
            "not_found",
            "tool is not in the declared inventory",
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    #[derive(Default)]
    struct Memory {
        artifacts: BTreeMap<String, Artifact>,
        receipts: BTreeMap<String, Receipt>,
        mutations: usize,
        fail_after_commit: bool,
    }
    impl Backend for Memory {
        fn get(&mut self, path: &str) -> Result<Option<Artifact>> {
            Ok(self.artifacts.get(path).cloned())
        }
        fn list(&mut self, prefix: &str, after: &str, limit: u32) -> Result<Vec<Value>> {
            Ok(self
                .artifacts
                .values()
                .filter(|a| !a.deleted && a.path.starts_with(prefix) && a.path.as_str() > after)
                .take(limit as usize)
                .map(Artifact::metadata)
                .collect())
        }
        fn receipt(&mut self, id: &str) -> Result<Option<Receipt>> {
            Ok(self.receipts.get(id).cloned())
        }
        fn commit(
            &mut self,
            id: &str,
            signature: &str,
            artifact: Artifact,
            result: Value,
        ) -> Result<Receipt> {
            let receipt = Receipt {
                signature: signature.into(),
                result,
            };
            self.artifacts.insert(artifact.path.clone(), artifact);
            self.receipts.insert(id.into(), receipt.clone());
            self.mutations += 1;
            if self.fail_after_commit {
                self.fail_after_commit = false;
                return Err(ToolError::new("backend", "delivery interrupted"));
            }
            Ok(receipt)
        }
        fn now_ms(&mut self) -> Result<u64> {
            Ok(1720000000123)
        }
        fn random(&mut self, length: u32) -> Result<Vec<u8>> {
            Ok(vec![0xab; length as usize])
        }
    }
    fn run(backend: &mut Memory, name: &str, args: Value, id: &str) -> Value {
        invoke(
            backend,
            &json!({"name":name,"arguments":args,"id":id}).to_string(),
        )
    }
    #[test]
    fn all_nine_tools_execute_and_artifact_digest_matches_utf8_bytes() {
        let mut store = Memory::default();
        let write = run(
            &mut store,
            "artifact.write",
            json!({"path":"goal/定義.txt","content":"猫"}),
            "one",
        );
        assert_eq!(write["result"]["size"], 3);
        assert_eq!(write["result"]["digest"], digest("猫".as_bytes()));
        assert_eq!(
            run(
                &mut store,
                "artifact.read",
                json!({"path":"goal/定義.txt"}),
                "r"
            )["result"]["content"],
            "猫"
        );
        assert_eq!(
            run(
                &mut store,
                "artifact.stat",
                json!({"path":"goal/定義.txt"}),
                "s"
            )["result"]["exists"],
            true
        );
        assert_eq!(
            run(&mut store, "artifact.list", json!({}), "l")["result"]["items"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            run(
                &mut store,
                "calculator",
                json!({"operation":"multiply","operands":[23,17]}),
                "c"
            )["result"]["value"],
            391
        );
        assert_eq!(
            run(
                &mut store,
                "json.validate",
                json!({"text":"{\"a\":1}"}),
                "j"
            )["result"]["valid"],
            true
        );
        assert_eq!(
            run(&mut store, "clock.now", json!({}), "t")["result"]["unix_ms"],
            1720000000123_u64
        );
        assert_eq!(
            run(&mut store, "random.bytes", json!({"length":2}), "b")["result"]["hex"],
            "abab"
        );
        assert_eq!(
            run(
                &mut store,
                "artifact.delete",
                json!({"path":"goal/定義.txt"}),
                "d"
            )["ok"],
            true
        );
        assert_eq!(
            run(
                &mut store,
                "artifact.stat",
                json!({"path":"goal/定義.txt"}),
                "s2"
            )["result"]["exists"],
            false
        );
        assert_eq!(
            run(
                &mut store,
                "artifact.read",
                json!({"path":"goal/定義.txt"}),
                "r2"
            )["error"]["code"],
            "not_found"
        );
    }
    #[test]
    fn interrupted_delivery_and_old_mutation_replay_do_not_overwrite_later_work() {
        let mut store = Memory {
            fail_after_commit: true,
            ..Memory::default()
        };
        let first = json!({"path":"goal.txt","content":"first"});
        assert_eq!(
            run(&mut store, "artifact.write", first.clone(), "one")["ok"],
            false
        );
        assert_eq!(
            run(&mut store, "artifact.write", first.clone(), "one")["ok"],
            true
        );
        run(
            &mut store,
            "artifact.write",
            json!({"path":"goal.txt","content":"second"}),
            "two",
        );
        assert_eq!(run(&mut store, "artifact.write", first, "one")["ok"], true);
        assert_eq!(store.mutations, 2);
        assert_eq!(store.get("goal.txt").unwrap().unwrap().content, "second");
        assert_eq!(
            run(
                &mut store,
                "artifact.delete",
                json!({"path":"goal.txt"}),
                "one"
            )["error"]["code"],
            "conflict"
        );
    }
    #[test]
    fn paths_cannot_escape_the_logical_artifact_namespace() {
        for path in [
            "", "/a", "../a", "a/../b", "a//b", "a/", "C:/a", "a\\b", "a\0b", "a\nb",
        ] {
            assert!(validate_path(path).is_err(), "{path:?}");
        }
        for path in ["goal.json", "goal/定義.md", "a%_b"] {
            assert!(validate_path(path).is_ok());
        }
    }
    #[test]
    fn limits_unknown_fields_and_missing_mutation_ids_fail_before_effects() {
        let mut store = Memory::default();
        for (name, args) in [
            ("artifact.list", json!({"limit":0})),
            ("artifact.list", json!({"limit":101})),
            ("clock.now", json!({"shell":"whoami"})),
            ("random.bytes", json!({"length":257})),
            ("random.bytes", json!({"length":0})),
            ("artifact.read", json!({"path":"a","unknown":true})),
            (
                "artifact.write",
                json!({"path":"a","content":"x".repeat(MAX_CONTENT_BYTES+1)}),
            ),
            ("shell", json!({"command":"whoami"})),
        ] {
            assert_eq!(run(&mut store, name, args, "id")["ok"], false);
        }
        assert_eq!(
            invoke(
                &mut store,
                r#"{"name":"artifact.write","arguments":{"path":"a","content":"x"}}"#
            )["ok"],
            false
        );
        assert_eq!(store.mutations, 0);
    }
    #[test]
    fn integer_arithmetic_rejects_loss_and_overflow() {
        let mut store = Memory::default();
        for (op, operands) in [
            ("add", vec![i64::MAX, 1]),
            ("multiply", vec![i64::MIN, -1]),
            ("divide", vec![1, 0]),
            ("divide", vec![5, 2]),
            ("divide", vec![i64::MIN, -1]),
            ("eval", vec![2, 3]),
        ] {
            assert_eq!(
                run(
                    &mut store,
                    "calculator",
                    json!({"operation":op,"operands":operands}),
                    "id"
                )["ok"],
                false
            );
        }
        assert_eq!(
            run(
                &mut store,
                "calculator",
                json!({"operation":"divide","operands":[30,3,2]}),
                "id"
            )["result"]["value"],
            5
        );
        assert_eq!(
            run(
                &mut store,
                "calculator",
                json!({"operation":"add","operands":[MAX_INTEGER,1]}),
                "id"
            )["ok"],
            false
        );
        assert_eq!(
            run(
                &mut store,
                "calculator",
                json!({"operation":"subtract","operands":[-MAX_INTEGER,1]}),
                "id"
            )["ok"],
            false
        );
        assert_eq!(
            run(
                &mut store,
                "calculator",
                json!({"operation":"add","operands":[MAX_INTEGER,0]}),
                "id"
            )["result"]["value"],
            MAX_INTEGER
        );
    }
    #[test]
    fn list_is_paginated_and_deletions_stay_deleted_when_replayed() {
        let mut store = Memory::default();
        for (path, id) in [("a/1", "one"), ("a/2", "two"), ("b/1", "three")] {
            run(
                &mut store,
                "artifact.write",
                json!({"path":path,"content":"x"}),
                id,
            );
        }
        let first = run(
            &mut store,
            "artifact.list",
            json!({"prefix":"a/","limit":1}),
            "l",
        );
        assert_eq!(first["result"]["next_after"], "a/1");
        let second = run(
            &mut store,
            "artifact.list",
            json!({"prefix":"a/","after":"a/1","limit":1}),
            "l2",
        );
        assert_eq!(second["result"]["items"][0]["path"], "a/2");
        assert!(second["result"]["next_after"].is_null());
        run(&mut store, "artifact.delete", json!({"path":"a/1"}), "d");
        run(
            &mut store,
            "artifact.write",
            json!({"path":"a/1","content":"x"}),
            "one",
        );
        assert_eq!(store.get("a/1").unwrap().unwrap().deleted, true);
    }
    #[test]
    fn malformed_json_reports_position_without_echoing_input() {
        let mut store = Memory::default();
        let invalid = run(
            &mut store,
            "json.validate",
            json!({"text":"{\"secret-marker\":"}),
            "j",
        );
        assert_eq!(invalid["result"]["valid"], false);
        assert!(!invalid.to_string().contains("secret-marker"));
    }
}
