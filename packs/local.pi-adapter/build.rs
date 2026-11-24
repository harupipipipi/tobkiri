use std::path::PathBuf;
fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let output = root.join("wit");
    let deps = output.join("deps");
    std::fs::create_dir_all(&deps).unwrap();
    // A WIT directory is one package. The published shape and compilation
    // world belong to distinct packages, so compile the world separately.
    let guest = root.join("guest-wit");
    let guest_deps = guest.join("deps");
    std::fs::create_dir_all(&guest_deps).unwrap();
    std::fs::copy(output.join("pack.wit"), guest.join("pack.wit")).unwrap();

    let core = root.join("../../crates/tobkiri-wit/wit/core/core.wit");
    std::fs::copy(&core, deps.join("core.wit")).unwrap();
    std::fs::copy(&core, guest_deps.join("core.wit")).unwrap();
    let shape = root.join("../shapes/pi-adapter.wit");
    std::fs::copy(&shape, output.join("pi-adapter.wit")).unwrap();
    std::fs::copy(&shape, guest_deps.join("pi-adapter.wit")).unwrap();
    for name in ["harness-chat", "harness-agent"] {
        let source = root.join(format!("../shapes/{name}.wit"));
        std::fs::copy(&source, deps.join(format!("{name}.wit"))).unwrap();
        std::fs::copy(&source, guest_deps.join(format!("{name}.wit"))).unwrap();
        println!("cargo:rerun-if-changed={}", source.display());
    }
    let old = deps.join("shape.wit");
    if old.exists() {
        std::fs::remove_file(old).unwrap();
    }
    println!("cargo:rerun-if-changed={}", core.display());
    println!("cargo:rerun-if-changed={}", shape.display());
    println!("cargo:rerun-if-changed=wit/pack.wit");
    println!("cargo:rustc-env=TOBKIRI_GUEST_WIT={}", output.display());
}
