#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$LAUNCHER_ROOT/.." && pwd)"
FRONTEND_ROOT="$LAUNCHER_ROOT/frontend"
TAURI_MANIFEST="$LAUNCHER_ROOT/src-tauri/Cargo.toml"
OUTPUT_DIR="$LAUNCHER_ROOT/artifacts/launcher-verification"
BASELINE=""
APP_PATH=""
LABEL=""
REQUIRE_MACOS=0
SKIP_TAURI=0
ENFORCE_LOG_ERRORS=0
LOGS=()

usage() {
  cat <<'EOF'
Usage: verify_launcher_release.sh [options]

Runs frontend typecheck/tests/production build, Rust tests, a macOS Tauri build,
and reproducible bundle/log measurements.

Options:
  --app PATH                 Measure this built .app (auto-detected after Tauri build otherwise).
  --baseline PATH            Previous measure_launcher_bundle.py JSON report.
  --label TEXT               Commit/build label written into the bundle report.
  --log PATH                 Log to classify; repeatable.
  --output-dir PATH          Verification artifact directory.
  --require-macos            Fail instead of skipping the Tauri app build off macOS.
  --skip-tauri               Skip the Tauri app build (Rust tests still run).
  --enforce-log-errors       Fail after reporting integrity/startup-race findings.
  -h, --help                 Show this help.
EOF
}

while (($#)); do
  case "$1" in
    --app) APP_PATH="$2"; shift 2 ;;
    --baseline) BASELINE="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --log) LOGS+=("$2"); shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --require-macos) REQUIRE_MACOS=1; shift ;;
    --skip-tauri) SKIP_TAURI=1; shift ;;
    --enforce-log-errors) ENFORCE_LOG_ERRORS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

mkdir -p "$OUTPUT_DIR"

run_logged() {
  local name="$1"
  shift
  echo "==> $name"
  "$@" 2>&1 | tee "$OUTPUT_DIR/$name.log"
}

command -v npm >/dev/null || { echo "npm is required" >&2; exit 69; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 69; }
command -v cargo >/dev/null || { echo "cargo is required" >&2; exit 69; }

run_logged frontend-typecheck npm --prefix "$FRONTEND_ROOT" run lint
run_logged frontend-tests npm --prefix "$FRONTEND_ROOT" test
run_logged frontend-production-build npm --prefix "$FRONTEND_ROOT" run build
run_logged measurement-tests python3 "$SCRIPT_DIR/tests/test_launcher_measurement.py"
run_logged tauri-rust-tests cargo test --locked --manifest-path "$TAURI_MANIFEST"

if ((SKIP_TAURI == 0)); then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    run_logged tauri-macos-production-build bash "$REPO_ROOT/scripts/build-and-sign.sh" --mode production --bundles app
  elif ((REQUIRE_MACOS == 1)); then
    echo "macOS is required for the requested Tauri app verification" >&2
    exit 3
  else
    printf '%s\n' "SKIPPED: macOS Tauri app build (host=$(uname -s))" | tee "$OUTPUT_DIR/tauri-macos-production-build.log"
  fi
fi

if [[ -z "$APP_PATH" && "$(uname -s)" == "Darwin" ]]; then
  APP_PATH="$(find "$LAUNCHER_ROOT/src-tauri/target/release/bundle/macos" -maxdepth 1 -type d -name '*.app' -print 2>/dev/null | head -n 1 || true)"
fi

if [[ -n "$APP_PATH" ]]; then
  measure_args=(
    --app "$APP_PATH"
    --output "$OUTPUT_DIR/bundle-metrics.json"
    --label "$LABEL"
  )
  if [[ -n "$BASELINE" ]]; then
    measure_args+=(--baseline "$BASELINE")
  fi
  run_logged bundle-measurement python3 "$SCRIPT_DIR/measure_launcher_bundle.py" "${measure_args[@]}"
else
  printf '%s\n' "SKIPPED: bundle measurement (no --app and no built .app detected)" | tee "$OUTPUT_DIR/bundle-measurement.log"
fi

if ((${#LOGS[@]})); then
  log_args=("${LOGS[@]}" --output "$OUTPUT_DIR/log-findings.json")
  if ((ENFORCE_LOG_ERRORS == 1)); then
    # Run once with both gates while preserving the generated report. Startup
    # race uses exit 3 only when no integrity error already selected exit 2.
    log_args+=(--fail-on-integrity --fail-on-startup-race)
  fi
  run_logged launcher-log-analysis python3 "$SCRIPT_DIR/analyze_launcher_logs.py" "${log_args[@]}"
else
  printf '%s\n' "SKIPPED: launcher log analysis (no --log arguments)" | tee "$OUTPUT_DIR/launcher-log-analysis.log"
fi

cat > "$OUTPUT_DIR/verification-summary.txt" <<EOF
host=$(uname -s)
label=$LABEL
frontend_typecheck=passed
frontend_tests=passed
frontend_production_build=passed
measurement_tests=passed
tauri_rust_tests=passed
tauri_macos_build=$([[ "$(uname -s)" == "Darwin" && $SKIP_TAURI == 0 ]] && echo passed || echo skipped)
app_path=${APP_PATH:-not-measured}
EOF

cat "$OUTPUT_DIR/verification-summary.txt"
