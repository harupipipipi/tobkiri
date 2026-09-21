from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

from domain.tool.permission_resolver import ToolPermissionResolver
from domain.tool.service_catalog import ToolServiceCatalog


MOBILE_TOOL_INVOKE_BASIC_SCOPE = "tools.invoke.basic"
MOBILE_TOOL_INVOKE_CLOUD_SCOPE = "tools.invoke.cloud"
MOBILE_PC_DELEGATION_ROUTE = "/api/mobile/v1/tools/invoke"
MOBILE_CLOUD_DELEGATION_ROUTE = "/api/mobile/v1/cloud/tools/invoke"
MOBILE_CLOUD_DELEGATION_PROVIDER_ID = "cloudflare_sandbox_bridge"
MOBILE_COMPATIBLE_TAG = "mobile-compatible"
MOBILE_FLUTTER_TAG = "mobile-flutter"
MOBILE_IOS_TAG = "mobile-ios"
MOBILE_ANDROID_TAG = "mobile-android"
MOBILE_SWIFT_NATIVE_TAG = "mobile-swift-native"
MOBILE_KOTLIN_NATIVE_TAG = "mobile-kotlin-native"
MOBILE_PC_DELEGATED_TAG = "pc-delegated"
MOBILE_AGENT_TEMPLATE = {
    "template_id": "rumi.composer.default",
    "ai_input_id": "rumi.composer.default:default_ai_input",
    "tool_policy_id": "rumi.composer.default:default_tools",
}

