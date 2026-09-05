import React, { Suspense, lazy } from "react";
import ReactDOM from "react-dom/client";
import { AppErrorBoundary } from "./components/AppErrorBoundary";
import { TobkiriLoadingScreen } from "./components/TobkiriLoadingScreen";
import { HostBootstrap } from "./host/HostBootstrap";
import {
  cleanupLegacyApprovalCredentialsEarly,
} from "./lib/authorityApprovalBrowserToken";
import { installGlobalClientDiagnostics } from "./lib/clientDiagnostics";
import { installKeyboardOnlyFocusRings } from "./lib/focusModality";
import "./index.css";

cleanupLegacyApprovalCredentialsEarly();

installKeyboardOnlyFocusRings();

installGlobalClientDiagnostics();

// Wave 3 compatibility projection. Product surfaces live in a separate chunk;
// the root host bundle imports no feature screen. Wave 10 removes this alias
// after every builtin screen is represented by a profile-scoped contribution.
// Keep this entry-point binding lowercase. When Vite's React Refresh transform
// mistakes a capitalized lazy binding for an exported component, an invalidated
// entry can import itself and call createRoot twice.
const compatibilitySurface = lazy(() => import("./App"));
const route = window.location.pathname;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <Suspense fallback={<TobkiriLoadingScreen />}>
        <HostBootstrap route={route} fallback={React.createElement(compatibilitySurface)} />
      </Suspense>
    </AppErrorBoundary>
  </React.StrictMode>,
);
