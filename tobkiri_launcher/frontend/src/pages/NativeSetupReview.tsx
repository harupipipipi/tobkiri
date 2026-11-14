import {useState} from 'react';
import {parseDefaultsSetupState, type DefaultsSetupState} from '@/src/lib/defaultsSetupContract';
import {DefaultsReview} from './DefaultsReview';

/** Render the host-owned candidate and its existing confirmation controls. */
export function NativeSetupReview({listing, interactive = false}: {
  readonly listing: unknown;
  readonly interactive?: boolean;
}) {
  const [reviewed, setReviewed] = useState(false);
  let setup: DefaultsSetupState | null = null;
  try {
    setup = parseDefaultsSetupState(listing);
  } catch {
    return <DefaultsReview setup={null} reviewed={false} activating={false}
      error="The Setup review could not be displayed." readOnly />;
  }
  const canConfirm = interactive && setup.state === 'review_required';
  return <DefaultsReview setup={setup} reviewed={reviewed} activating={false}
    error={null} readOnly={!canConfirm} nativeConfirmation={canConfirm}
    onReviewedChange={canConfirm ? setReviewed : undefined} />;
}
