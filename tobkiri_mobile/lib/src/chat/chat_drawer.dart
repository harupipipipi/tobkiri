import 'package:flutter/material.dart';

import '../domain/space.dart';
import 'chat_models.dart';
import 'defaultspack_action_icon.dart';
import 'mobile_operation_failure.dart';

class ChatDrawer extends StatelessWidget {
  const ChatDrawer({
    super.key,
    required this.spaces,
    required this.activeSpaceId,
    required this.conversations,
    required this.activeId,
    required this.onNewChat,
    required this.onSelectSpace,
    required this.onSelect,
    required this.onDelete,
    required this.onRename,
    required this.onPin,
    required this.onOpenSettings,
    required this.onReconnectSpace,
    required this.onContinueOffline,
    this.pcConversations = const [],
    this.loadingPc = false,
    this.pcConversationFailure,
    this.onRetryPcConversations,
    this.onRepairPcConnection,
  });

  final List<Space> spaces;
  final String activeSpaceId;
  final List<Conversation> conversations;
  final List<PcConversationItem> pcConversations;
  final String? activeId;
  final VoidCallback onNewChat;
  final ValueChanged<String> onSelectSpace;
  final ValueChanged<String> onSelect;
  final ValueChanged<String> onDelete;
  final ValueChanged<String> onRename;
  final ValueChanged<String> onPin;
  final VoidCallback onOpenSettings;
  final VoidCallback onReconnectSpace;
  final VoidCallback onContinueOffline;
  final bool loadingPc;
  final MobileOperationFailure? pcConversationFailure;
  final VoidCallback? onRetryPcConversations;
  final VoidCallback? onRepairPcConnection;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final activeSpace = _activeSpace();

    return SafeArea(
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 12, 8),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    'Rumi',
                    style: theme.textTheme.titleLarge?.copyWith(
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                IconButton(
                  tooltip: '新規チャット',
                  icon: const DefaultspackActionIcon(
                    kind: DefaultspackActionIconKind.newChat,
                  ),
                  onPressed: onNewChat,
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 4, 12, 8),
            child: FilledButton.icon(
              onPressed: onNewChat,
              icon: const DefaultspackActionIcon(
                kind: DefaultspackActionIconKind.newChat,
                size: 18,
              ),
              label: const Text('新規チャット'),
              style: FilledButton.styleFrom(
                minimumSize: const Size.fromHeight(44),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
            ),
          ),
          Expanded(child: _buildConversationList(activeSpace, theme)),
          const Divider(height: 1),
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: Text(
                    '接続先',
                    style: theme.textTheme.labelSmall?.copyWith(
                      color: theme.colorScheme.onSurfaceVariant,
                    ),
                  ),
                ),
                const SizedBox(height: 6),
                _SpaceSelector(
                  spaces: spaces,
                  activeSpaceId: activeSpaceId,
                  onSelect: onSelectSpace,
                ),
                ListTile(
                  leading: const Icon(Icons.settings_outlined),
                  title: const Text('設定'),
                  onTap: onOpenSettings,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Space _activeSpace() {
    return spaces.firstWhere(
      (s) => s.id == activeSpaceId,
      orElse: () => Space.local,
    );
  }

  Widget _buildConversationList(Space space, ThemeData theme) {
    final conversationFailure = pcConversationFailure;
    if (space.isPc && conversationFailure != null) {
      return MobileOperationFailureView(
        failure: conversationFailure,
        onRetry: onRetryPcConversations ?? onReconnectSpace,
        onRepair: onRepairPcConnection,
        compact: true,
      );
    }
    if (space.isPc && space.isOffline) {
      return _OfflineSpaceView(
        space: space,
        onReconnect: onReconnectSpace,
        onContinue: onContinueOffline,
      );
    }

    if (space.isPc) {
      if (loadingPc) {
        return const Center(
          child: Padding(
            padding: EdgeInsets.all(24),
            child: CircularProgressIndicator(),
          ),
        );
      }
      if (pcConversations.isEmpty) {
        return const Padding(
          padding: EdgeInsets.all(24),
          child: Center(
            child: Text('PC会話がありません', style: TextStyle(color: Colors.grey)),
          ),
        );
      }
      final pinned = pcConversations.where((c) => c.pinned).toList();
      final others = pcConversations.where((c) => !c.pinned).toList();
      return ListView(
        padding: const EdgeInsets.symmetric(horizontal: 8),
        children: [
          if (pinned.isNotEmpty) ...[
            const _GroupHeader('ピン留め'),
            for (final c in pinned)
              _PcConversationTile(
                item: c,
                selected: c.id == activeId,
                onSelect: () => onSelect(c.id),
              ),
            const SizedBox(height: 8),
          ],
          const _GroupHeader('PC会話'),
          for (final c in others)
            _PcConversationTile(
              item: c,
              selected: c.id == activeId,
              onSelect: () => onSelect(c.id),
            ),
        ],
      );
    }

    final localPinned = conversations.where((c) => c.pinned).toList();
    final localOthers = conversations.where((c) => !c.pinned).toList();

    return ListView(
      padding: const EdgeInsets.symmetric(horizontal: 8),
      children: [
        if (localPinned.isNotEmpty) ...[
          const _GroupHeader('ピン留め'),
          for (final c in localPinned)
            _ConversationTile(
              conversation: c,
              selected: c.id == activeId,
              onSelect: () => onSelect(c.id),
              onDelete: () => onDelete(c.id),
              onRename: () => onRename(c.id),
              onPin: () => onPin(c.id),
            ),
          const SizedBox(height: 8),
        ],
        const _GroupHeader('チャット'),
        if (localOthers.isEmpty && localPinned.isEmpty)
          const Padding(
            padding: EdgeInsets.all(24),
            child: Center(
              child: Text('チャット履歴がありません', style: TextStyle(color: Colors.grey)),
            ),
          ),
        for (final c in localOthers)
          _ConversationTile(
            conversation: c,
            selected: c.id == activeId,
            onSelect: () => onSelect(c.id),
            onDelete: () => onDelete(c.id),
            onRename: () => onRename(c.id),
            onPin: () => onPin(c.id),
          ),
      ],
    );
  }
}

class PcConversationItem {
  const PcConversationItem({
    required this.id,
    required this.title,
    required this.messageCount,
    required this.updatedAt,
    required this.pinned,
    required this.preview,
  });

