import type {ApiPackVMDoctor} from './apiTypes';

export const WINDOWS_PACKVM_ISSUE_URL =
  'https://github.com/harupipipipi/tobkiri/issues/1494';

/** Gate only a terminal unsupported host; Launch can prepare recoverable states. */
export function packVmLaunchBlockedReason(
  doctor: ApiPackVMDoctor | null,
): string | null {
  if (
    doctor?.ready === false
    && doctor.platform.startsWith('windows-')
    && /only on macos on apple silicon/i.test(doctor.reason ?? '')
  ) {
    return 'This Windows build cannot provision PackVM. It requires the bundled QEMU/WHPX runtime; see issue #1494.';
  }
  return null;
}

/** Keep Profile authorization separate from recoverable launch preparation. */
export function profileLaunchBlockedReason(input: {
  activeProfileReady: boolean;
  launchReady: boolean;
  packVmDoctor: ApiPackVMDoctor | null;
}): string | null {
  if (!input.activeProfileReady || !input.launchReady) {
    return 'The active Profile is not ready to launch. Verify its activation before trying again.';
  }
  return packVmLaunchBlockedReason(input.packVmDoctor);
}
