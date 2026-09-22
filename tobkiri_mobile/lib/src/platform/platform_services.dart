import 'package:flutter/services.dart';

class PlatformPreferences {
  PlatformPreferences({MethodChannel? channel})
      : _channel = channel ?? const MethodChannel('ai.rumi.remote/preferences');

  final MethodChannel _channel;

  Future<String?> read(String key) {
    return _channel.invokeMethod<String>('read', {'key': key});
  }

  Future<void> write(String key, String value) async {
    await _channel.invokeMethod<void>('write', {'key': key, 'value': value});
  }

  Future<void> delete(String key) async {
    await _channel.invokeMethod<void>('delete', {'key': key});
  }
}

class PlatformUrlLauncher {
  const PlatformUrlLauncher();

  static const _channel = MethodChannel('ai.rumi.remote/url_launcher');

  Future<bool> open(Uri uri) async {
    final ok =
        await _channel.invokeMethod<bool>('open', {'url': uri.toString()});
    return ok ?? false;
  }
}

class PlatformClipboard {
  const PlatformClipboard();

  Future<String?> readText() async {
    final data = await Clipboard.getData(Clipboard.kTextPlain);
    return data?.text;
  }

  Future<void> writeText(String text) {
    return Clipboard.setData(ClipboardData(text: text));
  }
}

class PlatformPickedMediaFile {
  const PlatformPickedMediaFile({
    required this.name,
    required this.mimeType,
    required this.size,
    required this.base64Data,
  });

  final String name;
  final String mimeType;
  final int size;
  final String base64Data;
}

class PlatformMediaPicker {
  const PlatformMediaPicker();

  static const _channel = MethodChannel('ai.rumi.remote/media_picker');

  Future<PlatformPickedMediaFile?> pick({
    required String kind,
    required int maxBytes,
  }) async {
    final raw = await _channel.invokeMapMethod<String, dynamic>('pick', {
      'kind': kind,
      'max_bytes': maxBytes,
    });
    if (raw == null) return null;
    final errorCode = '${raw['error_code'] ?? ''}'.trim();
    if (errorCode.isNotEmpty) {
      throw PlatformException(
        code: errorCode,
        message: '${raw['message'] ?? 'Media picker failed'}',
      );
    }
    return PlatformPickedMediaFile(
      name: '${raw['name'] ?? 'selected-file'}',
      mimeType: '${raw['mime_type'] ?? 'application/octet-stream'}',
      size: raw['size'] is num ? (raw['size'] as num).toInt() : 0,
      base64Data: '${raw['base64'] ?? ''}',
    );
  }
}

class PlatformCapturedScreenshot {
  const PlatformCapturedScreenshot({
    required this.mimeType,
    required this.size,
    required this.width,
    required this.height,
    required this.base64Data,
  });

  final String mimeType;
  final int size;
  final int width;
  final int height;
  final String base64Data;
}

class PlatformScreenshotCapture {
  const PlatformScreenshotCapture();

  static const _channel = MethodChannel('ai.rumi.remote/screenshot');

  Future<PlatformCapturedScreenshot> capture({
    required int maxBytes,
    required int maxDimension,
  }) async {
    final raw = await _channel.invokeMapMethod<String, dynamic>('capture', {
      'max_bytes': maxBytes,
      'max_dimension': maxDimension,
    });
    final map = raw ?? const <String, dynamic>{};
    final errorCode = '${map['error_code'] ?? ''}'.trim();
    if (errorCode.isNotEmpty) {
      throw PlatformException(
        code: errorCode,
        message: '${map['message'] ?? 'Screenshot capture failed'}',
      );
    }
    return PlatformCapturedScreenshot(
      mimeType: '${map['mime_type'] ?? 'image/png'}',
      size: map['size'] is num ? (map['size'] as num).toInt() : 0,
      width: map['width'] is num ? (map['width'] as num).toInt() : 0,
      height: map['height'] is num ? (map['height'] as num).toInt() : 0,
      base64Data: '${map['base64'] ?? ''}',
    );
  }
}

class PlatformTransformedImage {
  const PlatformTransformedImage({
    required this.mimeType,
    required this.size,
    required this.width,
    required this.height,
    required this.base64Data,
  });

