from __future__ import annotations

import os
import platform
import shutil
import struct
import zlib
from html import escape
from pathlib import Path
from typing import Any

from domain.ui_compiler import CandidateBundle, RenderMatrix, RenderSnapshot, UICompilerArtifactStore

from .candidate_generator import read_candidate_manifest
from .project_writer import write_json, write_text
from domain.tool.schema_adapter import list_or_empty, mapping_or_empty

_BROWSER_TIMEOUT_MS = 10_000
_BROWSER_SANDBOX_MODE = "enabled"


class RenderMatrixRunner:
    def __init__(self, *, store: UICompilerArtifactStore) -> None:
        self.store = store

    def render_candidate(
        self,
        *,
        run_id: str,
        bundle: CandidateBundle,
        viewports: list[int],
        scenarios: list[str],
        text_scales: list[float],
        browser_render: bool = False,
    ) -> RenderMatrix:
        root = Path(bundle.root)
        manifest = read_candidate_manifest(root)
        snapshots = self._render_subject(
            root=root / "renders",
            subject_id=bundle.node_id,
            candidate_id=bundle.candidate_id,
            manifest=manifest,
            viewports=viewports,
            scenarios=scenarios,
            text_scales=text_scales,
            browser_render=browser_render,
        )
        matrix = RenderMatrix(subject_id=bundle.node_id, candidate_id=bundle.candidate_id, snapshots=snapshots)
        self.store.save_render_matrix(
            run_id=run_id,
            subject_kind="candidate",
            subject_id=bundle.node_id,
            candidate_id=bundle.candidate_id,
            matrix=matrix.to_dict(),
        )
        return matrix

    def render_page(
        self,
        *,
        run_id: str,
        run_root: Path,
        manifest: dict[str, Any],
        viewports: list[int],
        scenarios: list[str],
        text_scales: list[float],
        browser_render: bool = False,
    ) -> RenderMatrix:
        snapshots = self._render_subject(
            root=run_root / "renders" / "page",
            subject_id="page",
            candidate_id="composition",
            manifest=manifest,
            viewports=viewports,
            scenarios=scenarios,
            text_scales=text_scales,
            browser_render=browser_render,
        )
        matrix = RenderMatrix(subject_id="page", candidate_id="composition", snapshots=snapshots)
        self.store.save_render_matrix(
            run_id=run_id,
            subject_kind="page",
            subject_id="page",
            candidate_id="composition",
            matrix=matrix.to_dict(),
        )
        return matrix

    def _render_subject(
        self,
        *,
        root: Path,
        subject_id: str,
        candidate_id: str,
        manifest: dict[str, Any],
        viewports: list[int],
        scenarios: list[str],
        text_scales: list[float],
        browser_render: bool,
    ) -> list[RenderSnapshot]:
        snapshots: list[RenderSnapshot] = []
        browser_executable_path = _browser_executable_path() if browser_render else ""
        visible_actions = int(manifest.get("visibleActionCount") or min(2, int(manifest.get("visibleActionBudget") or 2)))
        for viewport in viewports:
            for scenario in scenarios:
                for text_scale in text_scales:
                    key = _snapshot_key(viewport, scenario, text_scale)
                    metrics = _metrics(
                        viewport=viewport,
                        scenario=scenario,
                        text_scale=text_scale,
                        visible_actions=visible_actions,
                        manifest=manifest,
                    )
                    image_path = root / f"{key}.png"
                    dom_path = root / f"dom-{key}.json"
                    console_path = root / f"console-{key}.json"
                    html_path = root / f"{key}.html"
                    write_text(
                        html_path,
                        _html(
                            subject_id=subject_id,
                            candidate_id=candidate_id,
                            manifest=manifest,
                            metrics=metrics,
                        ),
                    )
                    captured_with_browser = False
                    if browser_render:
                        captured_with_browser = _capture_with_browser(
                            html_path=html_path,
                            image_path=image_path,
                            dom_path=dom_path,
                            console_path=console_path,
                            viewport=viewport,
                            text_scale=text_scale,
                            subject_id=subject_id,
                            candidate_id=candidate_id,
                            metrics=metrics,
                            executable_path=browser_executable_path,
                        )
                    if captured_with_browser:
                        pass
                    else:
                        metrics["renderer"] = "synthetic"
                        metrics["browserRenderRequested"] = bool(browser_render)
                        if browser_render:
                            metrics["browserRenderFallback"] = True
                            metrics.setdefault(
                                "browserRenderFallbackReason",
                                "browser renderer unavailable or capture failed",
                            )
                        _write_png(image_path, width=min(max(viewport, 120), 720), height=180, seed=subject_id + candidate_id)
                        write_json(
                            dom_path,
                            {
                                "subjectId": subject_id,
                                "candidateId": candidate_id,
                                "renderer": "synthetic",
                                "metrics": metrics,
                            },
                        )
                        write_json(console_path, {"renderer": "synthetic", "errors": []})
                    snapshots.append(
                        RenderSnapshot(
                            subject_id=subject_id,
                            candidate_id=candidate_id,
                            viewport=viewport,
                            scenario=scenario,
                            text_scale=text_scale,
                            image_path=str(image_path),
                            dom_path=str(dom_path),
                            console_path=str(console_path),
                            metrics=metrics,
                        )
                    )
        return snapshots


