import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../app_theme.dart';
import '../platform/platform_services.dart';
import 'assistant_link_policy.dart';
import 'chat_models.dart';

class MessageView extends StatelessWidget {
  const MessageView({
    super.key,
    required this.message,
    this.urlLauncher = const PlatformUrlLauncher(),
    this.clipboard = const PlatformClipboard(),
  });

  final ChatMessage message;
  final PlatformUrlLauncher urlLauncher;
  final PlatformClipboard clipboard;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = theme.extension<RumiColors>() ?? RumiColors.dark;
    final isUser = message.role == ChatRole.user;
    final bg = message.error
        ? Colors.red.withValues(alpha: 0.14)
        : (isUser ? colors.bubbleUser : colors.bubbleAssistant);
    final fg = isUser ? colors.bubbleUserText : colors.bubbleAssistantText;

    final bubble = Container(
      constraints: const BoxConstraints(maxWidth: 520),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.only(
          topLeft: const Radius.circular(16),
          topRight: const Radius.circular(16),
          bottomLeft: isUser ? const Radius.circular(16) : Radius.zero,
          bottomRight: isUser ? Radius.zero : const Radius.circular(16),
        ),
      ),
      child: _MessageBody(
        message: message,
        fg: fg,
        colors: colors,
        urlLauncher: urlLauncher,
        clipboard: clipboard,
      ),
    );

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Row(
        mainAxisAlignment:
            isUser ? MainAxisAlignment.end : MainAxisAlignment.start,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (isUser) _CopyMessageButton(message: message),
          Flexible(child: bubble),
          if (!isUser) _CopyMessageButton(message: message),
        ],
      ),
    );
  }
}

class _CopyMessageButton extends StatelessWidget {
  const _CopyMessageButton({required this.message});

  final ChatMessage message;

  @override
  Widget build(BuildContext context) {
    final disabled = message.content.trim().isEmpty;
    return Padding(
      padding: const EdgeInsets.only(top: 2),
      child: IconButton(
        tooltip: 'コピー',
        visualDensity: VisualDensity.compact,
        constraints: const BoxConstraints.tightFor(width: 34, height: 34),
        padding: EdgeInsets.zero,
        iconSize: 17,
        color: Theme.of(context).colorScheme.onSurfaceVariant,
        icon: const Icon(Icons.copy_outlined),
        onPressed: disabled
            ? null
            : () => unawaited(copyMessageContent(context, message)),
      ),
    );
  }
}

class _MessageBody extends StatelessWidget {
  const _MessageBody({
    required this.message,
    required this.fg,
    required this.colors,
    required this.urlLauncher,
    required this.clipboard,
  });
  final ChatMessage message;
  final Color fg;
  final RumiColors colors;
  final PlatformUrlLauncher urlLauncher;
  final PlatformClipboard clipboard;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    if (message.content.isEmpty && message.pending) {
      return Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text(
            '処理中...',
            style: TextStyle(color: fg, fontSize: 14, height: 1.4),
          ),
          const SizedBox(width: 10),
          _TypingIndicator(fg: fg),
        ],
      );
    }
    return MarkdownBody(
      data: message.content,
      selectable: true,
      builders: {
        'a': _AssistantLinkBuilder(
          onTap: (href) => unawaited(_handleAssistantLink(context, href)),
        ),
      },
      styleSheet: MarkdownStyleSheet.fromTheme(theme).copyWith(
        p: TextStyle(color: fg, fontSize: 15, height: 1.5),
        code: TextStyle(
          color: fg,
          backgroundColor: colors.codeBackground,
          fontFamily: 'monospace',
          fontSize: 13,
        ),
        codeblockDecoration: BoxDecoration(
          color: colors.codeBackground,
          borderRadius: BorderRadius.circular(10),
        ),
        blockquoteDecoration: BoxDecoration(
          color: colors.codeBackground,
          borderRadius: BorderRadius.circular(8),
        ),
        a: const TextStyle(
          color: Colors.lightBlueAccent,
          decoration: TextDecoration.underline,
        ),
      ),
      onTapLink: (text, href, title) async {
        if (href == null) return;
        await _handleAssistantLink(context, href);
      },
    );
  }

  Future<void> _handleAssistantLink(BuildContext context, String href) async {
    final preview = AssistantLinkPreview.parse(href);
    if (!preview.canOpen) {
      _showLinkMessage(context, preview.blockedMessage);
      return;
    }
    final action = await showDialog<_AssistantLinkAction>(
      context: context,
      builder: (dialogContext) => _AssistantLinkDialog(preview: preview),
    );
    if (!context.mounted || action == null) return;
    if (action == _AssistantLinkAction.copy) {
      try {
        await clipboard.writeText(preview.fullUrl);
        if (context.mounted) _showLinkMessage(context, 'リンクをコピーしました。');
      } catch (_) {
        if (context.mounted) _showLinkMessage(context, 'リンクをコピーできませんでした。');
      }
      return;
    }
    if (action != _AssistantLinkAction.open) return;
    try {
      final opened = await urlLauncher.open(preview.uri!);
      if (!opened && context.mounted) {
        _showLinkMessage(context, 'リンクを開けませんでした。URLをコピーして確認してください。');
      }
    } catch (_) {
      if (context.mounted) {
        _showLinkMessage(context, 'リンクを開けませんでした。URLをコピーして確認してください。');
      }
    }
  }

  void _showLinkMessage(BuildContext context, String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(message), duration: const Duration(seconds: 3)),
    );
  }
}