_HOST_BOUND_SERVICES = {
    "browser",
    "computer",
    "terminal",
    "coding",
    "files",
    "artifacts",
    "mcp",
}
_HOST_BOUND_TAGS = {
    "agent_os",
    "browser",
    "coding",
    "computer_use",
    "desktop",
    "file",
    "files",
    "host",
    "mcp",
    "sandbox",
    "shell",
    "terminal",
    "workspace",
}
_CLI_DRY_RUN_TOOLS = {
    "github_search",
    "github_pr_create",
    "github_issue_create",
    "github_issue_update",
    "github_issue_list",
    "linear_issue_sync",
    "jira_issue_sync",
}
_CONNECTOR_PAYLOAD_DRY_RUN_TOOLS = {
    "gmail_search",
    "gmail_draft",
    "calendar_create",
    "drive_create",
    "drive_export",
    "slack_send",
    "discord_send",
    "line_push",
}
_PHONE_AI_MODEL_TOOLS = {
    "ai_models": ("implemented_phone_ai_catalog", False),
    "ai_profiles": ("implemented_phone_ai_catalog", False),
    "ai_providers": ("implemented_phone_ai_catalog", False),
    "ai_get_provider_key_status": ("implemented_phone_ai_provider_key_status", False),
    "ai_set_provider_key": ("implemented_phone_ai_provider_key", True),
    "ai_delete_provider_key": ("implemented_phone_ai_provider_key", True),
    "ai_get_preferred_model": ("implemented_phone_ai_model_settings", False),
    "ai_set_preferred_model": ("implemented_phone_ai_model_settings", True),
    "ai_get_thinking_level": ("implemented_phone_ai_model_settings", False),
    "ai_set_thinking_level": ("implemented_phone_ai_model_settings", True),
    "ai_get_effective_thinking_level": ("implemented_phone_ai_model_settings", False),
    "ai_normalize_thinking_level": ("implemented_phone_ai_model_settings", False),
    "ai_validate_model_params": ("implemented_phone_ai_param_validation", False),
    "ai_recommend_model": ("implemented_phone_ai_routing_hint", False),
    "ai_route_model": ("implemented_phone_ai_routing_hint", False),
    "ai_explain_model_choice": ("implemented_phone_ai_routing_hint", False),
}
_PHONE_PROMPT_TOOLS = {
    "prompt_validate_template": ("implemented_phone_prompt_text", False),
    "prompt_render": ("implemented_phone_prompt_text", False),
    "prompt_lint_prompt": ("implemented_phone_prompt_text", False),
    "prompt_compact_prompt": ("implemented_phone_prompt_text", False),
    "prompt_test": ("implemented_phone_prompt_text", False),
    "prompt_system_get": ("implemented_phone_prompt_system", False),
    "prompt_system_set": ("implemented_phone_prompt_system", True),
    "prompt_list": ("implemented_phone_prompt_store", False),
    "prompt_create": ("implemented_phone_prompt_store", True),
    "prompt_update": ("implemented_phone_prompt_store", True),
    "prompt_delete": ("implemented_phone_prompt_store", True),
    "prompt_active": ("implemented_phone_prompt_effective", False),
    "prompt_load_effective": ("implemented_phone_prompt_effective", False),
    "prompt_resolve_for_conversation": ("implemented_phone_prompt_effective", False),
    "prompt_preview_toggle": ("implemented_phone_prompt_preview", False),
}
_PHONE_MEMORY_TOOLS = {
    "memory_store": ("implemented_phone_memory_store", True),
    "memory_list": ("implemented_phone_memory_store", False),
    "memory_recall": ("implemented_phone_memory_search", False),
    "memory_update": ("implemented_phone_memory_store", True),
    "memory_delete": ("implemented_phone_memory_store", True),
    "memory_compact": ("implemented_phone_memory_summary", False),
    "memory_project_context": ("implemented_phone_memory_context", False),
    "memory_resolve_for_agent": ("implemented_phone_memory_context", False),
    "memory_memo": ("implemented_phone_memo_store", True),
    "memory_memo_folders": ("implemented_phone_memo_store", True),
    "memory_memo_notes": ("implemented_phone_memo_store", True),
}
_PHONE_KNOWLEDGE_TOOLS = {
    "knowledge_create": ("implemented_phone_knowledge_store", True),
    "knowledge_get": ("implemented_phone_knowledge_store", False),
    "knowledge_list": ("implemented_phone_knowledge_store", False),
    "knowledge_update": ("implemented_phone_knowledge_store", True),
    "knowledge_delete": ("implemented_phone_knowledge_store", True),
    "knowledge_search": ("implemented_phone_knowledge_search", False),
    "knowledge_import_file": ("implemented_phone_knowledge_import", True),
    "knowledge_import_url": ("implemented_phone_knowledge_import", True),
    "knowledge_attach_to_project": ("implemented_phone_knowledge_store", True),
    "knowledge_index": ("implemented_phone_knowledge_index", True),
    "knowledge_reindex": ("implemented_phone_knowledge_index", True),
}
_PHONE_MEDIA_ARTIFACT_TOOLS = {
    "image_render": "implemented_phone_svg_image_render",
    "image_generate_local_or_provider": "implemented_phone_svg_image_placeholder",
    "audio_transcribe": "implemented_phone_audio_transcribe_payload",
    "audio_transcribe_local": "implemented_phone_audio_transcribe_payload",
    "tool_file_reader": "implemented_phone_artifact_file_reader",
    "file_reader": "implemented_phone_artifact_file_reader",
}
_PHONE_WORKFLOW_TOOLS = {
    "workflow_define": ("implemented_phone_workflow_record", False),
    "workflow_run": ("implemented_phone_workflow_record", True),
    "workflow_status": ("implemented_phone_workflow_record", False),
    "workflow_cancel": ("implemented_phone_workflow_record", True),
    "workflow_retry": ("implemented_phone_workflow_record", True),
}
_PHONE_ARTIFACT_WORKSPACE_TOOLS = {
    "artifact_file_list",
    "artifact_file_read",
    "artifact_file_write",
    "artifact_file_patch",
    "artifact_file_delete",
}
_PHONE_ARTIFACT_HTML_TOOLS = {
    "browser_save_page",
    "webapp_preview",
    "webapp_lint",
}
_PHONE_PACKAGE_WEBAPP_TOOLS = {
    "package_install_plan": ("implemented_phone_install_plan", False),
    "webapp_build": ("implemented_phone_static_webapp_build_plan", True),
    "research_report_export": ("implemented_phone_research_report_export", False),
}
_PHONE_ARTIFACT_GENERATOR_TOOLS = {
    "project_scaffold": "implemented_phone_artifact_scaffold",
    "doc_create": "implemented_phone_document_text",
    "slides_create": "implemented_phone_slide_outline",
    "slides_from_markdown": "implemented_phone_slide_outline",
    "chart_create": "implemented_phone_svg_chart",
}
_PHONE_DOCUMENT_SLIDE_TOOLS = {
    "doc_update": ("implemented_phone_document_text", True),
    "slides_update": ("implemented_phone_slide_outline", True),
    "slides_export": ("implemented_phone_slide_export", False),
}
_PHONE_JOB_TOOLS = {
    "job_create": ("implemented_phone_job_record", False),
    "job_status": ("implemented_phone_job_record", False),
    "job_history": ("implemented_phone_job_record", False),
    "job_artifacts": ("implemented_phone_job_record", False),
    "job_cancel": ("implemented_phone_job_record", True),
    "job_resume": ("implemented_phone_job_record", True),
}
_PHONE_SHEET_TOOLS = {
    "sheet_create": ("implemented_phone_sheet_text", False),
    "sheet_read": ("implemented_phone_sheet_text", False),
    "sheet_analyze": ("implemented_phone_sheet_text", False),
    "sheet_update": ("implemented_phone_sheet_text", True),
    "sheet_export": ("implemented_phone_sheet_export", False),
}
_PHONE_EXPORT_TOOLS = {
    "artifact_zip": "implemented_phone_zip_base64",
    "artifact_export": "implemented_phone_artifact_export",
    "static_site_export": "implemented_phone_zip_base64",
    "webapp_export_static": "implemented_phone_zip_base64",
    "doc_export": "implemented_phone_document_export",
    "pdf_export": "pc_delegation_required_binary_export",
    "doc_to_pdf": "pc_delegation_required_binary_export",
}


