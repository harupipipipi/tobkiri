import 'package:flutter/material.dart';

import 'chat_accessibility.dart';

class ComposerBar extends StatefulWidget {
  const ComposerBar({
    super.key,
    required this.onSend,
    required this.onStop,
    required this.busy,
    this.onAdd,
    this.hint = 'メッセージを入力...',
  });

  final ValueChanged<String> onSend;
  final VoidCallback onStop;
  final bool busy;
  final VoidCallback? onAdd;
  final String hint;

  @override
  State<ComposerBar> createState() => _ComposerBarState();
}

class _ComposerBarState extends State<ComposerBar> {
  final _controller = TextEditingController();
  final _focus = FocusNode();
  bool _hasText = false;

  @override
  void initState() {
    super.initState();
    _controller.addListener(() {
      final next = _controller.text.trim().isNotEmpty;
      if (next != _hasText) setState(() => _hasText = next);
    });
  }

  @override
  void dispose() {
    _controller.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _send() {
    final text = _controller.text.trim();
    if (text.isEmpty || widget.busy) return;
    widget.onSend(text);
    _controller.clear();
    _focus.requestFocus();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
        child: Container(
          decoration: BoxDecoration(
            color: theme.cardTheme.color,
            borderRadius: BorderRadius.circular(24),
            border: Border.all(
              color: theme.dividerTheme.color ?? Colors.transparent,
            ),
          ),
          padding: const EdgeInsets.fromLTRB(16, 4, 6, 4),
          child: FocusTraversalGroup(
            policy: OrderedTraversalPolicy(),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.end,
              children: [
                FocusTraversalOrder(
                  order: const NumericFocusOrder(1),
                  child: _ComposerActionButton(
                    key: const ValueKey('composer-add'),
                    label: TobkiriChatAccessibility.add,
                    hint: TobkiriChatAccessibility.addHint,
                    onPressed: widget.busy ? null : widget.onAdd,
                    icon: Icons.add_rounded,
                  ),
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: FocusTraversalOrder(
                    order: const NumericFocusOrder(2),
                    child: Semantics(
                      key: const ValueKey('composer-field'),
                      container: true,
                      textField: true,
                      label: TobkiriChatAccessibility.composer,
                      hint: TobkiriChatAccessibility.composerHint,
                      child: TextField(
                        controller: _controller,
                        focusNode: _focus,
                        minLines: 1,
                        maxLines: 6,
                        textInputAction: TextInputAction.newline,
                        keyboardType: TextInputType.multiline,
                        style: theme.textTheme.bodyMedium,
                        decoration: InputDecoration(
                          hintText: widget.hint,
                          border: InputBorder.none,
                          enabledBorder: InputBorder.none,
                          focusedBorder: InputBorder.none,
                          filled: false,
                          isDense: true,
                          contentPadding: const EdgeInsets.symmetric(
                            vertical: 10,
                          ),
                        ),
                        onTapOutside: (_) => _focus.unfocus(),
                        onSubmitted: (_) => _send(),
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 6),
                FocusTraversalOrder(
                  order: const NumericFocusOrder(3),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 150),
                    child: widget.busy
                        ? _StopButton(onStop: widget.onStop)
                        : _SendButton(enabled: _hasText, onSend: _send),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _SendButton extends StatelessWidget {
  const _SendButton({required this.enabled, required this.onSend});
  final bool enabled;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return _ComposerActionButton(
      key: const ValueKey('composer-send'),
      label: TobkiriChatAccessibility.send,
      hint: enabled
          ? TobkiriChatAccessibility.sendHint
          : TobkiriChatAccessibility.sendDisabledHint,
      onPressed: enabled ? onSend : null,
      icon: Icons.arrow_upward_rounded,
      backgroundColor:
          enabled ? theme.colorScheme.primary : theme.disabledColor,
      foregroundColor: enabled
          ? theme.colorScheme.onPrimary
          : theme.colorScheme.onSurface.withValues(alpha: 0.4),
      shape: const CircleBorder(),
    );
  }
}

class _StopButton extends StatelessWidget {
  const _StopButton({required this.onStop});
  final VoidCallback onStop;

  @override
  Widget build(BuildContext context) {
    return _ComposerActionButton(
      key: const ValueKey('composer-stop'),
      label: TobkiriChatAccessibility.stop,
      hint: TobkiriChatAccessibility.stopHint,
      onPressed: onStop,
      icon: Icons.stop_rounded,
      backgroundColor: Colors.redAccent,
      foregroundColor: Colors.white,
      shape: const CircleBorder(),
    );
  }
}

class _ComposerActionButton extends StatelessWidget {
  const _ComposerActionButton({
    super.key,
    required this.label,
    required this.hint,
    required this.onPressed,
    required this.icon,
    this.backgroundColor,
    this.foregroundColor,
    this.shape,
  });

  final String label;
  final String hint;
  final VoidCallback? onPressed;
  final IconData icon;
  final Color? backgroundColor;
  final Color? foregroundColor;
  final OutlinedBorder? shape;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      container: true,
      button: true,
      enabled: onPressed != null,
      label: label,
      hint: hint,
      onTap: onPressed,
      child: ExcludeSemantics(
        child: Tooltip(
          message: label,
          child: SizedBox.square(
            dimension: 48,
            child: IconButton(
              onPressed: onPressed,
              icon: Icon(icon),
              style: IconButton.styleFrom(
                backgroundColor: backgroundColor,
                foregroundColor: foregroundColor,
                shape: shape,
                padding: EdgeInsets.zero,
                minimumSize: const Size.square(48),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
