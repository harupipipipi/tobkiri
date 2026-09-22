#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER_ROOT="$REPO_ROOT/tobkiri_launcher"
DEFAULTSPACK_WEBAPP_ROOT="$REPO_ROOT/tobkiri_runtime/ecosystem/defaultspack/webapp"

target=""
has_target=0
mode=""
has_bundles=0
forwarded_args=()
for ((index = 1; index <= $#; index++)); do
  argument="${!index}"
  case "$argument" in
    --mode)
      next=$((index + 1))
      [[ "$next" -le "$#" ]] || {
        echo "--mode requires production or local-dev" >&2
        exit 64
      }
      mode="${!next}"
      index="$next"
      ;;
    --mode=*)
      mode="${argument#--mode=}"
      ;;
    --target)
      next=$((index + 1))
      [[ "$next" -le "$#" ]] || {
        echo "--target requires a Rust target triple" >&2
        exit 64
      }
      target="${!next}"
      has_target=1
      index="$next"
      ;;
    --target=*)
      target="${argument#--target=}"
      has_target=1
      ;;
    --debug)
      if [[ "$mode" != "local-dev" ]]; then
        echo "--debug is allowed only with explicit --mode local-dev" >&2
        exit 64
      fi
      ;;
    --no-bundle)
      echo "--no-bundle is not a package output mode" >&2
      exit 64
      ;;
    --config|--config=*)
      echo "build-and-sign owns the production Tauri configuration; $argument is not allowed" >&2
      exit 64
      ;;
    --bundles)
      has_bundles=1
      forwarded_args+=("$argument")
      ;;
    --bundles=*)
      has_bundles=1
      forwarded_args+=("$argument")
      ;;
    *)
      forwarded_args+=("$argument")
      ;;
  esac
done

case "$mode" in
  production|local-dev) ;;
  "")
    echo "build-and-sign requires explicit --mode production or --mode local-dev" >&2
    exit 64
    ;;
  *)
    echo "unsupported build-and-sign mode: $mode" >&2
    exit 64
    ;;
esac

if [[ -z "$target" ]]; then
  target="${TAURI_ENV_TARGET_TRIPLE:-}"
fi
if [[ -z "$target" ]]; then
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64|Darwin:aarch64)
      target="aarch64-apple-darwin"
      ;;
    Darwin:x86_64|Darwin:amd64)
      target="x86_64-apple-darwin"
      ;;
    Linux:x86_64|Linux:amd64)
      target="x86_64-unknown-linux-gnu"
      ;;
    Linux:arm64|Linux:aarch64)
      target="aarch64-unknown-linux-gnu"
      ;;
    MINGW*:x86_64|MSYS*:x86_64|CYGWIN*:x86_64)
      target="x86_64-pc-windows-msvc"
      ;;
    *)
      echo "Unable to infer a supported release target; pass --target explicitly" >&2
      exit 64
      ;;
  esac
fi

case "$target" in
  aarch64-apple-darwin)
    presentation_platform="macos"
    presentation_architecture="arm64"
    shell_bundles="app"
    artifact="$LAUNCHER_ROOT/src-tauri/target/$target/release/bundle/macos/Tobkiri.app"
    ;;
  x86_64-apple-darwin)
    presentation_platform="macos"
    presentation_architecture="x86_64"
    shell_bundles="app"
    artifact="$LAUNCHER_ROOT/src-tauri/target/$target/release/bundle/macos/Tobkiri.app"
    ;;
  x86_64-unknown-linux-gnu)
    presentation_platform="linux"
    presentation_architecture="x86_64"
    shell_bundles="appimage"
    artifact=""
    ;;
  x86_64-pc-windows-msvc)
    presentation_platform="windows"
    presentation_architecture="x86_64"
    shell_bundles="nsis"
    artifact="$LAUNCHER_ROOT/src-tauri/target/$target/release/tobkiri-shell.exe"
    ;;
  *)
    echo "Unsupported release target: $target" >&2
    exit 64
    ;;
esac

if [[ "$mode" == "production" && "$presentation_platform" != "macos" ]]; then
  echo "Production packaging is currently macOS-only; refusing $presentation_platform target $target" >&2
  exit 64
fi

python3 "$REPO_ROOT/scripts/release_gate.py" signing \
  --mode "$mode" \
  --platform "$presentation_platform" \
  --check-tools

if [[ "$presentation_platform" == "macos" && -z "${APPLE_CERTIFICATE:-}" ]]; then
  export APPLE_CERTIFICATE="${APPLE_CERTIFICATE_BASE64:-}"
fi

if [[ -n "${TOBKIRI_PRESENTATION_RELEASE_ROOT:-}" ]]; then
  echo "TOBKIRI_PRESENTATION_RELEASE_ROOT is owned by build-and-sign and may not be overridden" >&2
  exit 64
fi

source_revision="$(git -C "$REPO_ROOT" rev-parse --verify HEAD)"
origin="$(git -C "$REPO_ROOT" config --get remote.origin.url)"
origin_without_suffix="${origin%/}"
origin_without_suffix="${origin_without_suffix%.git}"
case "$origin_without_suffix" in
  https://github.com/*)
    source_identity="github:${origin_without_suffix#https://github.com/}"
    ;;
  http://github.com/*)
    source_identity="github:${origin_without_suffix#http://github.com/}"
    ;;
  git@github.com:*)
    source_identity="github:${origin_without_suffix#git@github.com:}"
    ;;
  *)
    source_identity="git:$origin"
    ;;
esac

