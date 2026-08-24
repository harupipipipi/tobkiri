import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/rumi_remote_home.dart';
import 'package:rumi_remote_app/src/secure_settings_store.dart';

void main() {
  testWidgets('API failure renders retry and preserves pairing and identity', (
    tester,
  ) async {
    final repository = _FakeSettingsRepository([
      _result(
        apiSettings: null,
        pairedDevice: const PairedDeviceSummary(
          deviceId: 'paired-safe',
          baseUrl: 'https://user:never-show@paired.example.test/path',
        ),
        deviceIdentity: const DeviceIdentitySummary(deviceId: 'identity-safe'),
        failures: const [
          SettingsLoadFailure(
            source: SettingsDataSource.apiConfiguration,
            code: 'read-unavailable',
          ),
        ],
      ),
      _result(),
    ]);

    await _pumpHome(tester, repository);

    expect(find.text('Settings could not be loaded'), findsOneWidget);
    expect(find.textContaining('API configuration:'), findsOneWidget);
    expect(find.textContaining('paired-safe'), findsOneWidget);
    expect(find.textContaining('never-show'), findsNothing);
    expect(find.textContaining('https://paired.example.test'), findsOneWidget);
    expect(find.text('identity-safe'), findsOneWidget);
    expect(
      find.byKey(const Key('open-authority-approvals-button')),
      findsOneWidget,
    );
    expect(find.text(RumiRemoteSettings.defaults.baseUrl), findsNothing);
    expect(find.text('Save'), findsNothing);

    final retry = find.byKey(const Key('retry-settings-button'));
    await tester.ensureVisible(retry);
    await tester.tap(retry);
    await tester.pumpAndSettle();

    expect(find.text('Settings could not be loaded'), findsNothing);
    expect(find.text('Tobkiri Remote'), findsOneWidget);
    expect(repository.loadCount, 2);
  });

  testWidgets('paired-device failure gates save until explicit recovery', (
    tester,
  ) async {
    final repository = _FakeSettingsRepository([
      _result(
        apiSettings: const RumiRemoteSettings(
          baseUrl: 'https://kernel.example.test',
          token: 'stored-secret-token',
          autoRefresh: false,
        ),
        deviceIdentity: const DeviceIdentitySummary(
          deviceId: 'identity-preserved',
        ),
        failures: const [
          SettingsLoadFailure(
            source: SettingsDataSource.pairedDevice,
            code: 'read-unavailable',
          ),
        ],
      ),
    ]);

    await _pumpHome(tester, repository);
    await tester.tap(find.byTooltip('Settings'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Paired device:'), findsOneWidget);
    expect(find.text('identity-preserved'), findsOneWidget);
    final tokenField = tester.widget<EditableText>(
      find.byType(EditableText).last,
    );
    expect(tokenField.obscureText, isTrue);
    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('save-settings-button')))
          .onPressed,
      isNull,
    );

    await tester.tap(find.byKey(const Key('recover-settings-button')));
    await tester.pumpAndSettle();
    expect(find.text('Use recovered sections?'), findsOneWidget);
    await tester.tap(find.byKey(const Key('confirm-safe-recovery-button')));
    await tester.pumpAndSettle();

    final save = tester.widget<FilledButton>(
      find.byKey(const Key('save-settings-button')),
    );
    expect(save.onPressed, isNotNull);
    final saveFinder = find.byKey(const Key('save-settings-button'));
    await tester.ensureVisible(saveFinder);
    await tester.tap(saveFinder);
    await tester.pumpAndSettle();
    expect(repository.savedApi, isNotNull);
    expect(repository.savedNotifications, isNotNull);
  });

  testWidgets('notification failure stays unavailable during safe recovery', (
    tester,
  ) async {
    final repository = _FakeSettingsRepository([
      _result(
        failures: const [
          SettingsLoadFailure(
            source: SettingsDataSource.notifications,
            code: 'invalid-notifications',
          ),
        ],
        notifications: null,
      ),
    ]);

    await _pumpHome(tester, repository);
    await tester.tap(find.byTooltip('Settings'));
    await tester.pumpAndSettle();

    expect(find.textContaining('Notifications:'), findsOneWidget);
    expect(find.text('Unavailable until loading succeeds'), findsOneWidget);
    final notifications = tester.widget<SwitchListTile>(
      find.widgetWithText(SwitchListTile, 'Notifications'),
    );
    expect(notifications.onChanged, isNull);

    await tester.tap(find.byKey(const Key('recover-settings-button')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('confirm-safe-recovery-button')));
    await tester.pumpAndSettle();
    final save = find.byKey(const Key('save-settings-button'));
    await tester.ensureVisible(save);
    await tester.tap(save);
    await tester.pumpAndSettle();

    expect(repository.savedApi, isNotNull);
    expect(repository.savedNotifications, isNull);
  });

  testWidgets('reset requires confirmation and preserves protected sources', (
    tester,
  ) async {
    final repository = _FakeSettingsRepository([
      _result(
        apiSettings: null,
        failures: const [
          SettingsLoadFailure(
            source: SettingsDataSource.apiConfiguration,
            code: 'corrupt-migration',
          ),
        ],
      ),
      _result(),
    ]);

    await _pumpHome(tester, repository);
    await tester.tap(find.byKey(const Key('reset-settings-button')));
    await tester.pumpAndSettle();

    expect(find.text('Reset editable settings?'), findsOneWidget);
    expect(
      find.textContaining('Pairing and device identity are kept'),
      findsOneWidget,
    );
    expect(repository.resetCount, 0);
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();
    expect(repository.resetCount, 0);

    await tester.tap(find.byKey(const Key('reset-settings-button')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('confirm-reset-settings-button')));
    await tester.pumpAndSettle();

    expect(repository.resetCount, 1);
    expect(repository.loadCount, 2);
    expect(find.text('Settings could not be loaded'), findsNothing);
  });

  testWidgets('error announcement is live and settings support keyboard flow', (
    tester,
  ) async {
    final repository = _FakeSettingsRepository([
      _result(
        apiSettings: null,
        failures: const [
          SettingsLoadFailure(
            source: SettingsDataSource.apiConfiguration,
            code: 'read-unavailable',
          ),
        ],
      ),
      _result(),
    ]);
    final semantics = tester.ensureSemantics();

    await _pumpHome(tester, repository);
    final errorSemantics = tester.getSemantics(
      find.byKey(const Key('settings-load-error')),
    );
    expect(errorSemantics.flagsCollection.isLiveRegion, isTrue);

    final retry = find.byKey(const Key('retry-settings-button'));
    await tester.ensureVisible(retry);
    await tester.tap(retry);
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('Settings'));
    await tester.pumpAndSettle();

    final serverField = find.byKey(const Key('server-settings-field'));
    await tester.tap(serverField);
    await tester.enterText(serverField, 'https://keyboard.example.test');
    await tester.sendKeyEvent(LogicalKeyboardKey.tab);
    await tester.pump();
    final tokenEditable = tester.widget<EditableText>(
      find.descendant(
        of: find.byKey(const Key('token-settings-field')),
        matching: find.byType(EditableText),
      ),
    );
    expect(tokenEditable.focusNode.hasFocus, isTrue);
    semantics.dispose();
  });
}

