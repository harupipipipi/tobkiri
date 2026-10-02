import 'package:flutter/material.dart';

import 'app_theme.dart';
import 'authority_approval_screen.dart';
import 'conversation/adaptive_conversation_screen.dart';
import 'rumi_remote_home.dart';

class RumiRemoteApp extends StatelessWidget {
  const RumiRemoteApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Tobkiri',
      debugShowCheckedModeBanner: false,
      theme: buildRumiTheme(),
      home: const _TobkiriMobileHome(),
    );
  }
}

class _TobkiriMobileHome extends StatefulWidget {
  const _TobkiriMobileHome();

  @override
  State<_TobkiriMobileHome> createState() => _TobkiriMobileHomeState();
}

class _TobkiriMobileHomeState extends State<_TobkiriMobileHome> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _index,
        children: const [
          AdaptiveConversationScreen(),
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
            label: 'Conversations',
            tooltip: 'Tobkiri conversations',
          ),
          NavigationDestination(
            icon: Icon(Icons.computer_outlined),
            selectedIcon: Icon(Icons.computer),
            label: 'PC',
            tooltip: 'Tobkiri PC management',
          ),
          NavigationDestination(
            icon: Icon(Icons.verified_user_outlined),
            selectedIcon: Icon(Icons.verified_user),
            label: 'Approvals',
            tooltip: 'Authority approvals',
          ),
        ],
      ),
    );
  }
}
