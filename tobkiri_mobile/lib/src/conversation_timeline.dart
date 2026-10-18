import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';

/// A stable entry in a [ConversationTimeline].
///
/// The [id] must remain stable for the lifetime of the entry.  When the
/// contents of an entry change while it is already in the list, increment
/// [revision].  That lets the timeline distinguish a streamed token or tool
/// update from a rebuild that did not change the conversation.
class ConversationTimelineItem {
  /// Creates a timeline entry.
  const ConversationTimelineItem({
    required this.id,
    required this.child,
    this.revision = 0,
    this.isActivity = false,
  });

  /// Stable identity for this entry.
  final String id;

  /// Widget rendered for this entry.
  final Widget child;

  /// Monotonically increasing content revision for streamed updates.
  final int revision;

  /// Whether this entry represents tool/status activity rather than a chat
  /// message.  Activity entries use the same unread affordance as messages.
  final bool isActivity;
}

/// Controller for imperative timeline actions and read-state observation.
///
/// A controller may be shared with a composer or event stream.  Calling
/// [jumpToLatest] is equivalent to tapping the visible `最新へ` control.  The
/// controller is not disposed by [ConversationTimeline]; its owner remains
/// responsible for disposing it.
class ConversationTimelineController extends ChangeNotifier {
  _ConversationTimelineState? _state;
  bool _isFollowing = true;
  int _unreadCount = 0;

  /// Whether new timeline content is currently being followed.
  bool get isFollowing => _isFollowing;

  /// Number of message or activity updates received while following was
  /// paused.
  int get unreadCount => _unreadCount;

  /// Returns to the newest entry.
  ///
  /// Set [animate] to false for an immediate return.  The timeline also
  /// disables the animation automatically when reduced motion is enabled by
  /// the platform accessibility settings.
  void jumpToLatest({bool animate = true}) {
    _state?._returnToLatest(animate: animate);
  }

  void _attach(_ConversationTimelineState state) {
    _state = state;
    _publish(state.isFollowing, state.unreadCount, notify: false);
  }

  void _detach(_ConversationTimelineState state) {
    if (identical(_state, state)) {
      _state = null;
    }
  }

  void _publish(bool following, int unread, {required bool notify}) {
    final changed = following != _isFollowing || unread != _unreadCount;
    _isFollowing = following;
    _unreadCount = unread;
    if (changed && notify) {
      notifyListeners();
    }
  }
}

/// A scrollable conversation surface that follows only while the reader is
/// near the newest entry.
///
/// The default [nearBottomThreshold] is 96 logical pixels.  A viewport whose
/// distance from [ScrollPosition.maxScrollExtent] is at most that threshold is
/// considered near the bottom.  A user gesture towards the older entries
/// pauses following immediately, even if the gesture has not yet crossed the
/// threshold.  Following resumes only when the reader manually reaches the
/// threshold again or explicitly taps `最新へ`/calls
/// [ConversationTimelineController.jumpToLatest].
///
/// Entries prepended at the beginning are treated as history loads and do not
/// increase the unread count.  Give entries stable IDs and advance [revision]
/// for streamed content or tool activity updates.  The widget keeps the first
/// visible entry's viewport offset stable while those updates, rotation, or a
/// keyboard resize change layout.
class ConversationTimeline extends StatefulWidget {
  /// Creates a conversation timeline.
  const ConversationTimeline({
    super.key,
    required this.items,
    this.controller,
    this.onLoadOlder,
    this.hasOlder = false,
    this.isLoadingOlder = false,
    this.nearBottomThreshold = 96,
    this.padding = EdgeInsets.zero,
    this.physics,
    this.latestLabel = '最新へ',
    this.activityLabel = '新しいアクティビティ',
    this.reducedMotion,
  }) : assert(nearBottomThreshold >= 0);

  /// Entries in chronological order, oldest first.
  final List<ConversationTimelineItem> items;

  /// Optional imperative/read-state controller.
  final ConversationTimelineController? controller;

  /// Called once when the reader reaches the top and older history is
  /// available.  The callback should update [items] by prepending entries.
  final Future<void> Function()? onLoadOlder;

  /// Whether older entries can currently be loaded.
  final bool hasOlder;

  /// Whether the owner is currently loading older entries.
  final bool isLoadingOlder;

