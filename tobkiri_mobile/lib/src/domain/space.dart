import 'conversation_locator.dart';
import '../data/pc/device_store.dart';
import '../settings/api_config_store.dart';

enum SpaceKind { local, pc }

class Space {
  const Space({
    required this.id,
    required this.label,
    required this.kind,
    this.deviceId,
    this.pairedDevice,
    this.online = true,
  });

  final String id;
  final String label;
  final SpaceKind kind;
  final String? deviceId;
  final PairedDevice? pairedDevice;
  final bool online;

  bool get isLocal => kind == SpaceKind.local;
  bool get isPc => kind == SpaceKind.pc;
  bool get isOffline => !online;

  ConversationAuthorityKind get authority =>
      isPc ? ConversationAuthorityKind.pc : ConversationAuthorityKind.local;

  PcConnection? get pcConnection => pairedDevice?.toPcConnection();

  static const Space local = Space(
    id: 'local',
    label: 'このスマホ',
    kind: SpaceKind.local,
    online: true,
  );

  Space copyWith({
    String? label,
    bool? online,
    PairedDevice? pairedDevice,
  }) {
    return Space(
      id: id,
      label: label ?? this.label,
      kind: kind,
      deviceId: deviceId,
      pairedDevice: pairedDevice ?? this.pairedDevice,
      online: online ?? this.online,
    );
  }

  static Space fromPairedDevice(PairedDevice device) {
    return Space(
      id: 'pc:${device.connectionId}',
      label: device.displayPcLabel,
      kind: SpaceKind.pc,
      deviceId: device.deviceId,
      pairedDevice: device,
      online: true,
    );
  }
}
