import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';

import 'mobile_authority.dart';

typedef AuthorityClientFactory = MobileAuthorityClient Function(
    MobileAuthorityConnection connection);

class AuthorityApprovalScreen extends StatefulWidget {
  const AuthorityApprovalScreen({
    super.key,
    this.connectionStore,
    this.clientFactory,
  });

  final MobileAuthorityConnectionStore? connectionStore;
  final AuthorityClientFactory? clientFactory;

  @override
  State<AuthorityApprovalScreen> createState() =>
      _AuthorityApprovalScreenState();
}

class _AuthorityApprovalScreenState extends State<AuthorityApprovalScreen> {
  late final MobileAuthorityConnectionStore _store =
      widget.connectionStore ?? MobileAuthorityConnectionStore();
  MobileAuthorityClient? _client;
  List<AuthorityRequestItem> _requests = const [];
  final Set<String> _busy = {};
  final Map<String, String> _requestErrors = {};
  String _deviceId = '';
  String? _error;
  bool _loading = true;
  bool _paired = false;
  int _invalidItemCount = 0;
  String _connectionKey = '';
  Timer? _expiryTimer;

  @override
  void initState() {
    super.initState();
    unawaited(_refresh());
  }

  @override
  void dispose() {
    _expiryTimer?.cancel();
    _client?.close();
    super.dispose();
  }

