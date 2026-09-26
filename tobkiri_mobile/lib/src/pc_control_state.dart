import 'dart:async';

import 'package:flutter/foundation.dart';

import 'pc_control_models.dart';

const pcPreferredModelStateRef = 'defaultspack:models.preferred_model';
const pcThinkingLevelStateRef = 'defaultspack:models.thinking_level';
const pcDeepthinkStateRef = 'defaultspack:models.deepthink_enabled';

enum PcControlPhase { unknown, current, pending, failed }

class PcControlDefinition {
  const PcControlDefinition({
    required this.id,
    required this.label,
    required this.stateRef,
    required this.commandRef,
    required this.argumentName,
    this.extraArguments = const {},
  });

  final String id;
  final String label;
  final String stateRef;
  final String commandRef;
  final String argumentName;
  final Map<String, Object?> extraArguments;

  Map<String, Object?> arguments(Object? value) => {
        argumentName: value,
        ...extraArguments,
      };
}

const pcControlDefinitions = <PcControlDefinition>[
  PcControlDefinition(
    id: 'model',
    label: 'Model',
    stateRef: pcPreferredModelStateRef,
    commandRef: 'defaultspack:model',
    argumentName: 'query',
  ),
  PcControlDefinition(
    id: 'thinking',
    label: 'Thinking level',
    stateRef: pcThinkingLevelStateRef,
    commandRef: 'defaultspack:think',
    argumentName: 'level',
    extraArguments: {'scope': 'global'},
  ),
  PcControlDefinition(
    id: 'deepthink',
    label: 'DeepThink',
    stateRef: pcDeepthinkStateRef,
    commandRef: 'defaultspack:deepthink',
    argumentName: 'enabled',
  ),
];

class PcControlRequest {
  const PcControlRequest({
    required this.definition,
    required this.value,
    required this.invocationId,
    required this.clientSequence,
    required this.expectedRevision,
    required this.idempotencyKey,
  });

  final PcControlDefinition definition;
  final Object? value;
  final String invocationId;
  final int clientSequence;
  final int expectedRevision;
  final String idempotencyKey;
}

typedef PcCommandInvoker = Future<PcCommandResult> Function(
    PcControlRequest request);
typedef PcSnapshotLoader = Future<PcRuntimeSnapshot> Function(
    Set<String> stateRefs);

@immutable
class PcControlState {
  const PcControlState({
    required this.definition,
    this.phase = PcControlPhase.unknown,
    this.confirmedValue,
    this.confirmedRevision = 0,
    this.requestedValue,
    this.requestId,
    this.clientSequence = 0,
    this.feedback = 'Connect and refresh to read the PC value.',
    this.retryable = false,
    this.approvalRequired = false,
    this.outcomeUnknown = false,
  });

  final PcControlDefinition definition;
  final PcControlPhase phase;
  final Object? confirmedValue;
  final int confirmedRevision;
  final Object? requestedValue;
  final String? requestId;
  final int clientSequence;
  final String feedback;
  final bool retryable;
  final bool approvalRequired;
  final bool outcomeUnknown;

  bool get hasConfirmedValue => confirmedValue != null;

  PcControlState copyWith({
    PcControlPhase? phase,
    Object? confirmedValue = _notProvided,
    int? confirmedRevision,
    Object? requestedValue = _notProvided,
    String? requestId = _notProvidedString,
    int? clientSequence,
    String? feedback,
    bool? retryable,
    bool? approvalRequired,
    bool? outcomeUnknown,
  }) {
    return PcControlState(
      definition: definition,
      phase: phase ?? this.phase,
      confirmedValue: identical(confirmedValue, _notProvided)
          ? this.confirmedValue
          : confirmedValue,
      confirmedRevision: confirmedRevision ?? this.confirmedRevision,
      requestedValue: identical(requestedValue, _notProvided)
          ? this.requestedValue
          : requestedValue,
      requestId:
          identical(requestId, _notProvidedString) ? this.requestId : requestId,
      clientSequence: clientSequence ?? this.clientSequence,
      feedback: feedback ?? this.feedback,
      retryable: retryable ?? this.retryable,
      approvalRequired: approvalRequired ?? this.approvalRequired,
      outcomeUnknown: outcomeUnknown ?? this.outcomeUnknown,
    );
  }
}

