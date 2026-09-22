import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:http/http.dart' as http;

import 'package:rumi_remote_app/src/data/pc/pc_chat_backend.dart';
import 'package:rumi_remote_app/src/domain/chat_event.dart';
import 'package:rumi_remote_app/src/domain/conversation_backend.dart';
import 'package:rumi_remote_app/src/domain/conversation_locator.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';

const _pc = PcConnection(baseUrl: 'http://192.168.1.10:8765', token: 'tok');

http.Response _ok(Map<String, dynamic> data) {
  return http.Response(jsonEncode({'status': 'ok', 'data': data}), 200,
      headers: {'content-type': 'application/json'});
}

void main() {
  group('PcConversationBackend', () {
    test('listConversations fetches from API', () async {
      final client = MockClient((request) async {
        expect(request.url.path, '/api/mobile/v1/conversations');
        expect(request.headers['Authorization'], 'Bearer tok');
        return _ok({
          'conversations': [
            {
              'id': 'c1',
              'title': 'テスト会話',
              'message_count': 5,
              'updated_at': '2026-01-01T00:00:00Z',
              'pinned': false,
              'revision': 3,
            },
          ],
        });
      });

      final backend = PcConversationBackend(
        connection: _pc,
        deviceId: 'mobile-abc',
        client: client,
      );

      final list = await backend.listConversations();
      backend.close();

      expect(list.length, 1);
      expect(list.first.id, 'c1');
      expect(list.first.title, 'テスト会話');
      expect(list.first.authority, ConversationAuthorityKind.pc);
      expect(list.first.messageCount, 5);
    });

    test('parses PC epoch timestamps and pinned aliases', () async {
      final client = MockClient((request) async {
        if (request.url.path == '/api/mobile/v1/conversations') {
          return _ok({
            'conversations': [
              {
                'id': 'c1',
                'title': 'PC Chat',
                'message_count': 1,
                'updated_at': 1782325909557,
                'is_pinned': true,
                'revision': 4,
              },
            ],
          });
        }
        return _ok({
          'conversation': {
            'id': 'c1',
            'title': 'PC Chat',
            'created_at': 1782325909557,
            'updated_at': 1782325909557,
            'is_pinned': true,
            'revision': 4,
            'messages': [
              {
                'id': 123,
                'role': 'user',
                'content': 'hello',
                'created_at': 1782325909557,
              },
            ],
          },
        });
      });

      final backend = PcConversationBackend(
        connection: _pc,
        deviceId: 'mobile-abc',
        client: client,
      );

      final summaries = await backend.listConversations();
      final snapshot =
          await backend.getConversation(ConversationLocator.pc('c1'));
      backend.close();

      expect(summaries.single.pinned, isTrue);
      expect(summaries.single.updatedAt.year, greaterThanOrEqualTo(2026));
      expect(snapshot.conversation.authority, ConversationAuthorityKind.pc);
      expect(snapshot.conversation.pinned, isTrue);
      expect(snapshot.conversation.messages.single.id, '123');
      expect(snapshot.conversation.messages.single.content, 'hello');
    });

    test('createConversation sends POST and returns locator', () async {
      final client = MockClient((request) async {
        expect(request.method, 'POST');
        expect(request.url.path, '/api/mobile/v1/conversations');
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['title'], 'New Chat');
        return _ok({
          'conversation_id': 'c2',
        });
      });

      final backend = PcConversationBackend(
        connection: _pc,
        deviceId: 'mobile-abc',
        client: client,
      );

      final locator = await backend.createConversation(
        const CreateConversationRequest(
          title: 'New Chat',
          authority: ConversationAuthorityKind.pc,
        ),
      );
      backend.close();

      expect(locator.authority, ConversationAuthorityKind.pc);
      expect(locator.conversationId, 'c2');
      expect(locator.deviceId, 'mobile-abc');
    });

    test('sendMessage streams SSE events', () async {
      final sseChunks = [
        'data: {"type":"delta","delta":"Hello","content":""}\n\n',
        'data: {"type":"delta","delta":" world","content":"Hello world"}\n\n',
        'data: {"type":"tool_call","tool_id":"t1","tool_name":"terminal","tool_status":"running","summary":"ls -la"}\n\n',
        'data: {"type":"tool_call","tool_id":"t1","tool_name":"terminal","tool_status":"complete"}\n\n',
        'data: [DONE]\n\n',
      ];

      final client = MockClient.streaming((request, bodyStream) async {
        expect(request.method, 'POST');
        expect(request.url.path, '/api/mobile/v1/conversations/c1/stream');
        final bytes = sseChunks.map(utf8.encode).toList();
        final stream = Stream<List<int>>.fromIterable(bytes);
        return http.StreamedResponse(stream, 200);
      });

      final backend = PcConversationBackend(
        connection: _pc,
        client: client,
      );

      final locator = ConversationLocator.pc('c1');
      final events = await backend
          .sendMessage(
            locator: locator,
            text: 'hi',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();
      backend.close();

      final deltas = events.whereType<ChatDelta>().toList();
      expect(deltas.length, 2);
      expect(deltas.last.accumulatedContent, 'Hello world');

      final toolCalls = events.whereType<ToolCallEvent>().toList();
      expect(toolCalls.length, 2);
      expect(toolCalls.first.status, 'running');
      expect(toolCalls.last.status, 'complete');

      expect(events.whereType<ChatRunStarted>(), isNotEmpty);
      expect(events.whereType<ChatMessageCommitted>(), isNotEmpty);
      expect(events.whereType<ChatRunCompleted>(), isNotEmpty);
    });

    test('sendMessage yields error on non-200 response', () async {
      final client = MockClient.streaming((request, bodyStream) async {
        return http.StreamedResponse(
            Stream.value(utf8.encode('Server Error')), 500);
      });

      final backend = PcConversationBackend(
        connection: _pc,
        client: client,
      );

      final locator = ConversationLocator.pc('c1');
      final events = await backend
          .sendMessage(
            locator: locator,
            text: 'hi',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();
      backend.close();

      expect(events.whereType<ChatRunStarted>(), isNotEmpty);
      expect(events.whereType<ChatErrorEvent>(), isNotEmpty);
    });

    test('sendMessage includes selected PC model in params', () async {
      Map<String, dynamic>? body;
      final client = MockClient.streaming((request, bodyStream) async {
        body = jsonDecode(await bodyStream.transform(utf8.decoder).join())
            as Map<String, dynamic>;
        return http.StreamedResponse(
            Stream.value(utf8.encode('data: [DONE]\n\n')), 200);
      });

      final backend = PcConversationBackend(
        connection: _pc,
        client: client,
      );

      await backend.sendMessage(
        locator: ConversationLocator.pc('c1'),
        text: 'hi',
        clientMessageId: 'u1',
        expectedRevision: 0,
        model: 'xiaomi-token-plan-sgp/mimo-v2.5-pro',
        profileId: 'xiaomi-token-plan-sgp/mimo-v2.5-pro',
        params: {
          'thinking_level': 'low',
          'metadata': {'mode': 'coding'},
        },
      ).toList();
      backend.close();

      expect(body?['model'], 'xiaomi-token-plan-sgp/mimo-v2.5-pro');
      expect(body?['profile_id'], 'xiaomi-token-plan-sgp/mimo-v2.5-pro');
      final params = body?['params'] as Map<String, dynamic>;
      expect(params['model'], 'xiaomi-token-plan-sgp/mimo-v2.5-pro');
      expect(params['profile_id'], 'xiaomi-token-plan-sgp/mimo-v2.5-pro');
      expect(params['thinking_level'], 'low');
      final message = body?['message'] as Map<String, dynamic>;
      expect((message['metadata'] as Map)['mode'], 'coding');
    });

    test('isConfigured reflects connection state', () {
      final backend1 = PcConversationBackend(connection: _pc);
      expect(backend1.isConfigured, isTrue);
      backend1.close();

      final backend2 = PcConversationBackend(
          connection: const PcConnection(baseUrl: '', token: ''));
      expect(backend2.isConfigured, isFalse);
      backend2.close();
    });

    test('throws on listConversations when not configured', () async {
      final backend = PcConversationBackend(
          connection: const PcConnection(baseUrl: '', token: ''));
      expect(
        () => backend.listConversations(),
        throwsA(isA<StateError>()),
      );
      backend.close();
    });
  });

  group('PcConversationBackend SSE event parsing', () {
    test('parses approval events from SSE stream', () async {
      final sseChunks = [
        'data: {"type":"approval","approval_id":"a1","tool_name":"terminal","prompt":"Execute rm -rf?","pending":true}\n\n',
        'data: {"type":"approval","approval_id":"a1","tool_name":"terminal","prompt":"Execute rm -rf?","approved":true,"pending":false}\n\n',
        'data: [DONE]\n\n',
      ];

      final client = MockClient.streaming((request, bodyStream) async {
        final bytes = sseChunks.map(utf8.encode).toList();
        final stream = Stream<List<int>>.fromIterable(bytes);
        return http.StreamedResponse(stream, 200);
      });

      final backend = PcConversationBackend(
        connection: _pc,
        client: client,
      );

      final locator = ConversationLocator.pc('c1');
      final events = await backend
          .sendMessage(
            locator: locator,
            text: 'hi',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();
      backend.close();

      final approvals = events.whereType<ApprovalEvent>().toList();
      expect(approvals.length, 2);
      expect(approvals.first.pending, isTrue);
      expect(approvals.last.approved, isTrue);
      expect(approvals.last.pending, isFalse);
    });

    test('parses canonical defaultspack status and tool events', () async {
      final sseChunks = [
        'data: {"type":"status","message":"モデルが考えています","phase":"thinking"}\n\n',
        'data: {"type":"tool_call_started","tool_call_id":"tc1","tool_name":"calculator","status":"running","summary":"2+2"}\n\n',
        'data: {"type":"tool_call_completed","tool_call_id":"tc1","tool_name":"calculator","status":"completed","result_summary":"4"}\n\n',
        'data: {"type":"approval_requested","request_id":"req1","tool_name":"terminal","message":"承認が必要です","arguments":{"command":"pwd"}}\n\n',
        'data: [DONE]\n\n',
      ];

      final client = MockClient.streaming((request, bodyStream) async {
        final bytes = sseChunks.map(utf8.encode).toList();
        final stream = Stream<List<int>>.fromIterable(bytes);
        return http.StreamedResponse(stream, 200);
      });

      final backend = PcConversationBackend(
        connection: _pc,
        client: client,
      );

      final events = await backend
          .sendMessage(
            locator: ConversationLocator.pc('c1'),
            text: 'hi',
            clientMessageId: 'u1',
            expectedRevision: 0,
          )
          .toList();
      backend.close();

      expect(events.whereType<ChatStatusEvent>().single.message, 'モデルが考えています');
      final toolEvents = events.whereType<ToolCallEvent>().toList();
      expect(toolEvents.length, 2);
      expect(toolEvents.first.toolId, 'tc1');
      expect(toolEvents.last.status, 'completed');
      expect(toolEvents.last.summary, '4');
      final approval = events.whereType<ApprovalEvent>().single;
      expect(approval.approvalId, 'req1');
      expect(approval.prompt, '承認が必要です');
      expect(approval.arguments['command'], 'pwd');
    });
  });
}
