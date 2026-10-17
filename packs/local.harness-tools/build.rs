use std::path::PathBuf;

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let deps = root.join("wit/deps");
    std::fs::create_dir_all(&deps).unwrap();
    for (source, name) in [
        (
            root.join("../../crates/tobkiri-wit/wit/core/core.wit"),
            "core.wit",
        ),
        (
            root.join("../shapes/harness-agent.wit"),
            "harness-agent.wit",
        ),
    ] {
        std::fs::copy(&source, deps.join(name)).unwrap();
        println!("cargo:rerun-if-changed={}", source.display());
    }
    let artifacts = root.join("../shapes/harness-tools.wit");
    std::fs::copy(&artifacts, root.join("wit/artifacts.wit")).unwrap();
    println!("cargo:rerun-if-changed={}", artifacts.display());
    println!("cargo:rerun-if-changed=wit/pack.wit");
}
