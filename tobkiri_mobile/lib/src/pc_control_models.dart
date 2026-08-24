import 'generated/command_protocol_models.dart' as protocol;

enum PcCommandDisposition { accepted, rejected, pending, approvalRequired }

class PcStateSnapshot {
  const PcStateSnapshot({
    required this.stateRef,
    required this.value,
    required this.revision,
    required this.freshness,
  });

  final String stateRef;
  final Object? value;
  final int revision;
  final String freshness;

  bool get authoritative => freshness == 'authoritative';

  factory PcStateSnapshot.fromJson(Map<String, dynamic> json) {
    return PcStateSnapshot(
      stateRef: asControlString(json['state_ref']),
      value: json['value'],
      revision: asControlInt(json['revision']),
      freshness: asControlString(json['freshness'], fallback: 'unknown'),
    );
  }
}

class PcRuntimeSnapshot {
  const PcRuntimeSnapshot({
    required this.snapshotId,
    required this.snapshotRevision,
    required this.states,
    this.capturedAt,
  });

  final String snapshotId;
  final int snapshotRevision;
  final DateTime? capturedAt;
  final Map<String, PcStateSnapshot> states;

  factory PcRuntimeSnapshot.fromJson(Map<String, dynamic> json) {
    final parsed = <String, PcStateSnapshot>{};
    for (final item in asControlList(json['states'])) {
      final snapshot = PcStateSnapshot.fromJson(asControlMap(item));
      if (snapshot.stateRef.isNotEmpty) {
        parsed[snapshot.stateRef] = snapshot;
      }
    }
    return PcRuntimeSnapshot(
      snapshotId: asControlString(json['snapshot_id']),
      snapshotRevision: asControlInt(json['snapshot_revision']),
      capturedAt: DateTime.tryParse(asControlString(json['captured_at'])),
      states: Map.unmodifiable(parsed),
    );
  }
}

class PcCommandResult {
  const PcCommandResult({
    required this.disposition,
    required this.operationId,
    required this.commandRef,
    required this.stateChanges,
    required this.clientSequence,
    required this.errorCode,
    required this.approvalRequestId,
  });

  final PcCommandDisposition disposition;
  final String operationId;
  final String commandRef;
  final List<PcStateSnapshot> stateChanges;
  final int? clientSequence;
  final String errorCode;
  final String approvalRequestId;

  bool get accepted => disposition == PcCommandDisposition.accepted;

  String get safeFeedback {
    switch (disposition) {
      case PcCommandDisposition.accepted:
        return 'Confirmed by the PC.';
      case PcCommandDisposition.pending:
        return 'The PC is still settling this request.';
      case PcCommandDisposition.approvalRequired:
        return 'Waiting for approval on the PC.';
      case PcCommandDisposition.rejected:
        if (errorCode == 'STATE_REVISION_CONFLICT') {
          return 'The setting changed on the PC. Refresh and try again.';
        }
        if (errorCode == 'COMMAND_UNAVAILABLE') {
          return 'This control is unavailable on the connected PC.';
        }
        if (errorCode == 'INVALID_INPUT') {
          return 'The PC could not apply that value.';
        }
        return 'The PC rejected this change.';
    }
  }

  factory PcCommandResult.fromJson(Map<String, dynamic> json) {
    final generated = protocol.CommandInvocationResult.fromJson(
      json.map((key, value) => MapEntry(key, value as Object?)),
    );
    final disposition = switch (generated.status) {
      'succeeded' => PcCommandDisposition.accepted,
      'approval_required' => PcCommandDisposition.approvalRequired,
      'pending' || 'queued' => PcCommandDisposition.pending,
      _ => PcCommandDisposition.rejected,
    };
    final approval = asControlMap(json['approval']);
    return PcCommandResult(
      disposition: disposition,
      operationId: generated.operationId,
      commandRef: generated.commandRef,
      stateChanges: generated.stateChanges
          .map(
            (item) => PcStateSnapshot.fromJson(
              item.map((key, value) => MapEntry(key, value)),
            ),
          )
          .where((item) => item.stateRef.isNotEmpty)
          .toList(growable: false),
      clientSequence: json['client_sequence'] is int
          ? json['client_sequence'] as int
          : null,
      errorCode: generated.error?.code ?? '',
      approvalRequestId: asControlString(approval['request_id']),
    );
  }
}

Map<String, dynamic> asControlMap(Object? value) {
  if (value is Map<String, dynamic>) {
    return value;
  }
  if (value is Map) {
    return value.map((key, item) => MapEntry('$key', item));
  }
  return const {};
}

List<Object?> asControlList(Object? value) {
  return value is List ? value : const [];
}

String asControlString(Object? value, {String fallback = ''}) {
  if (value == null) {
    return fallback;
  }
  final text = '$value'.trim();
  return text.isEmpty ? fallback : text;
}

int asControlInt(Object? value, {int fallback = 0}) {
  if (value is int && value >= 0) {
    return value;
  }
  return int.tryParse('$value') ?? fallback;
}