def mobile_agent_template() -> dict[str, str]:
    return dict(MOBILE_AGENT_TEMPLATE)


def mobile_cloud_delegation_status() -> dict[str, Any]:
    base_url = str(os.environ.get("RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL") or "").strip()
    api_key = str(os.environ.get("RUMI_CLOUDFLARE_SANDBOX_API_KEY") or "").strip()
    parsed = urlparse(base_url) if base_url else None
    is_local = _is_local_cloudflare_bridge_url(parsed)
    remote_plain_http = bool(parsed and parsed.scheme == "http" and not is_local)
    missing: list[str] = []
    if not base_url:
        missing.append("env:RUMI_CLOUDFLARE_SANDBOX_BRIDGE_URL")
    if base_url and remote_plain_http:
        missing.append("cloudflare_sandbox_bridge_https")
    if base_url and not api_key and not is_local:
        missing.append("env:RUMI_CLOUDFLARE_SANDBOX_API_KEY")

    available = bool(base_url) and not missing
    if available:
        status = "configured"
    elif remote_plain_http:
        status = "insecure_url"
    elif base_url and not api_key and not is_local:
        status = "missing_api_key"
    else:
        status = "not_configured"

    return {
        "provider_id": MOBILE_CLOUD_DELEGATION_PROVIDER_ID,
        "runtime": "cloudflare_sandbox_bridge",
        "available": available,
        "configured": bool(base_url),
        "status": status,
        "missing_requirements": missing,
    }


def mobile_tool_surface(
    *,
    pc_delegation_available: bool = True,
    cloud_delegation_available: bool | None = None,
) -> dict[str, Any]:
    cloud_status = mobile_cloud_delegation_status()
    if cloud_delegation_available is not None:
        cloud_status = {
            **cloud_status,
            "available": bool(cloud_delegation_available),
            "status": "configured" if cloud_delegation_available else cloud_status["status"],
        }
    return {
        "mode": "unified",
        "one_tool_surface": True,
        "phone_local_route": "phone",
        "pc_delegation_route": MOBILE_PC_DELEGATION_ROUTE,
        "pc_delegation_available": pc_delegation_available,
        "pc_delegation_scope": MOBILE_TOOL_INVOKE_BASIC_SCOPE,
        "cloud_delegation_route": MOBILE_CLOUD_DELEGATION_ROUTE,
        "cloud_delegation_available": bool(cloud_status.get("available")),
        "cloud_delegation_scope": MOBILE_TOOL_INVOKE_CLOUD_SCOPE,
        "cloud_delegation_status": cloud_status,
        "invoke_scopes": {
            "basic": MOBILE_TOOL_INVOKE_BASIC_SCOPE,
            "cloud": MOBILE_TOOL_INVOKE_CLOUD_SCOPE,
        },
        "routing": (
            "Call the defaultspack tool name directly. Phone-compatible tools run "
            "locally; host-bound tools route to the connected PC when PC delegation "
            "is available. Cloud delegation is advertised separately and only becomes "
            "available when the Cloudflare Sandbox Bridge is configured."
        ),
    }