  /// Distance in logical pixels from the bottom that still counts as
  /// following.  Defaults to 96 logical pixels.
  final double nearBottomThreshold;

  /// Padding around the timeline content.
  final EdgeInsetsGeometry padding;

  /// Optional physics for the underlying scroll view.
  final ScrollPhysics? physics;

  /// Visible label for the latest-message affordance.
  final String latestLabel;

  /// Activity text used when there is no numeric unread count.
  final String activityLabel;

  /// Overrides platform reduced-motion detection when non-null.
  final bool? reducedMotion;

  @override
  State<ConversationTimeline> createState() => _ConversationTimelineState();
}

class _AnchorSnapshot {
  const _AnchorSnapshot({required this.id, required this.viewportTop});

  final String id;
  final double viewportTop;
}

class _ConversationTimelineState extends State<ConversationTimeline> {
  final ScrollController _scrollController = ScrollController();
  final GlobalKey _viewportKey = GlobalKey();
  final Map<String, GlobalKey> _itemKeys = <String, GlobalKey>{};

  _AnchorSnapshot? _anchorSnapshot;
  bool _dependenciesInitialized = false;
  bool _hasLaidOut = false;
  bool _layoutWorkScheduled = false;
  bool _programmaticScroll = false;
  bool _loadRequestPending = false;
  bool _isFollowing = true;
  int _unreadCount = 0;
  double _lastPixels = 0;

  bool get isFollowing => _isFollowing;

  int get unreadCount => _unreadCount;

  @override
  void initState() {
    super.initState();
    assert(
      _hasUniqueIds(widget.items),
      'ConversationTimeline item IDs must be unique',
    );
    widget.controller?._attach(this);
    _scrollController.addListener(_rememberPixels);
    _scheduleLayoutWork();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();

    // Reading MediaQuery here makes rotation and keyboard viewport changes
    // observable even when reduced motion is explicitly overridden.
    MediaQuery.maybeOf(context);
    if (!_dependenciesInitialized) {
      _dependenciesInitialized = true;
      return;
    }

    _captureAnchor();
    _scheduleLayoutWork();
  }

  @override
  void didUpdateWidget(covariant ConversationTimeline oldWidget) {
    _captureAnchor();
    super.didUpdateWidget(oldWidget);

    if (oldWidget.controller != widget.controller) {
      oldWidget.controller?._detach(this);
      widget.controller?._attach(this);
    }

    final activityCount = _activityCount(oldWidget.items, widget.items);
    if (!_isFollowing && activityCount > 0) {
      _setUnreadCount(_unreadCount + activityCount);
    }

    final currentIds = widget.items.map((item) => item.id).toSet();
    _itemKeys.removeWhere((id, _) => !currentIds.contains(id));
    _scheduleLayoutWork();
  }

  @override
  void dispose() {
    widget.controller?._detach(this);
    _scrollController
      ..removeListener(_rememberPixels)
      ..dispose();
    super.dispose();
  }

  void _rememberPixels() {
    if (_scrollController.hasClients) {
      _lastPixels = _scrollController.position.pixels;
    }
  }

  int _activityCount(
    List<ConversationTimelineItem> oldItems,
    List<ConversationTimelineItem> newItems,
  ) {
    if (oldItems.isEmpty) {
      return newItems.isEmpty ? 0 : newItems.length;
    }

    final oldById = <String, ConversationTimelineItem>{
      for (final item in oldItems) item.id: item,
    };
    final firstExistingIndex = newItems.indexWhere(oldById.containsKey);
    var changed = 0;

    for (var index = 0; index < newItems.length; index++) {
      final item = newItems[index];
      final oldItem = oldById[item.id];
      if (oldItem == null) {
        // New IDs before the first existing entry are history prepends.
        if (firstExistingIndex < 0 || index >= firstExistingIndex) {
          changed++;
        }
        continue;
      }

      if (oldItem.revision != item.revision) {
        changed++;
      }
    }
    return changed;
  }

  bool _isNearBottom(ScrollMetrics metrics) {
    final distance = metrics.maxScrollExtent - metrics.pixels;
    return distance <= widget.nearBottomThreshold + 0.5;
  }