release_parent="$(mktemp -d "${TMPDIR:-/tmp}/tobkiri-presentation-release.XXXXXX")"
release_root="$release_parent/sealed"
signing_key="$release_parent/signing-key.raw"
cleanup() {
  rm -rf "$release_parent"
}
trap cleanup EXIT
umask 077
python3 -c 'import secrets,sys; open(sys.argv[1], "wb").write(secrets.token_bytes(32))' "$signing_key"

echo "=== Preparing verified runtime tools ($target) ==="
echo "=== Building canonical Defaultspack webapp ($target) ==="
(
  cd "$DEFAULTSPACK_WEBAPP_ROOT"
  npm run build
  npm run check:shell-bundle
)

python3 "$LAUNCHER_ROOT/scripts/prepare_viewer_runtime.py" \
  --mode "$([[ "$mode" == "production" ]] && echo release || echo dev)" \
  --repo-root "$REPO_ROOT" \
  --target "$target"

if [[ "$mode" == "local-dev" ]]; then
  echo "=== Building LOCAL-DEV ONLY Tobkiri Launcher package ($target) ==="
  local_args=("${forwarded_args[@]}")
  if [[ "$has_bundles" == "0" ]]; then
    local_args+=(--bundles "$shell_bundles")
  fi
  (cd "$LAUNCHER_ROOT" && cargo tauri build \
    --debug \
    --target "$target" \
    "${local_args[@]}")
  echo "LOCAL-DEV ONLY: unsigned/ad-hoc output is not a production release"
  exit 0
fi

echo "=== Building current-source Tobkiri Shell ($target) ==="
(cd "$LAUNCHER_ROOT" && cargo tauri build \
  --target "$target" \
  --config src-tauri/tauri.shell.conf.json \
  --bundles "$shell_bundles" \
  --ci)

if [[ "$presentation_platform" == "linux" ]]; then
  artifact="$(find "$LAUNCHER_ROOT/src-tauri/target/$target/release/bundle/appimage" \
    -maxdepth 1 -type f -name '*.AppImage' -print -quit)"
fi
[[ -n "$artifact" && -e "$artifact" ]] || {
  echo "Tauri Shell release artifact was not produced: ${artifact:-<empty>}" >&2
  exit 1
}

if [[ "$presentation_platform" == "macos" ]]; then
  python3 "$REPO_ROOT/scripts/release_gate.py" sign-artifacts \
    --repo-root "$REPO_ROOT" \
    --mode production \
    --platform macos \
    --app-bundle "$artifact" \
    --artifact "$artifact"
elif [[ "$presentation_platform" == "windows" ]]; then
  python3 "$REPO_ROOT/scripts/release_gate.py" sign-artifacts \
    --repo-root "$REPO_ROOT" \
    --mode production \
    --platform windows \
    --artifact "$artifact"
fi

manifest="$release_parent/shell-build-output.v4.json"
python3 "$LAUNCHER_ROOT/scripts/write_shell_build_output.py" \
  --artifact-id "shell.tauri.default.$presentation_platform-$presentation_architecture" \
  --artifact "$artifact" \
  --platform "$presentation_platform" \
  --architecture "$presentation_architecture" \
  --source-identity "$source_identity" \
  --source-revision "$source_revision" \
  --output "$manifest"
python3 "$LAUNCHER_ROOT/scripts/package_presentation_artifact.py" \
  --catalog "$LAUNCHER_ROOT/src-tauri/bundled/presentation_catalog.json" \
  --build-output-manifest "$manifest" \
  --signing-key "$signing_key" \
  --signing-key-id "local-build:$target:$source_revision" \
  --repository-root "$REPO_ROOT" \
  --output-dir "$release_root"

outer_args=("${forwarded_args[@]}")
outer_args+=(--target "$target")

echo "=== Building sealed Tobkiri Launcher package ($target) ==="
(cd "$LAUNCHER_ROOT" && \
  TOBKIRI_PRESENTATION_RELEASE_ROOT="$release_root" \
  cargo tauri build "${outer_args[@]}")

if [[ "$presentation_platform" == "macos" ]]; then
  app_bundle="$LAUNCHER_ROOT/src-tauri/target/$target/release/bundle/macos/Tobkiri Launcher.app"
  notary_args=(--artifact "$app_bundle")
  mapfile -t notarized_artifacts < <(
    find "$LAUNCHER_ROOT/src-tauri/target/$target/release/bundle" \
      -type f -name '*.dmg' -print
  )
  for artifact in "${notarized_artifacts[@]}"; do
    notary_args+=(--artifact "$artifact")
  done
  python3 "$REPO_ROOT/scripts/release_gate.py" sign-artifacts \
    --repo-root "$REPO_ROOT" \
    --mode production \
    --platform macos \
    --app-bundle "$app_bundle" \
    "${notary_args[@]}"
elif [[ "$presentation_platform" == "windows" ]]; then
  mapfile -t signed_artifacts < <(
    find "$LAUNCHER_ROOT/src-tauri/target/$target/release/bundle/nsis" \
      -maxdepth 1 -type f -name '*.exe' -print
  )
  sign_args=()
  for artifact in "${signed_artifacts[@]}"; do
    sign_args+=(--artifact "$artifact")
  done
  python3 "$REPO_ROOT/scripts/release_gate.py" sign-artifacts \
    --repo-root "$REPO_ROOT" \
    --mode production \
    --platform windows \
    "${sign_args[@]}"
fi

cat <<'EOF'

The package was built from a sealed Shell v4 release root. Tauri signs the
macOS app after copying its resources; do not re-sign the app afterward or the
DMG will contain a different signature from the post-build .app.
EOF