Future<void> _pumpHome(
  WidgetTester tester,
  SettingsRepository repository,
) async {
  await tester.pumpWidget(
    MaterialApp(
      home: RumiRemoteHome(
        settingsRepository: repository,
        refreshOnLoad: false,
      ),
    ),
  );
  await tester.pumpAndSettle();
}

SettingsLoadResult _result({
  RumiRemoteSettings? apiSettings = RumiRemoteSettings.defaults,
  PairedDeviceSummary? pairedDevice,
  MobileNotificationSettings? notifications =
      MobileNotificationSettings.defaults,
  DeviceIdentitySummary? deviceIdentity,
  List<SettingsLoadFailure> failures = const [],
}) {
  return SettingsLoadResult(
    apiSettings: apiSettings,
    pairedDevice: pairedDevice,
    notifications: notifications,
    deviceIdentity: deviceIdentity,
    failures: failures,
  );
}

class _FakeSettingsRepository implements SettingsRepository {
  _FakeSettingsRepository(this.results);

  final List<SettingsLoadResult> results;
  int loadCount = 0;
  int resetCount = 0;
  RumiRemoteSettings? savedApi;
  MobileNotificationSettings? savedNotifications;

  @override
  Future<SettingsLoadResult> loadAll() async {
    final index = loadCount < results.length ? loadCount : results.length - 1;
    loadCount += 1;
    return results[index];
  }

  @override
  Future<void> resetEditableSettings() async {
    resetCount += 1;
  }

  @override
  Future<void> saveApi(RumiRemoteSettings settings) async {
    savedApi = settings;
  }

  @override
  Future<void> saveNotifications(MobileNotificationSettings settings) async {
    savedNotifications = settings;
  }
}
