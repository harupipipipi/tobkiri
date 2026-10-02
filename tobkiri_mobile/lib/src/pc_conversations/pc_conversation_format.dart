import 'package:flutter/material.dart';

import '../models.dart';

const Duration pcConversationRecentWindow = Duration(days: 7);

enum PcConversationSection { pinned, recent, earlier }

PcConversationSection classifyPcConversation(
  PcConversation conversation, {
  required DateTime now,
  Duration recentWindow = pcConversationRecentWindow,
}) {
  if (conversation.pinned) {
    return PcConversationSection.pinned;
  }
  final updatedAt = conversation.updatedAt;
  if (updatedAt == null) {
    return PcConversationSection.earlier;
  }
  final age = now.toUtc().difference(updatedAt.toUtc());
  if (age <= recentWindow) {
    return PcConversationSection.recent;
  }
  return PcConversationSection.earlier;
}

String pcConversationSectionLabel(
  PcConversationSection section,
  Locale locale,
) {
  if (locale.languageCode == 'ja') {
    return switch (section) {
      PcConversationSection.pinned => 'ピン留め',
      PcConversationSection.recent => '最近',
      PcConversationSection.earlier => '以前',
    };
  }
  return switch (section) {
    PcConversationSection.pinned => 'Pinned',
    PcConversationSection.recent => 'Recent',
    PcConversationSection.earlier => 'Earlier',
  };
}

String formatPcConversationCount(BuildContext context, int count) {
  final locale = Localizations.localeOf(context);
  final number = MaterialLocalizations.of(
    context,
  ).formatDecimal(count < 0 ? 0 : count);
  if (locale.languageCode == 'ja') {
    return '$number件';
  }
  return '$number ${count == 1 ? 'message' : 'messages'}';
}

String formatPcConversationRecency(
  BuildContext context,
  DateTime? updatedAt, {
  DateTime? now,
}) {
  if (updatedAt == null) {
    return _localized(context, en: 'Unknown time', ja: '日時不明');
  }

  final current = (now ?? DateTime.now()).toUtc();
  final date = updatedAt.toUtc();
  final age = current.difference(date);
  final language = Localizations.localeOf(context).languageCode;
  if (age.isNegative || age < const Duration(minutes: 1)) {
    return _localized(context, en: 'Just now', ja: 'たった今');
  }
  if (age < const Duration(hours: 1)) {
    final minutes = age.inMinutes;
    return language == 'ja' ? '$minutes分前' : '$minutes min ago';
  }
  if (age < const Duration(days: 1)) {
    final hours = age.inHours;
    return language == 'ja' ? '$hours時間前' : '$hours hr ago';
  }
  if (age < const Duration(days: 2)) {
    return _localized(context, en: 'Yesterday', ja: '昨日');
  }
  if (age < const Duration(days: 7)) {
    final days = age.inDays;
    return language == 'ja' ? '$days日前' : '$days days ago';
  }
  return MaterialLocalizations.of(
    context,
  ).formatCompactDate(updatedAt.toLocal());
}

String _localized(
  BuildContext context, {
  required String en,
  required String ja,
}) {
  return Localizations.localeOf(context).languageCode == 'ja' ? ja : en;
}
