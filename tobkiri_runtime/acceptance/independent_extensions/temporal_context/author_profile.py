"""Author unresolved named Profile selection using public intent fields only."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


def render_intent(*, base_pack_id: str, baseline_packs: list[dict[str, Any]],
                  caller_function_id: str,
                  profile_id: str = "acceptance.temporal.named",
                  temporal_selected: bool = True,
                  projection_selected: bool = True,
                  temporal_pack_id: str = "acceptance.temporal.context",
                  projection_root: str = "profile_projections/temporal",
                  shell: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an unresolved selection; caller supplies public catalog identities.

    The source intent does not fabricate Base/Shell artifacts, runtime bindings,
    release pins, approval, or authority. A public compiler must resolve the
    supplied identities against the selected catalog before this can be used.
    """
    packs = deepcopy(baseline_packs)
    if temporal_selected:
        packs.append({"pack_id": temporal_pack_id, "artifact_digest": None,
                      "role": "provider"})
    edges = []
    if temporal_selected:
        edges.append({"caller_function_id": caller_function_id,
                      "target_provider_id": f"{temporal_pack_id}.provider",
                      "contract_id": f"{temporal_pack_id}.v1",
                      "operation_id": "temporal.reduce",
                      "requested_scope_template": {}})
    projections = []
    if projection_selected:
        projections.append({"projection_id": "acceptance.temporal.rules",
                            "kind": "profile_content",
                            "artifact_root": projection_root,
                            "content_digest": None})
    return {"intent_api_version": "io.tobkiri.profile-intent.v1",
            "profile_id": profile_id, "display_name": "Tobkiri temporal context",
            "state": "needs_resolution", "mode": "interactive" if shell else
            "headless", "catalog_revision": None,
            "base": {"pack_id": base_pack_id, "artifact_digest": None,
                     "definition_revision": None,
                     "resolution": "verified_exact_artifact_required"},
            "shell": deepcopy(shell), "packs": packs,
            "content_projections": projections, "requested_edges": edges,
            "authority_references": [],
            "profile_authority_snapshot_digest": None}


def render_tauri_intent(*, profile_id: str = "acceptance.temporal.named",
                        temporal_pack_id: str = "acceptance.temporal.context",
                        temporal_selected: bool = True,
                        projection_selected: bool = True) -> dict[str, Any]:
    """Select published Tauri source artifacts and an exact Pack-origin subtree.

    No caller edge is invented: catalog availability and Flow execution are
    separate. Shell remains build_required in this source-only template.
    """
    intent = render_intent(
        base_pack_id="defaults-basepack",
        baseline_packs=[{"pack_id": "runtime.tauri.application.default",
                         "artifact_digest": None, "role": "application"}],
        caller_function_id="unused.caller", profile_id=profile_id,
        temporal_selected=temporal_selected, projection_selected=False,
        temporal_pack_id=temporal_pack_id,
        shell={"provider_id": "shell.tauri.default",
               "pack_id": "shell.tauri.default", "artifact_digest": None,
               "executable_artifact_digest": None, "definition_revision": None,
               "contract_id": "app.shell.v1", "platform": "macos",
               "architecture": "arm64"},
    )
    intent["requested_edges"] = []
    if projection_selected:
        intent["content_projections"] = [{
            "projection_id": "acceptance.temporal.rules", "kind": "profile_content",
            "artifact_root": "content", "source_pack_id": temporal_pack_id,
            "source_artifact_digest": None, "content_digest": None,
        }]
    return intent
