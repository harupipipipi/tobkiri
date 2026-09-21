from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK))


def test_mobile_tool_records_tag_compatible_tools() -> None:
    from domain.mobile.tools import (
        MOBILE_CLOUD_DELEGATION_ROUTE,
        MOBILE_TOOL_INVOKE_BASIC_SCOPE,
        MOBILE_TOOL_INVOKE_CLOUD_SCOPE,
        MOBILE_COMPATIBLE_TAG,
        mobile_tool_records,
    )

    records = mobile_tool_records(
        [
            {
                "tool_id": "web_search",
                "name": "web_search",
                "summary": "Search the web",
                "tags": ["web"],
            }
        ]
    )

    assert records[0]["mobile_compatible"] is True
    assert MOBILE_COMPATIBLE_TAG in records[0]["tags"]
    assert records[0]["mobile"]["execution_location"] == "pc"
    assert records[0]["mobile"]["platforms"] == ["ios", "android"]
    assert "pc-defaultspack-runtime" in records[0]["mobile"]["runtime_layers"]
    assert records[0]["callable"] is True
    assert records[0]["execution_route"] == "pc"
    assert records[0]["automatic_routing"]["one_tool_surface"] is True
    assert records[0]["automatic_routing"]["selected_route"] == "pc"
    assert records[0]["automatic_routing"]["pc_delegation_scope"] == MOBILE_TOOL_INVOKE_BASIC_SCOPE
    assert records[0]["automatic_routing"]["cloud_delegation_route"] == MOBILE_CLOUD_DELEGATION_ROUTE
    assert records[0]["automatic_routing"]["cloud_delegation_scope"] == MOBILE_TOOL_INVOKE_CLOUD_SCOPE
    assert "cloud_delegation_status" in records[0]["automatic_routing"]


def test_mobile_tool_records_explain_host_bound_tools() -> None:
    from domain.mobile.tools import MOBILE_COMPATIBLE_TAG, mobile_tool_records

    records = mobile_tool_records(
        [
            {
                "tool_id": "desktop_input",
                "name": "desktop_input",
                "summary": "Control desktop input",
                "tags": ["desktop", "computer_use"],
            }
        ]
    )

    assert records[0]["mobile_compatible"] is False
    assert MOBILE_COMPATIBLE_TAG not in records[0]["tags"]
    assert "スマホ単体では" in records[0]["mobile_unavailable_reason"]
    assert records[0]["mobile"]["implementation_status"] == "pc_only"
    assert records[0]["mobile"]["platforms"] == []
    assert records[0]["callable"] is False
    assert records[0]["execution_route"] == "unavailable"