def annotate_mobile_tool_record(record: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(record)
    compatibility = mobile_tool_compatibility(tool, record)
    tags = _ordered_tags([*list(annotated.get("tags") or []), *(compatibility.get("tags") or [])])
    annotated["tags"] = tags
    annotated["mobile"] = compatibility
    annotated["mobile_compatible"] = bool(compatibility.get("compatible"))
    annotated["mobile_unavailable_reason"] = str(compatibility.get("unavailable_reason") or "")
    annotated["execution_location"] = str(compatibility.get("execution_location") or "pc")
    annotated["callable"] = bool(compatibility.get("compatible")) and bool(compatibility.get("available", True))
    annotated["callable_on_current_device"] = annotated["callable"]
    annotated["execution_route"] = str(
        compatibility.get("execution_route")
        or ("pc" if annotated["callable"] else "unavailable")
    )
    annotated["automatic_routing"] = {
        **mobile_tool_surface(pc_delegation_available=True),
        "selected_route": annotated["execution_route"],
        "phone_local": compatibility.get("execution_location") == "phone",
    }
    return annotated


def mobile_tool_records(
    tools: list[dict[str, Any]],
    *,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    catalog = ToolServiceCatalog(tools)
    resolver = ToolPermissionResolver()
    request_context = context if isinstance(context, dict) else {}
    records: list[dict[str, Any]] = []
    for tool in tools:
        record = catalog.compact_record(tool)
        record["permission"] = resolver.resolve(tool, context=request_context)
        records.append(annotate_mobile_tool_record(record, tool))
    return records


def mobile_tool_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    compatible = [record for record in records if record.get("mobile_compatible")]
    unavailable = [record for record in records if not record.get("mobile_compatible")]
    return {
        "compatible_count": len(compatible),
        "unavailable_count": len(unavailable),
        "compatible_tag": MOBILE_COMPATIBLE_TAG,
        "platform_tags": {
            "flutter": MOBILE_FLUTTER_TAG,
            "ios": MOBILE_IOS_TAG,
            "android": MOBILE_ANDROID_TAG,
            "swift": MOBILE_SWIFT_NATIVE_TAG,
            "kotlin": MOBILE_KOTLIN_NATIVE_TAG,
            "pc_delegated": MOBILE_PC_DELEGATED_TAG,
        },
        "agent_template": mobile_agent_template(),
        "tool_surface": mobile_tool_surface(pc_delegation_available=True),
    }


def mobile_tool_compatibility(tool: dict[str, Any], record: dict[str, Any] | None = None) -> dict[str, Any]:
    record = record if isinstance(record, dict) else ToolServiceCatalog.compact_record(tool)
    service_id = str(record.get("service_id") or "").strip().lower()
    tool_id = str(record.get("tool_id") or tool.get("tool_id") or tool.get("name") or "").strip().lower()
    tags = _tag_set(record.get("tags")) | _tag_set(tool.get("tags"))
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    tags |= _tag_set(metadata.get("tags"))

    phone_plan = _phone_local_plan(tool_id)
    if phone_plan is not None:
        return {
            "compatible": True,
            "available": True,
            "execution_location": "phone",
            "execution_route": "phone",
            "unavailable_reason": "",
            "platforms": phone_plan["platforms"],
            "runtime_layers": phone_plan["runtime_layers"],
            "native_layers": phone_plan["native_layers"],
            "requires_pc": False,
            "requires_mobile_approval": phone_plan["requires_mobile_approval"],
            "implementation_status": phone_plan.get("implementation_status") or "implemented",
            "tags": [
                MOBILE_COMPATIBLE_TAG,
                *_platform_tags(phone_plan, pc_delegated=False),
            ],
            "agent_template": mobile_agent_template(),
        }

    blocked_reason = _blocked_reason(service_id, tags, tool)
    connection_status = str(record.get("connection_status") or "").strip().lower()
    if blocked_reason:
        plan = _mobile_port_plan(service_id, tags, tool)
        return {
            "compatible": False,
            "available": False,
            "execution_location": "unsupported",
            "execution_route": "unavailable",
            "unavailable_reason": blocked_reason,
            "platforms": plan["platforms"],
            "runtime_layers": plan["runtime_layers"],
            "native_layers": plan["native_layers"],
            "requires_pc": True,
            "requires_mobile_approval": plan["requires_mobile_approval"],
            "implementation_status": plan["implementation_status"],
            "tags": _platform_tags(plan, pc_delegated=True),
            "agent_template": mobile_agent_template(),
        }
    unavailable_reason = ""
    if connection_status == "setup_required":
        unavailable_reason = "PC側で接続またはAPI key設定が必要です。"
    elif connection_status in {"unavailable", "error"}:
        unavailable_reason = "PC側でこのtoolを利用できません。"
    return {
        "compatible": True,
        "available": not unavailable_reason,
        "execution_location": "pc",
        "execution_route": "pc",
        "unavailable_reason": unavailable_reason,
        "platforms": ["ios", "android"],
        "runtime_layers": ["flutter", "pc-defaultspack-runtime"],
        "native_layers": [],
        "requires_pc": True,
        "requires_mobile_approval": False,
        "implementation_status": "pc_delegated",
        "tags": [
            MOBILE_COMPATIBLE_TAG,
            MOBILE_FLUTTER_TAG,
            MOBILE_IOS_TAG,
            MOBILE_ANDROID_TAG,
            MOBILE_PC_DELEGATED_TAG,
        ],
        "agent_template": mobile_agent_template(),
    }


def _phone_local_plan(tool_id: str) -> dict[str, Any] | None:
    if tool_id in _CLI_DRY_RUN_TOOLS | _CONNECTOR_PAYLOAD_DRY_RUN_TOOLS:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": (
                "implemented_cli_dry_run_pc_execute"
                if tool_id in _CLI_DRY_RUN_TOOLS
                else "implemented_connector_dry_run"
            ),
        }
    if tool_id in _PHONE_AI_MODEL_TOOLS:
        status, requires_approval = _PHONE_AI_MODEL_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart", "mobile-provider-config"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_PROMPT_TOOLS:
        status, requires_approval = _PHONE_PROMPT_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart", "mobile-prompt-store"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_MEMORY_TOOLS:
        status, requires_approval = _PHONE_MEMORY_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart", "mobile-memory-store"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_KNOWLEDGE_TOOLS:
        status, requires_approval = _PHONE_KNOWLEDGE_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart", "mobile-knowledge-store"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_MEDIA_ARTIFACT_TOOLS:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart", "mobile-media-artifact"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": _PHONE_MEDIA_ARTIFACT_TOOLS[tool_id],
        }
    if tool_id in _PHONE_WORKFLOW_TOOLS:
        status, requires_approval = _PHONE_WORKFLOW_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart", "mobile-workflow-record"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_ARTIFACT_WORKSPACE_TOOLS:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": tool_id
            in {"artifact_file_write", "artifact_file_patch", "artifact_file_delete"},
            "implementation_status": "implemented_phone_artifact_workspace",
        }
    if tool_id in _PHONE_ARTIFACT_HTML_TOOLS:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_phone_artifact_html",
        }
    if tool_id in _PHONE_PACKAGE_WEBAPP_TOOLS:
        status, requires_approval = _PHONE_PACKAGE_WEBAPP_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_ARTIFACT_GENERATOR_TOOLS:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": _PHONE_ARTIFACT_GENERATOR_TOOLS[tool_id],
        }
    if tool_id in _PHONE_DOCUMENT_SLIDE_TOOLS:
        status, requires_approval = _PHONE_DOCUMENT_SLIDE_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_JOB_TOOLS:
        status, requires_approval = _PHONE_JOB_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_SHEET_TOOLS:
        status, requires_approval = _PHONE_SHEET_TOOLS[tool_id]
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": requires_approval,
            "implementation_status": status,
        }
    if tool_id in _PHONE_EXPORT_TOOLS:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": _PHONE_EXPORT_TOOLS[tool_id],
        }
    if tool_id == "tool_batch":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_mobile_batch_router",
        }
    if tool_id in {
        "agent_plan",
        "agent_progress",
        "agent_status",
        "tool_calculator",
        "tool_consent_check",
        "tool_consent_confirm",
        "tool_list",
        "tool_names",
        "tool_schema",
        "tool_search",
        "tool_task_board",
        "tool_todo",
    }:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
        }
    if tool_id == "browser_open_url":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIApplication.open",
                "android:Kotlin Intent.ACTION_VIEW",
            ],
            "requires_mobile_approval": False,
        }
    if tool_id in {"media_clipboard_read", "media_clipboard_write"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Flutter Clipboard/Pasteboard bridge",
                "android:Flutter ClipboardManager bridge",
            ],
            "requires_mobile_approval": True,
        }
    if tool_id == "media_file_pick":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIDocumentPickerViewController",
                "android:Kotlin Intent.ACTION_OPEN_DOCUMENT",
            ],
            "requires_mobile_approval": True,
        }
    if tool_id == "media_screenshot":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIWindow screenshot capture",
                "android:Kotlin View drawing cache capture",
            ],
            "requires_mobile_approval": True,
        }
    if tool_id == "media_image_read":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
        }
    if tool_id == "media_image_transform":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIImage resize/encode",
                "android:Kotlin Bitmap resize/encode",
            ],
            "requires_mobile_approval": False,
        }
    if tool_id in {"image_resize", "image_convert"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIImage resize/encode",
                "android:Kotlin Bitmap resize/encode",
            ],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_payload_only",
        }
    if tool_id in {"media_ocr", "ocr_extract"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift Vision VNRecognizeTextRequest",
                "android:Kotlin ML Kit TextRecognition",
            ],
            "requires_mobile_approval": False,
            "implementation_status": (
                "implemented_payload_only_native_ocr"
                if tool_id == "ocr_extract"
                else "implemented_native_ocr_bridge"
            ),
        }
    if tool_id == "media_doc_parse":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_text_documents",
        }
    if tool_id == "media_pdf_parse":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_best_effort_bytes",
        }
    if tool_id in {"pdf_extract", "pdf_extract_tables"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": (
                "implemented_empty_table_fallback"
                if tool_id == "pdf_extract_tables"
                else "implemented_best_effort_bytes"
            ),
        }
    if tool_id in {"artifact_preview", "html_preview", "pdf_preview"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_payload_only_preview",
        }
    if tool_id in {"source_extract", "source_rank"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": (
                "implemented_payload_only" if tool_id == "source_extract" else "implemented"
            ),
        }
    if tool_id == "browser_extract_table":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_payload_only_html",
        }
    if tool_id in {"tts_generate", "tts_generate_local"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_silent_wav_fallback",
        }
    return None


