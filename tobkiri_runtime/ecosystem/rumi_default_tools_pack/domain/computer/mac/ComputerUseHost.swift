import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import Security
import Vision

let hostVersion = "rumi.mac.computer_use_host.v1"
let windowInventoryDiagnosticContract = "rumi.mac.window_inventory.v3"
// This marker describes only the helper-local identity annotations attached to
// on-screen inventory records.  It is intentionally independent from the v3
// visibility-topology diagnostic contract.
let selectedWindowIdentityDiagnosticContract = "rumi.mac.selected_window_identity.v1"

func readRequest() throws -> [String: Any] {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    if data.isEmpty {
        return [:]
    }
    let object = try JSONSerialization.jsonObject(with: data, options: [])
    return object as? [String: Any] ?? [:]
}

func emit(_ value: [String: Any]) -> Never {
    let safeValue: [String: Any]
    if JSONSerialization.isValidJSONObject(value) {
        safeValue = value
    } else {
        safeValue = ["ok": false, "error_code": "INVALID_RESULT", "error": "Result was not JSON serializable."]
    }
    let data = (try? JSONSerialization.data(withJSONObject: safeValue, options: [.sortedKeys])) ?? Data()
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
    exit(0)
}

func ok(_ result: [String: Any]) -> Never {
    emit(["ok": true, "result": result])
}

func fail(_ code: String, _ message: String, _ result: [String: Any] = [:]) -> Never {
    var payload: [String: Any] = ["ok": false, "error_code": code, "error": message]
    if !result.isEmpty {
        payload["result"] = result
    }
    emit(payload)
}

func stringValue(_ value: Any?) -> String {
    if let text = value as? String {
        return text
    }
    if let int = value as? Int {
        return String(int)
    }
    if let double = value as? Double {
        return String(double)
    }
    if let bool = value as? Bool {
        return bool ? "true" : "false"
    }
    if let number = value as? NSNumber {
        return number.stringValue
    }
    return ""
}

func intValue(_ value: Any?, default fallback: Int = 0) -> Int {
    if let int = value as? Int {
        return int
    }
    if let double = value as? Double {
        return Int(double)
    }
    if let number = value as? NSNumber {
        return number.intValue
    }
    if let text = value as? String, let parsed = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)) {
        return parsed
    }
    return fallback
}

func doubleValue(_ value: Any?, default fallback: Double = 0) -> Double {
    if let double = value as? Double {
        return double
    }
    if let int = value as? Int {
        return Double(int)
    }
    if let number = value as? NSNumber {
        return number.doubleValue
    }
    if let text = value as? String, let parsed = Double(text.trimmingCharacters(in: .whitespacesAndNewlines)) {
        return parsed
    }
    return fallback
}

func boolValue(_ value: Any?, default fallback: Bool = false) -> Bool {
    if let bool = value as? Bool {
        return bool
    }
    if let int = value as? Int {
        return int != 0
    }
    if let double = value as? Double {
        return double != 0
    }
    if let number = value as? NSNumber {
        return number.boolValue
    }
    let text = stringValue(value).lowercased()
    if ["1", "true", "yes", "y", "on"].contains(text) {
        return true
    }
    if ["0", "false", "no", "n", "off"].contains(text) {
        return false
    }
    return fallback
}

func frontmostPid() -> pid_t {
    NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
}

func runningApps() -> [[String: Any]] {
    let activePid = frontmostPid()
    return NSWorkspace.shared.runningApplications.compactMap { app in
        let name = app.localizedName ?? app.bundleIdentifier ?? ""
        if name.isEmpty {
            return nil
        }
        return [
            "name": name,
            "app": name,
            "bundle_id": app.bundleIdentifier ?? "",
            "pid": Int(app.processIdentifier),
            "active": app.processIdentifier == activePid,
            "path": app.bundleURL?.path ?? "",
            "running": true
        ]
    }
}

func appMatches(_ app: NSRunningApplication, args: [String: Any]) -> Bool {
    let pid = intValue(args["pid"])
    if pid > 0 && Int(app.processIdentifier) == pid {
        return true
    }
    let nameNeedle = stringValue(args["app"] ?? args["application"] ?? args["name"]).lowercased()
    let bundleNeedle = stringValue(args["bundle_id"] ?? args["bundleIdentifier"]).lowercased()
    let appName = (app.localizedName ?? "").lowercased()
    let bundleId = (app.bundleIdentifier ?? "").lowercased()
    if !bundleNeedle.isEmpty && bundleId.contains(bundleNeedle) {
        return true
    }
    if !nameNeedle.isEmpty && (appName.contains(nameNeedle) || bundleId.contains(nameNeedle)) {
        return true
    }
    return false
}

func activateApp(args: [String: Any]) -> Never {
    let matches = NSWorkspace.shared.runningApplications.filter { appMatches($0, args: args) }
    guard let app = matches.first else {
        fail("APP_NOT_FOUND", "No running macOS app matched the activation request.", [
            "action": "computer.activate_app",
            "platform": "Darwin",
            "driver": "mac_swift_host"
        ])
    }
    let activated = app.activate(options: [.activateAllWindows])
    usleep(250_000)
    ok([
        "action": "computer.activate_app",
        "platform": "Darwin",
        "executed": activated,
        "active": Int(frontmostPid()) == Int(app.processIdentifier),
        "app": app.localizedName ?? app.bundleIdentifier ?? "",
        "bundle_id": app.bundleIdentifier ?? "",
        "pid": Int(app.processIdentifier),
        "driver": "mac_swift_host"
    ])
}

func windowRecords(from info: [[String: Any]], activePid: Int) -> [[String: Any]] {
    return info.compactMap { item in
        let windowId = intValue(item[kCGWindowNumber as String])
        let pid = intValue(item[kCGWindowOwnerPID as String])
        let app = stringValue(item[kCGWindowOwnerName as String])
        let title = stringValue(item[kCGWindowName as String])
        let layer = intValue(item[kCGWindowLayer as String])
        guard windowId > 0, pid > 0, layer == 0 else {
            return nil
        }
        guard let bounds = item[kCGWindowBounds as String] as? [String: Any] else {
            return nil
        }
        let x = intValue(bounds["X"])
        let y = intValue(bounds["Y"])
        let width = intValue(bounds["Width"])
        let height = intValue(bounds["Height"])
        guard width > 0, height > 0 else {
            return nil
        }
        return [
            "window_id": windowId,
            "id": windowId,
            "pid": pid,
            "app": app,
            "title": title,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "active": pid == activePid,
            "platform": "Darwin"
        ]
    }
}

func windowRecords() -> [[String: Any]] {
    let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
    let info = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] ?? []
    return windowRecords(from: info, activePid: Int(frontmostPid()))
}

// This diagnostic is deliberately separate from window selection.  It never
// returns an AX element, frame, process identifier, window identifier, or
// display geometry; its closed facts are only useful for explaining why the
// on-screen inventory did not contain an already-running target application.
enum TargetVisibilityClass: String {
    case onScreenNonfrontmost = "on_screen_nonfrontmost"
    case onScreenFrontmost = "on_screen_frontmost"
    case appHidden = "app_hidden"
    case allAXWindowsMinimized = "all_ax_windows_minimized"
    case offscreenSamePIDFrameCorrelated = "offscreen_same_pid_frame_correlated"
    case offscreenCrossPIDFrameCorrelated = "offscreen_cross_pid_frame_correlated"
    case offDisplayGeometry = "off_display_geometry"
    case multipleProcessAmbiguous = "multiple_process_ambiguous"
    case axWindowsUnavailable = "ax_windows_unavailable"
    case mixed
    case indeterminate
}

enum TargetVisibilityIncompleteCause: String {
    case none
    case targetProcessCap = "target_process_cap"
    case axWindowCap = "ax_window_cap"
    case cgRecordCap = "cg_record_cap"
    case axReadFailure = "ax_read_failure"
    case protocolInvalid = "protocol_invalid"
    case multipleCandidates = "multiple_candidates"
}

struct TargetVisibilityDiagnosticFacts {
    var probePerformed = false
    var complete = false
    var truncated = false
    var targetHiddenPresent = false
    var targetUnhiddenPresent = false
    var targetAXWindowsReadComplete = false
    var targetProcessCount = 0
    var candidateProcessCount = 0
    var targetAXWindowCount = 0
    var axMinimizedCount = 0
    var axNonminimizedCount = 0
    var axFrameValidCount = 0
    var axDisplayIntersectionCount = 0
    var axSamePIDCGFrameMatchCount = 0
    var axCrossPIDCGFrameMatchCount = 0
    var targetCGOffscreenLayerZeroGeometryCount = 0
    var visibilityClass: TargetVisibilityClass = .indeterminate
    var incompleteCause: TargetVisibilityIncompleteCause = .none

    func payload() -> [String: Any] {
        [
            "selection_swift_visibility_probe_performed": probePerformed,
            "selection_swift_visibility_probe_complete": complete,
            "selection_swift_visibility_probe_truncated": truncated,
            "selection_swift_target_hidden_present": targetHiddenPresent,
            "selection_swift_target_unhidden_present": targetUnhiddenPresent,
            "selection_swift_target_ax_windows_read_complete": targetAXWindowsReadComplete,
            "selection_swift_visibility_target_process_count": min(4, max(0, targetProcessCount)),
            "selection_swift_visibility_candidate_process_count": min(4, max(0, candidateProcessCount)),
            "selection_swift_target_ax_window_count": min(16, max(0, targetAXWindowCount)),
            "selection_swift_ax_minimized_count": min(16, max(0, axMinimizedCount)),
            "selection_swift_ax_nonminimized_count": min(16, max(0, axNonminimizedCount)),
            "selection_swift_ax_frame_valid_count": min(16, max(0, axFrameValidCount)),
            "selection_swift_ax_display_intersection_count": min(16, max(0, axDisplayIntersectionCount)),
            "selection_swift_ax_same_pid_cg_frame_match_count": min(16, max(0, axSamePIDCGFrameMatchCount)),
            "selection_swift_ax_cross_pid_cg_frame_match_count": min(16, max(0, axCrossPIDCGFrameMatchCount)),
            "selection_swift_target_cg_offscreen_layer_zero_geometry_count": min(16, max(0, targetCGOffscreenLayerZeroGeometryCount)),
            "selection_swift_visibility_class": visibilityClass.rawValue,
            "selection_swift_visibility_incomplete_cause": incompleteCause.rawValue,
        ]
    }
}

struct TargetVisibilityClassificationInput {
    let probePerformed: Bool
    let complete: Bool
    let truncated: Bool
    let onScreenTargetPIDMatchCount: Int
    let targetFrontmost: Bool
    let targetProcessCount: Int
    let hiddenPresent: Bool
    let unhiddenPresent: Bool
    let axReadFailure: Bool
    let axWindowCount: Int
    let axMinimizedCount: Int
    let axNonminimizedCount: Int
    let axFrameValidCount: Int
    let axDisplayIntersectionCount: Int
    let samePIDFrameMatchCount: Int
    let crossPIDFrameMatchCount: Int
}

func classifyTargetVisibility(_ input: TargetVisibilityClassificationInput) -> TargetVisibilityClass {
    guard input.probePerformed else { return .indeterminate }
    // A bounded observation never assigns an actionable or definitive class
    // after a cap was reached.
    if input.truncated { return .indeterminate }
    if input.onScreenTargetPIDMatchCount > 0 {
        return input.targetFrontmost ? .onScreenFrontmost : .onScreenNonfrontmost
    }
    if input.targetProcessCount > 1 { return .multipleProcessAmbiguous }
    if input.hiddenPresent && input.unhiddenPresent { return .mixed }
    if input.hiddenPresent { return .appHidden }
    if input.axReadFailure { return .axWindowsUnavailable }
    if input.axWindowCount > 0 && input.axMinimizedCount > 0 && input.axNonminimizedCount == 0 {
        return .allAXWindowsMinimized
    }
    if input.axMinimizedCount > 0 && input.axNonminimizedCount > 0 { return .mixed }
    if input.samePIDFrameMatchCount > 0 && input.crossPIDFrameMatchCount > 0 { return .mixed }
    if input.samePIDFrameMatchCount > 0 { return .offscreenSamePIDFrameCorrelated }
    if input.crossPIDFrameMatchCount > 0 { return .offscreenCrossPIDFrameCorrelated }
    if input.axFrameValidCount > 0 && input.axDisplayIntersectionCount == 0 {
        return .offDisplayGeometry
    }
    return input.complete ? .indeterminate : .axWindowsUnavailable
}

func inventoryRect(_ value: Any?) -> CGRect? {
    guard let bounds = value as? [String: Any] else { return nil }
    let width = doubleValue(bounds["Width"] ?? bounds["width"])
    let height = doubleValue(bounds["Height"] ?? bounds["height"])
    guard width > 0, height > 0 else { return nil }
    return CGRect(
        x: doubleValue(bounds["X"] ?? bounds["x"]),
        y: doubleValue(bounds["Y"] ?? bounds["y"]), width: width, height: height
    )
}

func inventoryAXRect(_ element: AXUIElement) -> CGRect? {
    guard let position = axPoint(axAttribute(element, kAXPositionAttribute as CFString)),
          let size = axSize(axAttribute(element, kAXSizeAttribute as CFString)),
          size.width > 0, size.height > 0
    else { return nil }
    return CGRect(origin: position, size: size)
}

struct TargetVisibilityAXWindowsRead {
    let windows: [AXUIElement]
    let complete: Bool
    let protocolInvalid: Bool
}

func targetVisibilityAXWindows(_ app: NSRunningApplication) -> TargetVisibilityAXWindowsRead {
    let appElement = AXUIElementCreateApplication(app.processIdentifier)
    var raw: CFTypeRef?
    guard AXUIElementCopyAttributeValue(appElement, kAXWindowsAttribute as CFString, &raw) == .success,
          let value = raw
    else {
        return TargetVisibilityAXWindowsRead(windows: [], complete: false, protocolInvalid: false)
    }
    if let windows = value as? [AXUIElement] {
        return TargetVisibilityAXWindowsRead(windows: windows, complete: true, protocolInvalid: false)
    }
    if let values = value as? [Any], values.allSatisfy({ asAXUIElement($0) != nil }) {
        return TargetVisibilityAXWindowsRead(
            windows: values.compactMap { asAXUIElement($0) }, complete: true, protocolInvalid: false
        )
    }
    return TargetVisibilityAXWindowsRead(windows: [], complete: false, protocolInvalid: true)
}

func targetVisibilityDisplayBounds() -> (bounds: [CGRect], truncated: Bool) {
    let screens = NSScreen.screens
    return (Array(screens.prefix(16)).map(\.frame), screens.count > 16)
}

func targetVisibilityDiagnostic(
    running: [NSRunningApplication], targetPids: Set<Int>,
    onScreenTargetPIDMatchCount: Int, allWindows: [[String: Any]], axTrusted: Bool
) -> TargetVisibilityDiagnosticFacts {
    var facts = TargetVisibilityDiagnosticFacts()
    guard !targetPids.isEmpty, onScreenTargetPIDMatchCount == 0, axTrusted else {
        return facts
    }
    let candidates = running.filter { targetPids.contains(Int($0.processIdentifier)) }
        .sorted { Int($0.processIdentifier) < Int($1.processIdentifier) }
    guard !candidates.isEmpty else { return facts }
    facts.probePerformed = true
    facts.targetProcessCount = candidates.count
    facts.candidateProcessCount = candidates.count
    if candidates.count > 4 {
        facts.truncated = true
        facts.incompleteCause = .targetProcessCap
    }
    let displayObservation = targetVisibilityDisplayBounds()
    if displayObservation.truncated && !facts.truncated {
        facts.truncated = true
        facts.incompleteCause = .protocolInvalid
    }
    let inspectedAllWindows = Array(allWindows.prefix(256))
    if allWindows.count > 256 && !facts.truncated {
        facts.truncated = true
        facts.incompleteCause = .cgRecordCap
    }
    let targetCandidatePids = Set(candidates.prefix(4).map { Int($0.processIdentifier) })
    struct CGCandidate {
        let pid: Int
        let frame: CGRect
    }
    var allLayerZeroGeometry: [CGCandidate] = []
    var targetOffscreenLayerZeroGeometry: [CGCandidate] = []
    for item in inspectedAllWindows {
        guard intValue(item[kCGWindowLayer as String]) == 0,
              let frame = inventoryRect(item[kCGWindowBounds as String])
        else { continue }
        let pid = intValue(item[kCGWindowOwnerPID as String])
        guard pid > 0 else { continue }
        let candidate = CGCandidate(pid: pid, frame: frame)
        allLayerZeroGeometry.append(candidate)
        if targetCandidatePids.contains(pid), !boolValue(item[kCGWindowIsOnscreen as String]) {
            targetOffscreenLayerZeroGeometry.append(candidate)
        }
    }
    facts.targetCGOffscreenLayerZeroGeometryCount = targetOffscreenLayerZeroGeometry.count
    var remainingAXWindows = 16
    var axReadFailure = false
    var protocolInvalid = false
    for app in candidates.prefix(4) {
        facts.targetHiddenPresent = facts.targetHiddenPresent || app.isHidden
        facts.targetUnhiddenPresent = facts.targetUnhiddenPresent || !app.isHidden
        let read = targetVisibilityAXWindows(app)
        if !read.complete {
            axReadFailure = true
            protocolInvalid = protocolInvalid || read.protocolInvalid
            continue
        }
        if read.windows.count > remainingAXWindows {
            facts.truncated = true
            facts.incompleteCause = .axWindowCap
        }
        let windows = Array(read.windows.prefix(max(0, remainingAXWindows)))
        facts.targetAXWindowCount += windows.count
        remainingAXWindows -= windows.count
        for window in windows {
            if axBoolAttribute(window, kAXMinimizedAttribute as CFString, default: false) {
                facts.axMinimizedCount += 1
            } else {
                facts.axNonminimizedCount += 1
            }
            guard let frame = inventoryAXRect(window) else { continue }
            facts.axFrameValidCount += 1
            if displayObservation.bounds.contains(where: { $0.intersects(frame) }) {
                facts.axDisplayIntersectionCount += 1
            }
            let samePIDMatches = targetOffscreenLayerZeroGeometry.filter {
                $0.pid == Int(app.processIdentifier) && rectNearlyMatches($0.frame, frame)
            }
            if samePIDMatches.count == 1 {
                facts.axSamePIDCGFrameMatchCount += 1
                continue
            }
            let crossPIDMatches = allLayerZeroGeometry.filter {
                $0.pid != Int(app.processIdentifier) && rectNearlyMatches($0.frame, frame)
            }
            if crossPIDMatches.count == 1 {
                facts.axCrossPIDCGFrameMatchCount += 1
            }
        }
    }
    facts.targetAXWindowsReadComplete = !axReadFailure && !protocolInvalid && !facts.truncated
    if protocolInvalid && !facts.truncated {
        facts.incompleteCause = .protocolInvalid
    } else if axReadFailure && !facts.truncated {
        facts.incompleteCause = .axReadFailure
    } else if candidates.count > 1 && !facts.truncated {
        facts.incompleteCause = .multipleCandidates
    }
    facts.complete = !facts.truncated && !axReadFailure && !protocolInvalid && candidates.count == 1
    facts.visibilityClass = classifyTargetVisibility(TargetVisibilityClassificationInput(
        probePerformed: facts.probePerformed, complete: facts.complete, truncated: facts.truncated,
        onScreenTargetPIDMatchCount: onScreenTargetPIDMatchCount,
        targetFrontmost: targetCandidatePids.contains(Int(frontmostPid())),
        targetProcessCount: candidates.count, hiddenPresent: facts.targetHiddenPresent,
        unhiddenPresent: facts.targetUnhiddenPresent, axReadFailure: axReadFailure || protocolInvalid,
        axWindowCount: facts.targetAXWindowCount, axMinimizedCount: facts.axMinimizedCount,
        axNonminimizedCount: facts.axNonminimizedCount, axFrameValidCount: facts.axFrameValidCount,
        axDisplayIntersectionCount: facts.axDisplayIntersectionCount,
        samePIDFrameMatchCount: facts.axSamePIDCGFrameMatchCount,
        crossPIDFrameMatchCount: facts.axCrossPIDCGFrameMatchCount
    ))
    return facts
}

func targetVisibilityClassifierSelfTest() -> Bool {
    func sample(
        complete: Bool = true, truncated: Bool = false, onScreen: Int = 0,
        frontmost: Bool = false, processes: Int = 1, hidden: Bool = false,
        unhidden: Bool = true, axFailure: Bool = false, windows: Int = 0,
        minimized: Int = 0, nonminimized: Int = 0, frames: Int = 0,
        displayIntersections: Int = 0, samePID: Int = 0, crossPID: Int = 0
    ) -> TargetVisibilityClassificationInput {
        TargetVisibilityClassificationInput(
            probePerformed: true, complete: complete, truncated: truncated,
            onScreenTargetPIDMatchCount: onScreen, targetFrontmost: frontmost,
            targetProcessCount: processes, hiddenPresent: hidden, unhiddenPresent: unhidden,
            axReadFailure: axFailure, axWindowCount: windows, axMinimizedCount: minimized,
            axNonminimizedCount: nonminimized, axFrameValidCount: frames,
            axDisplayIntersectionCount: displayIntersections,
            samePIDFrameMatchCount: samePID, crossPIDFrameMatchCount: crossPID
        )
    }
    let cases: [(TargetVisibilityClassificationInput, TargetVisibilityClass)] = [
        (sample(onScreen: 1), .onScreenNonfrontmost),
        (sample(onScreen: 1, frontmost: true), .onScreenFrontmost),
        (sample(hidden: true, unhidden: false), .appHidden),
        (sample(windows: 2, minimized: 2), .allAXWindowsMinimized),
        (sample(windows: 1, nonminimized: 1, frames: 1, displayIntersections: 1, samePID: 1), .offscreenSamePIDFrameCorrelated),
        (sample(windows: 1, nonminimized: 1, frames: 1, displayIntersections: 1, crossPID: 1), .offscreenCrossPIDFrameCorrelated),
        (sample(windows: 1, nonminimized: 1, frames: 1), .offDisplayGeometry),
        (sample(complete: false, processes: 2), .multipleProcessAmbiguous),
        (sample(complete: false, axFailure: true), .axWindowsUnavailable),
        (sample(truncated: true, samePID: 1), .indeterminate),
        (sample(hidden: true, unhidden: true), .mixed),
    ]
    let capPayload = TargetVisibilityDiagnosticFacts(
        targetProcessCount: 99, candidateProcessCount: 99, targetAXWindowCount: 99,
        axMinimizedCount: 99, axNonminimizedCount: 99, axFrameValidCount: 99,
        axDisplayIntersectionCount: 99, axSamePIDCGFrameMatchCount: 99,
        axCrossPIDCGFrameMatchCount: 99, targetCGOffscreenLayerZeroGeometryCount: 99
    ).payload()
    return cases.allSatisfy { classifyTargetVisibility($0.0) == $0.1 }
        && capPayload["selection_swift_visibility_target_process_count"] as? Int == 4
        && capPayload["selection_swift_target_ax_window_count"] as? Int == 16
        && capPayload["selection_swift_target_cg_offscreen_layer_zero_geometry_count"] as? Int == 16
}

struct WindowInventoryDiagnosticFacts {
    var inventoryObserved = false
    var inventoryContractValid = false
    var nativeSnapshotAtomic = true
    var workspaceObservationCompleted = false
    var targetProcessPresent = false
    var localizedNameMatch = false
    var bundleIdMatch = false
    var targetProcessMatchCount = 0
    var targetPidMatchAvailable = false
    var targetBundleMatchAvailable = false
    var windowTotalCount = 0
    var usableWindowCount = 0
    var targetNameMatchCount = 0
    var targetPidMatchCount = 0
    var targetBundleMatchCount = 0
    var axTrust = "unavailable"
    var axTargetProbeOutcome = "unknown"
    var screenCapturePreflight = "unavailable"
    var onScreenQueryOutcome = "nil_or_unavailable"
    var allWindowsQueryOutcome = "nil_or_unavailable"
    var ownerNamePresentCount = 0
    var windowNamePresentCount = 0
    var rawTargetPidMatchCount = 0
    var rawTargetBundleMatchCount = 0
    var allWindowsTargetPidMatchCount = 0
    var targetRejectedNotOnScreenCount = 0
    var targetRejectedNonzeroLayerCount = 0
    var targetRejectedInvalidIdentityCount = 0
    var targetRejectedNonpositiveGeometryCount = 0
    var rejectedTargetPidMismatchCount = 0
    var rejectedTargetBundleMismatchCount = 0
    var onScreenOmissionConfirmed = false
    var signingClass = "unknown"
    var visibility = TargetVisibilityDiagnosticFacts()

    func payload() -> [String: Any] {
        var payload: [String: Any] = [
            "selection_native_snapshot_atomic": nativeSnapshotAtomic,
            "selection_nsworkspace_observation_completed": workspaceObservationCompleted,
            "selection_nsworkspace_target_process_present": targetProcessPresent,
            "selection_nsworkspace_localized_name_match": localizedNameMatch,
            "selection_nsworkspace_bundle_id_match": bundleIdMatch,
            "selection_nsworkspace_target_process_match_count": min(4, targetProcessMatchCount),
            "selection_target_pid_match_available": targetPidMatchAvailable,
            "selection_target_bundle_match_available": targetBundleMatchAvailable,
            "selection_swift_inventory_observed": inventoryObserved,
            "selection_swift_inventory_contract_valid": inventoryContractValid,
            "selection_swift_window_total_count": min(64, windowTotalCount),
            "selection_swift_usable_window_count": min(64, usableWindowCount),
            "selection_swift_target_name_match_count": min(8, targetNameMatchCount),
            "selection_swift_target_pid_match_count": min(8, targetPidMatchCount),
            "selection_swift_target_bundle_match_count": min(8, targetBundleMatchCount),
            "selection_swift_pid_match_available": targetPidMatchAvailable,
            "selection_swift_bundle_match_available": targetBundleMatchAvailable,
            "selection_swift_on_screen_only_filter_applied": true,
            "selection_swift_layer_zero_filter_applied": true,
            "selection_swift_execution_component": "swift_helper",
            "selection_swift_permission_check_colocated": true,
            "selection_swift_helper_signing_class": signingClass,
            "selection_codex_permission_comparison": "not_observable",
            "selection_permission_request_api_invoked": false,
            "selection_swift_ax_trust": axTrust,
            "selection_swift_ax_target_probe_outcome": axTargetProbeOutcome,
            "selection_swift_screen_capture_preflight": screenCapturePreflight,
            "selection_swift_cg_on_screen_query_outcome": onScreenQueryOutcome,
            "selection_swift_cg_all_windows_query_outcome": allWindowsQueryOutcome,
            "selection_swift_owner_name_present_count": min(64, ownerNamePresentCount),
            "selection_swift_window_name_present_count": min(64, windowNamePresentCount),
            "selection_swift_target_pid_set_constructed_privately": targetPidMatchAvailable,
            "selection_swift_raw_target_pid_match_count": min(8, rawTargetPidMatchCount),
            "selection_swift_raw_target_bundle_match_count": min(8, rawTargetBundleMatchCount),
            "selection_swift_all_windows_target_pid_match_count": min(8, allWindowsTargetPidMatchCount),
            "selection_swift_target_rejected_not_on_screen_count": min(8, targetRejectedNotOnScreenCount),
            "selection_swift_target_rejected_nonzero_layer_count": min(8, targetRejectedNonzeroLayerCount),
            "selection_swift_target_rejected_invalid_identity_count": min(8, targetRejectedInvalidIdentityCount),
            "selection_swift_target_rejected_nonpositive_geometry_count": min(8, targetRejectedNonpositiveGeometryCount),
            "selection_swift_rejected_target_pid_mismatch_count": min(64, rejectedTargetPidMismatchCount),
            "selection_swift_rejected_target_bundle_mismatch_count": min(8, rejectedTargetBundleMismatchCount),
            "selection_swift_on_screen_omission_confirmed": onScreenOmissionConfirmed,
            "selection_swift_all_windows_nonactionable": true,
        ]
        for (key, value) in visibility.payload() {
            payload[key] = value
        }
        return payload
    }
}

struct WindowInventorySnapshot {
    let windows: [[String: Any]]
    let facts: WindowInventoryDiagnosticFacts
    let signatureToken: String
}

func normalizedInventoryIdentity(_ value: Any?) -> String {
    stringValue(value).lowercased().unicodeScalars.filter {
        CharacterSet.alphanumerics.contains($0)
    }.map(String.init).joined()
}

func inventoryIdentitySet(_ value: Any?) -> Set<String> {
    guard let values = value as? [Any] else { return [] }
    return Set(values.map(normalizedInventoryIdentity).filter { !$0.isEmpty })
}

func inventoryIdentityMatches(_ aliases: Set<String>, _ candidate: Any?) -> Bool {
    let normalized = normalizedInventoryIdentity(candidate)
    return !normalized.isEmpty && aliases.contains(normalized)
}

func cgInventoryQuery(_ options: CGWindowListOption) -> (records: [[String: Any]], outcome: String) {
    guard let copied = CGWindowListCopyWindowInfo(options, kCGNullWindowID) else {
        return ([], "nil_or_unavailable")
    }
    guard let records = copied as? [[String: Any]] else {
        return ([], "invalid_payload")
    }
    return (records, records.isEmpty ? "success_empty" : "success_nonempty")
}

func axInventoryProbeOutcome(pid: Int, trusted: Bool) -> String {
    guard trusted else { return "skipped_not_trusted" }
    guard pid > 0 else { return "unavailable" }
    let app = AXUIElementCreateApplication(pid_t(pid))
    var copied: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(app, kAXRoleAttribute as CFString, &copied)
    switch error {
    case .success: return copied == nil ? "no_value" : "success"
    case .apiDisabled: return "api_disabled"
    case .invalidUIElement, .invalidUIElementObserver: return "invalid_ui_element"
    case .cannotComplete: return "cannot_complete"
    case .attributeUnsupported: return "attribute_unsupported"
    case .noValue: return "no_value"
    case .illegalArgument: return "illegal_argument"
    default: return "failure"
    }
}

func inventorySelfSigningObservation() -> (classification: String, token: String) {
    let url = URL(fileURLWithPath: CommandLine.arguments.first ?? "") as CFURL
    var code: SecStaticCode?
    let createStatus = SecStaticCodeCreateWithPath(url, SecCSFlags(), &code)
    guard createStatus == errSecSuccess, let code else {
        return (createStatus == errSecCSUnsigned ? "unsigned" : "unavailable", "")
    }
    var copied: CFDictionary?
    let status = SecCodeCopySigningInformation(
        code, SecCSFlags(rawValue: kSecCSSigningInformation), &copied
    )
    guard status == errSecSuccess, let info = copied as? [String: Any] else {
        return (status == errSecCSUnsigned ? "unsigned" : "unavailable", "")
    }
    let token = (info[kSecCodeInfoUnique as String] as? Data)?.base64EncodedString() ?? ""
    let flags = intValue(info[kSecCodeInfoFlags as String])
    if flags & 0x2 != 0 { return ("ad_hoc", token) }
    if let certificates = info[kSecCodeInfoCertificates as String] as? [Any], !certificates.isEmpty {
        return ("signed_stable", token)
    }
    return ("unsigned", token)
}

func windowInventorySnapshot(args: [String: Any]) -> WindowInventorySnapshot {
    let aliases = inventoryIdentitySet(args["target_aliases"])
    let bundleAliases = inventoryIdentitySet(args["target_bundle_aliases"])
    let running = NSWorkspace.shared.runningApplications
    let onScreenQuery = cgInventoryQuery([.optionOnScreenOnly, .excludeDesktopElements])
    let allWindowsQuery = cgInventoryQuery([.optionAll, .excludeDesktopElements])
    let info = onScreenQuery.records
    var windows = windowRecords(from: info, activePid: Int(frontmostPid()))
    var facts = WindowInventoryDiagnosticFacts()
    facts.inventoryObserved = ["success_empty", "success_nonempty"].contains(onScreenQuery.outcome)
    facts.inventoryContractValid = facts.inventoryObserved
        && ["success_empty", "success_nonempty"].contains(allWindowsQuery.outcome)
    facts.workspaceObservationCompleted = true
    facts.axTrust = AXIsProcessTrusted() ? "trusted" : "not_trusted"
    facts.screenCapturePreflight = CGPreflightScreenCaptureAccess() ? "granted" : "denied"
    facts.onScreenQueryOutcome = onScreenQuery.outcome
    facts.allWindowsQueryOutcome = allWindowsQuery.records.count > 256
        && allWindowsQuery.outcome == "success_nonempty"
        ? "success_nonempty_truncated" : allWindowsQuery.outcome
    let signing = inventorySelfSigningObservation()
    facts.signingClass = signing.classification
    facts.windowTotalCount = info.count
    facts.usableWindowCount = windows.count
    var targetPids = Set<Int>()
    var bundleMatchedPids = Set<Int>()
    for app in running {
        let nameMatched = inventoryIdentityMatches(aliases, app.localizedName)
        let bundleMatched = inventoryIdentityMatches(bundleAliases, app.bundleIdentifier)
        if nameMatched { facts.localizedNameMatch = true }
        if bundleMatched { facts.bundleIdMatch = true }
        if nameMatched || bundleMatched {
            facts.targetProcessMatchCount += 1
            targetPids.insert(Int(app.processIdentifier))
        }
        if bundleMatched { bundleMatchedPids.insert(Int(app.processIdentifier)) }
    }
    facts.targetProcessPresent = !targetPids.isEmpty
    facts.targetPidMatchAvailable = !targetPids.isEmpty
    facts.targetBundleMatchAvailable = !bundleAliases.isEmpty
    // These three booleans are strictly helper-local correlation hints.  The
    // Python boundary consumes them only after exact (pid, window_id)
    // correlation and strips every _rumi_* key before any record is public.
    windows = windows.map { window in
        var annotated = window
        let pid = intValue(window["pid"])
        annotated["_rumi_owner_alias_match"] = inventoryIdentityMatches(aliases, window["app"])
        annotated["_rumi_target_process_match"] = targetPids.contains(pid)
        annotated["_rumi_target_bundle_match"] = bundleMatchedPids.contains(pid)
        return annotated
    }
    let sortedTargetPids = targetPids.sorted()
    let _ = Set(sortedTargetPids.prefix(4))
    // These identities remain helper-local.  The sort and bounded prefix make
    // the diagnostic deterministic without exporting raw process identifiers.
    let _ = Array(bundleMatchedPids.sorted().prefix(4))
    facts.axTargetProbeOutcome = axInventoryProbeOutcome(
        pid: sortedTargetPids.first ?? 0, trusted: facts.axTrust == "trusted"
    )
    for item in info {
        let pid = intValue(item[kCGWindowOwnerPID as String])
        let windowId = intValue(item[kCGWindowNumber as String])
        let ownerMatched = inventoryIdentityMatches(aliases, item[kCGWindowOwnerName as String])
        let pidMatched = targetPids.contains(pid)
        let targetRecord = ownerMatched || pidMatched
        if !stringValue(item[kCGWindowOwnerName as String]).isEmpty {
            facts.ownerNamePresentCount += 1
        }
        if !stringValue(item[kCGWindowName as String]).isEmpty {
            facts.windowNamePresentCount += 1
        }
        if pidMatched { facts.rawTargetPidMatchCount += 1 }
        if bundleMatchedPids.contains(pid) { facts.rawTargetBundleMatchCount += 1 }
        if pid > 0 && windowId > 0 && !targetPids.contains(pid) {
            facts.rejectedTargetPidMismatchCount += 1
        }
        if ownerMatched && !bundleMatchedPids.isEmpty && !bundleMatchedPids.contains(pid) {
            facts.rejectedTargetBundleMismatchCount += 1
        }
        if targetRecord && (pid <= 0 || windowId <= 0) {
            facts.targetRejectedInvalidIdentityCount += 1
        }
        if targetRecord && intValue(item[kCGWindowLayer as String]) != 0 {
            facts.targetRejectedNonzeroLayerCount += 1
        }
        if targetRecord {
            let bounds = item[kCGWindowBounds as String] as? [String: Any]
            if bounds == nil || intValue(bounds?["Width"]) <= 0 || intValue(bounds?["Height"]) <= 0 {
                facts.targetRejectedNonpositiveGeometryCount += 1
            }
        }
    }
    let inspectedAllWindowRecords = Array(allWindowsQuery.records.prefix(256))
    for item in inspectedAllWindowRecords {
        let pid = intValue(item[kCGWindowOwnerPID as String])
        if targetPids.contains(pid) {
            facts.allWindowsTargetPidMatchCount += 1
            if !boolValue(item[kCGWindowIsOnscreen as String]) {
                facts.targetRejectedNotOnScreenCount += 1
            }
        }
    }
    facts.onScreenOmissionConfirmed = facts.rawTargetPidMatchCount == 0
        && facts.allWindowsTargetPidMatchCount > 0
    for window in windows {
        let pid = intValue(window["pid"])
        if inventoryIdentityMatches(aliases, window["app"]) {
            facts.targetNameMatchCount += 1
        }
        if targetPids.contains(pid) { facts.targetPidMatchCount += 1 }
        if bundleMatchedPids.contains(pid) { facts.targetBundleMatchCount += 1 }
    }
    facts.visibility = targetVisibilityDiagnostic(
        running: running, targetPids: targetPids,
        onScreenTargetPIDMatchCount: facts.rawTargetPidMatchCount,
        allWindows: allWindowsQuery.records, axTrusted: facts.axTrust == "trusted"
    )
    return WindowInventorySnapshot(
        windows: windows, facts: facts, signatureToken: signing.token
    )
}

func matchingWindow(args: [String: Any]) -> [String: Any]? {
    let explicitId = intValue(args["window_id"] ?? args["id"] ?? args["window"])
    let pid = intValue(args["pid"])
    let appNeedle = stringValue(args["app"] ?? args["application"] ?? args["name"]).lowercased()
    let titleNeedle = stringValue(args["title"] ?? args["title_contains"]).lowercased()
    for window in windowRecords() {
        if explicitId > 0 && intValue(window["window_id"]) == explicitId {
            return window
        }
    }
    for window in windowRecords() {
        if pid > 0 && intValue(window["pid"]) != pid {
            continue
        }
        if !appNeedle.isEmpty && !stringValue(window["app"]).lowercased().contains(appNeedle) {
            continue
        }
        if !titleNeedle.isEmpty && !stringValue(window["title"]).lowercased().contains(titleNeedle) {
            continue
        }
        if pid > 0 || !appNeedle.isEmpty || !titleNeedle.isEmpty {
            return window
        }
    }
    return nil
}

func temporaryPngPath() -> String {
    let name = "rumi-mac-computer-\(UUID().uuidString).png"
    return URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(name).path
}

func screenshotPayload(args: [String: Any]) -> (payload: [String: Any]?, code: String, message: String) {
    let path = stringValue(args["output_path"] ?? args["path"]).isEmpty ? temporaryPngPath() : stringValue(args["output_path"] ?? args["path"])
    var command = ["-x"]
    var target: [String: Any] = [:]
    if let window = matchingWindow(args: args) {
        command.append(contentsOf: ["-l", stringValue(window["window_id"])])
        target = window
    } else {
        let x = intValue(args["x"])
        let y = intValue(args["y"])
        let width = intValue(args["width"])
        let height = intValue(args["height"])
        if width > 0 && height > 0 {
            command.append(contentsOf: ["-R", "\(x),\(y),\(width),\(height)"])
            target = ["x": x, "y": y, "width": width, "height": height, "screen": "rect"]
        } else {
            let bounds = CGDisplayBounds(CGMainDisplayID())
            target = ["x": Int(bounds.origin.x), "y": Int(bounds.origin.y), "width": Int(bounds.width), "height": Int(bounds.height), "screen": "main_display"]
        }
    }
    command.append(path)
    guard runProcess("/usr/sbin/screencapture", command) || runProcess("/usr/bin/screencapture", command) else {
        return (nil, "SCREENSHOT_FAILED", "screencapture failed for the requested macOS image.")
    }
    let size = imageSize(path)
    guard size.width > 0, size.height > 0 else {
        return (nil, "IMAGE_UNAVAILABLE", "screencapture produced an unreadable image.")
    }
    return ([
        "action": "computer.screenshot",
        "platform": "Darwin",
        "path": path,
        "width": size.width,
        "height": size.height,
        "method": "swift_screencapture",
        "coordinate_system": "screen_pixels",
        "driver": "mac_swift_host",
        "target_window": target
    ], "", "")
}

func captureScreenshot(args: [String: Any]) -> Never {
    let captured = screenshotPayload(args: args)
    guard let payload = captured.payload else {
        fail(captured.code, captured.message)
    }
    ok(payload)
}

func runProcess(_ executable: String, _ arguments: [String]) -> Bool {
    guard FileManager.default.isExecutableFile(atPath: executable) else {
        return false
    }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: executable)
    process.arguments = arguments
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus == 0
    } catch {
        return false
    }
}

func imageSize(_ path: String) -> (width: Int, height: Int) {
    guard let image = NSImage(contentsOfFile: path) else {
        return (0, 0)
    }
    if let rep = image.representations.first {
        return (rep.pixelsWide, rep.pixelsHigh)
    }
    return (Int(image.size.width), Int(image.size.height))
}

func hostCapabilities(accessibilityTrusted: Bool? = nil) -> [String: Bool] {
    let trusted = accessibilityTrusted ?? AXIsProcessTrusted()
    let visionAvailable: Bool
    if #available(macOS 10.15, *) {
        visionAvailable = true
    } else {
        visionAvailable = false
    }
    return [
        "can_capture_background_window": true,
        "can_foreground_action": true,
        "can_semantic_action": trusted,
        "can_ax_tree": trusted,
        "can_ocr": visionAvailable,
        "can_click_text": visionAvailable,
        "requires_user_permission": true
    ]
}

func axAttribute(_ element: AXUIElement, _ attribute: CFString) -> Any? {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, attribute, &value)
    if error != .success {
        return nil
    }
    return value
}

func axStringAttribute(_ element: AXUIElement, _ attribute: CFString) -> String {
    stringValue(axAttribute(element, attribute))
}

func axTextAttribute(_ element: AXUIElement, _ attribute: CFString) -> String? {
    guard let value = axAttribute(element, attribute) else {
        return nil
    }
    if let text = value as? String {
        return text
    }
    if let text = value as? NSString {
        return text as String
    }
    return nil
}

func axBoolAttribute(_ element: AXUIElement, _ attribute: CFString, default fallback: Bool = true) -> Bool {
    boolValue(axAttribute(element, attribute), default: fallback)
}

func axActions(_ element: AXUIElement) -> [String] {
    var actionNames: CFArray?
    let error = AXUIElementCopyActionNames(element, &actionNames)
    if error != .success {
        return []
    }
    return (actionNames as? [String]) ?? []
}

func asAXUIElement(_ value: Any) -> AXUIElement? {
    let cfValue = value as CFTypeRef
    if CFGetTypeID(cfValue) != AXUIElementGetTypeID() {
        return nil
    }
    return (value as! AXUIElement)
}

func asAXValue(_ value: Any?) -> AXValue? {
    guard let value else {
        return nil
    }
    let cfValue = value as CFTypeRef
    if CFGetTypeID(cfValue) != AXValueGetTypeID() {
        return nil
    }
    return (value as! AXValue)
}

func axChildren(_ element: AXUIElement) -> [AXUIElement] {
    guard let value = axAttribute(element, kAXChildrenAttribute as CFString) else {
        return []
    }
    if let children = value as? [AXUIElement] {
        return children
    }
    if let children = value as? [Any] {
        return children.compactMap { asAXUIElement($0) }
    }
    return []
}

enum SemanticChildrenOutcome: String {
    case children
    case provenEmpty
    case unknownBranch
    case staleElement
    case additionalReadBudgetExhausted
    case globalUnavailable
    case protocolInvalid
}

enum SemanticChildrenErrorClass: String, Hashable {
    case none
    case noValue
    case attributeUnsupported
    case cannotComplete
    case invalidElement
    case apiDisabled
    case notImplemented
    case illegalArgument
    case genericFailure
    case payloadTypeInvalid
    case additionalReadBudgetExhausted
    case multiple
}

// A failed AXChildren read may still independently establish an empty branch,
// but only through one of these closed structural observations.  Do not infer
// emptiness from an invalid AX element alone.
enum SemanticChildrenStructuralEmptyProof: String {
    case none
    case countZero = "count_zero"
    case attributeNotAdvertised = "attribute_not_advertised"
}

// This cap applies only to recovery-only AXChildren reads: a one-shot
// cannotComplete retry and the bounded parent-branch refresh.  Ordinary BFS
// reads remain bounded by the per-pass node/depth limits below.  The object is
// native-process-local and is deliberately never serialized.
final class SemanticAdditionalChildrenReadBudget {
    let limit: Int
    private(set) var consumed = 0
    private(set) var exhausted = false

    init(limit: Int = 64) {
        self.limit = max(0, min(64, limit))
    }

    func consume() -> Bool {
        guard consumed < limit else {
            exhausted = true
            return false
        }
        consumed += 1
        return true
    }
}

struct AXChildrenReadResult {
    let children: [AXUIElement]
    let outcome: SemanticChildrenOutcome
    let errorClass: SemanticChildrenErrorClass
    let observedErrorClasses: Set<SemanticChildrenErrorClass>
    let childrenAttributeAdvertised: Bool
    let childrenCountKnown: Bool
    let childrenCountNonzero: Bool
    let structuralEmptyProof: SemanticChildrenStructuralEmptyProof
    let retryAttempted: Bool
    let retryRecovered: Bool
    let additionalReadBudgetExhausted: Bool
    let readAttemptCount: Int

    var branchProvenEmpty: Bool { outcome == .provenEmpty }
    var scanIncomplete: Bool {
        [
            .unknownBranch, .staleElement, .additionalReadBudgetExhausted,
            .globalUnavailable, .protocolInvalid
        ].contains(outcome)
    }
}

enum SemanticNavigationOrderFallbackOutcome: String, Hashable {
    case completeEmpty = "complete_empty"
    case completeChildren = "complete_children"
    case unavailable
    case incomplete
    case protocolInvalid = "protocol_invalid"
}

enum SemanticNavigationOrderFailureClass: String, Hashable {
    case none
    case notAdvertised = "not_advertised"
    case countUnavailable = "count_unavailable"
    case countOverLimit = "count_over_limit"
    case pageAXFailure = "page_ax_failure"
    case payloadInvalid = "payload_invalid"
    case countChanged = "count_changed"
    case duplicate
    case selfCycle = "self_cycle"
    case parentUnavailable = "parent_unavailable"
    case parentMismatch = "parent_mismatch"
}

enum SemanticNavigationOrderAXErrorClass: String, Hashable {
    case none
    case noValue = "no_value"
    case attributeUnsupported = "attribute_unsupported"
    case cannotComplete = "cannot_complete"
    case invalidElement = "invalid_element"
    case apiDisabled = "api_disabled"
    case notImplemented = "not_implemented"
    case illegalArgument = "illegal_argument"
    case generic
}

enum SemanticNavigationOrderCardinalityClass: String, Hashable {
    case zero
    case one
    case twoToEight = "two_to_eight"
    case nineTo64 = "nine_to_64"
    case sixtyFiveTo255 = "sixty_five_to_255"
    case overLimit = "over_limit"
    case unknown
}

enum SemanticNavigationOrderParentProof: String, Hashable {
    case notChecked = "not_checked"
    case empty
    case allDirect = "all_direct"
    case unavailable
    case mismatch
}

struct SemanticNavigationOrderReadResult {
    let children: [AXUIElement]
    let outcome: SemanticNavigationOrderFallbackOutcome
    let failureClass: SemanticNavigationOrderFailureClass
    let observedAXErrorClasses: Set<SemanticNavigationOrderAXErrorClass>
    let cardinalityClass: SemanticNavigationOrderCardinalityClass
    let parentProof: SemanticNavigationOrderParentProof
    let countStable: Bool
    let complete: Bool
    let pageReadCount: Int

    var succeeded: Bool {
        outcome == .completeEmpty || outcome == .completeChildren
    }
}

struct SemanticAuthoritativeChildrenResult {
    let effective: AXChildrenReadResult
    let navigationOrder: SemanticNavigationOrderReadResult?
}

func semanticNavigationOrderAXErrorClass(_ error: AXError) -> SemanticNavigationOrderAXErrorClass {
    switch error {
    case .success: return .none
    case .noValue: return .noValue
    case .attributeUnsupported: return .attributeUnsupported
    case .cannotComplete: return .cannotComplete
    case .invalidUIElement, .invalidUIElementObserver: return .invalidElement
    case .apiDisabled: return .apiDisabled
    case .notImplemented: return .notImplemented
    case .illegalArgument: return .illegalArgument
    default: return .generic
    }
}

func semanticNavigationOrderCardinality(_ count: CFIndex) -> SemanticNavigationOrderCardinalityClass {
    switch count {
    case 0: return .zero
    case 1: return .one
    case 2...8: return .twoToEight
    case 9...64: return .nineTo64
    case 65...255: return .sixtyFiveTo255
    default: return count > 255 ? .overLimit : .unknown
    }
}

// AXChildrenInNavigationOrder is accepted as an authoritative equivalent only
// for the same element whose primary AXChildren read returned invalidElement.
// The list is fully paged and every returned child's AXParent must point back
// to that source element. There are deliberately no retries or sleeps here.
func semanticNavigationOrderChildren(
    _ element: AXUIElement,
    pageSize: CFIndex = 32,
    maximumElements: CFIndex = 255,
    maximumPageReads: Int = 8,
    attributeInventory: (AXUIElement) -> (known: Bool, advertised: Bool) = { target in
        guard let names = semanticAttributeNames(target) else { return (false, false) }
        return (true, names.contains("AXChildrenInNavigationOrder"))
    },
    count: (AXUIElement) -> (AXError, CFIndex) = { target in
        var valueCount: CFIndex = 0
        let error = AXUIElementGetAttributeValueCount(
            target, "AXChildrenInNavigationOrder" as CFString, &valueCount
        )
        return (error, valueCount)
    },
    page: (AXUIElement, CFIndex, CFIndex) -> (AXError, Any?) = { target, index, length in
        var copied: CFArray?
        let error = AXUIElementCopyAttributeValues(
            target, "AXChildrenInNavigationOrder" as CFString,
            index, length, &copied
        )
        return (error, copied)
    },
    parent: (AXUIElement) -> (AXError, Any?) = { child in
        var copied: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(
            child, kAXParentAttribute as CFString, &copied
        )
        return (error, copied)
    }
) -> SemanticNavigationOrderReadResult {
    var observed: Set<SemanticNavigationOrderAXErrorClass> = [.invalidElement]
    let unavailable: (SemanticNavigationOrderFailureClass, Set<SemanticNavigationOrderAXErrorClass>)
        -> SemanticNavigationOrderReadResult = { failure, errors in
            SemanticNavigationOrderReadResult(
                children: [], outcome: .unavailable, failureClass: failure,
                observedAXErrorClasses: errors, cardinalityClass: .unknown,
                parentProof: .notChecked, countStable: false, complete: false,
                pageReadCount: 0
            )
        }
    let inventory = attributeInventory(element)
    guard inventory.known && inventory.advertised else {
        return unavailable(.notAdvertised, observed)
    }
    let firstCount = count(element)
    let firstCountError = semanticNavigationOrderAXErrorClass(firstCount.0)
    observed.insert(firstCountError)
    guard firstCount.0 == .success, firstCount.1 >= 0 else {
        return SemanticNavigationOrderReadResult(
            children: [], outcome: .incomplete, failureClass: .countUnavailable,
            observedAXErrorClasses: observed, cardinalityClass: .unknown,
            parentProof: .notChecked, countStable: false, complete: false,
            pageReadCount: 0
        )
    }
    let cardinality = semanticNavigationOrderCardinality(firstCount.1)
    let boundedMaximumElements = max(0, min(255, maximumElements))
    guard firstCount.1 <= boundedMaximumElements else {
        return SemanticNavigationOrderReadResult(
            children: [], outcome: .incomplete, failureClass: .countOverLimit,
            observedAXErrorClasses: observed, cardinalityClass: .overLimit,
            parentProof: .notChecked, countStable: false, complete: false,
            pageReadCount: 0
        )
    }
    let boundedPageSize = max(1, min(32, pageSize))
    let boundedPageReads = max(1, min(8, maximumPageReads))
    var children: [AXUIElement] = []
    var pageReadCount = 0
    var index: CFIndex = 0
    while index < firstCount.1 {
        guard pageReadCount < boundedPageReads else {
            return SemanticNavigationOrderReadResult(
                children: [], outcome: .incomplete, failureClass: .countOverLimit,
                observedAXErrorClasses: observed, cardinalityClass: cardinality,
                parentProof: .notChecked, countStable: false, complete: false,
                pageReadCount: pageReadCount
            )
        }
        let requested = min(boundedPageSize, firstCount.1 - index)
        pageReadCount += 1
        let read = page(element, index, requested)
        let pageError = semanticNavigationOrderAXErrorClass(read.0)
        observed.insert(pageError)
        guard read.0 == .success else {
            return SemanticNavigationOrderReadResult(
                children: [], outcome: .incomplete, failureClass: .pageAXFailure,
                observedAXErrorClasses: observed, cardinalityClass: cardinality,
                parentProof: .notChecked, countStable: false, complete: false,
                pageReadCount: pageReadCount
            )
        }
        guard let values = read.1 as? [Any], values.count == Int(requested) else {
            return SemanticNavigationOrderReadResult(
                children: [], outcome: .protocolInvalid, failureClass: .payloadInvalid,
                observedAXErrorClasses: observed, cardinalityClass: cardinality,
                parentProof: .notChecked, countStable: false, complete: false,
                pageReadCount: pageReadCount
            )
        }
        let converted = values.compactMap { asAXUIElement($0) }
        guard converted.count == values.count else {
            return SemanticNavigationOrderReadResult(
                children: [], outcome: .protocolInvalid, failureClass: .payloadInvalid,
                observedAXErrorClasses: observed, cardinalityClass: cardinality,
                parentProof: .notChecked, countStable: false, complete: false,
                pageReadCount: pageReadCount
            )
        }
        for child in converted {
            guard !CFEqual(child, element) else {
                return SemanticNavigationOrderReadResult(
                    children: [], outcome: .protocolInvalid, failureClass: .selfCycle,
                    observedAXErrorClasses: observed, cardinalityClass: cardinality,
                    parentProof: .mismatch, countStable: false, complete: false,
                    pageReadCount: pageReadCount
                )
            }
            guard !children.contains(where: { CFEqual($0, child) }) else {
                return SemanticNavigationOrderReadResult(
                    children: [], outcome: .protocolInvalid, failureClass: .duplicate,
                    observedAXErrorClasses: observed, cardinalityClass: cardinality,
                    parentProof: .notChecked, countStable: false, complete: false,
                    pageReadCount: pageReadCount
                )
            }
            children.append(child)
        }
        index += requested
    }
    let secondCount = count(element)
    let secondCountError = semanticNavigationOrderAXErrorClass(secondCount.0)
    observed.insert(secondCountError)
    guard secondCount.0 == .success else {
        return SemanticNavigationOrderReadResult(
            children: [], outcome: .incomplete, failureClass: .countUnavailable,
            observedAXErrorClasses: observed, cardinalityClass: cardinality,
            parentProof: .notChecked, countStable: false, complete: false,
            pageReadCount: pageReadCount
        )
    }
    guard secondCount.1 == firstCount.1 else {
        return SemanticNavigationOrderReadResult(
            children: [], outcome: .incomplete, failureClass: .countChanged,
            observedAXErrorClasses: observed, cardinalityClass: cardinality,
            parentProof: .notChecked, countStable: false, complete: false,
            pageReadCount: pageReadCount
        )
    }
    if children.isEmpty {
        return SemanticNavigationOrderReadResult(
            children: [], outcome: .completeEmpty, failureClass: .none,
            observedAXErrorClasses: observed, cardinalityClass: .zero,
            parentProof: .empty, countStable: true, complete: true,
            pageReadCount: pageReadCount
        )
    }
    for child in children {
        let read = parent(child)
        let parentError = semanticNavigationOrderAXErrorClass(read.0)
        observed.insert(parentError)
        guard read.0 == .success, let payload = read.1,
              let actualParent = asAXUIElement(payload) else {
            return SemanticNavigationOrderReadResult(
                children: [], outcome: .incomplete, failureClass: .parentUnavailable,
                observedAXErrorClasses: observed, cardinalityClass: cardinality,
                parentProof: .unavailable, countStable: true, complete: false,
                pageReadCount: pageReadCount
            )
        }
        guard CFEqual(actualParent, element) else {
            return SemanticNavigationOrderReadResult(
                children: [], outcome: .protocolInvalid, failureClass: .parentMismatch,
                observedAXErrorClasses: observed, cardinalityClass: cardinality,
                parentProof: .mismatch, countStable: true, complete: false,
                pageReadCount: pageReadCount
            )
        }
    }
    return SemanticNavigationOrderReadResult(
        children: children, outcome: .completeChildren, failureClass: .none,
        observedAXErrorClasses: observed, cardinalityClass: cardinality,
        parentProof: .allDirect, countStable: true, complete: true,
        pageReadCount: pageReadCount
    )
}

func semanticAuthoritativeChildren(
    _ element: AXUIElement,
    primary: AXChildrenReadResult,
    navigationOrder: (AXUIElement) -> SemanticNavigationOrderReadResult = {
        semanticNavigationOrderChildren($0)
    }
) -> SemanticAuthoritativeChildrenResult {
    guard primary.outcome == .staleElement else {
        return SemanticAuthoritativeChildrenResult(effective: primary, navigationOrder: nil)
    }
    let fallback = navigationOrder(element)
    let fatalObserved: Set<SemanticChildrenErrorClass>
    let fatalOutcome: SemanticChildrenOutcome?
    if fallback.observedAXErrorClasses.contains(.apiDisabled) {
        fatalObserved = primary.observedErrorClasses.union([.apiDisabled])
        fatalOutcome = .globalUnavailable
    } else if fallback.observedAXErrorClasses.contains(.notImplemented) {
        fatalObserved = primary.observedErrorClasses.union([.notImplemented])
        fatalOutcome = .globalUnavailable
    } else if fallback.observedAXErrorClasses.contains(.illegalArgument)
                || fallback.outcome == .protocolInvalid {
        fatalObserved = primary.observedErrorClasses.union([.payloadTypeInvalid])
        fatalOutcome = .protocolInvalid
    } else {
        fatalObserved = primary.observedErrorClasses
        fatalOutcome = nil
    }
    if let fatalOutcome {
        let effective = AXChildrenReadResult(
            children: [], outcome: fatalOutcome,
            errorClass: semanticChildrenAggregateErrorClass(fatalObserved),
            observedErrorClasses: fatalObserved,
            childrenAttributeAdvertised: primary.childrenAttributeAdvertised,
            childrenCountKnown: primary.childrenCountKnown,
            childrenCountNonzero: primary.childrenCountNonzero,
            structuralEmptyProof: .none,
            retryAttempted: primary.retryAttempted,
            retryRecovered: false,
            additionalReadBudgetExhausted: primary.additionalReadBudgetExhausted,
            readAttemptCount: primary.readAttemptCount
        )
        return SemanticAuthoritativeChildrenResult(
            effective: effective, navigationOrder: fallback
        )
    }
    guard fallback.succeeded else {
        return SemanticAuthoritativeChildrenResult(effective: primary, navigationOrder: fallback)
    }
    let effective = AXChildrenReadResult(
        children: fallback.children,
        outcome: fallback.children.isEmpty ? .provenEmpty : .children,
        errorClass: .none, observedErrorClasses: [.none],
        childrenAttributeAdvertised: true, childrenCountKnown: true,
        childrenCountNonzero: !fallback.children.isEmpty,
        structuralEmptyProof: .none, retryAttempted: false, retryRecovered: false,
        additionalReadBudgetExhausted: false, readAttemptCount: 1
    )
    return SemanticAuthoritativeChildrenResult(effective: effective, navigationOrder: fallback)
}

func semanticChildrenErrorClass(_ error: AXError) -> SemanticChildrenErrorClass {
    switch error {
    case .success: return .none
    case .noValue: return .noValue
    case .attributeUnsupported: return .attributeUnsupported
    case .cannotComplete: return .cannotComplete
    case .invalidUIElement, .invalidUIElementObserver: return .invalidElement
    case .apiDisabled: return .apiDisabled
    case .notImplemented: return .notImplemented
    case .illegalArgument: return .illegalArgument
    default: return .genericFailure
    }
}

func semanticChildrenAggregateErrorClass(
    _ classes: Set<SemanticChildrenErrorClass>
) -> SemanticChildrenErrorClass {
    let material = classes.filter { $0 != .none }
    if material.isEmpty { return .none }
    if material.count == 1 { return material.first! }
    return .multiple
}

func semanticChildrenPayload(_ payload: Any?) -> [AXUIElement]? {
    guard let values = payload as? [Any] else { return nil }
    let children = values.compactMap { asAXUIElement($0) }
    return children.count == values.count ? children : nil
}

func semanticChildrenSupportedAttributes(_ element: AXUIElement) -> (known: Bool, advertised: Bool) {
    var copied: CFArray?
    guard AXUIElementCopyAttributeNames(element, &copied) == .success,
          let names = copied as? [String]
    else {
        return (false, false)
    }
    return (true, names.contains(kAXChildrenAttribute as String))
}

func semanticAttributeNames(_ element: AXUIElement) -> Set<String>? {
    var copied: CFArray?
    guard AXUIElementCopyAttributeNames(element, &copied) == .success,
          let names = copied as? [String]
    else { return nil }
    return Set(names)
}

func semanticFixedAttributeInventory(_ element: AXUIElement) -> SemanticFixedAttributeInventory {
    guard let names = semanticAttributeNames(element) else {
        return SemanticFixedAttributeInventory(
            known: false, contents: false, visibleChildren: false, navigationOrder: false,
            sharedText: false, titleUIElement: false, servesAsTitle: false,
            linkedUIElements: false, parent: false
        )
    }
    return SemanticFixedAttributeInventory(
        known: true,
        contents: names.contains("AXContents"),
        visibleChildren: names.contains("AXVisibleChildren"),
        navigationOrder: names.contains("AXChildrenInNavigationOrder"),
        sharedText: names.contains("AXSharedTextUIElements"),
        titleUIElement: names.contains("AXTitleUIElement"),
        servesAsTitle: names.contains("AXServesAsTitleForUIElements"),
        linkedUIElements: names.contains("AXLinkedUIElements"),
        parent: names.contains("AXParent")
    )
}

func semanticFixedParameterizedInventory(_ element: AXUIElement) -> SemanticFixedParameterizedInventory {
    var copied: CFArray?
    guard AXUIElementCopyParameterizedAttributeNames(element, &copied) == .success,
          let names = copied as? [String]
    else {
        return SemanticFixedParameterizedInventory(
            known: false, searchPredicate: false,
            elementForTextMarker: false, textMarkerRangeForElement: false
        )
    }
    let inventory = Set(names)
    return SemanticFixedParameterizedInventory(
        known: true,
        searchPredicate: inventory.contains("AXUIElementsForSearchPredicate"),
        elementForTextMarker: inventory.contains("AXUIElementForTextMarker"),
        textMarkerRangeForElement: inventory.contains("AXTextMarkerRangeForUIElement")
    )
}

func semanticElementListAttribute(
    _ element: AXUIElement,
    attribute: String,
    maximumElements: Int = 8,
    maximumReadAttempts: Int = 2,
    read: (AXUIElement, CFString) -> (AXError, Any?) = { target, name in
        var copied: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(target, name, &copied)
        return (error, copied)
    },
    retryPause: () -> Void = { usleep(20_000) }
) -> SemanticElementListReadResult {
    let attemptLimit = max(1, min(2, maximumReadAttempts))
    var attempts = 1
    var (error, payload) = read(element, attribute as CFString)
    if error == .cannotComplete && attemptLimit > 1 {
        retryPause()
        attempts += 1
        let retry = read(element, attribute as CFString)
        error = retry.0
        payload = retry.1
    }
    if error == .noValue || error == .attributeUnsupported {
        return SemanticElementListReadResult(
            elements: [], complete: true, truncated: false,
            readAttempts: attempts, failed: false,
            outcome: .empty, cardinalityClass: .zero
        )
    }
    guard error == .success else {
        return SemanticElementListReadResult(
            elements: [], complete: false, truncated: false,
            readAttempts: attempts, failed: true,
            outcome: .axFailure, cardinalityClass: .unknown
        )
    }
    guard let payload else {
        return SemanticElementListReadResult(
            elements: [], complete: false, truncated: false,
            readAttempts: attempts, failed: true,
            outcome: .payloadMissing, cardinalityClass: .unknown
        )
    }
    let converted: [AXUIElement]
    if let one = asAXUIElement(payload) {
        converted = [one]
    } else if let values = payload as? [Any] {
        converted = values.compactMap { asAXUIElement($0) }
        guard converted.count == values.count else {
            return SemanticElementListReadResult(
                elements: [], complete: false, truncated: false,
                readAttempts: attempts, failed: true,
                outcome: .payloadMixed, cardinalityClass: .unknown
            )
        }
    } else {
        return SemanticElementListReadResult(
            elements: [], complete: false, truncated: false,
            readAttempts: attempts, failed: true,
            outcome: .payloadInvalid, cardinalityClass: .unknown
        )
    }
    let cap = max(0, min(8, maximumElements))
    let outcome: SemanticElementListOutcome
    let cardinalityClass: SemanticElementListCardinalityClass
    if converted.isEmpty {
        outcome = .empty
        cardinalityClass = .zero
    } else if converted.count > cap {
        outcome = .fanoutTruncated
        cardinalityClass = .overCap
    } else if converted.count == 1 {
        outcome = .complete
        cardinalityClass = .one
    } else {
        outcome = .complete
        cardinalityClass = .twoToCap
    }
    return SemanticElementListReadResult(
        elements: Array(converted.prefix(cap)), complete: converted.count <= cap,
        truncated: converted.count > cap, readAttempts: attempts, failed: false,
        outcome: outcome, cardinalityClass: cardinalityClass
    )
}

func semanticChildrenCount(_ element: AXUIElement) -> (known: Bool, count: CFIndex) {
    var count: CFIndex = 0
    let error = AXUIElementGetAttributeValueCount(element, kAXChildrenAttribute as CFString, &count)
    return error == .success ? (true, max(0, count)) : (false, 0)
}

func semanticChildren(
    _ element: AXUIElement,
    read: (AXUIElement) -> (AXError, Any?) = { target in
        var copied: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(target, kAXChildrenAttribute as CFString, &copied)
        return (error, copied)
    },
    supportedAttributes: (AXUIElement) -> (known: Bool, advertised: Bool) = semanticChildrenSupportedAttributes,
    count: (AXUIElement) -> (known: Bool, count: CFIndex) = semanticChildrenCount,
    additionalReadBudget: SemanticAdditionalChildrenReadBudget? = nil,
    countInitialReadAgainstAdditionalBudget: Bool = false,
    retryPause: () -> Void = { usleep(20_000) }
) -> AXChildrenReadResult {
    var observed = Set<SemanticChildrenErrorClass>()
    var retryAttempted = false
    var retryRecovered = false
    var additionalReadBudgetExhausted = false
    if countInitialReadAgainstAdditionalBudget,
       let additionalReadBudget,
       !additionalReadBudget.consume() {
        observed.insert(.additionalReadBudgetExhausted)
        return AXChildrenReadResult(
            children: [], outcome: .additionalReadBudgetExhausted,
            errorClass: .additionalReadBudgetExhausted,
            observedErrorClasses: observed,
            childrenAttributeAdvertised: false, childrenCountKnown: false,
            childrenCountNonzero: false, structuralEmptyProof: .none,
            retryAttempted: false,
            retryRecovered: false, additionalReadBudgetExhausted: true,
            readAttemptCount: 0
        )
    }
    var (error, payload) = read(element)
    var readAttemptCount = 1
    var errorClass = semanticChildrenErrorClass(error)
    observed.insert(errorClass)
    if error == .cannotComplete {
        retryAttempted = true
        if let additionalReadBudget, !additionalReadBudget.consume() {
            additionalReadBudgetExhausted = true
            observed.insert(.additionalReadBudgetExhausted)
            return AXChildrenReadResult(
                children: [], outcome: .additionalReadBudgetExhausted,
                errorClass: semanticChildrenAggregateErrorClass(observed),
                observedErrorClasses: observed,
                childrenAttributeAdvertised: false, childrenCountKnown: false,
                childrenCountNonzero: false, structuralEmptyProof: .none,
                retryAttempted: retryAttempted,
                retryRecovered: false, additionalReadBudgetExhausted: true,
                readAttemptCount: readAttemptCount
            )
        }
        retryPause()
        let retried = read(element)
        readAttemptCount += 1
        error = retried.0
        payload = retried.1
        errorClass = semanticChildrenErrorClass(error)
        observed.insert(errorClass)
        retryRecovered = error == .success
    }
    if error == .success {
        guard let children = semanticChildrenPayload(payload) else {
            observed.insert(.payloadTypeInvalid)
            return AXChildrenReadResult(
                children: [], outcome: .protocolInvalid,
                errorClass: semanticChildrenAggregateErrorClass(observed),
                observedErrorClasses: observed,
                childrenAttributeAdvertised: false, childrenCountKnown: false,
                childrenCountNonzero: false, structuralEmptyProof: .none,
                retryAttempted: retryAttempted,
                retryRecovered: false,
                additionalReadBudgetExhausted: additionalReadBudgetExhausted,
                readAttemptCount: readAttemptCount
            )
        }
        return AXChildrenReadResult(
            children: children, outcome: children.isEmpty ? .provenEmpty : .children,
            errorClass: semanticChildrenAggregateErrorClass(observed),
            observedErrorClasses: observed,
            childrenAttributeAdvertised: true, childrenCountKnown: true,
            childrenCountNonzero: !children.isEmpty, structuralEmptyProof: .none,
            retryAttempted: retryAttempted,
            retryRecovered: retryRecovered,
            additionalReadBudgetExhausted: additionalReadBudgetExhausted,
            readAttemptCount: readAttemptCount
        )
    }
    if error == .attributeUnsupported || error == .noValue {
        return AXChildrenReadResult(
            children: [], outcome: .provenEmpty,
            errorClass: semanticChildrenAggregateErrorClass(observed),
            observedErrorClasses: observed,
            childrenAttributeAdvertised: false, childrenCountKnown: false,
            childrenCountNonzero: false, structuralEmptyProof: .none,
            retryAttempted: retryAttempted,
            retryRecovered: false,
            additionalReadBudgetExhausted: additionalReadBudgetExhausted,
            readAttemptCount: readAttemptCount
        )
    }
    let support = supportedAttributes(element)
    let cardinality = count(element)
    // An advertised-absent attribute cannot simultaneously report a nonzero
    // child count. Treat that as a protocol contradiction rather than turning
    // a stale reference into a deceptively complete empty branch.
    let structuralContradiction = support.known && !support.advertised
        && cardinality.known && cardinality.count > 0
    if structuralContradiction {
        observed.insert(.payloadTypeInvalid)
        return AXChildrenReadResult(
            children: [], outcome: .protocolInvalid,
            errorClass: semanticChildrenAggregateErrorClass(observed),
            observedErrorClasses: observed,
            childrenAttributeAdvertised: support.advertised,
            childrenCountKnown: cardinality.known,
            childrenCountNonzero: true, structuralEmptyProof: .none,
            retryAttempted: retryAttempted, retryRecovered: false,
            additionalReadBudgetExhausted: additionalReadBudgetExhausted,
            readAttemptCount: readAttemptCount
        )
    }
    // invalidElement is a stale-reference signal, not an empty-branch proof.
    // It can be downgraded to a completed empty branch only when the separate
    // attribute inventory or count read proves that fact.
    let structuralProofAllowed = errorClass == .cannotComplete
        || errorClass == .genericFailure || errorClass == .invalidElement
    let structuralEmptyProof: SemanticChildrenStructuralEmptyProof
    if cardinality.known && cardinality.count == 0 {
        structuralEmptyProof = .countZero
    } else if support.known && !support.advertised {
        structuralEmptyProof = .attributeNotAdvertised
    } else {
        structuralEmptyProof = .none
    }
    if structuralProofAllowed && structuralEmptyProof != .none {
        return AXChildrenReadResult(
            children: [], outcome: .provenEmpty,
            errorClass: semanticChildrenAggregateErrorClass(observed),
            observedErrorClasses: observed,
            childrenAttributeAdvertised: support.advertised,
            childrenCountKnown: cardinality.known,
            childrenCountNonzero: false, structuralEmptyProof: structuralEmptyProof,
            retryAttempted: retryAttempted,
            retryRecovered: false,
            additionalReadBudgetExhausted: additionalReadBudgetExhausted,
            readAttemptCount: readAttemptCount
        )
    }
    let outcome: SemanticChildrenOutcome
    switch errorClass {
    case .invalidElement: outcome = .staleElement
    case .apiDisabled, .notImplemented: outcome = .globalUnavailable
    case .illegalArgument, .payloadTypeInvalid: outcome = .protocolInvalid
    default: outcome = .unknownBranch
    }
    return AXChildrenReadResult(
        children: [], outcome: outcome,
        errorClass: semanticChildrenAggregateErrorClass(observed),
        observedErrorClasses: observed,
        childrenAttributeAdvertised: support.advertised,
        childrenCountKnown: cardinality.known,
        childrenCountNonzero: cardinality.known && cardinality.count > 0,
        structuralEmptyProof: .none, retryAttempted: retryAttempted,
        retryRecovered: false,
        additionalReadBudgetExhausted: additionalReadBudgetExhausted,
        readAttemptCount: readAttemptCount
    )
}

func axPoint(_ value: Any?) -> CGPoint? {
    guard let axValue = asAXValue(value), AXValueGetType(axValue) == .cgPoint else {
        return nil
    }
    var point = CGPoint.zero
    if AXValueGetValue(axValue, .cgPoint, &point) {
        return point
    }
    return nil
}

func axSize(_ value: Any?) -> CGSize? {
    guard let axValue = asAXValue(value), AXValueGetType(axValue) == .cgSize else {
        return nil
    }
    var size = CGSize.zero
    if AXValueGetValue(axValue, .cgSize, &size) {
        return size
    }
    return nil
}

func axRange(_ value: Any?) -> CFRange? {
    guard let axValue = asAXValue(value), AXValueGetType(axValue) == .cfRange else {
        return nil
    }
    var range = CFRange()
    if AXValueGetValue(axValue, .cfRange, &range) {
        return range
    }
    return nil
}

func axFrame(_ element: AXUIElement) -> [String: Any] {
    guard
        let position = axPoint(axAttribute(element, kAXPositionAttribute as CFString)),
        let size = axSize(axAttribute(element, kAXSizeAttribute as CFString))
    else {
        return [:]
    }
    return [
        "x": Double(position.x),
        "y": Double(position.y),
        "width": Double(size.width),
        "height": Double(size.height),
        "center": [
            "x": Double(position.x + size.width / 2.0),
            "y": Double(position.y + size.height / 2.0)
        ],
        "coordinate_system": "screen_points"
    ]
}

func jsonScalar(_ value: Any?) -> Any {
    if let text = value as? String {
        return text
    }
    if let bool = value as? Bool {
        return bool
    }
    if let number = value as? NSNumber {
        return number
    }
    let text = stringValue(value)
    return text
}

func axElementId(pid: pid_t, path: String) -> String {
    "ax:\(Int(pid)):\(path)"
}

func axSummary(_ element: AXUIElement, pid: pid_t, path: String) -> [String: Any] {
    let role = axStringAttribute(element, kAXRoleAttribute as CFString)
    let title = axStringAttribute(element, kAXTitleAttribute as CFString)
    let description = axStringAttribute(element, kAXDescriptionAttribute as CFString)
    let roleDescription = axStringAttribute(element, kAXRoleDescriptionAttribute as CFString)
    let value = jsonScalar(axAttribute(element, kAXValueAttribute as CFString))
    let valueText = stringValue(value)
    let actions = axActions(element)
    var summary: [String: Any] = [
        "id": axElementId(pid: pid, path: path),
        "path": path,
        "pid": Int(pid),
        "role": role,
        "title": title,
        "description": description,
        "enabled": axBoolAttribute(element, kAXEnabledAttribute as CFString, default: true),
        "frame": axFrame(element),
        "actions": actions
    ]
    if !roleDescription.isEmpty {
        summary["role_description"] = roleDescription
    }
    if !valueText.isEmpty {
        summary["value"] = value
    }
    return summary
}

func buildAXTree(
    element: AXUIElement,
    pid: pid_t,
    path: String,
    depth: Int,
    maxDepth: Int,
    maxElements: Int,
    elements: inout [[String: Any]]
) -> [String: Any] {
    var node = axSummary(element, pid: pid, path: path)
    if elements.count >= maxElements {
        node["truncated"] = true
        return node
    }
    elements.append(node)
    if depth >= maxDepth {
        return node
    }
    var childrenPayload: [[String: Any]] = []
    let children = axChildren(element)
    for (index, child) in children.enumerated() {
        if elements.count >= maxElements {
            node["truncated"] = true
            break
        }
        childrenPayload.append(buildAXTree(
            element: child,
            pid: pid,
            path: "\(path).\(index)",
            depth: depth + 1,
            maxDepth: maxDepth,
            maxElements: maxElements,
            elements: &elements
        ))
    }
    if !childrenPayload.isEmpty {
        node["children"] = childrenPayload
    }
    return node
}

func targetPid(args: [String: Any]) -> pid_t {
    let explicitPid = intValue(args["pid"])
    if explicitPid > 0 {
        return pid_t(explicitPid)
    }
    if let window = matchingWindow(args: args) {
        let pid = intValue(window["pid"])
        if pid > 0 {
            return pid_t(pid)
        }
    }
    let nameNeedle = stringValue(args["app"] ?? args["application"] ?? args["name"]).lowercased()
    let bundleNeedle = stringValue(args["bundle_id"] ?? args["bundleIdentifier"]).lowercased()
    if !nameNeedle.isEmpty || !bundleNeedle.isEmpty {
        for app in NSWorkspace.shared.runningApplications {
            let appName = (app.localizedName ?? "").lowercased()
            let bundleId = (app.bundleIdentifier ?? "").lowercased()
            if (!bundleNeedle.isEmpty && bundleId.contains(bundleNeedle))
                || (!nameNeedle.isEmpty && (appName.contains(nameNeedle) || bundleId.contains(nameNeedle))) {
                return app.processIdentifier
            }
        }
    }
    return frontmostPid()
}

func axWindows(_ appElement: AXUIElement) -> [AXUIElement] {
    guard let value = axAttribute(appElement, kAXWindowsAttribute as CFString) else {
        return []
    }
    if let windows = value as? [AXUIElement] {
        return windows
    }
    if let windows = value as? [Any] {
        return windows.compactMap { asAXUIElement($0) }
    }
    return []
}

func selectedAXRoot(args: [String: Any]) -> (pid: pid_t, root: AXUIElement, targetWindow: [String: Any])? {
    let pid = targetPid(args: args)
    guard pid > 0 else {
        return nil
    }
    let appElement = AXUIElementCreateApplication(pid)
    let cgWindow = matchingWindow(args: args)
    let requestedTitle = stringValue(
        args["title"] ?? args["window_title"] ?? args["title_contains"] ?? cgWindow?["title"]
    ).lowercased()
    if !requestedTitle.isEmpty {
        for window in axWindows(appElement) {
            let axTitle = axStringAttribute(window, kAXTitleAttribute as CFString).lowercased()
            if !axTitle.isEmpty && (axTitle.contains(requestedTitle) || requestedTitle.contains(axTitle)) {
                return (pid, window, cgWindow ?? ["pid": Int(pid), "title": axTitle])
            }
        }
    }
    if let focusedValue = axAttribute(appElement, kAXFocusedWindowAttribute as CFString),
       let focused = asAXUIElement(focusedValue) {
        return (pid, focused, cgWindow ?? ["pid": Int(pid)])
    }
    if let first = axWindows(appElement).first {
        return (pid, first, cgWindow ?? ["pid": Int(pid)])
    }
    return (pid, appElement, cgWindow ?? ["pid": Int(pid)])
}

func axTreePayload(args: [String: Any]) -> [String: Any] {
    let trusted = AXIsProcessTrusted()
    var payload: [String: Any] = [
        "action": "computer.ax_tree",
        "platform": "Darwin",
        "driver": "mac_swift_host",
        "elements": [],
        "capabilities": hostCapabilities(accessibilityTrusted: trusted)
    ]
    guard trusted else {
        payload["ax_tree"] = [
            "error_code": "ACCESSIBILITY_NOT_TRUSTED",
            "error": "macOS Accessibility permission is required to read AX elements."
        ]
        return payload
    }
    guard let selected = selectedAXRoot(args: args) else {
        payload["ax_tree"] = [
            "error_code": "AX_TARGET_NOT_FOUND",
            "error": "No macOS Accessibility target matched the request."
        ]
        return payload
    }
    let maxDepth = max(1, intValue(args["max_depth"], default: 8))
    let maxElements = max(1, intValue(args["max_elements"], default: 500))
    var elements: [[String: Any]] = []
    let root = buildAXTree(
        element: selected.root,
        pid: selected.pid,
        path: "0",
        depth: 0,
        maxDepth: maxDepth,
        maxElements: maxElements,
        elements: &elements
    )
    payload["ax_tree"] = [
        "root": root,
        "coordinate_system": "screen_points",
        "element_count": elements.count,
        "truncated": elements.count >= maxElements
    ]
    payload["elements"] = elements
    payload["target_window"] = selected.targetWindow
    return payload
}

func axTree(args: [String: Any]) -> Never {
    ok(axTreePayload(args: args))
}

func observe(args: [String: Any]) -> Never {
    let captured = screenshotPayload(args: args)
    guard var payload = captured.payload else {
        fail(captured.code, captured.message)
    }
    payload["action"] = "computer.observe"
    let axPayload = axTreePayload(args: args)
    if let axTree = axPayload["ax_tree"] {
        payload["ax_tree"] = axTree
    }
    if let elements = axPayload["elements"] {
        payload["elements"] = elements
    }
    if let capabilities = axPayload["capabilities"] {
        payload["capabilities"] = capabilities
    }
    if let targetWindow = axPayload["target_window"] as? [String: Any], !targetWindow.isEmpty {
        payload["target_window"] = targetWindow
    }
    ok(payload)
}

func textTokens(_ text: String) -> [String] {
    let normalized = text.lowercased()
        .components(separatedBy: CharacterSet.whitespacesAndNewlines.union(.punctuationCharacters).union(.symbols))
        .filter { !$0.isEmpty }
    let stopWords: Set<String> = [
        "a", "an", "and", "button", "click", "control", "element", "for", "item",
        "link", "menu", "on", "open", "press", "select", "tap", "the", "to"
    ]
    return normalized.filter { !stopWords.contains($0) }
}

func normalizedText(_ text: String) -> String {
    text.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
}

func containsLoose(_ haystack: String, _ needle: String) -> Bool {
    let h = normalizedText(haystack)
    let n = normalizedText(needle)
    if n.isEmpty {
        return false
    }
    if h.contains(n) {
        return true
    }
    let compactH = h.replacingOccurrences(of: " ", with: "")
    let compactN = n.replacingOccurrences(of: " ", with: "")
    return !compactN.isEmpty && compactH.contains(compactN)
}

func semanticText(args: [String: Any]) -> String {
    for key in ["text", "text_query", "match_text", "title", "label", "name", "value"] {
        let value = stringValue(args[key]).trimmingCharacters(in: .whitespacesAndNewlines)
        if !value.isEmpty {
            return value
        }
    }
    let intent = stringValue(args["intent"] ?? args["query"]).trimmingCharacters(in: .whitespacesAndNewlines)
    if intent.isEmpty {
        return ""
    }
    let tokens = textTokens(intent)
    return tokens.isEmpty ? intent : tokens.joined(separator: " ")
}

struct AXCandidate {
    let element: AXUIElement
    let summary: [String: Any]
    let score: Int
}

func scoreAXSummary(_ summary: [String: Any], args: [String: Any]) -> Int {
    let requestedText = semanticText(args: args)
    let requestedRole = stringValue(args["role"]).lowercased()
    let intent = stringValue(args["intent"] ?? args["query"])
    let title = stringValue(summary["title"])
    let description = stringValue(summary["description"])
    let value = stringValue(summary["value"])
    let role = stringValue(summary["role"]).lowercased()
    let roleDescription = stringValue(summary["role_description"]).lowercased()
    let actions = (summary["actions"] as? [String]) ?? []
    let enabled = boolValue(summary["enabled"], default: true)
    let haystack = [title, description, value, role, roleDescription].joined(separator: " ")
    var score = 0
    if actions.contains(kAXPressAction as String) {
        score += 25
    }
    if enabled {
        score += 5
    } else {
        score -= 100
    }
    if ["axbutton", "axmenuitem", "axcheckbox", "axradiobutton", "axlink", "axtab"].contains(role) {
        score += 10
    }
    if !requestedRole.isEmpty && (role.contains(requestedRole) || roleDescription.contains(requestedRole)) {
        score += 35
    }
    if !requestedText.isEmpty {
        if normalizedText(title) == normalizedText(requestedText) || normalizedText(value) == normalizedText(requestedText) {
            score += 100
        } else if containsLoose(haystack, requestedText) {
            score += 70
        }
        let tokens = textTokens(requestedText)
        if !tokens.isEmpty {
            let matched = tokens.filter { containsLoose(haystack, $0) }.count
            score += matched * 12
            if matched == tokens.count {
                score += 30
            }
        }
    }
    let intentTokens = textTokens(intent)
    if !intentTokens.isEmpty {
        let matched = intentTokens.filter { containsLoose(haystack, $0) }.count
        score += matched * 8
        if matched == intentTokens.count {
            score += 20
        }
    }
    if requestedText.isEmpty && requestedRole.isEmpty && intentTokens.isEmpty {
        return 0
    }
    return score
}

func collectAXCandidates(
    element: AXUIElement,
    pid: pid_t,
    path: String,
    args: [String: Any],
    depth: Int,
    maxDepth: Int,
    maxElements: Int,
    visited: inout Int,
    candidates: inout [AXCandidate]
) {
    if visited >= maxElements {
        return
    }
    visited += 1
    let summary = axSummary(element, pid: pid, path: path)
    let score = scoreAXSummary(summary, args: args)
    if score > 25 {
        candidates.append(AXCandidate(element: element, summary: summary, score: score))
    }
    if depth >= maxDepth {
        return
    }
    for (index, child) in axChildren(element).enumerated() {
        collectAXCandidates(
            element: child,
            pid: pid,
            path: "\(path).\(index)",
            args: args,
            depth: depth + 1,
            maxDepth: maxDepth,
            maxElements: maxElements,
            visited: &visited,
            candidates: &candidates
        )
        if visited >= maxElements {
            break
        }
    }
}

func axPathFromElementId(_ elementId: String, expectedPid: pid_t) -> [Int]? {
    let parts = elementId.split(separator: ":", maxSplits: 2).map(String.init)
    guard parts.count == 3, parts[0] == "ax", Int(parts[1]) == Int(expectedPid) else {
        return nil
    }
    let indices = parts[2].split(separator: ".").compactMap { Int($0) }
    if indices.isEmpty || indices[0] != 0 {
        return nil
    }
    return indices
}

func resolveAXElement(root: AXUIElement, path: [Int]) -> AXUIElement? {
    var current = root
    for index in path.dropFirst() {
        let children = axChildren(current)
        guard index >= 0, index < children.count else {
            return nil
        }
        current = children[index]
    }
    return current
}

func pressAXElement(_ element: AXUIElement) -> AXError {
    AXUIElementPerformAction(element, kAXPressAction as CFString)
}

func ocrPayload(args: [String: Any]) -> (payload: [String: Any]?, code: String, message: String) {
    if #available(macOS 10.15, *) {
        return ocrPayloadAvailable(args: args)
    }
    return (nil, "VISION_UNAVAILABLE", "Vision text recognition requires macOS 10.15 or newer.")
}

@available(macOS 10.15, *)
func ocrPayloadAvailable(args: [String: Any]) -> (payload: [String: Any]?, code: String, message: String) {
    let captured = screenshotPayload(args: args)
    guard let screenshot = captured.payload else {
        return (nil, captured.code, captured.message)
    }
    let path = stringValue(screenshot["path"])
    let imageUrl = URL(fileURLWithPath: path)
    guard FileManager.default.isReadableFile(atPath: path) else {
        return (nil, "OCR_IMAGE_UNAVAILABLE", "OCR screenshot image was not readable.")
    }
    let width = max(1, intValue(screenshot["width"]))
    let height = max(1, intValue(screenshot["height"]))
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    if let languages = args["recognition_languages"] as? [String], !languages.isEmpty {
        request.recognitionLanguages = languages
    }
    let handler = VNImageRequestHandler(url: imageUrl, options: [:])
    do {
        try handler.perform([request])
    } catch {
        return (nil, "OCR_FAILED", "Vision OCR failed: \(String(describing: error))")
    }
    let observations = request.results ?? []
    var items: [[String: Any]] = []
    var textLines: [String] = []
    for observation in observations {
        guard let candidate = observation.topCandidates(1).first else {
            continue
        }
        let rect = observation.boundingBox
        let x = rect.origin.x * CGFloat(width)
        let y = (1.0 - rect.origin.y - rect.height) * CGFloat(height)
        let w = rect.width * CGFloat(width)
        let h = rect.height * CGFloat(height)
        let centerX = x + w / 2.0
        let centerY = y + h / 2.0
        let text = candidate.string
        textLines.append(text)
        items.append([
            "text": text,
            "bbox": [
                "x": Double(x),
                "y": Double(y),
                "width": Double(w),
                "height": Double(h)
            ],
            "center": [
                "x": Double(centerX),
                "y": Double(centerY)
            ],
            "confidence": Double(candidate.confidence),
            "coordinate_system": "screenshot_pixels_top_left"
        ])
    }
    return ([
        "action": "computer.ocr",
        "platform": "Darwin",
        "driver": "mac_swift_host",
        "executed": true,
        "text": textLines.joined(separator: "\n"),
        "items": items,
        "elements": items,
        "coordinate_system": "screenshot_pixels_top_left",
        "screenshot": screenshot
    ], "", "")
}

func ocr(args: [String: Any]) -> Never {
    let result = ocrPayload(args: args)
    guard let payload = result.payload else {
        fail(result.code, result.message, [
            "action": "computer.ocr",
            "platform": "Darwin",
            "driver": "mac_swift_host"
        ])
    }
    ok(payload)
}

func screenPointFromOCRCenter(_ center: [String: Any], screenshot: [String: Any]) -> CGPoint? {
    let imageWidth = Double(max(1, intValue(screenshot["width"])))
    let imageHeight = Double(max(1, intValue(screenshot["height"])))
    guard let target = screenshot["target_window"] as? [String: Any] else {
        return nil
    }
    let targetX = Double(intValue(target["x"]))
    let targetY = Double(intValue(target["y"]))
    let targetWidth = Double(max(1, intValue(target["width"], default: Int(imageWidth))))
    let targetHeight = Double(max(1, intValue(target["height"], default: Int(imageHeight))))
    let x = doubleValue(center["x"]) * targetWidth / imageWidth
    let y = doubleValue(center["y"]) * targetHeight / imageHeight
    return CGPoint(x: targetX + x, y: targetY + y)
}

func scoreOCRItem(_ item: [String: Any], query: String) -> Int {
    let text = stringValue(item["text"])
    if query.isEmpty || text.isEmpty {
        return 0
    }
    var score = 0
    if normalizedText(text) == normalizedText(query) {
        score += 100
    } else if containsLoose(text, query) || containsLoose(query, text) {
        score += 70
    }
    let tokens = textTokens(query)
    if !tokens.isEmpty {
        let matched = tokens.filter { containsLoose(text, $0) }.count
        score += matched * 15
        if matched == tokens.count {
            score += 25
        }
    }
    return score
}

func semanticOCRFallback(args: [String: Any], actionName: String, reason: String) -> Never {
    let query = semanticText(args: args)
    guard !query.isEmpty else {
        fail("SEMANTIC_TARGET_REQUIRED", "semantic_action requires element_id, text, title, role, or intent.", [
            "action": actionName,
            "platform": "Darwin",
            "driver": "mac_swift_host",
            "executed": false,
            "reason": reason
        ])
    }
    let ocrResult = ocrPayload(args: args)
    guard let payload = ocrResult.payload else {
        fail(ocrResult.code, ocrResult.message, [
            "action": actionName,
            "platform": "Darwin",
            "driver": "mac_swift_host",
            "executed": false,
            "reason": reason
        ])
    }
    let items = (payload["items"] as? [[String: Any]]) ?? []
    let ranked = items
        .map { (item: $0, score: scoreOCRItem($0, query: query)) }
        .filter { $0.score > 30 }
        .sorted { $0.score > $1.score }
    guard let match = ranked.first, let center = match.item["center"] as? [String: Any], let screenshot = payload["screenshot"] as? [String: Any] else {
        fail("SEMANTIC_TARGET_NOT_FOUND", "No AX element or OCR text matched the semantic request.", [
            "action": actionName,
            "platform": "Darwin",
            "driver": "mac_swift_host",
            "executed": false,
            "reason": reason,
            "query": query,
            "ocr": payload
        ])
    }
    guard let point = screenPointFromOCRCenter(center, screenshot: screenshot) else {
        fail("OCR_COORDINATE_UNAVAILABLE", "OCR matched text but could not map it to screen coordinates.", [
            "action": actionName,
            "platform": "Darwin",
            "driver": "mac_swift_host",
            "executed": false,
            "query": query,
            "match": match.item
        ])
    }
    CGWarpMouseCursorPosition(point)
    postMouse(.leftMouseDown, point: point, button: .left)
    usleep(35_000)
    postMouse(.leftMouseUp, point: point, button: .left)
    ok([
        "action": actionName,
        "platform": "Darwin",
        "driver": "mac_swift_host",
        "executed": true,
        "method": "ocr_click_fallback",
        "uses_physical_input": true,
        "query": query,
        "x": Int(point.x),
        "y": Int(point.y),
        "coordinate_system": "screen_points",
        "match": match.item,
        "ocr": payload,
        "fallback_reason": reason
    ])
}

func semanticAction(args: [String: Any], actionName: String = "computer.semantic_action") -> Never {
    let elementId = stringValue(args["element_id"] ?? args["id"])
    if !elementId.isEmpty && !AXIsProcessTrusted() {
        fail("ACCESSIBILITY_NOT_TRUSTED", "macOS Accessibility permission is required to press AX elements.", [
            "action": actionName,
            "platform": "Darwin",
            "driver": "mac_swift_host",
            "executed": false,
            "element_id": elementId
        ])
    }
    if AXIsProcessTrusted(), let selected = selectedAXRoot(args: args) {
        if !elementId.isEmpty {
            if let path = axPathFromElementId(elementId, expectedPid: selected.pid),
               let element = resolveAXElement(root: selected.root, path: path) {
                let error = pressAXElement(element)
                if error == .success {
                    ok([
                        "action": actionName,
                        "platform": "Darwin",
                        "driver": "mac_swift_host",
                        "executed": true,
                        "method": "ax_press",
                        "uses_physical_input": false,
                        "element": axSummary(element, pid: selected.pid, path: path.map(String.init).joined(separator: "."))
                    ])
                }
                fail("AX_PRESS_FAILED", "AXPress failed for element_id \(elementId): \(error.rawValue)", [
                    "action": actionName,
                    "platform": "Darwin",
                    "driver": "mac_swift_host",
                    "executed": false,
                    "element_id": elementId
                ])
            }
            fail("AX_ELEMENT_NOT_FOUND", "No AX element matched element_id \(elementId).", [
                "action": actionName,
                "platform": "Darwin",
                "driver": "mac_swift_host",
                "executed": false,
                "element_id": elementId
            ])
        }
        var visited = 0
        var candidates: [AXCandidate] = []
        collectAXCandidates(
            element: selected.root,
            pid: selected.pid,
            path: "0",
            args: args,
            depth: 0,
            maxDepth: max(1, intValue(args["max_depth"], default: 8)),
            maxElements: max(1, intValue(args["max_elements"], default: 500)),
            visited: &visited,
            candidates: &candidates
        )
        for candidate in candidates.sorted(by: { $0.score > $1.score }) {
            let error = pressAXElement(candidate.element)
            if error == .success {
                ok([
                    "action": actionName,
                    "platform": "Darwin",
                    "driver": "mac_swift_host",
                    "executed": true,
                    "method": "ax_press",
                    "uses_physical_input": false,
                    "score": candidate.score,
                    "element": candidate.summary
                ])
            }
        }
        semanticOCRFallback(args: args, actionName: actionName, reason: "No matching AX candidate could be pressed.")
    }
    if !elementId.isEmpty {
        fail("AX_TARGET_NOT_FOUND", "No macOS Accessibility target matched the element_id request.", [
            "action": actionName,
            "platform": "Darwin",
            "driver": "mac_swift_host",
            "executed": false,
            "element_id": elementId
        ])
    }
    semanticOCRFallback(args: args, actionName: actionName, reason: "Accessibility target unavailable.")
}

func postMouse(_ type: CGEventType, point: CGPoint, button: CGMouseButton) {
    let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: button)
    event?.post(tap: .cghidEventTap)
}

func mouseButton(_ raw: String) -> (CGMouseButton, CGEventType, CGEventType, CGEventType) {
    switch raw.lowercased() {
    case "right":
        return (.right, .rightMouseDown, .rightMouseUp, .rightMouseDragged)
    case "middle", "center":
        return (.center, .otherMouseDown, .otherMouseUp, .otherMouseDragged)
    default:
        return (.left, .leftMouseDown, .leftMouseUp, .leftMouseDragged)
    }
}

func move(args: [String: Any]) -> Never {
    let point = CGPoint(x: intValue(args["x"]), y: intValue(args["y"]))
    CGWarpMouseCursorPosition(point)
    CGAssociateMouseAndMouseCursorPosition(boolean_t(1))
    ok(["action": "computer.move", "platform": "Darwin", "executed": true, "x": Int(point.x), "y": Int(point.y), "driver": "mac_swift_host"])
}

func click(args: [String: Any]) -> Never {
    let point = CGPoint(x: intValue(args["x"]), y: intValue(args["y"]))
    let buttonSpec = mouseButton(stringValue(args["button"]).isEmpty ? "left" : stringValue(args["button"]))
    CGWarpMouseCursorPosition(point)
    postMouse(buttonSpec.1, point: point, button: buttonSpec.0)
    usleep(35_000)
    postMouse(buttonSpec.2, point: point, button: buttonSpec.0)
    ok(["action": "computer.click", "platform": "Darwin", "executed": true, "x": Int(point.x), "y": Int(point.y), "driver": "mac_swift_host"])
}

func drag(args: [String: Any]) -> Never {
    let start = CGPoint(x: intValue(args["x1"] ?? args["from_x"]), y: intValue(args["y1"] ?? args["from_y"]))
    let end = CGPoint(x: intValue(args["x2"] ?? args["to_x"]), y: intValue(args["y2"] ?? args["to_y"]))
    let buttonSpec = mouseButton(stringValue(args["button"]).isEmpty ? "left" : stringValue(args["button"]))
    CGWarpMouseCursorPosition(start)
    postMouse(buttonSpec.1, point: start, button: buttonSpec.0)
    let steps = 16
    for index in 1...steps {
        let t = CGFloat(index) / CGFloat(steps)
        let point = CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t)
        postMouse(buttonSpec.3, point: point, button: buttonSpec.0)
        usleep(10_000)
    }
    postMouse(buttonSpec.2, point: end, button: buttonSpec.0)
    ok(["action": "computer.drag", "platform": "Darwin", "executed": true, "driver": "mac_swift_host"])
}

struct FocusedTextInputState {
    let pid: pid_t
    let element: AXUIElement
    let value: String
    let selectedRange: CFRange
}

struct TextInsertionExpectation {
    let prefix: [UniChar]
    let inserted: [UniChar]
    let suffix: [UniChar]

    var finalValue: String {
        String(decoding: prefix + inserted + suffix, as: UTF16.self)
    }

    func matches(_ value: String) -> Bool {
        let units = Array(value.utf16)
        let requiredCount = prefix.count + inserted.count + suffix.count
        guard units.count == requiredCount else {
            return false
        }
        guard units.prefix(prefix.count).elementsEqual(prefix) else {
            return false
        }
        let insertedStart = prefix.count
        let insertedEnd = insertedStart + inserted.count
        guard units[insertedStart..<insertedEnd].elementsEqual(inserted) else {
            return false
        }
        return units.suffix(suffix.count).elementsEqual(suffix)
    }
}

enum DirectTextInsertionStatus {
    case unavailable
    case verified(strategy: String)
    case unverified(strategy: String, observedValue: String?)
}

struct PacedTextInsertionResult {
    let dispatchedUnitCount: Int
    let completionVerified: Bool
    let failureCode: String?
    let failureStage: String?
    let targetPidStable: Bool
    let focusedElementStable: Bool
}

struct TextInputTargetStability {
    let targetPidStable: Bool
    let focusedElementStable: Bool

    var isStable: Bool { targetPidStable && focusedElementStable }
}

func hasExplicitTextInputTarget(args: [String: Any]) -> Bool {
    for key in ["pid", "window_id", "windowId", "app", "application", "name", "bundle_id", "bundleIdentifier"] {
        if !stringValue(args[key]).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return true
        }
    }
    return false
}

// Resolving an explicit typing target must never silently fall back to the
// currently frontmost app: that could turn a target-binding failure into input
// for a different application. Keep the normal frontmost default only when the
// caller did not constrain the target at all.
func resolvedExplicitTextInputTargetPid(args: [String: Any]) -> pid_t? {
    guard hasExplicitTextInputTarget(args: args) else {
        return nil
    }
    let explicitPid = intValue(args["pid"])
    if explicitPid > 0 {
        let pid = pid_t(explicitPid)
        return NSRunningApplication(processIdentifier: pid) == nil ? nil : pid
    }
    if let window = matchingWindow(args: args) {
        let windowPid = intValue(window["pid"])
        if windowPid > 0 {
            let pid = pid_t(windowPid)
            if NSRunningApplication(processIdentifier: pid) != nil {
                return pid
            }
        }
    }
    let nameNeedle = stringValue(args["app"] ?? args["application"] ?? args["name"]).lowercased()
    let bundleNeedle = stringValue(args["bundle_id"] ?? args["bundleIdentifier"]).lowercased()
    for app in NSWorkspace.shared.runningApplications {
        let appName = (app.localizedName ?? "").lowercased()
        let bundleId = (app.bundleIdentifier ?? "").lowercased()
        if (!bundleNeedle.isEmpty && bundleId.contains(bundleNeedle))
            || (!nameNeedle.isEmpty && (appName.contains(nameNeedle) || bundleId.contains(nameNeedle))) {
            return app.processIdentifier
        }
    }
    return nil
}

func ensureResolvedTextInputTargetIsFrontmost(
    pid: pid_t,
    activate: (pid_t) -> Bool,
    frontmost: () -> pid_t,
    timeout: TimeInterval = 0.75,
    now: () -> TimeInterval = { Date().timeIntervalSinceReferenceDate },
    pause: () -> Void = { usleep(25_000) }
) -> Bool {
    guard pid > 0 else { return false }
    if frontmost() == pid { return true }
    guard activate(pid) else { return false }
    let deadline = now() + max(0, timeout)
    while frontmost() != pid && now() < deadline {
        pause()
    }
    return frontmost() == pid
}

func activateExactTextInputTarget(pid: pid_t) -> Bool {
    guard let app = NSRunningApplication(processIdentifier: pid) else {
        return false
    }
    return app.activate(options: [.activateAllWindows])
}

func focusedTextInputState(args: [String: Any], resolvedTargetPid: pid_t? = nil) -> FocusedTextInputState? {
    let pid = resolvedTargetPid ?? targetPid(args: args)
    guard pid > 0, pid == frontmostPid() else {
        return nil
    }
    let appElement = AXUIElementCreateApplication(pid)
    guard
        let focusedValue = axAttribute(appElement, kAXFocusedUIElementAttribute as CFString),
        let element = asAXUIElement(focusedValue),
        let value = axTextAttribute(element, kAXValueAttribute as CFString),
        let selectedRange = axRange(axAttribute(element, kAXSelectedTextRangeAttribute as CFString))
    else {
        return nil
    }
    return FocusedTextInputState(pid: pid, element: element, value: value, selectedRange: selectedRange)
}

func textInsertionExpectation(currentValue: String, selectedRange: CFRange, text: String) -> TextInsertionExpectation? {
    let currentUnits = Array(currentValue.utf16)
    let location = selectedRange.location
    let length = selectedRange.length
    guard location >= 0, length >= 0, location <= currentUnits.count, length <= currentUnits.count - location else {
        return nil
    }
    // AX text ranges use UTF-16 offsets. Reject a range boundary inside a
    // surrogate pair rather than constructing a lossy replacement String.
    func splitsSurrogatePair(_ offset: Int) -> Bool {
        guard offset > 0, offset < currentUnits.count else { return false }
        let previous = currentUnits[offset - 1]
        let next = currentUnits[offset]
        return (0xD800...0xDBFF).contains(previous) && (0xDC00...0xDFFF).contains(next)
    }
    guard !splitsSurrogatePair(location), !splitsSurrogatePair(location + length) else {
        return nil
    }
    return TextInsertionExpectation(
        prefix: Array(currentUnits[..<location]),
        inserted: Array(text.utf16),
        suffix: Array(currentUnits[(location + length)...])
    )
}

func directTextInsertion(
    expectation: TextInsertionExpectation,
    initialValue: String,
    valueSettable: Bool,
    selectedTextSettable: Bool,
    setValue: (String) -> Bool,
    setSelectedText: (String) -> Bool,
    insertedText: String,
    value: () -> String?,
    verificationTimeout: TimeInterval = 1.0,
    now: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
    pause: () -> Void = { usleep(5_000) }
) -> DirectTextInsertionStatus {
    let strategy: String
    let writeSucceeded: Bool
    if selectedTextSettable {
        strategy = "selected_text"
        writeSucceeded = setSelectedText(insertedText)
    } else if valueSettable {
        strategy = "ax_value"
        writeSucceeded = setValue(expectation.finalValue)
    } else {
        return .unavailable
    }

    // A write attempt may have partially mutated the control even when AX
    // reports an error. Never make a second physical-input attempt afterward.
    if writeSucceeded, waitForExpectedText(
        expectation,
        timeout: verificationTimeout,
        value: value,
        now: now,
        pause: pause
    ) {
        return .verified(strategy: strategy)
    }
    let observedValue = value()
    return .unverified(strategy: strategy, observedValue: observedValue)
}

struct ExactSemanticWindow {
    let pid: pid_t
    let windowId: Int
    let frame: CGRect
    let element: AXUIElement
}

struct ExactWindowResolutionFacts {
    var inputValid = false
    var runningAppPresent = false
    var quartzQueryCompleted = false
    var quartzRecordPresent = false
    var quartzOwnerMatches = false
    var quartzLayerAllowed = false
    var quartzVisible = false
    var quartzFrameMatches = false
    var axWindowsAttributeAvailable = false
    var axWindowsPayloadValid = false
    var axWindowsReadCompleted = false
    var axMatchPresent = false
    var axMatchUnique = false
    var resolved = false
    var retryAttempted = false
    var retryRecovered = false
    var frontmostCheckCompleted = false
    var targetNonFrontmostBefore = false
    var targetNonFrontmostAfter = false
    var frontmostUnchanged = false
    var attemptCount = 0
    var quartzRecordMatchCount = 0
    var axWindowCount = 0
    var axFrameValidCount = 0
    var axFrameMatchCount = 0
    var axWindowsOutcome = "protocol_invalid"
    var stage = "input_validation"
    var outcome = "input_invalid"

    func payload() -> [String: Any] {
        [
            "exact_binding_input_valid": inputValid,
            "exact_running_app_present": runningAppPresent,
            "exact_quartz_query_completed": quartzQueryCompleted,
            "exact_quartz_record_present": quartzRecordPresent,
            "exact_quartz_owner_matches": quartzOwnerMatches,
            "exact_quartz_layer_allowed": quartzLayerAllowed,
            "exact_quartz_visible": quartzVisible,
            "exact_quartz_frame_matches": quartzFrameMatches,
            "exact_ax_windows_attribute_available": axWindowsAttributeAvailable,
            "exact_ax_windows_payload_valid": axWindowsPayloadValid,
            "exact_ax_windows_read_completed": axWindowsReadCompleted,
            "exact_ax_match_present": axMatchPresent,
            "exact_ax_match_unique": axMatchUnique,
            "exact_window_resolved": resolved,
            "exact_resolution_retry_attempted": retryAttempted,
            "exact_resolution_retry_recovered": retryRecovered,
            "native_frontmost_check_completed": frontmostCheckCompleted,
            "native_target_non_frontmost_before": targetNonFrontmostBefore,
            "native_target_non_frontmost_after": targetNonFrontmostAfter,
            "native_frontmost_unchanged": frontmostUnchanged,
            "exact_resolution_attempt_count": min(2, attemptCount),
            "exact_quartz_record_match_count": min(2, quartzRecordMatchCount),
            "exact_ax_window_count": min(16, axWindowCount),
            "exact_ax_frame_valid_count": min(16, axFrameValidCount),
            "exact_ax_frame_match_count": min(8, axFrameMatchCount),
            "exact_ax_windows_outcome": axWindowsOutcome,
            "exact_resolution_stage": stage,
            "exact_resolution_outcome": outcome,
        ]
    }
}

struct ExactWindowResolution {
    let window: ExactSemanticWindow?
    var facts: ExactWindowResolutionFacts
    let errorCode: String
}

struct ExactAXWindowsReadResult {
    let windows: [AXUIElement]
    let outcome: String
    let attributeAvailable: Bool
    let payloadValid: Bool
    let completed: Bool
    let retryEligible: Bool
}

struct SemanticTextSelector {
    let roles: Set<String>
    let forbiddenAncestorRoles: Set<String>
    let minX: Double
    let maxX: Double
    let minY: Double
    let maxY: Double
    let requireEnabled: Bool
    let requireSettable: Bool
    let requireBackground: Bool
    let preference: String
}

func rectNearlyMatches(_ lhs: CGRect, _ rhs: CGRect, tolerance: CGFloat = 4.0) -> Bool {
    abs(lhs.origin.x - rhs.origin.x) <= tolerance
        && abs(lhs.origin.y - rhs.origin.y) <= tolerance
        && abs(lhs.size.width - rhs.size.width) <= tolerance
        && abs(lhs.size.height - rhs.size.height) <= tolerance
}

func exactAXWindows(_ appElement: AXUIElement) -> ExactAXWindowsReadResult {
    var settable: DarwinBoolean = false
    let supported = AXUIElementIsAttributeSettable(
        appElement, kAXWindowsAttribute as CFString, &settable
    ) != .attributeUnsupported
    var raw: CFTypeRef?
    var error = AXUIElementCopyAttributeValue(appElement, kAXWindowsAttribute as CFString, &raw)
    if error == .cannotComplete {
        usleep(20_000)
        error = AXUIElementCopyAttributeValue(appElement, kAXWindowsAttribute as CFString, &raw)
    }
    guard error == .success else {
        let outcome: String
        switch error {
        case .noValue: outcome = "no_value"
        case .attributeUnsupported: outcome = "unsupported"
        case .cannotComplete: outcome = "cannot_complete"
        case .invalidUIElement: outcome = "invalid_application_element"
        default: outcome = "global_failure"
        }
        return ExactAXWindowsReadResult(
            windows: [], outcome: outcome, attributeAvailable: supported,
            payloadValid: false, completed: false, retryEligible: error == .cannotComplete
        )
    }
    guard let value = raw else {
        return ExactAXWindowsReadResult(
            windows: [], outcome: "no_value", attributeAvailable: supported,
            payloadValid: false, completed: false, retryEligible: false
        )
    }
    let windows: [AXUIElement]
    if let typed = value as? [AXUIElement] {
        windows = typed
    } else if let values = value as? [Any], values.allSatisfy({ asAXUIElement($0) != nil }) {
        windows = values.compactMap { asAXUIElement($0) }
    } else {
        return ExactAXWindowsReadResult(
            windows: [], outcome: "protocol_invalid", attributeAvailable: supported,
            payloadValid: false, completed: false, retryEligible: false
        )
    }
    return ExactAXWindowsReadResult(
        windows: windows, outcome: "success", attributeAvailable: supported,
        payloadValid: true, completed: true, retryEligible: false
    )
}

func exactQuartzWindowRecord(pid: Int, windowId: Int) -> (record: [String: Any]?, facts: ExactWindowResolutionFacts) {
    var facts = ExactWindowResolutionFacts()
    let info = CGWindowListCopyWindowInfo(
        [.optionIncludingWindow, .excludeDesktopElements], CGWindowID(windowId)
    ) as? [[String: Any]]
    facts.quartzQueryCompleted = info != nil
    let matches = (info ?? []).filter { intValue($0[kCGWindowNumber as String]) == windowId }
    facts.quartzRecordMatchCount = min(2, matches.count)
    facts.quartzRecordPresent = matches.count == 1
    guard matches.count == 1, let record = matches.first else { return (nil, facts) }
    facts.quartzOwnerMatches = intValue(record[kCGWindowOwnerPID as String]) == pid
    facts.quartzLayerAllowed = intValue(record[kCGWindowLayer as String]) == 0
    facts.quartzVisible = boolValue(record[kCGWindowIsOnscreen as String])
    return (record, facts)
}

func resolveExactSemanticWindowOnce(args: [String: Any]) -> ExactWindowResolution {
    var facts = ExactWindowResolutionFacts()
    let pid = intValue(args["pid"])
    let windowId = intValue(args["window_id"])
    let width = doubleValue(args["window_width"])
    let height = doubleValue(args["window_height"])
    guard pid > 0, windowId > 0, width > 0, height > 0 else {
        return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_INPUT_INVALID")
    }
    facts.inputValid = true
    facts.stage = "running_application"
    guard NSRunningApplication(processIdentifier: pid_t(pid)) != nil else {
        facts.outcome = "application_not_running"
        return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_APP_NOT_RUNNING")
    }
    facts.runningAppPresent = true
    let requestedFrame = CGRect(
        x: doubleValue(args["window_x"]),
        y: doubleValue(args["window_y"]),
        width: width,
        height: height
    )
    facts.stage = "quartz_record"
    let quartz = exactQuartzWindowRecord(pid: pid, windowId: windowId)
    facts.quartzQueryCompleted = quartz.facts.quartzQueryCompleted
    facts.quartzRecordMatchCount = quartz.facts.quartzRecordMatchCount
    facts.quartzRecordPresent = quartz.facts.quartzRecordPresent
    facts.quartzOwnerMatches = quartz.facts.quartzOwnerMatches
    facts.quartzLayerAllowed = quartz.facts.quartzLayerAllowed
    facts.quartzVisible = quartz.facts.quartzVisible
    guard let record = quartz.record else {
        facts.outcome = "quartz_record_missing"
        return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_QUARTZ_RECORD_NOT_FOUND")
    }
    guard facts.quartzOwnerMatches, facts.quartzLayerAllowed, facts.quartzVisible,
          let bounds = record[kCGWindowBounds as String] as? [String: Any]
    else {
        facts.outcome = "quartz_record_invalid"
        return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_QUARTZ_RECORD_INVALID")
    }
    let currentFrame = CGRect(
        x: doubleValue(bounds["X"]), y: doubleValue(bounds["Y"]),
        width: doubleValue(bounds["Width"]), height: doubleValue(bounds["Height"])
    )
    facts.stage = "quartz_frame"
    facts.quartzFrameMatches = currentFrame.width > 0 && currentFrame.height > 0
        && rectNearlyMatches(requestedFrame, currentFrame)
    guard facts.quartzFrameMatches else {
        facts.outcome = "quartz_frame_mismatch"
        return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_FRAME_MISMATCH")
    }
    let appElement = AXUIElementCreateApplication(pid_t(pid))
    facts.stage = "ax_window_enumeration"
    let axRead = exactAXWindows(appElement)
    facts.axWindowsOutcome = axRead.outcome
    facts.axWindowsAttributeAvailable = axRead.attributeAvailable
    facts.axWindowsPayloadValid = axRead.payloadValid
    facts.axWindowsReadCompleted = axRead.completed
    guard axRead.completed else {
        facts.outcome = "ax_windows_unavailable"
        return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_AX_WINDOWS_UNAVAILABLE")
    }
    facts.axWindowCount = min(16, axRead.windows.count)
    var candidates: [AXUIElement] = []
    for element in axRead.windows {
        let frame = axFrame(element)
        let candidateFrame = CGRect(
            x: doubleValue(frame["x"]), y: doubleValue(frame["y"]),
            width: doubleValue(frame["width"]), height: doubleValue(frame["height"])
        )
        if candidateFrame.width > 0 && candidateFrame.height > 0 {
            facts.axFrameValidCount = min(16, facts.axFrameValidCount + 1)
            if rectNearlyMatches(candidateFrame, currentFrame) { candidates.append(element) }
        }
    }
    facts.stage = "ax_window_match"
    facts.axFrameMatchCount = min(8, candidates.count)
    facts.axMatchPresent = !candidates.isEmpty
    facts.axMatchUnique = candidates.count == 1
    guard let element = candidates.first, candidates.count == 1 else {
        if candidates.isEmpty {
            facts.outcome = "ax_match_absent"
            return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_AX_MATCH_NOT_FOUND")
        }
        facts.outcome = "ax_match_ambiguous"
        return ExactWindowResolution(window: nil, facts: facts, errorCode: "TYPE_EXACT_WINDOW_AX_MATCH_AMBIGUOUS")
    }
    facts.stage = "ready"
    facts.outcome = "ready"
    facts.resolved = true
    return ExactWindowResolution(
        window: ExactSemanticWindow(pid: pid_t(pid), windowId: windowId, frame: currentFrame, element: element),
        facts: facts, errorCode: ""
    )
}

func exactResolutionRetryEligible(_ facts: ExactWindowResolutionFacts) -> Bool {
    ["quartz_record_missing", "ax_match_absent", "ax_match_ambiguous"].contains(facts.outcome)
        || (facts.outcome == "ax_windows_unavailable" && facts.axWindowsOutcome == "cannot_complete")
}

func resolveExactSemanticWindow(
    args: [String: Any],
    resolveOnce: ([String: Any]) -> ExactWindowResolution = resolveExactSemanticWindowOnce,
    frontmost: () -> pid_t = frontmostPid,
    retryPause: () -> Void = { usleep(20_000) }
) -> ExactWindowResolution {
    let target = pid_t(intValue(args["pid"]))
    let frontmostBefore = frontmost()
    var first = resolveOnce(args)
    first.facts.attemptCount = 1
    first.facts.frontmostCheckCompleted = true
    first.facts.targetNonFrontmostBefore = target > 0 && target != frontmostBefore
    if exactResolutionRetryEligible(first.facts) {
        first.facts.retryAttempted = true
        retryPause()
        let beforeRetry = frontmost()
        if beforeRetry == frontmostBefore && target != beforeRetry {
            var second = resolveOnce(args)
            second.facts.attemptCount = 2
            second.facts.retryAttempted = true
            second.facts.retryRecovered = second.window != nil
            if second.window != nil { second.facts.outcome = "recovered" }
            let after = frontmost()
            second.facts.frontmostCheckCompleted = true
            second.facts.targetNonFrontmostBefore = first.facts.targetNonFrontmostBefore
            second.facts.targetNonFrontmostAfter = target > 0 && target != after
            second.facts.frontmostUnchanged = after == frontmostBefore
            if after != frontmostBefore || target == after {
                second.facts.stage = "background_validation"
                second.facts.outcome = target == after ? "frontmost_changed" : "frontmost_changed"
                second.facts.resolved = false
                return ExactWindowResolution(window: nil, facts: second.facts, errorCode: "TYPE_BACKGROUND_PRECONDITION_FAILED")
            }
            return second
        }
    }
    let after = frontmost()
    first.facts.targetNonFrontmostAfter = target > 0 && target != after
    first.facts.frontmostUnchanged = after == frontmostBefore
    if after != frontmostBefore || target == after {
        first.facts.stage = "background_validation"
        first.facts.outcome = "frontmost_changed"
        first.facts.resolved = false
        return ExactWindowResolution(window: nil, facts: first.facts, errorCode: "TYPE_BACKGROUND_PRECONDITION_FAILED")
    }
    return first
}

func exactSemanticWindow(args: [String: Any]) -> ExactSemanticWindow? {
    resolveExactSemanticWindow(args: args).window
}

func semanticTextSelector(args: [String: Any]) -> SemanticTextSelector? {
    guard let raw = args["selector"] as? [String: Any],
          let roleValues = raw["roles"] as? [Any]
    else {
        return nil
    }
    let roles = Set(roleValues.map { stringValue($0) }.filter { !$0.isEmpty })
    guard !roles.isEmpty else { return nil }
    let region = raw["relative_region"] as? [String: Any] ?? [:]
    let minX = doubleValue(region["min_x"], default: 0)
    let maxX = doubleValue(region["max_x"], default: 1)
    let minY = doubleValue(region["min_y"], default: 0)
    let maxY = doubleValue(region["max_y"], default: 1)
    guard minX >= 0, minY >= 0, maxX <= 1, maxY <= 1, minX < maxX, minY < maxY else {
        return nil
    }
    let preference = stringValue(raw["preference"])
    guard preference.isEmpty || preference == "widest" else { return nil }
    return SemanticTextSelector(
        roles: roles,
        forbiddenAncestorRoles: Set(
            (raw["forbidden_ancestor_roles"] as? [Any] ?? []).map { stringValue($0) }.filter { !$0.isEmpty }
        ),
        minX: minX, maxX: maxX, minY: minY, maxY: maxY,
        requireEnabled: boolValue(raw["require_enabled"], default: true),
        requireSettable: boolValue(raw["require_settable"], default: true),
        requireBackground: boolValue(raw["require_background"], default: true),
        preference: preference.isEmpty ? "widest" : preference
    )
}

struct RelativeCenterResult {
    let childFrameValid: Bool
    let childCenterInsideWindow: Bool
    let relativeRegionEvaluable: Bool
    let relativeRegionMatched: Bool
}

func relativeCenter(
    candidateFrame: CGRect?,
    windowFrame: CGRect,
    minX: Double,
    maxX: Double,
    minY: Double,
    maxY: Double
) -> RelativeCenterResult {
    guard let candidateFrame,
          candidateFrame.width > 0, candidateFrame.height > 0,
          windowFrame.width > 0, windowFrame.height > 0
    else {
        return RelativeCenterResult(
            childFrameValid: false, childCenterInsideWindow: false,
            relativeRegionEvaluable: false, relativeRegionMatched: false
        )
    }
    let relativeX = (candidateFrame.midX - windowFrame.minX) / windowFrame.width
    let relativeY = (candidateFrame.midY - windowFrame.minY) / windowFrame.height
    let inside = relativeX >= 0 && relativeX <= 1 && relativeY >= 0 && relativeY <= 1
    let frameInside = candidateFrame.minX >= windowFrame.minX
        && candidateFrame.maxX <= windowFrame.maxX
        && candidateFrame.minY >= windowFrame.minY
        && candidateFrame.maxY <= windowFrame.maxY
    return RelativeCenterResult(
        childFrameValid: true,
        childCenterInsideWindow: inside,
        relativeRegionEvaluable: inside && frameInside,
        relativeRegionMatched: inside && frameInside
            && relativeX >= minX && relativeX <= maxX
            && relativeY >= minY && relativeY <= maxY
    )
}

struct SemanticTextDiscoveryFacts {
    var nodesVisitedCount = 0
    var roleMatchCount = 0
    var windowOwnedCount = 0
    var nonWebContentCount = 0
    var frameValidCount = 0
    var regionMatchCount = 0
    var enabledCount = 0
    var valuePresentCount = 0
    var valueReadableCount = 0
    var valueSettableCount = 0
    var selectedTextSettableCount = 0
    var selectedRangeSettableCount = 0
    var focusSettableCount = 0
    var finalCandidateCount = 0
    // Candidate observations are made before a node's children are read. They
    // are diagnostic-only until the separate actionable scan-completeness gate
    // below is true.
    var preinvalidationCandidateCount = 0
    var windowNodesVisitedCount = 0
    // This is a count only. AX references remain native-process-local and are
    // compared with CFEqual before a node can be enqueued or processed twice.
    var windowDuplicateNodesSkippedCount = 0
    var windowScanComplete = false
    var windowScanTruncated = false
    var windowNodeBudgetTruncated = false
    var windowDepthTruncated = false
    var windowMaxDepthReached = 0
    var appNodesVisitedCount = 0
    var appScanPerformed = false
    var appScanComplete = false
    var appScanTruncated = false
    var forbiddenRootCount = 0
    var forbiddenSubtreePrunedCount = 0
    var otherWindowPrunedCount = 0
    var childrenReadFailureCount = 0
    var childrenReadSuccessCount = 0
    var childrenEmptyCount = 0
    var childrenUnsupportedCount = 0
    var childrenNoValueCount = 0
    var childrenCannotCompleteCount = 0
    var childrenInvalidElementCount = 0
    var childrenGlobalFailureCount = 0
    var childrenProtocolFailureCount = 0
    var childrenUnknownBranchCount = 0
    var unresolvedSelectorBranchCount = 0
    var childrenProvenEmptyAfterFailureCount = 0
    var childrenRetryAttemptedCount = 0
    var childrenRetryRecoveredCount = 0
    var childrenFailureOnWindowRoot = false
    var childrenFailureUnderToolbar = false
    var childrenAttributeAdvertised = false
    var childrenCountKnown = false
    var childrenCountNonzero = false
    var childrenBranchProvenEmpty = false
    var childrenStructuralEmptyProofs = Set<SemanticChildrenStructuralEmptyProof>()
    var childrenAXErrorClasses = Set<SemanticChildrenErrorClass>()
    var navigationOrderFallbackAttemptedCount = 0
    var navigationOrderFallbackSucceededCount = 0
    var navigationOrderRecoveredInvalidCount = 0
    var navigationOrderPageReadCount = 0
    var navigationOrderOutcomes = Set<SemanticNavigationOrderFallbackOutcome>()
    var navigationOrderFailureClasses = Set<SemanticNavigationOrderFailureClass>()
    var navigationOrderAXErrorClasses = Set<SemanticNavigationOrderAXErrorClass>()
    var navigationOrderCardinalityClasses = Set<SemanticNavigationOrderCardinalityClass>()
    var navigationOrderParentProofs = Set<SemanticNavigationOrderParentProof>()
    var navigationOrderCountStableAll = true
    var navigationOrderCompleteAll = true
    var staleBranchScopes = Set<String>()
    var staleNodeClasses = Set<SemanticStaleNodeClass>()
    var staleNodeSelfEligible = false
    var sawChildrenCannotComplete = false
    var sawChildrenStaleElement = false
    var sawChildrenGlobalAPI = false
    var sawChildrenProtocol = false
    var sawChildrenGeneric = false
    var sawIncompleteWindowRoot = false
    var sawIncompleteContainer = false
    var sawIncompleteStaticValue = false
    var sawIncompleteActionControl = false
    var sawIncompleteOther = false
    var windowChildrenReadFailureCount = 0
    var windowChildrenInvalidElementCount = 0
    var windowChildrenUnknownBranchCount = 0
    var windowChildrenCannotCompleteCount = 0
    var windowChildrenGlobalFailureCount = 0
    var windowChildrenProtocolFailureCount = 0
    var windowSawChildrenGeneric = false
    var staleRecoveryEligible = false
    var staleRecoveryAttempted = false
    var staleRecoveryWindowRebound = false
    var staleRecoveryWindowStable = false
    var staleRecoverySecondPassComplete = false
    var staleRecoverySucceeded = false
    var staleParentRefreshAttempted = false
    var staleParentRefreshSucceeded = false
    var staleRecoveryFinalScanComplete = false
    var staleAdditionalReadBudgetExhausted = false
    var discoveryPassCount = 1
    var staleRecoveryRestartCount = 0
    var staleParentRefreshCount = 0
    var staleParentRefreshReadCount = 0
    var staleAdditionalAXReadCount = 0
    var firstPassStaleCount = 0
    var secondPassStaleCount = 0
    var firstPassUnknownBranchCount = 0
    var secondPassUnknownBranchCount = 0
    var firstPassNodesVisitedCount = 0
    var secondPassNodesVisitedCount = 0
    var secondPassFinalCandidateCount = 0
    var thirdPassStaleCount = 0
    var thirdPassUnknownBranchCount = 0
    var thirdPassNodesVisitedCount = 0
    var thirdPassFinalCandidateCount = 0
    var staleReferenceRefreshClass = "not_attempted"
    var staleBranchComparison = "not_applicable"
    var secondThirdStaleReferenceClass = "not_comparable"
    var staleRecoveryOutcome = "not_needed"
    var windowAllowedRoleCount = 0
    var appOwnedAllowedRoleCount = 0
    var unlistedTextCapableCount = 0
    var unlistedWindowOwnedCount = 0
    var unlistedNonWebCount = 0
    var unlistedFrameValidCount = 0
    var unlistedRegionMatchCount = 0
    var unlistedEnabledCount = 0
    var unlistedValueReadableCount = 0
    var unlistedMutationReadyCount = 0
    var unlistedValueSettableCount = 0
    var unlistedSelectedTextSettableCount = 0
    var unlistedSelectedRangeSettableCount = 0
    var unlistedFocusSettableCount = 0
    var unlistedAttributeCapabilityKnownCount = 0
    var unlistedUnderToolbarCount = 0
    var unlistedRelatedAllowedRoleCount = 0
    var unlistedRelationScanComplete = true
    var sawUnlistedTitleRelation = false
    var sawUnlistedLinkedRelation = false
    var sawUnlistedParentChildRelation = false
    var sawUnlistedContainerClass = false
    var sawUnlistedStaticValueClass = false
    var sawUnlistedActionControlClass = false
    var sawUnlistedWebRootClass = false
    var sawUnlistedOtherClass = false
    // These are bounded, diagnostic-only summaries of allowed roles already
    // visited by the authoritative exact-window traversal.  They neither add
    // AX reads nor participate in candidate eligibility.
    var allowedAXTextFieldCount = 0
    var allowedAXComboBoxCount = 0
    var allowedAXTextAreaCount = 0
    var allowedFrameInsideWindowCount = 0
    var allowedRegionXMatchCount = 0
    var allowedRegionYMatchCount = 0
    var allowedRegionMissAxes = Set<String>()
    var allowedCenterYBands = Set<String>()
    var allowedWidthBands = Set<String>()
    var allowedHeightBands = Set<String>()
    var countsTruncated = false
    var actionableCountsTruncated = false
    var appDiagnosticCountsTruncated = false
    var sawAXTextField = false
    var sawAXComboBox = false
    var sawAXTextArea = false
    var sawAXSearchFieldSubrole = false
    var sawAXWebAreaAncestor = false
    var sawUnlistedTextCapableRole = false
    var windowFrameMatch = true
    var childFrameValid = false
    var childCenterInsideWindow = false
    var relativeRegionEvaluable = false
    var relativeRegionMatched = false
    var scanScope = "exact_window_descendants"
    var ownershipProof = "window_descendant"
    var appDiagnosticOwnershipProof = "none"

    mutating func increment(_ keyPath: WritableKeyPath<SemanticTextDiscoveryFacts, Int>, cap: Int = 64) {
        if self[keyPath: keyPath] < cap {
            self[keyPath: keyPath] += 1
        } else {
            countsTruncated = true
        }
    }

    private func closedClass(_ values: Set<String>, none: String = "none") -> String {
        if values.isEmpty { return none }
        return values.count == 1 ? values.first ?? none : "multiple"
    }

    func allowedRoleClass() -> String {
        let roles = [
            (allowedAXTextFieldCount > 0, "ax_text_field"),
            (allowedAXComboBoxCount > 0, "ax_combo_box"),
            (allowedAXTextAreaCount > 0, "ax_text_area")
        ].filter { $0.0 }.map { $0.1 }
        if roles.isEmpty { return "none" }
        return roles.count == 1 ? roles[0] : "multiple"
    }

    func allowedRegionMissAxis() -> String { closedClass(allowedRegionMissAxes) }
    func allowedCenterYBand() -> String { closedClass(allowedCenterYBands) }
    func allowedWidthBand() -> String { closedClass(allowedWidthBands) }
    func allowedHeightBand() -> String { closedClass(allowedHeightBands) }

    mutating func observeAllowedRoleGeometry(
        role: String,
        candidateFrame: CGRect?,
        windowFrame: CGRect,
        selector: SemanticTextSelector,
        relative: RelativeCenterResult
    ) {
        switch role {
        case "AXTextField": increment(\.allowedAXTextFieldCount, cap: 8)
        case "AXComboBox": increment(\.allowedAXComboBoxCount, cap: 8)
        case "AXTextArea": increment(\.allowedAXTextAreaCount, cap: 8)
        default: return
        }
        guard let candidateFrame,
              candidateFrame.width > 0, candidateFrame.height > 0,
              windowFrame.width > 0, windowFrame.height > 0,
              relative.childFrameValid
        else {
            allowedRegionMissAxes.insert("frame_unavailable")
            allowedCenterYBands.insert("frame_unavailable")
            allowedWidthBands.insert("frame_unavailable")
            allowedHeightBands.insert("frame_unavailable")
            return
        }
        // relativeRegionEvaluable already requires both the center and the
        // full candidate frame to lie inside the exact window frame.
        guard relative.relativeRegionEvaluable else {
            allowedRegionMissAxes.insert("outside_window")
            allowedCenterYBands.insert("outside_window")
            allowedWidthBands.insert("outside_window")
            allowedHeightBands.insert("outside_window")
            return
        }
        increment(\.allowedFrameInsideWindowCount, cap: 8)
        let centerX = (candidateFrame.midX - windowFrame.minX) / windowFrame.width
        let centerY = (candidateFrame.midY - windowFrame.minY) / windowFrame.height
        let xMatched = centerX >= selector.minX && centerX <= selector.maxX
        let yMatched = centerY >= selector.minY && centerY <= selector.maxY
        if xMatched { increment(\.allowedRegionXMatchCount, cap: 8) }
        if yMatched { increment(\.allowedRegionYMatchCount, cap: 8) }
        switch (xMatched, yMatched) {
        case (true, true): allowedRegionMissAxes.insert("none")
        case (false, true): allowedRegionMissAxes.insert("x")
        case (true, false): allowedRegionMissAxes.insert("y")
        case (false, false): allowedRegionMissAxes.insert("both")
        }
        if centerY <= 0.22 {
            allowedCenterYBands.insert("top_0_22")
        } else if centerY <= 0.35 {
            allowedCenterYBands.insert("upper_22_35")
        } else if centerY <= 0.65 {
            allowedCenterYBands.insert("middle_35_65")
        } else {
            allowedCenterYBands.insert("lower_65_100")
        }
        let width = candidateFrame.width / windowFrame.width
        if width < 0.40 {
            allowedWidthBands.insert("narrow_lt_40")
        } else if width <= 0.80 {
            allowedWidthBands.insert("wide_40_80")
        } else {
            allowedWidthBands.insert("near_full_80_100")
        }
        let height = candidateFrame.height / windowFrame.height
        if height <= 0.15 {
            allowedHeightBands.insert("shallow_0_15")
        } else if height <= 0.40 {
            allowedHeightBands.insert("medium_15_40")
        } else {
            allowedHeightBands.insert("tall_40_100")
        }
    }

    func discoveryStage() -> String {
        if windowScanTruncated || windowDepthTruncated {
            return "scan_incomplete"
        }
        if nodesVisitedCount == 0 { return "no_nodes" }
        if roleMatchCount == 0 { return "role_absent" }
        if windowOwnedCount == 0 { return "window_ownership_unverified" }
        if nonWebContentCount == 0 { return "web_content_excluded" }
        if frameValidCount == 0 { return "frame_unavailable" }
        if regionMatchCount == 0 { return "region_excluded" }
        if enabledCount == 0 { return "disabled" }
        if valuePresentCount == 0 || valueReadableCount == 0 { return "value_unreadable" }
        if finalCandidateCount == 0 { return "not_settable" }
        if finalCandidateCount > 1 { return "ambiguous" }
        return "ready"
    }

    func appDiagnosticStage() -> String {
        if !appScanPerformed { return "not_performed" }
        return appScanComplete ? "complete" : "scan_incomplete"
    }

    func unlistedRelationKind() -> String {
        let kinds = [
            (sawUnlistedTitleRelation, "title_relation"),
            (sawUnlistedLinkedRelation, "linked_relation"),
            (sawUnlistedParentChildRelation, "parent_child")
        ].filter { $0.0 }.map { $0.1 }
        if kinds.isEmpty { return "none" }
        return kinds.count == 1 ? kinds[0] : "multiple"
    }

    func coordinateStatus() -> String {
        if !windowFrameMatch { return "unavailable" }
        if roleMatchCount == 0 { return "window_frame_matched" }
        if !childFrameValid { return "child_frames_unavailable" }
        if !childCenterInsideWindow || !relativeRegionEvaluable { return "child_frames_outside_window" }
        if !relativeRegionMatched { return "relative_region_miss" }
        return "consistent"
    }

    func unlistedRoleClass() -> String {
        let classes = [
            (sawUnlistedContainerClass, "unlisted_container"),
            (sawUnlistedStaticValueClass, "unlisted_static_value"),
            (sawUnlistedActionControlClass, "unlisted_action_control"),
            (sawUnlistedWebRootClass, "unlisted_web_root"),
            (sawUnlistedOtherClass, "unlisted_other")
        ].filter { $0.0 }.map { $0.1 }
        if classes.isEmpty { return "none" }
        if classes.count == 1 { return classes[0] }
        return "multiple"
    }

    func childrenFailureClass() -> String {
        let classes = [
            (sawChildrenCannotComplete, "cannot_complete"),
            (sawChildrenStaleElement, "stale_element"),
            (sawChildrenGlobalAPI, "global_api"),
            (sawChildrenProtocol, "protocol"),
            (sawChildrenGeneric, "generic")
        ].filter { $0.0 }.map { $0.1 }
        if classes.isEmpty { return "none" }
        if classes.count == 1 { return classes[0] }
        return "multiple"
    }

    func childrenIncompleteBranchClass() -> String {
        let classes = [
            (sawIncompleteWindowRoot, "window_root"),
            (sawIncompleteContainer, "container"),
            (sawIncompleteStaticValue, "static_value"),
            (sawIncompleteActionControl, "action_control"),
            (sawIncompleteOther, "other")
        ].filter { $0.0 }.map { $0.1 }
        if classes.isEmpty { return "none" }
        if classes.count == 1 { return classes[0] }
        return "multiple"
    }

    func childrenStructuralEmptyProofClass() -> String {
        let proofs = childrenStructuralEmptyProofs.filter { $0 != .none }
        if proofs.isEmpty { return "none" }
        return proofs.count == 1 ? proofs.first?.rawValue ?? "none" : "multiple"
    }

    // Keep selector-critical child-read errors distinct from the coarser
    // failure class. This is still a closed, content-free summary.
    func childrenAXErrorClass() -> String {
        let classes = childrenAXErrorClasses.filter {
            ![.none, .additionalReadBudgetExhausted].contains($0)
        }
        if classes.isEmpty { return "none" }
        let names = classes.map { error -> String in
            switch error {
            case .noValue: return "no_value"
            case .attributeUnsupported: return "attribute_unsupported"
            case .cannotComplete: return "cannot_complete"
            case .invalidElement: return "invalid_element"
            case .apiDisabled: return "api_disabled"
            case .notImplemented: return "not_implemented"
            case .illegalArgument: return "illegal_argument"
            case .payloadTypeInvalid: return "payload_type_invalid"
            case .genericFailure: return "generic"
            case .none, .additionalReadBudgetExhausted, .multiple: return "none"
            }
        }.filter { $0 != "none" }
        let unique = Set(names)
        if unique.isEmpty { return "none" }
        return unique.count == 1 ? unique.first ?? "none" : "multiple"
    }

    func navigationOrderFallbackOutcome() -> String {
        if navigationOrderOutcomes.isEmpty { return "not_attempted" }
        return navigationOrderOutcomes.count == 1
            ? navigationOrderOutcomes.first?.rawValue ?? "not_attempted"
            : "multiple"
    }

    func navigationOrderFailureClass() -> String {
        let classes = navigationOrderFailureClasses.filter { $0 != .none }
        if classes.isEmpty { return "none" }
        return classes.count == 1 ? classes.first?.rawValue ?? "none" : "multiple"
    }

    func navigationOrderAXErrorClass() -> String {
        let classes = navigationOrderAXErrorClasses.filter { $0 != .none }
        if classes.isEmpty { return "none" }
        return classes.count == 1 ? classes.first?.rawValue ?? "none" : "multiple"
    }

    func navigationOrderCardinalityClass() -> String {
        if navigationOrderCardinalityClasses.isEmpty { return "unknown" }
        return navigationOrderCardinalityClasses.count == 1
            ? navigationOrderCardinalityClasses.first?.rawValue ?? "unknown"
            : "multiple"
    }

    func navigationOrderParentProof() -> String {
        if navigationOrderParentProofs.isEmpty { return "not_checked" }
        return navigationOrderParentProofs.count == 1
            ? navigationOrderParentProofs.first?.rawValue ?? "not_checked"
            : "multiple"
    }

    func staleBranchScope() -> String {
        closedClass(staleBranchScopes)
    }

    func staleNodeClass() -> String {
        let classes = staleNodeClasses.map(\.rawValue)
        return closedClass(Set(classes))
    }

    func actionableScanComplete() -> Bool {
        windowScanComplete
            && unresolvedSelectorBranchCount == 0
            && !actionableCountsTruncated
    }

    mutating func recordChildrenRead(
        _ result: AXChildrenReadResult,
        role: String,
        windowRoot: Bool,
        underToolbar: Bool,
        exactWindowScan: Bool = true
    ) {
        // The exact-window traversal is the only selector-authoritative
        // source.  The optional application traversal is diagnostic-only and
        // must not turn its AX failures into a selector-resolution result.
        if exactWindowScan {
            childrenAXErrorClasses.formUnion(result.observedErrorClasses)
        }
        if result.structuralEmptyProof != .none {
            childrenStructuralEmptyProofs.insert(result.structuralEmptyProof)
        }
        switch result.outcome {
        case .children: increment(\.childrenReadSuccessCount)
        case .provenEmpty:
            increment(\.childrenEmptyCount)
            if result.errorClass == .none { increment(\.childrenReadSuccessCount) }
        default: break
        }
        if result.observedErrorClasses.contains(.attributeUnsupported) { increment(\.childrenUnsupportedCount) }
        if result.observedErrorClasses.contains(.noValue) { increment(\.childrenNoValueCount) }
        if result.observedErrorClasses.contains(.cannotComplete) {
            increment(\.childrenCannotCompleteCount)
            sawChildrenCannotComplete = true
            if exactWindowScan { increment(\.windowChildrenCannotCompleteCount) }
        }
        if result.observedErrorClasses.contains(.invalidElement) {
            increment(\.childrenInvalidElementCount)
            sawChildrenStaleElement = true
            if exactWindowScan { increment(\.windowChildrenInvalidElementCount) }
        }
        if !result.observedErrorClasses.isDisjoint(with: [.apiDisabled, .notImplemented]) {
            increment(\.childrenGlobalFailureCount)
            sawChildrenGlobalAPI = true
            if exactWindowScan { increment(\.windowChildrenGlobalFailureCount) }
        }
        if !result.observedErrorClasses.isDisjoint(with: [.illegalArgument, .payloadTypeInvalid]) {
            increment(\.childrenProtocolFailureCount)
            sawChildrenProtocol = true
            if exactWindowScan { increment(\.windowChildrenProtocolFailureCount) }
        }
        if result.observedErrorClasses.contains(.genericFailure) {
            sawChildrenGeneric = true
            if exactWindowScan { windowSawChildrenGeneric = true }
        }
        let materialFailure = !result.observedErrorClasses.subtracting([
            .none, .noValue, .attributeUnsupported
        ]).isEmpty
        if materialFailure {
            increment(\.childrenReadFailureCount)
            if exactWindowScan { increment(\.windowChildrenReadFailureCount) }
        }
        if result.scanIncomplete {
            increment(\.childrenUnknownBranchCount)
            if exactWindowScan {
                increment(\.windowChildrenUnknownBranchCount)
                increment(\.unresolvedSelectorBranchCount)
            }
        }
        if result.branchProvenEmpty && materialFailure {
            increment(\.childrenProvenEmptyAfterFailureCount)
        }
        if result.retryAttempted { increment(\.childrenRetryAttemptedCount) }
        if result.retryRecovered { increment(\.childrenRetryRecoveredCount) }
        if materialFailure {
            childrenFailureOnWindowRoot = childrenFailureOnWindowRoot || windowRoot
            childrenFailureUnderToolbar = childrenFailureUnderToolbar || underToolbar
            childrenAttributeAdvertised = childrenAttributeAdvertised || result.childrenAttributeAdvertised
            childrenCountKnown = childrenCountKnown || result.childrenCountKnown
            childrenCountNonzero = childrenCountNonzero || result.childrenCountNonzero
            childrenBranchProvenEmpty = childrenBranchProvenEmpty || result.branchProvenEmpty
        }
        guard result.scanIncomplete else { return }
        if windowRoot {
            sawIncompleteWindowRoot = true
        } else {
            switch role {
            case "AXGroup", "AXToolbar", "AXSplitGroup", "AXScrollArea", "AXLayoutArea":
                sawIncompleteContainer = true
            case "AXStaticText": sawIncompleteStaticValue = true
            case "AXButton", "AXMenuButton", "AXPopUpButton", "AXMenuItem":
                sawIncompleteActionControl = true
            default: sawIncompleteOther = true
            }
        }
    }

    mutating func recordNavigationOrderFallback(_ result: SemanticNavigationOrderReadResult) {
        increment(\.navigationOrderFallbackAttemptedCount, cap: 8)
        navigationOrderOutcomes.insert(result.outcome)
        navigationOrderFailureClasses.insert(result.failureClass)
        navigationOrderAXErrorClasses.formUnion(result.observedAXErrorClasses)
        navigationOrderCardinalityClasses.insert(result.cardinalityClass)
        navigationOrderParentProofs.insert(result.parentProof)
        navigationOrderCountStableAll = navigationOrderCountStableAll && result.countStable
        navigationOrderCompleteAll = navigationOrderCompleteAll && result.complete
        let pageReadTotal = navigationOrderPageReadCount + result.pageReadCount
        if pageReadTotal > 16 { countsTruncated = true }
        navigationOrderPageReadCount = min(16, pageReadTotal)
        if result.succeeded {
            increment(\.navigationOrderFallbackSucceededCount, cap: 8)
            increment(\.navigationOrderRecoveredInvalidCount, cap: 8)
        }
    }

    func payload() -> [String: Any] {
        [
            "semantic_nodes_visited_count": nodesVisitedCount,
            "semantic_role_match_count": roleMatchCount,
            "semantic_window_owned_count": windowOwnedCount,
            "semantic_non_web_content_count": nonWebContentCount,
            "semantic_frame_valid_count": frameValidCount,
            "semantic_region_match_count": regionMatchCount,
            "semantic_enabled_count": enabledCount,
            "semantic_value_present_count": valuePresentCount,
            "semantic_value_readable_count": valueReadableCount,
            "semantic_value_settable_count": valueSettableCount,
            "semantic_selected_text_settable_count": selectedTextSettableCount,
            "semantic_selected_range_settable_count": selectedRangeSettableCount,
            "semantic_focus_settable_count": focusSettableCount,
            "semantic_final_candidate_count": finalCandidateCount,
            "semantic_preinvalidation_candidate_count": preinvalidationCandidateCount,
            "semantic_window_nodes_visited_count": windowNodesVisitedCount,
            "semantic_window_duplicate_nodes_skipped_count": windowDuplicateNodesSkippedCount,
            "semantic_window_scan_complete": windowScanComplete,
            "semantic_window_scan_truncated": windowScanTruncated,
            "semantic_window_depth_truncated": windowDepthTruncated,
            "semantic_actionable_branch_scope_complete": windowScanComplete,
            "semantic_actionable_candidates_complete": actionableScanComplete(),
            "semantic_actionable_scan_complete": actionableScanComplete(),
            "semantic_window_max_depth_reached": windowMaxDepthReached,
            "semantic_app_nodes_visited_count": appNodesVisitedCount,
            "semantic_app_scan_performed": appScanPerformed,
            "semantic_app_scan_complete": appScanComplete,
            "semantic_app_scan_truncated": appScanTruncated,
            "semantic_app_diagnostic_stage": appDiagnosticStage(),
            "semantic_app_diagnostic_scope": "application_tree_owned",
            "semantic_app_diagnostic_ownership_proof": appDiagnosticOwnershipProof,
            "semantic_forbidden_root_count": forbiddenRootCount,
            "semantic_forbidden_subtree_pruned_count": forbiddenSubtreePrunedCount,
            "semantic_other_window_pruned_count": otherWindowPrunedCount,
            "semantic_children_read_failure_count": childrenReadFailureCount,
            "semantic_children_read_success_count": childrenReadSuccessCount,
            "semantic_children_empty_count": childrenEmptyCount,
            "semantic_children_unsupported_count": childrenUnsupportedCount,
            "semantic_children_no_value_count": childrenNoValueCount,
            "semantic_children_cannot_complete_count": childrenCannotCompleteCount,
            "semantic_children_invalid_element_count": childrenInvalidElementCount,
            "semantic_children_global_failure_count": childrenGlobalFailureCount,
            "semantic_children_protocol_failure_count": childrenProtocolFailureCount,
            "semantic_children_unknown_branch_count": childrenUnknownBranchCount,
            "semantic_unresolved_selector_branch_count": unresolvedSelectorBranchCount,
            "semantic_children_proven_empty_after_failure_count": childrenProvenEmptyAfterFailureCount,
            "semantic_children_retry_attempted_count": childrenRetryAttemptedCount,
            "semantic_children_retry_recovered_count": childrenRetryRecoveredCount,
            "semantic_children_failure_class": childrenFailureClass(),
            "semantic_children_ax_error_class": childrenAXErrorClass(),
            "semantic_children_incomplete_branch_class": childrenIncompleteBranchClass(),
            "semantic_children_failure_on_window_root": childrenFailureOnWindowRoot,
            "semantic_children_failure_under_toolbar": childrenFailureUnderToolbar,
            "semantic_children_attribute_advertised": childrenAttributeAdvertised,
            "semantic_children_count_known": childrenCountKnown,
            "semantic_children_count_nonzero": childrenCountNonzero,
            "semantic_children_branch_proven_empty": childrenBranchProvenEmpty,
            "semantic_children_structural_empty_proof": childrenStructuralEmptyProofClass(),
            "semantic_navigation_order_fallback_attempted_count": navigationOrderFallbackAttemptedCount,
            "semantic_navigation_order_fallback_succeeded_count": navigationOrderFallbackSucceededCount,
            "semantic_navigation_order_recovered_invalid_count": navigationOrderRecoveredInvalidCount,
            "semantic_navigation_order_page_read_count": navigationOrderPageReadCount,
            "semantic_navigation_order_fallback_outcome": navigationOrderFallbackOutcome(),
            "semantic_navigation_order_failure_class": navigationOrderFailureClass(),
            "semantic_navigation_order_ax_error_class": navigationOrderAXErrorClass(),
            "semantic_navigation_order_cardinality_class": navigationOrderCardinalityClass(),
            "semantic_navigation_order_parent_proof": navigationOrderParentProof(),
            "semantic_navigation_order_count_stable": navigationOrderFallbackAttemptedCount > 0
                && navigationOrderCountStableAll,
            "semantic_navigation_order_complete": navigationOrderFallbackAttemptedCount > 0
                && navigationOrderCompleteAll,
            "semantic_stale_branch_scope": staleBranchScope(),
            "semantic_stale_node_self_eligible": staleNodeSelfEligible,
            "semantic_stale_node_class": staleNodeClass(),
            "semantic_stale_recovery_eligible": staleRecoveryEligible,
            "semantic_stale_recovery_attempted": staleRecoveryAttempted,
            "semantic_stale_recovery_window_rebound": staleRecoveryWindowRebound,
            "semantic_stale_recovery_window_stable": staleRecoveryWindowStable,
            "semantic_stale_recovery_second_pass_complete": staleRecoverySecondPassComplete,
            "semantic_stale_recovery_succeeded": staleRecoverySucceeded,
            "semantic_stale_parent_refresh_attempted": staleParentRefreshAttempted,
            "semantic_stale_parent_refresh_succeeded": staleParentRefreshSucceeded,
            "semantic_stale_recovery_final_scan_complete": staleRecoveryFinalScanComplete,
            "semantic_stale_additional_read_budget_exhausted": staleAdditionalReadBudgetExhausted,
            "semantic_discovery_pass_count": discoveryPassCount,
            "semantic_stale_recovery_restart_count": staleRecoveryRestartCount,
            "semantic_stale_parent_refresh_count": staleParentRefreshCount,
            "semantic_stale_parent_refresh_read_count": staleParentRefreshReadCount,
            "semantic_stale_additional_ax_read_count": staleAdditionalAXReadCount,
            "semantic_first_pass_stale_count": firstPassStaleCount,
            "semantic_second_pass_stale_count": secondPassStaleCount,
            "semantic_first_pass_unknown_branch_count": firstPassUnknownBranchCount,
            "semantic_second_pass_unknown_branch_count": secondPassUnknownBranchCount,
            "semantic_first_pass_nodes_visited_count": firstPassNodesVisitedCount,
            "semantic_second_pass_nodes_visited_count": secondPassNodesVisitedCount,
            "semantic_second_pass_final_candidate_count": secondPassFinalCandidateCount,
            "semantic_third_pass_stale_count": thirdPassStaleCount,
            "semantic_third_pass_unknown_branch_count": thirdPassUnknownBranchCount,
            "semantic_third_pass_nodes_visited_count": thirdPassNodesVisitedCount,
            "semantic_third_pass_final_candidate_count": thirdPassFinalCandidateCount,
            "semantic_stale_reference_refresh_class": staleReferenceRefreshClass,
            "semantic_stale_branch_comparison": staleBranchComparison,
            "semantic_second_third_stale_reference_class": secondThirdStaleReferenceClass,
            "semantic_stale_recovery_outcome": staleRecoveryOutcome,
            "semantic_window_allowed_role_count": windowAllowedRoleCount,
            "semantic_app_owned_allowed_role_count": appOwnedAllowedRoleCount,
            "semantic_allowed_ax_text_field_count": allowedAXTextFieldCount,
            "semantic_allowed_ax_combo_box_count": allowedAXComboBoxCount,
            "semantic_allowed_ax_text_area_count": allowedAXTextAreaCount,
            "semantic_allowed_frame_inside_window_count": allowedFrameInsideWindowCount,
            "semantic_allowed_region_x_match_count": allowedRegionXMatchCount,
            "semantic_allowed_region_y_match_count": allowedRegionYMatchCount,
            "semantic_allowed_role_class": allowedRoleClass(),
            "semantic_allowed_region_miss_axis": allowedRegionMissAxis(),
            "semantic_allowed_center_y_band": allowedCenterYBand(),
            "semantic_allowed_width_band": allowedWidthBand(),
            "semantic_allowed_height_band": allowedHeightBand(),
            "semantic_unlisted_text_capable_count": unlistedTextCapableCount,
            "semantic_unlisted_window_owned_count": unlistedWindowOwnedCount,
            "semantic_unlisted_non_web_count": unlistedNonWebCount,
            "semantic_unlisted_frame_valid_count": unlistedFrameValidCount,
            "semantic_unlisted_region_match_count": unlistedRegionMatchCount,
            "semantic_unlisted_enabled_count": unlistedEnabledCount,
            "semantic_unlisted_value_readable_count": unlistedValueReadableCount,
            "semantic_unlisted_mutation_ready_count": unlistedMutationReadyCount,
            "semantic_unlisted_value_settable_count": unlistedValueSettableCount,
            "semantic_unlisted_selected_text_settable_count": unlistedSelectedTextSettableCount,
            "semantic_unlisted_selected_range_settable_count": unlistedSelectedRangeSettableCount,
            "semantic_unlisted_focus_settable_count": unlistedFocusSettableCount,
            "semantic_unlisted_attribute_capability_known_count": unlistedAttributeCapabilityKnownCount,
            "semantic_unlisted_under_toolbar_count": unlistedUnderToolbarCount,
            "semantic_unlisted_relation_scan_complete": unlistedRelationScanComplete,
            "semantic_unlisted_related_allowed_role_count": unlistedRelatedAllowedRoleCount,
            "semantic_unlisted_relation_kind": unlistedRelationKind(),
            "semantic_counts_truncated": countsTruncated,
            "semantic_actionable_counts_truncated": actionableCountsTruncated,
            "semantic_app_diagnostic_counts_truncated": appDiagnosticCountsTruncated,
            "saw_ax_text_field": sawAXTextField,
            "saw_ax_combo_box": sawAXComboBox,
            "saw_ax_text_area": sawAXTextArea,
            "saw_ax_search_field_subrole": sawAXSearchFieldSubrole,
            "saw_ax_web_area_ancestor": sawAXWebAreaAncestor,
            "saw_unlisted_text_capable_role": sawUnlistedTextCapableRole,
            "saw_unlisted_container_class": sawUnlistedContainerClass,
            "saw_unlisted_static_value_class": sawUnlistedStaticValueClass,
            "saw_unlisted_action_control_class": sawUnlistedActionControlClass,
            "saw_unlisted_web_root_class": sawUnlistedWebRootClass,
            "saw_unlisted_other_class": sawUnlistedOtherClass,
            "semantic_unlisted_role_class": unlistedRoleClass(),
            "semantic_traversal_order": "breadth_first",
            "semantic_scan_scope": scanScope,
            "semantic_discovery_stage": discoveryStage(),
            "semantic_coordinate_status": coordinateStatus(),
            "semantic_ownership_proof": ownershipProof,
            "window_frame_match": windowFrameMatch,
            "child_frame_valid": childFrameValid,
            "child_center_inside_window": childCenterInsideWindow,
            "relative_region_evaluable": relativeRegionEvaluable,
            "relative_region_matched": relativeRegionMatched
        ]
    }
}

struct SemanticTextDiscovery {
    let eligibleCandidates: [AXUIElement]
    let facts: SemanticTextDiscoveryFacts
    // Native-process-local references used only by the read-only exposure probe.
    // They are never serialized and never enter eligibleCandidates.
    let diagnosticProxySeeds: [AXUIElement]
    // Stale-child recovery evidence is native-process-local only.  It retains
    // the immediate parent rather than a path/index so a later bounded parent
    // refresh can obtain current child references without exporting AX identity.
    let staleDescendants: [SemanticStaleDescendant]

    init(
        eligibleCandidates: [AXUIElement],
        facts: SemanticTextDiscoveryFacts,
        diagnosticProxySeeds: [AXUIElement] = [],
        staleDescendants: [SemanticStaleDescendant] = []
    ) {
        self.eligibleCandidates = eligibleCandidates
        self.facts = facts
        self.diagnosticProxySeeds = Array(diagnosticProxySeeds.prefix(4))
        self.staleDescendants = Array(staleDescendants.prefix(2))
    }
}

struct SemanticTraversalQueueEntry {
    let element: AXUIElement
    let parent: AXUIElement?
    let depth: Int
    let underToolbar: Bool
    // This is a private aggregate from the successful parent read, not a
    // child position or an AX identity.  It is used only to form a closed
    // parent-refresh comparison class.
    let parentChildCount: Int
}

enum SemanticStaleNodeClass: String {
    case container
    case textControl = "text_control"
    case staticValue = "static_value"
    case actionControl = "action_control"
    case other

    static func from(role: String) -> SemanticStaleNodeClass {
        switch role {
        case "AXGroup", "AXToolbar", "AXSplitGroup", "AXScrollArea", "AXLayoutArea":
            return .container
        case "AXTextField", "AXComboBox", "AXTextArea":
            return .textControl
        case "AXStaticText":
            return .staticValue
        case "AXButton", "AXMenuButton", "AXPopUpButton", "AXMenuItem":
            return .actionControl
        default:
            return .other
        }
    }
}

struct SemanticStaleDescendant {
    let parent: AXUIElement
    let staleElement: AXUIElement
    let nodeClass: SemanticStaleNodeClass
    let depth: Int
    let parentChildCount: Int
}

struct SemanticFixedAttributeInventory {
    let known: Bool
    let contents: Bool
    let visibleChildren: Bool
    let navigationOrder: Bool
    let sharedText: Bool
    let titleUIElement: Bool
    let servesAsTitle: Bool
    let linkedUIElements: Bool
    let parent: Bool
}

struct SemanticFixedParameterizedInventory {
    let known: Bool
    let searchPredicate: Bool
    let elementForTextMarker: Bool
    let textMarkerRangeForElement: Bool
}

enum SemanticElementListOutcome: String {
    case complete
    case empty
    case fanoutTruncated = "fanout_truncated"
    case axFailure = "ax_failure"
    case payloadMissing = "payload_missing"
    case payloadInvalid = "payload_invalid"
    case payloadMixed = "payload_mixed"
}

enum SemanticElementListCardinalityClass: String {
    case unknown
    case zero
    case one
    case twoToCap = "two_to_cap"
    case overCap = "over_cap"
}

enum SemanticExposureIncompleteCause: String, Hashable {
    case edgeFanout = "edge_fanout"
    case depthLimit = "depth_limit"
    case globalNodeLimit = "global_node_limit"
    case globalReadLimit = "global_read_limit"
    case queueRemainder = "queue_remainder"
    case focusCardinality = "focus_cardinality"
    case payloadInvalid = "payload_invalid"
    case attributeInventoryUnknown = "attribute_inventory_unknown"
    case parameterizedInventoryUnknown = "parameterized_inventory_unknown"
    case edgeIncompleteWithoutFailure = "edge_incomplete_without_failure"
    case counterSaturation = "counter_saturation"
}

enum SemanticExposureEdgeSource: String, Hashable {
    case contents
    case visibleChildren = "visible_children"
    case navigationOrder = "navigation_order"
    case sharedText = "shared_text"
    case titleRelation = "title_relation"
    case servesAsTitle = "serves_as_title"
    case linked
    case parent
}

enum SemanticExposureCountSaturationClass: String, Hashable {
    case incompleteCauseCount = "incomplete_cause_count"
    case edgeFanout = "edge_fanout"
    case depthLimitNewTarget = "depth_limit_new_target"
    case depthLimitQueuedTarget = "depth_limit_queued_target"
    case queueRemainder = "queue_remainder"
    case payloadMissing = "payload_missing"
    case payloadInvalid = "payload_invalid"
    case payloadMixed = "payload_mixed"
    case attributeInventoryUnknown = "attribute_inventory_unknown"
    case parameterizedInventoryUnknown = "parameterized_inventory_unknown"
    case edgeIncompleteWithoutFailure = "edge_incomplete_without_failure"
    case nodeOwnershipRejected = "node_ownership_rejected"
    case edgeTargetOwnershipRejected = "edge_target_ownership_rejected"
    case nodesVisited = "nodes_visited"
    case edgeReads = "edge_reads"
    case edgeReadFailures = "edge_read_failures"
    case exactOwned = "exact_owned"
    case nonWeb = "non_web"
    case allowedRole = "allowed_role"
    case fullEligibility = "full_eligibility"
    case sharedTextRelation = "shared_text_relation"
    case parameterizedCapability = "parameterized_capability"
    case pageControl = "page_control"
}

struct SemanticElementListReadResult {
    let elements: [AXUIElement]
    let complete: Bool
    let truncated: Bool
    let readAttempts: Int
    let failed: Bool
    let outcome: SemanticElementListOutcome
    let cardinalityClass: SemanticElementListCardinalityClass

    init(
        elements: [AXUIElement], complete: Bool, truncated: Bool,
        readAttempts: Int, failed: Bool,
        outcome: SemanticElementListOutcome? = nil,
        cardinalityClass: SemanticElementListCardinalityClass? = nil
    ) {
        self.elements = elements
        self.complete = complete
        self.truncated = truncated
        self.readAttempts = readAttempts
        self.failed = failed
        self.outcome = outcome ?? (failed ? .axFailure : (truncated ? .fanoutTruncated : (elements.isEmpty ? .empty : .complete)))
        self.cardinalityClass = cardinalityClass ?? (
            truncated ? .overCap : (elements.isEmpty ? .zero : (elements.count == 1 ? .one : .twoToCap))
        )
    }
}

struct SemanticExposureProbeFacts {
    var performed = false
    var complete = false
    var truncated = false
    var contentsAdvertised = false
    var visibleChildrenAdvertised = false
    var navigationOrderAdvertised = false
    var sharedTextAdvertised = false
    var focusedElementPresent = false
    var focusedElementExactOwned = false
    var focusedElementNonWeb = false
    var focusedElementAllowedRole = false
    var searchPredicateAdvertised = false
    var textMarkerRelationAdvertised = false
    var allowedRoleFound = false
    var fullEligibilityFound = false
    var nodesVisitedCount = 0
    var edgeReadsCount = 0
    var edgeReadFailureCount = 0
    var exactOwnedCount = 0
    var nonWebCount = 0
    var allowedRoleCount = 0
    var fullEligibilityCount = 0
    var sharedTextRelationCount = 0
    var parameterizedCapabilityCount = 0
    var pageControlCount = 0
    var edgeFanoutTruncatedCount = 0
    var depthLimitNewTargetCount = 0
    var depthLimitQueuedTargetCount = 0
    var queueRemainderCount = 0
    var payloadMissingCount = 0
    var payloadInvalidCount = 0
    var payloadMixedCount = 0
    var attributeInventoryUnknownCount = 0
    var parameterizedInventoryUnknownCount = 0
    var edgeIncompleteWithoutFailureCount = 0
    var nodeOwnershipRejectedCount = 0
    var edgeTargetOwnershipRejectedCount = 0
    var globalNodeLimitHit = false
    var globalReadLimitHit = false
    var countSaturated = false
    var focusCardinality = "unknown"
    var sawStructuralRole = false
    var sawRelationshipRole = false
    var sawFocusedPageControl = false
    var sawUnlistedProxy = false
    var sourceKinds = Set<String>()
    var parameterizedKinds = Set<String>()
    var incompleteCauses = Set<SemanticExposureIncompleteCause>()
    var fanoutSources = Set<SemanticExposureEdgeSource>()
    var depthLimitSources = Set<SemanticExposureEdgeSource>()
    var countSaturationClasses = Set<SemanticExposureCountSaturationClass>()

    mutating func markIncomplete(_ cause: SemanticExposureIncompleteCause) {
        incompleteCauses.insert(cause)
        if incompleteCauses.count > 8 {
            truncated = true
            countSaturated = true
            incompleteCauses.insert(.counterSaturation)
            countSaturationClasses.insert(.incompleteCauseCount)
        }
    }

    mutating func increment(
        _ keyPath: WritableKeyPath<SemanticExposureProbeFacts, Int>, cap: Int,
        saturationClass: SemanticExposureCountSaturationClass
    ) {
        if self[keyPath: keyPath] < cap {
            self[keyPath: keyPath] += 1
        } else {
            truncated = true
            countSaturated = true
            markIncomplete(.counterSaturation)
            countSaturationClasses.insert(saturationClass)
        }
    }

    func incompleteCause() -> String {
        if incompleteCauses.isEmpty { return "none" }
        return incompleteCauses.count == 1 ? incompleteCauses.first!.rawValue : "multiple"
    }

    func edgeSource(_ sources: Set<SemanticExposureEdgeSource>) -> String {
        if sources.isEmpty { return "none" }
        return sources.count == 1 ? sources.first!.rawValue : "multiple"
    }

    func countSaturationClass() -> String {
        if countSaturationClasses.isEmpty { return "none" }
        return countSaturationClasses.count == 1 ? countSaturationClasses.first!.rawValue : "multiple"
    }

    func stage() -> String {
        if !complete || truncated { return "incomplete" }
        if sawStructuralRole { return "alternate_structural_role_found" }
        if sawRelationshipRole { return "relationship_role_found" }
        if sawFocusedPageControl { return "focused_page_control" }
        if searchPredicateAdvertised || textMarkerRelationAdvertised {
            return "capability_advertised_only"
        }
        if sawUnlistedProxy { return "only_unlisted_proxy" }
        return "complete_no_fixed_exposure"
    }

    func source() -> String {
        if sourceKinds.isEmpty { return "none" }
        return sourceKinds.count == 1 ? sourceKinds.first! : "multiple"
    }

    func parameterizedCapabilityClass() -> String {
        if parameterizedKinds.isEmpty { return "none" }
        return parameterizedKinds.count == 1 ? parameterizedKinds.first! : "multiple"
    }

    func payload() -> [String: Any] {
        [
            "semantic_exposure_probe_performed": performed,
            "semantic_exposure_probe_complete": complete,
            "semantic_exposure_probe_truncated": truncated,
            "semantic_alt_contents_advertised": contentsAdvertised,
            "semantic_alt_visible_children_advertised": visibleChildrenAdvertised,
            "semantic_alt_navigation_order_advertised": navigationOrderAdvertised,
            "semantic_alt_shared_text_advertised": sharedTextAdvertised,
            "semantic_alt_focused_element_present": focusedElementPresent,
            "semantic_alt_focused_element_exact_owned": focusedElementExactOwned,
            "semantic_alt_focused_element_non_web": focusedElementNonWeb,
            "semantic_alt_focused_element_allowed_role": focusedElementAllowedRole,
            "semantic_alt_search_predicate_advertised": searchPredicateAdvertised,
            "semantic_alt_text_marker_relation_advertised": textMarkerRelationAdvertised,
            "semantic_alt_allowed_role_found": allowedRoleFound,
            "semantic_alt_full_eligibility_found": fullEligibilityFound,
            "semantic_exposure_nodes_visited_count": min(64, nodesVisitedCount),
            "semantic_exposure_edge_reads_count": min(128, edgeReadsCount),
            "semantic_exposure_edge_read_failure_count": min(16, edgeReadFailureCount),
            "semantic_exposure_exact_owned_count": min(64, exactOwnedCount),
            "semantic_exposure_non_web_count": min(64, nonWebCount),
            "semantic_exposure_allowed_role_count": min(8, allowedRoleCount),
            "semantic_exposure_full_eligibility_count": min(8, fullEligibilityCount),
            "semantic_exposure_shared_text_relation_count": min(8, sharedTextRelationCount),
            "semantic_exposure_parameterized_capability_count": min(8, parameterizedCapabilityCount),
            "semantic_exposure_page_control_count": min(8, pageControlCount),
            "semantic_exposure_incomplete_cause_count": min(8, incompleteCauses.count),
            "semantic_exposure_edge_fanout_truncated_count": min(16, edgeFanoutTruncatedCount),
            "semantic_exposure_depth_limit_new_target_count": min(16, depthLimitNewTargetCount),
            "semantic_exposure_depth_limit_queued_target_count": min(16, depthLimitQueuedTargetCount),
            "semantic_exposure_queue_remainder_count": min(64, queueRemainderCount),
            "semantic_exposure_payload_missing_count": min(16, payloadMissingCount),
            "semantic_exposure_payload_invalid_count": min(16, payloadInvalidCount),
            "semantic_exposure_payload_mixed_count": min(16, payloadMixedCount),
            "semantic_exposure_attribute_inventory_unknown_count": min(16, attributeInventoryUnknownCount),
            "semantic_exposure_parameterized_inventory_unknown_count": min(5, parameterizedInventoryUnknownCount),
            "semantic_exposure_edge_incomplete_without_failure_count": min(16, edgeIncompleteWithoutFailureCount),
            "semantic_exposure_node_ownership_rejected_count": min(64, nodeOwnershipRejectedCount),
            "semantic_exposure_edge_target_ownership_rejected_count": min(64, edgeTargetOwnershipRejectedCount),
            "semantic_exposure_global_node_limit_hit": globalNodeLimitHit,
            "semantic_exposure_global_read_limit_hit": globalReadLimitHit,
            "semantic_exposure_count_saturated": countSaturated,
            "semantic_exposure_incomplete_cause": incompleteCause(),
            "semantic_exposure_fanout_source": edgeSource(fanoutSources),
            "semantic_exposure_depth_limit_source": edgeSource(depthLimitSources),
            "semantic_exposure_focus_cardinality": focusCardinality,
            "semantic_exposure_count_saturation_class": countSaturationClass(),
            "semantic_exposure_stage": stage(),
            "semantic_exposure_source": source(),
            "semantic_parameterized_capability_class": parameterizedCapabilityClass()
        ]
    }
}

func semanticCGRect(_ element: AXUIElement) -> CGRect? {
    let frame = axFrame(element)
    guard frame["x"] != nil, frame["y"] != nil, frame["width"] != nil, frame["height"] != nil else {
        return nil
    }
    let result = CGRect(
        x: doubleValue(frame["x"]), y: doubleValue(frame["y"]),
        width: doubleValue(frame["width"]), height: doubleValue(frame["height"])
    )
    return result.width > 0 && result.height > 0 ? result : nil
}

func semanticElementAttribute(_ element: AXUIElement, _ attribute: String) -> AXUIElement? {
    guard let value = axAttribute(element, attribute as CFString) else { return nil }
    return asAXUIElement(value)
}

func semanticOwnershipProof(element: AXUIElement, window: AXUIElement) -> String {
    if let owner = semanticElementAttribute(element, "AXWindow"), CFEqual(owner, window) {
        return "ax_window_attribute"
    }
    if let topLevel = semanticElementAttribute(element, "AXTopLevelUIElement"), CFEqual(topLevel, window) {
        return "top_level_ui_element"
    }
    var current = element
    for _ in 0..<32 {
        guard let parent = semanticElementAttribute(current, "AXParent") else { break }
        if CFEqual(parent, window) { return "ancestor_chain" }
        current = parent
    }
    return "none"
}

func semanticHasForbiddenAncestor(
    element: AXUIElement,
    forbiddenRoles: Set<String>,
    inheritedForbidden: Bool = false
) -> Bool {
    if inheritedForbidden { return true }
    var current = element
    for _ in 0..<32 {
        guard let parent = semanticElementAttribute(current, "AXParent") else { break }
        let role = axTextAttribute(parent, kAXRoleAttribute as CFString) ?? ""
        if forbiddenRoles.contains(role) { return true }
        current = parent
    }
    return false
}

func observeSemanticRoleSignals(
    element: AXUIElement,
    role: String,
    facts: inout SemanticTextDiscoveryFacts
) {
    if role == "AXTextField" { facts.sawAXTextField = true }
    if role == "AXComboBox" { facts.sawAXComboBox = true }
    if role == "AXTextArea" { facts.sawAXTextArea = true }
    let subrole = axTextAttribute(element, kAXSubroleAttribute as CFString) ?? ""
    if subrole == "AXSearchField" { facts.sawAXSearchFieldSubrole = true }
    if !["AXTextField", "AXComboBox", "AXTextArea"].contains(role) {
        let valuePresent = axAttribute(element, kAXValueAttribute as CFString) != nil
        let selectedTextSettable = axAttributeIsSettable(element, kAXSelectedTextAttribute as CFString)
        if valuePresent || selectedTextSettable { facts.sawUnlistedTextCapableRole = true }
    }
}

func markUnlistedSemanticRoleClass(role: String, facts: inout SemanticTextDiscoveryFacts) {
    switch role {
    case "AXGroup", "AXToolbar", "AXSplitGroup", "AXScrollArea", "AXLayoutArea":
        facts.sawUnlistedContainerClass = true
    case "AXStaticText":
        facts.sawUnlistedStaticValueClass = true
    case "AXButton", "AXMenuButton", "AXPopUpButton", "AXMenuItem":
        facts.sawUnlistedActionControlClass = true
    case "AXWebArea":
        facts.sawUnlistedWebRootClass = true
    default:
        facts.sawUnlistedOtherClass = true
    }
}

func observeUnlistedSemanticPipeline(
    element: AXUIElement,
    role: String,
    window: ExactSemanticWindow,
    selector: SemanticTextSelector,
    owned: Bool,
    inheritedForbidden: Bool,
    underToolbar: Bool,
    facts: inout SemanticTextDiscoveryFacts
) {
    guard !["AXTextField", "AXComboBox", "AXTextArea"].contains(role) else { return }
    let copiedValue = axAttribute(element, kAXValueAttribute as CFString)
    let selectedTextSettable = axAttributeIsSettable(element, kAXSelectedTextAttribute as CFString)
    guard copiedValue != nil || selectedTextSettable else { return }
    facts.increment(\.unlistedTextCapableCount)
    facts.sawUnlistedTextCapableRole = true
    markUnlistedSemanticRoleClass(role: role, facts: &facts)
    guard owned else { return }
    facts.increment(\.unlistedWindowOwnedCount)
    guard !semanticHasForbiddenAncestor(
        element: element,
        forbiddenRoles: selector.forbiddenAncestorRoles,
        inheritedForbidden: inheritedForbidden
    ) else { return }
    facts.increment(\.unlistedNonWebCount)
    let relative = relativeCenter(
        candidateFrame: semanticCGRect(element), windowFrame: window.frame,
        minX: selector.minX, maxX: selector.maxX, minY: selector.minY, maxY: selector.maxY
    )
    guard relative.childFrameValid else { return }
    facts.increment(\.unlistedFrameValidCount)
    guard relative.relativeRegionMatched else { return }
    facts.increment(\.unlistedRegionMatchCount)
    let enabled = axBoolAttribute(element, kAXEnabledAttribute as CFString, default: false)
    guard !selector.requireEnabled || enabled else { return }
    facts.increment(\.unlistedEnabledCount)
    guard axTextAttribute(element, kAXValueAttribute as CFString) != nil else { return }
    facts.increment(\.unlistedValueReadableCount)
    let valueSettable = axAttributeIsSettable(element, kAXValueAttribute as CFString)
    let selectedRangeSettable = axAttributeIsSettable(element, kAXSelectedTextRangeAttribute as CFString)
    let focusSettable = axAttributeIsSettable(element, kAXFocusedAttribute as CFString)
    if valueSettable { facts.increment(\.unlistedValueSettableCount) }
    if selectedTextSettable { facts.increment(\.unlistedSelectedTextSettableCount) }
    if selectedRangeSettable { facts.increment(\.unlistedSelectedRangeSettableCount) }
    if focusSettable { facts.increment(\.unlistedFocusSettableCount) }
    if semanticAttributeNames(element) != nil {
        facts.increment(\.unlistedAttributeCapabilityKnownCount)
    }
    if underToolbar { facts.increment(\.unlistedUnderToolbarCount) }
    if valueSettable || (selectedTextSettable && selectedRangeSettable) {
        facts.increment(\.unlistedMutationReadyCount)
    }

    // Diagnostic-only, one-hop relationship probe. Related elements never become
    // candidates here and must independently pass the authoritative window BFS.
    let relationAttributes: [(String, String)] = [
        ("AXTitleUIElement", "title_relation"),
        ("AXServesAsTitleForUIElements", "title_relation"),
        ("AXLinkedUIElements", "linked_relation")
    ]
    for (attribute, kind) in relationAttributes {
        guard let value = axAttribute(element, attribute as CFString) else { continue }
        let related: [AXUIElement]
        if let single = asAXUIElement(value) {
            related = [single]
        } else if let values = value as? [Any] {
            let converted = values.compactMap { asAXUIElement($0) }
            if converted.count != values.count { facts.unlistedRelationScanComplete = false }
            related = Array(converted.prefix(8))
            if converted.count > 8 { facts.unlistedRelationScanComplete = false }
        } else {
            facts.unlistedRelationScanComplete = false
            continue
        }
        for target in related {
            guard semanticOwnershipProof(element: target, window: window.element) != "none",
                  !semanticHasForbiddenAncestor(
                    element: target, forbiddenRoles: selector.forbiddenAncestorRoles
                  )
            else { continue }
            let relatedRole = axTextAttribute(target, kAXRoleAttribute as CFString) ?? ""
            if selector.roles.contains(relatedRole) {
                facts.increment(\.unlistedRelatedAllowedRoleCount)
                if kind == "title_relation" { facts.sawUnlistedTitleRelation = true }
                if kind == "linked_relation" { facts.sawUnlistedLinkedRelation = true }
            }
        }
    }
    if let parent = semanticElementAttribute(element, "AXParent"),
       semanticOwnershipProof(element: parent, window: window.element) != "none",
       !semanticHasForbiddenAncestor(element: parent, forbiddenRoles: selector.forbiddenAncestorRoles),
       selector.roles.contains(axTextAttribute(parent, kAXRoleAttribute as CFString) ?? "") {
        facts.increment(\.unlistedRelatedAllowedRoleCount)
        facts.sawUnlistedParentChildRelation = true
    }
}

func evaluateSemanticCandidate(
    element: AXUIElement,
    window: ExactSemanticWindow,
    selector: SemanticTextSelector,
    owned: Bool,
    ownershipProof: String,
    inheritedForbidden: Bool,
    underToolbar: Bool = false,
    diagnosticOnlyApp: Bool = false,
    facts: inout SemanticTextDiscoveryFacts
) -> (eligible: Bool, width: Double) {
    let role = axTextAttribute(element, kAXRoleAttribute as CFString) ?? ""
    observeSemanticRoleSignals(element: element, role: role, facts: &facts)
    observeUnlistedSemanticPipeline(
        element: element, role: role, window: window, selector: selector,
        owned: owned, inheritedForbidden: inheritedForbidden,
        underToolbar: underToolbar, facts: &facts
    )
    guard selector.roles.contains(role) else { return (false, 0) }
    if diagnosticOnlyApp {
        if owned { facts.increment(\.appOwnedAllowedRoleCount) }
        return (false, 0)
    }
    facts.increment(\.roleMatchCount)
    facts.increment(\.windowAllowedRoleCount)
    guard owned else { return (false, 0) }
    facts.increment(\.windowOwnedCount)
    if facts.ownershipProof == "none" { facts.ownershipProof = ownershipProof }
    let forbidden = semanticHasForbiddenAncestor(
        element: element,
        forbiddenRoles: selector.forbiddenAncestorRoles,
        inheritedForbidden: inheritedForbidden
    )
    if forbidden {
        facts.sawAXWebAreaAncestor = facts.sawAXWebAreaAncestor
            || selector.forbiddenAncestorRoles.contains("AXWebArea")
        return (false, 0)
    }
    facts.increment(\.nonWebContentCount)
    let candidateFrame = semanticCGRect(element)
    let relative = relativeCenter(
        candidateFrame: candidateFrame, windowFrame: window.frame,
        minX: selector.minX, maxX: selector.maxX, minY: selector.minY, maxY: selector.maxY
    )
    facts.childFrameValid = facts.childFrameValid || relative.childFrameValid
    facts.childCenterInsideWindow = facts.childCenterInsideWindow || relative.childCenterInsideWindow
    facts.relativeRegionEvaluable = facts.relativeRegionEvaluable || relative.relativeRegionEvaluable
    facts.relativeRegionMatched = facts.relativeRegionMatched || relative.relativeRegionMatched
    // Observe only the frame already read for the authoritative candidate
    // check.  This remains diagnostic-only and does not add an AX read.
    facts.observeAllowedRoleGeometry(
        role: role, candidateFrame: candidateFrame, windowFrame: window.frame,
        selector: selector, relative: relative
    )
    guard relative.childFrameValid else { return (false, 0) }
    facts.increment(\.frameValidCount)
    guard relative.relativeRegionMatched else { return (false, 0) }
    facts.increment(\.regionMatchCount)
    let enabled = axBoolAttribute(element, kAXEnabledAttribute as CFString, default: false)
    guard !selector.requireEnabled || enabled else { return (false, 0) }
    facts.increment(\.enabledCount)
    let copiedValue = axAttribute(element, kAXValueAttribute as CFString)
    guard copiedValue != nil else { return (false, 0) }
    facts.increment(\.valuePresentCount)
    guard axTextAttribute(element, kAXValueAttribute as CFString) != nil else { return (false, 0) }
    facts.increment(\.valueReadableCount)
    let valueSettable = axAttributeIsSettable(element, kAXValueAttribute as CFString)
    let selectedTextSettable = axAttributeIsSettable(element, kAXSelectedTextAttribute as CFString)
    let selectedRangeSettable = axAttributeIsSettable(element, kAXSelectedTextRangeAttribute as CFString)
    let focusSettable = axAttributeIsSettable(element, kAXFocusedAttribute as CFString)
    if valueSettable { facts.increment(\.valueSettableCount) }
    if selectedTextSettable { facts.increment(\.selectedTextSettableCount) }
    if selectedRangeSettable { facts.increment(\.selectedRangeSettableCount) }
    if focusSettable { facts.increment(\.focusSettableCount) }
    let mutationReady = valueSettable || (selectedTextSettable && selectedRangeSettable)
    guard !selector.requireSettable || mutationReady else { return (false, 0) }
    facts.increment(\.finalCandidateCount, cap: 8)
    return (true, Double(candidateFrame?.width ?? 0))
}

func semanticDiagnosticProxySeed(
    element: AXUIElement,
    role: String,
    window: ExactSemanticWindow,
    selector: SemanticTextSelector,
    inheritedForbidden: Bool
) -> Bool {
    guard !selector.roles.contains(role),
          !semanticHasForbiddenAncestor(
              element: element,
              forbiddenRoles: selector.forbiddenAncestorRoles,
              inheritedForbidden: inheritedForbidden
          )
    else { return false }
    let relative = relativeCenter(
        candidateFrame: semanticCGRect(element), windowFrame: window.frame,
        minX: selector.minX, maxX: selector.maxX, minY: selector.minY, maxY: selector.maxY
    )
    guard relative.relativeRegionMatched,
          axTextAttribute(element, kAXValueAttribute as CFString) != nil
    else { return false }
    return true
}

func semanticTextDiscoveryPass(
    window: ExactSemanticWindow,
    selector: SemanticTextSelector,
    includeBroadAppDiagnostic: Bool = true,
    additionalReadBudget: SemanticAdditionalChildrenReadBudget? = nil
) -> SemanticTextDiscovery {
    var facts = SemanticTextDiscoveryFacts()
    var eligible: [(AXUIElement, Double)] = []
    var diagnosticProxySeeds: [AXUIElement] = []
    var staleDescendants: [SemanticStaleDescendant] = []
    var queue: [SemanticTraversalQueueEntry] = [
        SemanticTraversalQueueEntry(
            element: window.element, parent: nil, depth: 0, underToolbar: false,
            parentChildCount: 0
        )
    ]
    // AXUIElement has no stable public identity. Keep references local and use
    // CFEqual for BFS visit/enqueue deduplication so a cyclic or multiply
    // exposed branch cannot inflate actionable counts.
    var visited: [AXUIElement] = []
    var queued: [AXUIElement] = [window.element]
    var head = 0
    while head < queue.count && facts.windowNodesVisitedCount < 255 {
        let entry = queue[head]
        head += 1
        let element = entry.element
        if visited.contains(where: { CFEqual($0, element) }) {
            facts.increment(\.windowDuplicateNodesSkippedCount, cap: 255)
            continue
        }
        visited.append(element)
        let depth = entry.depth
        let underToolbar = entry.underToolbar
        facts.increment(\.nodesVisitedCount, cap: 255)
        facts.increment(\.windowNodesVisitedCount, cap: 255)
        facts.windowMaxDepthReached = min(20, max(facts.windowMaxDepthReached, depth))
        let role = axTextAttribute(element, kAXRoleAttribute as CFString) ?? ""
        let forbiddenRoot = selector.forbiddenAncestorRoles.contains(role)
        // Evaluate the currently observed node before attempting its children.
        // A subsequent unknown descendant can mark the scan incomplete, but it
        // never makes this node (or any candidate) actionable by itself.
        let candidate = evaluateSemanticCandidate(
            element: element, window: window, selector: selector,
            owned: true, ownershipProof: "window_descendant",
            inheritedForbidden: forbiddenRoot, underToolbar: underToolbar, facts: &facts
        )
        if candidate.eligible {
            facts.increment(\.preinvalidationCandidateCount, cap: 8)
            if eligible.count < 8 {
                eligible.append((element, candidate.width))
            } else {
                facts.countsTruncated = true
            }
        } else if diagnosticProxySeeds.count < 4,
                  semanticDiagnosticProxySeed(
                      element: element, role: role, window: window, selector: selector,
                      inheritedForbidden: forbiddenRoot
                  ),
                  !diagnosticProxySeeds.contains(where: { CFEqual($0, element) }) {
            diagnosticProxySeeds.append(element)
        }
        if forbiddenRoot {
            facts.increment(\.forbiddenRootCount)
            facts.increment(\.forbiddenSubtreePrunedCount)
        } else {
            let primaryChildRead = semanticChildren(
                element, additionalReadBudget: additionalReadBudget
            )
            let authoritativeChildRead = semanticAuthoritativeChildren(
                element, primary: primaryChildRead
            )
            if let navigationOrder = authoritativeChildRead.navigationOrder {
                facts.recordNavigationOrderFallback(navigationOrder)
            }
            let childRead = authoritativeChildRead.effective
            facts.recordChildrenRead(
                childRead, role: role, windowRoot: depth == 0, underToolbar: underToolbar
            )
            if childRead.outcome == .staleElement {
                let staleClass = SemanticStaleNodeClass.from(role: role)
                facts.staleNodeClasses.insert(staleClass)
                facts.staleNodeSelfEligible = facts.staleNodeSelfEligible || candidate.eligible
                facts.staleBranchScopes.insert(
                    depth == 0
                        ? "window_root"
                        : (candidate.eligible ? "candidate_node" : "selector_relevant_unknown")
                )
                if depth > 0,
                   let parent = entry.parent,
                   staleDescendants.count < 2 {
                    staleDescendants.append(
                        SemanticStaleDescendant(
                            parent: parent,
                            staleElement: element,
                            nodeClass: staleClass,
                            depth: depth,
                            parentChildCount: entry.parentChildCount
                        )
                    )
                }
            } else if childRead.branchProvenEmpty,
                      childRead.observedErrorClasses.contains(.invalidElement) {
                facts.staleBranchScopes.insert("structurally_empty")
            }
            if childRead.scanIncomplete {
                facts.windowScanTruncated = true
            } else if depth < 20 {
                let childUnderToolbar = underToolbar || role == "AXToolbar"
                let parentChildCount = min(64, childRead.children.count)
                for child in childRead.children {
                    if visited.contains(where: { CFEqual($0, child) })
                        || queued.contains(where: { CFEqual($0, child) }) {
                        facts.increment(\.windowDuplicateNodesSkippedCount, cap: 255)
                    } else {
                        queue.append(
                            SemanticTraversalQueueEntry(
                                element: child, parent: element, depth: depth + 1,
                                underToolbar: childUnderToolbar,
                                parentChildCount: parentChildCount
                            )
                        )
                        queued.append(child)
                    }
                }
            } else if !childRead.children.isEmpty {
                facts.windowDepthTruncated = true
            }
        }
    }
    if head < queue.count {
        facts.windowScanTruncated = true
        facts.windowNodeBudgetTruncated = true
    }
    facts.windowScanComplete = !facts.windowScanTruncated && !facts.windowDepthTruncated
    if !facts.windowScanComplete { facts.countsTruncated = true }
    facts.actionableCountsTruncated = facts.countsTruncated

    // Diagnostic-only app scan. It never contributes actionable references.
    if includeBroadAppDiagnostic && facts.windowScanComplete && facts.roleMatchCount == 0 {
        facts.appScanPerformed = true
        let app = AXUIElementCreateApplication(window.pid)
        var appQueue: [(AXUIElement, Int, Bool)] = [(app, 0, false)]
        var appHead = 0
        while appHead < appQueue.count && facts.appNodesVisitedCount < 255 {
            let (element, depth, underToolbar) = appQueue[appHead]
            appHead += 1
            facts.increment(\.appNodesVisitedCount, cap: 255)
            let role = axTextAttribute(element, kAXRoleAttribute as CFString) ?? ""
            let exactWindowRoot = CFEqual(element, window.element)
            let otherWindowRoot = role == "AXWindow" && !exactWindowRoot
            let forbiddenRoot = selector.forbiddenAncestorRoles.contains(role)
            if otherWindowRoot {
                facts.increment(\.otherWindowPrunedCount)
            }
            if forbiddenRoot {
                facts.increment(\.forbiddenRootCount)
                facts.increment(\.forbiddenSubtreePrunedCount)
            }
            if !exactWindowRoot && !otherWindowRoot && !forbiddenRoot {
                let childRead = semanticChildren(element)
                facts.recordChildrenRead(
                    childRead, role: role, windowRoot: false, underToolbar: underToolbar,
                    exactWindowScan: false
                )
                if childRead.scanIncomplete {
                    facts.appScanTruncated = true
                } else if depth < 20 {
                    let childUnderToolbar = underToolbar || role == "AXToolbar"
                    for child in childRead.children {
                        appQueue.append((child, depth + 1, childUnderToolbar))
                    }
                } else if !childRead.children.isEmpty {
                    facts.appScanTruncated = true
                }
            }
            let proof = semanticOwnershipProof(element: element, window: window.element)
            if proof != "none" {
                if facts.appDiagnosticOwnershipProof == "none" {
                    facts.appDiagnosticOwnershipProof = proof
                } else if facts.appDiagnosticOwnershipProof != proof {
                    facts.appDiagnosticOwnershipProof = "multiple"
                }
            }
            _ = evaluateSemanticCandidate(
                element: element, window: window, selector: selector,
                owned: proof != "none", ownershipProof: proof,
                inheritedForbidden: forbiddenRoot, underToolbar: underToolbar,
                diagnosticOnlyApp: true, facts: &facts
            )
        }
        if appHead < appQueue.count { facts.appScanTruncated = true }
        facts.appScanComplete = !facts.appScanTruncated
        if !facts.appScanComplete { facts.countsTruncated = true }
        facts.appDiagnosticCountsTruncated = facts.appScanTruncated
            || (facts.countsTruncated && !facts.actionableCountsTruncated)
    }
    return SemanticTextDiscovery(
        eligibleCandidates: eligible.sorted { $0.1 > $1.1 }.map { $0.0 },
        facts: facts,
        diagnosticProxySeeds: diagnosticProxySeeds,
        staleDescendants: staleDescendants
    )
}

func semanticExposureFullEligibility(
    element: AXUIElement,
    window: ExactSemanticWindow,
    selector: SemanticTextSelector
) -> Bool {
    let role = axTextAttribute(element, kAXRoleAttribute as CFString) ?? ""
    guard selector.roles.contains(role),
          semanticOwnershipProof(element: element, window: window.element) != "none",
          !semanticHasForbiddenAncestor(element: element, forbiddenRoles: selector.forbiddenAncestorRoles)
    else { return false }
    let relative = relativeCenter(
        candidateFrame: semanticCGRect(element), windowFrame: window.frame,
        minX: selector.minX, maxX: selector.maxX, minY: selector.minY, maxY: selector.maxY
    )
    guard relative.relativeRegionMatched else { return false }
    if selector.requireEnabled && !axBoolAttribute(element, kAXEnabledAttribute as CFString, default: false) {
        return false
    }
    guard axTextAttribute(element, kAXValueAttribute as CFString) != nil else { return false }
    let valueSettable = axAttributeIsSettable(element, kAXValueAttribute as CFString)
    let selectedTextSettable = axAttributeIsSettable(element, kAXSelectedTextAttribute as CFString)
    let selectedRangeSettable = axAttributeIsSettable(element, kAXSelectedTextRangeAttribute as CFString)
    return !selector.requireSettable || valueSettable || (selectedTextSettable && selectedRangeSettable)
}

func semanticAlternateExposureProbe(
    window: ExactSemanticWindow,
    selector: SemanticTextSelector,
    proxySeeds: [AXUIElement],
    attributeInventory: (AXUIElement) -> SemanticFixedAttributeInventory = semanticFixedAttributeInventory,
    parameterizedInventory: (AXUIElement) -> SemanticFixedParameterizedInventory = semanticFixedParameterizedInventory,
    readElements: (AXUIElement, String, Int, Int) -> SemanticElementListReadResult = {
        semanticElementListAttribute(
            $0, attribute: $1, maximumElements: $2, maximumReadAttempts: $3
        )
    },
    focusedElement: (pid_t, Int) -> SemanticElementListReadResult = { pid, attempts in
        semanticElementListAttribute(
            AXUIElementCreateApplication(pid), attribute: "AXFocusedUIElement",
            maximumElements: 1, maximumReadAttempts: attempts
        )
    },
    roleOf: (AXUIElement) -> String = {
        axTextAttribute($0, kAXRoleAttribute as CFString) ?? ""
    },
    ownershipOf: (AXUIElement, AXUIElement) -> String = semanticOwnershipProof,
    forbidden: (AXUIElement, Set<String>) -> Bool = {
        semanticHasForbiddenAncestor(element: $0, forbiddenRoles: $1)
    },
    fullyEligible: (AXUIElement, ExactSemanticWindow, SemanticTextSelector) -> Bool = semanticExposureFullEligibility
) -> SemanticExposureProbeFacts {
    var facts = SemanticExposureProbeFacts()
    facts.performed = true
    facts.sawUnlistedProxy = !proxySeeds.isEmpty
    var incomplete = false

    var seeds: [AXUIElement] = [window.element]
    for seed in proxySeeds.prefix(4) where !seeds.contains(where: { CFEqual($0, seed) }) {
        seeds.append(seed)
    }

    // Inventory parameterized capabilities only; never invoke them.
    for seed in seeds {
        let inventory = parameterizedInventory(seed)
        if !inventory.known {
            incomplete = true
            facts.markIncomplete(.parameterizedInventoryUnknown)
            facts.increment(\.parameterizedInventoryUnknownCount, cap: 5, saturationClass: .parameterizedInventoryUnknown)
            facts.increment(\.edgeReadFailureCount, cap: 16, saturationClass: .edgeReadFailures)
        }
        if inventory.searchPredicate {
            facts.searchPredicateAdvertised = true
            facts.parameterizedKinds.insert("search_predicate")
            facts.increment(\.parameterizedCapabilityCount, cap: 8, saturationClass: .parameterizedCapability)
        }
        if inventory.elementForTextMarker || inventory.textMarkerRangeForElement {
            facts.textMarkerRelationAdvertised = true
            facts.parameterizedKinds.insert("text_marker_relation")
        }
        if inventory.elementForTextMarker {
            facts.increment(\.parameterizedCapabilityCount, cap: 8, saturationClass: .parameterizedCapability)
        }
        if inventory.textMarkerRangeForElement {
            facts.increment(\.parameterizedCapabilityCount, cap: 8, saturationClass: .parameterizedCapability)
        }
    }

    var queue: [(element: AXUIElement, depth: Int)] = seeds.map { ($0, 0) }
    var seen: [AXUIElement] = []
    var head = 0
    while head < queue.count && facts.nodesVisitedCount < 64 {
        let (element, depth) = queue[head]
        head += 1
        if seen.contains(where: { CFEqual($0, element) }) { continue }
        seen.append(element)
        facts.increment(\.nodesVisitedCount, cap: 64, saturationClass: .nodesVisited)

        let isRoot = CFEqual(element, window.element)
        let isTrustedSeed = seeds.contains(where: { CFEqual($0, element) })
        let owned = isRoot || isTrustedSeed || ownershipOf(element, window.element) != "none"
        guard owned else {
            facts.increment(\.nodeOwnershipRejectedCount, cap: 64, saturationClass: .nodeOwnershipRejected)
            continue
        }
        facts.increment(\.exactOwnedCount, cap: 64, saturationClass: .exactOwned)
        let role = roleOf(element)
        if role == "AXWindow" && !isRoot { continue }
        if role == "AXWebArea" { continue }
        let isForbidden = forbidden(element, selector.forbiddenAncestorRoles)
        let allowed = selector.roles.contains(role)
        if isForbidden {
            if allowed {
                facts.increment(\.pageControlCount, cap: 8, saturationClass: .pageControl)
            }
            continue
        }
        facts.increment(\.nonWebCount, cap: 64, saturationClass: .nonWeb)
        if allowed {
            facts.allowedRoleFound = true
            facts.increment(\.allowedRoleCount, cap: 8, saturationClass: .allowedRole)
            if fullyEligible(element, window, selector) {
                facts.fullEligibilityFound = true
                facts.increment(\.fullEligibilityCount, cap: 8, saturationClass: .fullEligibility)
            }
        }

        let inventory = attributeInventory(element)
        guard inventory.known else {
            incomplete = true
            facts.markIncomplete(.attributeInventoryUnknown)
            facts.increment(\.attributeInventoryUnknownCount, cap: 16, saturationClass: .attributeInventoryUnknown)
            facts.increment(\.edgeReadFailureCount, cap: 16, saturationClass: .edgeReadFailures)
            continue
        }
        facts.contentsAdvertised = facts.contentsAdvertised || inventory.contents
        facts.visibleChildrenAdvertised = facts.visibleChildrenAdvertised || inventory.visibleChildren
        facts.navigationOrderAdvertised = facts.navigationOrderAdvertised || inventory.navigationOrder
        facts.sharedTextAdvertised = facts.sharedTextAdvertised || inventory.sharedText
        let edges: [(advertised: Bool, attribute: String, source: String?, diagnosticSource: SemanticExposureEdgeSource, relationship: Bool)] = [
            (inventory.contents, "AXContents", "contents", .contents, false),
            (inventory.visibleChildren, "AXVisibleChildren", "visible_children", .visibleChildren, false),
            (inventory.navigationOrder, "AXChildrenInNavigationOrder", "navigation_order", .navigationOrder, false),
            (inventory.sharedText, "AXSharedTextUIElements", "shared_text", .sharedText, true),
            (inventory.titleUIElement, "AXTitleUIElement", nil, .titleRelation, true),
            (inventory.servesAsTitle, "AXServesAsTitleForUIElements", nil, .servesAsTitle, true),
            (inventory.linkedUIElements, "AXLinkedUIElements", nil, .linked, true),
            (inventory.parent, "AXParent", nil, .parent, true)
        ]
        for edge in edges where edge.advertised {
            guard facts.edgeReadsCount < 128 else {
                facts.truncated = true
                incomplete = true
                facts.globalReadLimitHit = true
                facts.markIncomplete(.globalReadLimit)
                break
            }
            let remainingReads = 128 - facts.edgeReadsCount
            let read = readElements(element, edge.attribute, 8, min(2, remainingReads))
            facts.edgeReadsCount = min(128, facts.edgeReadsCount + read.readAttempts)
            if read.failed {
                facts.increment(\.edgeReadFailureCount, cap: 16, saturationClass: .edgeReadFailures)
            }
            switch read.outcome {
            case .fanoutTruncated:
                facts.increment(\.edgeFanoutTruncatedCount, cap: 16, saturationClass: .edgeFanout)
                facts.fanoutSources.insert(edge.diagnosticSource)
                facts.markIncomplete(.edgeFanout)
            case .payloadMissing:
                facts.increment(\.payloadMissingCount, cap: 16, saturationClass: .payloadMissing)
                facts.markIncomplete(.payloadInvalid)
            case .payloadInvalid:
                facts.increment(\.payloadInvalidCount, cap: 16, saturationClass: .payloadInvalid)
                facts.markIncomplete(.payloadInvalid)
            case .payloadMixed:
                facts.increment(\.payloadMixedCount, cap: 16, saturationClass: .payloadMixed)
                facts.markIncomplete(.payloadInvalid)
            case .complete, .empty, .axFailure:
                break
            }
            if !read.complete && !read.failed {
                facts.increment(\.edgeIncompleteWithoutFailureCount, cap: 16, saturationClass: .edgeIncompleteWithoutFailure)
                if read.outcome != .fanoutTruncated {
                    facts.markIncomplete(.edgeIncompleteWithoutFailure)
                }
            }
            if !read.complete || read.truncated {
                incomplete = true
                facts.truncated = facts.truncated || read.truncated
            }
            if edge.attribute == "AXSharedTextUIElements" {
                for _ in read.elements {
                    facts.increment(\.sharedTextRelationCount, cap: 8, saturationClass: .sharedTextRelation)
                }
            }
            for target in read.elements {
                let targetOwned = CFEqual(target, window.element)
                    || ownershipOf(target, window.element) != "none"
                if !targetOwned {
                    facts.increment(\.edgeTargetOwnershipRejectedCount, cap: 64, saturationClass: .edgeTargetOwnershipRejected)
                }
                let targetRole = roleOf(target)
                let targetForbidden = targetOwned && forbidden(target, selector.forbiddenAncestorRoles)
                if targetOwned && selector.roles.contains(targetRole) {
                    if targetForbidden {
                        facts.increment(\.pageControlCount, cap: 8, saturationClass: .pageControl)
                    } else {
                        facts.allowedRoleFound = true
                        if edge.relationship {
                            facts.sawRelationshipRole = true
                        } else {
                            facts.sawStructuralRole = true
                        }
                        if let source = edge.source { facts.sourceKinds.insert(source) }
                    }
                }
                guard depth < 4 else {
                    if !seen.contains(where: { CFEqual($0, target) }) {
                        facts.truncated = true
                        incomplete = true
                        facts.markIncomplete(.depthLimit)
                        facts.depthLimitSources.insert(edge.diagnosticSource)
                        if queue.contains(where: { CFEqual($0.element, target) }) {
                            facts.increment(\.depthLimitQueuedTargetCount, cap: 16, saturationClass: .depthLimitQueuedTarget)
                        } else {
                            facts.increment(\.depthLimitNewTargetCount, cap: 16, saturationClass: .depthLimitNewTarget)
                        }
                    }
                    continue
                }
                if !seen.contains(where: { CFEqual($0, target) }),
                   !queue.contains(where: { CFEqual($0.element, target) }) {
                    queue.append((target, depth + 1))
                }
            }
        }
    }
    if head < queue.count {
        facts.truncated = true
        incomplete = true
        let remainder = queue.count - head
        facts.queueRemainderCount = min(64, remainder)
        if remainder > 64 {
            facts.countSaturated = true
            facts.markIncomplete(.counterSaturation)
            facts.countSaturationClasses.insert(.queueRemainder)
        }
        if facts.nodesVisitedCount >= 64 {
            facts.globalNodeLimitHit = true
            facts.markIncomplete(.globalNodeLimit)
        } else {
            facts.markIncomplete(.queueRemainder)
        }
    }

    if facts.edgeReadsCount < 128 {
        let focusRead = focusedElement(window.pid, min(2, 128 - facts.edgeReadsCount))
        facts.edgeReadsCount = min(128, facts.edgeReadsCount + focusRead.readAttempts)
        if focusRead.failed {
            facts.increment(\.edgeReadFailureCount, cap: 16, saturationClass: .edgeReadFailures)
        }
        switch focusRead.outcome {
        case .payloadMissing:
            facts.increment(\.payloadMissingCount, cap: 16, saturationClass: .payloadMissing)
            facts.markIncomplete(.payloadInvalid)
        case .payloadInvalid:
            facts.increment(\.payloadInvalidCount, cap: 16, saturationClass: .payloadInvalid)
            facts.markIncomplete(.payloadInvalid)
        case .payloadMixed:
            facts.increment(\.payloadMixedCount, cap: 16, saturationClass: .payloadMixed)
            facts.markIncomplete(.payloadInvalid)
        case .complete, .empty, .fanoutTruncated, .axFailure:
            break
        }
        switch focusRead.cardinalityClass {
        case .zero:
            facts.focusCardinality = "none"
        case .one:
            facts.focusCardinality = "one"
        case .twoToCap, .overCap:
            facts.focusCardinality = "multiple"
        case .unknown:
            facts.focusCardinality = "unknown"
        }
        if !focusRead.complete || focusRead.truncated || focusRead.elements.count > 1 {
            incomplete = true
            facts.truncated = facts.truncated || focusRead.truncated || focusRead.elements.count > 1
            if focusRead.cardinalityClass == .twoToCap || focusRead.cardinalityClass == .overCap {
                facts.markIncomplete(.focusCardinality)
            }
        }
        if let focused = focusRead.elements.first {
            facts.focusedElementPresent = true
            let owned = CFEqual(focused, window.element)
                || ownershipOf(focused, window.element) != "none"
            facts.focusedElementExactOwned = owned
            let role = roleOf(focused)
            let allowed = selector.roles.contains(role)
            facts.focusedElementAllowedRole = allowed
            if owned {
                let nonWeb = !forbidden(focused, selector.forbiddenAncestorRoles)
                    && role != "AXWebArea"
                facts.focusedElementNonWeb = nonWeb
                let alreadyCounted = seen.contains(where: { CFEqual($0, focused) })
                if allowed && !nonWeb {
                    facts.sawFocusedPageControl = true
                    if !alreadyCounted {
                        facts.increment(\.pageControlCount, cap: 8, saturationClass: .pageControl)
                    }
                    facts.sourceKinds.insert("focused_element")
                } else if allowed && nonWeb {
                    facts.allowedRoleFound = true
                    facts.sawRelationshipRole = true
                    facts.sourceKinds.insert("focused_element")
                    if !alreadyCounted {
                        facts.increment(\.allowedRoleCount, cap: 8, saturationClass: .allowedRole)
                    }
                    if fullyEligible(focused, window, selector) {
                        facts.fullEligibilityFound = true
                        if !alreadyCounted {
                            facts.increment(\.fullEligibilityCount, cap: 8, saturationClass: .fullEligibility)
                        }
                    }
                }
            }
        }
    } else {
        incomplete = true
        facts.truncated = true
        facts.globalReadLimitHit = true
        facts.markIncomplete(.globalReadLimit)
    }
    facts.complete = !incomplete && !facts.truncated
    return facts
}

func semanticStaleOnlyDescendantPass(
    _ facts: SemanticTextDiscoveryFacts,
    requireExactlyOneStale: Bool = false
) -> Bool {
    let staleCount = facts.windowChildrenInvalidElementCount
    guard staleCount > 0,
          (!requireExactlyOneStale || staleCount == 1),
          !facts.childrenFailureOnWindowRoot,
          !facts.windowNodeBudgetTruncated,
          !facts.windowDepthTruncated,
          facts.windowChildrenGlobalFailureCount == 0,
          facts.windowChildrenProtocolFailureCount == 0,
          facts.windowChildrenCannotCompleteCount == 0,
          !facts.windowSawChildrenGeneric,
          facts.windowChildrenReadFailureCount == staleCount,
          facts.windowChildrenUnknownBranchCount == staleCount
    else { return false }
    return true
}

func semanticParentRefreshClassification(
    stale: SemanticStaleDescendant,
    refreshed: AXChildrenReadResult
) -> (referenceClass: String, branchComparison: String, succeeded: Bool) {
    guard !refreshed.scanIncomplete else {
        return ("unknown", "unknown", false)
    }
    guard !refreshed.children.isEmpty else {
        return ("branch_now_empty", "unknown", true)
    }
    let sameReference = refreshed.children.contains { CFEqual($0, stale.staleElement) }
    let referenceClass = sameReference
        ? "same_stale_reference_returned"
        : "stale_reference_absent_nonempty"
    guard refreshed.children.count == 1 else {
        return (referenceClass, "multiple", true)
    }
    let refreshedRole = axTextAttribute(
        refreshed.children[0], kAXRoleAttribute as CFString
    ) ?? ""
    let sameClass = SemanticStaleNodeClass.from(role: refreshedRole) == stale.nodeClass
    // A refreshed direct child necessarily remains at the native-only parent
    // depth plus one.  We intentionally emit only this closed comparison, not
    // a depth, role, child index, or AX identity.
    let sameDepth = stale.depth > 0
    let comparison = sameClass && sameDepth && stale.parentChildCount == 1
        ? "same_class_and_depth"
        : "different_class_or_depth"
    return (referenceClass, comparison, true)
}

func semanticSecondThirdStaleReferenceClass(
    second: SemanticTextDiscovery,
    third: SemanticTextDiscovery
) -> String {
    // This is a native-process-local comparison of exactly one stale
    // observation from each independent scan. It deliberately compares only
    // the parent and stale AX references with CFEqual; no identifiers, roles,
    // paths, indices, hashes, or other AX-derived values are produced.
    guard second.facts.windowChildrenInvalidElementCount == 1,
          third.facts.windowChildrenInvalidElementCount == 1,
          second.staleDescendants.count == 1,
          third.staleDescendants.count == 1
    else { return "not_comparable" }
    let secondStale = second.staleDescendants[0]
    let thirdStale = third.staleDescendants[0]
    let sameParent = CFEqual(secondStale.parent, thirdStale.parent)
    let sameReference = CFEqual(secondStale.staleElement, thirdStale.staleElement)
    switch (sameParent, sameReference) {
    case (true, true): return "same_parent_same_reference"
    case (true, false): return "same_parent_new_reference"
    case (false, true): return "new_parent_same_reference"
    case (false, false): return "new_parent_new_reference"
    }
}

func semanticTextDiscoveryWithStaleRecovery(
    args: [String: Any],
    window: ExactSemanticWindow,
    selector: SemanticTextSelector,
    frontmostBefore: pid_t,
    discover: ((ExactSemanticWindow, SemanticTextSelector) -> SemanticTextDiscovery)? = nil,
    recoveryDiscover: ((ExactSemanticWindow, SemanticTextSelector, SemanticAdditionalChildrenReadBudget) -> SemanticTextDiscovery)? = nil,
    parentRefresh: ((AXUIElement, SemanticAdditionalChildrenReadBudget) -> AXChildrenReadResult)? = nil,
    additionalReadBudgetLimit: Int = 64,
    rebind: ([String: Any]) -> ExactSemanticWindow? = exactSemanticWindow,
    frontmost: () -> pid_t = frontmostPid
) -> SemanticTextDiscovery {
    let additionalReadBudget = SemanticAdditionalChildrenReadBudget(
        limit: additionalReadBudgetLimit
    )
    func runPass(_ currentWindow: ExactSemanticWindow) -> SemanticTextDiscovery {
        if let recoveryDiscover {
            return recoveryDiscover(currentWindow, selector, additionalReadBudget)
        }
        if let discover {
            return discover(currentWindow, selector)
        }
        return semanticTextDiscoveryPass(
            window: currentWindow, selector: selector,
            additionalReadBudget: additionalReadBudget
        )
    }
    func applyBudget(_ facts: inout SemanticTextDiscoveryFacts) {
        facts.staleAdditionalAXReadCount = min(64, additionalReadBudget.consumed)
        facts.staleAdditionalReadBudgetExhausted = additionalReadBudget.exhausted
    }
    func stableRebind(_ facts: inout SemanticTextDiscoveryFacts) -> ExactSemanticWindow? {
        guard let rebound = rebind(args) else {
            facts.staleRecoveryOutcome = "exact_window_rebind_failed"
            return nil
        }
        facts.staleRecoveryWindowRebound = true
        let reboundFrontmost = frontmost()
        guard reboundFrontmost == frontmostBefore, rebound.pid != reboundFrontmost else {
            facts.staleRecoveryOutcome = "frontmost_changed"
            return nil
        }
        let sameTrustedBinding = rebound.pid == window.pid
            && rebound.windowId == window.windowId
            && CFEqual(rebound.element, window.element)
            && rectNearlyMatches(rebound.frame, window.frame)
        guard sameTrustedBinding else {
            facts.staleRecoveryOutcome = "exact_window_changed"
            return nil
        }
        facts.staleRecoveryWindowStable = true
        return rebound
    }
    func firstSecondFacts(
        _ source: SemanticTextDiscoveryFacts,
        first: SemanticTextDiscoveryFacts,
        second: SemanticTextDiscoveryFacts?
    ) -> SemanticTextDiscoveryFacts {
        var facts = source
        facts.staleRecoveryEligible = true
        facts.staleRecoveryAttempted = true
        facts.staleRecoveryWindowRebound = true
        facts.staleRecoveryWindowStable = true
        facts.discoveryPassCount = second == nil ? 1 : 2
        facts.staleRecoveryRestartCount = second == nil ? 0 : 1
        facts.firstPassStaleCount = min(64, first.windowChildrenInvalidElementCount)
        facts.firstPassUnknownBranchCount = min(64, first.windowChildrenUnknownBranchCount)
        facts.firstPassNodesVisitedCount = min(255, first.windowNodesVisitedCount)
        if let second {
            facts.secondPassStaleCount = min(64, second.windowChildrenInvalidElementCount)
            facts.secondPassUnknownBranchCount = min(64, second.windowChildrenUnknownBranchCount)
            facts.secondPassNodesVisitedCount = min(255, second.windowNodesVisitedCount)
            facts.secondPassFinalCandidateCount = min(8, second.finalCandidateCount)
        }
        return facts
    }

    let first = runPass(window)
    var firstFacts = first.facts
    firstFacts.discoveryPassCount = 1
    firstFacts.firstPassStaleCount = min(64, first.facts.windowChildrenInvalidElementCount)
    firstFacts.firstPassUnknownBranchCount = min(64, first.facts.windowChildrenUnknownBranchCount)
    firstFacts.firstPassNodesVisitedCount = min(255, first.facts.windowNodesVisitedCount)
    firstFacts.staleRecoveryFinalScanComplete = first.facts.windowScanComplete
    applyBudget(&firstFacts)
    // An invalid AX reference that independently proved this branch empty is
    // complete, not stale-recovery input. Only an unresolved invalid branch
    // needs rebinding/recovery and can suppress candidates.
    guard first.facts.windowChildrenInvalidElementCount > 0,
          first.facts.windowChildrenUnknownBranchCount > 0
    else {
        firstFacts.staleRecoveryOutcome = "not_needed"
        return SemanticTextDiscovery(
            eligibleCandidates: first.facts.windowScanComplete ? first.eligibleCandidates : [],
            facts: firstFacts, diagnosticProxySeeds: first.diagnosticProxySeeds
        )
    }
    let frontmostAfterFirst = frontmost()
    let frontmostStable = frontmostAfterFirst == frontmostBefore && window.pid != frontmostAfterFirst
    firstFacts.staleRecoveryEligible = semanticStaleOnlyDescendantPass(first.facts) && frontmostStable
    guard frontmostStable else {
        firstFacts.staleRecoveryOutcome = "frontmost_changed"
        firstFacts.staleRecoveryFinalScanComplete = false
        return SemanticTextDiscovery(eligibleCandidates: [], facts: firstFacts)
    }
    guard firstFacts.staleRecoveryEligible else {
        firstFacts.staleRecoveryOutcome = "recovery_not_eligible"
        firstFacts.staleRecoveryFinalScanComplete = false
        return SemanticTextDiscovery(eligibleCandidates: [], facts: firstFacts)
    }
    firstFacts.staleRecoveryAttempted = true
    guard let rebound = stableRebind(&firstFacts) else {
        firstFacts.staleRecoveryFinalScanComplete = false
        applyBudget(&firstFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: firstFacts)
    }

    let second = runPass(rebound)
    var finalFacts = firstSecondFacts(second.facts, first: first.facts, second: second.facts)
    let frontmostAfterSecond = frontmost()
    guard frontmostAfterSecond == frontmostBefore, rebound.pid != frontmostAfterSecond else {
        finalFacts.windowScanTruncated = true
        finalFacts.windowScanComplete = false
        finalFacts.staleRecoveryFinalScanComplete = false
        finalFacts.staleRecoveryOutcome = "frontmost_changed"
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: finalFacts)
    }
    finalFacts.staleRecoverySecondPassComplete = second.facts.windowScanComplete
    finalFacts.staleRecoveryFinalScanComplete = second.facts.windowScanComplete
    if second.facts.windowScanComplete {
        finalFacts.staleRecoverySucceeded = true
        finalFacts.staleRecoveryOutcome = "recovered_clean"
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(
            eligibleCandidates: second.eligibleCandidates,
            facts: finalFacts, diagnosticProxySeeds: second.diagnosticProxySeeds
        )
    }

    guard semanticStaleOnlyDescendantPass(second.facts, requireExactlyOneStale: true),
          second.staleDescendants.count == 1
    else {
        finalFacts.staleRecoveryOutcome = "parent_refresh_not_eligible"
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: finalFacts)
    }
    guard !additionalReadBudget.exhausted else {
        finalFacts.staleRecoveryOutcome = "parent_refresh_budget_exhausted"
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: finalFacts)
    }

    let stale = second.staleDescendants[0]
    finalFacts.staleParentRefreshAttempted = true
    finalFacts.staleParentRefreshCount = 1
    let parentRefresh = parentRefresh?(stale.parent, additionalReadBudget)
        ?? semanticChildren(
            stale.parent, additionalReadBudget: additionalReadBudget,
            countInitialReadAgainstAdditionalBudget: true
        )
    let refreshClassification = semanticParentRefreshClassification(
        stale: stale, refreshed: parentRefresh
    )
    finalFacts.staleParentRefreshReadCount = min(2, parentRefresh.readAttemptCount)
    finalFacts.staleReferenceRefreshClass = refreshClassification.referenceClass
    finalFacts.staleBranchComparison = refreshClassification.branchComparison
    finalFacts.staleParentRefreshSucceeded = refreshClassification.succeeded
    guard refreshClassification.succeeded else {
        finalFacts.staleRecoveryOutcome = additionalReadBudget.exhausted
            ? "parent_refresh_budget_exhausted"
            : "parent_refresh_failed"
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: finalFacts)
    }
    // The parent refresh is a classification-only read.  It never adds to a
    // BFS queue or candidate set.  A third, complete scan starts from a fresh
    // exact-window binding so no old/new generations are mixed.
    guard let thirdWindow = stableRebind(&finalFacts) else {
        finalFacts.staleRecoveryFinalScanComplete = false
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: finalFacts)
    }
    let third = runPass(thirdWindow)
    finalFacts = firstSecondFacts(third.facts, first: first.facts, second: second.facts)
    finalFacts.discoveryPassCount = 3
    finalFacts.staleRecoveryRestartCount = 2
    finalFacts.staleRecoverySecondPassComplete = false
    finalFacts.staleParentRefreshAttempted = true
    finalFacts.staleParentRefreshCount = 1
    finalFacts.staleParentRefreshSucceeded = true
    finalFacts.staleParentRefreshReadCount = min(2, parentRefresh.readAttemptCount)
    finalFacts.staleReferenceRefreshClass = refreshClassification.referenceClass
    finalFacts.staleBranchComparison = refreshClassification.branchComparison
    finalFacts.secondThirdStaleReferenceClass = semanticSecondThirdStaleReferenceClass(
        second: second, third: third
    )
    finalFacts.thirdPassStaleCount = min(64, third.facts.windowChildrenInvalidElementCount)
    finalFacts.thirdPassUnknownBranchCount = min(64, third.facts.windowChildrenUnknownBranchCount)
    finalFacts.thirdPassNodesVisitedCount = min(255, third.facts.windowNodesVisitedCount)
    finalFacts.thirdPassFinalCandidateCount = min(8, third.facts.finalCandidateCount)
    let frontmostAfterThird = frontmost()
    guard frontmostAfterThird == frontmostBefore, thirdWindow.pid != frontmostAfterThird else {
        finalFacts.windowScanTruncated = true
        finalFacts.windowScanComplete = false
        finalFacts.staleRecoveryFinalScanComplete = false
        finalFacts.staleRecoveryOutcome = "frontmost_changed"
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: finalFacts)
    }
    finalFacts.staleRecoveryFinalScanComplete = third.facts.windowScanComplete
    guard third.facts.windowScanComplete else {
        finalFacts.staleRecoveryOutcome = third.facts.windowChildrenInvalidElementCount > 0
            ? "final_pass_stale"
            : "final_pass_incomplete"
        applyBudget(&finalFacts)
        return SemanticTextDiscovery(eligibleCandidates: [], facts: finalFacts)
    }
    finalFacts.staleRecoverySucceeded = true
    finalFacts.staleRecoveryOutcome = "recovered_after_parent_refresh"
    applyBudget(&finalFacts)
    return SemanticTextDiscovery(
        eligibleCandidates: third.eligibleCandidates,
        facts: finalFacts, diagnosticProxySeeds: third.diagnosticProxySeeds
    )
}

func semanticWindowStillStable(_ binding: ExactSemanticWindow, frontmostBefore: pid_t) -> Bool {
    guard frontmostPid() == frontmostBefore,
          let rebound = exactSemanticWindow(args: [
            "pid": Int(binding.pid),
            "window_id": binding.windowId,
            "window_x": Double(binding.frame.origin.x),
            "window_y": Double(binding.frame.origin.y),
            "window_width": Double(binding.frame.width),
            "window_height": Double(binding.frame.height)
          ])
    else {
        return false
    }
    return CFEqual(binding.element, rebound.element)
}

func setSelectedTextRange(_ element: AXUIElement, range: CFRange) -> Bool {
    var mutableRange = range
    guard let value = AXValueCreate(.cfRange, &mutableRange) else { return false }
    return AXUIElementSetAttributeValue(
        element,
        kAXSelectedTextRangeAttribute as CFString,
        value
    ) == .success
}

func semanticRepeatedlyStaleBranch(_ facts: SemanticTextDiscoveryFacts) -> Bool {
    return facts.staleRecoveryOutcome == "final_pass_stale"
        && facts.staleReferenceRefreshClass == "same_stale_reference_returned"
        && facts.staleBranchComparison == "same_class_and_depth"
        && facts.secondThirdStaleReferenceClass == "same_parent_same_reference"
        && facts.discoveryPassCount == 3
        && facts.staleRecoveryRestartCount == 2
        && !facts.windowScanComplete
        && !facts.staleRecoveryFinalScanComplete
}

func semanticDiscoveryErrorCode(_ facts: SemanticTextDiscoveryFacts) -> String {
    if ["exact_window_rebind_failed", "exact_window_changed", "frontmost_changed"].contains(
        facts.staleRecoveryOutcome
    ) {
        return "TYPE_TARGET_DRIFTED"
    }
    // Preserve concrete AX failures before falling back to generic stale or
    // incomplete-discovery classifications.  `cannotComplete` deliberately
    // remains a traversal-completeness concern below.
    if facts.childrenAXErrorClasses.contains(.apiDisabled) {
        return "TYPE_ACCESSIBILITY_NOT_TRUSTED"
    }
    if facts.navigationOrderAXErrorClasses.contains(.apiDisabled) {
        return "TYPE_ACCESSIBILITY_NOT_TRUSTED"
    }
    if facts.childrenAXErrorClasses.contains(.notImplemented) {
        return "TYPE_ACCESSIBILITY_API_UNAVAILABLE"
    }
    if facts.navigationOrderAXErrorClasses.contains(.notImplemented) {
        return "TYPE_ACCESSIBILITY_API_UNAVAILABLE"
    }
    if !facts.childrenAXErrorClasses.isDisjoint(with: [.illegalArgument, .payloadTypeInvalid]) {
        return "TYPE_SEMANTIC_PROTOCOL_INVALID"
    }
    if facts.navigationOrderAXErrorClasses.contains(.illegalArgument)
        || !facts.navigationOrderFailureClasses.isDisjoint(with: [
            .payloadInvalid, .duplicate, .selfCycle, .parentMismatch
        ]) {
        return "TYPE_SEMANTIC_PROTOCOL_INVALID"
    }
    if facts.childrenFailureOnWindowRoot && facts.childrenInvalidElementCount > 0 {
        return "TYPE_TARGET_DRIFTED"
    }
    if semanticRepeatedlyStaleBranch(facts) {
        return "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE"
    }
    if [
        "parent_refresh_not_eligible", "parent_refresh_failed",
        "parent_refresh_budget_exhausted", "final_pass_stale", "final_pass_incomplete"
    ].contains(facts.staleRecoveryOutcome) {
        return "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    }
    let stage = facts.discoveryStage()
    switch stage {
    case "ready": return ""
    case "scan_incomplete": return "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    case "window_ownership_unverified": return "TYPE_SEMANTIC_WINDOW_OWNERSHIP_UNVERIFIED"
    case "frame_unavailable", "region_excluded": return "TYPE_SEMANTIC_COORDINATE_MISMATCH"
    case "disabled": return "TYPE_SEMANTIC_CONTROL_DISABLED"
    case "value_unreadable": return "TYPE_SEMANTIC_VALUE_UNREADABLE"
    case "not_settable": return "TYPE_SEMANTIC_CONTROL_NOT_SETTABLE"
    case "ambiguous": return "TYPE_SEMANTIC_CONTROL_AMBIGUOUS"
    default: return "TYPE_SEMANTIC_CONTROL_NOT_FOUND"
    }
}

func probeSemanticTextControl(args: [String: Any]) -> Never {
    let frontmostBefore = frontmostPid()
    // This is a preflight observation only.  It never asks macOS to prompt
    // for Accessibility permission and is included on both success and the
    // early permission-rejection response.
    let accessibilityTrustPreflight = AXIsProcessTrusted()
    let base: [String: Any] = [
        "action": "computer.probe_text_control", "platform": "Darwin", "executed": false,
        "probe_completed": false, "semantic_control_ready": false,
        "target_window_stable": false, "semantic_control_resolved": false,
        "semantic_control_role_allowed": false, "semantic_control_settable": false,
        "input_dispatched": false, "mutation_attempted": false,
        "driver": "mac_swift_host",
        "accessibility_trust_preflight": accessibilityTrustPreflight ? "granted" : "denied"
    ]
    func rejected(_ code: String, _ stage: String, _ additions: [String: Any] = [:]) -> Never {
        var result = base
        result["failure_stage"] = stage
        result["error_code"] = code
        for (key, value) in additions { result[key] = value }
        fail(code, "Background semantic text-control probe was rejected.", result)
    }
    guard accessibilityTrustPreflight else {
        rejected("TYPE_ACCESSIBILITY_NOT_TRUSTED", "accessibility_permission")
    }
    guard intValue(args["pid"]) > 0, intValue(args["window_id"]) > 0,
          doubleValue(args["window_width"]) > 0, doubleValue(args["window_height"]) > 0
    else { rejected("TYPE_EXACT_WINDOW_REQUIRED", "exact_window_binding") }
    guard let selector = semanticTextSelector(args: args) else {
        rejected("TYPE_SEMANTIC_SELECTOR_INVALID", "selector_validation")
    }
    let resolution = resolveExactSemanticWindow(args: args)
    guard let window = resolution.window else {
        rejected(resolution.errorCode, "exact_window_resolution", resolution.facts.payload())
    }
    guard !selector.requireBackground || window.pid != frontmostBefore else {
        rejected("TYPE_BACKGROUND_PRECONDITION_FAILED", "background_precondition", ["target_window_stable": true])
    }
    let discovery = semanticTextDiscoveryWithStaleRecovery(
        args: args, window: window, selector: selector, frontmostBefore: frontmostBefore,
        discover: {
            semanticTextDiscoveryPass(
                window: $0, selector: $1, includeBroadAppDiagnostic: false
            )
        },
        recoveryDiscover: {
            semanticTextDiscoveryPass(
                window: $0, selector: $1, includeBroadAppDiagnostic: false,
                additionalReadBudget: $2
            )
        }
    )
    // The alternate surface is diagnostic-only. In particular, a stale-only
    // authoritative branch is useful evidence for the probe, but alternate
    // exposure can never supply an actionable candidate or repair that branch.
    let exposure = (
        discovery.facts.windowScanComplete
            && discovery.facts.discoveryStage() == "role_absent"
    ) || semanticStaleOnlyDescendantPass(discovery.facts)
        ? semanticAlternateExposureProbe(
            window: window, selector: selector,
            proxySeeds: discovery.diagnosticProxySeeds
        )
        : nil
    let windowStable = semanticWindowStillStable(window, frontmostBefore: frontmostBefore)
    let stage = discovery.facts.discoveryStage()
    let ready = windowStable && discovery.facts.actionableScanComplete()
        && stage == "ready" && discovery.eligibleCandidates.count == 1
    var result = base
    for (key, value) in resolution.facts.payload() { result[key] = value }
    for (key, value) in discovery.facts.payload()
    where !key.hasPrefix("semantic_app_")
        && key != "semantic_other_window_pruned_count"
        && key != "semantic_app_diagnostic_counts_truncated" {
        result[key] = value
    }
    if let exposure {
        for (key, value) in exposure.payload() { result[key] = value }
    }
    result["executed"] = true
    result["probe_completed"] = true
    result["semantic_control_ready"] = ready
    result["target_window_stable"] = windowStable
    result["semantic_control_role_allowed"] = discovery.facts.roleMatchCount > 0
    result["semantic_control_settable"] = discovery.facts.finalCandidateCount > 0
    result["semantic_control_resolved"] = discovery.eligibleCandidates.count == 1
    result["failure_stage"] = ready ? NSNull() : "semantic_control_resolution"
    let code = windowStable ? semanticDiscoveryErrorCode(discovery.facts) : "TYPE_TARGET_DRIFTED"
    if !code.isEmpty { result["error_code"] = code }
    ok(result)
}

func setSemanticTextControl(args: [String: Any]) -> Never {
    let text = stringValue(args["text"])
    guard !text.isEmpty else { fail("TEXT_REQUIRED", "A non-empty replacement is required.") }
    let frontmostBefore = frontmostPid()
    let base: [String: Any] = [
        "action": "computer.set_text_control", "platform": "Darwin", "executed": false,
        "target_window_stable": false, "semantic_control_resolved": false,
        "semantic_control_role_allowed": false, "semantic_control_settable": false,
        "focus_attempted": false, "focus_succeeded": false, "focused_control_matches": false,
        "selection_verified": false, "input_dispatched": false,
        "value_readback_attempted": false, "value_readback_matched": false,
        "completion_verified": false, "input_strategy": "none", "driver": "mac_swift_host"
    ]
    func rejected(_ code: String, _ stage: String, _ additions: [String: Any] = [:]) -> Never {
        var result = base
        result["failure_stage"] = stage
        result["error_code"] = code
        for (key, value) in additions { result[key] = value }
        fail(code, "Verified background semantic text replacement was rejected.", result)
    }
    guard AXIsProcessTrusted() else { rejected("TYPE_ACCESSIBILITY_NOT_TRUSTED", "accessibility_permission") }
    guard intValue(args["pid"]) > 0, intValue(args["window_id"]) > 0,
          doubleValue(args["window_width"]) > 0, doubleValue(args["window_height"]) > 0
    else { rejected("TYPE_EXACT_WINDOW_REQUIRED", "exact_window_binding") }
    guard let selector = semanticTextSelector(args: args) else {
        rejected("TYPE_SEMANTIC_SELECTOR_INVALID", "selector_validation")
    }
    let resolution = resolveExactSemanticWindow(args: args)
    guard let window = resolution.window else {
        rejected(resolution.errorCode, "exact_window_resolution", resolution.facts.payload())
    }
    guard !selector.requireBackground || window.pid != frontmostBefore else {
        rejected("TYPE_BACKGROUND_PRECONDITION_FAILED", "background_precondition", ["target_window_stable": true])
    }
    let discovery = semanticTextDiscoveryWithStaleRecovery(
        args: args, window: window, selector: selector, frontmostBefore: frontmostBefore
    )
    var discoveryDiagnostics = discovery.facts.payload()
    for (key, value) in resolution.facts.payload() { discoveryDiagnostics[key] = value }
    discoveryDiagnostics["target_window_stable"] = true
    discoveryDiagnostics["semantic_control_role_allowed"] = discovery.facts.roleMatchCount > 0
    discoveryDiagnostics["semantic_control_settable"] = discovery.facts.valueSettableCount > 0
        || discovery.facts.selectedTextSettableCount > 0
    discoveryDiagnostics["semantic_control_resolved"] = discovery.facts.actionableScanComplete()
        && discovery.facts.discoveryStage() == "ready"
        && discovery.eligibleCandidates.count == 1
    // The one-shot mutation gate consumes the explicit actionable-scan fact;
    // a candidate observed before an unresolved descendant is never enough.
    guard discovery.facts.actionableScanComplete(),
          discovery.facts.discoveryStage() == "ready",
          discovery.eligibleCandidates.count == 1,
          let element = discovery.eligibleCandidates.first
    else {
        rejected(semanticDiscoveryErrorCode(discovery.facts), "semantic_control_resolution", discoveryDiagnostics)
    }
    let role = axTextAttribute(element, kAXRoleAttribute as CFString) ?? ""
    let valueSettable = axAttributeIsSettable(element, kAXValueAttribute as CFString)
    let selectedTextSettable = axAttributeIsSettable(element, kAXSelectedTextAttribute as CFString)
    guard selector.roles.contains(role), valueSettable || selectedTextSettable,
          let initialValue = axTextAttribute(element, kAXValueAttribute as CFString)
    else {
        rejected("TYPE_SEMANTIC_CONTROL_NOT_SETTABLE", "semantic_control_validation", [
            "target_window_stable": true, "semantic_control_resolved": true,
            "semantic_control_role_allowed": selector.roles.contains(role),
            "semantic_control_settable": valueSettable || selectedTextSettable
        ])
    }
    let wholeRange = CFRange(location: 0, length: Array(initialValue.utf16).count)
    var selectionVerified = false
    if selectedTextSettable && axAttributeIsSettable(element, kAXSelectedTextRangeAttribute as CFString) {
        let selectionSet = setSelectedTextRange(element, range: wholeRange)
        let observedRange = axRange(axAttribute(element, kAXSelectedTextRangeAttribute as CFString))
        selectionVerified = selectionSet
            && observedRange?.location == wholeRange.location
            && observedRange?.length == wholeRange.length
    }
    guard let expectation = textInsertionExpectation(
        currentValue: initialValue,
        selectedRange: selectionVerified ? wholeRange : CFRange(location: 0, length: Array(initialValue.utf16).count),
        text: text
    ) else {
        rejected("TYPE_SELECTION_INVALID", "selection_verification")
    }
    let stableValue = { () -> String? in
        guard semanticWindowStillStable(window, frontmostBefore: frontmostBefore) else { return nil }
        return axTextAttribute(element, kAXValueAttribute as CFString)
    }
    let direct = directTextInsertion(
        expectation: expectation,
        initialValue: initialValue,
        valueSettable: valueSettable,
        selectedTextSettable: selectedTextSettable && selectionVerified,
        setValue: { value in
            guard stableValue() == initialValue else { return false }
            return setAXTextAttribute(element, kAXValueAttribute as CFString, value)
        },
        setSelectedText: { value in
            guard stableValue() == initialValue else { return false }
            return setAXTextAttribute(element, kAXSelectedTextAttribute as CFString, value)
        },
        insertedText: text,
        value: stableValue
    )
    let common: [String: Any] = [
        "target_window_stable": semanticWindowStillStable(window, frontmostBefore: frontmostBefore),
        "semantic_control_resolved": true, "semantic_control_role_allowed": true,
        "semantic_control_settable": true, "selection_verified": selectionVerified,
        "value_readback_attempted": true, "focus_attempted": false,
        "focus_succeeded": false, "focused_control_matches": false
    ]
    var diagnosticCommon = common
    for (key, value) in discovery.facts.payload() { diagnosticCommon[key] = value }
    switch direct {
    case .verified(let strategy):
        var result = base
        for (key, value) in diagnosticCommon { result[key] = value }
        result["executed"] = true
        result["input_dispatched"] = true
        result["value_readback_matched"] = true
        result["completion_verified"] = true
        result["completion_check"] = "same_element_ax_value"
        result["input_strategy"] = strategy == "ax_value" ? "semantic_ax_value" : "semantic_ax_selected_text"
        result["failure_stage"] = NSNull()
        ok(result)
    case .unverified(let strategy, let observedValue):
        var additions = diagnosticCommon
        additions["input_dispatched"] = true
        additions["input_strategy"] = strategy == "ax_value" ? "semantic_ax_value" : "semantic_ax_selected_text"
        additions["mutation_observed"] = observedValue.map { $0 != initialValue } ?? false
        rejected("TYPE_COMPLETION_NOT_VERIFIED", "same_element_readback", additions)
    case .unavailable:
        rejected("TYPE_SEMANTIC_CONTROL_NOT_SETTABLE", "semantic_control_validation", diagnosticCommon)
    }
}

func axAttributeIsSettable(_ element: AXUIElement, _ attribute: CFString) -> Bool {
    var settable: DarwinBoolean = false
    return AXUIElementIsAttributeSettable(element, attribute, &settable) == .success && settable.boolValue
}

func setAXTextAttribute(_ element: AXUIElement, _ attribute: CFString, _ value: String) -> Bool {
    AXUIElementSetAttributeValue(element, attribute, value as CFTypeRef) == .success
}

func postUnicodeUnit(_ unit: String, targetPid: pid_t) -> Bool {
    let units = Array(unit.utf16)
    guard !units.isEmpty,
          let down = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)
    else {
        return false
    }
    units.withUnsafeBufferPointer { buffer in
        down.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
        up.keyboardSetUnicodeString(stringLength: buffer.count, unicodeString: buffer.baseAddress)
    }
    down.postToPid(targetPid)
    up.postToPid(targetPid)
    return true
}

func waitForExpectedText(
    _ expectation: TextInsertionExpectation,
    timeout: TimeInterval = 2.0,
    value: () -> String?,
    now: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
    pause: () -> Void = { usleep(10_000) }
) -> Bool {
    let deadline = now() + timeout
    while true {
        if let currentValue = value(), expectation.matches(currentValue) {
            return true
        }
        if now() >= deadline {
            return false
        }
        pause()
    }
}

func deliverPacedText(
    _ text: String,
    targetPid: pid_t,
    initialValue: String,
    selectedRange: CFRange,
    timeoutPerUnit: TimeInterval = 1.0,
    post: (pid_t, String) -> Bool,
    targetStability: () -> TextInputTargetStability,
    value: () -> String?,
    now: () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
    pause: () -> Void = { usleep(5_000) }
) -> PacedTextInsertionResult {
    var inserted = ""
    var dispatchedUnitCount = 0
    var targetPidStable = true
    var focusedElementStable = true

    func observeStability() -> Bool {
        let stability = targetStability()
        targetPidStable = targetPidStable && stability.targetPidStable
        focusedElementStable = focusedElementStable && stability.focusedElementStable
        return stability.isStable
    }

    // A Swift Character is one extended grapheme cluster. Each native event is
    // acknowledged through the focused AX value before the next event is posted,
    // so pacing follows target-app acceptance instead of a guessed delay.
    for unit in text.map(String.init) {
        guard observeStability() else {
            return PacedTextInsertionResult(
                dispatchedUnitCount: dispatchedUnitCount,
                completionVerified: false,
                failureCode: "TYPE_TARGET_DRIFTED",
                failureStage: "before_grapheme_dispatch",
                targetPidStable: targetPidStable,
                focusedElementStable: focusedElementStable
            )
        }
        inserted.append(unit)
        guard let expectation = textInsertionExpectation(
            currentValue: initialValue,
            selectedRange: selectedRange,
            text: inserted
        ) else {
            return PacedTextInsertionResult(
                dispatchedUnitCount: dispatchedUnitCount,
                completionVerified: false,
                failureCode: "TYPE_SELECTION_INVALID",
                failureStage: "build_expectation",
                targetPidStable: targetPidStable,
                focusedElementStable: focusedElementStable
            )
        }
        guard post(targetPid, unit) else {
            return PacedTextInsertionResult(
                dispatchedUnitCount: dispatchedUnitCount,
                completionVerified: false,
                failureCode: "TYPE_EVENT_UNAVAILABLE",
                failureStage: "grapheme_dispatch",
                targetPidStable: targetPidStable,
                focusedElementStable: focusedElementStable
            )
        }
        dispatchedUnitCount += 1
        let deadline = now() + timeoutPerUnit
        var acknowledged = false
        while true {
            guard observeStability() else {
                return PacedTextInsertionResult(
                    dispatchedUnitCount: dispatchedUnitCount,
                    completionVerified: false,
                    failureCode: "TYPE_TARGET_DRIFTED",
                    failureStage: "await_grapheme_acknowledgement",
                    targetPidStable: targetPidStable,
                    focusedElementStable: focusedElementStable
                )
            }
            if let currentValue = value(), expectation.matches(currentValue) {
                acknowledged = true
                break
            }
            if now() >= deadline { break }
            pause()
        }
        guard acknowledged else {
            return PacedTextInsertionResult(
                dispatchedUnitCount: dispatchedUnitCount,
                completionVerified: false,
                failureCode: "TYPE_COMPLETION_NOT_VERIFIED",
                failureStage: "await_grapheme_acknowledgement",
                targetPidStable: targetPidStable,
                focusedElementStable: focusedElementStable
            )
        }
    }

    return PacedTextInsertionResult(
        dispatchedUnitCount: dispatchedUnitCount,
        completionVerified: true,
        failureCode: nil,
        failureStage: nil,
        targetPidStable: targetPidStable,
        focusedElementStable: focusedElementStable
    )
}

func focusedTextTargetStability(pid: pid_t, element: AXUIElement) -> TextInputTargetStability {
    let pidStable = frontmostPid() == pid
    guard pidStable else {
        return TextInputTargetStability(targetPidStable: false, focusedElementStable: false)
    }
    let appElement = AXUIElementCreateApplication(pid)
    guard let focusedValue = axAttribute(appElement, kAXFocusedUIElementAttribute as CFString),
          let focusedElement = asAXUIElement(focusedValue)
    else {
        return TextInputTargetStability(targetPidStable: true, focusedElementStable: false)
    }
    return TextInputTargetStability(targetPidStable: true, focusedElementStable: CFEqual(element, focusedElement))
}

func focusedTextValue(pid: pid_t, element: AXUIElement) -> String? {
    guard frontmostPid() == pid else {
        return nil
    }
    let appElement = AXUIElementCreateApplication(pid)
    guard
        let focusedValue = axAttribute(appElement, kAXFocusedUIElementAttribute as CFString),
        let focusedElement = asAXUIElement(focusedValue),
        CFEqual(element, focusedElement)
    else {
        return nil
    }
    return axTextAttribute(element, kAXValueAttribute as CFString)
}

func typeText(args: [String: Any]) -> Never {
    let text = stringValue(args["text"])
    guard !text.isEmpty else {
        fail("TEXT_REQUIRED", "computer.type requires non-empty text.")
    }
    let explicitTargetPid = resolvedExplicitTextInputTargetPid(args: args)
    if hasExplicitTextInputTarget(args: args) {
        guard let explicitTargetPid,
              ensureResolvedTextInputTargetIsFrontmost(
                pid: explicitTargetPid,
                activate: activateExactTextInputTarget,
                frontmost: frontmostPid
              )
        else {
            fail("TYPE_VERIFICATION_UNAVAILABLE", "The explicit text-input target could not be brought to the foreground and verified before typing.", [
                "action": "computer.type",
                "platform": "Darwin",
                "executed": false,
                "input_dispatched": false,
                "input_strategy": "none",
                "completion_verified": false,
                "dispatched_units": 0,
                "target_pid_stable": false,
                "focused_element_stable": false,
                "failure_stage": "initial_target_rebind",
                "direct_ax_attempted": false,
                "mutation_observed": false,
                "driver": "mac_swift_host"
            ])
        }
    }
    guard AXIsProcessTrusted(), let initialState = focusedTextInputState(args: args, resolvedTargetPid: explicitTargetPid) else {
        fail("TYPE_VERIFICATION_UNAVAILABLE", "The focused text field could not be verified before typing.", [
            "action": "computer.type",
            "platform": "Darwin",
            "executed": false,
            "input_dispatched": false,
            "input_strategy": "none",
            "completion_verified": false,
            "dispatched_units": 0,
            "target_pid_stable": false,
            "focused_element_stable": false,
            "failure_stage": "initial_target_verification",
            "direct_ax_attempted": false,
            "mutation_observed": false,
            "driver": "mac_swift_host"
        ])
    }
    guard let expectation = textInsertionExpectation(
        currentValue: initialState.value,
        selectedRange: initialState.selectedRange,
        text: text
    ) else {
        fail("TYPE_SELECTION_INVALID", "The focused text selection could not be verified before typing.", [
            "action": "computer.type",
            "platform": "Darwin",
            "executed": false,
            "input_dispatched": false,
            "input_strategy": "none",
            "completion_verified": false,
            "dispatched_units": 0,
            "target_pid_stable": true,
            "focused_element_stable": true,
            "failure_stage": "selection_verification",
            "direct_ax_attempted": false,
            "mutation_observed": false,
            "driver": "mac_swift_host"
        ])
    }
    let verifiedValue = { focusedTextValue(pid: initialState.pid, element: initialState.element) }
    let direct = directTextInsertion(
        expectation: expectation,
        initialValue: initialState.value,
        valueSettable: axAttributeIsSettable(initialState.element, kAXValueAttribute as CFString),
        selectedTextSettable: axAttributeIsSettable(initialState.element, kAXSelectedTextAttribute as CFString),
        setValue: { value in
            guard verifiedValue() == initialState.value else { return false }
            return setAXTextAttribute(initialState.element, kAXValueAttribute as CFString, value)
        },
        setSelectedText: { value in
            guard verifiedValue() == initialState.value else { return false }
            return setAXTextAttribute(initialState.element, kAXSelectedTextAttribute as CFString, value)
        },
        insertedText: text,
        value: verifiedValue
    )
    let directWasAttempted: Bool
    if case .unavailable = direct {
        directWasAttempted = false
    } else {
        directWasAttempted = true
    }
    switch direct {
    case .verified(let strategy):
        ok([
            "action": "computer.type",
            "platform": "Darwin",
            "executed": true,
            "input_dispatched": true,
            "completion_verified": true,
            "completion_check": "focused_ax_value",
            "input_strategy": strategy,
            "dispatched_units": 0,
            "target_pid_stable": true,
            "focused_element_stable": true,
            "failure_stage": NSNull(),
            "direct_ax_attempted": true,
            "mutation_observed": true,
            "length": text.count,
            "driver": "mac_swift_host"
        ])
    case .unverified(let strategy, let observedValue):
        let stability = focusedTextTargetStability(pid: initialState.pid, element: initialState.element)
        let zeroMutation = stability.isStable && observedValue == initialState.value
        guard zeroMutation else {
            fail("TYPE_COMPLETION_NOT_VERIFIED", "Direct native text insertion partially mutated the field or its target drifted; fallback was rejected.", [
            "action": "computer.type",
            "platform": "Darwin",
            "executed": false,
            "input_dispatched": true,
            "direct_value_changed": observedValue.map { $0 != initialState.value } ?? false,
            "completion_verified": false,
            "input_strategy": strategy,
            "dispatched_units": 0,
            "target_pid_stable": stability.targetPidStable,
            "focused_element_stable": stability.focusedElementStable,
            "failure_stage": "partial_mutation_rejection",
            "direct_ax_attempted": true,
            "mutation_observed": observedValue.map { $0 != initialState.value } ?? false,
            "fallback_reason": "partial_mutation_rejection",
            "length": text.count,
            "driver": "mac_swift_host"
            ])
        }
    case .unavailable:
        break
    }

    let delivery = deliverPacedText(
        text,
        targetPid: initialState.pid,
        initialValue: initialState.value,
        selectedRange: initialState.selectedRange,
        post: { pid, unit in postUnicodeUnit(unit, targetPid: pid) },
        targetStability: { focusedTextTargetStability(pid: initialState.pid, element: initialState.element) },
        value: verifiedValue
    )
    guard delivery.completionVerified else {
        let code = delivery.failureCode ?? "TYPE_COMPLETION_NOT_VERIFIED"
        let message = code == "TYPE_EVENT_UNAVAILABLE"
            ? "The native text event could not be created."
            : "Native text input stopped because the focused field did not acknowledge complete input."
        fail(code, message, [
            "action": "computer.type",
            "platform": "Darwin",
            "executed": false,
            "input_dispatched": delivery.dispatchedUnitCount > 0,
            "completion_verified": false,
            "dispatched_units": delivery.dispatchedUnitCount,
            "input_strategy": "post_to_pid",
            "target_pid_stable": delivery.targetPidStable,
            "focused_element_stable": delivery.focusedElementStable,
            "failure_stage": delivery.failureStage ?? "unknown",
            "direct_ax_attempted": directWasAttempted,
            "direct_no_mutation_fallback": directWasAttempted,
            "fallback_reason": directWasAttempted ? "direct_no_mutation_fallback" : NSNull(),
            "mutation_observed": delivery.dispatchedUnitCount > 0,
            "length": text.count,
            "driver": "mac_swift_host"
        ])
    }
    ok([
        "action": "computer.type",
        "platform": "Darwin",
        "executed": true,
        "input_dispatched": true,
        "completion_verified": true,
        "completion_check": "focused_ax_value",
        "dispatched_units": delivery.dispatchedUnitCount,
        "input_strategy": "post_to_pid",
        "target_pid_stable": delivery.targetPidStable,
        "focused_element_stable": delivery.focusedElementStable,
        "failure_stage": NSNull(),
        "direct_ax_attempted": directWasAttempted,
        "direct_no_mutation_fallback": directWasAttempted,
        "fallback_reason": directWasAttempted ? "direct_no_mutation_fallback" : NSNull(),
        "mutation_observed": delivery.dispatchedUnitCount > 0,
        "length": text.count,
        "driver": "mac_swift_host"
    ])
}

func typingCompletionSelfTest() -> Never {
    let atlasAliases = inventoryIdentitySet(["ChatGPT Atlas", "Atlas"])
    let atlasBundles = inventoryIdentitySet(["com.openai.atlas"])
    guard inventoryIdentityMatches(atlasAliases, "ChatGPT Atlas"),
          inventoryIdentityMatches(atlasAliases, "atlas"),
          !inventoryIdentityMatches(atlasAliases, "ChatGPT"),
          !inventoryIdentityMatches(atlasAliases, "Codex"),
          inventoryIdentityMatches(atlasBundles, "com.openai.atlas"),
          !inventoryIdentityMatches(atlasBundles, "com.openai.chat"),
          selectedWindowIdentityDiagnosticContract == "rumi.mac.selected_window_identity.v1"
    else {
        fail("SELF_TEST_FAILED", "Window inventory identity matching failed.")
    }
    guard targetVisibilityClassifierSelfTest() else {
        fail("SELF_TEST_FAILED", "Window visibility topology diagnostics validation failed.")
    }
    guard
        let replacement = textInsertionExpectation(
            currentValue: "search google now",
            selectedRange: CFRange(location: 7, length: 6),
            text: "youtube"
        ),
        replacement.finalValue == "search youtube now",
        let unicodeReplacement = textInsertionExpectation(
            currentValue: "A👩‍💻Z",
            selectedRange: CFRange(location: 1, length: 5),
            text: "e\u{301}🌟"
        ),
        unicodeReplacement.finalValue == "Ae\u{301}🌟Z",
        textInsertionExpectation(
            currentValue: "A😀Z",
            selectedRange: CFRange(location: 2, length: 0),
            text: "x"
        ) == nil
    else {
        fail("SELF_TEST_FAILED", "UTF-16 text replacement was not exact.")
    }

    var visibleValue = "search google now"
    var assignedValue: String?
    var selectedTextWrites = 0
    let direct = directTextInsertion(
        expectation: replacement,
        initialValue: visibleValue,
        valueSettable: true,
        selectedTextSettable: true,
        setValue: { value in
            assignedValue = value
            visibleValue = value
            return true
        },
        setSelectedText: { _ in
            selectedTextWrites += 1
            visibleValue = replacement.finalValue
            return true
        },
        insertedText: "youtube",
        value: { visibleValue }
    )
    guard case .verified(let directStrategy) = direct,
          directStrategy == "selected_text",
          assignedValue == nil,
          selectedTextWrites == 1
    else {
        fail("SELF_TEST_FAILED", "AXSelectedText was not preferred and verified.")
    }

    visibleValue = "search google now"
    let valueDirect = directTextInsertion(
        expectation: replacement,
        initialValue: visibleValue,
        valueSettable: true,
        selectedTextSettable: false,
        setValue: { value in visibleValue = value; return true },
        setSelectedText: { _ in return false },
        insertedText: "youtube",
        value: { visibleValue }
    )
    guard case .verified(let valueStrategy) = valueDirect,
          valueStrategy == "ax_value",
          visibleValue == "search youtube now"
    else {
        fail("SELF_TEST_FAILED", "Whole AXValue replacement was not exact.")
    }

    visibleValue = "A👩‍💻Z"
    var valueWrites = 0
    let selectedDirect = directTextInsertion(
        expectation: unicodeReplacement,
        initialValue: visibleValue,
        valueSettable: false,
        selectedTextSettable: true,
        setValue: { _ in valueWrites += 1; return true },
        setSelectedText: { selectedText in
            guard selectedText == "e\u{301}🌟" else { return false }
            visibleValue = unicodeReplacement.finalValue
            return true
        },
        insertedText: "e\u{301}🌟",
        value: { visibleValue }
    )
    guard case .verified(let selectedStrategy) = selectedDirect,
          selectedStrategy == "selected_text",
          valueWrites == 0,
          visibleValue == "Ae\u{301}🌟Z"
    else {
        fail("SELF_TEST_FAILED", "AXSelectedText replacement was not Unicode-exact.")
    }

    visibleValue = "google"
    var directWrites = 0
    var fallbackWrites = 0
    var directClock: TimeInterval = 0
    let failedDirect = directTextInsertion(
        expectation: textInsertionExpectation(
            currentValue: "google",
            selectedRange: CFRange(location: 0, length: 6),
            text: "youtube"
        )!,
        initialValue: visibleValue,
        valueSettable: true,
        selectedTextSettable: false,
        setValue: { _ in
            directWrites += 1
            visibleValue = "yo"
            return true
        },
        setSelectedText: { _ in fallbackWrites += 1; return true },
        insertedText: "youtube",
        value: { visibleValue },
        verificationTimeout: 0.2,
        now: { directClock },
        pause: { directClock += 0.1 }
    )
    guard case .unverified(_, let observedValue) = failedDirect,
          observedValue == "yo",
          directWrites == 1,
          fallbackWrites == 0,
          visibleValue == "yo"
    else {
        fail("SELF_TEST_FAILED", "A partial direct mutation was retried or accepted as complete.")
    }

    visibleValue = "google"
    directClock = 0
    let zeroMutationDirect = directTextInsertion(
        expectation: textInsertionExpectation(
            currentValue: "google",
            selectedRange: CFRange(location: 0, length: 6),
            text: "youtube"
        )!,
        initialValue: visibleValue,
        valueSettable: false,
        selectedTextSettable: true,
        setValue: { _ in return false },
        setSelectedText: { _ in return false },
        insertedText: "youtube",
        value: { visibleValue },
        verificationTimeout: 0.2,
        now: { directClock },
        pause: { directClock += 0.1 }
    )
    guard case .unverified(_, let zeroMutationValue) = zeroMutationDirect,
          zeroMutationValue == "google"
    else {
        fail("SELF_TEST_FAILED", "A zero-mutation direct miss was not identified for fallback.")
    }

    var pendingValue: String?
    var dispatched = [String]()
    var acknowledgedUnitCount = 0
    var paceViolation = false
    var clock: TimeInterval = 0
    visibleValue = "google"
    let completed = deliverPacedText(
        "youtube",
        targetPid: 4242,
        initialValue: "google",
        selectedRange: CFRange(location: 0, length: 6),
        timeoutPerUnit: 1.0,
        post: { pid, unit in
            guard pid == 4242 else { return false }
            if dispatched.count != acknowledgedUnitCount {
                paceViolation = true
            }
            dispatched.append(unit)
            pendingValue = dispatched.joined()
            return true
        },
        targetStability: { TextInputTargetStability(targetPidStable: true, focusedElementStable: true) },
        value: { visibleValue },
        now: { clock },
        pause: {
            if let nextValue = pendingValue {
                visibleValue = nextValue
                acknowledgedUnitCount += 1
                pendingValue = nil
            }
            clock += 0.1
        }
    )
    guard
        completed.completionVerified,
        completed.dispatchedUnitCount == 7,
        visibleValue == "youtube",
        acknowledgedUnitCount == 7,
        !paceViolation
    else {
        fail("SELF_TEST_FAILED", "Typing was not paced by native value acknowledgements.")
    }

    visibleValue = "google"
    pendingValue = nil
    dispatched = []
    acknowledgedUnitCount = 0
    clock = 0
    let partial = deliverPacedText(
        "youtube",
        targetPid: 4242,
        initialValue: "google",
        selectedRange: CFRange(location: 0, length: 6),
        timeoutPerUnit: 0.2,
        post: { pid, unit in
            guard pid == 4242 else { return false }
            dispatched.append(unit)
            if dispatched.count <= 2 {
                pendingValue = dispatched.joined()
            }
            return true
        },
        targetStability: { TextInputTargetStability(targetPidStable: true, focusedElementStable: true) },
        value: { visibleValue },
        now: { clock },
        pause: {
            if let nextValue = pendingValue {
                visibleValue = nextValue
                pendingValue = nil
            }
            clock += 0.1
        }
    )
    guard
        !partial.completionVerified,
        partial.dispatchedUnitCount == 3,
        partial.failureCode == "TYPE_COMPLETION_NOT_VERIFIED",
        visibleValue == "yo"
    else {
        fail("SELF_TEST_FAILED", "Partial text was accepted as complete.")
    }

    visibleValue = "google"
    dispatched = []
    var stabilityChecks = 0
    let drifted = deliverPacedText(
        "yo",
        targetPid: 4242,
        initialValue: "google",
        selectedRange: CFRange(location: 0, length: 6),
        post: { pid, unit in
            guard pid == 4242 else { return false }
            dispatched.append(unit)
            return true
        },
        targetStability: {
            stabilityChecks += 1
            return TextInputTargetStability(
                targetPidStable: true,
                focusedElementStable: stabilityChecks < 2
            )
        },
        value: { visibleValue }
    )
    guard !drifted.completionVerified,
          drifted.failureCode == "TYPE_TARGET_DRIFTED",
          drifted.failureStage == "await_grapheme_acknowledgement",
          drifted.targetPidStable,
          !drifted.focusedElementStable,
          dispatched == ["y"]
    else {
        fail("SELF_TEST_FAILED", "Target drift was not rejected before further dispatch.")
    }

    dispatched = []
    let pidDrifted = deliverPacedText(
        "yo",
        targetPid: 4242,
        initialValue: "google",
        selectedRange: CFRange(location: 0, length: 6),
        post: { _, unit in dispatched.append(unit); return true },
        targetStability: { TextInputTargetStability(targetPidStable: false, focusedElementStable: true) },
        value: { "google" }
    )
    guard !pidDrifted.completionVerified,
          pidDrifted.failureCode == "TYPE_TARGET_DRIFTED",
          !pidDrifted.targetPidStable,
          dispatched.isEmpty
    else {
        fail("SELF_TEST_FAILED", "PID drift was not rejected before dispatch.")
    }

    var rebindClock: TimeInterval = 0
    var observedFrontmostPid: pid_t = 111
    var activatedPids = [pid_t]()
    let rebound = ensureResolvedTextInputTargetIsFrontmost(
        pid: 4242,
        activate: { pid in
            activatedPids.append(pid)
            return true
        },
        frontmost: { observedFrontmostPid },
        timeout: 0.2,
        now: { rebindClock },
        pause: {
            observedFrontmostPid = 4242
            rebindClock += 0.1
        }
    )
    guard rebound, activatedPids == [4242], observedFrontmostPid == 4242 else {
        fail("SELF_TEST_FAILED", "An explicit text target was not rebound to its exact PID.")
    }

    rebindClock = 0
    observedFrontmostPid = 111
    activatedPids = []
    let rejectedRebind = ensureResolvedTextInputTargetIsFrontmost(
        pid: 4242,
        activate: { pid in
            activatedPids.append(pid)
            return false
        },
        frontmost: { observedFrontmostPid },
        timeout: 0.2,
        now: { rebindClock },
        pause: { rebindClock += 0.1 }
    )
    guard !rejectedRebind, activatedPids == [4242], observedFrontmostPid == 111 else {
        fail("SELF_TEST_FAILED", "A failed explicit target rebind was accepted.")
    }
    let selector = semanticTextSelector(args: ["selector": [
        "roles": ["AXTextField", "AXComboBox", "AXTextArea"],
        "forbidden_ancestor_roles": ["AXWebArea"],
        "relative_region": ["min_x": 0.08, "max_x": 0.94, "min_y": 0.0, "max_y": 0.22],
        "require_enabled": true, "require_settable": true,
        "preference": "widest", "require_background": true
    ]])
    guard selector?.roles == Set(["AXTextField", "AXComboBox", "AXTextArea"]),
          selector?.forbiddenAncestorRoles == Set(["AXWebArea"]),
          selector?.requireBackground == true,
          semanticTextSelector(args: ["selector": ["roles": []]]) == nil,
          rectNearlyMatches(
            CGRect(x: 100, y: 50, width: 1200, height: 800),
            CGRect(x: 102, y: 49, width: 1198, height: 802)
          ),
          !rectNearlyMatches(
            CGRect(x: 100, y: 50, width: 1200, height: 800),
            CGRect(x: 150, y: 50, width: 1200, height: 800)
          )
    else {
        fail("SELF_TEST_FAILED", "Semantic selector or exact-window geometry validation failed.")
    }
    func syntheticExactResolution(
        outcome: String, code: String, window: ExactSemanticWindow? = nil,
        axOutcome: String = "success"
    ) -> ExactWindowResolution {
        var facts = ExactWindowResolutionFacts()
        facts.inputValid = true
        facts.runningAppPresent = true
        facts.outcome = outcome
        facts.axWindowsOutcome = axOutcome
        facts.resolved = window != nil
        facts.stage = window == nil ? "ax_window_match" : "ready"
        return ExactWindowResolution(window: window, facts: facts, errorCode: code)
    }
    let exactTestElement = AXUIElementCreateSystemWide()
    let exactTestWindow = ExactSemanticWindow(
        pid: 4242, windowId: 88, frame: CGRect(x: 10, y: 20, width: 800, height: 600),
        element: exactTestElement
    )
    var resolutionCalls = 0
    var resolutionPauses = 0
    let recoveredExact = resolveExactSemanticWindow(
        args: ["pid": 4242],
        resolveOnce: { _ in
            resolutionCalls += 1
            return resolutionCalls == 1
                ? syntheticExactResolution(
                    outcome: "quartz_record_missing",
                    code: "TYPE_EXACT_WINDOW_QUARTZ_RECORD_NOT_FOUND"
                )
                : syntheticExactResolution(outcome: "ready", code: "", window: exactTestWindow)
        },
        frontmost: { 111 }, retryPause: { resolutionPauses += 1 }
    )
    var nonRetryCalls = 0
    let driftedExact = resolveExactSemanticWindow(
        args: ["pid": 4242],
        resolveOnce: { _ in
            nonRetryCalls += 1
            return syntheticExactResolution(
                outcome: "quartz_frame_mismatch", code: "TYPE_EXACT_WINDOW_FRAME_MISMATCH"
            )
        },
        frontmost: { 111 }, retryPause: { resolutionPauses += 100 }
    )
    var persistentCalls = 0
    let persistentExact = resolveExactSemanticWindow(
        args: ["pid": 4242],
        resolveOnce: { _ in
            persistentCalls += 1
            return syntheticExactResolution(
                outcome: "ax_windows_unavailable",
                code: "TYPE_EXACT_WINDOW_AX_WINDOWS_UNAVAILABLE",
                axOutcome: "cannot_complete"
            )
        },
        frontmost: { 111 }, retryPause: {}
    )
    let recoveredPayload = recoveredExact.facts.payload()
    guard recoveredExact.window != nil,
          recoveredExact.facts.outcome == "recovered",
          recoveredExact.facts.attemptCount == 2,
          recoveredExact.facts.retryAttempted,
          recoveredExact.facts.retryRecovered,
          resolutionCalls == 2, resolutionPauses == 1,
          driftedExact.errorCode == "TYPE_EXACT_WINDOW_FRAME_MISMATCH",
          !driftedExact.facts.retryAttempted, nonRetryCalls == 1,
          persistentExact.errorCode == "TYPE_EXACT_WINDOW_AX_WINDOWS_UNAVAILABLE",
          persistentExact.facts.attemptCount == 2, persistentCalls == 2,
          recoveredPayload["exact_resolution_attempt_count"] as? Int == 2,
          Set(recoveredPayload.keys).isDisjoint(with: ["pid", "window_id", "x", "y", "width", "height"])
    else {
        fail("SELF_TEST_FAILED", "Exact-window closed resolution or bounded retry validation failed.")
    }
    func stagedDiscovery(_ update: (inout SemanticTextDiscoveryFacts) -> Void) -> String {
        var facts = SemanticTextDiscoveryFacts()
        update(&facts)
        return facts.discoveryStage()
    }
    guard
        stagedDiscovery({ _ in }) == "no_nodes",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.windowScanTruncated = true
        }) == "scan_incomplete",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.windowDepthTruncated = true
        }) == "scan_incomplete",
        stagedDiscovery({ $0.nodesVisitedCount = 1 }) == "role_absent",
        stagedDiscovery({ $0.nodesVisitedCount = 1; $0.roleMatchCount = 1 }) == "window_ownership_unverified",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 1; $0.windowOwnedCount = 1
        }) == "web_content_excluded",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 1; $0.windowOwnedCount = 1
            $0.nonWebContentCount = 1
        }) == "frame_unavailable",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 1; $0.windowOwnedCount = 1
            $0.nonWebContentCount = 1; $0.frameValidCount = 1
        }) == "region_excluded",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 1; $0.windowOwnedCount = 1
            $0.nonWebContentCount = 1; $0.frameValidCount = 1; $0.regionMatchCount = 1
        }) == "disabled",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 1; $0.windowOwnedCount = 1
            $0.nonWebContentCount = 1; $0.frameValidCount = 1; $0.regionMatchCount = 1
            $0.enabledCount = 1
        }) == "value_unreadable",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 1; $0.windowOwnedCount = 1
            $0.nonWebContentCount = 1; $0.frameValidCount = 1; $0.regionMatchCount = 1
            $0.enabledCount = 1; $0.valuePresentCount = 1; $0.valueReadableCount = 1
        }) == "not_settable",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 1; $0.windowOwnedCount = 1
            $0.nonWebContentCount = 1; $0.frameValidCount = 1; $0.regionMatchCount = 1
            $0.enabledCount = 1; $0.valuePresentCount = 1; $0.valueReadableCount = 1
            $0.finalCandidateCount = 1
        }) == "ready",
        stagedDiscovery({
            $0.nodesVisitedCount = 1; $0.roleMatchCount = 2; $0.windowOwnedCount = 2
            $0.nonWebContentCount = 2; $0.frameValidCount = 2; $0.regionMatchCount = 2
            $0.enabledCount = 2; $0.valuePresentCount = 2; $0.valueReadableCount = 2
            $0.finalCandidateCount = 2
        }) == "ambiguous"
    else {
        fail("SELF_TEST_FAILED", "Semantic staged discovery classification failed.")
    }
    let windowFrame = CGRect(x: 100, y: 200, width: 1000, height: 800)
    let top = relativeCenter(
        candidateFrame: CGRect(x: 200, y: 220, width: 700, height: 60), windowFrame: windowFrame,
        minX: 0, maxX: 1, minY: 0, maxY: 0.2
    )
    let bottom = relativeCenter(
        candidateFrame: CGRect(x: 200, y: 900, width: 700, height: 60), windowFrame: windowFrame,
        minX: 0, maxX: 1, minY: 0.8, maxY: 1
    )
    let negativeOrigin = relativeCenter(
        candidateFrame: CGRect(x: -1800, y: 20, width: 800, height: 50),
        windowFrame: CGRect(x: -1920, y: 0, width: 1200, height: 900),
        minX: 0, maxX: 1, minY: 0, maxY: 0.2
    )
    let offsetDisplay = relativeCenter(
        candidateFrame: CGRect(x: 2200, y: 350, width: 900, height: 50),
        windowFrame: CGRect(x: 2048, y: 300, width: 1400, height: 900),
        minX: 0, maxX: 1, minY: 0, maxY: 0.2
    )
    let partialOutside = relativeCenter(
        candidateFrame: CGRect(x: 50, y: 220, width: 200, height: 50), windowFrame: windowFrame,
        minX: 0, maxX: 1, minY: 0, maxY: 1
    )
    let inverted = relativeCenter(
        candidateFrame: CGRect(x: 200, y: -900, width: 700, height: 60), windowFrame: windowFrame,
        minX: 0, maxX: 1, minY: 0, maxY: 1
    )
    let unavailable = relativeCenter(
        candidateFrame: CGRect.zero, windowFrame: windowFrame,
        minX: 0, maxX: 1, minY: 0, maxY: 1
    )
    let geometrySelector = SemanticTextSelector(
        roles: ["AXTextField", "AXComboBox", "AXTextArea"], forbiddenAncestorRoles: [],
        minX: 0.08, maxX: 0.94, minY: 0, maxY: 0.22,
        requireEnabled: true, requireSettable: true, requireBackground: true, preference: "widest"
    )
    var geometryFacts = SemanticTextDiscoveryFacts()
    let upperTextArea = CGRect(x: 200, y: 400, width: 700, height: 56)
    let tallWrapper = CGRect(x: 120, y: 350, width: 950, height: 400)
    let outsideCandidate = CGRect(x: 0, y: 300, width: 300, height: 50)
    geometryFacts.observeAllowedRoleGeometry(
        role: "AXTextArea", candidateFrame: upperTextArea, windowFrame: windowFrame,
        selector: geometrySelector,
        relative: relativeCenter(candidateFrame: upperTextArea, windowFrame: windowFrame,
                                 minX: 0.08, maxX: 0.94, minY: 0, maxY: 0.22)
    )
    geometryFacts.observeAllowedRoleGeometry(
        role: "AXTextField", candidateFrame: tallWrapper, windowFrame: windowFrame,
        selector: geometrySelector,
        relative: relativeCenter(candidateFrame: tallWrapper, windowFrame: windowFrame,
                                 minX: 0.08, maxX: 0.94, minY: 0, maxY: 0.22)
    )
    geometryFacts.observeAllowedRoleGeometry(
        role: "AXComboBox", candidateFrame: outsideCandidate, windowFrame: windowFrame,
        selector: geometrySelector,
        relative: relativeCenter(candidateFrame: outsideCandidate, windowFrame: windowFrame,
                                 minX: 0.08, maxX: 0.94, minY: 0, maxY: 0.22)
    )
    var geometryCapFacts = SemanticTextDiscoveryFacts()
    for _ in 0..<9 {
        geometryCapFacts.observeAllowedRoleGeometry(
            role: "AXTextArea", candidateFrame: upperTextArea, windowFrame: windowFrame,
            selector: geometrySelector,
            relative: relativeCenter(candidateFrame: upperTextArea, windowFrame: windowFrame,
                                     minX: 0.08, maxX: 0.94, minY: 0, maxY: 0.22)
        )
    }
    var cappedFacts = SemanticTextDiscoveryFacts()
    for _ in 0..<70 { cappedFacts.increment(\.roleMatchCount) }
    for _ in 0..<260 { cappedFacts.increment(\.nodesVisitedCount, cap: 255) }
    let safePayloadKeys = Set(cappedFacts.payload().keys)
    guard top.relativeRegionMatched, bottom.relativeRegionMatched,
          negativeOrigin.relativeRegionMatched, offsetDisplay.relativeRegionMatched,
          partialOutside.childCenterInsideWindow, !partialOutside.relativeRegionEvaluable,
          !partialOutside.relativeRegionMatched, !inverted.childCenterInsideWindow,
          !unavailable.childFrameValid,
          cappedFacts.roleMatchCount == 64, cappedFacts.nodesVisitedCount == 255,
          cappedFacts.countsTruncated,
          geometryFacts.allowedAXTextAreaCount == 1,
          geometryFacts.allowedAXTextFieldCount == 1,
          geometryFacts.allowedAXComboBoxCount == 1,
          geometryFacts.allowedFrameInsideWindowCount == 2,
          geometryFacts.allowedRegionXMatchCount == 2,
          geometryFacts.allowedRegionYMatchCount == 0,
          geometryFacts.allowedRegionMissAxes.contains("y"),
          geometryFacts.allowedCenterYBands.contains("upper_22_35"),
          geometryFacts.allowedWidthBands.contains("wide_40_80"),
          geometryFacts.allowedHeightBands.contains("shallow_0_15"),
          geometryFacts.allowedWidthBands.contains("near_full_80_100"),
          geometryFacts.allowedHeightBands.contains("tall_40_100"),
          geometryFacts.allowedRegionMissAxes.contains("outside_window"),
          geometryFacts.allowedRoleClass() == "multiple",
          geometryFacts.allowedRegionMissAxis() == "multiple",
          geometryFacts.allowedCenterYBand() == "multiple",
          geometryFacts.allowedWidthBand() == "multiple",
          geometryFacts.allowedHeightBand() == "multiple",
          geometryCapFacts.allowedAXTextAreaCount == 8,
          geometryCapFacts.allowedFrameInsideWindowCount == 8,
          geometryCapFacts.countsTruncated,
          safePayloadKeys.isDisjoint(with: ["x", "y", "width", "height", "pid", "window_id"])
    else {
        fail("SELF_TEST_FAILED", "Semantic coordinate or bounded diagnostics validation failed.")
    }
    let dummyElement = AXUIElementCreateSystemWide()
    let fanoutElementRead = semanticElementListAttribute(
        dummyElement, attribute: "AXDiagnostic", maximumElements: 8,
        read: { _, _ in (.success, [Any](repeating: dummyElement, count: 9)) },
        retryPause: {}
    )
    let mixedElementRead = semanticElementListAttribute(
        dummyElement, attribute: "AXDiagnostic",
        read: { _, _ in (.success, [dummyElement, "invalid"] as [Any]) },
        retryPause: {}
    )
    let invalidElementRead = semanticElementListAttribute(
        dummyElement, attribute: "AXDiagnostic",
        read: { _, _ in (.success, "invalid") }, retryPause: {}
    )
    let missingElementRead = semanticElementListAttribute(
        dummyElement, attribute: "AXDiagnostic",
        read: { _, _ in (.success, nil) }, retryPause: {}
    )
    let emptyElementRead = semanticElementListAttribute(
        dummyElement, attribute: "AXDiagnostic",
        read: { _, _ in (.success, [] as [Any]) }, retryPause: {}
    )
    let unsupportedElementRead = semanticElementListAttribute(
        dummyElement, attribute: "AXDiagnostic",
        read: { _, _ in (.attributeUnsupported, nil) }, retryPause: {}
    )
    guard fanoutElementRead.outcome == .fanoutTruncated,
          fanoutElementRead.cardinalityClass == .overCap,
          !fanoutElementRead.failed, fanoutElementRead.truncated,
          fanoutElementRead.elements.count == 8,
          mixedElementRead.outcome == .payloadMixed, mixedElementRead.failed,
          invalidElementRead.outcome == .payloadInvalid, invalidElementRead.failed,
          missingElementRead.outcome == .payloadMissing, missingElementRead.failed,
          emptyElementRead.outcome == .empty, emptyElementRead.complete,
          emptyElementRead.cardinalityClass == .zero,
          unsupportedElementRead.outcome == .empty, unsupportedElementRead.complete
    else {
        fail("SELF_TEST_FAILED", "Semantic closed element-list read outcomes failed.")
    }
    let unknownSupport: (AXUIElement) -> (known: Bool, advertised: Bool) = { _ in (false, false) }
    let unknownCount: (AXUIElement) -> (known: Bool, count: CFIndex) = { _ in (false, 0) }
    let noValueChildren = semanticChildren(
        dummyElement, read: { _ in (.noValue, nil) },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    let unsupportedChildren = semanticChildren(
        dummyElement, read: { _ in (.attributeUnsupported, nil) },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    let emptyChildren = semanticChildren(
        dummyElement, read: { _ in (.success, [] as [Any]) },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    var retryReads = 0
    let recoveredChildren = semanticChildren(
        dummyElement,
        read: { _ in
            retryReads += 1
            return retryReads == 1 ? (.cannotComplete, nil) : (.success, [dummyElement] as [Any])
        },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    let retryProvenEmpty = semanticChildren(
        dummyElement, read: { _ in (.cannotComplete, nil) },
        supportedAttributes: { _ in (true, true) },
        count: { _ in (true, 0) }, retryPause: {}
    )
    let retryUnknown = semanticChildren(
        dummyElement, read: { _ in (.cannotComplete, nil) },
        supportedAttributes: { _ in (true, true) },
        count: { _ in (true, 2) }, retryPause: {}
    )
    let invalidElementChildren = semanticChildren(
        dummyElement, read: { _ in (.invalidUIElement, nil) },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    let invalidElementCountZero = semanticChildren(
        dummyElement, read: { _ in (.invalidUIElement, nil) },
        supportedAttributes: { _ in (true, true) }, count: { _ in (true, 0) },
        retryPause: {}
    )
    let invalidElementAttributeAbsent = semanticChildren(
        dummyElement, read: { _ in (.invalidUIElement, nil) },
        supportedAttributes: { _ in (true, false) }, count: unknownCount, retryPause: {}
    )
    let invalidElementContradiction = semanticChildren(
        dummyElement, read: { _ in (.invalidUIElement, nil) },
        supportedAttributes: { _ in (true, false) }, count: { _ in (true, 1) },
        retryPause: {}
    )
    var zeroCountReads = 0
    let navigationEmpty = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in
            zeroCountReads += 1
            return (.success, 0)
        },
        page: { _, _, _ in (.success, [] as [Any]) },
        parent: { _ in (.success, dummyElement) }
    )
    let navigationChildren = (1...9).map { AXUIElementCreateApplication(pid_t(20_000 + $0)) }
    var navigationPageReads = 0
    let navigationNine = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, CFIndex(navigationChildren.count)) },
        page: { _, index, length in
            navigationPageReads += 1
            let start = Int(index)
            return (.success, Array(navigationChildren[start..<(start + Int(length))]) as [Any])
        },
        parent: { _ in (.success, dummyElement) }
    )
    let navigationMixed = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, 2) },
        page: { _, _, _ in (.success, [navigationChildren[0], "invalid"] as [Any]) },
        parent: { _ in (.success, dummyElement) }
    )
    var navigationNotAdvertisedCountReads = 0
    let navigationNotAdvertised = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, false) },
        count: { _ in
            navigationNotAdvertisedCountReads += 1
            return (.success, 0)
        },
        page: { _, _, _ in (.success, [] as [Any]) },
        parent: { _ in (.success, dummyElement) }
    )
    var navigationUnavailableCountReads = 0
    let navigationCountUnavailable = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in
            navigationUnavailableCountReads += 1
            return (.cannotComplete, 0)
        },
        page: { _, _, _ in (.success, [] as [Any]) },
        parent: { _ in (.success, dummyElement) }
    )
    var navigationOverLimitPageReads = 0
    let navigationCountOverLimit = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, 256) },
        page: { _, _, _ in
            navigationOverLimitPageReads += 1
            return (.success, [] as [Any])
        },
        parent: { _ in (.success, dummyElement) }
    )
    var navigationPageFailureReads = 0
    let navigationPageCannotComplete = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, 1) },
        page: { _, _, _ in
            navigationPageFailureReads += 1
            return (.cannotComplete, nil)
        },
        parent: { _ in (.success, dummyElement) }
    )
    var changedCountReads = 0
    let navigationCountChanged = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in
            changedCountReads += 1
            return (.success, changedCountReads == 1 ? 1 : 2)
        },
        page: { _, _, _ in (.success, [navigationChildren[0]] as [Any]) },
        parent: { _ in (.success, dummyElement) }
    )
    let navigationDuplicate = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, 2) },
        page: { _, _, _ in
            (.success, [navigationChildren[0], navigationChildren[0]] as [Any])
        },
        parent: { _ in (.success, dummyElement) }
    )
    let navigationSelfCycle = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, 1) },
        page: { _, _, _ in (.success, [dummyElement] as [Any]) },
        parent: { _ in (.success, dummyElement) }
    )
    let navigationParentUnavailable = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, 1) },
        page: { _, _, _ in (.success, [navigationChildren[0]] as [Any]) },
        parent: { _ in (.cannotComplete, nil) }
    )
    let navigationParentMismatch = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.success, 1) },
        page: { _, _, _ in (.success, [navigationChildren[0]] as [Any]) },
        parent: { _ in (.success, navigationChildren[1]) }
    )
    let navigationAPIDisabled = semanticNavigationOrderChildren(
        dummyElement,
        attributeInventory: { _ in (true, true) },
        count: { _ in (.apiDisabled, 0) },
        page: { _, _, _ in (.success, [] as [Any]) },
        parent: { _ in (.success, dummyElement) }
    )
    let authoritativeEmpty = semanticAuthoritativeChildren(
        dummyElement, primary: invalidElementChildren,
        navigationOrder: { _ in navigationEmpty }
    )
    var unexpectedFallbackCalls = 0
    let authoritativePrimarySuccess = semanticAuthoritativeChildren(
        dummyElement, primary: emptyChildren,
        navigationOrder: { _ in
            unexpectedFallbackCalls += 1
            return navigationEmpty
        }
    )
    let authoritativeProtocolFailure = semanticAuthoritativeChildren(
        dummyElement, primary: invalidElementChildren,
        navigationOrder: { _ in navigationMixed }
    )
    let authoritativeGlobalFailure = semanticAuthoritativeChildren(
        dummyElement, primary: invalidElementChildren,
        navigationOrder: { _ in navigationAPIDisabled }
    )
    var navigationFacts = SemanticTextDiscoveryFacts()
    navigationFacts.recordNavigationOrderFallback(navigationEmpty)
    navigationFacts.recordChildrenRead(
        authoritativeEmpty.effective, role: "AXGroup", windowRoot: false,
        underToolbar: false
    )
    guard navigationEmpty.outcome == .completeEmpty,
          navigationEmpty.failureClass == .none,
          navigationEmpty.cardinalityClass == .zero,
          navigationEmpty.parentProof == .empty,
          navigationEmpty.countStable, navigationEmpty.complete,
          zeroCountReads == 2,
          navigationNine.outcome == .completeChildren,
          navigationNine.children.count == 9,
          navigationNine.cardinalityClass == .nineTo64,
          navigationNine.parentProof == .allDirect,
          navigationPageReads == 1,
          navigationMixed.outcome == .protocolInvalid,
          navigationMixed.failureClass == .payloadInvalid,
          navigationNotAdvertised.outcome == .unavailable,
          navigationNotAdvertised.failureClass == .notAdvertised,
          navigationNotAdvertisedCountReads == 0,
          navigationCountUnavailable.outcome == .incomplete,
          navigationCountUnavailable.failureClass == .countUnavailable,
          navigationCountUnavailable.observedAXErrorClasses.contains(.cannotComplete),
          navigationUnavailableCountReads == 1,
          navigationCountOverLimit.outcome == .incomplete,
          navigationCountOverLimit.failureClass == .countOverLimit,
          navigationCountOverLimit.cardinalityClass == .overLimit,
          navigationOverLimitPageReads == 0,
          navigationPageCannotComplete.outcome == .incomplete,
          navigationPageCannotComplete.failureClass == .pageAXFailure,
          navigationPageCannotComplete.observedAXErrorClasses.contains(.cannotComplete),
          navigationPageFailureReads == 1,
          navigationCountChanged.outcome == .incomplete,
          navigationCountChanged.failureClass == .countChanged,
          navigationDuplicate.failureClass == .duplicate,
          navigationSelfCycle.failureClass == .selfCycle,
          navigationParentUnavailable.failureClass == .parentUnavailable,
          navigationParentMismatch.failureClass == .parentMismatch,
          authoritativeEmpty.effective.branchProvenEmpty,
          authoritativePrimarySuccess.navigationOrder == nil,
          unexpectedFallbackCalls == 0,
          authoritativeProtocolFailure.effective.outcome == .protocolInvalid,
          authoritativeGlobalFailure.effective.outcome == .globalUnavailable,
          navigationFacts.navigationOrderFallbackAttemptedCount == 1,
          navigationFacts.navigationOrderFallbackSucceededCount == 1,
          navigationFacts.navigationOrderRecoveredInvalidCount == 1,
          navigationFacts.childrenInvalidElementCount == 0,
          navigationFacts.unresolvedSelectorBranchCount == 0
    else {
        fail("SELF_TEST_FAILED", "Semantic navigation-order equivalent children validation failed.")
    }
    let parentRefreshBudget = SemanticAdditionalChildrenReadBudget(limit: 2)
    var parentRefreshReads = 0
    let parentRefreshRetry = semanticChildren(
        dummyElement,
        read: { _ in
            parentRefreshReads += 1
            return parentRefreshReads == 1
                ? (.cannotComplete, nil)
                : (.success, [dummyElement] as [Any])
        },
        supportedAttributes: unknownSupport, count: unknownCount,
        additionalReadBudget: parentRefreshBudget,
        countInitialReadAgainstAdditionalBudget: true,
        retryPause: {}
    )
    let globalChildren = semanticChildren(
        dummyElement, read: { _ in (.apiDisabled, nil) },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    let unavailableChildren = semanticChildren(
        dummyElement, read: { _ in (.notImplemented, nil) },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    let illegalArgumentChildren = semanticChildren(
        dummyElement, read: { _ in (.illegalArgument, nil) },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    let protocolChildren = semanticChildren(
        dummyElement, read: { _ in (.success, "invalid payload") },
        supportedAttributes: unknownSupport, count: unknownCount, retryPause: {}
    )
    var rootFailureFacts = SemanticTextDiscoveryFacts()
    rootFailureFacts.windowScanTruncated = true
    rootFailureFacts.recordChildrenRead(
        invalidElementChildren, role: "AXWindow", windowRoot: true, underToolbar: false
    )
    var staticFailureFacts = SemanticTextDiscoveryFacts()
    staticFailureFacts.windowScanTruncated = true
    staticFailureFacts.recordChildrenRead(
        retryUnknown, role: "AXStaticText", windowRoot: false, underToolbar: true
    )
    var globalFailureFacts = SemanticTextDiscoveryFacts()
    globalFailureFacts.windowScanTruncated = true
    globalFailureFacts.recordChildrenRead(
        globalChildren, role: "AXGroup", windowRoot: false, underToolbar: false
    )
    var unavailableFailureFacts = SemanticTextDiscoveryFacts()
    unavailableFailureFacts.windowScanTruncated = true
    unavailableFailureFacts.recordChildrenRead(
        unavailableChildren, role: "AXGroup", windowRoot: false, underToolbar: false
    )
    var illegalArgumentFailureFacts = SemanticTextDiscoveryFacts()
    illegalArgumentFailureFacts.windowScanTruncated = true
    illegalArgumentFailureFacts.recordChildrenRead(
        illegalArgumentChildren, role: "AXGroup", windowRoot: false, underToolbar: false
    )
    var payloadProtocolFailureFacts = SemanticTextDiscoveryFacts()
    payloadProtocolFailureFacts.windowScanTruncated = true
    payloadProtocolFailureFacts.recordChildrenRead(
        protocolChildren, role: "AXGroup", windowRoot: false, underToolbar: false
    )
    var explicitAXFailurePrecedenceFacts = rootFailureFacts
    explicitAXFailurePrecedenceFacts.recordChildrenRead(
        globalChildren, role: "AXGroup", windowRoot: false, underToolbar: false
    )
    var boundedChildrenFacts = SemanticTextDiscoveryFacts()
    for _ in 0..<70 {
        boundedChildrenFacts.recordChildrenRead(
            retryUnknown, role: "AXGroup", windowRoot: false, underToolbar: false
        )
    }
    var classifiedChildrenFacts = SemanticTextDiscoveryFacts()
    for result in [
        noValueChildren, unsupportedChildren, emptyChildren, recoveredChildren,
        retryProvenEmpty, retryUnknown, protocolChildren
    ] {
        classifiedChildrenFacts.recordChildrenRead(
            result, role: "AXGroup", windowRoot: false, underToolbar: false
        )
    }
    var invalidChildFacts = SemanticTextDiscoveryFacts()
    invalidChildFacts.windowScanTruncated = true
    invalidChildFacts.recordChildrenRead(
        invalidElementChildren, role: "AXGroup", windowRoot: false, underToolbar: false
    )
    guard noValueChildren.outcome == .provenEmpty,
          unsupportedChildren.outcome == .provenEmpty,
          emptyChildren.outcome == .provenEmpty,
          recoveredChildren.outcome == .children,
          recoveredChildren.retryAttempted, recoveredChildren.retryRecovered,
          recoveredChildren.children.count == 1, retryReads == 2,
          recoveredChildren.readAttemptCount == 2,
          retryProvenEmpty.outcome == .provenEmpty,
          retryProvenEmpty.retryAttempted, retryProvenEmpty.branchProvenEmpty,
          retryUnknown.outcome == .unknownBranch,
          retryUnknown.childrenCountKnown, retryUnknown.childrenCountNonzero,
          invalidElementChildren.outcome == .staleElement,
          invalidElementChildren.readAttemptCount == 1,
          !invalidElementChildren.retryAttempted,
          invalidElementCountZero.outcome == .provenEmpty,
          invalidElementCountZero.structuralEmptyProof == .countZero,
          invalidElementAttributeAbsent.outcome == .provenEmpty,
          invalidElementAttributeAbsent.structuralEmptyProof == .attributeNotAdvertised,
          invalidElementContradiction.outcome == .protocolInvalid,
          invalidElementContradiction.observedErrorClasses.contains(.invalidElement),
          invalidElementContradiction.observedErrorClasses.contains(.payloadTypeInvalid),
          parentRefreshRetry.outcome == .children,
          parentRefreshRetry.retryAttempted,
          parentRefreshRetry.readAttemptCount == 2,
          parentRefreshReads == 2,
          parentRefreshBudget.consumed == 2,
          !parentRefreshBudget.exhausted,
          globalChildren.outcome == .globalUnavailable,
          protocolChildren.outcome == .protocolInvalid,
          semanticDiscoveryErrorCode(rootFailureFacts) == "TYPE_TARGET_DRIFTED",
          semanticDiscoveryErrorCode(invalidChildFacts) == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
          invalidChildFacts.childrenIncompleteBranchClass() == "container",
          staticFailureFacts.childrenIncompleteBranchClass() == "static_value",
          staticFailureFacts.childrenFailureUnderToolbar,
          semanticDiscoveryErrorCode(staticFailureFacts) == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
          semanticDiscoveryErrorCode(globalFailureFacts) == "TYPE_ACCESSIBILITY_NOT_TRUSTED",
          semanticDiscoveryErrorCode(unavailableFailureFacts) == "TYPE_ACCESSIBILITY_API_UNAVAILABLE",
          semanticDiscoveryErrorCode(illegalArgumentFailureFacts) == "TYPE_SEMANTIC_PROTOCOL_INVALID",
          semanticDiscoveryErrorCode(payloadProtocolFailureFacts) == "TYPE_SEMANTIC_PROTOCOL_INVALID",
          semanticDiscoveryErrorCode(explicitAXFailurePrecedenceFacts) == "TYPE_ACCESSIBILITY_NOT_TRUSTED",
          boundedChildrenFacts.childrenCannotCompleteCount == 64,
          boundedChildrenFacts.childrenUnknownBranchCount == 64,
          boundedChildrenFacts.countsTruncated,
          classifiedChildrenFacts.childrenReadSuccessCount == 2,
          classifiedChildrenFacts.childrenEmptyCount == 4,
          classifiedChildrenFacts.childrenUnsupportedCount == 1,
          classifiedChildrenFacts.childrenNoValueCount == 1,
          classifiedChildrenFacts.childrenCannotCompleteCount == 3,
          classifiedChildrenFacts.childrenProtocolFailureCount == 1,
          classifiedChildrenFacts.childrenProvenEmptyAfterFailureCount == 1,
          classifiedChildrenFacts.childrenRetryAttemptedCount == 3,
          classifiedChildrenFacts.childrenRetryRecoveredCount == 1
    else {
        fail("SELF_TEST_FAILED", "Semantic child-read classification or bounded retry validation failed.")
    }
    func syntheticReadyFacts(candidateCount: Int, nodes: Int) -> SemanticTextDiscoveryFacts {
        var facts = SemanticTextDiscoveryFacts()
        facts.nodesVisitedCount = nodes
        facts.windowNodesVisitedCount = nodes
        facts.windowScanComplete = true
        facts.roleMatchCount = candidateCount
        facts.windowOwnedCount = candidateCount
        facts.nonWebContentCount = candidateCount
        facts.frameValidCount = candidateCount
        facts.regionMatchCount = candidateCount
        facts.enabledCount = candidateCount
        facts.valuePresentCount = candidateCount
        facts.valueReadableCount = candidateCount
        facts.valueSettableCount = candidateCount
        facts.finalCandidateCount = candidateCount
        return facts
    }
    func syntheticStaleFacts(
        root: Bool = false,
        protocolFailure: Bool = false,
        nodeBudget: Bool = false,
        depthTruncated: Bool = false,
        nodes: Int = 31
    ) -> SemanticTextDiscoveryFacts {
        var facts = SemanticTextDiscoveryFacts()
        facts.nodesVisitedCount = nodes
        facts.windowNodesVisitedCount = nodes
        facts.windowScanTruncated = true
        facts.windowNodeBudgetTruncated = nodeBudget
        facts.windowDepthTruncated = depthTruncated
        facts.childrenInvalidElementCount = 1
        facts.childrenUnknownBranchCount = protocolFailure ? 2 : 1
        facts.childrenReadFailureCount = protocolFailure ? 2 : 1
        facts.windowChildrenInvalidElementCount = 1
        facts.windowChildrenUnknownBranchCount = protocolFailure ? 2 : 1
        facts.windowChildrenReadFailureCount = protocolFailure ? 2 : 1
        facts.childrenFailureOnWindowRoot = root
        if protocolFailure {
            facts.childrenProtocolFailureCount = 1
            facts.windowChildrenProtocolFailureCount = 1
            facts.sawChildrenProtocol = true
        } else {
            facts.sawChildrenStaleElement = true
        }
        return facts
    }
    let exactWindow = ExactSemanticWindow(
        pid: 4242, windowId: 7,
        frame: CGRect(x: 100, y: 100, width: 1200, height: 800),
        element: dummyElement
    )
    let oldCandidate = AXUIElementCreateApplication(1001)
    let newCandidate = AXUIElementCreateApplication(1002)
    let stalePass = SemanticTextDiscovery(
        eligibleCandidates: [oldCandidate], facts: syntheticStaleFacts()
    )
    let staleDescendant = SemanticStaleDescendant(
        parent: dummyElement, staleElement: oldCandidate, nodeClass: .other,
        depth: 3, parentChildCount: 1
    )
    let stalePassWithParent = SemanticTextDiscovery(
        eligibleCandidates: [oldCandidate], facts: syntheticStaleFacts(),
        staleDescendants: [staleDescendant]
    )
    let alternateParent = AXUIElementCreateApplication(1003)
    let sameParentNewReferencePass = SemanticTextDiscovery(
        eligibleCandidates: [newCandidate], facts: syntheticStaleFacts(),
        staleDescendants: [SemanticStaleDescendant(
            parent: dummyElement, staleElement: newCandidate, nodeClass: .other,
            depth: 3, parentChildCount: 1
        )]
    )
    let newParentSameReferencePass = SemanticTextDiscovery(
        eligibleCandidates: [oldCandidate], facts: syntheticStaleFacts(),
        staleDescendants: [SemanticStaleDescendant(
            parent: alternateParent, staleElement: oldCandidate, nodeClass: .other,
            depth: 3, parentChildCount: 1
        )]
    )
    let newParentNewReferencePass = SemanticTextDiscovery(
        eligibleCandidates: [newCandidate], facts: syntheticStaleFacts(),
        staleDescendants: [SemanticStaleDescendant(
            parent: alternateParent, staleElement: newCandidate, nodeClass: .other,
            depth: 3, parentChildCount: 1
        )]
    )
    let sameParentSameReferenceClass = semanticSecondThirdStaleReferenceClass(
        second: stalePassWithParent, third: stalePassWithParent
    )
    let sameParentNewReferenceClass = semanticSecondThirdStaleReferenceClass(
        second: stalePassWithParent, third: sameParentNewReferencePass
    )
    let newParentSameReferenceClass = semanticSecondThirdStaleReferenceClass(
        second: stalePassWithParent, third: newParentSameReferencePass
    )
    let newParentNewReferenceClass = semanticSecondThirdStaleReferenceClass(
        second: stalePassWithParent, third: newParentNewReferencePass
    )
    let notComparableStaleReferenceClass = semanticSecondThirdStaleReferenceClass(
        second: stalePassWithParent, third: stalePass
    )
    let cleanPass = SemanticTextDiscovery(
        eligibleCandidates: [newCandidate], facts: syntheticReadyFacts(candidateCount: 1, nodes: 7)
    )
    var structuralEmptyInvalidFacts = syntheticReadyFacts(candidateCount: 1, nodes: 7)
    structuralEmptyInvalidFacts.childrenInvalidElementCount = 1
    structuralEmptyInvalidFacts.childrenReadFailureCount = 1
    structuralEmptyInvalidFacts.windowChildrenInvalidElementCount = 1
    structuralEmptyInvalidFacts.windowChildrenReadFailureCount = 1
    structuralEmptyInvalidFacts.childrenBranchProvenEmpty = true
    let structuralEmptyInvalidPass = SemanticTextDiscovery(
        eligibleCandidates: [newCandidate], facts: structuralEmptyInvalidFacts
    )
    func successfulParentRefresh(
        _ _: AXUIElement, _ budget: SemanticAdditionalChildrenReadBudget
    ) -> AXChildrenReadResult {
        guard budget.consume() else {
            return AXChildrenReadResult(
                children: [], outcome: .additionalReadBudgetExhausted,
                errorClass: .additionalReadBudgetExhausted,
            observedErrorClasses: [.additionalReadBudgetExhausted],
            childrenAttributeAdvertised: false, childrenCountKnown: false,
            childrenCountNonzero: false, structuralEmptyProof: .none,
            retryAttempted: false,
                retryRecovered: false, additionalReadBudgetExhausted: true,
                readAttemptCount: 0
            )
        }
        return AXChildrenReadResult(
            children: [newCandidate], outcome: .children, errorClass: .none,
            observedErrorClasses: [.none], childrenAttributeAdvertised: true,
            childrenCountKnown: true, childrenCountNonzero: true,
            structuralEmptyProof: .none,
            retryAttempted: false, retryRecovered: false,
            additionalReadBudgetExhausted: false, readAttemptCount: 1
        )
    }
    var recoveryPassCalls = 0
    let recoveredDiscovery = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in
            recoveryPassCalls += 1
            return recoveryPassCalls == 1 ? stalePass : cleanPass
        },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    let structuralEmptyInvalidDiscovery = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in structuralEmptyInvalidPass },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    var persistentPassCalls = 0
    let persistentStale = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in persistentPassCalls += 1; return stalePass },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    var parentRecoveredPassCalls = 0
    let parentRecovered = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in
            parentRecoveredPassCalls += 1
            switch parentRecoveredPassCalls {
            case 1, 2: return stalePassWithParent
            default: return cleanPass
            }
        },
        parentRefresh: successfulParentRefresh,
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    var parentPersistentPassCalls = 0
    let parentPersistent = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in
            parentPersistentPassCalls += 1
            return stalePassWithParent
        },
        parentRefresh: successfulParentRefresh,
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    var persistentStaleSubtreeFacts = syntheticStaleFacts()
    persistentStaleSubtreeFacts.staleRecoveryOutcome = "final_pass_stale"
    persistentStaleSubtreeFacts.staleReferenceRefreshClass = "same_stale_reference_returned"
    persistentStaleSubtreeFacts.staleBranchComparison = "same_class_and_depth"
    persistentStaleSubtreeFacts.secondThirdStaleReferenceClass = "same_parent_same_reference"
    persistentStaleSubtreeFacts.discoveryPassCount = 3
    persistentStaleSubtreeFacts.staleRecoveryRestartCount = 2
    persistentStaleSubtreeFacts.staleRecoveryFinalScanComplete = false
    let persistentStaleSubtree = SemanticTextDiscovery(
        eligibleCandidates: [], facts: persistentStaleSubtreeFacts
    )
    let parentBudgetExhausted = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in stalePassWithParent },
        parentRefresh: successfulParentRefresh, additionalReadBudgetLimit: 0,
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    var mixedPassCalls = 0
    let mixedStale = SemanticTextDiscovery(
        eligibleCandidates: [oldCandidate], facts: syntheticStaleFacts(protocolFailure: true)
    )
    let mixedDiscovery = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in mixedPassCalls += 1; return mixedStale },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    var rootPassCalls = 0
    let rootStalePass = SemanticTextDiscovery(
        eligibleCandidates: [], facts: syntheticStaleFacts(root: true)
    )
    let rootStaleDiscovery = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in rootPassCalls += 1; return rootStalePass },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    let rebindFailed = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in stalePass }, rebind: { _ in nil }, frontmost: { 111 }
    )
    let changedWindow = ExactSemanticWindow(
        pid: exactWindow.pid, windowId: exactWindow.windowId, frame: exactWindow.frame,
        element: AXUIElementCreateApplication(999)
    )
    let rebindChanged = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in stalePass }, rebind: { _ in changedWindow }, frontmost: { 111 }
    )
    var foregroundObservations = [pid_t(111), pid_t(111), pid_t(222)]
    let frontmostChanged = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in foregroundObservations.count == 3 ? stalePass : cleanPass },
        rebind: { _ in exactWindow },
        frontmost: { foregroundObservations.removeFirst() }
    )
    let roleAbsentPass = SemanticTextDiscovery(
        eligibleCandidates: [], facts: syntheticReadyFacts(candidateCount: 0, nodes: 5)
    )
    var roleAbsentCalls = 0
    let recoveredRoleAbsent = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in roleAbsentCalls += 1; return roleAbsentCalls == 1 ? stalePass : roleAbsentPass },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    let multiplePass = SemanticTextDiscovery(
        eligibleCandidates: [newCandidate, oldCandidate],
        facts: syntheticReadyFacts(candidateCount: 2, nodes: 9)
    )
    var multipleCalls = 0
    let recoveredMultiple = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in multipleCalls += 1; return multipleCalls == 1 ? stalePass : multiplePass },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    let budgetStale = SemanticTextDiscovery(
        eligibleCandidates: [], facts: syntheticStaleFacts(nodeBudget: true)
    )
    let budgetRecovery = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in budgetStale }, rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    var appOnlyStaleFacts = syntheticReadyFacts(candidateCount: 0, nodes: 5)
    appOnlyStaleFacts.appScanPerformed = true
    appOnlyStaleFacts.appScanTruncated = true
    appOnlyStaleFacts.appScanComplete = false
    appOnlyStaleFacts.childrenInvalidElementCount = 1
    appOnlyStaleFacts.childrenUnknownBranchCount = 1
    appOnlyStaleFacts.childrenReadFailureCount = 1
    appOnlyStaleFacts.sawChildrenStaleElement = true
    var appOnlyPassCalls = 0
    let appOnlyStale = semanticTextDiscoveryWithStaleRecovery(
        args: [:], window: exactWindow, selector: selector!, frontmostBefore: 111,
        discover: { _, _ in
            appOnlyPassCalls += 1
            return SemanticTextDiscovery(eligibleCandidates: [], facts: appOnlyStaleFacts)
        },
        rebind: { _ in exactWindow }, frontmost: { 111 }
    )
    let persistentStaleSubtreeStage = persistentStaleSubtree.facts.discoveryStage()
    let persistentStaleSubtreeCandidateCount = persistentStaleSubtree.eligibleCandidates.count
    let persistentStaleSubtreeAdditionalReadCount = persistentStaleSubtree.facts.staleAdditionalAXReadCount
    let persistentStaleSubtreeBudgetExhausted = persistentStaleSubtree.facts.staleAdditionalReadBudgetExhausted
    let persistentStaleSubtreeMutationEligible = persistentStaleSubtreeStage == "ready"
        && persistentStaleSubtreeCandidateCount == 1
        && persistentStaleSubtree.facts.finalCandidateCount == 1
    let persistentStaleSubtreeCode = semanticDiscoveryErrorCode(persistentStaleSubtree.facts)
    var differentOutcomeFacts = persistentStaleSubtree.facts
    differentOutcomeFacts.staleRecoveryOutcome = "final_pass_incomplete"
    var differentRefreshFacts = persistentStaleSubtree.facts
    differentRefreshFacts.staleReferenceRefreshClass = "stale_reference_absent_nonempty"
    var differentBranchFacts = persistentStaleSubtree.facts
    differentBranchFacts.staleBranchComparison = "different_class_or_depth"
    var differentSecondThirdFacts = persistentStaleSubtree.facts
    differentSecondThirdFacts.secondThirdStaleReferenceClass = "same_parent_new_reference"
    var fewerPassesFacts = persistentStaleSubtree.facts
    fewerPassesFacts.discoveryPassCount = 2
    var fewerRestartsFacts = persistentStaleSubtree.facts
    fewerRestartsFacts.staleRecoveryRestartCount = 1
    var completedWindowFacts = persistentStaleSubtree.facts
    completedWindowFacts.windowScanComplete = true
    var completedFinalRecoveryFacts = persistentStaleSubtree.facts
    completedFinalRecoveryFacts.staleRecoveryFinalScanComplete = true
    let nonPersistentStaleFacts = [
        differentOutcomeFacts, differentRefreshFacts, differentBranchFacts,
        differentSecondThirdFacts, fewerPassesFacts, fewerRestartsFacts,
        completedWindowFacts, completedFinalRecoveryFacts,
    ]
    let negativePersistentStaleClassificationIsGeneric = nonPersistentStaleFacts.allSatisfy {
        !semanticRepeatedlyStaleBranch($0)
            && semanticDiscoveryErrorCode($0) == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE"
    }
    guard recoveryPassCalls == 2,
          recoveredDiscovery.facts.staleRecoveryOutcome == "recovered_clean",
          recoveredDiscovery.facts.staleRecoverySucceeded,
          recoveredDiscovery.facts.discoveryPassCount == 2,
          recoveredDiscovery.facts.staleRecoveryRestartCount == 1,
          structuralEmptyInvalidDiscovery.facts.staleRecoveryOutcome == "not_needed",
          structuralEmptyInvalidDiscovery.facts.windowScanComplete,
          structuralEmptyInvalidDiscovery.eligibleCandidates.count == 1,
          recoveredDiscovery.facts.firstPassNodesVisitedCount == 31,
          recoveredDiscovery.facts.secondPassNodesVisitedCount == 7,
          recoveredDiscovery.facts.secondPassFinalCandidateCount == 1,
          recoveredDiscovery.eligibleCandidates.count == 1,
          CFEqual(recoveredDiscovery.eligibleCandidates[0], newCandidate),
          !CFEqual(recoveredDiscovery.eligibleCandidates[0], oldCandidate),
          persistentPassCalls == 2,
          persistentStale.facts.staleRecoveryOutcome == "parent_refresh_not_eligible",
          semanticDiscoveryErrorCode(persistentStale.facts) == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
          persistentStale.eligibleCandidates.isEmpty,
          parentRecoveredPassCalls == 3,
          parentRecovered.facts.staleRecoveryOutcome == "recovered_after_parent_refresh",
          parentRecovered.facts.staleRecoverySucceeded,
          parentRecovered.facts.staleParentRefreshAttempted,
          parentRecovered.facts.staleParentRefreshSucceeded,
          parentRecovered.facts.staleParentRefreshCount == 1,
          parentRecovered.facts.staleParentRefreshReadCount == 1,
          parentRecovered.facts.staleRecoveryFinalScanComplete,
          parentRecovered.facts.discoveryPassCount == 3,
          parentRecovered.facts.staleRecoveryRestartCount == 2,
          parentRecovered.facts.staleAdditionalAXReadCount == 1,
          !parentRecovered.facts.staleAdditionalReadBudgetExhausted,
          parentRecovered.facts.staleReferenceRefreshClass == "stale_reference_absent_nonempty",
          parentRecovered.facts.staleBranchComparison == "same_class_and_depth",
          parentRecovered.facts.thirdPassStaleCount == 0,
          parentRecovered.facts.thirdPassUnknownBranchCount == 0,
          parentRecovered.facts.thirdPassNodesVisitedCount == 7,
          parentRecovered.facts.thirdPassFinalCandidateCount == 1,
          parentRecovered.eligibleCandidates.count == 1,
          CFEqual(parentRecovered.eligibleCandidates[0], newCandidate),
          sameParentSameReferenceClass == "same_parent_same_reference",
          sameParentNewReferenceClass == "same_parent_new_reference",
          newParentSameReferenceClass == "new_parent_same_reference",
          newParentNewReferenceClass == "new_parent_new_reference",
          notComparableStaleReferenceClass == "not_comparable",
          parentPersistentPassCalls == 3,
          parentPersistent.facts.staleRecoveryOutcome == "final_pass_stale",
          parentPersistent.facts.staleReferenceRefreshClass == "stale_reference_absent_nonempty",
          parentPersistent.facts.secondThirdStaleReferenceClass == "same_parent_same_reference",
          parentPersistent.facts.staleParentRefreshSucceeded,
          !parentPersistent.facts.staleRecoveryFinalScanComplete,
          parentPersistent.eligibleCandidates.isEmpty,
          semanticDiscoveryErrorCode(parentPersistent.facts) == "TYPE_SEMANTIC_DISCOVERY_INCOMPLETE",
          semanticRepeatedlyStaleBranch(persistentStaleSubtree.facts),
          persistentStaleSubtree.facts.staleRecoveryOutcome == "final_pass_stale",
          persistentStaleSubtree.facts.staleReferenceRefreshClass == "same_stale_reference_returned",
          persistentStaleSubtree.facts.staleBranchComparison == "same_class_and_depth",
          persistentStaleSubtree.facts.secondThirdStaleReferenceClass == "same_parent_same_reference",
          persistentStaleSubtree.facts.discoveryPassCount == 3,
          persistentStaleSubtree.facts.staleRecoveryRestartCount == 2,
          !persistentStaleSubtree.facts.windowScanComplete,
          !persistentStaleSubtree.facts.staleRecoveryFinalScanComplete,
          persistentStaleSubtreeCandidateCount == 0,
          persistentStaleSubtreeStage == "scan_incomplete",
          persistentStaleSubtreeAdditionalReadCount == 0,
          !persistentStaleSubtreeBudgetExhausted,
          !persistentStaleSubtreeMutationEligible,
          persistentStaleSubtreeCode == "TYPE_SEMANTIC_AX_BRANCH_REPEATEDLY_STALE",
          negativePersistentStaleClassificationIsGeneric,
          parentBudgetExhausted.facts.staleRecoveryOutcome == "parent_refresh_budget_exhausted",
          parentBudgetExhausted.facts.staleParentRefreshAttempted,
          !parentBudgetExhausted.facts.staleParentRefreshSucceeded,
          parentBudgetExhausted.facts.staleParentRefreshReadCount == 0,
          parentBudgetExhausted.facts.staleAdditionalReadBudgetExhausted,
          parentBudgetExhausted.eligibleCandidates.isEmpty,
          mixedPassCalls == 1,
          mixedDiscovery.facts.staleRecoveryOutcome == "recovery_not_eligible",
          rootPassCalls == 1,
          rootStaleDiscovery.facts.staleRecoveryOutcome == "recovery_not_eligible",
          semanticDiscoveryErrorCode(rootStaleDiscovery.facts) == "TYPE_TARGET_DRIFTED",
          rebindFailed.facts.staleRecoveryOutcome == "exact_window_rebind_failed",
          semanticDiscoveryErrorCode(rebindFailed.facts) == "TYPE_TARGET_DRIFTED",
          rebindChanged.facts.staleRecoveryOutcome == "exact_window_changed",
          semanticDiscoveryErrorCode(rebindChanged.facts) == "TYPE_TARGET_DRIFTED",
          frontmostChanged.facts.staleRecoveryOutcome == "frontmost_changed",
          semanticDiscoveryErrorCode(frontmostChanged.facts) == "TYPE_TARGET_DRIFTED",
          frontmostChanged.eligibleCandidates.isEmpty,
          recoveredRoleAbsent.facts.staleRecoveryOutcome == "recovered_clean",
          recoveredRoleAbsent.facts.discoveryStage() == "role_absent",
          recoveredMultiple.facts.staleRecoveryOutcome == "recovered_clean",
          recoveredMultiple.facts.discoveryStage() == "ambiguous",
          budgetRecovery.facts.staleRecoveryOutcome == "recovery_not_eligible",
          appOnlyPassCalls == 1,
          appOnlyStale.facts.staleRecoveryOutcome == "not_needed",
          appOnlyStale.facts.discoveryStage() == "role_absent",
          appOnlyStale.facts.appDiagnosticStage() == "scan_incomplete",
          semanticDiscoveryErrorCode(appOnlyStale.facts) == "TYPE_SEMANTIC_CONTROL_NOT_FOUND",
          appOnlyStale.eligibleCandidates.isEmpty
    else {
        fail("SELF_TEST_FAILED", "Semantic stale-tree recovery orchestration failed.")
    }
    func fixedAttributes(
        contents: Bool = false,
        visible: Bool = false,
        navigation: Bool = false,
        shared: Bool = false
    ) -> SemanticFixedAttributeInventory {
        SemanticFixedAttributeInventory(
            known: true, contents: contents, visibleChildren: visible,
            navigationOrder: navigation, sharedText: shared,
            titleUIElement: false, servesAsTitle: false,
            linkedUIElements: false, parent: false
        )
    }
    let noParameterized = SemanticFixedParameterizedInventory(
        known: true, searchPredicate: false,
        elementForTextMarker: false, textMarkerRangeForElement: false
    )
    let noFocusedElement: (pid_t, Int) -> SemanticElementListReadResult = { _, _ in
        SemanticElementListReadResult(
            elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
        )
    }
    let fanoutTargets = (0..<9).map { AXUIElementCreateApplication(pid_t(5100 + $0)) }
    let fanoutExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { CFEqual($0, dummyElement) ? fixedAttributes(navigation: true) : fixedAttributes() },
        parameterizedInventory: { _ in noParameterized },
        readElements: { element, attribute, _, _ in
            SemanticElementListReadResult(
                elements: CFEqual(element, dummyElement) && attribute == "AXChildrenInNavigationOrder"
                    ? Array(fanoutTargets.prefix(8)) : [],
                complete: false, truncated: true, readAttempts: 1, failed: false,
                outcome: .fanoutTruncated, cardinalityClass: .overCap
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXGroup" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let depthChain = (0..<5).map { AXUIElementCreateApplication(pid_t(5200 + $0)) }
    let depthExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { element in
            if CFEqual(element, dummyElement) || depthChain.dropLast().contains(where: { CFEqual($0, element) }) {
                return fixedAttributes(contents: true)
            }
            return fixedAttributes()
        },
        parameterizedInventory: { _ in noParameterized },
        readElements: { element, attribute, _, _ in
            guard attribute == "AXContents" else {
                return SemanticElementListReadResult(
                    elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
                )
            }
            if CFEqual(element, dummyElement) {
                return SemanticElementListReadResult(
                    elements: [depthChain[0]], complete: true, truncated: false, readAttempts: 1, failed: false
                )
            }
            for index in 0..<4 where CFEqual(element, depthChain[index]) {
                return SemanticElementListReadResult(
                    elements: [depthChain[index + 1]], complete: true, truncated: false, readAttempts: 1, failed: false
                )
            }
            return SemanticElementListReadResult(
                elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXGroup" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let queuedDepthElements = (0..<6).map { AXUIElementCreateApplication(pid_t(5300 + $0)) }
    let queuedDepthExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { element in
            if CFEqual(element, dummyElement)
                || queuedDepthElements.prefix(5).contains(where: { CFEqual($0, element) }) {
                return fixedAttributes(contents: true)
            }
            return fixedAttributes()
        },
        parameterizedInventory: { _ in noParameterized },
        readElements: { element, attribute, _, _ in
            var targets: [AXUIElement] = []
            if attribute == "AXContents" {
                if CFEqual(element, dummyElement) { targets = [queuedDepthElements[0]] }
                else if CFEqual(element, queuedDepthElements[0]) { targets = [queuedDepthElements[1]] }
                else if CFEqual(element, queuedDepthElements[1]) { targets = [queuedDepthElements[2]] }
                else if CFEqual(element, queuedDepthElements[2]) {
                    targets = [queuedDepthElements[3], queuedDepthElements[4]]
                } else if CFEqual(element, queuedDepthElements[3]) {
                    targets = [queuedDepthElements[4]]
                }
            }
            return SemanticElementListReadResult(
                elements: targets, complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXGroup" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let multipleFocusExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in fixedAttributes() }, parameterizedInventory: { _ in noParameterized },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: { _, _ in
            SemanticElementListReadResult(
                elements: [fanoutTargets[0], fanoutTargets[1]], complete: false,
                truncated: true, readAttempts: 1, failed: false,
                outcome: .fanoutTruncated, cardinalityClass: .overCap
            )
        },
        roleOf: { _ in "AXWindow" }, ownershipOf: { _, _ in "ancestor_chain" },
        forbidden: { _, _ in false }, fullyEligible: { _, _, _ in false }
    )
    let unknownAttributes = SemanticFixedAttributeInventory(
        known: false, contents: false, visibleChildren: false, navigationOrder: false,
        sharedText: false, titleUIElement: false, servesAsTitle: false,
        linkedUIElements: false, parent: false
    )
    let unknownParameterized = SemanticFixedParameterizedInventory(
        known: false, searchPredicate: false,
        elementForTextMarker: false, textMarkerRangeForElement: false
    )
    let unknownInventoryExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in unknownAttributes },
        parameterizedInventory: { _ in unknownParameterized },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXWindow" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let payloadFailureExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in fixedAttributes(contents: true) },
        parameterizedInventory: { _ in noParameterized },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [], complete: false, truncated: false, readAttempts: 1,
                failed: true, outcome: .payloadMixed, cardinalityClass: .unknown
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXWindow" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let rejectedTarget = AXUIElementCreateApplication(5400)
    let ownershipExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { CFEqual($0, dummyElement) ? fixedAttributes(contents: true) : fixedAttributes() },
        parameterizedInventory: { _ in noParameterized },
        readElements: { element, attribute, _, _ in
            SemanticElementListReadResult(
                elements: CFEqual(element, dummyElement) && attribute == "AXContents" ? [rejectedTarget] : [],
                complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXGroup" },
        ownershipOf: { element, _ in CFEqual(element, rejectedTarget) ? "none" : "ancestor_chain" },
        forbidden: { _, _ in false }, fullyEligible: { _, _, _ in false }
    )
    var saturationFacts = SemanticExposureProbeFacts()
    for _ in 0..<17 {
        saturationFacts.increment(\.edgeFanoutTruncatedCount, cap: 16, saturationClass: .edgeFanout)
    }
    guard fanoutExposure.incompleteCause() == "edge_fanout",
          fanoutExposure.fanoutSources == [.navigationOrder],
          fanoutExposure.edgeFanoutTruncatedCount == 1,
          !fanoutExposure.globalNodeLimitHit, !fanoutExposure.globalReadLimitHit,
          depthExposure.incompleteCause() == "depth_limit",
          depthExposure.depthLimitNewTargetCount == 1,
          depthExposure.depthLimitQueuedTargetCount == 0,
          depthExposure.depthLimitSources == [.contents],
          queuedDepthExposure.incompleteCause() == "depth_limit",
          queuedDepthExposure.depthLimitNewTargetCount == 0,
          queuedDepthExposure.depthLimitQueuedTargetCount == 1,
          multipleFocusExposure.incompleteCause() == "focus_cardinality",
          multipleFocusExposure.focusCardinality == "multiple",
          unknownInventoryExposure.incompleteCause() == "multiple",
          unknownInventoryExposure.incompleteCauses.count == 2,
          unknownInventoryExposure.attributeInventoryUnknownCount == 1,
          unknownInventoryExposure.parameterizedInventoryUnknownCount == 1,
          payloadFailureExposure.incompleteCause() == "payload_invalid",
          payloadFailureExposure.payloadMixedCount == 1,
          ownershipExposure.complete,
          ownershipExposure.incompleteCause() == "none",
          ownershipExposure.edgeTargetOwnershipRejectedCount == 1,
          ownershipExposure.nodeOwnershipRejectedCount == 1,
          saturationFacts.countSaturated,
          saturationFacts.incompleteCause() == "counter_saturation",
          saturationFacts.countSaturationClass() == "edge_fanout"
    else {
        fail("SELF_TEST_FAILED", "Semantic exposure branch-cause diagnostics failed.")
    }
    let exposedAllowedRole = AXUIElementCreateApplication(3001)
    let focusedPageRole = AXUIElementCreateApplication(3002)
    let structuralExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { CFEqual($0, dummyElement) ? fixedAttributes(contents: true) : fixedAttributes() },
        parameterizedInventory: { _ in noParameterized },
        readElements: { element, attribute, _, _ in
            SemanticElementListReadResult(
                elements: CFEqual(element, dummyElement) && attribute == "AXContents"
                    ? [exposedAllowedRole] : [],
                complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement,
        roleOf: { CFEqual($0, exposedAllowedRole) ? "AXTextField" : "AXWindow" },
        ownershipOf: { _, _ in "ax_window_attribute" },
        forbidden: { _, _ in false },
        fullyEligible: { element, _, _ in CFEqual(element, exposedAllowedRole) }
    )
    let relationshipExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { CFEqual($0, dummyElement) ? fixedAttributes(shared: true) : fixedAttributes() },
        parameterizedInventory: { _ in noParameterized },
        readElements: { element, attribute, _, _ in
            SemanticElementListReadResult(
                elements: CFEqual(element, dummyElement) && attribute == "AXSharedTextUIElements"
                    ? [exposedAllowedRole] : [],
                complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement,
        roleOf: { CFEqual($0, exposedAllowedRole) ? "AXTextField" : "AXWindow" },
        ownershipOf: { _, _ in "ax_window_attribute" },
        forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let focusedPageExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in fixedAttributes() },
        parameterizedInventory: { _ in noParameterized },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: { _, _ in
            SemanticElementListReadResult(
                elements: [focusedPageRole], complete: true, truncated: false,
                readAttempts: 1, failed: false
            )
        },
        roleOf: { CFEqual($0, focusedPageRole) ? "AXTextField" : "AXWindow" },
        ownershipOf: { _, _ in "ancestor_chain" },
        forbidden: { element, _ in CFEqual(element, focusedPageRole) },
        fullyEligible: { _, _, _ in false }
    )
    let capabilityExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in fixedAttributes() },
        parameterizedInventory: { _ in
            SemanticFixedParameterizedInventory(
                known: true, searchPredicate: true,
                elementForTextMarker: false, textMarkerRangeForElement: true
            )
        },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement,
        roleOf: { _ in "AXWindow" }, ownershipOf: { _, _ in "ancestor_chain" },
        forbidden: { _, _ in false }, fullyEligible: { _, _, _ in false }
    )
    let noExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in fixedAttributes() },
        parameterizedInventory: { _ in noParameterized },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [], complete: true, truncated: false, readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXWindow" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let incompleteExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in fixedAttributes(contents: true) },
        parameterizedInventory: { _ in noParameterized },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [], complete: false, truncated: false, readAttempts: 2, failed: true
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXWindow" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let cyclicExposure = semanticAlternateExposureProbe(
        window: exactWindow, selector: selector!, proxySeeds: [],
        attributeInventory: { _ in fixedAttributes(contents: true) },
        parameterizedInventory: { _ in noParameterized },
        readElements: { _, _, _, _ in
            SemanticElementListReadResult(
                elements: [dummyElement], complete: true, truncated: false,
                readAttempts: 1, failed: false
            )
        },
        focusedElement: noFocusedElement, roleOf: { _ in "AXWindow" },
        ownershipOf: { _, _ in "ancestor_chain" }, forbidden: { _, _ in false },
        fullyEligible: { _, _, _ in false }
    )
    let exposurePayloadKeys = Set(structuralExposure.payload().keys)
    guard structuralExposure.complete,
          structuralExposure.stage() == "alternate_structural_role_found",
          structuralExposure.source() == "contents",
          structuralExposure.allowedRoleCount == 1,
          structuralExposure.fullEligibilityCount == 1,
          relationshipExposure.complete,
          relationshipExposure.stage() == "relationship_role_found",
          relationshipExposure.source() == "shared_text",
          relationshipExposure.sharedTextRelationCount == 1,
          focusedPageExposure.stage() == "focused_page_control",
          focusedPageExposure.focusedElementPresent,
          focusedPageExposure.focusedElementExactOwned,
          !focusedPageExposure.focusedElementNonWeb,
          capabilityExposure.stage() == "capability_advertised_only",
          capabilityExposure.parameterizedCapabilityClass() == "multiple",
          noExposure.stage() == "complete_no_fixed_exposure",
          incompleteExposure.stage() == "incomplete",
          incompleteExposure.edgeReadsCount == 3,
          cyclicExposure.complete,
          cyclicExposure.nodesVisitedCount == 1,
          exposurePayloadKeys.isDisjoint(with: [
              "attribute", "parameterized_attribute", "role", "subrole", "value",
              "label", "title", "element", "frame", "pid", "window_id"
          ])
    else {
        fail("SELF_TEST_FAILED", "Semantic fixed-edge exposure diagnostics validation failed.")
    }
    ok([
        "self_test": true,
        "typing_completion": true,
        "direct_replacement": true,
        "selected_text_priority": true,
        "ax_value_replacement": true,
        "direct_no_mutation_fallback": true,
        "unicode_replacement": true,
        "partial_direct_not_retried": true,
        "pid_targeted_routing": true,
        "explicit_target_rebind": true,
        "explicit_target_rebind_failure_rejected": true,
        "semantic_selector_validated": true,
        "exact_window_geometry_validated": true,
        "semantic_staged_discovery_validated": true,
        "semantic_relative_coordinates_validated": true,
        "semantic_diagnostics_bounded": true,
        "semantic_children_outcomes_validated": true,
        "semantic_children_retry_bounded": true,
        "semantic_stale_recovery_validated": true,
        "semantic_exposure_probe_validated": true,
        "visibility_topology_diagnostics_validated": true,
        "target_drift_rejected": true,
        "pid_drift_rejected": true,
        "paced_units": completed.dispatchedUnitCount,
        "partial_rejected": true
    ])
}

func keyCode(_ key: String) -> CGKeyCode? {
    let lower = key.lowercased()
    let map: [String: CGKeyCode] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
        "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
        "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
        "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
        "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "return": 36,
        "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
        ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49,
        "`": 50, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
        "left": 123, "right": 124, "down": 125, "up": 126
    ]
    if let code = map[lower] {
        return code
    }
    if lower.hasPrefix("f"), let number = Int(lower.dropFirst()), number >= 1, number <= 20 {
        return CGKeyCode(121 + number)
    }
    return nil
}

func flags(_ modifiers: [String]) -> CGEventFlags {
    var result = CGEventFlags()
    for modifier in modifiers.map({ $0.lowercased() }) {
        switch modifier {
        case "cmd", "command", "meta":
            result.insert(.maskCommand)
        case "ctrl", "control":
            result.insert(.maskControl)
        case "alt", "option":
            result.insert(.maskAlternate)
        case "shift":
            result.insert(.maskShift)
        default:
            continue
        }
    }
    return result
}

func key(args: [String: Any]) -> Never {
    let combo = stringValue(args["key_combo"])
    var parts = combo.isEmpty ? [] : combo.split(separator: "+").map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
    if parts.isEmpty {
        let keyName = stringValue(args["key"])
        if keyName.isEmpty {
            fail("KEY_REQUIRED", "key or key_combo is required.")
        }
        var modifiers = (args["modifiers"] as? [String]) ?? []
        let modifier = stringValue(args["modifier"])
        if !modifier.isEmpty {
            modifiers.append(modifier)
        }
        parts = modifiers + [keyName]
    }
    let keyName = parts.removeLast()
    guard let code = keyCode(keyName) else {
        fail("UNSUPPORTED_KEY", "Unsupported key: \(keyName)")
    }
    let eventFlags = flags(parts)
    if let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true) {
        down.flags = eventFlags
        down.post(tap: .cghidEventTap)
    }
    if let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) {
        up.flags = eventFlags
        up.post(tap: .cghidEventTap)
    }
    ok(["action": "computer.key", "platform": "Darwin", "executed": true, "key": keyName, "modifiers": parts, "driver": "mac_swift_host"])
}

func scroll(args: [String: Any]) -> Never {
    let direction = stringValue(args["direction"]).isEmpty ? "down" : stringValue(args["direction"]).lowercased()
    let amount = max(1, intValue(args["amount"] ?? args["clicks"], default: 3))
    let dy = direction == "up" ? amount : (direction == "down" ? -amount : 0)
    let dx = direction == "left" ? amount : (direction == "right" ? -amount : 0)
    let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: Int32(dy), wheel2: Int32(dx), wheel3: 0)
    event?.post(tap: .cghidEventTap)
    ok(["action": "computer.scroll", "platform": "Darwin", "executed": true, "direction": direction, "amount": amount, "driver": "mac_swift_host"])
}

func clipboardRead() -> Never {
    let pasteboard = NSPasteboard.general
    let content = pasteboard.string(forType: .string) ?? ""
    ok(["action": "computer.clipboard.read", "platform": "Darwin", "format": "text/plain", "content": content, "length": content.count, "driver": "mac_swift_host"])
}

func clipboardWrite(args: [String: Any]) -> Never {
    let content = stringValue(args["content"] ?? args["value"] ?? args["text"])
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    pasteboard.setString(content, forType: .string)
    ok(["action": "computer.clipboard.write", "platform": "Darwin", "executed": true, "length": content.count, "driver": "mac_swift_host"])
}

func clipboardClear() -> Never {
    NSPasteboard.general.clearContents()
    ok(["action": "computer.clipboard.clear", "platform": "Darwin", "executed": true, "driver": "mac_swift_host"])
}

func doctor() -> Never {
    ok([
        "action": "computer.doctor",
        "platform": "Darwin",
        "host": hostVersion,
        "accessibility_trusted": AXIsProcessTrusted(),
        "screen_count": NSScreen.screens.count,
        "driver": "mac_swift_host"
    ])
}

if CommandLine.arguments.dropFirst().contains("--self-test") {
    typingCompletionSelfTest()
}

do {
    let request = try readRequest()
    let action = stringValue(request["action"] ?? request["function_id"])
    let args = (request["args"] as? [String: Any]) ?? (request["payload"] as? [String: Any]) ?? request
    switch action {
    case "computer.doctor", "doctor":
        doctor()
    case "computer.apps", "apps":
        ok(["action": "computer.apps", "platform": "Darwin", "apps": runningApps(), "driver": "mac_swift_host"])
    case "computer.windows", "windows":
        if boolValue(args["inventory_diagnostics"]) {
            let snapshot = windowInventorySnapshot(args: args)
            ok([
                "action": "computer.windows", "platform": "Darwin",
                "windows": snapshot.windows, "driver": "mac_swift_host",
                "inventory_diagnostic_contract": windowInventoryDiagnosticContract,
                "selected_window_identity_diagnostic_contract": selectedWindowIdentityDiagnosticContract,
                "inventory_diagnostics": snapshot.facts.payload(),
                "inventory_private": [
                    "helper_signature_token": snapshot.signatureToken,
                ],
            ])
        }
        ok(["action": "computer.windows", "platform": "Darwin", "windows": windowRecords(), "driver": "mac_swift_host"])
    case "computer.activate_app", "activate_app", "activate":
        activateApp(args: args)
    case "computer.screenshot", "screenshot":
        captureScreenshot(args: args)
    case "computer.observe", "observe":
        observe(args: args)
    case "computer.ax_tree", "ax_tree":
        axTree(args: args)
    case "computer.ocr", "ocr":
        ocr(args: args)
    case "computer.move", "move":
        move(args: args)
    case "computer.click", "click":
        click(args: args)
    case "computer.semantic_action", "semantic_action":
        semanticAction(args: args)
    case "computer.click_text", "click_text":
        semanticAction(args: args, actionName: "computer.click_text")
    case "computer.drag", "drag":
        drag(args: args)
    case "computer.type", "type":
        typeText(args: args)
    case "computer.probe_text_control", "probe_text_control":
        probeSemanticTextControl(args: args)
    case "computer.set_text_control", "set_text_control":
        setSemanticTextControl(args: args)
    case "computer.key", "key":
        key(args: args)
    case "computer.scroll", "scroll":
        scroll(args: args)
    case "computer.clipboard.read", "clipboard", "clipboard_read":
        clipboardRead()
    case "computer.clipboard.write", "clipboard_write":
        clipboardWrite(args: args)
    case "computer.clipboard.clear", "clipboard_clear":
        clipboardClear()
    default:
        fail("UNSUPPORTED_ACTION", "Unsupported macOS computer action: \(action)")
    }
} catch {
    fail("HOST_EXCEPTION", String(describing: error))
}