  Future<void> _refresh() async {
    setState(() {
      _loading = true;
      _error = null;
      _invalidItemCount = 0;
    });
    try {
      final connection = await _store.load();
      if (connection == null) {
        if (!mounted) return;
        setState(() {
          _paired = false;
          _deviceId = '';
          _connectionKey = '';
          _requests = const [];
        });
        return;
      }
      final connectionKey = '${connection.baseUrl}\u0000${connection.deviceId}';
      if (_connectionKey.isNotEmpty && _connectionKey != connectionKey) {
        setState(() {
          _requests = const [];
          _requestErrors.clear();
        });
      }
      _client?.close();
      final client = widget.clientFactory?.call(connection) ??
          MobileAuthorityClient(
            connection: connection,
            signer: SharedMobileIdentitySigner(),
          );
      _client = client;
      final result = await client.listRequestsWithDiagnostics();
      if (!mounted) return;
      setState(() {
        _paired = true;
        _deviceId = connection.deviceId;
        _connectionKey = connectionKey;
        _invalidItemCount = result.invalidItemCount;
        _requests = result.requests;
      });
      _scheduleExpiryRefresh();
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = _safeError(error));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  void _scheduleExpiryRefresh() {
    _expiryTimer?.cancel();
    final now = DateTime.now().toUtc();
    final expiries = _requests
        .where((request) => request.isPending)
        .map((request) => DateTime.tryParse(request.expiresAt))
        .whereType<DateTime>()
        .where((expiry) => expiry.isAfter(now))
        .toList()
      ..sort();
    if (expiries.isEmpty) return;
    _expiryTimer = Timer(expiries.first.difference(now), () {
      if (!mounted) return;
      setState(() {});
      _scheduleExpiryRefresh();
    });
  }

  Future<void> _approve(AuthorityRequestItem request) async {
    if (_busy.contains(request.requestId) || !request.isPending) return;
    final confirmation = await _confirmApproval(request);
    if (confirmation == null || !mounted) return;
    await _settle(request, approve: true, confirmationText: confirmation);
  }

  Future<String?> _confirmApproval(AuthorityRequestItem request) async {
    if (!request.isHighImpact && !request.typedConfirmationRequired) return '';
    if (request.typedConfirmationRequired &&
        request.confirmationPhrase.isEmpty) {
      setState(() {
        _requestErrors[request.requestId] =
            'This request is missing its required confirmation phrase. Refresh it before deciding.';
      });
      return null;
    }

    var typedValue = '';
    var reviewed = false;
    final result = await showDialog<String?>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) {
          final typed = request.typedConfirmationRequired;
          final canConfirm =
              typed ? typedValue == request.confirmationPhrase : reviewed;
          return AlertDialog(
            title: Text(
              typed
                  ? 'Type to confirm this critical action'
                  : 'Review this high-impact action',
            ),
            content: SingleChildScrollView(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _DialogFact(
                    label: 'What will happen',
                    value: _visible(request.consequence),
                  ),
                  _DialogFact(
                    label: 'Exact target',
                    value: _visible(request.target),
                  ),
                  _DialogFact(
                    label: 'Risk',
                    value: _visible(request.riskExplanation),
                  ),
                  const SizedBox(height: 12),
                  if (typed) ...[
                    Text('Type exactly: ${request.confirmationPhrase}'),
                    const SizedBox(height: 8),
                    TextField(
                      key: const Key('typed-confirmation-field'),
                      autofocus: true,
                      autocorrect: false,
                      enableSuggestions: false,
                      onChanged: (value) =>
                          setDialogState(() => typedValue = value),
                      decoration: const InputDecoration(
                        labelText: 'Confirmation phrase',
                        helperText: 'Approval stays disabled until it matches.',
                      ),
                    ),
                  ] else
                    CheckboxListTile(
                      contentPadding: EdgeInsets.zero,
                      value: reviewed,
                      onChanged: (value) =>
                          setDialogState(() => reviewed = value ?? false),
                      title: const Text(
                        'I reviewed the consequence and exact target.',
                      ),
                      controlAffinity: ListTileControlAffinity.leading,
                    ),
                ],
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: canConfirm
                    ? () => Navigator.pop(context, typedValue)
                    : null,
                child: const Text('Confirm and approve'),
              ),
            ],
          );
        },
      ),
    );
    return result;
  }

  Future<void> _deny(AuthorityRequestItem request) async {
    if (_busy.contains(request.requestId) || !request.isPending) return;
    var reasonValue = '';
    final reason = await showDialog<String?>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Deny authority request'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(_visible(request.consequence)),
              const SizedBox(height: 12),
              TextField(
                key: const Key('denial-reason-field'),
                onChanged: (value) => reasonValue = value,
                maxLines: 3,
                maxLength: 500,
                decoration: const InputDecoration(
                  labelText: 'Reason (optional)',
                  helperText:
                      'This helps the requester understand your decision.',
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, reasonValue.trim()),
            child: const Text('Deny'),
          ),
        ],
      ),
    );
    if (reason == null || !mounted) return;
    await _settle(request, approve: false, reason: reason);
  }

  Future<void> _settle(
    AuthorityRequestItem request, {
    required bool approve,
    String reason = '',
    String confirmationText = '',
  }) async {
    final client = _client;
    if (client == null || _busy.contains(request.requestId)) return;
    setState(() {
      _busy.add(request.requestId);
      _requestErrors.remove(request.requestId);
    });
    try {
      final authoritative = approve
          ? await client.approve(
              request,
              confirmationText: confirmationText,
            )
          : await client.deny(request, reason: reason);
      if (!mounted) return;
      _replaceRequest(
        authoritative.copyWith(
          settlementReason: approve ? '' : reason,
          settledAt: authoritative.settledAt.isEmpty
              ? DateTime.now().toUtc().toIso8601String()
              : authoritative.settledAt,
        ),
      );
    } on AuthorityClientException catch (error) {
      if (!mounted) return;
      final reconciled = await _reconcileAfterFailure(client, request, error);
      if (!mounted || reconciled) return;
      final status = switch (error.kind) {
        AuthorityFailureKind.expired => 'expired',
        AuthorityFailureKind.alreadySettled
            when error.settledStatus.isNotEmpty =>
          error.settledStatus,
        AuthorityFailureKind.alreadySettled ||
        AuthorityFailureKind.stale =>
          'stale',
        _ => request.status,
      };
      _replaceRequest(request.copyWith(status: status));
      setState(() => _requestErrors[request.requestId] = error.userMessage);
    } catch (error) {
      if (mounted) {
        setState(() => _requestErrors[request.requestId] = _safeError(error));
      }
    } finally {
      if (mounted) setState(() => _busy.remove(request.requestId));
    }
  }

  Future<bool> _reconcileAfterFailure(
    MobileAuthorityClient client,
    AuthorityRequestItem request,
    AuthorityClientException error,
  ) async {
    if (!error.retryable &&
        error.kind != AuthorityFailureKind.malformedResponse) {
      return false;
    }
    try {
      final current = await client.getRequest(request.requestId);
      if (!current.isPending || current.isExpired) {
        _replaceRequest(
          current.copyWith(
            status: current.isExpired ? 'expired' : current.status,
            settledAt: DateTime.now().toUtc().toIso8601String(),
          ),
        );
        return true;
      }
    } catch (_) {
      // Keep the original safe failure. Never infer success from reconciliation.
    }
    return false;
  }

  void _replaceRequest(AuthorityRequestItem replacement) {
    if (!mounted) return;
    setState(() {
      _requests = _requests
          .map(
            (item) =>
                item.requestId == replacement.requestId ? replacement : item,
          )
          .toList(growable: false);
    });
    _scheduleExpiryRefresh();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Authority approvals'),
        actions: [
          IconButton(
            tooltip: 'Refresh approvals',
            onPressed: _loading ? null : _refresh,
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: SafeArea(
        child: _loading
            ? const Center(child: CircularProgressIndicator())
            : RefreshIndicator(
                onRefresh: _refresh,
                child: ListView(
                  key: const Key('authority-approval-list'),
                  padding: const EdgeInsets.all(16),
                  children: [
                    if (_error != null)
                      _StatusNotice(
                        message: _error!,
                        isError: true,
                        actionLabel: 'Retry',
                        onAction: _refresh,
                      ),
                    if (_invalidItemCount > 0)
                      _StatusNotice(
                        message:
                            '$_invalidItemCount incomplete request${_invalidItemCount == 1 ? '' : 's'} could not be shown. Valid requests remain available.',
                        isError: true,
                        actionLabel: 'Refresh',
                        onAction: _refresh,
                      ),
                    if (_requests.isEmpty)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 48),
                        child: Center(
                          child: Text(
                            _paired
                                ? 'No pending approvals.'
                                : 'Pair this device with Authority approval scopes to review requests.',
                            textAlign: TextAlign.center,
                          ),
                        ),
                      ),
                    for (final request in _requests)
                      _AuthorityRequestCard(
                        key: ValueKey(request.requestId),
                        request: request,
                        approvingDeviceId: _deviceId,
                        busy: _busy.contains(request.requestId),
                        error: _requestErrors[request.requestId],
                        onRetry: _refresh,
                        onApprove: () => _approve(request),
                        onDeny: () => _deny(request),
                      ),
                  ],
                ),
              ),
      ),
    );
  }
}