def _is_local_cloudflare_bridge_url(parsed: Any) -> bool:
    if parsed is None:
        return False
    hostname = str(getattr(parsed, "hostname", "") or "").strip().lower()
    return hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".localhost")


def _blocked_reason(service_id: str, tags: set[str], tool: dict[str, Any]) -> str:
    if service_id in _HOST_BOUND_SERVICES:
        return "スマホ単体ではPC/desktop/terminal/workspace系toolを実行できません。PC接続時にPC側runtimeで実行してください。"
    if tags & _HOST_BOUND_TAGS:
        return "このtoolはPC側のhost runtimeまたはworkspaceに依存するため、スマホ単体では実行できません。"
    metadata = tool.get("metadata") if isinstance(tool.get("metadata"), dict) else {}
    runtime_caps = tool.get("requires_runtime_capabilities") or metadata.get("requires_runtime_capabilities")
    if isinstance(runtime_caps, list) and runtime_caps:
        return "このtoolはruntime capabilityが必要なため、スマホ単体では実行できません。"
    if tool.get("enabled") is False:
        return "このtoolはPC側で無効化されています。"
    return ""


def _mobile_port_plan(service_id: str, tags: set[str], tool: dict[str, Any]) -> dict[str, Any]:
    tool_id = str(tool.get("tool_id") or tool.get("name") or "").strip().lower()
    if tool_id in {"media_clipboard_read", "media_clipboard_write"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIPasteboard",
                "android:Kotlin ClipboardManager",
            ],
            "requires_mobile_approval": True,
            "implementation_status": "implemented",
        }
    if tool_id == "media_file_pick":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIDocumentPickerViewController",
                "android:Kotlin Intent.ACTION_OPEN_DOCUMENT",
            ],
            "requires_mobile_approval": True,
            "implementation_status": "implemented",
        }
    if tool_id == "media_screenshot":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIWindow screenshot capture",
                "android:Kotlin View drawing cache capture",
            ],
            "requires_mobile_approval": True,
            "implementation_status": "implemented",
        }
    if tool_id == "media_image_read":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented",
        }
    if tool_id == "media_image_transform":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIImage resize/encode",
                "android:Kotlin Bitmap resize/encode",
            ],
            "requires_mobile_approval": False,
            "implementation_status": "implemented",
        }
    if tool_id in {"image_resize", "image_convert"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift UIImage resize/encode",
                "android:Kotlin Bitmap resize/encode",
            ],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_payload_only",
        }
    if tool_id in {"media_ocr", "ocr_extract"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin"],
            "native_layers": [
                "ios:Swift Vision VNRecognizeTextRequest",
                "android:Kotlin ML Kit TextRecognition",
            ],
            "requires_mobile_approval": False,
            "implementation_status": (
                "implemented_payload_only_native_ocr"
                if tool_id == "ocr_extract"
                else "implemented_native_ocr_bridge"
            ),
        }
    if tool_id == "media_doc_parse":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_text_documents",
        }
    if tool_id == "media_pdf_parse":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_best_effort_bytes",
        }
    if tool_id in {"pdf_extract", "pdf_extract_tables"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": (
                "implemented_empty_table_fallback"
                if tool_id == "pdf_extract_tables"
                else "implemented_best_effort_bytes"
            ),
        }
    if tool_id in {"artifact_preview", "html_preview", "pdf_preview"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_payload_only_preview",
        }
    if tool_id in {"source_extract", "source_rank"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": (
                "implemented_payload_only" if tool_id == "source_extract" else "implemented"
            ),
        }
    if tool_id == "browser_extract_table":
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_payload_only_html",
        }
    if tool_id in {"tts_generate", "tts_generate_local"}:
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "dart"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "implemented_silent_wav_fallback",
        }
    if "media" in tags or tool_id.startswith(("audio_", "image_", "ocr_")):
        return {
            "platforms": ["ios", "android"],
            "runtime_layers": ["flutter", "ios-swift", "android-kotlin", "provider"],
            "native_layers": [
                "ios:Swift media/photo permission bridge",
                "android:Kotlin media permission bridge",
            ],
            "requires_mobile_approval": True,
            "implementation_status": "feasible_needs_picker_permission_or_provider",
        }
    if service_id == "browser" or "browser" in tags:
        return {
            "platforms": [],
            "runtime_layers": ["pc-browser-session"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "pc_browser_session_only",
        }
    if tags & {"desktop", "computer", "computer_use", "workspace", "sandbox", "terminal", "agent_os"}:
        return {
            "platforms": [],
            "runtime_layers": ["pc-defaultspack-runtime"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "pc_only",
        }
    if "agent" in tags:
        return {
            "platforms": [],
            "runtime_layers": ["pc-agent-service"],
            "native_layers": [],
            "requires_mobile_approval": False,
            "implementation_status": "pc_agent_runtime_only",
        }
    return {
        "platforms": [],
        "runtime_layers": ["pc-defaultspack-runtime"],
        "native_layers": [],
        "requires_mobile_approval": False,
        "implementation_status": "pc_delegation_required",
    }


def _platform_tags(plan: dict[str, Any], *, pc_delegated: bool) -> list[str]:
    platforms = {str(item).strip().lower() for item in plan.get("platforms") or []}
    runtime_layers = {str(item).strip().lower() for item in plan.get("runtime_layers") or []}
    tags: list[str] = []
    if "flutter" in runtime_layers or "dart" in runtime_layers:
        tags.append(MOBILE_FLUTTER_TAG)
    if "ios" in platforms:
        tags.append(MOBILE_IOS_TAG)
    if "android" in platforms:
        tags.append(MOBILE_ANDROID_TAG)
    if any("swift" in layer for layer in runtime_layers):
        tags.append(MOBILE_SWIFT_NATIVE_TAG)
    if any("kotlin" in layer for layer in runtime_layers):
        tags.append(MOBILE_KOTLIN_NATIVE_TAG)
    if pc_delegated:
        tags.append(MOBILE_PC_DELEGATED_TAG)
    return tags


def _tag_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {part.strip().lower() for part in value.replace(",", " ").split() if part.strip()}
    if isinstance(value, list):
        return {str(item).strip().lower() for item in value if str(item or "").strip()}
    return set()


def _ordered_tags(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = str(value or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        output.append(tag)
    return output
