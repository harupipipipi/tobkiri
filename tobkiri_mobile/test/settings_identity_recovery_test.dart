import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/data/pc/device_store.dart';
import 'package:rumi_remote_app/src/settings/api_config_store.dart';
import 'package:rumi_remote_app/src/settings/settings_screen.dart';

class _IdentityFaultStorage implements SecureKeyValueStorage {
  final values = <String, String>{};
  bool identityUnavailable = true;

  @override
  Future<String?> read(String key) async {
    if (key == 'rumi.device.identity.v1' && identityUnavailable) {
      throw StateError('secure storage locked');
    }
    return values[key];
  }

  @override
  Future<void> write(String key, String? value) async {
    if (value == null) {
      values.remove(key);
    } else {
      values[key] = value;
    }
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

void main() {
  testWidgets(
    'identity recovery keeps configured PC connection visible and retryable',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(800, 1000));
      final storage = _IdentityFaultStorage();
      final configStore = ApiConfigStore(storage: storage);
      final deviceStore = MobileDeviceStore(storage: storage);
      const connection = PcConnection(
        baseUrl: 'https://desk.example.test',
        token: 'pc-token',
      );
      await configStore.savePc(connection);
      await deviceStore.savePairedDevice(
        const PairedDevice(
          deviceId: 'mobile-1',
          deviceToken: 'pc-token',
          label: 'Phone',
          scopes: ['chat.read'],
          pcBaseUrl: 'https://desk.example.test',
          pcLabel: 'Desk',
          pairingId: 'pairing-1',
        ),
      );

      await tester.pumpWidget(
        MaterialApp(
          home: SettingsScreen(
            configStore: configStore,
            deviceStore: deviceStore,
            onApiChanged: (_) {},
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('端末IDを確認できません'), findsOneWidget);
      expect(find.text('再試行'), findsOneWidget);

      storage.identityUnavailable = false;
      await tester.tap(find.text('再試行'));
      await tester.pumpAndSettle();

      expect(find.text('端末IDを確認できません'), findsNothing);
      expect(find.text('PC: Desk'), findsOneWidget);
    },
  );
}