enum _AssistantLinkAction { open, copy, cancel }

class _AssistantLinkBuilder extends MarkdownElementBuilder {
  _AssistantLinkBuilder({required this.onTap});

  final void Function(String href) onTap;

  @override
  Widget? visitElementAfterWithContext(
    BuildContext context,
    dynamic element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) {
    final href = element.attributes['href'] as String?;
    final text = element.textContent as String;
    if (href == null || text.isEmpty) return null;
    final preview = AssistantLinkPreview.parse(href);
    final destination = preview.host.isEmpty ? href : preview.host;
    final linkStyle = parentStyle?.merge(preferredStyle) ?? preferredStyle;

    return Semantics(
      container: true,
      link: true,
      label: '$text, リンク, リンク先 $destination',
      excludeSemantics: true,
      onTap: () => onTap(href),
      child: TextButton(
        style: TextButton.styleFrom(
          minimumSize: const Size(48, 48),
          padding: const EdgeInsets.symmetric(horizontal: 4),
          tapTargetSize: MaterialTapTargetSize.shrinkWrap,
          foregroundColor: linkStyle?.color,
          textStyle: linkStyle,
        ),
        onPressed: () => onTap(href),
        child: Text(text),
      ),
    );
  }
}

class _AssistantLinkDialog extends StatelessWidget {
  const _AssistantLinkDialog({required this.preview});

  final AssistantLinkPreview preview;

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      semanticLabel: 'リンク先の確認: ${preview.host}',
      title: const Text('リンク先を確認'),
      content: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 440),
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text('外部アプリへ移動する前に、ドメインとURLを確認してください。'),
              const SizedBox(height: 16),
              Text('ドメイン', style: Theme.of(context).textTheme.labelLarge),
              const SizedBox(height: 4),
              SelectableText(preview.host),
              const SizedBox(height: 12),
              Text('完全なURL', style: Theme.of(context).textTheme.labelLarge),
              const SizedBox(height: 4),
              SelectableText(preview.fullUrl),
              for (final warning in preview.warnings) ...[
                const SizedBox(height: 12),
                Semantics(
                  liveRegion: true,
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Icon(Icons.warning_amber_rounded, size: 20),
                      const SizedBox(width: 8),
                      Expanded(child: Text(warning)),
                    ],
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () =>
              Navigator.of(context).pop(_AssistantLinkAction.cancel),
          child: const Text('キャンセル'),
        ),
        TextButton.icon(
          onPressed: () => Navigator.of(context).pop(_AssistantLinkAction.copy),
          icon: const Icon(Icons.copy_outlined),
          label: const Text('リンクをコピー'),
        ),
        FilledButton.icon(
          onPressed: () => Navigator.of(context).pop(_AssistantLinkAction.open),
          icon: const Icon(Icons.open_in_new),
          label: const Text('開く'),
        ),
      ],
    );
  }
}

class _TypingIndicator extends StatefulWidget {
  const _TypingIndicator({required this.fg});
  final Color fg;

  @override
  State<_TypingIndicator> createState() => _TypingIndicatorState();
}

class _TypingIndicatorState extends State<_TypingIndicator>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1100),
    )..repeat();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: List.generate(3, (i) {
        return Padding(
          padding: const EdgeInsets.only(right: 4),
          child: FadeTransition(
            opacity: _controller.drive(
              Tween(
                begin: 0.3,
                end: 1.0,
              ).chain(CurveTween(curve: Interval(i * 0.2, 0.6 + i * 0.2))),
            ),
            child: Container(
              width: 7,
              height: 7,
              decoration: BoxDecoration(
                color: widget.fg.withValues(alpha: 0.7),
                shape: BoxShape.circle,
              ),
            ),
          ),
        );
      }),
    );
  }
}

Future<void> copyMessageContent(
  BuildContext context,
  ChatMessage message,
) async {
  await Clipboard.setData(ClipboardData(text: message.content));
  if (context.mounted) {
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('コピーしました'), duration: Duration(seconds: 1)),
    );
  }
}
