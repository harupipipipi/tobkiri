import 'dart:convert';

import '../mobile_authority.dart';

/// Persists composer drafts independently from conversation messages.
abstract interface class ChatDraftStore {
  /// Loads the exact draft for [scope], or an empty string when none exists.
  Future<String> load(String scope);

  /// Replaces the draft for [scope]. Empty drafts remove persisted state.
  Future<void> save(String scope, String text);
}

/// Stores drafts in the platform-protected secret store.
///
/// Drafts can contain private user text, so they use the same encrypted storage
/// boundary as the paired mobile connection. Scope values are encoded before
/// becoming storage keys and are never interpreted as paths.
final class MobileChatDraftStore implements ChatDraftStore {
  MobileChatDraftStore({AuthoritySecretStore? storage})
      : _storage = storage ?? FlutterAuthoritySecretStore();

  static const _keyPrefix = 'tobkiri.mobile.chat_draft.v1.';
  static const _maximumDraftLength = 100000;

  final AuthoritySecretStore _storage;

  @override
  Future<String> load(String scope) async {
    final value = await _storage.read(_key(scope));
    if (value == null || value.length > _maximumDraftLength) return '';
    return value;
  }

  @override
  Future<void> save(String scope, String text) async {
    if (text.length > _maximumDraftLength) {
      throw StateError('chat draft is too large');
    }
    final key = _key(scope);
    if (text.isEmpty) {
      await _storage.delete(key);
      return;
    }
    await _storage.write(key, text);
  }

  String _key(String scope) {
    final normalized = scope.trim();
    if (normalized.isEmpty || normalized.length > 512) {
      throw StateError('chat draft scope is invalid');
    }
    final encoded = base64Url.encode(utf8.encode(normalized));
    return '$_keyPrefix$encoded';
  }
}
