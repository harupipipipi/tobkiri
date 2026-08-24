import 'dart:async';

import 'package:flutter/material.dart';

import 'mobile_authority.dart';

typedef AuthorityClientFactory = MobileAuthorityClient Function(
  MobileAuthorityConnection connection,
);

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
  String? _error;
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    unawaited(_refresh());
  }

  @override
  void dispose() {
    _client?.close();
    super.dispose();
  }

  Future<void> _refresh() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final connection = await _store.load();
      if (connection == null) {
        if (!mounted) return;
        setState(() => _requests = const []);
        return;
      }
      _client?.close();
      final client = widget.clientFactory?.call(connection) ??
          MobileAuthorityClient(
            connection: connection,
            signer: SharedMobileIdentitySigner(),
          );
      _client = client;
      final requests = await client.listPending();
      if (!mounted) return;
      setState(() => _requests = requests);
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = _safeError(error));
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _approve(AuthorityRequestItem request) async {
    if (_busy.contains(request.requestId)) return;
    if (request.isHighImpact) {
      final confirmed = await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('Confirm high-impact action'),
          content: Text(
            '${request.permissionId}\n${request.reason}\n'
            'This grants one execution only.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('Confirm and approve'),
            ),
          ],
        ),
      );
      if (confirmed != true || !mounted) return;
    }
    await _settle(request, approve: true);
  }

  Future<void> _deny(AuthorityRequestItem request) async {
    if (_busy.contains(request.requestId)) return;
    var reasonValue = '';
    final reason = await showDialog<String?>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Deny authority request'),
        content: TextField(
          onChanged: (value) => reasonValue = value,
          maxLines: 3,
          keyboardAppearance: Theme.of(context).brightness,
          decoration: const InputDecoration(
            labelText: 'Reason (optional)',
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
  }) async {
    final client = _client;
    if (client == null || _busy.contains(request.requestId)) return;
    setState(() {
      _busy.add(request.requestId);
      _error = null;
    });
    try {
      if (approve) {
        await client.approve(request);
      } else {
        await client.deny(request, reason: reason);
      }
      if (!mounted) return;
      setState(() => _requests = _requests
          .where((item) => item.requestId != request.requestId)
          .toList(growable: false));
    } catch (error) {
      if (mounted) setState(() => _error = _safeError(error));
    } finally {
      if (mounted) setState(() => _busy.remove(request.requestId));
    }
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
                  padding: const EdgeInsets.all(16),
                  children: [
                    if (_error != null)
                      Card(
                        color: Theme.of(context).colorScheme.errorContainer,
                        child: Padding(
                          padding: const EdgeInsets.all(12),
                          child: Text(_error!),
                        ),
                      ),
                    if (_requests.isEmpty)
                      const Padding(
                        padding: EdgeInsets.symmetric(vertical: 48),
                        child: Center(
                          child: Text(
                            'No pending approvals. Pair this device with '
                            'Authority approval scopes to review requests.',
                            textAlign: TextAlign.center,
                          ),
                        ),
                      ),
                    for (final request in _requests)
                      _AuthorityRequestCard(
                        request: request,
                        busy: _busy.contains(request.requestId),
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
    required this.request,
    required this.busy,
    required this.onApprove,
    required this.onDeny,
  });

  final AuthorityRequestItem request;
  final bool busy;
  final VoidCallback onApprove;
  final VoidCallback onDeny;

  @override
  Widget build(BuildContext context) {
    final resource = request.resource.entries
        .where((entry) => !_secretLike(entry.key))
        .take(8)
        .map((entry) => '${entry.key}: ${_bounded(entry.value)}')
        .join('\n');
    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Icon(Icons.shield_outlined),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    request.permissionId,
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ),
                Chip(label: Text(request.riskLevel)),
              ],
            ),
            const SizedBox(height: 8),
            Text(request.reason),
            if (resource.isNotEmpty) ...[
              const SizedBox(height: 8),
              SelectableText(resource),
            ],
            const SizedBox(height: 8),
            Text('Requester: ${request.principalId}'),
            if (request.expiresAt.isNotEmpty)
              Text('Expires: ${request.expiresAt}'),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton.icon(
                    onPressed: busy ? null : onDeny,
                    icon: const Icon(Icons.close),
                    label: const Text('Deny'),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: FilledButton.icon(
                    onPressed: busy ? null : onApprove,
                    icon: const Icon(Icons.check),
                    label: Text(busy ? 'Submitting…' : 'Approve once'),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

bool _secretLike(String key) => RegExp(
      r'(token|secret|password|cookie|credential|private.?key)',
      caseSensitive: false,
    ).hasMatch(key);

String _bounded(Object? value) {
  final text = value.toString().replaceAll(RegExp(r'[\r\n\t]+'), ' ').trim();
  return text.length > 160 ? '${text.substring(0, 157)}...' : text;
}

String _safeError(Object error) {
  final text = error.toString().replaceAll(
        RegExp(r'(Bearer\s+|token[=:]\s*)\S+', caseSensitive: false),
        r'$1[redacted]',
      );
  return text.length > 240 ? '${text.substring(0, 237)}...' : text;
}
