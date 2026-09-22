import { api } from "../../../lib/api";
import type {
  MobileDevice,
  MobileDevicesResponse,
  MobilePairingApprovePayload,
  MobilePairingReview,
  MobilePairingStatus,
  P2PPairing,
} from "../../../lib/api";

export const mobileApiResources = {
  startPairing(payload?: {
    peer_id?: string;
    peer_fingerprint?: string;
    peer_label?: string;
    ttl_seconds?: number;
    capabilities?: string[];
    allowed_company_ids?: string[];
  }): Promise<{ pairing: P2PPairing }> {
    return api.startP2PPairing(payload);
  },
  listDevices(): Promise<MobileDevicesResponse> {
    return api.listMobileDevices();
  },
  revokeDevice(deviceId: string) {
    return api.revokeMobileDevice(deviceId);
  },
  createCredentialTransfer(payload: { device_id: string; provider_id: string; api_id: string; provider_label?: string }) {
    return api.createCredentialTransfer(payload);
  },
  confirmCredentialTransfer(transferId: string, payload: { device_id: string; provider_id: string; api_id: string; user_confirmed: true }) {
    return api.confirmCredentialTransfer(transferId, payload);
  },
  getCredentialTransferStatus: (transferId: string) => api.getCredentialTransferStatus(transferId),
  cancelCredentialTransfer: (transferId: string) => api.cancelCredentialTransfer(transferId),
  revokeCredentialTransfer: (transferId: string) => api.revokeCredentialTransfer(transferId),
  getPairingStatus: (pairingId: string): Promise<MobilePairingStatus> => api.getMobilePairingStatus(pairingId),
  getPairingReview: (pairingId: string): Promise<MobilePairingReview> => api.getMobilePairingReview(pairingId),
  approvePairing: (pairingId: string, payload: MobilePairingApprovePayload) => api.approveMobilePairing(pairingId, payload),
  rejectPairing: (pairingId: string, reason?: string) => api.rejectMobilePairing(pairingId, reason),
};

export type MobilePairingApi = Pick<
  typeof mobileApiResources,
  "getPairingStatus" | "getPairingReview" | "approvePairing" | "rejectPairing"
>;
export type {
  MobileDevice,
  MobileDevicesResponse,
  MobilePairingApprovePayload,
  MobilePairingReview,
  MobilePairingStatus,
  P2PPairing,
};
