import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// The color appearance selected for the Tobkiri mobile client.
enum AppearanceMode {
  /// Follow the operating-system color appearance.
  system,

  /// Always use the light color appearance.
  light,

  /// Always use the dark color appearance.
  dark;

  /// The [ThemeMode] representation used by [MaterialApp].
  ThemeMode get themeMode => switch (this) {
        AppearanceMode.system => ThemeMode.system,
        AppearanceMode.light => ThemeMode.light,
        AppearanceMode.dark => ThemeMode.dark,
      };

  /// A short, user-facing label suitable for an appearance selection control.
  String get label => switch (this) {
        AppearanceMode.system => 'System',
        AppearanceMode.light => 'Light',
        AppearanceMode.dark => 'Dark',
      };

  /// An icon appropriate for an appearance selection control.
  IconData get icon => switch (this) {
        AppearanceMode.system => Icons.brightness_auto,
        AppearanceMode.light => Icons.light_mode_outlined,
        AppearanceMode.dark => Icons.dark_mode_outlined,
      };

  static AppearanceMode fromStorage(String? value) {
    for (final mode in AppearanceMode.values) {
      if (mode.name == value) {
        return mode;
      }
    }
    return AppearanceMode.system;
  }
}

/// Persists the non-secret appearance setting independently from credentials.
abstract interface class AppearanceSettingsStore {
  /// Loads the saved appearance, defaulting to [AppearanceMode.system].
  Future<AppearanceMode> load();

  /// Saves the user's selected appearance.
  Future<void> save(AppearanceMode mode);
}

/// An in-memory store for widget tests and short-lived previews.
class InMemoryAppearanceSettingsStore implements AppearanceSettingsStore {
  InMemoryAppearanceSettingsStore([this.value = AppearanceMode.system]);

  AppearanceMode value;

  @override
  Future<AppearanceMode> load() async => value;

  @override
  Future<void> save(AppearanceMode mode) async {
    value = mode;
  }
}

/// A [SharedPreferences]-backed implementation for non-secret UI preferences.
class SharedPreferencesAppearanceSettingsStore
    implements AppearanceSettingsStore {
  SharedPreferencesAppearanceSettingsStore(this._preferences);

  static const _appearanceModeKey = 'tobkiri.appearance.mode';

  final SharedPreferences _preferences;

  /// Creates a store after resolving the platform preferences service.
  static Future<SharedPreferencesAppearanceSettingsStore> create() async {
    final preferences = await SharedPreferences.getInstance();
    return SharedPreferencesAppearanceSettingsStore(preferences);
  }

  @override
  Future<AppearanceMode> load() async =>
      AppearanceMode.fromStorage(_preferences.getString(_appearanceModeKey));

  @override
  Future<void> save(AppearanceMode mode) async {
    final saved = await _preferences.setString(_appearanceModeKey, mode.name);
    if (!saved) {
      throw StateError('Could not save the Tobkiri appearance setting.');
    }
  }
}

/// Holds the loaded appearance and persists subsequent user selections.
class AppearanceSettingsController extends ChangeNotifier {
  AppearanceSettingsController({
    required AppearanceSettingsStore store,
    AppearanceMode initialMode = AppearanceMode.system,
  })  : _store = store,
        _mode = initialMode;

  final AppearanceSettingsStore _store;
  AppearanceMode _mode;

  /// The currently active user selection.
  AppearanceMode get mode => _mode;

  /// Loads an initial controller without allowing a storage failure to block app launch.
  static Future<AppearanceSettingsController> load({
    required AppearanceSettingsStore store,
  }) async {
    AppearanceMode initialMode = AppearanceMode.system;
    try {
      initialMode = await store.load();
    } catch (_) {
      // A non-secret preference must never prevent the app from launching.
    }
    return AppearanceSettingsController(store: store, initialMode: initialMode);
  }

  /// Changes the appearance and restores the prior value if persistence fails.
  Future<void> select(AppearanceMode mode) async {
    if (mode == _mode) {
      return;
    }

    final previousMode = _mode;
    _mode = mode;
    notifyListeners();
    try {
      await _store.save(mode);
    } catch (_) {
      _mode = previousMode;
      notifyListeners();
      rethrow;
    }
  }
}

/// Makes an [AppearanceSettingsController] available to the app's settings UI.
class AppearanceSettingsScope
    extends InheritedNotifier<AppearanceSettingsController> {
  const AppearanceSettingsScope({
    required AppearanceSettingsController controller,
    required super.child,
    super.key,
  }) : super(notifier: controller);

  /// Returns the controller and rebuilds when its selected appearance changes.
  static AppearanceSettingsController of(BuildContext context) {
    final scope =
        context.dependOnInheritedWidgetOfExactType<AppearanceSettingsScope>();
    assert(scope != null, 'AppearanceSettingsScope is missing from the tree.');
    return scope!.notifier!;
  }

  /// Returns the controller without establishing an inherited dependency.
  static AppearanceSettingsController? maybeOf(BuildContext context) {
    final element = context
        .getElementForInheritedWidgetOfExactType<AppearanceSettingsScope>();
    final widget = element?.widget;
    return widget is AppearanceSettingsScope ? widget.notifier : null;
  }
}
