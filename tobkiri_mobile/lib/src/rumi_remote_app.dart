import 'package:flutter/material.dart';

import 'appearance_settings.dart';
import 'app_theme.dart';
import 'rumi_remote_home.dart';

class RumiRemoteApp extends StatefulWidget {
  const RumiRemoteApp({
    super.key,
    this.appearanceController,
    this.appearanceStore,
    this.initialAppearance = AppearanceMode.system,
  });

  final AppearanceSettingsController? appearanceController;
  final AppearanceSettingsStore? appearanceStore;
  final AppearanceMode initialAppearance;

  @override
  State<RumiRemoteApp> createState() => _RumiRemoteAppState();
}

class _RumiRemoteAppState extends State<RumiRemoteApp> {
  late final AppearanceSettingsController _appearanceController =
      widget.appearanceController ??
          AppearanceSettingsController(
            store: widget.appearanceStore ?? InMemoryAppearanceSettingsStore(),
            initialMode: widget.initialAppearance,
          );
  late final bool _ownsAppearanceController =
      widget.appearanceController == null;

  @override
  void dispose() {
    if (_ownsAppearanceController) {
      _appearanceController.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _appearanceController,
      builder: (context, child) => AppearanceSettingsScope(
        controller: _appearanceController,
        child: MaterialApp(
          title: 'Tobkiri',
          debugShowCheckedModeBanner: false,
          theme: buildRumiLightTheme(),
          darkTheme: buildRumiDarkTheme(),
          highContrastTheme: buildRumiHighContrastLightTheme(),
          highContrastDarkTheme: buildRumiHighContrastDarkTheme(),
          themeMode: _appearanceController.mode.themeMode,
          home: RumiRemoteHome(
            appearanceMode: _appearanceController.mode,
            onAppearanceChanged: _appearanceController.select,
          ),
        ),
      ),
    );
  }
}
