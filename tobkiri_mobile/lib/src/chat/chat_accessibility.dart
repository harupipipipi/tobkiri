import 'package:flutter/widgets.dart';

class TobkiriChatAccessibilityLabels {
  const TobkiriChatAccessibilityLabels._(this.japanese);

  final bool japanese;

  static TobkiriChatAccessibilityLabels of(BuildContext context) =>
      TobkiriChatAccessibilityLabels._(
        Localizations.localeOf(context).languageCode == 'ja',
      );

  String get user => japanese ? 'あなたのメッセージ' : 'Your message';
  String get assistant => japanese ? 'Tobkiriの応答' : 'Tobkiri response';
  String get processing => japanese ? '処理中' : 'Processing';
  String get failed => japanese ? '送信に失敗しました' : 'Message failed';
  String get noContent => japanese ? '内容なし' : 'No content';
  String get content => japanese ? '内容' : 'Content';
  String get composer => japanese ? 'メッセージ入力欄' : 'Message field';
  String get composerHint =>
      japanese ? '複数行のメッセージを入力します' : 'Enter a multiline message';
  String get add => japanese ? 'オプションを追加' : 'Add options';
  String get addHint => japanese ? 'チャットのオプションを開きます' : 'Open chat options';
  String get send => japanese ? 'メッセージを送信' : 'Send message';
  String get sendHint =>
      japanese ? '入力したメッセージを送信します' : 'Send the entered message';
  String get sendDisabledHint =>
      japanese ? '送信するテキストを入力してください' : 'Enter text to send';
  String get stop => japanese ? '応答を停止' : 'Stop response';
  String get stopHint =>
      japanese ? '進行中の応答生成を停止します' : 'Stop the current response';
  String get copy => japanese ? 'メッセージをコピー' : 'Copy message';
  String get copyHint =>
      japanese ? 'メッセージの内容をコピーします' : 'Copy the message content';
  String get copied => japanese ? 'コピーしました' : 'Copied';
  String get openLinkHint => japanese ? 'リンクを開きます' : 'Open link';
  String link(String value) => japanese ? 'リンク: $value' : 'Link: $value';
}