class _AuthorityRequestCard extends StatelessWidget {
  const _AuthorityRequestCard({
    super.key,
    required this.request,
    required this.approvingDeviceId,
    required this.busy,
    required this.error,
    required this.onRetry,
    required this.onApprove,
    required this.onDeny,
  });

  final AuthorityRequestItem request;
  final String approvingDeviceId;
  final bool busy;
  final String? error;
  final VoidCallback onRetry;
  final VoidCallback onApprove;
  final VoidCallback onDeny;

  @override
  Widget build(BuildContext context) {
    final effectiveStatus = request.isExpired ? 'expired' : request.status;
    final pending = effectiveStatus == 'pending';
    final statusColor = _statusColor(context, effectiveStatus);
    return Semantics(
      container: true,
      explicitChildNodes: true,
      label: 'Authority approval request: ${_visible(request.title)}. '
          'Risk ${request.riskLevel}. Consequence: '
          '${_visible(request.consequence)}. Target: '
          '${_visible(request.target)}.',
      child: Card(
        margin: const EdgeInsets.only(bottom: 16),
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  ExcludeSemantics(
                    child: Icon(
                      request.isHighImpact
                          ? Icons.warning_amber_rounded
                          : Icons.shield_outlined,
                      color: request.isHighImpact
                          ? Theme.of(context).colorScheme.error
                          : null,
                    ),
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Semantics(
                      header: true,
                      child: Text(
                        _visible(request.title),
                        style: Theme.of(context).textTheme.titleMedium,
                      ),
                    ),
                  ),
                  const SizedBox(width: 8),
                  Chip(
                    avatar: effectiveStatus == 'pending'
                        ? null
                        : Icon(_statusIcon(effectiveStatus), size: 16),
                    label: Text(
                      effectiveStatus == 'pending'
                          ? request.riskLevel.toUpperCase()
                          : effectiveStatus.toUpperCase(),
                    ),
                    side: BorderSide(color: statusColor),
                  ),
                ],
              ),
              const SizedBox(height: 12),
              _ApprovalFact(
                label: 'What will happen',
                value: _visible(request.consequence),
                emphasize: true,
              ),
              _ApprovalFact(
                label: 'Exact target',
                value: _visible(request.target),
              ),
              _ApprovalFact(
                label: 'Affected data or resource',
                value: _visible(request.affectedData),
              ),
              _ApprovalFact(
                label: 'Why it is requested',
                value: _visible(request.reason),
              ),
              _ApprovalFact(
                label: 'Risk',
                value: _visible(request.riskExplanation),
              ),
              _ApprovalFact(label: 'Scope', value: request.scopeLabel),
              _ApprovalFact(
                label: 'Persistence',
                value: request.persistenceLabel,
              ),
              _ApprovalFact(
                label: 'Requester',
                value: request.principalId.isEmpty
                    ? 'Unknown requester'
                    : _visible(request.principalId),
              ),
              if (request.profileId.isNotEmpty)
                _ApprovalFact(
                  label: 'Profile',
                  value: _visible(request.profileId),
                ),
              if (approvingDeviceId.isNotEmpty)
                _ApprovalFact(
                  label: 'Reviewing device',
                  value: _visible(approvingDeviceId),
                ),
              if (request.expiresAt.isNotEmpty)
                _ApprovalFact(
                  label: 'Expires',
                  value: _visible(request.expiresAt),
                ),
              _ApprovalFact(
                label: 'Audit',
                value: _visible(request.auditText),
              ),
              const SizedBox(height: 4),
              ExpansionTile(
                key: ValueKey('technical-${request.requestId}'),
                tilePadding: EdgeInsets.zero,
                childrenPadding: const EdgeInsets.only(bottom: 12),
                title: const Text('Technical details'),
                subtitle: const Text('Raw IDs and redacted resource payload'),
                children: [
                  Align(
                    alignment: AlignmentDirectional.centerStart,
                    child: SelectableText(
                      _technicalDetails(request),
                      key: ValueKey('technical-text-${request.requestId}'),
                      style: Theme.of(
                        context,
                      ).textTheme.bodySmall?.copyWith(fontFamily: 'monospace'),
                    ),
                  ),
                ],
              ),
              if (effectiveStatus != 'pending')
                Semantics(
                  liveRegion: true,
                  child: _StatusNotice(
                    message: _settledMessage(request, effectiveStatus),
                    isError: effectiveStatus == 'expired' ||
                        effectiveStatus == 'stale',
                  ),
                ),
              if (error != null)
                Semantics(
                  liveRegion: true,
                  child: _StatusNotice(
                    message: error!,
                    isError: true,
                    actionLabel: 'Refresh request status',
                    onAction: onRetry,
                  ),
                ),
              if (pending) ...[
                const SizedBox(height: 8),
                OutlinedButton.icon(
                  onPressed: busy || !request.hasRequiredDecisionContext
                      ? null
                      : onDeny,
                  icon: const Icon(Icons.close),
                  label: const Text('Deny with optional reason'),
                ),
                const SizedBox(height: 8),
                FilledButton.icon(
                  onPressed: busy || !request.hasRequiredDecisionContext
                      ? null
                      : onApprove,
                  icon: busy
                      ? const SizedBox.square(
                          dimension: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.check),
                  label: Text(
                    busy ? 'Submitting decision…' : 'Review and approve once',
                  ),
                ),
                if (!request.hasRequiredDecisionContext)
                  const Padding(
                    padding: EdgeInsets.only(top: 8),
                    child: Text(
                      'Decision unavailable because the PC did not provide all required consequence, target, risk, scope, Profile, resource, and expiry details. Refresh before deciding.',
                    ),
                  ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _ApprovalFact extends StatelessWidget {
  const _ApprovalFact({
    required this.label,
    required this.value,
    this.emphasize = false,
  });

  final String label;
  final String value;
  final bool emphasize;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              label,
              style: Theme.of(context).textTheme.labelMedium?.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
            ),
            const SizedBox(height: 2),
            Text(
              value.isEmpty ? 'Not provided' : value,
              style: emphasize
                  ? Theme.of(
                      context,
                    ).textTheme.bodyLarge?.copyWith(fontWeight: FontWeight.w600)
                  : null,
            ),
          ],
        ),
      );
}

