import 'package:flutter/material.dart';

import '../../domain/chat_event.dart';

class ToolActivityCard extends StatelessWidget {
  const ToolActivityCard({super.key, required this.event});

  final ToolCallEvent event;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final isRunning = event.status == 'running';
    final isComplete =
        event.status == 'complete' || event.status == 'completed';
    final isFailed = event.status == 'failed';

    final statusColor = isFailed
        ? scheme.error
        : isComplete
            ? scheme.primary
            : scheme.tertiary;

    return Container(
      margin: const EdgeInsets.symmetric(vertical: 4),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: scheme.surfaceContainerHighest.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: statusColor.withValues(alpha: 0.3)),
      ),
      child: Row(
        children: [
          _ToolIcon(toolName: event.toolName, color: statusColor),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  _displayToolName(event.toolName),
                  style: theme.textTheme.bodyMedium?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (event.summary != null && event.summary!.isNotEmpty)
                  Text(
                    event.summary!,
                    style: theme.textTheme.bodySmall,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          if (isRunning)
            SizedBox(
              width: 16,
              height: 16,
              child: CircularProgressIndicator(
                strokeWidth: 2,
                color: statusColor,
              ),
            )
          else
            Icon(
              isComplete ? Icons.check_circle : Icons.error,
              size: 18,
              color: statusColor,
            ),
        ],
      ),
    );
  }

  String _displayToolName(String name) {
    switch (name) {
      case 'terminal':
        return 'ターミナル';
      case 'browser':
        return 'ブラウザ';
      case 'file_read':
        return 'ファイル読み取り';
      case 'file_write':
        return 'ファイル書き込み';
      case 'computer':
        return 'コンピュータ';
      default:
        return name;
    }
  }
}

class _ToolIcon extends StatelessWidget {
  const _ToolIcon({required this.toolName, required this.color});
  final String toolName;
  final Color color;

  @override
  Widget build(BuildContext context) {
    final icon = switch (toolName) {
      'terminal' => Icons.terminal,
      'browser' => Icons.language,
      'file_read' || 'file_write' => Icons.description,
      'computer' => Icons.computer,
      _ => Icons.build,
    };
    return Container(
      width: 32,
      height: 32,
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Icon(icon, size: 18, color: color),
    );
  }
}
