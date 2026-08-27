import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../domain/chat_event.dart';

const _safeArgumentKeys = <String>{
  'action',
  'artifact',
  'file_path',
  'host',
  'operation',
  'output_path',
  'path',
  'resource',
  'target',
  'url',
};

String? safeToolActivityText(String? value, {int maxLength = 1600}) {
  if (value == null) return null;
  var safe = value.trim();
  if (safe.isEmpty) return null;
  safe = safe.replaceAll(
    RegExp(r'Bearer\s+[A-Za-z0-9._~+/-]+=*', caseSensitive: false),
    'Bearer [redacted]',
  );
  safe = safe.replaceAll(
    RegExp(r'\bsk-[A-Za-z0-9_-]{8,}\b', caseSensitive: false),
    '[redacted key]',
  );
  safe = safe.replaceAllMapped(
    RegExp(
      r'\b(api[_-]?key|authorization|password|secret|token)\b\s*[:=]\s*[^\s,;]+',
      caseSensitive: false,
    ),
    (match) => '${match.group(1)}=[redacted]',
  );
  if (safe.length > maxLength) {
    safe = '${safe.substring(0, maxLength)}…';
  }
  return safe;
}

Map<String, dynamic> safeToolActivityArguments(
  Map<String, dynamic> arguments,
) {
  final safe = <String, dynamic>{};
  for (final entry in arguments.entries) {
    if (!_safeArgumentKeys.contains(entry.key.toLowerCase())) continue;
    final value = entry.value;
    if (value is! String && value is! num && value is! bool) continue;
    var rendered = safeToolActivityText('$value', maxLength: 320);
    if (rendered == null) continue;
    if (entry.key.toLowerCase() == 'url') {
      final uri = Uri.tryParse(rendered);
      if (uri != null && uri.hasScheme) {
        rendered = Uri(
          scheme: uri.scheme,
          host: uri.host,
          port: uri.hasPort ? uri.port : null,
          path: uri.path,
        ).toString();
      }
    }
    safe[entry.key] = rendered;
  }
  return safe;
}

class ToolActivityCard extends StatefulWidget {
  const ToolActivityCard({
    super.key,
    required this.event,
    this.onStop,
  });

  final ToolCallEvent event;
  final VoidCallback? onStop;

  @override
  State<ToolActivityCard> createState() => _ToolActivityCardState();
}

class _ToolActivityCardState extends State<ToolActivityCard> {
  bool _expanded = false;

  ToolCallEvent get event => widget.event;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final state = _activityState(event.status);
    final statusColor = switch (state) {
      _ActivityState.failed => scheme.error,
      _ActivityState.complete => scheme.primary,
      _ActivityState.cancelled ||
      _ActivityState.interrupted =>
        scheme.onSurfaceVariant,
      _ => scheme.tertiary,
    };
    final action = _displayToolName(event.toolName);
    final target = _activityTarget(event.arguments);
    final summary = safeToolActivityText(event.summary);
    final consequence = _consequence(event, state);
    final statusLabel = _statusLabel(state);
    final semanticParts = <String>[
      action,
      statusLabel,
      if (target != null) '対象 $target',
      if (consequence != null) consequence,
      _timeSummary(event),
      _expanded ? '詳細を表示中' : '詳細は折りたたみ中',
    ];

