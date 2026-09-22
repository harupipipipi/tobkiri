import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:rumi_remote_app/src/chat/chat_models.dart';
import 'package:rumi_remote_app/src/chat/chat_store.dart';
import 'package:rumi_remote_app/src/chat/openai_client.dart';
import 'package:rumi_remote_app/src/data/local/local_chat_backend.dart';
import 'package:rumi_remote_app/src/domain/branch_lineage.dart';
import 'package:rumi_remote_app/src/domain/chat_event.dart';
import 'package:rumi_remote_app/src/domain/connection_state.dart';
import 'package:rumi_remote_app/src/domain/conversation_backend.dart';
import 'package:rumi_remote_app/src/domain/conversation_locator.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

class _FakeSecureStorage implements SecureKeyValueStorage {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      _values.remove(key);
    } else {
      _values[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }
}

class _FakeChatStorage implements ChatKeyValueStorage {
  final Map<String, String> _values = {};

  @override
  Future<String?> read(String key) async => _values[key];

  @override
  Future<void> write(String key, String value) async {
    _values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    _values.remove(key);
  }
}

http.Client _sseClient(
    void Function(http.Request) onRequest, List<String> chunks) {
  return MockClient.streaming((request, bodyStream) async {
    onRequest(request as http.Request);
    final bytes = chunks.map(utf8.encode).toList();
    final stream = Stream<List<int>>.fromIterable(bytes);
    return http.StreamedResponse(stream, 200);
  });
}

