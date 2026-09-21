import 'dart:convert';
import 'dart:io';
import 'dart:math' as math;
import 'dart:typed_data';

import 'package:uuid/uuid.dart';

import '../../platform/platform_services.dart';
import '../../settings/api_config_store.dart';
import '../../settings/defaultspack_mobile_providers.g.dart';
import 'defaultspack_tool_agent_manifest.g.dart';

const mobileCompatibleTag = 'mobile-compatible';
const mobileFlutterTag = 'mobile-flutter';
const mobileIosTag = 'mobile-ios';
const mobileAndroidTag = 'mobile-android';
const mobileSwiftNativeTag = 'mobile-swift-native';
const mobileKotlinNativeTag = 'mobile-kotlin-native';
const mobilePcDelegatedTag = 'pc-delegated';
const mobileAgentTemplateId = 'rumi.composer.default';
const mobileAgentAiInputId = 'rumi.composer.default:default_ai_input';
const mobileAgentToolPolicyId = 'rumi.composer.default:default_tools';
const assistantProgressToolName = 'assistant_progress';
const assistantProgressDisplayName = '作業状況';
const mobileAssistantProgressSystemInstruction =
    'Internal progress tool: assistant_progress is only for short user-visible status, not reasoning. '
    'Call it at most at phase changes, important discoveries, failures, or final verification. '
    'Do not call it before every tool. Do not include hidden reasoning, analysis, or chain-of-thought. '
    'Keep summary and next_action under 120 characters. A normal external tool should occur between repeated progress updates unless you are finalizing or blocked.';

const _assistantProgressTextLimit = 120;
const _assistantProgressRelatedToolLimit = 4;
const _toolBatchMaxCalls = 8;
const _workflowMaxSteps = 20;
const _workflowStepOutputPreviewLimit = 2000;
const _assistantProgressPhases = <String>{
  'inspect',
  'change',
  'verify',
  'recover',
  'finalize',
};
const _assistantProgressStatuses = <String>{
  'active',
  'completed',
  'blocked',
};
const _taskBoardDefaultColumns = <String>['Backlog', 'Doing', 'Review', 'Done'];
const _mobileConsentCategories = <String, Map<String, Object>>{
  'investment': {
    'keywords': <String>[
      '投資',
      '株',
      '株式',
      '株価',
      '銘柄',
      'ポートフォリオ',
      '資産運用',
      '利回り',
      '配当',
      '投資信託',
      'ファンド',
      'fx',
      '為替',
      '仮想通貨',
      '暗号資産',
      'ビットコイン',
      'etf',
      'nisa',
      'ideco',
      '信用取引',
      '空売り',
      'investment',
      'stock',
      'portfolio',
      'dividend',
      'fund',
      'forex',
      'crypto',
      'bitcoin',
      'trading',
    ],
    'disclaimer':
        'この回答は一般的な情報提供のみを目的としており、特定の金融商品の購入・売却を推奨するものではありません。投資判断はご自身の責任で行ってください。',
  },
  'tax': {
    'keywords': <String>[
      '税金',
      '確定申告',
      '所得税',
      '住民税',
      '消費税',
      '法人税',
      '相続税',
      '贈与税',
      '控除',
      '節税',
      '税務',
      '年末調整',
      '源泉徴収',
      '経費',
      '減価償却',
      'tax',
      'deduction',
      'income tax',
      'tax return',
    ],
    'disclaimer':
        'この回答は一般的な税務情報の提供を目的としており、個別の税務アドバイスではありません。具体的な税務判断については、税理士等の専門家にご相談ください。',
  },
  'medical': {
    'keywords': <String>[
      '診断',
      '治療',
      '処方',
      '薬',
      '服薬',
      '投薬',
      '症状',
      '病気',
      '疾患',
      '手術',
      '副作用',
      '医療',
      '医師',
      '病院',
      'クリニック',
      'diagnosis',
      'treatment',
      'prescription',
      'medication',
      'symptom',
      'disease',
      'surgery',
      'side effect',
    ],
    'disclaimer':
        'この回答は一般的な医療情報の提供を目的としており、医学的な診断・治療の代替となるものではありません。健康上の問題については、必ず医師にご相談ください。',
  },
  'legal': {
    'keywords': <String>[
      '訴訟',
      '裁判',
      '弁護士',
      '法律相談',
      '契約書',
      '損害賠償',
      '慰謝料',
      '示談',
      '告訴',
      '起訴',
      '法的',
      '判例',
      '法令',
      '条文',
      '権利',
      'lawsuit',
      'attorney',
      'legal advice',
      'contract',
      'liability',
      'damages',
      'litigation',
    ],
    'disclaimer':
        'この回答は一般的な法律情報の提供を目的としており、個別の法的アドバイスではありません。具体的な法律問題については、弁護士等の専門家にご相談ください。',
  },
};
const _defaultMobilePlatforms = <String>['ios', 'android'];
const _flutterRuntimeLayers = <String>['flutter', 'dart'];
const _phoneMediaArtifactRuntimeLayers = <String>[
  'flutter',
  'dart',
  'mobile-media-artifact',
];
const _nativeUrlRuntimeLayers = <String>[
  'flutter',
  'ios-swift',
  'android-kotlin',
];
const _nativeMediaPickerRuntimeLayers = <String>[
  'flutter',
  'ios-swift',
  'android-kotlin',
];
const _nativeScreenshotRuntimeLayers = <String>[
  'flutter',
  'ios-swift',
  'android-kotlin',
];
const _nativeImageTransformRuntimeLayers = <String>[
  'flutter',
  'ios-swift',
  'android-kotlin',
];
const _nativeOcrRuntimeLayers = <String>[
  'flutter',
  'ios-swift',
  'android-kotlin',
];
const _defaultMediaPickMaxBytes = 4 * 1024 * 1024;
const _hardMediaPickMaxBytes = 8 * 1024 * 1024;
const _defaultScreenshotMaxBytes = 6 * 1024 * 1024;
const _hardScreenshotMaxBytes = 12 * 1024 * 1024;
const _defaultScreenshotMaxDimension = 1600;
const _hardScreenshotMaxDimension = 4096;
const _defaultImageReadMaxBytes = 8 * 1024 * 1024;
const _hardImageReadMaxBytes = 16 * 1024 * 1024;
const _defaultImageTransformOutputMaxBytes = 8 * 1024 * 1024;
const _hardImageTransformOutputMaxBytes = 16 * 1024 * 1024;
const _hardImageTransformInputMaxBytes = 24 * 1024 * 1024;
const _defaultImageTransformMaxDimension = 2048;
const _hardImageTransformMaxDimension = 4096;
const _defaultOcrMaxBytes = 8 * 1024 * 1024;
const _hardOcrMaxBytes = 16 * 1024 * 1024;
const _defaultDocParseMaxBytes = 2 * 1024 * 1024;
const _hardDocParseMaxBytes = 8 * 1024 * 1024;
const _defaultDocParseMaxChars = 120000;
const _hardDocParseMaxChars = 400000;
const _defaultPdfParseMaxBytes = 8 * 1024 * 1024;
const _hardPdfParseMaxBytes = 16 * 1024 * 1024;
const _defaultPdfParseMaxChars = 120000;
const _hardPdfParseMaxChars = 400000;
const _defaultSourceExtractMaxChars = 120000;
const _hardSourceExtractMaxChars = 400000;
const _defaultArtifactPreviewMaxBytes = 8 * 1024 * 1024;
const _hardArtifactPreviewMaxBytes = 16 * 1024 * 1024;
const _defaultArtifactPreviewMaxChars = 40000;
const _hardArtifactPreviewMaxChars = 120000;
const _defaultHtmlTableMaxRows = 200;
const _hardHtmlTableMaxRows = 1000;
const _defaultHtmlTableMaxTables = 20;
const _hardHtmlTableMaxTables = 100;
const _defaultTtsFallbackDurationMs = 100;
const _hardTtsFallbackDurationMs = 30000;
const _defaultTtsFallbackSampleRate = 16000;
const _hardTtsFallbackSampleRate = 48000;
const _minTtsFallbackSampleRate = 8000;
const _mobileCliDryRunToolIds = <String>{
  'github_search',
  'github_pr_create',
  'github_issue_create',
  'github_issue_update',
  'github_issue_list',
  'linear_issue_sync',
  'jira_issue_sync',
};
const _mobileConnectorPayloadDryRunToolIds = <String>{
  'gmail_search',
  'gmail_draft',
  'calendar_create',
  'drive_create',
  'drive_export',
  'slack_send',
  'discord_send',
  'line_push',
};
const _phoneAiModelToolIds = <String>{
  'ai_models',
  'ai_profiles',
  'ai_providers',
  'ai_get_provider_key_status',
  'ai_set_provider_key',
  'ai_delete_provider_key',
  'ai_get_preferred_model',
  'ai_set_preferred_model',
  'ai_get_thinking_level',
  'ai_set_thinking_level',
  'ai_get_effective_thinking_level',
  'ai_normalize_thinking_level',
  'ai_validate_model_params',
  'ai_recommend_model',
  'ai_route_model',
  'ai_explain_model_choice',
};
const _phoneAiModelMutationToolIds = <String>{
  'ai_set_provider_key',
  'ai_delete_provider_key',
  'ai_set_preferred_model',
  'ai_set_thinking_level',
};
const _phonePromptToolIds = <String>{
  'prompt_active',
  'prompt_compact_prompt',
  'prompt_create',
  'prompt_delete',
  'prompt_lint_prompt',
  'prompt_list',
  'prompt_load_effective',
  'prompt_preview_toggle',
  'prompt_render',
  'prompt_resolve_for_conversation',
  'prompt_system_get',
  'prompt_system_set',
  'prompt_test',
  'prompt_update',
  'prompt_validate_template',
};
const _phonePromptMutationToolIds = <String>{
  'prompt_create',
  'prompt_delete',
  'prompt_system_set',
  'prompt_update',
};
const _phoneMemoryToolIds = <String>{
  'memory_store',
  'memory_list',
  'memory_recall',
  'memory_update',
  'memory_delete',
  'memory_compact',
  'memory_project_context',
  'memory_resolve_for_agent',
  'memory_memo',
  'memory_memo_folders',
  'memory_memo_notes',
};
const _phoneMemoryMutationToolIds = <String>{
  'memory_store',
  'memory_update',
  'memory_delete',
  'memory_memo',
  'memory_memo_folders',
  'memory_memo_notes',
};
const _phoneKnowledgeToolIds = <String>{
  'knowledge_create',
  'knowledge_get',
  'knowledge_list',
  'knowledge_update',
  'knowledge_delete',
  'knowledge_search',
  'knowledge_import_file',
  'knowledge_import_url',
  'knowledge_attach_to_project',
  'knowledge_index',
  'knowledge_reindex',
};
const _phoneKnowledgeMutationToolIds = <String>{
  'knowledge_create',
  'knowledge_update',
  'knowledge_delete',
  'knowledge_import_file',
  'knowledge_import_url',
  'knowledge_attach_to_project',
  'knowledge_index',
  'knowledge_reindex',
};
const _phoneMediaArtifactToolIds = <String>{
  'image_render',
  'image_generate_local_or_provider',
  'audio_transcribe',
  'audio_transcribe_local',
};
const _phoneWorkflowToolIds = <String>{
  'workflow_define',
  'workflow_run',
  'workflow_status',
  'workflow_cancel',
  'workflow_retry',
};
const _phoneWorkflowMutationToolIds = <String>{
  'workflow_run',
  'workflow_cancel',
  'workflow_retry',
};
const _phoneWorkflowRuntimeLayers = <String>[
  'flutter',
  'dart',
  'mobile-workflow-record',
];
const _mobileThinkingLevels = <String>{
  'none',
  'low',
  'medium',
  'high',
  'xhigh',
};

class MobileToolDefinition {
  const MobileToolDefinition({
    required this.name,
    required this.description,
    required this.parameters,
    required this.tags,
    this.aliases = const [],
    this.unavailableReason = '',
    this.executionPlatforms = _defaultMobilePlatforms,
    this.runtimeLayers = _flutterRuntimeLayers,
    this.nativeLayers = const [],
    this.requiresMobileApproval = false,
    this.implementationStatus = 'implemented',
  });

  final String name;
  final String description;
  final Map<String, dynamic> parameters;
  final List<String> tags;
  final List<String> aliases;
  final String unavailableReason;
  final List<String> executionPlatforms;
  final List<String> runtimeLayers;
  final List<String> nativeLayers;
  final bool requiresMobileApproval;
  final String implementationStatus;

  bool get available => unavailableReason.trim().isEmpty;

  Map<String, dynamic> toOpenAiTool({String? exportedName}) => {
        'type': 'function',
        'function': {
          'name': exportedName ?? name,
          'description': description,
          'parameters': parameters,
        },
      };

  Iterable<String> get openAiNames sync* {
    final names = <String>{name, ...aliases};
    for (final candidate in names) {
      if (_isOpenAiFunctionName(candidate)) yield candidate;
    }
  }
}

const _defaultspackToolAgentManifestCatalog =
    defaultspackToolAgentManifestCatalog;

class MobileToolCall {
  const MobileToolCall({
    required this.id,
    required this.name,
    required this.arguments,
  });

  final String id;
  final String name;
  final Map<String, dynamic> arguments;
}

class MobileToolApprovalRequest {
  const MobileToolApprovalRequest({
    required this.toolName,
    required this.prompt,
    required this.arguments,
    required this.risk,
  });

  final String toolName;
  final String prompt;
  final Map<String, dynamic> arguments;
  final String risk;
}

class MobileToolResult {
  const MobileToolResult({
    required this.output,
    required this.ok,
    this.summary = '',
  });

  final String output;
  final bool ok;
  final String summary;

  String toToolMessageContent() => jsonEncode({
        'status': ok ? 'ok' : 'error',
        'summary': summary,
        'data': output,
      });
}

abstract interface class MobileToolDelegate {
  Future<MobileToolResult> invoke(MobileToolCall call);
}

abstract interface class MobileToolApprovalDelegate {
  Future<bool> approve(MobileToolApprovalRequest request);
}

class MobileToolRuntime {
  const MobileToolRuntime({
    MobileToolDelegate? pcDelegate,
    MobileToolApprovalDelegate? approvalDelegate,
    PlatformUrlLauncher urlLauncher = const PlatformUrlLauncher(),
    PlatformClipboard clipboard = const PlatformClipboard(),
    PlatformMediaPicker mediaPicker = const PlatformMediaPicker(),
    PlatformScreenshotCapture screenshotCapture =
        const PlatformScreenshotCapture(),
    PlatformImageTransformer imageTransformer =
        const PlatformImageTransformer(),
    PlatformOcrRecognizer ocrRecognizer = const PlatformOcrRecognizer(),
    ApiConfigStore? configStore,
  })  : _pcDelegate = pcDelegate,
        _approvalDelegate = approvalDelegate,
        _urlLauncher = urlLauncher,
        _clipboard = clipboard,
        _mediaPicker = mediaPicker,
        _screenshotCapture = screenshotCapture,
        _imageTransformer = imageTransformer,
        _ocrRecognizer = ocrRecognizer,
        _configStore = configStore;

  final MobileToolDelegate? _pcDelegate;
  final MobileToolApprovalDelegate? _approvalDelegate;
  final PlatformUrlLauncher _urlLauncher;
  final PlatformClipboard _clipboard;
  final PlatformMediaPicker _mediaPicker;
  final PlatformScreenshotCapture _screenshotCapture;
  final PlatformImageTransformer _imageTransformer;
  final PlatformOcrRecognizer _ocrRecognizer;
  final ApiConfigStore? _configStore;

  bool get pcDelegationAvailable => _pcDelegate != null;
  ApiConfigStore get _store => _configStore ?? ApiConfigStore();

  static const supportedTools = <MobileToolDefinition>[
    MobileToolDefinition(
      name: 'calculator',
      description:
          'Run the defaultspack-compatible calculator tool on this phone. Use it for arithmetic only.',
      tags: ['tool', 'math', mobileCompatibleTag],
      aliases: ['tool_calculator', 'defaultspack.tool.calculator'],
      parameters: {
        'type': 'object',
        'additionalProperties': false,
        'properties': {
          'expression': {
            'type': 'string',
            'description': 'Arithmetic expression, such as "(12.5 + 3) / 2".',
          },
        },
        'required': ['expression'],
      },
    ),
    MobileToolDefinition(
      name: 'tool_consent_check',
      description:
          'Run the defaultspack consent check locally on this phone using the built-in keyword classifier.',
      tags: ['tool', 'consent', mobileCompatibleTag],
      aliases: [
        'defaults_tool_consent_check',
        'defaultspack_tool_consent_check',
        'defaults.tool.consent.check',
        'defaults.tool.consent_check',
        'defaultspack.tool.consent.check',
        'defaultspack.tool.consent_check',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'use_ai': {'type': 'boolean', 'default': false},
          'model': {'type': 'string'},
        },
        'required': ['text'],
      },
    ),
    MobileToolDefinition(
      name: 'tool_consent_confirm',
      description:
          'Confirm a phone-local defaultspack consent record created by tool_consent_check.',
      tags: ['tool', 'consent', mobileCompatibleTag],
      aliases: [
        'defaults_tool_consent_confirm',
        'defaultspack_tool_consent_confirm',
        'defaults.tool.consent.confirm',
        'defaults.tool.consent_confirm',
        'defaultspack.tool.consent.confirm',
        'defaultspack.tool.consent_confirm',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'consent_id': {'type': 'string'},
          'accepted': {'type': 'boolean'},
        },
        'required': ['consent_id', 'accepted'],
      },
    ),
    MobileToolDefinition(
      name: 'current_time',
      description:
          'Return the current date/time from this phone. Use for time, date, and timezone questions.',
      tags: ['tool', 'time', mobileCompatibleTag],
      parameters: {
        'type': 'object',
        'additionalProperties': false,
        'properties': {
          'format': {
            'type': 'string',
            'enum': ['iso8601', 'human'],
            'default': 'human',
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'mobile_platform_info',
      description:
          'Return this app runtime and mobile platform information, split by Flutter, iOS, Android, Swift, and Kotlin layers.',
      tags: ['tool', 'diagnostics', 'mobile', mobileCompatibleTag],
      aliases: [
        'platform_info',
        'mobile.platform.info',
        'defaultspack.mobile.platform_info',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': false,
      },
    ),
    MobileToolDefinition(
      name: 'todo',
      description:
          'Run the defaultspack todo tool on this phone. Use it to keep a small task list during an agent turn.',
      tags: ['tool', 'planning', mobileCompatibleTag],
      aliases: ['tool_todo', 'defaultspack.tool.todo'],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'action': {
            'type': 'string',
            'enum': [
              'add',
              'create',
              'complete',
              'done',
              'update',
              'edit',
              'remove',
              'delete',
              'clear',
              'list',
              'show',
            ],
            'default': 'list',
          },
          'title': {'type': 'string'},
          'task': {'type': 'string'},
          'todo_id': {'type': 'string'},
          'id': {'type': 'string'},
          'status': {'type': 'string'},
          'priority': {'type': 'string'},
          'notes': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'tool_task_board',
      description:
          'Run the defaultspack task board tool on this phone. Use it for agent planning with Kanban-style cards.',
      tags: ['tool', 'planning', 'task_board', mobileCompatibleTag],
      aliases: ['task_board', 'defaultspack.tool.task_board'],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'action': {
            'type': 'string',
            'enum': [
              'configure',
              'configure_columns',
              'create',
              'add',
              'update',
              'edit',
              'move',
              'block',
              'unblock',
              'subtask_add',
              'add_subtask',
              'subtask_update',
              'subtask_complete',
              'subtask_remove',
              'remove_subtask',
              'delete',
              'remove',
              'clear',
              'list',
              'show',
            ],
            'default': 'list',
          },
          'title': {'type': 'string'},
          'task': {'type': 'string'},
          'card_id': {'type': 'string'},
          'id': {'type': 'string'},
          'column': {'type': 'string'},
          'column_id': {'type': 'string'},
          'status': {'type': 'string'},
          'columns': {
            'type': 'array',
            'items': {'type': 'string'},
          },
          'position': {'type': 'integer'},
          'notes': {'type': 'string'},
          'priority': {'type': 'string'},
          'assignee': {'type': 'string'},
          'subtask_id': {'type': 'string'},
          'done': {'type': 'boolean'},
          'blocker_reason': {'type': 'string'},
          'reason': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'mobile_json',
      description:
          'Validate, format, or minify JSON locally on this phone using Flutter/Dart.',
      tags: ['tool', 'json', 'text', mobileCompatibleTag],
      aliases: ['json_format', 'json_validate', 'mobile.json'],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'action': {
            'type': 'string',
            'enum': ['validate', 'format', 'pretty', 'minify'],
            'default': 'format',
          },
          'text': {'type': 'string'},
          'json': {},
          'indent': {'type': 'string', 'default': '  '},
        },
      },
    ),
    MobileToolDefinition(
      name: 'mobile_base64',
      description:
          'Encode or decode Base64 locally on this phone using Flutter/Dart.',
      tags: ['tool', 'encoding', 'text', mobileCompatibleTag],
      aliases: ['base64_codec', 'mobile.base64'],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'action': {
            'type': 'string',
            'enum': ['encode', 'decode'],
            'default': 'encode',
          },
          'text': {'type': 'string'},
          'data': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'mobile_uuid',
      description:
          'Generate UUID v4 values locally on this phone using Flutter/Dart.',
      tags: ['tool', 'id', 'uuid', mobileCompatibleTag],
      aliases: ['uuid_generate', 'mobile.uuid'],
      parameters: {
        'type': 'object',
        'additionalProperties': false,
        'properties': {
          'count': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 20,
            'default': 1,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_models',
      description:
          'List mobile-configured defaultspack provider models on this phone.',
      tags: ['tool', 'ai', 'model', 'catalog', mobileCompatibleTag],
      aliases: ['defaults_ai_models', 'defaultspack_ai_models'],
      implementationStatus: 'implemented_phone_ai_catalog',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'provider': {'type': 'string'},
          'configured_only': {'type': 'boolean'},
          'favorites_only': {'type': 'boolean'},
          'query': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_profiles',
      description:
          'List mobile model profiles and starred models stored on this phone.',
      tags: ['tool', 'ai', 'profile', 'catalog', mobileCompatibleTag],
      aliases: ['defaults_ai_profiles', 'defaultspack_ai_profiles'],
      implementationStatus: 'implemented_phone_ai_catalog',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'configured_only': {'type': 'boolean'},
          'favorites_only': {'type': 'boolean'},
          'query': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_providers',
      description:
          'List mobile AI providers and API-key status without exposing secrets.',
      tags: ['tool', 'ai', 'provider', 'catalog', mobileCompatibleTag],
      aliases: ['defaults_ai_providers', 'defaultspack_ai_providers'],
      implementationStatus: 'implemented_phone_ai_catalog',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'configured_only': {'type': 'boolean'},
          'query': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_get_provider_key_status',
      description:
          'Return phone-local provider API-key status without returning key material.',
      tags: ['tool', 'ai', 'provider_key', mobileCompatibleTag],
      aliases: [
        'defaults_ai_get_provider_key_status',
        'defaultspack_ai_get_provider_key_status',
      ],
      implementationStatus: 'implemented_phone_ai_provider_key_status',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'provider': {'type': 'string'},
          'provider_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_set_provider_key',
      description:
          'Save or update a provider API key in this phone secure storage after mobile approval.',
      tags: ['tool', 'ai', 'provider_key', mobileCompatibleTag],
      aliases: [
        'defaults_ai_set_provider_key',
        'defaultspack_ai_set_provider_key'
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_ai_provider_key',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'provider': {'type': 'string'},
          'provider_id': {'type': 'string'},
          'api_key': {'type': 'string'},
          'apiKey': {'type': 'string'},
          'base_url': {'type': 'string'},
          'baseUrl': {'type': 'string'},
          'model': {'type': 'string'},
          'label': {'type': 'string'},
          'activate': {'type': 'boolean'},
          'favorite': {'type': 'boolean'},
          'star': {'type': 'boolean'},
        },
        'required': ['api_key'],
      },
    ),
    MobileToolDefinition(
      name: 'ai_delete_provider_key',
      description:
          'Remove a provider API key from this phone secure storage after mobile approval.',
      tags: ['tool', 'ai', 'provider_key', mobileCompatibleTag],
      aliases: [
        'defaults_ai_delete_provider_key',
        'defaultspack_ai_delete_provider_key',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_ai_provider_key',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'provider': {'type': 'string'},
          'provider_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_get_preferred_model',
      description: 'Get the phone-local preferred model and provider.',
      tags: ['tool', 'ai', 'model_runtime', mobileCompatibleTag],
      aliases: [
        'defaults_ai_get_preferred_model',
        'defaultspack_ai_get_preferred_model',
      ],
      implementationStatus: 'implemented_phone_ai_model_settings',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'ai_set_preferred_model',
      description:
          'Set the phone-local preferred model/provider after mobile approval.',
      tags: ['tool', 'ai', 'model_runtime', mobileCompatibleTag],
      aliases: [
        'defaults_ai_set_preferred_model',
        'defaultspack_ai_set_preferred_model',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_ai_model_settings',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'provider': {'type': 'string'},
          'provider_id': {'type': 'string'},
          'model': {'type': 'string'},
          'profile': {'type': 'string'},
          'profile_id': {'type': 'string'},
          'favorite': {'type': 'boolean'},
          'star': {'type': 'boolean'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_get_thinking_level',
      description: 'Get the phone-local model thinking level.',
      tags: ['tool', 'ai', 'model_runtime', mobileCompatibleTag],
      aliases: [
        'defaults_ai_get_thinking_level',
        'defaultspack_ai_get_thinking_level',
        'defaults_model_runtime_get_thinking_level',
        'defaultspack_model_runtime_get_thinking_level',
      ],
      implementationStatus: 'implemented_phone_ai_model_settings',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'ai_set_thinking_level',
      description:
          'Set the phone-local model thinking level after mobile approval.',
      tags: ['tool', 'ai', 'model_runtime', mobileCompatibleTag],
      aliases: [
        'defaults_ai_set_thinking_level',
        'defaultspack_ai_set_thinking_level',
        'defaults_model_runtime_set_thinking_level',
        'defaultspack_model_runtime_set_thinking_level',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_ai_model_settings',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'thinking_level': {'type': 'string'},
          'level': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_get_effective_thinking_level',
      description:
          'Resolve the effective phone-local thinking level for a model request.',
      tags: ['tool', 'ai', 'model_runtime', mobileCompatibleTag],
      aliases: [
        'defaults_ai_get_effective_thinking_level',
        'defaultspack_ai_get_effective_thinking_level',
        'defaults_model_runtime_get_effective_thinking_level',
        'defaultspack_model_runtime_get_effective_thinking_level',
      ],
      implementationStatus: 'implemented_phone_ai_model_settings',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'requested_thinking_level': {'type': 'string'},
          'thinking_level': {'type': 'string'},
          'model': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_normalize_thinking_level',
      description:
          'Normalize a thinking level to the phone-supported defaultspack levels.',
      tags: ['tool', 'ai', 'model_runtime', mobileCompatibleTag],
      aliases: [
        'defaults_ai_normalize_thinking_level',
        'defaultspack_ai_normalize_thinking_level',
        'defaults_model_runtime_normalize_thinking_level',
        'defaultspack_model_runtime_normalize_thinking_level',
      ],
      implementationStatus: 'implemented_phone_ai_model_settings',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'thinking_level': {'type': 'string'},
          'level': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_validate_model_params',
      description:
          'Validate model runtime parameters locally on this phone without calling a provider.',
      tags: ['tool', 'ai', 'model_runtime', mobileCompatibleTag],
      aliases: [
        'defaults_ai_validate_model_params',
        'defaultspack_ai_validate_model_params',
      ],
      implementationStatus: 'implemented_phone_ai_param_validation',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'model': {'type': 'string'},
          'provider': {'type': 'string'},
          'provider_id': {'type': 'string'},
          'temperature': {'type': 'number'},
          'max_tokens': {'type': 'integer'},
          'thinking_level': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_recommend_model',
      description:
          'Recommend a phone-local configured or starred model for a request.',
      tags: ['tool', 'ai', 'model', 'routing', mobileCompatibleTag],
      aliases: [
        'defaults_ai_recommend_model',
        'defaultspack_ai_recommend_model',
      ],
      implementationStatus: 'implemented_phone_ai_routing_hint',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'prompt': {'type': 'string'},
          'task': {'type': 'string'},
          'preferred_model': {'type': 'string'},
          'model': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_route_model',
      description:
          'Route a request to a phone-local configured or starred model without calling a provider.',
      tags: ['tool', 'ai', 'model', 'routing', mobileCompatibleTag],
      aliases: ['defaults_ai_route_model', 'defaultspack_ai_route_model'],
      implementationStatus: 'implemented_phone_ai_routing_hint',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'prompt': {'type': 'string'},
          'task': {'type': 'string'},
          'preferred_model': {'type': 'string'},
          'model': {'type': 'string'},
          'provider': {'type': 'string'},
          'provider_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'ai_explain_model_choice',
      description:
          'Explain the phone-local model routing choice without calling a provider.',
      tags: ['tool', 'ai', 'model', 'routing', mobileCompatibleTag],
      aliases: [
        'defaults_ai_explain_model_choice',
        'defaultspack_ai_explain_model_choice',
      ],
      implementationStatus: 'implemented_phone_ai_routing_hint',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'model': {'type': 'string'},
          'provider': {'type': 'string'},
          'provider_id': {'type': 'string'},
          'reason': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_validate_template',
      description:
          'Validate a prompt template locally on this phone and report variables or syntax issues.',
      tags: ['tool', 'prompt', 'template', mobileCompatibleTag],
      aliases: [
        'defaults_prompt_validate_template',
        'defaultspack_prompt_validate_template',
      ],
      implementationStatus: 'implemented_phone_prompt_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'template': {'type': 'string'},
          'prompt': {'type': 'string'},
          'variables': {'type': 'object'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_render',
      description:
          'Render a prompt template locally on this phone using provided variables.',
      tags: ['tool', 'prompt', 'template', mobileCompatibleTag],
      aliases: ['defaults_prompt_render', 'defaultspack_prompt_render'],
      implementationStatus: 'implemented_phone_prompt_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'template': {'type': 'string'},
          'prompt': {'type': 'string'},
          'variables': {'type': 'object'},
          'strict': {'type': 'boolean'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_lint_prompt',
      description:
          'Lint prompt text locally for unresolved variables, repeated lines, and size risk.',
      tags: ['tool', 'prompt', 'lint', mobileCompatibleTag],
      aliases: [
        'defaults_prompt_lint_prompt',
        'defaultspack_prompt_lint_prompt'
      ],
      implementationStatus: 'implemented_phone_prompt_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'prompt': {'type': 'string'},
          'template': {'type': 'string'},
          'max_chars': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_compact_prompt',
      description:
          'Create a phone-local compacted prompt draft without calling an AI provider.',
      tags: ['tool', 'prompt', 'compact', mobileCompatibleTag],
      aliases: [
        'defaults_prompt_compact_prompt',
        'defaultspack_prompt_compact_prompt',
      ],
      implementationStatus: 'implemented_phone_prompt_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'prompt': {'type': 'string'},
          'text': {'type': 'string'},
          'max_chars': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_system_get',
      description:
          'Get the phone-local system prompt from this phone API configuration.',
      tags: ['tool', 'prompt', 'system', mobileCompatibleTag],
      aliases: ['defaults_prompt_system_get', 'defaultspack_prompt_system_get'],
      implementationStatus: 'implemented_phone_prompt_system',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'prompt_system_set',
      description: 'Set the phone-local system prompt after mobile approval.',
      tags: ['tool', 'prompt', 'system', mobileCompatibleTag],
      aliases: ['defaults_prompt_system_set', 'defaultspack_prompt_system_set'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_prompt_system',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'system_prompt': {'type': 'string'},
          'prompt': {'type': 'string'},
          'content': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_list',
      description:
          'List phone-local prompt records and the active mobile system prompt.',
      tags: ['tool', 'prompt', 'catalog', mobileCompatibleTag],
      aliases: ['defaults_prompt_list', 'defaultspack_prompt_list'],
      implementationStatus: 'implemented_phone_prompt_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'include_system': {'type': 'boolean'},
          'enabled_only': {'type': 'boolean'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_create',
      description: 'Create a phone-local prompt record after mobile approval.',
      tags: ['tool', 'prompt', 'catalog', mobileCompatibleTag],
      aliases: ['defaults_prompt_create', 'defaultspack_prompt_create'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_prompt_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'prompt': {'type': 'string'},
          'enabled': {'type': 'boolean'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_update',
      description: 'Update a phone-local prompt record after mobile approval.',
      tags: ['tool', 'prompt', 'catalog', mobileCompatibleTag],
      aliases: ['defaults_prompt_update', 'defaultspack_prompt_update'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_prompt_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'prompt_id': {'type': 'string'},
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'prompt': {'type': 'string'},
          'enabled': {'type': 'boolean'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_delete',
      description: 'Delete a phone-local prompt record after mobile approval.',
      tags: ['tool', 'prompt', 'catalog', mobileCompatibleTag],
      aliases: ['defaults_prompt_delete', 'defaultspack_prompt_delete'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_prompt_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'prompt_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_active',
      description:
          'Summarize active phone-local prompt segments for the mobile agent.',
      tags: ['tool', 'prompt', 'effective', mobileCompatibleTag],
      aliases: ['defaults_prompt_active', 'defaultspack_prompt_active'],
      implementationStatus: 'implemented_phone_prompt_effective',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'prompt_load_effective',
      description:
          'Load the effective phone-local prompt assembled from system and enabled prompt records.',
      tags: ['tool', 'prompt', 'effective', mobileCompatibleTag],
      aliases: [
        'defaults_prompt_load_effective',
        'defaultspack_prompt_load_effective',
      ],
      implementationStatus: 'implemented_phone_prompt_effective',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'prompt_resolve_for_conversation',
      description:
          'Resolve phone-local prompt context for a mobile conversation.',
      tags: ['tool', 'prompt', 'effective', mobileCompatibleTag],
      aliases: [
        'defaults_prompt_resolve_for_conversation',
        'defaultspack_prompt_resolve_for_conversation',
      ],
      implementationStatus: 'implemented_phone_prompt_effective',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'prompt_preview_toggle',
      description:
          'Preview enabling or disabling a phone-local prompt record without saving.',
      tags: ['tool', 'prompt', 'preview', mobileCompatibleTag],
      aliases: [
        'defaults_prompt_preview_toggle',
        'defaultspack_prompt_preview_toggle',
      ],
      implementationStatus: 'implemented_phone_prompt_preview',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'prompt_id': {'type': 'string'},
          'enabled': {'type': 'boolean'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'prompt_test',
      description: 'Run a phone-local prompt validation and render test.',
      tags: ['tool', 'prompt', 'test', mobileCompatibleTag],
      aliases: ['defaults_prompt_test', 'defaultspack_prompt_test'],
      implementationStatus: 'implemented_phone_prompt_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'template': {'type': 'string'},
          'prompt': {'type': 'string'},
          'variables': {'type': 'object'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_store',
      description: 'Store a phone-local memory record after mobile approval.',
      tags: ['tool', 'memory', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_memory_store', 'defaultspack_memory_store'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_memory_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'content': {'type': 'string'},
          'text': {'type': 'string'},
          'summary': {'type': 'string'},
          'tags': {},
          'importance': {'type': 'number'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_list',
      description: 'List phone-local memory records.',
      tags: ['tool', 'memory', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_memory_list', 'defaultspack_memory_list'],
      implementationStatus: 'implemented_phone_memory_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'tag': {'type': 'string'},
          'limit': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_recall',
      description:
          'Recall phone-local memories using lightweight lexical ranking.',
      tags: ['tool', 'memory', 'search', mobileCompatibleTag],
      aliases: ['defaults_memory_recall', 'defaultspack_memory_recall'],
      implementationStatus: 'implemented_phone_memory_search',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'limit': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_update',
      description: 'Update a phone-local memory record after mobile approval.',
      tags: ['tool', 'memory', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_memory_update', 'defaultspack_memory_update'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_memory_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'memory_id': {'type': 'string'},
          'content': {'type': 'string'},
          'text': {'type': 'string'},
          'summary': {'type': 'string'},
          'tags': {},
          'importance': {'type': 'number'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_delete',
      description: 'Delete a phone-local memory record after mobile approval.',
      tags: ['tool', 'memory', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_memory_delete', 'defaultspack_memory_delete'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_memory_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'memory_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_compact',
      description:
          'Build a compact phone-local memory summary without calling a provider.',
      tags: ['tool', 'memory', 'compact', mobileCompatibleTag],
      aliases: ['defaults_memory_compact', 'defaultspack_memory_compact'],
      implementationStatus: 'implemented_phone_memory_summary',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'limit': {'type': 'integer'},
          'max_chars': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_project_context',
      description:
          'Return phone-local memory context for a mobile project or conversation.',
      tags: ['tool', 'memory', 'context', mobileCompatibleTag],
      aliases: [
        'defaults_memory_project_context',
        'defaultspack_memory_project_context',
      ],
      implementationStatus: 'implemented_phone_memory_context',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'project_id': {'type': 'string'},
          'limit': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_resolve_for_agent',
      description:
          'Resolve phone-local memories for the mobile agent template.',
      tags: ['tool', 'memory', 'agent', mobileCompatibleTag],
      aliases: [
        'defaults_memory_resolve_for_agent',
        'defaultspack_memory_resolve_for_agent',
      ],
      implementationStatus: 'implemented_phone_memory_context',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'agent_id': {'type': 'string'},
          'limit': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_memo_folders',
      description: 'Create, update, delete, or list phone-local memo folders.',
      tags: ['tool', 'memory', 'memo', mobileCompatibleTag],
      aliases: [
        'defaults_memory_memo_folders',
        'defaultspack_memory_memo_folders',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_memo_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'action': {'type': 'string'},
          'id': {'type': 'string'},
          'folder_id': {'type': 'string'},
          'title': {'type': 'string'},
          'name': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_memo_notes',
      description: 'Create, update, delete, or list phone-local memo notes.',
      tags: ['tool', 'memory', 'memo', mobileCompatibleTag],
      aliases: [
        'defaults_memory_memo_notes',
        'defaultspack_memory_memo_notes',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_memo_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'action': {'type': 'string'},
          'id': {'type': 'string'},
          'note_id': {'type': 'string'},
          'folder_id': {'type': 'string'},
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'text': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'memory_memo',
      description:
          'Dispatch phone-local memo folder or note operations after mobile approval for mutations.',
      tags: ['tool', 'memory', 'memo', mobileCompatibleTag],
      aliases: ['defaults_memory_memo', 'defaultspack_memory_memo'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_memo_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'target': {'type': 'string'},
          'kind': {'type': 'string'},
          'action': {'type': 'string'},
          'id': {'type': 'string'},
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'folder_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_create',
      description:
          'Create a phone-local knowledge record after mobile approval.',
      tags: ['tool', 'knowledge', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_knowledge_create', 'defaultspack_knowledge_create'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'text': {'type': 'string'},
          'tags': {},
          'project_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_get',
      description: 'Get a phone-local knowledge record.',
      tags: ['tool', 'knowledge', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_knowledge_get', 'defaultspack_knowledge_get'],
      implementationStatus: 'implemented_phone_knowledge_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'knowledge_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_list',
      description: 'List phone-local knowledge records.',
      tags: ['tool', 'knowledge', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_knowledge_list', 'defaultspack_knowledge_list'],
      implementationStatus: 'implemented_phone_knowledge_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'project_id': {'type': 'string'},
          'tag': {'type': 'string'},
          'limit': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_update',
      description:
          'Update a phone-local knowledge record after mobile approval.',
      tags: ['tool', 'knowledge', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_knowledge_update', 'defaultspack_knowledge_update'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'knowledge_id': {'type': 'string'},
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'text': {'type': 'string'},
          'tags': {},
          'project_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_delete',
      description:
          'Delete a phone-local knowledge record after mobile approval.',
      tags: ['tool', 'knowledge', 'local_store', mobileCompatibleTag],
      aliases: ['defaults_knowledge_delete', 'defaultspack_knowledge_delete'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'knowledge_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_search',
      description:
          'Search phone-local knowledge records with lightweight lexical ranking.',
      tags: ['tool', 'knowledge', 'search', mobileCompatibleTag],
      aliases: ['defaults_knowledge_search', 'defaultspack_knowledge_search'],
      implementationStatus: 'implemented_phone_knowledge_search',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'project_id': {'type': 'string'},
          'limit': {'type': 'integer'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_import_file',
      description:
          'Import provided text or a phone-local artifact file into phone knowledge after mobile approval.',
      tags: [
        'tool',
        'knowledge',
        'import',
        'artifact_workspace',
        mobileCompatibleTag
      ],
      aliases: [
        'defaults_knowledge_import_file',
        'defaultspack_knowledge_import_file',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_import',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'text': {'type': 'string'},
          'project_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_import_url',
      description:
          'Import a URL reference and optional provided content into phone knowledge after mobile approval.',
      tags: ['tool', 'knowledge', 'import', mobileCompatibleTag],
      aliases: [
        'defaults_knowledge_import_url',
        'defaultspack_knowledge_import_url',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_import',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'url': {'type': 'string'},
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'text': {'type': 'string'},
          'project_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_attach_to_project',
      description:
          'Attach a phone-local knowledge record to a project id after mobile approval.',
      tags: ['tool', 'knowledge', 'project', mobileCompatibleTag],
      aliases: [
        'defaults_knowledge_attach_to_project',
        'defaultspack_knowledge_attach_to_project',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_store',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'knowledge_id': {'type': 'string'},
          'project_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_index',
      description:
          'Build a lightweight phone-local keyword index for knowledge records after mobile approval.',
      tags: ['tool', 'knowledge', 'index', mobileCompatibleTag],
      aliases: ['defaults_knowledge_index', 'defaultspack_knowledge_index'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_index',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'id': {'type': 'string'},
          'knowledge_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'knowledge_reindex',
      description:
          'Rebuild lightweight phone-local keyword indexes after mobile approval.',
      tags: ['tool', 'knowledge', 'index', mobileCompatibleTag],
      aliases: ['defaults_knowledge_reindex', 'defaultspack_knowledge_reindex'],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_knowledge_index',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'project_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'artifact_file_list',
      description:
          'List files inside this phone-local artifact workspace. This does not read the connected PC artifact workspace.',
      tags: ['tool', 'artifact', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_artifact_file_list',
        'defaultspack_artifact_file_list',
        'defaults.artifact.file.list',
        'defaultspack.artifact.file.list',
      ],
      implementationStatus: 'implemented_phone_artifact_workspace',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'recursive': {'type': 'boolean', 'default': false},
          'include_hidden': {'type': 'boolean', 'default': false},
          'max_entries': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 500,
            'default': 200,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'artifact_file_read',
      description:
          'Read a text file from this phone-local artifact workspace. This does not read PC artifact paths.',
      tags: ['tool', 'artifact', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_artifact_file_read',
        'defaultspack_artifact_file_read',
        'defaults.artifact.file.read',
        'defaultspack.artifact.file.read',
      ],
      implementationStatus: 'implemented_phone_artifact_workspace',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'file_reader',
      description:
          'Read text from the phone-local artifact workspace using the defaultspack file_reader convention. This does not read PC workspace paths.',
      tags: ['tool', 'file', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'tool_file_reader',
        'defaults_tool_file_reader',
        'defaultspack_tool_file_reader',
        'defaults.tool.file_reader',
        'defaultspack.tool.file_reader',
        'defaults.tool.file.reader',
        'defaultspack.tool.file.reader',
      ],
      implementationStatus: 'implemented_phone_artifact_file_reader',
      runtimeLayers: _phoneMediaArtifactRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'start_line': {'type': 'integer', 'minimum': 1},
          'end_line': {'type': 'integer', 'minimum': 1},
          'max_chars': {'type': 'integer', 'minimum': 1},
          'max_tokens': {'type': 'integer', 'minimum': 1},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'artifact_file_write',
      description:
          'Write a text file inside this phone-local artifact workspace after mobile approval.',
      tags: ['tool', 'artifact', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_artifact_file_write',
        'defaultspack_artifact_file_write',
        'defaults.artifact.file.write',
        'defaultspack.artifact.file.write',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_artifact_workspace',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'content': {'type': 'string'},
          'checkpoint': {'type': 'boolean', 'default': true},
        },
        'required': ['path', 'content'],
      },
    ),
    MobileToolDefinition(
      name: 'artifact_file_patch',
      description:
          'Patch text inside a phone-local artifact file after mobile approval.',
      tags: ['tool', 'artifact', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_artifact_file_patch',
        'defaultspack_artifact_file_patch',
        'defaults.artifact.file.patch',
        'defaultspack.artifact.file.patch',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_artifact_workspace',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'old_text': {'type': 'string'},
          'new_text': {'type': 'string'},
          'expected_replacements': {'type': 'integer'},
        },
        'required': ['path', 'old_text', 'new_text'],
      },
    ),
    MobileToolDefinition(
      name: 'artifact_file_delete',
      description:
          'Delete a file inside this phone-local artifact workspace after mobile approval.',
      tags: ['tool', 'artifact', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_artifact_file_delete',
        'defaultspack_artifact_file_delete',
        'defaults.artifact.file.delete',
        'defaultspack.artifact.file.delete',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_artifact_workspace',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'checkpoint': {'type': 'boolean', 'default': true},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'browser_save_page',
      description:
          'Save provided HTML into this phone-local artifact workspace. This does not read a PC browser session.',
      tags: [
        'tool',
        'browser',
        'webapp',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_browser_save_page',
        'defaultspack_browser_save_page',
        'defaults.browser.save_page',
        'defaultspack.browser.save_page',
      ],
      implementationStatus: 'implemented_phone_artifact_html',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'html': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['html'],
      },
    ),
    MobileToolDefinition(
      name: 'webapp_preview',
      description:
          'Preview a phone-local artifact webapp index.html with HTML metadata fallback.',
      tags: [
        'tool',
        'webapp',
        'preview',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_webapp_preview',
        'defaultspack_webapp_preview',
        'defaults.webapp.preview',
        'defaultspack.webapp.preview',
      ],
      implementationStatus: 'implemented_phone_artifact_html',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardArtifactPreviewMaxChars,
            'default': _defaultArtifactPreviewMaxChars,
          },
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'webapp_lint',
      description:
          'Run simple phone-local structural lint checks for an artifact webapp.',
      tags: [
        'tool',
        'webapp',
        'lint',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_webapp_lint',
        'defaultspack_webapp_lint',
        'defaults.webapp.lint',
        'defaultspack.webapp.lint',
      ],
      implementationStatus: 'implemented_phone_artifact_html',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'webapp_build',
      description:
          'Mark a phone-local static webapp artifact as build-ready after mobile approval. Real package commands remain PC-delegated.',
      tags: [
        'tool',
        'webapp',
        'build',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_webapp_build',
        'defaultspack_webapp_build',
        'defaults.webapp.build',
        'defaultspack.webapp.build',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_static_webapp_build_plan',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'command': {},
          'timeout': {'type': 'integer'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'project_scaffold',
      description:
          'Create a phone-local static webapp scaffold in the artifact workspace. Static HTML, plain JS, and Vite React file layouts are supported; package install/build still delegates to PC.',
      tags: [
        'tool',
        'webapp',
        'project',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_project_scaffold',
        'defaultspack_project_scaffold',
        'defaults.project.scaffold',
        'defaultspack.project.scaffold',
      ],
      implementationStatus: 'implemented_phone_artifact_scaffold',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'name': {'type': 'string'},
          'path': {'type': 'string'},
          'template': {
            'type': 'string',
            'enum': ['static_html', 'plain_js', 'vite_react'],
            'default': 'static_html',
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'doc_create',
      description:
          'Create a phone-local Markdown, text, HTML, or JSON document artifact. Binary DOCX/PDF output remains PC-delegated.',
      tags: [
        'tool',
        'document',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_doc_create',
        'defaultspack_doc_create',
        'defaults.doc.create',
        'defaultspack.doc.create',
      ],
      implementationStatus: 'implemented_phone_document_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'title': {'type': 'string'},
          'content': {'type': 'string'},
          'markdown': {'type': 'string'},
          'output_path': {'type': 'string'},
          'format': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'doc_update',
      description:
          'Update a phone-local text document artifact after mobile approval. Binary DOCX/PDF mutation remains PC-delegated.',
      tags: [
        'tool',
        'document',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_doc_update',
        'defaultspack_doc_update',
        'defaults.doc.update',
        'defaultspack.doc.update',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_document_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'content': {'type': 'string'},
          'append': {'type': 'string'},
          'replace': {'type': 'boolean'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'slides_create',
      description:
          'Create a phone-local slide outline artifact from structured slide JSON. Binary PPTX output remains PC-delegated.',
      tags: [
        'tool',
        'presentation',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_slides_create',
        'defaultspack_slides_create',
        'defaults.slides.create',
        'defaultspack.slides.create',
      ],
      implementationStatus: 'implemented_phone_slide_outline',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'title': {'type': 'string'},
          'slides': {
            'type': 'array',
            'items': {'type': 'object'},
          },
          'output_path': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'slides_from_markdown',
      description:
          'Create a phone-local slide outline artifact from markdown headings and bullets. Binary PPTX export remains PC-delegated.',
      tags: [
        'tool',
        'presentation',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_slides_from_markdown',
        'defaultspack_slides_from_markdown',
        'defaults.slides.from_markdown',
        'defaultspack.slides.from_markdown',
      ],
      implementationStatus: 'implemented_phone_slide_outline',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'markdown': {'type': 'string'},
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'slides_update',
      description:
          'Rewrite a phone-local slide outline artifact from structured slide JSON after mobile approval. Binary PPTX mutation remains PC-delegated.',
      tags: [
        'tool',
        'presentation',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_slides_update',
        'defaultspack_slides_update',
        'defaults.slides.update',
        'defaultspack.slides.update',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_slide_outline',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'slides': {
            'type': 'array',
            'items': {'type': 'object'},
          },
        },
        'required': ['path', 'slides'],
      },
    ),
    MobileToolDefinition(
      name: 'slides_export',
      description:
          'Export a phone-local slide outline artifact to JSON, Markdown, HTML, or text. Binary PPTX/PDF output remains PC-delegated.',
      tags: [
        'tool',
        'presentation',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_slides_export',
        'defaultspack_slides_export',
        'defaults.slides.export',
        'defaultspack.slides.export',
      ],
      implementationStatus: 'implemented_phone_slide_export',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'format': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'chart_create',
      description:
          'Create a phone-local SVG chart artifact from title and optional series data. PNG rendering remains PC-delegated.',
      tags: [
        'tool',
        'spreadsheet',
        'chart',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_chart_create',
        'defaultspack_chart_create',
        'defaults.chart.create',
        'defaultspack.chart.create',
      ],
      implementationStatus: 'implemented_phone_svg_chart',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'title': {'type': 'string'},
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
          'values': {
            'type': 'array',
            'items': {'type': 'number'},
          },
          'labels': {
            'type': 'array',
            'items': {'type': 'string'},
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'sheet_create',
      description:
          'Create a phone-local CSV, TSV, JSON, or HTML sheet artifact. XLSX output remains PC-delegated.',
      tags: [
        'tool',
        'spreadsheet',
        'sheet',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_sheet_create',
        'defaultspack_sheet_create',
        'defaults.sheet.create',
        'defaultspack.sheet.create',
      ],
      implementationStatus: 'implemented_phone_sheet_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'rows': {
            'type': 'array',
            'items': {'type': 'array'},
          },
          'columns': {
            'type': 'array',
            'items': {'type': 'string'},
          },
          'output_path': {'type': 'string'},
          'format': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'sheet_read',
      description:
          'Read rows from a phone-local CSV, TSV, JSON, or simple text sheet artifact. XLSX reading remains PC-delegated.',
      tags: [
        'tool',
        'spreadsheet',
        'sheet',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_sheet_read',
        'defaultspack_sheet_read',
        'defaults.sheet.read',
        'defaultspack.sheet.read',
      ],
      implementationStatus: 'implemented_phone_sheet_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'limit': {'type': 'integer'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'sheet_analyze',
      description:
          'Analyze row counts, missing values, headers, and numeric stats for a phone-local text sheet artifact.',
      tags: [
        'tool',
        'spreadsheet',
        'sheet',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_sheet_analyze',
        'defaultspack_sheet_analyze',
        'defaults.sheet.analyze',
        'defaultspack.sheet.analyze',
      ],
      implementationStatus: 'implemented_phone_sheet_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'sheet_update',
      description:
          'Replace rows in a phone-local CSV, TSV, JSON, or HTML sheet artifact after mobile approval. XLSX updates remain PC-delegated.',
      tags: [
        'tool',
        'spreadsheet',
        'sheet',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_sheet_update',
        'defaultspack_sheet_update',
        'defaults.sheet.update',
        'defaultspack.sheet.update',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_sheet_text',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'rows': {
            'type': 'array',
            'items': {'type': 'array'},
          },
        },
        'required': ['path', 'rows'],
      },
    ),
    MobileToolDefinition(
      name: 'sheet_export',
      description:
          'Export a phone-local text sheet artifact to CSV, TSV, JSON, HTML, or TXT. XLSX/PDF export remains PC-delegated.',
      tags: [
        'tool',
        'spreadsheet',
        'sheet',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_sheet_export',
        'defaultspack_sheet_export',
        'defaults.sheet.export',
        'defaultspack.sheet.export',
      ],
      implementationStatus: 'implemented_phone_sheet_export',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'format': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'artifact_zip',
      description:
          'Create a ZIP archive from phone-local artifact files or folders. ZIP bytes are stored as base64 content in the phone artifact workspace.',
      tags: [
        'tool',
        'artifact',
        'archive',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_artifact_zip',
        'defaultspack_artifact_zip',
        'defaults.artifact.zip',
        'defaultspack.artifact.zip',
      ],
      implementationStatus: 'implemented_phone_zip_base64',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'source_path': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'research_report_export',
      description:
          'Export a phone-local research report artifact or provided report content to Markdown, HTML, JSON, or text. Binary exports remain PC-delegated.',
      tags: [
        'tool',
        'research',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_research_report_export',
        'defaultspack_research_report_export',
        'defaults.research.report_export',
        'defaultspack.research.report_export',
      ],
      implementationStatus: 'implemented_phone_research_report_export',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'report_path': {'type': 'string'},
          'content': {'type': 'string'},
          'report': {'type': 'string'},
          'format': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'artifact_export',
      description:
          'Export phone-local artifacts to zip/base64, HTML, text, Markdown, JSON, CSV, or TSV. Binary PDF/PNG/DOCX/PPTX/XLSX output remains PC-delegated.',
      tags: [
        'tool',
        'artifact',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_artifact_export',
        'defaultspack_artifact_export',
        'defaults.artifact.export',
        'defaultspack.artifact.export',
      ],
      implementationStatus: 'implemented_phone_artifact_export',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'format': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path', 'format'],
      },
    ),
    MobileToolDefinition(
      name: 'static_site_export',
      description:
          'Export a phone-local static site artifact folder as a base64 ZIP artifact.',
      tags: [
        'tool',
        'webapp',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_static_site_export',
        'defaultspack_static_site_export',
        'defaults.static_site.export',
        'defaultspack.static_site.export',
      ],
      implementationStatus: 'implemented_phone_zip_base64',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'webapp_export_static',
      description:
          'Export a phone-local webapp artifact folder as a base64 ZIP artifact.',
      tags: [
        'tool',
        'webapp',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_webapp_export_static',
        'defaultspack_webapp_export_static',
        'defaults.webapp.export_static',
        'defaultspack.webapp.export_static',
      ],
      implementationStatus: 'implemented_phone_zip_base64',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'doc_export',
      description:
          'Export a phone-local document artifact to HTML, Markdown, text, or JSON. Binary DOCX/PDF output remains PC-delegated.',
      tags: [
        'tool',
        'document',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_doc_export',
        'defaultspack_doc_export',
        'defaults.doc.export',
        'defaultspack.doc.export',
      ],
      implementationStatus: 'implemented_phone_document_export',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'format': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'pdf_export',
      description:
          'Explain that PDF export requires the connected PC runtime; phone-local export cannot generate real PDF bytes.',
      tags: [
        'tool',
        'export',
        'pdf',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_pdf_export',
        'defaultspack_pdf_export',
        'defaults.pdf.export',
        'defaultspack.pdf.export',
      ],
      implementationStatus: 'pc_delegation_required_binary_export',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'doc_to_pdf',
      description:
          'Explain that document-to-PDF export requires the connected PC runtime; phone-local export cannot generate real PDF bytes.',
      tags: [
        'tool',
        'document',
        'pdf',
        'export',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_doc_to_pdf',
        'defaultspack_doc_to_pdf',
        'defaults.doc.to_pdf',
        'defaultspack.doc.to_pdf',
      ],
      implementationStatus: 'pc_delegation_required_binary_export',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
        },
        'required': ['path'],
      },
    ),
    MobileToolDefinition(
      name: 'mobile_url_open',
      description:
          'Open an http/https URL visibly on this phone. Implemented through Flutter with iOS Swift and Android Kotlin native bridges.',
      tags: ['tool', 'browser', 'url', 'mobile', mobileCompatibleTag],
      aliases: [
        'url_open',
        'browser_open_url',
        'defaults.browser.open_url',
        'defaultspack.browser.open_url',
        'defaults.browser.open.url',
        'defaultspack.browser.open.url',
      ],
      runtimeLayers: _nativeUrlRuntimeLayers,
      nativeLayers: [
        'ios:Swift UIApplication.open',
        'android:Kotlin Intent.ACTION_VIEW',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': false,
        'properties': {
          'url': {'type': 'string'},
        },
        'required': ['url'],
      },
    ),
    MobileToolDefinition(
      name: 'media_clipboard_read',
      description:
          'Read text from this phone clipboard after explicit mobile approval.',
      tags: ['tool', 'media', 'clipboard', mobileCompatibleTag],
      aliases: [
        'clipboard_read',
        'defaults_media_clipboard_read',
        'defaultspack_media_clipboard_read',
        'defaults.media.clipboard.read',
        'defaults.media.clipboard_read',
        'defaultspack.media.clipboard.read',
        'defaultspack.media.clipboard_read',
      ],
      runtimeLayers: _nativeUrlRuntimeLayers,
      nativeLayers: [
        'ios:Flutter Clipboard/Pasteboard bridge',
        'android:Flutter ClipboardManager bridge',
      ],
      requiresMobileApproval: true,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'reason': {
            'type': 'string',
            'description': 'Why clipboard text is needed.',
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 8000,
            'default': 4000,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'media_clipboard_write',
      description:
          'Write text to this phone clipboard after explicit mobile approval.',
      tags: ['tool', 'media', 'clipboard', mobileCompatibleTag],
      aliases: [
        'clipboard_write',
        'defaults_media_clipboard_write',
        'defaultspack_media_clipboard_write',
        'defaults.media.clipboard.write',
        'defaults.media.clipboard_write',
        'defaultspack.media.clipboard.write',
        'defaultspack.media.clipboard_write',
      ],
      runtimeLayers: _nativeUrlRuntimeLayers,
      nativeLayers: [
        'ios:Flutter Clipboard/Pasteboard bridge',
        'android:Flutter ClipboardManager bridge',
      ],
      requiresMobileApproval: true,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'content': {'type': 'string'},
          'reason': {
            'type': 'string',
            'description': 'Why clipboard text should be replaced.',
          },
        },
        'required': ['text'],
      },
    ),
    MobileToolDefinition(
      name: 'media_file_pick',
      description:
          'Pick one image, audio, or document from this phone after explicit mobile approval. Returns file metadata and base64 content.',
      tags: ['tool', 'media', 'file', 'picker', mobileCompatibleTag],
      aliases: [
        'file_pick',
        'media_pick_file',
        'mobile_media_pick',
        'defaults_media_file_pick',
        'defaultspack_media_file_pick',
        'defaults.media.file.pick',
        'defaults.media.file_pick',
        'defaultspack.media.file.pick',
        'defaultspack.media.file_pick',
      ],
      runtimeLayers: _nativeMediaPickerRuntimeLayers,
      nativeLayers: [
        'ios:Swift UIDocumentPickerViewController',
        'android:Kotlin Intent.ACTION_OPEN_DOCUMENT',
      ],
      requiresMobileApproval: true,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'kind': {
            'type': 'string',
            'enum': ['image', 'audio', 'file'],
            'default': 'file',
          },
          'type': {
            'type': 'string',
            'enum': ['image', 'audio', 'file'],
          },
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardMediaPickMaxBytes,
            'default': _defaultMediaPickMaxBytes,
          },
          'reason': {
            'type': 'string',
            'description': 'Why this phone file is needed.',
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'media_screenshot',
      description:
          'Capture a PNG screenshot of the visible Rumi app window on this phone after explicit mobile approval.',
      tags: ['tool', 'media', 'screenshot', mobileCompatibleTag],
      aliases: [
        'screenshot',
        'mobile_screenshot',
        'defaults_media_screenshot',
        'defaultspack_media_screenshot',
        'defaults.media.screenshot',
        'defaultspack.media.screenshot',
      ],
      runtimeLayers: _nativeScreenshotRuntimeLayers,
      nativeLayers: [
        'ios:Swift UIWindow screenshot capture',
        'android:Kotlin View drawing cache capture',
      ],
      requiresMobileApproval: true,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'reason': {
            'type': 'string',
            'description': 'Why the current app screen should be captured.',
          },
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardScreenshotMaxBytes,
            'default': _defaultScreenshotMaxBytes,
          },
          'max_dimension': {
            'type': 'integer',
            'minimum': 320,
            'maximum': _hardScreenshotMaxDimension,
            'default': _defaultScreenshotMaxDimension,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'media_image_read',
      description:
          'Read metadata from PNG, JPEG, GIF, WebP, or BMP image bytes already provided to this phone runtime.',
      tags: ['tool', 'media', 'image', mobileCompatibleTag],
      aliases: [
        'image_read',
        'defaults_media_image_read',
        'defaultspack_media_image_read',
        'defaults.media.image.read',
        'defaults.media.image_read',
        'defaultspack.media.image.read',
        'defaultspack.media.image_read',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'image_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'image': {'type': 'object'},
          'file': {'type': 'object'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardImageReadMaxBytes,
            'default': _defaultImageReadMaxBytes,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'media_image_transform',
      description:
          'Resize and encode image bytes already provided to this phone runtime. Does not read host file paths.',
      tags: ['tool', 'media', 'image', 'transform', mobileCompatibleTag],
      aliases: [
        'image_transform',
        'defaults_media_image_transform',
        'defaultspack_media_image_transform',
        'defaults.media.image.transform',
        'defaults.media.image_transform',
        'defaultspack.media.image.transform',
        'defaultspack.media.image_transform',
      ],
      runtimeLayers: _nativeImageTransformRuntimeLayers,
      nativeLayers: [
        'ios:Swift UIImage resize/encode',
        'android:Kotlin Bitmap resize/encode',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'image_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'image': {'type': 'object'},
          'file': {'type': 'object'},
          'operations': {
            'type': 'array',
            'items': {'type': 'object'},
          },
          'width': {'type': 'integer', 'minimum': 1},
          'height': {'type': 'integer', 'minimum': 1},
          'max_width': {'type': 'integer', 'minimum': 1},
          'max_height': {'type': 'integer', 'minimum': 1},
          'max_dimension': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardImageTransformMaxDimension,
            'default': _defaultImageTransformMaxDimension,
          },
          'format': {
            'type': 'string',
            'enum': ['jpeg', 'jpg', 'png'],
            'default': 'png',
          },
          'quality': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 100,
            'default': 90,
          },
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardImageTransformOutputMaxBytes,
            'default': _defaultImageTransformOutputMaxBytes,
          },
          'max_input_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardImageTransformInputMaxBytes,
            'default': _hardImageTransformInputMaxBytes,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'media_ocr',
      description:
          'Extract OCR text from image bytes already provided to this phone runtime using native iOS Vision or Android ML Kit.',
      tags: ['tool', 'media', 'image', 'ocr', mobileCompatibleTag],
      aliases: [
        'ocr',
        'defaults_media_ocr',
        'defaultspack_media_ocr',
        'defaults.media.ocr',
        'defaultspack.media.ocr',
      ],
      runtimeLayers: _nativeOcrRuntimeLayers,
      nativeLayers: [
        'ios:Swift Vision VNRecognizeTextRequest',
        'android:Kotlin ML Kit TextRecognition',
      ],
      implementationStatus: 'implemented_native_ocr_bridge',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'image_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'image': {'type': 'object'},
          'file': {'type': 'object'},
          'language_hint': {'type': 'string'},
          'language': {'type': 'string'},
          'locale': {'type': 'string'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardOcrMaxBytes,
            'default': _defaultOcrMaxBytes,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'ocr_extract',
      description:
          'Extract OCR text from image bytes already provided to this phone runtime. PC artifact paths are delegated to the PC runtime.',
      tags: [
        'tool',
        'media',
        'image',
        'ocr',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_ocr_extract',
        'defaultspack_ocr_extract',
        'defaults.ocr.extract',
        'defaultspack.ocr.extract',
      ],
      runtimeLayers: _nativeOcrRuntimeLayers,
      nativeLayers: [
        'ios:Swift Vision VNRecognizeTextRequest',
        'android:Kotlin ML Kit TextRecognition',
      ],
      implementationStatus: 'implemented_payload_only_native_ocr',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'image_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'image': {'type': 'object'},
          'file': {'type': 'object'},
          'path': {'type': 'string'},
          'language_hint': {'type': 'string'},
          'language': {'type': 'string'},
          'locale': {'type': 'string'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardOcrMaxBytes,
            'default': _defaultOcrMaxBytes,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'image_resize',
      description:
          'Resize image bytes already provided to this phone runtime using the native iOS Swift / Android Kotlin image bridge. Does not read or write PC artifact paths.',
      tags: ['tool', 'media', 'image', 'resize', mobileCompatibleTag],
      aliases: [
        'defaults_image_resize',
        'defaultspack_image_resize',
        'defaults.image.resize',
        'defaultspack.image.resize',
      ],
      runtimeLayers: _nativeImageTransformRuntimeLayers,
      nativeLayers: [
        'ios:Swift UIImage resize/encode',
        'android:Kotlin Bitmap resize/encode',
      ],
      implementationStatus: 'implemented_payload_only',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'image_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'image': {'type': 'object'},
          'file': {'type': 'object'},
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
          'width': {'type': 'integer', 'minimum': 1},
          'height': {'type': 'integer', 'minimum': 1},
          'format': {
            'type': 'string',
            'enum': ['jpeg', 'jpg', 'png'],
            'default': 'png',
          },
          'quality': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 100,
            'default': 90,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'image_convert',
      description:
          'Convert image bytes already provided to this phone runtime using the native iOS Swift / Android Kotlin image bridge. Does not read or write PC artifact paths.',
      tags: ['tool', 'media', 'image', 'convert', mobileCompatibleTag],
      aliases: [
        'defaults_image_convert',
        'defaultspack_image_convert',
        'defaults.image.convert',
        'defaultspack.image.convert',
      ],
      runtimeLayers: _nativeImageTransformRuntimeLayers,
      nativeLayers: [
        'ios:Swift UIImage resize/encode',
        'android:Kotlin Bitmap resize/encode',
      ],
      implementationStatus: 'implemented_payload_only',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'image_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'image': {'type': 'object'},
          'file': {'type': 'object'},
          'path': {'type': 'string'},
          'output_path': {'type': 'string'},
          'format': {
            'type': 'string',
            'enum': ['jpeg', 'jpg', 'png'],
            'default': 'png',
          },
          'quality': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 100,
            'default': 90,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'tts_generate',
      description:
          'Generate a phone-local fallback WAV audio payload in Flutter/Dart. This does not synthesize spoken speech yet and does not write PC artifact paths.',
      tags: ['tool', 'media', 'audio', 'tts', mobileCompatibleTag],
      aliases: [
        'defaults_tts_generate',
        'defaultspack_tts_generate',
        'defaults.tts.generate',
        'defaultspack.tts.generate',
      ],
      implementationStatus: 'implemented_silent_wav_fallback',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'output_path': {'type': 'string'},
          'duration_ms': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardTtsFallbackDurationMs,
            'default': _defaultTtsFallbackDurationMs,
          },
          'sample_rate': {
            'type': 'integer',
            'minimum': _minTtsFallbackSampleRate,
            'maximum': _hardTtsFallbackSampleRate,
            'default': _defaultTtsFallbackSampleRate,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'tts_generate_local',
      description:
          'Generate a phone-local fallback WAV audio payload in Flutter/Dart. This mirrors defaultspack local TTS fallback without writing PC artifact paths.',
      tags: ['tool', 'media', 'audio', 'tts', mobileCompatibleTag],
      aliases: [
        'defaults_tts_generate_local',
        'defaultspack_tts_generate_local',
        'defaults.tts.generate_local',
        'defaultspack.tts.generate_local',
      ],
      implementationStatus: 'implemented_silent_wav_fallback',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'output_path': {'type': 'string'},
          'duration_ms': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardTtsFallbackDurationMs,
            'default': _defaultTtsFallbackDurationMs,
          },
          'sample_rate': {
            'type': 'integer',
            'minimum': _minTtsFallbackSampleRate,
            'maximum': _hardTtsFallbackSampleRate,
            'default': _defaultTtsFallbackSampleRate,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'image_render',
      description:
          'Render a simple phone-local SVG image artifact in Flutter/Dart. This does not read or write PC artifact paths.',
      tags: [
        'tool',
        'preview',
        'image',
        'artifact_workspace',
        mobileCompatibleTag
      ],
      aliases: [
        'defaults_image_render',
        'defaultspack_image_render',
        'defaults.image.render',
        'defaultspack.image.render',
      ],
      implementationStatus: 'implemented_phone_svg_image_render',
      runtimeLayers: _phoneMediaArtifactRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'prompt': {'type': 'string'},
          'output_path': {'type': 'string'},
          'width': {'type': 'integer', 'minimum': 1, 'maximum': 4096},
          'height': {'type': 'integer', 'minimum': 1, 'maximum': 4096},
          'viewport': {'type': 'object'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'image_generate_local_or_provider',
      description:
          'Generate a phone-local placeholder SVG image artifact from a prompt. Provider-backed real image generation remains PC/provider delegated.',
      tags: [
        'tool',
        'media',
        'image',
        'artifact_workspace',
        mobileCompatibleTag
      ],
      aliases: [
        'defaults_image_generate_local_or_provider',
        'defaultspack_image_generate_local_or_provider',
        'defaults.image.generate.local_or_provider',
        'defaultspack.image.generate.local_or_provider',
      ],
      implementationStatus: 'implemented_phone_svg_image_placeholder',
      runtimeLayers: _phoneMediaArtifactRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'prompt': {'type': 'string'},
          'text': {'type': 'string'},
          'output_path': {'type': 'string'},
          'width': {'type': 'integer', 'minimum': 1, 'maximum': 4096},
          'height': {'type': 'integer', 'minimum': 1, 'maximum': 4096},
        },
      },
    ),
    MobileToolDefinition(
      name: 'audio_transcribe',
      description:
          'Return phone-local audio transcription text when provided explicitly, or audio metadata with a clear native/provider requirement. Does not read PC file paths.',
      tags: [
        'tool',
        'media',
        'audio',
        'artifact_workspace',
        mobileCompatibleTag
      ],
      aliases: [
        'defaults_audio_transcribe',
        'defaultspack_audio_transcribe',
        'defaults.audio.transcribe',
        'defaultspack.audio.transcribe',
      ],
      implementationStatus: 'implemented_phone_audio_transcribe_payload',
      runtimeLayers: _phoneMediaArtifactRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'transcript': {'type': 'string'},
          'content': {'type': 'string'},
          'base64': {'type': 'string'},
          'audio_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'audio': {'type': 'object'},
          'path': {'type': 'string'},
          'language': {'type': 'string'},
          'language_hint': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'audio_transcribe_local',
      description:
          'Mirror audio_transcribe on phone with explicit text/payload fallback. Native iOS/Android speech recognition can replace this route later.',
      tags: [
        'tool',
        'media',
        'audio',
        'artifact_workspace',
        mobileCompatibleTag
      ],
      aliases: [
        'defaults_audio_transcribe_local',
        'defaultspack_audio_transcribe_local',
        'defaults.audio.transcribe.local',
        'defaultspack.audio.transcribe.local',
      ],
      implementationStatus: 'implemented_phone_audio_transcribe_payload',
      runtimeLayers: _phoneMediaArtifactRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'transcript': {'type': 'string'},
          'content': {'type': 'string'},
          'base64': {'type': 'string'},
          'audio_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'audio': {'type': 'object'},
          'path': {'type': 'string'},
          'language': {'type': 'string'},
          'language_hint': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'media_doc_parse',
      description:
          'Parse text-like document bytes or text already provided to this phone runtime. Supports txt, markdown, json, csv, html, xml, and similar UTF text; does not read host file paths.',
      tags: ['tool', 'media', 'document', 'text', mobileCompatibleTag],
      aliases: [
        'doc_parse',
        'document_parse',
        'defaults_media_doc_parse',
        'defaultspack_media_doc_parse',
        'defaults.media.doc.parse',
        'defaults.media.doc_parse',
        'defaultspack.media.doc.parse',
        'defaultspack.media.doc_parse',
      ],
      implementationStatus: 'implemented_text_documents',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'content': {'type': 'string'},
          'base64': {'type': 'string'},
          'document_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'name': {'type': 'string'},
          'mime_type': {'type': 'string'},
          'format': {'type': 'string'},
          'encoding': {'type': 'string'},
          'strip_html': {'type': 'boolean', 'default': true},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardDocParseMaxBytes,
            'default': _defaultDocParseMaxBytes,
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardDocParseMaxChars,
            'default': _defaultDocParseMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'media_pdf_parse',
      description:
          'Extract best-effort text from PDF bytes already provided to this phone runtime. Does not read host file paths and does not perform full layout/table parsing.',
      tags: ['tool', 'media', 'pdf', 'document', mobileCompatibleTag],
      aliases: [
        'pdf_parse',
        'defaults_media_pdf_parse',
        'defaultspack_media_pdf_parse',
        'defaults.media.pdf.parse',
        'defaults.media.pdf_parse',
        'defaultspack.media.pdf.parse',
        'defaultspack.media.pdf_parse',
      ],
      implementationStatus: 'implemented_best_effort_bytes',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'pdf_base64': {'type': 'string'},
          'document_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'name': {'type': 'string'},
          'mime_type': {'type': 'string'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardPdfParseMaxBytes,
            'default': _defaultPdfParseMaxBytes,
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardPdfParseMaxChars,
            'default': _defaultPdfParseMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'pdf_extract',
      description:
          'Extract best-effort text from PDF bytes already provided to this phone runtime. Does not read PC artifact paths.',
      tags: ['tool', 'media', 'pdf', 'document', mobileCompatibleTag],
      aliases: [
        'defaults_pdf_extract',
        'defaultspack_pdf_extract',
        'defaults.pdf.extract',
        'defaultspack.pdf.extract',
      ],
      implementationStatus: 'implemented_best_effort_bytes',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'pdf_base64': {'type': 'string'},
          'document_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'path': {'type': 'string'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardPdfParseMaxBytes,
            'default': _defaultPdfParseMaxBytes,
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardPdfParseMaxChars,
            'default': _defaultPdfParseMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'pdf_extract_tables',
      description:
          'Return a phone-local PDF table extraction fallback for PDF bytes already provided to this phone runtime. Full table extraction remains PC/provider-only.',
      tags: ['tool', 'media', 'pdf', 'table', mobileCompatibleTag],
      aliases: [
        'defaults_pdf_extract_tables',
        'defaultspack_pdf_extract_tables',
        'defaults.pdf.extract_tables',
        'defaultspack.pdf.extract_tables',
      ],
      implementationStatus: 'implemented_empty_table_fallback',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'pdf_base64': {'type': 'string'},
          'document_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'path': {'type': 'string'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardPdfParseMaxBytes,
            'default': _defaultPdfParseMaxBytes,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'html_preview',
      description:
          'Preview HTML payloads already provided to this phone runtime with metadata and text fallback. Does not read PC artifact paths.',
      tags: [
        'tool',
        'preview',
        'html',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_html_preview',
        'defaultspack_html_preview',
        'defaults.html.preview',
        'defaultspack.html.preview',
      ],
      implementationStatus: 'implemented_payload_only_preview',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'html': {'type': 'string'},
          'content': {'type': 'string'},
          'source': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'path': {'type': 'string'},
          'viewport': {'type': 'object'},
          'full_page': {'type': 'boolean', 'default': true},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardArtifactPreviewMaxBytes,
            'default': _defaultArtifactPreviewMaxBytes,
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardArtifactPreviewMaxChars,
            'default': _defaultArtifactPreviewMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'pdf_preview',
      description:
          'Preview PDF bytes already provided to this phone runtime with metadata and best-effort text fallback. Does not read PC artifact paths.',
      tags: [
        'tool',
        'preview',
        'pdf',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_pdf_preview',
        'defaultspack_pdf_preview',
        'defaults.pdf.preview',
        'defaultspack.pdf.preview',
      ],
      implementationStatus: 'implemented_payload_only_preview',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'base64': {'type': 'string'},
          'pdf_base64': {'type': 'string'},
          'document_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'path': {'type': 'string'},
          'name': {'type': 'string'},
          'mime_type': {'type': 'string'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardArtifactPreviewMaxBytes,
            'default': _defaultArtifactPreviewMaxBytes,
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardArtifactPreviewMaxChars,
            'default': _defaultArtifactPreviewMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'artifact_preview',
      description:
          'Preview text, HTML, image, or PDF payloads already provided to this phone runtime. Does not read PC artifact paths or directories.',
      tags: [
        'tool',
        'preview',
        'media',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_artifact_preview',
        'defaultspack_artifact_preview',
        'defaults.artifact.preview',
        'defaultspack.artifact.preview',
      ],
      implementationStatus: 'implemented_payload_only_preview',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'text': {'type': 'string'},
          'content': {'type': 'string'},
          'html': {'type': 'string'},
          'base64': {'type': 'string'},
          'image_base64': {'type': 'string'},
          'pdf_base64': {'type': 'string'},
          'document_base64': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'image': {'type': 'object'},
          'path': {'type': 'string'},
          'name': {'type': 'string'},
          'mime_type': {'type': 'string'},
          'format': {'type': 'string'},
          'max_bytes': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardArtifactPreviewMaxBytes,
            'default': _defaultArtifactPreviewMaxBytes,
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardArtifactPreviewMaxChars,
            'default': _defaultArtifactPreviewMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'source_extract',
      description:
          'Extract text and title metadata from source text, HTML, or URL payloads already provided to this phone runtime. Does not read PC workspace paths.',
      tags: ['tool', 'research', 'source', 'text', mobileCompatibleTag],
      aliases: [
        'defaults_source_extract',
        'defaultspack_source_extract',
        'defaults.source.extract',
        'defaultspack.source.extract',
      ],
      implementationStatus: 'implemented_payload_only',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'source': {'type': 'string'},
          'path': {'type': 'string'},
          'url': {'type': 'string'},
          'title': {'type': 'string'},
          'text': {'type': 'string'},
          'content': {'type': 'string'},
          'html': {'type': 'string'},
          'strip_html': {'type': 'boolean', 'default': true},
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardSourceExtractMaxChars,
            'default': _defaultSourceExtractMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'browser_extract_table',
      description:
          'Extract structured table rows from HTML already provided to this phone runtime. Does not read the PC browser session or artifact paths.',
      tags: [
        'tool',
        'browser',
        'html',
        'table',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_browser_extract_table',
        'defaultspack_browser_extract_table',
        'defaults.browser.extract_table',
        'defaultspack.browser.extract_table',
      ],
      implementationStatus: 'implemented_payload_only_html',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'html': {'type': 'string'},
          'content': {'type': 'string'},
          'source': {'type': 'string'},
          'data_url': {'type': 'string'},
          'file': {'type': 'object'},
          'document': {'type': 'object'},
          'path': {'type': 'string'},
          'table_index': {'type': 'integer', 'minimum': 0, 'default': 0},
          'max_tables': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardHtmlTableMaxTables,
            'default': _defaultHtmlTableMaxTables,
          },
          'max_rows': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardHtmlTableMaxRows,
            'default': _defaultHtmlTableMaxRows,
          },
          'max_chars': {
            'type': 'integer',
            'minimum': 1,
            'maximum': _hardSourceExtractMaxChars,
            'default': _defaultSourceExtractMaxChars,
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'source_rank',
      description:
          'Rank provided source snippets by query term frequency locally on this phone.',
      tags: ['tool', 'research', 'source', 'ranking', mobileCompatibleTag],
      aliases: [
        'defaults_source_rank',
        'defaultspack_source_rank',
        'defaults.source.rank',
        'defaultspack.source.rank',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'query': {'type': 'string'},
          'sources': {
            'type': 'array',
            'items': {
              'type': 'object',
              'additionalProperties': true,
              'properties': {
                'title': {'type': 'string'},
                'content': {'type': 'string'},
                'source': {'type': 'string'},
              },
            },
          },
        },
        'required': ['query', 'sources'],
      },
    ),
    MobileToolDefinition(
      name: 'tool_search',
      description:
          'Search the mobile tool catalog and explain whether defaultspack tools are available on this phone.',
      tags: ['tool', 'search', 'catalog', mobileCompatibleTag],
      parameters: {
        'type': 'object',
        'additionalProperties': false,
        'properties': {
          'query': {
            'type': 'string',
            'description': 'Tool name, service, or desired action.',
          },
          'limit': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 12,
            'default': 6,
          },
          'platform': {
            'type': 'string',
            'enum': [
              'android',
              'flutter',
              'ios',
              'kotlin',
              'pc',
              'swift',
            ],
          },
        },
        'required': ['query'],
      },
    ),
    MobileToolDefinition(
      name: 'tool_names',
      description:
          'List tool function names available to the mobile defaultspack-compatible runtime.',
      tags: ['tool', 'catalog', mobileCompatibleTag],
      aliases: [
        'defaults_tool_names',
        'defaultspack_tool_names',
        'defaults.tool.names',
        'defaultspack.tool.names',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'tool_list',
      description:
          'List mobile-compatible tools and known PC-only defaultspack tools with reasons.',
      tags: ['tool', 'catalog', mobileCompatibleTag],
      aliases: [
        'defaults_tool_list',
        'defaultspack_tool_list',
        'defaults.tool.list',
        'defaultspack.tool.list',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'include_unavailable': {
            'type': 'boolean',
            'default': true,
          },
          'limit': {
            'type': 'integer',
            'minimum': 1,
            'maximum': 400,
            'default': 240,
          },
          'platform': {
            'type': 'string',
            'enum': [
              'android',
              'flutter',
              'ios',
              'kotlin',
              'pc',
              'swift',
            ],
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'tool_schema',
      description:
          'Return the schema and mobile execution status for a defaultspack tool name.',
      tags: ['tool', 'catalog', mobileCompatibleTag],
      aliases: [
        'defaults_tool_schema',
        'defaultspack_tool_schema',
        'defaults.tool.schema',
        'defaultspack.tool.schema',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'tool_name': {'type': 'string'},
          'name': {'type': 'string'},
          'tool_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'tool_invoke',
      description:
          'Invoke a mobile-compatible defaultspack tool through the defaultspack tool_invoke convention. Host-bound tools return a clear unavailable reason.',
      tags: ['tool', 'broker', mobileCompatibleTag],
      aliases: [
        'defaults_tool_invoke',
        'defaultspack_tool_invoke',
        'defaults.tool.invoke',
        'defaultspack.tool.invoke',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'tool_name': {'type': 'string'},
          'tool_id': {'type': 'string'},
          'name': {'type': 'string'},
          'arguments': {'type': 'object'},
          'args': {'type': 'object'},
          'input': {'type': 'object'},
        },
        'required': ['tool_name'],
      },
    ),
    MobileToolDefinition(
      name: 'tool_batch',
      description:
          'Invoke multiple defaultspack-compatible tools through one mobile call. Phone-compatible tools run locally; host-bound tools route to the connected PC when delegation is enabled.',
      tags: ['tool', 'broker', 'batch', mobileCompatibleTag],
      aliases: [
        'defaults_tool_batch',
        'defaultspack_tool_batch',
        'defaults.tool.batch',
        'defaultspack.tool.batch',
      ],
      implementationStatus: 'implemented_mobile_batch_router',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'calls': {
            'type': 'array',
            'minItems': 1,
            'maxItems': _toolBatchMaxCalls,
            'items': {
              'type': 'object',
              'additionalProperties': true,
              'properties': {
                'id': {'type': 'string'},
                'tool_name': {'type': 'string'},
                'tool_id': {'type': 'string'},
                'name': {'type': 'string'},
                'arguments': {'type': 'object'},
                'args': {'type': 'object'},
                'input': {'type': 'object'},
              },
            },
          },
          'parallel': {'type': 'boolean', 'default': false},
        },
        'required': ['calls'],
      },
    ),
    MobileToolDefinition(
      name: 'package_install_plan',
      description:
          'Plan pip/npm/pnpm/yarn package installation commands on this phone without executing them. Actual install execution remains PC-delegated.',
      tags: [
        'tool',
        'package',
        'sandbox',
        'artifact_workspace',
        mobileCompatibleTag,
      ],
      aliases: [
        'defaults_package_install_plan',
        'defaultspack_package_install_plan',
        'defaults.package.install_plan',
        'defaultspack.package.install_plan',
      ],
      implementationStatus: 'implemented_phone_install_plan',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'manager': {'type': 'string'},
          'packages': {},
          'dev': {'type': 'boolean'},
          'global': {'type': 'boolean'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'workflow_define',
      description:
          'Persist a phone-local workflow definition. Steps later run through the same unified phone/PC tool surface.',
      tags: ['tool', 'workflow', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_workflow_define',
        'defaultspack_workflow_define',
        'defaults.workflow.define',
        'defaultspack.workflow.define',
      ],
      implementationStatus: 'implemented_phone_workflow_record',
      runtimeLayers: _phoneWorkflowRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'workflow_id': {'type': 'string'},
          'name': {'type': 'string'},
          'steps': {
            'type': 'array',
            'items': {'type': 'object'},
          },
        },
        'required': ['steps'],
      },
    ),
    MobileToolDefinition(
      name: 'workflow_run',
      description:
          'Run a persisted or inline phone-local workflow after mobile approval. Each step uses the unified tool surface.',
      tags: ['tool', 'workflow', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_workflow_run',
        'defaultspack_workflow_run',
        'defaults.workflow.run',
        'defaultspack.workflow.run',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_workflow_record',
      runtimeLayers: _phoneWorkflowRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'workflow_id': {'type': 'string'},
          'steps': {
            'type': 'array',
            'items': {'type': 'object'},
          },
        },
      },
    ),
    MobileToolDefinition(
      name: 'workflow_status',
      description: 'Read a phone-local workflow run record.',
      tags: ['tool', 'workflow', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_workflow_status',
        'defaultspack_workflow_status',
        'defaults.workflow.status',
        'defaultspack.workflow.status',
      ],
      implementationStatus: 'implemented_phone_workflow_record',
      runtimeLayers: _phoneWorkflowRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'run_id': {'type': 'string'},
        },
        'required': ['run_id'],
      },
    ),
    MobileToolDefinition(
      name: 'workflow_cancel',
      description:
          'Cancel a phone-local workflow run record after mobile approval.',
      tags: ['tool', 'workflow', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_workflow_cancel',
        'defaultspack_workflow_cancel',
        'defaults.workflow.cancel',
        'defaultspack.workflow.cancel',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_workflow_record',
      runtimeLayers: _phoneWorkflowRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'run_id': {'type': 'string'},
        },
        'required': ['run_id'],
      },
    ),
    MobileToolDefinition(
      name: 'workflow_retry',
      description:
          'Retry a persisted phone-local workflow after mobile approval.',
      tags: ['tool', 'workflow', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_workflow_retry',
        'defaultspack_workflow_retry',
        'defaults.workflow.retry',
        'defaultspack.workflow.retry',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_workflow_record',
      runtimeLayers: _phoneWorkflowRuntimeLayers,
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'workflow_id': {'type': 'string'},
          'run_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'job_create',
      description:
          'Create a phone-local artifact-backed job record. run_immediately marks the local record completed and writes a result artifact without PC execution.',
      tags: ['tool', 'job', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_job_create',
        'defaultspack_job_create',
        'defaults.job.create',
        'defaultspack.job.create',
      ],
      implementationStatus: 'implemented_phone_job_record',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'kind': {'type': 'string'},
          'job_id': {'type': 'string'},
          'input': {'type': 'object'},
          'query': {'type': 'string'},
          'run_immediately': {'type': 'boolean', 'default': false},
        },
      },
    ),
    MobileToolDefinition(
      name: 'job_status',
      description:
          'Read a phone-local job record or list all phone-local job records.',
      tags: ['tool', 'job', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_job_status',
        'defaultspack_job_status',
        'defaults.job.status',
        'defaultspack.job.status',
      ],
      implementationStatus: 'implemented_phone_job_record',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'job_id': {'type': 'string'},
        },
      },
    ),
    MobileToolDefinition(
      name: 'job_history',
      description: 'Read phone-local job event history.',
      tags: ['tool', 'job', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_job_history',
        'defaultspack_job_history',
        'defaults.job.history',
        'defaultspack.job.history',
      ],
      implementationStatus: 'implemented_phone_job_record',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'job_id': {'type': 'string'},
        },
        'required': ['job_id'],
      },
    ),
    MobileToolDefinition(
      name: 'job_artifacts',
      description:
          'List artifacts attached to a phone-local artifact-backed job record.',
      tags: ['tool', 'job', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_job_artifacts',
        'defaultspack_job_artifacts',
        'defaults.job.artifacts',
        'defaultspack.job.artifacts',
      ],
      implementationStatus: 'implemented_phone_job_record',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'job_id': {'type': 'string'},
        },
        'required': ['job_id'],
      },
    ),
    MobileToolDefinition(
      name: 'job_cancel',
      description:
          'Cancel a phone-local job record after mobile approval. This only updates the phone-local record.',
      tags: ['tool', 'job', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_job_cancel',
        'defaultspack_job_cancel',
        'defaults.job.cancel',
        'defaultspack.job.cancel',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_job_record',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'job_id': {'type': 'string'},
        },
        'required': ['job_id'],
      },
    ),
    MobileToolDefinition(
      name: 'job_resume',
      description:
          'Resume a phone-local job record after mobile approval. This only updates the phone-local record.',
      tags: ['tool', 'job', 'artifact_workspace', mobileCompatibleTag],
      aliases: [
        'defaults_job_resume',
        'defaultspack_job_resume',
        'defaults.job.resume',
        'defaultspack.job.resume',
      ],
      requiresMobileApproval: true,
      implementationStatus: 'implemented_phone_job_record',
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'job_id': {'type': 'string'},
        },
        'required': ['job_id'],
      },
    ),
    MobileToolDefinition(
      name: 'agent_plan',
      description:
          'Create a lightweight phone-local agent plan using the defaultspack agent_plan convention.',
      tags: ['agent', 'planning', mobileCompatibleTag],
      aliases: [
        'defaults_agent_plan',
        'defaultspack_agent_plan',
        'defaults.agent.plan',
        'defaultspack.agent.plan',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
        'properties': {
          'objective': {'type': 'string'},
          'title': {'type': 'string'},
          'steps': {
            'type': 'array',
            'items': {'type': 'string'},
          },
          'plan': {},
        },
      },
    ),
    MobileToolDefinition(
      name: 'agent_progress',
      description:
          'Return phone-local agent progress, plans, task board cards, and todos.',
      tags: ['agent', 'status', mobileCompatibleTag],
      aliases: [
        'defaults_agent_progress',
        'defaultspack_agent_progress',
        'defaults.agent.progress',
        'defaultspack.agent.progress',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: 'agent_status',
      description:
          'Return phone-local agent status using the defaultspack agent_status convention.',
      tags: ['agent', 'status', mobileCompatibleTag],
      aliases: [
        'defaults_agent_status',
        'defaultspack_agent_status',
        'defaults.agent.status',
        'defaultspack.agent.status',
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': true,
      },
    ),
    MobileToolDefinition(
      name: assistantProgressToolName,
      description:
          'Emit a brief user-visible work progress update. Use sparingly at phase changes, important findings, failures, or final verification.',
      tags: [
        'tool',
        'progress',
        'internal',
        mobileCompatibleTag,
      ],
      parameters: {
        'type': 'object',
        'additionalProperties': false,
        'properties': {
          'phase': {
            'type': 'string',
            'enum': ['change', 'finalize', 'inspect', 'recover', 'verify'],
          },
          'status': {
            'type': 'string',
            'enum': ['active', 'blocked', 'completed'],
          },
          'summary': {
            'type': 'string',
            'maxLength': _assistantProgressTextLimit,
          },
          'next_action': {
            'type': 'string',
            'maxLength': _assistantProgressTextLimit,
          },
          'related_tool_call_ids': {
            'type': 'array',
            'maxItems': _assistantProgressRelatedToolLimit,
            'items': {'type': 'string'},
          },
        },
        'required': ['phase', 'status', 'summary', 'next_action'],
      },
    ),
  ];

  static const unavailableDefaultspackTools = <MobileToolDefinition>[
    MobileToolDefinition(
      name: 'web_search',
      description: 'Run the defaultspack web research tool.',
      tags: ['tool', 'research', 'web'],
      aliases: ['tool_web_search', 'defaultspack.tool.web_search'],
      unavailableReason:
          'このtoolはPC側defaultspackのresearch providerに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'reddit_search',
      description: 'Run the defaultspack reddit research tool.',
      tags: ['tool', 'research', 'reddit'],
      aliases: ['tool_reddit_search', 'defaultspack.tool.reddit_search'],
      unavailableReason:
          'このtoolはPC側defaultspackのresearch providerに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'file_reader',
      description: 'Read files from the host workspace.',
      tags: ['tool', 'file', 'workspace', 'host'],
      aliases: ['tool_file_reader', 'defaultspack.tool.file_reader'],
      unavailableReason:
          'このtoolはPCのworkspace/file systemに依存するため、このスマホ単体では実行できません。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'subagent',
      description: 'Run the default delegation/subagent tool.',
      tags: ['tool', 'agent', 'host'],
      aliases: ['tool_subagent', 'defaultspack.tool.subagent'],
      unavailableReason:
          'このtoolはPC側のagent runtimeと会話/workspace状態に依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'tool_task_board_agent_session',
      description:
          'Link task board cards to defaultspack coding agent sessions.',
      tags: ['tool', 'planning', 'task_board', 'agent', 'coding', 'host'],
      unavailableReason:
          'このtoolはPC側のcoding agent sessionに依存するため、このスマホ単体では実行できません。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'terminal',
      description: 'Run terminal commands on the host runtime.',
      tags: ['terminal', 'host', 'workspace'],
      unavailableReason:
          'このtoolはPC側のterminal/workspaceに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'browser',
      description: 'Operate the host browser or browser companion.',
      tags: ['browser', 'host'],
      unavailableReason:
          'このtoolはPC側のブラウザ状態に依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'computer',
      description: 'Operate the host desktop UI.',
      tags: ['computer', 'desktop', 'host'],
      unavailableReason: 'このtoolはPC画面の操作権限が必要なため、このスマホ単体では実行できません。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
    MobileToolDefinition(
      name: 'files',
      description: 'Read or write host workspace files.',
      tags: ['file', 'workspace', 'host'],
      unavailableReason:
          'このtoolはPCのworkspace/file systemに依存するため、このスマホ単体では実行できません。',
      parameters: {'type': 'object', 'additionalProperties': true},
    ),
  ];

  List<MobileToolDefinition> get availableTools => supportedTools;

  int get knownDefaultspackToolAgentCount =>
      _defaultspackToolAgentManifestCatalog.length;

  int get knownUnavailableDefaultspackToolCount =>
      _runtimeCatalogRecords(includeUnavailable: true)
          .where((record) => record['mobile_compatible'] != true)
          .length;

  List<Map<String, dynamic>> openAiTools() {
    final exported = <Map<String, dynamic>>[];
    final seenNames = <String>{};

    void addTool({
      required String name,
      required String description,
      required Map<String, dynamic> parameters,
    }) {
      final trimmed = name.trim();
      if (!_isOpenAiFunctionName(trimmed) || !seenNames.add(trimmed)) return;
      exported.add({
        'type': 'function',
        'function': {
          'name': trimmed,
          'description': description,
          'parameters': parameters,
        },
      });
    }

    for (final tool in supportedTools) {
      for (final name in tool.openAiNames) {
        addTool(
          name: name,
          description: _openAiToolDescription(tool),
          parameters: tool.parameters,
        );
      }
    }

    for (final record in _runtimeCatalogRecords(includeUnavailable: true)) {
      if (!_shouldExportCatalogRecordAsNativeTool(record)) continue;
      addTool(
        name: '${record['function_id'] ?? ''}',
        description: _openAiCatalogDescription(record),
        parameters: _asObjectSchema(record['parameters']),
      );
    }

    return exported;
  }

  static bool isAssistantProgressToolName(String name) =>
      name.trim() == assistantProgressToolName;

  static Map<String, dynamic> assistantProgressPayload(
    MobileToolResult result,
  ) {
    final parsed = _decodeObject(result.output);
    final data = parsed['data'];
    if (data is Map<String, dynamic>) return data;
    if (data is Map) return data.map((key, value) => MapEntry('$key', value));
    return normalizeAssistantProgressPayload({
      'summary': result.summary,
      'next_action': '',
    });
  }

  static Map<String, dynamic> normalizeAssistantProgressPayload(
    Map<String, dynamic>? arguments,
  ) {
    final args = arguments ?? const {};
    final phase = '${args['phase'] ?? ''}'.trim();
    final status = '${args['status'] ?? ''}'.trim();
    final summary = _clampText(args['summary'], _assistantProgressTextLimit);
    final nextAction =
        _clampText(args['next_action'], _assistantProgressTextLimit);
    final relatedRaw = args['related_tool_call_ids'];
    final related = <String>[];
    if (relatedRaw is List) {
      for (final item in relatedRaw) {
        final value = '$item'.trim();
        if (value.isNotEmpty) related.add(value);
        if (related.length >= _assistantProgressRelatedToolLimit) break;
      }
    }
    return {
      'phase': _assistantProgressPhases.contains(phase) ? phase : 'inspect',
      'status': _assistantProgressStatuses.contains(status) ? status : 'active',
      'summary': summary.isNotEmpty ? summary : '作業状況を更新しています',
      'next_action': nextAction.isNotEmpty ? nextAction : '続行します',
      'related_tool_call_ids': related,
    };
  }

  MobileToolResult execute(MobileToolCall call) {
    final name = _canonicalToolName(call.name);
    if (_isMobileConnectorDryRunTool(name)) {
      return _connectorDryRun(name, call.arguments);
    }
    switch (name) {
      case 'calculator':
        return _calculator(call.arguments);
      case 'tool_consent_check':
        return _toolConsentCheck(call.arguments);
      case 'tool_consent_confirm':
        return _toolConsentConfirm(call.arguments);
      case 'current_time':
        return _currentTime(call.arguments);
      case 'mobile_platform_info':
        return _mobilePlatformInfo(call.arguments);
      case 'todo':
        return _todo(call.arguments);
      case 'tool_task_board':
      case 'task_board':
        return _taskBoard(call.arguments);
      case 'mobile_json':
        return _mobileJson(call.arguments);
      case 'mobile_base64':
        return _mobileBase64(call.arguments);
      case 'mobile_uuid':
        return _mobileUuid(call.arguments);
      case 'ai_models':
      case 'ai_profiles':
      case 'ai_providers':
      case 'ai_get_provider_key_status':
      case 'ai_set_provider_key':
      case 'ai_delete_provider_key':
      case 'ai_get_preferred_model':
      case 'ai_set_preferred_model':
      case 'ai_get_thinking_level':
      case 'ai_set_thinking_level':
      case 'ai_get_effective_thinking_level':
      case 'ai_normalize_thinking_level':
      case 'ai_validate_model_params':
      case 'ai_recommend_model':
      case 'ai_route_model':
      case 'ai_explain_model_choice':
        return _asyncOnlyTool(name);
      case 'prompt_active':
      case 'prompt_compact_prompt':
      case 'prompt_create':
      case 'prompt_delete':
      case 'prompt_lint_prompt':
      case 'prompt_list':
      case 'prompt_load_effective':
      case 'prompt_preview_toggle':
      case 'prompt_render':
      case 'prompt_resolve_for_conversation':
      case 'prompt_system_get':
      case 'prompt_system_set':
      case 'prompt_test':
      case 'prompt_update':
      case 'prompt_validate_template':
        return _asyncOnlyTool(name);
      case 'memory_store':
      case 'memory_list':
      case 'memory_recall':
      case 'memory_update':
      case 'memory_delete':
      case 'memory_compact':
      case 'memory_project_context':
      case 'memory_resolve_for_agent':
      case 'memory_memo':
      case 'memory_memo_folders':
      case 'memory_memo_notes':
        return _asyncOnlyTool(name);
      case 'knowledge_create':
      case 'knowledge_get':
      case 'knowledge_list':
      case 'knowledge_update':
      case 'knowledge_delete':
      case 'knowledge_search':
      case 'knowledge_import_file':
      case 'knowledge_import_url':
      case 'knowledge_attach_to_project':
      case 'knowledge_index':
      case 'knowledge_reindex':
        return _asyncOnlyTool(name);
      case 'artifact_file_list':
        return _artifactFileList(call.arguments);
      case 'artifact_file_read':
        return _artifactFileRead(call.arguments);
      case 'file_reader':
        return _fileReader(call.arguments);
      case 'artifact_file_write':
      case 'artifact_file_patch':
      case 'artifact_file_delete':
        return _asyncOnlyTool(name);
      case 'browser_save_page':
        return _browserSavePage(call.arguments);
      case 'webapp_preview':
        return _webappPreview(call.arguments);
      case 'webapp_lint':
        return _webappLint(call.arguments);
      case 'webapp_build':
        return _asyncOnlyTool(name);
      case 'project_scaffold':
        return _projectScaffold(call.arguments);
      case 'doc_create':
        return _docCreate(call.arguments);
      case 'doc_update':
        return _asyncOnlyTool(name);
      case 'slides_create':
        return _slidesCreate(call.arguments);
      case 'slides_from_markdown':
        return _slidesFromMarkdown(call.arguments);
      case 'slides_update':
        return _asyncOnlyTool(name);
      case 'slides_export':
        return _slidesExport(call.arguments);
      case 'chart_create':
        return _chartCreate(call.arguments);
      case 'sheet_create':
        return _sheetCreate(call.arguments);
      case 'sheet_read':
        return _sheetRead(call.arguments);
      case 'sheet_analyze':
        return _sheetAnalyze(call.arguments);
      case 'sheet_export':
        return _sheetExport(call.arguments);
      case 'sheet_update':
        return _asyncOnlyTool(name);
      case 'artifact_zip':
        return _artifactZip(call.arguments);
      case 'research_report_export':
        return _researchReportExport(call.arguments);
      case 'artifact_export':
        return _artifactExport(call.arguments);
      case 'static_site_export':
        return _staticSiteExport(call.arguments);
      case 'webapp_export_static':
        return _webappExportStatic(call.arguments);
      case 'doc_export':
        return _docExport(call.arguments);
      case 'pdf_export':
        return _binaryExportRequiresPc(name, 'pdf');
      case 'doc_to_pdf':
        return _binaryExportRequiresPc(name, 'pdf');
      case 'mobile_url_open':
      case 'media_clipboard_read':
      case 'media_clipboard_write':
      case 'media_file_pick':
      case 'media_screenshot':
      case 'media_image_transform':
      case 'media_ocr':
      case 'ocr_extract':
      case 'image_resize':
      case 'image_convert':
        return _asyncOnlyTool(name);
      case 'media_image_read':
        return _mediaImageRead(call.arguments);
      case 'media_doc_parse':
        return _mediaDocParse(call.arguments);
      case 'media_pdf_parse':
        return _mediaPdfParse(call.arguments);
      case 'pdf_extract':
        return _pdfExtract(call.arguments);
      case 'pdf_extract_tables':
        return _pdfExtractTables(call.arguments);
      case 'html_preview':
        return _htmlPreview(call.arguments);
      case 'pdf_preview':
        return _pdfPreview(call.arguments);
      case 'artifact_preview':
        return _artifactPreview(call.arguments);
      case 'tts_generate':
      case 'tts_generate_local':
        return _ttsGenerate(call.arguments, toolName: name);
      case 'image_render':
        return _imageRender(call.arguments);
      case 'image_generate_local_or_provider':
        return _imageGenerateLocalOrProvider(call.arguments);
      case 'audio_transcribe':
      case 'audio_transcribe_local':
        return _audioTranscribe(call.arguments, toolName: name);
      case 'source_extract':
        return _sourceExtract(call.arguments);
      case 'browser_extract_table':
        return _browserExtractTable(call.arguments);
      case 'source_rank':
        return _sourceRank(call.arguments);
      case 'tool_search':
        return _toolSearch(call.arguments);
      case 'tool_names':
        return _toolNames(call.arguments);
      case 'tool_list':
        return _toolList(call.arguments);
      case 'tool_schema':
        return _toolSchema(call.arguments);
      case 'tool_invoke':
        return _toolInvoke(call.arguments);
      case 'tool_batch':
        return _asyncOnlyTool(name);
      case 'package_install_plan':
        return _packageInstallPlan(call.arguments);
      case 'workflow_define':
        return _workflowDefine(call.arguments);
      case 'workflow_status':
        return _workflowStatus(call.arguments);
      case 'workflow_run':
      case 'workflow_cancel':
      case 'workflow_retry':
        return _asyncOnlyTool(name);
      case 'job_create':
        return _jobCreate(call.arguments);
      case 'job_status':
        return _jobStatus(call.arguments);
      case 'job_history':
        return _jobHistory(call.arguments);
      case 'job_artifacts':
        return _jobArtifacts(call.arguments);
      case 'job_cancel':
      case 'job_resume':
        return _asyncOnlyTool(name);
      case 'agent_plan':
        return _agentPlan(call.arguments);
      case 'agent_progress':
      case 'agent_status':
        return _agentStatus(call.arguments, name);
      case assistantProgressToolName:
        return _assistantProgress(call.arguments);
      default:
        return MobileToolResult(
          ok: false,
          summary: 'unsupported tool',
          output: _unsupportedReason(name),
        );
    }
  }

  Future<MobileToolResult> executeAsync(MobileToolCall call) async {
    final name = _canonicalToolName(call.name);
    if (_isAsyncPhoneToolName(name)) {
      final result = await _executeAsyncPhoneTool(
        MobileToolCall(id: call.id, name: name, arguments: call.arguments),
      );
      if (_pcDelegate != null &&
          !result.ok &&
          _shouldDelegateToPc(call, result)) {
        return _pcDelegate.invoke(call);
      }
      return result;
    }
    if (name == 'tool_invoke') {
      final requested =
          '${call.arguments['tool_name'] ?? call.arguments['tool_id'] ?? call.arguments['name'] ?? ''}'
              .trim();
      final requestedCanonical = _canonicalToolName(requested);
      if (_isAsyncPhoneToolName(requestedCanonical)) {
        return _toolInvokeAsync(call.arguments);
      }
    }
    final result = execute(call);
    if (_pcDelegate == null ||
        result.ok ||
        !_shouldDelegateToPc(call, result)) {
      return result;
    }
    return _pcDelegate.invoke(call);
  }

  Future<MobileToolResult> _executeAsyncPhoneTool(MobileToolCall call) {
    final name = _canonicalToolName(call.name);
    switch (name) {
      case 'mobile_url_open':
        return _mobileUrlOpen(call.arguments);
      case 'media_clipboard_read':
        return _mediaClipboardRead(call.arguments);
      case 'media_clipboard_write':
        return _mediaClipboardWrite(call.arguments);
      case 'media_file_pick':
        return _mediaFilePick(call.arguments);
      case 'media_screenshot':
        return _mediaScreenshot(call.arguments);
      case 'media_image_transform':
        return _mediaImageTransform(call.arguments);
      case 'media_ocr':
      case 'ocr_extract':
        return _mediaOcr(call.arguments, toolName: name);
      case 'image_resize':
      case 'image_convert':
        return _mediaImageTransform(
          _imageToolTransformArguments(call.arguments, toolName: name),
        );
      case 'artifact_file_write':
      case 'artifact_file_patch':
      case 'artifact_file_delete':
        return _artifactFileMutation(name, call.arguments);
      case 'sheet_update':
        return _sheetUpdate(call.arguments);
      case 'doc_update':
        return _docUpdate(call.arguments);
      case 'slides_update':
        return _slidesUpdate(call.arguments);
      case 'job_cancel':
      case 'job_resume':
        return _jobMutation(name, call.arguments);
      case 'workflow_run':
        return _workflowRun(call.arguments);
      case 'workflow_cancel':
        return _workflowCancel(call.arguments);
      case 'workflow_retry':
        return _workflowRetry(call.arguments);
      case 'webapp_build':
        return _webappBuild(call.arguments);
      case 'tool_batch':
        return _toolBatch(call.arguments);
      case 'ai_models':
      case 'ai_profiles':
      case 'ai_providers':
      case 'ai_get_provider_key_status':
      case 'ai_set_provider_key':
      case 'ai_delete_provider_key':
      case 'ai_get_preferred_model':
      case 'ai_set_preferred_model':
      case 'ai_get_thinking_level':
      case 'ai_set_thinking_level':
      case 'ai_get_effective_thinking_level':
      case 'ai_normalize_thinking_level':
      case 'ai_validate_model_params':
      case 'ai_recommend_model':
      case 'ai_route_model':
      case 'ai_explain_model_choice':
        return _aiModelTool(name, call.arguments);
      case 'prompt_active':
      case 'prompt_compact_prompt':
      case 'prompt_create':
      case 'prompt_delete':
      case 'prompt_lint_prompt':
      case 'prompt_list':
      case 'prompt_load_effective':
      case 'prompt_preview_toggle':
      case 'prompt_render':
      case 'prompt_resolve_for_conversation':
      case 'prompt_system_get':
      case 'prompt_system_set':
      case 'prompt_test':
      case 'prompt_update':
      case 'prompt_validate_template':
        return _promptTool(name, call.arguments);
      case 'memory_store':
      case 'memory_list':
      case 'memory_recall':
      case 'memory_update':
      case 'memory_delete':
      case 'memory_compact':
      case 'memory_project_context':
      case 'memory_resolve_for_agent':
      case 'memory_memo':
      case 'memory_memo_folders':
      case 'memory_memo_notes':
        return _memoryTool(name, call.arguments);
      case 'knowledge_create':
      case 'knowledge_get':
      case 'knowledge_list':
      case 'knowledge_update':
      case 'knowledge_delete':
      case 'knowledge_search':
      case 'knowledge_import_file':
      case 'knowledge_import_url':
      case 'knowledge_attach_to_project':
      case 'knowledge_index':
      case 'knowledge_reindex':
        return _knowledgeTool(name, call.arguments);
      default:
        return Future.value(execute(call));
    }
  }

  Future<MobileToolResult> _toolInvokeAsync(Map<String, dynamic> args) async {
    final requested =
        '${args['tool_name'] ?? args['tool_id'] ?? args['name'] ?? ''}'.trim();
    if (requested.isEmpty) return _toolInvoke(args);
    final canonical = _canonicalToolName(requested);
    if (!_isAsyncPhoneToolName(canonical)) return _toolInvoke(args);
    final tool = _findToolDefinition(canonical);
    final result = await _executeAsyncPhoneTool(
      MobileToolCall(
        id: 'tool_invoke:${DateTime.now().microsecondsSinceEpoch}',
        name: canonical,
        arguments: _invokeArguments(args),
      ),
    );
    return MobileToolResult(
      ok: result.ok,
      summary: '${tool?.name ?? canonical}: ${result.summary}',
      output: jsonEncode({
        'status': result.ok ? 'ok' : 'error',
        'data': {
          'tool_name': tool?.name ?? canonical,
          'requested_tool_name': requested,
          'result': result.output,
          'summary': result.summary,
          'is_error': !result.ok,
          'execution_location': 'phone',
        },
      }),
    );
  }

  Future<MobileToolResult> _toolBatch(Map<String, dynamic> args) async {
    final rawCalls = args['calls'];
    if (rawCalls is! List || rawCalls.isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'calls are required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'MISSING_PARAM',
            'message': 'calls must be a non-empty array',
          },
        }),
      );
    }
    if (rawCalls.length > _toolBatchMaxCalls) {
      return MobileToolResult(
        ok: false,
        summary: 'too many tool calls',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'TOO_MANY_CALLS',
            'message': 'tool_batch supports at most $_toolBatchMaxCalls calls',
            'max_calls': _toolBatchMaxCalls,
          },
        }),
      );
    }

    final parallel = args['parallel'] == true;
    Future<Map<String, dynamic>> runOne(int index, Object? rawCall) async {
      if (rawCall is! Map) {
        return {
          'index': index,
          'ok': false,
          'summary': 'call must be an object',
          'error': {
            'code': 'INVALID_CALL',
            'message': 'Each calls item must be an object.',
          },
        };
      }
      final item = rawCall.map((key, value) => MapEntry('$key', value));
      final requested =
          '${item['tool_name'] ?? item['tool_id'] ?? item['name'] ?? ''}'
              .trim();
      if (requested.isEmpty) {
        return {
          'index': index,
          'ok': false,
          'summary': 'tool_name is required',
          'error': {
            'code': 'MISSING_TOOL_NAME',
            'message': 'tool_name is required for every batch call.',
          },
        };
      }
      final canonical = _canonicalToolName(requested);
      final phoneLocal = _isMobileConnectorDryRunTool(canonical) ||
          (_findToolDefinition(canonical)?.available == true);
      if (canonical == 'tool_batch') {
        return {
          'index': index,
          'id': '${item['id'] ?? ''}',
          'tool_name': canonical,
          'requested_tool_name': requested,
          'ok': false,
          'summary': 'recursive tool_batch is not allowed',
          'error': {
            'code': 'RECURSIVE_TOOL_BATCH',
            'message': 'tool_batch cannot invoke itself.',
          },
        };
      }
      final result = await executeAsync(
        MobileToolCall(
          id: '${item['id'] ?? 'tool_batch:$index'}',
          name: requested,
          arguments: _invokeArguments(item),
        ),
      );
      final parsed = _decodeObject(result.output);
      final data = parsed['data'];
      final executionLocation = data is Map
          ? '${data['execution_location'] ?? ''}'.trim()
          : '${parsed['execution_location'] ?? ''}'.trim();
      return {
        'index': index,
        'id': '${item['id'] ?? ''}',
        'tool_name': canonical,
        'requested_tool_name': requested,
        'ok': result.ok,
        'summary': result.summary,
        'output': result.output,
        'parsed_output': parsed.isEmpty ? null : parsed,
        'execution_location': executionLocation.isEmpty
            ? (phoneLocal ? 'phone' : 'phone_or_pc')
            : executionLocation,
      };
    }

    final results = <Map<String, dynamic>>[];
    if (parallel) {
      results.addAll(await Future.wait([
        for (var index = 0; index < rawCalls.length; index++)
          runOne(index, rawCalls[index]),
      ]));
    } else {
      for (var index = 0; index < rawCalls.length; index++) {
        results.add(await runOne(index, rawCalls[index]));
      }
    }
    final allOk = results.every((result) => result['ok'] == true);
    return MobileToolResult(
      ok: allOk,
      summary: allOk
          ? 'tool_batch completed ${results.length} calls'
          : 'tool_batch completed with errors',
      output: jsonEncode({
        'status': allOk ? 'ok' : 'error',
        'data': {
          'parallel': parallel,
          'count': results.length,
          'ok_count': results.where((result) => result['ok'] == true).length,
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
          'results': results,
        },
      }),
    );
  }

  String _openAiToolDescription(MobileToolDefinition tool) {
    if (tool.name == 'tool_invoke' && pcDelegationAvailable) {
      return '${tool.description} The mobile tool surface is unified: phone-compatible tools run locally, and host-bound defaultspack tools route to the connected PC runtime when PC delegation is enabled.';
    }
    return tool.description;
  }

  bool _shouldDelegateToPc(MobileToolCall call, MobileToolResult result) {
    final name = _canonicalToolName(call.name);
    if (MobileToolRuntime.isAssistantProgressToolName(name)) return false;
    if (const {
      'tool_search',
      'tool_names',
      'tool_list',
      'tool_schema',
      'tool_batch',
      'workflow_define',
      'workflow_run',
      'workflow_status',
      'workflow_cancel',
      'workflow_retry',
      'job_create',
      'job_status',
      'job_history',
      'job_artifacts',
      'job_cancel',
      'job_resume',
      'agent_plan',
      'agent_progress',
      'agent_status',
      'todo',
      'tool_task_board',
      'task_board',
      'calculator',
      'tool_consent_check',
      'tool_consent_confirm',
      'current_time',
      'mobile_platform_info',
      'mobile_json',
      'mobile_base64',
      'mobile_uuid',
      'artifact_file_list',
      'artifact_file_read',
      'artifact_file_write',
      'artifact_file_patch',
      'artifact_file_delete',
      'browser_save_page',
      'webapp_preview',
      'webapp_lint',
      'project_scaffold',
      'sheet_create',
      'sheet_read',
      'sheet_analyze',
      'sheet_update',
      'artifact_zip',
      'static_site_export',
      'webapp_export_static',
      'mobile_url_open',
      'media_clipboard_read',
      'media_clipboard_write',
      'media_file_pick',
      'media_screenshot',
      'media_image_transform',
      'media_ocr',
      'ocr_extract',
      'media_image_read',
      'media_doc_parse',
      'media_pdf_parse',
      'html_preview',
      'pdf_preview',
      'artifact_preview',
      'source_rank',
      'source_extract',
      'browser_extract_table',
      'tts_generate',
      'tts_generate_local',
    }.contains(name)) {
      return false;
    }
    if (name == 'tool_invoke') {
      final requested =
          '${call.arguments['tool_name'] ?? call.arguments['tool_id'] ?? call.arguments['name'] ?? ''}'
              .trim();
      final requestedCanonical = _canonicalToolName(requested);
      final requestedTool = _findToolDefinition(requestedCanonical);
      final output = result.output.toLowerCase();
      if (requestedTool != null && requestedTool.available) {
        return output.contains('pc_delegation_required') ||
            output.contains('pc delegation') ||
            output.contains('pc runtime');
      }
      return true;
    }
    final output = result.output.toLowerCase();
    return result.summary == 'unsupported tool' ||
        result.summary.contains('unavailable on phone') ||
        output.contains('tool_unavailable_on_phone') ||
        output.contains('pc_delegation_required') ||
        output.contains('pc delegation') ||
        output.contains('pc側') ||
        output.contains('pc runtime') ||
        output.contains('pc接続時');
  }

  MobileToolResult _calculator(Map<String, dynamic> args) {
    final expression = '${args['expression'] ?? args['input'] ?? ''}'.trim();
    if (expression.isEmpty) {
      return const MobileToolResult(
        ok: false,
        summary: 'expression is required',
        output: 'expression is required',
      );
    }
    try {
      final value = _ExpressionParser(expression).parse();
      final normalized = _formatNumber(value);
      return MobileToolResult(
        ok: true,
        summary: normalized,
        output: '$expression = $normalized',
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'calculation failed',
        output: '計算できませんでした: $error',
      );
    }
  }

  MobileToolResult _toolConsentCheck(Map<String, dynamic> args) {
    final text = '${args['text'] ?? args['input'] ?? ''}';
    if (text.trim().isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'text is required',
        output: jsonEncode({
          'status': 'error',
          'error': {'code': 'MISSING_PARAM', 'message': 'text is required'},
        }),
      );
    }
    final lower = text.toLowerCase();
    final categories = <String>[];
    for (final entry in _mobileConsentCategories.entries) {
      final keywords = entry.value['keywords'];
      if (keywords is! List<String>) continue;
      if (keywords.any((keyword) => lower.contains(keyword.toLowerCase()))) {
        categories.add(entry.key);
      }
    }
    categories.sort();
    final requiresConsent = categories.isNotEmpty;
    String? consentId;
    final disclaimers = <String, String>{};
    if (requiresConsent) {
      consentId = _nextToolId('consent');
      for (final category in categories) {
        final disclaimer = _mobileConsentCategories[category]?['disclaimer'];
        if (disclaimer is String) disclaimers[category] = disclaimer;
      }
      _mobileConsents[consentId] = {
        'consent_id': consentId,
        'categories': categories,
        'accepted': false,
        'created_at': DateTime.now().toUtc().toIso8601String(),
        'accepted_at': null,
      };
    }
    final useAi = args['use_ai'] == true;
    return MobileToolResult(
      ok: true,
      summary: requiresConsent
          ? 'consent required: ${categories.join(', ')}'
          : 'consent not required',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'requires_consent': requiresConsent,
          'categories': categories,
          'consent_id': consentId,
          'disclaimers': disclaimers,
          'classifier': 'mobile_keyword',
          if (useAi)
            'ai_classification_skipped':
                'mobile tool_consent_check currently uses the built-in keyword classifier; AI consent classification can be delegated to the PC runtime.',
        },
      }),
    );
  }

  MobileToolResult _toolConsentConfirm(Map<String, dynamic> args) {
    final consentId = '${args['consent_id'] ?? args['id'] ?? ''}'.trim();
    if (consentId.isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'consent_id is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'MISSING_PARAM',
            'message': 'consent_id is required',
          },
        }),
      );
    }
    final accepted = args['accepted'];
    if (accepted is! bool) {
      return MobileToolResult(
        ok: false,
        summary: 'accepted is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'INVALID_PARAM',
            'message': 'accepted must be a boolean',
          },
        }),
      );
    }
    final record = _mobileConsents[consentId];
    if (record == null) {
      return MobileToolResult(
        ok: false,
        summary: 'consent not found',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'NOT_FOUND',
            'message': "consent_id '$consentId' not found",
          },
        }),
      );
    }
    record['accepted'] = accepted;
    record['accepted_at'] =
        accepted ? DateTime.now().toUtc().toIso8601String() : null;
    return MobileToolResult(
      ok: true,
      summary: accepted ? 'consent accepted' : 'consent rejected',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'consent_id': consentId,
          'accepted': accepted,
          'accepted_at': record['accepted_at'],
        },
      }),
    );
  }

  MobileToolResult _currentTime(Map<String, dynamic> args) {
    final now = DateTime.now();
    final format = '${args['format'] ?? 'human'}'.trim();
    if (format == 'iso8601') {
      return MobileToolResult(
        ok: true,
        summary: now.toIso8601String(),
        output: now.toIso8601String(),
      );
    }
    final offset = now.timeZoneOffset;
    final sign = offset.isNegative ? '-' : '+';
    final abs = offset.abs();
    final hh = abs.inHours.toString().padLeft(2, '0');
    final mm = (abs.inMinutes % 60).toString().padLeft(2, '0');
    final text =
        '${now.toIso8601String()} (${now.timeZoneName}, UTC$sign$hh:$mm)';
    return MobileToolResult(ok: true, summary: text, output: text);
  }

  MobileToolResult _mobilePlatformInfo(Map<String, dynamic> args) {
    final platform = _currentMobilePlatform();
    final data = {
      'platform': platform,
      'supported_platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_bridge_layers': const {
        'ios': 'Swift MethodChannel',
        'android': 'Kotlin MethodChannel',
      },
      'dart': {
        'version': Platform.version,
        'locale': Platform.localeName,
        'processors': Platform.numberOfProcessors,
      },
      'os': {
        'name': Platform.operatingSystem,
        'version': Platform.operatingSystemVersion,
      },
      'tool_runtime': {
        'mobile_compatible_tag': mobileCompatibleTag,
        'platform_tags': const [
          mobileFlutterTag,
          mobileIosTag,
          mobileAndroidTag,
          mobileSwiftNativeTag,
          mobileKotlinNativeTag,
        ],
      },
    };
    return MobileToolResult(
      ok: true,
      summary: 'mobile platform: $platform',
      output: jsonEncode({'status': 'ok', 'data': data}),
    );
  }

  MobileToolResult _todo(Map<String, dynamic> args) {
    final action = '${args['action'] ?? 'list'}'.trim().toLowerCase();
    try {
      Map<String, dynamic>? changed;
      if (action == 'add' || action == 'create') {
        final title = '${args['title'] ?? args['task'] ?? ''}'.trim();
        if (title.isEmpty) throw const FormatException('title is required');
        changed = {
          'id': _nextToolId('todo'),
          'title': title,
          'status': '${args['status'] ?? 'todo'}',
          'priority': '${args['priority'] ?? 'normal'}',
          'notes': '${args['notes'] ?? ''}',
          'created_at': DateTime.now().millisecondsSinceEpoch,
          'updated_at': DateTime.now().millisecondsSinceEpoch,
        };
        _mobileTodos.add(changed);
      } else if (action == 'complete' || action == 'done') {
        final todo = _findById(_mobileTodos, _argId(args, 'todo_id'));
        if (todo == null) throw const FormatException('todo_id not found');
        todo['status'] = 'done';
        todo['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        changed = Map<String, dynamic>.from(todo);
      } else if (action == 'update' || action == 'edit') {
        final todo = _findById(_mobileTodos, _argId(args, 'todo_id'));
        if (todo == null) throw const FormatException('todo_id not found');
        for (final key in ['title', 'status', 'priority', 'notes']) {
          if (args.containsKey(key) && args[key] != null) {
            todo[key] = '${args[key]}';
          }
        }
        todo['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        changed = Map<String, dynamic>.from(todo);
      } else if (action == 'remove' || action == 'delete') {
        final id = _argId(args, 'todo_id');
        final before = _mobileTodos.length;
        _mobileTodos.removeWhere((todo) => todo['id'] == id);
        if (_mobileTodos.length == before) {
          throw const FormatException('todo_id not found');
        }
        changed = {'id': id};
      } else if (action == 'clear') {
        changed = {'cleared': _mobileTodos.length};
        _mobileTodos.clear();
      } else if (action != 'list' && action != 'show') {
        throw FormatException('Unsupported todo action: $action');
      }
      final openCount =
          _mobileTodos.where((todo) => todo['status'] != 'done').length;
      final summary = changed != null && '${changed['title'] ?? ''}'.isNotEmpty
          ? '$action: ${changed['title']}; ${_mobileTodos.length} todos ($openCount open)'
          : '${_mobileTodos.length} todos ($openCount open)';
      return MobileToolResult(
        ok: true,
        summary: summary,
        output: jsonEncode({
          'action': action,
          'summary': summary,
          'todos': _mobileTodos,
          'changed': changed,
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'todo failed',
        output: '$error',
      );
    }
  }

  MobileToolResult _taskBoard(Map<String, dynamic> args) {
    final action = '${args['action'] ?? 'list'}'.trim().toLowerCase();
    try {
      Map<String, dynamic>? changed;
      if (action == 'configure' ||
          action == 'configure_columns' ||
          action == 'set_columns' ||
          action == 'columns') {
        _mobileTaskBoard['columns'] = _normalizeColumns(args['columns']);
        changed = {'columns': _mobileTaskBoard['columns']};
      } else if (action == 'create' || action == 'add') {
        final title = '${args['title'] ?? args['task'] ?? ''}'.trim();
        if (title.isEmpty) throw const FormatException('title is required');
        final card = {
          'id': _nextToolId('card'),
          'title': title,
          'column': _resolveColumn(args),
          'status': '${args['status'] ?? 'todo'}',
          'priority': '${args['priority'] ?? 'normal'}',
          'notes': '${args['notes'] ?? ''}',
          'assignee': '${args['assignee'] ?? ''}',
          'subtasks': <Map<String, dynamic>>[],
          'blocked_by': <String>[],
          'blocker_reason': '',
          'created_at': DateTime.now().millisecondsSinceEpoch,
          'updated_at': DateTime.now().millisecondsSinceEpoch,
        };
        _mobileTaskCards.add(card);
        changed = Map<String, dynamic>.from(card);
      } else if (action == 'update' || action == 'edit') {
        final card = _findById(_mobileTaskCards, _argId(args, 'card_id'));
        if (card == null) throw const FormatException('card_id not found');
        for (final key in [
          'title',
          'status',
          'priority',
          'notes',
          'assignee'
        ]) {
          if (args.containsKey(key) && args[key] != null) {
            card[key] = '${args[key]}';
          }
        }
        if (args['column'] != null || args['column_id'] != null) {
          card['column'] = _resolveColumn(args);
        }
        card['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        changed = Map<String, dynamic>.from(card);
      } else if (action == 'move') {
        final card = _findById(_mobileTaskCards, _argId(args, 'card_id'));
        if (card == null) throw const FormatException('card_id not found');
        card['column'] = _resolveColumn(args);
        card['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        changed = Map<String, dynamic>.from(card);
      } else if (action == 'block' || action == 'unblock') {
        final card = _findById(_mobileTaskCards, _argId(args, 'card_id'));
        if (card == null) throw const FormatException('card_id not found');
        if (action == 'block') {
          card['blocker_reason'] =
              '${args['blocker_reason'] ?? args['reason'] ?? ''}';
          final blocker =
              '${args['blocked_by'] ?? args['depends_on'] ?? ''}'.trim();
          card['blocked_by'] = blocker.isEmpty ? <String>[] : [blocker];
        } else {
          card['blocker_reason'] = '';
          card['blocked_by'] = <String>[];
        }
        card['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        changed = Map<String, dynamic>.from(card);
      } else if (action == 'subtask_add' || action == 'add_subtask') {
        final card = _findById(_mobileTaskCards, _argId(args, 'card_id'));
        if (card == null) throw const FormatException('card_id not found');
        final title = '${args['title'] ?? args['task'] ?? ''}'.trim();
        if (title.isEmpty) throw const FormatException('title is required');
        final subtask = {
          'id': _nextToolId('subtask'),
          'title': title,
          'done': false,
          'status': 'todo',
          'assignee': '${args['assignee'] ?? ''}',
          'created_at': DateTime.now().millisecondsSinceEpoch,
          'updated_at': DateTime.now().millisecondsSinceEpoch,
        };
        _subtasks(card).add(subtask);
        card['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        changed = Map<String, dynamic>.from(card);
      } else if (action == 'subtask_update' ||
          action == 'subtask_complete' ||
          action == 'subtask_remove' ||
          action == 'remove_subtask') {
        final card = _findById(_mobileTaskCards, _argId(args, 'card_id'));
        if (card == null) throw const FormatException('card_id not found');
        final subtasks = _subtasks(card);
        final subtask = _findById(subtasks, _argId(args, 'subtask_id'));
        if (subtask == null) {
          throw const FormatException('subtask_id not found');
        }
        if (action == 'subtask_remove' || action == 'remove_subtask') {
          subtasks.removeWhere((item) => item['id'] == subtask['id']);
        } else {
          for (final key in ['title', 'status', 'notes', 'assignee']) {
            if (args.containsKey(key) && args[key] != null) {
              subtask[key] = '${args[key]}';
            }
          }
          if (action == 'subtask_complete') {
            subtask['done'] = true;
            subtask['status'] = 'done';
          } else if (args.containsKey('done')) {
            subtask['done'] = args['done'] == true;
          }
          subtask['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        }
        card['updated_at'] = DateTime.now().millisecondsSinceEpoch;
        changed = Map<String, dynamic>.from(card);
      } else if (action == 'delete' || action == 'remove') {
        final id = _argId(args, 'card_id');
        final before = _mobileTaskCards.length;
        _mobileTaskCards.removeWhere((card) => card['id'] == id);
        if (_mobileTaskCards.length == before) {
          throw const FormatException('card_id not found');
        }
        changed = {'id': id};
      } else if (action == 'clear') {
        changed = {'cleared': _mobileTaskCards.length};
        _mobileTaskCards.clear();
      } else if (action != 'list' && action != 'show') {
        throw FormatException('Unsupported task_board action: $action');
      }
      final summary = _taskBoardSummary(action);
      return MobileToolResult(
        ok: true,
        summary: summary,
        output: jsonEncode({
          'action': action,
          'summary': summary,
          'board': _taskBoardSnapshot(),
          'changed': changed,
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'task_board failed',
        output: '$error',
      );
    }
  }

  MobileToolResult _mobileJson(Map<String, dynamic> args) {
    final action = '${args['action'] ?? 'format'}'.trim().toLowerCase();
    final raw = args.containsKey('json')
        ? jsonEncode(args['json'])
        : '${args['text'] ?? args['input'] ?? ''}'.trim();
    if (raw.isEmpty) {
      return _jsonToolError('MISSING_INPUT', 'text or json is required');
    }
    try {
      final decoded = jsonDecode(raw);
      final output = switch (action) {
        'minify' => jsonEncode(decoded),
        'validate' => jsonEncode({
            'status': 'ok',
            'valid': true,
            'type': decoded.runtimeType.toString(),
          }),
        _ =>
          JsonEncoder.withIndent('${args['indent'] ?? '  '}').convert(decoded),
      };
      return MobileToolResult(
        ok: true,
        summary: action == 'validate' ? 'valid JSON' : 'JSON $action',
        output: output,
      );
    } catch (error) {
      return _jsonToolError('INVALID_JSON', '$error');
    }
  }

  MobileToolResult _jsonToolError(String code, String message) {
    return MobileToolResult(
      ok: false,
      summary: 'JSON failed',
      output: jsonEncode({
        'status': 'error',
        'error': {'code': code, 'message': message},
      }),
    );
  }

  MobileToolResult _mobileBase64(Map<String, dynamic> args) {
    final action = '${args['action'] ?? 'encode'}'.trim().toLowerCase();
    final text = '${args['text'] ?? args['data'] ?? args['input'] ?? ''}';
    try {
      if (action == 'decode') {
        final bytes = base64.decode(text.trim());
        final decoded = utf8.decode(bytes);
        return MobileToolResult(
          ok: true,
          summary: 'base64 decoded',
          output: decoded,
        );
      }
      final encoded = base64.encode(utf8.encode(text));
      return MobileToolResult(
        ok: true,
        summary: 'base64 encoded',
        output: encoded,
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'base64 failed',
        output: jsonEncode({
          'status': 'error',
          'error': {'code': 'INVALID_BASE64', 'message': '$error'},
        }),
      );
    }
  }

  MobileToolResult _mediaImageRead(Map<String, dynamic> args) {
    final maxBytes = _boundedImageReadMaxBytes(args['max_bytes']);
    final base64Image = _extractImageBase64(args);
    if (base64Image == null || base64Image.trim().isEmpty) {
      final path = '${args['path'] ?? ''}'.trim();
      return MobileToolResult(
        ok: false,
        summary: 'image bytes are required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': path.isEmpty ? 'MISSING_IMAGE_BYTES' : 'UNSUPPORTED_PATH',
            'message': path.isEmpty
                ? 'base64, image_base64, data_url, image.base64, or file.base64 is required.'
                : 'Phone-local media_image_read cannot read host file paths. Use media_file_pick or pass base64 image bytes.',
            if (path.isNotEmpty) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }
    try {
      final bytes = base64Decode(_stripDataUrlPrefix(base64Image));
      if (bytes.length > maxBytes) {
        return MobileToolResult(
          ok: false,
          summary: 'image is too large',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'IMAGE_TOO_LARGE',
              'message': 'Image bytes are larger than max_bytes.',
              'size_bytes': bytes.length,
              'max_bytes': maxBytes,
              'execution_location': 'phone',
            },
          }),
        );
      }
      final metadata = _readImageHeader(bytes);
      if (metadata == null) {
        return MobileToolResult(
          ok: false,
          summary: 'unsupported image format',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'UNSUPPORTED_IMAGE_FORMAT',
              'message':
                  'Only PNG, JPEG, GIF, WebP, and BMP headers are supported on this phone.',
              'size_bytes': bytes.length,
              'execution_location': 'phone',
            },
          }),
        );
      }
      return MobileToolResult(
        ok: true,
        summary: '${metadata.format} ${metadata.width}x${metadata.height}',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'width': metadata.width,
            'height': metadata.height,
            'format': metadata.format,
            'mime_type': metadata.mimeType,
            'size_bytes': bytes.length,
            'execution_location': 'phone',
            'runtime_layers': _flutterRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'image read failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'IMAGE_READ_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  Future<MobileToolResult> _mediaImageTransform(
    Map<String, dynamic> args,
  ) async {
    final base64Image = _extractImageBase64(args);
    if (base64Image == null || base64Image.trim().isEmpty) {
      final path = '${args['path'] ?? ''}'.trim();
      return MobileToolResult(
        ok: false,
        summary: 'image bytes are required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': path.isEmpty ? 'MISSING_IMAGE_BYTES' : 'UNSUPPORTED_PATH',
            'message': path.isEmpty
                ? 'base64, image_base64, data_url, image.base64, or file.base64 is required.'
                : 'Phone-local media_image_transform cannot read host file paths. Use media_file_pick or pass base64 image bytes.',
            if (path.isNotEmpty) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }

    try {
      final bytes = base64Decode(_stripDataUrlPrefix(base64Image));
      final maxInputBytes =
          _boundedImageTransformInputMaxBytes(args['max_input_bytes']);
      if (bytes.length > maxInputBytes) {
        return MobileToolResult(
          ok: false,
          summary: 'image is too large',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'IMAGE_TOO_LARGE',
              'message': 'Image bytes are larger than max_input_bytes.',
              'size_bytes': bytes.length,
              'max_input_bytes': maxInputBytes,
              'execution_location': 'phone',
            },
          }),
        );
      }

      final format = _normalizeImageTransformFormat(
        args['format'] ?? args['output_format'] ?? args['mime_type'],
      );
      final quality = _boundedImageQuality(args['quality']);
      final dimensions = _imageTransformDimensions(args);
      final maxBytes = _boundedImageTransformOutputMaxBytes(args['max_bytes']);
      final transformed = await _imageTransformer.transform(
        base64Data: base64Encode(bytes),
        outputFormat: format,
        quality: quality,
        maxWidth: dimensions.maxWidth,
        maxHeight: dimensions.maxHeight,
        maxBytes: maxBytes,
      );
      if (transformed.size > maxBytes) {
        return MobileToolResult(
          ok: false,
          summary: 'transformed image is too large',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'IMAGE_TRANSFORM_TOO_LARGE',
              'message': 'Transformed image is larger than max_bytes.',
              'size': transformed.size,
              'max_bytes': maxBytes,
              'execution_location': 'phone',
            },
          }),
        );
      }
      final metadata = _readImageHeader(bytes);
      final operations = _imageTransformOperationsApplied(
        args,
        format: format,
        dimensions: dimensions,
      );
      return MobileToolResult(
        ok: true,
        summary: 'transformed image ${transformed.width}x${transformed.height}',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'mime_type': transformed.mimeType,
            'format': _mimeToImageFormat(transformed.mimeType, format),
            'size': transformed.size,
            'size_bytes': transformed.size,
            'width': transformed.width,
            'height': transformed.height,
            'base64': transformed.base64Data,
            'encoding': 'base64',
            'operations_applied': operations,
            'quality': quality,
            'input': {
              'size_bytes': bytes.length,
              if (metadata != null) 'width': metadata.width,
              if (metadata != null) 'height': metadata.height,
              if (metadata != null) 'format': metadata.format,
              if (metadata != null) 'mime_type': metadata.mimeType,
            },
            'execution_location': 'phone',
            'runtime_layers': _nativeImageTransformRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'image transform failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'IMAGE_TRANSFORM_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  Future<MobileToolResult> _mediaOcr(
    Map<String, dynamic> args, {
    required String toolName,
  }) async {
    final base64Image = _extractImageBase64(args);
    if (base64Image == null || base64Image.trim().isEmpty) {
      final path = '${args['path'] ?? ''}'.trim();
      return MobileToolResult(
        ok: false,
        summary: 'image bytes are required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': path.isEmpty ? 'MISSING_IMAGE_BYTES' : 'UNSUPPORTED_PATH',
            'message': path.isEmpty
                ? 'base64, image_base64, data_url, image.base64, or file.base64 is required.'
                : 'Phone-local $toolName cannot read host artifact paths. Use media_file_pick/pass image bytes or route this call to the connected PC runtime.',
            if (path.isNotEmpty) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }

    try {
      final bytes = base64Decode(_stripDataUrlPrefix(base64Image));
      final maxBytes = _boundedOcrMaxBytes(args['max_bytes']);
      if (bytes.length > maxBytes) {
        return MobileToolResult(
          ok: false,
          summary: 'image is too large',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'IMAGE_TOO_LARGE',
              'message': 'Image bytes are larger than max_bytes.',
              'size_bytes': bytes.length,
              'max_bytes': maxBytes,
              'execution_location': 'phone',
            },
          }),
        );
      }
      final metadata = _readImageHeader(bytes);
      final languageHint = _stringOrNull(
        args['language_hint'] ?? args['language'] ?? args['locale'],
      );
      final recognized = await _ocrRecognizer.recognize(
        base64Data: base64Encode(bytes),
        maxBytes: maxBytes,
        languageHint: languageHint,
      );
      final text = recognized.text.trim();
      return MobileToolResult(
        ok: true,
        summary: text.isEmpty
            ? 'OCR found no text'
            : 'OCR ${text.length} chars: ${_clampText(text.replaceAll(RegExp(r'\s+'), ' '), 80)}',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'text': text,
            'content': text,
            'length': text.length,
            'blocks': recognized.blocks.map((block) => block.toJson()).toList(),
            'block_count': recognized.blocks.length,
            if (recognized.languageCode != null)
              'language_code': recognized.languageCode,
            'input': {
              'size_bytes': bytes.length,
              if (metadata != null) 'width': metadata.width,
              if (metadata != null) 'height': metadata.height,
              if (metadata != null) 'format': metadata.format,
              if (metadata != null) 'mime_type': metadata.mimeType,
            },
            'metadata': {
              'tool': toolName,
              'payload_only': true,
              'native_ocr': true,
              if (languageHint != null) 'language_hint': languageHint,
            },
            'execution_location': 'phone',
            'runtime_layers': _nativeOcrRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'OCR failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'OCR_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  MobileToolResult _mediaDocParse(Map<String, dynamic> args) {
    final _DocumentPayload? payload;
    try {
      payload = _extractDocumentPayload(args);
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'document content is invalid',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'INVALID_DOCUMENT_CONTENT',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
    if (payload == null) {
      final path = '${args['path'] ?? ''}'.trim();
      return MobileToolResult(
        ok: false,
        summary: 'document content is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code':
                path.isEmpty ? 'MISSING_DOCUMENT_CONTENT' : 'UNSUPPORTED_PATH',
            'message': path.isEmpty
                ? 'text, content, base64, data_url, file.base64, or document.base64 is required.'
                : 'Phone-local media_doc_parse cannot read host file paths. Use media_file_pick or pass document text/base64.',
            if (path.isNotEmpty) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }

    final maxBytes = _boundedDocParseMaxBytes(args['max_bytes']);
    final maxChars = _boundedDocParseMaxChars(args['max_chars']);
    if (payload.sizeBytes > maxBytes) {
      return MobileToolResult(
        ok: false,
        summary: 'document is too large',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'DOCUMENT_TOO_LARGE',
            'message': 'Document content is larger than max_bytes.',
            'size_bytes': payload.sizeBytes,
            'max_bytes': maxBytes,
            'execution_location': 'phone',
          },
        }),
      );
    }
    final format = _normalizeDocumentFormat(
      args['format'] ?? payload.format,
      mimeType: '${args['mime_type'] ?? payload.mimeType ?? ''}',
      name: '${args['name'] ?? payload.name ?? ''}',
    );
    final unsupportedReason = _unsupportedPhoneDocumentReason(
      format: format,
      mimeType: payload.mimeType,
      name: payload.name,
      explicitText: payload.text != null,
    );
    if (unsupportedReason != null) {
      return MobileToolResult(
        ok: false,
        summary: 'document format is unsupported on phone',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'UNSUPPORTED_DOCUMENT_FORMAT',
            'message': unsupportedReason,
            'format': format,
            if (payload.mimeType != null) 'mime_type': payload.mimeType,
            if (payload.name != null) 'name': payload.name,
            'execution_location': 'phone',
          },
        }),
      );
    }

    try {
      final decoded = payload.text == null
          ? _decodeDocumentBytes(payload.bytes ?? Uint8List(0), maxBytes)
          : null;
      final decodedText = payload.text ?? decoded!.text;
      final parsed = _parsePhoneDocumentText(
        decodedText,
        format: format,
        stripHtml: args['strip_html'] != false,
      );
      final truncated = parsed.length > maxChars;
      final content = truncated ? parsed.substring(0, maxChars) : parsed;
      return MobileToolResult(
        ok: true,
        summary: 'parsed ${content.length} chars',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'content': content,
            'truncated': truncated,
            'length': parsed.length,
            'returned_length': content.length,
            'metadata': {
              'format': format,
              if (payload.name != null) 'name': payload.name,
              if (payload.mimeType != null) 'mime_type': payload.mimeType,
              'encoding': payload.text != null ? 'string' : decoded!.encoding,
              'source': payload.source,
              'size_bytes': payload.sizeBytes,
              'supported_formats': _phoneTextDocumentFormats.toList()..sort(),
            },
            'execution_location': 'phone',
            'runtime_layers': _flutterRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    } on _UnsupportedDocumentEncoding catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'document encoding unsupported',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'UNSUPPORTED_DOCUMENT_ENCODING',
            'message': error.message,
            'execution_location': 'phone',
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'document parse failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'DOCUMENT_PARSE_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  MobileToolResult _mediaPdfParse(Map<String, dynamic> args) {
    final _DocumentPayload? payload;
    try {
      payload = _extractDocumentPayload(args);
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'PDF content is invalid',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'INVALID_PDF_CONTENT',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
    if (payload == null) {
      final path = '${args['path'] ?? ''}'.trim();
      return MobileToolResult(
        ok: false,
        summary: 'PDF bytes are required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': path.isEmpty ? 'MISSING_PDF_BYTES' : 'UNSUPPORTED_PATH',
            'message': path.isEmpty
                ? 'base64, pdf_base64, data_url, file.base64, or document.base64 is required.'
                : 'Phone-local media_pdf_parse cannot read host file paths. Use media_file_pick or pass PDF base64 bytes.',
            if (path.isNotEmpty) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }
    final maxBytes = _boundedPdfParseMaxBytes(args['max_bytes']);
    final maxChars = _boundedPdfParseMaxChars(args['max_chars']);
    if (payload.sizeBytes > maxBytes) {
      return MobileToolResult(
        ok: false,
        summary: 'PDF is too large',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'PDF_TOO_LARGE',
            'message': 'PDF content is larger than max_bytes.',
            'size_bytes': payload.sizeBytes,
            'max_bytes': maxBytes,
            'execution_location': 'phone',
          },
        }),
      );
    }

    try {
      final bytes = payload.bytes ??
          Uint8List.fromList(latin1.encode(payload.text ?? ''));
      final raw = _decodePdfBytesForScan(bytes);
      final looksPdf = raw.startsWith('%PDF') ||
          (payload.mimeType ?? '').trim().toLowerCase() == 'application/pdf' ||
          (payload.name ?? '').trim().toLowerCase().endsWith('.pdf');
      final extraction = _extractBestEffortPdfText(raw);
      final text = extraction.text;
      final truncated = text.length > maxChars;
      final content = truncated ? text.substring(0, maxChars) : text;
      return MobileToolResult(
        ok: true,
        summary: content.isEmpty
            ? 'PDF parsed with no extractable text'
            : 'parsed PDF ${content.length} chars',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'content': content,
            'text': content,
            'truncated': truncated,
            'length': text.length,
            'returned_length': content.length,
            'metadata': {
              'format': 'pdf',
              if (payload.name != null) 'name': payload.name,
              if (payload.mimeType != null) 'mime_type': payload.mimeType,
              'source': payload.source,
              'size_bytes': payload.sizeBytes,
              'looks_like_pdf': looksPdf,
              'method': extraction.method,
              'best_effort': true,
              'full_layout_supported': false,
              'tables_supported': false,
            },
            'execution_location': 'phone',
            'runtime_layers': _flutterRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'PDF parse failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'PDF_PARSE_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  MobileToolResult _pdfExtract(Map<String, dynamic> args) {
    final parsed = _mediaPdfParse(args);
    if (!parsed.ok) return parsed;
    final payload = _decodeObject(parsed.output);
    final data = payload['data'];
    final parsedData = data is Map<String, dynamic>
        ? data
        : data is Map
            ? data.map((key, value) => MapEntry('$key', value))
            : const <String, dynamic>{};
    final metadataRaw = parsedData['metadata'];
    final metadata = metadataRaw is Map<String, dynamic>
        ? metadataRaw
        : metadataRaw is Map
            ? metadataRaw.map((key, value) => MapEntry('$key', value))
            : const <String, dynamic>{};
    final text = '${parsedData['text'] ?? parsedData['content'] ?? ''}';
    return MobileToolResult(
      ok: true,
      summary: 'extracted PDF ${text.length} chars',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'text': text,
          'content': text,
          'fallback': metadata['method'] ?? 'best_effort_phone_pdf_scan',
          'length': parsedData['length'] ?? text.length,
          'returned_length': parsedData['returned_length'] ?? text.length,
          'truncated': parsedData['truncated'] ?? false,
          'metadata': {
            ...metadata,
            'payload_only': true,
            'tables_supported': false,
          },
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _pdfExtractTables(Map<String, dynamic> args) {
    final parsed = _mediaPdfParse({...args, 'max_chars': 4000});
    if (!parsed.ok) return parsed;
    final payload = _decodeObject(parsed.output);
    final data = payload['data'];
    final parsedData = data is Map<String, dynamic>
        ? data
        : data is Map
            ? data.map((key, value) => MapEntry('$key', value))
            : const <String, dynamic>{};
    return MobileToolResult(
      ok: true,
      summary: 'PDF table extraction fallback returned 0 tables',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'tables': const <dynamic>[],
          'table_count': 0,
          'text_preview': parsedData['content'] ?? '',
          'missing_dependency':
              'Full PDF table extraction requires the connected PC/provider runtime.',
          'metadata': {
            'payload_only': true,
            'best_effort': true,
            'tables_supported': false,
          },
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _htmlPreview(Map<String, dynamic> args) {
    final payload = _extractHtmlTablePayload(args);
    if (payload == null) {
      final path = _stringOrNull(args['path']);
      return MobileToolResult(
        ok: false,
        summary: 'HTML payload is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': path == null ? 'MISSING_HTML_PAYLOAD' : 'UNSUPPORTED_PATH',
            'message': path == null
                ? 'html, content, source, data_url, file.base64, or document.base64 is required.'
                : 'Phone-local html_preview cannot read PC artifact paths. Pass HTML payload or route this call to the connected PC runtime.',
            if (path != null) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }
    final maxBytes = _boundedArtifactPreviewMaxBytes(args['max_bytes']);
    final sizeBytes = utf8.encode(payload.html).length;
    if (sizeBytes > maxBytes) {
      return _artifactPreviewTooLarge(sizeBytes, maxBytes);
    }
    final maxChars = _boundedArtifactPreviewMaxChars(args['max_chars']);
    final html = payload.html.trim();
    final truncated = html.length > maxChars;
    final content = truncated ? html.substring(0, maxChars) : html;
    final title = _extractHtmlTitle(html) ?? 'HTML Preview';
    return MobileToolResult(
      ok: true,
      summary: 'preview HTML ${content.length} chars',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'kind': 'html',
          'title': title,
          'content': content,
          'text': _stripHtmlToText(content).trim(),
          'truncated': truncated,
          'length': html.length,
          'returned_length': content.length,
          'viewport': _previewViewport(args['viewport']),
          'full_page': _boolArg(args['full_page'], fallback: true),
          'fallback': 'phone_payload_metadata',
          'screenshot_supported': false,
          'metadata': {
            'payload_only': true,
            'source': payload.source,
            'size_bytes': sizeBytes,
            'preview_mode': 'html_payload',
          },
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _pdfPreview(Map<String, dynamic> args) {
    final path = _stringOrNull(args['path']);
    final parsed = _mediaPdfParse({
      ...args,
      'max_bytes': args['max_bytes'] ?? _defaultArtifactPreviewMaxBytes,
      'max_chars': args['max_chars'] ?? _defaultArtifactPreviewMaxChars,
    });
    if (!parsed.ok) {
      if (path != null) {
        return MobileToolResult(
          ok: false,
          summary: 'PDF payload is required',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'UNSUPPORTED_PATH',
              'message':
                  'Phone-local pdf_preview cannot read PC artifact paths. Pass PDF bytes or route this call to the connected PC runtime.',
              'path': path,
              'execution_location': 'phone',
            },
          }),
        );
      }
      return parsed;
    }
    final payload = _decodeObject(parsed.output);
    final data = payload['data'];
    final parsedData = data is Map<String, dynamic>
        ? data
        : data is Map
            ? data.map((key, value) => MapEntry('$key', value))
            : const <String, dynamic>{};
    final metadataRaw = parsedData['metadata'];
    final metadata = metadataRaw is Map<String, dynamic>
        ? metadataRaw
        : metadataRaw is Map
            ? metadataRaw.map((key, value) => MapEntry('$key', value))
            : const <String, dynamic>{};
    final content = '${parsedData['content'] ?? parsedData['text'] ?? ''}';
    return MobileToolResult(
      ok: true,
      summary: content.isEmpty
          ? 'preview PDF metadata'
          : 'preview PDF ${content.length} chars',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'kind': 'pdf',
          'pdf_path': null,
          'screenshot_path': null,
          'content': content,
          'text': content,
          'truncated': parsedData['truncated'] ?? false,
          'length': parsedData['length'] ?? content.length,
          'returned_length': parsedData['returned_length'] ?? content.length,
          'fallback': 'phone_payload_metadata',
          'screenshot_supported': false,
          'metadata': {
            ...metadata,
            'payload_only': true,
            'preview_mode': 'pdf_payload',
          },
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _artifactPreview(Map<String, dynamic> args) {
    final maxBytes = _boundedArtifactPreviewMaxBytes(args['max_bytes']);
    final maxChars = _boundedArtifactPreviewMaxChars(args['max_chars']);

    final imageBase64 = _extractImageBase64(args);
    if (imageBase64 != null && imageBase64.trim().isNotEmpty) {
      try {
        final bytes = base64Decode(_stripDataUrlPrefix(imageBase64));
        if (bytes.length > maxBytes) {
          return _artifactPreviewTooLarge(bytes.length, maxBytes);
        }
        final metadata = _readImageHeader(bytes);
        if (metadata != null) {
          return MobileToolResult(
            ok: true,
            summary: 'preview image ${metadata.width}x${metadata.height}',
            output: jsonEncode({
              'status': 'ok',
              'data': {
                'kind': 'image',
                'size': bytes.length,
                'size_bytes': bytes.length,
                'width': metadata.width,
                'height': metadata.height,
                'format': metadata.format,
                'mime_type': metadata.mimeType,
                'metadata': {
                  'payload_only': true,
                  'source': 'image_payload',
                  'preview_mode': 'metadata',
                },
                'execution_location': 'phone',
                'runtime_layers': _flutterRuntimeLayers,
                'requires_mobile_approval': false,
              },
            }),
          );
        }
      } catch (_) {
        // Fall through to document/text parsing; some base64 inputs are text.
      }
    }

    final explicitHtml = _stringOrNull(args['html']);
    if (explicitHtml != null) {
      return _artifactPreviewText(
        explicitHtml,
        kind: 'html',
        source: 'arguments.html',
        maxChars: maxChars,
        metadata: {'title': _extractHtmlTitle(explicitHtml)},
      );
    }
    final explicitText = _stringOrNull(args['text']) ??
        _stringOrNull(args['content']) ??
        _sourceArgumentAsInlineText(args['source']);
    if (explicitText != null) {
      return _artifactPreviewText(
        explicitText,
        kind: _looksLikeHtmlFragment(explicitText) ? 'html' : 'text',
        source: 'arguments.text',
        maxChars: maxChars,
        metadata: _looksLikeHtmlFragment(explicitText)
            ? {'title': _extractHtmlTitle(explicitText)}
            : const {},
      );
    }

    final _DocumentPayload? payload;
    try {
      payload = _extractDocumentPayload(args);
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'artifact payload is invalid',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'INVALID_ARTIFACT_PAYLOAD',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
    if (payload == null) {
      final path = _stringOrNull(args['path']);
      return MobileToolResult(
        ok: false,
        summary: 'artifact payload is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code':
                path == null ? 'MISSING_ARTIFACT_PAYLOAD' : 'UNSUPPORTED_PATH',
            'message': path == null
                ? 'text, html, content, base64, data_url, image/file/document payload is required.'
                : 'Phone-local artifact_preview cannot read PC artifact paths or directories. Pass payload bytes/text or route this call to the connected PC runtime.',
            if (path != null) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }

    if (payload.sizeBytes > maxBytes) {
      return _artifactPreviewTooLarge(payload.sizeBytes, maxBytes);
    }
    final format = _normalizeDocumentFormat(
      args['format'] ?? payload.format,
      mimeType: '${args['mime_type'] ?? payload.mimeType ?? ''}',
      name: '${args['name'] ?? payload.name ?? ''}',
    );
    final bytes =
        payload.bytes ?? Uint8List.fromList(latin1.encode(payload.text ?? ''));
    final isPdf = format == 'pdf' ||
        (payload.mimeType ?? '').trim().toLowerCase() == 'application/pdf' ||
        (payload.name ?? '').trim().toLowerCase().endsWith('.pdf') ||
        _decodePdfBytesForScan(bytes).startsWith('%PDF');
    if (isPdf) {
      final raw = _decodePdfBytesForScan(bytes);
      final extraction = _extractBestEffortPdfText(raw);
      final text = extraction.text;
      final truncated = text.length > maxChars;
      final content = truncated ? text.substring(0, maxChars) : text;
      return MobileToolResult(
        ok: true,
        summary: content.isEmpty
            ? 'preview PDF metadata'
            : 'preview PDF ${content.length} chars',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'kind': 'pdf',
            'content': content,
            'text': content,
            'truncated': truncated,
            'length': text.length,
            'returned_length': content.length,
            'metadata': {
              'payload_only': true,
              'source': payload.source,
              'size_bytes': payload.sizeBytes,
              'method': extraction.method,
              'best_effort': true,
              'screenshot_supported': false,
              if (payload.name != null) 'name': payload.name,
              if (payload.mimeType != null) 'mime_type': payload.mimeType,
            },
            'execution_location': 'phone',
            'runtime_layers': _flutterRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    }

    try {
      final decoded = payload.text == null
          ? _decodeDocumentBytes(payload.bytes ?? Uint8List(0), maxBytes)
          : null;
      final text = payload.text ?? decoded!.text;
      final kind =
          format == 'html' || _looksLikeHtmlFragment(text) ? 'html' : 'text';
      return _artifactPreviewText(
        kind == 'html'
            ? text
            : _parsePhoneDocumentText(text, format: format, stripHtml: false),
        kind: kind,
        source: payload.source,
        maxChars: maxChars,
        metadata: {
          if (payload.name != null) 'name': payload.name,
          if (payload.mimeType != null) 'mime_type': payload.mimeType,
          'format': format,
          'size_bytes': payload.sizeBytes,
          'encoding': payload.text != null ? 'string' : decoded!.encoding,
          if (kind == 'html') 'title': _extractHtmlTitle(text),
        },
      );
    } on _UnsupportedDocumentEncoding catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'artifact encoding unsupported',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'UNSUPPORTED_ARTIFACT_ENCODING',
            'message': error.message,
            'execution_location': 'phone',
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'artifact preview failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'ARTIFACT_PREVIEW_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  MobileToolResult _ttsGenerate(
    Map<String, dynamic> args, {
    required String toolName,
  }) {
    final text = '${args['text'] ?? ''}';
    final durationMs = _boundedTtsFallbackDurationMs(args['duration_ms']);
    final sampleRate = _boundedTtsFallbackSampleRate(args['sample_rate']);
    final wav = _silentWavBytes(
      durationMs: durationMs,
      sampleRate: sampleRate,
    );
    final outputPath = _stringOrNull(args['output_path']);
    return MobileToolResult(
      ok: true,
      summary: 'generated fallback WAV ${wav.length} bytes',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'mime_type': 'audio/wav',
          'format': 'wav',
          'encoding': 'base64',
          'base64': base64Encode(wav),
          'size': wav.length,
          'size_bytes': wav.length,
          'sample_rate': sampleRate,
          'channels': 1,
          'bits_per_sample': 16,
          'duration_ms': durationMs,
          'fallback': 'silent_wav',
          'text_length': text.length,
          if (outputPath != null) 'requested_output_path': outputPath,
          'metadata': {
            'tool': toolName,
            'payload_only': true,
            'real_tts_supported': false,
            'native_tts_bridge': false,
          },
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _imageRender(Map<String, dynamic> args) {
    final outputPath = _phoneImageArtifactOutputPath(
      args['output_path'],
      defaultPath: 'renders/${_nextToolId('image_render')}.svg',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_OUTPUT_PATH',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final ext = _phoneArtifactExtension(outputPath);
    if (ext.isNotEmpty && ext != 'svg') {
      return _phoneArtifactError(
        'PC_DELEGATION_REQUIRED',
        'Phone-local image_render writes SVG. Use PC delegation for PNG/JPEG rendering.',
        path: outputPath,
      );
    }
    final dimensions = _imageRenderDimensions(args);
    final text = _firstText(args, const ['text', 'prompt', 'content']);
    final title = text.isEmpty ? 'Rumi artifact render' : text;
    final svg = _phoneRenderedSvg(
      title: title,
      subtitle: 'phone-local SVG render',
      width: dimensions.width,
      height: dimensions.height,
    );
    final data = _putPhoneArtifactContent(
      outputPath,
      svg,
      source: 'image_render',
      mimeType: 'image/svg+xml',
      metadata: {
        'format': 'svg',
        'width': dimensions.width,
        'height': dimensions.height,
        'renderer': 'phone-svg-fallback',
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'rendered SVG image $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'width': dimensions.width,
          'height': dimensions.height,
          'format': 'svg',
          'path': outputPath,
          'workspace': 'phone',
          'fallback': 'svg_render',
          'execution_location': 'phone',
          'runtime_layers': _phoneMediaArtifactRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _imageGenerateLocalOrProvider(Map<String, dynamic> args) {
    final prompt = _firstText(args, const ['prompt', 'text', 'content']);
    final nextArgs = {
      ...args,
      'text': prompt.isEmpty ? 'Generated image' : prompt,
      'output_path':
          args['output_path'] ?? 'images/${_nextToolId('generated')}.svg',
    };
    final rendered = _imageRender(nextArgs);
    if (!rendered.ok) return rendered;
    final payload = jsonDecode(rendered.output) as Map<String, dynamic>;
    final data = payload['data'] as Map<String, dynamic>;
    data['tool'] = 'image_generate_local_or_provider';
    data['provider_backed'] = false;
    data['real_image_generation_supported'] = false;
    data['fallback'] = 'phone_svg_placeholder';
    data['prompt'] = prompt;
    return MobileToolResult(
      ok: true,
      summary: 'generated phone SVG placeholder ${data['path']}',
      output: jsonEncode({'status': 'ok', 'data': data}),
    );
  }

  MobileToolResult _audioTranscribe(
    Map<String, dynamic> args, {
    required String toolName,
  }) {
    final explicitTranscript =
        _firstText(args, const ['transcript', 'text', 'content']);
    final language = _firstText(args, const ['language', 'language_hint']);
    final path = _normalizePhoneArtifactPath(args['path']);
    final payload = _audioPayloadBytes(args);
    if (explicitTranscript.isNotEmpty) {
      return _audioTranscribeOk(
        toolName,
        explicitTranscript,
        language: language,
        source: 'arguments.transcript',
        sizeBytes: payload?.length,
        path: path,
        fallback: 'provided_transcript',
      );
    }
    if (path != null) {
      final file = _mobileArtifactFiles[path];
      if (file == null) {
        return _phoneArtifactError(
          'AUDIO_ARTIFACT_NOT_FOUND',
          'Phone-local audio artifact not found.',
          path: path,
        );
      }
      final storedBytes = _phoneArtifactStoredBytes(file);
      return _audioTranscribeNeedsNative(
        toolName,
        sizeBytes: storedBytes.length,
        path: path,
        language: language,
      );
    }
    if (payload != null) {
      return _audioTranscribeNeedsNative(
        toolName,
        sizeBytes: payload.length,
        language: language,
      );
    }
    final rawPath = '${args['path'] ?? ''}'.trim();
    if (rawPath.isNotEmpty) {
      return _phoneArtifactError(
        'UNSUPPORTED_PATH',
        'Phone-local audio_transcribe cannot read PC file paths. Use media_file_pick/pass audio bytes or route this call to the connected PC runtime.',
        path: rawPath,
      );
    }
    return _audioTranscribeNeedsNative(toolName, language: language);
  }

  MobileToolResult _audioTranscribeOk(
    String toolName,
    String transcript, {
    required String language,
    required String source,
    int? sizeBytes,
    String? path,
    required String fallback,
  }) {
    return MobileToolResult(
      ok: true,
      summary: transcript.isEmpty
          ? 'audio transcription unavailable on phone'
          : 'audio transcript ${transcript.length} chars',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'text': transcript,
          'transcript': transcript,
          'content': transcript,
          'length': transcript.length,
          if (language.isNotEmpty) 'language': language,
          if (sizeBytes != null) 'size_bytes': sizeBytes,
          if (path != null) 'path': path,
          'fallback': fallback,
          'metadata': {
            'tool': toolName,
            'source': source,
            'payload_only': true,
            'native_transcription_supported': false,
            'ios_native_layer': 'Speech framework not wired yet',
            'android_native_layer': 'SpeechRecognizer/ML Kit not wired yet',
          },
          'execution_location': 'phone',
          'runtime_layers': _phoneMediaArtifactRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _audioTranscribeNeedsNative(
    String toolName, {
    int? sizeBytes,
    String? path,
    required String language,
  }) {
    return _audioTranscribeOk(
      toolName,
      '',
      language: language,
      source: path == null ? 'arguments.audio' : 'phone_artifact_workspace',
      sizeBytes: sizeBytes,
      path: path,
      fallback: 'native_or_provider_transcription_required',
    );
  }

  MobileToolResult _sourceExtract(Map<String, dynamic> args) {
    final maxChars = _boundedSourceExtractMaxChars(args['max_chars']);
    final url = _stringOrNull(args['url']) ??
        (_looksLikeHttpUrl('${args['source'] ?? ''}')
            ? '${args['source']}'.trim()
            : null);
    if (url != null) {
      return MobileToolResult(
        ok: true,
        summary: 'source url placeholder',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'source': url,
            'title': _stringOrNull(args['title']) ?? url,
            'content': '',
            'length': 0,
            'network_required': true,
            'metadata': {
              'input_kind': 'url',
              'payload_only': true,
            },
            'execution_location': 'phone',
            'runtime_layers': _flutterRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    }

    final rawHtml = _stringOrNull(args['html']);
    final rawText = _stringOrNull(args['text']) ??
        _stringOrNull(args['content']) ??
        _sourceArgumentAsInlineText(args['source']);
    final path = _stringOrNull(args['path']) ??
        ((rawText == null && url == null)
            ? _stringOrNull(args['source'])
            : null);
    if (rawHtml == null && rawText == null) {
      return MobileToolResult(
        ok: false,
        summary: 'source payload is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code':
                path == null ? 'MISSING_SOURCE_PAYLOAD' : 'UNSUPPORTED_PATH',
            'message': path == null
                ? 'text, content, html, url, or inline source text is required.'
                : 'Phone-local source_extract cannot read PC workspace paths. PC接続時はPC側runtimeへ委譲できます。',
            if (path != null) 'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }

    final input = rawHtml ?? rawText ?? '';
    final title = _stringOrNull(args['title']) ??
        (rawHtml == null ? null : _extractHtmlTitle(rawHtml)) ??
        'Phone source';
    final content = (rawHtml != null && args['strip_html'] != false)
        ? _stripHtmlToText(rawHtml)
        : input;
    final clean = _normalizeSourceText(content);
    final truncated = clean.length > maxChars;
    final returned = truncated ? clean.substring(0, maxChars) : clean;
    return MobileToolResult(
      ok: true,
      summary: 'source extracted ${returned.length} chars',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'source': rawHtml != null ? 'arguments.html' : 'arguments.text',
          'title': title,
          'content': returned,
          'length': clean.length,
          'returned_length': returned.length,
          'truncated': truncated,
          'network_required': false,
          'metadata': {
            'input_kind': rawHtml != null ? 'html' : 'text',
            'payload_only': true,
          },
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _browserExtractTable(Map<String, dynamic> args) {
    final payload = _extractHtmlTablePayload(args);
    if (payload == null) {
      final path = _stringOrNull(args['path']) ??
          (_looksLikeHttpUrl('${args['source'] ?? ''}')
              ? _stringOrNull(args['source'])
              : null);
      return MobileToolResult(
        ok: false,
        summary: 'HTML payload is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code':
                path == null ? 'MISSING_HTML_PAYLOAD' : 'UNSUPPORTED_SOURCE',
            'message': path == null
                ? 'html, content, source, data_url, file.base64, or document.base64 is required.'
                : 'Phone-local browser_extract_table cannot read PC browser pages, URLs, or artifact paths. Pass HTML payload or route this call to the connected PC runtime.',
            if (path != null) 'source': path,
            'execution_location': 'phone',
          },
        }),
      );
    }

    final maxChars = _boundedSourceExtractMaxChars(args['max_chars']);
    final maxTables = _boundedHtmlTableMaxTables(args['max_tables']);
    final maxRows = _boundedHtmlTableMaxRows(args['max_rows']);
    if (payload.html.length > maxChars) {
      return MobileToolResult(
        ok: false,
        summary: 'HTML payload is too large',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'HTML_TOO_LARGE',
            'message': 'HTML payload is larger than max_chars.',
            'length': payload.html.length,
            'max_chars': maxChars,
            'execution_location': 'phone',
          },
        }),
      );
    }

    final tables = _extractHtmlTables(
      payload.html,
      maxTables: maxTables,
      maxRows: maxRows,
    );
    final tableIndex = _boundedTableIndex(args['table_index'], tables.length);
    final selectedTable =
        tables.isEmpty ? const <List<String>>[] : tables[tableIndex];
    return MobileToolResult(
      ok: true,
      summary: 'extracted ${tables.length} HTML table(s)',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'tables': tables,
          'table_count': tables.length,
          'selected_table_index': tables.isEmpty ? null : tableIndex,
          'selected_table': selectedTable,
          'rows': selectedTable,
          'row_count': selectedTable.length,
          'metadata': {
            'source': payload.source,
            'payload_only': true,
            'max_tables': maxTables,
            'max_rows': maxRows,
            'html_length': payload.html.length,
          },
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _sourceRank(Map<String, dynamic> args) {
    final query = '${args['query'] ?? ''}'.toLowerCase();
    final rawSources = args['sources'];
    final sources = rawSources is List ? rawSources : const [];
    final terms = _splitSourceRankTerms(query);
    final ranked = <Map<String, dynamic>>[];
    for (var index = 0; index < sources.length; index += 1) {
      final source = sources[index];
      final text = _sourceRankText(source).toLowerCase();
      final score = terms.fold<int>(
        0,
        (sum, term) =>
            sum + RegExp(RegExp.escape(term)).allMatches(text).length,
      );
      ranked.add({
        'source': _sourceRankSourceObject(source),
        'score': score,
        '_index': index,
      });
    }
    ranked.sort((left, right) {
      final scoreCompare =
          (right['score'] as int).compareTo(left['score'] as int);
      if (scoreCompare != 0) return scoreCompare;
      return (left['_index'] as int).compareTo(right['_index'] as int);
    });
    for (final item in ranked) {
      item.remove('_index');
    }
    return MobileToolResult(
      ok: true,
      summary: 'ranked ${ranked.length} sources',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'query': query,
          'ranked_sources': ranked,
          'terms': terms,
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _mobileUuid(Map<String, dynamic> args) {
    final count = (args['count'] is num)
        ? math.max(1, math.min(20, (args['count'] as num).toInt()))
        : 1;
    final values = List.generate(count, (_) => const Uuid().v4());
    return MobileToolResult(
      ok: true,
      summary: count == 1 ? values.single : '$count UUIDs',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'uuids': values,
          if (count == 1) 'uuid': values.single,
        },
      }),
    );
  }

  MobileToolResult _artifactFileList(Map<String, dynamic> args) {
    final rawPath = '${args['path'] ?? '.'}'.trim();
    final basePath = _normalizePhoneArtifactPath(rawPath, allowRoot: true);
    if (basePath == null) {
      return _phoneArtifactError('LIST_FAILED', 'Invalid artifact path.');
    }
    final recursive = _boolArg(args['recursive'], fallback: false);
    final includeHidden = _boolArg(args['include_hidden'], fallback: false);
    final maxEntries = _boundedConnectorLimit(
      args['max_entries'],
      defaultValue: 200,
    );
    final entries = <Map<String, dynamic>>[];
    final seenDirs = <String>{};
    for (final entry in _mobileArtifactFiles.entries) {
      final path = entry.key;
      if (!_artifactPathInBase(path, basePath, recursive: recursive)) continue;
      final relative =
          basePath == '.' ? path : path.substring(basePath.length + 1);
      final firstPart = relative.split('/').first;
      if (!recursive && relative.contains('/')) {
        final dirPath = basePath == '.' ? firstPart : '$basePath/$firstPart';
        if (!_artifactPathVisible(dirPath, includeHidden)) continue;
        if (seenDirs.add(dirPath)) {
          entries.add({
            'name': firstPart,
            'path': dirPath,
            'is_dir': true,
            'size': 0,
          });
        }
      } else {
        if (!_artifactPathVisible(path, includeHidden)) continue;
        final file = entry.value;
        entries.add({
          'name': path.split('/').last,
          'path': path,
          'is_dir': false,
          'size': file['size'] ?? 0,
          'updated_at': file['updated_at'],
        });
      }
      if (entries.length >= maxEntries) break;
    }
    entries.sort((a, b) => '${a['path']}'.compareTo('${b['path']}'));
    return MobileToolResult(
      ok: true,
      summary: '${entries.length} phone artifacts',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': basePath,
          'entries': entries,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _artifactFileRead(Map<String, dynamic> args) {
    final path = _normalizePhoneArtifactPath(args['path']);
    if (path == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' is required and must stay inside the phone artifact workspace.",
      );
    }
    final file = _mobileArtifactFiles[path];
    if (file == null) {
      return _phoneArtifactError('READ_FAILED', 'artifact file not found',
          path: path);
    }
    final content = '${file['content'] ?? ''}';
    return MobileToolResult(
      ok: true,
      summary: 'read $path (${content.length} chars)',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': path,
          'content': content,
          'size': file['size'] ?? utf8.encode(content).length,
          'encoding': file['encoding'] ?? 'utf8',
          if (file['mime_type'] != null) 'mime_type': file['mime_type'],
          if (file['metadata'] != null) 'metadata': file['metadata'],
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _fileReader(Map<String, dynamic> args) {
    final path = _normalizePhoneArtifactPath(args['path']);
    if (path == null) {
      final rawPath = '${args['path'] ?? ''}'.trim();
      return _phoneArtifactError(
        rawPath.isEmpty ? 'INVALID_INPUT' : 'UNSUPPORTED_PATH',
        rawPath.isEmpty
            ? "'path' is required."
            : 'Phone-local file_reader cannot read PC workspace paths. Use artifact_file_read for phone artifacts or route this call to the connected PC runtime.',
        path: rawPath.isEmpty ? null : rawPath,
      );
    }
    final file = _mobileArtifactFiles[path];
    if (file == null) {
      return _phoneArtifactError(
        'READ_FAILED',
        'phone artifact file not found',
        path: path,
      );
    }
    final content = '${file['content'] ?? ''}';
    final lines = const LineSplitter().convert(content);
    final totalLines = lines.length;
    final startLine = _boundedInt(args['start_line'], 1, 1, totalLines + 1);
    final endLine = args['end_line'] == null
        ? totalLines
        : _boundedInt(args['end_line'], totalLines, startLine, totalLines);
    final selectedLines = totalLines == 0 || startLine > totalLines
        ? <String>[]
        : lines.sublist(startLine - 1, endLine);
    final rawSelected = selectedLines.join('\n');
    final maxTokenChars = args['max_tokens'] is num
        ? math.max(1, (args['max_tokens'] as num).toInt()) * 4
        : null;
    final maxChars = _boundedInt(
      args['max_chars'] ?? maxTokenChars,
      40000,
      1,
      200000,
    );
    final truncated = rawSelected.length > maxChars;
    final text = truncated ? rawSelected.substring(0, maxChars) : rawSelected;
    return MobileToolResult(
      ok: true,
      summary: 'read $path (${text.length} chars)',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': path,
          'content': text,
          'text': text,
          'start_line': startLine,
          'end_line': endLine,
          'line_count': totalLines,
          'returned_line_count': selectedLines.length,
          'truncated': truncated,
          'size': file['size'] ?? utf8.encode(content).length,
          'encoding': file['encoding'] ?? 'utf8',
          if (file['mime_type'] != null) 'mime_type': file['mime_type'],
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _phoneMediaArtifactRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _artifactFileMutation(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    final risk = toolName == 'artifact_file_write' ? 'medium' : 'high';
    final path = '${args['path'] ?? ''}'.trim();
    final approved = await _requestMobileApproval(
      toolName: toolName,
      prompt:
          'このスマホ内のartifact workspaceで $toolName を実行します。対象: ${path.isEmpty ? '(未指定)' : path}',
      arguments: args,
      risk: risk,
    );
    if (!approved) return _mobileApprovalRequired(toolName);
    return switch (toolName) {
      'artifact_file_write' => _artifactFileWrite(args),
      'artifact_file_patch' => _artifactFilePatch(args),
      'artifact_file_delete' => _artifactFileDelete(args),
      _ =>
        _phoneArtifactError('INVALID_INPUT', 'Unsupported artifact mutation'),
    };
  }

  MobileToolResult _artifactFileWrite(Map<String, dynamic> args) {
    final path = _normalizePhoneArtifactPath(args['path']);
    if (path == null || !args.containsKey('content')) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' and 'content' are required.",
      );
    }
    final content = '${args['content'] ?? ''}';
    final before = '${_mobileArtifactFiles[path]?['content'] ?? ''}';
    final checkpoint = _boolArg(args['checkpoint'], fallback: true)
        ? _phoneArtifactCheckpoint('artifact.file.write', path, before)
        : null;
    final now = DateTime.now().toUtc().toIso8601String();
    _mobileArtifactFiles[path] = {
      'path': path,
      'content': content,
      'size': utf8.encode(content).length,
      'created_at': _mobileArtifactFiles[path]?['created_at'] ?? now,
      'updated_at': now,
    };
    return MobileToolResult(
      ok: true,
      summary: 'wrote $path',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': path,
          'size': utf8.encode(content).length,
          'diff': _simpleTextDiff(before, content, path: path),
          'checkpoint': checkpoint,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _artifactFilePatch(Map<String, dynamic> args) {
    final path = _normalizePhoneArtifactPath(args['path']);
    final oldText = args['old_text'];
    final newText = args['new_text'];
    if (path == null || oldText == null || newText == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path', 'old_text', and 'new_text' are required.",
      );
    }
    final file = _mobileArtifactFiles[path];
    if (file == null) {
      return _phoneArtifactError('PATCH_FAILED', 'artifact file not found',
          path: path);
    }
    final content = '${file['content'] ?? ''}';
    final oldValue = '$oldText';
    final newValue = '$newText';
    final found = _countOccurrences(content, oldValue);
    final expectedRaw = args['expected_replacements'];
    final expected = expectedRaw == null
        ? 1
        : expectedRaw is num
            ? expectedRaw.toInt()
            : int.tryParse('$expectedRaw'.trim()) ?? 1;
    if (found != expected) {
      return MobileToolResult(
        ok: false,
        summary: 'replacement count mismatch',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'REPLACEMENT_COUNT_MISMATCH',
            'message': 'expected $expected replacements but found $found',
            'found': found,
            'expected': expected,
            'path': path,
            'execution_location': 'phone',
          },
        }),
      );
    }
    final updated = content.replaceAll(oldValue, newValue);
    final checkpoint = _boolArg(args['checkpoint'], fallback: true)
        ? _phoneArtifactCheckpoint('artifact.file.patch', path, content)
        : null;
    final now = DateTime.now().toUtc().toIso8601String();
    _mobileArtifactFiles[path] = {
      ...file,
      'content': updated,
      'size': utf8.encode(updated).length,
      'updated_at': now,
    };
    return MobileToolResult(
      ok: true,
      summary: 'patched $path',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': path,
          'patched': true,
          'replacements': expected,
          'size': utf8.encode(updated).length,
          'diff': _simpleTextDiff(content, updated, path: path),
          'checkpoint': checkpoint,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _artifactFileDelete(Map<String, dynamic> args) {
    final path = _normalizePhoneArtifactPath(args['path']);
    if (path == null) {
      return _phoneArtifactError('INVALID_INPUT', "'path' is required.");
    }
    final file = _mobileArtifactFiles[path];
    if (file == null) {
      return _phoneArtifactError('DELETE_FAILED', 'artifact file not found',
          path: path);
    }
    final checkpoint = _boolArg(args['checkpoint'], fallback: true)
        ? _phoneArtifactCheckpoint(
            'artifact.file.delete',
            path,
            '${file['content'] ?? ''}',
          )
        : null;
    _mobileArtifactFiles.remove(path);
    return MobileToolResult(
      ok: true,
      summary: 'deleted $path',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': path,
          'deleted': true,
          'checkpoint': checkpoint,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _browserSavePage(Map<String, dynamic> args) {
    final html = '${args['html'] ?? ''}';
    if (html.isEmpty) {
      return _phoneArtifactError('INVALID_INPUT', "'html' is required.");
    }
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ?? 'browser/page.html',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final now = DateTime.now().toUtc().toIso8601String();
    _mobileArtifactFiles[outputPath] = {
      'path': outputPath,
      'content': html,
      'size': utf8.encode(html).length,
      'created_at': _mobileArtifactFiles[outputPath]?['created_at'] ?? now,
      'updated_at': now,
      'source': 'browser_save_page',
    };
    return MobileToolResult(
      ok: true,
      summary: 'saved HTML to $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': outputPath,
          'size': utf8.encode(html).length,
          'title': _extractHtmlTitle(html),
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _webappPreview(Map<String, dynamic> args) {
    final htmlPath = _phoneWebappIndexPath(args['path']);
    if (htmlPath == null) {
      return _phoneArtifactError('INVALID_INPUT', "'path' is required.");
    }
    final file = _mobileArtifactFiles[htmlPath];
    if (file == null) {
      return _phoneArtifactError(
        'WEBAPP_PREVIEW_FAILED',
        'webapp index.html not found in phone artifact workspace',
        path: htmlPath,
      );
    }
    final html = '${file['content'] ?? ''}';
    final maxChars = _boundedArtifactPreviewMaxChars(args['max_chars']);
    final truncated = html.length > maxChars;
    final content = truncated ? html.substring(0, maxChars) : html;
    return MobileToolResult(
      ok: true,
      summary: 'preview webapp $htmlPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': htmlPath,
          'kind': 'html',
          'title': _extractHtmlTitle(html) ?? 'Webapp Preview',
          'content': content,
          'text': _stripHtmlToText(content).trim(),
          'truncated': truncated,
          'length': html.length,
          'returned_length': content.length,
          'fallback': 'phone_artifact_html_metadata',
          'screenshot_supported': false,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
          'metadata': {
            'payload_only': false,
            'phone_artifact_workspace': true,
            'preview_mode': 'webapp_index_html',
          },
        },
      }),
    );
  }

  MobileToolResult _webappLint(Map<String, dynamic> args) {
    final rawPath = '${args['path'] ?? ''}'.trim();
    final root = _normalizePhoneArtifactPath(rawPath, allowRoot: true);
    if (root == null) {
      return _phoneArtifactError(
        'WEBAPP_LINT_FAILED',
        'Invalid webapp artifact path.',
      );
    }
    final htmlPath = _phoneWebappIndexPath(rawPath, allowRoot: true);
    final issues = <String>[];
    final warnings = <String>[];
    final file = htmlPath == null ? null : _mobileArtifactFiles[htmlPath];
    if (file == null) {
      issues.add('missing index.html');
    } else {
      final html = '${file['content'] ?? ''}';
      if (!_looksLikeHtmlFragment(html)) {
        warnings.add('index.html has no HTML tags');
      }
      if (_extractHtmlTitle(html) == null) warnings.add('missing <title>');
      if (!RegExp(
        '<meta[^>]+name=["\\\']viewport["\\\']',
        caseSensitive: false,
      ).hasMatch(html)) {
        warnings.add('missing viewport meta');
      }
    }
    return MobileToolResult(
      ok: true,
      summary: issues.isEmpty ? 'webapp lint ok' : 'webapp lint issues',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': root,
          'index_path': htmlPath,
          'issues': issues,
          'warnings': warnings,
          'ok': issues.isEmpty,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _webappBuild(Map<String, dynamic> args) async {
    final command = _phoneStringList(args['command']);
    if (command.isNotEmpty &&
        !const {'static', 'noop', 'no-op', 'mark-ready'}
            .contains(command.join(' ').trim().toLowerCase())) {
      return _pcDelegationRequired(
        'webapp_build',
        'Phone-local webapp_build can mark static artifacts as build-ready only. Custom build commands require the connected PC runtime.',
      );
    }
    final root = _normalizePhoneArtifactPath(args['path'], allowRoot: true);
    if (root == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' is required and must stay inside the phone artifact workspace.",
      );
    }
    final entries = _phoneZipSourceEntries(root);
    if (entries.isEmpty) {
      return _phoneArtifactError(
        'WEBAPP_BUILD_FAILED',
        'webapp source not found in phone artifact workspace',
        path: root,
      );
    }
    final indexPath = root == '.' ? 'index.html' : '$root/index.html';
    if (!_mobileArtifactFiles.containsKey(indexPath)) {
      return _phoneArtifactError(
        'WEBAPP_BUILD_FAILED',
        'phone-local static webapp build requires index.html',
        path: root,
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'webapp_build',
      prompt: 'このスマホ内のwebapp artifactへbuild-ready manifestを書き込みます。対象: $root',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('webapp_build');
    final packagePath = root == '.' ? 'package.json' : '$root/package.json';
    final buildPath = root == '.' ? 'build.rumi.json' : '$root/build.rumi.json';
    final files = entries.map((entry) => entry.path).toList();
    final manifest = const JsonEncoder.withIndent('  ').convert({
      'path': root,
      'status': 'build_ready',
      'build_type': 'phone_static_manifest',
      'files': files,
      'entrypoint': indexPath,
      'package_json':
          _mobileArtifactFiles.containsKey(packagePath) ? packagePath : null,
      'pc_build_note':
          'Use PC delegation for npm/pnpm/yarn build commands or bundler output.',
      'created_at': DateTime.now().toUtc().toIso8601String(),
    });
    final data = _putPhoneArtifactContent(
      buildPath,
      '$manifest\n',
      source: 'webapp_build',
      mimeType: 'application/json',
      metadata: {
        'source_path': root,
        'build_type': 'phone_static_manifest',
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'marked static webapp build-ready',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'source_path': root,
          'build_type': 'phone_static_manifest',
          'files': files.length,
          'entrypoint': indexPath,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _projectScaffold(Map<String, dynamic> args) {
    final name = _slugifyPhoneArtifactName(
      '${args['name'] ?? 'webapp'}',
      fallback: 'webapp',
    );
    final template = '${args['template'] ?? 'static_html'}'.trim();
    if (!{'static_html', 'plain_js', 'vite_react'}.contains(template)) {
      return _phoneArtifactError(
        'UNSUPPORTED_TEMPLATE',
        'Phone-local project_scaffold supports static_html, plain_js, and vite_react layouts.',
      );
    }
    final root = _normalizePhoneArtifactPath(args['path'] ?? 'webapps/$name');
    if (root == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' must stay inside the phone artifact workspace.",
      );
    }
    final files = <String>[];

    void write(String relativePath, String content) {
      final path = '$root/$relativePath';
      _putPhoneArtifactContent(path, content, source: 'project_scaffold');
      files.add(path);
    }

    if (template == 'vite_react') {
      write(
        'index.html',
        '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>${_escapeHtmlText(name)}</title></head>'
            '<body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body></html>\n',
      );
      write(
        'src/main.jsx',
        "import React from 'react';\n"
            "import { createRoot } from 'react-dom/client';\n\n"
            "createRoot(document.getElementById('root')).render(<h1>$name</h1>);\n",
      );
      write(
        'package.json',
        const JsonEncoder.withIndent('  ').convert({
          'scripts': {'build': 'vite'},
          'dependencies': {
            '@vitejs/plugin-react': 'latest',
            'vite': 'latest',
            'react': 'latest',
            'react-dom': 'latest',
          },
        }),
      );
    } else {
      write(
        'index.html',
        '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>${_escapeHtmlText(name)}</title></head>'
            '<body><main id="app"><h1>${_escapeHtmlText(name)}</h1></main>'
            '<script src="app.js"></script></body></html>\n',
      );
      write('app.js', "document.body.dataset.rumiWebapp = 'ready';\n");
    }

    return MobileToolResult(
      ok: true,
      summary: 'scaffolded $root',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': root,
          'template': template,
          'files': files,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
          'note': template == 'vite_react'
              ? 'Files are created on this phone; package install and build still require PC delegation.'
              : '',
        },
      }),
    );
  }

  MobileToolResult _docCreate(Map<String, dynamic> args) {
    final title = '${args['title'] ?? 'Document'}'.trim();
    final content = '${args['content'] ?? args['markdown'] ?? ''}';
    final explicitFormat = '${args['format'] ?? ''}'.trim().toLowerCase();
    final defaultPath =
        'documents/${_slugifyPhoneArtifactName(title, fallback: 'document')}.md';
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ?? defaultPath,
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final ext = _phoneArtifactExtension(outputPath);
    final format = explicitFormat.isNotEmpty
        ? explicitFormat.trim().replaceFirst(RegExp(r'^\.'), '')
        : (ext.isEmpty ? 'md' : ext);
    if ({'docx', 'pdf', 'png', 'pptx', 'xlsx'}.contains(format)) {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_DOCUMENT_FORMAT',
        'Phone-local doc_create can write Markdown, text, HTML, or JSON only. Use PC delegation for $format output.',
        path: outputPath,
      );
    }
    final documentTitle = title.isEmpty ? 'Document' : title;
    final rendered = switch (format) {
      'html' => '<!doctype html><html><head><meta charset="utf-8">'
          '<meta name="viewport" content="width=device-width,initial-scale=1">'
          '<title>${_escapeHtmlText(documentTitle)}</title></head><body>'
          '<main><h1>${_escapeHtmlText(documentTitle)}</h1><pre>${_escapeHtmlText(content)}</pre></main>'
          '</body></html>\n',
      'json' => '${const JsonEncoder.withIndent('  ').convert({
              'title': documentTitle,
              'content': content,
            })}\n',
      'txt' || 'text' => '$documentTitle\n\n$content\n',
      _ => '${'# $documentTitle\n\n$content'.trim()}\n',
    };
    final data =
        _putPhoneArtifactContent(outputPath, rendered, source: 'doc_create');
    return MobileToolResult(
      ok: true,
      summary: 'created document $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'title': documentTitle,
          'format': format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _docUpdate(Map<String, dynamic> args) async {
    final path = _normalizePhoneArtifactPath(args['path']);
    if (path == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' is required and must stay inside the phone artifact workspace.",
      );
    }
    final ext = _phoneArtifactExtension(path);
    if ({'docx', 'pdf', 'pptx', 'xlsx', 'png'}.contains(ext)) {
      return _binaryExportRequiresPc(
          'doc_update', ext.isEmpty ? 'binary' : ext);
    }
    final file = _mobileArtifactFiles[path];
    if (file == null) {
      return _phoneArtifactError(
        'DOC_UPDATE_FAILED',
        'document not found in phone artifact workspace',
        path: path,
      );
    }
    final hasContent = args.containsKey('content');
    final append = '${args['append'] ?? ''}';
    if (!hasContent && append.isEmpty) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'content' or 'append' is required for doc_update.",
        path: path,
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'doc_update',
      prompt: 'このスマホ内のdocument artifactを更新します。対象: $path',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('doc_update');
    final before = '${file['content'] ?? ''}';
    var updated = hasContent || _boolArg(args['replace'], fallback: false)
        ? '${args['content'] ?? ''}'
        : before;
    if (append.isNotEmpty) {
      updated =
          updated.isEmpty ? '$append\n' : '${updated.trimRight()}\n$append\n';
    }
    final data = _putPhoneArtifactContent(
      path,
      updated,
      source: 'doc_update',
      metadata: {
        'operation': hasContent ? 'replace' : 'append',
        'previous_size': utf8.encode(before).length,
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'updated document $path',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'operation': hasContent ? 'replace' : 'append',
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _slidesCreate(Map<String, dynamic> args) {
    final title = '${args['title'] ?? 'Deck'}'.trim();
    final explicitFormat = '${args['format'] ?? ''}'
        .trim()
        .toLowerCase()
        .replaceFirst(RegExp(r'^\.'), '');
    final defaultExt = explicitFormat.isEmpty || explicitFormat == 'json'
        ? 'slides.json'
        : explicitFormat;
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ??
          'slides/${_slugifyPhoneArtifactName(title, fallback: 'deck')}.$defaultExt',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final format = _phoneSlideFormat(args['format'], outputPath);
    final unsupported = _unsupportedPhoneSlideFormat(format);
    if (unsupported != null) {
      return _phoneArtifactError(
        'PC_DELEGATION_REQUIRED',
        unsupported,
        path: outputPath,
      );
    }
    final deck = _phoneSlidesFromArgs(args, fallbackTitle: title);
    final content = _phoneSlidesContentForFormat(deck, format);
    final data = _putPhoneArtifactContent(
      outputPath,
      content.content,
      source: 'slides_create',
      mimeType: content.mimeType,
      metadata: {
        'title': deck.title,
        'slides': deck.slides.length,
        'format': format,
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'created ${deck.slides.length} slide outlines',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'title': deck.title,
          'slides': deck.slides.length,
          'format': format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _slidesFromMarkdown(Map<String, dynamic> args) {
    var markdown = '${args['markdown'] ?? ''}';
    final sourcePathRaw = '${args['path'] ?? ''}'.trim();
    String? sourcePath;
    if (sourcePathRaw.isNotEmpty) {
      sourcePath = _normalizePhoneArtifactPath(sourcePathRaw);
      if (sourcePath == null) {
        return _phoneArtifactError(
          'INVALID_INPUT',
          "'path' must stay inside the phone artifact workspace.",
        );
      }
      final file = _mobileArtifactFiles[sourcePath];
      if (file == null) {
        return _phoneArtifactError(
          'SLIDES_FROM_MARKDOWN_FAILED',
          'markdown source not found in phone artifact workspace',
          path: sourcePath,
        );
      }
      markdown = '${file['content'] ?? ''}';
    }
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ?? 'slides/deck.slides.json',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    if (_phoneArtifactExtension(outputPath) == 'pptx') {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_SLIDE_FORMAT',
        'Phone-local slides_from_markdown creates slide outline JSON/Markdown only. Use PC delegation for PPTX output.',
        path: outputPath,
      );
    }
    final slides = _slidesFromMarkdownText(markdown);
    final payload = const JsonEncoder.withIndent('  ').convert({
      'source_path': sourcePath,
      'slides': slides,
      'format': 'slide_outline',
      'pc_export_note': 'Use PC delegation to export this outline to PPTX.',
    });
    final data = _putPhoneArtifactContent(
      outputPath,
      '$payload\n',
      source: 'slides_from_markdown',
    );
    return MobileToolResult(
      ok: true,
      summary: 'created ${slides.length} slide outlines',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'slides': slides.length,
          'format': 'slide_outline_json',
          'source_path': sourcePath,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _slidesUpdate(Map<String, dynamic> args) async {
    final path = _normalizePhoneArtifactPath(args['path']);
    if (path == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' is required and must stay inside the phone artifact workspace.",
      );
    }
    final format = _phoneSlideFormat(args['format'], path);
    final unsupported = _unsupportedPhoneSlideFormat(format);
    if (unsupported != null) {
      return _phoneArtifactError(
        'PC_DELEGATION_REQUIRED',
        unsupported,
        path: path,
      );
    }
    if (!_mobileArtifactFiles.containsKey(path)) {
      return _phoneArtifactError(
        'SLIDES_UPDATE_FAILED',
        'slide artifact not found in phone artifact workspace',
        path: path,
      );
    }
    if (args['slides'] is! List) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'slides' array is required for slides_update.",
        path: path,
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'slides_update',
      prompt: 'このスマホ内のslide artifactを書き換えます。対象: $path',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('slides_update');
    final deck =
        _phoneSlidesFromArgs(args, fallbackTitle: _phoneArtifactStem(path));
    final content = _phoneSlidesContentForFormat(deck, format);
    final data = _putPhoneArtifactContent(
      path,
      content.content,
      source: 'slides_update',
      mimeType: content.mimeType,
      metadata: {
        'title': deck.title,
        'slides': deck.slides.length,
        'format': format,
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'updated ${deck.slides.length} slide outlines',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'title': deck.title,
          'slides': deck.slides.length,
          'format': format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _slidesExport(Map<String, dynamic> args) {
    final path = _normalizePhoneArtifactPath(args['path']);
    if (path == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' is required and must stay inside the phone artifact workspace.",
      );
    }
    final requestedFormat =
        '${args['format'] ?? args['output_format'] ?? ''}'.trim();
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ??
          'exports/${_phoneArtifactStem(path)}.${requestedFormat.isEmpty ? 'md' : requestedFormat.replaceFirst(RegExp(r'^\.'), '')}',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final format = _phoneSlideFormat(
      requestedFormat,
      outputPath,
      fallback: 'md',
    );
    final unsupported = _unsupportedPhoneSlideFormat(format);
    if (unsupported != null) {
      return _phoneArtifactError(
        'PC_DELEGATION_REQUIRED',
        unsupported,
        path: path,
      );
    }
    final parsed = _readPhoneSlides(path);
    if (parsed.error != null) return parsed.error!;
    final content = _phoneSlidesContentForFormat(parsed.deck, format);
    final data = _putPhoneArtifactContent(
      outputPath,
      content.content,
      source: 'slides_export',
      mimeType: content.mimeType,
      metadata: {
        'source_path': path,
        'title': parsed.deck.title,
        'slides': parsed.deck.slides.length,
        'format': format,
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'exported slides $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'source_path': path,
          'title': parsed.deck.title,
          'slides': parsed.deck.slides.length,
          'format': format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _chartCreate(Map<String, dynamic> args) {
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ?? 'charts/chart.svg',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    if (_phoneArtifactExtension(outputPath) == 'png') {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_CHART_FORMAT',
        'Phone-local chart_create writes SVG. Use PC delegation for PNG rendering.',
        path: outputPath,
      );
    }
    final title = '${args['title'] ?? 'Chart'}'.trim();
    final values = _phoneChartValues(args);
    final labels = _phoneChartLabels(args, values.length);
    final svg = _phoneChartSvg(
      title: title.isEmpty ? 'Chart' : title,
      values: values,
      labels: labels,
    );
    final data = _putPhoneArtifactContent(
      outputPath,
      svg,
      source: 'chart_create',
    );
    return MobileToolResult(
      ok: true,
      summary: 'created SVG chart $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'title': title.isEmpty ? 'Chart' : title,
          'format': 'svg',
          'values': values,
          'labels': labels,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _sheetCreate(Map<String, dynamic> args) {
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ?? 'sheets/sheet.csv',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final format = _phoneSheetFormat(args['format'], outputPath);
    final unsupported = _unsupportedPhoneSheetFormat(format);
    if (unsupported != null) {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_SHEET_FORMAT',
        unsupported,
        path: outputPath,
      );
    }
    final rows = _phoneSheetRowsFromArgs(args);
    final content = _phoneSheetContentForFormat(rows, format);
    final data =
        _putPhoneArtifactContent(outputPath, content, source: 'sheet_create');
    return MobileToolResult(
      ok: true,
      summary: 'created sheet $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'rows': rows.length,
          'columns': rows.fold<int>(
            0,
            (maxColumns, row) => math.max(maxColumns, row.length).toInt(),
          ),
          'format': format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _sheetRead(Map<String, dynamic> args) {
    final parsed = _readPhoneSheetRows(args['path']);
    if (parsed.error != null) return parsed.error!;
    final limit = _boundedConnectorLimit(args['limit'], defaultValue: 200);
    final rows = parsed.rows;
    return MobileToolResult(
      ok: true,
      summary: 'read ${rows.length} sheet rows',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': parsed.path,
          'rows': rows.take(limit).toList(),
          'row_count': rows.length,
          'returned_rows': math.min(limit, rows.length),
          'format': parsed.format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _sheetAnalyze(Map<String, dynamic> args) {
    final parsed = _readPhoneSheetRows(args['path']);
    if (parsed.error != null) return parsed.error!;
    final rows = parsed.rows;
    final headers = rows.isNotEmpty ? rows.first : <String>[];
    final dataRows = rows.isNotEmpty ? rows.skip(1).toList() : <List<String>>[];
    final missing = dataRows.fold<int>(
      0,
      (count, row) => count + row.where((cell) => cell.trim().isEmpty).length,
    );
    final numericValues = <double>[];
    for (final row in dataRows) {
      for (final cell in row) {
        final value = double.tryParse(cell.trim());
        if (value != null && value.isFinite) numericValues.add(value);
      }
    }
    final numeric = numericValues.isEmpty
        ? <String, dynamic>{}
        : {
            'count': numericValues.length,
            'mean':
                numericValues.reduce((a, b) => a + b) / numericValues.length,
            'min': numericValues.reduce(math.min),
            'max': numericValues.reduce(math.max),
          };
    return MobileToolResult(
      ok: true,
      summary: 'analyzed ${rows.length} sheet rows',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'path': parsed.path,
          'headers': headers,
          'row_count': rows.length,
          'missing_values': missing,
          'numeric': numeric,
          'format': parsed.format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _sheetUpdate(Map<String, dynamic> args) async {
    final outputPath = _normalizePhoneArtifactPath(args['path']);
    if (outputPath == null || !args.containsKey('rows')) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' and 'rows' are required.",
      );
    }
    final format = _phoneSheetFormat(args['format'], outputPath);
    final unsupported = _unsupportedPhoneSheetFormat(format);
    if (unsupported != null) {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_SHEET_FORMAT',
        unsupported,
        path: outputPath,
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'sheet_update',
      risk: 'medium',
      arguments: {
        ...args,
        'path': outputPath,
        'row_count':
            args['rows'] is List ? (args['rows'] as List).length : null,
      },
      prompt: 'このスマホ内のsheet artifactを新しい行データで置き換えます。対象: $outputPath',
    );
    if (!approved) return _mobileApprovalRequired('sheet_update');
    final rows = _phoneSheetRowsFromArgs(args);
    final content = _phoneSheetContentForFormat(rows, format);
    final before = '${_mobileArtifactFiles[outputPath]?['content'] ?? ''}';
    final checkpoint = _boolArg(args['checkpoint'], fallback: true)
        ? _phoneArtifactCheckpoint('sheet.update', outputPath, before)
        : null;
    final data =
        _putPhoneArtifactContent(outputPath, content, source: 'sheet_update');
    return MobileToolResult(
      ok: true,
      summary: 'updated sheet $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'rows': rows.length,
          'format': format,
          'diff': _simpleTextDiff(before, content, path: outputPath),
          'checkpoint': checkpoint,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _sheetExport(Map<String, dynamic> args) {
    final parsed = _readPhoneSheetRows(args['path']);
    if (parsed.error != null) return parsed.error!;
    final format = _phoneSheetFormat(
      args['format'],
      _normalizePhoneArtifactPath(args['output_path']) ?? '',
      fallback: 'csv',
    );
    final unsupported = _unsupportedPhoneSheetFormat(format, export: true);
    if (unsupported != null) {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_SHEET_FORMAT',
        unsupported,
        path: parsed.path,
      );
    }
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ??
          'exports/${_phoneArtifactStem(parsed.path)}.$format',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final content = _phoneSheetContentForFormat(parsed.rows, format);
    final data =
        _putPhoneArtifactContent(outputPath, content, source: 'sheet_export');
    return MobileToolResult(
      ok: true,
      summary: 'exported sheet $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'source_path': parsed.path,
          'rows': parsed.rows.length,
          'format': format,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _artifactZip(Map<String, dynamic> args) {
    final sourcePath = _normalizePhoneArtifactPath(
      args['path'] ?? args['source_path'] ?? '.',
      allowRoot: true,
    );
    final outputPath =
        _normalizePhoneArtifactPath(args['output_path'] ?? 'artifact.zip');
    if (sourcePath == null || outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' and 'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final entries = _phoneZipSourceEntries(sourcePath);
    if (entries.isEmpty) {
      return _phoneArtifactError(
        'ZIP_FAILED',
        'No phone-local artifact files matched the requested path.',
        path: sourcePath,
      );
    }
    final zipBytes = _buildStoredZip(entries);
    final data = _putPhoneArtifactContent(
      outputPath,
      base64Encode(zipBytes),
      source: 'artifact_zip',
      encoding: 'base64',
      mimeType: 'application/zip',
      sizeOverride: zipBytes.length,
      metadata: {
        'source_path': sourcePath,
        'archived_files': entries.map((entry) => entry.path).toList(),
        'archive_format': 'zip',
        'compression': 'store',
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'created zip archive $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'source_path': sourcePath,
          'files': entries.length,
          'base64': base64Encode(zipBytes),
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _researchReportExport(Map<String, dynamic> args) {
    final rawPath = '${args['path'] ?? args['report_path'] ?? ''}'.trim();
    final sourcePath =
        rawPath.isEmpty ? null : _normalizePhoneArtifactPath(rawPath);
    if (rawPath.isNotEmpty && sourcePath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' must stay inside the phone artifact workspace.",
      );
    }
    final sourceContent = sourcePath == null
        ? _phoneReportContentFromArgs(args)
        : _mobileArtifactFiles[sourcePath]?['content'];
    if (sourceContent == null) {
      return _phoneArtifactError(
        'RESEARCH_REPORT_EXPORT_FAILED',
        sourcePath == null
            ? 'report content or path is required'
            : 'research report source not found in phone artifact workspace',
        path: sourcePath,
      );
    }
    final requestedFormat = '${args['format'] ?? ''}'.trim().toLowerCase();
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ??
          'exports/${_phoneArtifactStem(sourcePath ?? 'research-report')}.${requestedFormat.isEmpty ? 'md' : requestedFormat.replaceFirst(RegExp(r'^\.'), '')}',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final format = _phoneArtifactExtension(outputPath).isEmpty
        ? (requestedFormat.isEmpty ? 'md' : requestedFormat)
        : _phoneArtifactExtension(outputPath);
    final normalizedFormat = switch (format.replaceFirst(RegExp(r'^\.'), '')) {
      'markdown' => 'md',
      'text' => 'txt',
      final value => value,
    };
    if (const {'pdf', 'docx', 'pptx', 'xlsx', 'png'}
        .contains(normalizedFormat)) {
      return _pcDelegationRequired(
        'research_report_export',
        'Phone-local research_report_export supports Markdown, HTML, JSON, and text only. Use PC delegation for $normalizedFormat output.',
      );
    }
    if (!const {'md', 'html', 'htm', 'json', 'txt'}
        .contains(normalizedFormat)) {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_REPORT_FORMAT',
        'Unsupported phone-local research report export format: $normalizedFormat',
        path: sourcePath,
      );
    }
    final exportContent = _phoneFileExportContent(
      sourcePath ?? 'research-report',
      '$sourceContent',
      normalizedFormat,
    );
    final data = _putPhoneArtifactContent(
      outputPath,
      exportContent.content,
      source: 'research_report_export',
      mimeType: exportContent.mimeType,
      metadata: {
        if (sourcePath != null) 'source_path': sourcePath,
        'format': normalizedFormat,
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'exported research report $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          if (sourcePath != null) 'source_path': sourcePath,
          'format': normalizedFormat,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _artifactExport(Map<String, dynamic> args) {
    final sourcePath =
        _normalizePhoneArtifactPath(args['path'], allowRoot: true);
    if (sourcePath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'path' is required and must stay inside the phone artifact workspace.",
      );
    }
    final format =
        '${args['format'] ?? args['output_format'] ?? ''}'.trim().toLowerCase();
    if (format.isEmpty) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'format' is required for phone-local artifact_export.",
        path: sourcePath,
      );
    }
    final normalizedFormat = format.replaceFirst(RegExp(r'^\.'), '');
    if (normalizedFormat == 'zip') {
      return _artifactZip({
        ...args,
        'path': sourcePath,
        'output_path': args['output_path'] ??
            'exports/${_phoneArtifactStem(sourcePath == '.' ? 'artifact' : sourcePath)}.zip',
      });
    }
    final unsupported = _unsupportedPhoneArtifactExportFormat(normalizedFormat);
    if (unsupported != null) {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_EXPORT_FORMAT',
        unsupported,
        path: sourcePath,
      );
    }
    final outputPath = _normalizePhoneArtifactPath(
      args['output_path'] ??
          'exports/${_phoneArtifactStem(sourcePath == '.' ? 'artifact' : sourcePath)}.$normalizedFormat',
    );
    if (outputPath == null) {
      return _phoneArtifactError(
        'INVALID_INPUT',
        "'output_path' must stay inside the phone artifact workspace.",
      );
    }
    final exportContent = _phoneArtifactExportContent(
      sourcePath,
      normalizedFormat,
    );
    if (exportContent.error != null) return exportContent.error!;
    final data = _putPhoneArtifactContent(
      outputPath,
      exportContent.content,
      source: 'artifact_export',
      mimeType: exportContent.mimeType,
      metadata: {
        'source_path': sourcePath,
        'format': normalizedFormat,
      },
    );
    return MobileToolResult(
      ok: true,
      summary: 'exported artifact $outputPath',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'source_path': sourcePath,
          'format': normalizedFormat,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _staticSiteExport(Map<String, dynamic> args) {
    return _artifactZip({
      ...args,
      'path': args['path'],
      'output_path': args['output_path'] ?? 'exports/static-site.zip',
    });
  }

  MobileToolResult _webappExportStatic(Map<String, dynamic> args) {
    return _staticSiteExport(args);
  }

  MobileToolResult _docExport(Map<String, dynamic> args) {
    final format = '${args['format'] ?? 'html'}'.trim().toLowerCase();
    final normalizedFormat = format.replaceFirst(RegExp(r'^\.'), '');
    if ({'docx', 'pdf', 'pptx', 'xlsx', 'png'}.contains(normalizedFormat)) {
      return _phoneArtifactError(
        'UNSUPPORTED_PHONE_EXPORT_FORMAT',
        'Phone-local doc_export supports HTML, Markdown, text, and JSON only. Use PC delegation for $normalizedFormat output.',
        path: _normalizePhoneArtifactPath(args['path']) ?? '',
      );
    }
    return _artifactExport({
      ...args,
      'format': normalizedFormat,
      'output_path': args['output_path'] ??
          'exports/${_phoneArtifactStem('${args['path'] ?? 'document'}')}.$normalizedFormat',
    });
  }

  MobileToolResult _binaryExportRequiresPc(String toolName, String format) {
    return MobileToolResult(
      ok: false,
      summary: '$toolName requires PC runtime',
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': 'PC_DELEGATION_REQUIRED',
          'message':
              'Phone-local $toolName cannot generate real $format bytes. Use the connected PC defaultspack runtime for this export.',
          'execution_location': 'pc',
          'runtime_layers': ['pc-defaultspack-runtime'],
        },
      }),
    );
  }

  Future<MobileToolResult> _mobileUrlOpen(Map<String, dynamic> args) async {
    final raw = '${args['url'] ?? args['href'] ?? args['input'] ?? ''}'.trim();
    if (raw.isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'url is required',
        output: jsonEncode({
          'status': 'error',
          'error': {'code': 'MISSING_URL', 'message': 'url is required'},
        }),
      );
    }
    final uri = Uri.tryParse(raw);
    if (uri == null || !uri.hasScheme || uri.host.isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'invalid URL',
        output: jsonEncode({
          'status': 'error',
          'error': {'code': 'INVALID_URL', 'message': 'invalid URL: $raw'},
        }),
      );
    }
    if (uri.scheme != 'http' && uri.scheme != 'https') {
      return MobileToolResult(
        ok: false,
        summary: 'unsupported URL scheme',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'UNSUPPORTED_URL_SCHEME',
            'message': 'Only http and https URLs can be opened by this tool.',
          },
        }),
      );
    }
    final ok = await _urlLauncher.open(uri);
    return MobileToolResult(
      ok: ok,
      summary: ok ? 'opened ${uri.host}' : 'URL open failed',
      output: jsonEncode({
        'status': ok ? 'ok' : 'error',
        'data': {
          'url': uri.toString(),
          'opened': ok,
          'execution_location': 'phone',
          'runtime_layers': _nativeUrlRuntimeLayers,
        },
      }),
    );
  }

  Future<MobileToolResult> _mediaClipboardRead(
    Map<String, dynamic> args,
  ) async {
    final approved = await _requestMobileApproval(
      toolName: 'media_clipboard_read',
      risk: 'high',
      arguments: args,
      prompt: 'このスマホのclipboardからテキストを読み取ります。許可した内容はAIのtool結果として会話に渡されます。',
    );
    if (!approved) return _mobileApprovalRequired('media_clipboard_read');

    final text = await _clipboard.readText() ?? '';
    final maxChars = (args['max_chars'] is num)
        ? math.max(1, math.min(8000, (args['max_chars'] as num).toInt()))
        : 4000;
    final truncated = text.length > maxChars;
    final content = truncated ? text.substring(0, maxChars) : text;
    return MobileToolResult(
      ok: true,
      summary: truncated
          ? 'clipboard read ${content.length}/${text.length} chars'
          : 'clipboard read ${content.length} chars',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'content': content,
          'truncated': truncated,
          'length': text.length,
          'returned_length': content.length,
          'execution_location': 'phone',
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  Future<MobileToolResult> _mediaClipboardWrite(
    Map<String, dynamic> args,
  ) async {
    final text = '${args['text'] ?? args['content'] ?? args['input'] ?? ''}';
    if (text.isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'text is required',
        output: jsonEncode({
          'status': 'error',
          'error': {'code': 'MISSING_PARAM', 'message': 'text is required'},
        }),
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'media_clipboard_write',
      risk: 'high',
      arguments: {
        ...args,
        'preview': _clampText(text.replaceAll(RegExp(r'\s+'), ' '), 160),
        'length': text.length,
      },
      prompt: 'このスマホのclipboardを新しいテキストで置き換えます。現在のclipboard内容は上書きされます。',
    );
    if (!approved) return _mobileApprovalRequired('media_clipboard_write');

    await _clipboard.writeText(text);
    return MobileToolResult(
      ok: true,
      summary: 'clipboard wrote ${text.length} chars',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'written': true,
          'length': text.length,
          'execution_location': 'phone',
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  Future<MobileToolResult> _mediaFilePick(Map<String, dynamic> args) async {
    final kind = _normalizeMediaPickKind(
      args['kind'] ?? args['type'] ?? args['media_type'],
    );
    final maxBytes = _boundedMediaPickMaxBytes(args['max_bytes']);
    final approved = await _requestMobileApproval(
      toolName: 'media_file_pick',
      risk: 'high',
      arguments: {
        ...args,
        'kind': kind,
        'max_bytes': maxBytes,
      },
      prompt: 'このスマホから1つのファイルを選択します。選択したファイル内容はAIのtool結果として会話に渡されます。',
    );
    if (!approved) return _mobileApprovalRequired('media_file_pick');

    try {
      final picked = await _mediaPicker.pick(kind: kind, maxBytes: maxBytes);
      if (picked == null) {
        return MobileToolResult(
          ok: false,
          summary: 'file pick cancelled',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'MEDIA_PICK_CANCELLED',
              'message': 'No file was selected on this phone.',
              'execution_location': 'phone',
            },
          }),
        );
      }
      if (picked.size > maxBytes) {
        return MobileToolResult(
          ok: false,
          summary: 'file is too large',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'MEDIA_FILE_TOO_LARGE',
              'message': 'Selected file is larger than max_bytes.',
              'size': picked.size,
              'max_bytes': maxBytes,
              'execution_location': 'phone',
            },
          }),
        );
      }
      return MobileToolResult(
        ok: true,
        summary: 'picked ${picked.name} (${picked.size} bytes)',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'name': picked.name,
            'mime_type': picked.mimeType,
            'size': picked.size,
            'kind': kind,
            'base64': picked.base64Data,
            'encoding': 'base64',
            'execution_location': 'phone',
            'runtime_layers': _nativeMediaPickerRuntimeLayers,
            'requires_mobile_approval': true,
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'file pick failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'MEDIA_PICK_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  Future<MobileToolResult> _mediaScreenshot(Map<String, dynamic> args) async {
    final maxBytes = _boundedScreenshotMaxBytes(args['max_bytes']);
    final maxDimension = _boundedScreenshotMaxDimension(args['max_dimension']);
    final approved = await _requestMobileApproval(
      toolName: 'media_screenshot',
      risk: 'high',
      arguments: {
        ...args,
        'max_bytes': maxBytes,
        'max_dimension': maxDimension,
        'capture_scope': 'app_window',
      },
      prompt: 'このスマホで表示中のRumiアプリ画面をPNG画像として取得します。画面内の会話や設定がAIのtool結果として渡されます。',
    );
    if (!approved) return _mobileApprovalRequired('media_screenshot');

    try {
      final screenshot = await _screenshotCapture.capture(
        maxBytes: maxBytes,
        maxDimension: maxDimension,
      );
      if (screenshot.size > maxBytes) {
        return MobileToolResult(
          ok: false,
          summary: 'screenshot is too large',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'MEDIA_SCREENSHOT_TOO_LARGE',
              'message': 'Captured screenshot is larger than max_bytes.',
              'size': screenshot.size,
              'max_bytes': maxBytes,
              'execution_location': 'phone',
            },
          }),
        );
      }
      return MobileToolResult(
        ok: true,
        summary:
            'captured app screenshot ${screenshot.width}x${screenshot.height}',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'name': 'rumi-app-screenshot.png',
            'mime_type': screenshot.mimeType,
            'size': screenshot.size,
            'width': screenshot.width,
            'height': screenshot.height,
            'base64': screenshot.base64Data,
            'encoding': 'base64',
            'capture_scope': 'app_window',
            'execution_location': 'phone',
            'runtime_layers': _nativeScreenshotRuntimeLayers,
            'requires_mobile_approval': true,
          },
        }),
      );
    } catch (error) {
      return MobileToolResult(
        ok: false,
        summary: 'screenshot capture failed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'MEDIA_SCREENSHOT_FAILED',
            'message': '$error',
            'execution_location': 'phone',
          },
        }),
      );
    }
  }

  Future<bool> _requestMobileApproval({
    required String toolName,
    required String prompt,
    required Map<String, dynamic> arguments,
    required String risk,
  }) async {
    final delegate = _approvalDelegate;
    if (delegate == null) return false;
    try {
      return await delegate.approve(
        MobileToolApprovalRequest(
          toolName: toolName,
          prompt: prompt,
          arguments: arguments,
          risk: risk,
        ),
      );
    } catch (_) {
      return false;
    }
  }

  MobileToolResult _mobileApprovalRequired(String toolName) {
    return MobileToolResult(
      ok: false,
      summary: '$toolName requires mobile approval',
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': 'MOBILE_APPROVAL_REQUIRED',
          'message':
              '$toolName requires explicit approval on this phone before it can run.',
          'tool_name': toolName,
          'execution_location': 'phone',
        },
      }),
    );
  }

  MobileToolResult _asyncOnlyTool(String name) {
    return MobileToolResult(
      ok: false,
      summary: '$name requires async runtime',
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': 'ASYNC_TOOL_REQUIRES_EXECUTE_ASYNC',
          'message':
              '$name uses a Flutter platform channel and must be run through executeAsync.',
        },
      }),
    );
  }

  MobileToolResult _toolSearch(Map<String, dynamic> args) {
    final query = '${args['query'] ?? ''}'.trim().toLowerCase();
    final platformFilter = _normalizePlatformFilter(
      args['platform'] ?? args['execution_platform'] ?? args['mobile_platform'],
    );
    final limit = (args['limit'] is num)
        ? math.max(1, math.min(12, (args['limit'] as num).toInt()))
        : 6;
    final records = <Map<String, dynamic>>[];
    for (final record in _runtimeCatalogRecords(includeUnavailable: true)) {
      if (!_recordMatchesPlatform(record, platformFilter)) continue;
      final haystack = _recordSearchText(record);
      if (query.isEmpty || haystack.contains(query)) {
        records.add(record);
      }
      if (records.length >= limit) break;
    }
    if (records.isEmpty) {
      records.add(_unsupportedToolRecord(query));
    }
    return MobileToolResult(
      ok: true,
      summary: '${records.length} tools',
      output: jsonEncode({
        'agent_template': _agentTemplateRecord(),
        'tool_surface': _toolSurfaceRecord(pcDelegationAvailable),
        'platform_filter': platformFilter,
        'tools': records,
      }),
    );
  }

  MobileToolResult _toolNames(Map<String, dynamic> args) {
    final includeAliases = args['include_aliases'] != false;
    final includeUnavailable = args['include_unavailable'] != false;
    final platformFilter = _normalizePlatformFilter(
      args['platform'] ?? args['execution_platform'] ?? args['mobile_platform'],
    );
    final names = <String>[];
    for (final record
        in _runtimeCatalogRecords(includeUnavailable: includeUnavailable)) {
      if (!_recordMatchesPlatform(record, platformFilter)) continue;
      for (final key in ['function_id', 'tool_id', 'requested_name']) {
        final value = '${record[key] ?? ''}'.trim();
        if (value.isNotEmpty && _isOpenAiFunctionName(value)) names.add(value);
      }
      if (includeAliases) {
        names.addAll(
          (record['aliases'] as List? ?? const [])
              .map((alias) => '$alias')
              .where(_isOpenAiFunctionName),
        );
      }
    }
    return MobileToolResult(
      ok: true,
      summary: '${names.length} tool names',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'names': names.toSet().toList()..sort(),
          'mobile_compatible_tag': mobileCompatibleTag,
          'platform_filter': platformFilter,
        },
      }),
    );
  }

  MobileToolResult _toolList(Map<String, dynamic> args) {
    final includeUnavailable = args['include_unavailable'] != false;
    final platformFilter = _normalizePlatformFilter(
      args['platform'] ?? args['execution_platform'] ?? args['mobile_platform'],
    );
    final limit = (args['limit'] is num)
        ? math.max(1, math.min(400, (args['limit'] as num).toInt()))
        : 240;
    final allRecords =
        _runtimeCatalogRecords(includeUnavailable: includeUnavailable)
            .where((record) => _recordMatchesPlatform(record, platformFilter))
            .toList();
    final records = allRecords.take(limit).toList();
    return MobileToolResult(
      ok: true,
      summary: '${records.length} tools',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'agent_template': _agentTemplateRecord(),
          'tool_surface': _toolSurfaceRecord(pcDelegationAvailable),
          'platform_filter': platformFilter,
          'platform_summary': _platformSummary(allRecords),
          'tools': records,
          'manifest_tool_agent_count':
              _defaultspackToolAgentManifestCatalog.length,
          'truncated': allRecords.length > records.length,
        },
      }),
    );
  }

  MobileToolResult _toolSchema(Map<String, dynamic> args) {
    final requested =
        '${args['tool_name'] ?? args['name'] ?? args['tool_id'] ?? ''}'.trim();
    if (requested.isEmpty) {
      return const MobileToolResult(
        ok: false,
        summary: 'tool_name is required',
        output: 'tool_name is required',
      );
    }
    final canonical = _canonicalToolName(requested);
    final entry = _findDefaultspackCatalogEntry(requested) ??
        _findDefaultspackCatalogEntry(canonical);
    final tool = _findToolDefinition(canonical);
    final record = entry != null
        ? _decorateCatalogRecord(
            _catalogEntryRecord(entry, requestedName: requested),
          )
        : tool == null
            ? _decorateCatalogRecord(_unsupportedToolRecord(canonical))
            : _decorateCatalogRecord(
                _toolRecord(tool, requestedName: requested));
    return MobileToolResult(
      ok: true,
      summary: '${record['tool_id']} schema',
      output: jsonEncode({
        'status': 'ok',
        'data': record,
      }),
    );
  }

  MobileToolResult _toolInvoke(Map<String, dynamic> args) {
    final requested =
        '${args['tool_name'] ?? args['tool_id'] ?? args['name'] ?? ''}'.trim();
    if (requested.isEmpty) {
      return MobileToolResult(
        ok: false,
        summary: 'tool_name is required',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'MISSING_PARAM',
            'message': 'tool_name is required',
          },
        }),
      );
    }

    final canonical = _canonicalToolName(requested);
    if (canonical == 'tool_invoke') {
      return MobileToolResult(
        ok: false,
        summary: 'recursive tool_invoke is not allowed',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'RECURSIVE_TOOL_INVOKE',
            'message': 'tool_invoke cannot invoke itself',
          },
        }),
      );
    }

    final tool = _findToolDefinition(canonical);
    final invokeArgs = _invokeArguments(args);
    if (_isMobileConnectorDryRunTool(canonical)) {
      final result = _connectorDryRun(canonical, invokeArgs);
      return MobileToolResult(
        ok: result.ok,
        summary: '$canonical: ${result.summary}',
        output: jsonEncode({
          'status': result.ok ? 'ok' : 'error',
          'data': {
            'tool_name': canonical,
            'requested_tool_name': requested,
            'result': result.output,
            'summary': result.summary,
            'is_error': !result.ok,
            'execution_location': 'phone',
          },
        }),
      );
    }
    if (tool != null && tool.available) {
      final result = execute(
        MobileToolCall(
          id: 'tool_invoke:${DateTime.now().microsecondsSinceEpoch}',
          name: canonical,
          arguments: invokeArgs,
        ),
      );
      return MobileToolResult(
        ok: result.ok,
        summary: '${tool.name}: ${result.summary}',
        output: jsonEncode({
          'status': result.ok ? 'ok' : 'error',
          'data': {
            'tool_name': tool.name,
            'requested_tool_name': requested,
            'result': result.output,
            'summary': result.summary,
            'is_error': !result.ok,
            'execution_location': 'phone',
          },
        }),
      );
    }

    final entry = _findDefaultspackCatalogEntry(requested) ??
        _findDefaultspackCatalogEntry(canonical);
    final record = entry == null
        ? _decorateCatalogRecord(_unsupportedToolRecord(canonical))
        : _decorateCatalogRecord(
            _catalogEntryRecord(entry, requestedName: requested),
          );
    final reason = '${record['unavailable_reason'] ?? ''}'.trim();
    return MobileToolResult(
      ok: false,
      summary: '${record['tool_id'] ?? requested}: unavailable on phone',
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': 'TOOL_UNAVAILABLE_ON_PHONE',
          'message': reason.isEmpty
              ? 'This tool is not executable in the phone-local runtime.'
              : reason,
          'details': record,
        },
      }),
    );
  }

  List<Map<String, dynamic>> _runtimeCatalogRecords({
    required bool includeUnavailable,
  }) {
    return _catalogRecords(includeUnavailable: includeUnavailable)
        .map(_decorateCatalogRecord)
        .toList();
  }

  Map<String, dynamic> _decorateCatalogRecord(Map<String, dynamic> record) {
    final decorated = Map<String, dynamic>.from(record);
    final phoneCompatible = decorated['mobile_compatible'] == true;
    final executionRoute = phoneCompatible
        ? 'phone'
        : pcDelegationAvailable
            ? 'pc'
            : 'unavailable';
    decorated['pc_delegation_available'] = pcDelegationAvailable;
    decorated['callable'] = phoneCompatible || pcDelegationAvailable;
    decorated['callable_on_current_device'] =
        phoneCompatible || pcDelegationAvailable;
    decorated['execution_route'] = executionRoute;
    decorated['automatic_routing'] = {
      'enabled': true,
      'one_tool_surface': true,
      'selected_route': executionRoute,
      'phone_local': phoneCompatible,
      'pc_delegation_available': pcDelegationAvailable,
      'pc_delegation_route': '/api/mobile/v1/tools/invoke',
    };
    if (!phoneCompatible) {
      decorated['pc_delegation'] = {
        'available': pcDelegationAvailable,
        'route': '/api/mobile/v1/tools/invoke',
        'required_setting': 'PC環境のtoolを使う',
        'execution_location': 'pc',
      };
      if (pcDelegationAvailable) {
        decorated['unavailable_reason'] =
            '${decorated['unavailable_reason'] ?? ''} PC接続と「PC環境のtoolを使う」が有効なので、tool_invoke経由で接続中PCのdefaultspack runtimeへ委譲できます。';
      }
    }
    return decorated;
  }

  MobileToolResult _packageInstallPlan(Map<String, dynamic> args) {
    final rawManager = '${args['manager'] ?? 'npm'}'.trim().toLowerCase();
    final manager = switch (rawManager) {
      'node' || 'npm' => 'npm',
      'pnpm' => 'pnpm',
      'yarn' => 'yarn',
      'pip' || 'pip3' || 'python' || 'python3' => 'pip',
      _ => rawManager,
    };
    if (!const {'npm', 'pnpm', 'yarn', 'pip'}.contains(manager)) {
      return MobileToolResult(
        ok: false,
        summary: 'unsupported package manager',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'UNSUPPORTED_PACKAGE_MANAGER',
            'message':
                'Phone-local package_install_plan supports npm, pnpm, yarn, and pip.',
            'manager': rawManager,
            'execution_location': 'phone',
          },
        }),
      );
    }
    final packages = _phoneStringList(args['packages']);
    final dev = _boolArg(args['dev'], fallback: false);
    final global = _boolArg(args['global'], fallback: false);
    final command = <String>[
      if (manager == 'pip') ...[
        'python',
        '-m',
        'pip',
        'install',
        if (global) '--user',
        ...packages,
      ] else if (manager == 'npm') ...[
        'npm',
        'install',
        if (global) '-g',
        if (dev) '--save-dev',
        ...packages,
      ] else if (manager == 'pnpm') ...[
        'pnpm',
        packages.isEmpty ? 'install' : 'add',
        if (global) '-g',
        if (dev) '-D',
        ...packages,
      ] else ...[
        'yarn',
        packages.isEmpty ? 'install' : 'add',
        if (global) 'global',
        if (dev && packages.isNotEmpty) '--dev',
        ...packages,
      ],
    ];
    return MobileToolResult(
      ok: true,
      summary: '$manager install plan',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'manager': manager,
          'packages': packages,
          'command': command,
          'dry_run': true,
          'execute': false,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
          'implementation_status': 'implemented_phone_install_plan',
          'pc_execute_note':
              'Use PC delegation to execute package installation commands.',
        },
      }),
    );
  }

  MobileToolResult _workflowDefine(Map<String, dynamic> args) {
    final generatedId = _nextToolId('workflow');
    final workflowId = _slugifyPhoneArtifactName(
      _workflowIdArg(args).isEmpty ? generatedId : _workflowIdArg(args),
      fallback: generatedId,
    );
    final steps = _normalizeWorkflowSteps(args['steps'] ?? args['workflow']);
    if (steps.isEmpty) {
      return _phoneWorkflowError(
        'INVALID_INPUT',
        "'steps' must contain at least one executable workflow step.",
        workflowId: workflowId,
      );
    }
    if (steps.length > _workflowMaxSteps) {
      return _phoneWorkflowError(
        'TOO_MANY_STEPS',
        'Phone-local workflows support at most $_workflowMaxSteps steps.',
        workflowId: workflowId,
      );
    }
    final invalidStep = steps.where((step) {
      return '${step['tool_name'] ?? ''}'.trim().isEmpty;
    }).toList();
    if (invalidStep.isNotEmpty) {
      return _phoneWorkflowError(
        'INVALID_STEP',
        "Every workflow step must include 'tool', 'tool_name', or 'name'.",
        workflowId: workflowId,
      );
    }
    final existing = _mobileWorkflows[workflowId];
    final now = DateTime.now().toUtc().toIso8601String();
    _mobileWorkflows[workflowId] = {
      'workflow_id': workflowId,
      'name': '${args['name'] ?? args['title'] ?? workflowId}'.trim(),
      'description': '${args['description'] ?? ''}'.trim(),
      'steps': steps,
      'created_at': existing?['created_at'] ?? now,
      'updated_at': now,
      'workspace': 'phone',
      'execution_location': 'phone',
      'runtime_layers': _phoneWorkflowRuntimeLayers,
    };
    _persistPhoneWorkflow(workflowId);
    return MobileToolResult(
      ok: true,
      summary: 'defined phone-local workflow $workflowId',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ..._phoneWorkflowRecord(workflowId),
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _workflowRun(Map<String, dynamic> args) {
    return _workflowRunInternal(
      args,
      approvalToolName: 'workflow_run',
      requireApproval: true,
    );
  }

  MobileToolResult _workflowStatus(Map<String, dynamic> args) {
    final runId = _requiredWorkflowRunId(args);
    if (runId == null) {
      final runs = _mobileWorkflowRuns.keys
          .map(_phoneWorkflowRunRecord)
          .toList()
        ..sort((a, b) => '${a['created_at']}'.compareTo('${b['created_at']}'));
      return MobileToolResult(
        ok: true,
        summary: '${runs.length} phone-local workflow runs',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'runs': runs,
            'count': runs.length,
            'workspace': 'phone',
            'execution_location': 'phone',
            'runtime_layers': _phoneWorkflowRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    }
    if (!_mobileWorkflowRuns.containsKey(runId)) {
      return _phoneWorkflowError(
        'RUN_NOT_FOUND',
        'phone-local workflow run not found',
        runId: runId,
      );
    }
    return MobileToolResult(
      ok: true,
      summary: 'workflow run $runId ${_mobileWorkflowRuns[runId]?['status']}',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ..._phoneWorkflowRunRecord(runId),
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _workflowCancel(Map<String, dynamic> args) async {
    final runId = _requiredWorkflowRunId(args);
    if (runId == null) {
      return _phoneWorkflowError(
        'INVALID_INPUT',
        "'run_id' is required for workflow_cancel.",
      );
    }
    final run = _mobileWorkflowRuns[runId];
    if (run == null) {
      return _phoneWorkflowError(
        'RUN_NOT_FOUND',
        'phone-local workflow run not found',
        runId: runId,
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'workflow_cancel',
      prompt: 'このスマホ内のworkflow runをcancelします。対象: $runId',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('workflow_cancel');
    final now = DateTime.now().toUtc().toIso8601String();
    run['status'] = 'cancelled';
    run['cancelled_at'] = now;
    run['updated_at'] = now;
    _appendPhoneWorkflowEvent(runId, 'cancelled', const {});
    _persistPhoneWorkflowRun(runId);
    return MobileToolResult(
      ok: true,
      summary: 'cancelled workflow run $runId',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ..._phoneWorkflowRunRecord(runId),
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  Future<MobileToolResult> _workflowRetry(Map<String, dynamic> args) async {
    final requestedRunId = _requiredWorkflowRunId(args);
    final requestedWorkflowId =
        '${args['workflow_id'] ?? args['workflowId'] ?? ''}'.trim();
    Map<String, dynamic>? priorRun;
    if (requestedRunId != null) {
      priorRun = _mobileWorkflowRuns[requestedRunId];
      if (priorRun == null) {
        return _phoneWorkflowError(
          'RUN_NOT_FOUND',
          'phone-local workflow run not found',
          runId: requestedRunId,
        );
      }
    }
    final workflowId = requestedWorkflowId.isNotEmpty
        ? _slugifyPhoneArtifactName(
            requestedWorkflowId,
            fallback: requestedWorkflowId,
          )
        : '${priorRun?['workflow_id'] ?? ''}'.trim();
    final steps = _cloneWorkflowSteps(priorRun?['steps']);
    if (workflowId.isEmpty && steps.isEmpty) {
      return _phoneWorkflowError(
        'INVALID_INPUT',
        "'workflow_id' or 'run_id' is required for workflow_retry.",
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'workflow_retry',
      prompt:
          'このスマホ内のworkflowを再実行します。対象: ${workflowId.isEmpty ? requestedRunId : workflowId}',
      arguments: args,
      risk: 'high',
    );
    if (!approved) return _mobileApprovalRequired('workflow_retry');
    return _workflowRunInternal(
      {
        ...args,
        if (workflowId.isNotEmpty) 'workflow_id': workflowId,
        if (steps.isNotEmpty) 'steps': steps,
      },
      approvalToolName: 'workflow_retry',
      requireApproval: false,
      retryOfRunId: requestedRunId,
    );
  }

  Future<MobileToolResult> _workflowRunInternal(
    Map<String, dynamic> args, {
    required String approvalToolName,
    required bool requireApproval,
    String? retryOfRunId,
  }) async {
    final rawWorkflowId = _workflowIdArg(args);
    final workflowId = rawWorkflowId.isEmpty
        ? ''
        : _slugifyPhoneArtifactName(rawWorkflowId, fallback: rawWorkflowId);
    final workflow = workflowId.isEmpty ? null : _mobileWorkflows[workflowId];
    var steps = _normalizeWorkflowSteps(args['steps'] ?? args['workflow']);
    if (steps.isEmpty && workflow != null) {
      steps = _cloneWorkflowSteps(workflow['steps']);
    }
    if (steps.isEmpty) {
      return _phoneWorkflowError(
        'INVALID_INPUT',
        workflowId.isEmpty
            ? "'workflow_id' or inline 'steps' is required for workflow_run."
            : 'phone-local workflow not found or has no steps',
        workflowId: workflowId.isEmpty ? null : workflowId,
      );
    }
    if (steps.length > _workflowMaxSteps) {
      return _phoneWorkflowError(
        'TOO_MANY_STEPS',
        'Phone-local workflows support at most $_workflowMaxSteps steps.',
        workflowId: workflowId.isEmpty ? null : workflowId,
      );
    }
    final invalidStep = steps.where((step) {
      return '${step['tool_name'] ?? ''}'.trim().isEmpty;
    }).toList();
    if (invalidStep.isNotEmpty) {
      return _phoneWorkflowError(
        'INVALID_STEP',
        "Every workflow step must include 'tool', 'tool_name', or 'name'.",
        workflowId: workflowId.isEmpty ? null : workflowId,
      );
    }
    if (requireApproval) {
      final approved = await _requestMobileApproval(
        toolName: approvalToolName,
        prompt:
            'このスマホ内でworkflowを実行します。step数: ${steps.length}${workflowId.isEmpty ? '' : ' / workflow: $workflowId'}',
        arguments: args,
        risk: 'high',
      );
      if (!approved) return _mobileApprovalRequired(approvalToolName);
    }

    final requestedRunId = '${args['run_id'] ?? args['id'] ?? ''}'.trim();
    final generatedRunId = _nextToolId('workflow_run');
    final runId = _slugifyPhoneArtifactName(
      requestedRunId.isEmpty ? generatedRunId : requestedRunId,
      fallback: generatedRunId,
    );
    if (_mobileWorkflowRuns.containsKey(runId)) {
      return _phoneWorkflowError(
        'RUN_ALREADY_EXISTS',
        'phone-local workflow run already exists',
        runId: runId,
      );
    }
    final now = DateTime.now().toUtc().toIso8601String();
    _mobileWorkflowRuns[runId] = {
      'run_id': runId,
      if (workflowId.isNotEmpty) 'workflow_id': workflowId,
      if (retryOfRunId != null) 'retry_of_run_id': retryOfRunId,
      'name': '${workflow?['name'] ?? args['name'] ?? workflowId}'.trim(),
      'status': 'running',
      'steps': steps,
      'results': <Map<String, dynamic>>[],
      'created_at': now,
      'updated_at': now,
      'workspace': 'phone',
      'execution_location': 'phone',
      'runtime_layers': _phoneWorkflowRuntimeLayers,
    };
    _appendPhoneWorkflowEvent(runId, 'started', {
      'workflow_id': workflowId,
      'step_count': steps.length,
      if (retryOfRunId != null) 'retry_of_run_id': retryOfRunId,
    });
    _persistPhoneWorkflowRun(runId);

    final run = _mobileWorkflowRuns[runId]!;
    final results = run['results'] as List<Map<String, dynamic>>;
    var allOk = true;
    for (var index = 0; index < steps.length; index++) {
      final step = steps[index];
      final stepId = '${step['id'] ?? 'step-${index + 1}'}';
      final toolName = '${step['tool_name'] ?? ''}'.trim();
      final canonical = _canonicalToolName(toolName);
      final stepArgs = _workflowStepArgs(step);
      final blockedReason = _workflowStepBlockedReason(canonical, stepArgs);
      _appendPhoneWorkflowEvent(runId, 'step_started', {
        'step_id': stepId,
        'tool_name': canonical,
      });
      final startedAt = DateTime.now().toUtc().toIso8601String();
      if (blockedReason != null) {
        allOk = false;
        final result = {
          'step_id': stepId,
          'tool_name': canonical,
          'ok': false,
          'summary': blockedReason,
          'error': {
            'code': 'WORKFLOW_STEP_NOT_ALLOWED',
            'message': blockedReason,
          },
          'arguments_keys': stepArgs.keys.toList(),
          'started_at': startedAt,
          'completed_at': DateTime.now().toUtc().toIso8601String(),
          'execution_location': 'phone',
        };
        results.add(result);
        run['status'] = 'failed';
        run['failed_step_id'] = stepId;
        run['updated_at'] = DateTime.now().toUtc().toIso8601String();
        _appendPhoneWorkflowEvent(runId, 'step_failed', result);
        _persistPhoneWorkflowRun(runId);
        break;
      }

      try {
        final result = await executeAsync(
          MobileToolCall(
            id: 'workflow:$runId:$stepId',
            name: toolName,
            arguments: stepArgs,
          ),
        );
        final parsed = _decodeObject(result.output);
        final stepResult = {
          'step_id': stepId,
          'tool_name': canonical,
          'requested_tool_name': toolName,
          'ok': result.ok,
          'summary': result.summary,
          'output_preview': _workflowOutputPreview(result.output),
          if (parsed.isNotEmpty) 'parsed_status': parsed['status'],
          'arguments_keys': stepArgs.keys.toList(),
          'started_at': startedAt,
          'completed_at': DateTime.now().toUtc().toIso8601String(),
          'execution_location': _workflowStepExecutionLocation(parsed),
        };
        results.add(stepResult);
        if (!result.ok) {
          allOk = false;
          run['status'] = 'failed';
          run['failed_step_id'] = stepId;
          _appendPhoneWorkflowEvent(runId, 'step_failed', stepResult);
          run['updated_at'] = DateTime.now().toUtc().toIso8601String();
          _persistPhoneWorkflowRun(runId);
          break;
        }
        _appendPhoneWorkflowEvent(runId, 'step_completed', stepResult);
        run['updated_at'] = DateTime.now().toUtc().toIso8601String();
        _persistPhoneWorkflowRun(runId);
      } catch (error) {
        allOk = false;
        final stepResult = {
          'step_id': stepId,
          'tool_name': canonical,
          'ok': false,
          'summary': '$error',
          'error': {
            'code': 'WORKFLOW_STEP_EXCEPTION',
            'message': '$error',
          },
          'arguments_keys': stepArgs.keys.toList(),
          'started_at': startedAt,
          'completed_at': DateTime.now().toUtc().toIso8601String(),
          'execution_location': 'phone',
        };
        results.add(stepResult);
        run['status'] = 'failed';
        run['failed_step_id'] = stepId;
        run['updated_at'] = DateTime.now().toUtc().toIso8601String();
        _appendPhoneWorkflowEvent(runId, 'step_failed', stepResult);
        _persistPhoneWorkflowRun(runId);
        break;
      }
    }
    final finishedAt = DateTime.now().toUtc().toIso8601String();
    run['status'] = allOk ? 'completed' : run['status'];
    run['completed_at'] = finishedAt;
    run['updated_at'] = finishedAt;
    _appendPhoneWorkflowEvent(runId, allOk ? 'completed' : 'failed', {
      'ok': allOk,
      'completed_steps': results.length,
      'step_count': steps.length,
    });
    _persistPhoneWorkflowRun(runId);
    return MobileToolResult(
      ok: allOk,
      summary: allOk
          ? 'workflow run $runId completed'
          : 'workflow run $runId failed',
      output: jsonEncode({
        'status': allOk ? 'ok' : 'error',
        'data': {
          ..._phoneWorkflowRunRecord(runId),
          'requires_mobile_approval': requireApproval,
        },
      }),
    );
  }

  MobileToolResult _jobCreate(Map<String, dynamic> args) {
    final requestedId = '${args['job_id'] ?? args['id'] ?? ''}'.trim();
    final generatedId = _nextToolId('job');
    final jobId = _slugifyPhoneArtifactName(
      requestedId.isEmpty ? generatedId : requestedId,
      fallback: generatedId,
    );
    if (_mobileJobs.containsKey(jobId)) {
      return _phoneJobError(
        'JOB_ALREADY_EXISTS',
        'phone-local job already exists',
        jobId: jobId,
      );
    }
    final input = _phoneJobInput(args);
    final runImmediately = _boolArg(args['run_immediately'], fallback: false);
    final now = DateTime.now().toUtc().toIso8601String();
    final job = <String, dynamic>{
      'job_id': jobId,
      'kind': '${args['kind'] ?? 'local'}'.trim().isEmpty
          ? 'local'
          : '${args['kind'] ?? 'local'}'.trim(),
      'status': runImmediately ? 'completed' : 'queued',
      'input': input,
      'created_at': now,
      'updated_at': now,
      'workspace': 'phone',
      'execution_location': 'phone',
      'runtime_layers': _flutterRuntimeLayers,
      'artifacts': <String>[],
    };
    _mobileJobs[jobId] = job;
    _appendPhoneJobEvent(jobId, 'created', {'run_immediately': runImmediately});
    _persistPhoneJob(jobId);
    if (runImmediately) {
      final resultPath = 'jobs/$jobId/result.json';
      final resultContent = const JsonEncoder.withIndent('  ').convert({
        'job_id': jobId,
        'status': 'completed',
        'input': input,
        'note':
            'Completed by the phone-local job runtime. No PC process was executed.',
      });
      _putPhoneArtifactContent(
        resultPath,
        '$resultContent\n',
        source: 'job_create',
        mimeType: 'application/json',
        metadata: {'job_id': jobId, 'artifact_role': 'result'},
      );
      _phoneJobArtifacts(jobId).add(resultPath);
      _appendPhoneJobEvent(jobId, 'completed', {'artifact_path': resultPath});
      _persistPhoneJob(jobId);
    }
    return MobileToolResult(
      ok: true,
      summary: 'created phone-local job $jobId',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ..._phoneJobRecord(jobId),
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _jobStatus(Map<String, dynamic> args) {
    final jobId = '${args['job_id'] ?? args['id'] ?? ''}'.trim();
    if (jobId.isEmpty) {
      final jobs = _mobileJobs.keys.map(_phoneJobRecord).toList()
        ..sort((a, b) => '${a['created_at']}'.compareTo('${b['created_at']}'));
      return MobileToolResult(
        ok: true,
        summary: '${jobs.length} phone-local jobs',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            'jobs': jobs,
            'count': jobs.length,
            'workspace': 'phone',
            'execution_location': 'phone',
            'runtime_layers': _flutterRuntimeLayers,
            'requires_mobile_approval': false,
          },
        }),
      );
    }
    final normalized = _slugifyPhoneArtifactName(jobId, fallback: jobId);
    if (!_mobileJobs.containsKey(normalized)) {
      return _phoneJobError(
        'JOB_NOT_FOUND',
        'phone-local job not found',
        jobId: normalized,
      );
    }
    return MobileToolResult(
      ok: true,
      summary: 'job $normalized ${_mobileJobs[normalized]?['status']}',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ..._phoneJobRecord(normalized),
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _jobHistory(Map<String, dynamic> args) {
    final jobId = _requiredPhoneJobId(args);
    if (jobId == null) {
      return _phoneJobError(
        'INVALID_INPUT',
        "'job_id' is required for job_history.",
      );
    }
    if (!_mobileJobs.containsKey(jobId)) {
      return _phoneJobError('JOB_NOT_FOUND', 'phone-local job not found',
          jobId: jobId);
    }
    final events = List<Map<String, dynamic>>.from(
      _mobileJobEvents[jobId] ?? const [],
    );
    return MobileToolResult(
      ok: true,
      summary: '${events.length} job events',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'job_id': jobId,
          'events': events,
          'count': events.length,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  MobileToolResult _jobArtifacts(Map<String, dynamic> args) {
    final jobId = _requiredPhoneJobId(args);
    if (jobId == null) {
      return _phoneJobError(
        'INVALID_INPUT',
        "'job_id' is required for job_artifacts.",
      );
    }
    if (!_mobileJobs.containsKey(jobId)) {
      return _phoneJobError('JOB_NOT_FOUND', 'phone-local job not found',
          jobId: jobId);
    }
    final artifacts =
        _phoneJobArtifacts(jobId).where(_mobileArtifactFiles.containsKey).map(
      (path) {
        final file = _mobileArtifactFiles[path]!;
        return {
          'path': path,
          'size':
              file['size'] ?? utf8.encode('${file['content'] ?? ''}').length,
          'mime_type': file['mime_type'],
          'metadata': file['metadata'],
        };
      },
    ).toList();
    return MobileToolResult(
      ok: true,
      summary: '${artifacts.length} job artifacts',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'job_id': jobId,
          'artifacts': artifacts,
          'count': artifacts.length,
          'workspace': 'phone',
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
        },
      }),
    );
  }

  Future<MobileToolResult> _jobMutation(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    final jobId = _requiredPhoneJobId(args);
    if (jobId == null) {
      return _phoneJobError(
        'INVALID_INPUT',
        "'job_id' is required for $toolName.",
      );
    }
    final job = _mobileJobs[jobId];
    if (job == null) {
      return _phoneJobError('JOB_NOT_FOUND', 'phone-local job not found',
          jobId: jobId);
    }
    final approved = await _requestMobileApproval(
      toolName: toolName,
      prompt: 'このスマホ内のjob recordを更新します。対象: $jobId',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired(toolName);
    final now = DateTime.now().toUtc().toIso8601String();
    if (toolName == 'job_cancel') {
      job['status'] = 'canceled';
      job['canceled_at'] = now;
      _appendPhoneJobEvent(jobId, 'canceled', const {});
    } else {
      job['status'] = 'queued';
      job['resumed_at'] = now;
      _appendPhoneJobEvent(jobId, 'resumed', const {});
    }
    job['updated_at'] = now;
    _persistPhoneJob(jobId);
    return MobileToolResult(
      ok: true,
      summary: '$toolName updated $jobId',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ..._phoneJobRecord(jobId),
          'requires_mobile_approval': true,
        },
      }),
    );
  }

  MobileToolResult _agentPlan(Map<String, dynamic> args) {
    final action = '${args['action'] ?? 'create'}'.trim().toLowerCase();
    if (action == 'clear') {
      final count = _mobileAgentPlans.length;
      _mobileAgentPlans.clear();
      return MobileToolResult(
        ok: true,
        summary: 'cleared $count plans',
        output: jsonEncode({
          'status': 'ok',
          'data': {'cleared': count, 'plans': _mobileAgentPlans},
        }),
      );
    }
    if (action == 'list' || action == 'show' || action == 'status') {
      return _agentStatus(args, 'agent_plan');
    }
    final objective =
        '${args['objective'] ?? args['title'] ?? args['task'] ?? ''}'.trim();
    final steps = _normalizePlanSteps(args['steps'] ?? args['plan']);
    final plan = {
      'id': _nextToolId('plan'),
      'title': objective.isEmpty ? 'Mobile agent plan' : objective,
      'objective': objective,
      'steps': steps,
      'status': 'active',
      'created_at': DateTime.now().millisecondsSinceEpoch,
      'updated_at': DateTime.now().millisecondsSinceEpoch,
    };
    _mobileAgentPlans.add(plan);
    return MobileToolResult(
      ok: true,
      summary: '${steps.length} step plan',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'plan': plan,
          'agent_template': _agentTemplateRecord(),
        },
      }),
    );
  }

  MobileToolResult _agentStatus(Map<String, dynamic> args, String toolName) {
    final openTodos =
        _mobileTodos.where((todo) => todo['status'] != 'done').length;
    final activePlans = _mobileAgentPlans
        .where((plan) => '${plan['status'] ?? ''}' != 'done')
        .toList();
    final data = {
      'tool': toolName,
      'status':
          activePlans.isEmpty && _mobileTaskCards.isEmpty ? 'idle' : 'active',
      'agent_template': _agentTemplateRecord(),
      'plans': _mobileAgentPlans,
      'task_board': _taskBoardSnapshot(),
      'todos': _mobileTodos,
      'summary': {
        'plans': _mobileAgentPlans.length,
        'active_plans': activePlans.length,
        'task_cards': _mobileTaskCards.length,
        'todos': _mobileTodos.length,
        'open_todos': openTodos,
      },
    };
    return MobileToolResult(
      ok: true,
      summary:
          '${activePlans.length} active plans, ${_mobileTaskCards.length} cards, $openTodos open todos',
      output: jsonEncode({
        'status': 'ok',
        'data': data,
      }),
    );
  }

  MobileToolResult _assistantProgress(Map<String, dynamic> args) {
    final payload = normalizeAssistantProgressPayload(args);
    final summary = '${payload['summary']}';
    return MobileToolResult(
      ok: true,
      summary: summary,
      output: jsonEncode({
        'status': 'ok',
        'summary': summary,
        'next_action': payload['next_action'],
        'data': payload,
      }),
    );
  }

  MobileToolResult _connectorDryRun(
    String toolName,
    Map<String, dynamic> args,
  ) {
    final normalized = toolName.trim().toLowerCase();
    if (_mobileCliDryRunToolIds.contains(normalized)) {
      final commandPlan = _connectorCliCommands(normalized, args);
      if (!commandPlan.ok) {
        return MobileToolResult(
          ok: false,
          summary: '$normalized connector dry-run failed',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': commandPlan.errorCode,
              'message': commandPlan.errorMessage,
              'tool': normalized,
              'execution_location': 'phone',
            },
          }),
        );
      }
      final commands = commandPlan.commands;
      final data = <String, dynamic>{
        'dry_run': true,
        'commands': commands,
        'tool': normalized,
        if (commands.isNotEmpty) 'command': commands.first,
        if (commandPlan.payload != null) 'payload': commandPlan.payload,
      };
      if (_boolArg(args['execute'], fallback: false)) {
        return MobileToolResult(
          ok: false,
          summary: '$normalized requires PC runtime for execute=true',
          output: jsonEncode({
            'status': 'error',
            'error': {
              'code': 'PC_RUNTIME_REQUIRED',
              'message':
                  '$normalized can build a dry-run command plan on this phone, but execute=true requires the connected PC runtime/defaultspack CLI.',
              'data': {
                ...data,
                'dry_run': false,
                'execution_location': 'phone',
                'runtime_layers': _flutterRuntimeLayers,
                'implementation_status': 'implemented_cli_dry_run_pc_execute',
              },
            },
          }),
        );
      }
      return MobileToolResult(
        ok: true,
        summary: '$normalized dry-run command plan',
        output: jsonEncode({
          'status': 'ok',
          'data': {
            ...data,
            'execution_location': 'phone',
            'runtime_layers': _flutterRuntimeLayers,
            'requires_mobile_approval': false,
            'implementation_status': 'implemented_cli_dry_run_pc_execute',
          },
        }),
      );
    }

    final data = _connectorPayloadDryRunData(normalized, args);
    if (data == null) {
      return MobileToolResult(
        ok: false,
        summary: '$normalized is not a phone connector dry-run tool',
        output: jsonEncode({
          'status': 'error',
          'error': {
            'code': 'UNSUPPORTED_CONNECTOR_TOOL',
            'message': '$normalized is not available in phone-local dry-run.',
          },
        }),
      );
    }
    return MobileToolResult(
      ok: true,
      summary: '$normalized connector dry-run',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          ...data,
          'tool': normalized,
          'dry_run': true,
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'requires_mobile_approval': false,
          'implementation_status': 'implemented_connector_dry_run',
        },
      }),
    );
  }

  Future<MobileToolResult> _aiModelTool(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    switch (toolName) {
      case 'ai_models':
        return _aiModels(args);
      case 'ai_profiles':
        return _aiProfiles(args);
      case 'ai_providers':
        return _aiProviders(args);
      case 'ai_get_provider_key_status':
        return _aiProviderKeyStatus(args);
      case 'ai_set_provider_key':
        return _aiSetProviderKey(args);
      case 'ai_delete_provider_key':
        return _aiDeleteProviderKey(args);
      case 'ai_get_preferred_model':
        return _aiGetPreferredModel(args);
      case 'ai_set_preferred_model':
        return _aiSetPreferredModel(args);
      case 'ai_get_thinking_level':
        return _aiGetThinkingLevel(args);
      case 'ai_set_thinking_level':
        return _aiSetThinkingLevel(args);
      case 'ai_get_effective_thinking_level':
        return _aiGetEffectiveThinkingLevel(args);
      case 'ai_normalize_thinking_level':
        return _aiNormalizeThinkingLevel(args);
      case 'ai_validate_model_params':
        return _aiValidateModelParams(args);
      case 'ai_recommend_model':
      case 'ai_route_model':
        return _aiRouteModel(toolName, args);
      case 'ai_explain_model_choice':
        return _aiExplainModelChoice(args);
      default:
        return Future.value(execute(MobileToolCall(
          id: 'ai:${DateTime.now().microsecondsSinceEpoch}',
          name: toolName,
          arguments: args,
        )));
    }
  }

  Future<MobileToolResult> _aiModels(Map<String, dynamic> args) async {
    final providers = await _loadMergedMobileProviders();
    final active = await _store.loadApi();
    final favorites = await _store.loadModelFavorites();
    final providerFilter = _providerArg(args);
    final query = '${args['query'] ?? ''}'.trim().toLowerCase();
    final configuredOnly = _boolArg(args['configured_only'], fallback: false);
    final favoritesOnly = _boolArg(args['favorites_only'], fallback: false);
    final rows = <Map<String, dynamic>>[];
    for (final provider in providers) {
      if (providerFilter.isNotEmpty && provider.providerId != providerFilter) {
        continue;
      }
      final favorite = _isFavoriteProviderModel(provider, favorites);
      if (configuredOnly && !provider.isConfigured) continue;
      if (favoritesOnly && !favorite) continue;
      final searchable = [
        provider.providerId,
        provider.displayName,
        provider.effectiveLabel,
        provider.model,
      ].join(' ').toLowerCase();
      if (query.isNotEmpty && !searchable.contains(query)) continue;
      rows.add(_mobileModelRecord(provider, active, favorites));
    }
    return _aiToolOk(
      'ai_models',
      '${rows.length} mobile models',
      {
        'models': rows,
        'count': rows.length,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiProfiles(Map<String, dynamic> args) async {
    final providers = await _loadMergedMobileProviders();
    final active = await _store.loadApi();
    final favorites = await _store.loadModelFavorites();
    final query = '${args['query'] ?? ''}'.trim().toLowerCase();
    final configuredOnly = _boolArg(args['configured_only'], fallback: false);
    final favoritesOnly = _boolArg(args['favorites_only'], fallback: false);
    final profiles = <Map<String, dynamic>>[];
    for (final provider in providers) {
      final favorite = _isFavoriteProviderModel(provider, favorites);
      if (configuredOnly && !provider.isConfigured) continue;
      if (favoritesOnly && !favorite) continue;
      final profileId = '${provider.providerId}/${provider.model}';
      final searchable = [
        profileId,
        provider.displayName,
        provider.effectiveLabel,
      ].join(' ').toLowerCase();
      if (query.isNotEmpty && !searchable.contains(query)) continue;
      profiles.add({
        'profile_id': profileId,
        'provider_id': provider.providerId,
        'model': provider.model,
        'label': provider.effectiveLabel,
        'source': 'mobile',
        'configured': provider.isConfigured,
        'active': active.providerId == provider.providerId &&
            active.model == provider.model,
        'favorite': favorite,
        'api_compatibility': provider.apiCompatibility,
      });
    }
    for (final favorite in favorites.where((item) => item.isPc)) {
      final searchable = [
        favorite.profileId,
        favorite.providerId,
        favorite.modelId,
        favorite.effectiveLabel,
      ].join(' ').toLowerCase();
      if (query.isNotEmpty && !searchable.contains(query)) continue;
      profiles.add({
        'profile_id': favorite.profileId,
        'provider_id': favorite.providerId,
        'model': favorite.modelId,
        'label': favorite.effectiveLabel,
        'source': 'pc',
        'configured': false,
        'active': false,
        'favorite': true,
        'pc_label': favorite.pcLabel,
      });
    }
    return _aiToolOk(
      'ai_profiles',
      '${profiles.length} mobile profiles',
      {
        'profiles': profiles,
        'count': profiles.length,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiProviders(Map<String, dynamic> args) async {
    final providers = await _loadMergedMobileProviders();
    final active = await _store.loadApi();
    final favorites = await _store.loadModelFavorites();
    final configuredOnly = _boolArg(args['configured_only'], fallback: false);
    final query = '${args['query'] ?? ''}'.trim().toLowerCase();
    final rows = <Map<String, dynamic>>[];
    for (final provider in providers) {
      if (configuredOnly && !provider.isConfigured) continue;
      final searchable = [
        provider.providerId,
        provider.displayName,
        provider.effectiveLabel,
        provider.baseUrl,
      ].join(' ').toLowerCase();
      if (query.isNotEmpty && !searchable.contains(query)) continue;
      rows.add(_mobileProviderRecord(provider, active, favorites));
    }
    return _aiToolOk(
      'ai_providers',
      '${rows.length} mobile providers',
      {
        'providers': rows,
        'count': rows.length,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiProviderKeyStatus(
    Map<String, dynamic> args,
  ) async {
    final providers = await _loadMergedMobileProviders();
    final providerId = _providerArg(args);
    final selected = providerId.isEmpty
        ? providers
        : providers.where((provider) => provider.providerId == providerId);
    final statuses = [
      for (final provider in selected) _providerKeyStatusRecord(provider),
    ];
    return _aiToolOk(
      'ai_get_provider_key_status',
      providerId.isEmpty
          ? '${statuses.length} provider key statuses'
          : '$providerId key status',
      {
        'providers': statuses,
        if (providerId.isNotEmpty && statuses.isNotEmpty)
          'provider': statuses.first,
        'count': statuses.length,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiSetProviderKey(
    Map<String, dynamic> args,
  ) async {
    final apiKey = '${args['api_key'] ?? args['apiKey'] ?? ''}'.trim();
    if (apiKey.isEmpty) {
      return _aiToolError(
        'ai_set_provider_key',
        'MISSING_API_KEY',
        "'api_key' is required.",
      );
    }
    final providerId =
        _providerArg(args).isEmpty ? 'openai' : _providerArg(args);
    final provider =
        await _resolveProviderForArgs(args, providerId: providerId);
    final next = provider.copyWith(
      apiKey: apiKey,
      baseUrl: _firstText(args, const ['base_url', 'baseUrl']).isEmpty
          ? provider.baseUrl
          : _firstText(args, const ['base_url', 'baseUrl']),
      model: _firstText(args, const ['model']).isEmpty
          ? provider.model
          : _firstText(args, const ['model']),
      label: _firstText(args, const ['label']).isEmpty
          ? provider.label
          : _firstText(args, const ['label']),
    );
    final approved = await _requestMobileApproval(
      toolName: 'ai_set_provider_key',
      prompt: 'このスマホのsecure storageに${next.effectiveLabel}のAPI keyを保存します。',
      arguments: {
        ...args,
        'api_key': _maskSecret(apiKey),
        'apiKey': _maskSecret(apiKey),
      },
      risk: 'high',
    );
    if (!approved) return _mobileApprovalRequired('ai_set_provider_key');
    await _store.upsertProviderConfig(next);
    final active = await _store.loadApi();
    final activate = _boolArg(args['activate'], fallback: true);
    if (activate) {
      await _store.saveApi(
        next.toApiConfig(
          systemPrompt: active.systemPrompt,
          temperature: active.temperature,
        ),
      );
    }
    if (_boolArg(args['favorite'] ?? args['star'], fallback: activate)) {
      await _store.upsertModelFavorite(
        ModelFavoriteConfig.fromMobileProvider(next),
      );
    }
    return _aiToolOk(
      'ai_set_provider_key',
      '${next.providerId} provider key saved',
      {
        'provider': _providerKeyStatusRecord(next),
        'activated': activate,
        'favorite':
            _boolArg(args['favorite'] ?? args['star'], fallback: activate),
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiDeleteProviderKey(
    Map<String, dynamic> args,
  ) async {
    final providerId = _providerArg(args);
    if (providerId.isEmpty) {
      return _aiToolError(
        'ai_delete_provider_key',
        'MISSING_PROVIDER',
        "'provider' or 'provider_id' is required.",
      );
    }
    final provider =
        await _resolveProviderForArgs(args, providerId: providerId);
    final approved = await _requestMobileApproval(
      toolName: 'ai_delete_provider_key',
      prompt: 'このスマホから${provider.effectiveLabel}のAPI keyを削除します。',
      arguments: args,
      risk: 'high',
    );
    if (!approved) return _mobileApprovalRequired('ai_delete_provider_key');
    final next = provider.copyWith(apiKey: '');
    await _store.upsertProviderConfig(next);
    final active = await _store.loadApi();
    if (active.providerId == providerId) {
      await _store.saveApi(active.copyWith(apiKey: ''));
    }
    return _aiToolOk(
      'ai_delete_provider_key',
      '$providerId provider key deleted',
      {
        'provider': _providerKeyStatusRecord(next),
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiGetPreferredModel(
    Map<String, dynamic> args,
  ) async {
    final active = await _store.loadApi();
    final settings = await _store.loadModelRuntimeSettings();
    return _aiToolOk(
      'ai_get_preferred_model',
      active.model.trim().isEmpty ? 'no preferred model' : active.model,
      {
        'preferred_model': active.model,
        'provider_id': active.providerId,
        'label': active.label,
        'configured': active.isConfigured,
        'api_compatibility': active.apiCompatibility,
        'thinking_level':
            _normalizeThinkingLevelValue(settings['thinking_level']) ??
                'medium',
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiSetPreferredModel(
    Map<String, dynamic> args,
  ) async {
    final selected = await _selectMobileProviderModel(args);
    if (selected == null) {
      return _aiToolError(
        'ai_set_preferred_model',
        'MODEL_NOT_FOUND',
        'No matching phone-local provider/model was found.',
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'ai_set_preferred_model',
      prompt: 'このスマホの既定モデルを${selected.effectiveLabel}に変更します。',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('ai_set_preferred_model');
    final active = await _store.loadApi();
    await _store.saveApi(
      selected.toApiConfig(
        systemPrompt: active.systemPrompt,
        temperature: active.temperature,
      ),
    );
    final settings = await _store.loadModelRuntimeSettings();
    settings['preferred_model'] = selected.model;
    settings['preferred_provider_id'] = selected.providerId;
    settings['preferred_model_group'] = 'mobile';
    await _store.saveModelRuntimeSettings(settings);
    final shouldFavorite =
        _boolArg(args['favorite'] ?? args['star'], fallback: true);
    if (shouldFavorite) {
      await _store.upsertModelFavorite(
        ModelFavoriteConfig.fromMobileProvider(selected),
      );
    }
    return _aiToolOk(
      'ai_set_preferred_model',
      'preferred model set to ${selected.model}',
      {
        'preferred_model': selected.model,
        'provider_id': selected.providerId,
        'label': selected.effectiveLabel,
        'favorite': shouldFavorite,
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiGetThinkingLevel(
    Map<String, dynamic> args,
  ) async {
    final settings = await _store.loadModelRuntimeSettings();
    final level =
        _normalizeThinkingLevelValue(settings['thinking_level']) ?? 'medium';
    return _aiToolOk(
      'ai_get_thinking_level',
      'thinking level $level',
      {
        'thinking_level': level,
        'supported_levels': _mobileThinkingLevels.toList(),
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiSetThinkingLevel(
    Map<String, dynamic> args,
  ) async {
    final normalized = _normalizeThinkingLevelValue(
      args['thinking_level'] ?? args['level'],
    );
    if (normalized == null) {
      return _aiToolError(
        'ai_set_thinking_level',
        'INVALID_THINKING_LEVEL',
        'Supported thinking levels: ${_mobileThinkingLevels.join(', ')}.',
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'ai_set_thinking_level',
      prompt: 'このスマホのthinking levelを$normalizedに変更します。',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('ai_set_thinking_level');
    final settings = await _store.loadModelRuntimeSettings();
    settings['thinking_level'] = normalized;
    await _store.saveModelRuntimeSettings(settings);
    return _aiToolOk(
      'ai_set_thinking_level',
      'thinking level set to $normalized',
      {
        'thinking_level': normalized,
        'supported_levels': _mobileThinkingLevels.toList(),
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiGetEffectiveThinkingLevel(
    Map<String, dynamic> args,
  ) async {
    final requested = _normalizeThinkingLevelValue(
      args['requested_thinking_level'] ?? args['thinking_level'],
    );
    final settings = await _store.loadModelRuntimeSettings();
    final stored = _normalizeThinkingLevelValue(settings['thinking_level']);
    final level = requested ?? stored ?? 'medium';
    return _aiToolOk(
      'ai_get_effective_thinking_level',
      'effective thinking level $level',
      {
        'thinking_level': level,
        'requested_thinking_level': requested,
        'stored_thinking_level': stored,
        'source': requested != null
            ? 'request'
            : stored != null
                ? 'phone_settings'
                : 'default',
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiNormalizeThinkingLevel(
    Map<String, dynamic> args,
  ) async {
    final raw = args['thinking_level'] ?? args['level'];
    final normalized = _normalizeThinkingLevelValue(raw);
    if (normalized == null) {
      return _aiToolError(
        'ai_normalize_thinking_level',
        'INVALID_THINKING_LEVEL',
        'Supported thinking levels: ${_mobileThinkingLevels.join(', ')}.',
      );
    }
    return _aiToolOk(
      'ai_normalize_thinking_level',
      'normalized thinking level $normalized',
      {
        'input': raw,
        'thinking_level': normalized,
        'supported_levels': _mobileThinkingLevels.toList(),
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiValidateModelParams(
    Map<String, dynamic> args,
  ) async {
    final errors = <String>[];
    final warnings = <String>[];
    final provider = await _selectMobileProviderModel(args);
    final requestedModel = _firstText(args, const ['model']);
    if (requestedModel.isNotEmpty && provider == null) {
      errors.add('model is not in the phone-local provider catalog');
    }
    final temperature = args['temperature'];
    if (temperature is num && (temperature < 0 || temperature > 2)) {
      errors.add('temperature must be between 0 and 2');
    }
    final maxTokens = args['max_tokens'] ?? args['maxTokens'];
    if (maxTokens is num && maxTokens <= 0) {
      errors.add('max_tokens must be greater than 0');
    }
    final thinking = args['thinking_level'];
    if (thinking != null && _normalizeThinkingLevelValue(thinking) == null) {
      errors.add('thinking_level is invalid');
    }
    if (provider != null && !provider.isConfigured) {
      warnings.add('provider is catalog-only or missing API key on this phone');
    }
    return _aiToolOk(
      'ai_validate_model_params',
      errors.isEmpty ? 'model params valid' : 'model params invalid',
      {
        'valid': errors.isEmpty,
        'errors': errors,
        'warnings': warnings,
        if (provider != null)
          'model': _mobileModelRecord(provider, null, const []),
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
      ok: errors.isEmpty,
    );
  }

  Future<MobileToolResult> _aiRouteModel(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    final selected = await _selectMobileProviderModel(args) ??
        await _firstConfiguredOrDefaultProvider();
    if (selected == null) {
      return _aiToolError(
        toolName,
        'NO_MODEL_AVAILABLE',
        'No phone-local model catalog entry is available.',
      );
    }
    final active = await _store.loadApi();
    final reason = selected.providerId == active.providerId &&
            selected.model == active.model
        ? 'selected preferred phone-local model'
        : selected.isConfigured
            ? 'selected configured phone-local provider'
            : 'selected catalog fallback; API key may be required';
    return _aiToolOk(
      toolName,
      '${selected.providerId}/${selected.model}',
      {
        'selected_model': selected.model,
        'selected_provider_id': selected.providerId,
        'profile_id': '${selected.providerId}/${selected.model}',
        'reason': reason,
        'configured': selected.isConfigured,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _aiExplainModelChoice(
    Map<String, dynamic> args,
  ) async {
    final selected = await _selectMobileProviderModel(args) ??
        await _firstConfiguredOrDefaultProvider();
    if (selected == null) {
      return _aiToolError(
        'ai_explain_model_choice',
        'NO_MODEL_AVAILABLE',
        'No phone-local model catalog entry is available.',
      );
    }
    final reason = _firstText(args, const ['reason']).isEmpty
        ? 'スマホ内のprovider設定とstar付きモデルから選択しました。'
        : _firstText(args, const ['reason']);
    return _aiToolOk(
      'ai_explain_model_choice',
      'explained ${selected.providerId}/${selected.model}',
      {
        'model': selected.model,
        'provider_id': selected.providerId,
        'profile_id': '${selected.providerId}/${selected.model}',
        'explanation': reason,
        'configured': selected.isConfigured,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<List<MobileProviderConfig>> _loadMergedMobileProviders() async {
    final byId = <String, MobileProviderConfig>{
      for (final provider in defaultspackMobileProviderConfigs)
        provider.providerId: provider,
    };
    for (final stored in await _store.loadProviderConfigs()) {
      final providerId = stored.providerId.trim();
      if (providerId.isEmpty) continue;
      final base = byId[providerId];
      byId[providerId] = base == null
          ? stored
          : base.copyWith(
              displayName: stored.displayName.trim().isEmpty
                  ? base.displayName
                  : stored.displayName,
              label: stored.label,
              apiKey: stored.apiKey,
              baseUrl:
                  stored.baseUrl.trim().isEmpty ? base.baseUrl : stored.baseUrl,
              model: stored.model.trim().isEmpty ? base.model : stored.model,
              openaiCompatible: stored.openaiCompatible,
              local: stored.local,
              catalogOnly: stored.catalogOnly,
              apiCompatibility: stored.apiCompatibility,
            );
    }
    final active = await _store.loadApi();
    if (active.providerId.trim().isNotEmpty) {
      final providerId = active.providerId.trim();
      final base = byId[providerId] ??
          MobileProviderConfig(
            providerId: providerId,
            displayName: providerId,
            label: '',
            apiKey: '',
            baseUrl: active.baseUrl,
            model: active.model,
            openaiCompatible: active.apiCompatibility == 'openai',
            local: active.baseUrl.startsWith('local://') ||
                active.baseUrl.contains('127.0.0.1') ||
                active.baseUrl.contains('localhost'),
            catalogOnly: false,
            apiCompatibility: active.apiCompatibility,
          );
      byId[providerId] = base.copyWith(
        label: active.label.trim().isEmpty ? base.label : active.label,
        apiKey: active.apiKey.trim().isEmpty ? base.apiKey : active.apiKey,
        baseUrl: active.baseUrl.trim().isEmpty ? base.baseUrl : active.baseUrl,
        model: active.model.trim().isEmpty ? base.model : active.model,
        apiCompatibility: active.apiCompatibility,
      );
    }
    final list = byId.values.toList()
      ..sort((a, b) => a.effectiveLabel
          .toLowerCase()
          .compareTo(b.effectiveLabel.toLowerCase()));
    return list;
  }

  Future<MobileProviderConfig> _resolveProviderForArgs(
    Map<String, dynamic> args, {
    required String providerId,
  }) async {
    final providers = await _loadMergedMobileProviders();
    for (final provider in providers) {
      if (provider.providerId == providerId) return provider;
    }
    final baseUrl = _firstText(args, const ['base_url', 'baseUrl']);
    final model = _firstText(args, const ['model']);
    return MobileProviderConfig(
      providerId: providerId,
      displayName: providerId,
      label: _firstText(args, const ['label']),
      apiKey: '',
      baseUrl: baseUrl,
      model: model,
      openaiCompatible: true,
      local: baseUrl.startsWith('local://') ||
          baseUrl.contains('127.0.0.1') ||
          baseUrl.contains('localhost'),
      catalogOnly: baseUrl.isEmpty,
      apiCompatibility: 'openai',
    );
  }

  Future<MobileProviderConfig?> _selectMobileProviderModel(
    Map<String, dynamic> args,
  ) async {
    final providers = await _loadMergedMobileProviders();
    final active = await _store.loadApi();
    var providerId = _providerArg(args);
    var model = _firstText(
      args,
      const ['model', 'preferred_model', 'selected_model'],
    );
    final profile = _firstText(args, const ['profile', 'profile_id']);
    if (profile.isNotEmpty && providerId.isEmpty && model.isEmpty) {
      final separator = profile.indexOf('/');
      if (separator > 0 && separator < profile.length - 1) {
        providerId = profile.substring(0, separator).trim();
        model = profile.substring(separator + 1).trim();
      } else {
        model = profile;
      }
    }
    if (providerId.isEmpty && model.isEmpty) {
      for (final provider in providers) {
        if (provider.providerId == active.providerId &&
            provider.model == active.model) {
          return provider;
        }
      }
      if (active.providerId.trim().isNotEmpty &&
          active.model.trim().isNotEmpty) {
        return MobileProviderConfig(
          providerId: active.providerId,
          displayName: active.label.trim().isEmpty
              ? active.providerId
              : active.label.trim(),
          label: active.label,
          apiKey: active.apiKey,
          baseUrl: active.baseUrl,
          model: active.model,
          openaiCompatible: active.apiCompatibility == 'openai',
          local: active.baseUrl.startsWith('local://') ||
              active.baseUrl.contains('127.0.0.1') ||
              active.baseUrl.contains('localhost'),
          catalogOnly: false,
          apiCompatibility: active.apiCompatibility,
        );
      }
    }
    if (providerId.isNotEmpty) {
      for (final provider in providers) {
        if (provider.providerId != providerId) continue;
        if (model.isEmpty || provider.model == model) {
          return model.isEmpty ? provider : provider.copyWith(model: model);
        }
      }
    }
    if (model.isNotEmpty) {
      for (final provider in providers) {
        if (provider.model == model) return provider;
      }
    }
    return null;
  }

  Future<MobileProviderConfig?> _firstConfiguredOrDefaultProvider() async {
    final providers = await _loadMergedMobileProviders();
    for (final provider in providers) {
      if (provider.isConfigured) return provider;
    }
    for (final provider in providers) {
      if (!provider.catalogOnly && provider.model.trim().isNotEmpty) {
        return provider;
      }
    }
    return providers.isEmpty ? null : providers.first;
  }

  Map<String, dynamic> _mobileModelRecord(
    MobileProviderConfig provider,
    ApiConfig? active,
    List<ModelFavoriteConfig> favorites,
  ) {
    return {
      'id': '${provider.providerId}/${provider.model}',
      'provider_id': provider.providerId,
      'provider_name': provider.displayName,
      'model': provider.model,
      'label': provider.effectiveLabel,
      'configured': provider.isConfigured,
      'catalog_only': provider.catalogOnly,
      'local': provider.local,
      'openai_compatible': provider.openaiCompatible,
      'api_compatibility': provider.apiCompatibility,
      'active': active != null &&
          active.providerId == provider.providerId &&
          active.model == provider.model,
      'favorite': _isFavoriteProviderModel(provider, favorites),
      'execution_location': 'phone',
    };
  }

  Map<String, dynamic> _mobileProviderRecord(
    MobileProviderConfig provider,
    ApiConfig active,
    List<ModelFavoriteConfig> favorites,
  ) {
    return {
      'provider_id': provider.providerId,
      'display_name': provider.displayName,
      'label': provider.effectiveLabel,
      'base_url': provider.baseUrl,
      'default_model': provider.model,
      'configured': provider.isConfigured,
      'catalog_only': provider.catalogOnly,
      'local': provider.local,
      'openai_compatible': provider.openaiCompatible,
      'api_compatibility': provider.apiCompatibility,
      'active': active.providerId == provider.providerId,
      'favorite': _isFavoriteProviderModel(provider, favorites),
      'key_status': _providerKeyStatusRecord(provider),
    };
  }

  Map<String, dynamic> _providerKeyStatusRecord(
    MobileProviderConfig provider,
  ) {
    final key = provider.apiKey.trim();
    return {
      'provider_id': provider.providerId,
      'display_name': provider.displayName,
      'configured': provider.isConfigured,
      'has_api_key': key.isNotEmpty,
      'key_masked': _maskSecret(key),
      'key_length': key.length,
      'local': provider.local,
      'catalog_only': provider.catalogOnly,
      'api_compatibility': provider.apiCompatibility,
    };
  }

  bool _isFavoriteProviderModel(
    MobileProviderConfig provider,
    List<ModelFavoriteConfig> favorites,
  ) {
    return favorites
        .any((favorite) => favorite.matchesMobileProvider(provider));
  }

  MobileToolResult _aiToolOk(
    String toolName,
    String summary,
    Map<String, dynamic> data, {
    bool ok = true,
  }) {
    return MobileToolResult(
      ok: ok,
      summary: summary,
      output: jsonEncode({
        'status': ok ? 'ok' : 'error',
        'data': {
          ...data,
          'tool': toolName,
          'mobile_compatible': true,
          'requires_pc': false,
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  MobileToolResult _aiToolError(
    String toolName,
    String code,
    String message,
  ) {
    return MobileToolResult(
      ok: false,
      summary: message,
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': code,
          'message': message,
          'tool': toolName,
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  String _providerArg(Map<String, dynamic> args) {
    return _firstText(args, const ['provider', 'provider_id', 'providerId'])
        .toLowerCase();
  }

  String? _normalizeThinkingLevelValue(Object? value) {
    final raw = '$value'.trim().toLowerCase();
    if (raw.isEmpty || raw == 'null') return null;
    final normalized = switch (raw) {
      'off' || 'false' || 'disabled' || 'disable' || 'no' => 'none',
      'minimal' || 'min' || 'light' => 'low',
      'normal' || 'default' || 'auto' => 'medium',
      'max' || 'maximum' || 'extra' || 'extra_high' || 'very_high' => 'xhigh',
      _ => raw,
    };
    return _mobileThinkingLevels.contains(normalized) ? normalized : null;
  }

  String _maskSecret(String value) {
    final trimmed = value.trim();
    if (trimmed.isEmpty) return '';
    if (trimmed.length <= 8) return '****';
    return '${trimmed.substring(0, 4)}...${trimmed.substring(trimmed.length - 4)}';
  }

  String _firstText(Map<String, dynamic> args, List<String> keys) {
    for (final key in keys) {
      final value = args[key];
      if (value == null) continue;
      final text = '$value'.trim();
      if (text.isNotEmpty && text != 'null') return text;
    }
    return '';
  }

  Future<MobileToolResult> _promptTool(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    switch (toolName) {
      case 'prompt_validate_template':
        return _promptValidateTemplate(args);
      case 'prompt_render':
        return _promptRender(args);
      case 'prompt_lint_prompt':
        return _promptLintPrompt(args);
      case 'prompt_compact_prompt':
        return _promptCompactPrompt(args);
      case 'prompt_system_get':
        return _promptSystemGet(args);
      case 'prompt_system_set':
        return _promptSystemSet(args);
      case 'prompt_list':
        return _promptList(args);
      case 'prompt_create':
        return _promptCreate(args);
      case 'prompt_update':
        return _promptUpdate(args);
      case 'prompt_delete':
        return _promptDelete(args);
      case 'prompt_active':
      case 'prompt_load_effective':
      case 'prompt_resolve_for_conversation':
        return _promptEffective(toolName, args);
      case 'prompt_preview_toggle':
        return _promptPreviewToggle(args);
      case 'prompt_test':
        return _promptTest(args);
      default:
        return _promptToolError(
          toolName,
          'UNSUPPORTED_PROMPT_TOOL',
          '$toolName is not a phone prompt tool.',
        );
    }
  }

  Future<MobileToolResult> _promptValidateTemplate(
    Map<String, dynamic> args,
  ) async {
    final template = _promptTextArg(args);
    final variables = _promptVariables(args);
    final found = _promptTemplateVariables(template).toList()..sort();
    final issues = _promptTemplateIssues(template, variables, strict: false);
    return _promptToolOk(
      'prompt_validate_template',
      issues.isEmpty ? 'prompt template valid' : '${issues.length} issues',
      {
        'valid': issues.isEmpty,
        'variables': found,
        'provided_variables': variables.keys.toList()..sort(),
        'issues': issues,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
      ok: issues.isEmpty,
    );
  }

  Future<MobileToolResult> _promptRender(Map<String, dynamic> args) async {
    final template = _promptTextArg(args);
    final variables = _promptVariables(args);
    final strict = _boolArg(args['strict'], fallback: false);
    final rendered = _renderPromptTemplate(template, variables);
    final missing = _missingPromptVariables(template, variables).toList()
      ..sort();
    if (strict && missing.isNotEmpty) {
      return _promptToolError(
        'prompt_render',
        'MISSING_TEMPLATE_VARIABLES',
        'Missing prompt variables: ${missing.join(', ')}.',
      );
    }
    return _promptToolOk(
      'prompt_render',
      'rendered prompt ${rendered.length} chars',
      {
        'rendered': rendered,
        'missing_variables': missing,
        'variables': _promptTemplateVariables(template).toList()..sort(),
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptLintPrompt(Map<String, dynamic> args) async {
    final prompt = _promptTextArg(args);
    final maxChars = _boundedInt(args['max_chars'], 12000, 1000, 200000);
    final issues = _promptLintIssues(prompt, maxChars: maxChars);
    return _promptToolOk(
      'prompt_lint_prompt',
      issues.isEmpty ? 'prompt lint passed' : '${issues.length} lint issues',
      {
        'valid': issues.isEmpty,
        'issues': issues,
        'char_count': prompt.length,
        'max_chars': maxChars,
        'variables': _promptTemplateVariables(prompt).toList()..sort(),
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
      ok: issues.isEmpty,
    );
  }

  Future<MobileToolResult> _promptCompactPrompt(
    Map<String, dynamic> args,
  ) async {
    final prompt = _promptTextArg(args);
    final maxChars = _boundedInt(args['max_chars'], 4000, 200, 100000);
    final compacted = _compactPromptText(prompt, maxChars: maxChars);
    return _promptToolOk(
      'prompt_compact_prompt',
      'compacted prompt ${prompt.length} -> ${compacted.length} chars',
      {
        'prompt': compacted,
        'original_char_count': prompt.length,
        'char_count': compacted.length,
        'max_chars': maxChars,
        'changed': compacted != prompt,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptSystemGet(Map<String, dynamic> args) async {
    final config = await _store.loadApi();
    return _promptToolOk(
      'prompt_system_get',
      config.systemPrompt.trim().isEmpty
          ? 'no phone system prompt'
          : 'phone system prompt ${config.systemPrompt.length} chars',
      {
        'system_prompt': config.systemPrompt,
        'char_count': config.systemPrompt.length,
        'provider_id': config.providerId,
        'model': config.model,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptSystemSet(Map<String, dynamic> args) async {
    final systemPrompt =
        _firstText(args, const ['system_prompt', 'prompt', 'content', 'text']);
    final approved = await _requestMobileApproval(
      toolName: 'prompt_system_set',
      prompt: 'このスマホのsystem promptを更新します。',
      arguments: {
        ...args,
        'system_prompt_preview': _clampText(systemPrompt, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('prompt_system_set');
    final config = await _store.loadApi();
    await _store.saveApi(config.copyWith(systemPrompt: systemPrompt));
    return _promptToolOk(
      'prompt_system_set',
      'phone system prompt set',
      {
        'system_prompt': systemPrompt,
        'char_count': systemPrompt.length,
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptList(Map<String, dynamic> args) async {
    final includeSystem = _boolArg(args['include_system'], fallback: true);
    final enabledOnly = _boolArg(args['enabled_only'], fallback: false);
    final records = await _store.loadPromptRecords();
    final config = await _store.loadApi();
    final prompts = <Map<String, dynamic>>[
      if (includeSystem)
        {
          'id': 'system',
          'title': 'Phone system prompt',
          'content': config.systemPrompt,
          'enabled': config.systemPrompt.trim().isNotEmpty,
          'source': 'phone-system',
          'char_count': config.systemPrompt.length,
        },
      for (final record in records)
        if (!enabledOnly || record['enabled'] != false)
          _normalizePromptRecord(record),
    ];
    return _promptToolOk(
      'prompt_list',
      '${prompts.length} phone prompts',
      {
        'prompts': prompts,
        'count': prompts.length,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptCreate(Map<String, dynamic> args) async {
    final content = _promptTextArg(args);
    if (content.trim().isEmpty) {
      return _promptToolError(
        'prompt_create',
        'MISSING_PROMPT_CONTENT',
        "'content' or 'prompt' is required.",
      );
    }
    final title = _firstText(args, const ['title', 'name']).isEmpty
        ? 'Phone prompt'
        : _firstText(args, const ['title', 'name']);
    final id = _firstText(args, const ['id', 'prompt_id']).isEmpty
        ? _slugifyPhoneArtifactName(title, fallback: _nextToolId('prompt'))
        : _slugifyPhoneArtifactName(
            _firstText(args, const ['id', 'prompt_id']),
            fallback: _nextToolId('prompt'),
          );
    final now = DateTime.now().toUtc().toIso8601String();
    final record = {
      'id': id,
      'title': title,
      'content': content,
      'enabled': _boolArg(args['enabled'], fallback: true),
      'source': 'phone',
      'created_at': now,
      'updated_at': now,
    };
    final approved = await _requestMobileApproval(
      toolName: 'prompt_create',
      prompt: 'このスマホにphone-local promptを作成します。対象: $title',
      arguments: {
        ...args,
        'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('prompt_create');
    await _store.upsertPromptRecord(record);
    return _promptToolOk(
      'prompt_create',
      'created phone prompt $id',
      {
        'prompt': _normalizePromptRecord(record),
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptUpdate(Map<String, dynamic> args) async {
    final id = _firstText(args, const ['id', 'prompt_id']);
    if (id.isEmpty) {
      return _promptToolError(
        'prompt_update',
        'MISSING_PROMPT_ID',
        "'id' or 'prompt_id' is required.",
      );
    }
    final records = await _store.loadPromptRecords();
    final index =
        records.indexWhere((record) => '${record['id'] ?? ''}'.trim() == id);
    if (index < 0) {
      return _promptToolError(
        'prompt_update',
        'PROMPT_NOT_FOUND',
        'Phone-local prompt not found: $id.',
      );
    }
    final existing = Map<String, dynamic>.from(records[index]);
    final content = _firstText(args, const ['content', 'prompt', 'text']);
    final title = _firstText(args, const ['title', 'name']);
    final updated = {
      ...existing,
      if (title.isNotEmpty) 'title': title,
      if (content.isNotEmpty) 'content': content,
      if (args.containsKey('enabled'))
        'enabled': _boolArg(args['enabled'], fallback: true),
      'updated_at': DateTime.now().toUtc().toIso8601String(),
    };
    final approved = await _requestMobileApproval(
      toolName: 'prompt_update',
      prompt: 'このスマホのphone-local promptを更新します。対象: $id',
      arguments: {
        ...args,
        if (content.isNotEmpty) 'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('prompt_update');
    records[index] = updated;
    await _store.savePromptRecords(records);
    return _promptToolOk(
      'prompt_update',
      'updated phone prompt $id',
      {
        'prompt': _normalizePromptRecord(updated),
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptDelete(Map<String, dynamic> args) async {
    final id = _firstText(args, const ['id', 'prompt_id']);
    if (id.isEmpty) {
      return _promptToolError(
        'prompt_delete',
        'MISSING_PROMPT_ID',
        "'id' or 'prompt_id' is required.",
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'prompt_delete',
      prompt: 'このスマホのphone-local promptを削除します。対象: $id',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('prompt_delete');
    await _store.deletePromptRecord(id);
    return _promptToolOk(
      'prompt_delete',
      'deleted phone prompt $id',
      {
        'deleted': id,
        'requires_mobile_approval': true,
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptEffective(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    final effective = await _effectivePromptData();
    return _promptToolOk(
      toolName,
      'effective phone prompt ${effective['char_count']} chars',
      {
        ...effective,
        'conversation_id':
            _firstText(args, const ['conversation_id', 'chat_id']),
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptPreviewToggle(
    Map<String, dynamic> args,
  ) async {
    final id = _firstText(args, const ['id', 'prompt_id']);
    final enabled = _boolArg(args['enabled'], fallback: true);
    final effective = await _effectivePromptData(
      previewPromptId: id,
      previewEnabled: enabled,
    );
    return _promptToolOk(
      'prompt_preview_toggle',
      'preview ${enabled ? 'enable' : 'disable'} $id',
      {
        ...effective,
        'preview': {'id': id, 'enabled': enabled, 'saved': false},
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
    );
  }

  Future<MobileToolResult> _promptTest(Map<String, dynamic> args) async {
    final template = _promptTextArg(args);
    final variables = _promptVariables(args);
    final issues = _promptTemplateIssues(template, variables, strict: false);
    final rendered = _renderPromptTemplate(template, variables);
    return _promptToolOk(
      'prompt_test',
      issues.isEmpty ? 'prompt test passed' : '${issues.length} prompt issues',
      {
        'valid': issues.isEmpty,
        'issues': issues,
        'rendered': rendered,
        'variables': _promptTemplateVariables(template).toList()..sort(),
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
      },
      ok: issues.isEmpty,
    );
  }

  Future<Map<String, dynamic>> _effectivePromptData({
    String previewPromptId = '',
    bool? previewEnabled,
  }) async {
    final config = await _store.loadApi();
    final records = await _store.loadPromptRecords();
    final segments = <Map<String, dynamic>>[];
    if (config.systemPrompt.trim().isNotEmpty) {
      segments.add({
        'id': 'system',
        'title': 'Phone system prompt',
        'content': config.systemPrompt,
        'source': 'phone-system',
        'enabled': true,
      });
    }
    for (final raw in records) {
      final record = _normalizePromptRecord(raw);
      final id = '${record['id'] ?? ''}'.trim();
      var enabled = record['enabled'] != false;
      if (previewPromptId.isNotEmpty && id == previewPromptId) {
        enabled = previewEnabled ?? enabled;
      }
      if (!enabled) continue;
      segments.add({...record, 'enabled': enabled});
    }
    final effectivePrompt = segments
        .map((segment) => '${segment['content'] ?? ''}'.trim())
        .where((content) => content.isNotEmpty)
        .join('\n\n');
    return {
      'segments': segments,
      'segment_count': segments.length,
      'effective_prompt': effectivePrompt,
      'char_count': effectivePrompt.length,
      'source': 'phone',
      'agent_template': _agentTemplateRecord(),
    };
  }

  Map<String, dynamic> _normalizePromptRecord(Map<String, dynamic> record) {
    final content = '${record['content'] ?? record['prompt'] ?? ''}';
    final id = '${record['id'] ?? ''}'.trim();
    return {
      'id': id,
      'title': '${record['title'] ?? record['name'] ?? id}'.trim(),
      'content': content,
      'enabled': record['enabled'] is bool ? record['enabled'] : true,
      'source': '${record['source'] ?? 'phone'}',
      'char_count': content.length,
      if (record['created_at'] != null) 'created_at': record['created_at'],
      if (record['updated_at'] != null) 'updated_at': record['updated_at'],
    };
  }

  String _promptTextArg(Map<String, dynamic> args) {
    return _firstText(
      args,
      const ['template', 'prompt', 'content', 'text', 'system_prompt'],
    );
  }

  Map<String, String> _promptVariables(Map<String, dynamic> args) {
    final raw = args['variables'] ?? args['vars'];
    if (raw is! Map) return {};
    return {
      for (final entry in raw.entries)
        '${entry.key}'.trim(): '${entry.value ?? ''}'
    }..removeWhere((key, value) => key.isEmpty);
  }

  Set<String> _promptTemplateVariables(String template) {
    final variables = <String>{};
    for (final match in RegExp(
      r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\}\}',
    ).allMatches(template)) {
      variables.add(match.group(1)!);
    }
    return variables;
  }

  Set<String> _missingPromptVariables(
    String template,
    Map<String, String> variables,
  ) {
    final found = _promptTemplateVariables(template);
    return found.where((name) => !variables.containsKey(name)).toSet();
  }

  List<Map<String, dynamic>> _promptTemplateIssues(
    String template,
    Map<String, String> variables, {
    required bool strict,
  }) {
    final issues = <Map<String, dynamic>>[];
    final openCount = RegExp(r'\{\{').allMatches(template).length;
    final closeCount = RegExp(r'\}\}').allMatches(template).length;
    if (openCount != closeCount) {
      issues.add({
        'code': 'UNBALANCED_MUSTACHE',
        'message': 'Template has unbalanced {{ }} markers.',
      });
    }
    final invalid = RegExp(r'\{\{([^}]*)\}\}').allMatches(template).where(
      (match) {
        final name = (match.group(1) ?? '').trim();
        return !RegExp(r'^[a-zA-Z_][a-zA-Z0-9_.-]*$').hasMatch(name);
      },
    );
    for (final match in invalid) {
      issues.add({
        'code': 'INVALID_VARIABLE_NAME',
        'message': 'Invalid variable name: ${match.group(1)}',
      });
    }
    final missing = _missingPromptVariables(template, variables).toList()
      ..sort();
    if (strict && missing.isNotEmpty) {
      issues.add({
        'code': 'MISSING_VARIABLES',
        'message': 'Missing variables: ${missing.join(', ')}',
        'variables': missing,
      });
    }
    return issues;
  }

  String _renderPromptTemplate(
    String template,
    Map<String, String> variables,
  ) {
    return template.replaceAllMapped(
      RegExp(r'\{\{\s*([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\}\}'),
      (match) {
        final name = match.group(1)!;
        return variables.containsKey(name) ? variables[name]! : match.group(0)!;
      },
    );
  }

  List<Map<String, dynamic>> _promptLintIssues(
    String prompt, {
    required int maxChars,
  }) {
    final issues = <Map<String, dynamic>>[];
    if (prompt.length > maxChars) {
      issues.add({
        'code': 'PROMPT_TOO_LONG',
        'message': 'Prompt exceeds $maxChars chars.',
        'char_count': prompt.length,
      });
    }
    final lines = const LineSplitter()
        .convert(prompt)
        .map((line) => line.trim())
        .where((line) => line.isNotEmpty)
        .toList();
    final seen = <String, int>{};
    for (final line in lines) {
      seen[line] = (seen[line] ?? 0) + 1;
    }
    final repeated = seen.entries
        .where((entry) => entry.value >= 3 && entry.key.length > 12)
        .map((entry) => entry.key)
        .take(5)
        .toList();
    if (repeated.isNotEmpty) {
      issues.add({
        'code': 'REPEATED_LINES',
        'message': 'Prompt contains repeated lines.',
        'examples': repeated,
      });
    }
    final unresolved = _promptTemplateVariables(prompt).toList()..sort();
    if (unresolved.isNotEmpty) {
      issues.add({
        'code': 'UNRESOLVED_TEMPLATE_VARIABLES',
        'message': 'Prompt contains unresolved template variables.',
        'variables': unresolved,
      });
    }
    return issues;
  }

  String _compactPromptText(String prompt, {required int maxChars}) {
    if (prompt.length <= maxChars) return prompt;
    if (maxChars <= 40) return prompt.substring(0, maxChars);
    const marker = '\n\n[...compact...]\n\n';
    final room = maxChars - marker.length;
    final head = (room * 0.65).floor();
    final tail = room - head;
    return prompt.substring(0, head).trimRight() +
        marker +
        prompt.substring(prompt.length - tail).trimLeft();
  }

  MobileToolResult _promptToolOk(
    String toolName,
    String summary,
    Map<String, dynamic> data, {
    bool ok = true,
  }) {
    return MobileToolResult(
      ok: ok,
      summary: summary,
      output: jsonEncode({
        'status': ok ? 'ok' : 'error',
        'data': {
          ...data,
          'tool': toolName,
          'mobile_compatible': true,
          'requires_pc': false,
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  MobileToolResult _promptToolError(
    String toolName,
    String code,
    String message,
  ) {
    return MobileToolResult(
      ok: false,
      summary: message,
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': code,
          'message': message,
          'tool': toolName,
          'execution_location': 'phone',
          'runtime_layers': _flutterRuntimeLayers,
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  Future<MobileToolResult> _memoryTool(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    switch (toolName) {
      case 'memory_store':
        return _memoryStore(args);
      case 'memory_list':
        return _memoryList(args);
      case 'memory_recall':
        return _memoryRecall(args);
      case 'memory_update':
        return _memoryUpdate(args);
      case 'memory_delete':
        return _memoryDelete(args);
      case 'memory_compact':
        return _memoryCompact(args);
      case 'memory_project_context':
      case 'memory_resolve_for_agent':
        return _memoryContext(toolName, args);
      case 'memory_memo_folders':
        return _memoryMemoFolders(args);
      case 'memory_memo_notes':
        return _memoryMemoNotes(args);
      case 'memory_memo':
        return _memoryMemo(args);
      default:
        return _memoryToolError(
          toolName,
          'UNSUPPORTED_MEMORY_TOOL',
          '$toolName is not a phone memory tool.',
        );
    }
  }

  Future<MobileToolResult> _memoryStore(Map<String, dynamic> args) async {
    final content = _memoryContentArg(args);
    if (content.isEmpty) {
      return _memoryToolError(
        'memory_store',
        'MISSING_MEMORY_CONTENT',
        "'content', 'text', or 'summary' is required.",
      );
    }
    final now = DateTime.now().toUtc().toIso8601String();
    final title = _firstText(args, const ['title', 'label']).isEmpty
        ? _clampText(content.replaceAll(RegExp(r'\s+'), ' '), 48)
        : _firstText(args, const ['title', 'label']);
    final id = _firstText(args, const ['id', 'memory_id']).isEmpty
        ? _nextToolId('mem')
        : _slugifyPhoneArtifactName(
            _firstText(args, const ['id', 'memory_id']),
            fallback: _nextToolId('mem'),
          );
    final record = {
      'id': id,
      'title': title,
      'content': content,
      'tags': _memoryTags(args['tags'] ?? args['tag']),
      'importance': _memoryImportance(args['importance']),
      'source': 'phone',
      'created_at': now,
      'updated_at': now,
    };
    final approved = await _requestMobileApproval(
      toolName: 'memory_store',
      prompt: 'このスマホにphone-local memoryを保存します。対象: $title',
      arguments: {
        ...args,
        'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('memory_store');
    await _store.upsertMemoryRecord(record);
    return _memoryToolOk(
      'memory_store',
      'stored phone memory $id',
      {
        'memory': _normalizeMemoryRecord(record),
        'requires_mobile_approval': true,
      },
    );
  }

  Future<MobileToolResult> _memoryList(Map<String, dynamic> args) async {
    final records = await _filteredMemoryRecords(args);
    return _memoryToolOk(
      'memory_list',
      '${records.length} phone memories',
      {
        'memories': records,
        'count': records.length,
      },
    );
  }

  Future<MobileToolResult> _memoryRecall(Map<String, dynamic> args) async {
    final records = await _rankedMemoryRecords(args);
    return _memoryToolOk(
      'memory_recall',
      '${records.length} recalled phone memories',
      {
        'memories': records,
        'count': records.length,
        'query': _firstText(args, const ['query', 'text', 'prompt']),
      },
    );
  }

  Future<MobileToolResult> _memoryUpdate(Map<String, dynamic> args) async {
    final id = _memoryIdArg(args);
    if (id.isEmpty) {
      return _memoryToolError(
        'memory_update',
        'MISSING_MEMORY_ID',
        "'id' or 'memory_id' is required.",
      );
    }
    final records = await _store.loadMemoryRecords();
    final index =
        records.indexWhere((record) => '${record['id'] ?? ''}'.trim() == id);
    if (index < 0) {
      return _memoryToolError(
        'memory_update',
        'MEMORY_NOT_FOUND',
        'Phone-local memory not found: $id.',
      );
    }
    final existing = Map<String, dynamic>.from(records[index]);
    final content = _memoryContentArg(args);
    final title = _firstText(args, const ['title', 'label']);
    final updated = {
      ...existing,
      if (title.isNotEmpty) 'title': title,
      if (content.isNotEmpty) 'content': content,
      if (args.containsKey('tags') || args.containsKey('tag'))
        'tags': _memoryTags(args['tags'] ?? args['tag']),
      if (args.containsKey('importance'))
        'importance': _memoryImportance(args['importance']),
      'updated_at': DateTime.now().toUtc().toIso8601String(),
    };
    final approved = await _requestMobileApproval(
      toolName: 'memory_update',
      prompt: 'このスマホのphone-local memoryを更新します。対象: $id',
      arguments: {
        ...args,
        if (content.isNotEmpty) 'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('memory_update');
    records[index] = updated;
    await _store.saveMemoryRecords(records);
    return _memoryToolOk(
      'memory_update',
      'updated phone memory $id',
      {
        'memory': _normalizeMemoryRecord(updated),
        'requires_mobile_approval': true,
      },
    );
  }

  Future<MobileToolResult> _memoryDelete(Map<String, dynamic> args) async {
    final id = _memoryIdArg(args);
    if (id.isEmpty) {
      return _memoryToolError(
        'memory_delete',
        'MISSING_MEMORY_ID',
        "'id' or 'memory_id' is required.",
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'memory_delete',
      prompt: 'このスマホのphone-local memoryを削除します。対象: $id',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('memory_delete');
    await _store.deleteMemoryRecord(id);
    return _memoryToolOk(
      'memory_delete',
      'deleted phone memory $id',
      {
        'deleted': id,
        'requires_mobile_approval': true,
      },
    );
  }

  Future<MobileToolResult> _memoryCompact(Map<String, dynamic> args) async {
    final maxChars = _boundedInt(args['max_chars'], 3000, 200, 100000);
    final records = await _rankedMemoryRecords(args);
    final summary = _compactPromptText(
      records
          .map((record) => '- ${record['title']}: ${record['content']}')
          .join('\n'),
      maxChars: maxChars,
    );
    return _memoryToolOk(
      'memory_compact',
      'compacted ${records.length} phone memories',
      {
        'summary': summary,
        'memories': records,
        'count': records.length,
        'char_count': summary.length,
        'max_chars': maxChars,
      },
    );
  }

  Future<MobileToolResult> _memoryContext(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    final records = await _rankedMemoryRecords(args);
    final context = records
        .map((record) => '- ${record['title']}: ${record['content']}')
        .join('\n');
    return _memoryToolOk(
      toolName,
      '${records.length} phone memory context records',
      {
        'context': context,
        'memories': records,
        'count': records.length,
        'project_id': _firstText(args, const ['project_id', 'project']),
        'agent_id': _firstText(args, const ['agent_id', 'agent']),
        'agent_template': _agentTemplateRecord(),
      },
    );
  }

  Future<MobileToolResult> _memoryMemo(Map<String, dynamic> args) {
    final target =
        _firstText(args, const ['target', 'kind', 'type']).toLowerCase();
    if (target.contains('folder')) return _memoryMemoFolders(args);
    return _memoryMemoNotes(args);
  }

  Future<MobileToolResult> _memoryMemoFolders(Map<String, dynamic> args) async {
    final action = _memoAction(args);
    if (action == 'list' || action == 'show') {
      final folders =
          (await _store.loadMemoFolders()).map(_normalizeMemoFolder).toList();
      return _memoryToolOk(
        'memory_memo_folders',
        '${folders.length} phone memo folders',
        {'folders': folders, 'count': folders.length},
      );
    }
    if (action == 'create' || action == 'add') {
      final title = _firstText(args, const ['title', 'name']).isEmpty
          ? 'Memo folder'
          : _firstText(args, const ['title', 'name']);
      final id = _memoIdArg(args).isEmpty
          ? _slugifyPhoneArtifactName(title, fallback: _nextToolId('folder'))
          : _memoIdArg(args);
      final now = DateTime.now().toUtc().toIso8601String();
      final record = {
        'id': id,
        'title': title,
        'created_at': now,
        'updated_at': now,
        'source': 'phone',
      };
      final approved = await _requestMobileApproval(
        toolName: 'memory_memo_folders',
        prompt: 'このスマホにmemo folderを作成します。対象: $title',
        arguments: args,
        risk: 'medium',
      );
      if (!approved) return _mobileApprovalRequired('memory_memo_folders');
      await _store.upsertMemoFolder(record);
      return _memoryToolOk(
        'memory_memo_folders',
        'created phone memo folder $id',
        {
          'folder': _normalizeMemoFolder(record),
          'requires_mobile_approval': true,
        },
      );
    }
    if (action == 'update' || action == 'edit') {
      final id = _memoIdArg(args);
      if (id.isEmpty) {
        return _memoryToolError(
          'memory_memo_folders',
          'MISSING_FOLDER_ID',
          "'id' or 'folder_id' is required.",
        );
      }
      final folders = await _store.loadMemoFolders();
      final index =
          folders.indexWhere((folder) => '${folder['id'] ?? ''}' == id);
      if (index < 0) {
        return _memoryToolError(
          'memory_memo_folders',
          'FOLDER_NOT_FOUND',
          'Phone memo folder not found: $id.',
        );
      }
      final title = _firstText(args, const ['title', 'name']);
      final updated = {
        ...folders[index],
        if (title.isNotEmpty) 'title': title,
        'updated_at': DateTime.now().toUtc().toIso8601String(),
      };
      final approved = await _requestMobileApproval(
        toolName: 'memory_memo_folders',
        prompt: 'このスマホのmemo folderを更新します。対象: $id',
        arguments: args,
        risk: 'medium',
      );
      if (!approved) return _mobileApprovalRequired('memory_memo_folders');
      folders[index] = updated;
      await _store.saveMemoFolders(folders);
      return _memoryToolOk(
        'memory_memo_folders',
        'updated phone memo folder $id',
        {
          'folder': _normalizeMemoFolder(updated),
          'requires_mobile_approval': true,
        },
      );
    }
    if (action == 'delete' || action == 'remove') {
      final id = _memoIdArg(args);
      if (id.isEmpty) {
        return _memoryToolError(
          'memory_memo_folders',
          'MISSING_FOLDER_ID',
          "'id' or 'folder_id' is required.",
        );
      }
      final approved = await _requestMobileApproval(
        toolName: 'memory_memo_folders',
        prompt: 'このスマホのmemo folderと配下notesを削除します。対象: $id',
        arguments: args,
        risk: 'medium',
      );
      if (!approved) return _mobileApprovalRequired('memory_memo_folders');
      await _store.deleteMemoFolder(id);
      final notes = await _store.loadMemoNotes();
      await _store.saveMemoNotes(
        notes.where((note) => '${note['folder_id'] ?? ''}' != id).toList(),
      );
      return _memoryToolOk(
        'memory_memo_folders',
        'deleted phone memo folder $id',
        {'deleted': id, 'requires_mobile_approval': true},
      );
    }
    return _memoryToolError(
      'memory_memo_folders',
      'UNSUPPORTED_MEMO_ACTION',
      'Unsupported memo folder action: $action.',
    );
  }

  Future<MobileToolResult> _memoryMemoNotes(Map<String, dynamic> args) async {
    final action = _memoAction(args);
    if (action == 'list' || action == 'show') {
      final folderId = _firstText(args, const ['folder_id', 'folder']);
      final query = _firstText(args, const ['query', 'search']);
      var notes =
          (await _store.loadMemoNotes()).map(_normalizeMemoNote).toList();
      if (folderId.isNotEmpty) {
        notes = notes.where((note) => note['folder_id'] == folderId).toList();
      }
      if (query.isNotEmpty) {
        notes = notes
            .where((note) =>
                _memorySearchScore(
                    '${note['title']} ${note['content']}', query) >
                0)
            .toList();
      }
      return _memoryToolOk(
        'memory_memo_notes',
        '${notes.length} phone memo notes',
        {'notes': notes, 'count': notes.length},
      );
    }
    if (action == 'create' || action == 'add') {
      final content = _firstText(args, const ['content', 'text', 'body']);
      if (content.isEmpty) {
        return _memoryToolError(
          'memory_memo_notes',
          'MISSING_NOTE_CONTENT',
          "'content' or 'text' is required.",
        );
      }
      final title = _firstText(args, const ['title', 'name']).isEmpty
          ? _clampText(content.replaceAll(RegExp(r'\s+'), ' '), 48)
          : _firstText(args, const ['title', 'name']);
      final id =
          _memoIdArg(args).isEmpty ? _nextToolId('note') : _memoIdArg(args);
      final now = DateTime.now().toUtc().toIso8601String();
      final record = {
        'id': id,
        'folder_id': _firstText(args, const ['folder_id', 'folder']),
        'title': title,
        'content': content,
        'created_at': now,
        'updated_at': now,
        'source': 'phone',
      };
      final approved = await _requestMobileApproval(
        toolName: 'memory_memo_notes',
        prompt: 'このスマホにmemo noteを作成します。対象: $title',
        arguments: {
          ...args,
          'content_preview': _clampText(content, 160),
        },
        risk: 'medium',
      );
      if (!approved) return _mobileApprovalRequired('memory_memo_notes');
      await _store.upsertMemoNote(record);
      return _memoryToolOk(
        'memory_memo_notes',
        'created phone memo note $id',
        {'note': _normalizeMemoNote(record), 'requires_mobile_approval': true},
      );
    }
    if (action == 'update' || action == 'edit') {
      final id = _memoIdArg(args);
      if (id.isEmpty) {
        return _memoryToolError(
          'memory_memo_notes',
          'MISSING_NOTE_ID',
          "'id' or 'note_id' is required.",
        );
      }
      final notes = await _store.loadMemoNotes();
      final index = notes.indexWhere((note) => '${note['id'] ?? ''}' == id);
      if (index < 0) {
        return _memoryToolError(
          'memory_memo_notes',
          'NOTE_NOT_FOUND',
          'Phone memo note not found: $id.',
        );
      }
      final content = _firstText(args, const ['content', 'text', 'body']);
      final title = _firstText(args, const ['title', 'name']);
      final folderId = _firstText(args, const ['folder_id', 'folder']);
      final updated = {
        ...notes[index],
        if (title.isNotEmpty) 'title': title,
        if (content.isNotEmpty) 'content': content,
        if (folderId.isNotEmpty) 'folder_id': folderId,
        'updated_at': DateTime.now().toUtc().toIso8601String(),
      };
      final approved = await _requestMobileApproval(
        toolName: 'memory_memo_notes',
        prompt: 'このスマホのmemo noteを更新します。対象: $id',
        arguments: {
          ...args,
          if (content.isNotEmpty) 'content_preview': _clampText(content, 160),
        },
        risk: 'medium',
      );
      if (!approved) return _mobileApprovalRequired('memory_memo_notes');
      notes[index] = updated;
      await _store.saveMemoNotes(notes);
      return _memoryToolOk(
        'memory_memo_notes',
        'updated phone memo note $id',
        {'note': _normalizeMemoNote(updated), 'requires_mobile_approval': true},
      );
    }
    if (action == 'delete' || action == 'remove') {
      final id = _memoIdArg(args);
      if (id.isEmpty) {
        return _memoryToolError(
          'memory_memo_notes',
          'MISSING_NOTE_ID',
          "'id' or 'note_id' is required.",
        );
      }
      final approved = await _requestMobileApproval(
        toolName: 'memory_memo_notes',
        prompt: 'このスマホのmemo noteを削除します。対象: $id',
        arguments: args,
        risk: 'medium',
      );
      if (!approved) return _mobileApprovalRequired('memory_memo_notes');
      await _store.deleteMemoNote(id);
      return _memoryToolOk(
        'memory_memo_notes',
        'deleted phone memo note $id',
        {'deleted': id, 'requires_mobile_approval': true},
      );
    }
    return _memoryToolError(
      'memory_memo_notes',
      'UNSUPPORTED_MEMO_ACTION',
      'Unsupported memo note action: $action.',
    );
  }

  Future<List<Map<String, dynamic>>> _filteredMemoryRecords(
    Map<String, dynamic> args,
  ) async {
    final query = _firstText(args, const ['query', 'search', 'text']);
    final tag = _firstText(args, const ['tag']).toLowerCase();
    final limit = _boundedInt(args['limit'], 50, 1, 500);
    var records =
        (await _store.loadMemoryRecords()).map(_normalizeMemoryRecord).toList();
    if (tag.isNotEmpty) {
      records = records
          .where((record) => (record['tags'] as List? ?? const [])
              .map((item) => '$item'.toLowerCase())
              .contains(tag))
          .toList();
    }
    if (query.isNotEmpty) {
      records = records
          .where((record) => _memoryRecordScore(record, query) > 0)
          .toList();
    }
    records.sort((a, b) {
      final importance = ((b['importance'] as num?)?.toDouble() ?? 0)
          .compareTo((a['importance'] as num?)?.toDouble() ?? 0);
      if (importance != 0) return importance;
      return '${b['updated_at'] ?? ''}'.compareTo('${a['updated_at'] ?? ''}');
    });
    return records.take(limit).toList();
  }

  Future<List<Map<String, dynamic>>> _rankedMemoryRecords(
    Map<String, dynamic> args,
  ) async {
    final query = _firstText(args, const ['query', 'search', 'text', 'prompt']);
    final limit = _boundedInt(args['limit'], 8, 1, 100);
    final records =
        (await _store.loadMemoryRecords()).map(_normalizeMemoryRecord).toList();
    final scored = [
      for (final record in records)
        {
          ...record,
          'score': query.isEmpty ? 1.0 : _memoryRecordScore(record, query),
        }
    ]..sort((a, b) {
        final score = ((b['score'] as num?)?.toDouble() ?? 0)
            .compareTo((a['score'] as num?)?.toDouble() ?? 0);
        if (score != 0) return score;
        return '${b['updated_at'] ?? ''}'.compareTo('${a['updated_at'] ?? ''}');
      });
    return scored
        .where(
            (record) => query.isEmpty || ((record['score'] as num?) ?? 0) > 0)
        .take(limit)
        .toList();
  }

  Map<String, dynamic> _normalizeMemoryRecord(Map<String, dynamic> record) {
    final content = '${record['content'] ?? record['text'] ?? ''}';
    final id = '${record['id'] ?? ''}'.trim();
    return {
      'id': id,
      'title': '${record['title'] ?? record['label'] ?? id}'.trim(),
      'content': content,
      'tags': _memoryTags(record['tags'] ?? record['tag']),
      'importance': _memoryImportance(record['importance']),
      'source': '${record['source'] ?? 'phone'}',
      'char_count': content.length,
      if (record['created_at'] != null) 'created_at': record['created_at'],
      if (record['updated_at'] != null) 'updated_at': record['updated_at'],
    };
  }

  Map<String, dynamic> _normalizeMemoFolder(Map<String, dynamic> record) {
    final id = '${record['id'] ?? record['folder_id'] ?? ''}'.trim();
    return {
      'id': id,
      'title': '${record['title'] ?? record['name'] ?? id}'.trim(),
      'source': '${record['source'] ?? 'phone'}',
      if (record['created_at'] != null) 'created_at': record['created_at'],
      if (record['updated_at'] != null) 'updated_at': record['updated_at'],
    };
  }

  Map<String, dynamic> _normalizeMemoNote(Map<String, dynamic> record) {
    final content = '${record['content'] ?? record['text'] ?? ''}';
    final id = '${record['id'] ?? record['note_id'] ?? ''}'.trim();
    return {
      'id': id,
      'folder_id': '${record['folder_id'] ?? record['folder'] ?? ''}'.trim(),
      'title': '${record['title'] ?? record['name'] ?? id}'.trim(),
      'content': content,
      'source': '${record['source'] ?? 'phone'}',
      'char_count': content.length,
      if (record['created_at'] != null) 'created_at': record['created_at'],
      if (record['updated_at'] != null) 'updated_at': record['updated_at'],
    };
  }

  String _memoryContentArg(Map<String, dynamic> args) {
    return _firstText(args, const ['content', 'text', 'summary', 'memory']);
  }

  String _memoryIdArg(Map<String, dynamic> args) {
    return _firstText(args, const ['id', 'memory_id']);
  }

  String _memoIdArg(Map<String, dynamic> args) {
    return _firstText(args, const ['id', 'memo_id', 'folder_id', 'note_id']);
  }

  String _memoAction(Map<String, dynamic> args) {
    final action =
        _firstText(args, const ['action', 'operation']).toLowerCase();
    return action.isEmpty ? 'list' : action;
  }

  List<String> _memoryTags(Object? value) {
    if (value is List) {
      return _orderedStrings(
        value
            .map((item) => '$item'.trim().toLowerCase())
            .where((item) => item.isNotEmpty),
      );
    }
    final text = '${value ?? ''}'.trim();
    if (text.isEmpty) return const [];
    return _orderedStrings(
      text
          .split(RegExp(r'[,\s]+'))
          .map((item) => item.trim().toLowerCase())
          .where((item) => item.isNotEmpty),
    );
  }

  double _memoryImportance(Object? value) {
    if (value is num) return value.toDouble().clamp(0, 1).toDouble();
    final parsed = double.tryParse('${value ?? ''}'.trim());
    if (parsed == null) return 0.5;
    return parsed.clamp(0, 1).toDouble();
  }

  double _memoryRecordScore(Map<String, dynamic> record, String query) {
    final text = [
      record['title'],
      record['content'],
      ...(record['tags'] as List? ?? const []),
    ].join(' ');
    final base = _memorySearchScore(text, query);
    final importance = (record['importance'] as num?)?.toDouble() ?? 0.5;
    return base + importance * 0.1;
  }

  double _memorySearchScore(String text, String query) {
    final haystack = text.toLowerCase();
    final terms = query
        .toLowerCase()
        .split(RegExp(r'\s+'))
        .map((term) => term.trim())
        .where((term) => term.isNotEmpty)
        .toList();
    if (terms.isEmpty) return 1;
    var score = 0.0;
    for (final term in terms) {
      if (haystack.contains(term)) score += 1;
    }
    return score / terms.length;
  }

  MobileToolResult _memoryToolOk(
    String toolName,
    String summary,
    Map<String, dynamic> data, {
    bool ok = true,
  }) {
    return MobileToolResult(
      ok: ok,
      summary: summary,
      output: jsonEncode({
        'status': ok ? 'ok' : 'error',
        'data': {
          ...data,
          'tool': toolName,
          'mobile_compatible': true,
          'requires_pc': false,
          'execution_location': 'phone',
          'runtime_layers': [..._flutterRuntimeLayers, 'mobile-memory-store'],
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  MobileToolResult _memoryToolError(
    String toolName,
    String code,
    String message,
  ) {
    return MobileToolResult(
      ok: false,
      summary: message,
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': code,
          'message': message,
          'tool': toolName,
          'execution_location': 'phone',
          'runtime_layers': [..._flutterRuntimeLayers, 'mobile-memory-store'],
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  Future<MobileToolResult> _knowledgeTool(
    String toolName,
    Map<String, dynamic> args,
  ) async {
    switch (toolName) {
      case 'knowledge_create':
        return _knowledgeCreate(args);
      case 'knowledge_get':
        return _knowledgeGet(args);
      case 'knowledge_list':
        return _knowledgeList(args);
      case 'knowledge_update':
        return _knowledgeUpdate(args);
      case 'knowledge_delete':
        return _knowledgeDelete(args);
      case 'knowledge_search':
        return _knowledgeSearch(args);
      case 'knowledge_import_file':
        return _knowledgeImportFile(args);
      case 'knowledge_import_url':
        return _knowledgeImportUrl(args);
      case 'knowledge_attach_to_project':
        return _knowledgeAttachToProject(args);
      case 'knowledge_index':
        return _knowledgeIndex(args);
      case 'knowledge_reindex':
        return _knowledgeReindex(args);
      default:
        return _knowledgeToolError(
          toolName,
          'UNSUPPORTED_KNOWLEDGE_TOOL',
          '$toolName is not a phone knowledge tool.',
        );
    }
  }

  Future<MobileToolResult> _knowledgeCreate(Map<String, dynamic> args) async {
    final content = _knowledgeContentArg(args);
    if (content.isEmpty) {
      return _knowledgeToolError(
        'knowledge_create',
        'MISSING_KNOWLEDGE_CONTENT',
        "'content' or 'text' is required.",
      );
    }
    final title = _knowledgeTitleArg(args, content);
    final id = _knowledgeIdArg(args).isEmpty
        ? _slugifyPhoneArtifactName(title, fallback: _nextToolId('kn'))
        : _knowledgeIdArg(args);
    final now = DateTime.now().toUtc().toIso8601String();
    final record = {
      'id': id,
      'title': title,
      'content': content,
      'tags': _memoryTags(args['tags'] ?? args['tag']),
      'project_id': _firstText(args, const ['project_id', 'project']),
      'source': 'phone',
      'created_at': now,
      'updated_at': now,
    };
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_create',
      prompt: 'このスマホにknowledge recordを作成します。対象: $title',
      arguments: {
        ...args,
        'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('knowledge_create');
    await _store.upsertKnowledgeRecord(_withKnowledgeIndex(record));
    return _knowledgeToolOk(
      'knowledge_create',
      'created phone knowledge $id',
      {
        'knowledge': _normalizeKnowledgeRecord(_withKnowledgeIndex(record)),
        'requires_mobile_approval': true,
      },
    );
  }

  Future<MobileToolResult> _knowledgeGet(Map<String, dynamic> args) async {
    final id = _knowledgeIdArg(args);
    if (id.isEmpty) {
      return _knowledgeToolError(
        'knowledge_get',
        'MISSING_KNOWLEDGE_ID',
        "'id' or 'knowledge_id' is required.",
      );
    }
    final record = await _findKnowledgeRecord(id);
    if (record == null) {
      return _knowledgeToolError(
        'knowledge_get',
        'KNOWLEDGE_NOT_FOUND',
        'Phone-local knowledge not found: $id.',
      );
    }
    return _knowledgeToolOk(
      'knowledge_get',
      'got phone knowledge $id',
      {'knowledge': _normalizeKnowledgeRecord(record)},
    );
  }

  Future<MobileToolResult> _knowledgeList(Map<String, dynamic> args) async {
    final records = await _filteredKnowledgeRecords(args);
    return _knowledgeToolOk(
      'knowledge_list',
      '${records.length} phone knowledge records',
      {'knowledge': records, 'count': records.length},
    );
  }

  Future<MobileToolResult> _knowledgeUpdate(Map<String, dynamic> args) async {
    final id = _knowledgeIdArg(args);
    if (id.isEmpty) {
      return _knowledgeToolError(
        'knowledge_update',
        'MISSING_KNOWLEDGE_ID',
        "'id' or 'knowledge_id' is required.",
      );
    }
    final records = await _store.loadKnowledgeRecords();
    final index =
        records.indexWhere((record) => '${record['id'] ?? ''}'.trim() == id);
    if (index < 0) {
      return _knowledgeToolError(
        'knowledge_update',
        'KNOWLEDGE_NOT_FOUND',
        'Phone-local knowledge not found: $id.',
      );
    }
    final content = _knowledgeContentArg(args);
    final title = _firstText(args, const ['title', 'name']);
    final updated = {
      ...records[index],
      if (title.isNotEmpty) 'title': title,
      if (content.isNotEmpty) 'content': content,
      if (args.containsKey('tags') || args.containsKey('tag'))
        'tags': _memoryTags(args['tags'] ?? args['tag']),
      if (args.containsKey('project_id') || args.containsKey('project'))
        'project_id': _firstText(args, const ['project_id', 'project']),
      'updated_at': DateTime.now().toUtc().toIso8601String(),
    };
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_update',
      prompt: 'このスマホのknowledge recordを更新します。対象: $id',
      arguments: {
        ...args,
        if (content.isNotEmpty) 'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('knowledge_update');
    records[index] = _withKnowledgeIndex(updated);
    await _store.saveKnowledgeRecords(records);
    return _knowledgeToolOk(
      'knowledge_update',
      'updated phone knowledge $id',
      {
        'knowledge': _normalizeKnowledgeRecord(records[index]),
        'requires_mobile_approval': true,
      },
    );
  }

  Future<MobileToolResult> _knowledgeDelete(Map<String, dynamic> args) async {
    final id = _knowledgeIdArg(args);
    if (id.isEmpty) {
      return _knowledgeToolError(
        'knowledge_delete',
        'MISSING_KNOWLEDGE_ID',
        "'id' or 'knowledge_id' is required.",
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_delete',
      prompt: 'このスマホのknowledge recordを削除します。対象: $id',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('knowledge_delete');
    await _store.deleteKnowledgeRecord(id);
    return _knowledgeToolOk(
      'knowledge_delete',
      'deleted phone knowledge $id',
      {'deleted': id, 'requires_mobile_approval': true},
    );
  }

  Future<MobileToolResult> _knowledgeSearch(Map<String, dynamic> args) async {
    final records = await _rankedKnowledgeRecords(args);
    return _knowledgeToolOk(
      'knowledge_search',
      '${records.length} phone knowledge results',
      {
        'results': records,
        'count': records.length,
        'query': _firstText(args, const ['query', 'search', 'text']),
      },
    );
  }

  Future<MobileToolResult> _knowledgeImportFile(
    Map<String, dynamic> args,
  ) async {
    var content = _knowledgeContentArg(args);
    final path = _normalizePhoneArtifactPath(args['path'], allowRoot: false);
    if (content.isEmpty && path != null) {
      final artifact = _mobileArtifactFiles[path];
      if (artifact != null) content = '${artifact['content'] ?? ''}';
    }
    if (content.isEmpty) {
      return _knowledgeToolError(
        'knowledge_import_file',
        'MISSING_PHONE_FILE_CONTENT',
        'Provide content/text or a path inside the phone artifact workspace.',
      );
    }
    final title = _knowledgeTitleArg(args, path ?? content);
    final nextArgs = {
      ...args,
      'title': title,
      'content': content,
      if (path != null) 'source_path': path,
    };
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_import_file',
      prompt: 'このスマホのartifact/textをknowledgeへ取り込みます。対象: $title',
      arguments: {
        ...args,
        'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('knowledge_import_file');
    return _knowledgeCreateWithApprovalAlreadyGranted(
      'knowledge_import_file',
      nextArgs,
      source: 'phone-file',
    );
  }

  Future<MobileToolResult> _knowledgeImportUrl(
    Map<String, dynamic> args,
  ) async {
    final url = _firstText(args, const ['url', 'href']);
    if (!_looksLikeHttpUrl(url)) {
      return _knowledgeToolError(
        'knowledge_import_url',
        'INVALID_URL',
        "'url' must be an http or https URL.",
      );
    }
    final content =
        _knowledgeContentArg(args).isEmpty ? url : _knowledgeContentArg(args);
    final title = _knowledgeTitleArg(args, url);
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_import_url',
      prompt: 'このURL参照をスマホ内knowledgeへ取り込みます。対象: $title',
      arguments: {
        ...args,
        'content_preview': _clampText(content, 160),
      },
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('knowledge_import_url');
    return _knowledgeCreateWithApprovalAlreadyGranted(
      'knowledge_import_url',
      {
        ...args,
        'title': title,
        'content': content,
        'url': url,
      },
      source: 'phone-url',
    );
  }

  Future<MobileToolResult> _knowledgeAttachToProject(
    Map<String, dynamic> args,
  ) async {
    final id = _knowledgeIdArg(args);
    final projectId = _firstText(args, const ['project_id', 'project']);
    if (id.isEmpty || projectId.isEmpty) {
      return _knowledgeToolError(
        'knowledge_attach_to_project',
        'MISSING_INPUT',
        "'id'/'knowledge_id' and 'project_id' are required.",
      );
    }
    final records = await _store.loadKnowledgeRecords();
    final index =
        records.indexWhere((record) => '${record['id'] ?? ''}'.trim() == id);
    if (index < 0) {
      return _knowledgeToolError(
        'knowledge_attach_to_project',
        'KNOWLEDGE_NOT_FOUND',
        'Phone-local knowledge not found: $id.',
      );
    }
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_attach_to_project',
      prompt: 'このスマホのknowledgeをprojectへ紐付けます。対象: $id -> $projectId',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) {
      return _mobileApprovalRequired('knowledge_attach_to_project');
    }
    records[index] = {
      ...records[index],
      'project_id': projectId,
      'updated_at': DateTime.now().toUtc().toIso8601String(),
    };
    await _store.saveKnowledgeRecords(records);
    return _knowledgeToolOk(
      'knowledge_attach_to_project',
      'attached phone knowledge $id to $projectId',
      {
        'knowledge': _normalizeKnowledgeRecord(records[index]),
        'requires_mobile_approval': true,
      },
    );
  }

  Future<MobileToolResult> _knowledgeIndex(Map<String, dynamic> args) async {
    final id = _knowledgeIdArg(args);
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_index',
      prompt: id.isEmpty
          ? 'このスマホのknowledge indexを作成します。'
          : 'このスマホのknowledge indexを作成します。対象: $id',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('knowledge_index');
    final records = await _store.loadKnowledgeRecords();
    var changed = 0;
    final next = [
      for (final record in records)
        if (id.isEmpty || '${record['id'] ?? ''}'.trim() == id)
          (() {
            changed += 1;
            return _withKnowledgeIndex(record);
          })()
        else
          record,
    ];
    await _store.saveKnowledgeRecords(next);
    return _knowledgeToolOk(
      'knowledge_index',
      'indexed $changed phone knowledge records',
      {'indexed_count': changed, 'requires_mobile_approval': true},
    );
  }

  Future<MobileToolResult> _knowledgeReindex(Map<String, dynamic> args) async {
    final projectId = _firstText(args, const ['project_id', 'project']);
    final approved = await _requestMobileApproval(
      toolName: 'knowledge_reindex',
      prompt: projectId.isEmpty
          ? 'このスマホのknowledge indexを再作成します。'
          : 'このスマホのknowledge indexを再作成します。project: $projectId',
      arguments: args,
      risk: 'medium',
    );
    if (!approved) return _mobileApprovalRequired('knowledge_reindex');
    final records = await _store.loadKnowledgeRecords();
    var changed = 0;
    final next = [
      for (final record in records)
        if (projectId.isEmpty || '${record['project_id'] ?? ''}' == projectId)
          (() {
            changed += 1;
            return _withKnowledgeIndex(record);
          })()
        else
          record,
    ];
    await _store.saveKnowledgeRecords(next);
    return _knowledgeToolOk(
      'knowledge_reindex',
      'reindexed $changed phone knowledge records',
      {'indexed_count': changed, 'requires_mobile_approval': true},
    );
  }

  Future<MobileToolResult> _knowledgeCreateWithApprovalAlreadyGranted(
    String toolName,
    Map<String, dynamic> args, {
    required String source,
  }) async {
    final content = _knowledgeContentArg(args);
    final title = _knowledgeTitleArg(args, content);
    final id = _knowledgeIdArg(args).isEmpty
        ? _slugifyPhoneArtifactName(title, fallback: _nextToolId('kn'))
        : _knowledgeIdArg(args);
    final now = DateTime.now().toUtc().toIso8601String();
    final record = _withKnowledgeIndex({
      'id': id,
      'title': title,
      'content': content,
      'tags': _memoryTags(args['tags'] ?? args['tag']),
      'project_id': _firstText(args, const ['project_id', 'project']),
      'url': _firstText(args, const ['url', 'href']),
      'source_path': _firstText(args, const ['source_path', 'path']),
      'source': source,
      'created_at': now,
      'updated_at': now,
    });
    await _store.upsertKnowledgeRecord(record);
    return _knowledgeToolOk(
      toolName,
      'imported phone knowledge $id',
      {
        'knowledge': _normalizeKnowledgeRecord(record),
        'requires_mobile_approval': true,
      },
    );
  }

  Future<Map<String, dynamic>?> _findKnowledgeRecord(String id) async {
    for (final record in await _store.loadKnowledgeRecords()) {
      if ('${record['id'] ?? ''}'.trim() == id) return record;
    }
    return null;
  }

  Future<List<Map<String, dynamic>>> _filteredKnowledgeRecords(
    Map<String, dynamic> args,
  ) async {
    final query = _firstText(args, const ['query', 'search', 'text']);
    final tag = _firstText(args, const ['tag']).toLowerCase();
    final projectId = _firstText(args, const ['project_id', 'project']);
    final limit = _boundedInt(args['limit'], 50, 1, 500);
    var records = (await _store.loadKnowledgeRecords())
        .map(_normalizeKnowledgeRecord)
        .toList();
    if (projectId.isNotEmpty) {
      records = records
          .where((record) => '${record['project_id'] ?? ''}' == projectId)
          .toList();
    }
    if (tag.isNotEmpty) {
      records = records
          .where((record) => (record['tags'] as List? ?? const [])
              .map((item) => '$item'.toLowerCase())
              .contains(tag))
          .toList();
    }
    if (query.isNotEmpty) {
      records = records
          .where((record) => _knowledgeRecordScore(record, query) > 0)
          .toList();
    }
    records.sort((a, b) =>
        '${b['updated_at'] ?? ''}'.compareTo('${a['updated_at'] ?? ''}'));
    return records.take(limit).toList();
  }

  Future<List<Map<String, dynamic>>> _rankedKnowledgeRecords(
    Map<String, dynamic> args,
  ) async {
    final query = _firstText(args, const ['query', 'search', 'text']);
    final limit = _boundedInt(args['limit'], 8, 1, 100);
    final projectId = _firstText(args, const ['project_id', 'project']);
    var records = (await _store.loadKnowledgeRecords())
        .map(_normalizeKnowledgeRecord)
        .toList();
    if (projectId.isNotEmpty) {
      records = records
          .where((record) => '${record['project_id'] ?? ''}' == projectId)
          .toList();
    }
    final scored = [
      for (final record in records)
        {
          ...record,
          'score': query.isEmpty ? 1.0 : _knowledgeRecordScore(record, query),
        }
    ]..sort((a, b) {
        final score = ((b['score'] as num?)?.toDouble() ?? 0)
            .compareTo((a['score'] as num?)?.toDouble() ?? 0);
        if (score != 0) return score;
        return '${b['updated_at'] ?? ''}'.compareTo('${a['updated_at'] ?? ''}');
      });
    return scored
        .where(
            (record) => query.isEmpty || ((record['score'] as num?) ?? 0) > 0)
        .take(limit)
        .toList();
  }

  Map<String, dynamic> _normalizeKnowledgeRecord(Map<String, dynamic> record) {
    final content = '${record['content'] ?? record['text'] ?? ''}';
    final id = '${record['id'] ?? record['knowledge_id'] ?? ''}'.trim();
    return {
      'id': id,
      'title': '${record['title'] ?? record['name'] ?? id}'.trim(),
      'content': content,
      'tags': _memoryTags(record['tags'] ?? record['tag']),
      'project_id': '${record['project_id'] ?? record['project'] ?? ''}'.trim(),
      'source': '${record['source'] ?? 'phone'}',
      'url': '${record['url'] ?? ''}'.trim(),
      'source_path': '${record['source_path'] ?? record['path'] ?? ''}'.trim(),
      'index_terms': _knowledgeTerms(record['index_terms'] ?? content),
      'indexed_at': '${record['indexed_at'] ?? ''}'.trim(),
      'char_count': content.length,
      if (record['created_at'] != null) 'created_at': record['created_at'],
      if (record['updated_at'] != null) 'updated_at': record['updated_at'],
    };
  }

  Map<String, dynamic> _withKnowledgeIndex(Map<String, dynamic> record) {
    final normalized = _normalizeKnowledgeRecord(record);
    return {
      ...record,
      'index_terms': _knowledgeTerms(
        '${normalized['title']} ${normalized['content']} ${(normalized['tags'] as List).join(' ')}',
      ),
      'indexed_at': DateTime.now().toUtc().toIso8601String(),
    };
  }

  String _knowledgeContentArg(Map<String, dynamic> args) {
    return _firstText(args, const ['content', 'text', 'body', 'summary']);
  }

  String _knowledgeTitleArg(Map<String, dynamic> args, String fallbackText) {
    final title = _firstText(args, const ['title', 'name', 'label']);
    if (title.isNotEmpty) return title;
    final compact = fallbackText.replaceAll(RegExp(r'\s+'), ' ').trim();
    if (_looksLikeHttpUrl(compact)) return Uri.parse(compact).host;
    return compact.isEmpty ? 'Phone knowledge' : _clampText(compact, 60);
  }

  String _knowledgeIdArg(Map<String, dynamic> args) {
    final raw = _firstText(args, const ['id', 'knowledge_id']);
    if (raw.isEmpty) return '';
    return _slugifyPhoneArtifactName(raw, fallback: raw);
  }

  List<String> _knowledgeTerms(Object? value) {
    final text = value is List ? value.join(' ') : '${value ?? ''}';
    final terms = text
        .toLowerCase()
        .split(RegExp(r'[^a-z0-9ぁ-んァ-ヶ一-龠ー]+'))
        .map((term) => term.trim())
        .where((term) => term.length >= 2)
        .take(80);
    return _orderedStrings(terms);
  }

  double _knowledgeRecordScore(Map<String, dynamic> record, String query) {
    final indexed = (record['index_terms'] as List? ?? const []).join(' ');
    final text = [
      record['title'],
      record['content'],
      indexed,
      ...(record['tags'] as List? ?? const []),
    ].join(' ');
    return _memorySearchScore(text, query);
  }

  MobileToolResult _knowledgeToolOk(
    String toolName,
    String summary,
    Map<String, dynamic> data, {
    bool ok = true,
  }) {
    return MobileToolResult(
      ok: ok,
      summary: summary,
      output: jsonEncode({
        'status': ok ? 'ok' : 'error',
        'data': {
          ...data,
          'tool': toolName,
          'mobile_compatible': true,
          'requires_pc': false,
          'execution_location': 'phone',
          'runtime_layers': [
            ..._flutterRuntimeLayers,
            'mobile-knowledge-store',
          ],
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  MobileToolResult _knowledgeToolError(
    String toolName,
    String code,
    String message,
  ) {
    return MobileToolResult(
      ok: false,
      summary: message,
      output: jsonEncode({
        'status': 'error',
        'error': {
          'code': code,
          'message': message,
          'tool': toolName,
          'execution_location': 'phone',
          'runtime_layers': [
            ..._flutterRuntimeLayers,
            'mobile-knowledge-store',
          ],
          'platforms': _defaultMobilePlatforms,
        },
      }),
    );
  }

  String _unsupportedReason(String name) {
    final normalized = name.trim().toLowerCase();
    for (final tool in unavailableDefaultspackTools) {
      if (normalized == tool.name ||
          tool.tags.contains(normalized) ||
          tool.aliases.contains(normalized)) {
        return tool.unavailableReason;
      }
    }
    if (normalized == 'media_clipboard_read' ||
        normalized == 'media_clipboard_write') {
      return 'このdefaultspack toolはiOS Swift/Android Kotlinのclipboard bridgeでスマホ実装可能ですが、clipboard読み書き用のモバイル承認UIがまだないため、このスマホ単体では実行しません。PC接続時はPC側runtimeへ委譲できます。';
    }
    if (_isMobileConnectorDryRunTool(normalized)) {
      return 'このdefaultspack connector toolはFlutter/Dartでdry-run planをスマホ実行できます。実送信やCLI execute=trueはPC接続時にPC側runtimeへ委譲してください。';
    }
    final catalogEntry = _findDefaultspackCatalogEntry(normalized);
    if (catalogEntry != null) {
      return _unsupportedReasonForTags(normalized, catalogEntry.tags);
    }
    if (normalized.startsWith('coding_') ||
        normalized.startsWith('sandbox_') ||
        normalized.contains('terminal') ||
        normalized.contains('workspace') ||
        normalized.contains('git_')) {
      return 'このdefaultspack toolはPC側のcoding/workspace/terminal runtimeに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
    }
    if (normalized.startsWith('browser_')) {
      return 'このdefaultspack toolはPC側のbrowser sessionに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
    }
    if (normalized.startsWith('computer_')) {
      return 'このdefaultspack toolはPC画面の操作権限が必要なため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
    }
    if (normalized.startsWith('agent_')) {
      return 'このdefaultspack toolはPC側のagent serviceまたはagent queueに依存するため、このスマホ単体では実行できません。スマホ内agentではmobile-compatible toolだけを実行します。';
    }
    if (normalized.startsWith('ai_')) {
      return 'このdefaultspack toolはPC側のAI provider catalog/routing/key管理に依存します。スマホ単体では設定済みモデルへのチャットとmobile-compatible tool実行に対応しています。';
    }
    if (normalized.startsWith('chat_')) {
      return 'このdefaultspack toolはPC側の会話storeに依存します。PC接続時はPCスペースで実行し、スマホ単体ではローカル会話として送信してください。';
    }
    if (normalized.startsWith('media_') ||
        normalized.startsWith('ambient_') ||
        normalized.startsWith('input_endpoint_')) {
      return 'このdefaultspack toolはPC側のOS/media/input runtimeに依存するため、このスマホ単体ではまだ実行できません。';
    }
    if (normalized.startsWith('memory_') ||
        normalized.startsWith('knowledge_') ||
        normalized.startsWith('artifact_')) {
      return 'このdefaultspack toolはPC側のprofile/workspace storageに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
    }
    if (normalized.startsWith('tool_')) {
      return 'このdefaultspack toolはPC側のtool registryまたは外部serviceに依存するため、このスマホ単体では未対応です。mobile-compatible tag付きtoolだけをスマホ内で実行できます。';
    }
    return 'このtoolはこのスマホのmobile-compatible runtimeに未登録です。PC接続時はPC側のtool catalogを確認してください。';
  }
}

String _canonicalToolName(String name) {
  final normalized = name.trim().toLowerCase();
  for (final tool in [
    ...MobileToolRuntime.supportedTools,
    ...MobileToolRuntime.unavailableDefaultspackTools,
  ]) {
    if (normalized == tool.name || tool.aliases.contains(normalized)) {
      return tool.name;
    }
  }
  return normalized;
}

bool _isAsyncPhoneToolName(String name) {
  final normalized = name.trim().toLowerCase();
  if (_phoneAiModelToolIds.contains(normalized)) return true;
  if (_phonePromptToolIds.contains(normalized)) return true;
  if (_phoneMemoryToolIds.contains(normalized)) return true;
  if (_phoneKnowledgeToolIds.contains(normalized)) return true;
  if (_phoneWorkflowMutationToolIds.contains(normalized)) return true;
  return const {
    'media_clipboard_read',
    'media_clipboard_write',
    'media_file_pick',
    'media_screenshot',
    'media_image_transform',
    'media_ocr',
    'ocr_extract',
    'image_resize',
    'image_convert',
    'artifact_file_write',
    'artifact_file_patch',
    'artifact_file_delete',
    'mobile_url_open',
    'tool_batch',
    'webapp_build',
    'sheet_update',
    'doc_update',
    'slides_update',
    'job_cancel',
    'job_resume',
  }.contains(normalized);
}

bool _isMobileConnectorDryRunTool(String name) {
  final normalized = name.trim().toLowerCase();
  return _mobileCliDryRunToolIds.contains(normalized) ||
      _mobileConnectorPayloadDryRunToolIds.contains(normalized);
}

bool _isOpenAiFunctionName(String name) {
  final trimmed = name.trim();
  return RegExp(r'^[a-zA-Z0-9_-]{1,64}$').hasMatch(trimmed);
}

class _ConnectorCommandPlan {
  const _ConnectorCommandPlan({
    required this.ok,
    this.commands = const [],
    this.payload,
    this.errorCode = '',
    this.errorMessage = '',
  });

  final bool ok;
  final List<List<String>> commands;
  final Map<String, dynamic>? payload;
  final String errorCode;
  final String errorMessage;
}

_ConnectorCommandPlan _connectorCliCommands(
  String toolName,
  Map<String, dynamic> args,
) {
  return switch (toolName) {
    'github_search' => _githubSearchCommands(args),
    'github_pr_create' => _githubPrCreateCommands(args),
    'github_issue_create' => _githubIssueCreateCommands(args),
    'github_issue_update' => _githubIssueUpdateCommands(args),
    'github_issue_list' => _githubIssueListCommands(args),
    'linear_issue_sync' => _thirdPartyIssueSyncCommands(
        args,
        toolName: 'linear_issue_sync',
        connectorName: 'linear',
        executable: 'linear',
      ),
    'jira_issue_sync' => _thirdPartyIssueSyncCommands(
        args,
        toolName: 'jira_issue_sync',
        connectorName: 'jira',
        executable: 'jira',
      ),
    _ => _ConnectorCommandPlan(
        ok: false,
        errorCode: 'UNSUPPORTED_CONNECTOR_TOOL',
        errorMessage: '$toolName is not a phone CLI dry-run tool.',
      ),
  };
}

_ConnectorCommandPlan _githubSearchCommands(Map<String, dynamic> args) {
  final query = '${args['query'] ?? ''}';
  final kind = '${args['kind'] ?? 'repos'}'.trim().isEmpty
      ? 'repos'
      : '${args['kind']}'.trim();
  final limit = _boundedConnectorLimit(args['limit'], defaultValue: 10);
  return _ConnectorCommandPlan(
    ok: true,
    commands: [
      ['gh', 'search', kind, query, '--limit', '$limit'],
    ],
  );
}

_ConnectorCommandPlan _githubPrCreateCommands(Map<String, dynamic> args) {
  final command = [
    'gh',
    'pr',
    'create',
    '--title',
    '${args['title'] ?? 'PR'}',
    '--body',
    '${args['body'] ?? ''}',
  ];
  if (_boolArg(args['draft'], fallback: true)) command.add('--draft');
  return _ConnectorCommandPlan(ok: true, commands: [command]);
}

_ConnectorCommandPlan _githubIssueCreateCommands(Map<String, dynamic> args) {
  return _ConnectorCommandPlan(
    ok: true,
    commands: [
      [
        'gh',
        'issue',
        'create',
        '--title',
        '${args['title'] ?? 'Issue'}',
        '--body',
        '${args['body'] ?? ''}',
      ],
    ],
  );
}

_ConnectorCommandPlan _githubIssueListCommands(Map<String, dynamic> args) {
  final command = ['gh', 'issue', 'list'];
  _addRepo(command, args);
  var state = _firstConnectorText(args, const ['state']);
  final status = _firstConnectorText(args, const ['status']);
  if (state.isEmpty && {'open', 'opened', 'closed'}.contains(status)) {
    state = status == 'closed' ? 'closed' : 'open';
  }
  if (state.isNotEmpty) command.addAll(['--state', state]);
  command.addAll([
    '--limit',
    '${_boundedConnectorLimit(args['limit'], defaultValue: 30)}',
  ]);
  final assignee = _firstConnectorText(args, const ['assignee']);
  if (assignee.isNotEmpty) command.addAll(['--assignee', assignee]);
  for (final label in _connectorTextList(args['label'] ?? args['labels'])) {
    command.addAll(['--label', label]);
  }
  if (status.isNotEmpty && state.isEmpty) {
    command.addAll(['--label', _statusLabel(status)]);
  }
  final search = _firstConnectorText(args, const ['search', 'query']);
  if (search.isNotEmpty) command.addAll(['--search', search]);
  return _ConnectorCommandPlan(ok: true, commands: [command]);
}

_ConnectorCommandPlan _githubIssueUpdateCommands(Map<String, dynamic> args) {
  final issue = _issueReference(args);
  if (issue.isEmpty) {
    return const _ConnectorCommandPlan(
      ok: false,
      errorCode: 'MISSING_ISSUE',
      errorMessage:
          'github_issue_update requires issue, issue_number, id, or key.',
    );
  }

  final command = ['gh', 'issue', 'edit', issue];
  _addRepo(command, args);
  var hasEdit = false;
  final title = _firstConnectorText(args, const ['title', 'summary']);
  final body = _firstConnectorText(args, const ['body', 'description']);
  if (title.isNotEmpty) {
    command.addAll(['--title', title]);
    hasEdit = true;
  }
  if (body.isNotEmpty) {
    command.addAll(['--body', body]);
    hasEdit = true;
  }
  for (final assignee
      in _connectorTextList(args['assignee'] ?? args['assignees'])) {
    command.addAll(['--add-assignee', assignee]);
    hasEdit = true;
  }

  final commands = <List<String>>[];
  final status = _firstConnectorText(args, const ['status', 'state']);
  final statusLower = status.toLowerCase();
  if ({'closed', 'close', 'done', 'resolved'}.contains(statusLower)) {
    final closeCommand = ['gh', 'issue', 'close', issue];
    _addRepo(closeCommand, args);
    commands.add(closeCommand);
  } else if ({'open', 'opened', 'reopen', 'reopened'}.contains(statusLower)) {
    final reopenCommand = ['gh', 'issue', 'reopen', issue];
    _addRepo(reopenCommand, args);
    commands.add(reopenCommand);
  } else if (status.isNotEmpty) {
    command.addAll(['--add-label', _statusLabel(status)]);
    hasEdit = true;
  }
  if (hasEdit) commands.insert(0, command);

  final comment = _firstConnectorText(args, const ['comment', 'note']);
  if (comment.isNotEmpty) {
    final commentCommand = ['gh', 'issue', 'comment', issue, '--body', comment];
    _addRepo(commentCommand, args);
    commands.add(commentCommand);
  }
  if (commands.isEmpty) {
    return const _ConnectorCommandPlan(
      ok: false,
      errorCode: 'EMPTY_UPDATE',
      errorMessage:
          'github_issue_update needs a title, body, status, assignee, or comment.',
    );
  }
  return _ConnectorCommandPlan(
    ok: true,
    commands: commands,
    payload: _redactConnectorPayload({
      'issue': issue,
      'title': title.isEmpty ? null : title,
      'status': status.isEmpty ? null : status,
      'assignee': args['assignee'] ?? args['assignees'],
      'comment': comment.isEmpty ? null : comment,
    }) as Map<String, dynamic>,
  );
}

_ConnectorCommandPlan _thirdPartyIssueSyncCommands(
  Map<String, dynamic> args, {
  required String toolName,
  required String connectorName,
  required String executable,
}) {
  final issue = _issueReference(args);
  if (issue.isEmpty) {
    return _ConnectorCommandPlan(
      ok: false,
      errorCode: 'MISSING_ISSUE',
      errorMessage: '$toolName requires issue, issue_id, id, or key.',
    );
  }
  final command = [executable, 'issue', 'update', issue];
  var hasUpdate = false;
  final title = _firstConnectorText(args, const ['title', 'summary']);
  final description = _firstConnectorText(args, const ['body', 'description']);
  final status = _firstConnectorText(args, const ['status', 'state']);
  final assignee = _firstConnectorText(args, const ['assignee']);
  if (title.isNotEmpty) {
    command.addAll(['--title', title]);
    hasUpdate = true;
  }
  if (description.isNotEmpty) {
    command.addAll(['--description', description]);
    hasUpdate = true;
  }
  if (status.isNotEmpty) {
    command.addAll(['--status', status]);
    hasUpdate = true;
  }
  if (assignee.isNotEmpty) {
    command.addAll(['--assignee', assignee]);
    hasUpdate = true;
  }
  final commands = <List<String>>[];
  if (hasUpdate) commands.add(command);
  final comment = _firstConnectorText(args, const ['comment', 'note']);
  if (comment.isNotEmpty) {
    commands.add([executable, 'issue', 'comment', issue, '--body', comment]);
  }
  if (commands.isEmpty) {
    return _ConnectorCommandPlan(
      ok: false,
      errorCode: 'EMPTY_UPDATE',
      errorMessage:
          '$toolName needs a title, description, status, assignee, or comment.',
    );
  }
  return _ConnectorCommandPlan(
    ok: true,
    commands: commands,
    payload: _redactConnectorPayload({
      'connector_required': connectorName,
      'issue': issue,
      'title': title.isEmpty ? null : title,
      'description': description.isEmpty ? null : description,
      'status': status.isEmpty ? null : status,
      'assignee': assignee.isEmpty ? null : assignee,
      'comment': comment.isEmpty ? null : comment,
      'external_url': args['external_url'] ?? args['url'],
      'metadata': args['metadata'] is Map ? args['metadata'] : null,
    }) as Map<String, dynamic>,
  );
}

Map<String, dynamic>? _connectorPayloadDryRunData(
  String toolName,
  Map<String, dynamic> args,
) {
  final redacted = _redactConnectorPayload(args);
  return switch (toolName) {
    'gmail_search' => {
        'connector_required': 'gmail',
        'query': args['query'],
      },
    'gmail_draft' => {
        'connector_required': 'gmail',
        'draft': {
          'to': args['to'],
          'subject': args['subject'],
          'body': args['body'],
        },
      },
    'calendar_create' => {
        'connector_required': 'calendar',
        'event': redacted,
      },
    'drive_create' => {
        'connector_required': 'drive',
        'file': redacted,
      },
    'drive_export' => {
        'connector_required': 'drive',
        'export': redacted,
      },
    'slack_send' => {
        'connector_required': 'slack',
        'message': redacted,
      },
    'discord_send' => {
        'connector_required': 'discord',
        'message': redacted,
      },
    'line_push' => {
        'connector_required': 'line',
        'message': redacted,
      },
    _ => null,
  };
}

String _firstConnectorText(Map<String, dynamic> args, Iterable<String> keys) {
  for (final key in keys) {
    final value = args[key];
    if (value != null && '$value'.trim().isNotEmpty) return '$value'.trim();
  }
  return '';
}

List<String> _connectorTextList(Object? value) {
  if (value is List) {
    return value
        .map((item) => '$item'.trim())
        .where((item) => item.isNotEmpty)
        .toList();
  }
  final text = '${value ?? ''}'.trim();
  return text.isEmpty ? const [] : [text];
}

void _addRepo(List<String> command, Map<String, dynamic> args) {
  final repo = _firstConnectorText(args, const ['repo', 'repository']);
  if (repo.isNotEmpty) command.addAll(['--repo', repo]);
}

String _statusLabel(String status) {
  return 'status:${status.trim().toLowerCase().replaceAll(RegExp(r'\s+'), '-')}';
}

String _issueReference(Map<String, dynamic> args) {
  return _firstConnectorText(
    args,
    const ['issue', 'issue_number', 'number', 'issue_id', 'id', 'key'],
  );
}

int _boundedConnectorLimit(Object? value, {required int defaultValue}) {
  if (value is num) return math.max(1, math.min(100, value.toInt()));
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) return math.max(1, math.min(100, parsed));
  return defaultValue;
}

int _boundedInt(Object? value, int defaultValue, int min, int max) {
  if (value is num) return math.max(min, math.min(max, value.toInt()));
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) return math.max(min, math.min(max, parsed));
  return math.max(min, math.min(max, defaultValue));
}

Object? _redactConnectorPayload(Object? value) {
  if (value is Map) {
    return value.map((key, item) {
      final keyText = '$key';
      return MapEntry(
        keyText,
        _isSensitiveConnectorKey(keyText)
            ? '[redacted]'
            : _redactConnectorPayload(item),
      );
    });
  }
  if (value is List) return value.map(_redactConnectorPayload).toList();
  return value;
}

bool _isSensitiveConnectorKey(String key) {
  final lowered = key.toLowerCase();
  return const [
    'api_key',
    'authorization',
    'bearer',
    'credential',
    'password',
    'secret',
    'token',
  ].any(lowered.contains);
}

Map<String, dynamic> _agentTemplateRecord() => {
      'template_id': mobileAgentTemplateId,
      'ai_input_id': mobileAgentAiInputId,
      'tool_policy_id': mobileAgentToolPolicyId,
    };

Map<String, dynamic> _toolSurfaceRecord(bool pcDelegationAvailable) => {
      'mode': 'unified',
      'one_tool_surface': true,
      'phone_local_route': 'phone',
      'pc_delegation_route': '/api/mobile/v1/tools/invoke',
      'pc_delegation_available': pcDelegationAvailable,
      'routing':
          'Call the defaultspack tool name directly. Phone-compatible tools run locally; host-bound tools route to the connected PC when PC delegation is available.',
    };

MobileToolDefinition? _findToolDefinition(String name) {
  final normalized = name.trim().toLowerCase();
  for (final tool in [
    ...MobileToolRuntime.supportedTools,
    ...MobileToolRuntime.unavailableDefaultspackTools,
  ]) {
    if (normalized == tool.name || tool.aliases.contains(normalized)) {
      return tool;
    }
  }
  return null;
}

DefaultspackToolAgentManifestEntry? _findDefaultspackCatalogEntry(String name) {
  final normalized = name.trim().toLowerCase();
  if (normalized.isEmpty) return null;
  for (final entry in _defaultspackToolAgentManifestCatalog) {
    if (entry.id == normalized || entry.aliases.contains(normalized)) {
      return entry;
    }
  }
  return null;
}

List<Map<String, dynamic>> _catalogRecords({
  required bool includeUnavailable,
}) {
  final records = <Map<String, dynamic>>[];
  final seenFunctionIds = <String>{};
  for (final entry in _defaultspackToolAgentManifestCatalog) {
    final record = _catalogEntryRecord(entry);
    if (includeUnavailable || record['mobile_compatible'] == true) {
      records.add(record);
    }
    seenFunctionIds.add(entry.id);
  }

  for (final tool in MobileToolRuntime.supportedTools) {
    final coveredByManifest = seenFunctionIds.contains(tool.name) ||
        tool.aliases.any((alias) => seenFunctionIds.contains(alias));
    if (coveredByManifest) continue;
    records.add(_toolRecord(tool, functionId: tool.name));
    seenFunctionIds.add(tool.name);
  }

  if (includeUnavailable) {
    for (final tool in MobileToolRuntime.unavailableDefaultspackTools) {
      final coveredByManifest = seenFunctionIds.contains(tool.name) ||
          tool.aliases.any((alias) => seenFunctionIds.contains(alias));
      if (coveredByManifest) continue;
      records.add(_toolRecord(tool, functionId: tool.name));
      seenFunctionIds.add(tool.name);
    }
  }

  return records;
}

Map<String, dynamic> _catalogEntryRecord(
  DefaultspackToolAgentManifestEntry entry, {
  String requestedName = '',
}) {
  final canonical = _canonicalToolName(entry.id);
  final tool = _findToolDefinition(canonical);
  if (tool != null) {
    final record = _toolRecord(
      tool,
      functionId: entry.id,
      requestedName: requestedName.isEmpty ? entry.id : requestedName,
    );
    record['summary'] =
        entry.description.isEmpty ? record['summary'] : entry.description;
    record['manifest_tags'] = entry.tags;
    record['aliases'] = {
      ...entry.aliases,
      ...(record['aliases'] as List? ?? const []),
    }.map((alias) => '$alias').toList()
      ..sort();
    if (!tool.available) {
      record['parameters'] = entry.inputSchema;
    }
    record['tags'] = _orderedStrings([
      ...entry.tags,
      ...(record['tags'] as List? ?? const []),
    ]);
    return record;
  }
  final record = _unsupportedToolRecord(entry.id);
  record['function_id'] = entry.id;
  if (requestedName.trim().isNotEmpty) {
    record['requested_name'] = requestedName.trim();
  }
  record['aliases'] = entry.aliases;
  record['summary'] =
      entry.description.isEmpty ? record['summary'] : entry.description;
  record['manifest_tags'] = entry.tags;
  record['tags'] = _orderedStrings([
    ...entry.tags,
    ...(record['tags'] as List? ?? const []),
  ]);
  record['parameters'] = entry.inputSchema;
  return record;
}

Map<String, dynamic> _toolRecord(
  MobileToolDefinition tool, {
  String functionId = '',
  String requestedName = '',
}) {
  final mobile = _mobileRuntimeRecordForTool(tool);
  return {
    'function_id': functionId.trim().isEmpty ? tool.name : functionId.trim(),
    'tool_id': tool.name,
    if (requestedName.trim().isNotEmpty) 'requested_name': requestedName,
    'aliases': tool.aliases,
    'tags': _orderedStrings([
      ...tool.tags,
      ...(mobile['tags'] as List? ?? const []),
    ]),
    'mobile_compatible': tool.available,
    'mobile': mobile,
    'execution_location': mobile['execution_location'],
    'execution_platforms': mobile['platforms'],
    'mobile_runtime_layers': mobile['runtime_layers'],
    'native_layers': mobile['native_layers'],
    'requires_mobile_approval': tool.requiresMobileApproval,
    'unavailable_reason': tool.unavailableReason,
    'summary': tool.description,
    'parameters': tool.parameters,
  };
}

Map<String, dynamic> _mobileRuntimeRecordForTool(MobileToolDefinition tool) {
  if (!tool.available) {
    return _mobileRuntimeRecordForUnavailable(
      tool.name,
      tool.tags,
      tool.unavailableReason,
    );
  }
  final runtimeLayers = _phoneAiModelToolIds.contains(tool.name)
      ? _orderedStrings([...tool.runtimeLayers, 'mobile-provider-config'])
      : _phonePromptToolIds.contains(tool.name)
          ? _orderedStrings([...tool.runtimeLayers, 'mobile-prompt-store'])
          : _phoneMemoryToolIds.contains(tool.name)
              ? _orderedStrings([...tool.runtimeLayers, 'mobile-memory-store'])
              : _phoneKnowledgeToolIds.contains(tool.name)
                  ? _orderedStrings([
                      ...tool.runtimeLayers,
                      'mobile-knowledge-store',
                    ])
                  : tool.runtimeLayers;
  return {
    'compatible': true,
    'available': true,
    'execution_location': 'phone',
    'platforms': tool.executionPlatforms,
    'runtime_layers': runtimeLayers,
    'native_layers': tool.nativeLayers,
    'requires_pc': false,
    'requires_mobile_approval': tool.requiresMobileApproval,
    'implementation_status': tool.implementationStatus,
    'tags': _mobilePlatformTags(
      platforms: tool.executionPlatforms,
      runtimeLayers: runtimeLayers,
      pcDelegated: false,
    ),
  };
}

Map<String, dynamic> _mobileRuntimeRecordForConnectorDryRun(String name) {
  final normalized = name.trim().toLowerCase();
  final implementationStatus = _mobileCliDryRunToolIds.contains(normalized)
      ? 'implemented_cli_dry_run_pc_execute'
      : 'implemented_connector_dry_run';
  return {
    'compatible': true,
    'available': true,
    'execution_location': 'phone',
    'platforms': _defaultMobilePlatforms,
    'runtime_layers': _flutterRuntimeLayers,
    'native_layers': [],
    'requires_pc': false,
    'requires_mobile_approval': false,
    'implementation_status': implementationStatus,
    'tags': _mobilePlatformTags(
      platforms: _defaultMobilePlatforms,
      runtimeLayers: _flutterRuntimeLayers,
      pcDelegated: false,
    ),
  };
}

Map<String, dynamic> _mobileRuntimeRecordForUnavailable(
  String name,
  List<String> tags,
  String reason,
) {
  final plan = _mobilePortPlan(name, tags);
  return {
    'compatible': false,
    'available': false,
    'execution_location': 'pc',
    'platforms': plan['platforms'],
    'runtime_layers': plan['runtime_layers'],
    'native_layers': plan['native_layers'],
    'requires_pc': true,
    'requires_mobile_approval': plan['requires_mobile_approval'],
    'implementation_status': plan['implementation_status'],
    'unavailable_reason': reason,
    'tags': _mobilePlatformTags(
      platforms: (plan['platforms'] as List? ?? const []).map((e) => '$e'),
      runtimeLayers:
          (plan['runtime_layers'] as List? ?? const []).map((e) => '$e'),
      pcDelegated: true,
    ),
  };
}

Map<String, dynamic> _mobilePortPlan(String name, List<String> tags) {
  final normalized = name.trim().toLowerCase();
  final tagSet = tags.map((tag) => tag.trim().toLowerCase()).toSet();
  final isClipboard = normalized == 'media_clipboard_read' ||
      normalized == 'media_clipboard_write';
  final isMediaPicker = normalized == 'media_file_pick';
  final isScreenshot = normalized == 'media_screenshot';
  final isImageRead = normalized == 'media_image_read';
  final isImageTransform = normalized == 'media_image_transform';
  final isImagePayloadTool =
      normalized == 'image_resize' || normalized == 'image_convert';
  final isOcrTool = normalized == 'media_ocr' || normalized == 'ocr_extract';
  final isDocParse = normalized == 'media_doc_parse';
  final isPdfParse = normalized == 'media_pdf_parse';
  final isPdfPayloadTool =
      normalized == 'pdf_extract' || normalized == 'pdf_extract_tables';
  final isPreviewPayloadTool = normalized == 'artifact_preview' ||
      normalized == 'html_preview' ||
      normalized == 'pdf_preview';
  final isSourcePayloadTool =
      normalized == 'source_extract' || normalized == 'source_rank';
  final isBrowserExtractTable = normalized == 'browser_extract_table';
  final isTtsFallbackTool =
      normalized == 'tts_generate' || normalized == 'tts_generate_local';
  final isPhoneAiModelTool = _phoneAiModelToolIds.contains(normalized);
  final isPhonePromptTool = _phonePromptToolIds.contains(normalized);
  final isPhoneMemoryTool = _phoneMemoryToolIds.contains(normalized);
  final isPhoneKnowledgeTool = _phoneKnowledgeToolIds.contains(normalized);
  final isPhoneMediaArtifactTool =
      _phoneMediaArtifactToolIds.contains(normalized);
  final isPhoneWorkflowTool = _phoneWorkflowToolIds.contains(normalized);
  final isPhoneArtifactWorkspaceTool = const {
    'artifact_file_list',
    'artifact_file_read',
    'artifact_file_write',
    'artifact_file_patch',
    'artifact_file_delete',
  }.contains(normalized);
  final isPhoneArtifactHtmlTool = const {
    'browser_save_page',
    'webapp_preview',
    'webapp_lint',
  }.contains(normalized);
  final isPhonePackageWebappTool = const {
    'package_install_plan',
    'webapp_build',
    'research_report_export',
  }.contains(normalized);
  final isPhoneArtifactGeneratorTool = const {
    'project_scaffold',
    'doc_create',
    'slides_create',
    'slides_from_markdown',
    'chart_create',
  }.contains(normalized);
  final isPhoneDocumentSlideMutationTool = const {
    'doc_update',
    'slides_update',
  }.contains(normalized);
  final isPhoneSlideExportTool = normalized == 'slides_export';
  final isPhoneJobTool = const {
    'job_create',
    'job_status',
    'job_history',
    'job_artifacts',
    'job_cancel',
    'job_resume',
  }.contains(normalized);
  final isPhoneSheetTool = const {
    'sheet_create',
    'sheet_read',
    'sheet_analyze',
    'sheet_update',
    'sheet_export',
  }.contains(normalized);
  final isPhoneExportTool = const {
    'artifact_zip',
    'artifact_export',
    'static_site_export',
    'webapp_export_static',
    'doc_export',
    'pdf_export',
    'doc_to_pdf',
  }.contains(normalized);
  if (isClipboard) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': ['flutter', 'ios-swift', 'android-kotlin'],
      'native_layers': [
        'ios:Swift UIPasteboard',
        'android:Kotlin ClipboardManager',
      ],
      'requires_mobile_approval': true,
      'implementation_status': 'implemented',
    };
  }
  if (isMediaPicker) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _nativeMediaPickerRuntimeLayers,
      'native_layers': [
        'ios:Swift UIDocumentPickerViewController',
        'android:Kotlin Intent.ACTION_OPEN_DOCUMENT',
      ],
      'requires_mobile_approval': true,
      'implementation_status': 'implemented',
    };
  }
  if (isScreenshot) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _nativeScreenshotRuntimeLayers,
      'native_layers': [
        'ios:Swift UIWindow screenshot capture',
        'android:Kotlin View drawing cache capture',
      ],
      'requires_mobile_approval': true,
      'implementation_status': 'implemented',
    };
  }
  if (isImageRead) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented',
    };
  }
  if (isImageTransform) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _nativeImageTransformRuntimeLayers,
      'native_layers': [
        'ios:Swift UIImage resize/encode',
        'android:Kotlin Bitmap resize/encode',
      ],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented',
    };
  }
  if (isImagePayloadTool) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _nativeImageTransformRuntimeLayers,
      'native_layers': [
        'ios:Swift UIImage resize/encode',
        'android:Kotlin Bitmap resize/encode',
      ],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented_payload_only',
    };
  }
  if (isOcrTool) {
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _nativeOcrRuntimeLayers,
      'native_layers': const [
        'ios:Swift Vision VNRecognizeTextRequest',
        'android:Kotlin ML Kit TextRecognition',
      ],
      'requires_mobile_approval': false,
      'implementation_status': normalized == 'ocr_extract'
          ? 'implemented_payload_only_native_ocr'
          : 'implemented_native_ocr_bridge',
    };
  }
  if (isDocParse) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented_text_documents',
    };
  }
  if (isPdfParse) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented_best_effort_bytes',
    };
  }
  if (isPdfPayloadTool) {
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': normalized == 'pdf_extract_tables'
          ? 'implemented_empty_table_fallback'
          : 'implemented_best_effort_bytes',
    };
  }
  if (isPreviewPayloadTool) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented_payload_only_preview',
    };
  }
  if (isSourcePayloadTool) {
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': normalized == 'source_extract'
          ? 'implemented_payload_only'
          : 'implemented',
    };
  }
  if (isBrowserExtractTable) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented_payload_only_html',
    };
  }
  if (isTtsFallbackTool) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented_silent_wav_fallback',
    };
  }
  if (isPhoneAiModelTool) {
    final status = switch (normalized) {
      'ai_models' ||
      'ai_profiles' ||
      'ai_providers' =>
        'implemented_phone_ai_catalog',
      'ai_get_provider_key_status' =>
        'implemented_phone_ai_provider_key_status',
      'ai_set_provider_key' ||
      'ai_delete_provider_key' =>
        'implemented_phone_ai_provider_key',
      'ai_get_preferred_model' ||
      'ai_set_preferred_model' ||
      'ai_get_thinking_level' ||
      'ai_set_thinking_level' ||
      'ai_get_effective_thinking_level' ||
      'ai_normalize_thinking_level' =>
        'implemented_phone_ai_model_settings',
      'ai_validate_model_params' => 'implemented_phone_ai_param_validation',
      _ => 'implemented_phone_ai_routing_hint',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': [..._flutterRuntimeLayers, 'mobile-provider-config'],
      'native_layers': [],
      'requires_mobile_approval':
          _phoneAiModelMutationToolIds.contains(normalized),
      'implementation_status': status,
    };
  }
  if (isPhonePromptTool) {
    final status = switch (normalized) {
      'prompt_system_get' ||
      'prompt_system_set' =>
        'implemented_phone_prompt_system',
      'prompt_list' ||
      'prompt_create' ||
      'prompt_update' ||
      'prompt_delete' =>
        'implemented_phone_prompt_store',
      'prompt_active' ||
      'prompt_load_effective' ||
      'prompt_resolve_for_conversation' =>
        'implemented_phone_prompt_effective',
      'prompt_preview_toggle' => 'implemented_phone_prompt_preview',
      _ => 'implemented_phone_prompt_text',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': [..._flutterRuntimeLayers, 'mobile-prompt-store'],
      'native_layers': [],
      'requires_mobile_approval':
          _phonePromptMutationToolIds.contains(normalized),
      'implementation_status': status,
    };
  }
  if (isPhoneMemoryTool) {
    final status = switch (normalized) {
      'memory_recall' => 'implemented_phone_memory_search',
      'memory_compact' => 'implemented_phone_memory_summary',
      'memory_project_context' ||
      'memory_resolve_for_agent' =>
        'implemented_phone_memory_context',
      'memory_memo' ||
      'memory_memo_folders' ||
      'memory_memo_notes' =>
        'implemented_phone_memo_store',
      _ => 'implemented_phone_memory_store',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': [..._flutterRuntimeLayers, 'mobile-memory-store'],
      'native_layers': [],
      'requires_mobile_approval':
          _phoneMemoryMutationToolIds.contains(normalized),
      'implementation_status': status,
    };
  }
  if (isPhoneKnowledgeTool) {
    final status = switch (normalized) {
      'knowledge_search' => 'implemented_phone_knowledge_search',
      'knowledge_import_file' ||
      'knowledge_import_url' =>
        'implemented_phone_knowledge_import',
      'knowledge_index' ||
      'knowledge_reindex' =>
        'implemented_phone_knowledge_index',
      _ => 'implemented_phone_knowledge_store',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': [..._flutterRuntimeLayers, 'mobile-knowledge-store'],
      'native_layers': [],
      'requires_mobile_approval':
          _phoneKnowledgeMutationToolIds.contains(normalized),
      'implementation_status': status,
    };
  }
  if (isPhoneMediaArtifactTool) {
    final status = switch (normalized) {
      'image_render' => 'implemented_phone_svg_image_render',
      'image_generate_local_or_provider' =>
        'implemented_phone_svg_image_placeholder',
      _ => 'implemented_phone_audio_transcribe_payload',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _phoneMediaArtifactRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': status,
    };
  }
  if (isPhoneWorkflowTool) {
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _phoneWorkflowRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval':
          _phoneWorkflowMutationToolIds.contains(normalized),
      'implementation_status': 'implemented_phone_workflow_record',
    };
  }
  if (isPhoneArtifactWorkspaceTool) {
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': const {
        'artifact_file_write',
        'artifact_file_patch',
        'artifact_file_delete',
      }.contains(normalized),
      'implementation_status': 'implemented_phone_artifact_workspace',
    };
  }
  if (isPhoneArtifactHtmlTool) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'implemented_phone_artifact_html',
    };
  }
  if (isPhonePackageWebappTool) {
    final status = switch (normalized) {
      'package_install_plan' => 'implemented_phone_install_plan',
      'webapp_build' => 'implemented_phone_static_webapp_build_plan',
      _ => 'implemented_phone_research_report_export',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': normalized == 'webapp_build',
      'implementation_status': status,
    };
  }
  if (isPhoneArtifactGeneratorTool) {
    final status = switch (normalized) {
      'project_scaffold' => 'implemented_phone_artifact_scaffold',
      'doc_create' => 'implemented_phone_document_text',
      'slides_create' => 'implemented_phone_slide_outline',
      'slides_from_markdown' => 'implemented_phone_slide_outline',
      'chart_create' => 'implemented_phone_svg_chart',
      _ => 'implemented_phone_artifact_generator',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': status,
    };
  }
  if (isPhoneDocumentSlideMutationTool || isPhoneSlideExportTool) {
    final status = switch (normalized) {
      'doc_update' => 'implemented_phone_document_text',
      'slides_export' => 'implemented_phone_slide_export',
      _ => 'implemented_phone_slide_outline',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': isPhoneDocumentSlideMutationTool,
      'implementation_status': status,
    };
  }
  if (isPhoneJobTool) {
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval':
          normalized == 'job_cancel' || normalized == 'job_resume',
      'implementation_status': 'implemented_phone_job_record',
    };
  }
  if (isPhoneSheetTool) {
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': normalized == 'sheet_update',
      'implementation_status': normalized == 'sheet_export'
          ? 'implemented_phone_sheet_export'
          : 'implemented_phone_sheet_text',
    };
  }
  if (isPhoneExportTool) {
    final status = switch (normalized) {
      'artifact_zip' ||
      'static_site_export' ||
      'webapp_export_static' =>
        'implemented_phone_zip_base64',
      'artifact_export' => 'implemented_phone_artifact_export',
      'doc_export' => 'implemented_phone_document_export',
      'pdf_export' || 'doc_to_pdf' => 'pc_delegation_required_binary_export',
      _ => 'implemented_phone_artifact_export',
    };
    return {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': _flutterRuntimeLayers,
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': status,
    };
  }
  if (tagSet.contains('media') ||
      normalized.startsWith('audio_') ||
      normalized.startsWith('image_') ||
      normalized.startsWith('ocr_')) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': ['flutter', 'ios-swift', 'android-kotlin', 'provider'],
      'native_layers': [
        'ios:Swift media/photo permission bridge',
        'android:Kotlin media permission bridge',
      ],
      'requires_mobile_approval': true,
      'implementation_status': 'feasible_needs_picker_permission_or_provider',
    };
  }
  if (tagSet.contains('browser') && normalized != 'browser_open_url') {
    return const {
      'platforms': [],
      'runtime_layers': ['pc-browser-session'],
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'pc_browser_session_only',
    };
  }
  if (tagSet.any({
    'desktop',
    'computer',
    'computer_use',
    'workspace',
    'sandbox',
    'terminal',
    'agent_os',
  }.contains)) {
    return const {
      'platforms': [],
      'runtime_layers': ['pc-defaultspack-runtime'],
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'pc_only',
    };
  }
  if (tagSet.contains('agent')) {
    return const {
      'platforms': [],
      'runtime_layers': ['pc-agent-service'],
      'native_layers': [],
      'requires_mobile_approval': false,
      'implementation_status': 'pc_agent_runtime_only',
    };
  }
  if (tagSet.any({'connector', 'integration', 'external'}.contains)) {
    return const {
      'platforms': _defaultMobilePlatforms,
      'runtime_layers': ['flutter', 'oauth-or-provider'],
      'native_layers': [],
      'requires_mobile_approval': true,
      'implementation_status': 'feasible_needs_mobile_oauth_or_connector',
    };
  }
  return const {
    'platforms': [],
    'runtime_layers': ['pc-defaultspack-runtime'],
    'native_layers': [],
    'requires_mobile_approval': false,
    'implementation_status': 'pc_delegation_required',
  };
}

List<String> _mobilePlatformTags({
  required Iterable<String> platforms,
  required Iterable<String> runtimeLayers,
  required bool pcDelegated,
}) {
  final normalizedPlatforms =
      platforms.map((value) => value.trim().toLowerCase()).toSet();
  final normalizedLayers =
      runtimeLayers.map((value) => value.trim().toLowerCase()).toSet();
  final tags = <String>[
    if (normalizedLayers.contains('flutter') ||
        normalizedLayers.contains('dart'))
      mobileFlutterTag,
    if (normalizedPlatforms.contains('ios')) mobileIosTag,
    if (normalizedPlatforms.contains('android')) mobileAndroidTag,
    if (normalizedLayers.any((layer) => layer.contains('swift')))
      mobileSwiftNativeTag,
    if (normalizedLayers.any((layer) => layer.contains('kotlin')))
      mobileKotlinNativeTag,
    if (pcDelegated) mobilePcDelegatedTag,
  ];
  return _orderedStrings(tags);
}

String _openAiCatalogDescription(Map<String, dynamic> record) {
  final summary = '${record['summary'] ?? ''}'.trim();
  final location = '${record['execution_location'] ?? ''}'.trim();
  final route = '${record['execution_route'] ?? ''}'.trim();
  final platforms =
      (record['execution_platforms'] as List? ?? const []).join(', ');
  final runtimeLayers =
      (record['mobile_runtime_layers'] as List? ?? const []).join(', ');
  if (record['mobile_compatible'] == true) {
    return [
      if (summary.isNotEmpty) summary,
      'Execution: phone-local defaultspack-compatible runtime.',
      if (platforms.isNotEmpty) 'Mobile platforms: $platforms.',
      if (runtimeLayers.isNotEmpty) 'Runtime layers: $runtimeLayers.',
    ].join(' ');
  }
  final reason = '${record['unavailable_reason'] ?? ''}'.trim();
  if (record['callable'] == true && route == 'pc') {
    return [
      if (summary.isNotEmpty) summary,
      'Execution: same mobile tool surface, delegated to the connected PC defaultspack runtime.',
      if (runtimeLayers.isNotEmpty) 'Runtime layers: $runtimeLayers.',
      if (reason.isNotEmpty) 'Phone-local note: $reason',
    ].join(' ');
  }
  return [
    if (summary.isNotEmpty) summary,
    'Execution: not phone-executable; calling this function returns the unavailable reason.',
    if (location.isNotEmpty) 'Required runtime: $location.',
    if (platforms.isNotEmpty) 'Potential mobile platforms: $platforms.',
    if (runtimeLayers.isNotEmpty) 'Runtime layers: $runtimeLayers.',
    if (reason.isNotEmpty) 'Reason: $reason',
  ].join(' ');
}

bool _shouldExportCatalogRecordAsNativeTool(Map<String, dynamic> record) {
  if (record['mobile_compatible'] == true) return true;
  final tags =
      (record['manifest_tags'] as List? ?? record['tags'] as List? ?? const [])
          .map((tag) => '$tag')
          .toSet();
  if (tags.contains('agent')) return true;
  return tags.contains('tool') && !tags.contains('tool_registry');
}

String? _normalizePlatformFilter(Object? value) {
  final text = '${value ?? ''}'.trim().toLowerCase();
  if (text.isEmpty || text == 'all') return null;
  return switch (text) {
    'ios' || 'iphone' || 'ipad' => 'ios',
    'swift' || 'swift-native' || 'ios-swift' => 'swift',
    'android' => 'android',
    'kotlin' || 'kotlin-native' || 'android-kotlin' => 'kotlin',
    'flutter' || 'dart' => 'flutter',
    'pc' || 'desktop' || 'host' || 'defaultspack' => 'pc',
    _ => text,
  };
}

bool _recordMatchesPlatform(Map<String, dynamic> record, String? platform) {
  if (platform == null) return true;
  final mobile = record['mobile'] is Map ? record['mobile'] as Map : const {};
  final values = {
    '${record['execution_location'] ?? ''}',
    ...(record['execution_platforms'] as List? ?? const []).map((e) => '$e'),
    ...(record['mobile_runtime_layers'] as List? ?? const []).map((e) => '$e'),
    ...(record['native_layers'] as List? ?? const []).map((e) => '$e'),
    ...(record['tags'] as List? ?? const []).map((e) => '$e'),
    ...(mobile['platforms'] as List? ?? const []).map((e) => '$e'),
    ...(mobile['runtime_layers'] as List? ?? const []).map((e) => '$e'),
    ...(mobile['native_layers'] as List? ?? const []).map((e) => '$e'),
    ...(mobile['tags'] as List? ?? const []).map((e) => '$e'),
  }.map((value) => value.trim().toLowerCase()).toSet();
  if (platform == 'pc') {
    return values.contains('pc') ||
        values.contains('pc-delegated') ||
        values.contains(mobilePcDelegatedTag);
  }
  if (platform == 'flutter') {
    return values.contains('flutter') ||
        values.contains('dart') ||
        values.contains(mobileFlutterTag);
  }
  if (platform == 'ios') {
    return values.contains('ios') ||
        values.any((value) => value.contains('swift')) ||
        values.contains(mobileIosTag);
  }
  if (platform == 'swift') {
    return values.contains('ios-swift') ||
        values.contains('swift') ||
        values.contains('swift-native') ||
        values.contains(mobileSwiftNativeTag) ||
        values.any((value) => value.contains('swift ')) ||
        values.any((value) => value.contains(':swift'));
  }
  if (platform == 'android') {
    return values.contains('android') ||
        values.any((value) => value.contains('kotlin')) ||
        values.contains(mobileAndroidTag);
  }
  if (platform == 'kotlin') {
    return values.contains('android-kotlin') ||
        values.contains('kotlin') ||
        values.contains('kotlin-native') ||
        values.contains(mobileKotlinNativeTag) ||
        values.any((value) => value.contains('kotlin ')) ||
        values.any((value) => value.contains(':kotlin'));
  }
  return values.contains(platform);
}

String _recordSearchText(Map<String, dynamic> record) {
  final mobile = record['mobile'] is Map ? record['mobile'] as Map : const {};
  return [
    record['function_id'],
    record['tool_id'],
    record['requested_name'],
    record['summary'],
    record['execution_location'],
    ...(record['execution_platforms'] as List? ?? const []),
    ...(record['mobile_runtime_layers'] as List? ?? const []),
    ...(record['native_layers'] as List? ?? const []),
    ...(record['tags'] as List? ?? const []),
    ...(record['aliases'] as List? ?? const []),
    mobile['implementation_status'],
    ...(mobile['platforms'] as List? ?? const []),
    ...(mobile['runtime_layers'] as List? ?? const []),
    ...(mobile['native_layers'] as List? ?? const []),
  ].join(' ').toLowerCase();
}

Map<String, dynamic> _platformSummary(List<Map<String, dynamic>> records) {
  int count(String platform) => records
      .where((record) => _recordMatchesPlatform(record, platform))
      .length;
  return {
    'flutter': count('flutter'),
    'ios': count('ios'),
    'swift': count('swift'),
    'android': count('android'),
    'kotlin': count('kotlin'),
    'pc': count('pc'),
  };
}

List<String> _orderedStrings(Iterable<Object?> values) {
  final output = <String>[];
  final seen = <String>{};
  for (final value in values) {
    final text = '$value'.trim();
    if (text.isEmpty || seen.contains(text)) continue;
    seen.add(text);
    output.add(text);
  }
  return output;
}

String _currentMobilePlatform() {
  if (Platform.isIOS) return 'ios';
  if (Platform.isAndroid) return 'android';
  return Platform.operatingSystem;
}

String _unsupportedReasonForTags(String name, List<String> tags) {
  final normalized = name.trim().toLowerCase();
  final tagSet = tags.map((tag) => tag.trim().toLowerCase()).toSet();
  bool hasAny(Iterable<String> values) => values.any(tagSet.contains);

  if (normalized == 'media_clipboard_read' ||
      normalized == 'media_clipboard_write') {
    return 'このdefaultspack-compatible toolはiOS Swift/Android Kotlinのclipboard bridgeとモバイル承認UIでスマホ実装済みです。';
  }
  if (normalized == 'media_file_pick') {
    return 'このdefaultspack-compatible toolはiOS Swift/Android KotlinのOSファイルピッカーでスマホ実装済みです。';
  }
  if (normalized == 'media_screenshot') {
    return 'このdefaultspack-compatible toolはiOS Swift/Android KotlinのRumiアプリ画面captureでスマホ実装済みです。';
  }
  if (normalized == 'media_image_read') {
    return 'このdefaultspack-compatible toolはDartの画像ヘッダー解析でスマホ実装済みです。';
  }
  if (normalized == 'media_image_transform') {
    return 'このdefaultspack-compatible toolはiOS Swift/Android Kotlinの画像resize/encode bridgeでスマホ実装済みです。';
  }
  if (normalized == 'image_resize' || normalized == 'image_convert') {
    return 'このdefaultspack-compatible toolはiOS Swift/Android Kotlinの画像resize/encode bridgeで渡されたimage bytesのpayload-only変換にスマホ対応済みです。PC artifact path入出力はPC runtimeへ委譲してください。';
  }
  if (normalized == 'media_ocr') {
    return 'このdefaultspack-compatible toolはiOS Swift Vision / Android Kotlin ML KitのOCR bridgeでスマホ実装済みです。';
  }
  if (normalized == 'ocr_extract') {
    return 'このdefaultspack-compatible toolはiOS Swift Vision / Android Kotlin ML Kitで渡されたimage bytesのpayload-only OCRにスマホ対応済みです。PC artifact pathはPC runtimeへ委譲してください。';
  }
  if (normalized == 'media_doc_parse') {
    return 'このdefaultspack-compatible toolはDartでtext/markdown/json/csv/html/xml等のテキスト系document parseをスマホ実装済みです。PDF/docx等はPC runtimeへ委譲してください。';
  }
  if (normalized == 'media_pdf_parse') {
    return 'このdefaultspack-compatible toolはDartで渡されたPDF bytesのbest-effort text抽出にスマホ対応済みです。フルlayout/table抽出はPC runtimeへ委譲してください。';
  }
  if (normalized == 'pdf_extract') {
    return 'このdefaultspack-compatible toolはDartで渡されたPDF bytesのbest-effort text抽出にスマホ対応済みです。PC artifact pathはPC runtimeへ委譲してください。';
  }
  if (normalized == 'pdf_extract_tables') {
    return 'このdefaultspack-compatible toolはスマホでは空table fallbackまで対応済みです。フルPDF table抽出はPC runtimeへ委譲してください。';
  }
  if (normalized == 'artifact_preview') {
    return 'このdefaultspack-compatible toolはDartで渡されたtext/html/image/pdf payloadのpreviewにスマホ対応済みです。PC artifact pathやdirectory previewはPC runtimeへ委譲してください。';
  }
  if (normalized == 'html_preview' || normalized == 'pdf_preview') {
    return 'このdefaultspack-compatible toolはDartで渡されたHTML/PDF payloadのpreviewにスマホ対応済みです。PC artifact pathやscreenshot artifact生成はPC runtimeへ委譲してください。';
  }
  if (normalized == 'source_extract') {
    return 'このdefaultspack-compatible toolはDartで渡されたtext/html/url payloadの抽出にスマホ対応済みです。PC workspace pathはPC runtimeへ委譲してください。';
  }
  if (normalized == 'source_rank') {
    return 'このdefaultspack-compatible toolはDartで渡されたsource snippetsのterm frequency rankingにスマホ対応済みです。';
  }
  if (normalized == 'browser_extract_table') {
    return 'このdefaultspack-compatible toolはDartで渡されたHTML payloadのtable抽出にスマホ対応済みです。PC browser sessionやartifact pathはPC runtimeへ委譲してください。';
  }
  if (normalized == 'tts_generate' || normalized == 'tts_generate_local') {
    return 'このdefaultspack-compatible toolはFlutter/Dartでsilent WAV fallback payload生成にスマホ対応済みです。実音声TTS合成とPC artifact保存はPC/native runtimeへ委譲してください。';
  }
  if (hasAny(['desktop', 'computer_use', 'computer'])) {
    return 'このdefaultspack toolはPC側のdesktop/computer-use権限と画面状態に依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
  }
  if (hasAny(['browser'])) {
    return 'このdefaultspack toolはPC側のbrowser sessionに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
  }
  if (hasAny(['connector', 'integration', 'external'])) {
    return 'このdefaultspack toolはPC側のconnector認証、外部連携、または送信承認に依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
  }
  if (hasAny(['sandbox', 'agent_os', 'artifact_workspace']) ||
      normalized.endsWith('_exec')) {
    return 'このdefaultspack toolはPC側のagent OS、sandbox、artifact workspace、または実行承認に依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
  }
  if (hasAny([
    'artifact',
    'document',
    'spreadsheet',
    'presentation',
    'export',
    'media',
    'preview',
    'research',
    'source',
    'webapp',
    'workflow',
    'job',
  ])) {
    return 'このdefaultspack toolはPC側のartifact/media/workflow runtimeまたは外部providerに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
  }
  if (hasAny(['agent'])) {
    return 'このdefaultspack toolはPC側のagent serviceまたはagent queueに依存するため、このスマホ単体では実行できません。スマホ内agentではmobile-compatible toolだけを実行します。';
  }
  if (hasAny(['tool_registry', 'tool'])) {
    return 'このdefaultspack toolはPC側のtool registryまたはdefaultspack runtimeに依存するため、このスマホ単体では実行できません。mobile-compatible tag付きtoolだけをスマホ内で実行できます。';
  }
  return 'このdefaultspack toolはPC側defaultspack runtimeに依存するため、このスマホ単体では実行できません。PC接続時にPC側runtimeで実行してください。';
}

Map<String, dynamic> _asObjectSchema(Object? value) {
  if (value is Map<String, dynamic>) return value;
  if (value is Map) return value.map((key, value) => MapEntry('$key', value));
  return const {'type': 'object', 'additionalProperties': true};
}

Map<String, dynamic> _invokeArguments(Map<String, dynamic> args) {
  for (final key in ['arguments', 'args', 'input']) {
    final value = args[key];
    if (value is Map<String, dynamic>) return value;
    if (value is Map) return value.map((key, value) => MapEntry('$key', value));
    if (value is String && value.trim().isNotEmpty) {
      try {
        final decoded = jsonDecode(value);
        if (decoded is Map<String, dynamic>) return decoded;
        if (decoded is Map) {
          return decoded.map((key, value) => MapEntry('$key', value));
        }
      } catch (_) {
        return {'input': value.trim()};
      }
    }
  }
  return {
    for (final entry in args.entries)
      if (!{'tool_name', 'tool_id', 'name', 'context'}.contains(entry.key))
        entry.key: entry.value,
  };
}

Map<String, dynamic> _unsupportedToolRecord(String name) {
  final normalized = name.trim();
  final tags = _inferredDefaultspackTags(normalized);
  if (_isMobileConnectorDryRunTool(normalized)) {
    final mobile = _mobileRuntimeRecordForConnectorDryRun(normalized);
    return {
      'function_id': normalized,
      'tool_id': normalized,
      'aliases': const <String>[],
      'tags': _orderedStrings([
        ...tags,
        'connector',
        'dry_run',
        ...(mobile['tags'] as List? ?? const []),
      ]),
      'mobile_compatible': true,
      'mobile': mobile,
      'execution_location': mobile['execution_location'],
      'execution_platforms': mobile['platforms'],
      'mobile_runtime_layers': mobile['runtime_layers'],
      'native_layers': mobile['native_layers'],
      'requires_mobile_approval': mobile['requires_mobile_approval'],
      'unavailable_reason': '',
      'summary':
          'Defaultspack connector tool is available as a phone-local dry-run plan.',
      'parameters': {'type': 'object', 'additionalProperties': true},
    };
  }
  final reason = const MobileToolRuntime()._unsupportedReason(normalized);
  final mobile = _mobileRuntimeRecordForUnavailable(normalized, tags, reason);
  return {
    'function_id': normalized,
    'tool_id': normalized,
    'aliases': const <String>[],
    'tags': _orderedStrings([
      ...tags,
      ...(mobile['tags'] as List? ?? const []),
    ]),
    'mobile_compatible': false,
    'mobile': mobile,
    'execution_location': mobile['execution_location'],
    'execution_platforms': mobile['platforms'],
    'mobile_runtime_layers': mobile['runtime_layers'],
    'native_layers': mobile['native_layers'],
    'requires_mobile_approval': mobile['requires_mobile_approval'],
    'unavailable_reason': reason,
    'summary':
        'Defaultspack function is not executable in the phone-local runtime.',
    'parameters': {'type': 'object', 'additionalProperties': true},
  };
}

List<String> _inferredDefaultspackTags(String name) {
  final tags = <String>[];
  if (name.startsWith('agent_')) tags.add('agent');
  if (name.startsWith('tool_')) tags.add('tool');
  if (name.startsWith('browser_')) tags.addAll(['tool', 'browser']);
  if (name.startsWith('computer_')) tags.addAll(['tool', 'computer']);
  if (name.startsWith('media_')) tags.addAll(['tool', 'media']);
  if (name.startsWith('audio_')) tags.addAll(['tool', 'media', 'audio']);
  if (name.startsWith('image_')) tags.addAll(['tool', 'media', 'image']);
  if (name.startsWith('ocr_')) tags.addAll(['tool', 'media', 'ocr']);
  if (name.startsWith('coding_') || name.startsWith('sandbox_')) {
    tags.addAll(['tool', 'workspace']);
  }
  if (tags.isEmpty) tags.add('defaultspack');
  return tags.toSet().toList();
}

Map<String, dynamic> _decodeObject(String raw) {
  try {
    final decoded = jsonDecode(raw);
    if (decoded is Map<String, dynamic>) return decoded;
    if (decoded is Map) {
      return decoded.map((key, value) => MapEntry('$key', value));
    }
  } catch (_) {
    return const {};
  }
  return const {};
}

String _clampText(Object? value, int limit) {
  final text = '$value'.trim();
  if (text.isEmpty || text == 'null') return '';
  if (text.length <= limit) return text;
  return text.substring(0, limit);
}

String _normalizeMediaPickKind(Object? value) {
  final raw = '${value ?? ''}'.trim().toLowerCase();
  if (raw == 'image' ||
      raw == 'photo' ||
      raw == 'picture' ||
      raw == 'png' ||
      raw == 'jpg' ||
      raw == 'jpeg') {
    return 'image';
  }
  if (raw == 'audio' || raw == 'sound' || raw == 'voice') {
    return 'audio';
  }
  return 'file';
}

int _boundedMediaPickMaxBytes(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardMediaPickMaxBytes, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardMediaPickMaxBytes, parsed));
  }
  return _defaultMediaPickMaxBytes;
}

int _boundedScreenshotMaxBytes(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardScreenshotMaxBytes, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardScreenshotMaxBytes, parsed));
  }
  return _defaultScreenshotMaxBytes;
}

int _boundedScreenshotMaxDimension(Object? value) {
  if (value is num) {
    return math.max(320, math.min(_hardScreenshotMaxDimension, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(320, math.min(_hardScreenshotMaxDimension, parsed));
  }
  return _defaultScreenshotMaxDimension;
}

int _boundedImageReadMaxBytes(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardImageReadMaxBytes, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardImageReadMaxBytes, parsed));
  }
  return _defaultImageReadMaxBytes;
}

int _boundedImageTransformOutputMaxBytes(Object? value) {
  if (value is num) {
    return math.max(
      1,
      math.min(_hardImageTransformOutputMaxBytes, value.toInt()),
    );
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardImageTransformOutputMaxBytes, parsed));
  }
  return _defaultImageTransformOutputMaxBytes;
}

int _boundedImageTransformInputMaxBytes(Object? value) {
  if (value is num) {
    return math.max(
      1,
      math.min(_hardImageTransformInputMaxBytes, value.toInt()),
    );
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardImageTransformInputMaxBytes, parsed));
  }
  return _hardImageTransformInputMaxBytes;
}

int _boundedImageQuality(Object? value) {
  if (value is num) {
    return math.max(1, math.min(100, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) return math.max(1, math.min(100, parsed));
  return 90;
}

int _boundedOcrMaxBytes(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardOcrMaxBytes, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardOcrMaxBytes, parsed));
  }
  return _defaultOcrMaxBytes;
}

int? _positiveImageTransformDimension(Object? value) {
  if (value is num) {
    return math.max(
      1,
      math.min(_hardImageTransformMaxDimension, value.toInt()),
    );
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed == null) return null;
  return math.max(1, math.min(_hardImageTransformMaxDimension, parsed));
}

String _normalizeImageTransformFormat(Object? value) {
  final raw = '${value ?? ''}'.trim().toLowerCase();
  if (raw == 'jpg' || raw == 'jpeg' || raw == 'image/jpeg') return 'jpeg';
  if (raw == 'png' || raw == 'image/png' || raw.isEmpty) return 'png';
  return 'png';
}

_ImageTransformDimensions _imageTransformDimensions(Map<String, dynamic> args) {
  int? maxWidth;
  int? maxHeight;

  void apply(Object? width, Object? height, Object? maxDimension) {
    final dimension = _positiveImageTransformDimension(maxDimension);
    maxWidth = _positiveImageTransformDimension(width) ?? maxWidth;
    maxHeight = _positiveImageTransformDimension(height) ?? maxHeight;
    if (dimension != null) {
      maxWidth ??= dimension;
      maxHeight ??= dimension;
    }
  }

  final operations = args['operations'];
  if (operations is List) {
    for (final operation in operations) {
      if (operation is! Map) continue;
      final type =
          '${operation['type'] ?? operation['op'] ?? ''}'.trim().toLowerCase();
      if (type.isNotEmpty &&
          !{'resize', 'scale', 'thumbnail', 'fit', 'encode', 'convert'}
              .contains(type)) {
        continue;
      }
      apply(
        operation['width'] ?? operation['max_width'],
        operation['height'] ?? operation['max_height'],
        operation['max_dimension'] ?? operation['max_size'],
      );
    }
  }

  apply(
    args['width'] ?? args['max_width'],
    args['height'] ?? args['max_height'],
    args['max_dimension'],
  );

  final defaultDimension =
      _positiveImageTransformDimension(args['max_dimension']) ??
          _defaultImageTransformMaxDimension;
  maxWidth ??= defaultDimension;
  maxHeight ??= defaultDimension;
  return _ImageTransformDimensions(maxWidth: maxWidth, maxHeight: maxHeight);
}

Map<String, dynamic> _imageToolTransformArguments(
  Map<String, dynamic> args, {
  required String toolName,
}) {
  final normalized = Map<String, dynamic>.from(args);
  if (toolName == 'image_convert' &&
      _stringOrNull(normalized['format']) == null) {
    final inferred = _imageFormatFromPath(
      _stringOrNull(normalized['output_path']),
    );
    if (inferred != null) normalized['format'] = inferred;
  }
  return normalized;
}

String? _imageFormatFromPath(String? path) {
  if (path == null) return null;
  final lower = path.trim().toLowerCase();
  if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return 'jpeg';
  if (lower.endsWith('.png')) return 'png';
  return null;
}

List<String> _imageTransformOperationsApplied(
  Map<String, dynamic> args, {
  required String format,
  required _ImageTransformDimensions dimensions,
}) {
  final operations = <String>[];
  final raw = args['operations'];
  if (raw is List) {
    for (final operation in raw) {
      if (operation is! Map) continue;
      final type =
          '${operation['type'] ?? operation['op'] ?? ''}'.trim().toLowerCase();
      if (type.isNotEmpty && !operations.contains(type)) {
        operations.add(type);
      }
    }
  }
  if (dimensions.maxWidth != null || dimensions.maxHeight != null) {
    final label = [
      'resize_fit',
      if (dimensions.maxWidth != null) 'w${dimensions.maxWidth}',
      if (dimensions.maxHeight != null) 'h${dimensions.maxHeight}',
    ].join(':');
    if (!operations.contains(label)) operations.add(label);
  }
  final encode = 'encode:$format';
  if (!operations.contains(encode)) operations.add(encode);
  return operations;
}

String _mimeToImageFormat(String mimeType, String fallback) {
  final mime = mimeType.trim().toLowerCase();
  if (mime == 'image/jpeg') return 'jpeg';
  if (mime == 'image/png') return 'png';
  return fallback;
}

int _boundedDocParseMaxBytes(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardDocParseMaxBytes, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardDocParseMaxBytes, parsed));
  }
  return _defaultDocParseMaxBytes;
}

int _boundedDocParseMaxChars(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardDocParseMaxChars, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardDocParseMaxChars, parsed));
  }
  return _defaultDocParseMaxChars;
}

int _boundedPdfParseMaxBytes(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardPdfParseMaxBytes, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardPdfParseMaxBytes, parsed));
  }
  return _defaultPdfParseMaxBytes;
}

int _boundedPdfParseMaxChars(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardPdfParseMaxChars, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardPdfParseMaxChars, parsed));
  }
  return _defaultPdfParseMaxChars;
}

int _boundedSourceExtractMaxChars(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardSourceExtractMaxChars, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardSourceExtractMaxChars, parsed));
  }
  return _defaultSourceExtractMaxChars;
}

int _boundedArtifactPreviewMaxBytes(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardArtifactPreviewMaxBytes, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardArtifactPreviewMaxBytes, parsed));
  }
  return _defaultArtifactPreviewMaxBytes;
}

int _boundedArtifactPreviewMaxChars(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardArtifactPreviewMaxChars, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardArtifactPreviewMaxChars, parsed));
  }
  return _defaultArtifactPreviewMaxChars;
}

Map<String, int> _previewViewport(Object? value) {
  var width = 1280;
  var height = 720;
  if (value is Map) {
    final rawWidth = value['width'];
    final rawHeight = value['height'];
    if (rawWidth is num) width = rawWidth.toInt();
    if (rawHeight is num) height = rawHeight.toInt();
    final parsedWidth = int.tryParse('${rawWidth ?? ''}'.trim());
    final parsedHeight = int.tryParse('${rawHeight ?? ''}'.trim());
    if (parsedWidth != null) width = parsedWidth;
    if (parsedHeight != null) height = parsedHeight;
  }
  return {
    'width': math.max(1, width),
    'height': math.max(1, height),
  };
}

bool _boolArg(Object? value, {required bool fallback}) {
  if (value is bool) return value;
  final text = '${value ?? ''}'.trim().toLowerCase();
  if (text == 'true' || text == '1' || text == 'yes') return true;
  if (text == 'false' || text == '0' || text == 'no') return false;
  return fallback;
}

int _boundedHtmlTableMaxTables(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardHtmlTableMaxTables, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardHtmlTableMaxTables, parsed));
  }
  return _defaultHtmlTableMaxTables;
}

int _boundedHtmlTableMaxRows(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardHtmlTableMaxRows, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardHtmlTableMaxRows, parsed));
  }
  return _defaultHtmlTableMaxRows;
}

int _boundedTableIndex(Object? value, int tableCount) {
  if (tableCount <= 0) return 0;
  if (value is num) {
    return math.max(0, math.min(tableCount - 1, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(0, math.min(tableCount - 1, parsed));
  }
  return 0;
}

int _boundedTtsFallbackDurationMs(Object? value) {
  if (value is num) {
    return math.max(1, math.min(_hardTtsFallbackDurationMs, value.toInt()));
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(1, math.min(_hardTtsFallbackDurationMs, parsed));
  }
  return _defaultTtsFallbackDurationMs;
}

int _boundedTtsFallbackSampleRate(Object? value) {
  if (value is num) {
    return math.max(
      _minTtsFallbackSampleRate,
      math.min(_hardTtsFallbackSampleRate, value.toInt()),
    );
  }
  final parsed = int.tryParse('${value ?? ''}'.trim());
  if (parsed != null) {
    return math.max(
      _minTtsFallbackSampleRate,
      math.min(_hardTtsFallbackSampleRate, parsed),
    );
  }
  return _defaultTtsFallbackSampleRate;
}

Uint8List _silentWavBytes({
  required int durationMs,
  required int sampleRate,
}) {
  const channels = 1;
  const bitsPerSample = 16;
  final samples = math.max(1, (sampleRate * durationMs / 1000).round());
  final dataBytes = samples * channels * (bitsPerSample ~/ 8);
  final bytes = Uint8List(44 + dataBytes);
  final data = ByteData.sublistView(bytes);
  _writeAscii(bytes, 0, 'RIFF');
  data.setUint32(4, 36 + dataBytes, Endian.little);
  _writeAscii(bytes, 8, 'WAVE');
  _writeAscii(bytes, 12, 'fmt ');
  data.setUint32(16, 16, Endian.little);
  data.setUint16(20, 1, Endian.little);
  data.setUint16(22, channels, Endian.little);
  data.setUint32(24, sampleRate, Endian.little);
  data.setUint32(
      28, sampleRate * channels * (bitsPerSample ~/ 8), Endian.little);
  data.setUint16(32, channels * (bitsPerSample ~/ 8), Endian.little);
  data.setUint16(34, bitsPerSample, Endian.little);
  _writeAscii(bytes, 36, 'data');
  data.setUint32(40, dataBytes, Endian.little);
  return bytes;
}

void _writeAscii(Uint8List bytes, int offset, String value) {
  for (var index = 0; index < value.length; index += 1) {
    bytes[offset + index] = value.codeUnitAt(index);
  }
}

bool _looksLikeHttpUrl(String value) {
  final text = value.trim().toLowerCase();
  return text.startsWith('http://') || text.startsWith('https://');
}

String? _sourceArgumentAsInlineText(Object? value) {
  final text = _stringOrNull(value);
  if (text == null || _looksLikeHttpUrl(text)) return null;
  if (text.contains('\n') ||
      text.contains('\t') ||
      text.contains('<') ||
      text.split(RegExp(r'\s+')).length >= 8) {
    return text;
  }
  return null;
}

String? _extractHtmlTitle(String html) {
  final match =
      RegExp(r'<title[^>]*>(.*?)</title>', caseSensitive: false, dotAll: true)
          .firstMatch(html);
  final raw = match?.group(1);
  if (raw == null) return null;
  final title = _normalizeSourceText(_stripHtmlToText(raw));
  return title.isEmpty ? null : title;
}

bool _looksLikeHtmlFragment(String value) {
  final text = value.trim().toLowerCase();
  return text.contains('<html') ||
      text.contains('<body') ||
      text.contains('<table') ||
      RegExp(r'<[a-z][a-z0-9:-]*(\s|>|/)').hasMatch(text);
}

MobileToolResult _artifactPreviewTooLarge(int sizeBytes, int maxBytes) {
  return MobileToolResult(
    ok: false,
    summary: 'artifact payload is too large',
    output: jsonEncode({
      'status': 'error',
      'error': {
        'code': 'ARTIFACT_TOO_LARGE',
        'message': 'Artifact payload is larger than max_bytes.',
        'size_bytes': sizeBytes,
        'max_bytes': maxBytes,
        'execution_location': 'phone',
      },
    }),
  );
}

MobileToolResult _artifactPreviewText(
  String text, {
  required String kind,
  required String source,
  required int maxChars,
  Map<String, dynamic> metadata = const {},
}) {
  final normalized = kind == 'html' ? text.trim() : _normalizeSourceText(text);
  final truncated = normalized.length > maxChars;
  final content = truncated ? normalized.substring(0, maxChars) : normalized;
  final cleanMetadata = <String, dynamic>{
    for (final entry in metadata.entries)
      if (entry.value != null) entry.key: entry.value,
  };
  return MobileToolResult(
    ok: true,
    summary: 'preview $kind ${content.length} chars',
    output: jsonEncode({
      'status': 'ok',
      'data': {
        'kind': kind,
        'content': content,
        'text': kind == 'html' ? _stripHtmlToText(content).trim() : content,
        'truncated': truncated,
        'length': normalized.length,
        'returned_length': content.length,
        'metadata': {
          ...cleanMetadata,
          'payload_only': true,
          'source': source,
          'preview_mode': kind == 'html' ? 'html_payload' : 'text_payload',
        },
        'execution_location': 'phone',
        'runtime_layers': _flutterRuntimeLayers,
        'requires_mobile_approval': false,
      },
    }),
  );
}

String _normalizeSourceText(String text) {
  return text
      .replaceAll('\r\n', '\n')
      .replaceAll('\r', '\n')
      .replaceAll(RegExp(r'[ \t]+'), ' ')
      .replaceAll(RegExp(r'\s*\n\s*'), '\n')
      .replaceAll(RegExp(r'\n{3,}'), '\n\n')
      .trim();
}

List<String> _splitSourceRankTerms(String query) {
  return query
      .split(RegExp(r'\W+'))
      .map((term) => term.trim().toLowerCase())
      .where((term) => term.isNotEmpty)
      .toList();
}

String _sourceRankText(Object? source) {
  if (source is Map) {
    for (final key in const [
      'content',
      'text',
      'snippet',
      'summary',
      'title'
    ]) {
      final value = _stringOrNull(source[key]);
      if (value != null) return value;
    }
    return jsonEncode(source);
  }
  return '$source';
}

Object _sourceRankSourceObject(Object? source) {
  if (source is Map<String, dynamic>) return source;
  if (source is Map) {
    return source.map((key, value) => MapEntry('$key', value));
  }
  return {'content': '$source'};
}

String _decodePdfBytesForScan(Uint8List bytes) {
  return latin1.decode(bytes, allowInvalid: true);
}

_PdfTextExtraction _extractBestEffortPdfText(String raw) {
  final literalStrings = _extractPdfLiteralStrings(raw);
  final hexStrings = _extractPdfHexStrings(raw);
  final candidates = <String>[
    ...literalStrings,
    ...hexStrings,
  ].map(_cleanPdfExtractedText).where((text) => text.isNotEmpty).toList();
  if (candidates.isNotEmpty) {
    return _PdfTextExtraction(
      _dedupeAdjacentLines(candidates).join('\n').trim(),
      'literal_and_hex_strings',
    );
  }
  final fallback = _printablePdfScan(raw);
  return _PdfTextExtraction(fallback, 'latin1_printable_scan');
}

List<String> _extractPdfLiteralStrings(String raw) {
  final strings = <String>[];
  var index = 0;
  while (index < raw.length && strings.length < 5000) {
    if (raw.codeUnitAt(index) != 0x28) {
      index += 1;
      continue;
    }
    final buffer = StringBuffer();
    var depth = 1;
    var cursor = index + 1;
    var escaped = false;
    while (cursor < raw.length && depth > 0) {
      final code = raw.codeUnitAt(cursor);
      final char = raw[cursor];
      if (escaped) {
        if (char == 'n') {
          buffer.write('\n');
        } else if (char == 'r') {
          buffer.write('\r');
        } else if (char == 't') {
          buffer.write('\t');
        } else if (char == 'b') {
          buffer.write('\b');
        } else if (char == 'f') {
          buffer.write('\f');
        } else if (_isOctalDigit(code)) {
          var octal = char;
          var lookahead = cursor + 1;
          while (lookahead < raw.length &&
              octal.length < 3 &&
              _isOctalDigit(raw.codeUnitAt(lookahead))) {
            octal += raw[lookahead];
            lookahead += 1;
          }
          buffer.writeCharCode(
            int.parse(octal, radix: 8).clamp(0, 255).toInt(),
          );
          cursor = lookahead - 1;
        } else {
          buffer.write(char);
        }
        escaped = false;
      } else if (code == 0x5c) {
        escaped = true;
      } else if (code == 0x28) {
        depth += 1;
        buffer.write(char);
      } else if (code == 0x29) {
        depth -= 1;
        if (depth > 0) buffer.write(char);
      } else {
        buffer.write(char);
      }
      cursor += 1;
      if (buffer.length > 20000) break;
    }
    if (depth == 0) {
      final value = buffer.toString();
      if (_looksLikeHumanText(value)) strings.add(value);
      index = cursor;
    } else {
      index += 1;
    }
  }
  return strings;
}

List<String> _extractPdfHexStrings(String raw) {
  final output = <String>[];
  final pattern = RegExp(r'<([0-9A-Fa-f\s]{4,})>');
  for (final match in pattern.allMatches(raw).take(1000)) {
    final start = match.start;
    final end = match.end;
    if ((start > 0 && raw[start - 1] == '<') ||
        (end < raw.length && raw[end] == '>')) {
      continue;
    }
    final hex = match.group(1)?.replaceAll(RegExp(r'\s+'), '') ?? '';
    if (hex.length < 4 || hex.length.isOdd) continue;
    final bytes = <int>[];
    for (var index = 0; index + 1 < hex.length; index += 2) {
      bytes.add(int.parse(hex.substring(index, index + 2), radix: 16));
    }
    final decoded = _decodePdfHexBytes(Uint8List.fromList(bytes));
    if (_looksLikeHumanText(decoded)) output.add(decoded);
  }
  return output;
}

String _decodePdfHexBytes(Uint8List bytes) {
  if (bytes.length >= 2 && bytes[0] == 0xfe && bytes[1] == 0xff) {
    return _decodeUtf16(bytes.sublist(2), littleEndian: false);
  }
  if (bytes.length >= 2 && bytes[0] == 0xff && bytes[1] == 0xfe) {
    return _decodeUtf16(bytes.sublist(2), littleEndian: true);
  }
  final hasUtf16Nulls = bytes.length >= 4 &&
      bytes.length.isEven &&
      Iterable<int>.generate(math.min(bytes.length ~/ 2, 20))
              .where((index) => bytes[index * 2] == 0)
              .length >=
          2;
  if (hasUtf16Nulls) {
    return _decodeUtf16(bytes, littleEndian: false);
  }
  return utf8.decode(bytes, allowMalformed: true);
}

String _printablePdfScan(String raw) {
  final chunks = RegExp(r'[\x09\x0A\x0D\x20-\x7E]{4,}')
      .allMatches(raw)
      .map((match) => match.group(0) ?? '')
      .map(_cleanPdfExtractedText)
      .where((text) => text.length >= 4 && !_isPdfSyntaxNoise(text))
      .take(2000)
      .toList();
  return _dedupeAdjacentLines(chunks).join('\n').trim();
}

String _cleanPdfExtractedText(String value) {
  return value
      .replaceAll('\u0000', '')
      .replaceAll(RegExp(r'[ \t]+'), ' ')
      .replaceAll(RegExp(r'\s*\n\s*'), '\n')
      .trim();
}

List<String> _dedupeAdjacentLines(List<String> values) {
  final output = <String>[];
  for (final value in values) {
    final text = value.trim();
    if (text.isEmpty) continue;
    if (output.isNotEmpty && output.last == text) continue;
    output.add(text);
  }
  return output;
}

bool _looksLikeHumanText(String value) {
  final text = value.trim();
  if (text.length < 2) return false;
  final printable = text.codeUnits.where((code) {
    return code == 0x09 || code == 0x0a || code == 0x0d || code >= 0x20;
  }).length;
  return printable / math.max(1, text.length) > 0.8 && !_isPdfSyntaxNoise(text);
}

bool _isPdfSyntaxNoise(String value) {
  final text = value.trim();
  if (text.isEmpty) return true;
  if (RegExp(r'^(obj|endobj|stream|endstream|xref|trailer|startxref)$')
      .hasMatch(text)) {
    return true;
  }
  if (RegExp(r'^/?[A-Za-z0-9]+\s+\d+(\.\d+)?$').hasMatch(text)) return true;
  if (RegExp(r'^[<>{}\[\]/\d\s\.\-]+$').hasMatch(text)) return true;
  return false;
}

bool _isOctalDigit(int code) => code >= 0x30 && code <= 0x37;

_DocumentPayload? _extractDocumentPayload(Map<String, dynamic> args) {
  return _documentPayloadFromMap(args, source: 'arguments');
}

_DocumentPayload? _documentPayloadFromMap(
  Map<dynamic, dynamic> value, {
  required String source,
  int depth = 0,
}) {
  if (depth > 4) return null;
  final name = _stringOrNull(
    value['name'] ?? value['filename'] ?? value['file_name'] ?? value['path'],
  );
  final mimeType = _stringOrNull(
    value['mime_type'] ?? value['mimeType'] ?? value['content_type'],
  );
  final format = _stringOrNull(value['format'] ?? value['extension']);

  for (final key in const [
    'base64',
    'pdf_base64',
    'document_base64',
    'data_base64',
    'file_base64',
  ]) {
    final encoded = _stringOrNull(value[key]);
    if (encoded != null) {
      return _documentPayloadFromEncoded(
        encoded,
        name: name,
        mimeType: mimeType,
        format: format,
        source: '$source.$key',
      );
    }
  }

  final dataUrl = _stringOrNull(value['data_url']);
  if (dataUrl != null) {
    return _documentPayloadFromEncoded(
      dataUrl,
      name: name,
      mimeType: mimeType,
      format: format,
      source: '$source.data_url',
    );
  }

  for (final key in const ['file', 'document', 'doc', 'result', 'output']) {
    final nested = value[key];
    if (nested is Map) {
      final found = _documentPayloadFromMap(
        nested,
        source: '$source.$key',
        depth: depth + 1,
      );
      if (found != null) {
        return found.withFallbacks(
          name: name,
          mimeType: mimeType,
          format: format,
        );
      }
    }
  }

  for (final key in const ['text', 'content', 'markdown', 'html']) {
    final text = _stringOrNull(value[key]);
    if (text == null) continue;
    final encoding = '${value['encoding'] ?? ''}'.trim().toLowerCase();
    if (encoding == 'base64' || text.startsWith('data:')) {
      return _documentPayloadFromEncoded(
        text,
        name: name,
        mimeType: mimeType,
        format: format ?? (key == 'html' ? 'html' : null),
        source: '$source.$key',
      );
    }
    return _DocumentPayload(
      text: text,
      name: name,
      mimeType: mimeType,
      format: format ?? (key == 'html' ? 'html' : null),
      source: '$source.$key',
    );
  }

  return null;
}

_DocumentPayload _documentPayloadFromEncoded(
  String raw, {
  required String? name,
  required String? mimeType,
  required String? format,
  required String source,
}) {
  final text = raw.trim();
  if (text.startsWith('data:')) {
    final comma = text.indexOf(',');
    if (comma <= 5) {
      throw const FormatException('Invalid data URL');
    }
    final meta = text.substring(5, comma);
    final data = text.substring(comma + 1);
    final parts = meta.split(';').where((part) => part.isNotEmpty).toList();
    final dataMime = parts.isNotEmpty && !parts.first.contains('=')
        ? parts.first.trim()
        : null;
    final isBase64 = parts.any((part) => part.toLowerCase() == 'base64');
    if (!isBase64) {
      return _DocumentPayload(
        text: Uri.decodeComponent(data),
        name: name,
        mimeType: mimeType ?? dataMime,
        format: format,
        source: source,
      );
    }
    return _DocumentPayload(
      bytes: base64Decode(_normalizeBase64ForDecode(data)),
      name: name,
      mimeType: mimeType ?? dataMime,
      format: format,
      source: source,
    );
  }
  return _DocumentPayload(
    bytes: base64Decode(_normalizeBase64ForDecode(text)),
    name: name,
    mimeType: mimeType,
    format: format,
    source: source,
  );
}

String _normalizeBase64ForDecode(String value) {
  var text = value
      .replaceAll(RegExp(r'\s+'), '')
      .replaceAll('-', '+')
      .replaceAll('_', '/');
  final padding = text.length % 4;
  if (padding != 0) text = text.padRight(text.length + (4 - padding), '=');
  return text;
}

String _normalizeDocumentFormat(
  Object? value, {
  required String mimeType,
  required String name,
}) {
  final explicit = _stringOrNull(value);
  if (explicit != null) return _canonicalDocumentFormat(explicit);
  final mime = mimeType.trim().toLowerCase();
  if (mime.startsWith('text/')) {
    final subtype = mime.substring('text/'.length).split(';').first;
    return _canonicalDocumentFormat(subtype.isEmpty ? 'txt' : subtype);
  }
  const mimeFormats = {
    'application/json': 'json',
    'application/ld+json': 'json',
    'application/xml': 'xml',
    'application/xhtml+xml': 'html',
    'application/javascript': 'javascript',
    'application/x-javascript': 'javascript',
    'application/x-yaml': 'yaml',
    'application/yaml': 'yaml',
    'text/yaml': 'yaml',
    'text/x-yaml': 'yaml',
    'application/pdf': 'pdf',
    'application/msword': 'doc',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document':
        'docx',
  };
  if (mimeFormats.containsKey(mime)) return mimeFormats[mime]!;
  final filename = name.trim();
  final dot = filename.lastIndexOf('.');
  if (dot >= 0 && dot + 1 < filename.length) {
    return _canonicalDocumentFormat(filename.substring(dot + 1));
  }
  return '';
}

String _canonicalDocumentFormat(String value) {
  final format = value.trim().toLowerCase().replaceFirst(RegExp(r'^\.'), '');
  return switch (format) {
    'text' => 'txt',
    'mdown' => 'markdown',
    'md' => 'markdown',
    'htm' => 'html',
    'xhtml' => 'html',
    'yml' => 'yaml',
    'js' => 'javascript',
    _ => format,
  };
}

String? _unsupportedPhoneDocumentReason({
  required String format,
  required String? mimeType,
  required String? name,
  required bool explicitText,
}) {
  if (explicitText) return null;
  final mime = (mimeType ?? '').trim().toLowerCase();
  if (format.isEmpty &&
      (mime.isEmpty ||
          mime == 'application/octet-stream' ||
          mime == 'binary/octet-stream')) {
    return null;
  }
  if (_phoneTextDocumentFormats.contains(format)) return null;
  if (mime.startsWith('text/')) return null;
  if (mime.contains('json') ||
      mime.contains('xml') ||
      mime.contains('yaml') ||
      mime.contains('csv') ||
      mime.contains('javascript')) {
    return null;
  }
  final label = [
    if (format.isNotEmpty) format,
    if (mime.isNotEmpty) mime,
    if ((name ?? '').trim().isNotEmpty) name,
  ].join(' / ');
  return 'Phone-local media_doc_parse supports UTF text documents only. $label requires PC/provider parsing.';
}

_DecodedDocumentText _decodeDocumentBytes(Uint8List bytes, int maxBytes) {
  if (bytes.length > maxBytes) {
    throw const _UnsupportedDocumentEncoding(
      'Document content is larger than max_bytes.',
    );
  }
  if (bytes.isEmpty) return const _DecodedDocumentText('', 'utf-8');
  if (bytes.length >= 2 && bytes[0] == 0xff && bytes[1] == 0xfe) {
    return _DecodedDocumentText(
        _decodeUtf16(bytes.sublist(2), littleEndian: true), 'utf-16le');
  }
  if (bytes.length >= 2 && bytes[0] == 0xfe && bytes[1] == 0xff) {
    return _DecodedDocumentText(
        _decodeUtf16(bytes.sublist(2), littleEndian: false), 'utf-16be');
  }
  if (bytes.length >= 3 &&
      bytes[0] == 0xef &&
      bytes[1] == 0xbb &&
      bytes[2] == 0xbf) {
    return _DecodedDocumentText(
        utf8.decode(bytes.sublist(3), allowMalformed: true), 'utf-8-bom');
  }
  if (_looksBinaryDocumentBytes(bytes)) {
    throw const _UnsupportedDocumentEncoding(
      'Document bytes look binary, not UTF text.',
    );
  }
  try {
    return _DecodedDocumentText(utf8.decode(bytes), 'utf-8');
  } on FormatException {
    return _DecodedDocumentText(
        utf8.decode(bytes, allowMalformed: true), 'utf-8-lossy');
  }
}

String _decodeUtf16(Uint8List bytes, {required bool littleEndian}) {
  final codes = <int>[];
  for (var index = 0; index + 1 < bytes.length; index += 2) {
    final code = littleEndian
        ? bytes[index] | (bytes[index + 1] << 8)
        : (bytes[index] << 8) | bytes[index + 1];
    codes.add(code);
  }
  return String.fromCharCodes(codes);
}

bool _looksBinaryDocumentBytes(Uint8List bytes) {
  final scanLength = math.min(bytes.length, 4096);
  if (scanLength == 0) return false;
  var suspicious = 0;
  for (var index = 0; index < scanLength; index += 1) {
    final byte = bytes[index];
    final allowedControl =
        byte == 0x09 || byte == 0x0a || byte == 0x0d || byte == 0x1b;
    if (byte == 0 || (byte < 0x08 && !allowedControl)) suspicious += 1;
  }
  return suspicious > math.max(4, scanLength ~/ 20);
}

String _parsePhoneDocumentText(
  String text, {
  required String format,
  required bool stripHtml,
}) {
  var output = text.replaceAll('\r\n', '\n').replaceAll('\r', '\n');
  if (stripHtml && format == 'html') {
    output = _stripHtmlToText(output);
  }
  return output.trim();
}

String _stripHtmlToText(String html) {
  var text = html
      .replaceAll(
          RegExp(r'<script\b[^>]*>.*?</script>',
              caseSensitive: false, dotAll: true),
          ' ')
      .replaceAll(
          RegExp(r'<style\b[^>]*>.*?</style>',
              caseSensitive: false, dotAll: true),
          ' ')
      .replaceAll(RegExp(r'<br\s*/?>', caseSensitive: false), '\n')
      .replaceAll(RegExp(r'</p\s*>', caseSensitive: false), '\n')
      .replaceAll(RegExp(r'<[^>]+>'), ' ');
  const entities = {
    '&amp;': '&',
    '&lt;': '<',
    '&gt;': '>',
    '&quot;': '"',
    '&#39;': "'",
    '&nbsp;': ' ',
  };
  for (final entry in entities.entries) {
    text = text.replaceAll(entry.key, entry.value);
  }
  return text
      .replaceAll(RegExp(r'[ \t]+'), ' ')
      .replaceAll(RegExp(r'\n{3,}'), '\n\n');
}

_HtmlTablePayload? _extractHtmlTablePayload(Map<String, dynamic> args) {
  final inline = _stringOrNull(args['html']) ??
      _stringOrNull(args['content']) ??
      _sourceArgumentAsInlineText(args['source']);
  if (inline != null) {
    return _HtmlTablePayload(html: inline, source: 'arguments.inline');
  }
  try {
    final payload = _extractDocumentPayload(args);
    if (payload == null) return null;
    final text = payload.text ??
        (payload.bytes == null
            ? null
            : _decodeDocumentBytes(payload.bytes!, payload.bytes!.length).text);
    if (text == null || text.trim().isEmpty) return null;
    return _HtmlTablePayload(html: text, source: payload.source);
  } catch (_) {
    return null;
  }
}

List<List<List<String>>> _extractHtmlTables(
  String html, {
  required int maxTables,
  required int maxRows,
}) {
  final cleaned = html
      .replaceAll(
        RegExp(r'<!--.*?-->', caseSensitive: false, dotAll: true),
        ' ',
      )
      .replaceAll(
        RegExp(
          r'<script\b[^>]*>.*?</script>',
          caseSensitive: false,
          dotAll: true,
        ),
        ' ',
      )
      .replaceAll(
        RegExp(
          r'<style\b[^>]*>.*?</style>',
          caseSensitive: false,
          dotAll: true,
        ),
        ' ',
      );
  final tables = <List<List<String>>>[];
  final tablePattern = RegExp(
    r'<table\b[^>]*>(.*?)</table>',
    caseSensitive: false,
    dotAll: true,
  );
  for (final tableMatch in tablePattern.allMatches(cleaned)) {
    if (tables.length >= maxTables) break;
    final rows = <List<String>>[];
    final rowPattern = RegExp(
      r'<tr\b[^>]*>(.*?)</tr>',
      caseSensitive: false,
      dotAll: true,
    );
    for (final rowMatch in rowPattern.allMatches(tableMatch.group(1) ?? '')) {
      if (rows.length >= maxRows) break;
      final cells = <String>[];
      final cellPattern = RegExp(
        r'<t[dh]\b[^>]*>(.*?)</t[dh]>',
        caseSensitive: false,
        dotAll: true,
      );
      for (final cellMatch in cellPattern.allMatches(rowMatch.group(1) ?? '')) {
        final cell = _normalizeSourceText(
          _stripHtmlToText(cellMatch.group(1) ?? ''),
        );
        cells.add(cell);
      }
      if (cells.isNotEmpty) rows.add(cells);
    }
    if (rows.isNotEmpty) tables.add(rows);
  }
  return tables;
}

String? _stringOrNull(Object? value) {
  if (value == null) return null;
  final text = '$value'.trim();
  if (text.isEmpty || text == 'null') return null;
  return text;
}

String? _extractImageBase64(Object? value, [int depth = 0]) {
  if (depth > 4 || value == null) return null;
  if (value is String) {
    final text = value.trim();
    if (text.isEmpty) return null;
    if (text.startsWith('data:image/') || _looksLikeBase64(text)) return text;
    try {
      final decoded = jsonDecode(text);
      return _extractImageBase64(decoded, depth + 1);
    } catch (_) {
      return null;
    }
  }
  if (value is Map) {
    for (final key in const [
      'base64',
      'image_base64',
      'data_base64',
      'data_url',
      'content',
      'data',
    ]) {
      final found = _extractImageBase64(value[key], depth + 1);
      if (found != null) return found;
    }
    for (final key in const ['image', 'file', 'result', 'output']) {
      final found = _extractImageBase64(value[key], depth + 1);
      if (found != null) return found;
    }
  }
  return null;
}

String _stripDataUrlPrefix(String value) {
  final text = value.trim();
  final comma = text.indexOf(',');
  if (text.startsWith('data:') && comma >= 0) {
    return text.substring(comma + 1).trim();
  }
  return text;
}

bool _looksLikeBase64(String value) {
  final text = _stripDataUrlPrefix(value).replaceAll(RegExp(r'\s+'), '');
  if (text.length < 16) return false;
  return RegExp(r'^[A-Za-z0-9+/=_-]+$').hasMatch(text);
}

String? _phoneImageArtifactOutputPath(
  Object? value, {
  required String defaultPath,
}) {
  final path = _normalizePhoneArtifactPath(value ?? defaultPath);
  if (path == null) return null;
  final ext = _phoneArtifactExtension(path);
  return ext.isEmpty ? '$path.svg' : path;
}

_PhoneImageRenderDimensions _imageRenderDimensions(Map<String, dynamic> args) {
  final viewport = args['viewport'];
  final viewportMap = viewport is Map ? viewport : const {};
  final width = _boundedInt(
    args['width'] ?? viewportMap['width'],
    1024,
    1,
    4096,
  );
  final height = _boundedInt(
    args['height'] ?? viewportMap['height'],
    640,
    1,
    4096,
  );
  return _PhoneImageRenderDimensions(width: width, height: height);
}

String _phoneRenderedSvg({
  required String title,
  required String subtitle,
  required int width,
  required int height,
}) {
  final safeSubtitle = _escapeHtmlText(_clampText(subtitle, 120));
  final titleSize = math.max(16, math.min(42, (width / 18).round()));
  final subtitleSize = math.max(12, math.min(20, (width / 48).round()));
  final padding = math.max(24, math.min(72, (width / 16).round()));
  final lines = _wrapSvgText(title, maxChars: math.max(16, width ~/ 18));
  final buffer = StringBuffer()
    ..writeln(
      '<svg xmlns="http://www.w3.org/2000/svg" width="$width" height="$height" viewBox="0 0 $width $height" role="img">',
    )
    ..writeln('<rect width="100%" height="100%" fill="#f8fafc"/>')
    ..writeln(
      '<rect x="${padding / 2}" y="${padding / 2}" width="${width - padding}" height="${height - padding}" fill="#ffffff" stroke="#d1d5db" stroke-width="2" rx="18"/>',
    )
    ..writeln(
      '<text x="$padding" y="${padding + titleSize}" font-family="Arial, sans-serif" font-size="$titleSize" fill="#111827" font-weight="700">$safeSubtitle</text>',
    );
  var y = padding + titleSize + 48;
  for (final line in lines.take(8)) {
    buffer.writeln(
      '<text x="$padding" y="$y" font-family="Arial, sans-serif" font-size="$titleSize" fill="#111827">${_escapeHtmlText(line)}</text>',
    );
    y += titleSize + 12;
  }
  if (lines.length > 8) {
    buffer.writeln(
      '<text x="$padding" y="$y" font-family="Arial, sans-serif" font-size="$subtitleSize" fill="#6b7280">...</text>',
    );
  }
  buffer
    ..writeln(
      '<text x="$padding" y="${height - padding}" font-family="Arial, sans-serif" font-size="$subtitleSize" fill="#6b7280">Generated on this phone</text>',
    )
    ..writeln('</svg>');
  return buffer.toString();
}

List<String> _wrapSvgText(String value, {required int maxChars}) {
  final words = value.replaceAll(RegExp(r'\s+'), ' ').trim().split(' ');
  final lines = <String>[];
  final current = StringBuffer();
  for (final word in words) {
    if (word.isEmpty) continue;
    if (current.isEmpty) {
      current.write(word);
      continue;
    }
    if (current.length + word.length + 1 > maxChars) {
      lines.add(current.toString());
      current
        ..clear()
        ..write(word);
    } else {
      current.write(' $word');
    }
  }
  if (current.isNotEmpty) lines.add(current.toString());
  return lines.isEmpty ? ['Rumi artifact render'] : lines;
}

Uint8List? _audioPayloadBytes(Map<String, dynamic> args) {
  final encoded = _extractAudioBase64(args);
  if (encoded == null) return null;
  try {
    return base64Decode(_stripDataUrlPrefix(encoded));
  } catch (_) {
    return null;
  }
}

String? _extractAudioBase64(Object? value, [int depth = 0]) {
  if (depth > 4 || value == null) return null;
  if (value is String) {
    final text = value.trim();
    if (text.isEmpty) return null;
    if (text.startsWith('data:audio/') || _looksLikeBase64(text)) return text;
    try {
      final decoded = jsonDecode(text);
      return _extractAudioBase64(decoded, depth + 1);
    } catch (_) {
      return null;
    }
  }
  if (value is Map) {
    for (final key in const [
      'audio_base64',
      'base64',
      'data_base64',
      'data_url',
      'content',
      'data',
    ]) {
      final found = _extractAudioBase64(value[key], depth + 1);
      if (found != null) return found;
    }
    for (final key in const ['audio', 'file', 'result', 'output']) {
      final found = _extractAudioBase64(value[key], depth + 1);
      if (found != null) return found;
    }
  }
  return null;
}

class _PhoneImageRenderDimensions {
  const _PhoneImageRenderDimensions({
    required this.width,
    required this.height,
  });

  final int width;
  final int height;
}

_MobileImageMetadata? _readImageHeader(Uint8List bytes) {
  if (_isPng(bytes)) {
    return _MobileImageMetadata(
      width: _readUint32Be(bytes, 16),
      height: _readUint32Be(bytes, 20),
      format: 'png',
      mimeType: 'image/png',
    );
  }
  if (_isGif(bytes)) {
    return _MobileImageMetadata(
      width: _readUint16Le(bytes, 6),
      height: _readUint16Le(bytes, 8),
      format: 'gif',
      mimeType: 'image/gif',
    );
  }
  if (_isBmp(bytes)) {
    return _MobileImageMetadata(
      width: _readInt32Le(bytes, 18).abs(),
      height: _readInt32Le(bytes, 22).abs(),
      format: 'bmp',
      mimeType: 'image/bmp',
    );
  }
  final jpeg = _readJpegHeader(bytes);
  if (jpeg != null) return jpeg;
  final webp = _readWebpHeader(bytes);
  if (webp != null) return webp;
  return null;
}

bool _isPng(Uint8List bytes) =>
    bytes.length >= 24 &&
    bytes[0] == 0x89 &&
    bytes[1] == 0x50 &&
    bytes[2] == 0x4e &&
    bytes[3] == 0x47 &&
    bytes[4] == 0x0d &&
    bytes[5] == 0x0a &&
    bytes[6] == 0x1a &&
    bytes[7] == 0x0a;

bool _isGif(Uint8List bytes) =>
    bytes.length >= 10 &&
    bytes[0] == 0x47 &&
    bytes[1] == 0x49 &&
    bytes[2] == 0x46 &&
    bytes[3] == 0x38;

bool _isBmp(Uint8List bytes) =>
    bytes.length >= 26 && bytes[0] == 0x42 && bytes[1] == 0x4d;

_MobileImageMetadata? _readJpegHeader(Uint8List bytes) {
  if (bytes.length < 4 || bytes[0] != 0xff || bytes[1] != 0xd8) return null;
  var offset = 2;
  while (offset + 4 < bytes.length) {
    while (offset < bytes.length && bytes[offset] == 0xff) {
      offset += 1;
    }
    if (offset >= bytes.length) return null;
    final marker = bytes[offset];
    offset += 1;
    if (marker == 0xd9 || marker == 0xda) return null;
    if (offset + 2 > bytes.length) return null;
    final length = _readUint16Be(bytes, offset);
    if (length < 2 || offset + length > bytes.length) return null;
    if (_jpegSofMarkers.contains(marker)) {
      if (offset + 7 > bytes.length) return null;
      return _MobileImageMetadata(
        width: _readUint16Be(bytes, offset + 5),
        height: _readUint16Be(bytes, offset + 3),
        format: 'jpeg',
        mimeType: 'image/jpeg',
      );
    }
    offset += length;
  }
  return null;
}

const _jpegSofMarkers = <int>{
  0xc0,
  0xc1,
  0xc2,
  0xc3,
  0xc5,
  0xc6,
  0xc7,
  0xc9,
  0xca,
  0xcb,
  0xcd,
  0xce,
  0xcf,
};

_MobileImageMetadata? _readWebpHeader(Uint8List bytes) {
  if (bytes.length < 30 ||
      !_asciiAt(bytes, 0, 'RIFF') ||
      !_asciiAt(bytes, 8, 'WEBP')) {
    return null;
  }
  if (_asciiAt(bytes, 12, 'VP8X')) {
    return _MobileImageMetadata(
      width: 1 + _readUint24Le(bytes, 24),
      height: 1 + _readUint24Le(bytes, 27),
      format: 'webp',
      mimeType: 'image/webp',
    );
  }
  if (_asciiAt(bytes, 12, 'VP8 ') && bytes.length >= 30) {
    return _MobileImageMetadata(
      width: _readUint16Le(bytes, 26) & 0x3fff,
      height: _readUint16Le(bytes, 28) & 0x3fff,
      format: 'webp',
      mimeType: 'image/webp',
    );
  }
  if (_asciiAt(bytes, 12, 'VP8L') && bytes.length >= 25 && bytes[20] == 0x2f) {
    final b1 = bytes[21];
    final b2 = bytes[22];
    final b3 = bytes[23];
    final b4 = bytes[24];
    return _MobileImageMetadata(
      width: 1 + (((b2 & 0x3f) << 8) | b1),
      height: 1 + (((b4 & 0x0f) << 10) | (b3 << 2) | ((b2 & 0xc0) >> 6)),
      format: 'webp',
      mimeType: 'image/webp',
    );
  }
  return null;
}

bool _asciiAt(Uint8List bytes, int offset, String text) {
  if (offset < 0 || offset + text.length > bytes.length) return false;
  for (var i = 0; i < text.length; i += 1) {
    if (bytes[offset + i] != text.codeUnitAt(i)) return false;
  }
  return true;
}

int _readUint16Be(Uint8List bytes, int offset) =>
    (bytes[offset] << 8) | bytes[offset + 1];

int _readUint16Le(Uint8List bytes, int offset) =>
    bytes[offset] | (bytes[offset + 1] << 8);

int _readUint24Le(Uint8List bytes, int offset) =>
    bytes[offset] | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16);

int _readUint32Be(Uint8List bytes, int offset) =>
    (bytes[offset] << 24) |
    (bytes[offset + 1] << 16) |
    (bytes[offset + 2] << 8) |
    bytes[offset + 3];

int _readInt32Le(Uint8List bytes, int offset) {
  final value = bytes[offset] |
      (bytes[offset + 1] << 8) |
      (bytes[offset + 2] << 16) |
      (bytes[offset + 3] << 24);
  return value >= 0x80000000 ? value - 0x100000000 : value;
}

class _MobileImageMetadata {
  const _MobileImageMetadata({
    required this.width,
    required this.height,
    required this.format,
    required this.mimeType,
  });

  final int width;
  final int height;
  final String format;
  final String mimeType;
}

class _ImageTransformDimensions {
  const _ImageTransformDimensions({
    required this.maxWidth,
    required this.maxHeight,
  });

  final int? maxWidth;
  final int? maxHeight;
}

class _DocumentPayload {
  const _DocumentPayload({
    this.text,
    this.bytes,
    this.name,
    this.mimeType,
    this.format,
    required this.source,
  });

  final String? text;
  final Uint8List? bytes;
  final String? name;
  final String? mimeType;
  final String? format;
  final String source;

  int get sizeBytes => bytes?.length ?? utf8.encode(text ?? '').length;

  _DocumentPayload withFallbacks({
    required String? name,
    required String? mimeType,
    required String? format,
  }) {
    return _DocumentPayload(
      text: text,
      bytes: bytes,
      name: this.name ?? name,
      mimeType: this.mimeType ?? mimeType,
      format: this.format ?? format,
      source: source,
    );
  }
}

class _HtmlTablePayload {
  const _HtmlTablePayload({
    required this.html,
    required this.source,
  });

  final String html;
  final String source;
}

class _DecodedDocumentText {
  const _DecodedDocumentText(this.text, this.encoding);

  final String text;
  final String encoding;
}

class _UnsupportedDocumentEncoding implements Exception {
  const _UnsupportedDocumentEncoding(this.message);

  final String message;
}

class _PdfTextExtraction {
  const _PdfTextExtraction(this.text, this.method);

  final String text;
  final String method;
}

const _phoneTextDocumentFormats = <String>{
  '',
  'txt',
  'text',
  'markdown',
  'json',
  'jsonl',
  'csv',
  'tsv',
  'html',
  'xml',
  'yaml',
  'toml',
  'ini',
  'log',
  'javascript',
  'css',
  'svg',
};

final List<Map<String, dynamic>> _mobileTodos = [];
final Map<String, dynamic> _mobileTaskBoard = {
  'board_id': 'mobile-default',
  'title': 'Mobile Task Board',
  'columns': _taskBoardDefaultColumns,
};
final List<Map<String, dynamic>> _mobileTaskCards = [];
final List<Map<String, dynamic>> _mobileAgentPlans = [];
final Map<String, Map<String, dynamic>> _mobileArtifactFiles = {};
final Map<String, Map<String, dynamic>> _mobileConsents = {};
final Map<String, Map<String, dynamic>> _mobileWorkflows = {};
final Map<String, Map<String, dynamic>> _mobileWorkflowRuns = {};
final Map<String, List<Map<String, dynamic>>> _mobileWorkflowEvents = {};
final Map<String, Map<String, dynamic>> _mobileJobs = {};
final Map<String, List<Map<String, dynamic>>> _mobileJobEvents = {};
int _mobileToolIdSequence = 0;

String _nextToolId(String prefix) {
  _mobileToolIdSequence += 1;
  return '${prefix}_${DateTime.now().microsecondsSinceEpoch}_$_mobileToolIdSequence';
}

String _argId(Map<String, dynamic> args, String primaryKey) {
  return '${args[primaryKey] ?? args['id'] ?? ''}'.trim();
}

String? _normalizePhoneArtifactPath(Object? value, {bool allowRoot = false}) {
  final raw = '${value ?? ''}'.trim().replaceAll('\\', '/');
  if (raw.isEmpty) return allowRoot ? '.' : null;
  if (raw.startsWith('/')) return null;
  final parts = <String>[];
  for (final part in raw.split('/')) {
    final trimmed = part.trim();
    if (trimmed.isEmpty || trimmed == '.') continue;
    if (trimmed == '..') return null;
    parts.add(trimmed);
  }
  if (parts.isEmpty) return allowRoot ? '.' : null;
  return parts.join('/');
}

String? _phoneWebappIndexPath(Object? value, {bool allowRoot = false}) {
  final path = _normalizePhoneArtifactPath(value, allowRoot: allowRoot);
  if (path == null) return null;
  final lower = path.toLowerCase();
  if (lower.endsWith('.html') || lower.endsWith('.htm')) return path;
  if (path == '.') return 'index.html';
  return '$path/index.html';
}

Map<String, dynamic> _putPhoneArtifactContent(
  String path,
  String content, {
  required String source,
  String encoding = 'utf8',
  String? mimeType,
  int? sizeOverride,
  Map<String, dynamic>? metadata,
}) {
  final now = DateTime.now().toUtc().toIso8601String();
  final size = sizeOverride ?? utf8.encode(content).length;
  _mobileArtifactFiles[path] = {
    'path': path,
    'content': content,
    'size': size,
    'encoding': encoding,
    if (mimeType != null) 'mime_type': mimeType,
    if (metadata != null) 'metadata': metadata,
    'created_at': _mobileArtifactFiles[path]?['created_at'] ?? now,
    'updated_at': now,
    'source': source,
  };
  return {
    'path': path,
    'size': size,
    'encoding': encoding,
    if (mimeType != null) 'mime_type': mimeType,
    if (metadata != null) 'metadata': metadata,
  };
}

String _phoneArtifactExtension(String path) {
  final name = path.split('/').last;
  final index = name.lastIndexOf('.');
  if (index <= 0 || index == name.length - 1) return '';
  return name.substring(index + 1).toLowerCase();
}

String _slugifyPhoneArtifactName(String value, {required String fallback}) {
  final lower = value.trim().toLowerCase();
  final slug = lower
      .replaceAll(RegExp(r'[^a-z0-9]+'), '-')
      .replaceAll(RegExp(r'^-+|-+$'), '');
  return slug.isEmpty ? fallback : slug;
}

String _escapeHtmlText(Object? value) =>
    const HtmlEscape(HtmlEscapeMode.element).convert('${value ?? ''}');

List<Map<String, dynamic>> _slidesFromMarkdownText(String markdown) {
  final slides = <Map<String, dynamic>>[];
  Map<String, dynamic>? current;
  for (final rawLine in const LineSplitter().convert(markdown)) {
    final line = rawLine.trim();
    if (line.startsWith('#')) {
      if (current != null) slides.add(current);
      current = {
        'title': line.replaceFirst(RegExp(r'^#+'), '').trim().isEmpty
            ? 'Slide'
            : line.replaceFirst(RegExp(r'^#+'), '').trim(),
        'bullets': <String>[],
      };
    } else if (line.startsWith('-') || line.startsWith('*')) {
      current ??= {
        'title': 'Slide',
        'bullets': <String>[],
      };
      (current['bullets'] as List<String>)
          .add(line.replaceFirst(RegExp(r'^[-*]\s*'), '').trim());
    }
  }
  if (current != null) slides.add(current);
  return slides.isEmpty
      ? [
          {'title': 'Slide', 'bullets': <String>[]}
        ]
      : slides;
}

String _phoneSlideFormat(
  Object? explicit,
  String path, {
  String fallback = 'json',
}) {
  final raw = '${explicit ?? ''}'.trim().toLowerCase().replaceFirst(
        RegExp(r'^\.'),
        '',
      );
  if (raw.isNotEmpty) return raw;
  final ext = _phoneArtifactExtension(path);
  if (ext == 'slides') return fallback;
  return ext.isEmpty ? fallback : ext;
}

String? _unsupportedPhoneSlideFormat(String format) {
  if (const {'json', 'md', 'markdown', 'html', 'htm', 'txt', 'text'}
      .contains(format)) {
    return null;
  }
  if (const {'pptx', 'pdf', 'png', 'key'}.contains(format)) {
    return 'Phone-local slide tools support JSON, Markdown, HTML, and text only. Use PC delegation for $format output.';
  }
  return 'Unsupported phone-local slide format: $format';
}

_PhoneSlidesDocument _phoneSlidesFromArgs(
  Map<String, dynamic> args, {
  required String fallbackTitle,
}) {
  final title = '${args['title'] ?? fallbackTitle}'.trim();
  final slides = _normalizePhoneSlides(args['slides']);
  return _PhoneSlidesDocument(
    title: title.isEmpty ? 'Deck' : title,
    slides: slides.isEmpty
        ? [
            {
              'title': title.isEmpty ? 'Slide' : title,
              'bullets': <String>[],
            }
          ]
        : slides,
  );
}

_PhoneSlidesReadResult _readPhoneSlides(String path) {
  final file = _mobileArtifactFiles[path];
  if (file == null) {
    return _PhoneSlidesReadResult(
      error: _phoneArtifactError(
        'SLIDES_READ_FAILED',
        'slide source not found in phone artifact workspace',
        path: path,
      ),
    );
  }
  final content = '${file['content'] ?? ''}';
  final stem = _phoneArtifactStem(path);
  try {
    final decoded = jsonDecode(content);
    if (decoded is Map) {
      final title = '${decoded['title'] ?? stem}'.trim();
      final slides = _normalizePhoneSlides(decoded['slides']);
      return _PhoneSlidesReadResult(
        deck: _PhoneSlidesDocument(
          title: title.isEmpty ? stem : title,
          slides: slides.isEmpty ? _slidesFromMarkdownText(content) : slides,
        ),
      );
    }
    if (decoded is List) {
      return _PhoneSlidesReadResult(
        deck: _PhoneSlidesDocument(
          title: stem,
          slides: _normalizePhoneSlides(decoded),
        ),
      );
    }
  } catch (_) {
    // Fall through to markdown/text parsing.
  }
  final markdownSlides = _slidesFromMarkdownText(content);
  return _PhoneSlidesReadResult(
    deck: _PhoneSlidesDocument(title: stem, slides: markdownSlides),
  );
}

List<Map<String, dynamic>> _normalizePhoneSlides(Object? value) {
  Object? decoded = value;
  if (value is String && value.trim().isNotEmpty) {
    try {
      decoded = jsonDecode(value);
    } catch (_) {
      decoded = null;
    }
  }
  if (decoded is! List) return const [];
  final slides = <Map<String, dynamic>>[];
  for (final item in decoded) {
    if (item is Map) {
      final title = '${item['title'] ?? 'Slide'}'.trim();
      final rawBullets = item['bullets'] ?? item['body'] ?? item['items'];
      final bullets = rawBullets is List
          ? rawBullets.map((entry) => '$entry').toList()
          : '${rawBullets ?? ''}'.trim().isEmpty
              ? <String>[]
              : ['${rawBullets ?? ''}'.trim()];
      slides.add({
        'title': title.isEmpty ? 'Slide' : title,
        'bullets': bullets,
      });
    } else {
      final title = '$item'.trim();
      if (title.isNotEmpty) {
        slides.add({'title': title, 'bullets': <String>[]});
      }
    }
  }
  return slides;
}

_PhoneArtifactExportContent _phoneSlidesContentForFormat(
  _PhoneSlidesDocument deck,
  String format,
) {
  return switch (format) {
    'html' || 'htm' => _PhoneArtifactExportContent(
        content: _phoneSlidesHtml(deck),
        mimeType: 'text/html',
      ),
    'md' || 'markdown' => _PhoneArtifactExportContent(
        content: _phoneSlidesMarkdown(deck),
        mimeType: 'text/markdown',
      ),
    'txt' || 'text' => _PhoneArtifactExportContent(
        content: _phoneSlidesText(deck),
        mimeType: 'text/plain',
      ),
    _ => _PhoneArtifactExportContent(
        content: '${const JsonEncoder.withIndent('  ').convert({
              'title': deck.title,
              'slides': deck.slides,
              'format': 'slide_outline',
              'pc_export_note':
                  'Use PC delegation to export this outline to PPTX.',
            })}\n',
        mimeType: 'application/json',
      ),
  };
}

String _phoneSlidesMarkdown(_PhoneSlidesDocument deck) {
  final buffer = StringBuffer('# ${deck.title}\n');
  for (final slide in deck.slides) {
    buffer.writeln('\n## ${slide['title']}');
    for (final bullet in slide['bullets'] as List? ?? const []) {
      buffer.writeln('- $bullet');
    }
  }
  return '${buffer.toString().trimRight()}\n';
}

String _phoneSlidesText(_PhoneSlidesDocument deck) {
  final buffer = StringBuffer('${deck.title}\n');
  for (final slide in deck.slides) {
    buffer.writeln('\n${slide['title']}');
    for (final bullet in slide['bullets'] as List? ?? const []) {
      buffer.writeln('- $bullet');
    }
  }
  return '${buffer.toString().trimRight()}\n';
}

String _phoneSlidesHtml(_PhoneSlidesDocument deck) {
  final buffer = StringBuffer(
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>${_escapeHtmlText(deck.title)}</title></head><body><main>'
    '<h1>${_escapeHtmlText(deck.title)}</h1>',
  );
  for (final slide in deck.slides) {
    buffer.write('<section><h2>${_escapeHtmlText(slide['title'])}</h2><ul>');
    for (final bullet in slide['bullets'] as List? ?? const []) {
      buffer.write('<li>${_escapeHtmlText(bullet)}</li>');
    }
    buffer.write('</ul></section>');
  }
  buffer.write('</main></body></html>\n');
  return buffer.toString();
}

class _PhoneSlidesDocument {
  const _PhoneSlidesDocument({
    required this.title,
    required this.slides,
  });

  final String title;
  final List<Map<String, dynamic>> slides;
}

class _PhoneSlidesReadResult {
  const _PhoneSlidesReadResult({
    this.deck = const _PhoneSlidesDocument(title: 'Deck', slides: []),
    this.error,
  });

  final _PhoneSlidesDocument deck;
  final MobileToolResult? error;
}

List<double> _phoneChartValues(Map<String, dynamic> args) {
  final direct = args['values'] ?? args['data'];
  final values = <double>[];
  if (direct is List) {
    for (final item in direct) {
      final value = item is num ? item.toDouble() : double.tryParse('$item');
      if (value != null && value.isFinite) values.add(value);
    }
  }
  if (values.isEmpty) {
    final path = _normalizePhoneArtifactPath(args['path']);
    final content =
        path == null ? null : _mobileArtifactFiles[path]?['content'];
    if (content != null) {
      for (final match in RegExp(r'-?\d+(?:\.\d+)?').allMatches('$content')) {
        final value = double.tryParse(match.group(0) ?? '');
        if (value != null && value.isFinite) values.add(value);
        if (values.length >= 24) break;
      }
    }
  }
  return values.isEmpty ? const [3, 5, 2, 8] : values.take(24).toList();
}

List<String> _phoneChartLabels(Map<String, dynamic> args, int count) {
  final raw = args['labels'];
  final labels = <String>[];
  if (raw is List) {
    for (final item in raw) {
      final label = '$item'.trim();
      if (label.isNotEmpty) labels.add(label);
    }
  }
  while (labels.length < count) {
    labels.add('${labels.length + 1}');
  }
  return labels.take(count).toList();
}

String _phoneChartSvg({
  required String title,
  required List<double> values,
  required List<String> labels,
}) {
  const width = 900.0;
  const height = 520.0;
  const left = 72.0;
  const top = 72.0;
  const bottom = 92.0;
  const right = 48.0;
  final chartWidth = width - left - right;
  final chartHeight = height - top - bottom;
  final maxValue = values.fold<double>(0, math.max);
  final safeMax = maxValue <= 0 ? 1.0 : maxValue;
  final gap = values.length <= 1 ? 0.0 : 10.0;
  final barWidth =
      math.max(8.0, (chartWidth - gap * (values.length - 1)) / values.length);
  final buffer = StringBuffer()
    ..writeln(
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="520" viewBox="0 0 900 520" role="img">')
    ..writeln('<rect width="900" height="520" fill="#f8fafc"/>')
    ..writeln(
        '<text x="72" y="44" font-family="Arial, sans-serif" font-size="28" fill="#111827">${_escapeHtmlText(title)}</text>')
    ..writeln(
        '<line x1="$left" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}" stroke="#111827" stroke-width="2"/>')
    ..writeln(
        '<line x1="$left" y1="$top" x2="$left" y2="${height - bottom}" stroke="#111827" stroke-width="2"/>');
  for (var index = 0; index < values.length; index++) {
    final value = math.max(0.0, values[index]);
    final barHeight = chartHeight * (value / safeMax);
    final x = left + index * (barWidth + gap);
    final y = height - bottom - barHeight;
    final label = labels[index];
    buffer
      ..writeln(
          '<rect x="${x.toStringAsFixed(1)}" y="${y.toStringAsFixed(1)}" width="${barWidth.toStringAsFixed(1)}" height="${barHeight.toStringAsFixed(1)}" fill="#2563eb" rx="3"/>')
      ..writeln(
          '<text x="${(x + barWidth / 2).toStringAsFixed(1)}" y="${(height - bottom + 24).toStringAsFixed(1)}" font-family="Arial, sans-serif" font-size="14" fill="#374151" text-anchor="middle">${_escapeHtmlText(label)}</text>')
      ..writeln(
          '<text x="${(x + barWidth / 2).toStringAsFixed(1)}" y="${(y - 8).toStringAsFixed(1)}" font-family="Arial, sans-serif" font-size="14" fill="#111827" text-anchor="middle">${value.toStringAsFixed(value.truncateToDouble() == value ? 0 : 1)}</text>');
  }
  buffer.writeln('</svg>');
  return buffer.toString();
}

String _phoneArtifactStem(String path) {
  final name = path.split('/').last;
  final index = name.lastIndexOf('.');
  return index <= 0 ? name : name.substring(0, index);
}

String _phoneSheetFormat(
  Object? explicit,
  String path, {
  String fallback = 'csv',
}) {
  final raw = '${explicit ?? ''}'.trim().toLowerCase().replaceFirst(
        RegExp(r'^\.'),
        '',
      );
  if (raw.isNotEmpty) return raw;
  final ext = _phoneArtifactExtension(path);
  return ext.isEmpty ? fallback : ext;
}

String? _unsupportedPhoneSheetFormat(String format, {bool export = false}) {
  if (const {'csv', 'tsv', 'json', 'html', 'htm', 'txt', 'text'}
      .contains(format)) {
    return null;
  }
  if (const {'xlsx', 'xls', 'pdf', 'png'}.contains(format)) {
    return 'Phone-local sheet ${export ? 'export' : 'tools'} support CSV, TSV, JSON, HTML, and text only. Use PC delegation for $format.';
  }
  return 'Unsupported phone-local sheet format: $format';
}

List<List<String>> _phoneSheetRowsFromArgs(Map<String, dynamic> args) {
  final columns = args['columns'];
  final rows = _normalizePhoneSheetRows(args['rows']);
  if (columns is List && columns.isNotEmpty) {
    return [
      columns.map((item) => '$item').toList(),
      ...rows,
    ];
  }
  return rows.isEmpty
      ? const [
          ['value']
        ]
      : rows;
}

List<List<String>> _normalizePhoneSheetRows(Object? value) {
  if (value is String && value.trim().isNotEmpty) {
    try {
      return _normalizePhoneSheetRows(jsonDecode(value));
    } catch (_) {
      return _parseDelimitedRows(value);
    }
  }
  if (value is List) {
    if (value.every((item) => item is Map)) {
      final maps = value.cast<Map>();
      final headers = <String>{
        for (final map in maps)
          for (final key in map.keys) '$key',
      }.toList()
        ..sort();
      return [
        headers,
        for (final map in maps)
          [for (final key in headers) '${map[key] ?? ''}'],
      ];
    }
    return [
      for (final row in value)
        if (row is List) [for (final cell in row) '$cell'] else ['$row'],
    ];
  }
  if (value is Map) {
    return [
      const ['key', 'value'],
      for (final entry in value.entries) ['${entry.key}', '${entry.value}'],
    ];
  }
  return const [];
}

String _phoneSheetContentForFormat(List<List<String>> rows, String format) {
  return switch (format) {
    'tsv' => _rowsToDelimitedText(rows, delimiter: '\t'),
    'json' => '${const JsonEncoder.withIndent('  ').convert(rows)}\n',
    'html' || 'htm' => _rowsToHtmlTable(rows),
    'txt' || 'text' => _rowsToDelimitedText(rows, delimiter: '\t'),
    _ => _rowsToDelimitedText(rows, delimiter: ','),
  };
}

_PhoneSheetReadResult _readPhoneSheetRows(Object? value) {
  final path = _normalizePhoneArtifactPath(value);
  if (path == null) {
    return _PhoneSheetReadResult(
      error: _phoneArtifactError(
        'INVALID_INPUT',
        "'path' is required and must stay inside the phone artifact workspace.",
      ),
    );
  }
  final file = _mobileArtifactFiles[path];
  if (file == null) {
    return _PhoneSheetReadResult(
      path: path,
      error: _phoneArtifactError(
          'SHEET_READ_FAILED', 'sheet artifact not found',
          path: path),
    );
  }
  final format = _phoneSheetFormat(null, path);
  final unsupported = _unsupportedPhoneSheetFormat(format);
  if (unsupported != null) {
    return _PhoneSheetReadResult(
      path: path,
      format: format,
      error: _phoneArtifactError(
        'UNSUPPORTED_PHONE_SHEET_FORMAT',
        unsupported,
        path: path,
      ),
    );
  }
  final content = '${file['content'] ?? ''}';
  try {
    final rows = switch (format) {
      'json' => _normalizePhoneSheetRows(jsonDecode(content)),
      'tsv' => _parseDelimitedRows(content, delimiter: '\t'),
      'html' || 'htm' => _parseHtmlTableRows(content),
      'txt' || 'text' => const LineSplitter()
          .convert(content)
          .map((line) => <String>[line])
          .toList(),
      _ => _parseDelimitedRows(content),
    };
    return _PhoneSheetReadResult(path: path, rows: rows, format: format);
  } catch (error) {
    return _PhoneSheetReadResult(
      path: path,
      format: format,
      error: _phoneArtifactError(
        'SHEET_READ_FAILED',
        'could not parse phone-local sheet artifact: $error',
        path: path,
      ),
    );
  }
}

String _rowsToDelimitedText(
  List<List<String>> rows, {
  required String delimiter,
}) {
  return '${rows.map((row) => row.map((cell) => _escapeDelimitedCell(cell, delimiter)).join(delimiter)).join('\n')}\n';
}

String _escapeDelimitedCell(String cell, String delimiter) {
  final needsQuote = cell.contains(delimiter) ||
      cell.contains('"') ||
      cell.contains('\n') ||
      cell.contains('\r');
  if (!needsQuote) return cell;
  return '"${cell.replaceAll('"', '""')}"';
}

List<List<String>> _parseDelimitedRows(
  String content, {
  String delimiter = ',',
}) {
  final rows = <List<String>>[];
  final row = <String>[];
  final cell = StringBuffer();
  var inQuotes = false;
  for (var index = 0; index < content.length; index++) {
    final char = content[index];
    if (char == '"') {
      if (inQuotes && index + 1 < content.length && content[index + 1] == '"') {
        cell.write('"');
        index += 1;
      } else {
        inQuotes = !inQuotes;
      }
      continue;
    }
    if (!inQuotes && char == delimiter) {
      row.add(cell.toString());
      cell.clear();
      continue;
    }
    if (!inQuotes && (char == '\n' || char == '\r')) {
      if (char == '\r' &&
          index + 1 < content.length &&
          content[index + 1] == '\n') {
        index += 1;
      }
      row.add(cell.toString());
      cell.clear();
      rows.add(List<String>.from(row));
      row.clear();
      continue;
    }
    cell.write(char);
  }
  if (cell.isNotEmpty || row.isNotEmpty) {
    row.add(cell.toString());
    rows.add(List<String>.from(row));
  }
  return rows;
}

String _rowsToHtmlTable(List<List<String>> rows) {
  final buffer = StringBuffer()
    ..writeln('<!doctype html><html><head><meta charset="utf-8">')
    ..writeln(
        '<meta name="viewport" content="width=device-width,initial-scale=1">')
    ..writeln('<title>Sheet</title></head><body><table>');
  for (var rowIndex = 0; rowIndex < rows.length; rowIndex++) {
    final tag = rowIndex == 0 ? 'th' : 'td';
    buffer.writeln('<tr>');
    for (final cell in rows[rowIndex]) {
      buffer.writeln('<$tag>${_escapeHtmlText(cell)}</$tag>');
    }
    buffer.writeln('</tr>');
  }
  buffer.writeln('</table></body></html>');
  return buffer.toString();
}

List<List<String>> _parseHtmlTableRows(String html) {
  final rows = <List<String>>[];
  final rowRegExp =
      RegExp(r'<tr[^>]*>(.*?)</tr>', caseSensitive: false, dotAll: true);
  final cellRegExp =
      RegExp(r'<t[hd][^>]*>(.*?)</t[hd]>', caseSensitive: false, dotAll: true);
  for (final rowMatch in rowRegExp.allMatches(html)) {
    final rowHtml = rowMatch.group(1) ?? '';
    final cells = [
      for (final cellMatch in cellRegExp.allMatches(rowHtml))
        _stripHtmlToText(cellMatch.group(1) ?? '').trim(),
    ];
    if (cells.isNotEmpty) rows.add(cells);
  }
  return rows;
}

class _PhoneSheetReadResult {
  const _PhoneSheetReadResult({
    this.path = '',
    this.rows = const [],
    this.format = '',
    this.error,
  });

  final String path;
  final List<List<String>> rows;
  final String format;
  final MobileToolResult? error;
}

String? _unsupportedPhoneArtifactExportFormat(String format) {
  if (const {
    'html',
    'htm',
    'txt',
    'text',
    'md',
    'markdown',
    'json',
    'csv',
    'tsv'
  }.contains(format)) {
    return null;
  }
  if (const {'pdf', 'png', 'docx', 'pptx', 'xlsx', 'xls'}.contains(format)) {
    return 'Phone-local artifact_export supports zip/base64 plus HTML, text, Markdown, JSON, CSV, and TSV only. Use PC delegation for $format output.';
  }
  return 'Unsupported phone-local artifact export format: $format';
}

_PhoneArtifactExportContent _phoneArtifactExportContent(
  String sourcePath,
  String format,
) {
  final exact = _mobileArtifactFiles[sourcePath];
  final entries = _phoneZipSourceEntries(sourcePath);
  if (exact == null && entries.isEmpty) {
    return _PhoneArtifactExportContent(
      error: _phoneArtifactError(
        'EXPORT_FAILED',
        'artifact source not found in phone artifact workspace',
        path: sourcePath,
      ),
    );
  }
  if (exact == null) {
    return _phoneDirectoryExportContent(sourcePath, entries, format);
  }
  final content = '${exact['content'] ?? ''}';
  if (format == 'csv' || format == 'tsv') {
    final parsed = _readPhoneSheetRows(sourcePath);
    if (parsed.error != null) {
      return _phoneFileExportContent(sourcePath, content, format);
    }
    return _PhoneArtifactExportContent(
      content: _phoneSheetContentForFormat(parsed.rows, format),
      mimeType: format == 'csv' ? 'text/csv' : 'text/tab-separated-values',
    );
  }
  return _phoneFileExportContent(sourcePath, content, format);
}

_PhoneArtifactExportContent _phoneDirectoryExportContent(
  String sourcePath,
  List<_ZipSourceEntry> entries,
  String format,
) {
  final records = [
    for (final entry in entries)
      {'path': entry.path, 'size': entry.bytes.length},
  ];
  return switch (format) {
    'json' => _PhoneArtifactExportContent(
        content: '${const JsonEncoder.withIndent('  ').convert({
              'source_path': sourcePath,
              'files': records,
            })}\n',
        mimeType: 'application/json',
      ),
    'csv' => _PhoneArtifactExportContent(
        content: _rowsToDelimitedText([
          const ['path', 'size'],
          for (final record in records)
            ['${record['path']}', '${record['size']}'],
        ], delimiter: ','),
        mimeType: 'text/csv',
      ),
    'tsv' => _PhoneArtifactExportContent(
        content: _rowsToDelimitedText([
          const ['path', 'size'],
          for (final record in records)
            ['${record['path']}', '${record['size']}'],
        ], delimiter: '\t'),
        mimeType: 'text/tab-separated-values',
      ),
    'html' || 'htm' => _PhoneArtifactExportContent(
        content: '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Artifact Export</title></head><body><h1>${_escapeHtmlText(sourcePath)}</h1>'
            '<ul>${records.map((record) => '<li>${_escapeHtmlText(record['path'])} (${record['size']} bytes)</li>').join()}</ul>'
            '</body></html>\n',
        mimeType: 'text/html',
      ),
    _ => _PhoneArtifactExportContent(
        content:
            '${records.map((record) => '${record['path']}\t${record['size']}').join('\n')}\n',
        mimeType: 'text/plain',
      ),
  };
}

_PhoneArtifactExportContent _phoneFileExportContent(
  String sourcePath,
  String content,
  String format,
) {
  return switch (format) {
    'html' || 'htm' => _PhoneArtifactExportContent(
        content: _looksLikeHtmlFragment(content)
            ? content
            : '<!doctype html><html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>${_escapeHtmlText(sourcePath)}</title></head><body><pre>${_escapeHtmlText(content)}</pre></body></html>\n',
        mimeType: 'text/html',
      ),
    'json' => _PhoneArtifactExportContent(
        content: _looksLikeJsonText(content)
            ? '${content.trimRight()}\n'
            : '${const JsonEncoder.withIndent('  ').convert({
                    'path': sourcePath,
                    'content': content,
                  })}\n',
        mimeType: 'application/json',
      ),
    'md' || 'markdown' => _PhoneArtifactExportContent(
        content: '${content.trimRight()}\n',
        mimeType: 'text/markdown',
      ),
    _ => _PhoneArtifactExportContent(
        content: '${content.trimRight()}\n',
        mimeType: 'text/plain',
      ),
  };
}

bool _looksLikeJsonText(String content) {
  final text = content.trim();
  if (text.isEmpty) return false;
  if (!text.startsWith('{') && !text.startsWith('[')) return false;
  try {
    jsonDecode(text);
    return true;
  } catch (_) {
    return false;
  }
}

class _PhoneArtifactExportContent {
  const _PhoneArtifactExportContent({
    this.content = '',
    this.mimeType,
    this.error,
  });

  final String content;
  final String? mimeType;
  final MobileToolResult? error;
}

List<_ZipSourceEntry> _phoneZipSourceEntries(String sourcePath) {
  final exact = _mobileArtifactFiles[sourcePath];
  if (sourcePath != '.' && exact != null) {
    return [
      _ZipSourceEntry(
        path: sourcePath.split('/').last,
        bytes: _phoneArtifactStoredBytes(exact),
      ),
    ];
  }
  final entries = <_ZipSourceEntry>[];
  for (final entry in _mobileArtifactFiles.entries) {
    final path = entry.key;
    if (!_artifactPathInBase(path, sourcePath, recursive: true)) continue;
    final archivePath =
        sourcePath == '.' ? path : path.substring(sourcePath.length + 1);
    if (archivePath.isEmpty) continue;
    entries.add(
      _ZipSourceEntry(
        path: archivePath,
        bytes: _phoneArtifactStoredBytes(entry.value),
      ),
    );
  }
  entries.sort((a, b) => a.path.compareTo(b.path));
  return entries;
}

Uint8List _phoneArtifactStoredBytes(Map<String, dynamic> file) {
  final content = '${file['content'] ?? ''}';
  if ('${file['encoding'] ?? ''}'.trim().toLowerCase() == 'base64') {
    try {
      return base64Decode(content.trim());
    } catch (_) {
      return Uint8List.fromList(utf8.encode(content));
    }
  }
  return Uint8List.fromList(utf8.encode(content));
}

Uint8List _buildStoredZip(List<_ZipSourceEntry> entries) {
  final local = BytesBuilder(copy: false);
  final central = BytesBuilder(copy: false);
  final centralRecords = <_ZipCentralRecord>[];
  for (final entry in entries) {
    final nameBytes = utf8.encode(entry.path);
    final crc = _crc32(entry.bytes);
    final offset = local.length;
    _zipWriteUint32(local, 0x04034b50);
    _zipWriteUint16(local, 20);
    _zipWriteUint16(local, 0x0800);
    _zipWriteUint16(local, 0);
    _zipWriteUint16(local, 0);
    _zipWriteUint16(local, 0);
    _zipWriteUint32(local, crc);
    _zipWriteUint32(local, entry.bytes.length);
    _zipWriteUint32(local, entry.bytes.length);
    _zipWriteUint16(local, nameBytes.length);
    _zipWriteUint16(local, 0);
    local.add(nameBytes);
    local.add(entry.bytes);
    centralRecords.add(_ZipCentralRecord(entry, nameBytes, crc, offset));
  }
  for (final record in centralRecords) {
    _zipWriteUint32(central, 0x02014b50);
    _zipWriteUint16(central, 20);
    _zipWriteUint16(central, 20);
    _zipWriteUint16(central, 0x0800);
    _zipWriteUint16(central, 0);
    _zipWriteUint16(central, 0);
    _zipWriteUint16(central, 0);
    _zipWriteUint32(central, record.crc);
    _zipWriteUint32(central, record.entry.bytes.length);
    _zipWriteUint32(central, record.entry.bytes.length);
    _zipWriteUint16(central, record.nameBytes.length);
    _zipWriteUint16(central, 0);
    _zipWriteUint16(central, 0);
    _zipWriteUint16(central, 0);
    _zipWriteUint16(central, 0);
    _zipWriteUint32(central, 0);
    _zipWriteUint32(central, record.offset);
    central.add(record.nameBytes);
  }
  final localBytes = local.takeBytes();
  final centralBytes = central.takeBytes();
  final zip = BytesBuilder(copy: false)
    ..add(localBytes)
    ..add(centralBytes);
  _zipWriteUint32(zip, 0x06054b50);
  _zipWriteUint16(zip, 0);
  _zipWriteUint16(zip, 0);
  _zipWriteUint16(zip, entries.length);
  _zipWriteUint16(zip, entries.length);
  _zipWriteUint32(zip, centralBytes.length);
  _zipWriteUint32(zip, localBytes.length);
  _zipWriteUint16(zip, 0);
  return zip.takeBytes();
}

void _zipWriteUint16(BytesBuilder builder, int value) {
  builder.add([value & 0xff, (value >> 8) & 0xff]);
}

void _zipWriteUint32(BytesBuilder builder, int value) {
  builder.add([
    value & 0xff,
    (value >> 8) & 0xff,
    (value >> 16) & 0xff,
    (value >> 24) & 0xff,
  ]);
}

int _crc32(Uint8List bytes) {
  var crc = 0xffffffff;
  for (final byte in bytes) {
    crc ^= byte;
    for (var bit = 0; bit < 8; bit++) {
      final mask = -(crc & 1);
      crc = (crc >> 1) ^ (0xedb88320 & mask);
    }
  }
  return (crc ^ 0xffffffff) & 0xffffffff;
}

class _ZipSourceEntry {
  const _ZipSourceEntry({
    required this.path,
    required this.bytes,
  });

  final String path;
  final Uint8List bytes;
}

class _ZipCentralRecord {
  const _ZipCentralRecord(
    this.entry,
    this.nameBytes,
    this.crc,
    this.offset,
  );

  final _ZipSourceEntry entry;
  final List<int> nameBytes;
  final int crc;
  final int offset;
}

bool _artifactPathInBase(
  String path,
  String basePath, {
  required bool recursive,
}) {
  if (basePath == '.') {
    return recursive || !path.contains('/');
  }
  if (path == basePath) return true;
  if (!path.startsWith('$basePath/')) return false;
  if (recursive) return true;
  return !path.substring(basePath.length + 1).contains('/');
}

bool _artifactPathVisible(String path, bool includeHidden) {
  if (includeHidden) return true;
  return !path.split('/').any((part) => part.startsWith('.'));
}

Map<String, dynamic> _phoneArtifactCheckpoint(
  String action,
  String path,
  String before,
) {
  return {
    'id': _nextToolId('artifact_checkpoint'),
    'action': action,
    'path': path,
    'size': utf8.encode(before).length,
    'created_at': DateTime.now().toUtc().toIso8601String(),
    'workspace': 'phone',
  };
}

MobileToolResult _phoneArtifactError(
  String code,
  String message, {
  String? path,
}) {
  return MobileToolResult(
    ok: false,
    summary: message,
    output: jsonEncode({
      'status': 'error',
      'error': {
        'code': code,
        'message': message,
        if (path != null) 'path': path,
        'workspace': 'phone',
        'execution_location': 'phone',
      },
    }),
  );
}

MobileToolResult _pcDelegationRequired(String toolName, String message) {
  return MobileToolResult(
    ok: false,
    summary: '$toolName requires PC runtime',
    output: jsonEncode({
      'status': 'error',
      'error': {
        'code': 'PC_DELEGATION_REQUIRED',
        'message': message,
        'tool_name': toolName,
        'execution_location': 'pc',
        'runtime_layers': ['pc-defaultspack-runtime'],
      },
    }),
  );
}

List<String> _phoneStringList(Object? value) {
  if (value is List) {
    return value.map((entry) => '$entry'.trim()).where((entry) {
      return entry.isNotEmpty;
    }).toList();
  }
  final text = '${value ?? ''}'.trim();
  if (text.isEmpty) return const [];
  if (text.startsWith('[')) {
    try {
      final decoded = jsonDecode(text);
      if (decoded is List) return _phoneStringList(decoded);
    } catch (_) {
      // Fall through to shell-ish splitting.
    }
  }
  return text
      .split(RegExp(r'[\s,]+'))
      .map((entry) => entry.trim())
      .where((entry) => entry.isNotEmpty)
      .toList();
}

String? _phoneReportContentFromArgs(Map<String, dynamic> args) {
  for (final key in ['content', 'report', 'markdown', 'text']) {
    final value = args[key];
    if (value is String && value.trim().isNotEmpty) return value;
    if (value is Map || value is List) {
      return const JsonEncoder.withIndent('  ').convert(value);
    }
  }
  return null;
}

String _workflowIdArg(Map<String, dynamic> args) {
  return '${args['workflow_id'] ?? args['workflowId'] ?? args['id'] ?? ''}'
      .trim();
}

String? _requiredWorkflowRunId(Map<String, dynamic> args) {
  final raw = '${args['run_id'] ?? args['runId'] ?? args['id'] ?? ''}'.trim();
  if (raw.isEmpty) return null;
  return _slugifyPhoneArtifactName(raw, fallback: raw);
}

List<Map<String, dynamic>> _normalizeWorkflowSteps(Object? raw) {
  final source = raw is List ? raw : const [];
  final steps = <Map<String, dynamic>>[];
  for (var index = 0; index < source.length; index++) {
    final item = source[index];
    if (item is! Map) continue;
    final map = item.map((key, value) => MapEntry('$key', value));
    final rawStepId =
        '${map['step_id'] ?? map['id'] ?? map['key'] ?? 'step-${index + 1}'}'
            .trim();
    final stepId = _slugifyPhoneArtifactName(
      rawStepId.isEmpty ? 'step-${index + 1}' : rawStepId,
      fallback: 'step-${index + 1}',
    );
    final toolName =
        '${map['tool_name'] ?? map['tool_id'] ?? map['tool'] ?? map['name'] ?? ''}'
            .trim();
    steps.add({
      'id': stepId,
      'title': '${map['title'] ?? map['description'] ?? stepId}'.trim(),
      'tool_name': toolName,
      'arguments': _workflowStepArguments(map),
    });
  }
  return steps;
}

List<Map<String, dynamic>> _cloneWorkflowSteps(Object? raw) {
  if (raw is! List) return <Map<String, dynamic>>[];
  return raw.whereType<Map>().map((item) {
    return item.map((key, value) => MapEntry('$key', value));
  }).toList();
}

Map<String, dynamic> _workflowStepArguments(Map<String, dynamic> step) {
  for (final key in ['arguments', 'args', 'input', 'params']) {
    final value = step[key];
    if (value is Map<String, dynamic>) return Map<String, dynamic>.from(value);
    if (value is Map) return value.map((key, value) => MapEntry('$key', value));
  }
  const metadataKeys = {
    'id',
    'key',
    'step_id',
    'tool',
    'tool_id',
    'tool_name',
    'name',
    'title',
    'description',
  };
  return {
    for (final entry in step.entries)
      if (!metadataKeys.contains(entry.key)) entry.key: entry.value,
  };
}

Map<String, dynamic> _workflowStepArgs(Map<String, dynamic> step) {
  final value = step['arguments'];
  if (value is Map<String, dynamic>) return Map<String, dynamic>.from(value);
  if (value is Map) return value.map((key, value) => MapEntry('$key', value));
  return const {};
}

String? _workflowStepBlockedReason(
  String canonicalToolName,
  Map<String, dynamic> args,
) {
  if (_phoneWorkflowToolIds.contains(canonicalToolName)) {
    return 'workflow tools cannot be nested inside phone-local workflow steps.';
  }
  if (canonicalToolName == 'tool_batch') {
    final calls = args['calls'];
    if (calls is List) {
      for (final call in calls) {
        if (call is! Map) continue;
        final requested =
            '${call['tool_name'] ?? call['tool_id'] ?? call['name'] ?? ''}'
                .trim();
        if (_phoneWorkflowToolIds.contains(_canonicalToolName(requested))) {
          return 'tool_batch inside a phone-local workflow cannot call workflow tools.';
        }
      }
    }
  }
  return null;
}

String _workflowOutputPreview(String raw) {
  if (raw.length <= _workflowStepOutputPreviewLimit) return raw;
  return '${raw.substring(0, _workflowStepOutputPreviewLimit)}...';
}

String _workflowStepExecutionLocation(Map<String, dynamic> parsed) {
  final data = parsed['data'];
  if (data is Map) {
    final location = '${data['execution_location'] ?? ''}'.trim();
    if (location.isNotEmpty) return location;
  }
  final location = '${parsed['execution_location'] ?? ''}'.trim();
  return location.isEmpty ? 'phone' : location;
}

void _persistPhoneWorkflow(String workflowId) {
  final workflow = _mobileWorkflows[workflowId];
  if (workflow == null) return;
  _putPhoneArtifactContent(
    'workflows/$workflowId/workflow.json',
    '${const JsonEncoder.withIndent('  ').convert(_phoneWorkflowRecord(workflowId))}\n',
    source: 'workflow_define',
    mimeType: 'application/json',
    metadata: {'workflow_id': workflowId, 'artifact_role': 'workflow_record'},
  );
}

void _persistPhoneWorkflowRun(String runId) {
  final run = _mobileWorkflowRuns[runId];
  if (run == null) return;
  _putPhoneArtifactContent(
    'workflows/runs/$runId.json',
    '${const JsonEncoder.withIndent('  ').convert(_phoneWorkflowRunRecord(runId))}\n',
    source: 'workflow_run',
    mimeType: 'application/json',
    metadata: {'run_id': runId, 'artifact_role': 'workflow_run_record'},
  );
}

void _appendPhoneWorkflowEvent(
  String runId,
  String type,
  Map<String, dynamic> data,
) {
  final events = _mobileWorkflowEvents.putIfAbsent(runId, () => []);
  events.add({
    'id': _nextToolId('workflow_event'),
    'run_id': runId,
    'type': type,
    'data': data,
    'created_at': DateTime.now().toUtc().toIso8601String(),
    'workspace': 'phone',
  });
}

Map<String, dynamic> _phoneWorkflowRecord(String workflowId) {
  final workflow = _mobileWorkflows[workflowId];
  if (workflow == null) return {'workflow_id': workflowId, 'status': 'missing'};
  return {
    ...workflow,
    'workspace': 'phone',
    'execution_location': 'phone',
    'runtime_layers': _phoneWorkflowRuntimeLayers,
  };
}

Map<String, dynamic> _phoneWorkflowRunRecord(String runId) {
  final run = _mobileWorkflowRuns[runId];
  if (run == null) return {'run_id': runId, 'status': 'missing'};
  return {
    ...run,
    'event_count': _mobileWorkflowEvents[runId]?.length ?? 0,
    'events': List<Map<String, dynamic>>.from(
      _mobileWorkflowEvents[runId] ?? const [],
    ),
    'workspace': 'phone',
    'execution_location': 'phone',
    'runtime_layers': _phoneWorkflowRuntimeLayers,
  };
}

MobileToolResult _phoneWorkflowError(
  String code,
  String message, {
  String? workflowId,
  String? runId,
}) {
  return MobileToolResult(
    ok: false,
    summary: message,
    output: jsonEncode({
      'status': 'error',
      'error': {
        'code': code,
        'message': message,
        if (workflowId != null && workflowId.isNotEmpty)
          'workflow_id': workflowId,
        if (runId != null && runId.isNotEmpty) 'run_id': runId,
        'workspace': 'phone',
        'execution_location': 'phone',
      },
    }),
  );
}

Map<String, dynamic> _phoneJobInput(Map<String, dynamic> args) {
  final input = args['input'];
  if (input is Map<String, dynamic>) return Map<String, dynamic>.from(input);
  if (input is Map) return input.map((key, value) => MapEntry('$key', value));
  final query = '${args['query'] ?? ''}'.trim();
  return {
    if (query.isNotEmpty) 'query': query,
  };
}

String? _requiredPhoneJobId(Map<String, dynamic> args) {
  final raw = '${args['job_id'] ?? args['id'] ?? ''}'.trim();
  if (raw.isEmpty) return null;
  return _slugifyPhoneArtifactName(raw, fallback: raw);
}

List<String> _phoneJobArtifacts(String jobId) {
  final job = _mobileJobs[jobId];
  if (job == null) return <String>[];
  final artifacts = job['artifacts'];
  if (artifacts is List<String>) return artifacts;
  if (artifacts is List) {
    final normalized = artifacts.map((entry) => '$entry').toList();
    job['artifacts'] = normalized;
    return normalized;
  }
  final normalized = <String>[];
  job['artifacts'] = normalized;
  return normalized;
}

void _appendPhoneJobEvent(
  String jobId,
  String type,
  Map<String, dynamic> data,
) {
  final events = _mobileJobEvents.putIfAbsent(jobId, () => []);
  events.add({
    'id': _nextToolId('job_event'),
    'job_id': jobId,
    'type': type,
    'data': data,
    'created_at': DateTime.now().toUtc().toIso8601String(),
    'workspace': 'phone',
  });
}

void _persistPhoneJob(String jobId) {
  final job = _mobileJobs[jobId];
  if (job == null) return;
  final path = 'jobs/$jobId/job.json';
  if (!_phoneJobArtifacts(jobId).contains(path)) {
    _phoneJobArtifacts(jobId).insert(0, path);
  }
  _putPhoneArtifactContent(
    path,
    '${const JsonEncoder.withIndent('  ').convert(_phoneJobRecord(jobId))}\n',
    source: 'job_record',
    mimeType: 'application/json',
    metadata: {'job_id': jobId, 'artifact_role': 'job_record'},
  );
}

Map<String, dynamic> _phoneJobRecord(String jobId) {
  final job = _mobileJobs[jobId];
  if (job == null) return {'job_id': jobId, 'status': 'missing'};
  return {
    ...job,
    'artifacts': List<String>.from(_phoneJobArtifacts(jobId)),
    'event_count': _mobileJobEvents[jobId]?.length ?? 0,
    'workspace': 'phone',
    'execution_location': 'phone',
    'runtime_layers': _flutterRuntimeLayers,
  };
}

MobileToolResult _phoneJobError(
  String code,
  String message, {
  String? jobId,
}) {
  return MobileToolResult(
    ok: false,
    summary: message,
    output: jsonEncode({
      'status': 'error',
      'error': {
        'code': code,
        'message': message,
        if (jobId != null) 'job_id': jobId,
        'workspace': 'phone',
        'execution_location': 'phone',
      },
    }),
  );
}

String _simpleTextDiff(String before, String after, {required String path}) {
  if (before == after) return '';
  return [
    '--- a/$path',
    '+++ b/$path',
    '@@',
    if (before.isNotEmpty) '-$before',
    if (after.isNotEmpty) '+$after',
  ].join('\n');
}

int _countOccurrences(String text, String needle) {
  if (needle.isEmpty) return 0;
  var count = 0;
  var index = 0;
  while (true) {
    final found = text.indexOf(needle, index);
    if (found < 0) return count;
    count += 1;
    index = found + needle.length;
  }
}

Map<String, dynamic>? _findById(
  List<Map<String, dynamic>> records,
  String id,
) {
  if (id.isEmpty) return null;
  for (final record in records) {
    if (record['id'] == id) return record;
  }
  return null;
}

List<String> _normalizeColumns(Object? raw) {
  if (raw is List) {
    final columns = raw
        .map((item) => '$item'.trim())
        .where((item) => item.isNotEmpty)
        .toList();
    if (columns.isNotEmpty) return columns;
  }
  if (raw is String && raw.trim().isNotEmpty) {
    final columns = raw
        .split(RegExp(r'[,|\n]'))
        .map((item) => item.trim())
        .where((item) => item.isNotEmpty)
        .toList();
    if (columns.isNotEmpty) return columns;
  }
  return List<String>.from(_taskBoardDefaultColumns);
}

String _resolveColumn(Map<String, dynamic> args) {
  final requested = '${args['column'] ?? args['column_id'] ?? ''}'.trim();
  final columns = List<String>.from(_mobileTaskBoard['columns'] as List);
  if (requested.isEmpty) return columns.first;
  for (final column in columns) {
    if (column.toLowerCase() == requested.toLowerCase()) return column;
  }
  return requested;
}

List<Map<String, dynamic>> _subtasks(Map<String, dynamic> card) {
  final raw = card['subtasks'];
  if (raw is List<Map<String, dynamic>>) return raw;
  if (raw is List) {
    final subtasks = raw
        .whereType<Map>()
        .map((item) => item.map((key, value) => MapEntry('$key', value)))
        .toList();
    card['subtasks'] = subtasks;
    return subtasks;
  }
  final subtasks = <Map<String, dynamic>>[];
  card['subtasks'] = subtasks;
  return subtasks;
}

Map<String, dynamic> _taskBoardSnapshot() {
  return {
    'board_id': _mobileTaskBoard['board_id'],
    'title': _mobileTaskBoard['title'],
    'columns': _mobileTaskBoard['columns'],
    'cards': _mobileTaskCards,
  };
}

String _taskBoardSummary(String action) {
  final columns = List<String>.from(_mobileTaskBoard['columns'] as List);
  final counts = {
    for (final column in columns)
      column: _mobileTaskCards.where((card) => card['column'] == column).length,
  };
  final countsText =
      counts.entries.map((entry) => '${entry.key}:${entry.value}').join(', ');
  return '$action: ${_mobileTaskCards.length} cards ($countsText)';
}

List<String> _normalizePlanSteps(Object? raw) {
  final steps = <String>[];
  if (raw is List) {
    for (final item in raw) {
      final value = '$item'.trim();
      if (value.isNotEmpty) steps.add(value);
    }
  } else if (raw is String && raw.trim().isNotEmpty) {
    for (final line in raw.split(RegExp(r'[\n;]+'))) {
      final normalized =
          line.replaceFirst(RegExp(r'^\s*[-*\d.)]+\s*'), '').trim();
      if (normalized.isNotEmpty) steps.add(normalized);
    }
  }
  if (steps.isNotEmpty) return steps;
  return const ['状況を確認する', '必要な作業を実行する', '結果を検証する'];
}

String _formatNumber(double value) {
  if (value.isFinite && value == value.roundToDouble()) {
    return value.toInt().toString();
  }
  final fixed = value.toStringAsPrecision(12);
  return fixed.replaceFirst(RegExp(r'\.?0+$'), '');
}

class _ExpressionParser {
  _ExpressionParser(this.source);

  final String source;
  int _index = 0;

  double parse() {
    final value = _parseExpression();
    _skipWhitespace();
    if (_index != source.length) {
      throw FormatException('unexpected token "${source[_index]}"');
    }
    if (!value.isFinite) throw const FormatException('result is not finite');
    return value;
  }

  double _parseExpression() {
    var value = _parseTerm();
    while (true) {
      _skipWhitespace();
      if (_match('+')) {
        value += _parseTerm();
      } else if (_match('-')) {
        value -= _parseTerm();
      } else {
        return value;
      }
    }
  }

  double _parseTerm() {
    var value = _parsePower();
    while (true) {
      _skipWhitespace();
      if (_match('*')) {
        value *= _parsePower();
      } else if (_match('/')) {
        final rhs = _parsePower();
        if (rhs == 0) throw const FormatException('division by zero');
        value /= rhs;
      } else {
        return value;
      }
    }
  }

  double _parsePower() {
    var value = _parseUnary();
    _skipWhitespace();
    if (_match('^')) {
      value = math.pow(value, _parsePower()).toDouble();
    }
    return value;
  }

  double _parseUnary() {
    _skipWhitespace();
    if (_match('+')) return _parseUnary();
    if (_match('-')) return -_parseUnary();
    return _parsePrimary();
  }

  double _parsePrimary() {
    _skipWhitespace();
    if (_match('(')) {
      final value = _parseExpression();
      _skipWhitespace();
      if (!_match(')')) throw const FormatException('missing ")"');
      return value;
    }
    final start = _index;
    var sawDigit = false;
    while (_index < source.length) {
      final code = source.codeUnitAt(_index);
      final isDigit = code >= 48 && code <= 57;
      if (isDigit) {
        sawDigit = true;
        _index += 1;
        continue;
      }
      if (source[_index] == '.') {
        _index += 1;
        continue;
      }
      break;
    }
    if (!sawDigit) throw const FormatException('number expected');
    return double.parse(source.substring(start, _index));
  }

  bool _match(String char) {
    if (_index >= source.length || source[_index] != char) return false;
    _index += 1;
    return true;
  }

  void _skipWhitespace() {
    while (_index < source.length && source[_index].trim().isEmpty) {
      _index += 1;
    }
  }
}
