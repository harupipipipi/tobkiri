import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:rumi_remote_app/src/mobile_chat_api.dart';

void main() {
  test('lists and loads conversations through finite mobile routes', () async {
    final requests = <http.Request>[];
    final client = MockClient((request) async {
      requests.add(request);
      if (request.url.path.endsWith('/api/mobile/v1/conversations')) {
        return http.Response(
          jsonEncode({
            'status': 'ok',
            'data': {
              'conversations': [
                {'id': 'c-1', 'title': '会話', 'message_count': 2},
              ],
            },
          }),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }
      return http.Response(
        jsonEncode({
          'status': 'ok',
          'data': {
            'conversation': {
              'id': 'c-1',
              'title': '会話',
              'revision': 4,
              'messages': [
                {'id': 'm-1', 'role': 'user', 'content': 'こんにちは'},
                {
                  'id': 'm-2',
                  'role': 'assistant',
                  'content': [
                    {'text': '回答'},
                  ],
                },
              ],
            },
          },
        }),
        200,
        headers: {'content-type': 'application/json; charset=utf-8'},
      );
    });
    final gateway = HttpMobileChatGateway(
      baseUrl: 'https://tobkiri.example/base/',
      bearerToken: 'test-token',
      httpClient: client,
    );

    final summaries = await gateway.listConversations();
    final snapshot = await gateway.getConversation('c-1');

    expect(summaries.single.title, '会話');
    expect(snapshot.revision, 4);
    expect(snapshot.messages.last.content, '回答');
    expect(requests.map((request) => request.url.path), [
      '/base/api/mobile/v1/conversations',
      '/base/api/mobile/v1/conversations/c-1',
    ]);
    expect(
      requests.every(
        (request) => request.headers['authorization'] == 'Bearer test-token',
      ),
      isTrue,
    );
  });

  test('normalizes delta, tool activity, and completion SSE events', () async {
    late http.BaseRequest captured;
    final client = MockClient.streaming((request, _) async {
      captured = request;
      final body = [
        'data: {"type":"content_delta","delta":"こ"}\n\n',
        'data: {"type":"content_delta","delta":"ん"}\n\n',
        'data: {"type":"tool_call_started","tool_call_id":"tool-1","tool_name":"検索","status":"running"}\n\n',
        'data: [DONE]\n\n',
      ].join();
      return http.StreamedResponse(
        Stream.value(utf8.encode(body)),
        200,
        headers: {'content-type': 'text/event-stream'},
      );
    });
    final gateway = HttpMobileChatGateway(
      baseUrl: 'https://tobkiri.example',
      bearerToken: 'test-token',
      httpClient: client,
    );

    final events = await gateway
        .streamMessage(
          conversationId: 'c-1',
          text: '質問',
          clientMessageId: 'client-1',
          expectedRevision: 2,
        )
        .toList();

    expect(captured.url.path, '/api/mobile/v1/conversations/c-1/stream');
    expect(captured.headers['accept'], 'text/event-stream');
    expect(events.whereType<MobileChatDelta>().map((event) => event.content), [
      'こ',
      'こん',
    ]);
    final tool = events.whereType<MobileChatActivity>().single;
    expect(tool.kind, 'tool');
    expect(tool.label, '検索 · running');
    expect(events.last, isA<MobileChatCompleted>());
  });

  test('rejects chat calls without an injected bearer token', () async {
    final gateway = HttpMobileChatGateway(
      baseUrl: 'https://tobkiri.example',
      bearerToken: '',
      httpClient: MockClient((_) async => http.Response('{}', 200)),
    );

    expect(gateway.listConversations, throwsA(isA<MobileChatApiException>()));
  });

  test('stream errors do not expose backend response bodies', () async {
    final client = MockClient.streaming((_, __) async {
      return http.StreamedResponse(
        Stream.value(utf8.encode('internal-secret-detail')),
        500,
      );
    });
    final gateway = HttpMobileChatGateway(
      baseUrl: 'https://tobkiri.example',
      bearerToken: 'test-token',
      httpClient: client,
    );

    final events = await gateway
        .streamMessage(
          conversationId: 'c-1',
          text: '質問',
          clientMessageId: 'client-1',
          expectedRevision: 0,
        )
        .toList();

    final error = events.single as MobileChatFailed;
    expect(error.message, contains('HTTP 500'));
    expect(error.message, isNot(contains('internal-secret-detail')));
  });

  test('transport errors do not expose endpoint details', () async {
    final client = MockClient((_) async {
      throw Exception('private-host.example:19400/internal/path');
    });
    final gateway = HttpMobileChatGateway(
      baseUrl: 'https://tobkiri.example',
      bearerToken: 'test-token',
      httpClient: client,
    );

    final events = await gateway
        .streamMessage(
          conversationId: 'c-1',
          text: '質問',
          clientMessageId: 'client-1',
          expectedRevision: 0,
        )
        .toList();

    final error = events.single as MobileChatFailed;
    expect(error.message, 'Tobkiriとの通信に失敗しました。');
    expect(error.message, isNot(contains('private-host.example')));
  });
}