void main() {
  group('ConversationLocator', () {
    test('local/pc factories set authority', () {
      expect(ConversationLocator.local('a').authority,
          ConversationAuthorityKind.local);
      expect(
          ConversationLocator.pc('b').authority, ConversationAuthorityKind.pc);
    });

    test('equality considers authority, id, device', () {
      expect(ConversationLocator.local('a'), ConversationLocator.local('a'));
      expect(ConversationLocator.local('a') == ConversationLocator.pc('a'),
          isFalse);
      expect(
          ConversationLocator.pc('a', deviceId: 'd') ==
              ConversationLocator.pc('a', deviceId: 'd'),
          isTrue);
    });
  });

  group('BranchLineage', () {
    test('round-trips through json', () {
      final lineage = BranchLineage(
        parentConversationId: 'p1',
        forkedAtMessageId: 'm3',
        parentAuthority: ConversationAuthorityKind.pc,
        parentDeviceId: 'mac',
        reason: BranchReason.offlineContinue,
      );
      final decoded = BranchLineage.fromJson(lineage.toJson());
      expect(decoded.parentConversationId, 'p1');
      expect(decoded.parentAuthority, ConversationAuthorityKind.pc);
      expect(decoded.reason, BranchReason.offlineContinue);
    });
  });

  group('DeviceConnectionView', () {
    test('unpaired default has no capabilities', () {
      const view = DeviceConnectionView.unpaired;
      expect(view.pairingState, PairingState.unpaired);
      expect(view.canWritePcConversations, isFalse);
      expect(view.isPcOnline, isFalse);
    });
  });

  group('LocalConversationBackend', () {
    late ChatStore store;
    late ApiConfigStore configStore;

    setUp(() async {
      store = ChatStore(storage: _FakeChatStorage());
      await store.load();
      final storage = _FakeSecureStorage();
      await storage.write(
        'rumi.api_config.v1',
        jsonEncode(const ApiConfig(
          baseUrl: 'https://api.example.com/v1',
          apiKey: 'sk-test',
          model: 'gpt-test',
        ).toJson()),
      );
      configStore = ApiConfigStore(storage: storage);
    });

    test('create + get conversation', () async {
      final backend =
          LocalConversationBackend(store: store, configStore: configStore);
      final locator = await backend.createConversation(
        const CreateConversationRequest(
            authority: ConversationAuthorityKind.local),
      );
      expect(locator.isLocal, isTrue);
      final snap = await backend.getConversation(locator);
      expect(snap.conversation.id, locator.conversationId);
      expect(snap.revision, 0);
    });

    test('listConversations reflects store', () async {
      await store.createAndPersist();
      final backend =
          LocalConversationBackend(store: store, configStore: configStore);
      final list = await backend.listConversations();
      expect(list.length, 1);
      expect(list.first.authority, ConversationAuthorityKind.local);
    });

    test('sendMessage streams deltas and commits assistant content', () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);

      final client = _sseClient(
        (request) {
          expect(request.url.toString(),
              'https://api.example.com/v1/chat/completions');
        },
        [
          'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
          'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
          'data: [DONE]\n\n',
        ],
      );

      final backend = LocalConversationBackend(
        store: store,
        configStore: configStore,
        createClient: () => OpenAiClient(client: client),
      );

      final events = await backend
          .sendMessage(
            locator: locator,
            text: 'hi',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();

      final deltas = events.whereType<ChatDelta>().toList();
      expect(deltas, isNotEmpty);
      expect(deltas.last.accumulatedContent, 'Hello world');
      expect(events.whereType<ChatRunStarted>(), isNotEmpty);
      expect(events.whereType<ChatMessageCommitted>().single.content,
          'Hello world');
      expect(events.whereType<ChatMessageCommitted>().single.error, isFalse);
      expect(events.whereType<ChatRunCompleted>(), isNotEmpty);

      final updated = store.conversations.firstWhere((c) => c.id == convo.id);
      expect(updated.messages.length, 2);
      expect(updated.messages.first.role, ChatRole.user);
      expect(updated.messages.last.content, 'Hello world');
      expect(updated.messages.last.pending, isFalse);
    });

    test('sendMessage executes mobile tool calls and continues agent turn',
        () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);
      var requestCount = 0;
      final bodies = <Map<String, dynamic>>[];

      final client = MockClient.streaming((request, bodyStream) async {
        requestCount += 1;
        bodies.add(jsonDecode(await bodyStream.transform(utf8.decoder).join())
            as Map<String, dynamic>);
        if (requestCount == 1) {
          final args = jsonEncode({'expression': '2 + 2'});
          final toolChunk = jsonEncode({
            'choices': [
              {
                'delta': {
                  'tool_calls': [
                    {
                      'index': 0,
                      'id': 'call_1',
                      'type': 'function',
                      'function': {
                        'name': 'calculator',
                        'arguments': args,
                      },
                    },
                  ],
                },
              },
            ],
          });
          return http.StreamedResponse(
            Stream<List<int>>.fromIterable([
              utf8.encode('data: $toolChunk\n\n'),
              utf8.encode('data: [DONE]\n\n'),
            ]),
            200,
          );
        }
        return http.StreamedResponse(
          Stream<List<int>>.fromIterable([
            utf8.encode(
                'data: {"choices":[{"delta":{"content":"答えは4です"}}]}\n\n'),
            utf8.encode('data: [DONE]\n\n'),
          ]),
          200,
        );
      });

      final backend = LocalConversationBackend(
        store: store,
        configStore: configStore,
        createClient: () => OpenAiClient(client: client),
      );

      final events = await backend
          .sendMessage(
            locator: locator,
            text: '2+2を計算して',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();

      expect(requestCount, 2);
      expect((bodies.first['tools'] as List).first['function']['name'],
          'calculator');
      final system = (bodies.first['messages'] as List)
          .where((message) => message['role'] == 'system')
          .single['content'] as String;
      expect(system, contains('defaultspack mobile agent template'));
      expect(system, contains('agent_plan'));
      expect(system, contains('tool_task_board'));
      final secondMessages = bodies.last['messages'] as List;
      expect(secondMessages.any((m) => m['role'] == 'tool'), isTrue);
      final toolEvents = events.whereType<ToolCallEvent>().toList();
      expect(toolEvents.map((e) => e.status),
          containsAll(['running', 'completed']));
      expect(toolEvents.last.output, contains('2 + 2 = 4'));
      expect(events.whereType<ChatStatusEvent>(), isNotEmpty);
      expect(events.whereType<ChatMessageCommitted>().single.content, '答えは4です');
    });

    test('sendMessage falls back to JSON tool protocol when tools are rejected',
        () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);
      var requestCount = 0;
      final bodies = <Map<String, dynamic>>[];

      String textDelta(String text) {
        return 'data: ${jsonEncode({
              'choices': [
                {
                  'delta': {'content': text},
                },
              ],
            })}\n\n';
      }

      final client = MockClient.streaming((request, bodyStream) async {
        requestCount += 1;
        bodies.add(jsonDecode(await bodyStream.transform(utf8.decoder).join())
            as Map<String, dynamic>);
        if (requestCount == 1) {
          return http.StreamedResponse(
            Stream<List<int>>.fromIterable([
              utf8.encode(jsonEncode({
                'error': {'message': 'tools are unsupported by this model'},
              })),
            ]),
            400,
          );
        }
        if (requestCount == 2) {
          return http.StreamedResponse(
            Stream<List<int>>.fromIterable([
              utf8.encode(textDelta(jsonEncode({
                'tool_calls': [
                  {
                    'id': 'call_fallback',
                    'name': 'tool_calculator',
                    'arguments': {'expression': '9 - 4'},
                  },
                ],
              }))),
              utf8.encode('data: [DONE]\n\n'),
            ]),
            200,
          );
        }
        return http.StreamedResponse(
          Stream<List<int>>.fromIterable([
            utf8.encode(textDelta('答えは5です')),
            utf8.encode('data: [DONE]\n\n'),
          ]),
          200,
        );
      });

      final backend = LocalConversationBackend(
        store: store,
        configStore: configStore,
        createClient: () => OpenAiClient(client: client),
      );

      final events = await backend
          .sendMessage(
            locator: locator,
            text: '9-4を計算して',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();

      expect(requestCount, 3);
      expect(bodies.first.containsKey('tools'), isTrue);
      expect(bodies[1].containsKey('tools'), isFalse);
      final fallbackSystem = (bodies[1]['messages'] as List)
          .where((message) => message['role'] == 'system')
          .single['content'] as String;
      expect(fallbackSystem, contains('JSON tool protocol fallback'));
      expect(fallbackSystem, contains('Generated defaultspack catalog'));
      expect(fallbackSystem, contains('not phone-executable'));
      expect(fallbackSystem, isNot(contains('. 0 are not phone-executable')));
      expect(fallbackSystem, contains('agent_plan'));
      final finalMessages = bodies.last['messages'] as List;
      expect(
        finalMessages.any((message) =>
            message['role'] == 'user' &&
            '${message['content']}'.contains('Tool results JSON')),
        isTrue,
      );
      final statuses = events.whereType<ChatStatusEvent>().toList();
      expect(statuses.map((event) => event.phase), contains('tools_fallback'));
      expect(statuses.map((event) => event.phase), contains('tool_execution'));
      final toolEvents = events.whereType<ToolCallEvent>().toList();
      expect(toolEvents.map((event) => event.status),
          containsAll(['running', 'completed']));
      expect(toolEvents.last.toolName, 'tool_calculator');
      expect(toolEvents.last.output, contains('9 - 4 = 5'));
      expect(events.whereType<ChatMessageCommitted>().single.content, '答えは5です');
    });

    test('sendMessage executes Anthropic tool_use calls on phone', () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);
      final anthropicStorage = _FakeSecureStorage();
      final anthropicConfigStore = ApiConfigStore(storage: anthropicStorage);
      await anthropicConfigStore.saveApi(const ApiConfig(
        providerId: 'anthropic',
        apiCompatibility: 'anthropic_messages',
        baseUrl: 'https://anthropic.example.com/v1',
        apiKey: 'sk-ant-test',
        model: 'claude-test',
      ));
      var requestCount = 0;
      final bodies = <Map<String, dynamic>>[];

      String event(Map<String, dynamic> data) =>
          'event: ${data['type']}\ndata: ${jsonEncode(data)}\n\n';

      final client = MockClient.streaming((request, bodyStream) async {
        requestCount += 1;
        expect(request.url.toString(),
            'https://anthropic.example.com/v1/messages');
        expect(request.headers['x-api-key'], 'sk-ant-test');
        bodies.add(jsonDecode(await bodyStream.transform(utf8.decoder).join())
            as Map<String, dynamic>);
        if (requestCount == 1) {
          return http.StreamedResponse(
            Stream<List<int>>.fromIterable([
              utf8.encode(event({
                'type': 'content_block_start',
                'index': 0,
                'content_block': {
                  'type': 'tool_use',
                  'id': 'toolu_1',
                  'name': 'tool_calculator',
                  'input': {},
                },
              })),
              utf8.encode(event({
                'type': 'content_block_delta',
                'index': 0,
                'delta': {
                  'type': 'input_json_delta',
                  'partial_json': '{"expression":"3 * 5"}',
                },
              })),
              utf8.encode(event({'type': 'message_stop'})),
            ]),
            200,
          );
        }
        return http.StreamedResponse(
          Stream<List<int>>.fromIterable([
            utf8.encode(event({
              'type': 'content_block_start',
              'index': 0,
              'content_block': {'type': 'text', 'text': ''},
            })),
            utf8.encode(event({
              'type': 'content_block_delta',
              'index': 0,
              'delta': {'type': 'text_delta', 'text': '答えは15です'},
            })),
            utf8.encode(event({'type': 'message_stop'})),
          ]),
          200,
        );
      });

      final backend = LocalConversationBackend(
        store: store,
        configStore: anthropicConfigStore,
        createClient: () => OpenAiClient(client: client),
      );

      final events = await backend
          .sendMessage(
            locator: locator,
            text: '3*5を計算して',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();

      expect(requestCount, 2);
      expect((bodies.first['tools'] as List).map((tool) => tool['name']),
          contains('tool_calculator'));
      expect(bodies.first['system'], contains('Rumi Mobile'));
      final secondMessages = bodies.last['messages'] as List;
      expect(
        secondMessages.any((message) =>
            message['role'] == 'user' &&
            message['content'] is List &&
            (message['content'] as List)
                .any((block) => block['type'] == 'tool_result')),
        isTrue,
      );
      final toolEvents = events.whereType<ToolCallEvent>().toList();
      expect(toolEvents.map((e) => e.status),
          containsAll(['running', 'completed']));
      expect(toolEvents.last.output, contains('3 * 5 = 15'));
      expect(
        events.whereType<ChatStatusEvent>().map((event) => event.message),
        isNot(contains(contains('未対応'))),
      );
      expect(
          events.whereType<ChatMessageCommitted>().single.content, '答えは15です');
    });

    test('sendMessage renders assistant_progress as status only', () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);
      var requestCount = 0;
      final bodies = <Map<String, dynamic>>[];

      final client = MockClient.streaming((request, bodyStream) async {
        requestCount += 1;
        bodies.add(jsonDecode(await bodyStream.transform(utf8.decoder).join())
            as Map<String, dynamic>);
        if (requestCount == 1) {
          final args = jsonEncode({
            'phase': 'inspect',
            'status': 'active',
            'summary': '調べています',
            'next_action': '必要なtoolを選びます',
          });
          final progressChunk = jsonEncode({
            'choices': [
              {
                'delta': {
                  'tool_calls': [
                    {
                      'index': 0,
                      'id': 'progress_1',
                      'type': 'function',
                      'function': {
                        'name': 'assistant_progress',
                        'arguments': args,
                      },
                    },
                  ],
                },
              },
            ],
          });
          return http.StreamedResponse(
            Stream<List<int>>.fromIterable([
              utf8.encode('data: $progressChunk\n\n'),
              utf8.encode('data: [DONE]\n\n'),
            ]),
            200,
          );
        }
        return http.StreamedResponse(
          Stream<List<int>>.fromIterable([
            utf8.encode('data: {"choices":[{"delta":{"content":"完了"}}]}\n\n'),
            utf8.encode('data: [DONE]\n\n'),
          ]),
          200,
        );
      });

      final backend = LocalConversationBackend(
        store: store,
        configStore: configStore,
        createClient: () => OpenAiClient(client: client),
      );

      final events = await backend
          .sendMessage(
            locator: locator,
            text: '進捗を出して',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();

      expect(requestCount, 2);
      final tools = bodies.first['tools'] as List;
      expect(
        tools.any((tool) => tool['function']['name'] == 'assistant_progress'),
        isTrue,
      );
      final system = (bodies.first['messages'] as List)
          .where((message) => message['role'] == 'system')
          .single['content'] as String;
      expect(system, contains('Internal progress tool'));
      expect(events.whereType<ToolCallEvent>(), isEmpty);
      expect(
        events.whereType<ChatStatusEvent>().map((event) => event.message).any(
            (message) =>
                message.contains('調べています') && message.contains('必要なtoolを選びます')),
        isTrue,
      );
      expect(events.whereType<ChatMessageCommitted>().single.content, '完了');
    });

    test('sendMessage exposes PC-only defaultspack tools with reasons',
        () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);
      var requestCount = 0;
      final bodies = <Map<String, dynamic>>[];

      final client = MockClient.streaming((request, bodyStream) async {
        requestCount += 1;
        bodies.add(jsonDecode(await bodyStream.transform(utf8.decoder).join())
            as Map<String, dynamic>);
        if (requestCount == 1) {
          final args = jsonEncode({'objective': 'PC agent task'});
          final toolChunk = jsonEncode({
            'choices': [
              {
                'delta': {
                  'tool_calls': [
                    {
                      'index': 0,
                      'id': 'pc_only_1',
                      'type': 'function',
                      'function': {
                        'name': 'agent_execute',
                        'arguments': args,
                      },
                    },
                  ],
                },
              },
            ],
          });
          return http.StreamedResponse(
            Stream<List<int>>.fromIterable([
              utf8.encode('data: $toolChunk\n\n'),
              utf8.encode('data: [DONE]\n\n'),
            ]),
            200,
          );
        }
        return http.StreamedResponse(
          Stream<List<int>>.fromIterable([
            utf8.encode(
              'data: {"choices":[{"delta":{"content":"PC側runtimeが必要です"}}]}\n\n',
            ),
            utf8.encode('data: [DONE]\n\n'),
          ]),
          200,
        );
      });

      final backend = LocalConversationBackend(
        store: store,
        configStore: configStore,
        createClient: () => OpenAiClient(client: client),
      );

      final events = await backend
          .sendMessage(
            locator: locator,
            text: 'PC agentを動かして',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();

      expect(requestCount, 2);
      final toolNames = (bodies.first['tools'] as List)
          .map((tool) => tool['function']['name'] as String)
          .toSet();
      expect(toolNames, contains('agent_execute'));
      final exposed = (bodies.first['tools'] as List).singleWhere(
        (tool) => tool['function']['name'] == 'agent_execute',
      );
      expect(
        exposed['function']['description'],
        contains('not phone-executable'),
      );
      final toolEvents = events.whereType<ToolCallEvent>().toList();
      expect(toolEvents.map((event) => event.status),
          containsAll(['running', 'failed']));
      expect(toolEvents.last.toolName, 'agent_execute');
      expect(toolEvents.last.output, contains('PC側'));
      expect(events.whereType<ChatMessageCommitted>().single.content,
          'PC側runtimeが必要です');
    });

    test('sendMessage executes defaultspack tool_invoke broker on phone',
        () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);
      var requestCount = 0;
      final bodies = <Map<String, dynamic>>[];

      final client = MockClient.streaming((request, bodyStream) async {
        requestCount += 1;
        bodies.add(jsonDecode(await bodyStream.transform(utf8.decoder).join())
            as Map<String, dynamic>);
        if (requestCount == 1) {
          final args = jsonEncode({
            'tool_name': 'tool_calculator',
            'arguments': {'expression': '7 * 6'},
          });
          final toolChunk = jsonEncode({
            'choices': [
              {
                'delta': {
                  'tool_calls': [
                    {
                      'index': 0,
                      'id': 'invoke_1',
                      'type': 'function',
                      'function': {
                        'name': 'tool_invoke',
                        'arguments': args,
                      },
                    },
                  ],
                },
              },
            ],
          });
          return http.StreamedResponse(
            Stream<List<int>>.fromIterable([
              utf8.encode('data: $toolChunk\n\n'),
              utf8.encode('data: [DONE]\n\n'),
            ]),
            200,
          );
        }
        return http.StreamedResponse(
          Stream<List<int>>.fromIterable([
            utf8.encode('data: {"choices":[{"delta":{"content":"42です"}}]}\n\n'),
            utf8.encode('data: [DONE]\n\n'),
          ]),
          200,
        );
      });

      final backend = LocalConversationBackend(
        store: store,
        configStore: configStore,
        createClient: () => OpenAiClient(client: client),
      );

      final events = await backend
          .sendMessage(
            locator: locator,
            text: 'tool_invokeで計算して',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();

      expect(requestCount, 2);
      final toolNames = (bodies.first['tools'] as List)
          .map((tool) => tool['function']['name'] as String)
          .toSet();
      expect(toolNames, contains('tool_invoke'));
      final toolEvents = events.whereType<ToolCallEvent>().toList();
      expect(toolEvents.map((event) => event.status),
          containsAll(['running', 'completed']));
      expect(toolEvents.last.toolName, 'tool_invoke');
      expect(toolEvents.last.output, contains('7 * 6 = 42'));
      expect(events.whereType<ChatMessageCommitted>().single.content, '42です');
    });

    test('sendMessage surfaces unconfigured api as error event', () async {
      final convo = await store.createAndPersist();
      final locator = ConversationLocator.local(convo.id);
      final emptyStorage = _FakeSecureStorage();
      final emptyConfig = ApiConfigStore(storage: emptyStorage);
      final backend = LocalConversationBackend(
        store: store,
        configStore: emptyConfig,
      );
      final events = await backend
          .sendMessage(
            locator: locator,
            text: 'hi',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();
      expect(events, isA<List<ChatEvent>>());
      expect(events.single, isA<ChatErrorEvent>());
      expect((events.single as ChatErrorEvent).message, contains('APIのURLとキー'));
    });
  });
}
