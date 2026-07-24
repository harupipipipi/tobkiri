use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

const APP_SOURCE_DIR: &str = "tobkiri_runtime";
const GENERATED_RESOURCE_DIRS: &[&str] = &[
    "core_runtime/core_pack/core_control_panel/web",
    "ecosystem/defaultspack/ui",
    "bundled",
];

fn main() {
    println!("cargo:rerun-if-changed=splash/index.html");
    println!("cargo:rerun-if-changed=splash/tobkiri_launcher_startup_blade_cut.svg");
    println!("cargo:rerun-if-changed=src/lib.rs");
    println!("cargo:rerun-if-changed=../../pack-shell/Cargo.toml");
    println!("cargo:rerun-if-changed=../../pack-shell/src");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/app.py");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/core_runtime");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/ecosystem");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/flows");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/lang");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/rumi_setup");
    println!("cargo:rerun-if-changed=../../tobkiri_runtime/requirements.txt");
    println!("cargo:rerun-if-changed=bundled");

    warn_legacy_defaultspack_app_bundle();
    stage_runtime_bundle().expect("failed to stage runtime bundle");
    tauri_build::build()
}

fn warn_legacy_defaultspack_app_bundle() {
    let Some(home) = std::env::var_os("HOME").map(PathBuf::from) else {
        return;
    };
    let legacy_app = home.join("Applications").join("Rumi_Defaultspack.app");
    if !legacy_app.exists() {
        return;
    }

    let launch = fs::read_to_string(legacy_app.join("Contents").join("MacOS").join("launch"))
        .unwrap_or_default();
    let missing_markers = [
        "--api-token",
        "--port",
        "RUMI_LOG_DIR",
        "RUMI_DEFAULTSPACK_OPEN_BROWSER",
    ]
    .into_iter()
    .filter(|marker| !launch.contains(marker))
    .collect::<Vec<_>>();
    if missing_markers.is_empty() {
        println!(
            "cargo:warning=legacy underscore-named Defaultspack app bundle detected at {}; re-register Defaultspack from Rumi Viewer to clean it up",
            legacy_app.display()
        );
    } else {
        println!(
            "cargo:warning=legacy Defaultspack app bundle detected at {}; missing launch markers: {}; re-register Defaultspack from Rumi Viewer or remove the legacy bundle",
            legacy_app.display(),
            missing_markers.join(", ")
        );
    }
}

fn stage_runtime_bundle() -> io::Result<()> {
    let project_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = project_dir
        .parent()
        .and_then(Path::parent)
        .expect("src-tauri should live under tobkiri_launcher/");
    let runtime_root = repo_root.join(APP_SOURCE_DIR);
    let staged_root = project_dir.join("gen").join("app");

    reset_dir(&staged_root)?;
    if !copy_tracked_runtime_tree(repo_root, &staged_root)? {
        copy_runtime_tree(&runtime_root, &staged_root, &runtime_root)?;
    }
    copy_generated_resource_dirs(&runtime_root, &staged_root)?;
    stage_setup_brand_icon(&repo_root, &staged_root)?;

    let bundled_src = project_dir.join("bundled");
    if bundled_src.exists() {
        copy_dir_recursive(&bundled_src, &staged_root.join("bundled"))?;
    }

    stage_pack_shell(&repo_root, &staged_root)?;

    Ok(())
}

fn stage_setup_brand_icon(repo_root: &Path, staged_root: &Path) -> io::Result<()> {
    let icon_source = repo_root
        .join("tobkiri_launcher")
        .join("assets")
        .join("app-icon")
        .join("tobkiri-launcher-icon.png");
    let icon_target = staged_root
        .join("core_runtime")
        .join("core_pack")
        .join("core_setup")
        .join("web")
        .join("assets")
        .join("tobkiri-launcher-icon.png");
    copy_file(&icon_source, &icon_target).map(|_| ())
}

fn stage_pack_shell(repo_root: &Path, staged_root: &Path) -> io::Result<()> {
    let Some(pack_shell) = ensure_pack_shell_binary(repo_root)? else {
        return Ok(());
    };
    let bundled_dir = staged_root.join("bundled");
    fs::create_dir_all(&bundled_dir)?;
    copy_file(&pack_shell, &bundled_dir.join(pack_shell_binary_name()))?;
    Ok(())
}