class _DialogFact extends StatelessWidget {
  const _DialogFact({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Text('$label\n$value'),
      );
}

class _StatusNotice extends StatelessWidget {
  const _StatusNotice({
    required this.message,
    required this.isError,
    this.actionLabel,
    this.onAction,
  });

  final String message;
  final bool isError;
  final String? actionLabel;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) => Card(
        color: isError
            ? Theme.of(context).colorScheme.errorContainer
            : Theme.of(context).colorScheme.secondaryContainer,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(message),
              if (actionLabel != null && onAction != null) ...[
                const SizedBox(height: 8),
                OutlinedButton(onPressed: onAction, child: Text(actionLabel!)),
              ],
            ],
          ),
        ),
      );
}

String _technicalDetails(AuthorityRequestItem request) {
  final details = <String, Object?>{
    'request_id': request.requestId,
    'permission_id': request.permissionId,
    'principal_id': request.principalId,
    if (request.profileId.isNotEmpty) 'profile_id': request.profileId,
    if (request.conversationId.isNotEmpty)
      'conversation_id': request.conversationId,
    if (request.nodeId.isNotEmpty) 'node_id': request.nodeId,
    if (request.graphId.isNotEmpty) 'graph_id': request.graphId,
    'risk_level': request.riskLevel,
    'allowed_scopes': request.allowedScopes,
    'resource': request.resource,
  };
  return const JsonEncoder.withIndent('  ').convert(_redact(details));
}

