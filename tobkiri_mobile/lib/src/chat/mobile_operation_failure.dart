import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../data/pc/pc_catalog_client.dart';

/// Stable categories used for user-facing mobile recovery guidance.
enum MobileFailureKind {
  authentication,
  configuration,
  network,
  storage,
  invalidResponse,
  unknown,
}

/// A secret-safe description of one failed mobile operation.
class MobileOperationFailure {
  const MobileOperationFailure({
    required this.area,
    required this.kind,
    required this.errorType,
    this.statusCode,
  });

  final String area;
  final MobileFailureKind kind;
  final String errorType;
  final int? statusCode;

  factory MobileOperationFailure.from(Object error, {required String area}) {
    final statusCode = switch (error) {
      PcCatalogFetchException(:final statusCode) => statusCode,
      _ => _statusCodeFromText(error.toString()),
    };
    final normalized = error.toString().toLowerCase();
    final kind = switch (statusCode) {
      401 || 403 => MobileFailureKind.authentication,
      _ when error is TimeoutException || error is SocketException =>
        MobileFailureKind.network,
      _
          when normalized.contains('token expired') ||
              normalized.contains('unauthorized') ||
              normalized.contains('authentication') ||
              normalized.contains('認証') =>
        MobileFailureKind.authentication,
      _
          when normalized.contains('not configured') ||
              normalized.contains('設定されていません') ||
              normalized.contains('pairing') ||
              normalized.contains('ペアリング') =>
        MobileFailureKind.configuration,
      _
          when normalized.contains('storage') ||
              normalized.contains('keychain') ||
              normalized.contains('preferences') ||
              normalized.contains('secure store') =>
        MobileFailureKind.storage,
      _
          when error is FormatException ||
              normalized.contains('parse') ||
              normalized.contains('解析') ||
              normalized.contains('invalid response') ||
              normalized.contains('incompatible') =>
        MobileFailureKind.invalidResponse,
      _
          when normalized.contains('socket') ||
              normalized.contains('connection') ||
              normalized.contains('network') ||
              normalized.contains('通信') =>
        MobileFailureKind.network,
      _ => MobileFailureKind.unknown,
    };
    return MobileOperationFailure(
      area: area,
      kind: kind,
      errorType: error.runtimeType.toString(),
      statusCode: statusCode,
    );
  }

  String get title => switch (kind) {
        MobileFailureKind.authentication => '$areaの認証が必要です',
        MobileFailureKind.configuration => '$areaの設定を確認してください',
        MobileFailureKind.network => '$areaに接続できません',
        MobileFailureKind.storage => '$areaを読み込めません',
        MobileFailureKind.invalidResponse => '$areaの応答を読み取れません',
        MobileFailureKind.unknown => '$areaで問題が発生しました',
      };

  String get message => switch (kind) {
        MobileFailureKind.authentication =>
          'ペアリングまたは認証の有効期限を確認し、必要なら再接続してください。',
        MobileFailureKind.configuration => '接続先とペアリング設定を確認してから、もう一度お試しください。',
        MobileFailureKind.network => 'PCとスマホが通信できる状態か確認して、もう一度お試しください。',
        MobileFailureKind.storage => '端末の安全な保存領域を利用できません。設定を確認してください。',
        MobileFailureKind.invalidResponse =>
          'PC側のTobkiriを更新し、互換性を確認してから再試行してください。',
        MobileFailureKind.unknown => 'データが空なのではなく、処理を完了できませんでした。再試行してください。',
      };

  String get safeDetails {
    final lines = <String>[
      'area: $area',
      'category: ${kind.name}',
      'error_type: $errorType',
      if (statusCode != null) 'http_status: $statusCode',
    ];
    return lines.join('\n');
  }

  bool get needsRepair =>
      kind == MobileFailureKind.authentication ||
      kind == MobileFailureKind.configuration;

  static int? _statusCodeFromText(String text) {
    final match =
        RegExp(r'HTTP\s+(\d{3})', caseSensitive: false).firstMatch(text);
    return match == null ? null : int.tryParse(match.group(1) ?? '');
  }
}

/// Renders a failure separately from an empty data state with recovery actions.
class MobileOperationFailureView extends StatelessWidget {
  const MobileOperationFailureView({
    super.key,
    required this.failure,
    required this.onRetry,
    this.onRepair,
    this.compact = false,
  });

  final MobileOperationFailure failure;
  final VoidCallback onRetry;
  final VoidCallback? onRepair;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Center(
      child: SingleChildScrollView(
        padding: EdgeInsets.all(compact ? 16 : 24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520),
          child: Card(
            color: scheme.errorContainer,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(Icons.error_outline, color: scheme.onErrorContainer),
                      const SizedBox(width: 12),
                      Expanded(
                        child: Text(
                          failure.title,
                          style:
                              Theme.of(context).textTheme.titleMedium?.copyWith(
                                    color: scheme.onErrorContainer,
                                    fontWeight: FontWeight.w700,
                                  ),
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    failure.message,
                    style: TextStyle(color: scheme.onErrorContainer),
                  ),
                  const SizedBox(height: 12),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      FilledButton.icon(
                        onPressed: onRetry,
                        icon: const Icon(Icons.refresh),
                        label: const Text('再試行'),
                      ),
                      if (onRepair != null)
                        OutlinedButton.icon(
                          onPressed: onRepair,
                          icon: const Icon(Icons.settings_outlined),
                          label: Text(
                            failure.needsRepair ? 'ペアリングを修復' : '設定を開く',
                          ),
                        ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  ExpansionTile(
                    tilePadding: EdgeInsets.zero,
                    childrenPadding: EdgeInsets.zero,
                    title: const Text('診断情報'),
                    children: [
                      SelectableText(failure.safeDetails),
                      Align(
                        alignment: Alignment.centerLeft,
                        child: TextButton.icon(
                          onPressed: () async {
                            await Clipboard.setData(
                              ClipboardData(text: failure.safeDetails),
                            );
                            if (!context.mounted) return;
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(
                                content: Text('秘密情報を除いた診断情報をコピーしました'),
                              ),
                            );
                          },
                          icon: const Icon(Icons.copy),
                          label: const Text('診断情報をコピー'),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
