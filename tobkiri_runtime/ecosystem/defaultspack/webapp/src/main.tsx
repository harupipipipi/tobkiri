import React, { Suspense } from "react";
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

const pathname = window.location.pathname;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <Suspense fallback={<TobkiriLoadingScreen />}>
        <HostBootstrap pathname={pathname} />
      </Suspense>
    </AppErrorBoundary>
  </React.StrictMode>,
);
