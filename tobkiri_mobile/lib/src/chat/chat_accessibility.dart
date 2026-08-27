import 'chat_models.dart';

/// Stable Japanese accessibility copy for the Tobkiri mobile chat surface.
abstract final class TobkiriChatAccessibility {
  static const userMessage = 'あなたのメッセージ';
  static const assistantMessage = 'Tobkiriの応答';
  static const processing = '処理中';
  static const failed = '送信に失敗しました';
  static const noContent = '内容なし';
  static const content = '内容';

  static const composer = 'メッセージ入力欄';
  static const composerHint = '複数行のメッセージを入力します';
  static const add = 'オプションを追加';
  static const addHint = 'チャットのオプションを開きます';
  static const send = 'メッセージを送信';
  static const sendHint = '入力したメッセージを送信します';
  static const sendDisabledHint = '送信するテキストを入力してください';
  static const stop = '応答を停止';
  static const stopHint = '進行中の応答生成を停止します';
  static const copy = 'メッセージをコピー';
  static const copyHint = 'メッセージの内容をコピーします';
  static const copied = 'コピーしました';
  static const openLinkHint = 'リンクを開きます';

  /// Build one non-duplicative announcement for a chat message.
  static String messageLabel(ChatMessage message) {
    final parts = <String>[
      message.role == ChatRole.user ? userMessage : assistantMessage,
      if (message.pending) processing,
      if (message.error) failed,
    ];
    final plainContent = _plainText(message.content.trim());
    parts.add(plainContent.isEmpty ? noContent : '$content: $plainContent');
    return parts.join(', ');
  }

  /// Build the accessible name for a safe link action.
  static String link(Uri uri) => 'リンク: $uri';
}

String _plainText(String markdown) => markdown
    .replaceAll(RegExp(r'!\[([^\]]*)\]\([^)]*\)'), r'$1')
    .replaceAll(RegExp(r'\[([^\]]+)\]\([^)]*\)'), r'$1')
    .replaceAll(RegExp(r'[`*_>#~-]'), '')
    .trim();
