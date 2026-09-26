import packageMetadata from '../../package.json';

import {LAUNCHER_DISPLAY_NAME} from './launcherBrand';

/** Canonical build version embedded in the Launcher frontend bundle. */
export const LAUNCHER_VERSION = packageMetadata.version;

/** Human-readable Launcher build label used by diagnostic UI. */
export const LAUNCHER_VERSION_LABEL = `${LAUNCHER_DISPLAY_NAME} v${LAUNCHER_VERSION}`;

/** Accessible Launcher build label with an unambiguous version separator. */
export const LAUNCHER_VERSION_ACCESSIBLE_LABEL =
  `${LAUNCHER_DISPLAY_NAME} version ${LAUNCHER_VERSION}`;