fn ensure_pack_shell_binary(repo_root: &Path) -> io::Result<Option<PathBuf>> {
    if let Some(pack_shell) = find_pack_shell_binary(repo_root) {
        return Ok(Some(pack_shell));
    }

    let manifest = repo_root.join("pack-shell").join("Cargo.toml");
    if !manifest.is_file() {
        return Ok(None);
    }

    let cargo = std::env::var_os("CARGO").unwrap_or_else(|| "cargo".into());
    let mut command = Command::new(cargo);
    command
        .args(["build", "--locked", "--manifest-path"])
        .arg(&manifest);

    if std::env::var("PROFILE").as_deref() == Ok("release") {
        command.arg("--release");
    }

    let output = command.output()?;
    if output.status.success() {
        return Ok(find_pack_shell_binary(repo_root));
    }

    Err(io::Error::other(format!(
        "failed to build pack-shell with status {}\nstdout:\n{}\nstderr:\n{}",
        output.status,
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )))
}

fn find_pack_shell_binary(repo_root: &Path) -> Option<PathBuf> {
    let binary_name = pack_shell_binary_name();
    let mut candidates = Vec::new();
    if let Ok(target) = std::env::var("TARGET") {
        candidates.push(
            repo_root
                .join("target")
                .join(&target)
                .join("release")
                .join(binary_name),
        );
        candidates.push(
            repo_root
                .join("pack-shell")
                .join("target")
                .join(target)
                .join("release")
                .join(binary_name),
        );
    }
    candidates.extend([
        repo_root.join("target").join("release").join(binary_name),
        repo_root.join("target").join("debug").join(binary_name),
        repo_root
            .join("pack-shell")
            .join("target")
            .join("release")
            .join(binary_name),
        repo_root
            .join("pack-shell")
            .join("target")
            .join("debug")
            .join(binary_name),
    ]);
    candidates.into_iter().find(|candidate| candidate.is_file())
}

fn pack_shell_binary_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "pack-shell.exe"
    } else {
        "pack-shell"
    }
}

fn reset_dir(path: &Path) -> io::Result<()> {
    if path.exists() {
        clear_dir(path)?;
    } else {
        fs::create_dir_all(path)?;
    }
    Ok(())
}

fn clear_dir(path: &Path) -> io::Result<()> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        let entry_path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            clear_dir(&entry_path)?;
            fs::remove_dir(&entry_path)?;
        } else {
            fs::remove_file(&entry_path)?;
        }
    }
    Ok(())
}

fn copy_file(src: &Path, dst: &Path) -> io::Result<u64> {
    if let Some(parent) = dst.parent() {
        fs::create_dir_all(parent)?;
    }
    let bytes = fs::copy(src, dst)?;
    if let Ok(permissions) = fs::metadata(src).map(|metadata| metadata.permissions()) {
        let _ = fs::set_permissions(dst, permissions);
    }
    Ok(bytes)
}

fn copy_tracked_runtime_tree(repo_root: &Path, staged_root: &Path) -> io::Result<bool> {
    let output = match Command::new("git")
        .args(["ls-files", "-z", "--", APP_SOURCE_DIR])
        .current_dir(repo_root)
        .output()
    {
        Ok(output) => output,
        Err(_) => return Ok(false),
    };

    if !output.status.success() {
        return Ok(false);
    }

    let source_prefix = format!("{APP_SOURCE_DIR}/");
    for rel in output.stdout.split(|byte| *byte == 0) {
        if rel.is_empty() {
            continue;
        }
        let rel = String::from_utf8_lossy(rel);
        let Some(rel_under_app) = rel.strip_prefix(&source_prefix) else {
            continue;
        };
        let rel_path = Path::new(rel_under_app);
        if should_skip(rel_path, false) {
            continue;
        }
        let source_path = repo_root.join(rel.as_ref());
        if !source_path.is_file() {
            continue;
        }
        copy_file(&source_path, &staged_root.join(rel_path))?;
    }

    Ok(true)
}