  bool _handleScrollNotification(ScrollNotification notification) {
    if (notification.depth != 0 || _programmaticScroll) {
      return false;
    }

    final metrics = notification.metrics;
    if (notification is UserScrollNotification) {
      if (notification.direction == ScrollDirection.forward) {
        _pauseFollowing();
      } else if (notification.direction == ScrollDirection.reverse &&
          _isNearBottom(metrics)) {
        _resumeFollowing();
      }
    } else if (notification is ScrollUpdateNotification &&
        notification.dragDetails != null) {
      final movedDown = metrics.pixels > _lastPixels + 0.5;
      if (_isNearBottom(metrics) && movedDown) {
        _resumeFollowing();
      } else if (metrics.pixels <
          metrics.maxScrollExtent - widget.nearBottomThreshold) {
        _pauseFollowing();
      }
    }

    _lastPixels = metrics.pixels;
    if (notification is ScrollEndNotification) {
      _maybeLoadOlder(metrics);
    }
    return false;
  }

  void _maybeLoadOlder(ScrollMetrics metrics) {
    if (widget.onLoadOlder == null ||
        !widget.hasOlder ||
        widget.isLoadingOlder ||
        _loadRequestPending ||
        metrics.pixels > 24 ||
        metrics.maxScrollExtent <= 0) {
      return;
    }

    _loadRequestPending = true;
    unawaited(_loadOlder());
  }

  Future<void> _loadOlder() async {
    try {
      await widget.onLoadOlder!();
    } finally {
      _loadRequestPending = false;
    }
  }

  void _pauseFollowing() {
    if (!_isFollowing) {
      return;
    }
    _setFollowing(false);
  }

  void _resumeFollowing() {
    if (_isFollowing && _unreadCount == 0) {
      return;
    }
    _setFollowing(true);
    _setUnreadCount(0);
  }

  void _setFollowing(bool value) {
    if (_isFollowing == value) {
      return;
    }
    setState(() => _isFollowing = value);
    widget.controller?._publish(_isFollowing, _unreadCount, notify: true);
  }

  void _setUnreadCount(int value) {
    final normalized = value < 0 ? 0 : value;
    if (_unreadCount == normalized) {
      return;
    }
    setState(() => _unreadCount = normalized);
    widget.controller?._publish(_isFollowing, _unreadCount, notify: true);
  }

  void _captureAnchor() {
    if (!_hasLaidOut || _anchorSnapshot != null) {
      return;
    }
    final viewport = _viewportRenderBox;
    if (viewport == null || viewport.size.height <= 0) {
      return;
    }

    final viewportTop = viewport.localToGlobal(Offset.zero).dy;
    final viewportBottom = viewportTop + viewport.size.height;
    for (final item in widget.items) {
      final box = _itemRenderBox(item.id);
      if (box == null) {
        continue;
      }
      final itemTop = box.localToGlobal(Offset.zero).dy;
      final itemBottom = itemTop + box.size.height;
      if (itemBottom > viewportTop + 0.5 && itemTop < viewportBottom - 0.5) {
        _anchorSnapshot = _AnchorSnapshot(
          id: item.id,
          viewportTop: itemTop - viewportTop,
        );
        return;
      }
    }
  }

  RenderBox? get _viewportRenderBox {
    final renderObject = _viewportKey.currentContext?.findRenderObject();
    return renderObject is RenderBox && renderObject.hasSize
        ? renderObject
        : null;
  }

  RenderBox? _itemRenderBox(String id) {
    final renderObject = _itemKeys[id]?.currentContext?.findRenderObject();
    return renderObject is RenderBox && renderObject.hasSize
        ? renderObject
        : null;
  }

