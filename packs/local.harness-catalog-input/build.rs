use std::path::PathBuf;

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let output = root.join("wit");
    let dependencies = output.join("deps");
    std::fs::create_dir_all(&dependencies).unwrap();

    let core = root.join("../../crates/tobkiri-wit/wit/core/core.wit");
    std::fs::copy(&core, dependencies.join("core.wit")).unwrap();

    println!("cargo:rerun-if-changed={}", core.display());
    println!("cargo:rerun-if-changed=wit/pack.wit");
}