def _snapshot_key(viewport: int, scenario: str, text_scale: float) -> str:
    scale = str(text_scale).replace(".", "-")
    return f"{viewport}-{scenario}-text-{scale}"


def _metrics(
    *,
    viewport: int,
    scenario: str,
    text_scale: float,
    visible_actions: int,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    required_padding = 16 if viewport < 600 else 20
    action_width = visible_actions * 94 + max(0, visible_actions - 1) * 8
    content_width = max(1, viewport - required_padding * 2)
    forced_overflow = bool(manifest.get("forceHorizontalOverflow"))
    font_size = float(manifest.get("fontSize") or 14) * float(text_scale)
    line_height = float(manifest.get("lineHeight") or 20) * float(text_scale)
    actual_padding = float(manifest.get("actualPadding") or required_padding)
    actual_gap = float(manifest.get("actualGap") or 12)
    toolbar_rows = int(manifest.get("toolbarRows") or 1)
    primary_action_reachable = manifest.get("primaryActionReachable", True) is not False
    scroll_width = max(content_width, action_width)
    if forced_overflow:
        scroll_width = content_width + 48
    visible_text_blocks = int(manifest.get("visibleTextBlocks") or (6 if scenario == "long" else 3))
    visible_characters = int(
        manifest.get("visibleCharacters")
        or (920 if scenario == "long" else 240)
        + max(0, visible_actions - 2) * 36
    )
    line_clamps = int(manifest.get("lineClampCount") or (1 if scenario == "long" and manifest.get("longTextClipped") else 0))
    ellipses = int(manifest.get("ellipsisCount") or line_clamps)
    label_count = int(manifest.get("labelCount") or max(2, visible_actions + 2))
    mobile_strategy = str(manifest.get("mobileBehavior") or manifest.get("responsiveTopology") or "stack")
    return {
        "viewport": viewport,
        "scenario": scenario,
        "textScale": text_scale,
        "visibleActions": visible_actions,
        "allowedActions": int(manifest.get("visibleActionBudget") or 3),
        "contentWidth": content_width,
        "scrollWidth": scroll_width,
        "horizontalOverflow": scroll_width > content_width,
        "minPadding": required_padding,
        "actualPadding": actual_padding,
        "minGap": 12,
        "actualGap": actual_gap,
        "lineHeight": line_height,
        "fontSize": font_size,
        "surfaceDepth": int(manifest.get("surfaceDepth") or 1),
        "shadowCount": int(manifest.get("shadowCount") or 0),
        "dividerCount": int(manifest.get("dividerCount") or 0),
        "hierarchyContrast": float(manifest.get("hierarchyContrast") or 0.65),
        "toolbarRows": toolbar_rows,
        "primaryActionReachable": primary_action_reachable,
        "consoleErrors": 0,
        "primaryClipped": bool(manifest.get("forcePrimaryClipped")) or (scenario == "long" and bool(manifest.get("longTextClipped"))),
        "touchTargetMin": int(manifest.get("touchTargetMin") or 36),
        "requiredStates": list_or_empty(manifest.get("requiredStates")),
        "visibleTextBlocks": visible_text_blocks,
        "visibleCharacters": visible_characters,
        "averageLineLength": int(manifest.get("averageLineLength") or min(84, max(28, visible_characters // max(visible_text_blocks, 1)))),
        "lineClampCount": line_clamps,
        "ellipsisCount": ellipses,
        "repeatedMetadataLines": int(manifest.get("repeatedMetadataLines") or 0),
        "japaneseBreakQuality": float(manifest.get("japaneseBreakQuality") or 0.9),
        "labelDensity": round(label_count / max(1, visible_text_blocks), 3),
        "gradientCount": int(manifest.get("gradientCount") or 0),
        "nonSemanticColorCount": int(manifest.get("nonSemanticColorCount") or 0),
        "mutedTextRatio": float(manifest.get("mutedTextRatio") or 0.25),
        "borderCount": int(manifest.get("borderCount") or max(1, int(manifest.get("dividerCount") or 0) + 1)),
        "cardCount": int(manifest.get("cardCount") or max(0, int(manifest.get("surfaceDepth") or 1) - 1)),
        "cardNestingDepth": int(manifest.get("cardNestingDepth") or int(manifest.get("surfaceDepth") or 1)),
        "radiusUniformity": float(manifest.get("radiusUniformity") or 0.45),
        "mobileBehavior": mobile_strategy,
        "desktopColumns": int(manifest.get("desktopColumns") or (2 if viewport >= 768 else 1)),
        "mobileDisclosureUsed": bool(manifest.get("mobileDisclosureUsed") or (viewport <= 390 and mobile_strategy in {"route", "sheet", "drawer", "stack"})),
        "focusVisible": manifest.get("focusVisible", True) is not False,
        "ariaRoles": int(manifest.get("ariaRoles") or 2),
        "contrastMin": float(manifest.get("contrastMin") or 4.8),
        "keyboardNav": manifest.get("keyboardNav", True) is not False,
    }


def _html(*, subject_id: str, candidate_id: str, manifest: dict[str, Any], metrics: dict[str, Any]) -> str:
    title = escape(subject_id.replace("-", " ").title())
    mode = escape(str(manifest.get("implementationMode", "component")))
    action_count = int(metrics.get("visibleActions") or 1)
    surface_depth = int(metrics.get("surfaceDepth") or 1)
    gap = int(float(metrics.get("actualGap") or 12))
    padding = int(float(metrics.get("actualPadding") or 16))
    font_size = float(metrics.get("fontSize") or 14)
    line_height = float(metrics.get("lineHeight") or 20)
    hierarchy_contrast = float(metrics.get("hierarchyContrast") or 0.65)
    force_overflow = bool(manifest.get("forceHorizontalOverflow"))
    toolbar_rows = int(metrics.get("toolbarRows") or 1)
    actions = "".join(
        f"<button class=\"rui-action\">Action {index + 1}</button>"
        for index in range(action_count)
    )
    nested_surfaces = "".join(
        "<div class=\"rui-nested\"><span></span><span></span></div>"
        for _ in range(max(0, surface_depth - 1))
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Rumi Render</title>"
        "<style>"
        ":root{--rui-canvas:rgb(248 249 251);--rui-surface:rgb(255 255 255);"
        "--rui-text-primary:rgb(18 24 32);--rui-text-secondary:rgb(79 90 106);"
        "--rui-border-subtle:rgb(218 224 232);--rui-action-primary:rgb(28 105 212);}"
        f"body{{margin:0;background:var(--rui-canvas);font-family:system-ui,-apple-system,sans-serif;font-size:{font_size}px;line-height:{line_height / max(font_size, 1):.3f};}}"
        f".rui-root{{box-sizing:border-box;margin:18px;padding:{padding}px;display:grid;gap:{gap}px;background:var(--rui-surface);color:var(--rui-text-primary);border:1px solid var(--rui-border-subtle);border-radius:6px;min-width:{'calc(100vw + 48px)' if force_overflow else '0'};}}"
        ".rui-title{font-size:18px;font-weight:650;margin:0;color:var(--rui-text-primary)}"
        f".rui-copy{{margin:0;color:rgba(79,90,106,{max(0.2, min(hierarchy_contrast, 1))});max-width:70ch;}}"
        f".rui-actions{{display:flex;flex-wrap:wrap;gap:8px;align-content:flex-start;max-height:{toolbar_rows * 44}px;overflow:hidden;}}"
        ".rui-action{min-height:36px;padding:0 12px;border:0;border-radius:6px;background:var(--rui-action-primary);color:var(--rui-surface);font:inherit;}"
        ".rui-nested{border:1px solid var(--rui-border-subtle);padding:10px;display:grid;gap:8px;background:rgb(252 253 254)}"
        ".rui-nested span{display:block;height:8px;background:var(--rui-border-subtle);border-radius:999px}"
        "</style></head>"
        "<body>"
        f"<main class=\"rui-root\" data-subject=\"{escape(subject_id)}\" data-candidate=\"{escape(candidate_id)}\">"
        f"<h1 class=\"rui-title\">{title}</h1>"
        f"<p class=\"rui-copy\">{mode}: 日本語の長文、数値 12,345、状態差分、主要アクションが読み取れるかを検査します。</p>"
        f"<div class=\"rui-actions\">{actions}</div>"
        f"{nested_surfaces}"
        "</main></body></html>"
    )


def _capture_with_browser(
    *,
    html_path: Path,
    image_path: Path,
    dom_path: Path,
    console_path: Path,
    viewport: int,
    text_scale: float,
    subject_id: str,
    candidate_id: str,
    metrics: dict[str, Any],
    executable_path: str = "",
) -> bool:
    _set_browser_runtime_metrics(metrics, executable_path)
    if not executable_path:
        metrics["browserRenderFallbackReason"] = "local Chromium executable not found"
        return False
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        metrics["browserRenderFallbackReason"] = "optional Playwright Python package unavailable"
        return False
    image_path.parent.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    browser_version = ""
    browser = None
    context = None
    cleanup_errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=executable_path,
                timeout=_BROWSER_TIMEOUT_MS,
                chromium_sandbox=True,
            )
            try:
                browser_version = str(browser.version or "")
                context = browser.new_context(
                    viewport={"width": int(viewport), "height": 260},
                    device_scale_factor=1,
                )
                page = context.new_page()
                page.set_default_timeout(_BROWSER_TIMEOUT_MS)
                page.set_default_navigation_timeout(_BROWSER_TIMEOUT_MS)
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.goto(
                    html_path.resolve().as_uri(),
                    wait_until="load",
                    timeout=_BROWSER_TIMEOUT_MS,
                )
                page.evaluate(
                    "(scale) => { document.documentElement.style.fontSize = `${16 * scale}px`; }",
                    float(text_scale),
                )
                dom = page.evaluate(
                    """() => {
                        const root = document.querySelector('[data-subject]');
                        const actions = Array.from(document.querySelectorAll('button'));
                        const rect = root ? root.getBoundingClientRect() : null;
                        return {
                            document: {
                                scrollWidth: document.documentElement.scrollWidth,
                                clientWidth: document.documentElement.clientWidth,
                                scrollHeight: document.documentElement.scrollHeight,
                                clientHeight: document.documentElement.clientHeight
                            },
                            root: rect ? {
                                x: rect.x, y: rect.y, width: rect.width, height: rect.height
                            } : null,
                            actions: actions.map((item) => {
                                const box = item.getBoundingClientRect();
                                return { text: item.textContent, x: box.x, y: box.y, width: box.width, height: box.height };
                            })
                        };
                    }"""
                )
                page.screenshot(
                    path=str(image_path),
                    full_page=True,
                    timeout=_BROWSER_TIMEOUT_MS,
                )
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception as exc:
                        cleanup_errors.append(f"context cleanup: {type(exc).__name__}: {exc}")
                    context = None
                if browser is not None:
                    try:
                        browser.close()
                    except Exception as exc:
                        cleanup_errors.append(f"browser cleanup: {type(exc).__name__}: {exc}")
                    browser = None
                metrics["browserCleanup"] = (
                    "cleanup-error" if cleanup_errors else "context-and-browser-closed"
                )
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
    except Exception as exc:
        metrics["browserRenderFallbackReason"] = "browser launch or capture failed"
        metrics["browserRenderError"] = f"{type(exc).__name__}: {exc}"
        write_json(console_path, {"renderer": "playwright", "errors": [str(exc)]})
        return False
    browser_metrics = dict(metrics)
    browser_metrics["renderer"] = "playwright"
    browser_metrics["browserRenderRequested"] = True
    browser_metrics["browserRenderFallback"] = False
    browser_metrics["browserVersion"] = browser_version
    document = mapping_or_empty(dom.get("document") if isinstance(dom, dict) else None)
    browser_metrics["scrollWidth"] = int(document.get("scrollWidth") or browser_metrics.get("scrollWidth") or 0)
    browser_metrics["contentWidth"] = int(document.get("clientWidth") or browser_metrics.get("contentWidth") or 0)
    browser_metrics["horizontalOverflow"] = browser_metrics["scrollWidth"] > browser_metrics["contentWidth"]
    browser_metrics["consoleErrors"] = len(console_errors)
    write_json(
        dom_path,
        {
            "subjectId": subject_id,
            "candidateId": candidate_id,
            "renderer": "playwright",
            "metrics": browser_metrics,
            "dom": dom,
        },
    )
    write_json(console_path, {"renderer": "playwright", "errors": console_errors})
    metrics.update(browser_metrics)
    return True


def _browser_executable_path() -> str:
    """Find a local Chromium executable without downloading or starting a browser."""

    for candidate in _system_browser_candidates():
        executable_path = _validated_executable_path(candidate)
        if executable_path:
            return executable_path
    return _playwright_browser_executable_path()


def _system_browser_candidates() -> list[str]:
    """Return deterministic system browser candidates for the current platform."""

    system = platform.system().lower()
    candidates: list[str] = []
    path_names: tuple[str, ...]
    if system == "darwin":
        app_bundles = (
            ("Google Chrome.app", "Google Chrome"),
            ("Chromium.app", "Chromium"),
            ("Microsoft Edge.app", "Microsoft Edge"),
        )
        for applications_root in (Path("/Applications"), Path.home() / "Applications"):
            for bundle, executable in app_bundles:
                candidates.append(str(applications_root / bundle / "Contents" / "MacOS" / executable))
        path_names = ("google-chrome", "chromium", "microsoft-edge")
    elif system == "windows":
        windows_roots = (
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMW6432"),
            os.environ.get("LOCALAPPDATA"),
            str(Path.home() / "AppData" / "Local"),
        )
        application_paths = (
            ("Google", "Chrome", "Application", "chrome.exe"),
            ("Chromium", "Application", "chrome.exe"),
            ("Microsoft", "Edge", "Application", "msedge.exe"),
        )
        for root in windows_roots:
            if not root:
                continue
            for application_path in application_paths:
                candidates.append(str(Path(root).joinpath(*application_path)))
        path_names = ("chrome.exe", "chromium.exe", "msedge.exe")
    else:
        candidates.extend(
            (
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
                "/snap/bin/chromium",
            )
        )
        path_names = (
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "microsoft-edge-stable",
        )
    candidates.extend(path for name in path_names if (path := shutil.which(name)))
    return candidates


def _validated_executable_path(candidate: str) -> str:
    """Return a resolved executable file path, or an empty string if unavailable."""

    try:
        path = Path(candidate).expanduser()
        if not path.is_file():
            return ""
        if platform.system().lower() != "windows" and not os.access(path, os.X_OK):
            return ""
        return str(path.resolve())
    except (OSError, RuntimeError, TypeError):
        return ""


def _playwright_browser_executable_path() -> str:
    """Find an already-installed Playwright Chromium binary without network access."""

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""
    try:
        with sync_playwright() as playwright:
            return _validated_executable_path(str(playwright.chromium.executable_path))
    except Exception:
        return ""


def _browser_executable_source(executable_path: str) -> str:
    """Describe whether a browser path came from the system or a Playwright cache."""

    normalized = executable_path.replace("\\", "/").lower()
    if "playwright" in normalized or "ms-playwright" in normalized:
        return "playwright-cache"
    return "system"


def _set_browser_runtime_metrics(metrics: dict[str, Any], executable_path: str) -> None:
    """Record browser discovery and launch policy for an auditable render snapshot."""

    metrics["browserExecutablePath"] = executable_path or None
    metrics["browserExecutableSource"] = (
        _browser_executable_source(executable_path) if executable_path else "unavailable"
    )
    metrics["browserTimeoutMs"] = _BROWSER_TIMEOUT_MS
    metrics["browserSandbox"] = _BROWSER_SANDBOX_MODE
    metrics["browserCleanup"] = "not-started"


def _write_png(path: Path, *, width: int, height: int, seed: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    accent = ((sum(ord(char) for char in seed) * 17) % 110 + 48, 104, 190)
    canvas = (248, 249, 251)
    border = (218, 224, 232)
    text = (32, 40, 52)
    muted = (130, 142, 158)
    surface = (255, 255, 255)
    pixels = [[canvas for _ in range(width)] for _ in range(height)]

    def rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        for yy in range(max(0, y0), min(height, y1)):
            row = pixels[yy]
            for xx in range(max(0, x0), min(width, x1)):
                row[xx] = color

    def outline(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int]) -> None:
        rect(x0, y0, x1, y0 + 1, color)
        rect(x0, y1 - 1, x1, y1, color)
        rect(x0, y0, x0 + 1, y1, color)
        rect(x1 - 1, y0, x1, y1, color)

    gutter = 16 if width < 500 else 24
    rect(gutter, 18, width - gutter, height - 18, surface)
    outline(gutter, 18, width - gutter, height - 18, border)
    rect(gutter + 14, 34, min(width - gutter - 14, gutter + 170), 42, text)
    rect(gutter + 14, 52, min(width - gutter - 14, gutter + 250), 58, muted)
    rect(width - gutter - 112, 34, width - gutter - 18, 58, accent)
    if width >= 600:
        side = gutter + 190
        rect(gutter + 14, 76, side, height - 34, (244, 247, 250))
        outline(gutter + 14, 76, side, height - 34, border)
        for index in range(4):
            y = 92 + index * 22
            rect(gutter + 28, y, side - 22, y + 8, muted if index else accent)
        rect(side + 18, 76, width - gutter - 18, height - 34, (252, 253, 254))
        outline(side + 18, 76, width - gutter - 18, height - 34, border)
        for index in range(5):
            y = 94 + index * 18
            rect(side + 34, y, width - gutter - 52, y + 7, text if index == 0 else muted)
    else:
        for index in range(4):
            y = 78 + index * 20
            rect(gutter + 14, y, width - gutter - 14, y + 8, text if index == 0 else muted)
        rect(gutter + 14, height - 54, width - gutter - 14, height - 32, accent)

    rows = []
    for row_pixels in pixels:
        row = bytearray()
        for pixel in row_pixels:
            row.extend(pixel)
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(raw))
    png += _chunk(b"IEND", b"")
    path.write_bytes(png)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
