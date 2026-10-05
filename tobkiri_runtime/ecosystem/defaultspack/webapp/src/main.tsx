import React, { Suspense } from "react";
import ReactDOM from "react-dom/client";
import { AppErrorBoundary } from "./components/AppErrorBoundary";
import { TobkiriLoadingScreen } from "./components/TobkiriLoadingScreen";
import { HostBootstrap } from "./host/HostBootstrap";
import { TaskPetWindow } from "./components/TaskPetWindow";
import { parseProfileScreenPath } from "./lib/profileRoute";
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
const taskPetRoute = new URLSearchParams(window.location.search).get("surface") === "task-pet"
  ? parseProfileScreenPath(pathname)
  : null;
const taskPetProfileId = taskPetRoute?.applicationRoute === "/chat" ? taskPetRoute.profileId : null;

if (taskPetProfileId) {
  document.documentElement.classList.add("task-pet-window-document");
  document.title = "Tobkiri ペット";
  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode><AppErrorBoundary><TaskPetWindow profileId={taskPetProfileId} /></AppErrorBoundary></React.StrictMode>,
  );
} else {

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <Suspense fallback={<TobkiriLoadingScreen />}>
        <HostBootstrap pathname={pathname} />
      </Suspense>
    </AppErrorBoundary>
  </React.StrictMode>,
);
}
