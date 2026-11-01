use clap::{Parser, Subcommand};
use std::path::PathBuf;

/// In-process Tobkiri host and Pack authoring commands.
#[derive(Parser)]
#[command(name = "tobkiri", version, about)]
pub struct Args {
    /// Fail whenever a trusted approval would be required.
    #[arg(long, global = true)]
    pub non_interactive: bool,
    /// Isolated test data root (debug/test builds only).
    #[arg(long, global = true, hide = true)]
    pub data_root: Option<PathBuf>,
    #[command(subcommand)]
    pub command: Command,
}
#[derive(Subcommand)]
pub enum Command {
    /// Manage independently validated immutable Profile revisions.
    Profile {
        #[command(subcommand)]
        command: ProfileCommand,
    },
    /// Build, sign, verify and wrap independently admitted Pack artifacts.
    Pack {
        #[command(subcommand)]
        command: PackCommand,
    },
    /// Revisit trusted approval for a Profile's current Pack permissions.
    Approve {
        #[arg(long)]
        profile: String,
    },
    /// Manage per-Profile secrets using the Core user API.
    Secrets {
        #[command(subcommand)]
        command: SecretCommand,
    },
}
#[derive(Subcommand)]
pub enum ProfileCommand {
    /// Create from a Profile or published structural definition; approvals and
    /// data are never copied from the definition's previous identity.
    Create {
        #[arg(long)]
        name: String,
        #[arg(long)]
        /// Definition file; relative Pack sources resolve beside this file.
        definition: PathBuf,
    },
    /// Start actual input Packs and route every input through the single Flow.
    Run {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        source: Option<String>,
        #[arg(long)]
        input_shape: Option<String>,
        #[arg(long, default_value = "read")]
        input_op: String,
        #[arg(long)]
        once: bool,
    },
    /// Publish the explicit Profile structural fields only.
    Export {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        output: PathBuf,
    },
    /// Import independently verified Packs with fresh approval records.
    Import {
        /// Publication file; relative Pack sources resolve beside this file.
        file: PathBuf,
        #[arg(long)]
        name: String,
        #[arg(long)]
        ceiling: Option<PathBuf>,
    },
    /// Add a verified local directory or .tpk artifact pin.
    Add {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        pack: PathBuf,
    },
    /// Update an installed pin, approving only newly added permissions.
    Update {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        pack: PathBuf,
    },
    /// Remove only when every required port remains satisfied.
    Remove {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        pack: String,
    },
    /// Inspect conflicts and optionally call a declared repair-generator Pack.
    Repair {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        candidate: PathBuf,
        #[arg(long)]
        generator: Option<String>,
        #[arg(long)]
        output: PathBuf,
    },
}
#[derive(Subcommand)]
pub enum PackCommand {
    /// Build a deterministic .tpk from a manifest and its declared files.
    Build {
        directory: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    /// Sign exact StrictV1 manifest bytes with a local Ed25519 key file.
    Sign {
        manifest: PathBuf,
        #[arg(long)]
        key: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
    /// Verify bytes, signatures, declared imports and authoritative WIT.
    Verify {
        artifact: PathBuf,
        #[arg(long)]
        publishers: Option<PathBuf>,
    },
    /// Compile an unmodified pinned extension through the SDK's pi adapter.
    WrapPi {
        source: PathBuf,
        #[arg(long)]
        upstream: String,
        #[arg(long)]
        id: String,
        #[arg(long)]
        output: PathBuf,
        #[arg(long)]
        builder: Option<PathBuf>,
    },
}
#[derive(Subcommand)]
pub enum SecretCommand {
    /// Store exact file bytes in the per-Profile encrypted vault; never echo them.
    Set {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        name: String,
        #[arg(long)]
        from_file: PathBuf,
    },
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn there_is_no_yes_approval_bypass() {
        assert!(
            Args::try_parse_from(["tobkiri", "--yes", "approve", "--profile", "prof_one"]).is_err()
        );
    }
    #[test]
    fn runtime_selection_is_generic() {
        let args = Args::try_parse_from([
            "tobkiri",
            "profile",
            "run",
            "--profile",
            "prof_one",
            "--source",
            "local.input",
            "--input-shape",
            "local.app.input@1.0",
            "--once",
        ])
        .unwrap();
        assert!(matches!(
            args.command,
            Command::Profile {
                command: ProfileCommand::Run { once: true, .. }
            }
        ));
    }
    #[test]
    fn host_config_map_cannot_override_hash_bound_pack_defaults() {
        assert!(
            Args::try_parse_from([
                "tobkiri",
                "profile",
                "run",
                "--profile",
                "prof_one",
                "--config",
                "override.json"
            ])
            .is_err()
        );
    }
}
