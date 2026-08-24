import 'package:flutter/material.dart';

import 'app_theme.dart';
import 'rumi_remote_home.dart';

class RumiRemoteApp extends StatelessWidget {
  const RumiRemoteApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Tobkiri Remote',
      debugShowCheckedModeBanner: false,
      theme: buildRumiTheme(),
      home: const RumiRemoteHome(),
    );
  }
}
