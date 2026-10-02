# Permissions Policy

Profile permission files are defaults only.

`grants.yaml` starts empty. `tool_policy.yaml` defaults network to deny, requires approval for write actions and high-risk tools, and rejects client-supplied approved flags. `approvals.yaml` starts with no one-shot tokens or persistent approvals.

The final enforcement boundary remains the existing approval, grant, and capability systems. A profile permission file must never permit a high-risk tool by itself, and runtime code must not trust a client-supplied `approved` flag.

## Authority v2 Boundary

Signed `CapabilityGrant` records are the source of truth for runtime authority. Profile YAML and UI defaults can propose policy, but enforcement must resolve to a signed grant, a signed one-shot approval token, or a core-local decision path.

Profile principals use `profile:<id>` as the parent ceiling. A child principal such as `profile:work__surface:mobile__device:phone-1`, `profile:work__pack:defaultspack`, `profile:work__provider:rumi`, or `profile:work__frontend:mobile` is allowed only when the required profile parent and child grants exist, are enabled, and their Authority v2 constraints intersect to include the requested resource. Missing child grants deny rather than inheriting broad parent authority. Profile principals do not fall back to conversation or global grants.

Authority v2 constraints are the recognized facets only: provider, API, model, function, pack, caller pack/function, domain, port, host action, stream allowance, and input-token ceiling. Legacy grant metadata such as `mode: builtin` may remain on persisted grants, but metadata is ignored for constraint intersection and does not widen authority.

Server-derived request context is sealed at the transport boundary. `_headers`, `_authenticated_principal`, `_authority_subject`, `_method`, `_actual_method`, `_path`, `_query_params`, and raw body fields are never trusted from client JSON or form bodies.

Scoped access tokens use the `rumi_at_` opaque-token format. Token issuance is role allowlisted by the token manager and core API: `mobile_client` maps to `surface=mobile`, and `mobile_approver` maps to `surface=mobile-approver`; both are limited to `audience=kernel_api`. Mobile approver tokens can list, read, approve, or deny authority requests only through normal grants and only for their own profile. They may issue one-shot approvals, not profile-scope persistent grants.

Mobile approval requires device attestation. A `mobile_approver` first requests a server-generated challenge for one exact request, profile, device, token, permission, resource hash, decision, scope, and server-fixed one-shot token TTL. The challenge route is guarded by `authority.request.approve` because it is the first step of the approve/deny flow, but creating a challenge does not issue a grant or one-shot approval. The mobile app signs the server canonical payload hash with the registered device Ed25519 key. Approval or denial consumes that challenge atomically; unsigned bodies, wrong-device signatures, replayed challenges, post-signing related-permission or TTL expansion, and challenges whose grants are no longer valid fail closed.

Non-core code does not run directly on the host. Capability execution classifies trusted core and shipped built-in pack code as `core_in_process`; third-party pack functions, provider code, raw subprocess declarations, binary entries, command entries, and Docker-style user functions require `managed_sandbox`. If the profile-scoped managed runtime, Bubblewrap, or cgroup controller is unavailable, execution fails with `SANDBOX_RUNTIME_UNAVAILABLE` or `SANDBOX_RESOURCE_CONTROLLER_UNAVAILABLE`; host subprocess and `RUMI_ALLOW_HOST_FALLBACK` are not sandbox substitutes.

Profile runtime names are derived from a stable hash such as `rumi-profile-<sha256(profile_id)[:16]>`, never from raw profile IDs. The implemented managed sandbox slice stages regular function files into a temporary `/workspace`, rejects symlinks, hardlinks, devices, fifos, sockets, oversized trees, and oversized files before Bubblewrap starts, clears inherited environment variables, disables nested user namespaces, runs with network off, and applies systemd cgroup limits. The immutable root must be configured explicitly with `RUMI_SANDBOX_IMMUTABLE_ROOT` or a server-side request root, must not be `/`, must not be group/other writable, and must contain a non group/other writable `.rumi-sandbox-root` marker.

Sandbox-internal child processes are not ambient authority. A child executable
must be an exact verified ArtifactVariant selected and started by the Host-owned
runner. Python Pack functions cannot dynamically invoke guest system binaries
through `subprocess`, `os.system`, `exec`, `spawn`, `fork`, or equivalent native
syscalls. The capability runner installs a fail-closed Linux syscall filter after
its own controlled start; missing filter support denies the invocation. Explicit
coding-terminal operations remain separate Host-routed capabilities with their
own approval, resource, timeout, cleanup, and audit boundaries.

This PR provides the managed sandbox runner core, not durable rootfs provisioning. Developer or operator environments must prepare a minimal immutable rootfs before third-party pack execution is enabled. A minimal Linux setup looks like:

```bash
sudo mkdir -p /opt/rumi/sandbox-root/{usr,bin,lib,lib64,tmp,home}
sudo touch /opt/rumi/sandbox-root/.rumi-sandbox-root
sudo chmod 0755 /opt/rumi/sandbox-root
sudo chmod 0644 /opt/rumi/sandbox-root/.rumi-sandbox-root
export RUMI_SANDBOX_IMMUTABLE_ROOT=/opt/rumi/sandbox-root
```

The rootfs must contain the interpreter and libraries needed by the staged runner, for example `python3` and its runtime libraries, using the distribution's normal packaging or a prepared image extraction flow. `bootstrap.py --cli doctor` reports Bubblewrap availability, the configured root marker, and whether `systemd-run --user --scope true` can actually create a user scope. Execution treats a failed user-systemd probe as `SANDBOX_RESOURCE_CONTROLLER_UNAVAILABLE`, not as a host fallback trigger.

The sandbox does not yet provide egress proxying, a secret broker, durable per-profile rootfs provisioning, desktop window sandboxing, or pack-controlled port forwarding. Host operations must flow through HostIntent and the viewer broker with one-shot authority; broker bearer tokens, signing secrets, and ambient provider API keys are not inherited into sandbox environments.

Legacy bearer/HMAC LAN compatibility remains local-only by default. Remote callers must use scoped tokens and still pass route authority, local policy, approval, and audit checks.
