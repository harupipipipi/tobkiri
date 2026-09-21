import 'package:flutter/material.dart';
import 'app_theme.dart';
import 'chat/chat_screen.dart';
import 'chat/chat_store.dart';
import 'data/pc/device_store.dart';
import 'settings/api_config_store.dart';

class RumiApp extends StatelessWidget {
  const RumiApp({super.key});

  @override
  Widget build(BuildContext context) {
    final store = ChatStore();
    final configStore = ApiConfigStore();
    final deviceStore = MobileDeviceStore();

    return MaterialApp(
      title: 'Rumi',
      debugShowCheckedModeBanner: false,
      theme: buildRumiTheme(dark: true),
      darkTheme: buildRumiTheme(dark: true),
      themeMode: ThemeMode.dark,
      home: ChatScreen(
        store: store,
        configStore: configStore,
        deviceStore: deviceStore,
      ),
    );
  }
}
