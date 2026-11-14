import {parseDefaultsSetupState, type DefaultsSetupState} from '@/src/lib/defaultsSetupContract';
import {DefaultsReview} from './DefaultsReview';

/** Render the validated Setup listing in a document with no activation action. */
export function NativeSetupReview({listing}: {readonly listing: unknown}) {
  let setup: DefaultsSetupState | null = null;
  try {
    setup = parseDefaultsSetupState(listing);
  } catch {
    return <DefaultsReview setup={null} reviewed={false} activating={false}
      error="The Setup review could not be displayed." readOnly />;
  }
  return <DefaultsReview setup={setup} reviewed={false} activating={false}
    error={null} readOnly />;
}
