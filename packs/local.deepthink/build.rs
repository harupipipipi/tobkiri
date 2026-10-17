use std::path::PathBuf;

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let deps = root.join("wit/deps");
    std::fs::create_dir_all(&deps).unwrap();
    for (source, target) in [
        ("../../crates/tobkiri-wit/wit/core/core.wit", "core.wit"),
        ("../shapes/harness-chat.wit", "harness-chat.wit"),
        ("../shapes/harness-agent.wit", "harness-agent.wit"),
        ("../shapes/deepthink.wit", "deepthink.wit"),
    ] {
        let source = root.join(source);
        std::fs::copy(&source, deps.join(target)).unwrap();
        println!("cargo:rerun-if-changed={}", source.display());
    }
    println!("cargo:rerun-if-changed=wit/pack.wit");
}
