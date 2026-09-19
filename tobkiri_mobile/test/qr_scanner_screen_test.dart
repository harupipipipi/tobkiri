import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/qr/qr_scanner_screen.dart';

void main() {
  testWidgets('manual plaintext API QR stays open with a safe rejection',
      (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    try {
      const secret = 'do-not-display-this-key';
      await tester.pumpWidget(
        const MaterialApp(
          home: QrScannerScreen(purpose: QrScanPurpose.apiImport),
        ),
      );

      await tester.tap(find.text('手入力に切り替え'));
      await tester.pump();
      await tester.enterText(
        find.byType(TextField),
        '{"kind":"rumi_api","baseUrl":"https://attacker.invalid",'
        '"apiKey":"$secret"}',
      );
      await tester.tap(find.text('取り込む'));
      await tester.pump();

      expect(find.byType(QrScannerScreen), findsOneWidget);
      expect(
          find.byKey(const ValueKey('qr-rejection-message')), findsOneWidget);
      expect(find.textContaining('旧式QR'), findsOneWidget);
      expect(find.textContaining(secret), findsNothing);
    } finally {
      debugDefaultTargetPlatformOverride = null;
    }
  });
}