fn copy_generated_resource_dirs(runtime_root: &Path, staged_root: &Path) -> io::Result<()> {
    for rel_dir in GENERATED_RESOURCE_DIRS {
        let source_dir = runtime_root.join(rel_dir);
        if !source_dir.exists() {
            continue;
        }
        copy_dir_recursive_filtered(&source_dir, &staged_root.join(rel_dir), runtime_root)?;
    }
    Ok(())
}

fn copy_runtime_tree(src: &Path, dst: &Path, runtime_root: &Path) -> io::Result<()> {
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let source_path = entry.path();
        let file_type = entry.file_type()?;
        let target_path = dst.join(entry.file_name());
        let relative = source_path
            .strip_prefix(runtime_root)
            .unwrap_or(&source_path);

        if should_skip(relative, file_type.is_dir()) {
            continue;
        }

        if file_type.is_dir() {
            copy_dir_recursive_filtered(&source_path, &target_path, runtime_root)?;
        } else if file_type.is_file() {
            copy_file(&source_path, &target_path)?;
        }
    }

    Ok(())
}

fn copy_dir_recursive(src: &Path, dst: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let source_path = entry.path();
        let target_path = dst.join(entry.file_name());
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            copy_dir_recursive(&source_path, &target_path)?;
        } else if file_type.is_file() {
            copy_file(&source_path, &target_path)?;
        }
    }
    Ok(())
}

fn copy_dir_recursive_filtered(src: &Path, dst: &Path, runtime_root: &Path) -> io::Result<()> {
    fs::create_dir_all(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let source_path = entry.path();
        let target_path = dst.join(entry.file_name());
        let file_type = entry.file_type()?;
        let relative = source_path
            .strip_prefix(runtime_root)
            .unwrap_or(&source_path);

        if should_skip(relative, file_type.is_dir()) {
            continue;
        }

        if file_type.is_dir() {
            copy_dir_recursive_filtered(&source_path, &target_path, runtime_root)?;
        } else if file_type.is_file() {
            copy_file(&source_path, &target_path)?;
        }
    }
    Ok(())
}

fn should_skip(relative: &Path, is_dir: bool) -> bool {
    let Some(first) = relative.components().next().map(|c| c.as_os_str()) else {
        return false;
    };

    let first = first.to_str();
    if matches!(
        first,
        Some(".env")
            | Some(".env.local")
            | Some(".backups")
            | Some(".backup_dead_code_removal")
            | Some("chats")
            | Some("tenpu")
            | Some("tests")
            | Some("user_data")
            | Some("userdata")
            | Some("venv")
    ) {
        return true;
    }

    if matches!(
        first,
        Some(".git")
            | Some(".mypy_cache")
            | Some(".pytest_cache")
            | Some(".ruff_cache")
            | Some(".rumi_snapshots")
            | Some(".venv")
            | Some("docs")
    ) {
        return true;
    }

    if relative.components().any(|component| {
        matches!(
            component.as_os_str().to_str(),
            Some("__pycache__")
                | Some(".pytest_cache")
                | Some(".ruff_cache")
                | Some(".rumi_snapshots")
                | Some(".venv")
                | Some("node_modules")
                | Some("target")
                | Some("user_data")
                | Some("userdata")
        )
    }) {
        return true;
    }

    if !is_dir {
        if relative.file_name().and_then(|name| name.to_str()) == Some(".DS_Store") {
            return true;
        }
        if matches!(
            relative.extension().and_then(|ext| ext.to_str()),
            Some("bak") | Some("pyc") | Some("pyo") | Some("zip")
        ) {
            return true;
        }
    }

    if first == Some("frontend") {
        let second = relative.components().nth(1).map(|c| c.as_os_str());
        if matches!(
            second.and_then(|part| part.to_str()),
            Some("node_modules") | Some(".vite-temp")
        ) {
            return true;
        }

        if !is_dir
            && matches!(
                relative.extension().and_then(|ext| ext.to_str()),
                Some("tsbuildinfo")
            )
        {
            return true;
        }
    }

    false
}
