import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/pc_control_models.dart';
import 'package:rumi_remote_app/src/pc_control_state.dart';

void main() {
  const definitions = <PcControlDefinition>[
    ...pcControlDefinitions,
    PcControlDefinition(
      id: 'mode',
      label: 'Mode',
      stateRef: 'test:mode',
      commandRef: 'test:mode',
      argumentName: 'mode',
    ),
    PcControlDefinition(
      id: 'tools',
      label: 'Tools',
      stateRef: 'test:tools',
      commandRef: 'test:tools',
      argumentName: 'enabled',
    ),
  ];

  test(
    'model thinking DeepThink mode and tool success commit snapshots',
    () async {
      final remote = <String, PcStateSnapshot>{
        for (final definition in definitions)
          definition.stateRef: _state(
            definition.stateRef,
            _initial(definition.id),
          ),
      };
      final coordinator = PcControlCoordinator(
        definitions: definitions,
        loadSnapshot: (_) async => _snapshot(remote),
        invoke: (request) async {
          final next = _state(
            request.definition.stateRef,
            request.value,
            revision: remote[request.definition.stateRef]!.revision + 1,
          );
          remote[request.definition.stateRef] = next;
          return _result(request, PcCommandDisposition.accepted, state: next);
        },
      );
      await coordinator.refresh();

      final desired = <String, Object?>{
        'model': 'provider/new-model',
        'thinking': 'high',
        'deepthink': true,
        'mode': 'coding',
        'tools': false,
      };
      for (final entry in desired.entries) {
        await coordinator.request(entry.key, entry.value);
        final state = coordinator.state(entry.key);
        expect(state.phase, PcControlPhase.current, reason: entry.key);
        expect(state.confirmedValue, entry.value, reason: entry.key);
        expect(state.requestedValue, isNull, reason: entry.key);
      }
    },
  );

  test('rejection keeps confirmed value and exposes durable retry', () async {
    var attempt = 0;
    final coordinator = PcControlCoordinator(
      loadSnapshot: (_) async => _snapshot({
        for (final definition in pcControlDefinitions)
          definition.stateRef: _state(
            definition.stateRef,
            _initial(definition.id),
          ),
      }),
      invoke: (request) async {
        attempt += 1;
        if (attempt == 1) {
          return _result(
            request,
            PcCommandDisposition.rejected,
            errorCode: 'STATE_REVISION_CONFLICT',
          );
        }
        return _result(
          request,
          PcCommandDisposition.accepted,
          state: _state(
            request.definition.stateRef,
            request.value,
            revision: 2,
          ),
        );
      },
    );
    await coordinator.refresh();

    await coordinator.request('thinking', 'high');

    expect(coordinator.state('thinking').phase, PcControlPhase.failed);
    expect(coordinator.state('thinking').confirmedValue, 'medium');
    expect(coordinator.state('thinking').requestedValue, 'high');
    expect(coordinator.state('thinking').retryable, isTrue);
    expect(coordinator.state('thinking').feedback, contains('Refresh'));

    await coordinator.retry('thinking');
    expect(coordinator.state('thinking').phase, PcControlPhase.current);
    expect(coordinator.state('thinking').confirmedValue, 'high');
  });

  test(
    'timeout after commit stays unknown until authoritative refresh',
    () async {
      var remote = _state(pcDeepthinkStateRef, false);
      final coordinator = PcControlCoordinator(
        loadSnapshot: (_) async => _snapshot({pcDeepthinkStateRef: remote}),
        invoke: (request) async {
          remote = _state(pcDeepthinkStateRef, true, revision: 2);
          throw TimeoutException('response lost after commit');
        },
        definitions: [pcControlDefinitions[2]],
      );
      await coordinator.refresh();

      await coordinator.request('deepthink', true);
      expect(coordinator.state('deepthink').phase, PcControlPhase.unknown);
      expect(coordinator.state('deepthink').confirmedValue, false);
      expect(coordinator.state('deepthink').outcomeUnknown, isTrue);

      await coordinator.refresh();
      expect(coordinator.state('deepthink').phase, PcControlPhase.current);
      expect(coordinator.state('deepthink').confirmedValue, true);
    },
  );

  test('offline command outcome stays unknown until refresh', () async {
    final coordinator = PcControlCoordinator(
      definitions: [pcControlDefinitions[0]],
      loadSnapshot: (_) async => _snapshot({
        pcPreferredModelStateRef: _state(
          pcPreferredModelStateRef,
          'provider/model-a',
        ),
      }),
      invoke: (_) => throw const RumiApiExceptionForTest(),
    );
    await coordinator.refresh();

    await coordinator.request('model', 'provider/model-b');

    final state = coordinator.state('model');
    expect(state.phase, PcControlPhase.unknown);
    expect(state.confirmedValue, 'provider/model-a');
    expect(state.requestedValue, 'provider/model-b');
    expect(state.retryable, isFalse);
    expect(state.outcomeUnknown, isTrue);
  });

  test('app sessions do not reuse invocation or idempotency keys', () async {
    final requests = <PcControlRequest>[];
    PcControlCoordinator coordinator(String sessionId) => PcControlCoordinator(
          sessionId: sessionId,
          definitions: [pcControlDefinitions[2]],
          loadSnapshot: (_) async => _snapshot({
            pcDeepthinkStateRef: _state(pcDeepthinkStateRef, false),
          }),
          invoke: (request) async {
            requests.add(request);
            return _result(request, PcCommandDisposition.rejected);
          },
        );
    final first = coordinator('session-a');
    final second = coordinator('session-b');
    await first.refresh();
    await second.refresh();

    await first.request('deepthink', true);
    await second.request('deepthink', true);

    expect(requests[0].invocationId, isNot(requests[1].invocationId));
    expect(requests[0].idempotencyKey, isNot(requests[1].idempotencyKey));
  });

  test(
    'approval-required request remains pending until final settlement',
    () async {
      var remote = _state(pcDeepthinkStateRef, false);
      final coordinator = PcControlCoordinator(
        loadSnapshot: (_) async => _snapshot({pcDeepthinkStateRef: remote}),
        invoke: (request) async => _result(
          request,
          PcCommandDisposition.approvalRequired,
          approvalRequestId: 'approval-1',
        ),
        definitions: [pcControlDefinitions[2]],
      );
      await coordinator.refresh();
      await coordinator.request('deepthink', true);

      await coordinator.refresh();
      expect(coordinator.state('deepthink').phase, PcControlPhase.pending);
      expect(coordinator.state('deepthink').approvalRequired, isTrue);
      expect(coordinator.state('deepthink').confirmedValue, false);

      remote = _state(pcDeepthinkStateRef, true, revision: 2);
      await coordinator.refresh();
      expect(coordinator.state('deepthink').phase, PcControlPhase.current);
      expect(coordinator.state('deepthink').approvalRequired, isFalse);
    },
  );

  test(
    'offline refresh marks value unknown without losing last confirmation',
    () async {
      var online = true;
      final coordinator = PcControlCoordinator(
        definitions: [pcControlDefinitions[0]],
        loadSnapshot: (_) async {
          if (!online) {
            throw TimeoutException('offline');
          }
          return _snapshot({
            pcPreferredModelStateRef: _state(
              pcPreferredModelStateRef,
              'provider/model-a',
            ),
          });
        },
        invoke: (_) => throw UnimplementedError(),
      );
      await coordinator.refresh();
      online = false;

      await coordinator.refresh();

      expect(coordinator.state('model').phase, PcControlPhase.unknown);
      expect(coordinator.state('model').confirmedValue, 'provider/model-a');
      expect(coordinator.state('model').feedback, contains('last confirmed'));
    },
  );

  test('rapid changes ignore stale out-of-order command responses', () async {
    final first = Completer<PcCommandResult>();
    final second = Completer<PcCommandResult>();
    final requests = <PcControlRequest>[];
    final coordinator = PcControlCoordinator(
      definitions: [pcControlDefinitions[1]],
      loadSnapshot: (_) async => _snapshot({
        pcThinkingLevelStateRef: _state(pcThinkingLevelStateRef, 'medium'),
      }),
      invoke: (request) {
        requests.add(request);
        return requests.length == 1 ? first.future : second.future;
      },
    );
    await coordinator.refresh();

    final firstCall = coordinator.request('thinking', 'high');
    final secondCall = coordinator.request('thinking', 'low');
    second.complete(
      _result(
        requests[1],
        PcCommandDisposition.accepted,
        state: _state(pcThinkingLevelStateRef, 'low', revision: 2),
      ),
    );
    await secondCall;
    first.complete(
      _result(
        requests[0],
        PcCommandDisposition.accepted,
        state: _state(pcThinkingLevelStateRef, 'high', revision: 1),
      ),
    );
    await firstCall;

    expect(coordinator.state('thinking').phase, PcControlPhase.current);
    expect(coordinator.state('thinking').confirmedValue, 'low');
  });

  test('reconnect explains an external remote change', () async {
    var remote = _state(pcPreferredModelStateRef, 'provider/model-a');
    final coordinator = PcControlCoordinator(
      definitions: [pcControlDefinitions[0]],
      loadSnapshot: (_) async => _snapshot({pcPreferredModelStateRef: remote}),
      invoke: (_) => throw UnimplementedError(),
    );
    await coordinator.refresh();
    remote = _state(pcPreferredModelStateRef, 'provider/model-b', revision: 2);

    await coordinator.refresh();

    expect(coordinator.state('model').confirmedValue, 'provider/model-b');
    expect(coordinator.state('model').feedback, contains('Changed on the PC'));
  });
}

