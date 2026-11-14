import {parseDefaultsSetupState} from '@/src/lib/defaultsSetupContract';
import {DefaultsReview} from './DefaultsReview';

/** Render the validated Setup listing in a document with no activation action. */
export function NativeSetupReview({listing}: {readonly listing: unknown}) {
  const setup = parseDefaultsSetupState(listing);
  return <DefaultsReview
    setup={setup}
    reviewed={false}
    activating={false}
    error={null}
    readOnly
  />;
}
