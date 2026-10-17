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
    /// Apply a structural definition to the same existing Profile with head CAS.
    Apply {
        #[arg(long)]
        profile: tobkiri_core::ids::ProfileId,
        /// Profile or published definition; local Pack sources resolve beside it.
        #[arg(long)]
        definition: PathBuf,
        /// Exact current sha256: digest; stale heads require an explicit reload.
        #[arg(long)]
        expected_head: tobkiri_core::Digest,
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
    /// Invoke one declared typed User port, draining its producer-owned stream.
    Invoke {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        shape: String,
        #[arg(long)]
        op: String,
        /// Bounded StrictV1 object arguments. Never reads metadata from stdin.
        #[arg(long)]
        args_file: PathBuf,
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
    /// Store exact secret bytes in the per-Profile encrypted vault; never echo them.
    Set {
        #[arg(long)]
        profile: String,
        #[arg(long)]
        name: String,
        #[arg(
            long,
            required_unless_present = "from_env",
            conflicts_with = "from_env"
        )]
        from_file: Option<PathBuf>,
        /// Name of a process environment variable; its value is never an argument.
        #[arg(
            long,
            required_unless_present = "from_file",
            conflicts_with = "from_file"
        )]
        from_env: Option<String>,
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
    fn profile_apply_requires_target_definition_and_valid_expected_head() {
        let head = tobkiri_core::Digest::hash(b"expected head").to_string();
        let base = [
            "tobkiri",
            "profile",
            "apply",
            "--profile",
            "prof_existing",
            "--definition",
            "profile.json",
            "--expected-head",
            head.as_str(),
        ];
        let args = Args::try_parse_from(base).unwrap();
        assert!(matches!(args.command, Command::Profile {
            command: ProfileCommand::Apply { profile, expected_head, .. }
        } if profile.as_str() == "prof_existing" && expected_head.as_str() == head));
        for omitted in ["--profile", "--definition", "--expected-head"] {
            let mut missing = Vec::new();
            let mut skip_value = false;
            for item in base {
                if skip_value {
                    skip_value = false;
                    continue;
                }
                if item == omitted {
                    skip_value = true;
                } else {
                    missing.push(item);
                }
            }
            assert!(Args::try_parse_from(missing).is_err(), "{omitted}");
        }
        for malformed in ["", "sha256:00", "SHA256:abcd", "not-a-digest"] {
            let mut invalid = base;
            invalid[8] = malformed;
            assert!(Args::try_parse_from(invalid).is_err());
        }
        let mut foreign = base;
        foreign[4] = "invalid profile id";
        assert!(Args::try_parse_from(foreign).is_err());
    }
    #[test]
    fn secrets_require_exactly_one_source_and_never_a_value_argument() {
        let base = [
            "tobkiri",
            "secrets",
            "set",
            "--profile",
            "prof_one",
            "--name",
            "api-key",
        ];
        assert!(Args::try_parse_from(base).is_err());
        for source in [
            ["--from-env", "TOBKIRI_TEST_KEY"],
            ["--from-file", "key.bin"],
        ] {
            assert!(Args::try_parse_from(base.into_iter().chain(source)).is_ok());
        }
        assert!(
            Args::try_parse_from(base.into_iter().chain([
                "--from-file",
                "key.bin",
                "--from-env",
                "TOBKIRI_TEST_KEY",
            ]))
            .is_err()
        );
        assert!(Args::try_parse_from(base.into_iter().chain(["--value", "secret"])).is_err());
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
    fn native_user_invocation_requires_explicit_typed_file_arguments() {
        let args = Args::try_parse_from([
            "tobkiri",
            "profile",
            "invoke",
            "--profile",
            "prof_one",
            "--shape",
            "local.fixture.input@1.0",
            "--op",
            "stream",
            "--args-file",
            "request.json",
        ])
        .unwrap();
        assert!(matches!(
            args.command,
            Command::Profile {
                command: ProfileCommand::Invoke { .. }
            }
        ));
        assert!(
            Args::try_parse_from([
                "tobkiri",
                "profile",
                "invoke",
                "--profile",
                "prof_one",
                "--shape",
                "local.fixture.input@1.0",
                "--op",
                "stream"
            ])
            .is_err()
        );
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
