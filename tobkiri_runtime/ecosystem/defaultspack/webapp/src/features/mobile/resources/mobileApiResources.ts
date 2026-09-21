import { api } from "../../../lib/api";
import type {
  MobileDevice,
  MobileDevicesResponse,
  MobilePairingApprovePayload,
  MobilePairingReview,
  MobilePairingStatus,
} from "../../../lib/api";

export const mobileApiResources = {
  listDevices(): Promise<MobileDevicesResponse> {
    return api.listMobileDevices();
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
};