class RumiApiExceptionForTest implements Exception {
  const RumiApiExceptionForTest();
}

Object? _initial(String id) => switch (id) {
      'model' => 'provider/model-a',
      'thinking' => 'medium',
      'deepthink' => false,
      'mode' => 'chat',
      'tools' => true,
      _ => null,
    };

PcStateSnapshot _state(String stateRef, Object? value, {int revision = 1}) {
  return PcStateSnapshot(
    stateRef: stateRef,
    value: value,
    revision: revision,
    freshness: 'authoritative',
  );
}

PcRuntimeSnapshot _snapshot(Map<String, PcStateSnapshot> states) {
  return PcRuntimeSnapshot(
    snapshotId: 'snapshot-1',
    snapshotRevision: states.values.fold(0, (max, item) {
      return item.revision > max ? item.revision : max;
    }),
    states: states,
  );
}

PcCommandResult _result(
  PcControlRequest request,
  PcCommandDisposition disposition, {
  PcStateSnapshot? state,
  String errorCode = '',
  String approvalRequestId = '',
}) {
  return PcCommandResult(
    disposition: disposition,
    operationId: request.invocationId,
    commandRef: request.definition.commandRef,
    stateChanges: state == null ? const [] : [state],
    clientSequence: request.clientSequence,
    errorCode: errorCode,
    approvalRequestId: approvalRequestId,
  );
}
