import 'package:flutter_test/flutter_test.dart';
import 'package:rumi_remote_app/src/qr/qr_payload.dart';

void main() {
  test('parses rumi_pair_v2 payload', () {
    final payload = parseQrPayload(
      '{"kind":"rumi_pair_v2","pairingId":"p1","code":"abc","baseUrls":["http://192.168.1.10:8765"],"serverPublicKey":"pk-1","expiresAt":9999999999999}',
    );
    expect(payload, isA<QrPairingV2>());
    final p = payload as QrPairingV2;
    expect(p.payload.pairingId, 'p1');
    expect(p.payload.code, 'abc');
    expect(p.payload.pickupSecret, '');
    expect(p.payload.baseUrls, ['http://192.168.1.10:8765']);
    expect(p.payload.serverPublicKey, 'pk-1');
    expect(p.payload.isExpired, isFalse);
  });

  test('parses rumi_mobile_pair_v1 payload', () {
    final payload = parseQrPayload(
      '{"kind":"rumi_mobile_pair_v1","version":1,"pairingId":"p1","code":"abc","pickupSecret":"pup_123","baseUrls":["http://192.168.1.10:8765"],"manifestUrl":"http://192.168.1.10:8765/api/mobile/v1/manifest","roles":["mobile_client","mobile_approver"],"serverPublicKey":"pk-1","expiresAt":9999999999999}',
    );
    expect(payload, isA<QrPairingV2>());
    final p = payload as QrPairingV2;
    expect(p.payload.pairingId, 'p1');
    expect(p.payload.pickupSecret, 'pup_123');
    expect(
      p.payload.manifestUrl,
      'http://192.168.1.10:8765/api/mobile/v1/manifest',
    );
    expect(p.payload.roles, ['mobile_client', 'mobile_approver']);
  });

  test('QrPairingV2 isExpired when past', () {
    final payload = parseQrPayload(
      '{"kind":"rumi_pair_v2","pairingId":"p1","code":"abc","baseUrls":[],"serverPublicKey":"","expiresAt":1000}',
    );
    expect(payload, isA<QrPairingV2>());
    expect((payload as QrPairingV2).payload.isExpired, isTrue);
  });

  test('QrPairingV2 with missing fields defaults gracefully', () {
    final payload = parseQrPayload('{"kind":"rumi_pair_v2"}');
    expect(payload, isA<QrPairingV2>());
    final p = payload as QrPairingV2;
    expect(p.payload.pairingId, '');
    expect(p.payload.code, '');
    expect(p.payload.baseUrls, isEmpty);
  });
}
