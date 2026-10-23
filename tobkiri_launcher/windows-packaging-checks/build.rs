use std::{env, fs, path::PathBuf};
fn main() {
    let root = PathBuf::from(
        env::var_os("TOBKIRI_REPO_ROOT").expect("set TOBKIRI_REPO_ROOT to the checkout"),
    );
    let manifest = root
        .join("tobkiri_launcher/src-tauri")
        .canonicalize()
        .unwrap();
    println!("cargo:rerun-if-env-changed=TOBKIRI_REPO_ROOT");
    println!("cargo:rustc-env=CARGO_MANIFEST_DIR={}", manifest.display());
    let path = manifest.join("build.rs");
    println!("cargo:rerun-if-changed={}", path.display());
    fs::write(
        PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("modules.rs"),
        format!(
            "#[path = {:?}]\nmod launcher_build;\n",
            path.to_string_lossy()
        ),
    )
    .unwrap();
    let filesystem = manifest.join("src/windows_packaging_fs.rs");
    println!("cargo:rerun-if-changed={}", filesystem.display());
    fs::write(
        PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("filesystem.rs"),
        format!(
            "#[path = {:?}]\nmod windows_packaging_fs;\n",
            filesystem.to_string_lossy()
        ),
    )
    .unwrap();
}
