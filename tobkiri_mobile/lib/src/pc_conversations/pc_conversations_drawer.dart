import 'package:flutter/material.dart';

import '../models.dart';
import 'pc_conversation_format.dart';
import 'pc_conversations_controller.dart';

/// A read-only navigation drawer for conversations owned by the PC.
///
/// The drawer deliberately accepts a controller rather than a client.  This
/// keeps transport errors and the last successful projection outside the
/// presentation layer, and lets the same cached list survive a rebuild of
/// the remote home screen.
class PcConversationsDrawer extends StatefulWidget {
  const PcConversationsDrawer({
    super.key,
    required this.controller,
    this.activeConversationId,
    this.onConversationSelected,
    this.clock,
  });

  final PcConversationsController controller;
  final String? activeConversationId;
  final ValueChanged<PcConversation>? onConversationSelected;
  final DateTime Function()? clock;

  @override
  State<PcConversationsDrawer> createState() => _PcConversationsDrawerState();
}

class _PcConversationsDrawerState extends State<PcConversationsDrawer> {
  late final TextEditingController _searchController;
  late final FocusNode _searchFocusNode;
  late final ScrollController _scrollController;

  @override
  void initState() {
    super.initState();
    _searchController = TextEditingController();
    _searchFocusNode = FocusNode(debugLabel: 'PC conversation search');
    _scrollController = ScrollController();
    widget.controller.addListener(_controllerChanged);
  }