  final String id;
  final String title;
  final int messageCount;
  final DateTime updatedAt;
  final bool pinned;
  final String preview;
}

class _SpaceSelector extends StatelessWidget {
  const _SpaceSelector({
    required this.spaces,
    required this.activeSpaceId,
    required this.onSelect,
  });

  final List<Space> spaces;
  final String activeSpaceId;
  final ValueChanged<String> onSelect;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SizedBox(
      height: 56,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 12),
        itemCount: spaces.length,
        separatorBuilder: (_, __) => const SizedBox(width: 8),
        itemBuilder: (context, index) {
          final space = spaces[index];
          final isActive = space.id == activeSpaceId;
          final isOffline = space.isOffline;

          return GestureDetector(
            onTap: () => onSelect(space.id),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
              decoration: BoxDecoration(
                color: isActive
                    ? theme.colorScheme.primary.withValues(alpha: 0.15)
                    : theme.cardTheme.color,
                borderRadius: BorderRadius.circular(12),
                border: Border.all(
                  color: isActive
                      ? theme.colorScheme.primary.withValues(alpha: 0.4)
                      : theme.dividerColor,
                ),
              ),
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(
                        space.isLocal
                            ? Icons.phone_android
                            : Icons.desktop_windows,
                        size: 16,
                        color: isOffline
                            ? Colors.grey
                            : isActive
                                ? theme.colorScheme.primary
                                : theme.colorScheme.onSurfaceVariant,
                      ),
                      const SizedBox(width: 6),
                      ConstrainedBox(
                        constraints: const BoxConstraints(maxWidth: 132),
                        child: Text(
                          space.label,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: 12,
                            fontWeight:
                                isActive ? FontWeight.w600 : FontWeight.w400,
                            color: isOffline
                                ? Colors.grey
                                : isActive
                                    ? theme.colorScheme.primary
                                    : theme.colorScheme.onSurface,
                          ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 2),
                  Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Container(
                        width: 7,
                        height: 7,
                        decoration: BoxDecoration(
                          shape: BoxShape.circle,
                          color: isOffline
                              ? Colors.grey
                              : space.isLocal
                                  ? Colors.green
                                  : theme.colorScheme.primary,
                        ),
                      ),
                      const SizedBox(width: 5),
                      Text(
                        isOffline ? 'オフライン' : (space.isLocal ? 'スマホ' : 'オンライン'),
                        style: TextStyle(
                          fontSize: 10,
                          color: isOffline
                              ? Colors.grey
                              : theme.colorScheme.onSurfaceVariant,
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          );
        },
      ),
    );
  }
}

class _OfflineSpaceView extends StatelessWidget {
  const _OfflineSpaceView({
    required this.space,
    required this.onReconnect,
    required this.onContinue,
  });
  final Space space;
  final VoidCallback onReconnect;
  final VoidCallback onContinue;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.cloud_off, size: 48, color: Colors.grey.shade400),
            const SizedBox(height: 16),
            Text(
              '${space.label}との接続が切れました',
              style: Theme.of(context).textTheme.titleSmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              '履歴はこの端末にキャッシュされています。',
              style: Theme.of(context).textTheme.bodySmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 20),
            FilledButton.icon(
              onPressed: onReconnect,
              icon: const Icon(Icons.refresh),
              label: const Text('再接続する'),
            ),
            const SizedBox(height: 10),
            TextButton(
              onPressed: onContinue,
              child: const Text('この地点からスマホで続ける'),
            ),
          ],
        ),
      ),
    );
  }
}

