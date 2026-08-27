import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../app_theme.dart';
import '../platform/platform_services.dart';
import 'chat_accessibility.dart';
import 'chat_models.dart';

typedef MessageLinkOpener = Future<void> Function(Uri uri);

class MessageView extends StatefulWidget {
  const MessageView({super.key, required this.message, this.openLink});

  final ChatMessage message;
  final MessageLinkOpener? openLink;

  @override
  State<MessageView> createState() => _MessageViewState();
}

class _MessageViewState extends State<MessageView> {
  bool _showCopy = false;

  void _revealCopy() {
    if (widget.message.content.trim().isEmpty || _showCopy) return;
    setState(() => _showCopy = true);
  }

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.message.content));
    if (!mounted) return;
    setState(() => _showCopy = false);
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text(TobkiriChatAccessibility.copied),
        duration: Duration(seconds: 1),
      ),
    );
  }

  Future<void> _openLink(Uri uri) async {
    final opener = widget.openLink;
    if (opener != null) {
      await opener(uri);
      return;
    }
    await const PlatformUrlLauncher().open(uri);
  }

  @override
  Widget build(BuildContext context) {
    final message = widget.message;
    final theme = Theme.of(context);
    final colors = theme.extension<RumiColors>() ?? RumiColors.dark;
    final isUser = message.role == ChatRole.user;
    final content = message.content.trim();
    final links = _extractSafeLinks(content);
    final customActions = content.isEmpty
        ? null
        : <CustomSemanticsAction, VoidCallback>{
            const CustomSemanticsAction(
              label: TobkiriChatAccessibility.copy,
            ): () => unawaited(_copy()),
          };
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
        onOpenLink: _openLink,
      ),
    );

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      child: Column(
        crossAxisAlignment:
            isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
        children: [
          Semantics(
            key: ValueKey('message-semantics:${message.id}'),
            container: true,
            label: TobkiriChatAccessibility.messageLabel(message),
            hint: content.isEmpty ? null : TobkiriChatAccessibility.copyHint,
            customSemanticsActions: customActions,
            child: Focus(
              canRequestFocus: content.isNotEmpty,
              onFocusChange: (focused) {
                if (focused) _revealCopy();
              },
              child: GestureDetector(
                behavior: HitTestBehavior.opaque,
                onLongPress: content.isEmpty ? null : _revealCopy,
                child: ExcludeSemantics(child: bubble),
              ),
            ),
          ),
          if (_showCopy || links.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Wrap(
                alignment: isUser ? WrapAlignment.end : WrapAlignment.start,
                spacing: 4,
                runSpacing: 4,
                children: [
                  if (_showCopy)
                    _MessageAction(
                      key: const ValueKey('message-copy'),
                      label: TobkiriChatAccessibility.copy,
                      hint: TobkiriChatAccessibility.copyHint,
                      icon: Icons.copy_outlined,
                      onPressed: () => unawaited(_copy()),
                    ),
                  for (final link in links)
                    _MessageAction(
                      key: ValueKey('message-link:$link'),
                      label: TobkiriChatAccessibility.link(link),
                      hint: TobkiriChatAccessibility.openLinkHint,
                      icon: Icons.open_in_new,
                      link: true,
                      onPressed: () => unawaited(_openLink(link)),
                    ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _MessageBody extends StatelessWidget {
  const _MessageBody({
    required this.message,
    required this.fg,
    required this.colors,
    required this.onOpenLink,
  });
  final ChatMessage message;
  final Color fg;
  final RumiColors colors;
  final MessageLinkOpener onOpenLink;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final content = message.content.trim();
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (message.error)
          _StatusRow(
            icon: Icons.error_outline,
            label: TobkiriChatAccessibility.failed,
            color: fg,
          ),
        if (message.error && (message.pending || content.isNotEmpty))
          const SizedBox(height: 8),
        if (message.pending)
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                '${TobkiriChatAccessibility.processing}...',
                style: TextStyle(
                  color: fg,
                  fontSize: 14,
                  height: 1.4,
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(width: 10),
              _TypingIndicator(fg: fg),
            ],
          ),
        if (message.pending && content.isNotEmpty) const SizedBox(height: 8),
        if (content.isEmpty && !message.pending)
          Text(TobkiriChatAccessibility.noContent, style: TextStyle(color: fg)),
        if (content.isNotEmpty)
          MarkdownBody(
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
              final uri = href == null ? null : Uri.tryParse(href);
              if (uri != null && _isSafeLink(uri)) {
                await onOpenLink(uri);
              }
            },
          ),
      ],
    );
  }
}

class _StatusRow extends StatelessWidget {
  const _StatusRow({
    required this.icon,
    required this.label,
    required this.color,
  });

  final IconData icon;
  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 18, color: color),
        const SizedBox(width: 6),
        Text(
          label,
          style: TextStyle(color: color, fontWeight: FontWeight.w600),
        ),
      ],
    );
  }
}

class _MessageAction extends StatelessWidget {
  const _MessageAction({
    super.key,
    required this.label,
    required this.hint,
    required this.icon,
    required this.onPressed,
    this.link = false,
  });

  final String label;
  final String hint;
  final IconData icon;
  final VoidCallback onPressed;
  final bool link;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      container: true,
      button: true,
      link: link,
      label: label,
      hint: hint,
      onTap: onPressed,
      child: ExcludeSemantics(
        child: SizedBox.square(
          dimension: 48,
          child: IconButton(
            tooltip: label,
            onPressed: onPressed,
            icon: Icon(icon),
          ),
        ),
      ),
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

List<Uri> _extractSafeLinks(String value) {
  final links = <Uri>[];
  final seen = <String>{};
  final pattern = RegExp(r'https?://[^\s\]\)<>]+');
  for (final match in pattern.allMatches(value)) {
    final raw = match.group(0)?.replaceFirst(RegExp(r'[.,;:!?]+$'), '') ?? '';
    final uri = Uri.tryParse(raw);
    if (uri != null && _isSafeLink(uri) && seen.add(uri.toString())) {
      links.add(uri);
    }
  }
  return links;
}

bool _isSafeLink(Uri uri) =>
    uri.hasAuthority && (uri.scheme == 'https' || uri.scheme == 'http');
