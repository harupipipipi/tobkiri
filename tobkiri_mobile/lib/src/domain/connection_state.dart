enum PairingState {
  unpaired,
  awaitingPcApproval,
  paired,
  revoked,
}

enum PcConnectionState {
  offline,
  connecting,
  online,
  degraded,
  reauthRequired,
}

class DeviceConnectionView {
  const DeviceConnectionView({
    required this.pairingState,
    required this.pcConnectionState,
    this.canReadPcConversations = false,
    this.canWritePcConversations = false,
    this.canObservePcTools = false,
    this.canApprovePcTools = false,
    this.canRequestCredentialCopy = false,
  });

  final PairingState pairingState;
  final PcConnectionState pcConnectionState;

  final bool canReadPcConversations;
  final bool canWritePcConversations;
  final bool canObservePcTools;
  final bool canApprovePcTools;
  final bool canRequestCredentialCopy;

  bool get isPcOnline => pcConnectionState == PcConnectionState.online;
  bool get isPaired => pairingState == PairingState.paired;

  static const DeviceConnectionView unpaired = DeviceConnectionView(
    pairingState: PairingState.unpaired,
    pcConnectionState: PcConnectionState.offline,
  );
}
