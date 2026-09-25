import 'package:flutter_test/flutter_test.dart';

import 'package:rumi_remote_app/src/chat/assistant_link_policy.dart';

void main() {
  test('accepts absolute HTTPS links and discloses the destination', () {
    final preview = AssistantLinkPreview.parse(
      'https://example.com/path?q=release#details',
    );

    expect(preview.canOpen, isTrue, reason: '${preview.blockReason}');
    expect(preview.host, 'example.com');
    expect(preview.fullUrl, 'https://example.com/path?q=release#details');
    expect(preview.warnings, isEmpty);
  });

  test('warns about cleartext, lookalike, and redirect-shaped web links', () {
    final cleartext = AssistantLinkPreview.parse('http://example.com/path');
    final lookalike = AssistantLinkPreview.parse('https://xn--80ak6aa92e.com/');
    final redirect = AssistantLinkPreview.parse(
      'https://example.com/login?next=https%3A%2F%2Fother.example',
    );

    expect(cleartext.canOpen, isTrue);
    expect(cleartext.warnings, contains(contains('HTTP')));
    expect(lookalike.canOpen, isTrue);
    expect(lookalike.warnings, contains(contains('似た文字')));
    expect(redirect.canOpen, isTrue);
    expect(redirect.warnings, contains(contains('別のページ')));
  });

  test('blocks custom, file, data, javascript, and credential links', () {
    for (final href in [
      'tel:+810000000000',
      'custom-app://open/item',
      'file:///private/secret',
      'data:text/html,hello',
      'javascript:alert(1)',
    ]) {
      final preview = AssistantLinkPreview.parse(href);
      expect(preview.canOpen, isFalse, reason: href);
      expect(
        preview.blockReason,
        AssistantLinkBlockReason.unsupportedScheme,
        reason: href,
      );
    }

    final credentials = AssistantLinkPreview.parse(
      'https://user:secret@example.com/path',
    );
    expect(credentials.canOpen, isFalse);
    expect(
      credentials.blockReason,
      AssistantLinkBlockReason.embeddedCredentials,
    );
  });

  test('blocks relative, whitespace, missing-host, and oversized links', () {
    expect(
      AssistantLinkPreview.parse('/relative/path').blockReason,
      AssistantLinkBlockReason.malformed,
    );
    expect(
      AssistantLinkPreview.parse('https://example.com/a b').blockReason,
      AssistantLinkBlockReason.malformed,
    );
    expect(
      AssistantLinkPreview.parse('https:///missing-host').blockReason,
      AssistantLinkBlockReason.missingHost,
    );
    expect(
      AssistantLinkPreview.parse('https://example%2ecom/path').blockReason,
      AssistantLinkBlockReason.malformed,
    );
    expect(
      AssistantLinkPreview.parse(
        'https://example.com/${List.filled(2048, 'a').join()}',
      ).blockReason,
      AssistantLinkBlockReason.tooLong,
    );
  });
}
