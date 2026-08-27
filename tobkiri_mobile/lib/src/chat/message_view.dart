import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../accessibility/motion_policy.dart';
import '../app_theme.dart';
import '../platform/platform_services.dart';
import 'chat_models.dart';

class MessageView extends StatelessWidget {
  const MessageView({super.key, required this.message});

  final ChatMessage message;

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
      child: _MessageBody(message: message, fg: fg, colors: colors),
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
  });
  final ChatMessage message;
  final Color fg;
  final RumiColors colors;

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
        a: const TextStyle(color: Colors.lightBlueAccent),
      ),
      onTapLink: (text, href, title) async {
        if (href != null) {
          final uri = Uri.parse(href);
          await const PlatformUrlLauncher().open(uri);
        }
      },
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
    );
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (motionAllowedOf(context)) {
      if (!_controller.isAnimating) _controller.repeat();
    } else {
      _controller.stop();
      _controller.value = 1;
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final animate = motionAllowedOf(context);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: List.generate(3, (i) {
        return Padding(
          padding: const EdgeInsets.only(right: 4),
          child: animate
              ? FadeTransition(
                  opacity: _controller.drive(
                    Tween(begin: 0.3, end: 1.0).chain(
                      CurveTween(curve: Interval(i * 0.2, 0.6 + i * 0.2)),
                    ),
                  ),
                  child: _TypingDot(color: widget.fg),
                )
              : _TypingDot(color: widget.fg),
        );
      }),
    );
  }
}

class _TypingDot extends StatelessWidget {
  const _TypingDot({required this.color});

  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 7,
      height: 7,
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.7),
        shape: BoxShape.circle,
      ),
    );
  }
}

Future<void> copyMessageContent(
    BuildContext context, ChatMessage message) async {
  await Clipboard.setData(ClipboardData(text: message.content));
  if (context.mounted) {
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('コピーしました'), duration: Duration(seconds: 1)),
    );
  }
}