  @override
  void didUpdateWidget(covariant PcConversationsDrawer oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller == widget.controller) {
      return;
    }
    oldWidget.controller.removeListener(_controllerChanged);
    widget.controller.addListener(_controllerChanged);
  }

  @override
  void dispose() {
    widget.controller.removeListener(_controllerChanged);
    _searchController.dispose();
    _searchFocusNode.dispose();
    _scrollController.dispose();
    super.dispose();
  }

  void _controllerChanged() {
    if (mounted) {
      setState(() {});
    }
  }

  Future<void> _refresh() async {
    await widget.controller.refresh();
  }

  @override
  Widget build(BuildContext context) {
    final all = widget.controller.conversations;
    final query = _searchController.text.trim().toLowerCase();
    final visible = query.isEmpty
        ? all
        : all
            .where(
              (conversation) =>
                  conversation.displayTitle.toLowerCase().contains(query) ||
                  conversation.safePreview.toLowerCase().contains(query),
            )
            .toList(growable: false);
    final groups = _groupConversations(visible);
    final entries = _buildEntries(groups);

    return Drawer(
      child: SafeArea(
        child: CustomScrollView(
          controller: _scrollController,
          slivers: [
            SliverToBoxAdapter(
              child: Column(
                children: [
                  _DrawerHeader(
                    loading: widget.controller.loading,
                    onRefresh: widget.controller.loading ? null : _refresh,
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                    child: Text(
                      _localized(
                        context,
                        en: 'Read-only PC conversations. Open a conversation on the '
                            'PC to continue.',
                        ja: 'PC上の会話を表示します（読み取り専用）。続けるにはPCで会話を開いてください。',
                      ),
                      semanticsLabel: _localized(
                        context,
                        en: 'Read-only PC conversations. Open a conversation on the '
                            'PC to continue.',
                        ja: 'PC上の会話を表示します。読み取り専用です。続けるにはPCで会話を開いてください。',
                      ),
                      style: Theme.of(context).textTheme.bodySmall,
                    ),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                    child: TextField(
                      controller: _searchController,
                      focusNode: _searchFocusNode,
                      textInputAction: TextInputAction.search,
                      autocorrect: false,
                      enableSuggestions: false,
                      decoration: InputDecoration(
                        labelText: _localized(context, en: 'Search', ja: '検索'),
                        hintText: _localized(
                          context,
                          en: 'Title or preview',
                          ja: 'タイトルまたはプレビュー',
                        ),
                        prefixIcon: const Icon(Icons.search),
                        suffixIcon: _searchController.text.isEmpty
                            ? null
                            : IconButton(
                                tooltip:
                                    _localized(context, en: 'Clear', ja: 'クリア'),
                                icon: const Icon(Icons.clear),
                                onPressed: () {
                                  _searchController.clear();
                                  setState(() {});
                                },
                              ),
                      ),
                      onChanged: (_) => setState(() {}),
                    ),
                  ),
                  if (widget.controller.offline || widget.controller.stale)
                    _OfflineBanner(
                      offline: widget.controller.offline,
                      stale: widget.controller.stale,
                    ),
                ],
              ),
            ),
            if (entries.isEmpty)
              SliverFillRemaining(
                hasScrollBody: false,
                child: _EmptyConversations(
                  hasSearch: query.isNotEmpty,
                  offline: widget.controller.offline,
                ),
              )
            else
              SliverPadding(
                padding: const EdgeInsets.fromLTRB(12, 4, 12, 20),
                sliver: SliverList(
                  delegate: SliverChildBuilderDelegate(
                    (context, index) {
                      final entry = entries[index];
                      if (entry.section != null) {
                        return _SectionHeader(label: entry.section!);
                      }
                      final conversation = entry.conversation!;
                      return _ConversationTile(
                        conversation: conversation,
                        duplicateTitle: _isDuplicateTitle(
                          visible,
                          conversation,
                        ),
                        active: conversation.id == widget.activeConversationId,
                        clock: widget.clock,
                        onTap: widget.onConversationSelected == null
                            ? null
                            : () =>
                                widget.onConversationSelected!(conversation),
                      );
                    },
                    childCount: entries.length,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Map<PcConversationSection, List<PcConversation>> _groupConversations(
    List<PcConversation> conversations,
  ) {
    final now = (widget.clock ?? DateTime.now).call();
    final groups = <PcConversationSection, List<PcConversation>>{
      for (final section in PcConversationSection.values) section: [],
    };
    for (final conversation in conversations) {
      groups[classifyPcConversation(conversation, now: now)]!.add(conversation);
    }
    for (final list in groups.values) {
      list.sort(_compareConversations);
    }
    return groups;
  }

  int _compareConversations(PcConversation first, PcConversation second) {
    if (first.updatedAt == null && second.updatedAt == null) {
      return first.displayTitle.toLowerCase().compareTo(
            second.displayTitle.toLowerCase(),
          );
    }
    if (first.updatedAt == null) {
      return 1;
    }
    if (second.updatedAt == null) {
      return -1;
    }
    final dateComparison = second.updatedAt!.compareTo(first.updatedAt!);
    if (dateComparison != 0) {
      return dateComparison;
    }
    return first.displayTitle.toLowerCase().compareTo(
          second.displayTitle.toLowerCase(),
        );
  }

  List<_DrawerEntry> _buildEntries(
    Map<PcConversationSection, List<PcConversation>> groups,
  ) {
    final entries = <_DrawerEntry>[];
    for (final section in PcConversationSection.values) {
      final conversations = groups[section]!;
      if (conversations.isEmpty) {
        continue;
      }
      entries.add(
        _DrawerEntry.section(
          pcConversationSectionLabel(section, Localizations.localeOf(context)),
        ),
      );
      entries.addAll(conversations.map(_DrawerEntry.conversation));
    }
    return entries;
  }

  bool _isDuplicateTitle(
    List<PcConversation> visible,
    PcConversation conversation,
  ) {
    final matching = visible.where(
      (item) => item.normalizedTitle == conversation.normalizedTitle,
    );
    return matching.length > 1;
  }
}

class _DrawerHeader extends StatelessWidget {
  const _DrawerHeader({required this.loading, required this.onRefresh});

  final bool loading;
  final VoidCallback? onRefresh;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 8, 8),
      child: Row(
        children: [
          const Icon(Icons.forum_outlined),
          const SizedBox(width: 12),
          Expanded(
            child: Text(
              _localized(context, en: 'PC conversations', ja: 'PCの会話'),
              style: Theme.of(context).textTheme.titleLarge,
            ),
          ),
          IconButton(
            tooltip: _localized(
              context,
              en: 'Refresh conversations',
              ja: '会話を更新',
            ),
            icon: loading
                ? const SizedBox.square(
                    dimension: 20,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.refresh),
            onPressed: onRefresh,
          ),
        ],
      ),
    );
  }
}

class _OfflineBanner extends StatelessWidget {
  const _OfflineBanner({required this.offline, required this.stale});

  final bool offline;
  final bool stale;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final hasCache = stale;
    final label = hasCache
        ? _localized(
            context,
            en: 'Offline — showing cached conversations',
            ja: 'オフライン — キャッシュ済みの会話を表示中',
          )
        : _localized(
            context,
            en: 'Offline — conversations unavailable',
            ja: 'オフライン — 会話を取得できません',
          );
    return Semantics(
      container: true,
      label: label,
      child: Container(
        width: double.infinity,
        margin: const EdgeInsets.fromLTRB(12, 0, 12, 8),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: scheme.errorContainer,
          borderRadius: BorderRadius.circular(8),
        ),
        child: Row(
          children: [
            Icon(
              offline ? Icons.cloud_off_outlined : Icons.sync_problem_outlined,
              size: 18,
              color: scheme.onErrorContainer,
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                label,
                style: TextStyle(color: scheme.onErrorContainer),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _SectionHeader extends StatelessWidget {
  const _SectionHeader({required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 12, 4, 6),
      child: Text(
        label,
        style: Theme.of(context).textTheme.labelLarge?.copyWith(
              color: Theme.of(context).colorScheme.primary,
              fontWeight: FontWeight.w700,
            ),
      ),
    );
  }
}

class _ConversationTile extends StatelessWidget {
  const _ConversationTile({
    required this.conversation,
    required this.duplicateTitle,
    required this.active,
    required this.clock,
    required this.onTap,
  });

  final PcConversation conversation;
  final bool duplicateTitle;
  final bool active;
  final DateTime Function()? clock;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final title = duplicateTitle
        ? '${conversation.displayTitle} · ${_shortConversationId(conversation.id)}'
        : conversation.displayTitle;
    final count = formatPcConversationCount(context, conversation.messageCount);
    final recency = formatPcConversationRecency(
      context,
      conversation.updatedAt,
      now: (clock ?? DateTime.now).call(),
    );
    final preview = conversation.safePreview;
    final metadata = '$recency · $count';
    final stateLabel = [
      if (active) _localized(context, en: 'active', ja: '選択中'),
      if (conversation.pinned) _localized(context, en: 'pinned', ja: 'ピン留め'),
    ].join(', ');
    final semanticLabel = [
      title,
      if (preview.isNotEmpty) preview,
      metadata,
      if (stateLabel.isNotEmpty) stateLabel,
      _localized(context, en: 'read-only', ja: '読み取り専用'),
    ].join('. ');

    return Semantics(
      button: onTap != null,
      container: true,
      selected: active,
      label: semanticLabel,
      child: Card(
        color: active ? scheme.secondaryContainer : null,
        margin: const EdgeInsets.only(bottom: 6),
        child: ListTile(
          contentPadding: const EdgeInsets.symmetric(
            horizontal: 12,
            vertical: 4,
          ),
          selected: active,
          leading: Icon(
            active
                ? Icons.radio_button_checked
                : conversation.pinned
                    ? Icons.push_pin_outlined
                    : Icons.chat_bubble_outline,
            color: active ? scheme.primary : scheme.onSurfaceVariant,
            semanticLabel: active
                ? _localized(context, en: 'Active', ja: '選択中')
                : conversation.pinned
                    ? _localized(context, en: 'Pinned', ja: 'ピン留め')
                    : null,
          ),
          title: Text(title, maxLines: 2, overflow: TextOverflow.ellipsis),
          subtitle: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (stateLabel.isNotEmpty)
                Text(
                  stateLabel,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    color: active ? scheme.primary : scheme.onSurfaceVariant,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              Text(
                _localized(context, en: 'Read-only', ja: '読み取り専用'),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.labelSmall,
              ),
              if (preview.isNotEmpty)
                Text(preview, maxLines: 2, overflow: TextOverflow.ellipsis),
              Text(metadata, maxLines: 2, overflow: TextOverflow.ellipsis),
            ],
          ),
          trailing: Icon(
            Icons.lock_outline,
            size: 18,
            semanticLabel: _localized(context, en: 'Read-only', ja: '読み取り専用'),
          ),
          onTap: onTap,
        ),
      ),
    );
  }
}

class _EmptyConversations extends StatelessWidget {
  const _EmptyConversations({required this.hasSearch, required this.offline});

  final bool hasSearch;
  final bool offline;

  @override
  Widget build(BuildContext context) {
    final label = hasSearch
        ? _localized(
            context,
            en: 'No conversations match your search',
            ja: '検索に一致する会話はありません',
          )
        : offline
            ? _localized(
                context,
                en: 'No cached conversations',
                ja: 'キャッシュ済みの会話はありません',
              )
            : _localized(
                context,
                en: 'No PC conversations yet',
                ja: 'PCの会話はまだありません',
              );
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.forum_outlined,
              size: 36,
              color: Theme.of(context).colorScheme.outline,
            ),
            const SizedBox(height: 12),
            Text(label, textAlign: TextAlign.center),
          ],
        ),
      ),
    );
  }
}

class _DrawerEntry {
  const _DrawerEntry._({this.section, this.conversation});

  const _DrawerEntry.section(String label) : this._(section: label);

  const _DrawerEntry.conversation(PcConversation conversation)
      : this._(conversation: conversation);

  final String? section;
  final PcConversation? conversation;
}

String _shortConversationId(String id) {
  final normalized = id.trim();
  if (normalized.length <= 12) {
    return normalized;
  }
  return '${normalized.substring(0, 8)}…';
}

String _localized(
  BuildContext context, {
  required String en,
  required String ja,
}) {
  return Localizations.localeOf(context).languageCode == 'ja' ? ja : en;
}
