import {useInRouterContext, useLocation} from 'react-router';
import {createContext, useContext, useMemo, useState, type ReactNode} from 'react';

/**
 * Which named execution Profile the user is currently inspecting in this
 * panel session, shared across panel routes so the identity survives
 * navigation between Dev surfaces.
 *
 * Selection is presentation-only Launcher state: `selectProfile` performs no
 * Host call, so it can never activate a Profile, publish a Plan, or grant a
 * capability. Runtime surfaces are captured from the *active* execution
 * Profile, so consumers that render runtime data must state explicitly when
 * the selected Profile cannot appear there instead of pretending the
 * surface is bound to it.
 */
export interface ProfileSelection {
  selectedProfileId: string | null;
  selectProfile: (profileId: string | null) => void;
}

const ProfileSelectionContext = createContext<ProfileSelection | null>(null);

const NO_SELECTION: ProfileSelection = {
  selectedProfileId: null,
  selectProfile: () => undefined,
};

/** URL selection is an inspection constraint, never Host authority. */
export function inspectedProfileFromSearch(search: string): string | undefined {
  const query = new URLSearchParams(search);
  const values = [...query.getAll('profile_id'), ...query.getAll('profile')];
  if (!values.length) return undefined;
  if (values.length !== 1 || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(values[0])) {
    return '__invalid_profile_selection__';
  }
  return values[0];
}

function SelectionState({children, explicit}: {children: ReactNode; explicit?: string}) {
  const [remembered, setSelectedProfileId] = useState<string | null>(null);
  const selectedProfileId = explicit ?? remembered;
  const value = useMemo<ProfileSelection>(() => ({
    selectedProfileId,
    selectProfile: setSelectedProfileId,
  }), [selectedProfileId]);
  return <ProfileSelectionContext.Provider value={value}>{children}</ProfileSelectionContext.Provider>;
}

function RoutedSelection({children}: {children: ReactNode}) {
  const location = useLocation();
  return <SelectionState explicit={inspectedProfileFromSearch(location.search)}>{children}</SelectionState>;
}

export function ProfileSelectionProvider({children}: {children: ReactNode}) {
  const routed = useInRouterContext();
  return routed ? <RoutedSelection>{children}</RoutedSelection> : <SelectionState>{children}</SelectionState>;
}

/**
 * Read the panel-wide Profile inspection selection. Outside the provider the
 * hook reports no selection, so surfaces keep their active-Profile default.
 */
export function useProfileSelection(): ProfileSelection {
  return useContext(ProfileSelectionContext) ?? NO_SELECTION;
}

/**
 * Fail-closed rule for Profile-scoped writes: while an inspection selection
 * exists, a mutation is allowed only when the surface can prove it is bound
 * to that exact Profile. Every Dev/runtime surface binds to the active
 * execution Profile, so a selection naming any other Profile — or an
 * unproven binding — locks writes. Selecting a Profile never activates it or
 * retargets a write.
 */
export function isProfileMutationBlocked(
  selectedProfileId: string | null,
  boundProfileId: string | null | undefined,
): boolean {
  return selectedProfileId !== null && boundProfileId !== selectedProfileId;
}

/** Hook form of {@link isProfileMutationBlocked}. */
export function useProfileMutationBlocked(
  boundProfileId: string | null | undefined,
): boolean {
  const {selectedProfileId} = useProfileSelection();
  return isProfileMutationBlocked(selectedProfileId, boundProfileId);
}
