use std::{env, fs, path::PathBuf};

fn main() {
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let discovery = env::var_os("CARGO_FEATURE_DISCOVERY").is_some();
    let (variant, world) = if discovery {
        ("discovery", "wit/pack-discovery.wit")
    } else {
        ("legacy", "wit/pack.wit")
    };
    // Keep the host-facing WIT separate from Cargo's generated guest world.
    let guest = root.join("guest-wit").join(variant);
    fs::create_dir_all(guest.join("deps")).unwrap();
    let mut files = vec![
        (world, "pack.wit"),
        (
            "../../crates/tobkiri-wit/wit/core/core.wit",
            "deps/core.wit",
        ),
        ("../shapes/pi-adapter.wit", "deps/pi-adapter.wit"),
        ("../shapes/harness-agent.wit", "deps/harness-agent.wit"),
    ];
    if discovery {
        files.extend([
            (
                "../shapes/pi-adapter-v1-1/pi-adapter.wit",
                "deps/pi-adapter-v1-1.wit",
            ),
            (
                "../shapes/provider-discovery-v1-1/harness-agent.wit",
                "deps/harness-agent-v1-1.wit",
            ),
            (
                "../shapes/provider-discovery-v1-1/harness-chat.wit",
                "deps/harness-chat-v1-1.wit",
            ),
        ]);
    } else {
        files.push(("../shapes/harness-chat.wit", "deps/harness-chat.wit"));
    }
    for (source, destination) in files {
        fs::copy(root.join(source), guest.join(destination)).unwrap();
        println!("cargo:rerun-if-changed={source}");
    }
}