def test_mobile_tool_records_mark_phone_local_overrides() -> None:
    from domain.mobile.tools import mobile_tool_records

    records = mobile_tool_records(
        [
            {
                "tool_id": "browser_open_url",
                "name": "browser_open_url",
                "summary": "Open a URL",
                "service_id": "browser",
                "tags": ["browser", "tool"],
            },
            {
                "tool_id": "media_clipboard_read",
                "name": "media_clipboard_read",
                "summary": "Read clipboard",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "media_file_pick",
                "name": "media_file_pick",
                "summary": "Pick a phone file",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "media_screenshot",
                "name": "media_screenshot",
                "summary": "Capture a phone screenshot",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "media_image_read",
                "name": "media_image_read",
                "summary": "Read image metadata",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "media_image_transform",
                "name": "media_image_transform",
                "summary": "Transform image bytes",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "image_resize",
                "name": "image_resize",
                "summary": "Resize image bytes",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "image_convert",
                "name": "image_convert",
                "summary": "Convert image bytes",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "media_ocr",
                "name": "media_ocr",
                "summary": "Run OCR",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "ocr_extract",
                "name": "ocr_extract",
                "summary": "Extract OCR text",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "media_doc_parse",
                "name": "media_doc_parse",
                "summary": "Parse a text document",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "media_pdf_parse",
                "name": "media_pdf_parse",
                "summary": "Parse PDF bytes",
                "tags": ["media", "tool"],
            },
            {
                "tool_id": "pdf_extract",
                "name": "pdf_extract",
                "summary": "Extract PDF bytes",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "pdf_extract_tables",
                "name": "pdf_extract_tables",
                "summary": "Extract PDF tables",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "artifact_preview",
                "name": "artifact_preview",
                "summary": "Preview artifact payload",
                "tags": ["preview", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "artifact_file_list",
                "name": "artifact_file_list",
                "summary": "List phone artifacts",
                "tags": ["artifact", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "artifact_file_read",
                "name": "artifact_file_read",
                "summary": "Read phone artifact",
                "tags": ["artifact", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "tool_file_reader",
                "name": "tool_file_reader",
                "summary": "Read phone artifact text",
                "tags": ["tool", "file"],
            },
            {
                "tool_id": "artifact_file_write",
                "name": "artifact_file_write",
                "summary": "Write phone artifact",
                "tags": ["artifact", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "artifact_file_patch",
                "name": "artifact_file_patch",
                "summary": "Patch phone artifact",
                "tags": ["artifact", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "artifact_file_delete",
                "name": "artifact_file_delete",
                "summary": "Delete phone artifact",
                "tags": ["artifact", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "browser_save_page",
                "name": "browser_save_page",
                "summary": "Save HTML page",
                "tags": ["browser", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "webapp_preview",
                "name": "webapp_preview",
                "summary": "Preview phone webapp",
                "tags": ["webapp", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "webapp_lint",
                "name": "webapp_lint",
                "summary": "Lint phone webapp",
                "tags": ["webapp", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "webapp_build",
                "name": "webapp_build",
                "summary": "Build static webapp",
                "tags": ["webapp", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "package_install_plan",
                "name": "package_install_plan",
                "summary": "Plan package install commands",
                "tags": ["sandbox", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "research_report_export",
                "name": "research_report_export",
                "summary": "Export research report",
                "tags": ["research", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "ai_models",
                "name": "ai_models",
                "summary": "List mobile AI models",
                "tags": ["ai", "model", "catalog"],
            },
            {
                "tool_id": "ai_profiles",
                "name": "ai_profiles",
                "summary": "List mobile AI profiles",
                "tags": ["ai", "profile", "catalog"],
            },
            {
                "tool_id": "ai_providers",
                "name": "ai_providers",
                "summary": "List mobile AI providers",
                "tags": ["ai", "provider", "catalog"],
            },
            {
                "tool_id": "ai_get_provider_key_status",
                "name": "ai_get_provider_key_status",
                "summary": "Read mobile provider key status",
                "tags": ["ai", "provider_key"],
            },
            {
                "tool_id": "ai_set_provider_key",
                "name": "ai_set_provider_key",
                "summary": "Set mobile provider key",
                "tags": ["ai", "provider_key"],
            },
            {
                "tool_id": "ai_delete_provider_key",
                "name": "ai_delete_provider_key",
                "summary": "Delete mobile provider key",
                "tags": ["ai", "provider_key"],
            },
            {
                "tool_id": "ai_get_preferred_model",
                "name": "ai_get_preferred_model",
                "summary": "Get preferred mobile model",
                "tags": ["ai", "model_runtime"],
            },
            {
                "tool_id": "ai_set_preferred_model",
                "name": "ai_set_preferred_model",
                "summary": "Set preferred mobile model",
                "tags": ["ai", "model_runtime"],
            },
            {
                "tool_id": "ai_get_thinking_level",
                "name": "ai_get_thinking_level",
                "summary": "Get thinking level",
                "tags": ["ai", "model_runtime"],
            },
            {
                "tool_id": "ai_set_thinking_level",
                "name": "ai_set_thinking_level",
                "summary": "Set thinking level",
                "tags": ["ai", "model_runtime"],
            },
            {
                "tool_id": "ai_get_effective_thinking_level",
                "name": "ai_get_effective_thinking_level",
                "summary": "Get effective thinking level",
                "tags": ["ai", "model_runtime"],
            },
            {
                "tool_id": "ai_normalize_thinking_level",
                "name": "ai_normalize_thinking_level",
                "summary": "Normalize thinking level",
                "tags": ["ai", "model_runtime"],
            },
            {
                "tool_id": "ai_validate_model_params",
                "name": "ai_validate_model_params",
                "summary": "Validate model params",
                "tags": ["ai", "model_runtime"],
            },
            {
                "tool_id": "ai_recommend_model",
                "name": "ai_recommend_model",
                "summary": "Recommend mobile model",
                "tags": ["ai", "model", "routing"],
            },
            {
                "tool_id": "ai_route_model",
                "name": "ai_route_model",
                "summary": "Route mobile model",
                "tags": ["ai", "model", "routing"],
            },
            {
                "tool_id": "ai_explain_model_choice",
                "name": "ai_explain_model_choice",
                "summary": "Explain mobile model routing",
                "tags": ["ai", "model", "routing"],
            },
            {
                "tool_id": "prompt_validate_template",
                "name": "prompt_validate_template",
                "summary": "Validate prompt template",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_render",
                "name": "prompt_render",
                "summary": "Render prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_lint_prompt",
                "name": "prompt_lint_prompt",
                "summary": "Lint prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_compact_prompt",
                "name": "prompt_compact_prompt",
                "summary": "Compact prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_test",
                "name": "prompt_test",
                "summary": "Test prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_system_get",
                "name": "prompt_system_get",
                "summary": "Get system prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_system_set",
                "name": "prompt_system_set",
                "summary": "Set system prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_list",
                "name": "prompt_list",
                "summary": "List prompts",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_create",
                "name": "prompt_create",
                "summary": "Create prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_update",
                "name": "prompt_update",
                "summary": "Update prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_delete",
                "name": "prompt_delete",
                "summary": "Delete prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_active",
                "name": "prompt_active",
                "summary": "Active prompts",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_load_effective",
                "name": "prompt_load_effective",
                "summary": "Load effective prompt",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_resolve_for_conversation",
                "name": "prompt_resolve_for_conversation",
                "summary": "Resolve prompt for conversation",
                "tags": ["prompt"],
            },
            {
                "tool_id": "prompt_preview_toggle",
                "name": "prompt_preview_toggle",
                "summary": "Preview prompt toggle",
                "tags": ["prompt"],
            },
            {
                "tool_id": "memory_store",
                "name": "memory_store",
                "summary": "Store memory",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_list",
                "name": "memory_list",
                "summary": "List memory",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_recall",
                "name": "memory_recall",
                "summary": "Recall memory",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_update",
                "name": "memory_update",
                "summary": "Update memory",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_delete",
                "name": "memory_delete",
                "summary": "Delete memory",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_compact",
                "name": "memory_compact",
                "summary": "Compact memory",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_project_context",
                "name": "memory_project_context",
                "summary": "Project memory context",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_resolve_for_agent",
                "name": "memory_resolve_for_agent",
                "summary": "Resolve memory for agent",
                "tags": ["memory"],
            },
            {
                "tool_id": "memory_memo",
                "name": "memory_memo",
                "summary": "Dispatch memo operation",
                "tags": ["memory", "memo"],
            },
            {
                "tool_id": "memory_memo_folders",
                "name": "memory_memo_folders",
                "summary": "Manage memo folders",
                "tags": ["memory", "memo"],
            },
            {
                "tool_id": "memory_memo_notes",
                "name": "memory_memo_notes",
                "summary": "Manage memo notes",
                "tags": ["memory", "memo"],
            },
            {
                "tool_id": "knowledge_create",
                "name": "knowledge_create",
                "summary": "Create knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_get",
                "name": "knowledge_get",
                "summary": "Get knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_list",
                "name": "knowledge_list",
                "summary": "List knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_update",
                "name": "knowledge_update",
                "summary": "Update knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_delete",
                "name": "knowledge_delete",
                "summary": "Delete knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_search",
                "name": "knowledge_search",
                "summary": "Search knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_import_file",
                "name": "knowledge_import_file",
                "summary": "Import knowledge from a phone file",
                "tags": ["knowledge", "artifact_workspace"],
            },
            {
                "tool_id": "knowledge_import_url",
                "name": "knowledge_import_url",
                "summary": "Import knowledge from a URL reference",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_attach_to_project",
                "name": "knowledge_attach_to_project",
                "summary": "Attach knowledge to a project",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_index",
                "name": "knowledge_index",
                "summary": "Index knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "knowledge_reindex",
                "name": "knowledge_reindex",
                "summary": "Reindex knowledge",
                "tags": ["knowledge"],
            },
            {
                "tool_id": "workflow_define",
                "name": "workflow_define",
                "summary": "Define a phone-local workflow",
                "tags": ["workflow", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "workflow_run",
                "name": "workflow_run",
                "summary": "Run a phone-local workflow",
                "tags": ["workflow", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "workflow_status",
                "name": "workflow_status",
                "summary": "Read phone-local workflow status",
                "tags": ["workflow", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "workflow_cancel",
                "name": "workflow_cancel",
                "summary": "Cancel a phone-local workflow",
                "tags": ["workflow", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "workflow_retry",
                "name": "workflow_retry",
                "summary": "Retry a phone-local workflow",
                "tags": ["workflow", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "tool_batch",
                "name": "tool_batch",
                "summary": "Invoke multiple tools",
                "tags": ["tool", "broker", "batch"],
            },
            {
                "tool_id": "job_create",
                "name": "job_create",
                "summary": "Create local job",
                "tags": ["job", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "job_status",
                "name": "job_status",
                "summary": "Read local job status",
                "tags": ["job", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "job_history",
                "name": "job_history",
                "summary": "Read local job history",
                "tags": ["job", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "job_artifacts",
                "name": "job_artifacts",
                "summary": "List local job artifacts",
                "tags": ["job", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "job_cancel",
                "name": "job_cancel",
                "summary": "Cancel local job",
                "tags": ["job", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "job_resume",
                "name": "job_resume",
                "summary": "Resume local job",
                "tags": ["job", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "project_scaffold",
                "name": "project_scaffold",
                "summary": "Create static webapp files",
                "tags": ["webapp", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "doc_create",
                "name": "doc_create",
                "summary": "Create document artifact",
                "tags": ["document", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "doc_update",
                "name": "doc_update",
                "summary": "Update document artifact",
                "tags": ["document", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "slides_create",
                "name": "slides_create",
                "summary": "Create slides artifact",
                "tags": ["presentation", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "slides_from_markdown",
                "name": "slides_from_markdown",
                "summary": "Create slides from markdown",
                "tags": ["presentation", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "slides_update",
                "name": "slides_update",
                "summary": "Update slides artifact",
                "tags": ["presentation", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "slides_export",
                "name": "slides_export",
                "summary": "Export slides artifact",
                "tags": ["export", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "chart_create",
                "name": "chart_create",
                "summary": "Create chart artifact",
                "tags": ["spreadsheet", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "sheet_create",
                "name": "sheet_create",
                "summary": "Create sheet artifact",
                "tags": ["spreadsheet", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "sheet_read",
                "name": "sheet_read",
                "summary": "Read sheet artifact",
                "tags": ["spreadsheet", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "sheet_analyze",
                "name": "sheet_analyze",
                "summary": "Analyze sheet artifact",
                "tags": ["spreadsheet", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "sheet_update",
                "name": "sheet_update",
                "summary": "Update sheet artifact",
                "tags": ["spreadsheet", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "sheet_export",
                "name": "sheet_export",
                "summary": "Export sheet artifact",
                "tags": ["export", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "artifact_zip",
                "name": "artifact_zip",
                "summary": "Create artifact zip",
                "tags": ["artifact", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "artifact_export",
                "name": "artifact_export",
                "summary": "Export artifact",
                "tags": ["export", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "static_site_export",
                "name": "static_site_export",
                "summary": "Export static site",
                "tags": ["webapp", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "webapp_export_static",
                "name": "webapp_export_static",
                "summary": "Export webapp static",
                "tags": ["webapp", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "doc_export",
                "name": "doc_export",
                "summary": "Export document artifact",
                "tags": ["document", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "pdf_export",
                "name": "pdf_export",
                "summary": "Export PDF artifact",
                "tags": ["export", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "doc_to_pdf",
                "name": "doc_to_pdf",
                "summary": "Export document to PDF",
                "tags": ["document", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "html_preview",
                "name": "html_preview",
                "summary": "Preview HTML payload",
                "tags": ["preview", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "pdf_preview",
                "name": "pdf_preview",
                "summary": "Preview PDF payload",
                "tags": ["preview", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "image_render",
                "name": "image_render",
                "summary": "Render image artifact",
                "tags": ["preview", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "image_generate_local_or_provider",
                "name": "image_generate_local_or_provider",
                "summary": "Generate image placeholder",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "audio_transcribe",
                "name": "audio_transcribe",
                "summary": "Transcribe audio",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "audio_transcribe_local",
                "name": "audio_transcribe_local",
                "summary": "Transcribe audio locally",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "source_extract",
                "name": "source_extract",
                "summary": "Extract source payload",
                "tags": ["research", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "source_rank",
                "name": "source_rank",
                "summary": "Rank source snippets",
                "tags": ["research", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "github_search",
                "name": "github_search",
                "summary": "Plan GitHub search",
                "tags": ["connector", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "slack_send",
                "name": "slack_send",
                "summary": "Prepare Slack send",
                "tags": ["connector", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "browser_extract_table",
                "name": "browser_extract_table",
                "summary": "Extract HTML table rows",
                "tags": ["browser", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "tts_generate",
                "name": "tts_generate",
                "summary": "Generate fallback audio",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
            {
                "tool_id": "tts_generate_local",
                "name": "tts_generate_local",
                "summary": "Generate fallback audio locally",
                "tags": ["media", "agent_os", "artifact_workspace"],
            },
        ]
    )

    by_id = {record["tool_id"]: record for record in records}
    assert by_id["browser_open_url"]["mobile_compatible"] is True
    assert by_id["browser_open_url"]["execution_route"] == "phone"
    assert by_id["browser_open_url"]["mobile"]["requires_pc"] is False
    assert "ios-swift" in by_id["browser_open_url"]["mobile"]["runtime_layers"]

    assert by_id["media_clipboard_read"]["mobile_compatible"] is True
    assert by_id["media_clipboard_read"]["execution_route"] == "phone"
    assert by_id["media_clipboard_read"]["mobile"]["requires_mobile_approval"] is True
    assert by_id["media_clipboard_read"]["mobile"]["implementation_status"] == "implemented"

    assert by_id["media_file_pick"]["mobile_compatible"] is True
    assert by_id["media_file_pick"]["execution_route"] == "phone"
    assert by_id["media_file_pick"]["mobile"]["requires_mobile_approval"] is True
    assert by_id["media_file_pick"]["mobile"]["implementation_status"] == "implemented"
    assert "ios-swift" in by_id["media_file_pick"]["mobile"]["runtime_layers"]
    assert "android-kotlin" in by_id["media_file_pick"]["mobile"]["runtime_layers"]

    assert by_id["media_screenshot"]["mobile_compatible"] is True
    assert by_id["media_screenshot"]["execution_route"] == "phone"
    assert by_id["media_screenshot"]["mobile"]["requires_mobile_approval"] is True
    assert by_id["media_screenshot"]["mobile"]["implementation_status"] == "implemented"
    assert "ios-swift" in by_id["media_screenshot"]["mobile"]["runtime_layers"]
    assert "android-kotlin" in by_id["media_screenshot"]["mobile"]["runtime_layers"]

    assert by_id["media_image_read"]["mobile_compatible"] is True
    assert by_id["media_image_read"]["execution_route"] == "phone"
    assert by_id["media_image_read"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["media_image_read"]["mobile"]["implementation_status"] == "implemented"
    assert "flutter" in by_id["media_image_read"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["media_image_read"]["mobile"]["runtime_layers"]

    assert by_id["media_image_transform"]["mobile_compatible"] is True
    assert by_id["media_image_transform"]["execution_route"] == "phone"
    assert by_id["media_image_transform"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["media_image_transform"]["mobile"]["implementation_status"] == "implemented"
    assert "ios-swift" in by_id["media_image_transform"]["mobile"]["runtime_layers"]
    assert "android-kotlin" in by_id["media_image_transform"]["mobile"]["runtime_layers"]

    assert by_id["image_resize"]["mobile_compatible"] is True
    assert by_id["image_resize"]["execution_route"] == "phone"
    assert by_id["image_resize"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["image_resize"]["mobile"]["implementation_status"] == "implemented_payload_only"
    assert "ios-swift" in by_id["image_resize"]["mobile"]["runtime_layers"]
    assert "android-kotlin" in by_id["image_resize"]["mobile"]["runtime_layers"]

    assert by_id["image_convert"]["mobile_compatible"] is True
    assert by_id["image_convert"]["execution_route"] == "phone"
    assert by_id["image_convert"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["image_convert"]["mobile"]["implementation_status"] == "implemented_payload_only"
    assert "ios-swift" in by_id["image_convert"]["mobile"]["runtime_layers"]
    assert "android-kotlin" in by_id["image_convert"]["mobile"]["runtime_layers"]

    assert by_id["media_ocr"]["mobile_compatible"] is True
    assert by_id["media_ocr"]["execution_route"] == "phone"
    assert by_id["media_ocr"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["media_ocr"]["mobile"]["implementation_status"] == "implemented_native_ocr_bridge"
    assert "ios-swift" in by_id["media_ocr"]["mobile"]["runtime_layers"]
    assert "android-kotlin" in by_id["media_ocr"]["mobile"]["runtime_layers"]

    assert by_id["ocr_extract"]["mobile_compatible"] is True
    assert by_id["ocr_extract"]["execution_route"] == "phone"
    assert by_id["ocr_extract"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["ocr_extract"]["mobile"]["implementation_status"] == "implemented_payload_only_native_ocr"
    assert "ios-swift" in by_id["ocr_extract"]["mobile"]["runtime_layers"]
    assert "android-kotlin" in by_id["ocr_extract"]["mobile"]["runtime_layers"]

    assert by_id["media_doc_parse"]["mobile_compatible"] is True
    assert by_id["media_doc_parse"]["execution_route"] == "phone"
    assert by_id["media_doc_parse"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["media_doc_parse"]["mobile"]["implementation_status"] == "implemented_text_documents"
    assert "flutter" in by_id["media_doc_parse"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["media_doc_parse"]["mobile"]["runtime_layers"]

    assert by_id["media_pdf_parse"]["mobile_compatible"] is True
    assert by_id["media_pdf_parse"]["execution_route"] == "phone"
    assert by_id["media_pdf_parse"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["media_pdf_parse"]["mobile"]["implementation_status"] == "implemented_best_effort_bytes"
    assert "flutter" in by_id["media_pdf_parse"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["media_pdf_parse"]["mobile"]["runtime_layers"]

    assert by_id["pdf_extract"]["mobile_compatible"] is True
    assert by_id["pdf_extract"]["execution_route"] == "phone"
    assert by_id["pdf_extract"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["pdf_extract"]["mobile"]["implementation_status"] == "implemented_best_effort_bytes"
    assert "flutter" in by_id["pdf_extract"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["pdf_extract"]["mobile"]["runtime_layers"]

    assert by_id["pdf_extract_tables"]["mobile_compatible"] is True
    assert by_id["pdf_extract_tables"]["execution_route"] == "phone"
    assert by_id["pdf_extract_tables"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["pdf_extract_tables"]["mobile"]["implementation_status"] == "implemented_empty_table_fallback"
    assert "flutter" in by_id["pdf_extract_tables"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["pdf_extract_tables"]["mobile"]["runtime_layers"]

    assert by_id["artifact_preview"]["mobile_compatible"] is True
    assert by_id["artifact_preview"]["execution_route"] == "phone"
    assert by_id["artifact_preview"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["artifact_preview"]["mobile"]["implementation_status"] == "implemented_payload_only_preview"
    assert "flutter" in by_id["artifact_preview"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["artifact_preview"]["mobile"]["runtime_layers"]

    for tool_id, requires_approval in {
        "artifact_file_list": False,
        "artifact_file_read": False,
        "artifact_file_write": True,
        "artifact_file_patch": True,
        "artifact_file_delete": True,
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert (
            by_id[tool_id]["mobile"]["implementation_status"]
            == "implemented_phone_artifact_workspace"
        )
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id in ("browser_save_page", "webapp_preview", "webapp_lint"):
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is False
        assert (
            by_id[tool_id]["mobile"]["implementation_status"]
            == "implemented_phone_artifact_html"
        )
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]

    assert by_id["tool_batch"]["mobile_compatible"] is True
    assert by_id["tool_batch"]["execution_route"] == "phone"
    assert by_id["tool_batch"]["mobile"]["requires_mobile_approval"] is False
    assert (
        by_id["tool_batch"]["mobile"]["implementation_status"]
        == "implemented_mobile_batch_router"
    )
    assert "flutter" in by_id["tool_batch"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["tool_batch"]["mobile"]["runtime_layers"]

    for tool_id, (status, requires_approval) in {
        "package_install_plan": ("implemented_phone_install_plan", False),
        "webapp_build": ("implemented_phone_static_webapp_build_plan", True),
        "research_report_export": ("implemented_phone_research_report_export", False),
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, (status, requires_approval) in {
        "ai_models": ("implemented_phone_ai_catalog", False),
        "ai_profiles": ("implemented_phone_ai_catalog", False),
        "ai_providers": ("implemented_phone_ai_catalog", False),
        "ai_get_provider_key_status": (
            "implemented_phone_ai_provider_key_status",
            False,
        ),
        "ai_set_provider_key": ("implemented_phone_ai_provider_key", True),
        "ai_delete_provider_key": ("implemented_phone_ai_provider_key", True),
        "ai_get_preferred_model": ("implemented_phone_ai_model_settings", False),
        "ai_set_preferred_model": ("implemented_phone_ai_model_settings", True),
        "ai_get_thinking_level": ("implemented_phone_ai_model_settings", False),
        "ai_set_thinking_level": ("implemented_phone_ai_model_settings", True),
        "ai_get_effective_thinking_level": (
            "implemented_phone_ai_model_settings",
            False,
        ),
        "ai_normalize_thinking_level": (
            "implemented_phone_ai_model_settings",
            False,
        ),
        "ai_validate_model_params": ("implemented_phone_ai_param_validation", False),
        "ai_recommend_model": ("implemented_phone_ai_routing_hint", False),
        "ai_route_model": ("implemented_phone_ai_routing_hint", False),
        "ai_explain_model_choice": ("implemented_phone_ai_routing_hint", False),
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-provider-config" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, (status, requires_approval) in {
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
        "prompt_resolve_for_conversation": (
            "implemented_phone_prompt_effective",
            False,
        ),
        "prompt_preview_toggle": ("implemented_phone_prompt_preview", False),
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-prompt-store" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, (status, requires_approval) in {
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
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-memory-store" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, (status, requires_approval) in {
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
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-knowledge-store" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, requires_approval in {
        "workflow_define": False,
        "workflow_run": True,
        "workflow_status": False,
        "workflow_cancel": True,
        "workflow_retry": True,
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert (
            by_id[tool_id]["mobile"]["implementation_status"]
            == "implemented_phone_workflow_record"
        )
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-workflow-record" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "ios-swift" not in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "android-kotlin" not in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-swift-native" not in by_id[tool_id]["tags"]
        assert "mobile-kotlin-native" not in by_id[tool_id]["tags"]

    for tool_id, requires_approval in {
        "job_create": False,
        "job_status": False,
        "job_history": False,
        "job_artifacts": False,
        "job_cancel": True,
        "job_resume": True,
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert (
            by_id[tool_id]["mobile"]["implementation_status"]
            == "implemented_phone_job_record"
        )
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, status in {
        "project_scaffold": "implemented_phone_artifact_scaffold",
        "doc_create": "implemented_phone_document_text",
        "slides_create": "implemented_phone_slide_outline",
        "slides_from_markdown": "implemented_phone_slide_outline",
        "chart_create": "implemented_phone_svg_chart",
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is False
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "ios-swift" not in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "android-kotlin" not in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-swift-native" not in by_id[tool_id]["tags"]
        assert "mobile-kotlin-native" not in by_id[tool_id]["tags"]

    for tool_id, (status, requires_approval) in {
        "doc_update": ("implemented_phone_document_text", True),
        "slides_update": ("implemented_phone_slide_outline", True),
        "slides_export": ("implemented_phone_slide_export", False),
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "ios-swift" not in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "android-kotlin" not in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, (status, requires_approval) in {
        "sheet_create": ("implemented_phone_sheet_text", False),
        "sheet_read": ("implemented_phone_sheet_text", False),
        "sheet_analyze": ("implemented_phone_sheet_text", False),
        "sheet_update": ("implemented_phone_sheet_text", True),
        "sheet_export": ("implemented_phone_sheet_export", False),
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is requires_approval
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, status in {
        "artifact_zip": "implemented_phone_zip_base64",
        "artifact_export": "implemented_phone_artifact_export",
        "static_site_export": "implemented_phone_zip_base64",
        "webapp_export_static": "implemented_phone_zip_base64",
        "doc_export": "implemented_phone_document_export",
        "pdf_export": "pc_delegation_required_binary_export",
        "doc_to_pdf": "pc_delegation_required_binary_export",
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is False
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id in ("html_preview", "pdf_preview"):
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is False
        assert (
            by_id[tool_id]["mobile"]["implementation_status"]
            == "implemented_payload_only_preview"
        )
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]

    for tool_id, status in {
        "image_render": "implemented_phone_svg_image_render",
        "image_generate_local_or_provider": "implemented_phone_svg_image_placeholder",
        "audio_transcribe": "implemented_phone_audio_transcribe_payload",
        "audio_transcribe_local": "implemented_phone_audio_transcribe_payload",
        "tool_file_reader": "implemented_phone_artifact_file_reader",
    }.items():
        assert by_id[tool_id]["mobile_compatible"] is True
        assert by_id[tool_id]["execution_route"] == "phone"
        assert by_id[tool_id]["mobile"]["requires_mobile_approval"] is False
        assert by_id[tool_id]["mobile"]["implementation_status"] == status
        assert "flutter" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "dart" in by_id[tool_id]["mobile"]["runtime_layers"]
        assert "mobile-media-artifact" in by_id[tool_id]["mobile"]["runtime_layers"]

    assert by_id["source_extract"]["mobile_compatible"] is True
    assert by_id["source_extract"]["execution_route"] == "phone"
    assert by_id["source_extract"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["source_extract"]["mobile"]["implementation_status"] == "implemented_payload_only"
    assert "flutter" in by_id["source_extract"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["source_extract"]["mobile"]["runtime_layers"]

    assert by_id["source_rank"]["mobile_compatible"] is True
    assert by_id["source_rank"]["execution_route"] == "phone"
    assert by_id["source_rank"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["source_rank"]["mobile"]["implementation_status"] == "implemented"
    assert "flutter" in by_id["source_rank"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["source_rank"]["mobile"]["runtime_layers"]

    assert by_id["github_search"]["mobile_compatible"] is True
    assert by_id["github_search"]["execution_route"] == "phone"
    assert by_id["github_search"]["mobile"]["requires_mobile_approval"] is False
    assert (
        by_id["github_search"]["mobile"]["implementation_status"]
        == "implemented_cli_dry_run_pc_execute"
    )
    assert "flutter" in by_id["github_search"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["github_search"]["mobile"]["runtime_layers"]

    assert by_id["slack_send"]["mobile_compatible"] is True
    assert by_id["slack_send"]["execution_route"] == "phone"
    assert by_id["slack_send"]["mobile"]["requires_mobile_approval"] is False
    assert (
        by_id["slack_send"]["mobile"]["implementation_status"]
        == "implemented_connector_dry_run"
    )
    assert "flutter" in by_id["slack_send"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["slack_send"]["mobile"]["runtime_layers"]

    assert by_id["browser_extract_table"]["mobile_compatible"] is True
    assert by_id["browser_extract_table"]["execution_route"] == "phone"
    assert by_id["browser_extract_table"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["browser_extract_table"]["mobile"]["implementation_status"] == "implemented_payload_only_html"
    assert "flutter" in by_id["browser_extract_table"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["browser_extract_table"]["mobile"]["runtime_layers"]

    assert by_id["tts_generate"]["mobile_compatible"] is True
    assert by_id["tts_generate"]["execution_route"] == "phone"
    assert by_id["tts_generate"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["tts_generate"]["mobile"]["implementation_status"] == "implemented_silent_wav_fallback"
    assert "flutter" in by_id["tts_generate"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["tts_generate"]["mobile"]["runtime_layers"]

    assert by_id["tts_generate_local"]["mobile_compatible"] is True
    assert by_id["tts_generate_local"]["execution_route"] == "phone"
    assert by_id["tts_generate_local"]["mobile"]["requires_mobile_approval"] is False
    assert by_id["tts_generate_local"]["mobile"]["implementation_status"] == "implemented_silent_wav_fallback"
    assert "flutter" in by_id["tts_generate_local"]["mobile"]["runtime_layers"]
    assert "dart" in by_id["tts_generate_local"]["mobile"]["runtime_layers"]


def test_mobile_tool_summary_includes_defaultspack_agent_template() -> None:
    from domain.mobile.tools import mobile_tool_records, mobile_tool_summary

    records = mobile_tool_records([{"tool_id": "web_search", "name": "web_search"}])
    summary = mobile_tool_summary(records)

    assert summary["compatible_count"] == 1
    assert summary["agent_template"]["template_id"] == "rumi.composer.default"
    assert summary["agent_template"]["ai_input_id"] == "rumi.composer.default:default_ai_input"
    assert summary["platform_tags"]["ios"] == "mobile-ios"
    assert summary["platform_tags"]["android"] == "mobile-android"
    assert summary["platform_tags"]["swift"] == "mobile-swift-native"
    assert summary["platform_tags"]["kotlin"] == "mobile-kotlin-native"
    assert summary["tool_surface"]["mode"] == "unified"
    assert summary["tool_surface"]["one_tool_surface"] is True