  void _scheduleLayoutWork() {
    if (_layoutWorkScheduled) {
      return;
    }
    _layoutWorkScheduled = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _layoutWorkScheduled = false;
      if (!mounted || !_scrollController.hasClients) {
        return;
      }

      final anchor = _anchorSnapshot;
      _anchorSnapshot = null;
      if (_isFollowing) {
        _scrollToLatest(animate: false);
      } else if (anchor != null) {
        _restoreAnchor(anchor);
      }
      _hasLaidOut = true;
    });
  }

  void _restoreAnchor(_AnchorSnapshot anchor) {
    final viewport = _viewportRenderBox;
    final item = _itemRenderBox(anchor.id);
    if (viewport == null || item == null || !_scrollController.hasClients) {
      return;
    }

    final viewportTop = viewport.localToGlobal(Offset.zero).dy;
    final itemTop = item.localToGlobal(Offset.zero).dy;
    final delta = itemTop - viewportTop - anchor.viewportTop;
    if (delta.abs() < 0.5) {
      return;
    }

    final position = _scrollController.position;
    final target = (position.pixels + delta).clamp(
      position.minScrollExtent,
      position.maxScrollExtent,
    );
    _programmaticScroll = true;
    position.jumpTo(target.toDouble());
    _programmaticScroll = false;
  }

  void _scrollToLatest({required bool animate}) {
    if (!_scrollController.hasClients) {
      return;
    }
    final position = _scrollController.position;
    final target = position.maxScrollExtent;
    final shouldAnimate = animate && !_reducedMotion;
    _programmaticScroll = true;
    if (!shouldAnimate || (target - position.pixels).abs() < 0.5) {
      position.jumpTo(target);
      _programmaticScroll = false;
      return;
    }

    _scrollController
        .animateTo(
          target,
          duration: const Duration(milliseconds: 220),
          curve: Curves.easeOutCubic,
        )
        .whenComplete(() => _programmaticScroll = false);
  }

  bool get _reducedMotion {
    final override = widget.reducedMotion;
    if (override != null) {
      return override;
    }
    final mediaQuery = MediaQuery.maybeOf(context);
    return mediaQuery?.disableAnimations == true ||
        mediaQuery?.accessibleNavigation == true;
  }

  void _returnToLatest({bool animate = true}) {
    _setFollowing(true);
    _setUnreadCount(0);
    _scrollToLatest(animate: animate);
  }

  Widget _latestButton(BuildContext context) {
    final count = _unreadCount;
    final mediaQuery = MediaQuery.maybeOf(context);
    final bottom = (mediaQuery?.viewInsets.bottom ?? 0) + 12;
    final semanticsLabel = count > 0
        ? '${widget.latestLabel}。$count件の新着'
        : '${widget.latestLabel}。${widget.activityLabel}';

    return Positioned(
      left: 16,
      right: 16,
      bottom: bottom,
      child: Align(
        alignment: Alignment.bottomCenter,
        child: Semantics(
          button: true,
          label: semanticsLabel,
          child: FilledButton.icon(
            key: const ValueKey<String>('conversation-timeline-latest'),
            onPressed: () => _returnToLatest(),
            icon: const Icon(Icons.keyboard_arrow_down),
            label: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(widget.latestLabel),
                if (count > 0) ...[
                  const SizedBox(width: 8),
                  Container(
                    constraints: const BoxConstraints(minWidth: 20),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 6,
                      vertical: 2,
                    ),
                    decoration: BoxDecoration(
                      color: Theme.of(context).colorScheme.onPrimary,
                      borderRadius: BorderRadius.circular(12),
                    ),
                    child: Text(
                      '$count',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.primary,
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                ] else ...[
                  const SizedBox(width: 8),
                  Text(
                    widget.activityLabel,
                    style: Theme.of(context).textTheme.labelSmall,
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    // Keep MediaQuery as a dependency for rotation and keyboard resizes even
    // when no latest button is currently visible.
    MediaQuery.maybeOf(context);
    final children = <Widget>[];
    for (final item in widget.items) {
      final key = _itemKeys.putIfAbsent(
        item.id,
        () => GlobalKey(debugLabel: 'conversation-timeline-${item.id}'),
      );
      children.add(KeyedSubtree(key: key, child: item.child));
    }

    final timeline = ListView(
      key: _viewportKey,
      controller: _scrollController,
      padding: widget.padding,
      physics: widget.physics,
      children: children,
    );

    return NotificationListener<ScrollNotification>(
      onNotification: _handleScrollNotification,
      child: Stack(
        fit: StackFit.expand,
        children: [
          timeline,
          if (!_isFollowing) _latestButton(context),
          if (widget.isLoadingOlder)
            const Positioned(
              top: 8,
              left: 0,
              right: 0,
              child: Center(
                child: SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

bool _hasUniqueIds(List<ConversationTimelineItem> items) {
  final ids = <String>{};
  return items.every((item) => ids.add(item.id));
}
