import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/data/local/defaultspack_tool_agent_manifest.g.dart';
import 'package:rumi_remote_app/src/data/local/mobile_tool_runtime.dart';
import 'package:rumi_remote_app/src/platform/platform_services.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FakePcToolDelegate implements MobileToolDelegate {
  MobileToolCall? lastCall;

  @override
  Future<MobileToolResult> invoke(MobileToolCall call) async {
    lastCall = call;
    return MobileToolResult(
      ok: true,
      summary: 'PC ${call.name}',
      output: jsonEncode({
        'status': 'ok',
        'data': {
          'execution_location': 'pc',
          'tool_name': call.name,
          'result': 'ran on pc',
        },
      }),
    );
  }
}

class _FakeUrlLauncher extends PlatformUrlLauncher {
  _FakeUrlLauncher(this.opened);

  final List<Uri> opened;

  @override
  Future<bool> open(Uri uri) async {
    opened.add(uri);
    return true;
  }
}

class _FakeClipboard extends PlatformClipboard {
  _FakeClipboard(this.text);

  String? text;

  @override
  Future<String?> readText() async => text;

  @override
  Future<void> writeText(String text) async {
    this.text = text;
  }
}

class _FakeMediaPicker extends PlatformMediaPicker {
  _FakeMediaPicker(this.file);

  final PlatformPickedMediaFile? file;
  bool called = false;
  String? lastKind;
  int? lastMaxBytes;

  @override
  Future<PlatformPickedMediaFile?> pick({
    required String kind,
    required int maxBytes,
  }) async {
    called = true;
    lastKind = kind;
    lastMaxBytes = maxBytes;
    return file;
  }
}

class _FakeScreenshotCapture extends PlatformScreenshotCapture {
  _FakeScreenshotCapture(this.screenshot);

  final PlatformCapturedScreenshot screenshot;
  bool called = false;
  int? lastMaxBytes;
  int? lastMaxDimension;

  @override
  Future<PlatformCapturedScreenshot> capture({
    required int maxBytes,
    required int maxDimension,
  }) async {
    called = true;
    lastMaxBytes = maxBytes;
    lastMaxDimension = maxDimension;
    return screenshot;
  }
}

class _FakeImageTransformer extends PlatformImageTransformer {
  _FakeImageTransformer(this.image);

  final PlatformTransformedImage image;
  bool called = false;
  String? lastBase64Data;
  String? lastOutputFormat;
  int? lastQuality;
  int? lastMaxWidth;
  int? lastMaxHeight;
  int? lastMaxBytes;

  @override
  Future<PlatformTransformedImage> transform({
    required String base64Data,
    required String outputFormat,
    required int quality,
    required int? maxWidth,
    required int? maxHeight,
    required int maxBytes,
  }) async {
    called = true;
    lastBase64Data = base64Data;
    lastOutputFormat = outputFormat;
    lastQuality = quality;
    lastMaxWidth = maxWidth;
    lastMaxHeight = maxHeight;
    lastMaxBytes = maxBytes;
    return image;
  }
}

class _FakeOcrRecognizer extends PlatformOcrRecognizer {
  _FakeOcrRecognizer(this.result);

  final PlatformOcrResult result;
  bool called = false;
  String? lastBase64Data;
  int? lastMaxBytes;
  String? lastLanguageHint;

  @override
  Future<PlatformOcrResult> recognize({
    required String base64Data,
    required int maxBytes,
    String? languageHint,
  }) async {
    called = true;
    lastBase64Data = base64Data;
    lastMaxBytes = maxBytes;
    lastLanguageHint = languageHint;
    return result;
  }
}

class _FakeSecureStorage implements SecureKeyValueStorage {
  final values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      values.remove(key);
      return;
    }
    values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

class _FakeMobileToolApproval implements MobileToolApprovalDelegate {
  _FakeMobileToolApproval(this.approved);

  final bool approved;
  MobileToolApprovalRequest? lastRequest;

  @override
  Future<bool> approve(MobileToolApprovalRequest request) async {
    lastRequest = request;
    return approved;
  }
}

