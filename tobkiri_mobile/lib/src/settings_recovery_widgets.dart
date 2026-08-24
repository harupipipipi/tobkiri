part of 'rumi_remote_home.dart';

class _SettingsLoadErrorView extends StatelessWidget {
  const _SettingsLoadErrorView({
    required this.failures,
    required this.pairedDevice,
    required this.deviceIdentity,
    required this.onRetry,
    required this.onReset,
    required this.onOpenAuthority,
  });

  final List<SettingsLoadFailure> failures;
  final PairedDeviceSummary? pairedDevice;
  final DeviceIdentitySummary? deviceIdentity;
  final Future<void> Function() onRetry;
  final Future<bool> Function() onReset;
  final VoidCallback onOpenAuthority;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 560),
          child: Semantics(
            key: const Key('settings-load-error'),
            label: 'Settings load error',
            container: true,
            liveRegion: true,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Icon(
                  Icons.settings_backup_restore,
                  size: 48,
                  color: Theme.of(context).colorScheme.error,
                ),
                const SizedBox(height: 16),
                Text(
                  'Settings could not be loaded',
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
                const SizedBox(height: 8),
                const Text(
                  'Stored values have not been replaced. Retry after secure '
                  'storage is available, or use the separate confirmed reset.',
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 16),
                _SettingsFailureCard(failures: failures),
                if (pairedDevice != null || deviceIdentity != null) ...[
                  const SizedBox(height: 12),
                  _SettingsReadOnlySummary(
                    pairedDevice: pairedDevice,
                    deviceIdentity: deviceIdentity,
                    pairedDeviceFailed: false,
                    deviceIdentityFailed: false,
                  ),
                ],
                const SizedBox(height: 20),
                OutlinedButton.icon(
                  key: const Key('open-authority-approvals-button'),
                  onPressed: onOpenAuthority,
                  icon: const Icon(Icons.shield_outlined),
                  label: const Text('Authority approvals'),
                ),
                const SizedBox(height: 8),
                FilledButton.icon(
                  key: const Key('retry-settings-button'),
                  onPressed: onRetry,
                  icon: const Icon(Icons.refresh),
                  label: const Text('Retry'),
                ),
                const SizedBox(height: 8),
                TextButton.icon(
                  key: const Key('reset-settings-button'),
                  onPressed: onReset,
                  icon: const Icon(Icons.restart_alt),
                  label: const Text('Reset editable settings'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _SettingsFailureCard extends StatelessWidget {
  const _SettingsFailureCard({
    required this.failures,
    this.onRetry,
    this.onRecover,
  });

  final List<SettingsLoadFailure> failures;
  final VoidCallback? onRetry;
  final VoidCallback? onRecover;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Card(
      color: colors.errorContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              'Load diagnostics',
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(color: colors.onErrorContainer),
            ),
            const SizedBox(height: 8),
            for (final failure in failures)
              Text(
                '${failure.source.label}: ${_diagnosticLabel(failure.code)}',
                style: TextStyle(color: colors.onErrorContainer),
              ),
            if (onRetry != null || onRecover != null) ...[
              const SizedBox(height: 12),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  if (onRetry != null)
                    OutlinedButton.icon(
                      key: const Key('retry-settings-button'),
                      onPressed: onRetry,
                      icon: const Icon(Icons.refresh),
                      label: const Text('Retry'),
                    ),
                  if (onRecover != null)
                    FilledButton(
                      key: const Key('recover-settings-button'),
                      onPressed: onRecover,
                      child: const Text('Use loaded sections'),
                    ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _SettingsReadOnlySummary extends StatelessWidget {
  const _SettingsReadOnlySummary({
    required this.pairedDevice,
    required this.deviceIdentity,
    required this.pairedDeviceFailed,
    required this.deviceIdentityFailed,
  });

  final PairedDeviceSummary? pairedDevice;
  final DeviceIdentitySummary? deviceIdentity;
  final bool pairedDeviceFailed;
  final bool deviceIdentityFailed;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        ListTile(
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.computer),
          title: const Text('Paired device'),
          subtitle: Text(
            pairedDeviceFailed
                ? 'Unavailable; stored pairing was not changed'
                : pairedDevice == null
                    ? 'Not paired'
                    : '${pairedDevice!.deviceId} · '
                        '${_safePairedDeviceOrigin(pairedDevice!.baseUrl)}',
          ),
        ),
        ListTile(
          contentPadding: EdgeInsets.zero,
          leading: const Icon(Icons.fingerprint),
          title: const Text('Device identity'),
          subtitle: Text(
            deviceIdentityFailed
                ? 'Unavailable; stored identity was not changed'
                : deviceIdentity?.deviceId ?? 'Not provisioned',
          ),
        ),
      ],
    );
  }
}

String _diagnosticLabel(String code) {
  return switch (code) {
    'reset-incomplete' => 'confirmed reset was interrupted; retry reset',
    'corrupt-migration' => 'stored migration data is invalid',
    'incompatible-schema' => 'stored data requires a newer app',
    'invalid-paired-device' => 'stored pairing record is invalid',
    'invalid-notifications' => 'stored notification record is invalid',
    'invalid-device-identity' => 'stored identity record is invalid',
    _ => 'secure storage is unavailable',
  };
}

String _safePairedDeviceOrigin(String value) {
  final uri = Uri.tryParse(value);
  if (uri == null || uri.scheme.isEmpty || uri.host.isEmpty) {
    return 'stored PC';
  }
  return Uri(
    scheme: uri.scheme,
    host: uri.host,
    port: uri.hasPort ? uri.port : null,
  ).toString();
}