Object? _redact(Object? value, {String key = ''}) {
  if (_secretLike(key)) return '[redacted]';
  if (value is Map) {
    return {
      for (final entry in value.entries)
        entry.key.toString(): _redact(entry.value, key: entry.key.toString()),
    };
  }
  if (value is Iterable) {
    return value.map((item) => _redact(item)).toList(growable: false);
  }
  if (value is String) return _redactString(value);
  return value;
}

bool _secretLike(String key) => RegExp(
      r'(token|secret|password|cookie|credential|private.?key|api.?key|access.?token|authorization)',
      caseSensitive: false,
    ).hasMatch(key);

String _redactString(String value) => value
    .replaceAll(
      RegExp("Bearer\\s+[^\\s\\\"']+", caseSensitive: false),
      'Bearer [redacted]',
    )
    .replaceAll(
      RegExp(
        r'([?&](?:token|secret|password|key|api[_-]?key|access[_-]?token|credential|authorization)=)[^&#\s]+',
        caseSensitive: false,
      ),
      r'$1[redacted]',
    )
    .replaceAll(
      RegExp(r'(https?://)[^/@\s]+@', caseSensitive: false),
      r'$1[redacted]@',
    )
    .replaceAll(
      RegExp(
        r'\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*[:=]\s*[^\r\n,;]+',
        caseSensitive: false,
      ),
      '[credential redacted]',
    )
    .replaceAll(
      RegExp(
        r'\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|secret|password|passwd|credential)\b[\s\"\x27]*[:=][\s\"\x27]*[^\s,;\"\x27}\]]+',
        caseSensitive: false,
      ),
      '[credential redacted]',
    )
    .replaceAll(
      RegExp(
        r'\b(?:sk|rk|pk|ghp|github_pat|xox[baprs]|ya29)[-_][A-Za-z0-9_-]{8,}\b',
        caseSensitive: false,
      ),
      '[credential redacted]',
    );

String _visible(String value) => _redactString(value);

String _safeError(Object error) {
  if (error is AuthorityClientException) return error.userMessage;
  return 'Authority approvals could not be loaded. Check the connection and retry.';
}

Color _statusColor(BuildContext context, String status) {
  final colors = Theme.of(context).colorScheme;
  return switch (status) {
    'approved' => colors.primary,
    'denied' => colors.error,
    'expired' || 'stale' => colors.outline,
    _ => colors.secondary,
  };
}

IconData _statusIcon(String status) => switch (status) {
      'approved' => Icons.check_circle_outline,
      'denied' => Icons.cancel_outlined,
      'expired' => Icons.timer_off_outlined,
      _ => Icons.sync_problem_outlined,
    };

String _settledMessage(AuthorityRequestItem request, String status) {
  final when = request.settledAt.isEmpty ? '' : ' at ${request.settledAt}';
  return switch (status) {
    'approved' =>
      'Approved once$when. The signed decision is recorded locally.',
    'denied' => request.settlementReason.isEmpty
        ? 'Denied$when. The decision is recorded locally.'
        : 'Denied$when. Reason: ${_visible(request.settlementReason)}',
    'expired' => 'Expired. This request can no longer be approved or denied.',
    _ =>
      'Stale or already settled elsewhere. Refresh to confirm its latest status.',
  };
}
