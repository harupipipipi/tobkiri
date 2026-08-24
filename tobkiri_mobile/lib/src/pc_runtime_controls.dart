import 'dart:async';

import 'package:flutter/material.dart';

import 'pc_control_state.dart';

class PcRuntimeControlsPanel extends StatelessWidget {
  const PcRuntimeControlsPanel({required this.coordinator, super.key});

  final PcControlCoordinator coordinator;

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: coordinator,
      builder: (context, _) {
        final model = coordinator.state('model');
        final thinking = coordinator.state('thinking');
        final deepthink = coordinator.state('deepthink');
        return SafeArea(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      'PC runtime controls',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                  ),
                  IconButton(
                    tooltip: 'Refresh PC runtime controls',
                    onPressed: coordinator.refreshing
                        ? null
                        : () => unawaited(coordinator.refresh()),
                    icon: coordinator.refreshing
                        ? const SizedBox.square(
                            dimension: 20,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.refresh),
                  ),
                ],
              ),
              const SizedBox(height: 8),
              Text(
                'Current values come only from one authoritative PC snapshot. '
                'A requested value is never shown as active before settlement.',
                style: Theme.of(context).textTheme.bodySmall,
              ),
              const SizedBox(height: 12),
              _ControlCard(
                state: model,
                valueLabel: _valueLabel(model.confirmedValue),
                control: OutlinedButton.icon(
                  onPressed: () => _requestModel(context, model),
                  icon: const Icon(Icons.swap_horiz),
                  label: const Text('Request model'),
                ),
                onRetry: () => unawaited(coordinator.retry('model')),
              ),
              _ControlCard(
                state: thinking,
                valueLabel: _valueLabel(thinking.confirmedValue),
                control: Wrap(
                  spacing: 6,
                  runSpacing: 6,
                  children: [
                    for (final level in const [
                      'none',
                      'low',
                      'medium',
                      'high',
                      'xhigh',
                    ])
                      ChoiceChip(
                        label: Text(level),
                        selected: thinking.confirmedValue == level,
                        onSelected: (_) =>
                            unawaited(coordinator.request('thinking', level)),
                      ),
                  ],
                ),
                onRetry: () => unawaited(coordinator.retry('thinking')),
              ),
              _ControlCard(
                state: deepthink,
                valueLabel: _valueLabel(deepthink.confirmedValue),
                control: Semantics(
                  label: 'Request DeepThink on or off',
                  child: Switch(
                    value: deepthink.confirmedValue == true,
                    onChanged: (value) =>
                        unawaited(coordinator.request('deepthink', value)),
                  ),
                ),
                onRetry: () => unawaited(coordinator.retry('deepthink')),
              ),
              const _UnavailableControlCard(
                label: 'Mode',
                explanation:
                    'Mode is request-scoped in Command Protocol v1. There is '
                    'no single remote active value to claim as Current.',
              ),
              const _UnavailableControlCard(
                label: 'Tool authority',
                explanation:
                    'Tool eligibility and approvals remain PC-authoritative. '
                    'This client does not create a second Full Access state.',
              ),
            ],
          ),
        );
      },
    );
  }

  Future<void> _requestModel(BuildContext context, PcControlState state) async {
    var draft =
        state.confirmedValue is String ? state.confirmedValue! as String : '';
    final value = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Request PC model'),
        content: TextFormField(
          autofocus: true,
          initialValue: draft,
          decoration: const InputDecoration(
            labelText: 'Exact model or profile ID',
            helperText: 'The PC validates availability before activation.',
          ),
          textInputAction: TextInputAction.done,
          onChanged: (value) => draft = value,
          onFieldSubmitted: (value) {
            if (value.trim().isNotEmpty) {
              Navigator.of(context).pop(value.trim());
            }
          },
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              final value = draft.trim();
              if (value.isNotEmpty) {
                Navigator.of(context).pop(value);
              }
            },
            child: const Text('Request'),
          ),
        ],
      ),
    );
    if (value != null && context.mounted) {
      unawaited(coordinator.request('model', value));
    }
  }
}

class _ControlCard extends StatelessWidget {
  const _ControlCard({
    required this.state,
    required this.valueLabel,
    required this.control,
    required this.onRetry,
  });

  final PcControlState state;
  final String valueLabel;
  final Widget control;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    final pending =
        state.requestedValue == null ? null : _valueLabel(state.requestedValue);
    return Semantics(
      container: true,
      label:
          '${state.definition.label}, ${_phaseLabel(state.phase)}, current $valueLabel',
      child: Card(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      state.definition.label,
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                  ),
                  _StatusPill(phase: state.phase),
                ],
              ),
              const SizedBox(height: 8),
              Text('Current: $valueLabel'),
              if (pending != null) Text('Requested: $pending'),
              const SizedBox(height: 6),
              Text(
                state.feedback,
                key: ValueKey('${state.definition.id}-feedback'),
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: state.phase == PcControlPhase.failed
                          ? Theme.of(context).colorScheme.error
                          : null,
                    ),
              ),
              if (state.approvalRequired)
                const Padding(
                  padding: EdgeInsets.only(top: 6),
                  child: Text('Approval required · pending final settlement'),
                ),
              const SizedBox(height: 10),
              Row(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  Expanded(child: control),
                  if (state.retryable) ...[
                    const SizedBox(width: 8),
                    TextButton.icon(
                      key: ValueKey('${state.definition.id}-retry'),
                      onPressed: onRetry,
                      icon: const Icon(Icons.replay),
                      label: const Text('Retry'),
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _UnavailableControlCard extends StatelessWidget {
  const _UnavailableControlCard({
    required this.label,
    required this.explanation,
  });

  final String label;
  final String explanation;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      container: true,
      label: '$label, Unknown, no authoritative remote value',
      child: Card(
        child: ListTile(
          leading: const Icon(Icons.lock_outline),
          title: Text(label),
          subtitle: Text(explanation),
          trailing: const _StatusPill(phase: PcControlPhase.unknown),
        ),
      ),
    );
  }
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.phase});

  final PcControlPhase phase;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final (background, foreground) = switch (phase) {
      PcControlPhase.current => (
          const Color(0xFFDFF3E8),
          const Color(0xFF174E36),
        ),
      PcControlPhase.pending => (
          scheme.tertiaryContainer,
          scheme.onTertiaryContainer,
        ),
      PcControlPhase.failed => (scheme.errorContainer, scheme.onErrorContainer),
      PcControlPhase.unknown => (
          scheme.surfaceContainerHighest,
          scheme.onSurfaceVariant,
        ),
    };
    return Semantics(
      label: 'Status ${_phaseLabel(phase)}',
      child: DecoratedBox(
        decoration: BoxDecoration(
          color: background,
          borderRadius: BorderRadius.circular(999),
        ),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
          child: Text(
            _phaseLabel(phase),
            style: TextStyle(color: foreground, fontWeight: FontWeight.w600),
          ),
        ),
      ),
    );
  }
}

String _phaseLabel(PcControlPhase phase) => switch (phase) {
      PcControlPhase.current => 'Current',
      PcControlPhase.pending => 'Pending',
      PcControlPhase.failed => 'Failed',
      PcControlPhase.unknown => 'Unknown',
    };

String _valueLabel(Object? value) {
  if (value == null) {
    return 'Unknown';
  }
  if (value is bool) {
    return value ? 'On' : 'Off';
  }
  return '$value';
}
