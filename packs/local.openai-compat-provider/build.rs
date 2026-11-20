use std::path::PathBuf;
fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let structured = std::env::var_os("CARGO_FEATURE_STRUCTURED_CHAT").is_some();
    let output = root.join(if structured { "structured-wit" } else { "wit" });
    let deps = output.join("deps");
    std::fs::create_dir_all(&deps).unwrap();

    let core = root.join("../../crates/tobkiri-wit/wit/core/core.wit");
    std::fs::copy(&core, deps.join("core.wit")).unwrap();
    let shape = root.join("../shapes/harness-chat.wit");
    std::fs::copy(&shape, deps.join("shape.wit")).unwrap();
    if structured {
        let shape = root.join("../shapes/harness-chat-v2/harness-chat.wit");
        std::fs::copy(&shape, deps.join("shape-v2.wit")).unwrap();
        println!("cargo:rerun-if-changed={}", shape.display());
    }
    println!("cargo:rerun-if-changed={}", core.display());
    println!("cargo:rerun-if-changed={}", shape.display());
    println!(
        "cargo:rerun-if-changed={}",
        output.join("pack.wit").display()
    );
    println!("cargo:rustc-env=TOBKIRI_GUEST_WIT={}", output.display());
}
