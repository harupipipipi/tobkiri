import 'package:flutter/material.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';

import 'qr_payload.dart';

enum QrScanPurpose { apiImport, pcConnect, general }

class QrScannerScreen extends StatefulWidget {
  const QrScannerScreen({super.key, required this.purpose, this.hint});

  final QrScanPurpose purpose;
  final String? hint;

  @override
  State<QrScannerScreen> createState() => _QrScannerScreenState();
}

class _QrScannerScreenState extends State<QrScannerScreen> {
  final _controller = TextEditingController();
  bool _handled = false;
  bool _showManualInput = false;
  bool _scanLaunching = false;
  String? _scanError;

  static const _qrScannerChannel = MethodChannel('ai.rumi.remote/qr_scanner');

  @override
  void initState() {
    super.initState();
    if (defaultTargetPlatform == TargetPlatform.iOS) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && !_showManualInput) {
          _startNativeScanner();
        }
      });
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  String get _title => switch (widget.purpose) {
        QrScanPurpose.apiImport => 'API/モデルをQRで取り込む',
        QrScanPurpose.pcConnect => 'PCにQRで接続',
        QrScanPurpose.general => 'QRスキャン',
      };

  bool _matchesPurpose(QrPayload payload) {
    switch (widget.purpose) {
      case QrScanPurpose.apiImport:
        return payload is QrApiImport;
      case QrScanPurpose.pcConnect:
        return payload is QrPcConnection || payload is QrPairingV2;
      case QrScanPurpose.general:
        return true;
    }
  }

  void _handleRaw(String raw) {
    if (_handled) return;
    raw = raw.trim();
    if (raw.isEmpty) return;
    _handled = true;
    final payload = parseQrPayload(raw);
    final mismatch = !_matchesPurpose(payload);
    Navigator.of(context).pop((payload, mismatch));
  }

  void _submitManual() {
    _handleRaw(_controller.text);
  }

  Future<void> _startNativeScanner() async {
    if (_scanLaunching || _handled) return;
    setState(() {
      _scanLaunching = true;
      _scanError = null;
    });
    try {
      final value = await _qrScannerChannel.invokeMethod<String>('scan');
      if (!mounted || _handled) return;
      if (value != null && value.trim().isNotEmpty) {
        _handleRaw(value);
      }
    } catch (error) {
      if (mounted) {
        setState(() => _scanError = 'カメラを開始できませんでした');
      }
    } finally {
      if (mounted) {
        setState(() => _scanLaunching = false);
      }
    }
  }

  Widget _buildScanner() {
    if (defaultTargetPlatform != TargetPlatform.iOS) {
      return _buildScannerUnavailable();
    }
    return AspectRatio(
      aspectRatio: 1,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(16),
        child: Container(
          color: Colors.black,
          padding: const EdgeInsets.all(20),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const Icon(
                Icons.qr_code_scanner,
                color: Colors.white70,
                size: 48,
              ),
              const SizedBox(height: 14),
              Text(
                _scanError ?? 'カメラを開いてQRをスキャンします',
                textAlign: TextAlign.center,
                style: const TextStyle(color: Colors.white70, fontSize: 13),
              ),
              const SizedBox(height: 18),
              FilledButton.icon(
                icon: _scanLaunching
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.camera_alt_outlined),
                label: Text(_scanLaunching ? 'カメラを開いています' : 'カメラを開く'),
                onPressed: _scanLaunching ? null : _startNativeScanner,
              ),
              const SizedBox(height: 10),
              TextButton.icon(
                icon: const Icon(Icons.keyboard_outlined),
                label: const Text('手入力'),
                onPressed: () {
                  setState(() => _showManualInput = true);
                },
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildScannerUnavailable() {
    return AspectRatio(
      aspectRatio: 1,
      child: Container(
        color: Colors.black,
        padding: const EdgeInsets.all(20),
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            const Icon(
              Icons.no_photography_outlined,
              color: Colors.white70,
              size: 42,
            ),
            const SizedBox(height: 12),
            const Text(
              'この環境ではカメラを開始できませんでした',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.white70, fontSize: 13),
            ),
            const SizedBox(height: 16),
            FilledButton.tonal(
              onPressed: () {
                setState(() => _showManualInput = true);
              },
              child: const Text('手入力に切り替え'),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildManualInput() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        TextField(
          controller: _controller,
          maxLines: 4,
          decoration: const InputDecoration(
            labelText: 'QR内容 (JSON または URL)',
            hintText: '{"kind":"rumi_mobile_pair_v1","pairingId":"..."}',
            border: OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 16),
        FilledButton.icon(
          icon: const Icon(Icons.check),
          label: const Text('取り込む'),
          onPressed: _submitManual,
        ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(_title)),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              widget.hint ?? _title,
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 12),
            _showManualInput ? _buildManualInput() : _buildScanner(),
          ],
        ),
      ),
    );
  }
}
