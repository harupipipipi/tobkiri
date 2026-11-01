import { lazy, Suspense } from "react";

import { TobkiriLoadingScreen } from "../components/TobkiriLoadingScreen";
import { ErrorNotice } from "../components/ErrorNotice";
import type { VerifiedFrontendContribution } from "./frontendContracts";

const Chat = lazy(() => import("../App").then(({ ChatApp }) => ({ default: ChatApp })));
const Application = lazy(() => import("../App"));
const implementations = {
  "defaultspack.chat": Chat,
  "defaultspack.application": Application,
} as const;
const digestPattern = /^sha256:[0-9a-f]{64}$/;

/** Resolve only implementations shipped by this Application, after capture. */
export function applicationBuiltin(item: VerifiedFrontendContribution) {
  if (item.mode !== "application_builtin"
      || item.owner_pack_id !== "defaultspack"
      || !item.build_identity
      || !digestPattern.test(item.owner_pack_hash)
      || !digestPattern.test(item.descriptor_hash)) return null;
  const identity = item.implementation;
  if (!identity || !Object.prototype.hasOwnProperty.call(implementations, identity)) return null;
  return implementations[identity as keyof typeof implementations];
}

/** Render an explicitly selected, sealed Application implementation. */
export function ApplicationBuiltinView({ item }: { item: VerifiedFrontendContribution }) {
  const View = applicationBuiltin(item);
  if (!View) return <ErrorNotice copyLabel="Copy Application availability error"
    copyText="The selected Application view is unavailable."
    errorIcon="application-unavailable" message="The selected Application view is unavailable." />;
  return (
    <div data-application-implementation={item.implementation}>
      <Suspense fallback={<TobkiriLoadingScreen />}><View /></Suspense>
    </div>
  );
}