void main() {
  group('MobileToolRuntime', () {
    const runtime = MobileToolRuntime();

    setUp(() {
      runtime.execute(
        const MobileToolCall(
          id: 'reset_todos',
          name: 'todo',
          arguments: {'action': 'clear'},
        ),
      );
      runtime.execute(
        const MobileToolCall(
          id: 'reset_board',
          name: 'tool_task_board',
          arguments: {'action': 'clear'},
        ),
      );
      runtime.execute(
        const MobileToolCall(
          id: 'reset_plans',
          name: 'agent_plan',
          arguments: {'action': 'clear'},
        ),
      );
    });

    test('marks local agent tools as mobile-compatible and advertises aliases',
        () {
      final tools = runtime.availableTools;
      expect(
        tools
            .where((tool) => [
                  'todo',
                  'tool_task_board',
                  'calculator',
                  'agent_plan',
                ].contains(tool.name))
            .every((tool) => tool.tags.contains(mobileCompatibleTag)),
        isTrue,
      );
      final openAiNames = runtime
          .openAiTools()
          .map((tool) => tool['function']['name'] as String)
          .toSet();
      expect(
        openAiNames,
        containsAll([
          'todo',
          'tool_todo',
          'tool_task_board',
          'tool_calculator',
          'tool_names',
          'tool_list',
          'tool_schema',
          'tool_invoke',
          'tool_batch',
          'package_install_plan',
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
          'tool_consent_check',
          'tool_consent_confirm',
          'artifact_file_list',
          'artifact_file_read',
          'artifact_file_write',
          'artifact_file_patch',
          'artifact_file_delete',
          'file_reader',
          'tool_file_reader',
          'browser_save_page',
          'webapp_preview',
          'webapp_lint',
          'webapp_build',
          'project_scaffold',
          'doc_create',
          'doc_update',
          'slides_create',
          'slides_from_markdown',
          'slides_update',
          'slides_export',
          'chart_create',
          'sheet_create',
          'sheet_read',
          'sheet_analyze',
          'sheet_update',
          'sheet_export',
          'artifact_zip',
          'research_report_export',
          'artifact_export',
          'static_site_export',
          'webapp_export_static',
          'doc_export',
          'pdf_export',
          'doc_to_pdf',
          'media_clipboard_read',
          'media_clipboard_write',
          'media_file_pick',
          'media_screenshot',
          'media_image_read',
          'media_image_transform',
          'media_ocr',
          'ocr_extract',
          'image_resize',
          'image_convert',
          'media_doc_parse',
          'media_pdf_parse',
          'pdf_extract',
          'pdf_extract_tables',
          'artifact_preview',
          'html_preview',
          'pdf_preview',
          'source_extract',
          'source_rank',
          'browser_extract_table',
          'tts_generate',
          'tts_generate_local',
          'image_render',
          'image_generate_local_or_provider',
          'audio_transcribe',
          'audio_transcribe_local',
          'mobile_platform_info',
          'mobile_json',
          'mobile_base64',
          'mobile_uuid',
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
          'prompt_validate_template',
          'prompt_render',
          'prompt_lint_prompt',
          'prompt_compact_prompt',
          'prompt_system_get',
          'prompt_system_set',
          'prompt_list',
          'prompt_create',
          'prompt_update',
          'prompt_delete',
          'prompt_active',
          'prompt_load_effective',
          'prompt_resolve_for_conversation',
          'prompt_preview_toggle',
          'prompt_test',
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
          'browser_open_url',
          'agent_plan',
          'agent_progress',
          'agent_status',
        ]),
      );
      expect(openAiNames, isNot(contains('defaultspack.tool.todo')));
    });

    test('runs phone-local platform utility tools', () {
      final platform = runtime.execute(
        const MobileToolCall(
          id: 'platform_1',
          name: 'mobile_platform_info',
          arguments: {},
        ),
      );
      expect(platform.ok, isTrue);
      final platformPayload =
          jsonDecode(platform.output) as Map<String, dynamic>;
      final platformData = platformPayload['data'] as Map<String, dynamic>;
      expect(
          platformData['supported_platforms'], containsAll(['ios', 'android']));
      expect(platformData['runtime_layers'], contains('flutter'));

      final jsonResult = runtime.execute(
        const MobileToolCall(
          id: 'json_1',
          name: 'mobile_json',
          arguments: {'action': 'minify', 'text': '{ "a": 1 }'},
        ),
      );
      expect(jsonResult.ok, isTrue);
      expect(jsonResult.output, '{"a":1}');

      final base64Result = runtime.execute(
        const MobileToolCall(
          id: 'base64_1',
          name: 'mobile_base64',
          arguments: {'action': 'decode', 'text': 'aGVsbG8='},
        ),
      );
      expect(base64Result.ok, isTrue);
      expect(base64Result.output, 'hello');

      final uuidResult = runtime.execute(
        const MobileToolCall(
          id: 'uuid_1',
          name: 'mobile_uuid',
          arguments: {'count': 2},
        ),
      );
      expect(uuidResult.ok, isTrue);
      final uuidPayload = jsonDecode(uuidResult.output) as Map<String, dynamic>;
      final uuidData = uuidPayload['data'] as Map<String, dynamic>;
      expect(uuidData['uuids'], hasLength(2));
    });

    test('runs phone-local AI model and provider settings tools', () async {
      final storage = _FakeSecureStorage();
      final approval = _FakeMobileToolApproval(true);
      final runtime = MobileToolRuntime(
        configStore: ApiConfigStore(storage: storage),
        approvalDelegate: approval,
      );

      final setKey = await runtime.executeAsync(
        const MobileToolCall(
          id: 'ai_key',
          name: 'ai_set_provider_key',
          arguments: {
            'provider': 'openai',
            'api_key': 'sk-test-secret-1234',
            'model': 'gpt-5.4',
            'favorite': true,
          },
        ),
      );
      expect(setKey.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'ai_set_provider_key');
      final setKeyData = (jsonDecode(setKey.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      final providerStatus = setKeyData['provider'] as Map<String, dynamic>;
      expect(providerStatus['has_api_key'], isTrue);
      expect(providerStatus.containsKey('api_key'), isFalse);
      expect(providerStatus['key_masked'], isNot(contains('secret')));

      final models = await runtime.executeAsync(
        const MobileToolCall(
          id: 'ai_models',
          name: 'ai_models',
          arguments: {'configured_only': true},
        ),
      );
      expect(models.ok, isTrue);
      final modelsData = (jsonDecode(models.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(modelsData['models'], isNotEmpty);
      expect(
        (modelsData['models'] as List).any(
          (entry) => entry is Map && entry['provider_id'] == 'openai',
        ),
        isTrue,
      );

      final setThinking = await runtime.executeAsync(
        const MobileToolCall(
          id: 'ai_thinking',
          name: 'ai_set_thinking_level',
          arguments: {'level': 'max'},
        ),
      );
      expect(setThinking.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'ai_set_thinking_level');

      final effective = await runtime.executeAsync(
        const MobileToolCall(
          id: 'ai_effective',
          name: 'ai_get_effective_thinking_level',
          arguments: {},
        ),
      );
      final effectiveData = (jsonDecode(effective.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(effectiveData['thinking_level'], 'xhigh');

      final route = await runtime.executeAsync(
        const MobileToolCall(
          id: 'ai_route',
          name: 'ai_route_model',
          arguments: {'provider': 'openai'},
        ),
      );
      expect(route.ok, isTrue);
      final routeData = (jsonDecode(route.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(routeData['selected_provider_id'], 'openai');
      expect(routeData['execution_location'], 'phone');
    });

    test('runs phone-local prompt system template and store tools', () async {
      final storage = _FakeSecureStorage();
      final approval = _FakeMobileToolApproval(true);
      final runtime = MobileToolRuntime(
        configStore: ApiConfigStore(storage: storage),
        approvalDelegate: approval,
      );

      final setSystem = await runtime.executeAsync(
        const MobileToolCall(
          id: 'prompt_system_set',
          name: 'prompt_system_set',
          arguments: {
            'system_prompt': 'You are concise about {{topic}}.',
          },
        ),
      );
      expect(setSystem.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'prompt_system_set');

      final render = await runtime.executeAsync(
        const MobileToolCall(
          id: 'prompt_render',
          name: 'prompt_render',
          arguments: {
            'template': 'Hello {{name}}',
            'variables': {'name': 'Rumi'},
          },
        ),
      );
      expect(render.ok, isTrue);
      final renderData = (jsonDecode(render.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(renderData['rendered'], 'Hello Rumi');

      final create = await runtime.executeAsync(
        const MobileToolCall(
          id: 'prompt_create',
          name: 'prompt_create',
          arguments: {
            'id': 'style',
            'title': 'Style',
            'content': 'Prefer short answers.',
          },
        ),
      );
      expect(create.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'prompt_create');

      final effective = await runtime.executeAsync(
        const MobileToolCall(
          id: 'prompt_effective',
          name: 'prompt_load_effective',
          arguments: {},
        ),
      );
      expect(effective.ok, isTrue);
      final effectiveData = (jsonDecode(effective.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(
        effectiveData['effective_prompt'],
        contains('You are concise about {{topic}}.'),
      );
      expect(
          effectiveData['effective_prompt'], contains('Prefer short answers.'));
      expect(effectiveData['runtime_layers'], contains('flutter'));

      final lint = await runtime.executeAsync(
        const MobileToolCall(
          id: 'prompt_lint',
          name: 'prompt_lint_prompt',
          arguments: {'prompt': 'Use {{missing}}.'},
        ),
      );
      expect(lint.ok, isFalse);
      final lintData = (jsonDecode(lint.output) as Map<String, dynamic>)['data']
          as Map<String, dynamic>;
      expect(lintData['issues'], isNotEmpty);
    });

    test('runs phone-local memory and memo store tools', () async {
      final storage = _FakeSecureStorage();
      final approval = _FakeMobileToolApproval(true);
      final runtime = MobileToolRuntime(
        configStore: ApiConfigStore(storage: storage),
        approvalDelegate: approval,
      );

      final stored = await runtime.executeAsync(
        const MobileToolCall(
          id: 'memory_store',
          name: 'memory_store',
          arguments: {
            'content': 'Haru prefers short Japanese status updates.',
            'tags': ['preference', 'language'],
            'importance': 0.9,
          },
        ),
      );
      expect(stored.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'memory_store');
      final storedData = (jsonDecode(stored.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      final memory = storedData['memory'] as Map<String, dynamic>;
      expect(memory['tags'], contains('preference'));

      final recall = await runtime.executeAsync(
        const MobileToolCall(
          id: 'memory_recall',
          name: 'memory_recall',
          arguments: {'query': 'Japanese updates'},
        ),
      );
      expect(recall.ok, isTrue);
      final recallData = (jsonDecode(recall.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(recallData['memories'], isNotEmpty);
      expect(recallData['runtime_layers'], contains('mobile-memory-store'));

      final compact = await runtime.executeAsync(
        const MobileToolCall(
          id: 'memory_compact',
          name: 'memory_compact',
          arguments: {'max_chars': 120},
        ),
      );
      expect(compact.ok, isTrue);
      final compactData = (jsonDecode(compact.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(compactData['summary'], contains('Haru'));

      final folder = await runtime.executeAsync(
        const MobileToolCall(
          id: 'memo_folder',
          name: 'memory_memo_folders',
          arguments: {'action': 'create', 'id': 'ideas', 'title': 'Ideas'},
        ),
      );
      expect(folder.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'memory_memo_folders');

      final note = await runtime.executeAsync(
        const MobileToolCall(
          id: 'memo_note',
          name: 'memory_memo_notes',
          arguments: {
            'action': 'create',
            'folder_id': 'ideas',
            'title': 'Mobile tools',
            'content': 'Keep phone tools unified with PC names.',
          },
        ),
      );
      expect(note.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'memory_memo_notes');

      final notes = await runtime.executeAsync(
        const MobileToolCall(
          id: 'memo_notes',
          name: 'memory_memo_notes',
          arguments: {'action': 'list', 'folder_id': 'ideas'},
        ),
      );
      final notesData = (jsonDecode(notes.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(notesData['notes'], hasLength(1));
    });

    test('runs phone-local knowledge store search and import tools', () async {
      final storage = _FakeSecureStorage();
      final approval = _FakeMobileToolApproval(true);
      final runtime = MobileToolRuntime(
        configStore: ApiConfigStore(storage: storage),
        approvalDelegate: approval,
      );

      final created = await runtime.executeAsync(
        const MobileToolCall(
          id: 'knowledge_create',
          name: 'knowledge_create',
          arguments: {
            'id': 'mobile-tools',
            'title': 'Mobile tools',
            'content': 'Phone tools should keep the same names as PC tools.',
            'tags': ['mobile', 'tools'],
            'project_id': 'rumi',
          },
        ),
      );
      expect(created.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'knowledge_create');
      final createdData = (jsonDecode(created.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(createdData['knowledge']['index_terms'], contains('mobile'));

      final search = await runtime.executeAsync(
        const MobileToolCall(
          id: 'knowledge_search',
          name: 'knowledge_search',
          arguments: {'query': 'same names', 'project_id': 'rumi'},
        ),
      );
      expect(search.ok, isTrue);
      final searchData = (jsonDecode(search.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(searchData['results'], isNotEmpty);
      expect(searchData['runtime_layers'], contains('mobile-knowledge-store'));

      final imported = await runtime.executeAsync(
        const MobileToolCall(
          id: 'knowledge_import_url',
          name: 'knowledge_import_url',
          arguments: {
            'url': 'https://example.com/rumi-mobile',
            'content': 'Rumi mobile imports URL references as knowledge.',
          },
        ),
      );
      expect(imported.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'knowledge_import_url');

      final attach = await runtime.executeAsync(
        const MobileToolCall(
          id: 'knowledge_attach',
          name: 'knowledge_attach_to_project',
          arguments: {'id': 'mobile-tools', 'project_id': 'mobile'},
        ),
      );
      expect(attach.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'knowledge_attach_to_project');

      final index = await runtime.executeAsync(
        const MobileToolCall(
          id: 'knowledge_index',
          name: 'knowledge_index',
          arguments: {'id': 'mobile-tools'},
        ),
      );
      expect(index.ok, isTrue);
      final indexData = (jsonDecode(index.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(indexData['indexed_count'], 1);
    });

    test('runs defaultspack todo-compatible actions on phone', () {
      final added = runtime.execute(
        const MobileToolCall(
          id: 'todo_1',
          name: 'tool_todo',
          arguments: {
            'action': 'add',
            'title': 'Write mobile agent tests',
          },
        ),
      );

      expect(added.ok, isTrue);
      expect(added.summary, contains('Write mobile agent tests'));
      final payload = jsonDecode(added.output) as Map<String, dynamic>;
      final changed = payload['changed'] as Map<String, dynamic>;

      final completed = runtime.execute(
        MobileToolCall(
          id: 'todo_2',
          name: 'todo',
          arguments: {
            'action': 'complete',
            'todo_id': changed['id'],
          },
        ),
      );

      expect(completed.ok, isTrue);
      expect(completed.output, contains('"status":"done"'));
    });

    test('runs defaultspack calculator function alias on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'calc_1',
          name: 'tool_calculator',
          arguments: {'expression': '6 * 7'},
        ),
      );

      expect(result.ok, isTrue);
      expect(result.output, contains('6 * 7 = 42'));
    });

    test('runs defaultspack consent check and confirm on phone', () {
      final check = runtime.execute(
        const MobileToolCall(
          id: 'consent_check_1',
          name: 'tool_consent_check',
          arguments: {
            'text': 'この株の投資判断について教えて',
          },
        ),
      );

      expect(check.ok, isTrue);
      final checkPayload = jsonDecode(check.output) as Map<String, dynamic>;
      final checkData = checkPayload['data'] as Map<String, dynamic>;
      expect(checkData['requires_consent'], isTrue);
      expect(checkData['categories'], contains('investment'));
      final consentId = checkData['consent_id'] as String;

      final confirm = runtime.execute(
        MobileToolCall(
          id: 'consent_confirm_1',
          name: 'defaultspack.tool.consent_confirm',
          arguments: {
            'consent_id': consentId,
            'accepted': true,
          },
        ),
      );

      expect(confirm.ok, isTrue);
      final confirmPayload = jsonDecode(confirm.output) as Map<String, dynamic>;
      final confirmData = confirmPayload['data'] as Map<String, dynamic>;
      expect(confirmData['consent_id'], consentId);
      expect(confirmData['accepted'], isTrue);

      final schema = runtime.execute(
        const MobileToolCall(
          id: 'consent_schema_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'tool_consent_check'},
        ),
      );
      final schemaPayload = jsonDecode(schema.output) as Map<String, dynamic>;
      final schemaData = schemaPayload['data'] as Map<String, dynamic>;
      expect(schemaData['mobile_compatible'], isTrue);
      expect(schemaData['execution_route'], 'phone');
      expect(schemaData['callable'], isTrue);
    });

    test('runs phone-local artifact workspace tools with approval', () async {
      final approval = _FakeMobileToolApproval(true);
      final artifactRuntime = MobileToolRuntime(approvalDelegate: approval);
      const path = 'notes/hello.txt';

      final write = await artifactRuntime.executeAsync(
        const MobileToolCall(
          id: 'artifact_write_1',
          name: 'artifact_file_write',
          arguments: {
            'path': path,
            'content': 'hello mobile artifact',
          },
        ),
      );

      expect(write.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'artifact_file_write');
      final writePayload = jsonDecode(write.output) as Map<String, dynamic>;
      final writeData = writePayload['data'] as Map<String, dynamic>;
      expect(writeData['path'], path);
      expect(writeData['workspace'], 'phone');
      expect(writeData['checkpoint'], isA<Map>());

      final read = artifactRuntime.execute(
        const MobileToolCall(
          id: 'artifact_read_1',
          name: 'artifact_file_read',
          arguments: {'path': path},
        ),
      );
      expect(read.ok, isTrue);
      final readPayload = jsonDecode(read.output) as Map<String, dynamic>;
      final readData = readPayload['data'] as Map<String, dynamic>;
      expect(readData['content'], 'hello mobile artifact');
      expect(readData['runtime_layers'], containsAll(['flutter', 'dart']));

      final fileRead = artifactRuntime.execute(
        const MobileToolCall(
          id: 'file_reader_1',
          name: 'tool_file_reader',
          arguments: {
            'path': path,
            'start_line': 1,
            'end_line': 1,
            'max_chars': 10,
          },
        ),
      );
      expect(fileRead.ok, isTrue);
      final fileReadPayload =
          jsonDecode(fileRead.output) as Map<String, dynamic>;
      final fileReadData = fileReadPayload['data'] as Map<String, dynamic>;
      expect(fileReadData['content'], 'hello mobi');
      expect(fileReadData['truncated'], isTrue);
      expect(fileReadData['runtime_layers'], contains('mobile-media-artifact'));

      final patch = await artifactRuntime.executeAsync(
        const MobileToolCall(
          id: 'artifact_patch_1',
          name: 'artifact_file_patch',
          arguments: {
            'path': path,
            'old_text': 'mobile',
            'new_text': 'phone',
            'expected_replacements': 1,
          },
        ),
      );
      expect(patch.ok, isTrue);
      final patchPayload = jsonDecode(patch.output) as Map<String, dynamic>;
      final patchData = patchPayload['data'] as Map<String, dynamic>;
      expect(patchData['patched'], isTrue);
      expect(patchData['replacements'], 1);

      final list = artifactRuntime.execute(
        const MobileToolCall(
          id: 'artifact_list_1',
          name: 'artifact_file_list',
          arguments: {'path': '.', 'recursive': true},
        ),
      );
      expect(list.ok, isTrue);
      final listPayload = jsonDecode(list.output) as Map<String, dynamic>;
      final listData = listPayload['data'] as Map<String, dynamic>;
      final entries = listData['entries'] as List;
      expect(entries.map((entry) => entry['path']), contains(path));

      final delete = await artifactRuntime.executeAsync(
        const MobileToolCall(
          id: 'artifact_delete_1',
          name: 'artifact_file_delete',
          arguments: {'path': path},
        ),
      );
      expect(delete.ok, isTrue);
      final deletePayload = jsonDecode(delete.output) as Map<String, dynamic>;
      final deleteData = deletePayload['data'] as Map<String, dynamic>;
      expect(deleteData['deleted'], isTrue);
    });

    test('artifact workspace mutations fail closed without approval', () async {
      final artifactRuntime = MobileToolRuntime(
        approvalDelegate: _FakeMobileToolApproval(false),
      );
      final result = await artifactRuntime.executeAsync(
        const MobileToolCall(
          id: 'artifact_write_denied_1',
          name: 'artifact_file_write',
          arguments: {
            'path': 'denied.txt',
            'content': 'nope',
          },
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'MOBILE_APPROVAL_REQUIRED');
    });

    test('saves and previews phone-local webapp HTML artifacts', () {
      const html = '<!doctype html><html><head><title>Phone Site</title>'
          '<meta name="viewport" content="width=device-width"></head>'
          '<body><main>Hello webapp</main></body></html>';
      final save = runtime.execute(
        const MobileToolCall(
          id: 'browser_save_page_1',
          name: 'browser_save_page',
          arguments: {
            'html': html,
            'output_path': 'webapps/phone/index.html',
          },
        ),
      );

      expect(save.ok, isTrue);
      final savePayload = jsonDecode(save.output) as Map<String, dynamic>;
      final saveData = savePayload['data'] as Map<String, dynamic>;
      expect(saveData['path'], 'webapps/phone/index.html');
      expect(saveData['workspace'], 'phone');

      final preview = runtime.execute(
        const MobileToolCall(
          id: 'webapp_preview_1',
          name: 'webapp_preview',
          arguments: {'path': 'webapps/phone'},
        ),
      );
      expect(preview.ok, isTrue);
      final previewPayload = jsonDecode(preview.output) as Map<String, dynamic>;
      final previewData = previewPayload['data'] as Map<String, dynamic>;
      expect(previewData['path'], 'webapps/phone/index.html');
      expect(previewData['title'], 'Phone Site');
      expect(previewData['text'], contains('Hello webapp'));
      expect(previewData['screenshot_supported'], isFalse);
      expect(previewData['runtime_layers'], containsAll(['flutter', 'dart']));

      final lint = runtime.execute(
        const MobileToolCall(
          id: 'webapp_lint_1',
          name: 'webapp_lint',
          arguments: {'path': 'webapps/phone'},
        ),
      );
      expect(lint.ok, isTrue);
      final lintPayload = jsonDecode(lint.output) as Map<String, dynamic>;
      final lintData = lintPayload['data'] as Map<String, dynamic>;
      expect(lintData['ok'], isTrue);
      expect(lintData['issues'], isEmpty);
      expect(lintData['warnings'], isEmpty);
    });

    test('webapp_lint reports missing phone-local index html', () {
      final lint = runtime.execute(
        const MobileToolCall(
          id: 'webapp_lint_missing_1',
          name: 'webapp_lint',
          arguments: {'path': 'webapps/missing'},
        ),
      );

      expect(lint.ok, isTrue);
      final payload = jsonDecode(lint.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['ok'], isFalse);
      expect(data['issues'], contains('missing index.html'));
    });

    test('plans packages builds static webapps and exports research reports',
        () async {
      final buildRuntime = MobileToolRuntime(
        approvalDelegate: _FakeMobileToolApproval(true),
      );

      final plan = buildRuntime.execute(
        const MobileToolCall(
          id: 'package_plan_1',
          name: 'package_install_plan',
          arguments: {
            'manager': 'pnpm',
            'packages': ['vite', 'react'],
            'dev': true,
          },
        ),
      );
      expect(plan.ok, isTrue);
      final planPayload = jsonDecode(plan.output) as Map<String, dynamic>;
      final planData = planPayload['data'] as Map<String, dynamic>;
      expect(planData['dry_run'], isTrue);
      expect(planData['command'], ['pnpm', 'add', '-D', 'vite', 'react']);

      final scaffold = buildRuntime.execute(
        const MobileToolCall(
          id: 'build_scaffold_1',
          name: 'project_scaffold',
          arguments: {'name': 'Build Demo', 'template': 'plain_js'},
        ),
      );
      expect(scaffold.ok, isTrue);

      final built = await buildRuntime.executeAsync(
        const MobileToolCall(
          id: 'webapp_build_1',
          name: 'webapp_build',
          arguments: {'path': 'webapps/build-demo'},
        ),
      );
      expect(built.ok, isTrue);
      final builtPayload = jsonDecode(built.output) as Map<String, dynamic>;
      final builtData = builtPayload['data'] as Map<String, dynamic>;
      expect(builtData['path'], 'webapps/build-demo/build.rumi.json');
      expect(builtData['requires_mobile_approval'], isTrue);

      final reportSource = buildRuntime.execute(
        const MobileToolCall(
          id: 'report_source_1',
          name: 'doc_create',
          arguments: {
            'title': 'Research Notes',
            'content': 'finding one',
            'output_path': 'reports/research.md',
          },
        ),
      );
      expect(reportSource.ok, isTrue);

      final report = buildRuntime.execute(
        const MobileToolCall(
          id: 'research_export_1',
          name: 'research_report_export',
          arguments: {
            'path': 'reports/research.md',
            'format': 'html',
            'output_path': 'exports/research.html',
          },
        ),
      );
      expect(report.ok, isTrue);
      final reportPayload = jsonDecode(report.output) as Map<String, dynamic>;
      final reportData = reportPayload['data'] as Map<String, dynamic>;
      expect(reportData['path'], 'exports/research.html');
      expect(reportData['format'], 'html');
    });

    test('creates phone-local project document slides and chart artifacts', () {
      final scaffold = runtime.execute(
        const MobileToolCall(
          id: 'project_scaffold_1',
          name: 'project_scaffold',
          arguments: {'name': 'Phone Demo', 'template': 'plain_js'},
        ),
      );
      expect(scaffold.ok, isTrue);
      final scaffoldPayload =
          jsonDecode(scaffold.output) as Map<String, dynamic>;
      final scaffoldData = scaffoldPayload['data'] as Map<String, dynamic>;
      expect(scaffoldData['path'], 'webapps/phone-demo');
      expect(scaffoldData['files'], contains('webapps/phone-demo/index.html'));

      final doc = runtime.execute(
        const MobileToolCall(
          id: 'doc_create_1',
          name: 'doc_create',
          arguments: {
            'title': 'Mobile Notes',
            'content': 'hello docs',
          },
        ),
      );
      expect(doc.ok, isTrue);
      final docPayload = jsonDecode(doc.output) as Map<String, dynamic>;
      final docData = docPayload['data'] as Map<String, dynamic>;
      expect(docData['path'], 'documents/mobile-notes.md');
      expect(docData['format'], 'md');

      final slides = runtime.execute(
        const MobileToolCall(
          id: 'slides_from_markdown_1',
          name: 'slides_from_markdown',
          arguments: {
            'markdown': '# Intro\n- One\n# Next\n- Two',
          },
        ),
      );
      expect(slides.ok, isTrue);
      final slidesPayload = jsonDecode(slides.output) as Map<String, dynamic>;
      final slidesData = slidesPayload['data'] as Map<String, dynamic>;
      expect(slidesData['path'], 'slides/deck.slides.json');
      expect(slidesData['slides'], 2);
      expect(slidesData['format'], 'slide_outline_json');

      final chart = runtime.execute(
        const MobileToolCall(
          id: 'chart_create_1',
          name: 'chart_create',
          arguments: {
            'title': 'Mobile Chart',
            'values': [1, 3, 2],
            'labels': ['A', 'B', 'C'],
          },
        ),
      );
      expect(chart.ok, isTrue);
      final chartPayload = jsonDecode(chart.output) as Map<String, dynamic>;
      final chartData = chartPayload['data'] as Map<String, dynamic>;
      expect(chartData['path'], 'charts/chart.svg');
      expect(chartData['format'], 'svg');
      expect(chartData['runtime_layers'], containsAll(['flutter', 'dart']));

      final readChart = runtime.execute(
        const MobileToolCall(
          id: 'chart_read_1',
          name: 'artifact_file_read',
          arguments: {'path': 'charts/chart.svg'},
        ),
      );
      final readPayload = jsonDecode(readChart.output) as Map<String, dynamic>;
      final readData = readPayload['data'] as Map<String, dynamic>;
      expect(readData['content'], contains('<svg'));
      expect(readData['content'], contains('Mobile Chart'));
    });

    test('updates phone-local documents and slide outlines', () async {
      final editRuntime = MobileToolRuntime(
        approvalDelegate: _FakeMobileToolApproval(true),
      );

      final doc = editRuntime.execute(
        const MobileToolCall(
          id: 'doc_update_source_1',
          name: 'doc_create',
          arguments: {
            'title': 'Editable Notes',
            'content': 'first',
          },
        ),
      );
      expect(doc.ok, isTrue);

      final docUpdate = await editRuntime.executeAsync(
        const MobileToolCall(
          id: 'doc_update_1',
          name: 'doc_update',
          arguments: {
            'path': 'documents/editable-notes.md',
            'append': 'second',
          },
        ),
      );
      expect(docUpdate.ok, isTrue);
      final docUpdatePayload =
          jsonDecode(docUpdate.output) as Map<String, dynamic>;
      final docUpdateData = docUpdatePayload['data'] as Map<String, dynamic>;
      expect(docUpdateData['requires_mobile_approval'], isTrue);

      final docRead = editRuntime.execute(
        const MobileToolCall(
          id: 'doc_update_read_1',
          name: 'artifact_file_read',
          arguments: {'path': 'documents/editable-notes.md'},
        ),
      );
      final docReadPayload = jsonDecode(docRead.output) as Map<String, dynamic>;
      final docReadData = docReadPayload['data'] as Map<String, dynamic>;
      expect(docReadData['content'], contains('first'));
      expect(docReadData['content'], contains('second'));

      final slidesCreate = editRuntime.execute(
        const MobileToolCall(
          id: 'slides_create_1',
          name: 'slides_create',
          arguments: {
            'title': 'Mobile Deck',
            'slides': [
              {
                'title': 'Intro',
                'bullets': ['One']
              },
            ],
          },
        ),
      );
      expect(slidesCreate.ok, isTrue);
      final slidesCreatePayload =
          jsonDecode(slidesCreate.output) as Map<String, dynamic>;
      final slidesCreateData =
          slidesCreatePayload['data'] as Map<String, dynamic>;
      expect(slidesCreateData['path'], 'slides/mobile-deck.slides.json');
      expect(slidesCreateData['slides'], 1);

      final slidesUpdate = await editRuntime.executeAsync(
        const MobileToolCall(
          id: 'slides_update_1',
          name: 'slides_update',
          arguments: {
            'path': 'slides/mobile-deck.slides.json',
            'slides': [
              {
                'title': 'Updated',
                'bullets': ['Two', 'Three']
              },
            ],
          },
        ),
      );
      expect(slidesUpdate.ok, isTrue);
      final slidesUpdatePayload =
          jsonDecode(slidesUpdate.output) as Map<String, dynamic>;
      final slidesUpdateData =
          slidesUpdatePayload['data'] as Map<String, dynamic>;
      expect(slidesUpdateData['requires_mobile_approval'], isTrue);

      final slidesExport = editRuntime.execute(
        const MobileToolCall(
          id: 'slides_export_1',
          name: 'slides_export',
          arguments: {
            'path': 'slides/mobile-deck.slides.json',
            'format': 'md',
            'output_path': 'exports/mobile-deck.md',
          },
        ),
      );
      expect(slidesExport.ok, isTrue);
      final slidesExportPayload =
          jsonDecode(slidesExport.output) as Map<String, dynamic>;
      final slidesExportData =
          slidesExportPayload['data'] as Map<String, dynamic>;
      expect(slidesExportData['path'], 'exports/mobile-deck.md');
      expect(slidesExportData['format'], 'md');

      final exportedRead = editRuntime.execute(
        const MobileToolCall(
          id: 'slides_export_read_1',
          name: 'artifact_file_read',
          arguments: {'path': 'exports/mobile-deck.md'},
        ),
      );
      final exportedPayload =
          jsonDecode(exportedRead.output) as Map<String, dynamic>;
      final exportedData = exportedPayload['data'] as Map<String, dynamic>;
      expect(exportedData['content'], contains('Updated'));
      expect(exportedData['content'], contains('Three'));
    });

    test('phone-local document generators reject binary output formats', () {
      final docx = runtime.execute(
        const MobileToolCall(
          id: 'doc_create_docx_1',
          name: 'doc_create',
          arguments: {
            'title': 'Binary',
            'output_path': 'documents/binary.docx',
          },
        ),
      );
      expect(docx.ok, isFalse);
      expect(docx.output, contains('UNSUPPORTED_PHONE_DOCUMENT_FORMAT'));

      final pptx = runtime.execute(
        const MobileToolCall(
          id: 'slides_pptx_1',
          name: 'slides_from_markdown',
          arguments: {
            'markdown': '# Slide',
            'output_path': 'slides/deck.pptx',
          },
        ),
      );
      expect(pptx.ok, isFalse);
      expect(pptx.output, contains('UNSUPPORTED_PHONE_SLIDE_FORMAT'));

      final slidesCreatePptx = runtime.execute(
        const MobileToolCall(
          id: 'slides_create_pptx_1',
          name: 'slides_create',
          arguments: {'output_path': 'slides/deck.pptx'},
        ),
      );
      expect(slidesCreatePptx.ok, isFalse);
      expect(slidesCreatePptx.output, contains('PC_DELEGATION_REQUIRED'));

      final png = runtime.execute(
        const MobileToolCall(
          id: 'chart_png_1',
          name: 'chart_create',
          arguments: {'output_path': 'charts/chart.png'},
        ),
      );
      expect(png.ok, isFalse);
      expect(png.output, contains('UNSUPPORTED_PHONE_CHART_FORMAT'));
    });

    test('runs phone-local sheet create read analyze update and export',
        () async {
      final sheetRuntime = MobileToolRuntime(
        approvalDelegate: _FakeMobileToolApproval(true),
      );

      final create = sheetRuntime.execute(
        const MobileToolCall(
          id: 'sheet_create_1',
          name: 'sheet_create',
          arguments: {
            'output_path': 'sheets/mobile.csv',
            'columns': ['name', 'score'],
            'rows': [
              ['a', '1'],
              ['b', '3'],
              ['c', ''],
            ],
          },
        ),
      );
      expect(create.ok, isTrue);
      final createPayload = jsonDecode(create.output) as Map<String, dynamic>;
      final createData = createPayload['data'] as Map<String, dynamic>;
      expect(createData['path'], 'sheets/mobile.csv');
      expect(createData['rows'], 4);
      expect(createData['format'], 'csv');

      final read = sheetRuntime.execute(
        const MobileToolCall(
          id: 'sheet_read_1',
          name: 'sheet_read',
          arguments: {'path': 'sheets/mobile.csv', 'limit': 2},
        ),
      );
      expect(read.ok, isTrue);
      final readPayload = jsonDecode(read.output) as Map<String, dynamic>;
      final readData = readPayload['data'] as Map<String, dynamic>;
      expect(readData['row_count'], 4);
      expect(readData['returned_rows'], 2);

      final analyze = sheetRuntime.execute(
        const MobileToolCall(
          id: 'sheet_analyze_1',
          name: 'sheet_analyze',
          arguments: {'path': 'sheets/mobile.csv'},
        ),
      );
      expect(analyze.ok, isTrue);
      final analyzePayload = jsonDecode(analyze.output) as Map<String, dynamic>;
      final analyzeData = analyzePayload['data'] as Map<String, dynamic>;
      final numeric = analyzeData['numeric'] as Map<String, dynamic>;
      expect(analyzeData['headers'], ['name', 'score']);
      expect(analyzeData['missing_values'], 1);
      expect(numeric['count'], 2);
      expect(numeric['mean'], 2);

      final export = sheetRuntime.execute(
        const MobileToolCall(
          id: 'sheet_export_1',
          name: 'sheet_export',
          arguments: {
            'path': 'sheets/mobile.csv',
            'format': 'html',
            'output_path': 'exports/mobile-sheet.html',
          },
        ),
      );
      expect(export.ok, isTrue);
      final exportPayload = jsonDecode(export.output) as Map<String, dynamic>;
      final exportData = exportPayload['data'] as Map<String, dynamic>;
      expect(exportData['path'], 'exports/mobile-sheet.html');
      expect(exportData['format'], 'html');

      final update = await sheetRuntime.executeAsync(
        const MobileToolCall(
          id: 'sheet_update_1',
          name: 'sheet_update',
          arguments: {
            'path': 'sheets/mobile.csv',
            'rows': [
              ['name', 'score'],
              ['z', '5'],
            ],
          },
        ),
      );
      expect(update.ok, isTrue);
      final updatePayload = jsonDecode(update.output) as Map<String, dynamic>;
      final updateData = updatePayload['data'] as Map<String, dynamic>;
      expect(updateData['rows'], 2);
      expect(updateData['requires_mobile_approval'], isTrue);

      final updatedRead = sheetRuntime.execute(
        const MobileToolCall(
          id: 'sheet_read_updated_1',
          name: 'sheet_read',
          arguments: {'path': 'sheets/mobile.csv'},
        ),
      );
      final updatedPayload =
          jsonDecode(updatedRead.output) as Map<String, dynamic>;
      final updatedData = updatedPayload['data'] as Map<String, dynamic>;
      expect(updatedData['rows'], [
        ['name', 'score'],
        ['z', '5'],
      ]);
    });

    test('phone-local sheet tools reject binary spreadsheet formats', () {
      final create = runtime.execute(
        const MobileToolCall(
          id: 'sheet_create_xlsx_1',
          name: 'sheet_create',
          arguments: {'output_path': 'sheets/native.xlsx'},
        ),
      );
      expect(create.ok, isFalse);
      expect(create.output, contains('UNSUPPORTED_PHONE_SHEET_FORMAT'));

      final source = runtime.execute(
        const MobileToolCall(
          id: 'sheet_create_source_1',
          name: 'sheet_create',
          arguments: {
            'output_path': 'sheets/binary-source.csv',
            'rows': [
              ['a', '1'],
            ],
          },
        ),
      );
      expect(source.ok, isTrue);

      final export = runtime.execute(
        const MobileToolCall(
          id: 'sheet_export_xlsx_1',
          name: 'sheet_export',
          arguments: {
            'path': 'sheets/binary-source.csv',
            'format': 'xlsx',
          },
        ),
      );
      expect(export.ok, isFalse);
      expect(export.output, contains('UNSUPPORTED_PHONE_SHEET_FORMAT'));
    });

    test('exports phone-local artifacts and webapps as text or base64 zip', () {
      final scaffold = runtime.execute(
        const MobileToolCall(
          id: 'export_scaffold_1',
          name: 'project_scaffold',
          arguments: {'name': 'Export Demo'},
        ),
      );
      expect(scaffold.ok, isTrue);

      final zip = runtime.execute(
        const MobileToolCall(
          id: 'artifact_zip_1',
          name: 'artifact_zip',
          arguments: {
            'path': 'webapps/export-demo',
            'output_path': 'exports/export-demo.zip',
          },
        ),
      );
      expect(zip.ok, isTrue);
      final zipPayload = jsonDecode(zip.output) as Map<String, dynamic>;
      final zipData = zipPayload['data'] as Map<String, dynamic>;
      expect(zipData['path'], 'exports/export-demo.zip');
      expect(zipData['encoding'], 'base64');
      expect(zipData['mime_type'], 'application/zip');
      final zipBytes = base64Decode(zipData['base64'] as String);
      expect(zipBytes.take(4).toList(), [0x50, 0x4b, 0x03, 0x04]);

      final readZip = runtime.execute(
        const MobileToolCall(
          id: 'artifact_zip_read_1',
          name: 'artifact_file_read',
          arguments: {'path': 'exports/export-demo.zip'},
        ),
      );
      final readPayload = jsonDecode(readZip.output) as Map<String, dynamic>;
      final readData = readPayload['data'] as Map<String, dynamic>;
      expect(readData['encoding'], 'base64');
      expect(readData['mime_type'], 'application/zip');

      final htmlExport = runtime.execute(
        const MobileToolCall(
          id: 'artifact_export_html_1',
          name: 'artifact_export',
          arguments: {
            'path': 'webapps/export-demo/index.html',
            'format': 'html',
            'output_path': 'exports/index.html',
          },
        ),
      );
      expect(htmlExport.ok, isTrue);
      final htmlPayload = jsonDecode(htmlExport.output) as Map<String, dynamic>;
      final htmlData = htmlPayload['data'] as Map<String, dynamic>;
      expect(htmlData['path'], 'exports/index.html');
      expect(htmlData['format'], 'html');

      final staticZip = runtime.execute(
        const MobileToolCall(
          id: 'static_site_export_1',
          name: 'static_site_export',
          arguments: {'path': 'webapps/export-demo'},
        ),
      );
      expect(staticZip.ok, isTrue);
      expect(staticZip.output, contains('exports/static-site.zip'));

      final webappZip = runtime.execute(
        const MobileToolCall(
          id: 'webapp_export_static_1',
          name: 'webapp_export_static',
          arguments: {
            'path': 'webapps/export-demo',
            'output_path': 'exports/webapp.zip',
          },
        ),
      );
      expect(webappZip.ok, isTrue);
      expect(webappZip.output, contains('exports/webapp.zip'));
    });

    test('phone-local document export supports text formats and rejects PDF',
        () {
      final doc = runtime.execute(
        const MobileToolCall(
          id: 'doc_export_source_1',
          name: 'doc_create',
          arguments: {
            'title': 'Exported Doc',
            'content': 'phone export',
          },
        ),
      );
      expect(doc.ok, isTrue);

      final html = runtime.execute(
        const MobileToolCall(
          id: 'doc_export_html_1',
          name: 'doc_export',
          arguments: {
            'path': 'documents/exported-doc.md',
            'format': 'html',
          },
        ),
      );
      expect(html.ok, isTrue);
      expect(html.output, contains('exports/exported-doc.html'));

      final pdf = runtime.execute(
        const MobileToolCall(
          id: 'pdf_export_1',
          name: 'pdf_export',
          arguments: {'path': 'documents/exported-doc.md'},
        ),
      );
      expect(pdf.ok, isFalse);
      expect(pdf.output, contains('PC_DELEGATION_REQUIRED'));

      final docPdf = runtime.execute(
        const MobileToolCall(
          id: 'doc_to_pdf_1',
          name: 'doc_to_pdf',
          arguments: {'path': 'documents/exported-doc.md'},
        ),
      );
      expect(docPdf.ok, isFalse);
      expect(docPdf.output, contains('PC_DELEGATION_REQUIRED'));
    });

    test('runs mobile clipboard tools after explicit phone approval', () async {
      final approval = _FakeMobileToolApproval(true);
      final clipboard = _FakeClipboard('clipboard secret');
      final runtime = MobileToolRuntime(
        approvalDelegate: approval,
        clipboard: clipboard,
      );

      final read = await runtime.executeAsync(
        const MobileToolCall(
          id: 'clipboard_read_1',
          name: 'media_clipboard_read',
          arguments: {'reason': 'Use pasted text in the answer'},
        ),
      );

      expect(read.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'media_clipboard_read');
      final readPayload = jsonDecode(read.output) as Map<String, dynamic>;
      final readData = readPayload['data'] as Map<String, dynamic>;
      expect(readData['content'], 'clipboard secret');
      expect(readData['requires_mobile_approval'], isTrue);

      final write = await runtime.executeAsync(
        const MobileToolCall(
          id: 'clipboard_write_1',
          name: 'defaultspack.media.clipboard_write',
          arguments: {'text': 'new clipboard', 'reason': 'Copy result'},
        ),
      );

      expect(write.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'media_clipboard_write');
      expect(clipboard.text, 'new clipboard');
      final writePayload = jsonDecode(write.output) as Map<String, dynamic>;
      final writeData = writePayload['data'] as Map<String, dynamic>;
      expect(writeData['written'], isTrue);
    });

    test('mobile clipboard tools fail closed without approval', () async {
      final approval = _FakeMobileToolApproval(false);
      final clipboard = _FakeClipboard('private');
      final runtime = MobileToolRuntime(
        approvalDelegate: approval,
        clipboard: clipboard,
      );

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'clipboard_denied_1',
          name: 'media_clipboard_read',
          arguments: {},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'MOBILE_APPROVAL_REQUIRED');
    });

    test('runs phone media picker tool after explicit phone approval',
        () async {
      final approval = _FakeMobileToolApproval(true);
      final picker = _FakeMediaPicker(
        const PlatformPickedMediaFile(
          name: 'voice-note.txt',
          mimeType: 'text/plain',
          size: 5,
          base64Data: 'aGVsbG8=',
        ),
      );
      final runtime = MobileToolRuntime(
        approvalDelegate: approval,
        mediaPicker: picker,
      );

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'file_pick_1',
          name: 'defaultspack_media_file_pick',
          arguments: {
            'kind': 'photo',
            'reason': 'Use selected file in the answer',
            'max_bytes': 1024,
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'media_file_pick');
      expect(picker.called, isTrue);
      expect(picker.lastKind, 'image');
      expect(picker.lastMaxBytes, 1024);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['name'], 'voice-note.txt');
      expect(data['mime_type'], 'text/plain');
      expect(data['base64'], 'aGVsbG8=');
      expect(data['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(data['requires_mobile_approval'], isTrue);
    });

    test('phone media picker fails closed without approval', () async {
      final approval = _FakeMobileToolApproval(false);
      final picker = _FakeMediaPicker(
        const PlatformPickedMediaFile(
          name: 'ignored.txt',
          mimeType: 'text/plain',
          size: 7,
          base64Data: 'aWdub3JlZA==',
        ),
      );
      final runtime = MobileToolRuntime(
        approvalDelegate: approval,
        mediaPicker: picker,
      );

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'file_pick_denied_1',
          name: 'media_file_pick',
          arguments: {'kind': 'file'},
        ),
      );

      expect(result.ok, isFalse);
      expect(picker.called, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'MOBILE_APPROVAL_REQUIRED');
    });

    test('runs phone app screenshot tool after explicit phone approval',
        () async {
      final approval = _FakeMobileToolApproval(true);
      final capture = _FakeScreenshotCapture(
        const PlatformCapturedScreenshot(
          mimeType: 'image/png',
          size: 12,
          width: 320,
          height: 640,
          base64Data: 'iVBORw0KGgo=',
        ),
      );
      final runtime = MobileToolRuntime(
        approvalDelegate: approval,
        screenshotCapture: capture,
      );

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'screenshot_1',
          name: 'defaultspack.media.screenshot',
          arguments: {
            'reason': 'Inspect the current Rumi app screen',
            'max_bytes': 2048,
            'max_dimension': 900,
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(approval.lastRequest?.toolName, 'media_screenshot');
      expect(capture.called, isTrue);
      expect(capture.lastMaxBytes, 2048);
      expect(capture.lastMaxDimension, 900);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['mime_type'], 'image/png');
      expect(data['base64'], 'iVBORw0KGgo=');
      expect(data['capture_scope'], 'app_window');
      expect(data['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(data['requires_mobile_approval'], isTrue);
    });

    test('phone app screenshot fails closed without approval', () async {
      final approval = _FakeMobileToolApproval(false);
      final capture = _FakeScreenshotCapture(
        const PlatformCapturedScreenshot(
          mimeType: 'image/png',
          size: 8,
          width: 100,
          height: 100,
          base64Data: 'ignored',
        ),
      );
      final runtime = MobileToolRuntime(
        approvalDelegate: approval,
        screenshotCapture: capture,
      );

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'screenshot_denied_1',
          name: 'media_screenshot',
          arguments: {},
        ),
      );

      expect(result.ok, isFalse);
      expect(capture.called, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'MOBILE_APPROVAL_REQUIRED');
    });

    test('reads phone-provided image metadata through media_image_read', () {
      final pngHeader = base64Encode([
        0x89,
        0x50,
        0x4e,
        0x47,
        0x0d,
        0x0a,
        0x1a,
        0x0a,
        0x00,
        0x00,
        0x00,
        0x0d,
        0x49,
        0x48,
        0x44,
        0x52,
        0x00,
        0x00,
        0x00,
        0x02,
        0x00,
        0x00,
        0x00,
        0x03,
      ]);

      final result = runtime.execute(
        MobileToolCall(
          id: 'image_read_1',
          name: 'defaultspack.media.image_read',
          arguments: {
            'image': {
              'base64': 'data:image/png;base64,$pngHeader',
            },
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['width'], 2);
      expect(data['height'], 3);
      expect(data['format'], 'png');
      expect(data['mime_type'], 'image/png');
      expect(data['execution_location'], 'phone');
      expect(data['requires_mobile_approval'], isFalse);
    });

    test('media_image_read does not read host paths on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'image_read_path_1',
          name: 'media_image_read',
          arguments: {'path': '/tmp/image.png'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
    });

    test('transforms phone-provided image bytes through media_image_transform',
        () async {
      final pngHeader = base64Encode([
        0x89,
        0x50,
        0x4e,
        0x47,
        0x0d,
        0x0a,
        0x1a,
        0x0a,
        0x00,
        0x00,
        0x00,
        0x0d,
        0x49,
        0x48,
        0x44,
        0x52,
        0x00,
        0x00,
        0x00,
        0x04,
        0x00,
        0x00,
        0x00,
        0x03,
      ]);
      final transformedBase64 = base64Encode([1, 2, 3, 4]);
      final transformer = _FakeImageTransformer(
        PlatformTransformedImage(
          mimeType: 'image/jpeg',
          size: 4,
          width: 2,
          height: 2,
          base64Data: transformedBase64,
        ),
      );
      final runtime = MobileToolRuntime(imageTransformer: transformer);

      final result = await runtime.executeAsync(
        MobileToolCall(
          id: 'image_transform_1',
          name: 'defaultspack.media.image_transform',
          arguments: {
            'image': {
              'base64': 'data:image/png;base64,$pngHeader',
            },
            'operations': [
              {'type': 'resize', 'width': 2, 'height': 2},
            ],
            'format': 'jpeg',
            'quality': 80,
            'max_bytes': 1024,
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(transformer.called, isTrue);
      expect(transformer.lastOutputFormat, 'jpeg');
      expect(transformer.lastQuality, 80);
      expect(transformer.lastMaxWidth, 2);
      expect(transformer.lastMaxHeight, 2);
      expect(transformer.lastMaxBytes, 1024);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['width'], 2);
      expect(data['height'], 2);
      expect(data['format'], 'jpeg');
      expect(data['mime_type'], 'image/jpeg');
      expect(data['base64'], transformedBase64);
      expect(data['operations_applied'], contains('resize'));
      expect(data['operations_applied'], contains('encode:jpeg'));
      expect(data['execution_location'], 'phone');
      expect(data['requires_mobile_approval'], isFalse);
      expect(data['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
    });

    test('media_image_transform does not read host paths on phone', () async {
      final transformer = _FakeImageTransformer(
        PlatformTransformedImage(
          mimeType: 'image/png',
          size: 1,
          width: 1,
          height: 1,
          base64Data: base64Encode([1]),
        ),
      );
      final runtime = MobileToolRuntime(imageTransformer: transformer);

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'image_transform_path_1',
          name: 'media_image_transform',
          arguments: {'path': '/tmp/image.png'},
        ),
      );

      expect(result.ok, isFalse);
      expect(transformer.called, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
    });

    test('runs image_resize through the phone native image bridge', () async {
      final pngHeader = base64Encode([
        0x89,
        0x50,
        0x4e,
        0x47,
        0x0d,
        0x0a,
        0x1a,
        0x0a,
        0x00,
        0x00,
        0x00,
        0x0d,
        0x49,
        0x48,
        0x44,
        0x52,
        0x00,
        0x00,
        0x00,
        0x02,
        0x00,
        0x00,
        0x00,
        0x02,
      ]);
      final transformedBase64 = base64Encode([9, 8, 7]);
      final transformer = _FakeImageTransformer(
        PlatformTransformedImage(
          mimeType: 'image/png',
          size: 3,
          width: 320,
          height: 200,
          base64Data: transformedBase64,
        ),
      );
      final runtime = MobileToolRuntime(imageTransformer: transformer);

      final result = await runtime.executeAsync(
        MobileToolCall(
          id: 'image_resize_1',
          name: 'image_resize',
          arguments: {
            'base64': 'data:image/png;base64,$pngHeader',
            'width': 320,
            'height': 200,
            'format': 'png',
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(transformer.called, isTrue);
      expect(transformer.lastMaxWidth, 320);
      expect(transformer.lastMaxHeight, 200);
      expect(transformer.lastOutputFormat, 'png');
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['base64'], transformedBase64);
      expect(data['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
    });

    test('runs image_convert through the phone native image bridge', () async {
      final pngHeader = base64Encode([
        0x89,
        0x50,
        0x4e,
        0x47,
        0x0d,
        0x0a,
        0x1a,
        0x0a,
        0x00,
        0x00,
        0x00,
        0x0d,
        0x49,
        0x48,
        0x44,
        0x52,
        0x00,
        0x00,
        0x00,
        0x02,
        0x00,
        0x00,
        0x00,
        0x02,
      ]);
      final transformedBase64 = base64Encode([4, 5, 6]);
      final transformer = _FakeImageTransformer(
        PlatformTransformedImage(
          mimeType: 'image/jpeg',
          size: 3,
          width: 100,
          height: 50,
          base64Data: transformedBase64,
        ),
      );
      final runtime = MobileToolRuntime(imageTransformer: transformer);

      final result = await runtime.executeAsync(
        MobileToolCall(
          id: 'image_convert_1',
          name: 'image_convert',
          arguments: {
            'base64': 'data:image/png;base64,$pngHeader',
            'output_path': 'converted.jpg',
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(transformer.called, isTrue);
      expect(transformer.lastOutputFormat, 'jpeg');
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['mime_type'], 'image/jpeg');
      expect(data['format'], 'jpeg');
      expect(data['base64'], transformedBase64);
    });

    test('runs media_ocr through the phone native OCR bridge', () async {
      final imageBase64 = base64Encode([
        0x89,
        0x50,
        0x4e,
        0x47,
        0x0d,
        0x0a,
        0x1a,
        0x0a,
        0x00,
        0x00,
        0x00,
        0x0d,
        0x49,
        0x48,
        0x44,
        0x52,
        0x00,
        0x00,
        0x00,
        0x03,
        0x00,
        0x00,
        0x00,
        0x02,
      ]);
      final recognizer = _FakeOcrRecognizer(
        const PlatformOcrResult(
          text: 'Hello OCR',
          languageCode: 'en',
          blocks: [
            PlatformOcrBlock(
              text: 'Hello OCR',
              confidence: 0.91,
              boundingBox: {'x': 0.1, 'y': 0.2, 'width': 0.7, 'height': 0.1},
            ),
          ],
        ),
      );
      final runtime = MobileToolRuntime(ocrRecognizer: recognizer);

      final result = await runtime.executeAsync(
        MobileToolCall(
          id: 'media_ocr_1',
          name: 'defaultspack.media.ocr',
          arguments: {
            'data_url': 'data:image/png;base64,$imageBase64',
            'language_hint': 'en',
            'max_bytes': 2048,
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(recognizer.called, isTrue);
      expect(recognizer.lastBase64Data, imageBase64);
      expect(recognizer.lastMaxBytes, 2048);
      expect(recognizer.lastLanguageHint, 'en');
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final blocks = data['blocks'] as List<dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['text'], 'Hello OCR');
      expect(data['content'], 'Hello OCR');
      expect(data['language_code'], 'en');
      expect(blocks.single, containsPair('text', 'Hello OCR'));
      expect(metadata['tool'], 'media_ocr');
      expect(metadata['native_ocr'], isTrue);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
    });

    test('ocr_extract rejects host artifact paths on phone', () async {
      final recognizer = _FakeOcrRecognizer(
        const PlatformOcrResult(
            text: 'ignored', blocks: [], languageCode: null),
      );
      final runtime = MobileToolRuntime(ocrRecognizer: recognizer);

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'ocr_extract_path_1',
          name: 'ocr_extract',
          arguments: {'path': '/tmp/image.png'},
        ),
      );

      expect(result.ok, isFalse);
      expect(recognizer.called, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
      expect(error['path'], '/tmp/image.png');
    });

    test('generates phone-local TTS fallback WAV payload', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'tts_generate_1',
          name: 'tts_generate',
          arguments: {
            'text': 'hello mobile',
            'duration_ms': 100,
            'sample_rate': 16000,
            'output_path': 'audio/hello.wav',
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      final wav = base64Decode(data['base64'] as String);
      expect(ascii.decode(wav.sublist(0, 4)), 'RIFF');
      expect(ascii.decode(wav.sublist(8, 12)), 'WAVE');
      expect(data['mime_type'], 'audio/wav');
      expect(data['fallback'], 'silent_wav');
      expect(data['sample_rate'], 16000);
      expect(data['duration_ms'], 100);
      expect(data['requested_output_path'], 'audio/hello.wav');
      expect(metadata['payload_only'], isTrue);
      expect(metadata['real_tts_supported'], isFalse);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('tts_generate_local shares the phone-local WAV fallback', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'tts_generate_local_1',
          name: 'tts_generate_local',
          arguments: {'text': 'local'},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      final wav = base64Decode(data['base64'] as String);
      expect(ascii.decode(wav.sublist(0, 4)), 'RIFF');
      expect(metadata['tool'], 'tts_generate_local');
    });

    test('renders phone-local image artifacts and audio transcript payloads',
        () {
      final rendered = runtime.execute(
        const MobileToolCall(
          id: 'image_render_1',
          name: 'image_render',
          arguments: {
            'text': 'Hello from phone image render',
            'output_path': 'renders/mobile-tool-test.svg',
            'width': 480,
            'height': 320,
          },
        ),
      );
      expect(rendered.ok, isTrue);
      final renderedData = (jsonDecode(rendered.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(renderedData['path'], 'renders/mobile-tool-test.svg');
      expect(renderedData['format'], 'svg');
      expect(renderedData['runtime_layers'], contains('mobile-media-artifact'));

      final read = runtime.execute(
        const MobileToolCall(
          id: 'read_render_1',
          name: 'artifact_file_read',
          arguments: {'path': 'renders/mobile-tool-test.svg'},
        ),
      );
      expect(read.ok, isTrue);
      final readData = (jsonDecode(read.output) as Map<String, dynamic>)['data']
          as Map<String, dynamic>;
      expect(readData['content'], contains('<svg'));

      final generated = runtime.execute(
        const MobileToolCall(
          id: 'image_generate_1',
          name: 'image_generate_local_or_provider',
          arguments: {
            'prompt': 'a simple mobile placeholder',
            'output_path': 'images/generated-tool-test.svg',
          },
        ),
      );
      expect(generated.ok, isTrue);
      final generatedData = (jsonDecode(generated.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(generatedData['provider_backed'], isFalse);
      expect(generatedData['fallback'], 'phone_svg_placeholder');

      final transcript = runtime.execute(
        const MobileToolCall(
          id: 'audio_transcribe_1',
          name: 'audio_transcribe',
          arguments: {'transcript': 'hello audio', 'language': 'en'},
        ),
      );
      expect(transcript.ok, isTrue);
      final transcriptData = (jsonDecode(transcript.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(transcriptData['text'], 'hello audio');
      expect(transcriptData['fallback'], 'provided_transcript');

      final audioBytes = runtime.execute(
        MobileToolCall(
          id: 'audio_transcribe_local_1',
          name: 'audio_transcribe_local',
          arguments: {
            'audio_base64': base64Encode([0, 1, 2, 3])
          },
        ),
      );
      expect(audioBytes.ok, isTrue);
      final audioData = (jsonDecode(audioBytes.output)
          as Map<String, dynamic>)['data'] as Map<String, dynamic>;
      expect(audioData['text'], '');
      expect(
          audioData['fallback'], 'native_or_provider_transcription_required');
      expect(audioData['runtime_layers'], contains('mobile-media-artifact'));
    });

    test('parses phone-provided text documents through media_doc_parse', () {
      final markdown = '# Hello\n\nmobile docs';
      final result = runtime.execute(
        MobileToolCall(
          id: 'doc_parse_1',
          name: 'defaultspack.media.doc_parse',
          arguments: {
            'file': {
              'name': 'note.md',
              'mime_type': 'text/markdown',
              'base64': base64Encode(utf8.encode(markdown)),
            },
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['content'], markdown);
      expect(metadata['format'], 'markdown');
      expect(metadata['name'], 'note.md');
      expect(metadata['mime_type'], 'text/markdown');
      expect(data['execution_location'], 'phone');
      expect(data['requires_mobile_approval'], isFalse);
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('media_doc_parse strips simple html when requested', () {
      final html = '<html><body><h1>Title</h1><p>A &amp; B</p></body></html>';
      final result = runtime.execute(
        MobileToolCall(
          id: 'doc_parse_html_1',
          name: 'media_doc_parse',
          arguments: {
            'data_url':
                'data:text/html;base64,${base64Encode(utf8.encode(html))}',
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['content'], contains('Title'));
      expect(data['content'], contains('A & B'));
      expect(data['content'], isNot(contains('<h1>')));
    });

    test('media_doc_parse does not read host paths on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'doc_parse_path_1',
          name: 'media_doc_parse',
          arguments: {'path': '/tmp/note.md'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
    });

    test('media_doc_parse rejects binary document formats on phone', () {
      final result = runtime.execute(
        MobileToolCall(
          id: 'doc_parse_pdf_1',
          name: 'media_doc_parse',
          arguments: {
            'file': {
              'name': 'report.pdf',
              'mime_type': 'application/pdf',
              'base64': base64Encode(utf8.encode('%PDF-1.7')),
            },
          },
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_DOCUMENT_FORMAT');
      expect(error['message'], contains('PC/provider'));
    });

    test('extracts best-effort text from phone-provided PDF bytes', () {
      final pdf = '%PDF-1.4\n'
          '1 0 obj <<>> stream\n'
          'BT /F1 12 Tf 72 720 Td (Hello mobile PDF) Tj ET\n'
          'endstream endobj\n'
          '%%EOF';
      final result = runtime.execute(
        MobileToolCall(
          id: 'pdf_parse_1',
          name: 'defaultspack.media.pdf_parse',
          arguments: {
            'file': {
              'name': 'hello.pdf',
              'mime_type': 'application/pdf',
              'base64': base64Encode(latin1.encode(pdf)),
            },
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['content'], contains('Hello mobile PDF'));
      expect(metadata['format'], 'pdf');
      expect(metadata['best_effort'], isTrue);
      expect(metadata['full_layout_supported'], isFalse);
      expect(metadata['method'], 'literal_and_hex_strings');
      expect(data['execution_location'], 'phone');
      expect(data['requires_mobile_approval'], isFalse);
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('media_pdf_parse does not read host paths on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'pdf_parse_path_1',
          name: 'media_pdf_parse',
          arguments: {'path': '/tmp/report.pdf'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
    });

    test('pdf_extract returns best-effort text from phone-provided bytes', () {
      final pdf = '%PDF-1.4\nBT (PDF extract text) Tj ET\n%%EOF';
      final result = runtime.execute(
        MobileToolCall(
          id: 'pdf_extract_1',
          name: 'pdf_extract',
          arguments: {'pdf_base64': base64Encode(latin1.encode(pdf))},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['text'], contains('PDF extract text'));
      expect(metadata['payload_only'], isTrue);
      expect(metadata['tables_supported'], isFalse);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('pdf_extract_tables returns explicit phone fallback', () {
      final pdf = '%PDF-1.4\nBT (Table-ish PDF text) Tj ET\n%%EOF';
      final result = runtime.execute(
        MobileToolCall(
          id: 'pdf_extract_tables_1',
          name: 'pdf_extract_tables',
          arguments: {'pdf_base64': base64Encode(latin1.encode(pdf))},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['tables'], isEmpty);
      expect(data['table_count'], 0);
      expect(metadata['tables_supported'], isFalse);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('artifact_preview previews text and HTML payloads on phone', () {
      const html = '<html><head><title>Preview</title></head>'
          '<body><p>Hello <strong>artifact</strong></p></body></html>';
      final result = runtime.execute(
        const MobileToolCall(
          id: 'artifact_preview_html_1',
          name: 'artifact_preview',
          arguments: {'html': html, 'max_chars': 80},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['kind'], 'html');
      expect(data['content'], contains('<html'));
      expect(data['text'], contains('Hello artifact'));
      expect(metadata['title'], 'Preview');
      expect(metadata['payload_only'], isTrue);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('artifact_preview previews image metadata payloads on phone', () {
      final pngHeader = base64Encode([
        0x89,
        0x50,
        0x4e,
        0x47,
        0x0d,
        0x0a,
        0x1a,
        0x0a,
        0x00,
        0x00,
        0x00,
        0x0d,
        0x49,
        0x48,
        0x44,
        0x52,
        0x00,
        0x00,
        0x00,
        0x05,
        0x00,
        0x00,
        0x00,
        0x04,
      ]);
      final result = runtime.execute(
        MobileToolCall(
          id: 'artifact_preview_image_1',
          name: 'artifact_preview',
          arguments: {'image_base64': pngHeader},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['kind'], 'image');
      expect(data['format'], 'png');
      expect(data['width'], 5);
      expect(data['height'], 4);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('artifact_preview previews PDF payloads on phone', () {
      final pdf = '%PDF-1.4\nBT (Artifact PDF preview) Tj ET\n%%EOF';
      final result = runtime.execute(
        MobileToolCall(
          id: 'artifact_preview_pdf_1',
          name: 'artifact_preview',
          arguments: {
            'file': {
              'name': 'preview.pdf',
              'mime_type': 'application/pdf',
              'base64': base64Encode(latin1.encode(pdf)),
            },
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['kind'], 'pdf');
      expect(data['content'], contains('Artifact PDF preview'));
      expect(metadata['payload_only'], isTrue);
      expect(metadata['screenshot_supported'], isFalse);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('artifact_preview does not read host artifact paths on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'artifact_preview_path_1',
          name: 'artifact_preview',
          arguments: {'path': '/tmp/artifact.html'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
      expect(error['path'], '/tmp/artifact.html');
    });

    test('html_preview previews HTML payload metadata on phone', () {
      const html = '<html><head><title>HTML Tool</title></head>'
          '<body><p>Hi <b>preview</b></p></body></html>';
      final result = runtime.execute(
        const MobileToolCall(
          id: 'html_preview_1',
          name: 'html_preview',
          arguments: {
            'html': html,
            'viewport': {'width': 390, 'height': 844},
            'full_page': false,
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      final viewport = data['viewport'] as Map<String, dynamic>;
      expect(data['kind'], 'html');
      expect(data['title'], 'HTML Tool');
      expect(data['text'], contains('Hi preview'));
      expect(viewport['width'], 390);
      expect(viewport['height'], 844);
      expect(data['full_page'], isFalse);
      expect(data['screenshot_supported'], isFalse);
      expect(metadata['payload_only'], isTrue);
      expect(metadata['preview_mode'], 'html_payload');
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('html_preview does not read host artifact paths on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'html_preview_path_1',
          name: 'html_preview',
          arguments: {'path': '/tmp/preview.html'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
      expect(error['path'], '/tmp/preview.html');
    });

    test('pdf_preview previews PDF payload metadata on phone', () {
      final pdf = '%PDF-1.4\nBT (PDF preview text) Tj ET\n%%EOF';
      final result = runtime.execute(
        MobileToolCall(
          id: 'pdf_preview_1',
          name: 'pdf_preview',
          arguments: {'pdf_base64': base64Encode(latin1.encode(pdf))},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['kind'], 'pdf');
      expect(data['content'], contains('PDF preview text'));
      expect(data['screenshot_supported'], isFalse);
      expect(metadata['payload_only'], isTrue);
      expect(metadata['preview_mode'], 'pdf_payload');
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('pdf_preview does not read host artifact paths on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'pdf_preview_path_1',
          name: 'pdf_preview',
          arguments: {'path': '/tmp/preview.pdf'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
      expect(error['path'], '/tmp/preview.pdf');
    });

    test('extracts provided HTML source payload on phone', () {
      const html = '<html><head><title>Source Title</title></head>'
          '<body><h1>Hello</h1><p>mobile research &amp; ranking</p></body></html>';
      final result = runtime.execute(
        const MobileToolCall(
          id: 'source_extract_1',
          name: 'source_extract',
          arguments: {'html': html},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final metadata = data['metadata'] as Map<String, dynamic>;
      expect(data['title'], 'Source Title');
      expect(data['content'], contains('Hello'));
      expect(data['content'], contains('mobile research & ranking'));
      expect(data['content'], isNot(contains('<h1>')));
      expect(metadata['payload_only'], isTrue);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('source_extract does not read host paths on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'source_extract_path_1',
          name: 'source_extract',
          arguments: {'path': '/tmp/source.html'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_PATH');
    });

    test('extracts HTML tables from payload on phone', () {
      const html = '''
        <html><body>
          <table>
            <tr><th>Name</th><th>Score</th></tr>
            <tr><td>Alice &amp; Bob</td><td><strong>9</strong></td></tr>
          </table>
          <table><tr><td>Only</td></tr></table>
        </body></html>
      ''';
      final result = runtime.execute(
        const MobileToolCall(
          id: 'browser_extract_table_1',
          name: 'browser_extract_table',
          arguments: {'html': html, 'table_index': 1},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final tables = data['tables'] as List<dynamic>;
      final selected = data['selected_table'] as List<dynamic>;
      expect(data['table_count'], 2);
      expect((tables.first as List<dynamic>).first, ['Name', 'Score']);
      expect((tables.first as List<dynamic>)[1], ['Alice & Bob', '9']);
      expect(selected.single, ['Only']);
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('browser_extract_table does not read browser URLs or host paths', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'browser_extract_table_path_1',
          name: 'browser_extract_table',
          arguments: {'path': '/tmp/page.html'},
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      expect(error['code'], 'UNSUPPORTED_SOURCE');
      expect(error['source'], '/tmp/page.html');
    });

    test('ranks provided source snippets on phone', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'source_rank_1',
          name: 'source_rank',
          arguments: {
            'query': 'mobile tool',
            'sources': [
              {
                'title': 'B',
                'content': 'mobile only',
                'source': 'b',
              },
              {
                'title': 'A',
                'content': 'mobile tool mobile',
                'source': 'a',
              },
            ],
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final ranked = data['ranked_sources'] as List<dynamic>;
      expect((ranked.first as Map<String, dynamic>)['score'], 3);
      expect(
        (((ranked.first as Map<String, dynamic>)['source']
            as Map<String, dynamic>)['source']),
        'a',
      );
      expect(data['execution_location'], 'phone');
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
    });

    test('runs mobile-compatible tools through defaultspack tool_invoke', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'invoke_1',
          name: 'tool_invoke',
          arguments: {
            'tool_name': 'tool_calculator',
            'arguments': {'expression': '8 * 8'},
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(result.summary, contains('calculator'));
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['tool_name'], 'calculator');
      expect(data['execution_location'], 'phone');
      expect(data['result'], contains('8 * 8 = 64'));
    });

    test('runs browser_open_url as a phone native URL tool', () async {
      final opened = <Uri>[];
      final runtime = MobileToolRuntime(urlLauncher: _FakeUrlLauncher(opened));

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'url_1',
          name: 'tool_invoke',
          arguments: {
            'tool_name': 'browser_open_url',
            'arguments': {'url': 'https://example.com/mobile'},
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(opened.single.host, 'example.com');
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['tool_name'], 'mobile_url_open');
      expect(data['execution_location'], 'phone');

      final schema = runtime.execute(
        const MobileToolCall(
          id: 'schema_url_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'browser_open_url'},
        ),
      );
      final schemaPayload = jsonDecode(schema.output) as Map<String, dynamic>;
      final schemaData = schemaPayload['data'] as Map<String, dynamic>;
      final mobile = schemaData['mobile'] as Map<String, dynamic>;
      expect(schemaData['mobile_compatible'], isTrue);
      expect(schemaData['tool_id'], 'mobile_url_open');
      expect(mobile['runtime_layers'],
          containsAll(['ios-swift', 'android-kotlin']));
      expect(schemaData['tags'], contains(mobileSwiftNativeTag));
      expect(schemaData['tags'], contains(mobileKotlinNativeTag));
    });

    test('tool_invoke returns specific reasons for host-bound tools', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'invoke_host_1',
          name: 'defaultspack.tool.invoke',
          arguments: {
            'tool_name': 'python_exec',
            'arguments': {'code': 'print(1)'},
          },
        ),
      );

      expect(result.ok, isFalse);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final error = payload['error'] as Map<String, dynamic>;
      final details = error['details'] as Map<String, dynamic>;
      expect(error['code'], 'TOOL_UNAVAILABLE_ON_PHONE');
      expect(error['message'], contains('PC側'));
      expect(details['tool_id'], 'python_exec');
      expect(details['tags'], contains('tool_registry'));
    });

    test('executeAsync delegates host-bound tool_invoke to PC when enabled',
        () async {
      final delegate = _FakePcToolDelegate();
      final runtime = MobileToolRuntime(pcDelegate: delegate);

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'invoke_pc_1',
          name: 'tool_invoke',
          arguments: {
            'tool_name': 'python_exec',
            'arguments': {'code': 'print(1)'},
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(result.output, contains('execution_location'));
      expect(delegate.lastCall?.name, 'tool_invoke');
    });

    test('executeAsync delegates direct host-bound defaultspack tool names',
        () async {
      final delegate = _FakePcToolDelegate();
      final runtime = MobileToolRuntime(pcDelegate: delegate);

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'agent_multi_1',
          name: 'agent_multi_execute',
          arguments: {'task': 'summarize the current thread'},
        ),
      );

      expect(result.ok, isTrue);
      expect(delegate.lastCall?.name, 'agent_multi_execute');
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['execution_location'], 'pc');
    });

    test('executeAsync delegates binary export tools to PC when enabled',
        () async {
      final delegate = _FakePcToolDelegate();
      final runtime = MobileToolRuntime(pcDelegate: delegate);

      final direct = await runtime.executeAsync(
        const MobileToolCall(
          id: 'pdf_export_pc_1',
          name: 'pdf_export',
          arguments: {'path': 'documents/report.md'},
        ),
      );

      expect(direct.ok, isTrue);
      expect(delegate.lastCall?.name, 'pdf_export');

      final viaInvoke = await runtime.executeAsync(
        const MobileToolCall(
          id: 'doc_to_pdf_pc_1',
          name: 'tool_invoke',
          arguments: {
            'tool_name': 'doc_to_pdf',
            'arguments': {'path': 'documents/report.md'},
          },
        ),
      );

      expect(viaInvoke.ok, isTrue);
      expect(delegate.lastCall?.name, 'tool_invoke');

      final slidePptx = await runtime.executeAsync(
        const MobileToolCall(
          id: 'slides_create_pc_1',
          name: 'slides_create',
          arguments: {
            'title': 'PC Deck',
            'output_path': 'slides/pc-deck.pptx',
          },
        ),
      );

      expect(slidePptx.ok, isTrue);
      expect(delegate.lastCall?.name, 'slides_create');

      final webappCommand = await runtime.executeAsync(
        const MobileToolCall(
          id: 'webapp_build_pc_1',
          name: 'webapp_build',
          arguments: {
            'path': 'webapps/app',
            'command': ['pnpm', 'build'],
          },
        ),
      );

      expect(webappCommand.ok, isTrue);
      expect(delegate.lastCall?.name, 'webapp_build');
    });

    test(
        'tool_batch runs phone tools and delegates PC tools through one surface',
        () async {
      final delegate = _FakePcToolDelegate();
      final runtime = MobileToolRuntime(pcDelegate: delegate);

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'batch_1',
          name: 'tool_batch',
          arguments: {
            'parallel': true,
            'calls': [
              {
                'id': 'calc',
                'tool_name': 'tool_calculator',
                'arguments': {'expression': '7 * 6'},
              },
              {
                'id': 'pc',
                'tool_name': 'python_exec',
                'arguments': {'code': 'print(42)'},
              },
            ],
          },
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      final results = data['results'] as List<dynamic>;
      expect(data['parallel'], isTrue);
      expect(data['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(results, hasLength(2));
      final calc = results.first as Map<String, dynamic>;
      final pc = results.last as Map<String, dynamic>;
      expect(calc['ok'], isTrue);
      expect(calc['tool_name'], 'calculator');
      expect(calc['execution_location'], 'phone');
      expect(calc['output'], contains('7 * 6 = 42'));
      expect(pc['ok'], isTrue);
      expect(pc['tool_name'], 'python_exec');
      expect(pc['execution_location'], 'pc');
      expect(delegate.lastCall?.name, 'python_exec');
    });

    test('runs phone-local artifact-backed job records', () async {
      final jobRuntime = MobileToolRuntime(
        approvalDelegate: _FakeMobileToolApproval(true),
      );

      final created = jobRuntime.execute(
        const MobileToolCall(
          id: 'job_create_1',
          name: 'job_create',
          arguments: {
            'job_id': 'Morning Report',
            'kind': 'research',
            'query': 'mobile tools',
            'run_immediately': true,
          },
        ),
      );
      expect(created.ok, isTrue);
      final createdPayload = jsonDecode(created.output) as Map<String, dynamic>;
      final createdData = createdPayload['data'] as Map<String, dynamic>;
      expect(createdData['job_id'], 'morning-report');
      expect(createdData['status'], 'completed');
      expect(
          createdData['artifacts'], contains('jobs/morning-report/job.json'));
      expect(createdData['artifacts'],
          contains('jobs/morning-report/result.json'));

      final status = jobRuntime.execute(
        const MobileToolCall(
          id: 'job_status_1',
          name: 'job_status',
          arguments: {'job_id': 'morning-report'},
        ),
      );
      final statusPayload = jsonDecode(status.output) as Map<String, dynamic>;
      final statusData = statusPayload['data'] as Map<String, dynamic>;
      expect(statusData['status'], 'completed');
      expect(statusData['runtime_layers'], containsAll(['flutter', 'dart']));

      final history = jobRuntime.execute(
        const MobileToolCall(
          id: 'job_history_1',
          name: 'job_history',
          arguments: {'job_id': 'morning-report'},
        ),
      );
      final historyPayload = jsonDecode(history.output) as Map<String, dynamic>;
      final historyData = historyPayload['data'] as Map<String, dynamic>;
      expect(historyData['count'], 2);

      final artifacts = jobRuntime.execute(
        const MobileToolCall(
          id: 'job_artifacts_1',
          name: 'job_artifacts',
          arguments: {'job_id': 'morning-report'},
        ),
      );
      final artifactsPayload =
          jsonDecode(artifacts.output) as Map<String, dynamic>;
      final artifactsData = artifactsPayload['data'] as Map<String, dynamic>;
      expect(artifactsData['count'], 2);

      final cancel = await jobRuntime.executeAsync(
        const MobileToolCall(
          id: 'job_cancel_1',
          name: 'job_cancel',
          arguments: {'job_id': 'morning-report'},
        ),
      );
      expect(cancel.ok, isTrue);
      final cancelPayload = jsonDecode(cancel.output) as Map<String, dynamic>;
      final cancelData = cancelPayload['data'] as Map<String, dynamic>;
      expect(cancelData['status'], 'canceled');
      expect(cancelData['requires_mobile_approval'], isTrue);

      final resume = await jobRuntime.executeAsync(
        const MobileToolCall(
          id: 'job_resume_1',
          name: 'job_resume',
          arguments: {'job_id': 'morning-report'},
        ),
      );
      expect(resume.ok, isTrue);
      final resumePayload = jsonDecode(resume.output) as Map<String, dynamic>;
      final resumeData = resumePayload['data'] as Map<String, dynamic>;
      expect(resumeData['status'], 'queued');
      expect(resumeData['requires_mobile_approval'], isTrue);

      final listed = jobRuntime.execute(
        const MobileToolCall(
          id: 'job_status_list_1',
          name: 'job_status',
          arguments: {},
        ),
      );
      final listedPayload = jsonDecode(listed.output) as Map<String, dynamic>;
      final listedData = listedPayload['data'] as Map<String, dynamic>;
      expect(listedData['count'], greaterThanOrEqualTo(1));
    });

    test('runs phone-local workflow records through unified tool surface',
        () async {
      final workflowRuntime = MobileToolRuntime(
        approvalDelegate: _FakeMobileToolApproval(true),
      );

      final defined = workflowRuntime.execute(
        const MobileToolCall(
          id: 'workflow_define_1',
          name: 'workflow_define',
          arguments: {
            'workflow_id': 'Daily Check',
            'name': 'Daily Check',
            'steps': [
              {
                'id': 'calc',
                'tool': 'calculator',
                'arguments': {'expression': '2+2'},
              },
              {
                'id': 'write',
                'tool': 'artifact_file_write',
                'arguments': {
                  'path': 'workflow/daily.txt',
                  'content': 'done',
                },
              },
            ],
          },
        ),
      );
      expect(defined.ok, isTrue);
      final definedPayload = jsonDecode(defined.output) as Map<String, dynamic>;
      final definedData = definedPayload['data'] as Map<String, dynamic>;
      expect(definedData['workflow_id'], 'daily-check');
      expect(definedData['runtime_layers'],
          containsAll(['flutter', 'dart', 'mobile-workflow-record']));

      final run = await workflowRuntime.executeAsync(
        const MobileToolCall(
          id: 'workflow_run_1',
          name: 'workflow_run',
          arguments: {'workflow_id': 'daily-check'},
        ),
      );
      expect(run.ok, isTrue);
      final runPayload = jsonDecode(run.output) as Map<String, dynamic>;
      final runData = runPayload['data'] as Map<String, dynamic>;
      final runId = runData['run_id'] as String;
      final results = runData['results'] as List;
      expect(runData['status'], 'completed');
      expect(runData['requires_mobile_approval'], isTrue);
      expect(results, hasLength(2));
      expect(results.every((result) => result['ok'] == true), isTrue);
      expect(runData['runtime_layers'],
          containsAll(['flutter', 'dart', 'mobile-workflow-record']));

      final status = workflowRuntime.execute(
        MobileToolCall(
          id: 'workflow_status_1',
          name: 'workflow_status',
          arguments: {'run_id': runId},
        ),
      );
      final statusPayload = jsonDecode(status.output) as Map<String, dynamic>;
      final statusData = statusPayload['data'] as Map<String, dynamic>;
      expect(statusData['status'], 'completed');
      expect(statusData['event_count'], greaterThanOrEqualTo(5));

      final record = workflowRuntime.execute(
        MobileToolCall(
          id: 'workflow_record_read_1',
          name: 'artifact_file_read',
          arguments: {'path': 'workflows/runs/$runId.json'},
        ),
      );
      expect(record.ok, isTrue);

      final cancel = await workflowRuntime.executeAsync(
        MobileToolCall(
          id: 'workflow_cancel_1',
          name: 'workflow_cancel',
          arguments: {'run_id': runId},
        ),
      );
      expect(cancel.ok, isTrue);
      final cancelPayload = jsonDecode(cancel.output) as Map<String, dynamic>;
      final cancelData = cancelPayload['data'] as Map<String, dynamic>;
      expect(cancelData['status'], 'cancelled');
      expect(cancelData['requires_mobile_approval'], isTrue);

      final retry = await workflowRuntime.executeAsync(
        const MobileToolCall(
          id: 'workflow_retry_1',
          name: 'workflow_retry',
          arguments: {'workflow_id': 'daily-check'},
        ),
      );
      expect(retry.ok, isTrue);
      final retryPayload = jsonDecode(retry.output) as Map<String, dynamic>;
      final retryData = retryPayload['data'] as Map<String, dynamic>;
      expect(retryData['status'], 'completed');
    });

    test('runs connector dry-run plans on phone', () {
      final github = runtime.execute(
        const MobileToolCall(
          id: 'github_search_1',
          name: 'github_search',
          arguments: {
            'query': 'rumiai mobile',
            'kind': 'issues',
            'limit': 3,
          },
        ),
      );

      expect(github.ok, isTrue);
      final githubPayload = jsonDecode(github.output) as Map<String, dynamic>;
      final githubData = githubPayload['data'] as Map<String, dynamic>;
      expect(githubData['dry_run'], isTrue);
      expect(
          githubData['command'],
          orderedEquals(
              ['gh', 'search', 'issues', 'rumiai mobile', '--limit', '3']));
      expect(githubData['execution_location'], 'phone');
      expect(githubData['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(githubData['implementation_status'],
          'implemented_cli_dry_run_pc_execute');

      final slack = runtime.execute(
        const MobileToolCall(
          id: 'slack_send_1',
          name: 'slack_send',
          arguments: {
            'channel': '#dev',
            'text': 'hello',
            'token': 'xoxb-secret',
          },
        ),
      );

      expect(slack.ok, isTrue);
      final slackPayload = jsonDecode(slack.output) as Map<String, dynamic>;
      final slackData = slackPayload['data'] as Map<String, dynamic>;
      final message = slackData['message'] as Map<String, dynamic>;
      expect(slackData['connector_required'], 'slack');
      expect(slackData['dry_run'], isTrue);
      expect(message['token'], '[redacted]');
      expect(
          slackData['implementation_status'], 'implemented_connector_dry_run');
    });

    test('executeAsync delegates connector execute=true to PC', () async {
      final delegate = _FakePcToolDelegate();
      final runtime = MobileToolRuntime(pcDelegate: delegate);

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'github_search_execute_1',
          name: 'github_search',
          arguments: {
            'query': 'rumiai mobile',
            'execute': true,
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(delegate.lastCall?.name, 'github_search');
    });

    test('executeAsync keeps phone-compatible tools on phone', () async {
      final delegate = _FakePcToolDelegate();
      final runtime = MobileToolRuntime(pcDelegate: delegate);

      final result = await runtime.executeAsync(
        const MobileToolCall(
          id: 'invoke_phone_1',
          name: 'tool_invoke',
          arguments: {
            'tool_name': 'tool_calculator',
            'arguments': {'expression': '7 * 6'},
          },
        ),
      );

      expect(result.ok, isTrue);
      expect(result.output, contains('execution_location":"phone'));
      expect(delegate.lastCall, isNull);
    });

    test('schemas explain PC delegation when it is available', () {
      final runtime = MobileToolRuntime(pcDelegate: _FakePcToolDelegate());

      final schema = runtime.execute(
        const MobileToolCall(
          id: 'schema_pc_delegate_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'python_exec'},
        ),
      );

      expect(schema.ok, isTrue);
      final payload = jsonDecode(schema.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['mobile_compatible'], isFalse);
      expect(data['callable'], isTrue);
      expect(data['execution_route'], 'pc');
      final routing = data['automatic_routing'] as Map<String, dynamic>;
      expect(routing['one_tool_surface'], isTrue);
      expect(routing['selected_route'], 'pc');
      expect(data['pc_delegation_available'], isTrue);
      expect(data['unavailable_reason'], contains('tool_invoke'));
      final delegation = data['pc_delegation'] as Map<String, dynamic>;
      expect(delegation['available'], isTrue);
      expect(delegation['route'], '/api/mobile/v1/tools/invoke');
    });

    test('classifies mobile platform layers separately from PC-only tools', () {
      final urlSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_platform_url_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'browser_open_url'},
        ),
      );
      final urlPayload = jsonDecode(urlSchema.output) as Map<String, dynamic>;
      final urlData = urlPayload['data'] as Map<String, dynamic>;
      expect(urlData['mobile_compatible'], isTrue);
      expect(urlData['execution_platforms'], containsAll(['ios', 'android']));
      expect(urlData['mobile_runtime_layers'], contains('flutter'));
      expect(urlData['mobile_runtime_layers'], contains('ios-swift'));

      final clipboardSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_clipboard_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_clipboard_read'},
        ),
      );
      final clipboardPayload =
          jsonDecode(clipboardSchema.output) as Map<String, dynamic>;
      final clipboardData = clipboardPayload['data'] as Map<String, dynamic>;
      final clipboardMobile = clipboardData['mobile'] as Map<String, dynamic>;
      expect(clipboardData['mobile_compatible'], isTrue);
      expect(clipboardData['execution_route'], 'phone');
      expect(clipboardMobile['implementation_status'], 'implemented');
      expect(clipboardMobile['requires_mobile_approval'], isTrue);
      expect(clipboardMobile['platforms'], containsAll(['ios', 'android']));
      expect(clipboardData['callable'], isTrue);

      final pickerSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_file_pick_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_file_pick'},
        ),
      );
      final pickerPayload =
          jsonDecode(pickerSchema.output) as Map<String, dynamic>;
      final pickerData = pickerPayload['data'] as Map<String, dynamic>;
      final pickerMobile = pickerData['mobile'] as Map<String, dynamic>;
      expect(pickerData['mobile_compatible'], isTrue);
      expect(pickerData['execution_route'], 'phone');
      expect(pickerMobile['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(pickerData['tags'], contains(mobileSwiftNativeTag));
      expect(pickerData['tags'], contains(mobileKotlinNativeTag));

      final screenshotSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_screenshot_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_screenshot'},
        ),
      );
      final screenshotPayload =
          jsonDecode(screenshotSchema.output) as Map<String, dynamic>;
      final screenshotData = screenshotPayload['data'] as Map<String, dynamic>;
      final screenshotMobile = screenshotData['mobile'] as Map<String, dynamic>;
      expect(screenshotData['mobile_compatible'], isTrue);
      expect(screenshotData['execution_route'], 'phone');
      expect(screenshotMobile['requires_mobile_approval'], isTrue);
      expect(screenshotMobile['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(screenshotData['tags'], contains(mobileSwiftNativeTag));
      expect(screenshotData['tags'], contains(mobileKotlinNativeTag));

      final imageReadSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_image_read_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_image_read'},
        ),
      );
      final imageReadPayload =
          jsonDecode(imageReadSchema.output) as Map<String, dynamic>;
      final imageReadData = imageReadPayload['data'] as Map<String, dynamic>;
      final imageReadMobile = imageReadData['mobile'] as Map<String, dynamic>;
      expect(imageReadData['mobile_compatible'], isTrue);
      expect(imageReadData['execution_route'], 'phone');
      expect(imageReadMobile['requires_mobile_approval'], isFalse);
      expect(
          imageReadMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(imageReadData['tags'], contains(mobileFlutterTag));

      final imageTransformSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_image_transform_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_image_transform'},
        ),
      );
      final imageTransformPayload =
          jsonDecode(imageTransformSchema.output) as Map<String, dynamic>;
      final imageTransformData =
          imageTransformPayload['data'] as Map<String, dynamic>;
      final imageTransformMobile =
          imageTransformData['mobile'] as Map<String, dynamic>;
      expect(imageTransformData['mobile_compatible'], isTrue);
      expect(imageTransformData['execution_route'], 'phone');
      expect(imageTransformMobile['requires_mobile_approval'], isFalse);
      expect(imageTransformMobile['implementation_status'], 'implemented');
      expect(imageTransformMobile['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(imageTransformData['tags'], contains(mobileSwiftNativeTag));
      expect(imageTransformData['tags'], contains(mobileKotlinNativeTag));

      final imageResizeSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_image_resize_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'image_resize'},
        ),
      );
      final imageResizePayload =
          jsonDecode(imageResizeSchema.output) as Map<String, dynamic>;
      final imageResizeData =
          imageResizePayload['data'] as Map<String, dynamic>;
      final imageResizeMobile =
          imageResizeData['mobile'] as Map<String, dynamic>;
      expect(imageResizeData['mobile_compatible'], isTrue);
      expect(imageResizeData['execution_route'], 'phone');
      expect(imageResizeMobile['requires_mobile_approval'], isFalse);
      expect(imageResizeMobile['implementation_status'],
          'implemented_payload_only');
      expect(imageResizeMobile['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(imageResizeData['tags'], contains(mobileSwiftNativeTag));
      expect(imageResizeData['tags'], contains(mobileKotlinNativeTag));

      final imageConvertSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_image_convert_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'image_convert'},
        ),
      );
      final imageConvertPayload =
          jsonDecode(imageConvertSchema.output) as Map<String, dynamic>;
      final imageConvertData =
          imageConvertPayload['data'] as Map<String, dynamic>;
      final imageConvertMobile =
          imageConvertData['mobile'] as Map<String, dynamic>;
      expect(imageConvertData['mobile_compatible'], isTrue);
      expect(imageConvertData['execution_route'], 'phone');
      expect(imageConvertMobile['requires_mobile_approval'], isFalse);
      expect(imageConvertMobile['implementation_status'],
          'implemented_payload_only');
      expect(imageConvertMobile['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(imageConvertData['tags'], contains(mobileSwiftNativeTag));
      expect(imageConvertData['tags'], contains(mobileKotlinNativeTag));

      final ocrSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_ocr_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_ocr'},
        ),
      );
      final ocrPayload = jsonDecode(ocrSchema.output) as Map<String, dynamic>;
      final ocrData = ocrPayload['data'] as Map<String, dynamic>;
      final ocrMobile = ocrData['mobile'] as Map<String, dynamic>;
      expect(ocrData['mobile_compatible'], isTrue);
      expect(ocrData['execution_route'], 'phone');
      expect(ocrMobile['requires_mobile_approval'], isFalse);
      expect(
          ocrMobile['implementation_status'], 'implemented_native_ocr_bridge');
      expect(ocrMobile['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(ocrData['tags'], contains(mobileSwiftNativeTag));
      expect(ocrData['tags'], contains(mobileKotlinNativeTag));

      final ocrExtractSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_ocr_extract_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'ocr_extract'},
        ),
      );
      final ocrExtractPayload =
          jsonDecode(ocrExtractSchema.output) as Map<String, dynamic>;
      final ocrExtractData = ocrExtractPayload['data'] as Map<String, dynamic>;
      final ocrExtractMobile = ocrExtractData['mobile'] as Map<String, dynamic>;
      expect(ocrExtractData['mobile_compatible'], isTrue);
      expect(ocrExtractData['execution_route'], 'phone');
      expect(ocrExtractMobile['requires_mobile_approval'], isFalse);
      expect(ocrExtractMobile['implementation_status'],
          'implemented_payload_only_native_ocr');
      expect(ocrExtractMobile['runtime_layers'],
          containsAll(['flutter', 'ios-swift', 'android-kotlin']));
      expect(ocrExtractData['tags'], contains(mobileSwiftNativeTag));
      expect(ocrExtractData['tags'], contains(mobileKotlinNativeTag));

      final docParseSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_doc_parse_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_doc_parse'},
        ),
      );
      final docParsePayload =
          jsonDecode(docParseSchema.output) as Map<String, dynamic>;
      final docParseData = docParsePayload['data'] as Map<String, dynamic>;
      final docParseMobile = docParseData['mobile'] as Map<String, dynamic>;
      expect(docParseData['mobile_compatible'], isTrue);
      expect(docParseData['execution_route'], 'phone');
      expect(docParseMobile['requires_mobile_approval'], isFalse);
      expect(docParseMobile['implementation_status'],
          'implemented_text_documents');
      expect(
          docParseMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(docParseData['tags'], contains(mobileFlutterTag));

      final pdfParseSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_pdf_parse_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'media_pdf_parse'},
        ),
      );
      final pdfParsePayload =
          jsonDecode(pdfParseSchema.output) as Map<String, dynamic>;
      final pdfParseData = pdfParsePayload['data'] as Map<String, dynamic>;
      final pdfParseMobile = pdfParseData['mobile'] as Map<String, dynamic>;
      expect(pdfParseData['mobile_compatible'], isTrue);
      expect(pdfParseData['execution_route'], 'phone');
      expect(pdfParseMobile['requires_mobile_approval'], isFalse);
      expect(pdfParseMobile['implementation_status'],
          'implemented_best_effort_bytes');
      expect(
          pdfParseMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(pdfParseData['tags'], contains(mobileFlutterTag));

      final pdfExtractSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_pdf_extract_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'pdf_extract'},
        ),
      );
      final pdfExtractPayload =
          jsonDecode(pdfExtractSchema.output) as Map<String, dynamic>;
      final pdfExtractData = pdfExtractPayload['data'] as Map<String, dynamic>;
      final pdfExtractMobile = pdfExtractData['mobile'] as Map<String, dynamic>;
      expect(pdfExtractData['mobile_compatible'], isTrue);
      expect(pdfExtractData['execution_route'], 'phone');
      expect(pdfExtractMobile['requires_mobile_approval'], isFalse);
      expect(pdfExtractMobile['implementation_status'],
          'implemented_best_effort_bytes');
      expect(
          pdfExtractMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(pdfExtractData['tags'], contains(mobileFlutterTag));

      final pdfTablesSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_pdf_tables_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'pdf_extract_tables'},
        ),
      );
      final pdfTablesPayload =
          jsonDecode(pdfTablesSchema.output) as Map<String, dynamic>;
      final pdfTablesData = pdfTablesPayload['data'] as Map<String, dynamic>;
      final pdfTablesMobile = pdfTablesData['mobile'] as Map<String, dynamic>;
      expect(pdfTablesData['mobile_compatible'], isTrue);
      expect(pdfTablesData['execution_route'], 'phone');
      expect(pdfTablesMobile['requires_mobile_approval'], isFalse);
      expect(pdfTablesMobile['implementation_status'],
          'implemented_empty_table_fallback');
      expect(
          pdfTablesMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(pdfTablesData['tags'], contains(mobileFlutterTag));

      final artifactPreviewSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_artifact_preview_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'artifact_preview'},
        ),
      );
      final artifactPreviewPayload =
          jsonDecode(artifactPreviewSchema.output) as Map<String, dynamic>;
      final artifactPreviewData =
          artifactPreviewPayload['data'] as Map<String, dynamic>;
      final artifactPreviewMobile =
          artifactPreviewData['mobile'] as Map<String, dynamic>;
      expect(artifactPreviewData['mobile_compatible'], isTrue);
      expect(artifactPreviewData['execution_route'], 'phone');
      expect(artifactPreviewMobile['requires_mobile_approval'], isFalse);
      expect(artifactPreviewMobile['implementation_status'],
          'implemented_payload_only_preview');
      expect(artifactPreviewMobile['runtime_layers'],
          containsAll(['flutter', 'dart']));
      expect(artifactPreviewData['tags'], contains(mobileFlutterTag));

      for (final entry in const {
        'artifact_file_list': false,
        'artifact_file_read': false,
        'artifact_file_write': true,
        'artifact_file_patch': true,
        'artifact_file_delete': true,
      }.entries) {
        final artifactSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final artifactPayload =
            jsonDecode(artifactSchema.output) as Map<String, dynamic>;
        final artifactData = artifactPayload['data'] as Map<String, dynamic>;
        final artifactMobile = artifactData['mobile'] as Map<String, dynamic>;
        expect(artifactData['mobile_compatible'], isTrue);
        expect(artifactData['execution_route'], 'phone');
        expect(artifactMobile['requires_mobile_approval'], entry.value);
        expect(artifactMobile['implementation_status'],
            'implemented_phone_artifact_workspace');
        expect(
            artifactMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(artifactData['tags'], contains(mobileFlutterTag));
      }

      final fileReaderSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_tool_file_reader_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'tool_file_reader'},
        ),
      );
      final fileReaderPayload =
          jsonDecode(fileReaderSchema.output) as Map<String, dynamic>;
      final fileReaderData = fileReaderPayload['data'] as Map<String, dynamic>;
      final fileReaderMobile = fileReaderData['mobile'] as Map<String, dynamic>;
      expect(fileReaderData['mobile_compatible'], isTrue);
      expect(fileReaderData['execution_route'], 'phone');
      expect(fileReaderData['function_id'], 'tool_file_reader');
      expect(fileReaderData['tool_id'], 'file_reader');
      expect(fileReaderMobile['requires_mobile_approval'], isFalse);
      expect(fileReaderMobile['implementation_status'],
          'implemented_phone_artifact_file_reader');
      expect(fileReaderMobile['runtime_layers'],
          contains('mobile-media-artifact'));

      for (final toolName in const [
        'browser_save_page',
        'webapp_preview',
        'webapp_lint',
      ]) {
        final htmlSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${toolName}_1',
            name: 'tool_schema',
            arguments: {'tool_name': toolName},
          ),
        );
        final htmlPayload =
            jsonDecode(htmlSchema.output) as Map<String, dynamic>;
        final htmlData = htmlPayload['data'] as Map<String, dynamic>;
        final htmlMobile = htmlData['mobile'] as Map<String, dynamic>;
        expect(htmlData['mobile_compatible'], isTrue);
        expect(htmlData['execution_route'], 'phone');
        expect(htmlMobile['requires_mobile_approval'], isFalse);
        expect(htmlMobile['implementation_status'],
            'implemented_phone_artifact_html');
        expect(htmlMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(htmlData['tags'], contains(mobileFlutterTag));
      }

      final batchSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_tool_batch_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'tool_batch'},
        ),
      );
      final batchPayload =
          jsonDecode(batchSchema.output) as Map<String, dynamic>;
      final batchData = batchPayload['data'] as Map<String, dynamic>;
      final batchMobile = batchData['mobile'] as Map<String, dynamic>;
      expect(batchData['mobile_compatible'], isTrue);
      expect(batchData['execution_route'], 'phone');
      expect(batchMobile['requires_mobile_approval'], isFalse);
      expect(batchMobile['implementation_status'],
          'implemented_mobile_batch_router');
      expect(batchMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(batchData['tags'], contains(mobileFlutterTag));

      for (final entry in const {
        'package_install_plan': ['implemented_phone_install_plan', false],
        'webapp_build': ['implemented_phone_static_webapp_build_plan', true],
        'research_report_export': [
          'implemented_phone_research_report_export',
          false
        ],
      }.entries) {
        final schema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final payload = jsonDecode(schema.output) as Map<String, dynamic>;
        final data = payload['data'] as Map<String, dynamic>;
        final mobile = data['mobile'] as Map<String, dynamic>;
        expect(data['mobile_compatible'], isTrue);
        expect(data['execution_route'], 'phone');
        expect(mobile['implementation_status'], entry.value[0]);
        expect(mobile['requires_mobile_approval'], entry.value[1]);
        expect(mobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(data['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'ai_models': ['implemented_phone_ai_catalog', false],
        'ai_profiles': ['implemented_phone_ai_catalog', false],
        'ai_providers': ['implemented_phone_ai_catalog', false],
        'ai_get_provider_key_status': [
          'implemented_phone_ai_provider_key_status',
          false
        ],
        'ai_set_provider_key': ['implemented_phone_ai_provider_key', true],
        'ai_delete_provider_key': ['implemented_phone_ai_provider_key', true],
        'ai_get_preferred_model': [
          'implemented_phone_ai_model_settings',
          false
        ],
        'ai_set_preferred_model': ['implemented_phone_ai_model_settings', true],
        'ai_get_thinking_level': ['implemented_phone_ai_model_settings', false],
        'ai_set_thinking_level': ['implemented_phone_ai_model_settings', true],
        'ai_get_effective_thinking_level': [
          'implemented_phone_ai_model_settings',
          false
        ],
        'ai_normalize_thinking_level': [
          'implemented_phone_ai_model_settings',
          false
        ],
        'ai_validate_model_params': [
          'implemented_phone_ai_param_validation',
          false
        ],
        'ai_recommend_model': ['implemented_phone_ai_routing_hint', false],
        'ai_route_model': ['implemented_phone_ai_routing_hint', false],
        'ai_explain_model_choice': ['implemented_phone_ai_routing_hint', false],
      }.entries) {
        final schema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final payload = jsonDecode(schema.output) as Map<String, dynamic>;
        final data = payload['data'] as Map<String, dynamic>;
        final mobile = data['mobile'] as Map<String, dynamic>;
        expect(data['mobile_compatible'], isTrue);
        expect(data['execution_route'], 'phone');
        expect(mobile['implementation_status'], entry.value[0]);
        expect(mobile['requires_mobile_approval'], entry.value[1]);
        expect(mobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(mobile['runtime_layers'], contains('mobile-provider-config'));
        expect(data['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'prompt_validate_template': ['implemented_phone_prompt_text', false],
        'prompt_render': ['implemented_phone_prompt_text', false],
        'prompt_lint_prompt': ['implemented_phone_prompt_text', false],
        'prompt_compact_prompt': ['implemented_phone_prompt_text', false],
        'prompt_test': ['implemented_phone_prompt_text', false],
        'prompt_system_get': ['implemented_phone_prompt_system', false],
        'prompt_system_set': ['implemented_phone_prompt_system', true],
        'prompt_list': ['implemented_phone_prompt_store', false],
        'prompt_create': ['implemented_phone_prompt_store', true],
        'prompt_update': ['implemented_phone_prompt_store', true],
        'prompt_delete': ['implemented_phone_prompt_store', true],
        'prompt_active': ['implemented_phone_prompt_effective', false],
        'prompt_load_effective': ['implemented_phone_prompt_effective', false],
        'prompt_resolve_for_conversation': [
          'implemented_phone_prompt_effective',
          false
        ],
        'prompt_preview_toggle': ['implemented_phone_prompt_preview', false],
      }.entries) {
        final schema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final payload = jsonDecode(schema.output) as Map<String, dynamic>;
        final data = payload['data'] as Map<String, dynamic>;
        final mobile = data['mobile'] as Map<String, dynamic>;
        expect(data['mobile_compatible'], isTrue);
        expect(data['execution_route'], 'phone');
        expect(mobile['implementation_status'], entry.value[0]);
        expect(mobile['requires_mobile_approval'], entry.value[1]);
        expect(mobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(mobile['runtime_layers'], contains('mobile-prompt-store'));
        expect(data['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'memory_store': ['implemented_phone_memory_store', true],
        'memory_list': ['implemented_phone_memory_store', false],
        'memory_recall': ['implemented_phone_memory_search', false],
        'memory_update': ['implemented_phone_memory_store', true],
        'memory_delete': ['implemented_phone_memory_store', true],
        'memory_compact': ['implemented_phone_memory_summary', false],
        'memory_project_context': ['implemented_phone_memory_context', false],
        'memory_resolve_for_agent': ['implemented_phone_memory_context', false],
        'memory_memo': ['implemented_phone_memo_store', true],
        'memory_memo_folders': ['implemented_phone_memo_store', true],
        'memory_memo_notes': ['implemented_phone_memo_store', true],
      }.entries) {
        final schema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final payload = jsonDecode(schema.output) as Map<String, dynamic>;
        final data = payload['data'] as Map<String, dynamic>;
        final mobile = data['mobile'] as Map<String, dynamic>;
        expect(data['mobile_compatible'], isTrue);
        expect(data['execution_route'], 'phone');
        expect(mobile['implementation_status'], entry.value[0]);
        expect(mobile['requires_mobile_approval'], entry.value[1]);
        expect(mobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(mobile['runtime_layers'], contains('mobile-memory-store'));
        expect(data['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'knowledge_create': ['implemented_phone_knowledge_store', true],
        'knowledge_get': ['implemented_phone_knowledge_store', false],
        'knowledge_list': ['implemented_phone_knowledge_store', false],
        'knowledge_update': ['implemented_phone_knowledge_store', true],
        'knowledge_delete': ['implemented_phone_knowledge_store', true],
        'knowledge_search': ['implemented_phone_knowledge_search', false],
        'knowledge_import_file': ['implemented_phone_knowledge_import', true],
        'knowledge_import_url': ['implemented_phone_knowledge_import', true],
        'knowledge_attach_to_project': [
          'implemented_phone_knowledge_store',
          true
        ],
        'knowledge_index': ['implemented_phone_knowledge_index', true],
        'knowledge_reindex': ['implemented_phone_knowledge_index', true],
      }.entries) {
        final schema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final payload = jsonDecode(schema.output) as Map<String, dynamic>;
        final data = payload['data'] as Map<String, dynamic>;
        final mobile = data['mobile'] as Map<String, dynamic>;
        expect(data['mobile_compatible'], isTrue);
        expect(data['execution_route'], 'phone');
        expect(mobile['implementation_status'], entry.value[0]);
        expect(mobile['requires_mobile_approval'], entry.value[1]);
        expect(mobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(mobile['runtime_layers'], contains('mobile-knowledge-store'));
        expect(data['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'workflow_define': false,
        'workflow_run': true,
        'workflow_status': false,
        'workflow_cancel': true,
        'workflow_retry': true,
      }.entries) {
        final workflowSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final workflowPayload =
            jsonDecode(workflowSchema.output) as Map<String, dynamic>;
        final workflowData = workflowPayload['data'] as Map<String, dynamic>;
        final workflowMobile = workflowData['mobile'] as Map<String, dynamic>;
        expect(workflowData['mobile_compatible'], isTrue);
        expect(workflowData['execution_route'], 'phone');
        expect(workflowMobile['implementation_status'],
            'implemented_phone_workflow_record');
        expect(workflowMobile['requires_mobile_approval'], entry.value);
        expect(workflowMobile['runtime_layers'],
            containsAll(['flutter', 'dart', 'mobile-workflow-record']));
        expect(workflowData['tags'], contains(mobileFlutterTag));
        expect(workflowData['tags'], isNot(contains(mobileSwiftNativeTag)));
        expect(workflowData['tags'], isNot(contains(mobileKotlinNativeTag)));
      }

      for (final entry in const {
        'job_create': false,
        'job_status': false,
        'job_history': false,
        'job_artifacts': false,
        'job_cancel': true,
        'job_resume': true,
      }.entries) {
        final jobSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final jobPayload = jsonDecode(jobSchema.output) as Map<String, dynamic>;
        final jobData = jobPayload['data'] as Map<String, dynamic>;
        final jobMobile = jobData['mobile'] as Map<String, dynamic>;
        expect(jobData['mobile_compatible'], isTrue);
        expect(jobData['execution_route'], 'phone');
        expect(
            jobMobile['implementation_status'], 'implemented_phone_job_record');
        expect(jobMobile['requires_mobile_approval'], entry.value);
        expect(jobMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(jobData['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'project_scaffold': 'implemented_phone_artifact_scaffold',
        'doc_create': 'implemented_phone_document_text',
        'slides_create': 'implemented_phone_slide_outline',
        'slides_from_markdown': 'implemented_phone_slide_outline',
        'chart_create': 'implemented_phone_svg_chart',
      }.entries) {
        final generatorSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final generatorPayload =
            jsonDecode(generatorSchema.output) as Map<String, dynamic>;
        final generatorData = generatorPayload['data'] as Map<String, dynamic>;
        final generatorMobile = generatorData['mobile'] as Map<String, dynamic>;
        expect(generatorData['mobile_compatible'], isTrue);
        expect(generatorData['execution_route'], 'phone');
        expect(generatorMobile['requires_mobile_approval'], isFalse);
        expect(generatorMobile['implementation_status'], entry.value);
        expect(generatorMobile['runtime_layers'],
            containsAll(['flutter', 'dart']));
        expect(generatorData['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'doc_update': ['implemented_phone_document_text', true],
        'slides_update': ['implemented_phone_slide_outline', true],
        'slides_export': ['implemented_phone_slide_export', false],
      }.entries) {
        final editSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final editPayload =
            jsonDecode(editSchema.output) as Map<String, dynamic>;
        final editData = editPayload['data'] as Map<String, dynamic>;
        final editMobile = editData['mobile'] as Map<String, dynamic>;
        expect(editData['mobile_compatible'], isTrue);
        expect(editData['execution_route'], 'phone');
        expect(editMobile['implementation_status'], entry.value[0]);
        expect(editMobile['requires_mobile_approval'], entry.value[1]);
        expect(editMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(editData['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'sheet_create': ['implemented_phone_sheet_text', false],
        'sheet_read': ['implemented_phone_sheet_text', false],
        'sheet_analyze': ['implemented_phone_sheet_text', false],
        'sheet_update': ['implemented_phone_sheet_text', true],
        'sheet_export': ['implemented_phone_sheet_export', false],
      }.entries) {
        final sheetSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final sheetPayload =
            jsonDecode(sheetSchema.output) as Map<String, dynamic>;
        final sheetData = sheetPayload['data'] as Map<String, dynamic>;
        final sheetMobile = sheetData['mobile'] as Map<String, dynamic>;
        expect(sheetData['mobile_compatible'], isTrue);
        expect(sheetData['execution_route'], 'phone');
        expect(sheetMobile['implementation_status'], entry.value[0]);
        expect(sheetMobile['requires_mobile_approval'], entry.value[1]);
        expect(sheetMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(sheetData['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'artifact_zip': 'implemented_phone_zip_base64',
        'artifact_export': 'implemented_phone_artifact_export',
        'static_site_export': 'implemented_phone_zip_base64',
        'webapp_export_static': 'implemented_phone_zip_base64',
        'doc_export': 'implemented_phone_document_export',
        'pdf_export': 'pc_delegation_required_binary_export',
        'doc_to_pdf': 'pc_delegation_required_binary_export',
      }.entries) {
        final exportSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final exportPayload =
            jsonDecode(exportSchema.output) as Map<String, dynamic>;
        final exportData = exportPayload['data'] as Map<String, dynamic>;
        final exportMobile = exportData['mobile'] as Map<String, dynamic>;
        expect(exportData['mobile_compatible'], isTrue);
        expect(exportData['execution_route'], 'phone');
        expect(exportMobile['implementation_status'], entry.value);
        expect(exportMobile['requires_mobile_approval'], isFalse);
        expect(
            exportMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(exportData['tags'], contains(mobileFlutterTag));
      }

      for (final toolName in const ['html_preview', 'pdf_preview']) {
        final previewSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${toolName}_1',
            name: 'tool_schema',
            arguments: {'tool_name': toolName},
          ),
        );
        final previewPayload =
            jsonDecode(previewSchema.output) as Map<String, dynamic>;
        final previewData = previewPayload['data'] as Map<String, dynamic>;
        final previewMobile = previewData['mobile'] as Map<String, dynamic>;
        expect(previewData['mobile_compatible'], isTrue);
        expect(previewData['execution_route'], 'phone');
        expect(previewMobile['requires_mobile_approval'], isFalse);
        expect(previewMobile['implementation_status'],
            'implemented_payload_only_preview');
        expect(
            previewMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(previewData['tags'], contains(mobileFlutterTag));
      }

      final sourceExtractSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_source_extract_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'source_extract'},
        ),
      );
      final sourceExtractPayload =
          jsonDecode(sourceExtractSchema.output) as Map<String, dynamic>;
      final sourceExtractData =
          sourceExtractPayload['data'] as Map<String, dynamic>;
      final sourceExtractMobile =
          sourceExtractData['mobile'] as Map<String, dynamic>;
      expect(sourceExtractData['mobile_compatible'], isTrue);
      expect(sourceExtractData['execution_route'], 'phone');
      expect(sourceExtractMobile['requires_mobile_approval'], isFalse);
      expect(sourceExtractMobile['implementation_status'],
          'implemented_payload_only');
      expect(sourceExtractMobile['runtime_layers'],
          containsAll(['flutter', 'dart']));
      expect(sourceExtractData['tags'], contains(mobileFlutterTag));

      final sourceRankSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_source_rank_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'source_rank'},
        ),
      );
      final sourceRankPayload =
          jsonDecode(sourceRankSchema.output) as Map<String, dynamic>;
      final sourceRankData = sourceRankPayload['data'] as Map<String, dynamic>;
      final sourceRankMobile = sourceRankData['mobile'] as Map<String, dynamic>;
      expect(sourceRankData['mobile_compatible'], isTrue);
      expect(sourceRankData['execution_route'], 'phone');
      expect(sourceRankMobile['requires_mobile_approval'], isFalse);
      expect(sourceRankMobile['implementation_status'], 'implemented');
      expect(
          sourceRankMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(sourceRankData['tags'], contains(mobileFlutterTag));

      for (final entry in const {
        'image_render': 'implemented_phone_svg_image_render',
        'image_generate_local_or_provider':
            'implemented_phone_svg_image_placeholder',
        'audio_transcribe': 'implemented_phone_audio_transcribe_payload',
        'audio_transcribe_local': 'implemented_phone_audio_transcribe_payload',
      }.entries) {
        final mediaSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final mediaPayload =
            jsonDecode(mediaSchema.output) as Map<String, dynamic>;
        final mediaData = mediaPayload['data'] as Map<String, dynamic>;
        final mediaMobile = mediaData['mobile'] as Map<String, dynamic>;
        expect(mediaData['mobile_compatible'], isTrue);
        expect(mediaData['execution_route'], 'phone');
        expect(mediaMobile['requires_mobile_approval'], isFalse);
        expect(mediaMobile['implementation_status'], entry.value);
        expect(mediaMobile['runtime_layers'], containsAll(['flutter', 'dart']));
        expect(
            mediaMobile['runtime_layers'], contains('mobile-media-artifact'));
        expect(mediaData['tags'], contains(mobileFlutterTag));
      }

      for (final entry in const {
        'github_search': 'implemented_cli_dry_run_pc_execute',
        'slack_send': 'implemented_connector_dry_run',
      }.entries) {
        final connectorSchema = runtime.execute(
          MobileToolCall(
            id: 'schema_${entry.key}_1',
            name: 'tool_schema',
            arguments: {'tool_name': entry.key},
          ),
        );
        final connectorPayload =
            jsonDecode(connectorSchema.output) as Map<String, dynamic>;
        final connectorData = connectorPayload['data'] as Map<String, dynamic>;
        final connectorMobile = connectorData['mobile'] as Map<String, dynamic>;
        expect(connectorData['mobile_compatible'], isTrue);
        expect(connectorData['execution_route'], 'phone');
        expect(connectorMobile['requires_mobile_approval'], isFalse);
        expect(connectorMobile['implementation_status'], entry.value);
        expect(connectorMobile['runtime_layers'],
            containsAll(['flutter', 'dart']));
        expect(connectorData['tags'], contains(mobileFlutterTag));
      }

      final tableSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_browser_extract_table_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'browser_extract_table'},
        ),
      );
      final tablePayload =
          jsonDecode(tableSchema.output) as Map<String, dynamic>;
      final tableData = tablePayload['data'] as Map<String, dynamic>;
      final tableMobile = tableData['mobile'] as Map<String, dynamic>;
      expect(tableData['mobile_compatible'], isTrue);
      expect(tableData['execution_route'], 'phone');
      expect(tableMobile['requires_mobile_approval'], isFalse);
      expect(tableMobile['implementation_status'],
          'implemented_payload_only_html');
      expect(tableMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(tableData['tags'], contains(mobileFlutterTag));

      final ttsSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_tts_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'tts_generate'},
        ),
      );
      final ttsPayload = jsonDecode(ttsSchema.output) as Map<String, dynamic>;
      final ttsData = ttsPayload['data'] as Map<String, dynamic>;
      final ttsMobile = ttsData['mobile'] as Map<String, dynamic>;
      expect(ttsData['mobile_compatible'], isTrue);
      expect(ttsData['execution_route'], 'phone');
      expect(ttsMobile['requires_mobile_approval'], isFalse);
      expect(ttsMobile['implementation_status'],
          'implemented_silent_wav_fallback');
      expect(ttsMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(ttsData['tags'], contains(mobileFlutterTag));

      final ttsLocalSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_tts_local_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'tts_generate_local'},
        ),
      );
      final ttsLocalPayload =
          jsonDecode(ttsLocalSchema.output) as Map<String, dynamic>;
      final ttsLocalData = ttsLocalPayload['data'] as Map<String, dynamic>;
      final ttsLocalMobile = ttsLocalData['mobile'] as Map<String, dynamic>;
      expect(ttsLocalData['mobile_compatible'], isTrue);
      expect(ttsLocalData['execution_route'], 'phone');
      expect(ttsLocalMobile['requires_mobile_approval'], isFalse);
      expect(ttsLocalMobile['implementation_status'],
          'implemented_silent_wav_fallback');
      expect(
          ttsLocalMobile['runtime_layers'], containsAll(['flutter', 'dart']));
      expect(ttsLocalData['tags'], contains(mobileFlutterTag));

      final computerSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_computer_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'computer_click'},
        ),
      );
      final computerPayload =
          jsonDecode(computerSchema.output) as Map<String, dynamic>;
      final computerData = computerPayload['data'] as Map<String, dynamic>;
      final computerMobile = computerData['mobile'] as Map<String, dynamic>;
      expect(computerData['mobile_compatible'], isFalse);
      expect(computerMobile['implementation_status'], 'pc_only');
      expect(computerMobile['platforms'], isEmpty);
    });

    test('filters catalog by mobile platform layer', () {
      Set<String> namesFor(String platform) {
        final result = runtime.execute(
          MobileToolCall(
            id: 'names_$platform',
            name: 'tool_names',
            arguments: {'platform': platform, 'include_aliases': false},
          ),
        );
        final payload = jsonDecode(result.output) as Map<String, dynamic>;
        final data = payload['data'] as Map<String, dynamic>;
        expect(data['platform_filter'], platform);
        return (data['names'] as List).map((entry) => '$entry').toSet();
      }

      final flutterNames = namesFor('flutter');
      expect(flutterNames, contains('artifact_export'));
      expect(flutterNames, contains('doc_export'));

      final iosNames = namesFor('ios');
      expect(iosNames, contains('artifact_export'));
      expect(iosNames, contains('browser_open_url'));

      final androidNames = namesFor('android');
      expect(androidNames, contains('artifact_export'));
      expect(androidNames, contains('browser_open_url'));

      final swiftNames = namesFor('swift');
      expect(swiftNames, contains('browser_open_url'));
      expect(swiftNames, isNot(contains('artifact_export')));
      expect(swiftNames, isNot(contains('doc_export')));

      final kotlinNames = namesFor('kotlin');
      expect(kotlinNames, contains('browser_open_url'));
      expect(kotlinNames, isNot(contains('artifact_export')));
      expect(kotlinNames, isNot(contains('doc_export')));

      final swiftTools = runtime.execute(
        const MobileToolCall(
          id: 'list_swift_1',
          name: 'tool_list',
          arguments: {'platform': 'swift'},
        ),
      );
      final swiftPayload =
          jsonDecode(swiftTools.output) as Map<String, dynamic>;
      final swiftData = swiftPayload['data'] as Map<String, dynamic>;
      final surface = swiftData['tool_surface'] as Map<String, dynamic>;
      expect(surface['mode'], 'unified');
      expect(surface['one_tool_surface'], isTrue);
      expect(swiftData['platform_filter'], 'swift');
      final summary = swiftData['platform_summary'] as Map<String, dynamic>;
      expect(summary['swift'], greaterThan(0));
      expect(summary['ios'], greaterThanOrEqualTo(summary['swift'] as int));
      final swiftIds = (swiftData['tools'] as List)
          .map((entry) => '${entry['function_id'] ?? entry['tool_id']}')
          .toSet();
      expect(swiftIds, contains('browser_open_url'));
      expect(swiftIds, isNot(contains('artifact_export')));

      final pcTools = runtime.execute(
        const MobileToolCall(
          id: 'list_pc_1',
          name: 'tool_list',
          arguments: {'platform': 'pc', 'limit': 20},
        ),
      );
      final pcPayload = jsonDecode(pcTools.output) as Map<String, dynamic>;
      final pcData = pcPayload['data'] as Map<String, dynamic>;
      expect(pcData['platform_filter'], 'pc');
      expect(pcData['tools'], isNotEmpty);
    });

    test('runs defaultspack task_board-compatible actions on phone', () {
      final created = runtime.execute(
        const MobileToolCall(
          id: 'board_1',
          name: 'tool_task_board',
          arguments: {
            'action': 'create',
            'title': 'Implement mobile tools',
            'column': 'Doing',
          },
        ),
      );

      expect(created.ok, isTrue);
      final payload = jsonDecode(created.output) as Map<String, dynamic>;
      final changed = payload['changed'] as Map<String, dynamic>;

      final moved = runtime.execute(
        MobileToolCall(
          id: 'board_2',
          name: 'task_board',
          arguments: {
            'action': 'move',
            'card_id': changed['id'],
            'column': 'Done',
          },
        ),
      );

      expect(moved.ok, isTrue);
      expect(moved.output, contains('"column":"Done"'));
    });

    test('explains host-bound defaultspack tools through tool_search', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'search_1',
          name: 'tool_search',
          arguments: {'query': 'tool_web_search'},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final tools = payload['tools'] as List;
      expect(tools.single['tool_id'], 'web_search');
      expect(tools.single['aliases'], contains('tool_web_search'));
      expect(tools.single['mobile_compatible'], isFalse);
      expect(tools.single['unavailable_reason'], contains('PC側defaultspack'));
    });

    test('tool_search maps defaultspack function aliases to phone tools', () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'search_2',
          name: 'tool_search',
          arguments: {'query': 'tool_todo'},
        ),
      );

      expect(result.ok, isTrue);
      final payload = jsonDecode(result.output) as Map<String, dynamic>;
      final tools = payload['tools'] as List;
      expect(tools.single['tool_id'], 'todo');
      expect(tools.single['aliases'], contains('tool_todo'));
      expect(tools.single['mobile_compatible'], isTrue);
    });

    test('defaultspack catalog tools list names and schemas on phone', () {
      expect(runtime.knownDefaultspackToolAgentCount, greaterThan(0));
      expect(runtime.knownUnavailableDefaultspackToolCount, greaterThan(0));

      final manifestIds = _defaultspackToolAgentIds();
      final names = runtime.execute(
        const MobileToolCall(
          id: 'names_1',
          name: 'tool_names',
          arguments: {},
        ),
      );
      expect(names.ok, isTrue);
      final namesPayload = jsonDecode(names.output) as Map<String, dynamic>;
      final nameData = namesPayload['data'] as Map<String, dynamic>;
      expect(nameData['names'], containsAll(['tool_todo', 'agent_plan']));
      expect(nameData['names'], containsAll(manifestIds));
      final openAiNames = runtime
          .openAiTools()
          .map((tool) => tool['function']['name'] as String)
          .toSet();
      expect(openAiNames, containsAll(_nativeDefaultspackToolAgentIds()));
      final agentExecuteTool = runtime.openAiTools().singleWhere(
            (tool) => tool['function']['name'] == 'agent_execute',
          );
      expect(agentExecuteTool['function']['description'],
          contains('not phone-executable'));

      final list = runtime.execute(
        const MobileToolCall(
          id: 'list_1',
          name: 'tool_list',
          arguments: {'limit': 400},
        ),
      );
      expect(list.ok, isTrue);
      final listPayload = jsonDecode(list.output) as Map<String, dynamic>;
      final listData = listPayload['data'] as Map<String, dynamic>;
      expect(listData['truncated'], isFalse);
      final functionIds = (listData['tools'] as List)
          .map((entry) => '${entry['function_id'] ?? entry['tool_id']}')
          .toSet();
      expect(functionIds, containsAll(manifestIds));

      final schema = runtime.execute(
        const MobileToolCall(
          id: 'schema_1',
          name: 'defaultspack.tool.schema',
          arguments: {'tool_name': 'agent_execute'},
        ),
      );
      expect(schema.ok, isTrue);
      final schemaPayload = jsonDecode(schema.output) as Map<String, dynamic>;
      final data = schemaPayload['data'] as Map<String, dynamic>;
      expect(data['tool_id'], 'agent_execute');
      expect(data['mobile_compatible'], isFalse);
      expect(data['unavailable_reason'], contains('PC側'));
    });

    test('defaultspack generated manifest aliases and schemas are exposed', () {
      final aliasSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_alias_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'defaultspack.agent.execute'},
        ),
      );
      expect(aliasSchema.ok, isTrue);
      final aliasPayload =
          jsonDecode(aliasSchema.output) as Map<String, dynamic>;
      final aliasData = aliasPayload['data'] as Map<String, dynamic>;
      expect(aliasData['function_id'], 'agent_execute');
      expect(aliasData['requested_name'], 'defaultspack.agent.execute');
      expect(aliasData['aliases'], contains('defaults.agent.execute'));

      final webSearchSchema = runtime.execute(
        const MobileToolCall(
          id: 'schema_web_search_1',
          name: 'tool_schema',
          arguments: {'tool_name': 'tool_web_search'},
        ),
      );
      expect(webSearchSchema.ok, isTrue);
      final webPayload =
          jsonDecode(webSearchSchema.output) as Map<String, dynamic>;
      final webData = webPayload['data'] as Map<String, dynamic>;
      final parameters = webData['parameters'] as Map<String, dynamic>;
      expect(parameters['required'], contains('query'));
      expect(parameters['properties'], contains('query'));
      expect(webData['aliases'], contains('defaultspack.tool.web_search'));
    });

    test('generated defaultspack tool catalog matches source manifests', () {
      final source = _defaultspackToolAgentRecords();
      final generated = {
        for (final entry in defaultspackToolAgentManifestCatalog)
          entry.id: entry,
      };

      expect(generated.keys.toList()..sort(), source.keys.toList()..sort());
      for (final id in source.keys) {
        final expected = source[id]!;
        final actual = generated[id]!;
        expect(actual.description, expected.description, reason: id);
        expect(actual.tags, expected.tags, reason: id);
        expect(actual.aliases, expected.aliases, reason: id);
        expect(
          _canonicalJson(actual.inputSchema),
          _canonicalJson(expected.inputSchema),
          reason: id,
        );
      }
    });

    test('runs phone-local defaultspack agent plan and status tools', () {
      final plan = runtime.execute(
        const MobileToolCall(
          id: 'agent_plan_1',
          name: 'agent_plan',
          arguments: {
            'objective': 'スマホでtool実行を確認する',
            'steps': ['toolを選ぶ', '実行する', '結果を見る'],
          },
        ),
      );
      expect(plan.ok, isTrue);
      expect(plan.summary, contains('3 step plan'));

      final status = runtime.execute(
        const MobileToolCall(
          id: 'agent_status_1',
          name: 'defaultspack.agent.status',
          arguments: {},
        ),
      );
      expect(status.ok, isTrue);
      final payload = jsonDecode(status.output) as Map<String, dynamic>;
      final data = payload['data'] as Map<String, dynamic>;
      expect(data['status'], 'active');
      expect(data['plans'], isNotEmpty);
      expect(data['agent_template']['template_id'], mobileAgentTemplateId);
    });

    test('explains defaultspack function ids even when not in local catalog',
        () {
      final result = runtime.execute(
        const MobileToolCall(
          id: 'missing_1',
          name: 'coding_git_status',
          arguments: {},
        ),
      );

      expect(result.ok, isFalse);
      expect(result.output, contains('defaultspack tool'));
      expect(result.output, contains('PC側'));
    });

    test('classifies every defaultspack tool or agent id', () {
      final ids = _defaultspackToolAgentIds();
      expect(ids, isNotEmpty);

      for (final id in ids) {
        final result = runtime.execute(
          MobileToolCall(id: 'classify_$id', name: id, arguments: const {}),
        );
        expect(
          result.output,
          isNot(contains('mobile-compatible runtimeに未登録')),
          reason: '$id must be executable on phone or classified with a reason',
        );
      }
    });

    test('returns schema or reason for every defaultspack tool or agent id',
        () {
      final ids = _defaultspackToolAgentIds();
      expect(ids, isNotEmpty);

      for (final id in ids) {
        final result = runtime.execute(
          MobileToolCall(
            id: 'schema_$id',
            name: 'tool_schema',
            arguments: {'tool_name': id},
          ),
        );
        expect(result.ok, isTrue, reason: '$id schema should be inspectable');
        final payload = jsonDecode(result.output) as Map<String, dynamic>;
        final data = payload['data'] as Map<String, dynamic>;
        final aliases = (data['aliases'] as List? ?? const [])
            .map((alias) => '$alias')
            .toSet();
        expect(
          data['tool_id'] == id ||
              data['requested_name'] == id ||
              aliases.contains(id),
          isTrue,
          reason: '$id must resolve directly or as a mobile alias',
        );
        expect(
          '${data['unavailable_reason'] ?? ''}',
          isNot(contains('mobile-compatible runtimeに未登録')),
          reason: '$id must have a specific phone/PC execution answer',
        );
      }
    });
  });
}

List<String> _defaultspackToolAgentIds() {
  return _defaultspackToolAgentRecords().keys.toList()..sort();
}

List<String> _nativeDefaultspackToolAgentIds() {
  return _defaultspackToolAgentRecords()
      .entries
      .where((entry) {
        final tags = entry.value.tags.toSet();
        return tags.contains('agent') ||
            (tags.contains('tool') && !tags.contains('tool_registry'));
      })
      .map((entry) => entry.key)
      .toList()
    ..sort();
}

Map<String, _ManifestRecord> _defaultspackToolAgentRecords() {
  final functionsRoot =
      Directory('../tobkiri_runtime/ecosystem/defaultspack/functions');
  final toolsRoot =
      Directory('../tobkiri_runtime/ecosystem/defaultspack/tools');
  expect(functionsRoot.existsSync(), isTrue);
  expect(toolsRoot.existsSync(), isTrue);
  final records = <String, _ManifestRecord>{};

  void putRecord(String id, _ManifestRecord record) {
    if (id.isEmpty) return;
    records[id] = records[id]?.merge(record) ?? record;
  }

  final functionManifests = functionsRoot
      .listSync(recursive: true)
      .whereType<File>()
      .where((file) => file.path.endsWith('/manifest.json'));
  for (final file in functionManifests) {
    final manifest = jsonDecode(file.readAsStringSync());
    if (manifest is! Map<String, dynamic>) continue;
    final tags =
        (manifest['tags'] as List? ?? const []).map((tag) => '$tag').toSet();
    if (!tags.contains('tool') && !tags.contains('agent')) continue;
    final functionId = '${manifest['function_id'] ?? ''}'.trim();
    if (functionId.isEmpty) continue;
    final aliases = (manifest['vocab_aliases'] as List? ?? const [])
        .map((alias) => '$alias'.trim())
        .where((alias) => alias.isNotEmpty)
        .toList()
      ..sort();
    final inputSchema = manifest['input_schema'] is Map<String, dynamic>
        ? manifest['input_schema'] as Map<String, dynamic>
        : const <String, dynamic>{
            'type': 'object',
            'additionalProperties': true,
          };
    putRecord(
      functionId,
      _ManifestRecord(
        description: '${manifest['description'] ?? ''}',
        tags: tags.toList()..sort(),
        aliases: aliases,
        inputSchema: inputSchema,
      ),
    );
  }

  final toolManifests = toolsRoot
      .listSync(recursive: true)
      .whereType<File>()
      .where((file) => file.path.endsWith('/manifest.json'));
  for (final file in toolManifests) {
    final manifest = jsonDecode(file.readAsStringSync());
    if (manifest is! Map<String, dynamic>) continue;
    final config = manifest['config'] is Map<String, dynamic>
        ? manifest['config'] as Map<String, dynamic>
        : const <String, dynamic>{};
    final id = '${config['tool_id'] ?? manifest['id'] ?? ''}'.trim();
    if (id.isEmpty) continue;
    final tags = <String>{
      'tool_registry',
      ...(config['tags'] as List? ?? const []).map((tag) => '$tag'),
    };
    if (config['requires_approval'] == true) tags.add('requires_approval');
    final category = '${config['tool_category'] ?? ''}'.trim();
    if (category.isNotEmpty) tags.add(category);
    final schema = config['schema'] is Map<String, dynamic>
        ? config['schema'] as Map<String, dynamic>
        : const <String, dynamic>{};
    final parameters = schema['parameters'] is Map<String, dynamic>
        ? schema['parameters'] as Map<String, dynamic>
        : const <String, dynamic>{
            'type': 'object',
            'additionalProperties': true,
          };
    putRecord(
      id,
      _ManifestRecord(
        description:
            '${config['summary'] ?? manifest['description'] ?? ''}'.trim(),
        tags: tags.toList()..sort(),
        aliases: const [],
        inputSchema: parameters,
      ),
    );
  }
  return records;
}

String _canonicalJson(Object? value) => jsonEncode(_normalizeJson(value));

Object? _normalizeJson(Object? value) {
  if (value is Map) {
    final keys = value.keys.map((key) => '$key').toList()..sort();
    return {
      for (final key in keys) key: _normalizeJson(value[key]),
    };
  }
  if (value is List) {
    return value.map(_normalizeJson).toList();
  }
  return value;
}

class _ManifestRecord {
  const _ManifestRecord({
    required this.description,
    required this.tags,
    required this.aliases,
    required this.inputSchema,
  });

  final String description;
  final List<String> tags;
  final List<String> aliases;
  final Map<String, dynamic> inputSchema;

  _ManifestRecord merge(_ManifestRecord other) {
    return _ManifestRecord(
      description:
          other.description.isNotEmpty ? other.description : description,
      tags: {...tags, ...other.tags}.toList()..sort(),
      aliases: {...aliases, ...other.aliases}.toList()..sort(),
      inputSchema:
          other.inputSchema.isNotEmpty ? other.inputSchema : inputSchema,
    );
  }
}