const _notProvided = Object();
const _notProvidedString = '__pc_control_not_provided__';

class PcControlCoordinator extends ChangeNotifier {
  PcControlCoordinator({
    required PcCommandInvoker invoke,
    required PcSnapshotLoader loadSnapshot,
    List<PcControlDefinition> definitions = pcControlDefinitions,
    String? sessionId,
  })  : _invoke = invoke,
        _loadSnapshot = loadSnapshot,
        _sessionId = sessionId ??
            DateTime.now().microsecondsSinceEpoch.toRadixString(36),
        _states = {
          for (final definition in definitions)
            definition.id: PcControlState(definition: definition),
        };

  final PcCommandInvoker _invoke;
  final PcSnapshotLoader _loadSnapshot;
  final String _sessionId;
  final Map<String, PcControlState> _states;
  int _sequence = 0;
  bool _refreshing = false;
  String _snapshotId = '';

  List<PcControlState> get states => List.unmodifiable(_states.values);
  bool get refreshing => _refreshing;
  String get snapshotId => _snapshotId;

  PcControlState state(String id) => _states[id]!;

  Future<void> refresh() async {
    if (_refreshing) {
      return;
    }
    _refreshing = true;
    notifyListeners();
    try {
      final snapshot = await _loadSnapshot(
        _states.values.map((item) => item.definition.stateRef).toSet(),
      );
      _snapshotId = snapshot.snapshotId;
      for (final entry in _states.entries) {
        final current = entry.value;
        final remote = snapshot.states[current.definition.stateRef];
        if (remote == null || !remote.authoritative) {
          _states[entry.key] = current.copyWith(
            phase: PcControlPhase.unknown,
            feedback: 'The PC did not provide an authoritative value.',
            retryable: current.requestedValue != null,
          );
          continue;
        }
        if (remote.revision < current.confirmedRevision) {
          continue;
        }
        _states[entry.key] = _reconcile(current, remote);
      }
    } catch (_) {
      for (final entry in _states.entries) {
        _states[entry.key] = entry.value.copyWith(
          phase: PcControlPhase.unknown,
          feedback: entry.value.hasConfirmedValue
              ? 'PC offline. Showing the last confirmed value.'
              : 'PC offline. The active value is unknown.',
          retryable: entry.value.requestedValue != null,
        );
      }
    } finally {
      _refreshing = false;
      notifyListeners();
    }
  }

  Future<PcCommandResult?> request(String id, Object? value) async {
    final current = _states[id];
    if (current == null) {
      throw ArgumentError.value(id, 'id', 'unknown PC control');
    }
    final sequence = ++_sequence;
    final invocationId = 'mobile-control-$_sessionId-$id-$sequence';
    final request = PcControlRequest(
      definition: current.definition,
      value: value,
      invocationId: invocationId,
      clientSequence: sequence,
      expectedRevision: current.confirmedRevision,
      idempotencyKey: invocationId,
    );
    _states[id] = current.copyWith(
      phase: PcControlPhase.pending,
      requestedValue: value,
      requestId: invocationId,
      clientSequence: sequence,
      feedback: 'Waiting for the PC to confirm this value.',
      retryable: false,
      approvalRequired: false,
      outcomeUnknown: false,
    );
    notifyListeners();

    try {
      final result = await _invoke(request);
      final latest = _states[id]!;
      if (latest.clientSequence != sequence ||
          (result.clientSequence != null &&
              result.clientSequence != sequence)) {
        return result;
      }
      _states[id] = _settle(latest, request, result);
      notifyListeners();
      return result;
    } on TimeoutException {
      final latest = _states[id]!;
      if (latest.clientSequence == sequence) {
        _states[id] = latest.copyWith(
          phase: PcControlPhase.unknown,
          feedback: 'The outcome is unknown. Refresh before retrying.',
          retryable: false,
          outcomeUnknown: true,
        );
        notifyListeners();
      }
      return null;
    } catch (_) {
      final latest = _states[id]!;
      if (latest.clientSequence == sequence) {
        _states[id] = latest.copyWith(
          phase: PcControlPhase.unknown,
          feedback: 'Connection lost. Refresh before retrying.',
          retryable: false,
          outcomeUnknown: true,
        );
        notifyListeners();
      }
      return null;
    }
  }

