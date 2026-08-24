import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/pc_control_models.dart';
import 'package:rumi_remote_app/src/pc_control_state.dart';
import 'package:rumi_remote_app/src/pc_runtime_controls.dart';

void main() {
  testWidgets(
    'renders current unknown and request-scoped states semantically',
    (tester) async {
      final coordinator = _coordinator(
        invoke: (request) async => _accepted(request, request.value),
      );
      await coordinator.refresh();
      final semantics = tester.ensureSemantics();

      await tester.pumpWidget(_app(coordinator));
      await tester.pumpAndSettle();

      expect(find.text('PC runtime controls'), findsOneWidget);
      expect(find.text('Current'), findsNWidgets(3));
      expect(
        find.bySemanticsLabel(
          RegExp(r'Model, Current, current provider/model-a'),
        ),
        findsOneWidget,
      );
      await tester.scrollUntilVisible(find.text('Tool authority'), 200);
      await tester.pumpAndSettle();
      expect(find.text('Unknown'), findsNWidgets(2));
      expect(
        find.bySemanticsLabel(
          'Tool authority, Unknown, no authoritative remote value',
        ),
        findsOneWidget,
      );
      semantics.dispose();
    },
  );

  testWidgets('failed request stays inline with requested value and Retry', (
    tester,
  ) async {
    var attempts = 0;
    final coordinator = _coordinator(
      invoke: (request) async {
        attempts += 1;
        if (attempts == 1) {
          return PcCommandResult(
            disposition: PcCommandDisposition.rejected,
            operationId: request.invocationId,
            commandRef: request.definition.commandRef,
            stateChanges: const [],
            clientSequence: request.clientSequence,
            errorCode: 'STATE_REVISION_CONFLICT',
            approvalRequestId: '',
          );
        }
        return _accepted(request, request.value);
      },
    );
    await coordinator.refresh();
    await tester.pumpWidget(_app(coordinator));

    await tester.tap(find.text('high'));
    await tester.pumpAndSettle();

    expect(find.text('Failed'), findsOneWidget);
    expect(find.text('Requested: high'), findsOneWidget);
    expect(
      find.text('The setting changed on the PC. Refresh and try again.'),
      findsOneWidget,
    );
    expect(find.byKey(const ValueKey('thinking-retry')), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('thinking-retry')));
    await tester.pumpAndSettle();
    expect(find.text('Current: high'), findsOneWidget);
    expect(find.byKey(const ValueKey('thinking-retry')), findsNothing);
  });

  testWidgets('approval request remains visibly pending', (tester) async {
    final coordinator = _coordinator(
      invoke: (request) async => PcCommandResult(
        disposition: PcCommandDisposition.approvalRequired,
        operationId: request.invocationId,
        commandRef: request.definition.commandRef,
        stateChanges: const [],
        clientSequence: request.clientSequence,
        errorCode: '',
        approvalRequestId: 'approval-1',
      ),
    );
    await coordinator.refresh();
    await tester.pumpWidget(_app(coordinator));

    await tester.tap(find.byType(Switch));
    await tester.pumpAndSettle();

    expect(find.text('Pending'), findsOneWidget);
    expect(find.text('Requested: On'), findsOneWidget);
    expect(
      find.text('Approval required · pending final settlement'),
      findsOneWidget,
    );
    expect(find.text('Current: Off'), findsOneWidget);
  });

  testWidgets('model request dialog supports keyboard submission', (
    tester,
  ) async {
    final coordinator = _coordinator(
      invoke: (request) async => _accepted(request, request.value),
    );
    await coordinator.refresh();
    await tester.pumpWidget(_app(coordinator));

    await tester.tap(find.text('Request model'));
    await tester.pumpAndSettle();
    final field = find.byType(TextFormField);
    expect(field, findsOneWidget);
    await tester.enterText(field, 'provider/model-keyboard');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pumpAndSettle();

    expect(find.text('Current: provider/model-keyboard'), findsOneWidget);
  });
}

Widget _app(PcControlCoordinator coordinator) {
  return MaterialApp(
    home: Scaffold(
      body: SizedBox(
        width: 600,
        height: 1200,
        child: PcRuntimeControlsPanel(coordinator: coordinator),
      ),
    ),
  );
}

PcControlCoordinator _coordinator({required PcCommandInvoker invoke}) {
  return PcControlCoordinator(
    invoke: invoke,
    loadSnapshot: (_) async => PcRuntimeSnapshot(
      snapshotId: 'snapshot-1',
      snapshotRevision: 1,
      states: {
        pcPreferredModelStateRef: _state(
          pcPreferredModelStateRef,
          'provider/model-a',
        ),
        pcThinkingLevelStateRef: _state(pcThinkingLevelStateRef, 'medium'),
        pcDeepthinkStateRef: _state(pcDeepthinkStateRef, false),
      },
    ),
  );
}

PcStateSnapshot _state(String ref, Object? value, {int revision = 1}) {
  return PcStateSnapshot(
    stateRef: ref,
    value: value,
    revision: revision,
    freshness: 'authoritative',
  );
}

PcCommandResult _accepted(PcControlRequest request, Object? value) {
  return PcCommandResult(
    disposition: PcCommandDisposition.accepted,
    operationId: request.invocationId,
    commandRef: request.definition.commandRef,
    stateChanges: [_state(request.definition.stateRef, value, revision: 2)],
    clientSequence: request.clientSequence,
    errorCode: '',
    approvalRequestId: '',
  );
}
