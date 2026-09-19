import 'package:flutter/material.dart';

enum UnavailableModelAction { refresh, settings }

Future<UnavailableModelAction?> showUnavailableModelDialog(
  BuildContext context, {
  required String modelLabel,
  required String reason,
  required bool canRefresh,
}) {
  return showDialog<UnavailableModelAction>(
    context: context,
    builder: (context) => AlertDialog(
      title: Text('$modelLabel は利用できません'),
      content: Text(reason),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('閉じる'),
        ),
        if (canRefresh)
          TextButton(
            onPressed: () => Navigator.pop(
              context,
              UnavailableModelAction.refresh,
            ),
            child: const Text('再取得'),
          ),
        FilledButton(
          onPressed: () => Navigator.pop(
            context,
            UnavailableModelAction.settings,
          ),
          child: const Text('設定を開く'),
        ),
      ],
    ),
  );
}

Future<bool> showMissingModelProviderDialog(BuildContext context) async {
  return await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: const Text('先にプロバイダーを選択してください'),
          content: const Text(
            'カスタムモデルIDは、設定済みプロバイダーに結び付けて確認します。',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('閉じる'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('設定を開く'),
            ),
          ],
        ),
      ) ??
      false;
}

Future<String?> showProviderBoundCustomModelDialog(
  BuildContext context, {
  required String providerLabel,
  required String initialModelId,
}) async {
  final selected = await showDialog<String>(
    context: context,
    builder: (context) => _CustomModelInputDialog(
      providerLabel: providerLabel,
      initialModelId: initialModelId,
    ),
  );
  if (!context.mounted || selected == null || selected.trim().isEmpty) {
    return null;
  }
  final normalized = selected.trim();
  final confirmed = await showDialog<bool>(
    context: context,
    builder: (context) => AlertDialog(
      title: const Text('変更内容を確認'),
      content: Text(
        'プロバイダー: $providerLabel\n'
        'モデルID: $normalized\n\n'
        'この画面ではサーバー上の存在を確認できません。'
        '送信前にプロバイダー設定とモデルIDを確認してください。',
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context, false),
          child: const Text('戻る'),
        ),
        FilledButton(
          onPressed: () => Navigator.pop(context, true),
          child: const Text('このモデルを選択'),
        ),
      ],
    ),
  );
  return confirmed == true ? normalized : null;
}

String? validateCustomModelId(String? value) {
  final normalized = value?.trim() ?? '';
  if (normalized.isEmpty) return 'モデルIDを入力してください';
  if (normalized.length > 256) return 'モデルIDは256文字以内で入力してください';
  if (!RegExp(r'^[A-Za-z0-9][A-Za-z0-9._:/+\-]*$').hasMatch(normalized)) {
    return '空白や制御文字を除き、モデルIDとして有効な文字を使ってください';
  }
  return null;
}

class _CustomModelInputDialog extends StatefulWidget {
  const _CustomModelInputDialog({
    required this.providerLabel,
    required this.initialModelId,
  });

  final String providerLabel;
  final String initialModelId;

  @override
  State<_CustomModelInputDialog> createState() =>
      _CustomModelInputDialogState();
}

class _CustomModelInputDialogState extends State<_CustomModelInputDialog> {
  final _formKey = GlobalKey<FormState>();
  late final TextEditingController _controller;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController(text: widget.initialModelId);
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _submit() {
    if (_formKey.currentState?.validate() == true) {
      Navigator.pop(context, _controller.text.trim());
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('モデル名を直接入力'),
      content: SingleChildScrollView(
        child: Form(
          key: _formKey,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('プロバイダー: ${widget.providerLabel}'),
              const SizedBox(height: 12),
              TextFormField(
                controller: _controller,
                autofocus: true,
                textInputAction: TextInputAction.done,
                decoration: const InputDecoration(
                  labelText: 'モデルID',
                  hintText: 'namespace/model-name',
                ),
                validator: validateCustomModelId,
                onFieldSubmitted: (_) => _submit(),
              ),
              const SizedBox(height: 8),
              const Text(
                '使用できる文字: 英数字、. _ : / + -',
                style: TextStyle(fontSize: 12),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('キャンセル'),
        ),
        FilledButton(
          onPressed: _submit,
          child: const Text('次へ'),
        ),
      ],
    );
  }
}
