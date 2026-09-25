enum AssistantLinkBlockReason {
  empty,
  tooLong,
  malformed,
  unsupportedScheme,
  missingHost,
  embeddedCredentials,
}

class AssistantLinkPreview {
  const AssistantLinkPreview._({
    required this.raw,
    required this.uri,
    required this.host,
    required this.blockReason,
    required this.warnings,
  });

  final String raw;
  final Uri? uri;
  final String host;
  final AssistantLinkBlockReason? blockReason;
  final List<String> warnings;

  bool get canOpen => uri != null && blockReason == null;

  String get fullUrl => uri?.toString() ?? raw;

  String get blockedMessage => switch (blockReason) {
        AssistantLinkBlockReason.empty => 'リンク先が空です。',
        AssistantLinkBlockReason.tooLong => 'リンク先が長すぎるため開けません。',
        AssistantLinkBlockReason.malformed => 'リンク先の形式を確認できません。',
        AssistantLinkBlockReason.unsupportedScheme => 'この種類のリンクは安全のため開けません。',
        AssistantLinkBlockReason.missingHost => 'リンク先のホストを確認できません。',
        AssistantLinkBlockReason.embeddedCredentials =>
          '認証情報を含むリンクは安全のため開けません。',
        null => '',
      };

  static AssistantLinkPreview parse(String href) {
    final raw = href.trim();
    if (raw.isEmpty) {
      return const AssistantLinkPreview._(
        raw: '',
        uri: null,
        host: '',
        blockReason: AssistantLinkBlockReason.empty,
        warnings: [],
      );
    }
    if (raw.length > 2048) {
      return AssistantLinkPreview._(
        raw: raw,
        uri: null,
        host: '',
        blockReason: AssistantLinkBlockReason.tooLong,
        warnings: const [],
      );
    }
    if (raw.codeUnits.any((unit) => unit <= 0x20 || unit == 0x7f)) {
      return AssistantLinkPreview._(
        raw: raw,
        uri: null,
        host: '',
        blockReason: AssistantLinkBlockReason.malformed,
        warnings: const [],
      );
    }

    final uri = Uri.tryParse(raw);
    if (uri == null || !uri.hasScheme) {
      return AssistantLinkPreview._(
        raw: raw,
        uri: null,
        host: '',
        blockReason: AssistantLinkBlockReason.malformed,
        warnings: const [],
      );
    }
    final scheme = uri.scheme.toLowerCase();
    if (scheme != 'https' && scheme != 'http') {
      return AssistantLinkPreview._(
        raw: raw,
        uri: null,
        host: '',
        blockReason: AssistantLinkBlockReason.unsupportedScheme,
        warnings: const [],
      );
    }
    if (uri.host.trim().isEmpty) {
      return AssistantLinkPreview._(
        raw: raw,
        uri: null,
        host: '',
        blockReason: AssistantLinkBlockReason.missingHost,
        warnings: const [],
      );
    }
    final rawHost = _rawHost(raw);
    if (rawHost.contains('%')) {
      return AssistantLinkPreview._(
        raw: raw,
        uri: null,
        host: '',
        blockReason: AssistantLinkBlockReason.malformed,
        warnings: const [],
      );
    }
    if (uri.userInfo.isNotEmpty) {
      return AssistantLinkPreview._(
        raw: raw,
        uri: null,
        host: uri.host,
        blockReason: AssistantLinkBlockReason.embeddedCredentials,
        warnings: const [],
      );
    }

    final warnings = <String>[];
    if (scheme == 'http') {
      warnings.add('暗号化されていない HTTP リンクです。');
    }
    final hasUnicodeHost = rawHost.runes.any((codePoint) => codePoint > 0x7f);
    final hasPunycode = uri.host
        .toLowerCase()
        .split('.')
        .any((label) => label.startsWith('xn--'));
    if (hasUnicodeHost || hasPunycode) {
      warnings.add('見た目が似た文字を使うドメインの可能性があります。');
    }
    const redirectKeys = {
      'continue',
      'dest',
      'destination',
      'next',
      'redirect',
      'redirect_uri',
      'return',
      'return_url',
      'target',
      'url',
    };
    if (uri.queryParameters.keys
        .map((key) => key.toLowerCase())
        .any(redirectKeys.contains)) {
      warnings.add('別のページへ移動する指定を含むリンクです。');
    }

    return AssistantLinkPreview._(
      raw: raw,
      uri: uri,
      host: uri.host,
      blockReason: null,
      warnings: List.unmodifiable(warnings),
    );
  }
}

String _rawHost(String raw) {
  final schemeEnd = raw.indexOf('://');
  if (schemeEnd < 0) return '';
  final authorityStart = schemeEnd + 3;
  var authorityEnd = raw.length;
  for (final delimiter in ['/', '?', '#']) {
    final index = raw.indexOf(delimiter, authorityStart);
    if (index >= 0 && index < authorityEnd) authorityEnd = index;
  }
  var authority = raw.substring(authorityStart, authorityEnd);
  final userInfoEnd = authority.lastIndexOf('@');
  if (userInfoEnd >= 0) authority = authority.substring(userInfoEnd + 1);
  if (authority.startsWith('[')) {
    final closing = authority.indexOf(']');
    return closing >= 0 ? authority.substring(1, closing) : authority;
  }
  final portSeparator = authority.lastIndexOf(':');
  if (portSeparator >= 0) authority = authority.substring(0, portSeparator);
  return authority;
}
