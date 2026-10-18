import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'app_theme.dart';
import 'authority_approval_screen.dart';
import 'chat/chat_screen.dart';
import 'rumi_remote_home.dart';

class RumiRemoteApp extends StatelessWidget {
  const RumiRemoteApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Tobkiri',
      debugShowCheckedModeBanner: false,
      theme: buildRumiTheme(),
      localizationsDelegates: GlobalMaterialLocalizations.delegates,
      supportedLocales: const [Locale('en'), Locale('ja')],
      home: const _TobkiriHome(),
    );
  }
}

class _TobkiriHome extends StatefulWidget {
  const _TobkiriHome();

  @override
  State<_TobkiriHome> createState() => _TobkiriHomeState();
}

class _TobkiriHomeState extends State<_TobkiriHome> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _index,
        children: const [
          ChatScreen(),
          RumiRemoteHome(),
          AuthorityApprovalScreen(),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (index) => setState(() => _index = index),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.chat_bubble_outline),
            selectedIcon: Icon(Icons.chat_bubble),
            label: 'チャット',
            tooltip: 'Tobkiri チャット',
          ),
          NavigationDestination(
            icon: Icon(Icons.computer_outlined),
            selectedIcon: Icon(Icons.computer),
            label: 'PC 管理',
            tooltip: 'Tobkiri PC 管理',
          ),
          NavigationDestination(
            icon: Icon(Icons.verified_user_outlined),
            selectedIcon: Icon(Icons.verified_user),
            label: '承認',
            tooltip: '権限リクエストの承認',
          ),
        ],
      ),
    );
  }
}
