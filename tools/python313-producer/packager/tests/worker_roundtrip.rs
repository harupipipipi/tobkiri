//! Executes only the locally built compression worker on synthetic byte fixtures.
use std::io::Write;
use std::process::{Command, Stdio};

fn append_custom(module: &mut Vec<u8>, name: &str) {
    assert!(name.len() < 127);
    module.extend_from_slice(&[0, (name.len() + 1) as u8, name.len() as u8]);
    module.extend_from_slice(name.as_bytes());
}

#[test]
fn actual_worker_zstd_roundtrip_and_strip_whitelist() {
    let base = b"\0asm\x01\0\0\0\x01\x04\x01\x60\x00\x00\x03\x02\x01\x00\x0a\x04\x01\x02\x00\x0b";
    let mut input = base.to_vec();
    let mut stripped = base.to_vec();
    for name in ["name", "component-type:test", "dylink.0", "producers", ".debug_info"] {
        append_custom(&mut input, name);
        if matches!(name, "name" | "component-type:test" | "dylink.0") {
            append_custom(&mut stripped, name);
        }
    }
    for (mode, expected) in [("wasm-raw", &input), ("wasm-strip", &stripped)] {
        let mut child = Command::new(env!("CARGO_BIN_EXE_tobkiri-python313-packager"))
            .arg(mode)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn().unwrap();
        child.stdin.take().unwrap().write_all(&input).unwrap();
        let output = child.wait_with_output().unwrap();
        assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
        assert!(output.stdout.starts_with(&[0x28, 0xb5, 0x2f, 0xfd]));
        let decoded = zstd::decode_all(output.stdout.as_slice()).unwrap();
        assert_eq!(&decoded, expected);
    }
}