class _GroupHeader extends StatelessWidget {
  const _GroupHeader(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
      child: Text(
        text,
        style: const TextStyle(
          fontSize: 12,
          fontWeight: FontWeight.w600,
          color: Colors.grey,
        ),
      ),
    );
  }
}

class _ConversationTile extends StatelessWidget {
  const _ConversationTile({
    required this.conversation,
    required this.selected,
    required this.onSelect,
    required this.onDelete,
    required this.onRename,
    required this.onPin,
  });

  final Conversation conversation;
  final bool selected;
  final VoidCallback onSelect;
  final VoidCallback onDelete;
  final VoidCallback onRename;
  final VoidCallback onPin;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Material(
      color: selected
          ? theme.colorScheme.primaryContainer.withValues(alpha: 0.5)
          : Colors.transparent,
      borderRadius: BorderRadius.circular(10),
      child: ListTile(
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        leading: Icon(
          conversation.pinned ? Icons.push_pin : Icons.chat_bubble_outline,
          size: 18,
          color: Colors.grey,
        ),
        title: Text(
          conversation.title,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Text(
          conversation.preview,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontSize: 12),
        ),
        trailing: PopupMenuButton<String>(
          icon: const Icon(Icons.more_horiz, size: 18),
          onSelected: (value) {
            switch (value) {
              case 'rename':
                onRename();
                break;
              case 'pin':
                onPin();
                break;
              case 'delete':
                onDelete();
                break;
            }
          },
          itemBuilder: (_) => [
            const PopupMenuItem(value: 'rename', child: Text('名前を変更')),
            PopupMenuItem(
              value: 'pin',
              child: Text(conversation.pinned ? 'ピン留め解除' : 'ピン留め'),
            ),
            const PopupMenuItem(
              value: 'delete',
              child: Text('削除', style: TextStyle(color: Colors.redAccent)),
            ),
          ],
        ),
        onTap: onSelect,
      ),
    );
  }
}

class _PcConversationTile extends StatelessWidget {
  const _PcConversationTile({
    required this.item,
    required this.selected,
    required this.onSelect,
  });

  final PcConversationItem item;
  final bool selected;
  final VoidCallback onSelect;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Material(
      color: selected
          ? theme.colorScheme.primaryContainer.withValues(alpha: 0.5)
          : Colors.transparent,
      borderRadius: BorderRadius.circular(10),
      child: ListTile(
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(10)),
        leading: Icon(
          item.pinned ? Icons.push_pin : Icons.cloud_outlined,
          size: 18,
          color: Colors.grey,
        ),
        title: Text(item.title, maxLines: 1, overflow: TextOverflow.ellipsis),
        subtitle: Text(
          item.preview,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontSize: 12),
        ),
        onTap: onSelect,
      ),
    );
  }
}