  Future<PcCommandResult?> retry(String id) async {
    final current = _states[id];
    if (current == null || !current.retryable) {
      return null;
    }
    return request(id, current.requestedValue);
  }

  PcControlState _settle(
    PcControlState current,
    PcControlRequest request,
    PcCommandResult result,
  ) {
    switch (result.disposition) {
      case PcCommandDisposition.rejected:
        return current.copyWith(
          phase: PcControlPhase.failed,
          feedback: result.safeFeedback,
          retryable: true,
        );
      case PcCommandDisposition.approvalRequired:
        return current.copyWith(
          phase: PcControlPhase.pending,
          requestId: result.approvalRequestId.isEmpty
              ? current.requestId
              : result.approvalRequestId,
          feedback: result.safeFeedback,
          approvalRequired: true,
          retryable: false,
        );
      case PcCommandDisposition.pending:
        return current.copyWith(
          phase: PcControlPhase.pending,
          feedback: result.safeFeedback,
          retryable: false,
        );
      case PcCommandDisposition.accepted:
        final matching = result.stateChanges
            .where((item) => item.stateRef == request.definition.stateRef)
            .cast<PcStateSnapshot?>()
            .firstOrNull;
        if (matching == null || !matching.authoritative) {
          return current.copyWith(
            phase: PcControlPhase.pending,
            feedback: 'Accepted; waiting for an authoritative PC snapshot.',
            retryable: false,
          );
        }
        if (matching.revision < current.confirmedRevision) {
          return current;
        }
        if (matching.value != request.value) {
          return current.copyWith(
            phase: PcControlPhase.failed,
            confirmedValue: matching.value,
            confirmedRevision: matching.revision,
            feedback: 'The PC confirmed a different value.',
            retryable: true,
          );
        }
        return current.copyWith(
          phase: PcControlPhase.current,
          confirmedValue: matching.value,
          confirmedRevision: matching.revision,
          requestedValue: null,
          requestId: null,
          feedback: result.safeFeedback,
          retryable: false,
          approvalRequired: false,
          outcomeUnknown: false,
        );
    }
  }

  PcControlState _reconcile(PcControlState current, PcStateSnapshot remote) {
    final changedExternally = current.hasConfirmedValue &&
        current.confirmedValue != remote.value &&
        current.requestedValue == null;
    if (current.approvalRequired && current.requestedValue != remote.value) {
      return current.copyWith(
        confirmedValue: remote.value,
        confirmedRevision: remote.revision,
        feedback: 'Waiting for final approval settlement on the PC.',
      );
    }
    if (current.requestedValue != null &&
        current.requestedValue == remote.value) {
      return current.copyWith(
        phase: PcControlPhase.current,
        confirmedValue: remote.value,
        confirmedRevision: remote.revision,
        requestedValue: null,
        requestId: null,
        feedback: 'Confirmed from the authoritative PC snapshot.',
        retryable: false,
        approvalRequired: false,
        outcomeUnknown: false,
      );
    }
    if (current.outcomeUnknown &&
        remote.revision <= current.confirmedRevision) {
      return current.copyWith(
        confirmedValue: remote.value,
        feedback: 'The request outcome is still unknown.',
      );
    }
    return current.copyWith(
      phase: current.requestedValue == null
          ? PcControlPhase.current
          : PcControlPhase.failed,
      confirmedValue: remote.value,
      confirmedRevision: remote.revision,
      feedback: changedExternally
          ? 'Changed on the PC since the last refresh.'
          : current.requestedValue == null
              ? 'Current remote value.'
              : 'The PC retained a different value.',
      retryable: current.requestedValue != null,
      approvalRequired: false,
      outcomeUnknown: false,
    );
  }
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull {
    final iterator = this.iterator;
    return iterator.moveNext() ? iterator.current : null;
  }
}
