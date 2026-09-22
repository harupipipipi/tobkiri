import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'src/rumi_app.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  FlutterError.onError = (details) {
    FlutterError.presentError(details);
    debugPrint('Rumi Flutter error: ${details.exceptionAsString()}');
    final stack = details.stack;
    if (stack != null) debugPrint(stack.toString());
  };
  PlatformDispatcher.instance.onError = (error, stack) {
    debugPrint('Rumi platform error: $error\n$stack');
    return true;
  };
  runZonedGuarded(
    () => runApp(const RumiApp()),
    (error, stack) => debugPrint('Rumi uncaught error: $error\n$stack'),
  );
}