    return Semantics(
      container: true,
      explicitChildNodes: true,
      liveRegion: true,
      label: semanticParts.join('。'),
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        decoration: BoxDecoration(
          color: scheme.surfaceContainerHighest.withValues(alpha: 0.5),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: statusColor.withValues(alpha: 0.45)),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Semantics(
              button: true,
              excludeSemantics: true,
              expanded: _expanded,
              label: '${_expanded ? '閉じる' : '開く'}: $action、$statusLabel',
              child: InkWell(
                borderRadius: BorderRadius.circular(12),
                onTap: () => setState(() => _expanded = !_expanded),
                child: ConstrainedBox(
                  constraints: const BoxConstraints(minHeight: 56),
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Row(
                      children: [
                        _ToolIcon(
                          toolName: event.toolName,
                          color: statusColor,
                        ),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Wrap(
                                spacing: 8,
                                runSpacing: 4,
                                crossAxisAlignment: WrapCrossAlignment.center,
                                children: [
                                  Text(
                                    action,
                                    style: theme.textTheme.bodyMedium?.copyWith(
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                  Text(
                                    statusLabel,
                                    style:
                                        theme.textTheme.labelMedium?.copyWith(
                                      color: statusColor,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                ],
                              ),
                              if (target != null)
                                Text(
                                  '対象: $target',
                                  style: theme.textTheme.bodySmall?.copyWith(
                                    color: scheme.onSurfaceVariant,
                                  ),
                                ),
                              if (summary != null)
                                Text(
                                  summary,
                                  style: theme.textTheme.bodySmall,
                                  maxLines: _expanded ? null : 2,
                                  overflow: _expanded
                                      ? TextOverflow.visible
                                      : TextOverflow.ellipsis,
                                ),
                            ],
                          ),
                        ),
                        const SizedBox(width: 8),
                        _StatusIcon(state: state, color: statusColor),
                        const SizedBox(width: 4),
                        Icon(
                          _expanded ? Icons.expand_less : Icons.expand_more,
                          color: scheme.onSurfaceVariant,
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
            if (MediaQuery.disableAnimationsOf(context))
              _expanded
                  ? _ActivityDetails(
                      event: event,
                      state: state,
                      target: target,
                      consequence: consequence,
                      onStop: widget.onStop,
                    )
                  : const SizedBox.shrink()
            else
              AnimatedSize(
                duration: const Duration(milliseconds: 180),
                alignment: Alignment.topCenter,
                child: _expanded
                    ? _ActivityDetails(
                        event: event,
                        state: state,
                        target: target,
                        consequence: consequence,
                        onStop: widget.onStop,
                      )
                    : const SizedBox.shrink(),
              ),
          ],
        ),
      ),
    );
  }
}

class _ActivityDetails extends StatelessWidget {
  const _ActivityDetails({
    required this.event,
    required this.state,
    required this.target,
    required this.consequence,
    required this.onStop,
  });

  final ToolCallEvent event;
  final _ActivityState state;
  final String? target;
  final String? consequence;
  final VoidCallback? onStop;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final scheme = theme.colorScheme;
    final safeArguments = safeToolActivityArguments(event.arguments);
    final summary = safeToolActivityText(event.summary);
    final error = safeToolActivityText(
      event.error ?? (state == _ActivityState.failed ? event.output : null),
      maxLength: 1200,
    );
    final output = safeToolActivityText(event.output, maxLength: 4000);
    final copyText = [summary, error, output].whereType<String>().join('\n\n');
    final canStop = state == _ActivityState.running && onStop != null;

    return DecoratedBox(
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: scheme.outlineVariant)),
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _DetailLine(label: '状態', value: _statusLabel(state)),
            if (target != null) _DetailLine(label: '対象', value: target!),
            _DetailLine(label: '時間', value: _timeSummary(event)),
            if (summary != null) ...[
              const SizedBox(height: 10),
              Text('説明', style: theme.textTheme.labelLarge),
              const SizedBox(height: 4),
              SelectableText(summary),
            ],
            if (safeArguments.isNotEmpty) ...[
              const SizedBox(height: 10),
              Text('入力コンテキスト', style: theme.textTheme.labelLarge),
              const SizedBox(height: 4),
              for (final entry in safeArguments.entries)
                _DetailLine(label: entry.key, value: '${entry.value}'),
            ],
            if (error != null) ...[
              const SizedBox(height: 10),
              Text(
                '安全な原因',
                style: theme.textTheme.labelLarge?.copyWith(
                  color: scheme.error,
                ),
              ),
              const SizedBox(height: 4),
              SelectableText(error),
              const SizedBox(height: 4),
              Text(
                '入力を確認し、必要なら内容を修正して会話から再実行を依頼してください。自動Retryは行いません。',
                style: theme.textTheme.bodySmall,
              ),
            ],
            if (output != null && output != error) ...[
              const SizedBox(height: 10),
              Text('結果 / 出力', style: theme.textTheme.labelLarge),
              const SizedBox(height: 4),
              SelectableText(output),
            ] else if (consequence != null && consequence != summary) ...[
              const SizedBox(height: 10),
              Text('結果', style: theme.textTheme.labelLarge),
              const SizedBox(height: 4),
              SelectableText(consequence!),
            ],
            if (copyText.isNotEmpty || canStop) ...[
              const SizedBox(height: 12),
              Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (copyText.isNotEmpty)
                    OutlinedButton.icon(
                      onPressed: () async {
                        await Clipboard.setData(ClipboardData(text: copyText));
                        if (context.mounted) {
                          ScaffoldMessenger.of(context).showSnackBar(
                            const SnackBar(content: Text('ツール詳細をコピーしました')),
                          );
                        }
                      },
                      icon: const Icon(Icons.copy, size: 18),
                      label: Text('${_displayToolName(event.toolName)}の詳細をコピー'),
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(48, 48),
                      ),
                    ),
                  if (copyText.isNotEmpty && canStop) const SizedBox(height: 8),
                  if (canStop)
                    FilledButton.tonalIcon(
                      onPressed: onStop,
                      icon: const Icon(Icons.stop_circle_outlined, size: 18),
                      label: Text('${_displayToolName(event.toolName)}を停止'),
                      style: FilledButton.styleFrom(
                        minimumSize: const Size(48, 48),
                      ),
                    ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _DetailLine extends StatelessWidget {
  const _DetailLine({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 64,
            child: Text(label, style: Theme.of(context).textTheme.labelMedium),
          ),
          Expanded(child: SelectableText(value)),
        ],
      ),
    );
  }
}

class _StatusIcon extends StatelessWidget {
  const _StatusIcon({required this.state, required this.color});

  final _ActivityState state;
  final Color color;

