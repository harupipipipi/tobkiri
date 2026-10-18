import 'package:flutter/foundation.dart';

import '../models.dart';

typedef PcConversationLoader = Future<List<PcConversation>> Function();

/// Owns the last successful PC conversation projection for the drawer.
///
/// A failed refresh never clears [conversations].  The UI can therefore make
/// the offline/stale state explicit while still letting the user choose a
/// cached conversation.
class PcConversationsController extends ChangeNotifier {
  PcConversationsController({
    required PcConversationLoader loader,
    List<PcConversation> initialConversations = const [],
    bool initiallyOffline = false,
    bool initiallyStale = false,
    DateTime Function()? clock,
  })  : _loader = loader,
        _conversations = List.unmodifiable(initialConversations),
        _offline = initiallyOffline,
        _stale = initiallyStale,
        _clock = clock ?? DateTime.now;

  PcConversationLoader _loader;
  List<PcConversation> _conversations;
  final DateTime Function() _clock;
  bool _loading = false;
  bool _offline;
  bool _stale;
  Object? _error;
  DateTime? _lastSuccessfulAt;
  int _refreshGeneration = 0;
  bool _disposed = false;

  List<PcConversation> get conversations => _conversations;
  bool get loading => _loading;
  bool get offline => _offline;
  bool get stale => _stale;
  Object? get error => _error;
  DateTime? get lastSuccessfulAt => _lastSuccessfulAt;

  void updateLoader(PcConversationLoader loader) {
    _loader = loader;
  }

  Future<void> refresh() async {
    if (_loading) {
      return;
    }
    final generation = ++_refreshGeneration;
    _loading = true;
    _notifyListeners();
    try {
      final next = await _loader();
      if (generation != _refreshGeneration) {
        return;
      }
      _conversations = List.unmodifiable(next);
      _offline = false;
      _stale = false;
      _error = null;
      _lastSuccessfulAt = _clock();
    } catch (error) {
      if (generation != _refreshGeneration) {
        return;
      }
      // Keep the previous projection intact so a transient network failure
      // cannot erase the user's cached navigation context.
      _error = error;
      _offline = true;
      _stale = _conversations.isNotEmpty;
    } finally {
      if (generation == _refreshGeneration) {
        _loading = false;
        _notifyListeners();
      }
    }
  }

  /// Clears state when the server authority changes.
  ///
  /// An in-flight request is invalidated as well, so a response from the old
  /// server can never repopulate a drawer now pointed at a new server.
  void reset() {
    _refreshGeneration += 1;
    _loading = false;
    _conversations = const [];
    _offline = false;
    _stale = false;
    _error = null;
    _lastSuccessfulAt = null;
    _notifyListeners();
  }

  void markOffline({Object? error, bool stale = true}) {
    _offline = true;
    _stale = stale && _conversations.isNotEmpty;
    _error = error;
    _notifyListeners();
  }

  void clearError() {
    if (_error == null) {
      return;
    }
    _error = null;
    _notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _refreshGeneration += 1;
    super.dispose();
  }

  void _notifyListeners() {
    if (!_disposed) {
      notifyListeners();
    }
  }
}