  final String mimeType;
  final int size;
  final int width;
  final int height;
  final String base64Data;
}

class PlatformImageTransformer {
  const PlatformImageTransformer();

  static const _channel = MethodChannel('ai.rumi.remote/image_transformer');

  Future<PlatformTransformedImage> transform({
    required String base64Data,
    required String outputFormat,
    required int quality,
    required int? maxWidth,
    required int? maxHeight,
    required int maxBytes,
  }) async {
    final raw = await _channel.invokeMapMethod<String, dynamic>('transform', {
      'base64': base64Data,
      'format': outputFormat,
      'quality': quality,
      'max_width': maxWidth,
      'max_height': maxHeight,
      'max_bytes': maxBytes,
    });
    final map = raw ?? const <String, dynamic>{};
    final errorCode = '${map['error_code'] ?? ''}'.trim();
    if (errorCode.isNotEmpty) {
      throw PlatformException(
        code: errorCode,
        message: '${map['message'] ?? 'Image transform failed'}',
      );
    }
    return PlatformTransformedImage(
      mimeType: '${map['mime_type'] ?? 'image/png'}',
      size: map['size'] is num ? (map['size'] as num).toInt() : 0,
      width: map['width'] is num ? (map['width'] as num).toInt() : 0,
      height: map['height'] is num ? (map['height'] as num).toInt() : 0,
      base64Data: '${map['base64'] ?? ''}',
    );
  }
}

class PlatformOcrBlock {
  const PlatformOcrBlock({
    required this.text,
    required this.confidence,
    required this.boundingBox,
  });

  final String text;
  final double? confidence;
  final Map<String, dynamic> boundingBox;

  Map<String, dynamic> toJson() => {
        'text': text,
        if (confidence != null) 'confidence': confidence,
        if (boundingBox.isNotEmpty) 'bounding_box': boundingBox,
      };
}

class PlatformOcrResult {
  const PlatformOcrResult({
    required this.text,
    required this.blocks,
    required this.languageCode,
  });

  final String text;
  final List<PlatformOcrBlock> blocks;
  final String? languageCode;
}

class PlatformOcrRecognizer {
  const PlatformOcrRecognizer();

  static const _channel = MethodChannel('ai.rumi.remote/ocr');

  Future<PlatformOcrResult> recognize({
    required String base64Data,
    required int maxBytes,
    String? languageHint,
  }) async {
    final raw = await _channel.invokeMapMethod<String, dynamic>('recognize', {
      'base64': base64Data,
      'max_bytes': maxBytes,
      'language_hint': languageHint,
    });
    final map = raw ?? const <String, dynamic>{};
    final errorCode = '${map['error_code'] ?? ''}'.trim();
    if (errorCode.isNotEmpty) {
      throw PlatformException(
        code: errorCode,
        message: '${map['message'] ?? 'OCR failed'}',
      );
    }
    final rawBlocks = map['blocks'];
    final blocks = rawBlocks is List
        ? rawBlocks.whereType<Map>().map((block) {
            final box = block['bounding_box'];
            return PlatformOcrBlock(
              text: '${block['text'] ?? ''}',
              confidence: block['confidence'] is num
                  ? (block['confidence'] as num).toDouble()
                  : null,
              boundingBox: box is Map
                  ? box.map((key, value) => MapEntry('$key', value))
                  : const <String, dynamic>{},
            );
          }).toList()
        : const <PlatformOcrBlock>[];
    final language = '${map['language_code'] ?? ''}'.trim();
    return PlatformOcrResult(
      text: '${map['text'] ?? ''}',
      blocks: blocks,
      languageCode: language.isEmpty ? null : language,
    );
  }
}

class PlatformNotifications {
  const PlatformNotifications();

  static const _channel = MethodChannel('ai.rumi.remote/notifications');

  Future<bool> requestAuthorization() async {
    try {
      final ok = await _channel.invokeMethod<bool>('requestAuthorization');
      return ok ?? false;
    } catch (_) {
      return false;
    }
  }

  Future<bool> showPcTaskFinished({
    required String title,
    required String body,
  }) async {
    try {
      final ok = await _channel.invokeMethod<bool>('showPcTaskFinished', {
        'title': title,
        'body': body,
      });
      return ok ?? false;
    } catch (_) {
      return false;
    }
  }
}