  @override
  Widget build(BuildContext context) {
    if (state == _ActivityState.running) {
      return ExcludeSemantics(
        child: SizedBox(
          width: 18,
          height: 18,
          child: CircularProgressIndicator(strokeWidth: 2, color: color),
        ),
      );
    }
    final icon = switch (state) {
      _ActivityState.complete => Icons.check_circle,
      _ActivityState.failed => Icons.error,
      _ActivityState.cancelRequested => Icons.pending,
      _ActivityState.cancelled => Icons.cancel,
      _ActivityState.interrupted => Icons.pause_circle,
      _ActivityState.approval => Icons.verified_user,
      _ => Icons.info,
    };
    return ExcludeSemantics(child: Icon(icon, size: 20, color: color));
  }
}

enum _ActivityState {
  running,
  complete,
  failed,
  cancelRequested,
  cancelled,
  interrupted,
  approval,
  unknown,
}

_ActivityState _activityState(String raw) {
  final status = raw.trim().toLowerCase();
  if (status == 'running' || status == 'started' || status == 'pending') {
    return _ActivityState.running;
  }
  if (status == 'complete' ||
      status == 'completed' ||
      status == 'success' ||
      status == 'succeeded') {
    return _ActivityState.complete;
  }
  if (status == 'failed' || status == 'error' || status == 'cancel_failed') {
    return _ActivityState.failed;
  }
  if (status == 'cancel_requested' || status == 'cancelling') {
    return _ActivityState.cancelRequested;
  }
  if (status == 'cancelled' || status == 'canceled' || status == 'stopped') {
    return _ActivityState.cancelled;
  }
  if (status == 'interrupted' || status == 'partial') {
    return _ActivityState.interrupted;
  }
  if (status.contains('approval')) return _ActivityState.approval;
  return _ActivityState.unknown;
}

String _statusLabel(_ActivityState state) => switch (state) {
      _ActivityState.running => '実行中',
      _ActivityState.complete => '完了',
      _ActivityState.failed => '失敗',
      _ActivityState.cancelRequested => '停止を要求中',
      _ActivityState.cancelled => '停止済み',
      _ActivityState.interrupted => '中断 / 一部完了',
      _ActivityState.approval => '承認待ち',
      _ActivityState.unknown => '状態不明',
    };

String _displayToolName(String name) {
  switch (name) {
    case 'terminal':
      return 'ターミナル操作';
    case 'browser':
      return 'ブラウザ操作';
    case 'file_read':
      return 'ファイル読み取り';
    case 'file_write':
      return 'ファイル書き込み';
    case 'computer':
      return 'コンピュータ操作';
    case 'calculator':
    case 'tool_calculator':
      return '計算';
    case 'todo':
      return 'タスク更新';
    default:
      return 'ツール操作';
  }
}

String? _activityTarget(Map<String, dynamic> arguments) {
  final safe = safeToolActivityArguments(arguments);
  for (final key in const [
    'target',
    'path',
    'file_path',
    'output_path',
    'url',
    'host',
    'resource',
    'artifact',
  ]) {
    final value = safe[key];
    if (value != null && '$value'.trim().isNotEmpty) return '$value';
  }
  final action = safe['action'] ?? safe['operation'];
  return action == null ? null : '$action';
}

String? _consequence(ToolCallEvent event, _ActivityState state) {
  if (state == _ActivityState.failed) {
    return safeToolActivityText(event.error ?? event.summary ?? event.output);
  }
  if (state == _ActivityState.complete || state == _ActivityState.interrupted) {
    return safeToolActivityText(event.summary ?? event.output);
  }
  return safeToolActivityText(event.summary);
}

String _timeSummary(ToolCallEvent event) {
  final parts = <String>[];
  if (event.startedAt != null) {
    parts.add('開始 ${_clock(event.startedAt!)}');
  } else {
    parts.add('開始時刻は未提供');
  }
  if (event.endedAt != null) parts.add('終了 ${_clock(event.endedAt!)}');
  final duration = event.duration ??
      (event.startedAt != null && event.endedAt != null
          ? event.endedAt!.difference(event.startedAt!)
          : null);
  if (duration != null) parts.add('所要 ${_durationLabel(duration)}');
  return parts.join(' ・ ');
}

String _clock(DateTime value) {
  final local = value.toLocal();
  return '${local.hour.toString().padLeft(2, '0')}:'
      '${local.minute.toString().padLeft(2, '0')}:'
      '${local.second.toString().padLeft(2, '0')}';
}

String _durationLabel(Duration value) {
  if (value.inSeconds < 1) return '${value.inMilliseconds}ミリ秒';
  if (value.inMinutes < 1) return '${value.inSeconds}秒';
  final seconds = value.inSeconds.remainder(60);
  return '${value.inMinutes}分${seconds.toString().padLeft(2, '0')}秒';
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
    return ExcludeSemantics(
      child: Container(
        width: 36,
        height: 36,
        decoration: BoxDecoration(
          color: color.withValues(alpha: 0.15),
          borderRadius: BorderRadius.circular(8),
        ),
        child: Icon(icon, size: 20, color: color),
      ),
    );
  }
}
